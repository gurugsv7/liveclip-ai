<#  Production-like local start: backend serves the built UI at http://127.0.0.1:8765 #>
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$vpy = Join-Path $Root "backend\.venv\Scripts\python.exe"
if (-not (Test-Path $vpy)) { throw "Run scripts\setup.ps1 first" }
if (-not (Test-Path (Join-Path $Root "frontend\dist\index.html"))) { Push-Location "$Root\frontend"; npm run build; Pop-Location }
$port = if ($env:LIVECLIP_PORT) { $env:LIVECLIP_PORT } else { "8765" }
# Already running? Just open the dashboard instead of starting a second copy.
try {
  Invoke-RestMethod "http://127.0.0.1:$port/api/system/status" -TimeoutSec 2 | Out-Null
  Start-Process "http://127.0.0.1:$port/"
  Write-Host "LiveClip AI already running - opened http://127.0.0.1:$port" -ForegroundColor Green
  exit 0
} catch {}
$logs = Join-Path $Root "data\logs"; New-Item -ItemType Directory -Force $logs | Out-Null
Start-Process -FilePath $vpy -ArgumentList "-m", "liveclip", "--port", $port -WorkingDirectory (Join-Path $Root "backend") -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $logs "server.out") -RedirectStandardError (Join-Path $logs "server.err")
$up = $false
for ($i = 0; $i -lt 60; $i++) {
  Start-Sleep -Milliseconds 500
  try { Invoke-RestMethod "http://127.0.0.1:$port/api/system/status" -TimeoutSec 2 | Out-Null; $up = $true; break } catch {}
}
if (-not $up) { Write-Host "LiveClip AI did not start - see data\logs\server.err" -ForegroundColor Red; Get-Content (Join-Path $logs "server.err") -Tail 20; exit 1 }
Start-Process "http://127.0.0.1:$port/"
Write-Host "LiveClip AI running at http://127.0.0.1:$port  (API docs: /docs). Stop with scripts\stop.ps1" -ForegroundColor Green
