# LiveClip AI — macOS install (MacBook)

> Looking for the Windows version? See [INSTALL-WINDOWS.md](INSTALL-WINDOWS.md) and download **LiveClip-AI-Windows.zip**.

**Download:** `LiveClip-AI-macOS.zip` from the [latest release](../../releases/latest), or clone this repo.

Same app as the Windows version — same detection, scoring, 30-second reel cutting and captions.
Mac differences: exports use Apple **VideoToolbox** (hardware), Whisper runs on the CPU (same results,
a little slower), captions use Arial Black, and the sidebar doesn't show GPU stats.
Apple Silicon (M1–M4) recommended; Intel Macs work but transcription is slower.

## 1. Install
1. Install Homebrew if you don't have it: https://brew.sh
2. Unzip `LiveClip-AI-macOS.zip` into your home folder (not iCloud Drive), then in Terminal:

```bash
cd ~/liveclip-ai
bash scripts/mac/setup.sh
```

This installs python@3.11, FFmpeg and Node via Homebrew (only if missing), the Python environment,
the dashboard, and the Whisper + YAMNet models (~600 MB).

## 2. Run
Double-click **`LiveClip.command`** — the dashboard opens at http://127.0.0.1:8765.
The first time, macOS may say it's from an unidentified developer: **right-click → Open** once.

From Terminal instead:
```bash
bash scripts/mac/start.sh
bash scripts/mac/stop.sh      # graceful stop
```
Double-click `Shutdown-in-30s.command` to stop LiveClip cleanly and shut the Mac down in 30 s
(cancel: `pkill -f liveclip-shutdown-timer`).

## 3. Optional: Twitch viewer clips
Create a free app at https://dev.twitch.tv/console and put `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET` in `.env`.

## Tests
```bash
bash scripts/mac/test.sh
```

See [README.md](README.md) for how the app works.
