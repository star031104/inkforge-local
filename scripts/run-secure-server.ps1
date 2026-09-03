param(
    [int]$Port = 7860
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Project Python environment not found: $pythonPath"
}

$secureToken = Read-Host "Paste SiliconFlow API Key (masked; never written to disk)" -AsSecureString
$tokenPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
try {
    $processToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($tokenPointer)
    if ([string]::IsNullOrWhiteSpace($processToken) -or -not $processToken.StartsWith("sk-")) {
        throw "Invalid API Key format; server was not started."
    }
    $env:INKFORGE_SILICONFLOW_API_KEY = $processToken
    Remove-Variable processToken -ErrorAction SilentlyContinue
    Set-Location -LiteralPath $projectRoot
    & $pythonPath -m uvicorn app.main:app --host 127.0.0.1 --port $Port
}
finally {
    if ($tokenPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($tokenPointer)
    }
    Remove-Item Env:INKFORGE_SILICONFLOW_API_KEY -ErrorAction SilentlyContinue
}
