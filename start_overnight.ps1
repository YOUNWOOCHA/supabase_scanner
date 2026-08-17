$ErrorActionPreference = "Stop"

Set-Location -LiteralPath $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($env:BRAVE_API_KEY)) {
    throw 'BRAVE_API_KEY is missing. Set it in this PowerShell window first.'
}

$pythonPath = (Get-Command python -ErrorAction Stop).Source
$stdoutPath = Join-Path $PSScriptRoot "output\overnight_stdout.log"
$stderrPath = Join-Path $PSScriptRoot "output\overnight_stderr.log"
$pidPath = Join-Path $PSScriptRoot "output\overnight_pid.txt"
New-Item -ItemType Directory -Force -Path (Join-Path $PSScriptRoot "output") | Out-Null

if (Test-Path -LiteralPath $pidPath) {
    $oldPid = (Get-Content -LiteralPath $pidPath -Raw).Trim()
    if ($oldPid -match '^\d+$' -and (Get-Process -Id ([int]$oldPid) -ErrorAction SilentlyContinue)) {
        throw "The overnight scanner is already running (PID $oldPid)."
    }
}

$process = Start-Process `
    -FilePath $pythonPath `
    -ArgumentList @("brave_supabase_pipeline.py", "--overnight-mode", "--api-request-budget", "700") `
    -WorkingDirectory $PSScriptRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutPath `
    -RedirectStandardError $stderrPath `
    -PassThru

Set-Content -LiteralPath $pidPath -Value $process.Id -Encoding ascii
Write-Host "Overnight scanner started in the background. PID: $($process.Id)"
Write-Host "Live log: Get-Content .\output\overnight_stderr.log -Encoding UTF8 -Tail 30 -Wait"
