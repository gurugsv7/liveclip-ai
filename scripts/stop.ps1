<#  Graceful shutdown: finalizes open candidates, stops capture workers, flushes DB. Force-kills after 20 s. #>
$Root = Split-Path -Parent $PSScriptRoot
$port = if ($env:LIVECLIP_PORT) { $env:LIVECLIP_PORT } else { "8765" }
try { Invoke-RestMethod -Method Post "http://127.0.0.1:$port/api/system/shutdown" -TimeoutSec 5 | Out-Null; Write-Host "Shutdown requested" } catch { Write-Host "Backend not responding" }
$pidFile = Join-Path $Root "data\liveclip.pid"
for ($i = 0; $i -lt 40 -and (Test-Path $pidFile); $i++) { Start-Sleep -Milliseconds 500 }
if (Test-Path $pidFile) {
  $p = Get-Content $pidFile
  Write-Host "Forcing stop of PID $p (and its ffmpeg/streamlink children)"
  taskkill /PID $p /T /F | Out-Null
  Remove-Item $pidFile -ErrorAction SilentlyContinue
}
# any orphaned capture processes started by LiveClip (command line points at our data dir)
Get-CimInstance Win32_Process -Filter "Name='ffmpeg.exe' OR Name='streamlink.exe'" |
  Where-Object { $_.CommandLine -match [regex]::Escape((Join-Path $Root "data")) } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Write-Host "Stopped."
