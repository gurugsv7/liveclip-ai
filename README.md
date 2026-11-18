# LiveClip AI

A local-first command center for clipping livestreams. It monitors up to four Twitch, YouTube Live or Kick streams at once, keeps a 2-minute rolling buffer of original-quality footage for each one, spots likely highlights from audio, chat and speech, and turns the moments you keep into 1080×1920 Shorts/Reels with word-timed captions.

It needs no cloud services, no paid APIs and no social-media accounts to record, analyze, edit or export.

Built for: Windows 10/11 · RTX 4060 Laptop (8 GB) · i7-13250H · 16 GB RAM · 30 Mbps.


## Download

| Platform | Download | Install guide |
|---|---|---|
| 🪟 **Windows 10/11** (NVIDIA GPU recommended) | **LiveClip-AI-Windows.zip** from [Releases](../../releases/latest) | [INSTALL-WINDOWS.md](INSTALL-WINDOWS.md) |
| 🍎 **macOS** (Apple Silicon recommended) | **LiveClip-AI-macOS.zip** from [Releases](../../releases/latest) | [INSTALL-MAC.md](INSTALL-MAC.md) |

Both are the same app. Each zip contains only its own platform's scripts.

---

## Quick start

```powershell
# 1. one-time setup (installs Python 3.11 if missing, FFmpeg, venv, UI, Whisper model, fixtures)
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1

# 2. run
powershell -ExecutionPolicy Bypass -File scripts\start.ps1      # opens http://127.0.0.1:8765

# 3. stop (graceful: finalizes pending clips, stops capture)
powershell -ExecutionPolicy Bypass -File scripts\stop.ps1
```

To develop with hot reload, run `scripts\start-dev.ps1`. It starts the backend on :8765 and Vite on :5173.

**To try it without live streams:** set `LIVECLIP_ALLOW_LOCAL=1` in `.env`, start the app, then add the streams `local:fixture_a` … `local:fixture_d`. Each one is a looping test video with its own tone and chat, and a "reaction" partway through.

### Manual setup (if you prefer)

| Step | Command |
|---|---|
| Python venv | `py -3.11 -m venv backend\.venv` |
| Backend + Streamlink + Whisper | `backend\.venv\Scripts\pip install -e "backend[ai,vision,test]"` |
| CUDA for Whisper (GPU) | `backend\.venv\Scripts\pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"` |
| FFmpeg | Install any build that has `h264_nvenc` and `libass`, e.g. gyan.dev "release essentials". Put it on PATH or set `LIVECLIP_FFMPEG`/`LIVECLIP_FFPROBE`. |
| Frontend | `cd frontend && npm install && npm run build` |
| Whisper model | Downloaded on first use into `data/models` (`small` ≈ 480 MB) |
| Run | `cd backend && .venv\Scripts\python -m liveclip` |

Copy `.env.example` to `.env` to change the data directory, tool paths, port or API keys. **Keep `LIVECLIP_DATA_DIR` out of OneDrive.** The buffer creates and deletes a file every few seconds.

---

## macOS (MacBook) — same app, same behaviour

The code is shared; only platform pieces differ. Detection, scoring, reel cutting, captions and the dashboard are identical.

