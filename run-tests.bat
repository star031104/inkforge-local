@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Please run run.bat once first so the virtual environment is created.
  exit /b 1
)
".venv\Scripts\python.exe" -m pytest -q
endlocal

