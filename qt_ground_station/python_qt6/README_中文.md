# Qt6/PySide6 可运行测试版

这个目录是为了在当前电脑缺少 C++ 编译器时，仍然能完整运行和测试 Qt6 上位机。

运行环境在项目内：

```powershell
E:\AI_project\slam\.venv_qt6\Scripts\python.exe E:\AI_project\slam\qt_ground_station\python_qt6\qt_drone_station.py
```

依赖和缓存位置：

- 虚拟环境：`E:\AI_project\slam\.venv_qt6`
- pip 缓存：`E:\AI_project\slam\runtime\pip_cache`
- 运行日志：`E:\AI_project\slam\runtime\qt6_logs`

功能与 C++ Qt 工程保持一致：

- WiFi UFO UDP 40000 心跳、信息包解析、视频分片与图像预览
- COM9 / Cleanflight CLI 监控
- 中文遥测表、日志、控制滑条、安全锁

