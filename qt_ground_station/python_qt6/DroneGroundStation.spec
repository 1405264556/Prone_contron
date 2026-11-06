# -*- mode: python ; coding: utf-8 -*-
# 打包命令（在仓库根目录执行）：
#   pyinstaller --noconfirm --distpath dist --workpath build qt_ground_station/python_qt6/DroneGroundStation.spec
# 打包后自检：dist\DroneGroundStation.exe --selftest   （退出码 0 表示依赖完整）

a = Analysis(
    ['qt_drone_station.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['PySide6.QtSerialPort'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 只用到 QtCore/QtGui/QtWidgets/QtSerialPort，排除常见大模块以减小体积
    excludes=['tkinter', 'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'PySide6.Qt3DCore',
              'PySide6.QtQuick', 'PySide6.QtQml', 'PySide6.QtMultimedia', 'PySide6.QtCharts'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='DroneGroundStation',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
