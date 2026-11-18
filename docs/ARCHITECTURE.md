# Architecture

```
                     ┌──────────────────────── one per stream (×4, fully independent) ────────────────────────┐
 Twitch/YouTube/Kick │ streamlink --stdout ─► ffmpeg -i pipe:0 ─┬─ -c copy -f segment ─► recordings/<slug>/<session>/segments/seg_NNNNNN.ts
   (one download)    │                                          │                         (+ segments_runNNN.csv: completed segments only)
                     │                                          └─ audio→16 kHz mono PCM ─► AudioAnalyzer (RMS/peak/baseline/VAD, 150 s ring)
                     │ ChatClient (own channel only) ──────────────────────────────────────► ChatAnalyzer (rate vs own baseline, keywords, laughs)
                     │ HypeDetector (state machine) ◄── audio + chat every 0.5 s            │
                     └─────────────────────────────────────────────────────────────────────┘
                                   │ trigger → protect range        │ finalize → clip record
                                   ▼                                ▼
   RetentionManager (lock) ── deletes only indexed, completed, unprotected segments older than 120 s; byte budget; orphan cleanup
   JobRunner (SQLite-backed, per-kind concurrency) ── extract(2) → transcribe(1, shared Whisper) → score(1) → render(1, low priority)
   FastAPI ── REST + WebSocket + /media/live/<session>/index.m3u8 (HLS over the same .ts files) + /media/clips/…
   React UI ── hls.js players (muted unless selected), meters, controls, editor
```

## Data isolation: why audio, video, chat and clips never mix

- **One pipeline per stream.** Each `LiveSession` has its own Streamlink and FFmpeg processes, segment directory (`recordings/<slug>/<session_id>/`), `AudioAnalyzer`, `ChatClient` + `ChatAnalyzer` and `HypeDetector`. There is no shared mixer, no loopback or desktop audio, and no OBS output.
- **Audio comes from that stream's own FFmpeg process.** The PCM fed to analysis is decoded from the same input that produces the stream's segments. Clip audio is copied from that stream's own segments.
- **Chat is pinned to a channel.** Each chat client knows only its channel. The Twitch parser drops any `PRIVMSG` for another channel. Every chat row stores `streamer_id` and `session_id`.
- **Database rows carry both ids.** Every media row has `streamer_id` and `session_id` with foreign keys. Extraction selects `WHERE session_id = ?`, and from a **single capture run**, so it never splices across a reconnect, another session or another streamer.
- **Shared Whisper, tagged jobs.** All streams share one Whisper model, but each job carries the clip, session and streamer ids, and the result is checked against them before it is stored.
- **Files are always namespaced.** All file names live under a streamer- and session-specific directory. Nothing like `latest.ts` is shared.
- **Tests prove it.** In `test_integration::test_03`, two streams with different tones (440 Hz and 880 Hz) are saved at the same time, and an FFT of each clip shows only its own tone. `test_04` checks that each session's chat contains only its own tag.

## Network: why previews don't download anything twice

- Each source is downloaded **exactly once**, by Streamlink, into FFmpeg's stdin.
- FFmpeg stream-copies (`-c copy`, no transcode, no downscale) into 2–5 s MPEG-TS segments. These files are the recording.
- The dashboard preview is an HLS playlist the backend generates on the fly from the segment index (`/media/live/<session>/index.m3u8`). It points at **those same files**, served from localhost. The browser never contacts Twitch, YouTube or Kick.
- The cost is a preview delay of about 2–3 segments. The UI shows that delay (`−6.5s`), and Save and Mark subtract it so you clip what you actually saw.
- `test_02` blocks every outbound socket while it fetches the playlist and segments, and the preview still works.
- **Quality and bandwidth.** `BandwidthManager` budgets capture at 80% of 30 Mbps = 24 Mbps.
  - Priority-1 streams always ask for `1080p60,1080p,…,best`.
  - Other streams drop to `720p60,…` when the sum of measured bitrates plus the new stream's estimate would exceed the budget.
  - Measured bitrate is segment bytes ÷ duration, not the platform's label.
  - The resolution and frame rate shown come from ffprobe of the captured segment, so 720p is never reported as 1080p.

## Rolling buffer & protection

1. FFmpeg's segment muxer writes a CSV line only when a segment is **complete**. The worker tails it and indexes `(session, seq, run, path, t_start, t_end, size)` in SQLite.
2. Time is **session media time** (seconds since the session started), which stays continuous across reconnects. Each reconnect is a new `run` that starts at the wall-clock offset.
3. Protection:
   - `protect()` inserts a range under the retention lock.
   - Every hype trigger immediately protects [event − pre, event + post + max wait].
   - Manual saves and marks protect their range too.
