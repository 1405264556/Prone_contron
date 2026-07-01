from __future__ import annotations

import queue
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QByteArray, QDateTime, QThread, Qt, Signal, Slot
from PySide6.QtGui import QFont, QImage, QKeyEvent, QPixmap
from PySide6.QtSerialPort import QSerialPort, QSerialPortInfo
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)


PROJECT_DIR = Path(__file__).resolve().parents[2]
RUNTIME_DIR = PROJECT_DIR / "runtime"
QT_LOG_DIR = RUNTIME_DIR / "qt6_logs"
QT_LOG_DIR.mkdir(parents=True, exist_ok=True)

WIFI_UFO_HEARTBEAT = bytes.fromhex("63 63 01 00 00 00 00")
WIFI_UFO_CONTROL_TEMPLATE = bytearray.fromhex("63 63 0a 00 00 08 00 66 80 80 80 80 00 00 99")
VIDEO_FRAGMENT_HEADER_OFFSET = 47
VIDEO_PAYLOAD_OFFSET = 54
CONTROL_INTERVAL = 0.05


def clamp_byte(value: int) -> int:
    return max(0, min(255, int(value)))


def card_label(text: str, accent: str = "#f6f8fb") -> QLabel:
    label = QLabel(text)
    label.setAlignment(Qt.AlignCenter)
    label.setMinimumHeight(30)
    label.setStyleSheet(
        f"QLabel {{ background:{accent}; border:1px solid #d2dbe8; border-radius:4px; padding:6px; }}"
    )
    return label


@dataclass
class ControlState:
    roll: int = 128
    pitch: int = 128
    throttle: int = 128
    yaw: int = 128

    def clamped(self) -> "ControlState":
        return ControlState(
            clamp_byte(self.roll),
            clamp_byte(self.pitch),
            clamp_byte(self.throttle),
            clamp_byte(self.yaw),
        )


