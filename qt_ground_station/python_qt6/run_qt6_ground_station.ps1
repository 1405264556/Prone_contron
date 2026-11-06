# 从源码启动上位机（Windows PowerShell）。首次使用先执行：
#   python -m venv .venv_qt6
#   .\.venv_qt6\Scripts\pip install -r requirements.txt
$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$Python = Join-Path $ProjectRoot ".venv_qt6\Scripts\pythonw.exe"
$App = Join-Path $PSScriptRoot "qt_drone_station.py"

if (-not (Test-Path $Python)) {
    Write-Host "未找到虚拟环境：$Python"
    Write-Host "请先在仓库根目录执行：python -m venv .venv_qt6 ; .\.venv_qt6\Scripts\pip install -r requirements.txt"
    exit 1
}
Start-Process -FilePath $Python -ArgumentList "`"$App`"" -WorkingDirectory $ProjectRoot
