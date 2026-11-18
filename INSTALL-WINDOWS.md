# LiveClip AI — Windows install

> Looking for the Mac version? See [INSTALL-MAC.md](INSTALL-MAC.md) and download **LiveClip-AI-macOS.zip**.

**Download:** `LiveClip-AI-Windows.zip` from the [latest release](../../releases/latest), or clone this repo.

Tested on Windows 11 with an NVIDIA RTX 4060 Laptop GPU (any NVIDIA GPU with NVENC works; without one, exports fall back to the CPU).

## 1. Install
Unzip to a normal folder (not OneDrive — the rolling buffer writes and deletes a file every few seconds), e.g. `C:\Users\<you>\liveclip-ai`, then in PowerShell:

```powershell
cd C:\Users\<you>\liveclip-ai
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

This installs Python 3.11 if missing, FFmpeg (with NVENC + captions), the Python environment, the
CUDA libraries for GPU transcription, the dashboard, and the Whisper + YAMNet models (~1.5 GB total).
Node.js 18+ is required: https://nodejs.org/

## 2. Run
Double-click **`LiveClip.cmd`**. It starts LiveClip and opens the dashboard, or just opens the dashboard if
LiveClip is already running. Right-click it → *Send to → Desktop (create shortcut)* for a desktop button.

From PowerShell instead:
```powershell
powershell -ExecutionPolicy Bypass -File scripts\start.ps1     # opens http://127.0.0.1:8765
powershell -ExecutionPolicy Bypass -File scripts\stop.ps1      # graceful stop
```
Double-click `Shutdown-in-30s.cmd` to stop LiveClip cleanly and shut Windows down in 30 s (cancel: `shutdown /a`).

## 3. Optional: Twitch viewer clips
Create a free app at https://dev.twitch.tv/console and put `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET` in `.env`.

## Tests
```powershell
powershell -ExecutionPolicy Bypass -File scripts\test.ps1
```

See [README.md](README.md) for how the app works.
