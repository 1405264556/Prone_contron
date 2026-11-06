"""端到端自测：假无人机（UDP，同进程线程）+ 假飞控（pty 串口），驱动真实的 MainWindow。

仅支持 Linux/macOS（依赖 pty）。运行：
    QT_QPA_PLATFORM=offscreen python tests/test_e2e.py
覆盖：UDP 链路/心跳/图传解码、键盘组合控制、50 Hz 控制包、失焦与界面卡死自动回中、
安全锁/锁定/急停、链路丢失检测、MSP 遥测解析与频率、通道映射、有线 RC、拔线重连。
"""
import faulthandler; faulthandler.enable()
import os, socket, struct, sys, tempfile, threading, time
import pty, tty
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["DRONE_GS_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="drone_gs_test_")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "qt_ground_station" / "python_qt6"))
import qt_drone_station as m
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QEvent, Qt
from PySide6.QtGui import QImage, QColor, QKeyEvent

class FakeBox:
    Yes, No, Ok = QMessageBox.Yes, QMessageBox.No, QMessageBox.Ok
    question = staticmethod(lambda *a, **k: QMessageBox.Yes)
    warning = staticmethod(lambda *a, **k: QMessageBox.Yes)
    information = staticmethod(lambda *a, **k: QMessageBox.Ok)
    about = staticmethod(lambda *a, **k: None)
m.QMessageBox = FakeBox

app = QApplication([])
results = []
def check(name, ok, detail=""):
    results.append((name, ok)); print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)

# ---------------- fake drone ----------------
DRONE_PORT, APP_PORT = 47001, 47002
img = QImage(320, 240, QImage.Format_RGB32); img.fill(QColor("#3366cc"))
buf = QByteArray(); qb = QBuffer(buf); qb.open(QIODevice.WriteOnly); img.save(qb, "JPG"); JPEG = bytes(buf)
class Drone(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); self.sock.bind(("127.0.0.1", DRONE_PORT)); self.sock.settimeout(0.05)
        self.controls = []; self.heartbeats = 0; self.peer = None; self.running = True; self.silent = False; self.frame_no = 0
    def video(self):
        chunk = 700; parts = [JPEG[i:i+chunk] for i in range(0, len(JPEG), chunk)]
        self.frame_no += 1
        for idx, part in enumerate(parts, 1):
            hdr = bytearray(54); hdr[0:2] = b"cc"; hdr[2] = 3; hdr[8:14] = self.frame_no.to_bytes(6, "little")
            hdr[47] = 1; hdr[48] = idx; hdr[50] = len(parts); hdr[52:54] = len(part).to_bytes(2, "little")
            self.sock.sendto(bytes(hdr) + part, self.peer)
    def run(self):
        last_video = 0
        while self.running:
            try:
                data, addr = self.sock.recvfrom(2048)
                self.peer = addr
                if data[:3] == b"cc\x01": self.heartbeats += 1
                elif data[:3] == b"cc\x0a":
                    x = 0
                    for v in data[8:13]: x ^= v
                    self.controls.append((time.monotonic(), tuple(data[8:13]), data[13] == x))
            except socket.timeout:
                pass
            if self.peer and not self.silent and time.monotonic() - last_video > 0.05:
                last_video = time.monotonic(); self.video()
                info = bytearray(80); info[0:3] = b"cc\x01"; info[7:21] = b"WiFiUFO-TEST01"; self.sock.sendto(bytes(info), self.peer)
drone = Drone(); drone.start()

def pump(sec):
    end = time.monotonic() + sec
    while time.monotonic() < end:
        app.processEvents(); time.sleep(0.005)

w = m.MainWindow(); w.show(); w.isActiveWindow = lambda: True
w.ip_edit.setText("127.0.0.1"); w.remote_udp.setValue(DRONE_PORT); w.local_udp.setValue(APP_PORT)
w.open_udp(); pump(2.5)
check("UDP 链路在线", w.link_online, w.quick_wifi.text())
check("心跳保活", drone.heartbeats >= 3, f"heartbeats={drone.heartbeats}")
check("图传解码显示", w.video_view.image is not None and w.video_view.image.width() == 320, w.video_stats.text())

def key(k, press=True):
    ev = QKeyEvent(QEvent.KeyPress if press else QEvent.KeyRelease, k, Qt.NoModifier)
    QApplication.sendEvent(w, ev); app.processEvents()
def last_ctrl(): return drone.controls[-1][1] if drone.controls else None

# 未解锁按 W：不应发送控制
n0 = len(drone.controls); key(Qt.Key_W); pump(0.3); key(Qt.Key_W, False)
check("未解锁时键盘不发控制", len(drone.controls) == n0, w.command_status.text())

