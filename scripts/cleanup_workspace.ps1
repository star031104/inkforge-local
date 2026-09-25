param(
    [switch]$RemoveCurrentReleases
)

$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent)).TrimEnd("\")
Set-Location $projectRoot

function Assert-WorkspacePath([string]$Path) {
    $resolved = [System.IO.Path]::GetFullPath($Path)
    if (-not $resolved.StartsWith(
        $projectRoot + "\",
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Refusing to remove a path outside the workspace: $resolved"
    }
    return $resolved
}

function Remove-WorkspaceItem([string]$RelativePath) {
    $target = Assert-WorkspacePath (Join-Path $projectRoot $RelativePath)
    if (Test-Path -LiteralPath $target) {
        Remove-Item -LiteralPath $target -Recurse -Force
        Write-Host "Removed $RelativePath"
    }
}

foreach ($relative in @(
    "build",
    "output",
    ".pytest_cache",
    ".ruff_cache",
    ".playwright-cli",
    "inkforge_local.egg-info",
    "logs",
    "InkForge.spec"
)) {
    Remove-WorkspaceItem $relative
}

$excludedRoots = @(
    (Join-Path $projectRoot ".venv") + "\",
    (Join-Path $projectRoot "dist") + "\",
    (Join-Path $projectRoot "data") + "\"
)
Get-ChildItem -LiteralPath $projectRoot -Directory -Recurse -Force `
    -Filter "__pycache__" -ErrorAction SilentlyContinue | ForEach-Object {
        $candidate = $_.FullName
        $excluded = $false
        foreach ($prefix in $excludedRoots) {
            if ($candidate.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
                $excluded = $true
                break
            }
        }
        if (-not $excluded) {
            Remove-Item -LiteralPath (Assert-WorkspacePath $candidate) -Recurse -Force
        }
    }

if ($RemoveCurrentReleases) {
    Remove-WorkspaceItem "dist"
}
elseif (Test-Path -LiteralPath "dist") {
    $config = Get-Content -LiteralPath "app\core\config.py" -Raw
    $match = [regex]::Match($config, 'APP_VERSION\s*=\s*"([^"]+)"')
    if (-not $match.Success) {
        throw "Unable to read APP_VERSION before pruning old release archives."
    }
    $currentArchive = "InkForge-Deploy-$($match.Groups[1].Value)-win64.zip"
    Get-ChildItem -LiteralPath "dist" -File -Filter "InkForge-Deploy-*-win64.zip" |
        Where-Object { $_.Name -ne $currentArchive } |
        ForEach-Object {
            Remove-Item -LiteralPath (Assert-WorkspacePath $_.FullName) -Force
            Write-Host "Removed old release $($_.Name)"
        }
}

Write-Host "Workspace cleanup complete. User data and backups were not touched."
