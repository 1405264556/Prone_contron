# 在 Windows 上打包单文件 exe（输出到 dist\DroneGroundStation.exe）
#   用法：在仓库根目录执行  .\build_exe.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv_qt6")) { python -m venv .venv_qt6 }
.\.venv_qt6\Scripts\python -m pip install -r requirements-build.txt
.\.venv_qt6\Scripts\pyinstaller --noconfirm --distpath dist --workpath build qt_ground_station\python_qt6\DroneGroundStation.spec

# 自检：确认打包后的 exe 能正常启动
$p = Start-Process -FilePath "dist\DroneGroundStation.exe" -ArgumentList "--selftest" -Wait -PassThru
if ($p.ExitCode -ne 0) { throw "exe 自检失败，退出码 $($p.ExitCode)" }
Write-Host "打包完成：dist\DroneGroundStation.exe（自检通过）"