| | Windows | macOS |
|---|---|---|
| Setup | `scripts\setup.ps1` | `bash scripts/mac/setup.sh` (needs [Homebrew](https://brew.sh); installs python@3.11, ffmpeg, node) |
| Start / stop | `scripts\start.ps1` / `stop.ps1` | **double-click `LiveClip.command`** or `bash scripts/mac/start.sh` / `stop.sh` |
| Tests | `scripts	est.ps1` | `bash scripts/mac/test.sh` |
| Shut down in 30 s | `Shutdown-in-30s.cmd` | `Shutdown-in-30s.command` (cancel: `pkill -f liveclip-shutdown-timer`) |
| Export encoder (auto) | NVIDIA NVENC | Apple **VideoToolbox** (hardware), x264 fallback |
| Whisper | CUDA (GPU) | CPU int8: same model and results, a little slower |
| Caption font | Segoe UI Black | Arial Black (same heavy look) |
| GPU stats in sidebar | nvidia-smi | not shown (CPU/RAM/network/disk still are) |

First run on a Mac:
```bash
cd ~/liveclip-ai && bash scripts/mac/setup.sh
```
Then double-click **LiveClip.command**. If macOS blocks it the first time ("unidentified developer"), right-click → Open once.
Keep `LIVECLIP_DATA_DIR` out of iCloud Drive (the buffer writes/deletes a file every few seconds).

## Using it

1. **Live Monitor:** click an empty slot, paste a channel URL, then start monitoring. Each panel shows:
   - the local preview
   - the real source quality (taken from ffprobe of the captured segments, not the platform's label)
   - measured bitrate and recording time
   - audio, chat and hype meters
   - a rolling-buffer bar (grey = buffered, violet = protected, ticks = events)
2. **Listen:** click a speaker icon to hear that stream only. Click a different one to switch. Click the active one again to mute everything. Recording audio is never affected.
3. **Save:** "Save 60s" (the arrow offers 15/30/60/120) or **Mark** (protects 30 s before and 20 s after). The app subtracts the preview delay, so you get what you just saw.
4. **AI:** while AI is on for a stream, hype candidates are protected, extracted, transcribed and scored automatically. Turning AI off never stops recording.
5. **Review Queue:** clips sorted by AI score. Keys: `Y` approve, `N` reject, `←/→` move between clips.
6. **Clips:** the editor has:
   - trim
   - layout: Smart crop / Facecam + gameplay / Original letterboxed
   - cold-open hook and punch-in at the peak
   - caption style, size and position
   - word-level caption corrections
   - **Preview** (540×960 fast render), then **Export 1080×1920**

   Exports go to `data/exports/<streamer>/` together with an `.srt` and a HyperFrames-format `.transcript.json`.

Keyboard (Live Monitor; ignored while typing): `1–4` select · `M` mute/unmute · `C` save 60 s · `B` bookmark · `E` expand · `A` toggle AI · `F` fullscreen. You can rebind them in Settings → Shortcuts.

### How detection avoids missing moments (without filling your disk)
| Signal | What it catches | Cost |
|---|---|---|
| **Twitch viewer clips** | Any moment a viewer clips, saved from our own 1080p60 buffer. Needs a free Twitch app ID/secret in `.env` | API poll every 15 s |
| **Crowd "clip it"** | 3+ different chatters asking for a clip | free |
| **Chat** | Rate spikes vs the channel's normal, LMAO/WTF/emotes, copy-paste spam, short reactions (LIGHTOR research) | free |
| **Sound events (YAMNet)** | Laughter, screams, shouts and cheering, even at normal volume. Ignores loud game noise (gunfire, explosions, cars, music) | ~5 ms CPU/stream/s |
| **Loudness** | Shouting vs the streamer's own baseline; silence ignored | free |
| **Live speech (Whisper)** | "Oh my god", "let's go", "no way", repetition | ~0.5 s GPU per 6 s |

Near-misses aren't thrown away. AI clips scoring 0.25–0.50 go to a **Maybe** pool, capped at 3 GB (lowest scores are evicted first), so you can rescue them from Clips → Maybe. The rolling buffer is 3 minutes per stream (~170 MB at 1080p60).

### How exported reels are made (house style)

Exports follow the short-form rules from the `short-form-video`, `caption-animation` and `embedded-captions` skills in your `insta-reels` workspace:

- **Reels are 30 seconds max: the best moment only.** People don't watch 1-minute clips.
  - Source clips can be longer, but every export is auto-cut to the strongest ≤30 s stretch, usually 15–30 s.
  - It picks the run with the densest reactions (including the hype peak) and can splice a short setup line in front, like "I already lost… GG" before the comeback.
  - Silences over 1 s are removed as jump cuts, and the reel ends right after the payoff line.
  - If you set a trim of 30 s or less yourself, it's used exactly. The limit is in Settings → Export → "Max reel length".

- **The hook is on screen from frame 1.** The clip title appears in the top safe area with no fade-in. The optional *cold open* flashes the peak first, then cuts back to the setup with a two-frame white flash.
- **Captions use real word timestamps from Whisper.** They show 1–3 words per page, highlight the active word in a single accent colour and use heavy stroke and shadow. They sit in the centre band (~66% down), inside the 900×1400 safe box and clear of the bottom 320 px and the right-hand rail. Fillers (um/uh) are dropped and nothing is invented.
- **Framing:**
  - Smart crop follows motion and detail with a dead zone and a speed limit, so it glides rather than jitters.
  - Facecam mode stacks the facecam (40%) over the gameplay.
  - Letterbox keeps the whole frame over a blurred fill.
- **Quality:**
  - NVENC `p6/hq` with VBR at CQ 19 (cap 20 Mbps) and AQ, or x264 `slow` at CRF 18.
  - AAC 192k / 48 kHz, `+faststart`.
  - The output never exceeds the source frame rate.
  - The original audio is kept; loudness normalization is opt-in.
- For a full cinematic caption treatment, feed the exported `*.transcript.json` and MP4 to the HyperFrames `embedded-captions` workflow. The file is already in its flat word-array format.

---

## Tests

```powershell
powershell -ExecutionPolicy Bypass -File scripts\test.ps1          # everything (~5 min)
powershell -ExecutionPolicy Bypass -File scripts\test.ps1 -Fast    # unit tests + frontend only
cd backend; .venv\Scripts\python -m pytest tests\test_integration.py -v -k audio   # one case
cd frontend; npx vitest run
```

| Requirement | Test |
|---|---|
| 1 Independent recording of ≥2 streams | `test_integration::test_01` |
| 2 Streamer↔audio association (FFT: 440 Hz vs 880 Hz) | `test_integration::test_03` |
| 3 Streamer↔chat association | `test_integration::test_04`, `test_units::test_twitch_parser_rejects_other_channels` |
| 4 Rolling-buffer deletion | `test_units::test_rolling_buffer…`, `test_integration::test_06` |
| 5 Protected-segment preservation | `test_units::test_rolling_buffer…`, `…concurrent_protections…`, `test_integration::test_10` |
| 6 Concurrent manual saves | `test_integration::test_03`, `test_units::test_concurrent_protections…` |
| 7 Simultaneous hype events on separate streamers | `test_units::test_simultaneous_events…`, `test_integration::test_05` |
| 8 Recording worker crash | `test_integration::test_07` |
| 9 Stream reconnection | `test_integration::test_07` |
| 10 AI job failures | `test_units::test_job_failures…`, `…transcriber_reports_unavailable…` |
| 11 Disk-space limits | `test_units::test_temp_budget…`, `test_integration::test_12` |
| 12 Duplicate clip suppression | `test_units::test_duplicate_triggers…`, `…cooldown…`, `test_integration::test_05` |
| 13 Valid MP4 output | `test_integration::test_03`, `test_08` |
| 14 Frontend API controls | `frontend/src/test/controls.test.tsx` |
| 15 DB consistency after interrupted jobs | `test_integration::test_13`, `test_units::test_crash_recovery…` |
| 16 Timestamp alignment | `test_integration::test_05` (event lands on the known 22 s burst) |
| 17 Preview/audio mute controls | `controls.test.tsx` (dashboard audio monitoring) |
| 18 Unavailable external services | `test_units::test_ollama…`, `…transcriber…`, `test_integration::test_11` |
| Bandwidth: preview causes no extra downloads | `test_integration::test_02` (all sockets blocked while the preview is served) |

**Soak test (bounded storage over hours):**

```powershell
backend\.venv\Scripts\python scripts\soak_test.py --hours 3                       # 4 local fixtures, no internet
backend\.venv\Scripts\python scripts\soak_test.py --hours 2 --real https://www.twitch.tv/a https://www.twitch.tv/b
```

Every minute it writes a CSV row with temp bytes, segment files, protected segments, DB size, RSS, download Mbps and process counts. It fails if unprotected footage, file counts or memory grow beyond their bounds.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| "FFmpeg not found" | Run `setup.ps1`, or set `LIVECLIP_FFMPEG` and `LIVECLIP_FFPROBE` in `.env`. |
| Stream shows **Offline** | The channel isn't live. The app keeps checking every 45 s. |
| **Reconnecting** loops | Check `data/logs/liveclip.log`. The panel tooltip shows the last Streamlink/FFmpeg error. |
| Preview stays "Buffering" | The preview starts after 2 segments (~10 s). AV1/HEVC sources may not decode in the browser; a warning appears. |
| Whisper says CPU even though you have a GPU | Install the CUDA wheels (`setup.ps1` without `-NoCuda`), then restart. Settings → AI models → "Load Whisper now" shows the reason. |
| Exports use x264 | NVENC isn't present in your FFmpeg build, or the driver is too old. Update the NVIDIA driver or use the gyan.dev build. |
| Captions use the wrong font | Settings → Export → caption font must be an installed family name (default `Segoe UI Black`), or point "Extra fonts folder" at TTF/OTF files. |
| No chat on YouTube | Set `YOUTUBE_API_KEY`. Some streams have chat disabled. Quota use is shown in the chat tooltip. |
| Disk warnings | Settings → Storage: lower the buffer budget, clean up temp files, or move `LIVECLIP_DATA_DIR`. |

Logs: `data/logs/liveclip.log` (rotated). API docs: http://127.0.0.1:8765/docs.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design and [docs/LICENSES.md](docs/LICENSES.md) for third-party licenses.
