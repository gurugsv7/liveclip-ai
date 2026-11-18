<#
  LiveClip AI - Windows setup.
  Installs (only if missing): Python 3.11, FFmpeg (gyan.dev build with NVENC + libass),
  the backend virtualenv (FastAPI, Streamlink, faster-whisper, CUDA runtime wheels),
  frontend packages, builds the UI, downloads the Whisper model and generates test fixtures.

  Usage (from the repo root, in PowerShell):
     powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
  Options:
     -NoCuda        skip the ~1 GB NVIDIA cuBLAS/cuDNN wheels (Whisper then runs on CPU)
     -NoModel       don't pre-download the Whisper model
     -WhisperModel  model to pre-download (default: small)
#>
param([switch]$NoCuda, [switch]$NoModel, [string]$WhisperModel = "small")
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Tools = Join-Path $env:LOCALAPPDATA "LiveClipTools"
New-Item -ItemType Directory -Force $Tools | Out-Null
function Step($m) { Write-Host "`n==> $m" -ForegroundColor Magenta }

# ---------------------------------------------------------------- Python
Step "Python 3.11+"
$pyExe = $null; $pyArgs = @()
foreach ($cand in @(@("py", "-3.12"), @("py", "-3.11"), @("python"))) {
  if (-not (Get-Command $cand[0] -ErrorAction SilentlyContinue)) { continue }
  $a = @($cand | Select-Object -Skip 1)
  try {
    $v = & $cand[0] @a -c "import sys;print(sys.version_info>=(3,11))" 2>$null
    if ($v -eq "True") { $pyExe = $cand[0]; $pyArgs = $a; break }
  } catch {}
}
if (-not $pyExe) {
  $uv = Join-Path $Tools "uv\uv.exe"
  if (-not (Test-Path $uv)) {
    Write-Host "Downloading uv (Python installer from Astral, MIT/Apache-2.0)..."
    $zip = Join-Path $Tools "uv.zip"
    Invoke-WebRequest "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip" -OutFile $zip -UseBasicParsing
    Expand-Archive $zip -DestinationPath (Join-Path $Tools "uv") -Force
    Remove-Item $zip
  }
  & $uv python install 3.11
  $pyExe = (& $uv python find 3.11).Trim()
  $pyArgs = @()
}
Write-Host "Using Python: $pyExe $pyArgs"

# ---------------------------------------------------------------- FFmpeg
Step "FFmpeg"
$ff = Get-Command ffmpeg -ErrorAction SilentlyContinue
$local = Get-ChildItem (Join-Path $Tools "ffmpeg") -Recurse -Filter ffmpeg.exe -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $ff -and -not $local) {
  Write-Host "Downloading FFmpeg release essentials (gyan.dev, GPL build incl. NVENC + libass)..."
  $zip = Join-Path $Tools "ffmpeg.zip"
  Invoke-WebRequest "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" -OutFile $zip -UseBasicParsing
  Expand-Archive $zip -DestinationPath (Join-Path $Tools "ffmpeg") -Force
  Remove-Item $zip
  $local = Get-ChildItem (Join-Path $Tools "ffmpeg") -Recurse -Filter ffmpeg.exe | Select-Object -First 1
}
$ffpath = if ($local) { $local.FullName } else { $ff.Source }
& $ffpath -hide_banner -version | Select-Object -First 1
$enc = & $ffpath -hide_banner -encoders 2>$null | Select-String "h264_nvenc"
if ($enc) { Write-Host "NVENC encoder available" -ForegroundColor Green } else { Write-Host "NVENC not available: exports will use x264 (CPU)" -ForegroundColor Yellow }

# ---------------------------------------------------------------- backend venv
Step "Backend virtual environment"
$venv = Join-Path $Root "backend\.venv"
if (-not (Test-Path "$venv\Scripts\python.exe")) { & $pyExe @pyArgs -m venv $venv }
$vpy = "$venv\Scripts\python.exe"
& $vpy -m pip install --upgrade pip wheel
& $vpy -m pip install -e "$Root\backend[ai,vision,test]"
if (-not $NoCuda) {
  Write-Host "Installing CUDA 12 runtime wheels for faster-whisper (cuBLAS + cuDNN 9)..."
  & $vpy -m pip install "nvidia-cublas-cu12" "nvidia-cudnn-cu12==9.*"
}
# YouTube changes break older streamlink plugins; always pull the newest on (re)setup
& $vpy -m pip install --upgrade streamlink
& "$venv\Scripts\streamlink.exe" --version

# ---------------------------------------------------------------- frontend
Step "Frontend"
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { throw "Node.js 18+ is required: https://nodejs.org/" }
Push-Location (Join-Path $Root "frontend")
npm install --no-audit --no-fund
npm run build
Pop-Location

# ---------------------------------------------------------------- config
Step "Configuration"
$envFile = Join-Path $Root ".env"
if (-not (Test-Path $envFile)) { Copy-Item (Join-Path $Root ".env.example") $envFile; Write-Host "Created .env (edit it to set paths/keys)" }

if (-not $NoModel) {
  Step "Whisper model ($WhisperModel)"
  $models = Join-Path $Root "data\models"
  New-Item -ItemType Directory -Force $models | Out-Null
  & $vpy -c "from faster_whisper import WhisperModel; WhisperModel('$WhisperModel', device='cpu', compute_type='int8', download_root=r'$models'); print('model ready')"
}

Step "Sound-event model (YAMNet, Apache-2.0, 16 MB)"
$yd = Join-Path $Root "data\models\yamnet"
New-Item -ItemType Directory -Force $yd | Out-Null
if (-not (Test-Path "$yd\yamnet.onnx")) {
  Invoke-WebRequest "https://huggingface.co/audiomagic/yamnet-onnx/resolve/main/yamnet.onnx" -OutFile "$yd\yamnet.onnx" -UseBasicParsing
  Invoke-WebRequest "https://huggingface.co/audiomagic/yamnet-onnx/resolve/main/yamnet_class_map.csv" -OutFile "$yd\yamnet_class_map.csv" -UseBasicParsing
}

Step "Test fixtures"
& $vpy -m liveclip.fixtures

Step "Done"
Write-Host "Start the app:   powershell -ExecutionPolicy Bypass -File scripts\start.ps1" -ForegroundColor Green
Write-Host "Dev mode:        powershell -ExecutionPolicy Bypass -File scripts\start-dev.ps1"
Write-Host "Run tests:       powershell -ExecutionPolicy Bypass -File scripts\test.ps1"