class WifiUfoWorker(QThread):
    log_message = Signal(str)
    status_changed = Signal(str)
    info_received = Signal(dict)
    video_frame = Signal(QImage)
    video_stats = Signal(int, float, int, int)
    control_packet_sent = Signal(str)

    def __init__(self, host: str, remote_port: int, local_port: int) -> None:
        super().__init__()
        self.host = host
        self.remote_port = remote_port
        self.local_port = local_port
        self.commands: queue.Queue[tuple] = queue.Queue()
        self.stop_event = threading.Event()
        self.control = ControlState()
        self.control_stream = False
        self.next_control_at = 0.0
        self.burst_until = 0.0
        self.burst_mode = 0
        self.burst_label = ""
        self.nudge_until = 0.0
        self.nudge_state = ControlState()
        self.nudge_label = ""

        self.frame_buffer = bytearray()
        self.frame_open = False
        self.frame_key: bytes | None = None
        self.expected_fragment = 1
        self.video_packets = 0
        self.video_frames = 0
        self.bad_frames = 0
        self.video_bytes = 0
        self.stats_start = time.monotonic()
        self.last_frame_emit = 0.0
        self.last_size: tuple[int, int] | None = None

    def enqueue(self, command: tuple) -> None:
        self.commands.put(command)

    def stop(self) -> None:
        self.stop_event.set()
        self.commands.put(("stop",))

    def run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", self.local_port))
            sock.setblocking(False)
            self.status_changed.emit("UDP 已连接")
            self.log_message.emit(f"UDP 已打开：本地 0.0.0.0:{self.local_port} -> 无人机 {self.host}:{self.remote_port}")

            while not self.stop_event.is_set():
                self._drain_commands(sock)
                self._read_socket(sock)
                self._tick_control(sock)
                time.sleep(0.004)
        except OSError as exc:
            self.status_changed.emit("UDP 错误")
            self.log_message.emit(f"UDP 线程错误：{exc}")
        finally:
            try:
                sock.close()
            except OSError:
                pass
            self.status_changed.emit("UDP 已断开")

    def _drain_commands(self, sock: socket.socket) -> None:
        while True:
            try:
                command = self.commands.get_nowait()
            except queue.Empty:
                return

            name = command[0]
            if name == "stop":
                self.stop_event.set()
                return
            if name == "heartbeat":
                self._send(sock, WIFI_UFO_HEARTBEAT)
                self.log_message.emit(f"TX {self.host}:{self.remote_port} WiFi UFO 心跳 {WIFI_UFO_HEARTBEAT.hex()}")
            elif name == "set_control":
                self.control = command[1].clamped()
            elif name == "start_stream":
                self.control_stream = True
                self.next_control_at = 0.0
                self.log_message.emit("控制流已启动：50 ms 周期")
                self.status_changed.emit("控制流发送中")
            elif name == "stop_stream":
                self.control_stream = False
                self.log_message.emit("控制流已停止")
                self.status_changed.emit("UDP 已连接")
            elif name == "burst":
                self.burst_mode = int(command[1])
                self.burst_label = str(command[2])
                self.burst_until = time.monotonic() + float(command[3])
                self.log_message.emit(f"开始发送{self.burst_label}指令脉冲 {command[3]:.1f} 秒")
            elif name == "nudge":
                self.nudge_state = command[1].clamped()
                self.nudge_label = str(command[2])
                self.nudge_until = time.monotonic() + float(command[3])
                self.log_message.emit(f"短促控制：{self.nudge_label}")

    def _read_socket(self, sock: socket.socket) -> None:
        while True:
            try:
                data, addr = sock.recvfrom(65535)
            except BlockingIOError:
                return
            except OSError:
                return
            self._parse_datagram(data, addr)

    def _tick_control(self, sock: socket.socket) -> None:
        now = time.monotonic()
        if now < self.next_control_at:
            return

        if self.burst_until > now:
            self._send_control(sock, ControlState(), self.burst_mode, self.burst_label)
            self.next_control_at = now + CONTROL_INTERVAL
            return
        if self.burst_until and self.burst_until <= now:
            self.log_message.emit(f"{self.burst_label}指令脉冲结束")
            self.burst_until = 0.0

        if self.nudge_until > now:
            self._send_control(sock, self.nudge_state, 0, self.nudge_label)
            self.next_control_at = now + CONTROL_INTERVAL
            return
        if self.nudge_until and self.nudge_until <= now:
            self._send_control(sock, ControlState(), 0, "回中")
            self.nudge_until = 0.0
            self.next_control_at = now + CONTROL_INTERVAL
            return

        if self.control_stream:
            self._send_control(sock, self.control, 0, "手动控制")
            self.next_control_at = now + CONTROL_INTERVAL

    def _parse_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        if not data.startswith(b"cc"):
            self.info_received.emit({"格式": "未知 UDP", "来源": f"{addr[0]}:{addr[1]}", "长度": str(len(data))})
            return

        packet_type = data[2] if len(data) > 2 else -1
        if packet_type == 1:
            ssid = data[7:71].split(b"\x00", 1)[0].decode("ascii", errors="replace")
            self.info_received.emit(
                {
                    "格式": "WiFi UFO 信息包",
                    "来源": f"{addr[0]}:{addr[1]}",
                    "SSID": ssid,
                    "包类型": str(packet_type),
                    "长度": str(len(data)),
                    "预览": data[:80].hex(" "),
                }
            )
            self.log_message.emit(f"收到 WiFi UFO 信息包：SSID={ssid} len={len(data)}")
            return
        if packet_type == 3:
            self._handle_video_packet(data)
            return
        self.info_received.emit(
            {
                "格式": "WiFi UFO 数据包",
                "来源": f"{addr[0]}:{addr[1]}",
                "包类型": str(packet_type),
                "长度": str(len(data)),
            }
        )

    def _handle_video_packet(self, data: bytes) -> None:
        self.video_packets += 1
        self.video_bytes += len(data)

        meta = self._video_packet_meta(data)
        soi = data.find(b"\xff\xd8")
        if meta is not None:
            frame_key, fragment_index, payload = meta
            if fragment_index <= 1 or soi >= 0:
                start = soi if soi >= 0 else VIDEO_PAYLOAD_OFFSET
                self.frame_buffer = bytearray(data[start:])
                self.frame_open = True
                self.frame_key = frame_key
                self.expected_fragment = fragment_index + 1
            elif self.frame_open and frame_key == self.frame_key and fragment_index == self.expected_fragment:
                self.frame_buffer.extend(payload)
                self.expected_fragment += 1
            elif self.frame_open:
                self.frame_buffer.clear()
                self.frame_open = False
                self.frame_key = None
                self.expected_fragment = 1
                self.bad_frames += 1
        elif soi >= 0:
            self.frame_buffer = bytearray(data[soi:])
            self.frame_open = True
            self.frame_key = None
            self.expected_fragment = 1
        elif self.frame_open:
            payload_offset = min(VIDEO_PAYLOAD_OFFSET, len(data))
            self.frame_buffer.extend(data[payload_offset:])

        if self.frame_open:
            eoi = self.frame_buffer.find(b"\xff\xd9")
            if eoi >= 0:
                jpg = bytes(self.frame_buffer[: eoi + 2])
                self.frame_buffer.clear()
                self.frame_open = False
                self.frame_key = None
                self.expected_fragment = 1
                image = QImage()
                image.loadFromData(QByteArray(jpg), "JPG")
                if self._frame_is_usable(image):
                    now = time.monotonic()
                    if now - self.last_frame_emit >= 1 / 18:
                        self.video_frames += 1
                        self.last_frame_emit = now
                        self.video_frame.emit(image)
                else:
                    self.bad_frames += 1
            elif len(self.frame_buffer) > 2 * 1024 * 1024:
                self.frame_buffer.clear()
                self.frame_open = False
                self.frame_key = None
                self.expected_fragment = 1
                self.bad_frames += 1

        if self.video_packets == 1 or self.video_packets % 30 == 0:
            elapsed = max(0.001, time.monotonic() - self.stats_start)
            kbps = (self.video_bytes / 1024.0) / elapsed
            self.video_stats.emit(self.video_packets, kbps, self.video_frames, self.bad_frames)

    def _video_packet_meta(self, data: bytes) -> tuple[bytes, int, bytes] | None:
        if len(data) < VIDEO_PAYLOAD_OFFSET or not data.startswith(b"cc") or data[2] != 3:
            return None

        fragment_marker = data[VIDEO_FRAGMENT_HEADER_OFFSET]
        fragment_index = data[VIDEO_FRAGMENT_HEADER_OFFSET + 1]
        total_fragments = data[VIDEO_FRAGMENT_HEADER_OFFSET + 3]
        payload_len = int.from_bytes(data[VIDEO_FRAGMENT_HEADER_OFFSET + 5 : VIDEO_FRAGMENT_HEADER_OFFSET + 7], "little")
        if fragment_marker != 1 or fragment_index < 1 or total_fragments < fragment_index:
            return None
        if payload_len <= 0 or payload_len > len(data) - VIDEO_PAYLOAD_OFFSET:
            payload_len = len(data) - VIDEO_PAYLOAD_OFFSET

        frame_key = bytes(data[8:14])
        payload = data[VIDEO_PAYLOAD_OFFSET : VIDEO_PAYLOAD_OFFSET + payload_len]
        return frame_key, fragment_index, payload

    def _frame_is_usable(self, image: QImage) -> bool:
        if image.isNull() or image.width() < 80 or image.height() < 60:
            return False
        size = (image.width(), image.height())
        if self.last_size is None:
            self.last_size = size
            return True
        if size != self.last_size:
            self.last_size = size
            return False
        return True

    def _send_control(self, sock: socket.socket, state: ControlState, mode: int, label: str) -> None:
        packet = bytearray(WIFI_UFO_CONTROL_TEMPLATE)
        state = state.clamped()
        packet[8] = state.roll
        packet[9] = state.pitch
        packet[10] = state.throttle
        packet[11] = state.yaw
        packet[12] = clamp_byte(mode)
        checksum = 0
        for value in packet[8:13]:
            checksum ^= value
        packet[13] = checksum
        self._send(sock, bytes(packet))
        self.control_packet_sent.emit(f"{label} R{state.roll} P{state.pitch} T{state.throttle} Y{state.yaw} M{mode}")

    def _send(self, sock: socket.socket, payload: bytes) -> None:
        try:
            sock.sendto(payload, (self.host, self.remote_port))
        except OSError as exc:
            self.log_message.emit(f"UDP 发送失败：{exc}")


