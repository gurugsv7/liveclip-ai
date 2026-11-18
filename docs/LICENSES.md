# Third-party software

LiveClip AI's own code has no license file yet. Add one before you distribute it.

| Component | Used for | License | Notes |
|---|---|---|---|
| FFmpeg (gyan.dev "essentials" build) | capture, segmenting, extraction, rendering | **GPL-3.0** build (includes libx264, libass) | Runs as a separate executable and isn't linked. If you redistribute it, ship its license and source offer. |
| Streamlink | stream capture | BSD-2-Clause | |
| faster-whisper / CTranslate2 | transcription | MIT | |
| OpenAI Whisper model weights | speech model | MIT | Downloaded on first use |
| Silero VAD (bundled in faster-whisper) | voice activity | MIT | |
| NVIDIA cuBLAS / cuDNN wheels | GPU inference | NVIDIA proprietary EULA (free) | Optional (`-NoCuda`) |
| FastAPI, Starlette, Uvicorn, Pydantic, httpx | backend | MIT / BSD-3-Clause | |
| websockets | chat clients | BSD-3-Clause | |
| NumPy, psutil | analysis, monitoring | BSD-3-Clause | |
| OpenCV (headless) | optional facecam detection | Apache-2.0 | |
| React, Vite, Zustand, hls.js, lucide-react, Radix UI, Tailwind CSS, clsx, tailwind-merge, class-variance-authority | frontend | MIT (lucide: ISC) | |
| Inter, JetBrains Mono, Space Grotesk (Google Fonts) | UI typography | SIL OFL 1.1 | Loaded from Google Fonts. Falls back to system fonts when offline. |
| Ollama + chosen LLM | optional semantic scoring | Ollama MIT; model license varies (e.g. Llama 3.2 Community License) | Disabled by default |
| uv (used only by setup.ps1 to install Python if missing) | installer | MIT / Apache-2.0 | |

Nothing here is a paid service, and nothing is required at runtime beyond FFmpeg and Streamlink.

**Content rights:** clipping, editing or uploading someone else's stream may need the creator's permission and has to follow the platform's terms. A clip is not automatically eligible for monetization. Settings → Streams has a per-streamer "Permissions / usage notes" field for keeping track of this.
