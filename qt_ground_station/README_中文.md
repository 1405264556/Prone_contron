# Qt6 无人机可视化上位机

这是基于 Qt6 Widgets 的中文上位机工程，面向当前实验无人机的两条已确认链路：

- WiFi：`WiFiUFO-3BE7F2`，无人机 IP `192.168.0.1`，UDP `40000`
- 串口：`COM9`，Cleanflight `SPRACINGF3 1.13.0`，`115200 8N1`

## 功能

- WiFi UFO UDP 连接、心跳握手、信息包解析
- WiFi UFO 视频分片接收、JPEG 帧重组预览、视频包速率统计
- Cleanflight CLI 串口连接、`version/status/dump` 快捷命令
- 遥测表格：WiFi 信息、视频状态、飞控版本、电压、传感器、I2C 错误等
- 手动控制区：roll/pitch/throttle/yaw 滑条
- 安全锁：未勾选安全确认时，控制流、起飞、降落、急停都不会发送
- 日志落盘：`E:\AI_project\slam\runtime\qt_logs`

## 工程结构

```text
qt_ground_station/
  QtDroneStation.pro          # qmake 工程
  CMakeLists.txt              # CMake 工程
  src/
    main.cpp
    mainwindow.h/.cpp         # 中文主界面
    wifiufoclient.h/.cpp      # WiFi UFO UDP 协议、图传、控制包
    serialcleanflightclient.h/.cpp # Cleanflight 串口 CLI
```

## 构建

本机目前已有 Qt：`F:\Qt\6.10.3\mingw_64`，但当前 PATH 中没有 `g++/mingw32-make/cmake`，所以我只能生成工程，暂时不能在此环境完成编译。

安装或配置 MinGW 后可用：

```powershell
cd E:\AI_project\slam\qt_ground_station
F:\Qt\6.10.3\mingw_64\bin\qmake.exe QtDroneStation.pro
mingw32-make
```

或者用 Qt Creator 打开 `QtDroneStation.pro`，选择 Qt 6.10.3 MinGW Kit 构建。

## 使用顺序

1. 拆桨或固定机体。
2. 连接无人机 WiFi：`WiFiUFO-3BE7F2`。
3. 打开上位机，协议选择 `WiFi UFO UDP`。
4. 保持 IP `192.168.0.1`，远端/本地 UDP 都为 `40000`。
5. 点击 `UFO 心跳`，应出现 WiFi UFO 信息包和视频包统计。
6. 串口选择 `COM9 - Silicon Labs CP210x...`，波特率 `115200`，点击连接串口。
7. 在 `飞控 CLI` 页点击 `version/status` 查看 Cleanflight 状态。
8. 只有确认安全后，才勾选安全锁并启动控制流。

## 控制协议

WiFi UFO 控制包参考公开逆向项目 `wifi-ufo-drone`：

- 心跳：`63 63 01 00 00 00 00`
- 控制模板：`63 63 0a 00 00 08 00 66 80 80 80 80 00 00 99`
- 字节 8-11：roll / pitch / throttle / yaw
- 字节 12：模式，`0` 普通控制，`1` 起飞，`2` 降落，`4` 急停
- 字节 13：字节 8-12 的 XOR 校验

上位机默认不会发送控制流；必须手动勾选安全锁。

