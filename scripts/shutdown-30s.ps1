<#  Stop LiveClip AI cleanly, then shut the laptop down in 30 seconds.
    Run:    powershell -ExecutionPolicy Bypass -File scripts\shutdown-30s.ps1
    Cancel: shutdown /a   (within the 30 seconds)                                    #>
$Root = Split-Path -Parent $PSScriptRoot
Write-Host "Stopping LiveClip AI (finishing open clips, closing recordings)..." -ForegroundColor Magenta
& powershell -ExecutionPolicy Bypass -File (Join-Path $Root "scripts\stop.ps1")
shutdown /s /t 30 /c "LiveClip AI stopped. Laptop shuts down in 30 seconds. Run 'shutdown /a' to cancel."
Write-Host "Shutting down in 30 s. To cancel, run:  shutdown /a" -ForegroundColor Yellow
