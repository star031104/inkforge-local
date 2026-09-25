$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    throw "Run run.ps1 once to create the development environment."
}

& ".venv\Scripts\python.exe" -m pip install -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw "Failed to install build dependencies." }
& ".venv\Scripts\python.exe" "scripts\check_project.py"
if ($LASTEXITCODE -ne 0) { throw "Project contract check failed." }

Remove-Item -LiteralPath "build\InkForge" -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath "dist\InkForge" -Recurse -Force -ErrorAction SilentlyContinue
$pythonBase = & ".venv\Scripts\python.exe" -c "import sys; print(sys.base_prefix)"
$extraBinaries = @()
foreach ($dllName in @("ffi.dll", "sqlite3.dll")) {
    $dllPath = Join-Path $pythonBase "Library\bin\$dllName"
    if (Test-Path $dllPath) {
        $extraBinaries += @("--add-binary", "$dllPath;.")
    }
}
& ".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onedir `
    --name InkForge --add-data "static;static" @extraBinaries --collect-all uvicorn `
    --collect-all fastapi --hidden-import app.main --hidden-import app.entrypoints.server `
    scripts\desktop_entry.py
if ($LASTEXITCODE -ne 0) { throw "Application packaging failed." }
& ".venv\Scripts\python.exe" "scripts\smoke_packaged.py" "dist\InkForge\InkForge.exe"
if ($LASTEXITCODE -ne 0) { throw "Packaged application smoke test failed." }

$iscc = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if ($iscc) {
    & $iscc.Source "packaging\inkforge.iss"
    if ($LASTEXITCODE -ne 0) { throw "Installer creation failed." }
    Write-Host "Installer created in dist\installer."
} else {
    Write-Host "Portable build created in dist\InkForge. Install Inno Setup to create the installer."
}