4. A sweep runs every 2 s under the same lock. For each active session it deletes indexed segments that end more than `retention_seconds` before the newest one and don't overlap an unreleased protection. Ended sessions are cleared entirely, except for protected ranges.
5. **Byte budget** (default 5 GB): if the buffer is over budget, the oldest unprotected segments go first. The newest 3 per stream are kept for the preview. A warning is raised if protected footage alone exceeds the budget.
6. After a clip file is extracted and verified (ffprobe plus a 1 s decode), its protection is released.
7. **Recovery.** Orphaned `.ts` files (crash leftovers that aren't indexed, older than 60 s and not the current segment) are deleted. On startup:
   - sessions that were live are closed
   - rows whose files have vanished are marked deleted
   - protections of finished clips are released
   - jobs that were `running` are re-queued (handlers are idempotent)
8. **Disk pressure.** Below 10 GB free there is a warning. Below 3 GB the lowest-priority stream stops and new captures are refused.

## Detection layers

- **Layer A (continuous, cheap).**
  - Per-streamer robust baselines (median/MAD of 1 s loudness over 5 min, and of chat rate over 10 min), so a naturally loud streamer or a busy chat must exceed *its own* normal level.
  - Audio adds a bonus for silence followed by a loud reaction.
  - Chat combines the rate z-score, keywords, laugh tokens, emote density and repetition.
  - The chat signal is matched against audio from up to `chat_reaction_delay_s` earlier.
  - Combined score = w_a·audio + w_c·chat + 0.15·min(audio, chat). Audio-only mode (no chat) is slightly stricter.
- **State machine.** Each candidate goes through trigger → extend (merge window) → finalize when the reaction cools for 4 s or the max wait is reached. Triggers within the cooldown are suppressed, so one scream produces one clip.
- **Layer B (per candidate).**
  1. Extract the clip and transcribe it with faster-whisper (Silero VAD, word timestamps, hallucination filter).
  2. Compute transcript features: energy, reaction phrases, exclamations, profanity, conflict and win/fail words.
  3. Measure laughter from the transcript and from chat.
  4. Measure novelty against the same streamer's recent clips.
  5. Take a weighted mean over the signals that are *available*, using the weights from Settings.
  6. Auto clips below `save_threshold` are discarded. Manual saves never are.
- **Layer C (optional).** Ollama on localhost produces a JSON score, category, reason, boundaries and titles, which are validated with Pydantic. Invalid or slow output is ignored.
- **Layer D (optional).** OpenCV face detection on 12 sampled frames proposes a facecam rectangle, and only when the face is stationary. Full-resolution feeds are never analyzed continuously.

## Resource policy

- **Capture processes** run at `ABOVE_NORMAL` priority and **renders** at `BELOW_NORMAL`.
- One Whisper model is loaded lazily and shared: about 1 GB of VRAM for `small` in float16. CUDA is tried first, with a fallback to CPU int8 and the reason reported.
- One render runs at a time, using NVENC. Renders pause while the GPU is above 86 °C and resume below 80 °C.
- `nvidia-smi` is polled every 3 s for utilization, VRAM, temperature and encoder use.
- Capture never depends on Whisper, Ollama, chat or the browser. They run in separate processes, threads or tasks, and their failures are caught and reported.

## Security

- The server binds to `127.0.0.1`. Binding elsewhere is refused unless `LIVECLIP_I_UNDERSTAND_LAN=1`.
- Every request must carry a localhost `Host` header, which blocks DNS rebinding. Requests that change state must come from a localhost `Origin`, which blocks cross-site requests.
- Stream URLs must be https on the Twitch, YouTube or Kick hosts:
  - no credentials, ports, `file:`, LAN or IP targets
  - `local:` fixtures only when explicitly enabled, and confined to the fixtures directory
- Every subprocess call uses an argument list, never a shell string.
- Media endpoints serve only files that are both indexed in the database and inside the data directory.
- Destructive endpoints require `confirm`.
- No secrets live in code. Keys come from `.env`, and the YouTube OAuth token is stored under `data/database`.

## Platform notes

| Platform | Capture | Chat | Notes |
|---|---|---|---|
| Twitch | Streamlink (ads skipped, low latency) | Anonymous IRC over WebSocket (documented, read-only) | Works without credentials |
| YouTube Live | Streamlink | Data API v3 `liveChatMessages` (needs `YOUTUBE_API_KEY`; 10k units/day ≈ 2–3 h at the server-suggested interval) | No DVR/replay dependency |
| Kick | Streamlink `kick` plugin (Cloudflare may block) | Experimental public Pusher socket, off by default | Capture works without chat |
| Local | `ffmpeg -re -stream_loop` | JSONL fixture | Tests/demos |

## Publishing

- **Local export** always works.
- **YouTube:** the OAuth installed-app (loopback) flow plus a resumable upload. Success is reported only when YouTube returns a video id.
- **Instagram:** the Graph API needs a Business or Creator account and a *public* video URL, which a local-only app can't provide, so the UI says to upload manually.
