<#  Download an extra Whisper model once, e.g. the bigger one used for Tamil clip captions.
    Run it while you're NOT monitoring streams: large-v3 is ~3 GB.
    Usage: powershell -ExecutionPolicy Bypass -File scripts\download-model.ps1 [-Model large-v3] #>
param([string]$Model = "large-v3")
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$vpy = Join-Path $Root "backend\.venv\Scripts\python.exe"
$models = Join-Path $Root "data\models"
Write-Host "Downloading Whisper $Model to $models (one time)..." -ForegroundColor Cyan
& $vpy -c "from faster_whisper.utils import download_model; print(download_model('$Model', cache_dir=r'$models'))"
Write-Host "Done. Restart LiveClip to use it." -ForegroundColor Green
