$pidPath = Join-Path $PSScriptRoot "output\overnight_pid.txt"
$logPath = Join-Path $PSScriptRoot "output\overnight_stderr.log"

if (Test-Path -LiteralPath $pidPath) {
    $scannerPid = (Get-Content -LiteralPath $pidPath -Raw).Trim()
    $running = $scannerPid -match '^\d+$' -and (Get-Process -Id ([int]$scannerPid) -ErrorAction SilentlyContinue)
    if ($running) { Write-Host "RUNNING (PID $scannerPid)" } else { Write-Host "NOT RUNNING" }
} else {
    Write-Host "No overnight run record found."
}

if (Test-Path -LiteralPath $logPath) {
    Write-Host "`nRecent log:"
    Get-Content -LiteralPath $logPath -Encoding UTF8 -Tail 25
}
