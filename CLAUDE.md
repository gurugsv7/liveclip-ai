# LiveClip AI — project rules

Local-first multi-stream clipping app: FastAPI backend in `backend/liveclip`, React UI in `frontend/`.
Setup/run/test: `scripts\setup.ps1`, `scripts\start.ps1`, `scripts\stop.ps1`, `scripts\test.ps1`.

## Reel rules (from the user — always apply)
- **Reels are 30 seconds max, best moment only.** People swipe away from 1-minute clips.
  Source clips may be longer (context for editing), but every exported reel is ≤ 30 s
  (`render.max_reel_s`, default 30). Aim for 15–30 s.
- Cut like an editor: the strongest reaction stretch (incl. the hype peak), optionally a
  short setup line spliced in front ("I already lost… GG" → the comeback), dead air removed
  as jump cuts, and end right after the payoff line — never trail into filler.
  Implemented in `backend/liveclip/editing/highlight.py` (`best_reel_cut`).
- **No title/hook header** on reels or covers (user: the box over the first seconds is irritating).
  `render.hook_text_enabled` defaults to off; the clip title is still used for the post caption and filenames.
- Word-timed captions from real Whisper timestamps, 1–3 words, one accent colour, inside
  the 9:16 safe area. With the facecam layout, put captions on the seam between face and
  gameplay so they don't cover the action.
- Prefer the facecam layout (face on top, gameplay pinned below) when the stream has a
  facecam; pin the gameplay region (e.g. a chess board) instead of chasing the streamer's head.
- **The user's reel window wins**: once they press "Use this window" (`layout_params.manual_window`),
  export renders exactly that ≤30 s stretch — no automatic cut. Without one, `best_reel_cut` picks.
- Facecam layout is automatic when a facecam is known (`effective_layout`), unless the user picked
  a layout for that clip (`layout_params.layout_locked`).
- Tamil streamers (`streamers.language = ta`): Whisper in Tamil, words romanized to casual
  Tanglish (`ai/tanglish.py`, same timings) unless `caption_mode = native` (Tamil script, Nirmala UI).
  Bigger model only if already downloaded — never download 3 GB mid-capture.
