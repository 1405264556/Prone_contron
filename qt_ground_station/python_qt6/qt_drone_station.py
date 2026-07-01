from __future__ import annotations

import queue
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QByteArray, QDateTime, QThread, Qt, Signal, Slot
from PySide6.QtGui import QAction, QFont, QIcon, QImage, QKeyEvent, QPixmap
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
IDLE_THROTTLE = 110  # 待机时电机低频旋转的油门值 (0-255)


def clamp_byte(value: int) -> int:
    return max(0, min(255, int(value)))


def card_label(text: str, accent: str = "#f0f4f8", color: str = "#334155") -> QLabel:
    label = QLabel(text)
    label.setAlignment(Qt.AlignCenter)
    label.setMinimumHeight(32)
    label.setStyleSheet(
        f"QLabel {{ background:{accent}; color:{color}; border:1px solid #d4dce8; "
        f"border-radius:6px; padding:6px 10px; font-weight:500; }}"
    )
    return label


def status_dot(color: str) -> str:
    """返回带颜色圆点的 HTML 状态文本前缀."""
    return f"<span style='color:{color};font-size:14px;'>&#9679;</span> "


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
        self.burst_auto_hover = False
        self.hold_active = False
        self.hold_state = ControlState()
        self.hold_label = ""
        self.last_heartbeat_at = 0.0

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
                self.last_heartbeat_at = time.monotonic()
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
                self._send(sock, WIFI_UFO_HEARTBEAT)
                self.last_heartbeat_at = time.monotonic()
                self.burst_mode = int(command[1])
                self.burst_label = str(command[2])
                self.burst_until = time.monotonic() + float(command[3])
                self.burst_auto_hover = len(command) > 4 and bool(command[4])
                self.next_control_at = 0.0
                self.log_message.emit(f"开始发送{self.burst_label}指令脉冲 {command[3]:.1f} 秒")
            elif name == "hold_start":
                self._send(sock, WIFI_UFO_HEARTBEAT)
                self.last_heartbeat_at = time.monotonic()
                self.hold_state = command[1].clamped()
                self.hold_label = str(command[2]) if len(command) > 2 else ""
                self.hold_active = True
                self.next_control_at = 0.0
                self.log_message.emit(f"持续控制开始：{self.hold_label}")
                self.status_changed.emit("持续控制中")
            elif name == "hold_stop":
                self.hold_active = False
                # 立即发送回中包，不延迟
                self._send_control(sock, ControlState(), 0, "回中")
                self.next_control_at = time.monotonic() + CONTROL_INTERVAL
                self.log_message.emit(f"持续控制停止：{self.hold_label}")
                self.status_changed.emit("UDP 已连接")

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
            # 空闲时才发心跳，避免和控制包挤在同一个 tick
            if now - self.last_heartbeat_at >= 0.5:
                self._send(sock, WIFI_UFO_HEARTBEAT)
                self.last_heartbeat_at = now
            return

        sent_control = False

        # --- 优先级 1: 突发指令 (起飞/降落/急停) ---
        if self.burst_until > now:
            self._send_control(sock, ControlState(), self.burst_mode, self.burst_label)
            self.next_control_at = now + CONTROL_INTERVAL
            sent_control = True

        if self.burst_until and self.burst_until <= now:
            self.log_message.emit(f"{self.burst_label}指令脉冲结束")
            self.burst_until = 0.0
            if self.burst_auto_hover:
                # 起飞后进入待机状态：电机低频旋转，等待方向指令
                self.hold_state = ControlState(128, 128, IDLE_THROTTLE, 128)
                self.hold_active = True
                self.hold_label = "待机怠速"
                self.log_message.emit("起飞完成，进入待机怠速状态（电机低频旋转）")
                self.status_changed.emit("待机中")
            self.next_control_at = now + CONTROL_INTERVAL
            sent_control = True

        # --- 优先级 2: 持续按键保持 ---
        if not sent_control and self.hold_active:
            self._send_control(sock, self.hold_state, 0, self.hold_label)
            self.next_control_at = now + CONTROL_INTERVAL
            sent_control = True

        # --- 优先级 3: 滑块连续控制流 ---
        if not sent_control and self.control_stream:
            self._send_control(sock, self.control, 0, "手动控制")
            self.next_control_at = now + CONTROL_INTERVAL
            sent_control = True

        # 控制包本身维持连接，重置心跳计时器避免心跳紧跟在控制包之后
        if sent_control:
            self.last_heartbeat_at = now

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
        self.resize(1400, 880)
        self.setMinimumSize(1180, 720)
        self.setStatusBar(QStatusBar(self))
        self.setStyleSheet("""
            QMainWindow { background:#eef2f7; }
            QGroupBox {
                font-weight:600; font-size:13px; color:#1e293b;
                border:1px solid #d5dde8; border-radius:8px;
                margin-top:14px; padding-top:8px; background:white;
            }
            QGroupBox::title {
                subcontrol-origin: margin; left:12px; padding:0 6px;
                color:#2563eb;
            }
            QPushButton {
                background:white; border:1px solid #c4cdd9; border-radius:5px;
                padding:7px 14px; color:#334155; font-weight:500;
            }
            QPushButton:hover { background:#eff6ff; border-color:#3b82f6; color:#1d4ed8; }
            QPushButton:pressed { background:#dbeafe; }
            QPushButton#danger { background:#fef2f2; border-color:#fca5a5; color:#991b1b; }
            QPushButton#danger:hover { background:#fee2e2; border-color:#ef4444; }
            QPushButton#primary { background:#2563eb; border-color:#2563eb; color:white; }
            QPushButton#primary:hover { background:#1d4ed8; }
            QLineEdit, QSpinBox, QComboBox {
                background:white; border:1px solid #c4cdd9; border-radius:5px;
                padding:5px; color:#334155;
            }
            QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border-color:#3b82f6; }
            QSlider::groove:horizontal { border-radius:3px; height:6px; background:#dde3ed; }
            QSlider::handle:horizontal {
                background:#2563eb; border-radius:8px; width:16px;
                margin:-5px 0; border:2px solid white;
            }
            QSlider::handle:horizontal:hover { background:#1d4ed8; }
            QSlider::sub-page:horizontal { background:#93c5fd; border-radius:3px; }
            QTabWidget::pane { border:1px solid #d5dde8; border-radius:6px; background:white; }
            QTabBar::tab {
                background:#f1f5f9; border:1px solid #d5dde8; border-bottom:none;
                border-radius:6px 6px 0 0; padding:8px 18px; margin-right:2px;
                color:#64748b;
            }
            QTabBar::tab:selected { background:white; color:#2563eb; font-weight:600; }
            QTabBar::tab:hover { color:#1d4ed8; }
            QTableWidget { gridline-color:#e8ecf1; border:1px solid #d5dde8; border-radius:4px; }
            QTableWidget::item { padding:5px 8px; }
            QHeaderView::section { background:#f8fafc; border:1px solid #e8ecf1; padding:6px; font-weight:600; }
            QCheckBox { color:#334155; spacing:8px; }
            QCheckBox::indicator { width:18px; height:18px; border-radius:3px; }
            QPlainTextEdit { border:1px solid #d5dde8; border-radius:5px; background:#fafbfc; }
            QStatusBar { background:#f1f5f9; border-top:1px solid #d5dde8; color:#64748b; }
        """)

        self._setup_menubar()

        central = QWidget(self)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
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
        self.monitor_index = self.tabs.addTab(self.monitor_tab, "📊 监控")
        self.control_index = self.tabs.addTab(self.control_tab, "🎮 飞行控制")
        self.dev_index = self.tabs.addTab(self.dev_tab, "🔧 开发者")
        self.safety_index = self.tabs.addTab(self.safety_tab, "📋 安全/说明")
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        root.addWidget(splitter, 1)
        self.setCentralWidget(central)

    def _setup_menubar(self) -> None:
        menubar = self.menuBar()
        menubar.setStyleSheet(
            "QMenuBar { background:white; border-bottom:1px solid #d5dde8; padding:2px; }"
            "QMenuBar::item { padding:4px 12px; border-radius:4px; }"
            "QMenuBar::item:selected { background:#eff6ff; }"
            "QMenu { background:white; border:1px solid #d5dde8; border-radius:6px; padding:4px; }"
            "QMenu::item { padding:6px 28px; border-radius:4px; }"
            "QMenu::item:selected { background:#eff6ff; color:#1d4ed8; }"
            "QMenu::separator { height:1px; background:#e8ecf1; margin:4px 8px; }"
        )

        file_menu = menubar.addMenu("文件(&F)")
        refresh_action = QAction("刷新串口列表", self)
        refresh_action.triggered.connect(self.refresh_ports)
        file_menu.addAction(refresh_action)
        file_menu.addSeparator()
        exit_action = QAction("退出(&X)", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        view_menu = menubar.addMenu("视图(&V)")
        self.toggle_dev_action = QAction("开发者界面", self)
        self.toggle_dev_action.setCheckable(True)
        self.toggle_dev_action.triggered.connect(
            lambda checked: self.mode_combo.setCurrentText("开发者界面" if checked else "使用者界面")
        )
        view_menu.addAction(self.toggle_dev_action)
        view_menu.addSeparator()
        clear_log_action = QAction("清空运行日志", self)
        clear_log_action.triggered.connect(lambda: self.log_edit.clear())
        view_menu.addAction(clear_log_action)

        help_menu = menubar.addMenu("帮助(&H)")
        about_action = QAction("关于(&A)", self)
        about_action.triggered.connect(
            lambda: QMessageBox.about(
                self, "关于",
                "无人机 Qt6 可视化上位机 v2.0\n\n"
                "WiFi UFO 协议无人机地面站\n"
                "支持图传接收、飞行控制、Cleanflight CLI 调试\n\n"
                "基于 PySide6 / Qt6 构建",
            )
        )
        help_menu.addAction(about_action)

    def _top_panel(self) -> QGroupBox:
        group = QGroupBox("连接配置")
        layout = QGridLayout(group)
        layout.setVerticalSpacing(6)
        layout.setHorizontalSpacing(8)

        # --- Row 0: 界面模式 + 协议 ---
        layout.addWidget(QLabel("界面模式"), 0, 0)
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["使用者界面", "开发者界面"])
        self.mode_combo.setToolTip("使用者界面隐藏开发者 CLI 标签页")
        layout.addWidget(self.mode_combo, 0, 1)

        layout.addWidget(QLabel("协议"), 0, 2)
        self.profile_combo = QComboBox()
        self.profile_combo.addItems(["WiFi UFO UDP", "Tello 兼容 UDP", "仅串口 Cleanflight"])
        self.profile_combo.setToolTip("选择通信协议，自动配置默认端口")
        layout.addWidget(self.profile_combo, 0, 3)

        # UDP 状态指示
        self.udp_status = card_label("UDP 未连接", "#fef2f2", "#991b1b")
        layout.addWidget(self.udp_status, 0, 4, 1, 2)

        # --- Row 1: UDP 配置 ---
        layout.addWidget(QLabel("无人机 IP"), 1, 0)
        self.ip_edit = QLineEdit("192.168.0.1")
        self.ip_edit.setToolTip("无人机 WiFi 模块 IP 地址")
        layout.addWidget(self.ip_edit, 1, 1)

        layout.addWidget(QLabel("远端端口"), 1, 2)
        self.remote_udp = QSpinBox()
        self.remote_udp.setRange(1, 65535)
        self.remote_udp.setValue(40000)
        self.remote_udp.setToolTip("无人机 UDP 端口")
        layout.addWidget(self.remote_udp, 1, 3)

        layout.addWidget(QLabel("本地端口"), 1, 4)
        self.local_udp = QSpinBox()
        self.local_udp.setRange(1, 65535)
        self.local_udp.setValue(40000)
        self.local_udp.setToolTip("本地上位机 UDP 监听端口")
        layout.addWidget(self.local_udp, 1, 5)

        open_udp = QPushButton("打开 UDP")
        open_udp.setObjectName("primary")
        open_udp.setToolTip("打开 UDP 连接，开始接收图传和发送控制")
        close_udp = QPushButton("关闭 UDP")
        heartbeat = QPushButton("UFO 心跳")
        heartbeat.setToolTip("向无人机发送 WiFi UFO 心跳包，启动图传")
        layout.addWidget(open_udp, 1, 6)
        layout.addWidget(close_udp, 1, 7)
        layout.addWidget(heartbeat, 1, 8)

        # --- Row 2: 串口配置 ---
        layout.addWidget(QLabel("飞控串口"), 2, 0)
        self.serial_combo = QComboBox()
        self.serial_combo.setMinimumWidth(220)
        self.serial_combo.setToolTip("选择飞控对应的串口")
        layout.addWidget(self.serial_combo, 2, 1, 1, 3)

        layout.addWidget(QLabel("波特率"), 2, 4)
        self.baud_spin = QSpinBox()
        self.baud_spin.setRange(1200, 2_000_000)
        self.baud_spin.setValue(115200)
        self.baud_spin.setToolTip("串口波特率，Cleanflight 默认 115200")
        layout.addWidget(self.baud_spin, 2, 5)

        refresh = QPushButton("刷新串口")
        refresh.setToolTip("重新扫描可用串口列表")
        open_serial = QPushButton("连接串口")
        open_serial.setToolTip("连接到选中的飞控串口")
        close_serial = QPushButton("关闭串口")
        self.serial_status = card_label("串口未连接", "#fef2f2", "#991b1b")
        layout.addWidget(refresh, 2, 6)
        layout.addWidget(open_serial, 2, 7)
        layout.addWidget(close_serial, 2, 8)
        layout.addWidget(self.serial_status, 2, 9)

        # 连接信号
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
        layout.setSpacing(8)

        video_group = QGroupBox("图传预览")
        video_layout = QVBoxLayout(video_group)
        self.video_label = QLabel("等待 WiFi UFO 视频流...")
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setMinimumSize(600, 380)
        self.video_label.setStyleSheet(
            "QLabel { background:#0f172a; color:#94a3b8; border-radius:6px; "
            "border:1px solid #1e293b; font-size:14px; }"
        )
        self.video_stats = card_label("视频：等待连接", "#f8fafc", "#64748b")
        video_layout.addWidget(self.video_label, 1)
        video_layout.addWidget(self.video_stats)
        layout.addWidget(video_group, 3)

        log_group = QGroupBox("运行日志")
        log_layout = QVBoxLayout(log_group)
        log_row = QHBoxLayout()
        log_row.addStretch()
        clear_btn = QPushButton("清空日志")
        clear_btn.setToolTip("清空当前显示的运行日志")
        clear_btn.clicked.connect(lambda: self.log_edit.clear())
        log_row.addWidget(clear_btn)
        log_layout.addLayout(log_row)
        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumBlockCount(2000)
        self.log_edit.setFont(QFont("Consolas", 9))
        log_layout.addWidget(self.log_edit)
        layout.addWidget(log_group, 2)
        return left

    def _monitor_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(8)

        quick = QHBoxLayout()
        self.quick_wifi = card_label("WiFi：等待连接", "#fef2f2", "#991b1b")
        self.quick_fc = card_label("飞控：未连接", "#fef2f2", "#991b1b")
        self.quick_lock = card_label("控制：默认锁定", "#fff7ed", "#c2410c")
        self.quick_wifi.setMinimumHeight(48)
        self.quick_fc.setMinimumHeight(48)
        self.quick_lock.setMinimumHeight(48)
        quick.addWidget(self.quick_wifi)
        quick.addWidget(self.quick_fc)
        quick.addWidget(self.quick_lock)
        layout.addLayout(quick)

        self.metric_table = QTableWidget(0, 2)
        self.metric_table.setHorizontalHeaderLabels(["字段", "值"])
        self.metric_table.horizontalHeader().setStretchLastSection(True)
        self.metric_table.verticalHeader().setVisible(False)
        self.metric_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.metric_table.setAlternatingRowColors(True)
        self.metric_table.setStyleSheet(
            "QTableWidget { alternate-background-color:#f8fafc; }"
        )
        layout.addWidget(self.metric_table, 1)
        self.set_metric("无人机 IP", "192.168.0.1")
        self.set_metric("WiFi 协议", "WiFi UFO UDP 40000")
        self.set_metric("串口飞控", "未连接")
        self.set_metric("安全锁", "未解锁")
        return page

    def _control_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(8)

        safety = QGroupBox("安全锁")
        safety_layout = QHBoxLayout(safety)
        self.unlock_check = QCheckBox("我已拆桨/固定机体，并确认允许发送飞行动作")
        self.unlock_check.setToolTip("必须勾选此选项才能发送任何飞行控制指令")
        self.lock_label = card_label("控制锁定", "#fff7ed", "#c2410c")
        safety_layout.addWidget(self.unlock_check, 1)
        safety_layout.addWidget(self.lock_label)
        layout.addWidget(safety)

        self.command_status = card_label("当前命令：待机", "#eff6ff", "#1e40af")
        layout.addWidget(self.command_status)

        remote = QGroupBox("键盘/鼠标持续控制（按下持续发送，松开自动回中）")
        remote_grid = QGridLayout(remote)
        remote_grid.setSpacing(6)
        btn_style = "QPushButton { min-height:44px; font-weight:600; font-size:13px; }"
        self.btn_up = QPushButton("↑ 前进 (W)")
        self.btn_down = QPushButton("↓ 后退 (S)")
        self.btn_left = QPushButton("← 左移 (A)")
        self.btn_right = QPushButton("→ 右移 (D)")
        self.btn_yaw_l = QPushButton("↺ 左旋 (Q)")
        self.btn_yaw_r = QPushButton("↻ 右旋 (E)")
        self.btn_thr_up = QPushButton("⇧ 升高 (R)")
        self.btn_thr_down = QPushButton("⇩ 降低 (F)")
        self.btn_hover = QPushButton("◎ 悬停 (Space)")
        for btn in [self.btn_up, self.btn_down, self.btn_left, self.btn_right,
                     self.btn_yaw_l, self.btn_yaw_r, self.btn_thr_up, self.btn_thr_down,
                     self.btn_hover]:
            btn.setStyleSheet(btn_style)
        self.btn_hover.setObjectName("primary")
        remote_grid.addWidget(self.btn_thr_up, 0, 0)
        remote_grid.addWidget(self.btn_up, 0, 1)
        remote_grid.addWidget(self.btn_yaw_r, 0, 2)
        remote_grid.addWidget(self.btn_left, 1, 0)
        remote_grid.addWidget(self.btn_hover, 1, 1)
        remote_grid.addWidget(self.btn_right, 1, 2)
        remote_grid.addWidget(self.btn_thr_down, 2, 0)
        remote_grid.addWidget(self.btn_down, 2, 1)
        remote_grid.addWidget(self.btn_yaw_l, 2, 2)
        layout.addWidget(remote)

        manual = QGroupBox("滑块微调（拖动滑块实时发送控制量）")
        grid = QGridLayout(manual)
        grid.setSpacing(8)
        self.roll = self._slider(0, 255, 128)
        self.pitch = self._slider(0, 255, 128)
        self.throttle = self._slider(0, 255, 128)
        self.yaw = self._slider(0, 255, 128)
        self.roll_value = card_label("128", "#f1f5f9", "#334155")
        self.pitch_value = card_label("128", "#f1f5f9", "#334155")
        self.throttle_value = card_label("128", "#f1f5f9", "#334155")
        self.yaw_value = card_label("128", "#f1f5f9", "#334155")
        rows = [
            ("横滚 Roll", self.roll, self.roll_value, "左右倾斜"),
            ("俯仰 Pitch", self.pitch, self.pitch_value, "前后倾斜"),
            ("油门 Throttle", self.throttle, self.throttle_value, "上升下降力度"),
            ("偏航 Yaw", self.yaw, self.yaw_value, "左右旋转"),
        ]
        for row, (name, slider, value_label, tip) in enumerate(rows):
            name_label = QLabel(name)
            name_label.setToolTip(tip)
            name_label.setMinimumWidth(100)
            grid.addWidget(name_label, row, 0)
            grid.addWidget(slider, row, 1)
            grid.addWidget(value_label, row, 2)
        grid.setColumnStretch(1, 1)
        layout.addWidget(manual)

        buttons = QGridLayout()
        buttons.setSpacing(6)
        self.arm_btn = QPushButton("解锁待机")
        self.arm_btn.setToolTip("发送解锁指令使电机进入低频怠速状态，之后方向控制才生效")
        self.arm_btn.setMinimumHeight(38)
        self.arm_btn.setObjectName("primary")
        self.disarm_btn = QPushButton("锁定停机")
        self.disarm_btn.setToolTip("发送降落/锁定指令，停止所有电机")
        self.disarm_btn.setMinimumHeight(38)
        hard_stop = QPushButton("⚠ 急停")
        hard_stop.setObjectName("danger")
        hard_stop.setToolTip("立即发送急停指令——可能导致飞行器直接掉落！")
        hard_stop.setMinimumHeight(38)
        buttons.addWidget(self.arm_btn, 0, 0)
        buttons.addWidget(self.disarm_btn, 0, 1)
        buttons.addWidget(hard_stop, 0, 2, 1, 2)
        layout.addLayout(buttons)
        layout.addStretch(1)

        # --- 滑块信号 ---
        for slider in (self.roll, self.pitch, self.throttle, self.yaw):
            slider.valueChanged.connect(self.update_control)
        self.unlock_check.toggled.connect(self._unlock_changed)

        # --- 方向按钮：按下=基准叠加，松开=回到待机怠速 ---
        self.btn_up.pressed.connect(lambda: self.start_hold("前进", pitch=176))
        self.btn_up.released.connect(self.stop_hold_to_idle)
        self.btn_down.pressed.connect(lambda: self.start_hold("后退", pitch=80))
        self.btn_down.released.connect(self.stop_hold_to_idle)
        self.btn_left.pressed.connect(lambda: self.start_hold("左移", roll=80))
        self.btn_left.released.connect(self.stop_hold_to_idle)
        self.btn_right.pressed.connect(lambda: self.start_hold("右移", roll=176))
        self.btn_right.released.connect(self.stop_hold_to_idle)
        self.btn_yaw_l.pressed.connect(lambda: self.start_hold("左旋", yaw=80))
        self.btn_yaw_l.released.connect(self.stop_hold_to_idle)
        self.btn_yaw_r.pressed.connect(lambda: self.start_hold("右旋", yaw=176))
        self.btn_yaw_r.released.connect(self.stop_hold_to_idle)
        self.btn_thr_up.pressed.connect(lambda: self.start_hold("升高", throttle=192))
        self.btn_thr_up.released.connect(self.stop_hold_to_idle)
        self.btn_thr_down.pressed.connect(lambda: self.start_hold("降低", throttle=80))
        self.btn_thr_down.released.connect(self.stop_hold_to_idle)
        self.btn_hover.pressed.connect(lambda: self.start_hold("悬停", roll=128, pitch=128, throttle=IDLE_THROTTLE,
                                                               yaw=128))
        self.btn_hover.released.connect(self.stop_hold_to_idle)

        # --- 动作按钮 ---
        self.arm_btn.clicked.connect(self.arm_idle)
        self.disarm_btn.clicked.connect(self.disarm)
        hard_stop.clicked.connect(self.hard_stop)
        return page

    def _developer_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(8)

        row = QHBoxLayout()
        version = QPushButton("version")
        version.setToolTip("查询飞控固件版本")
        status = QPushButton("status")
        status.setToolTip("查询飞控运行状态")
        dump = QPushButton("dump")
        dump.setToolTip("导出飞控全部配置参数")
        enter_cli = QPushButton("进入 CLI (#)")
        enter_cli.setToolTip("发送 # 进入 Cleanflight CLI 模式")
        self.cli_edit = QLineEdit()
        self.cli_edit.setPlaceholderText("输入 Cleanflight CLI 命令，如 help / version / status / dump ...")
        send = QPushButton("发送 CLI")
        send.setToolTip("发送命令到飞控串口 CLI")
        send.setObjectName("primary")
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
        self.serial_console.setFont(QFont("Consolas", 9))
        self.serial_console.setStyleSheet(
            "QPlainTextEdit { background:#0f172a; color:#e2e8f0; border:1px solid #334155; }"
        )
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
        layout.setSpacing(8)

        title = QLabel("安全使用说明")
        title.setStyleSheet("QLabel { font-size:16px; font-weight:700; color:#1e293b; padding:4px 0; }")
        layout.addWidget(title)

        text = QLabel(
            "<style>"
            "  b { color:#2563eb; }"
            "  .warn { color:#dc2626; font-weight:600; }"
            "  li { margin:6px 0; line-height:1.6; }"
            "</style>"
            "<ol>"
            "<li><b>拆桨或固定机体</b> —— 测试前务必拆掉螺旋桨或用夹具固定无人机。</li>"
            "<li><b>连接无人机 WiFi</b> —— 将电脑连接到 <code>WiFiUFO-3BE7F2</code> 热点。</li>"
            "<li><b>点击 UFO 心跳</b> —— 启动 WiFi UFO UDP 40000 图传和控制通道。</li>"
            "<li><b>选择界面模式</b> —— 使用者界面只显示监控和飞行控制；开发者界面额外显示 CLI。</li>"
            "<li><b>持续方向控制</b> —— 按住方向按钮或键盘按键持续发送指令, 松开自动回中悬停。</li>"
            "<li><b>滑块微调</b> —— 拖动滑块实时调整控制量, 自动以 50ms 间隔持续发送。</li>"
            "<li class='warn'>所有飞行动作默认锁定，必须勾选安全确认后才会发出！</li>"
            "</ol>"
            "<br><b>键盘快捷键：</b><br>"
            "<table cellspacing='4'>"
            "<tr><td><b>W / ↑</b></td><td>前进</td>"
            "<td width='20'></td><td><b>S / ↓</b></td><td>后退</td></tr>"
            "<tr><td><b>A</b></td><td>左移</td>"
            "<td></td><td><b>D</b></td><td>右移</td></tr>"
            "<tr><td><b>Q</b></td><td>左旋</td>"
            "<td></td><td><b>E</b></td><td>右旋</td></tr>"
            "<tr><td><b>R</b></td><td>升高</td>"
            "<td></td><td><b>F</b></td><td>降低</td></tr>"
            "<tr><td><b>Space</b></td><td>悬停/回中</td><td></td><td></td><td></td></tr>"
            "</table>"
            "<br><b>控制协议参考：</b><br>"
            "WiFi UFO 控制包格式：<code>63 63 0a 00 00 08 00 66 [R] [P] [T] [Y] [M] [XOR]</code><br>"
            "字节 8-11: Roll / Pitch / Throttle / Yaw (0-255, 128=中位)<br>"
            "字节 12: 模式 (0=普通, 1=起飞, 2=降落, 4=急停)<br>"
            "字节 13: 字节 8-12 的 XOR 校验<br>"
            "<br><b>视频处理说明：</b><br>"
            "后台线程接收 UDP 视频分片，按 54 字节 JPEG 载荷偏移和分片序号重组，<br>"
            "丢弃尺寸变化的坏帧并以约 18 FPS 限帧显示，减少彩色闪烁伪影。"
        )
        text.setWordWrap(True)
        text.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        text.setTextFormat(Qt.RichText)
        text.setStyleSheet(
            "QLabel { background:white; padding:16px; border:1px solid #d5dde8; "
            "border-radius:8px; color:#334155; line-height:1.8; }"
        )
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
        worker.status_changed.connect(self._serial_status)
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
        if hasattr(self, 'toggle_dev_action'):
            self.toggle_dev_action.setChecked(developer)
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
        self.throttle.setValue(IDLE_THROTTLE)
        self.yaw.setValue(128)
        self.update_control()
        if self.wifi_worker:
            self.wifi_worker.enqueue(("hold_stop",))
            self.wifi_worker.enqueue(("stop_stream",))
        self.command_status.setText("当前命令：待机")

    @Slot(bool)
    def _unlock_changed(self, checked: bool) -> None:
        if checked:
            self.lock_label.setText("控制已解锁")
            self.lock_label.setStyleSheet(
                "QLabel { background:#ecfdf5; color:#065f46; border:1px solid #a7f3d0; "
                "border-radius:6px; padding:6px 10px; font-weight:500; }"
            )
            self.quick_lock.setText("控制：已解锁")
            self.quick_lock.setStyleSheet(
                "QLabel { background:#ecfdf5; color:#065f46; border:1px solid #a7f3d0; "
                "border-radius:6px; padding:6px 10px; font-weight:500; }"
            )
        else:
            self.lock_label.setText("控制锁定")
            self.lock_label.setStyleSheet(
                "QLabel { background:#fff7ed; color:#c2410c; border:1px solid #fed7aa; "
                "border-radius:6px; padding:6px 10px; font-weight:500; }"
            )
            self.quick_lock.setText("控制：默认锁定")
            self.quick_lock.setStyleSheet(
                "QLabel { background:#fff7ed; color:#c2410c; border:1px solid #fed7aa; "
                "border-radius:6px; padding:6px 10px; font-weight:500; }"
            )
        self.set_metric("安全锁", "已解锁" if checked else "未解锁")
        self.append_log("安全锁已手动解锁" if checked else "安全锁已锁定")

    def ensure_unlocked(self, action: str) -> bool:
        if self.unlock_check.isChecked():
            return True
        QMessageBox.warning(self, "控制已锁定", f"“{action}” 需要先勾选安全锁。请确认拆桨或固定机体后再解锁。")
        self.append_log(f"已阻止动作：{action}，原因：安全锁未解锁")
        return False

    def start_hold(
        self,
        label: str,
        roll: int = 128,
        pitch: int = 128,
        throttle: int = IDLE_THROTTLE,
        yaw: int = 128,
    ) -> None:
        """开始持续发送方向控制（按下按键/鼠标时调用）.
        默认油门为 IDLE_THROTTLE 待机怠速，保证电机持续低频旋转。"""
        if not self.ensure_unlocked(label):
            return
        self.open_udp()
        if self.wifi_worker:
            state = ControlState(roll, pitch, throttle, yaw)
            self.wifi_worker.enqueue(("hold_start", state, label))
        self.command_status.setText(f"持续控制：{label}")

    @Slot()
    def stop_hold(self) -> None:
        """完全停止控制（发送回中包后不再发送）."""
        if self.wifi_worker:
            self.wifi_worker.enqueue(("hold_stop",))
        self.command_status.setText("当前命令：已停机")

    @Slot()
    def stop_hold_to_idle(self) -> None:
        """松开方向键后回到待机怠速状态（而非完全停机）."""
        if self.wifi_worker:
            state = ControlState(128, 128, IDLE_THROTTLE, 128)
            self.wifi_worker.enqueue(("hold_start", state, "待机怠速"))
        self.command_status.setText("当前命令：待机怠速")

    @Slot()
    def arm_idle(self) -> None:
        """解锁并进入待机状态：发送起飞脉冲后自动维持电机低频旋转."""
        if not self.ensure_unlocked("解锁待机"):
            return
        if (
            QMessageBox.question(
                self,
                "确认解锁",
                "即将发送解锁指令，电机将进入低频怠速旋转状态。\n请确认桨叶安全、机体固定或处于安全飞行区。",
            )
            == QMessageBox.Yes
        ):
            self.open_udp()
            if self.wifi_worker:
                # 先发心跳预热连接，确保无人机已就绪
                self.wifi_worker.enqueue(("heartbeat",))
                self.wifi_worker.enqueue(("heartbeat",))
                # 发送 2 秒起飞脉冲 → 自动进入待机怠速
                self.wifi_worker.enqueue(("burst", 1, "解锁", 2.0, True))
                self.command_status.setText("当前命令：解锁中...")

    @Slot()
    def disarm(self) -> None:
        """锁定停机：发送降落指令停止所有电机."""
        if not self.ensure_unlocked("锁定停机"):
            return
        if QMessageBox.question(self, "确认锁定", "即将发送降落/锁定指令，电机将停止。") == QMessageBox.Yes:
            self.open_udp()
            if self.wifi_worker:
                self.wifi_worker.enqueue(("hold_stop",))
                self.wifi_worker.enqueue(("stop_stream",))
                self.wifi_worker.enqueue(("burst", 2, "锁定", 1.0, False))
                self.command_status.setText("当前命令：已锁定")

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
                self.wifi_worker.enqueue(("hold_stop",))
                self.wifi_worker.enqueue(("stop_stream",))
                self.wifi_worker.enqueue(("burst", 4, "急停", 1.0))

    @Slot(dict)
    def update_telemetry(self, values: dict) -> None:
        for key, value in values.items():
            self.set_metric(str(key), str(value))
        if "飞控固件" in values and self.serial_worker:
            port_info = f"{self.serial_worker.port_label} / {values['飞控固件']}"
            self.set_metric("串口飞控", port_info)

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
        if "已连接" in status or "发送中" in status:
            self.udp_status.setStyleSheet(
                "QLabel { background:#ecfdf5; color:#065f46; border:1px solid #a7f3d0; "
                "border-radius:6px; padding:6px 10px; font-weight:500; }"
            )
            self.quick_wifi.setText("WiFi：UDP 已连接")
            self.quick_wifi.setStyleSheet(
                "QLabel { background:#ecfdf5; color:#065f46; border:1px solid #a7f3d0; "
                "border-radius:6px; padding:6px 10px; font-weight:500; }"
            )
        elif "断开" in status or "错误" in status:
            self.udp_status.setStyleSheet(
                "QLabel { background:#fef2f2; color:#991b1b; border:1px solid #fecaca; "
                "border-radius:6px; padding:6px 10px; font-weight:500; }"
            )
            self.quick_wifi.setText("WiFi：已断开")
            self.quick_wifi.setStyleSheet(
                "QLabel { background:#fef2f2; color:#991b1b; border:1px solid #fecaca; "
                "border-radius:6px; padding:6px 10px; font-weight:500; }"
            )
        self.statusBar().showMessage(status, 4000)

    @Slot(str)
    def _serial_status(self, status: str) -> None:
        self.serial_status.setText(status)
        if "已连接" in status:
            self.serial_status.setStyleSheet(
                "QLabel { background:#ecfdf5; color:#065f46; border:1px solid #a7f3d0; "
                "border-radius:6px; padding:6px 10px; font-weight:500; }"
            )
            self.quick_fc.setText("飞控：已连接")
            self.quick_fc.setStyleSheet(
                "QLabel { background:#ecfdf5; color:#065f46; border:1px solid #a7f3d0; "
                "border-radius:6px; padding:6px 10px; font-weight:500; }"
            )
            if self.serial_worker:
                port_info = f"{self.serial_worker.port_label} @ {self.serial_worker.baud}"
                self.set_metric("串口飞控", port_info)
        elif "未连接" in status or "失败" in status:
            self.serial_status.setStyleSheet(
                "QLabel { background:#fef2f2; color:#991b1b; border:1px solid #fecaca; "
                "border-radius:6px; padding:6px 10px; font-weight:500; }"
            )
            self.quick_fc.setText("飞控：未连接")
            self.quick_fc.setStyleSheet(
                "QLabel { background:#fef2f2; color:#991b1b; border:1px solid #fecaca; "
                "border-radius:6px; padding:6px 10px; font-weight:500; }"
            )
            self.set_metric("串口飞控", "未连接")
        self.statusBar().showMessage(status, 4000)

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
            self.start_hold("前进", pitch=176)
        elif key in (Qt.Key_S, Qt.Key_Down):
            self.start_hold("后退", pitch=80)
        elif key == Qt.Key_A:
            self.start_hold("左移", roll=80)
        elif key == Qt.Key_D:
            self.start_hold("右移", roll=176)
        elif key == Qt.Key_Q:
            self.start_hold("左旋", yaw=80)
        elif key == Qt.Key_E:
            self.start_hold("右旋", yaw=176)
        elif key == Qt.Key_R:
            self.start_hold("升高", throttle=192)
        elif key == Qt.Key_F:
            self.start_hold("降低", throttle=64)
        elif key == Qt.Key_Space:
            self.start_hold("悬停")
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.isAutoRepeat():
            return
        key = event.key()
        movement_keys = {
            Qt.Key_W, Qt.Key_Up, Qt.Key_S, Qt.Key_Down,
            Qt.Key_A, Qt.Key_D, Qt.Key_Q, Qt.Key_E,
            Qt.Key_R, Qt.Key_F, Qt.Key_Space,
        }
        if key in movement_keys:
            self.stop_hold_to_idle()
        else:
            super().keyReleaseEvent(event)


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
