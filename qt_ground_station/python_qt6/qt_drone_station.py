from __future__ import annotations

import queue
import re
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QByteArray, QDateTime, QPointF, QRectF, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QIcon, QImage, QKeyEvent, QPainter, QPen, QPixmap
from PySide6.QtSerialPort import QSerialPort, QSerialPortInfo
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
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


def resolve_project_dir() -> Path:
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        return exe_dir.parent if exe_dir.name.lower() == "dist" else exe_dir
    return Path(__file__).resolve().parents[2]


PROJECT_DIR = resolve_project_dir()
RUNTIME_DIR = PROJECT_DIR / "runtime"
QT_LOG_DIR = RUNTIME_DIR / "qt6_logs"
QT_LOG_DIR.mkdir(parents=True, exist_ok=True)

WIFI_UFO_HEARTBEAT = bytes.fromhex("63 63 01 00 00 00 00")
WIFI_UFO_CONTROL_TEMPLATE = bytearray.fromhex("63 63 0a 00 00 08 00 66 80 80 80 80 00 00 99")
VIDEO_FRAGMENT_HEADER_OFFSET = 47
VIDEO_PAYLOAD_OFFSET = 54
CONTROL_INTERVAL = 0.02  # 标准遥控刷新目标 50Hz，避免过高包频让廉价 WiFi 飞控忽略控制
SERIAL_CONTROL_INTERVAL = 0.05  # MSP_SET_RAW_RC 走串口，20Hz 更稳，避免和遥测抢带宽
IDLE_THROTTLE = 128  # WiFiUFO 中位油门；起飞/解锁后用它维持待机/悬停控制
WIRED_IDLE_THROTTLE = 0  # MSP RC 油门最低；电机怠速由飞控的 arm/min_throttle 逻辑决定
HEARTBEAT_INTERVAL = 0.5
ARM_PULSE_DURATION = 1.0
ARMED_HOLD_MODE = 1
CLEANFLIGHT_ARM_DURATION = 4.0
MSP_RC_LOW = 1000
MSP_RC_MID = 1500
MSP_RC_HIGH = 2000


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


