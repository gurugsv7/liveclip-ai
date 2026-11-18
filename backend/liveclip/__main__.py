"""Entry point: `python -m liveclip [--host 127.0.0.1] [--port 8765]`."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def load_env_file(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE, # comments). Existing env vars win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v and k not in os.environ:
            os.environ[k] = v


def main() -> None:
    load_env_file(Path(__file__).resolve().parents[2] / ".env")
    ap = argparse.ArgumentParser(prog="liveclip")
    ap.add_argument("--host", default=os.environ.get("LIVECLIP_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("LIVECLIP_PORT", "8765")))
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args()
    if args.host not in ("127.0.0.1", "localhost", "::1") and os.environ.get("LIVECLIP_I_UNDERSTAND_LAN") != "1":
        print("Refusing to bind to a non-localhost address: the control API can delete files and has no auth.\n"
              "Set LIVECLIP_I_UNDERSTAND_LAN=1 to override.", file=sys.stderr)
        sys.exit(2)
    if os.name == "nt":
        import asyncio
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    import uvicorn

    pid_file = Path(os.environ.get("LIVECLIP_DATA_DIR") or Path(__file__).resolve().parents[2] / "data") / "liveclip.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    # native crashes (CUDA/onnxruntime/ctranslate2) leave no Python traceback: record them
    import faulthandler
    (pid_file.parent / "logs").mkdir(parents=True, exist_ok=True)
    _crash = open(pid_file.parent / "logs" / "crash.log", "a", buffering=1, encoding="utf-8")
    faulthandler.enable(file=_crash, all_threads=True)
    pid_file.write_text(str(os.getpid()))
    try:
        uvicorn.run("liveclip.app:create_app", factory=True, host=args.host, port=args.port, reload=args.reload,
                    log_level="info", ws_ping_interval=20)
    finally:
        pid_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
