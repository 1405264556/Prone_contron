# 无人机 Qt6 可视化上位机

这是一个面向实验无人机的中文上位机项目，当前重点支持 `WiFiUFO-3BE7F2` 这类 WiFi UFO 协议无人机，并保留 Cleanflight 串口调试入口。

> 安全提醒：测试飞控、起飞、降落、方向控制前，请先拆桨或固定机体。程序默认锁定所有飞行动作，必须手动确认安全后才会发送控制命令。

## 当前已确认的设备信息

- 无人机 WiFi：`WiFiUFO-3BE7F2`
- 电脑 WLAN IP：`192.168.0.2`
- 无人机 IP：`192.168.0.1`
- WiFi UFO UDP：`40000`
- 串口飞控：`COM9`，Silicon Labs CP210x USB to UART Bridge
- 飞控固件线索：Cleanflight / SPRACINGF3 1.13.0，MPU6050

## 主要功能

- Qt6 中文图形界面，顶部可切换「使用者界面 / 开发者界面」
- WiFi UFO UDP 心跳、图传接收、视频统计、遥控指令发送
- 视频接收放入后台线程，按分片序号和 54 字节 JPEG 负载偏移重组，丢弃坏帧以减少彩色闪烁
- 飞行控制放入后台线程，支持连续控制流、短促方向控制、起飞、降落、急停
- 控制页提供 Roll / Pitch / Throttle / Yaw 滑块、方向按钮和键盘快捷控制
- 串口线程支持 Cleanflight CLI，开发者界面可发送 `version`、`status`、`dump`
- 日志、缓存、抓包测试文件统一放在项目内 `E:\AI_project\slam\runtime`，不写入 C 盘

## 快速运行

当前电脑缺少可用的 C++ 编译器，因此主力可测试版本是 PySide6/Qt6 版本：

```powershell
cd E:\AI_project\slam
.\.venv_qt6\Scripts\python.exe .\qt_ground_station\python_qt6\qt_drone_station.py
```

也可以运行脚本：

```powershell
cd E:\AI_project\slam
.\qt_ground_station\python_qt6\run_qt6_ground_station.ps1
```

依赖和缓存位置：

- Python 虚拟环境：`E:\AI_project\slam\.venv_qt6`
- pip 缓存：`E:\AI_project\slam\runtime\pip_cache`
- 运行日志：`E:\AI_project\slam\runtime\qt6_logs`

## 使用顺序

1. 拆桨或固定机体。
2. 连接无人机 WiFi：`WiFiUFO-3BE7F2`。
3. 打开上位机，协议选择 `WiFi UFO UDP`。
4. 保持 IP 为 `192.168.0.1`，远端 UDP 和本地 UDP 均为 `40000`。
5. 点击 `UFO 心跳`，观察视频预览和视频包统计。
6. 需要串口调试时，选择 `COM9`，波特率 `115200`，连接后进入开发者界面。
7. 飞行控制前勾选安全锁，再使用起飞、降落、方向按钮或连续控制流。

## 控制说明

WiFi UFO 控制包基于已抓包确认的格式：

- 心跳：`63 63 01 00 00 00 00`
- 控制模板：`63 63 0a 00 00 08 00 66 80 80 80 80 00 00 99`
- 字节 8-11：`roll / pitch / throttle / yaw`
- 字节 12：模式，`0` 普通控制，`1` 起飞，`2` 降落，`4` 急停
- 字节 13：字节 8-12 的 XOR 校验

键盘快捷键：

- `W/S` 或 `↑/↓`：前进 / 后退
- `A/D`：左移 / 右移
- `Q/E`：左旋 / 右旋
- `R/F`：升高 / 降低
- `Space`：悬停 / 回中

## 工程结构

```text
E:\AI_project\slam
  drone_ground_station.py                  # 早期 Tkinter 探测工具
  qt_ground_station/
    python_qt6/
      qt_drone_station.py                  # 当前可运行的 Qt6/PySide6 上位机
      run_qt6_ground_station.ps1
      README_中文.md
    src/                                   # C++ Qt6 版本源码
    QtDroneStation.pro
    CMakeLists.txt
  runtime/                                 # 本地运行产物，已被 .gitignore 排除
```

## C++ Qt6 构建说明

本机已发现 Qt：`F:\Qt\6.10.3\mingw_64`，但当前 PATH 中缺少 `g++`、`mingw32-make`、`cmake`。安装或配置 MinGW 后可尝试：

```powershell
cd E:\AI_project\slam\qt_ground_station
F:\Qt\6.10.3\mingw_64\bin\qmake.exe QtDroneStation.pro
mingw32-make
```

## 开发记录

- UI 已改为「使用者 / 开发者」双模式。
- WiFi、图传、串口均使用独立线程，减少界面卡顿。
- 图传从固定 47 字节偏移修正为 54 字节 JPEG 负载偏移，并使用分片序号校验，降低坏帧闪烁。
- 控制包发送后会在界面显示最后一次已发送的控制量，便于确认滑块和方向按钮是否生效。