class AttitudeView(QWidget):
    """轻量姿态仪：用自绘地平线和机身示意展示 roll / pitch / yaw."""

    def __init__(self) -> None:
        super().__init__()
        self.roll = 0.0
        self.pitch = 0.0
        self.yaw = 0.0
        self.source = "无真实姿态回传"
        self.setMinimumSize(220, 185)

    def set_attitude(self, roll: float, pitch: float, yaw: float, source: str = "无真实姿态回传") -> None:
        self.roll = max(-60.0, min(60.0, float(roll)))
        self.pitch = max(-45.0, min(45.0, float(pitch)))
        self.yaw = ((float(yaw) + 180.0) % 360.0) - 180.0
        self.source = source
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = self.rect().adjusted(8, 8, -8, -8)
        painter.fillRect(self.rect(), QColor("#f8fafc"))
        painter.setPen(QPen(QColor("#d8e0ec"), 1))
        painter.setBrush(QBrush(QColor("#ffffff")))
        painter.drawRoundedRect(QRectF(rect), 8, 8)

        cx = rect.center().x()
        horizon_cy = rect.top() + rect.height() * 0.44
        radius = min(rect.width() * 0.42, rect.height() * 0.35)

        painter.save()
        painter.setClipRect(rect)
        painter.translate(cx, horizon_cy)
        painter.rotate(-self.roll)
        offset = self.pitch * radius / 35.0
        painter.fillRect(QRectF(-rect.width(), -rect.height() * 2 + offset, rect.width() * 2, rect.height() * 2), QColor("#bfdbfe"))
        painter.fillRect(QRectF(-rect.width(), offset, rect.width() * 2, rect.height() * 2), QColor("#bbf7d0"))
        painter.setPen(QPen(QColor("#2563eb"), 2))
        painter.drawLine(QPointF(-rect.width(), offset), QPointF(rect.width(), offset))
        painter.setPen(QPen(QColor("#64748b"), 1))
        for step in (-30, -20, -10, 10, 20, 30):
            y = offset - step * radius / 35.0
            painter.drawLine(QPointF(-28, y), QPointF(28, y))
        painter.restore()

        painter.setPen(QPen(QColor("#0f172a"), 2))
        painter.drawLine(QPointF(cx - 34, horizon_cy), QPointF(cx - 10, horizon_cy))
        painter.drawLine(QPointF(cx + 10, horizon_cy), QPointF(cx + 34, horizon_cy))
        painter.drawLine(QPointF(cx, horizon_cy - 10), QPointF(cx, horizon_cy + 10))

        body_cy = rect.top() + rect.height() * 0.61
        arm = min(rect.width(), rect.height()) * 0.17
        painter.save()
        painter.translate(cx, body_cy)
        painter.rotate(self.yaw)
        painter.setPen(QPen(QColor("#334155"), 4, Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(QPointF(-arm, -arm * 0.55), QPointF(arm, arm * 0.55))
        painter.drawLine(QPointF(-arm, arm * 0.55), QPointF(arm, -arm * 0.55))
        painter.setBrush(QBrush(QColor("#eff6ff")))
        painter.setPen(QPen(QColor("#2563eb"), 2))
        painter.drawRoundedRect(QRectF(-28, -16, 56, 32), 9, 9)
        painter.setBrush(QBrush(QColor("#dbeafe")))
        for x, y in ((-arm, -arm * 0.55), (arm, arm * 0.55), (-arm, arm * 0.55), (arm, -arm * 0.55)):
            painter.drawEllipse(QPointF(x, y), 12, 12)
        painter.setPen(QPen(QColor("#1d4ed8"), 2))
        painter.drawLine(QPointF(0, -24), QPointF(0, -40))
        painter.restore()

        painter.setPen(QPen(QColor("#334155"), 1))
        text_rect = QRectF(rect.left() + 12, rect.bottom() - 48, rect.width() - 24, 38)
        painter.drawText(
            text_rect,
            Qt.AlignCenter,
            f"Roll {self.roll:+.1f}°   Pitch {self.pitch:+.1f}°   Yaw {self.yaw:+.1f}°\n{self.source}",
        )


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
        self.send_lock = threading.Lock()
        self.control = ControlState()
        self.control_stream = False
        self.next_control_at = 0.0
        self.burst_until = 0.0
        self.burst_mode = 0
        self.burst_state = ControlState()
        self.burst_label = ""
        self.burst_auto_hover = False
        self.queued_burst: tuple[ControlState, int, str, float, bool] | None = None
        self.hold_active = False
        self.hold_state = ControlState()
        self.hold_mode = ARMED_HOLD_MODE
        self.hold_label = ""
        self.active_control_mode = 0
        self.burst_hold_mode = 0
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
        self.control_thread: threading.Thread | None = None

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
            self.control_thread = threading.Thread(target=self._control_loop, args=(sock,), daemon=True)
            self.control_thread.start()

            while not self.stop_event.is_set():
                self._read_socket(sock)
                time.sleep(0.001)
        except OSError as exc:
            self.status_changed.emit("UDP 错误")
            self.log_message.emit(f"UDP 线程错误：{exc}")
        finally:
            self.stop_event.set()
            if self.control_thread and self.control_thread.is_alive():
                self.control_thread.join(timeout=0.5)
            try:
                sock.close()
            except OSError:
                pass
            self.status_changed.emit("UDP 已断开")

    def _control_loop(self, sock: socket.socket) -> None:
        while not self.stop_event.is_set():
            self._drain_commands(sock)
            self._tick_control(sock)
            time.sleep(0.002)

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
            elif name == "set_active_mode":
                self.active_control_mode = int(command[1])
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
                self.burst_state = command[1].clamped()
                self.burst_mode = int(command[2])
                self.burst_label = str(command[3])
                self.burst_until = time.monotonic() + float(command[4])
                self.burst_auto_hover = len(command) > 5 and bool(command[5])
                self.burst_hold_mode = int(command[6]) if len(command) > 6 else self.active_control_mode
                self.next_control_at = 0.0
                self.log_message.emit(f"开始发送{self.burst_label}指令脉冲 {command[4]:.1f} 秒")
            elif name == "arm_sequence":
                self._send(sock, WIFI_UFO_HEARTBEAT)
                self.last_heartbeat_at = time.monotonic()
                self.active_control_mode = ARMED_HOLD_MODE
                self.burst_hold_mode = ARMED_HOLD_MODE
                self.hold_active = False
                self.control_stream = False
                self.queued_burst = None
                self.burst_state = command[1].clamped()
                self.burst_mode = ARMED_HOLD_MODE
                self.burst_label = "解锁待机"
                self.burst_until = time.monotonic() + ARM_PULSE_DURATION
                self.burst_auto_hover = True
                self.next_control_at = 0.0
                self.log_message.emit(
                    f"开始解锁序列：M{ARMED_HOLD_MODE} 脉冲 {ARM_PULSE_DURATION:.1f} 秒，随后继续 M{ARMED_HOLD_MODE} 待机"
                )
            elif name == "hold_start":
                self._send(sock, WIFI_UFO_HEARTBEAT)
                self.last_heartbeat_at = time.monotonic()
                self.hold_state = command[1].clamped()
                self.hold_label = str(command[2]) if len(command) > 2 else ""
                self.hold_mode = int(command[3]) if len(command) > 3 else self.active_control_mode
                self.hold_active = True
                self.next_control_at = 0.0
                self.log_message.emit(f"持续控制开始：{self.hold_label}")
                self.status_changed.emit("持续控制中")
            elif name == "hold_update":
                self.hold_state = command[1].clamped()
                self.hold_label = str(command[2]) if len(command) > 2 else self.hold_label
                self.hold_mode = int(command[3]) if len(command) > 3 else self.active_control_mode
                self.hold_active = True
                self.next_control_at = 0.0
            elif name == "hold_stop":
                self.hold_active = False
                # 立即发送回中包，不延迟
                self._send_control(sock, ControlState(), 0, "回中")
                self.next_control_at = time.monotonic() + CONTROL_INTERVAL
                self.log_message.emit(f"持续控制停止：{self.hold_label}")
                self.status_changed.emit("UDP 已连接")

    def _read_socket(self, sock: socket.socket) -> None:
        for _ in range(80):
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
            if now - self.last_heartbeat_at >= HEARTBEAT_INTERVAL:
                self._send(sock, WIFI_UFO_HEARTBEAT)
                self.last_heartbeat_at = now
            return

        sent_control = False

        # --- 优先级 1: 突发指令 (起飞/降落/急停) ---
        if self.burst_until > now:
            self._send_control(sock, self.burst_state, self.burst_mode, self.burst_label)
            self.next_control_at = now + CONTROL_INTERVAL
            sent_control = True

        if self.burst_until and self.burst_until <= now:
            self.log_message.emit(f"{self.burst_label}指令脉冲结束")
            self.burst_until = 0.0
            if self.queued_burst:
                state, mode, label, duration, auto_hover = self.queued_burst
                self.queued_burst = None
                self.burst_state = state
                self.burst_mode = mode
                self.burst_label = label
                self.burst_until = now + duration
                self.burst_auto_hover = auto_hover
                self.log_message.emit(f"开始发送{label}指令脉冲 {duration:.1f} 秒")
            elif self.burst_auto_hover:
                # 起飞后进入待机状态：电机低频旋转，等待方向指令
                self.hold_state = ControlState(128, 128, IDLE_THROTTLE, 128)
                self.hold_mode = self.burst_hold_mode
                self.hold_active = True
                self.hold_label = "待机怠速"
                self.log_message.emit("起飞完成，进入待机怠速状态（电机低频旋转）")
                self.status_changed.emit("待机中")
            self.next_control_at = now + CONTROL_INTERVAL
            sent_control = True

        # --- 优先级 2: 持续按键保持 ---
        if not sent_control and self.hold_active:
            self._send_control(sock, self.hold_state, self.hold_mode, self.hold_label)
            self.next_control_at = now + CONTROL_INTERVAL
            sent_control = True

        # --- 优先级 3: 滑块连续控制流 ---
        if not sent_control and self.control_stream:
            self._send_control(sock, self.control, self.active_control_mode, "手动控制")
            self.next_control_at = now + CONTROL_INTERVAL
            sent_control = True

        # 控制包不等价于心跳；保持周期心跳可避免 WiFiUFO 控制板退出控制态。
        if sent_control and now - self.last_heartbeat_at >= HEARTBEAT_INTERVAL:
            self._send(sock, WIFI_UFO_HEARTBEAT)
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
            with self.send_lock:
                sock.sendto(payload, (self.host, self.remote_port))
        except OSError as exc:
            self.log_message.emit(f"UDP 发送失败：{exc}")


class CleanflightSerialClient(QThread):
    log_message = Signal(str)
    status_changed = Signal(str)
    text_received = Signal(str)
    telemetry = Signal(dict)

    MSP_STATUS = 101
    MSP_RAW_IMU = 102
    MSP_ATTITUDE = 108
    MSP_ANALOG = 110
    MSP_SET_RAW_RC = 200

    def __init__(self) -> None:
        super().__init__()
        self.commands: queue.Queue[tuple] = queue.Queue()
        self.stop_event = threading.Event()
        self.port_label = ""
        self.baud = 115200
        self.dtr = False
        self.rts = False
        self.auto_cli = False
        self.line_ending = "\r\n"
        self.msp_buffer = bytearray()

    @staticmethod
    def ports() -> list[str]:
        labels = []
        for info in QSerialPortInfo.availablePorts():
            label = info.portName()
            details = []
            if info.description():
                details.append(info.description())
            if info.manufacturer():
                details.append(info.manufacturer())
            if info.hasVendorIdentifier() and info.hasProductIdentifier():
                details.append(f"VID:{info.vendorIdentifier():04X} PID:{info.productIdentifier():04X}")
            if details:
                label += f" - {' / '.join(details)}"
            labels.append(label)
        return labels

    def configure(
        self,
        port_label: str,
        baud: int,
        dtr: bool = False,
        rts: bool = False,
        auto_cli: bool = False,
        line_ending: str = "\r\n",
    ) -> None:
        self.port_label = port_label
        self.baud = baud
        self.dtr = dtr
        self.rts = rts
        self.auto_cli = auto_cli
        self.line_ending = line_ending

    def enqueue_line(self, line: str) -> None:
        self.commands.put(("line", line.strip()))

    def enqueue_cli_probe(self) -> None:
        self.commands.put(("probe_cli",))

    def enqueue_msp_probe(self) -> None:
        self.commands.put(("msp", self.MSP_STATUS, "MSP_STATUS"))
        self.commands.put(("msp", self.MSP_ANALOG, "MSP_ANALOG"))
        self.commands.put(("msp", self.MSP_RAW_IMU, "MSP_RAW_IMU"))
        self.commands.put(("msp", self.MSP_ATTITUDE, "MSP_ATTITUDE"))

    def enqueue_msp_attitude(self, silent: bool = False) -> None:
        self.commands.put(("msp", self.MSP_ATTITUDE, "MSP_ATTITUDE", silent))

    def enqueue_msp_rc(self, channels: list[int], label: str = "MSP_SET_RAW_RC", silent: bool = False) -> None:
        safe_channels = [max(900, min(2100, int(value))) for value in channels[:8]]
        while len(safe_channels) < 8:
            safe_channels.append(MSP_RC_LOW)
        payload = b"".join(value.to_bytes(2, "little", signed=False) for value in safe_channels)
        self.commands.put(("msp_payload", self.MSP_SET_RAW_RC, payload, label, silent))

    def enqueue_raw(self, payload: bytes, label: str = "RAW") -> None:
        self.commands.put(("raw", payload, label))

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
            serial.setDataTerminalReady(self.dtr)
            serial.setRequestToSend(self.rts)

            self.status_changed.emit("串口已连接")
            self.log_message.emit(
                f"串口已打开：{port} @ {self.baud} 8N1，DTR={'ON' if self.dtr else 'OFF'}，"
                f"RTS={'ON' if self.rts else 'OFF'}"
            )
            if self.auto_cli:
                self.commands.put(("probe_cli",))

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
                        payload = (cmd[1] + self.line_ending).encode("utf-8")
                        serial.write(payload)
                        serial.waitForBytesWritten(80)
                        self.log_message.emit(f"CLI TX：{cmd[1]}")
                    elif cmd[0] == "probe_cli":
                        for payload in (b"\r\n", b"#\r\n", b"version\r\n", b"status\r\n"):
                            serial.write(payload)
                            serial.waitForBytesWritten(80)
                            time.sleep(0.03)
                        self.log_message.emit("CLI 探针已发送：# / version / status")
                    elif cmd[0] == "msp":
                        payload = self._msp_request(int(cmd[1]))
                        serial.write(payload)
                        serial.waitForBytesWritten(80)
                        silent = len(cmd) > 3 and bool(cmd[3])
                        if not silent:
                            self.log_message.emit(f"MSP TX：{cmd[2]}")
                    elif cmd[0] == "msp_payload":
                        payload = self._msp_packet(int(cmd[1]), bytes(cmd[2]))
                        serial.write(payload)
                        serial.waitForBytesWritten(80)
                        if not bool(cmd[4]):
                            self.log_message.emit(f"MSP TX：{cmd[3]}")
                    elif cmd[0] == "raw":
                        serial.write(cmd[1])
                        serial.waitForBytesWritten(80)
                        self.log_message.emit(f"{cmd[2]} TX：{bytes(cmd[1]).hex(' ')}")

                if serial.waitForReadyRead(20):
                    raw = bytearray(bytes(serial.readAll()))
                    while serial.waitForReadyRead(5):
                        raw.extend(bytes(serial.readAll()))
                    if raw:
                        payload = bytes(raw)
                        self._parse_bytes(payload)
                        text = payload.decode("utf-8", errors="replace")
                        if self._looks_like_text(payload):
                            self.text_received.emit(text)
                        else:
                            self.text_received.emit(f"RX HEX：{payload.hex(' ')}")
                        self._parse_text(text)
        finally:
            if serial.isOpen():
                serial.close()
            self.status_changed.emit("串口未连接")
            self.log_message.emit("串口已关闭")

    @staticmethod
    def _looks_like_text(payload: bytes) -> bool:
        if not payload:
            return True
        printable = 0
        for value in payload:
            if value in (9, 10, 13) or 32 <= value <= 126 or value >= 0x80:
                printable += 1
        return printable / len(payload) >= 0.75

    @staticmethod
    def _msp_request(command: int) -> bytes:
        return CleanflightSerialClient._msp_packet(command, b"")

    @staticmethod
    def _msp_packet(command: int, payload: bytes) -> bytes:
        command &= 0xFF
        size = len(payload) & 0xFF
        checksum = size ^ command
        for value in payload:
            checksum ^= value
        return b"$M<" + bytes((size, command)) + payload + bytes((checksum,))

    @staticmethod
    def _int16(payload: bytes, offset: int) -> int:
        return int.from_bytes(payload[offset:offset + 2], "little", signed=True)

    @staticmethod
    def _uint16(payload: bytes, offset: int) -> int:
        return int.from_bytes(payload[offset:offset + 2], "little", signed=False)

    def _parse_bytes(self, payload: bytes) -> None:
        self.msp_buffer.extend(payload)
        while self.msp_buffer:
            start = self.msp_buffer.find(b"$M")
            if start < 0:
                self.msp_buffer[:] = self.msp_buffer[-2:]
                return
            if start:
                del self.msp_buffer[:start]
            if len(self.msp_buffer) < 6:
                return
            if self.msp_buffer[2] not in (ord(">"), ord("!")):
                del self.msp_buffer[0]
                continue
            size = self.msp_buffer[3]
            total = 6 + size
            if len(self.msp_buffer) < total:
                return
            command = self.msp_buffer[4]
            frame_payload = bytes(self.msp_buffer[5:5 + size])
            checksum = self.msp_buffer[5 + size]
            expected = size ^ command
            for value in frame_payload:
                expected ^= value
            if checksum == expected:
                self._handle_msp(command, frame_payload)
            else:
                self.log_message.emit(
                    f"MSP 校验失败：cmd={command} len={size} rx={checksum:02X} calc={expected:02X}"
                )
            del self.msp_buffer[:total]

    def _handle_msp(self, command: int, payload: bytes) -> None:
        data: dict[str, str | float] = {}
        if command == self.MSP_ATTITUDE and len(payload) >= 6:
            roll = self._int16(payload, 0) / 10.0
            pitch = self._int16(payload, 2) / 10.0
            yaw = float(self._int16(payload, 4))
            data.update({
                "roll": roll,
                "pitch": pitch,
                "yaw": yaw,
                "姿态来源": "MSP_ATTITUDE",
            })
        elif command == self.MSP_RAW_IMU and len(payload) >= 18:
            names = (
                "acc_x_raw", "acc_y_raw", "acc_z_raw",
                "gyro_x_raw", "gyro_y_raw", "gyro_z_raw",
                "mag_x_raw", "mag_y_raw", "mag_z_raw",
            )
            for index, name in enumerate(names):
                data[name] = self._int16(payload, index * 2)
            data["IMU 原始数据"] = "MSP_RAW_IMU"
        elif command == self.MSP_ANALOG and payload:
            data["voltage"] = payload[0] / 10.0
            data["电压来源"] = "MSP_ANALOG"
            if len(payload) >= 7:
                data["RSSI"] = str(self._uint16(payload, 3))
        elif command == self.MSP_STATUS and len(payload) >= 11:
            data["循环时间"] = f"{self._uint16(payload, 0)} us"
            data["I2C 错误"] = str(self._uint16(payload, 2))
            data["传感器掩码"] = str(self._uint16(payload, 4))
            data["飞控模式掩码"] = str(int.from_bytes(payload[6:10], "little", signed=False))
        if data:
            self.telemetry.emit(data)

    def _parse_text(self, text: str) -> None:
        data: dict[str, str] = {}
        version = re.search(r"(Cleanflight/[^\r\n]+)", text)
        if version:
            data["飞控固件"] = version.group(1).strip()
        betaflight = re.search(r"(Betaflight/[^\r\n]+)", text)
        if betaflight:
            data["飞控固件"] = betaflight.group(1).strip()
        inav = re.search(r"(INAV/[^\r\n]+)", text)
        if inav:
            data["飞控固件"] = inav.group(1).strip()
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


class SerialProbeWorker(QThread):
    progress = Signal(str)
    found = Signal(int, str)
    probe_finished = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.port_label = ""
        self.baud_values: list[int] = []
        self.dtr = False
        self.rts = False
        self.stop_event = threading.Event()

    def configure(self, port_label: str, baud_values: list[int], dtr: bool = False, rts: bool = False) -> None:
        self.port_label = port_label
        self.baud_values = baud_values
        self.dtr = dtr
        self.rts = rts
        self.stop_event.clear()

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        port = self.port_label.split(" - ", 1)[0].strip()
        for baud in self.baud_values:
            if self.stop_event.is_set():
                self.probe_finished.emit(False)
                return
            serial = QSerialPort()
            serial.setPortName(port)
            serial.setBaudRate(baud)
            serial.setDataBits(QSerialPort.Data8)
            serial.setParity(QSerialPort.NoParity)
            serial.setStopBits(QSerialPort.OneStop)
            serial.setFlowControl(QSerialPort.NoFlowControl)
            if not serial.open(QSerialPort.ReadWrite):
                self.progress.emit(f"探测 {baud} 失败：{serial.errorString()}")
                continue
            serial.setDataTerminalReady(self.dtr)
            serial.setRequestToSend(self.rts)
            serial.write(b"\r\n#\r\nversion\r\nstatus\r\n")
            serial.waitForBytesWritten(120)
            response = bytearray()
            deadline = time.monotonic() + 0.45
            while time.monotonic() < deadline and not self.stop_event.is_set():
                if serial.waitForReadyRead(50):
                    response.extend(bytes(serial.readAll()))
            serial.write(b"exit\r\n")
            serial.waitForBytesWritten(100)
            serial.close()
            if response:
                text = bytes(response).decode("utf-8", errors="replace").strip()
                self.found.emit(baud, text[:500] if text else bytes(response).hex(" "))
                self.probe_finished.emit(True)
                return
            self.progress.emit(f"波特率 {baud} 无返回")
        self.probe_finished.emit(False)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.wifi_worker: WifiUfoWorker | None = None
        self.serial_worker: CleanflightSerialClient | None = None
        self.serial_probe_worker: SerialProbeWorker | None = None
        self.metric_rows: dict[str, int] = {}
        self.sensor_cards: dict[str, QLabel] = {}
        self.sensor_titles: dict[str, str] = {}
        self.live_attitude = False
        self.last_live_attitude_at = 0.0
        self.last_control_log = 0.0
        self.last_control_status = 0.0
        self.last_serial_control_log = 0.0
        self.flight_armed = False
        self.serial_rc_active = False
        self.serial_rc_state = ControlState(128, 128, WIRED_IDLE_THROTTLE, 128)
        self.serial_rc_label = "有线待机"
        self.serial_burst_until = 0.0
        self.serial_burst_state = ControlState(128, 128, WIRED_IDLE_THROTTLE, 128)
        self.serial_burst_label = ""
        self.serial_burst_auto_idle = False
        self.log_path = QT_LOG_DIR / f"qt6_ground_station_{QDateTime.currentDateTime().toString('yyyyMMdd_HHmmss')}.log"
        self.log_file = self.log_path.open("a", encoding="utf-8")
        self._build_ui()
        self.msp_timer = QTimer(self)
        self.msp_timer.setInterval(200)
        self.msp_timer.timeout.connect(self.poll_msp_attitude)
        self.serial_control_timer = QTimer(self)
        self.serial_control_timer.setInterval(int(SERIAL_CONTROL_INTERVAL * 1000))
        self.serial_control_timer.timeout.connect(self.tick_serial_control)
        self.wireless_telemetry_timer = QTimer(self)
        self.wireless_telemetry_timer.setInterval(700)
        self.wireless_telemetry_timer.timeout.connect(self.poll_wireless_telemetry)
        self.refresh_ports()
        self.apply_profile()
        self.update_control_transport_status()
        self.reset_hover()
        self.mark_attitude_unavailable()
        self.apply_interface_mode()
        self.append_log("Qt6/PySide6 中文上位机已启动")

    def closeEvent(self, event) -> None:  # noqa: N802
        if hasattr(self, "msp_timer"):
            self.msp_timer.stop()
        if hasattr(self, "serial_control_timer"):
            self.serial_control_timer.stop()
        if hasattr(self, "wireless_telemetry_timer"):
            self.wireless_telemetry_timer.stop()
        if self.serial_probe_worker:
            self.serial_probe_worker.stop()
            self.serial_probe_worker.wait(300)
        self.close_udp()
        self.close_serial()
        self.log_file.close()
        super().closeEvent(event)

    def _build_ui(self) -> None:
        self.setWindowTitle("无人机 Qt6 可视化上位机")
        self.resize(1580, 900)
        self.setMinimumSize(1320, 760)
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
            QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
                background:white; border:1px solid #c4cdd9; border-radius:5px;
                padding:5px; color:#334155;
            }
            QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus { border-color:#3b82f6; }
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
        splitter.addWidget(self._monitor_tab())
        splitter.addWidget(self._center_panel())
        splitter.addWidget(self._control_panel())
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setStretchFactor(2, 1)
        splitter.setSizes([470, 760, 350])
        root.addWidget(splitter, 1)

        self.dev_tabs = QTabWidget()
        self.dev_tabs.setMaximumHeight(420)
        self.dev_console_index = self.dev_tabs.addTab(self._developer_tab(), "开发者 CLI")
        self.algorithm_index = self.dev_tabs.addTab(self._algorithm_tab(), "算法/调参/烧录")
        self.safety_index = self.dev_tabs.addTab(self._safety_tab(), "安全/说明")
        root.addWidget(self.dev_tabs)
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

    def _top_panel(self) -> QTabWidget:
        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        tabs.setMaximumHeight(190)

        overview = QWidget()
        overview_layout = QGridLayout(overview)
        overview_layout.setVerticalSpacing(8)
        overview_layout.setHorizontalSpacing(10)
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["使用者界面", "开发者界面"])
        self.mode_combo.setToolTip("使用者界面隐藏底部开发者区，开发者界面显示 CLI 与安全说明")
        self.profile_combo = QComboBox()
        self.profile_combo.addItems(["WiFi UFO UDP", "Tello 兼容 UDP", "仅串口 Cleanflight"])
        self.profile_combo.setToolTip("选择通信协议，自动配置默认端口")
        self.connection_hint = card_label("连接策略：无线/有线遥测默认开启", "#eff6ff", "#1e40af")
        self.control_transport_combo = QComboBox()
        self.control_transport_combo.addItems([
            "自动：有线优先 + WiFi 图传",
            "无线 UDP 控制/图传",
            "有线 MSP RC 控制",
            "混合：有线控电机 + WiFi 图传",
        ])
        self.control_transport_combo.setToolTip("普通串口无法承载摄像图传；需要图像时请使用 WiFi UDP 或混合模式")
        self.control_transport_status = card_label("控制：自动，有线优先；图传：WiFi UDP", "#f8fafc", "#334155")
        overview_layout.addWidget(QLabel("界面模式"), 0, 0)
        overview_layout.addWidget(self.mode_combo, 0, 1)
        overview_layout.addWidget(QLabel("协议"), 0, 2)
        overview_layout.addWidget(self.profile_combo, 0, 3)
        overview_layout.addWidget(self.connection_hint, 0, 4, 1, 3)
        overview_layout.addWidget(QLabel("控制链路"), 1, 0)
        overview_layout.addWidget(self.control_transport_combo, 1, 1, 1, 3)
        overview_layout.addWidget(self.control_transport_status, 1, 4, 1, 3)
        overview_layout.setColumnStretch(4, 1)
        tabs.addTab(overview, "连接概览")

        udp_page = QWidget()
        udp_layout = QGridLayout(udp_page)
        udp_layout.setVerticalSpacing(8)
        udp_layout.setHorizontalSpacing(10)
        self.ip_edit = QLineEdit("192.168.0.1")
        self.ip_edit.setToolTip("无人机 WiFi 模块 IP 地址")
        self.remote_udp = QSpinBox()
        self.remote_udp.setRange(1, 65535)
        self.remote_udp.setValue(40000)
        self.remote_udp.setToolTip("无人机 UDP 端口")
        self.local_udp = QSpinBox()
        self.local_udp.setRange(1, 65535)
        self.local_udp.setValue(40000)
        self.local_udp.setToolTip("本地上位机 UDP 监听端口")
        open_udp = QPushButton("打开 UDP")
        open_udp.setObjectName("primary")
        open_udp.setToolTip("打开 UDP 连接，开始接收图传和发送控制")
        close_udp = QPushButton("关闭 UDP")
        heartbeat = QPushButton("UFO 心跳")
        heartbeat.setToolTip("向无人机发送 WiFi UFO 心跳包，启动图传/信息回传")
        self.udp_status = card_label("UDP 未连接", "#fef2f2", "#991b1b")
        udp_layout.addWidget(QLabel("无人机 IP"), 0, 0)
        udp_layout.addWidget(self.ip_edit, 0, 1)
        udp_layout.addWidget(QLabel("远端端口"), 0, 2)
        udp_layout.addWidget(self.remote_udp, 0, 3)
        udp_layout.addWidget(QLabel("本地端口"), 0, 4)
        udp_layout.addWidget(self.local_udp, 0, 5)
        udp_layout.addWidget(open_udp, 0, 6)
        udp_layout.addWidget(close_udp, 0, 7)
        udp_layout.addWidget(heartbeat, 0, 8)
        udp_layout.addWidget(self.udp_status, 0, 9)
        udp_layout.setColumnStretch(9, 1)
        tabs.addTab(udp_page, "无线 UDP")

        serial_page = QWidget()
        serial_layout = QGridLayout(serial_page)
        serial_layout.setVerticalSpacing(8)
        serial_layout.setHorizontalSpacing(10)
        self.serial_combo = QComboBox()
        self.serial_combo.setMinimumWidth(270)
        self.serial_combo.setToolTip("选择飞控对应的串口")
        self.baud_spin = QSpinBox()
        self.baud_spin.setRange(1200, 2_000_000)
        self.baud_spin.setValue(115200)
        self.baud_spin.setToolTip("串口波特率，Cleanflight 默认 115200")
        refresh = QPushButton("刷新串口")
        refresh.setToolTip("重新扫描可用串口列表")
        open_serial = QPushButton("连接串口")
        open_serial.setObjectName("primary")
        open_serial.setToolTip("连接到选中的飞控串口")
        close_serial = QPushButton("关闭串口")
        self.serial_status = card_label("串口未连接", "#fef2f2", "#991b1b")
        serial_layout.addWidget(QLabel("飞控串口"), 0, 0)
        serial_layout.addWidget(self.serial_combo, 0, 1, 1, 3)
        serial_layout.addWidget(QLabel("波特率"), 0, 4)
        serial_layout.addWidget(self.baud_spin, 0, 5)
        serial_layout.addWidget(refresh, 0, 6)
        serial_layout.addWidget(open_serial, 0, 7)
        serial_layout.addWidget(close_serial, 0, 8)
        serial_layout.addWidget(self.serial_status, 0, 9)
        serial_layout.setColumnStretch(9, 1)
        tabs.addTab(serial_page, "有线串口")

        telemetry_page = QWidget()
        telemetry_layout = QGridLayout(telemetry_page)
        telemetry_layout.setVerticalSpacing(8)
        telemetry_layout.setHorizontalSpacing(10)
        self.wired_sensor_check = QCheckBox("有线 MSP 传感器默认开启")
        self.wired_sensor_check.setChecked(True)
        self.wired_sensor_check.setToolTip("串口连接成功后自动启动 MSP 姿态轮询")
        self.wireless_sensor_check = QCheckBox("无线 UDP 遥测/图传心跳默认开启")
        self.wireless_sensor_check.setChecked(True)
        self.wireless_sensor_check.setToolTip("UDP 打开后自动周期发送 UFO 心跳，维持无线信息/图传入口")
        self.auto_telemetry_status = card_label("遥测：有线/无线均为默认开启", "#ecfdf5", "#065f46")
        telemetry_layout.addWidget(self.wired_sensor_check, 0, 0)
        telemetry_layout.addWidget(self.wireless_sensor_check, 0, 1)
        telemetry_layout.addWidget(self.auto_telemetry_status, 0, 2, 1, 4)
        telemetry_layout.setColumnStretch(2, 1)
        tabs.addTab(telemetry_page, "遥测策略")

        self.mode_combo.currentTextChanged.connect(self.apply_interface_mode)
        self.profile_combo.currentTextChanged.connect(self.apply_profile)
        self.control_transport_combo.currentTextChanged.connect(self.update_control_transport_status)
        self.wired_sensor_check.toggled.connect(self.telemetry_strategy_changed)
        self.wireless_sensor_check.toggled.connect(self.telemetry_strategy_changed)
        open_udp.clicked.connect(self.open_udp)
        close_udp.clicked.connect(self.close_udp)
        heartbeat.clicked.connect(self.send_heartbeat)
        refresh.clicked.connect(self.refresh_ports)
        open_serial.clicked.connect(self.open_serial)
        close_serial.clicked.connect(self.close_serial)
        return tabs

    def _center_panel(self) -> QWidget:
        center = QWidget()
        layout = QVBoxLayout(center)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        video_group = QGroupBox("图传预览")
        video_layout = QVBoxLayout(video_group)
        self.video_label = QLabel("等待 WiFi UFO 视频流...")
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setMinimumSize(520, 360)
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
        return center

    def _monitor_tab(self) -> QWidget:
        page = QWidget()
        page.setMinimumWidth(420)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        quick_group = QGroupBox("飞行状态")
        quick = QGridLayout(quick_group)
        self.quick_wifi = card_label("WiFi：等待连接", "#fef2f2", "#991b1b")
        self.quick_fc = card_label("飞控：未连接", "#fef2f2", "#991b1b")
        self.quick_lock = card_label("控制：默认锁定", "#fff7ed", "#c2410c")
        self.quick_wifi.setMinimumHeight(36)
        self.quick_fc.setMinimumHeight(36)
        self.quick_lock.setMinimumHeight(36)
        self.quick_wifi.setWordWrap(True)
        self.quick_fc.setWordWrap(True)
        self.quick_lock.setWordWrap(True)
        quick.addWidget(self.quick_wifi, 0, 0)
        quick.addWidget(self.quick_fc, 0, 1)
        quick.addWidget(self.quick_lock, 0, 2)
        quick.setColumnStretch(0, 1)
        quick.setColumnStretch(1, 1)
        quick.setColumnStretch(2, 1)
        layout.addWidget(quick_group)

        attitude_group = QGroupBox("姿态与机身状态")
        attitude_layout = QVBoxLayout(attitude_group)
        self.attitude_view = AttitudeView()
        self.attitude_status = card_label("姿态：无真实回传", "#f8fafc", "#64748b")
        attitude_layout.addWidget(self.attitude_view)
        attitude_layout.addWidget(self.attitude_status)
        layout.addWidget(attitude_group, 2)

        sensor_group = QGroupBox("传感器信息")
        sensor_grid = QGridLayout(sensor_group)
        sensor_grid.setHorizontalSpacing(6)
        sensor_grid.setVerticalSpacing(6)
        sensor_items = [
            ("姿态 Roll", "roll", "--"),
            ("姿态 Pitch", "pitch", "--"),
            ("姿态 Yaw", "yaw", "--"),
            ("加速度 X", "acc_x", "--"),
            ("加速度 Y", "acc_y", "--"),
            ("加速度 Z", "acc_z", "--"),
            ("陀螺 X", "gyro_x", "--"),
            ("陀螺 Y", "gyro_y", "--"),
            ("陀螺 Z", "gyro_z", "--"),
            ("高度", "altitude", "--"),
            ("电压", "voltage", "--"),
            ("IMU", "imu", "等待"),
        ]
        for index, (title, key, default) in enumerate(sensor_items):
            row = index // 3
            column = index % 3
            value_label = card_label(f"{title}\n{default}", "#f8fafc", "#334155")
            value_label.setMinimumHeight(46)
            value_label.setWordWrap(True)
            sensor_grid.addWidget(value_label, row, column)
            self.sensor_cards[key] = value_label
            self.sensor_titles[key] = title
        sensor_grid.setColumnStretch(0, 1)
        sensor_grid.setColumnStretch(1, 1)
        sensor_grid.setColumnStretch(2, 1)
        layout.addWidget(sensor_group, 2)

        metric_group = QGroupBox("遥测明细")
        metric_layout = QVBoxLayout(metric_group)
        self.metric_table = QTableWidget(0, 2)
        self.metric_table.setHorizontalHeaderLabels(["字段", "值"])
        self.metric_table.horizontalHeader().setStretchLastSection(True)
        self.metric_table.verticalHeader().setVisible(False)
        self.metric_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.metric_table.setAlternatingRowColors(True)
        self.metric_table.setStyleSheet(
            "QTableWidget { alternate-background-color:#f8fafc; }"
        )
        metric_layout.addWidget(self.metric_table)
        metric_group.setMinimumHeight(260)
        layout.addWidget(metric_group, 2)
        self.set_metric("无人机 IP", "192.168.0.1")
        self.set_metric("WiFi 协议", "WiFi UFO UDP 40000")
        self.set_metric("串口飞控", "未连接")
        self.set_metric("安全锁", "未解锁")
        return page

    def _control_panel(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setMinimumWidth(350)
        scroll.setWidget(self._control_tab())
        return scroll

    def _control_tab(self) -> QWidget:
        page = QWidget()
        page.setMinimumWidth(330)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
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

        self.control_link_card = card_label("控制链路：自动，有线优先", "#f8fafc", "#334155")
        layout.addWidget(self.control_link_card)

        arm_mode = QGroupBox("解锁方案")
        arm_mode_layout = QGridLayout(arm_mode)
        self.arm_profile_combo = QComboBox()
        self.arm_profile_combo.addItems([
            "Cleanflight 摇杆解锁 / M0 控制",
            "WiFiUFO M1 起飞 / M1 控制",
            "WiFiUFO M1 起飞 / M0 控制",
        ])
        self.arm_profile_combo.setToolTip("如果启动待机无反应，请在此切换协议方案后重新解锁测试")
        self.control_mode_status = card_label("控制模式：M0", "#f8fafc", "#334155")
        arm_mode_layout.addWidget(QLabel("当前方案"), 0, 0)
        arm_mode_layout.addWidget(self.arm_profile_combo, 0, 1)
        arm_mode_layout.addWidget(self.control_mode_status, 1, 0, 1, 2)
        arm_mode_layout.setColumnStretch(1, 1)
        layout.addWidget(arm_mode)

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
        self.arm_profile_combo.currentTextChanged.connect(self.update_arm_profile_status)

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
        self.update_arm_profile_status()
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
        exit_cli = QPushButton("退出 CLI")
        exit_cli.setToolTip("发送 exit 返回 MSP/正常串口模式")
        self.cli_edit = QLineEdit()
        self.cli_edit.setPlaceholderText("输入 Cleanflight CLI 命令，如 help / version / status / dump ...")
        send = QPushButton("发送 CLI")
        send.setToolTip("发送命令到飞控串口 CLI")
        send.setObjectName("primary")
        row.addWidget(version)
        row.addWidget(status)
        row.addWidget(dump)
        row.addWidget(enter_cli)
        row.addWidget(exit_cli)
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
        exit_cli.clicked.connect(lambda: self.send_cli_line("exit"))
        send.clicked.connect(self.send_cli_from_edit)
        self.cli_edit.returnPressed.connect(self.send_cli_from_edit)
        return page

    def _algorithm_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(8)

        diag = QGroupBox("串口诊断与 MSP 遥测")
        diag_grid = QGridLayout(diag)
        self.serial_dtr_check = QCheckBox("DTR")
        self.serial_rts_check = QCheckBox("RTS")
        self.serial_auto_cli_check = QCheckBox("连接后自动 CLI 探针")
        self.serial_auto_cli_check.setChecked(False)
        self.serial_line_ending_combo = QComboBox()
        self.serial_line_ending_combo.addItems(["CRLF", "LF", "CR"])
        self.serial_diag_status = card_label("诊断：等待", "#f8fafc", "#64748b")
        auto_baud = QPushButton("自动波特率探测")
        cli_probe = QPushButton("CLI 探针")
        msp_probe = QPushButton("MSP 全量探针")
        msp_once = QPushButton("MSP 姿态一次")
        self.msp_poll_btn = QPushButton("开始 MSP 姿态轮询")
        diag_grid.addWidget(QLabel("线路"), 0, 0)
        diag_grid.addWidget(self.serial_dtr_check, 0, 1)
        diag_grid.addWidget(self.serial_rts_check, 0, 2)
        diag_grid.addWidget(self.serial_auto_cli_check, 0, 3)
        diag_grid.addWidget(QLabel("换行"), 0, 4)
        diag_grid.addWidget(self.serial_line_ending_combo, 0, 5)
        diag_grid.addWidget(auto_baud, 1, 0, 1, 2)
        diag_grid.addWidget(cli_probe, 1, 2)
        diag_grid.addWidget(msp_probe, 1, 3)
        diag_grid.addWidget(msp_once, 1, 4)
        diag_grid.addWidget(self.msp_poll_btn, 1, 5)
        diag_grid.addWidget(self.serial_diag_status, 2, 0, 1, 6)
        diag_grid.setColumnStretch(5, 1)
        layout.addWidget(diag)

        sensor_stream = QGroupBox("传感器传输")
        stream_grid = QGridLayout(sensor_stream)
        self.telemetry_mode_status = card_label("传感器：有线 MSP / 无线 UDP 默认开启", "#ecfdf5", "#065f46")
        start_wired = QPushButton("启动有线 MSP")
        stop_wired = QPushButton("停止有线 MSP")
        start_wireless = QPushButton("启动无线心跳")
        stop_wireless = QPushButton("停止无线心跳")
        stream_grid.addWidget(self.telemetry_mode_status, 0, 0, 1, 4)
        stream_grid.addWidget(start_wired, 1, 0)
        stream_grid.addWidget(stop_wired, 1, 1)
        stream_grid.addWidget(start_wireless, 1, 2)
        stream_grid.addWidget(stop_wireless, 1, 3)
        stream_grid.setColumnStretch(0, 1)
        layout.addWidget(sensor_stream)

        params = QGroupBox("算法参数调试")
        params_layout = QGridLayout(params)
        self.pid_axis_combo = QComboBox()
        self.pid_axis_combo.addItems(["roll", "pitch", "yaw"])
        self.pid_p_spin = QDoubleSpinBox()
        self.pid_i_spin = QDoubleSpinBox()
        self.pid_d_spin = QDoubleSpinBox()
        for spin, value in ((self.pid_p_spin, 40.0), (self.pid_i_spin, 30.0), (self.pid_d_spin, 23.0)):
            spin.setRange(0.0, 500.0)
            spin.setDecimals(2)
            spin.setSingleStep(0.5)
            spin.setValue(value)
        make_pid = QPushButton("生成 PID CLI")
        self.param_filter_edit = QLineEdit()
        self.param_filter_edit.setPlaceholderText("参数过滤，如 pid / gyro / acc / looptime")
        get_param = QPushButton("get 参数")
        diff_params = QPushButton("读取 diff")
        help_params = QPushButton("CLI help")
        read_params = QPushButton("读取状态/参数")
        send_batch = QPushButton("发送参数批处理")
        save_params = QPushButton("保存参数")
        self.parameter_batch = QPlainTextEdit()
        self.parameter_batch.setMaximumBlockCount(1000)
        self.parameter_batch.setPlaceholderText(
            "先用 dump/status 确认飞控支持的参数名；每行一条 CLI 命令，# 开头为注释。"
        )
        self.parameter_batch.setPlainText(
            "# 示例：请先读取 dump，按实际飞控参数名调整\n"
            "# set looptime = 3500\n"
            "# set gyro_lpf = 42\n"
            "# set p_pitch = 40\n"
        )
        params_layout.addWidget(QLabel("轴向"), 0, 0)
        params_layout.addWidget(self.pid_axis_combo, 0, 1)
        params_layout.addWidget(QLabel("P"), 0, 2)
        params_layout.addWidget(self.pid_p_spin, 0, 3)
        params_layout.addWidget(QLabel("I"), 0, 4)
        params_layout.addWidget(self.pid_i_spin, 0, 5)
        params_layout.addWidget(QLabel("D"), 0, 6)
        params_layout.addWidget(self.pid_d_spin, 0, 7)
        params_layout.addWidget(make_pid, 0, 8)
        params_layout.addWidget(QLabel("过滤"), 1, 0)
        params_layout.addWidget(self.param_filter_edit, 1, 1, 1, 3)
        params_layout.addWidget(get_param, 1, 4)
        params_layout.addWidget(diff_params, 1, 5)
        params_layout.addWidget(help_params, 1, 6)
        params_layout.addWidget(self.parameter_batch, 2, 0, 1, 9)
        params_layout.addWidget(read_params, 3, 0, 1, 2)
        params_layout.addWidget(send_batch, 3, 2, 1, 2)
        params_layout.addWidget(save_params, 3, 4, 1, 2)
        params_layout.setColumnStretch(8, 1)
        layout.addWidget(params)

        firmware = QGroupBox("固件烧录准备")
        firmware_grid = QGridLayout(firmware)
        self.firmware_target_combo = QComboBox()
        self.firmware_target_combo.addItems(["未知飞控/仅预检", "Cleanflight 兼容", "Betaflight 兼容", "INAV 兼容"])
        self.firmware_path_edit = QLineEdit()
        self.firmware_path_edit.setReadOnly(True)
        self.firmware_path_edit.setPlaceholderText("选择 .hex / .bin 固件文件")
        self.flash_status = card_label("烧录：等待飞控型号、MCU 与 Bootloader 识别", "#fff7ed", "#c2410c")
        choose_fw = QPushButton("选择固件")
        preflight = QPushButton("烧录前检查")
        bootloader = QPushButton("尝试进入 Bootloader")
        flash = QPushButton("开始烧录")
        flash.setEnabled(False)
        flash.setToolTip("未知飞控暂不开放直接刷写；完成 MCU/Bootloader 识别后再启用")
        firmware_grid.addWidget(QLabel("目标"), 0, 0)
        firmware_grid.addWidget(self.firmware_target_combo, 0, 1)
        firmware_grid.addWidget(self.flash_status, 0, 2, 1, 4)
        firmware_grid.addWidget(QLabel("固件文件"), 1, 0)
        firmware_grid.addWidget(self.firmware_path_edit, 1, 1, 1, 4)
        firmware_grid.addWidget(choose_fw, 1, 5)
        firmware_grid.addWidget(preflight, 2, 0, 1, 2)
        firmware_grid.addWidget(bootloader, 2, 2, 1, 2)
        firmware_grid.addWidget(flash, 2, 4, 1, 2)
        firmware_grid.setColumnStretch(1, 1)
        layout.addWidget(firmware)
        layout.addStretch(1)

        auto_baud.clicked.connect(self.auto_probe_serial)
        cli_probe.clicked.connect(self.send_cli_probe)
        msp_probe.clicked.connect(self.send_msp_probe)
        msp_once.clicked.connect(self.send_msp_attitude)
        self.msp_poll_btn.clicked.connect(self.toggle_msp_poll)
        start_wired.clicked.connect(self.start_wired_sensor_stream)
        stop_wired.clicked.connect(self.stop_wired_sensor_stream)
        start_wireless.clicked.connect(self.start_wireless_sensor_stream)
        stop_wireless.clicked.connect(self.stop_wireless_sensor_stream)
        make_pid.clicked.connect(self.generate_pid_cli)
        get_param.clicked.connect(self.get_parameter_filter)
        diff_params.clicked.connect(self.read_diff_parameters)
        help_params.clicked.connect(self.read_cli_help)
        read_params.clicked.connect(self.read_parameters)
        send_batch.clicked.connect(self.send_parameter_batch)
        save_params.clicked.connect(self.save_parameters)
        choose_fw.clicked.connect(self.choose_firmware_file)
        preflight.clicked.connect(self.firmware_preflight)
        bootloader.clicked.connect(self.request_bootloader)

        scroll.setWidget(page)
        return scroll

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
    def update_control_transport_status(self) -> None:
        text = self.control_transport_combo.currentText() if hasattr(self, "control_transport_combo") else "自动"
        if "有线 MSP RC" in text:
            status = "控制：有线 MSP RC；图传：需 WiFi UDP"
        elif "混合" in text:
            status = "控制：有线 MSP RC；图传：WiFi UDP"
        elif "无线" in text:
            status = "控制/图传：WiFi UDP"
        else:
            status = "控制：自动，有线优先；图传：WiFi UDP"
        if hasattr(self, "control_transport_status"):
            self.control_transport_status.setText(status)
        if hasattr(self, "control_link_card"):
            self.control_link_card.setText(status)
        self.set_metric("控制链路", status)

    def serial_link_ready(self) -> bool:
        return bool(self.serial_worker and self.serial_worker.isRunning())

    def use_wired_control(self) -> bool:
        text = self.control_transport_combo.currentText() if hasattr(self, "control_transport_combo") else "自动"
        if text.startswith("自动"):
            return self.serial_link_ready()
        if "无线 UDP" in text:
            return False
        if "有线" in text or "混合" in text:
            return True
        return False

    def use_wifi_control(self) -> bool:
        text = self.control_transport_combo.currentText() if hasattr(self, "control_transport_combo") else "自动"
        if text.startswith("自动"):
            return not self.serial_link_ready()
        if "无线 UDP" in text:
            return True
        if "有线" in text or "混合" in text:
            return False
        return True

    def should_keep_wifi_video(self) -> bool:
        text = self.control_transport_combo.currentText() if hasattr(self, "control_transport_combo") else "自动"
        wireless_enabled = self.wireless_sensor_check.isChecked() if hasattr(self, "wireless_sensor_check") else True
        return wireless_enabled or "图传" in text or "混合" in text

    def prepare_video_link(self) -> None:
        if self.should_keep_wifi_video():
            self.open_udp()

    def prepare_control_link(self, action: str) -> bool:
        self.update_control_transport_status()
        if self.use_wired_control():
            if not self.serial_link_ready():
                QMessageBox.information(self, "有线控制未连接", f"“{action}” 选择了有线 MSP RC 控制，请先连接飞控串口。")
                self.append_log(f"已阻止动作：{action}，原因：有线 MSP RC 未连接")
                return False
            if self.serial_worker:
                self.serial_worker.enqueue_line("exit")
            self.prepare_video_link()
            return True
        self.open_udp()
        return True

    @Slot()
    def apply_interface_mode(self) -> None:
        developer = self.mode_combo.currentText() == "开发者界面"
        if hasattr(self, "dev_tabs"):
            self.dev_tabs.setVisible(developer)
        if hasattr(self, 'toggle_dev_action'):
            self.toggle_dev_action.setChecked(developer)
        self.append_log(f"界面模式：{self.mode_combo.currentText()}")

    @Slot()
    def telemetry_strategy_changed(self) -> None:
        wired = self.wired_sensor_check.isChecked() if hasattr(self, "wired_sensor_check") else True
        wireless = self.wireless_sensor_check.isChecked() if hasattr(self, "wireless_sensor_check") else True
        text = f"遥测：有线 MSP {'开' if wired else '关'} / 无线 UDP {'开' if wireless else '关'}"
        if hasattr(self, "auto_telemetry_status"):
            self.auto_telemetry_status.setText(text)
        if hasattr(self, "telemetry_mode_status"):
            self.telemetry_mode_status.setText(text)
        if wired and self.serial_worker:
            self.start_wired_sensor_stream()
        elif hasattr(self, "msp_timer"):
            self.msp_timer.stop()
            if hasattr(self, "msp_poll_btn"):
                self.msp_poll_btn.setText("开始 MSP 姿态轮询")
        if wireless and self.wifi_worker:
            self.start_wireless_sensor_stream()
        elif hasattr(self, "wireless_telemetry_timer"):
            self.wireless_telemetry_timer.stop()
        self.append_log(text)

    @Slot()
    def open_udp(self) -> None:
        if self.wifi_worker and self.wifi_worker.isRunning():
            if hasattr(self, "wireless_sensor_check") and self.wireless_sensor_check.isChecked():
                self.start_wireless_sensor_stream()
            return
        self.wifi_worker = WifiUfoWorker(self.ip_edit.text().strip(), self.remote_udp.value(), self.local_udp.value())
        self._connect_worker(self.wifi_worker)
        self.wifi_worker.start()
        if hasattr(self, "wireless_sensor_check") and self.wireless_sensor_check.isChecked():
            self.start_wireless_sensor_stream()

    @Slot()
    def close_udp(self) -> None:
        if hasattr(self, "wireless_telemetry_timer"):
            self.wireless_telemetry_timer.stop()
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
    def poll_wireless_telemetry(self) -> None:
        if not self.wifi_worker:
            self.wireless_telemetry_timer.stop()
            return
        self.wifi_worker.enqueue(("heartbeat",))

    @Slot()
    def start_wireless_sensor_stream(self) -> None:
        if not self.wifi_worker:
            return
        self.wifi_worker.enqueue(("heartbeat",))
        self.wireless_telemetry_timer.start()
        if hasattr(self, "telemetry_mode_status"):
            self.telemetry_mode_status.setText("传感器：无线心跳/遥测已开启")

    @Slot()
    def stop_wireless_sensor_stream(self) -> None:
        if hasattr(self, "wireless_telemetry_timer"):
            self.wireless_telemetry_timer.stop()
        if hasattr(self, "telemetry_mode_status"):
            self.telemetry_mode_status.setText("传感器：无线心跳/遥测已停止")

    @Slot()
    def open_serial(self) -> None:
        if not self.serial_combo.currentText():
            QMessageBox.information(self, "没有串口", "未发现可用串口。")
            return
        self.close_serial()
        if self.serial_worker and self.serial_worker.isRunning():
            self.append_log("旧串口线程仍在退出中，请稍后再连接")
            return
        worker = CleanflightSerialClient()
        worker.configure(
            self.serial_combo.currentText(),
            self.baud_spin.value(),
            dtr=self.serial_dtr_check.isChecked() if hasattr(self, "serial_dtr_check") else False,
            rts=self.serial_rts_check.isChecked() if hasattr(self, "serial_rts_check") else False,
            auto_cli=self.serial_auto_cli_check.isChecked() if hasattr(self, "serial_auto_cli_check") else False,
            line_ending=self.current_serial_line_ending(),
        )
        self._connect_serial(worker)
        self.serial_worker = worker
        worker.start()

    @Slot()
    def close_serial(self) -> None:
        self.stop_serial_rc_control()
        if hasattr(self, "msp_timer"):
            self.msp_timer.stop()
        if hasattr(self, "msp_poll_btn"):
            self.msp_poll_btn.setText("开始 MSP 姿态轮询")
        if self.serial_worker:
            worker = self.serial_worker
            worker.stop()
            if worker.wait(450):
                self.serial_worker = None
            else:
                self.append_log("串口线程正在后台退出，界面保持可操作")

    def current_serial_line_ending(self) -> str:
        if not hasattr(self, "serial_line_ending_combo"):
            return "\r\n"
        return {
            "LF": "\n",
            "CR": "\r",
            "CRLF": "\r\n",
        }.get(self.serial_line_ending_combo.currentText(), "\r\n")

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
    def send_cli_probe(self) -> None:
        if not self.serial_worker:
            self.append_log("串口未连接，无法发送 CLI 探针")
            return
        self.serial_worker.enqueue_cli_probe()
        self.serial_diag_status.setText("诊断：CLI 探针已发送")

    @Slot()
    def send_msp_probe(self) -> None:
        if not self.serial_worker:
            self.append_log("串口未连接，无法发送 MSP 探针")
            return
        self.serial_worker.enqueue_line("exit")
        self.serial_worker.enqueue_msp_probe()
        self.serial_diag_status.setText("诊断：MSP 全量探针已发送")

    @Slot()
    def send_msp_attitude(self) -> None:
        if not self.serial_worker:
            self.append_log("串口未连接，无法请求 MSP 姿态")
            return
        self.serial_worker.enqueue_line("exit")
        self.serial_worker.enqueue_msp_attitude(False)
        self.serial_diag_status.setText("诊断：MSP 姿态请求已发送")

    @Slot()
    def poll_msp_attitude(self) -> None:
        if not self.serial_worker:
            self.msp_timer.stop()
            if hasattr(self, "msp_poll_btn"):
                self.msp_poll_btn.setText("开始 MSP 姿态轮询")
            return
        self.serial_worker.enqueue_msp_attitude(True)

    @Slot()
    def toggle_msp_poll(self) -> None:
        if self.msp_timer.isActive():
            self.stop_wired_sensor_stream()
            return
        self.start_wired_sensor_stream()

    @Slot()
    def start_wired_sensor_stream(self) -> None:
        if not self.serial_worker:
            self.append_log("串口未连接，无法启动 MSP 姿态轮询")
            return
        self.serial_worker.enqueue_line("exit")
        self.msp_timer.start()
        self.msp_poll_btn.setText("停止 MSP 姿态轮询")
        self.serial_diag_status.setText("诊断：MSP 姿态轮询中")
        if hasattr(self, "telemetry_mode_status"):
            self.telemetry_mode_status.setText("传感器：有线 MSP 姿态轮询中")

    @Slot()
    def stop_wired_sensor_stream(self) -> None:
        if hasattr(self, "msp_timer"):
            self.msp_timer.stop()
        if hasattr(self, "msp_poll_btn"):
            self.msp_poll_btn.setText("开始 MSP 姿态轮询")
        if hasattr(self, "serial_diag_status"):
            self.serial_diag_status.setText("诊断：MSP 轮询已停止")
        if hasattr(self, "telemetry_mode_status"):
            self.telemetry_mode_status.setText("传感器：有线 MSP 已停止")

    @Slot()
    def auto_probe_serial(self) -> None:
        if not self.serial_combo.currentText():
            QMessageBox.information(self, "没有串口", "未发现可用串口。")
            return
        if self.serial_worker:
            self.append_log("自动波特率探测需要先关闭当前串口连接")
            return
        if self.serial_probe_worker and self.serial_probe_worker.isRunning():
            self.append_log("自动波特率探测已在运行")
            return
        port = self.serial_combo.currentText().split(" - ", 1)[0].strip()
        baud_values = [115200, 57600, 38400, 9600, 230400, 250000]
        self.serial_diag_status.setText("诊断：正在探测波特率...")
        self.append_log(f"开始自动波特率探测：{port}")
        worker = SerialProbeWorker()
        worker.configure(
            self.serial_combo.currentText(),
            baud_values,
            self.serial_dtr_check.isChecked(),
            self.serial_rts_check.isChecked(),
        )
        worker.progress.connect(self._serial_probe_progress)
        worker.found.connect(self._serial_probe_found)
        worker.probe_finished.connect(self._serial_probe_finished)
        worker.finished.connect(self._serial_probe_thread_done)
        self.serial_probe_worker = worker
        worker.start()

    @Slot(str)
    def _serial_probe_progress(self, message: str) -> None:
        self.append_log(message)

    @Slot(int, str)
    def _serial_probe_found(self, baud: int, response: str) -> None:
        self.baud_spin.setValue(baud)
        self.serial_diag_status.setText(f"诊断：{baud} 有返回")
        self.append_log(f"波特率 {baud} 收到返回：{response[:180]}")

    @Slot(bool)
    def _serial_probe_finished(self, ok: bool) -> None:
        if ok:
            self.append_log("自动波特率探测完成")
        else:
            self.serial_diag_status.setText("诊断：未探测到串口返回")
            self.append_log("自动波特率探测结束：未找到可读返回")

    @Slot()
    def _serial_probe_thread_done(self) -> None:
        self.serial_probe_worker = None

    @Slot()
    def generate_pid_cli(self) -> None:
        axis = self.pid_axis_combo.currentText()
        lines = [
            f"set p_{axis} = {self.pid_p_spin.value():.2f}",
            f"set i_{axis} = {self.pid_i_spin.value():.2f}",
            f"set d_{axis} = {self.pid_d_spin.value():.2f}",
        ]
        current = self.parameter_batch.toPlainText().rstrip()
        next_text = ("\n" if current else "").join([current, *lines]) if current else "\n".join(lines)
        self.parameter_batch.setPlainText(next_text)
        self.append_log(f"已生成 {axis} 轴 PID CLI 模板")

    @Slot()
    def read_parameters(self) -> None:
        if not self.serial_worker:
            self.append_log("串口未连接，无法读取飞控参数")
            return
        for line in ("#", "version", "status", "dump"):
            self.serial_worker.enqueue_line(line)
        self.serial_diag_status.setText("诊断：已请求 version/status/dump")

    @Slot()
    def get_parameter_filter(self) -> None:
        if not self.serial_worker:
            self.append_log("串口未连接，无法读取参数")
            return
        key = self.param_filter_edit.text().strip()
        self.serial_worker.enqueue_line("#")
        self.serial_worker.enqueue_line(f"get {key}" if key else "get")
        self.serial_diag_status.setText(f"诊断：已请求 get {key}".strip())

    @Slot()
    def read_diff_parameters(self) -> None:
        if not self.serial_worker:
            self.append_log("串口未连接，无法读取 diff")
            return
        self.serial_worker.enqueue_line("#")
        self.serial_worker.enqueue_line("diff")
        self.serial_diag_status.setText("诊断：已请求 diff")

    @Slot()
    def read_cli_help(self) -> None:
        if not self.serial_worker:
            self.append_log("串口未连接，无法读取 CLI help")
            return
        self.serial_worker.enqueue_line("#")
        self.serial_worker.enqueue_line("help")
        self.serial_diag_status.setText("诊断：已请求 help")

    @Slot()
    def send_parameter_batch(self) -> None:
        if not self.serial_worker:
            self.append_log("串口未连接，无法发送参数批处理")
            return
        lines = []
        for raw_line in self.parameter_batch.toPlainText().splitlines():
            line = raw_line.strip()
            if line and not line.startswith("#"):
                lines.append(line)
        if not lines:
            self.append_log("参数批处理为空")
            return
        reply = QMessageBox.question(
            self,
            "确认发送参数",
            f"即将向飞控发送 {len(lines)} 条 CLI 参数命令。发送前请确认已拆桨/固定机体。是否继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        for line in lines:
            self.serial_worker.enqueue_line(line)
        self.append_log(f"已排队发送 {len(lines)} 条参数命令")

    @Slot()
    def save_parameters(self) -> None:
        if not self.serial_worker:
            self.append_log("串口未连接，无法保存飞控参数")
            return
        reply = QMessageBox.question(
            self,
            "确认保存参数",
            "即将发送 save，飞控通常会重启并短暂断开串口。是否继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.serial_worker.enqueue_line("save")
            self.append_log("已发送 save")

    @Slot()
    def choose_firmware_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择飞控固件",
            str(PROJECT_DIR),
            "Firmware (*.hex *.bin);;All files (*.*)",
        )
        if path:
            self.firmware_path_edit.setText(path)
            self.append_log(f"已选择固件：{path}")

    @Slot()
    def firmware_preflight(self) -> None:
        firmware = self.firmware_path_edit.text().strip()
        if firmware and not Path(firmware).exists():
            QMessageBox.warning(self, "固件不存在", "选择的固件文件不存在。")
            return
        target = self.firmware_target_combo.currentText() if hasattr(self, "firmware_target_combo") else "未知飞控"
        self.append_log("烧录前检查：开始识别飞控固件、串口协议和 Bootloader 状态")
        self.append_log(f"目标类型：{target}")
        if firmware:
            self.append_log(f"待烧录固件：{firmware}")
        else:
            self.append_log("未选择固件；仅执行飞控识别检查")
        self.append_log("安全策略：未知 MCU/Bootloader 前不会直接刷写固件，避免飞控变砖或安全逻辑失效")
        if hasattr(self, "flash_status"):
            self.flash_status.setText(f"烧录预检：{target}，等待飞控识别结果")
        if self.serial_worker:
            self.serial_worker.enqueue_cli_probe()
            self.serial_worker.enqueue_msp_probe()
        else:
            self.append_log("串口未连接：请先选择 COM 口并连接，再执行完整预检")
            if hasattr(self, "flash_status"):
                self.flash_status.setText("烧录预检：串口未连接")

    @Slot()
    def request_bootloader(self) -> None:
        if not self.serial_worker:
            self.append_log("串口未连接，无法进入 Bootloader")
            return
        reply = QMessageBox.warning(
            self,
            "确认进入 Bootloader",
            "该操作会让飞控重启到 Bootloader，串口可能短暂断开。仅在拆桨/固定机体后继续。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.serial_worker.enqueue_line("#")
            self.serial_worker.enqueue_line("bl")
            self.append_log("已发送 Bootloader 请求：bl")

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
            if self.unlock_check.isChecked() and self.flight_armed:
                self.wifi_worker.enqueue(("hold_update", self.current_control(), "滑块控制"))
        if self.use_wired_control() and self.flight_armed:
            self.set_serial_rc_state(self.current_control(), "滑块控制")

    def current_control(self) -> ControlState:
        return ControlState(self.roll.value(), self.pitch.value(), self.throttle.value(), self.yaw.value())

    @staticmethod
    def rc_from_byte(value: int) -> int:
        value = clamp_byte(value)
        if value == 128:
            return MSP_RC_MID
        if value < 128:
            return MSP_RC_LOW + round(value * (MSP_RC_MID - MSP_RC_LOW) / 128)
        return MSP_RC_MID + round((value - 128) * (MSP_RC_HIGH - MSP_RC_MID) / 127)

    def serial_channels_from_control(self, state: ControlState) -> list[int]:
        state = state.clamped()
        return [
            self.rc_from_byte(state.roll),
            self.rc_from_byte(state.pitch),
            self.rc_from_byte(state.yaw),
            self.rc_from_byte(state.throttle),
            MSP_RC_LOW,
            MSP_RC_LOW,
            MSP_RC_LOW,
            MSP_RC_LOW,
        ]

    def send_serial_rc_state(self, state: ControlState, label: str, silent: bool = True) -> None:
        if not self.serial_link_ready() or not self.serial_worker:
            return
        self.serial_worker.enqueue_msp_rc(self.serial_channels_from_control(state), label, silent)
        now = time.monotonic()
        if now - self.last_serial_control_log > 0.8:
            channels = self.serial_channels_from_control(state)
            self.append_log(
                f"有线RC：{label} R{channels[0]} P{channels[1]} Y{channels[2]} T{channels[3]}"
            )
            self.last_serial_control_log = now

    def set_serial_rc_state(self, state: ControlState, label: str) -> None:
        self.serial_rc_state = state.clamped()
        self.serial_rc_label = label
        self.serial_rc_active = True
        if not self.serial_control_timer.isActive():
            self.serial_control_timer.start()

    def start_serial_rc_burst(self, state: ControlState, duration: float, label: str, auto_idle: bool) -> None:
        if self.serial_worker:
            self.serial_worker.enqueue_line("exit")
        self.serial_burst_state = state.clamped()
        self.serial_burst_label = label
        self.serial_burst_until = time.monotonic() + max(0.1, float(duration))
        self.serial_burst_auto_idle = auto_idle
        self.serial_rc_active = False
        self.serial_control_timer.start()
        self.append_log(f"开始有线RC脉冲：{label} {duration:.1f} 秒")

    def stop_serial_rc_control(self) -> None:
        self.serial_burst_until = 0.0
        self.serial_burst_auto_idle = False
        self.serial_rc_active = False
        if hasattr(self, "serial_control_timer"):
            self.serial_control_timer.stop()

    @Slot()
    def tick_serial_control(self) -> None:
        if not self.serial_link_ready():
            self.stop_serial_rc_control()
            return
        now = time.monotonic()
        if self.serial_burst_until:
            if now < self.serial_burst_until:
                self.send_serial_rc_state(self.serial_burst_state, self.serial_burst_label, True)
                return
            self.append_log(f"有线RC脉冲结束：{self.serial_burst_label}")
            self.serial_burst_until = 0.0
            if self.serial_burst_auto_idle:
                self.set_serial_rc_state(ControlState(128, 128, WIRED_IDLE_THROTTLE, 128), "有线待机")
                self.command_status.setText("当前命令：有线待机")
            else:
                self.stop_serial_rc_control()
                return
        if self.serial_rc_active:
            self.send_serial_rc_state(self.serial_rc_state, self.serial_rc_label, True)

    def current_active_mode(self) -> int:
        if not hasattr(self, "arm_profile_combo"):
            return 0
        return 1 if "/ M1 控制" in self.arm_profile_combo.currentText() else 0

    @Slot()
    def update_arm_profile_status(self) -> None:
        if not hasattr(self, "control_mode_status"):
            return
        mode = self.current_active_mode()
        self.control_mode_status.setText(f"控制模式：M{mode}，监控只显示真实回传")
        if self.wifi_worker:
            self.wifi_worker.enqueue(("set_active_mode", mode))

    @Slot()
    def reset_hover(self) -> None:
        self.roll.setValue(128)
        self.pitch.setValue(128)
        self.throttle.setValue(IDLE_THROTTLE)
        self.yaw.setValue(128)
        self.update_control()
        if self.use_wired_control() and self.flight_armed:
            self.set_serial_rc_state(ControlState(128, 128, WIRED_IDLE_THROTTLE, 128), "有线待机")
        elif self.wifi_worker:
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
        if not checked:
            self.flight_armed = False
            self.stop_serial_rc_control()
            if self.wifi_worker:
                self.wifi_worker.enqueue(("hold_stop",))
                self.wifi_worker.enqueue(("stop_stream",))
            self.command_status.setText("当前命令：已锁定")

    def ensure_unlocked(self, action: str) -> bool:
        if self.unlock_check.isChecked():
            return True
        QMessageBox.warning(self, "控制已锁定", f"“{action}” 需要先勾选安全锁。请确认拆桨或固定机体后再解锁。")
        self.append_log(f"已阻止动作：{action}，原因：安全锁未解锁")
        return False

    def ensure_flight_armed(self, action: str) -> bool:
        if self.flight_armed:
            return True
        QMessageBox.information(self, "尚未解锁待机", f"“{action}” 需要先点击“解锁待机”，等待无人机进入待机状态。")
        self.append_log(f"已阻止动作：{action}，原因：尚未执行解锁待机")
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
        if not self.ensure_flight_armed(label):
            return
        state = ControlState(roll, pitch, throttle, yaw)
        if not self.prepare_control_link(label):
            return
        if self.use_wired_control():
            self.set_serial_rc_state(state, label)
        elif self.wifi_worker:
            self.wifi_worker.enqueue(("hold_start", state, label))
        self.command_status.setText(f"持续控制：{label}")

    @Slot()
    def stop_hold(self) -> None:
        """完全停止控制（发送回中包后不再发送）."""
        if self.use_wired_control() and self.flight_armed:
            self.set_serial_rc_state(ControlState(128, 128, WIRED_IDLE_THROTTLE, 128), "有线待机")
            self.command_status.setText("当前命令：有线待机")
        elif self.wifi_worker:
            self.wifi_worker.enqueue(("hold_stop",))
            self.command_status.setText("当前命令：已停机")

    @Slot()
    def stop_hold_to_idle(self) -> None:
        """松开方向键后回到待机怠速状态（而非完全停机）."""
        if not self.flight_armed:
            return
        if self.use_wired_control():
            self.set_serial_rc_state(ControlState(128, 128, WIRED_IDLE_THROTTLE, 128), "有线待机")
        elif self.wifi_worker:
            state = ControlState(128, 128, IDLE_THROTTLE, 128)
            self.wifi_worker.enqueue(("hold_start", state, "待机怠速"))
        self.command_status.setText("当前命令：待机怠速")

    @Slot()
    def arm_idle(self) -> None:
        """解锁并进入待机状态：发送起飞脉冲后自动维持电机低频旋转."""
        if not self.ensure_unlocked("解锁待机"):
            return
        wired_control = self.use_wired_control()
        profile = self.arm_profile_combo.currentText()
        if wired_control:
            profile_text = (
                f"即将通过有线 MSP RC 发送摇杆解锁序列：油门最低 + 偏航最大，持续 {CLEANFLIGHT_ARM_DURATION:.0f} 秒。\n"
                "完成后保持油门最低的有线待机。摄像图传仍通过 WiFi UDP。"
            )
        elif "Cleanflight" in profile:
            profile_text = (
                f"即将发送 Cleanflight 摇杆解锁序列：油门最低 + 偏航最大，持续 {CLEANFLIGHT_ARM_DURATION:.0f} 秒。\n"
                "完成后使用 M0 中位油门待机/控制。"
            )
        else:
            profile_text = (
                f"即将发送 WiFiUFO M1 解锁/起飞脉冲 {ARM_PULSE_DURATION:.1f} 秒。\n"
                f"完成后使用 M{self.current_active_mode()} 中位油门待机/控制。"
            )
        if (
            QMessageBox.question(
                self,
                "确认解锁",
                profile_text + "\n"
                "请确认桨叶安全、机体固定或处于安全飞行区。",
            )
            == QMessageBox.Yes
        ):
            if not self.prepare_control_link("解锁待机"):
                return
            if wired_control:
                arm_state = ControlState(128, 128, WIRED_IDLE_THROTTLE, 255)
                self.start_serial_rc_burst(arm_state, CLEANFLIGHT_ARM_DURATION, "有线摇杆解锁", True)
                self.flight_armed = True
                self.command_status.setText("当前命令：有线解锁中...")
                return
            if self.wifi_worker:
                # 先发心跳预热连接，确保无人机已就绪
                self.wifi_worker.enqueue(("heartbeat",))
                self.wifi_worker.enqueue(("heartbeat",))
                active_mode = self.current_active_mode()
                self.wifi_worker.enqueue(("set_active_mode", active_mode))
                if "Cleanflight" in profile:
                    arm_state = ControlState(128, 128, 0, 255)
                    self.wifi_worker.enqueue((
                        "burst",
                        arm_state,
                        0,
                        "摇杆解锁",
                        CLEANFLIGHT_ARM_DURATION,
                        True,
                        active_mode,
                    ))
                else:
                    arm_state = ControlState(128, 128, IDLE_THROTTLE, 128)
                    if active_mode == ARMED_HOLD_MODE:
                        self.wifi_worker.enqueue(("arm_sequence", arm_state))
                    else:
                        self.wifi_worker.enqueue((
                            "burst",
                            arm_state,
                            ARMED_HOLD_MODE,
                            "M1解锁",
                            ARM_PULSE_DURATION,
                            True,
                            active_mode,
                        ))
                self.flight_armed = True
                self.command_status.setText("当前命令：解锁中...")

    @Slot()
    def disarm(self) -> None:
        """锁定停机：发送降落指令停止所有电机."""
        if not self.ensure_unlocked("锁定停机"):
            return
        if QMessageBox.question(self, "确认锁定", "即将发送降落/锁定指令，电机将停止。") == QMessageBox.Yes:
            if not self.prepare_control_link("锁定停机"):
                return
            if self.use_wired_control():
                self.start_serial_rc_burst(
                    ControlState(128, 128, WIRED_IDLE_THROTTLE, 0),
                    2.0,
                    "有线锁定",
                    False,
                )
                self.flight_armed = False
                self.command_status.setText("当前命令：有线锁定中...")
            elif self.wifi_worker:
                self.wifi_worker.enqueue(("hold_stop",))
                self.wifi_worker.enqueue(("stop_stream",))
                self.wifi_worker.enqueue(("burst", ControlState(), 2, "锁定", 1.0, False))
                self.flight_armed = False
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
            if not self.prepare_control_link("急停"):
                return
            if self.use_wired_control():
                self.start_serial_rc_burst(
                    ControlState(128, 128, WIRED_IDLE_THROTTLE, 0),
                    2.0,
                    "有线急停/锁定",
                    False,
                )
                self.flight_armed = False
                self.command_status.setText("当前命令：有线急停")
            elif self.wifi_worker:
                self.wifi_worker.enqueue(("hold_stop",))
                self.wifi_worker.enqueue(("stop_stream",))
                self.wifi_worker.enqueue(("burst", ControlState(), 4, "急停", 1.0))
                self.flight_armed = False

    def _set_sensor_value(self, key: str, value: str) -> None:
        label = self.sensor_cards.get(key)
        if label:
            title = self.sensor_titles.get(key)
            label.setText(f"{title}\n{value}" if title else value)

    @staticmethod
    def _field_key(text: object) -> str:
        return re.sub(r"[\s_\-:：/（）()]+", "", str(text).lower())

    @staticmethod
    def _first_number(text: object) -> float | None:
        match = re.search(r"[-+]?\d+(?:\.\d+)?", str(text))
        return float(match.group(0)) if match else None

    def _numeric_value(self, values: dict, aliases: tuple[str, ...]) -> float | None:
        normalized_aliases = tuple(self._field_key(alias) for alias in aliases)
        for key, value in values.items():
            field = self._field_key(key)
            if "raw" in field and not any("raw" in alias for alias in normalized_aliases):
                continue
            if any(alias == field or alias in field or field in alias for alias in normalized_aliases):
                number = self._first_number(value)
                if number is not None:
                    return number
        joined = " ".join(f"{key}={value}" for key, value in values.items())
        for alias in aliases:
            pattern = rf"{re.escape(alias)}\s*[:=：]\s*([-+]?\d+(?:\.\d+)?)"
            match = re.search(pattern, joined, flags=re.IGNORECASE)
            if match:
                return float(match.group(1))
        return None

    @staticmethod
    def _fmt_number(value: float, unit: str = "") -> str:
        if abs(value) >= 100:
            return f"{value:.0f}{unit}"
        return f"{value:.1f}{unit}"

    def mark_attitude_unavailable(self) -> None:
        if not hasattr(self, "attitude_view"):
            return
        self.attitude_view.set_attitude(0.0, 0.0, 0.0, "无真实姿态回传")
        self.attitude_status.setText("姿态：无真实回传")

    def update_sensor_monitor(self, values: dict) -> None:
        sensor_map = {
            "roll": ("roll", "attituderoll", "横滚", "姿态roll"),
            "pitch": ("pitch", "attitudepitch", "俯仰", "姿态pitch"),
            "yaw": ("yaw", "heading", "attitudeyaw", "偏航", "航向", "姿态yaw"),
            "acc_x": ("accx", "accelx", "accelerometerx", "加速度x", "加速度计x", "ax"),
            "acc_y": ("accy", "accely", "accelerometery", "加速度y", "加速度计y", "ay"),
            "acc_z": ("accz", "accelz", "accelerometerz", "加速度z", "加速度计z", "az"),
            "gyro_x": ("gyrox", "gyrx", "陀螺x", "陀螺仪x", "gx"),
            "gyro_y": ("gyroy", "gyry", "陀螺y", "陀螺仪y", "gy"),
            "gyro_z": ("gyroz", "gyrz", "陀螺z", "陀螺仪z", "gz"),
            "altitude": ("altitude", "height", "alt", "高度", "气压高度"),
            "voltage": ("voltage", "vbat", "batteryvoltage", "电压"),
        }
        numeric: dict[str, float] = {}
        for key, aliases in sensor_map.items():
            value = self._numeric_value(values, aliases)
            if value is not None:
                numeric[key] = value

        unit_map = {
            "roll": "°",
            "pitch": "°",
            "yaw": "°",
            "acc_x": " g",
            "acc_y": " g",
            "acc_z": " g",
            "gyro_x": " °/s",
            "gyro_y": " °/s",
            "gyro_z": " °/s",
            "altitude": " m",
            "voltage": " V",
        }
        for key, value in numeric.items():
            self._set_sensor_value(key, self._fmt_number(value, unit_map.get(key, "")))

        raw_card_map = {
            "acc_x_raw": "acc_x",
            "acc_y_raw": "acc_y",
            "acc_z_raw": "acc_z",
            "gyro_x_raw": "gyro_x",
            "gyro_y_raw": "gyro_y",
            "gyro_z_raw": "gyro_z",
        }
        for raw_key, card_key in raw_card_map.items():
            if raw_key in values and card_key not in numeric:
                self._set_sensor_value(card_key, f"{values[raw_key]} raw")

        imu_parts = []
        if "加速度计" in values:
            imu_parts.append(f"ACC {values['加速度计']}")
        if "陀螺仪" in values:
            imu_parts.append(f"GYRO {values['陀螺仪']}")
        if "IMU 原始数据" in values:
            imu_parts.append(str(values["IMU 原始数据"]))
        if imu_parts:
            self._set_sensor_value("imu", " / ".join(imu_parts))

        if any(key in numeric for key in ("roll", "pitch", "yaw")):
            roll = numeric.get("roll", self.attitude_view.roll)
            pitch = numeric.get("pitch", self.attitude_view.pitch)
            yaw = numeric.get("yaw", self.attitude_view.yaw)
            self.live_attitude = True
            self.last_live_attitude_at = time.monotonic()
            self.attitude_view.set_attitude(roll, pitch, yaw, "IMU 实测姿态")
            self.attitude_status.setText("姿态：IMU 实测")

    @Slot(dict)
    def update_telemetry(self, values: dict) -> None:
        for key, value in values.items():
            self.set_metric(str(key), str(value))
        self.update_sensor_monitor(values)
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
            if hasattr(self, "wireless_sensor_check") and self.wireless_sensor_check.isChecked():
                self.start_wireless_sensor_stream()
            self.update_control_transport_status()
        elif "断开" in status or "错误" in status:
            if hasattr(self, "wireless_telemetry_timer"):
                self.wireless_telemetry_timer.stop()
            self.udp_status.setStyleSheet(
                "QLabel { background:#fef2f2; color:#991b1b; border:1px solid #fecaca; "
                "border-radius:6px; padding:6px 10px; font-weight:500; }"
            )
            self.quick_wifi.setText("WiFi：已断开")
            self.quick_wifi.setStyleSheet(
                "QLabel { background:#fef2f2; color:#991b1b; border:1px solid #fecaca; "
                "border-radius:6px; padding:6px 10px; font-weight:500; }"
            )
            self.update_control_transport_status()
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
            if hasattr(self, "wired_sensor_check") and self.wired_sensor_check.isChecked():
                self.start_wired_sensor_stream()
            self.update_control_transport_status()
        elif "未连接" in status or "失败" in status:
            if hasattr(self, "msp_timer"):
                self.msp_timer.stop()
            if hasattr(self, "msp_poll_btn"):
                self.msp_poll_btn.setText("开始 MSP 姿态轮询")
            self.stop_serial_rc_control()
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
            self.update_control_transport_status()
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
