$ErrorActionPreference = "Stop"
$pidPath = Join-Path $PSScriptRoot "output\overnight_pid.txt"

if (-not (Test-Path -LiteralPath $pidPath)) {
    Write-Host "No overnight scanner record found."
    exit 0
}

$scannerPid = (Get-Content -LiteralPath $pidPath -Raw).Trim()
if ($scannerPid -notmatch '^\d+$') {
    throw "Invalid PID file: $pidPath"
}

$process = Get-Process -Id ([int]$scannerPid) -ErrorAction SilentlyContinue
if ($process) {
    Stop-Process -Id ([int]$scannerPid)
    Write-Host "Overnight scanner stopped (PID $scannerPid)."
} else {
    Write-Host "The overnight scanner is already stopped."
}
