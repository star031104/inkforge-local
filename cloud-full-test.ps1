$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$env:PYTHONUTF8 = "1"

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    throw "Python virtual environment was not found. Run run.bat once first."
}

& ".venv\Scripts\python.exe" "scripts\cloud_full_e2e.py"
exit $LASTEXITCODE