import re as _re
fps = int(_re.search(r"图传 (\d+) fps", w.status_udp_label.text()).group(1))
check("20fps 源图传显示 >=15 fps（不再被限帧减半）", fps >= 15, w.status_udp_label.text())
w.ip_edit.setFocus(); app.processEvents()
w.unlock_check.setChecked(True); w.arm_profile_combo.setCurrentIndex(1)  # WiFiUFO M1 起飞 / M1 控制
w.arm_idle(); pump(0.3)
key(Qt.Key_D); pump(1.2)
check("解锁脉冲期间按住的方向键在脉冲结束后生效", last_ctrl() == (176, 128, 128, 128, 1), str(last_ctrl()))
key(Qt.Key_D, False); pump(0.2)
check("解锁后焦点离开输入框", not w._typing_focus(), type(QApplication.focusWidget()).__name__)
check("解锁后进入待机怠速 M1", last_ctrl() == (128, 128, 128, 128, 1), str(last_ctrl()))

key(Qt.Key_W); key(Qt.Key_A); pump(0.4)
check("W+A 组合 = 左前", last_ctrl() == (80, 176, 128, 128, 1), str(last_ctrl()))
key(Qt.Key_A, False); pump(0.3)
check("松开 A 仍保持前进", last_ctrl() == (128, 176, 128, 128, 1), str(last_ctrl()))
# 控制包频率与抖动
t0 = time.monotonic(); pump(1.0)
win = [c for c in drone.controls if c[0] >= t0]
gaps = [b[0]-a[0] for a, b in zip(win, win[1:])]
rate = len(win)
check("控制包约 50 Hz", 45 <= rate <= 55, f"{rate} 包/s, 最大间隔 {max(gaps)*1000:.1f} ms")
check("控制包校验正确", all(c[2] for c in drone.controls))
# 窗口失焦自动松键
QApplication.sendEvent(w, QEvent(QEvent.WindowDeactivate)); pump(0.3)
check("失焦自动回中", last_ctrl() == (128, 128, 128, 128, 1) and not w.held_actions, str(last_ctrl()))
# 看门狗：界面不再 ping 时后台自动回中
key(Qt.Key_W); pump(0.2); w.ping_timer.stop()
check("按住前进", last_ctrl()[1] == 176, str(last_ctrl()))
pump(1.4)
check("界面无响应 1s 后台自动回中", last_ctrl() == (128, 128, 128, 128, 1), str(last_ctrl()))
key(Qt.Key_W, False); w.ping_timer.start(); pump(0.2)
# 滑块基准 + 叠加
w.throttle.setValue(150); pump(0.2)
check("滑块基准生效", last_ctrl()[2] == 150, str(last_ctrl()))
key(Qt.Key_R); pump(0.2)
check("升高叠加在滑块上", last_ctrl()[2] == 214, str(last_ctrl()))
key(Qt.Key_R, False); key(Qt.Key_Space); key(Qt.Key_Space, False); pump(0.2)
check("空格悬停回中滑块", last_ctrl() == (128, 128, 128, 128, 1) and w.throttle.value() == 128, str(last_ctrl()))
# 关闭安全锁 -> 自动锁定（M2）
w.unlock_check.setChecked(False); pump(0.3)
check("关闭安全锁自动发送锁定 M2", last_ctrl()[4] == 2 and not w.flight_armed, str(last_ctrl()))
pump(1.2)
# 急停无需安全锁
w.hard_stop(); pump(0.3)
check("急停无需安全锁 M4", last_ctrl()[4] == 4, str(last_ctrl()))
pump(1.2)
# 链路丢失检测
drone.silent = True; pump(3.5)
check("链路丢失提示", (not w.link_online) and "丢失" in w.quick_wifi.text(), w.quick_wifi.text())
drone.silent = False; pump(1.5)
check("链路恢复", w.link_online, w.quick_wifi.text())
w.close_udp(); pump(0.3)

# ---------------- fake flight controller over pty ----------------
master, slave = pty.openpty(); tty.setraw(master); tty.setraw(slave)
slave_name = os.ttyname(slave)
def msp_reply(cmd, payload):
    cs = len(payload) ^ cmd
    for v in payload: cs ^= v
    return b"$M>" + bytes((len(payload), cmd)) + payload + bytes((cs,))
