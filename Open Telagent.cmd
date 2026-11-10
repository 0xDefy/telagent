@echo off
if not exist "%~dp0.venv\Scripts\pythonw.exe" (
  echo Python environment missing. Complete the setup in README.md first.
  pause
  exit /b 1
)
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0gui.py"
