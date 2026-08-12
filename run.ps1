$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$env:PYTHONUTF8 = "1"

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        py -3 -m venv .venv
    }
    elseif (Get-Command python -ErrorAction SilentlyContinue) {
        python -m venv .venv
    }
    else {
        throw "Python was not found. Install Python 3.10+ and add it to PATH."
    }
}

$requirementsHash = (Get-FileHash "requirements.txt" -Algorithm SHA256).Hash
$requirementsMarker = ".venv\requirements.sha256"
$installedHash = if (Test-Path $requirementsMarker) {
    (Get-Content $requirementsMarker -Raw).Trim()
} else {
    ""
}
if ($installedHash -ne $requirementsHash) {
    Write-Host "Installing or updating project dependencies..."
    & ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    Set-Content -Path $requirementsMarker -Value $requirementsHash -Encoding ASCII
}
Write-Host "InkForge Local: http://127.0.0.1:7860"
Write-Host "Press Ctrl+C to stop the application."
& ".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 7860
