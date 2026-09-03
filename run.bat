@echo off
setlocal
cd /d "%~dp0"
rem Windows may block unsigned .ps1 files downloaded from the Internet.
rem Launching with Process-scoped Bypass avoids changing the user's global policy.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1"
endlocal

