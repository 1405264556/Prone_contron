$ErrorActionPreference = "Stop"

$ProjectRoot = "E:\AI_project\slam"
$env:PIP_CACHE_DIR = Join-Path $ProjectRoot "runtime\pip_cache"
$env:PYTHONPYCACHEPREFIX = Join-Path $ProjectRoot "runtime\pycache"

$Python = Join-Path $ProjectRoot ".venv_qt6\Scripts\pythonw.exe"
$App = Join-Path $ProjectRoot "qt_ground_station\python_qt6\qt_drone_station.py"

Start-Process -FilePath $Python -ArgumentList $App -WorkingDirectory $ProjectRoot