- **Every export (and Approve, which exports) ships a post kit** next to the MP4
  (`editing/cover.py`): `.cover.jpg` and `.caption.txt`.
  - Cover = a real frame, never generated. With a facecam: the streamer's own face (inside the
    facecam rect, sharp, lit, nearest the peak) in the facecam layout, no text on it. Faces elsewhere (YouTube thumbnails, people in a watched video) don't count. No facecam:
    the sharpest well-lit frame near the peak with no half-shown caption word.
  - Caption = hook line + ≤1 emoji, one more line the streamer really said, a credit line
    (`🎥 <streamer> live on <Platform>: <url>`, it's their footage), 3–5 lowercase hashtags.

## Detection rules (don't miss moments, don't fill the disk)
Signals, all relative to *that* streamer's own normal and all local/free:
1. **Viewer clips** (Twitch Helix `GET /clips`, polled every 15 s): a viewer clipping = confirmed
   moment → always saved, never discarded. Needs TWITCH_CLIENT_ID/SECRET. Strongest signal.
2. **Crowd "clip it"**: ≥3 distinct chatters typing clip it / !clip within 15 s → chat forced hot.
3. **Chat** (LIGHTOR-style): message rate vs baseline + keywords, laugh tokens, emotes,
   copy-paste repeats and *short-message ratio*; chat lags the moment by ~3 s.
4. **Sound events** (YAMNet ONNX on CPU, every 1 s): laughter / screams / shouts / cheering count
   as hype regardless of volume; when the loudness is just game noise (gunfire, explosions,
   engines, music) and nobody reacts, loudness is discounted (GTA-style games).
5. **Loudness** vs the streamer's own baseline, silence excluded.
6. **Live speech** (Whisper every 6 s): excited phrases, normalized per streamer.
Any single very strong signal can trigger; weak partners are "no evidence", not negative.
**Loudness only counts when a human is voicing** (YAMNet voice ≥ 0.35): game audio (gunfire,
footsteps, R6 breaching) is never "excitement". Applause/cheering are NOT reactions in games
(real case: R6 sound tagged "Applause 0.80" while the streamer sat silent).
**Loudness must be sustained**: full credit needs ≥ 0.8 s of clearly-loud audio within ±2 s of
the peak (`SUSTAIN_FRAMES`). A lone bang/thud/mic bump is not a reaction (real case: xqc watching
a bodycam video, one 0.5 s thud scored "audio 0.90" → a clip with nothing in it).
**Limited/compressed mics** (≥ 30% of voiced seconds peak at ~0 dBFS, e.g. Kai Cenat: whole range
~7 dB) are scored against the streamer's own range — reaching their loudest ~0.5% = a reaction —
because they can never get "+5–15 dB over normal". Dynamic streamers keep the normal rule.
**A replaced stream keeps its old slot's settings** (language, facecam): after Replace, check them
(real case: Kai Cenat in groot's slot was transcribed as Tamil).
**A busy chat that stays quiet is evidence against**: if chat normally runs ≥ 1 msg/s and did not
rise (z ≤ 0.5, also checked over the whole clip since chat lags), it is NOT dropped as
"no evidence" — the clip goes to Maybe. Slow chats (< 1 msg/s) still count as no evidence.
Reaction/watch-party content: voices and sounds in the watched video are not the streamer;
trust chat more than loudness there.
**Sustained builds trigger too** (`detection/hype.py` `_sustained`): combined score ≥ 0.30 for
80% of 10 s *and* chat z ≥ 1.5 for half of it. A long rant/yell with an engaged chat never
spikes past 0.62 (real xqc miss at 8:37–8:51: +9 dB for 15 s, chat 15 msg/s). Chat is required,
so a long loud stretch of a game or watched video alone can't trigger it.

### Reel cutting (editing/highlight.py)
- Use both timelines: Whisper words *and* YAMNet voice. Raging/yelling is often garbled, so
  Whisper returns no words — a voiced stretch without words is a reaction, never dead air.
- Pick one contiguous moment; to fit 30 s remove at most ONE dull block from its middle
  (single jump cut) plus an optional short setup line. No choppy micro-cuts, no scraps < 1.5 s.
- End on the payoff line (incl. self-blame like "why did I…"), never on trailing filler.
- Purely visual moments (silent deaths, glitches) can still be missed by audio — the Gemini
  bridge (`scripts/gemini_edit.py`) watches the video and is the fallback for those.
Storage stays bounded: rolling buffer 180 s/stream; near-miss AI clips (score 0.25–0.50) go to a
**Maybe** pool capped at 3 GB (lowest scores evicted first); below 0.25 is deleted.
Manual saves, marks and viewer clips are never auto-deleted.
When tuning, replay real recorded session data and add a regression test from it.

## VODs / past broadcasts (`backend/liveclip/vod/`, VODs page)
- Paste a Twitch / YouTube / Kick VOD link or a local file path. **Audio only** is downloaded
  (Twitch `Audio_Only`: 9.5 h ≈ 0.9 GB vs 30 GB source), the *same* live detectors run over it on the
  recording's clock (`vod/analyzer.py`, ~25-30× realtime with Whisper), then **only the moments' video**
  is fetched (`yt-dlp --download-sections`, ≤ `vod.max_height`) or cut from the local file.
- Moments become normal `auto` clips in their own `kind='vod'` session → extract/transcribe/score →
  Review / Maybe. Never touch the user's own local file; VOD section files are deleted once extracted.
- While streams are captured, yt-dlp is rate-limited to a share of the spare bandwidth (capture first).
- Per-streamer "after each stream" (`streamers.vod_after_stream`): when a monitored stream goes offline,
  its past broadcast is found and analysed; moments already caught live (hype events) are skipped.
- Twitch/Kick VODs have no chat replay (no private APIs); YouTube live replays include chat (yt-dlp `live_chat`).

## Engineering rules
- Never break capture: recording must keep running if AI, chat, rendering or the UI fails.
- Never mix streams: every row/file/job carries its own `streamer_id` + `session_id`.
- Real data only — no fabricated chat, transcripts, clips or stats.
- Run `scripts\test.ps1` (or `pytest` in `backend`, `npx vitest run` in `frontend`) after changes;
  add a regression test built from real session data when fixing detection behaviour.
- FFmpeg 9: use `-/filter_complex file`, not `-filter_complex_script`.
