# Qt6 / PySide6 上位机（当前维护版本）

主程序：`qt_drone_station.py`。完整使用说明见仓库根目录 [README.md](../../README.md)。

从源码运行（在仓库根目录）：

```powershell
python -m venv .venv_qt6
.\.venv_qt6\Scripts\pip install -r requirements.txt
.\.venv_qt6\Scripts\python qt_ground_station\python_qt6\qt_drone_station.py
# 或：.\qt_ground_station\python_qt6\run_qt6_ground_station.ps1
```

打包：仓库根目录执行 `.\build_exe.ps1`，输出 `dist\DroneGroundStation.exe`。

运行时文件：日志 `runtime\qt6_logs\`、设置 `runtime\settings.ini`、崩溃记录 `runtime\qt6_logs\crash.log`
（目录不可写时改存 `%USERPROFILE%\.drone_ground_station\`；也可用环境变量 `DRONE_GS_RUNTIME_DIR` 指定）。
