$ErrorActionPreference = "Stop"
$projectRoot = (Split-Path $PSScriptRoot -Parent)
Set-Location $projectRoot

if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
    throw "Run run.ps1 once to create the development environment."
}

& ".venv\Scripts\python.exe" -m pip install -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw "Failed to install build dependencies." }
& ".venv\Scripts\python.exe" "scripts\check_project.py"
if ($LASTEXITCODE -ne 0) { throw "Project contract check failed." }

$deployRoot = Join-Path $projectRoot "dist\InkForge-Deploy"
$launcherBuild = Join-Path $projectRoot "build\InkForgeLauncher"
if ((Split-Path $deployRoot -Parent) -ne (Join-Path $projectRoot "dist")) {
    throw "Unsafe deployment output path."
}
Remove-Item -LiteralPath $deployRoot -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $launcherBuild -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $deployRoot -Force | Out-Null

$pythonBase = & ".venv\Scripts\python.exe" -c "import sys; print(sys.base_prefix)"
$extraBinaries = @()
foreach ($dllName in @("tcl86t.dll", "tk86t.dll")) {
    $dllPath = Join-Path $pythonBase "Library\bin\$dllName"
    if (Test-Path -LiteralPath $dllPath) {
        $extraBinaries += @("--add-binary", "$dllPath;.")
    }
}

& ".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name InkForgeLauncher --distpath $deployRoot --workpath $launcherBuild `
    --specpath "build" @extraBinaries --hidden-import tkinter "scripts\bootstrap_launcher.py"
if ($LASTEXITCODE -ne 0) { throw "Launcher packaging failed." }

# PyInstaller owns its dist path while building. Add the application payload only
# after the executable is complete so a clean build cannot remove source files.
Copy-Item -LiteralPath "app" -Destination $deployRoot -Recurse
Copy-Item -LiteralPath "static" -Destination $deployRoot -Recurse
New-Item -ItemType Directory -Path (Join-Path $deployRoot "scripts") -Force | Out-Null
Copy-Item -LiteralPath "scripts\preflight_upgrade.py" `
    -Destination (Join-Path $deployRoot "scripts\preflight_upgrade.py")
Copy-Item -LiteralPath "scripts\rollback_upgrade.py" `
    -Destination (Join-Path $deployRoot "scripts\rollback_upgrade.py")
Copy-Item -LiteralPath "requirements.txt" -Destination (Join-Path $deployRoot "requirements.txt")
Copy-Item -LiteralPath "README.md" -Destination (Join-Path $deployRoot "README.md")

Get-ChildItem -LiteralPath $deployRoot -Directory -Recurse -Filter "__pycache__" | `
    Remove-Item -Recurse -Force
Get-ChildItem -LiteralPath $deployRoot -File -Recurse | `
    Where-Object { $_.Extension -in @(".pyc", ".pyo") } | `
    Remove-Item -Force

& ".venv\Scripts\python.exe" "scripts\smoke_launcher.py" `
    (Join-Path $deployRoot "InkForgeLauncher.exe") $deployRoot
if ($LASTEXITCODE -ne 0) { throw "Launcher smoke test failed." }

$archive = Join-Path $projectRoot "dist\InkForge-Deploy-0.33.1-win64.zip"
Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
Compress-Archive -Path (Join-Path $deployRoot "*") -DestinationPath $archive -CompressionLevel Optimal
Write-Host "Automatic deployment launcher: $deployRoot\InkForgeLauncher.exe"
Write-Host "Distribution archive: $archive"