fc = {"rc": [], "cli": False, "lines": [], "alive": True}
def fc_loop():
    buf = bytearray()
    while fc["alive"]:
        try:
            data = os.read(master, 1024)
        except OSError:
            return
        buf.extend(data)
        while True:
            if buf.startswith(b"$M<"):
                if len(buf) < 6: break
                size, cmd = buf[3], buf[4]
                if len(buf) < 6 + size: break
                payload = bytes(buf[5:5+size]); del buf[:6+size]
                if fc["cli"]: continue
                if cmd == 108: os.write(master, msp_reply(108, struct.pack("<hhh", 123, -45, 270)))
                elif cmd == 102: os.write(master, msp_reply(102, struct.pack("<9h", 1, 2, 512, 3, 4, 5, 0, 0, 0)))
                elif cmd == 110: os.write(master, msp_reply(110, bytes([39, 0, 0, 0, 0, 0, 0])))
                elif cmd == 101: os.write(master, msp_reply(101, bytes(11)))
                elif cmd == 200: fc["rc"].append(struct.unpack("<8H", payload))
                continue
            nl = buf.find(b"\n")
            dollar = buf.find(b"$M<")
            if nl < 0 or (0 <= dollar < nl): 
                if dollar > 0: del buf[:dollar]; continue
                break
            line = bytes(buf[:nl]).strip().decode(); del buf[:nl+1]
            fc["lines"].append(line)
            if line == "#": fc["cli"] = True; os.write(master, b"\r\nEntering CLI Mode, type 'exit' to return, or 'help'\r\n\r\n# ")
            elif line == "exit": fc["cli"] = False; os.write(master, b"\r\nLeaving CLI mode, unsaved changes lost.\r\nRebooting\r\n")
            elif line == "feature": os.write(master, b"feature\r\nEnabled: RX_MSP VBAT \r\n\r\n# ")
            elif line == "map": os.write(master, b"map\r\nMap: AETR1234\r\n\r\n# ")
            elif line == "status": os.write(master, b"status\r\nSystem Uptime: 12 seconds, Voltage: 39 * 0.1V (1S battery - OK)\r\nCPU Clock=72MHz, GYRO=MPU6050, ACC=MPU6050\r\n\r\n# ")
def fc_loop_safe():
    try:
        fc_loop()
    except OSError:
        pass  # 测试中途关闭 pty 模拟拔线
threading.Thread(target=fc_loop_safe, daemon=True).start()

w.scene_combo.setCurrentText("有线调参/传感器"); pump(0.1)
w.serial_combo.addItem(slave_name); w.serial_combo.setCurrentText(slave_name)
w.open_serial(); pump(2.0)
check("串口连接", "已连接" in w.serial_status.text(), w.serial_status.text())
w.flush_ui()
check("MSP 姿态显示", "12.3" in w.sensor_cards["roll"].text() and "-4.5" in w.sensor_cards["pitch"].text(), w.sensor_cards["roll"].text().replace("\n"," "))
check("MSP 电压显示", "3.9" in w.sensor_cards["voltage"].text(), w.sensor_cards["voltage"].text().replace("\n"," "))
check("姿态速率 ~10Hz", "姿态 9" in w.status_serial_label.text() or "姿态 10" in w.status_serial_label.text() or "姿态 11" in w.status_serial_label.text(), w.status_serial_label.text())
timeouts = 0
for _ in range(30):
    pump(0.1)
    if "超时" in w.attitude_status.text(): timeouts += 1
check("稳态 3 秒内姿态无误报超时", timeouts == 0, w.attitude_status.text())
check("串口回调运行在串口线程", w.serial_worker.wrong_thread_calls == 0, f"wrong={w.serial_worker.wrong_thread_calls}")
check("控制台无 MSP 二进制刷屏", "RX HEX" not in w.serial_console.toPlainText(), w.serial_console.toPlainText()[-80:])
w.detect_receiver_mode(); pump(1.5)
check("读取通道映射 AETR1234", w.rc_map == "AETR1234", w.rc_map)
check("读取接收机 RX_MSP", "RX_MSP" in w.fc_features, str(w.fc_features))
pump(1.0)
w.control_transport_combo.setCurrentIndex(2)  # 有线控制
w.unlock_check.setChecked(True); w.arm_idle(); pump(0.5)
ch = fc["rc"][-1] if fc["rc"] else None
check("有线解锁：AETR 顺序 油门(ch3)=1000 偏航(ch4)=2000", ch is not None and ch[2] == 1000 and ch[3] == 2000, str(ch))
n = len(fc["rc"]); pump(1.0)
check("有线 RC 约 20 Hz", 17 <= len(fc["rc"]) - n <= 23, f"{len(fc['rc']) - n} 包/s")
pump(3.2)
ch = fc["rc"][-1]
check("解锁后有线待机（偏航回中、油门最低）", ch[2] == 1000 and ch[3] == 1500, str(ch))
key(Qt.Key_W); pump(0.3)
ch = fc["rc"][-1]
check("有线前进：俯仰通道(ch2) > 1500", ch[1] > 1500 and ch[2] == 1000, str(ch))
key(Qt.Key_W, False); pump(0.3)
w._send_disarm(2, "锁定"); pump(0.3)
# 拔线
fc["alive"] = False; os.close(master); pump(2.0)
check("拔线检测 -> 重连中", "重连" in w.serial_status.text() or "未连接" in w.serial_status.text(), w.serial_status.text())
w.close_serial(); pump(0.8)
check("关闭串口", w.serial_worker is None, w.serial_status.text())
w.flush_ui()
log = w.log_edit.toPlainText()
check("日志无 UDP 心跳刷屏", log.count("WiFi UFO 心跳") <= 3, f"心跳日志 {log.count('WiFi UFO 心跳')} 条")
w.flight_armed = False; w.close(); pump(0.5)
drone.running = False
print(f"\n{sum(ok for _, ok in results)}/{len(results)} passed")
sys.exit(0 if all(ok for _, ok in results) else 1)
