# 无人机 Qt6 可视化上位机

基于 PySide6 / Qt6 的 WiFi UFO 协议无人机地面站，支持图传接收、飞行控制和 Cleanflight 串口调试。

> **安全警告**：测试飞控、起飞、降落、方向控制前，请务必拆桨或固定机体。程序默认锁定所有飞行动作，必须手动勾选安全确认后才会发送控制命令。

## 支持的设备

| 项目 | 参数 |
|------|------|
| 无人机 WiFi | `WiFiUFO-3BE7F2` |
| 无人机 IP | `192.168.0.1` |
| WiFi UFO UDP 端口 | `40000` |
| 串口飞控 | `COM9` (Silicon Labs CP210x) |
| 飞控固件 | Cleanflight / SPRACINGF3 1.13.0 (MPU6050) |

## 主要功能

- **Qt6 中文图形界面**：使用者界面 / 开发者界面双模式切换，清晰的标签页布局
- **WiFi UFO 协议支持**：UDP 心跳、图传接收、视频统计、遥控指令发送
- **视频流处理**：后台线程按 54 字节 JPEG 载荷偏移和分片序号重组，丢弃坏帧，约 18 FPS 限帧显示
- **飞行控制**：连续控制流（50ms 间隔）、短促方向脉冲、起飞、降落、急停
- **遥控器式布局**：方向按钮 + 滑块（Roll / Pitch / Throttle / Yaw）+ 键盘快捷键
- **串口 CLI 调试**：Cleanflight 命令行接口，支持 version / status / dump 等命令
- **安全锁机制**：所有飞行动作默认锁定，起飞/降落/急停需二次确认
- **监控面板**：实时遥测数据表格、WiFi/飞控/安全锁状态卡片
- **运行日志**：带时间戳的完整日志记录，同时写入文件

## 快速开始（Windows 可执行文件）

直接下载并运行 `DroneGroundStation.exe`（约 47 MB，已包含所有依赖）：

1. 从 [Releases](../../releases) 页面下载最新的 `DroneGroundStation.exe`
2. 双击运行，无需安装 Python 或任何依赖

## 开发环境运行

### 环境要求

- Python 3.11+
- PySide6

### 安装和运行

```powershell
# 克隆仓库
git clone https://github.com/1405264556/Prone_contron.git
cd Prone_contron

# 创建虚拟环境并安装依赖
python -m venv .venv_qt6
.\.venv_qt6\Scripts\pip install PySide6

# 运行上位机
.\.venv_qt6\Scripts\python.exe .\qt_ground_station\python_qt6\qt_drone_station.py
```

### 打包为 exe

```powershell
.\.venv_qt6\Scripts\pip install pyinstaller
.\.venv_qt6\Scripts\pyinstaller.exe --onefile --windowed --name DroneGroundStation `
    --distpath .\dist --workpath .\build `
    .\qt_ground_station\python_qt6\qt_drone_station.py
```

## 使用步骤

1. **拆桨或固定机体** —— 安全第一
2. **连接无人机 WiFi** —— 将电脑连接到 `WiFiUFO-3BE7F2` 热点
3. **启动上位机** —— 协议选择 `WiFi UFO UDP`，确认 IP 为 `192.168.0.1`，端口 `40000`
4. **点击 "UFO 心跳"** —— 启动图传和控制通道，观察视频预览
5. **串口调试（可选）** —— 选择 `COM9`，波特率 `115200`，切换到开发者界面
6. **飞行控制** —— 勾选安全锁，使用方向按钮或启动连续控制

## UI 界面说明

| 区域 | 内容 |
|------|------|
| 顶部配置栏 | 界面模式、协议选择、UDP 配置、串口配置、状态指示 |
| 左侧面板 | 图传预览 + 视频统计、运行日志（支持清空） |
| 监控标签页 | WiFi / 飞控 / 安全锁状态卡片 + 详细遥测数据表 |
| 飞行控制标签页 | 安全锁、命令状态、方向按钮（3x3 布局）、滑块调节、动作按钮 |
| 开发者标签页 | Cleanflight CLI 命令行（暗色终端风格） |
| 安全说明标签页 | 使用步骤、键盘快捷键表、控制协议说明 |

## 控制协议

WiFi UFO 控制包基于抓包确认的格式：

| 字节偏移 | 内容 | 说明 |
|----------|------|------|
| 0-1 | `63 63` | 协议头 |
| 2 | `0a` | 包类型（控制包） |
| 3-7 | — | 保留 |
| 8 | Roll | 0-255，128 为中位 |
| 9 | Pitch | 0-255，128 为中位 |
| 10 | Throttle | 0-255，128 为中位 |
| 11 | Yaw | 0-255，128 为中位 |
| 12 | Mode | 0=普通，1=起飞，2=降落，4=急停 |
| 13 | XOR | 字节 8-12 的 XOR 校验 |

- **心跳包**：`63 63 01 00 00 00 00`
- **控制模板**：`63 63 0a 00 00 08 00 66 80 80 80 80 00 00 99`

## 键盘快捷键

| 按键 | 功能 |
|------|------|
| `W` / `↑` | 前进 |
| `S` / `↓` | 后退 |
| `A` | 左移 |
| `D` | 右移 |
| `Q` | 左旋 |
| `E` | 右旋 |
| `R` | 升高 |
| `F` | 降低 |
| `Space` | 悬停 / 回中 |

## 项目结构

```text
Prone_contron/
├── README.md
├── DroneGroundStation.spec          # PyInstaller 打包配置
├── dist/
│   └── DroneGroundStation.exe       # 打包好的可执行文件
├── drone_ground_station.py          # 早期 Tkinter 探测原型
├── qt_ground_station/
│   ├── python_qt6/
│   │   ├── qt_drone_station.py      # 主程序（PySide6 / Qt6）
│   │   └── run_qt6_ground_station.ps1
│   ├── src/                         # C++ Qt6 版本源码
│   │   ├── main.cpp
│   │   ├── mainwindow.h / .cpp
│   │   ├── wifiufoclient.h / .cpp
│   │   └── serialcleanflightclient.h / .cpp
│   ├── QtDroneStation.pro           # QMake 工程文件
│   ├── CMakeLists.txt
│   └── DESIGN_中文.md               # 架构设计文档
└── runtime/                         # 运行时产物（日志等，已 gitignore）
```

## C++ Qt6 构建说明

本机 Qt 路径：`F:\Qt\6.10.3\mingw_64`。安装 MinGW 编译器后可尝试：

```powershell
cd qt_ground_station
F:\Qt\6.10.3\mingw_64\bin\qmake.exe QtDroneStation.pro
mingw32-make
```

## 开发记录

- UI 采用使用者 / 开发者双模式，通过顶部下拉框或菜单栏切换
- WiFi、图传、串口均使用独立 `QThread` 线程，避免界面卡顿
- 图传从固定 47 字节偏移修正为 54 字节 JPEG 载荷偏移，使用分片序号校验
- 控制包发送后实时显示最后一次已发送的控制量
- 安全锁状态通过颜色（红/绿）直观显示
- 菜单栏提供快速操作入口（刷新串口、清空日志、关于）

## License

MIT License
