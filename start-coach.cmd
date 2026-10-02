@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" (
  echo Projektumgebung fehlt. Bitte zuerst uv sync ausfuehren.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -u d3coach.py watch
pause
