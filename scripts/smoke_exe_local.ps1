# Local frozen-exe smoke (mirrors the CI build-windows smoke step).
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_exe_local.ps1
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot "..")

dist/NSE-MT5-Bridge.exe --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "help failed" }
Write-Host "help OK"

$sw = [System.Diagnostics.Stopwatch]::StartNew()
$proc = Start-Process -FilePath "dist/NSE-MT5-Bridge.exe" -ArgumentList "--gui","none","--port","8799" -PassThru
try {
    $ready = $false
    while ($sw.Elapsed.TotalSeconds -lt 90 -and -not $ready) {
        Start-Sleep -Milliseconds 300
        try {
            $h = Invoke-WebRequest -Uri "http://127.0.0.1:8799/health" -UseBasicParsing -TimeoutSec 2
            if ($h.StatusCode -eq 200) { $ready = $true }
        } catch { }
    }
    if (-not $ready) { throw "no /health in 90s" }
    Write-Host ("startup-to-ready: {0:N1}s" -f $sw.Elapsed.TotalSeconds)
    $ui = Invoke-WebRequest -Uri "http://127.0.0.1:8799/ui" -UseBasicParsing -TimeoutSec 15
    Write-Host ("/ui HTTP {0}, {1} bytes" -f $ui.StatusCode, $ui.RawContentLength)
    $docs = Invoke-WebRequest -Uri "http://127.0.0.1:8799/docs" -UseBasicParsing -TimeoutSec 15
    Write-Host ("/docs HTTP {0}" -f $docs.StatusCode)
    foreach ($cli in @( @("status"), @("health"), @("mt5","list") )) {
        $out = & dist/NSE-MT5-Bridge.exe @cli --json 2>$null
        if ($LASTEXITCODE -ne 0) { throw "CLI '$cli' exit $LASTEXITCODE" }
        $doc = ($out -join "`n") | ConvertFrom-Json
        Write-Host ("CLI {0}: ok={1}" -f ($cli -join " "), $doc.ok)
    }
    $second = Start-Process -FilePath "dist/NSE-MT5-Bridge.exe" -ArgumentList "--gui","none","--port","8799" -Wait -PassThru
    Write-Host ("second instance exit code: {0}" -f $second.ExitCode)
} finally {
    if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }
}
