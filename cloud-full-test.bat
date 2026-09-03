@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"

if not exist ".venv\Scripts\python.exe" (
    echo Preparing isolated Python environment for cloud QA...
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 -m venv .venv
    ) else (
        where python >nul 2>nul
        if errorlevel 1 (
            echo [ERROR] Python 3.10+ was not found in PATH.
            endlocal
            exit /b 2
        )
        python -m venv .venv
    )
    if errorlevel 1 (
        echo [ERROR] Failed to create .venv.
        endlocal
        exit /b 2
    )
)

echo Checking project dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Dependency installation failed.
    endlocal
    exit /b 3
)

".venv\Scripts\python.exe" "scripts\cloud_full_e2e.py"
set "ERR=%ERRORLEVEL%"
endlocal & exit /b %ERR%

