"""WiFi UFO 协议诊断 v4 —— 测试 type=0x01 控制 + TCP + 多端口."""
from __future__ import annotations

import io
import socket
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

IP = "192.168.0.1"
PORT = 40000
LOCAL = 40000


def hex_str(d): return " ".join(f"{b:02x}" for b in d)


def ctl_type0a(r, p, t, y, m):
    """type=0x0a 控制包 (原格式)."""
    pkt = bytearray.fromhex("63 63 0a 00 00 08 00 66 80 80 80 80 00 00 99")
    pkt[8], pkt[9], pkt[10], pkt[11], pkt[12] = r, p, t, y, m
    pkt[13] = pkt[8] ^ pkt[9] ^ pkt[10] ^ pkt[11] ^ pkt[12]
    return bytes(pkt)


def ctl_type01(r, p, t, y, m):
    """type=0x01 控制包 (心跳类型+控制负载)."""
    # 格式: 63 63 01 00 00 [len] [R] [P] [T] [Y] [M] [XOR]
    payload = bytearray([r, p, t, y, m, 0])
    payload[5] = payload[0] ^ payload[1] ^ payload[2] ^ payload[3] ^ payload[4]
    hdr = bytearray([0x63, 0x63, 0x01, 0x00, 0x00, len(payload), 0x00])
    return bytes(hdr + payload)


def ctl_type0a_no66(r, p, t, y, m):
    """type=0x0a 无 0x66 前缀无 0x99 后缀."""
    hdr = bytearray([0x63, 0x63, 0x0a, 0x00, 0x00, 6, 0x00])
    payload = bytearray([r, p, t, y, m, 0])
    payload[5] = payload[0] ^ payload[1] ^ payload[2] ^ payload[3] ^ payload[4]
    return bytes(hdr + payload)


def send_n(sock, pkt, n, interval=0.02, tcp=False):
    for _ in range(n):
        if tcp:
            try:
                sock.send(pkt)
            except:
                pass
        else:
            sock.sendto(pkt, (IP, PORT))
        time.sleep(interval)


def test_udp_variants():
    print("=" * 60)
    print("WiFi UFO 协议诊断 v4: 不同 type/传输 测试")
    print("=" * 60)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", LOCAL))
    sock.setblocking(False)

    hb = bytes.fromhex("63 63 01 00 00 00 00")

    try:
        # 心跳
        for _ in range(3):
            sock.sendto(hb, (IP, PORT))
            time.sleep(0.2)
        time.sleep(0.5)

        tests = [
            ("UDP type=0x0a 油门0偏航255 (解锁)", ctl_type0a(128, 128, 0, 255, 0), False),
            ("UDP type=0x01 油门0偏航255 (解锁)", ctl_type01(128, 128, 0, 255, 0), False),
            ("UDP type=0x0a 无66/99 油门0偏航255", ctl_type0a_no66(128, 128, 0, 255, 0), False),
            ("UDP type=0x0a 油门200 mode=1", ctl_type0a(128, 128, 200, 128, 1), False),
            ("UDP type=0x01 油门200 mode=1", ctl_type01(128, 128, 200, 128, 1), False),
        ]

        for label, pkt, tcp in tests:
            print(f"\n[{label}]")
            print(f"  包({len(pkt)}B): {hex_str(pkt)}")
            if tcp:
                print("  通过 TCP 发送...")
            sock.sendto(hb, (IP, PORT))
            time.sleep(0.05)
            send_n(sock, pkt, 150, interval=0.02)
            print("  已发送 150 包 @ 50Hz，观察电机...")
            time.sleep(1.0)

        # TCP 测试
        print(f"\n[TCP 连接测试]")
        try:
            tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            tcp.settimeout(3)
            tcp.connect((IP, PORT))
            print(f"  TCP {IP}:{PORT} 连接成功!")
            pkt = ctl_type0a(128, 128, 0, 255, 0)
            send_n(tcp, pkt, 200, interval=0.02, tcp=True)
            print("  已通过 TCP 发送 200 包")
            tcp.close()
        except Exception as e:
            print(f"  TCP 连接失败: {e}")

        # 停止
        stop = ctl_type0a(128, 128, 128, 128, 0)
        for _ in range(30):
            sock.sendto(stop, (IP, PORT))
            time.sleep(0.02)

        print("\n完成。如果仍然无反应，需要串口 CLI 检查飞控状态。")

    finally:
        sock.close()


if __name__ == "__main__":
    test_udp_variants()
