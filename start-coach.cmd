@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" (
    echo Project environment missing. Run uv sync --locked first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -u d3coach.py watch
pause