class CleanflightSerialClient(QThread):
    log_message = Signal(str)
    status_changed = Signal(str)
    text_received = Signal(str)
    telemetry = Signal(dict)

    def __init__(self) -> None:
        super().__init__()
        self.commands: queue.Queue[tuple] = queue.Queue()
        self.stop_event = threading.Event()
        self.port_label = ""
        self.baud = 115200

    @staticmethod
    def ports() -> list[str]:
        labels = []
        for info in QSerialPortInfo.availablePorts():
            label = info.portName()
            if info.description():
                label += f" - {info.description()}"
            labels.append(label)
        return labels

    def configure(self, port_label: str, baud: int) -> None:
        self.port_label = port_label
        self.baud = baud

    def enqueue_line(self, line: str) -> None:
        self.commands.put(("line", line.strip()))

    def close_port(self) -> None:
        self.commands.put(("close",))

    def stop(self) -> None:
        self.stop_event.set()
        self.commands.put(("stop",))

    def run(self) -> None:
        serial = QSerialPort()
        buffer = bytearray()
        try:
            port = self.port_label.split(" - ", 1)[0].strip()
            serial.setPortName(port)
            serial.setBaudRate(self.baud)
            serial.setDataBits(QSerialPort.Data8)
            serial.setParity(QSerialPort.NoParity)
            serial.setStopBits(QSerialPort.OneStop)
            serial.setFlowControl(QSerialPort.NoFlowControl)
            if not serial.open(QSerialPort.ReadWrite):
                self.status_changed.emit("串口失败")
                self.log_message.emit(f"串口打开失败：{port}，{serial.errorString()}")
                return

            self.status_changed.emit("串口已连接")
            self.log_message.emit(f"串口已打开：{port} @ {self.baud} 8N1")
            serial.write(b"#\r\n")
            serial.waitForBytesWritten(100)

            while not self.stop_event.is_set():
                while True:
                    try:
                        cmd = self.commands.get_nowait()
                    except queue.Empty:
                        break
                    if cmd[0] == "stop" or cmd[0] == "close":
                        self.stop_event.set()
                        break
                    if cmd[0] == "line" and cmd[1]:
                        payload = (cmd[1] + "\r\n").encode("utf-8")
                        serial.write(payload)
                        serial.waitForBytesWritten(80)
                        self.log_message.emit(f"CLI TX：{cmd[1]}")

                if serial.waitForReadyRead(20):
                    buffer.extend(bytes(serial.readAll()))
                    while serial.waitForReadyRead(5):
                        buffer.extend(bytes(serial.readAll()))
                    if buffer:
                        text = buffer.decode("utf-8", errors="replace")
                        buffer.clear()
                        self.text_received.emit(text)
                        self._parse_text(text)
        finally:
            if serial.isOpen():
                serial.close()
            self.status_changed.emit("串口未连接")
            self.log_message.emit("串口已关闭")

    def _parse_text(self, text: str) -> None:
        import re

        data: dict[str, str] = {}
        version = re.search(r"(Cleanflight/[^\r\n]+)", text)
        if version:
            data["飞控固件"] = version.group(1).strip()
        voltage = re.search(r"Voltage:\s*(\d+)\s*\*\s*0\.1V", text)
        if voltage:
            data["电压"] = f"{int(voltage.group(1)) * 0.1:.1f} V"
        cpu = re.search(r"CPU Clock=(\d+MHz),\s*GYRO=([^,\r\n]+),\s*ACC=([^\r\n]+)", text)
        if cpu:
            data["CPU"] = cpu.group(1)
            data["陀螺仪"] = cpu.group(2).strip()
            data["加速度计"] = cpu.group(3).strip()
        uptime = re.search(r"System Uptime:\s*(\d+)\s*seconds", text)
        if uptime:
            data["飞控运行时间"] = f"{uptime.group(1)} s"
        i2c = re.search(r"I2C Errors:\s*(\d+)", text)
        if i2c:
            data["I2C 错误"] = i2c.group(1)
        if data:
            self.telemetry.emit(data)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.wifi_worker: WifiUfoWorker | None = None
        self.serial_worker: CleanflightSerialClient | None = None
        self.metric_rows: dict[str, int] = {}
        self.last_control_log = 0.0
        self.last_control_status = 0.0
        self.log_path = QT_LOG_DIR / f"qt6_ground_station_{QDateTime.currentDateTime().toString('yyyyMMdd_HHmmss')}.log"
        self.log_file = self.log_path.open("a", encoding="utf-8")
        self._build_ui()
        self.refresh_ports()
        self.apply_profile()
        self.reset_hover()
        self.apply_interface_mode()
        self.append_log("Qt6/PySide6 中文上位机已启动")

    def closeEvent(self, event) -> None:  # noqa: N802
        self.close_udp()
        self.close_serial()
        self.log_file.close()
        super().closeEvent(event)

    def _build_ui(self) -> None:
        self.setWindowTitle("无人机 Qt6 可视化上位机")
        self.resize(1380, 860)
        self.setMinimumSize(1180, 720)
        self.setStatusBar(QStatusBar(self))
        self.setStyleSheet(
            """
            QMainWindow { background:#eef2f7; }
            QGroupBox { font-weight:600; border:1px solid #cfd8e5; border-radius:6px; margin-top:10px; background:white; }
            QGroupBox::title { subcontrol-origin: margin; left:10px; padding:0 4px; }
            QPushButton { background:white; border:1px solid #b9c4d4; border-radius:4px; padding:6px 10px; }
            QPushButton:hover { background:#edf5ff; border-color:#5b8def; }
            QPushButton:pressed { background:#dbeafe; }
            QLineEdit, QSpinBox, QComboBox { background:white; border:1px solid #b9c4d4; border-radius:4px; padding:4px; }
            QTabWidget::pane { border:1px solid #cfd8e5; background:white; }
            """
        )

        central = QWidget(self)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)
        root.addWidget(self._top_panel())

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._left_panel())

        self.tabs = QTabWidget()
        self.monitor_tab = self._monitor_tab()
        self.control_tab = self._control_tab()
        self.dev_tab = self._developer_tab()
        self.safety_tab = self._safety_tab()
        self.monitor_index = self.tabs.addTab(self.monitor_tab, "监控")
        self.control_index = self.tabs.addTab(self.control_tab, "飞行控制")
        self.dev_index = self.tabs.addTab(self.dev_tab, "开发者")
        self.safety_index = self.tabs.addTab(self.safety_tab, "安全/说明")
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        root.addWidget(splitter, 1)
        self.setCentralWidget(central)

    def _top_panel(self) -> QGroupBox:
        group = QGroupBox("连接配置")
        layout = QGridLayout(group)
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["使用者界面", "开发者界面"])
        self.profile_combo = QComboBox()
        self.profile_combo.addItems(["WiFi UFO UDP", "Tello 兼容 UDP", "仅串口 Cleanflight"])
        self.ip_edit = QLineEdit("192.168.0.1")
        self.remote_udp = QSpinBox()
        self.remote_udp.setRange(1, 65535)
        self.remote_udp.setValue(40000)
        self.local_udp = QSpinBox()
        self.local_udp.setRange(1, 65535)
        self.local_udp.setValue(40000)
        self.udp_status = card_label("UDP 未连接")

        self.serial_combo = QComboBox()
        self.serial_combo.setMinimumWidth(240)
        self.baud_spin = QSpinBox()
        self.baud_spin.setRange(1200, 2_000_000)
        self.baud_spin.setValue(115200)
        self.serial_status = card_label("串口未连接")

        open_udp = QPushButton("打开 UDP")
        close_udp = QPushButton("关闭 UDP")
        heartbeat = QPushButton("UFO 心跳")
        refresh = QPushButton("刷新串口")
        open_serial = QPushButton("连接串口")
        close_serial = QPushButton("关闭串口")

        layout.addWidget(QLabel("界面"), 0, 0)
        layout.addWidget(self.mode_combo, 0, 1)
        layout.addWidget(QLabel("协议"), 0, 2)
        layout.addWidget(self.profile_combo, 0, 3)
        layout.addWidget(QLabel("无人机 IP"), 0, 4)
        layout.addWidget(self.ip_edit, 0, 5)
        layout.addWidget(QLabel("远端 UDP"), 0, 6)
        layout.addWidget(self.remote_udp, 0, 7)
        layout.addWidget(QLabel("本地 UDP"), 0, 8)
        layout.addWidget(self.local_udp, 0, 9)
        layout.addWidget(open_udp, 0, 10)
        layout.addWidget(close_udp, 0, 11)
        layout.addWidget(heartbeat, 0, 12)
        layout.addWidget(self.udp_status, 0, 13)

        layout.addWidget(QLabel("飞控串口"), 1, 0)
        layout.addWidget(self.serial_combo, 1, 1, 1, 4)
        layout.addWidget(QLabel("波特率"), 1, 5)
        layout.addWidget(self.baud_spin, 1, 6)
        layout.addWidget(refresh, 1, 7)
        layout.addWidget(open_serial, 1, 8)
        layout.addWidget(close_serial, 1, 9)
        layout.addWidget(self.serial_status, 1, 10, 1, 4)
        layout.setColumnStretch(13, 1)

        self.mode_combo.currentTextChanged.connect(self.apply_interface_mode)
        self.profile_combo.currentTextChanged.connect(self.apply_profile)
        open_udp.clicked.connect(self.open_udp)
        close_udp.clicked.connect(self.close_udp)
        heartbeat.clicked.connect(self.send_heartbeat)
        refresh.clicked.connect(self.refresh_ports)
        open_serial.clicked.connect(self.open_serial)
        close_serial.clicked.connect(self.close_serial)
        return group

    def _left_panel(self) -> QWidget:
        left = QWidget()
        layout = QVBoxLayout(left)
        layout.setContentsMargins(0, 0, 0, 0)
        video_group = QGroupBox("图传预览 / 状态")
        video_layout = QVBoxLayout(video_group)
        self.video_label = QLabel("等待 WiFi UFO 视频流")
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setMinimumSize(600, 380)
        self.video_label.setStyleSheet("QLabel { background:#101820; color:#dce7f7; border-radius:4px; }")
        self.video_stats = card_label("视频：未连接")
        video_layout.addWidget(self.video_label, 1)
        video_layout.addWidget(self.video_stats)
        layout.addWidget(video_group, 3)

        log_group = QGroupBox("运行日志")
        log_layout = QVBoxLayout(log_group)
        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumBlockCount(1400)
        log_layout.addWidget(self.log_edit)
        layout.addWidget(log_group, 2)
        return left

    def _monitor_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        quick = QGridLayout()
        quick.addWidget(card_label("WiFi：WiFiUFO-3BE7F2"), 0, 0)
        quick.addWidget(card_label("飞控：Cleanflight SPRACINGF3"), 0, 1)
        quick.addWidget(card_label("控制：默认锁定"), 0, 2)
        layout.addLayout(quick)
        self.metric_table = QTableWidget(0, 2)
        self.metric_table.setHorizontalHeaderLabels(["字段", "值"])
        self.metric_table.horizontalHeader().setStretchLastSection(True)
        self.metric_table.verticalHeader().setVisible(False)
        self.metric_table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.metric_table, 1)
        self.set_metric("无人机 IP", "192.168.0.1")
        self.set_metric("WiFi 协议", "WiFi UFO UDP 40000")
        self.set_metric("串口飞控", "Cleanflight/SPRACINGF3 1.13.0")
        self.set_metric("安全锁", "未解锁")
        return page

    def _control_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        safety = QGroupBox("安全锁")
        safety_layout = QHBoxLayout(safety)
        self.unlock_check = QCheckBox("我已拆桨/固定机体，并确认允许发送飞行动作")
        self.lock_label = card_label("控制锁定", "#fff7ed")
        safety_layout.addWidget(self.unlock_check, 1)
        safety_layout.addWidget(self.lock_label)
        layout.addWidget(safety)

        self.command_status = card_label("当前命令：待机", "#eef6ff")
        layout.addWidget(self.command_status)

        remote = QGroupBox("遥控器式快捷控制")
        remote_grid = QGridLayout(remote)
        btn_up = QPushButton("前进 W / ↑")
        btn_down = QPushButton("后退 S / ↓")
        btn_left = QPushButton("左移 A")
        btn_right = QPushButton("右移 D")
        btn_yaw_l = QPushButton("左旋 Q")
        btn_yaw_r = QPushButton("右旋 E")
        btn_thr_up = QPushButton("升高 R")
        btn_thr_down = QPushButton("降低 F")
        btn_hover = QPushButton("悬停 Space")
        remote_grid.addWidget(btn_thr_up, 0, 0)
        remote_grid.addWidget(btn_up, 0, 1)
        remote_grid.addWidget(btn_yaw_r, 0, 2)
        remote_grid.addWidget(btn_left, 1, 0)
        remote_grid.addWidget(btn_hover, 1, 1)
        remote_grid.addWidget(btn_right, 1, 2)
        remote_grid.addWidget(btn_thr_down, 2, 0)
        remote_grid.addWidget(btn_down, 2, 1)
        remote_grid.addWidget(btn_yaw_l, 2, 2)
        layout.addWidget(remote)

        manual = QGroupBox("连续控制量")
        grid = QGridLayout(manual)
        self.roll = self._slider(0, 255, 128)
        self.pitch = self._slider(0, 255, 128)
        self.throttle = self._slider(0, 255, 128)
        self.yaw = self._slider(0, 255, 128)
        self.roll_value = card_label("128")
        self.pitch_value = card_label("128")
        self.throttle_value = card_label("128")
        self.yaw_value = card_label("128")
        rows = [
            ("横滚 Roll", self.roll, self.roll_value),
            ("俯仰 Pitch", self.pitch, self.pitch_value),
            ("油门 Throttle", self.throttle, self.throttle_value),
            ("偏航 Yaw", self.yaw, self.yaw_value),
        ]
        for row, (name, slider, value_label) in enumerate(rows):
            grid.addWidget(QLabel(name), row, 0)
            grid.addWidget(slider, row, 1)
            grid.addWidget(value_label, row, 2)
        grid.setColumnStretch(1, 1)
        layout.addWidget(manual)

        buttons = QGridLayout()
        self.start_control = QPushButton("启动连续控制")
        self.stop_control = QPushButton("停止连续控制")
        neutral = QPushButton("悬停/回中")
        zero_throttle = QPushButton("油门归零")
        takeoff = QPushButton("起飞")
        land = QPushButton("降落")
        hard_stop = QPushButton("急停")
        hard_stop.setStyleSheet("QPushButton { background:#fff1f2; border-color:#fb7185; color:#9f1239; }")
        buttons.addWidget(self.start_control, 0, 0)
        buttons.addWidget(self.stop_control, 0, 1)
        buttons.addWidget(neutral, 0, 2)
        buttons.addWidget(zero_throttle, 0, 3)
        buttons.addWidget(takeoff, 1, 0)
        buttons.addWidget(land, 1, 1)
        buttons.addWidget(hard_stop, 1, 2, 1, 2)
        layout.addLayout(buttons)
        layout.addStretch(1)

        for slider in (self.roll, self.pitch, self.throttle, self.yaw):
            slider.valueChanged.connect(self.update_control)
        self.unlock_check.toggled.connect(self._unlock_changed)
        self.start_control.clicked.connect(self.start_control_stream)
        self.stop_control.clicked.connect(self.stop_control_stream)
        neutral.clicked.connect(self.reset_hover)
        zero_throttle.clicked.connect(self.zero_throttle)
        takeoff.clicked.connect(self.takeoff)
        land.clicked.connect(self.land)
        hard_stop.clicked.connect(self.hard_stop)
        btn_up.clicked.connect(lambda: self.nudge("前进", pitch=176))
        btn_down.clicked.connect(lambda: self.nudge("后退", pitch=80))
        btn_left.clicked.connect(lambda: self.nudge("左移", roll=80))
        btn_right.clicked.connect(lambda: self.nudge("右移", roll=176))
        btn_yaw_l.clicked.connect(lambda: self.nudge("左旋", yaw=80))
        btn_yaw_r.clicked.connect(lambda: self.nudge("右旋", yaw=176))
        btn_thr_up.clicked.connect(lambda: self.nudge("升高", throttle=176))
        btn_thr_down.clicked.connect(lambda: self.nudge("降低", throttle=80))
        btn_hover.clicked.connect(lambda: self.nudge("悬停", duration=0.25))
        return page

    def _developer_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        row = QHBoxLayout()
        version = QPushButton("version")
        status = QPushButton("status")
        dump = QPushButton("dump")
        enter_cli = QPushButton("进入 CLI (#)")
        self.cli_edit = QLineEdit()
        self.cli_edit.setPlaceholderText("输入 Cleanflight CLI 命令，例如 help / version / status")
        send = QPushButton("发送 CLI")
        row.addWidget(version)
        row.addWidget(status)
        row.addWidget(dump)
        row.addWidget(enter_cli)
        row.addWidget(self.cli_edit, 1)
        row.addWidget(send)
        layout.addLayout(row)
        self.serial_console = QPlainTextEdit()
        self.serial_console.setReadOnly(True)
        self.serial_console.setMaximumBlockCount(3000)
        layout.addWidget(self.serial_console, 1)
        version.clicked.connect(lambda: self.send_cli_line("version"))
        status.clicked.connect(lambda: self.send_cli_line("status"))
        dump.clicked.connect(lambda: self.send_cli_line("dump"))
        enter_cli.clicked.connect(lambda: self.send_cli_line("#"))
        send.clicked.connect(self.send_cli_from_edit)
        self.cli_edit.returnPressed.connect(self.send_cli_from_edit)
        return page

    def _safety_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        text = QLabel(
            "使用步骤：\n"
            "1. 先拆桨或固定机体，再连接 WiFiUFO-3BE7F2。\n"
            "2. 点击“UFO 心跳”启动 WiFi UFO UDP 40000 图传。\n"
            "3. 使用者界面只显示监控和飞行控制；开发者界面额外显示 CLI。\n"
            "4. 快捷控制按钮会短促发送方向命令；连续控制需要点击“启动连续控制”。\n"
            "5. 所有飞行动作默认锁定，必须确认安全后才会发出。\n\n"
            "键盘快捷键：W/S 前后，A/D 左右，Q/E 旋转，R/F 升降，Space 悬停。\n"
            "视频优化：后台线程接收 UDP，按 54 字节载荷偏移和分片序号重组 JPEG，丢弃坏帧并限帧显示。"
        )
        text.setWordWrap(True)
        text.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        text.setStyleSheet("QLabel { background:white; padding:12px; border:1px solid #cfd8e5; border-radius:6px; }")
        layout.addWidget(text)
        layout.addStretch(1)
        return page

    def _connect_worker(self, worker: WifiUfoWorker) -> None:
        worker.log_message.connect(self.append_log)
        worker.status_changed.connect(self._udp_status)
        worker.info_received.connect(self.update_telemetry)
        worker.video_frame.connect(self.update_video)
        worker.video_stats.connect(self.update_video_stats)
        worker.control_packet_sent.connect(self.control_packet_sent)

    def _connect_serial(self, worker: CleanflightSerialClient) -> None:
        worker.log_message.connect(self.append_log)
        worker.status_changed.connect(self.serial_status.setText)
        worker.text_received.connect(self.append_serial)
        worker.telemetry.connect(self.update_telemetry)

    def _slider(self, minimum: int, maximum: int, value: int) -> QSlider:
        slider = QSlider(Qt.Horizontal)
        slider.setRange(minimum, maximum)
        slider.setValue(value)
        slider.setTickInterval(16)
        slider.setTickPosition(QSlider.TicksBelow)
        return slider

    @Slot()
    def refresh_ports(self) -> None:
        current = self.serial_combo.currentText()
        self.serial_combo.clear()
        self.serial_combo.addItems(CleanflightSerialClient.ports())
        index = self.serial_combo.findText(current)
        if index >= 0:
            self.serial_combo.setCurrentIndex(index)
        else:
            for i in range(self.serial_combo.count()):
                if self.serial_combo.itemText(i).startswith("COM9"):
                    self.serial_combo.setCurrentIndex(i)
                    break

    @Slot()
    def apply_profile(self) -> None:
        profile = self.profile_combo.currentText()
        if "WiFi UFO" in profile:
            self.remote_udp.setValue(40000)
            self.local_udp.setValue(40000)
        elif "Tello" in profile:
            self.remote_udp.setValue(8889)
            self.local_udp.setValue(8890)
        self.append_log(f"协议配置：{profile}")

    @Slot()
    def apply_interface_mode(self) -> None:
        developer = self.mode_combo.currentText() == "开发者界面"
        self.tabs.setTabVisible(self.dev_index, developer)
        if not developer and self.tabs.currentIndex() == self.dev_index:
            self.tabs.setCurrentIndex(self.monitor_index)
        self.append_log(f"界面模式：{self.mode_combo.currentText()}")

    @Slot()
    def open_udp(self) -> None:
        if self.wifi_worker and self.wifi_worker.isRunning():
            return
        self.wifi_worker = WifiUfoWorker(self.ip_edit.text().strip(), self.remote_udp.value(), self.local_udp.value())
        self._connect_worker(self.wifi_worker)
        self.wifi_worker.start()

    @Slot()
    def close_udp(self) -> None:
        if self.wifi_worker:
            self.wifi_worker.stop()
            self.wifi_worker.wait(1200)
            self.wifi_worker = None

    @Slot()
    def send_heartbeat(self) -> None:
        self.profile_combo.setCurrentText("WiFi UFO UDP")
        self.open_udp()
        if self.wifi_worker:
            self.wifi_worker.enqueue(("heartbeat",))

    @Slot()
    def open_serial(self) -> None:
        if not self.serial_combo.currentText():
            QMessageBox.information(self, "没有串口", "未发现可用串口。")
            return
        self.close_serial()
        worker = CleanflightSerialClient()
        worker.configure(self.serial_combo.currentText(), self.baud_spin.value())
        self._connect_serial(worker)
        self.serial_worker = worker
        worker.start()

    @Slot()
    def close_serial(self) -> None:
        if self.serial_worker:
            self.serial_worker.stop()
            self.serial_worker.wait(1200)
            self.serial_worker = None

    def send_cli_line(self, line: str) -> None:
        if not self.serial_worker:
            self.append_log(f"串口未连接，无法发送：{line}")
            return
        self.serial_worker.enqueue_line(line)

    @Slot()
    def send_cli_from_edit(self) -> None:
        line = self.cli_edit.text().strip()
        if not line:
            return
        self.send_cli_line(line)
        self.cli_edit.clear()

    @Slot()
    def update_control(self) -> None:
        self.roll_value.setText(str(self.roll.value()))
        self.pitch_value.setText(str(self.pitch.value()))
        self.throttle_value.setText(str(self.throttle.value()))
        self.yaw_value.setText(str(self.yaw.value()))
        self.command_status.setText(
            f"控制量：R{self.roll.value()} P{self.pitch.value()} T{self.throttle.value()} Y{self.yaw.value()}"
        )
        if self.wifi_worker:
            self.wifi_worker.enqueue(("set_control", self.current_control()))

    def current_control(self) -> ControlState:
        return ControlState(self.roll.value(), self.pitch.value(), self.throttle.value(), self.yaw.value())

    @Slot()
    def reset_hover(self) -> None:
        self.roll.setValue(128)
        self.pitch.setValue(128)
        self.throttle.setValue(128)
        self.yaw.setValue(128)
        self.update_control()

    @Slot()
    def zero_throttle(self) -> None:
        self.throttle.setValue(0)
        self.update_control()

    @Slot(bool)
    def _unlock_changed(self, checked: bool) -> None:
        self.lock_label.setText("控制已解锁" if checked else "控制锁定")
        self.lock_label.setStyleSheet(
            "QLabel { background:%s; border:1px solid #d2dbe8; border-radius:4px; padding:6px; }"
            % ("#ecfdf5" if checked else "#fff7ed")
        )
        self.set_metric("安全锁", "已解锁" if checked else "未解锁")
        self.append_log("安全锁已手动解锁" if checked else "安全锁已锁定")

    def ensure_unlocked(self, action: str) -> bool:
        if self.unlock_check.isChecked():
            return True
        QMessageBox.warning(self, "控制已锁定", f"“{action}”需要先勾选安全锁。请确认拆桨或固定机体后再解锁。")
        self.append_log(f"已阻止动作：{action}，原因：安全锁未解锁")
        return False

    @Slot()
    def start_control_stream(self) -> None:
        if not self.ensure_unlocked("启动连续控制"):
            return
        self.open_udp()
        if self.wifi_worker:
            self.wifi_worker.enqueue(("set_control", self.current_control()))
            self.wifi_worker.enqueue(("start_stream",))
        self.command_status.setText("当前命令：连续控制中")

    @Slot()
    def stop_control_stream(self) -> None:
        if self.wifi_worker:
            self.wifi_worker.enqueue(("stop_stream",))
        self.command_status.setText("当前命令：待机")

    def nudge(
        self,
        label: str,
        roll: int = 128,
        pitch: int = 128,
        throttle: int = 128,
        yaw: int = 128,
        duration: float = 0.45,
    ) -> None:
        if not self.ensure_unlocked(label):
            return
        self.open_udp()
        if self.wifi_worker:
            state = ControlState(roll, pitch, throttle, yaw)
            self.wifi_worker.enqueue(("nudge", state, label, duration))
        self.command_status.setText(f"短促控制：{label}")

    @Slot()
    def takeoff(self) -> None:
        if not self.ensure_unlocked("起飞"):
            return
        if (
            QMessageBox.question(
                self,
                "确认起飞",
                "即将连续发送 1 秒起飞指令。请确认桨叶安全、机体固定或处于安全飞行区。",
            )
            == QMessageBox.Yes
        ):
            self.open_udp()
            if self.wifi_worker:
                self.wifi_worker.enqueue(("burst", 1, "起飞", 1.0))

    @Slot()
    def land(self) -> None:
        if not self.ensure_unlocked("降落"):
            return
        if QMessageBox.question(self, "确认降落", "即将连续发送 1 秒降落指令。") == QMessageBox.Yes:
            self.open_udp()
            if self.wifi_worker:
                self.wifi_worker.enqueue(("burst", 2, "降落", 1.0))

    @Slot()
    def hard_stop(self) -> None:
        if not self.ensure_unlocked("急停"):
            return
        if (
            QMessageBox.warning(
                self,
                "确认急停",
                "急停可能导致飞行器直接掉落。只有在失控或危险时使用。是否发送？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            == QMessageBox.Yes
        ):
            self.open_udp()
            if self.wifi_worker:
                self.wifi_worker.enqueue(("burst", 4, "急停", 1.0))

    @Slot(dict)
    def update_telemetry(self, values: dict) -> None:
        for key, value in values.items():
            self.set_metric(str(key), str(value))

    @Slot(QImage)
    def update_video(self, frame: QImage) -> None:
        if frame.isNull():
            return
        pixmap = QPixmap.fromImage(frame).scaled(self.video_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.video_label.setPixmap(pixmap)

    @Slot(int, float, int, int)
    def update_video_stats(self, packets: int, kbps: float, frames: int, bad_frames: int) -> None:
        self.video_stats.setText(
            f"视频包：{packets}    速率：{kbps:.1f} KB/s    解码帧：{frames}    丢弃坏帧：{bad_frames}"
        )
        self.set_metric("WiFi 视频包", str(packets))
        self.set_metric("WiFi 视频速率", f"{kbps:.1f} KB/s")
        self.set_metric("WiFi 解码帧", str(frames))
        self.set_metric("WiFi 丢弃坏帧", str(bad_frames))

    @Slot(str)
    def _udp_status(self, status: str) -> None:
        self.udp_status.setText(status)
        self.statusBar().showMessage(status, 3000)

    @Slot(str)
    def control_packet_sent(self, message: str) -> None:
        now = time.monotonic()
        if now - self.last_control_status > 0.2:
            self.command_status.setText(f"已发送：{message}")
            self.last_control_status = now
        if now - self.last_control_log > 0.8:
            self.append_log(f"控制包：{message}")
            self.last_control_log = now

    @Slot(str)
    def append_log(self, message: str) -> None:
        line = f"[{QDateTime.currentDateTime().toString('HH:mm:ss')}] {message}"
        self.log_edit.appendPlainText(line)
        self.log_file.write(line + "\n")
        self.log_file.flush()

    @Slot(str)
    def append_serial(self, text: str) -> None:
        self.serial_console.appendPlainText(text.strip())

    def set_metric(self, key: str, value: str) -> None:
        if key not in self.metric_rows:
            row = self.metric_table.rowCount()
            self.metric_table.insertRow(row)
            self.metric_table.setItem(row, 0, QTableWidgetItem(key))
            self.metric_table.setItem(row, 1, QTableWidgetItem(value))
            self.metric_rows[key] = row
        else:
            row = self.metric_rows[key]
            self.metric_table.item(row, 1).setText(value)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.isAutoRepeat():
            return
        key = event.key()
        if key in (Qt.Key_W, Qt.Key_Up):
            self.nudge("前进", pitch=176)
        elif key in (Qt.Key_S, Qt.Key_Down):
            self.nudge("后退", pitch=80)
        elif key == Qt.Key_A:
            self.nudge("左移", roll=80)
        elif key == Qt.Key_D:
            self.nudge("右移", roll=176)
        elif key == Qt.Key_Q:
            self.nudge("左旋", yaw=80)
        elif key == Qt.Key_E:
            self.nudge("右旋", yaw=176)
        elif key == Qt.Key_R:
            self.nudge("升高", throttle=176)
        elif key == Qt.Key_F:
            self.nudge("降低", throttle=80)
        elif key == Qt.Key_Space:
            self.nudge("悬停", duration=0.25)
        else:
            super().keyPressEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("无人机 Qt6 可视化上位机")
    app.setOrganizationName("AIProject")
    app.setFont(QFont("Microsoft YaHei UI", 9))
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
