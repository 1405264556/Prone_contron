import csv
import json
import os
import queue
import socket
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import filedialog, messagebox, ttk


COMMON_TCP_PORTS = [
    21, 22, 23, 53, 80, 81, 82, 88, 443, 554, 1935, 5000, 5555,
    6666, 7070, 8000, 8080, 8081, 8088, 8888, 8889, 8890, 8895,
    9000, 10000, 11111, 20000, 40000, 7060,
]

COMMON_UDP_PORTS = [
    5000, 5555, 7070, 8080, 8088, 8888, 8889, 8890, 8895,
    9000, 10000, 11111, 20000, 40000,
]

WIFI_UFO_HEARTBEAT = bytes.fromhex("63 63 01 00 00 00 00")

APP_DIR = os.path.dirname(os.path.abspath(__file__))
RUNTIME_DIR = os.path.join(APP_DIR, "runtime")
LOG_DIR = os.path.join(RUNTIME_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

TELLO_STATE_KEYS = {
    "pitch", "roll", "yaw", "vgx", "vgy", "vgz", "templ", "temph", "tof",
    "h", "bat", "baro", "time", "agx", "agy", "agz",
}


@dataclass
class SensorPacket:
    timestamp: float
    source: str
    raw: bytes
    parsed: dict


def parse_sensor_payload(data: bytes) -> dict:
    if data.startswith(b"cc"):
        packet_type = data[2] if len(data) > 2 else None
        if packet_type == 1:
            ssid = ""
            ssid_bytes = data[7:71].split(b"\x00", 1)[0]
            ssid = ssid_bytes.decode("ascii", errors="replace")
            return {
                "format": "wifi_ufo_info",
                "packet_type": packet_type,
                "ssid": ssid,
                "prefix_hex": data[:80].hex(),
                "length": len(data),
            }
        if packet_type == 3:
            return {
                "format": "wifi_ufo_video",
                "packet_type": packet_type,
                "jpeg_soi_offset": data.find(b"\xff\xd8"),
                "prefix_hex": data[:32].hex(),
                "length": len(data),
            }
        return {
            "format": "wifi_ufo",
            "packet_type": packet_type,
            "prefix_hex": data[:80].hex(),
            "length": len(data),
        }

    text = data.decode("utf-8", errors="replace").strip()
    if not text:
        return {"format": "empty"}

    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return {"format": "json", **obj}
    except json.JSONDecodeError:
        pass

    fields = {}
    if ":" in text and ";" in text:
        for item in text.split(";"):
            if ":" not in item:
                continue
            key, value = item.split(":", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                continue
            try:
                fields[key] = float(value) if "." in value else int(value)
            except ValueError:
                fields[key] = value
        if fields:
            looks_like_tello = bool(TELLO_STATE_KEYS.intersection(fields))
            return {"format": "tello_state" if looks_like_tello else "key_value", **fields}

    return {
        "format": "text",
        "text": text,
        "hex": data[:128].hex(),
        "length": len(data),
    }


class UdpListener(threading.Thread):
    def __init__(self, bind_port, packet_queue, stop_event):
        super().__init__(daemon=True)
        self.bind_port = bind_port
        self.packet_queue = packet_queue
        self.stop_event = stop_event
        self.sock = None

    def run(self):
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind(("0.0.0.0", self.bind_port))
            self.sock.settimeout(0.25)
            self.packet_queue.put(("log", f"UDP listener started on 0.0.0.0:{self.bind_port}"))
            while not self.stop_event.is_set():
                try:
                    data, addr = self.sock.recvfrom(65535)
                except socket.timeout:
                    continue
                packet = SensorPacket(
                    timestamp=time.time(),
                    source=f"{addr[0]}:{addr[1]}",
                    raw=data,
                    parsed=parse_sensor_payload(data),
                )
                self.packet_queue.put(("packet", packet))
        except OSError as exc:
            self.packet_queue.put(("error", f"UDP listener error on {self.bind_port}: {exc}"))
        finally:
            if self.sock:
                self.sock.close()


class UdpEndpoint(threading.Thread):
    def __init__(self, bind_port, packet_queue, stop_event):
        super().__init__(daemon=True)
        self.bind_port = bind_port
        self.packet_queue = packet_queue
        self.stop_event = stop_event
        self.sock = None
        self.ready = threading.Event()

    def run(self):
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind(("0.0.0.0", self.bind_port))
            self.sock.settimeout(0.25)
            self.ready.set()
            self.packet_queue.put(("log", f"Command UDP socket opened on 0.0.0.0:{self.bind_port}"))
            while not self.stop_event.is_set():
                try:
                    data, addr = self.sock.recvfrom(65535)
                except socket.timeout:
                    continue
                packet = SensorPacket(
                    timestamp=time.time(),
                    source=f"{addr[0]}:{addr[1]}",
                    raw=data,
                    parsed=parse_sensor_payload(data),
                )
                self.packet_queue.put(("packet", packet))
        except OSError as exc:
            self.ready.set()
            self.packet_queue.put(("error", f"Command UDP socket error on {self.bind_port}: {exc}"))
        finally:
            if self.sock:
                self.sock.close()

    def send(self, host, port, payload):
        if not self.sock:
            raise OSError("Command UDP socket is not open")
        self.sock.sendto(payload, (host, port))


class GroundStation(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Drone Ground Station")
        self.geometry("980x680")
        self.minsize(900, 600)

        self.packet_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.command_endpoint = None
        self.state_listener = None
        self.packets = []
        self.video_packet_count = 0
        self.session_log_path = os.path.join(
            LOG_DIR,
            f"ground_station_{time.strftime('%Y%m%d_%H%M%S')}.log",
        )

        self.host_var = tk.StringVar(value="192.168.0.1")
        self.command_port_var = tk.IntVar(value=8889)
        self.listen_port_var = tk.IntVar(value=8890)
        self.profile_var = tk.StringVar(value="Tello-compatible UDP")
        self.command_var = tk.StringVar(value="battery?")
        self.allow_flight_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Idle")

        self._build_ui()
        self.after(100, self._drain_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        outer = ttk.Frame(self, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        connection = ttk.LabelFrame(outer, text="Connection")
        connection.grid(row=0, column=0, sticky="ew")
        for i in range(9):
            connection.columnconfigure(i, weight=0)
        connection.columnconfigure(8, weight=1)

        ttk.Label(connection, text="Drone IP").grid(row=0, column=0, padx=4, pady=6)
        ttk.Entry(connection, textvariable=self.host_var, width=16).grid(row=0, column=1, padx=4)
        ttk.Label(connection, text="Command UDP").grid(row=0, column=2, padx=4)
        ttk.Entry(connection, textvariable=self.command_port_var, width=7).grid(row=0, column=3, padx=4)
        ttk.Label(connection, text="Listen UDP").grid(row=0, column=4, padx=4)
        ttk.Entry(connection, textvariable=self.listen_port_var, width=7).grid(row=0, column=5, padx=4)
        profile_combo = ttk.Combobox(
            connection,
            textvariable=self.profile_var,
            values=["Tello-compatible UDP", "WiFi UFO UDP", "Generic UDP"],
            width=22,
            state="readonly",
        )
        profile_combo.grid(row=0, column=6, padx=4)
        profile_combo.bind("<<ComboboxSelected>>", self.apply_profile)
        ttk.Button(connection, text="Probe TCP", command=self.probe_tcp).grid(row=0, column=7, padx=4)
        ttk.Button(connection, text="Probe UDP", command=self.probe_udp).grid(row=0, column=8, padx=4)
        ttk.Label(connection, textvariable=self.status_var).grid(row=0, column=9, padx=8, sticky="w")

        controls = ttk.LabelFrame(outer, text="Control")
        controls.grid(row=1, column=0, sticky="ew", pady=(8, 8))
        controls.columnconfigure(12, weight=1)

        ttk.Button(controls, text="Start Listener", command=self.start_listener).grid(row=0, column=0, padx=4, pady=6)
        ttk.Button(controls, text="Stop Listener", command=self.stop_listener).grid(row=0, column=1, padx=4)
        ttk.Button(controls, text="SDK Init", command=lambda: self.send_command("command")).grid(row=0, column=2, padx=4)
        ttk.Button(controls, text="Battery?", command=lambda: self.send_command("battery?")).grid(row=0, column=3, padx=4)
        ttk.Button(controls, text="Time?", command=lambda: self.send_command("time?")).grid(row=0, column=4, padx=4)
        ttk.Button(controls, text="UFO Ping", command=self.wifiufo_ping).grid(row=0, column=5, padx=4)
        ttk.Entry(controls, textvariable=self.command_var, width=24).grid(row=0, column=6, padx=4)
        ttk.Button(controls, text="Send", command=lambda: self.send_command(self.command_var.get())).grid(row=0, column=7, padx=4)
        ttk.Checkbutton(controls, text="Unlock flight commands", variable=self.allow_flight_var).grid(row=0, column=8, padx=12)
        ttk.Button(controls, text="Takeoff", command=lambda: self.send_command("takeoff")).grid(row=0, column=9, padx=4)
        ttk.Button(controls, text="Land", command=lambda: self.send_command("land")).grid(row=0, column=10, padx=4)
        ttk.Button(controls, text="Emergency", command=lambda: self.send_command("emergency")).grid(row=0, column=11, padx=4)
        ttk.Button(controls, text="Save CSV", command=self.save_csv).grid(row=0, column=12, padx=4)

        body = ttk.PanedWindow(outer, orient=tk.HORIZONTAL)
        body.grid(row=2, column=0, sticky="nsew")

        sensor_frame = ttk.LabelFrame(body, text="Sensor Data")
        sensor_frame.rowconfigure(0, weight=1)
        sensor_frame.columnconfigure(0, weight=1)
        self.sensor_tree = ttk.Treeview(sensor_frame, columns=("value",), show="tree headings")
        self.sensor_tree.heading("#0", text="Field")
        self.sensor_tree.heading("value", text="Value")
        self.sensor_tree.column("#0", width=170, stretch=False)
        self.sensor_tree.column("value", width=220, stretch=True)
        self.sensor_tree.grid(row=0, column=0, sticky="nsew")
        body.add(sensor_frame, weight=1)

        log_frame = ttk.LabelFrame(body, text="Log")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, wrap="word", height=20)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        body.add(log_frame, weight=2)

    def log(self, message):
        timestamp = time.strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}"
        self.log_text.insert(tk.END, f"{line}\n")
        self.log_text.see(tk.END)
        with open(self.session_log_path, "a", encoding="utf-8") as handle:
            handle.write(f"{line}\n")

    def apply_profile(self, _event=None):
        profile = self.profile_var.get()
        if profile == "Tello-compatible UDP":
            self.command_port_var.set(8889)
            self.listen_port_var.set(8890)
            self.command_var.set("battery?")
        elif profile == "WiFi UFO UDP":
            self.command_port_var.set(40000)
            self.listen_port_var.set(40000)
            self.command_var.set("63630100000000")
        self.log(f"Profile selected: {profile}")

    def probe_tcp(self):
        host = self.host_var.get().strip()
        self.status_var.set("Scanning TCP...")
        threading.Thread(target=self._probe_tcp_worker, args=(host,), daemon=True).start()

    def _probe_tcp_worker(self, host):
        open_ports = []
        for port in COMMON_TCP_PORTS:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.25)
                if sock.connect_ex((host, port)) == 0:
                    open_ports.append(port)
        self.packet_queue.put(("log", f"TCP open ports on {host}: {open_ports if open_ports else 'none'}"))
        self.packet_queue.put(("status", "Idle"))

    def probe_udp(self):
        host = self.host_var.get().strip()
        self.status_var.set("Probing UDP...")
        threading.Thread(target=self._probe_udp_worker, args=(host,), daemon=True).start()

    def _probe_udp_worker(self, host):
        probes = [b"command", b"battery?", b"time?", b"status?", b"info?"]
        found = []
        self.packet_queue.put(("log", f"UDP probe started for {host}"))
        for port in COMMON_UDP_PORTS:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    sock.settimeout(0.3)
                    sock.bind(("0.0.0.0", 0))
                    for payload in probes:
                        sock.sendto(payload, (host, port))
                        try:
                            data, addr = sock.recvfrom(2048)
                        except socket.timeout:
                            continue
                        packet = SensorPacket(
                            timestamp=time.time(),
                            source=f"{addr[0]}:{addr[1]}",
                            raw=data,
                            parsed=parse_sensor_payload(data),
                        )
                        found.append((port, payload.decode("ascii", errors="replace"), packet.parsed))
                        self.packet_queue.put(("packet", packet))
                        break
            except OSError as exc:
                self.packet_queue.put(("log", f"UDP probe port {port} error: {exc}"))
        self.packet_queue.put(("log", f"UDP probe results: {found if found else 'no replies'}"))
        self.packet_queue.put(("status", "Listening" if self._any_udp_running() else "Idle"))

    def start_listener(self):
        self.stop_event.clear()
        if self.command_endpoint and self.command_endpoint.is_alive():
            self.log("Command UDP socket is already running")
        else:
            self.command_endpoint = UdpEndpoint(self.command_port_var.get(), self.packet_queue, self.stop_event)
            self.command_endpoint.start()
            self.command_endpoint.ready.wait(timeout=1)
        if self.state_listener and self.state_listener.is_alive():
            self.log("State listener is already running")
            self.status_var.set("Listening")
            return
        if self.listen_port_var.get() == self.command_port_var.get():
            self.log("State listener skipped because it shares the command UDP port")
            self.status_var.set("Listening")
            return
        self.state_listener = UdpListener(self.listen_port_var.get(), self.packet_queue, self.stop_event)
        self.state_listener.start()
        self.status_var.set("Listening")

    def stop_listener(self):
        self.stop_event.set()
        self.status_var.set("Idle")
        self.log("UDP sockets stopping")

    def _any_udp_running(self):
        return (
            self.command_endpoint and self.command_endpoint.is_alive()
        ) or (
            self.state_listener and self.state_listener.is_alive()
        )

    def send_command(self, command):
        command = command.strip()
        if not command:
            return
        dangerous = command.split()[0].lower() in {"takeoff", "land", "emergency", "up", "down", "left", "right", "forward", "back", "cw", "ccw", "flip", "go", "curve", "rc"}
        if dangerous and not self.allow_flight_var.get():
            messagebox.showwarning(
                "Flight command locked",
                "Enable 'Unlock flight commands' before sending motion, takeoff, land, or emergency commands.",
            )
            return
        try:
            if self.profile_var.get() == "WiFi UFO UDP" and all(char in "0123456789abcdefABCDEF " for char in command):
                payload = bytes.fromhex(command)
                self.send_payload(payload, f"hex:{payload.hex()}")
                return
        except ValueError:
            self.log(f"Invalid hex command: {command!r}")
            return
        self.send_payload(command.encode("utf-8"), repr(command))

    def wifiufo_ping(self):
        if self.profile_var.get() != "WiFi UFO UDP":
            self.profile_var.set("WiFi UFO UDP")
            self.apply_profile()
        self.send_payload(WIFI_UFO_HEARTBEAT, f"WiFi UFO heartbeat {WIFI_UFO_HEARTBEAT.hex()}")

    def send_payload(self, payload, label):
        host = self.host_var.get().strip()
        port = self.command_port_var.get()
        try:
            if self.stop_event.is_set():
                self.stop_event.clear()
            if not self.command_endpoint or not self.command_endpoint.is_alive():
                self.command_endpoint = UdpEndpoint(self.command_port_var.get(), self.packet_queue, self.stop_event)
                self.command_endpoint.start()
                self.command_endpoint.ready.wait(timeout=1)
            self.command_endpoint.send(host, port, payload)
            self.log(f"TX {host}:{port} {label}")
        except OSError as exc:
            self.log(f"TX error: {exc}")

    def _drain_queue(self):
        while True:
            try:
                kind, payload = self.packet_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "packet":
                if payload.parsed.get("format") != "wifi_ufo_video":
                    self.packets.append(payload)
                self._show_packet(payload)
            elif kind == "status":
                self.status_var.set(payload)
            elif kind == "error":
                self.status_var.set("Error")
                self.log(payload)
            else:
                self.log(str(payload))
        self.after(100, self._drain_queue)

    def _show_packet(self, packet):
        if packet.parsed.get("format") == "wifi_ufo_video":
            self.video_packet_count += 1
            if self.video_packet_count == 1 or self.video_packet_count % 30 == 0:
                self.log(f"RX video {packet.source} packets={self.video_packet_count} len={len(packet.raw)}")
        else:
            self.log(f"RX {packet.source} len={len(packet.raw)} {packet.parsed}")
        for item in self.sensor_tree.get_children():
            self.sensor_tree.delete(item)
        self.sensor_tree.insert("", "end", text="source", values=(packet.source,))
        self.sensor_tree.insert("", "end", text="timestamp", values=(time.strftime("%H:%M:%S", time.localtime(packet.timestamp)),))
        for key, value in packet.parsed.items():
            self.sensor_tree.insert("", "end", text=str(key), values=(str(value),))

    def save_csv(self):
        if not self.packets:
            messagebox.showinfo("No data", "No packets have been received yet.")
            return
        path = filedialog.asksaveasfilename(
            initialdir=LOG_DIR,
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not path:
            return
        keys = sorted({key for packet in self.packets for key in packet.parsed})
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["timestamp", "source", "raw_hex", *keys])
            writer.writeheader()
            for packet in self.packets:
                row = {
                    "timestamp": packet.timestamp,
                    "source": packet.source,
                    "raw_hex": packet.raw.hex(),
                    **packet.parsed,
                }
                writer.writerow(row)
        self.log(f"Saved CSV: {path}")

    def _on_close(self):
        self.stop_event.set()
        self.destroy()


if __name__ == "__main__":
    GroundStation().mainloop()
