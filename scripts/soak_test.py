"""Multi-hour soak test: proves bounded storage growth with four streams.

Uses the four local fixtures by default (no internet). With --real you can
pass real URLs to also measure network usage.

    backend\\.venv\\Scripts\\python.exe scripts\\soak_test.py --hours 3
    backend\\.venv\\Scripts\\python.exe scripts\\soak_test.py --hours 2 --real https://www.twitch.tv/a https://www.twitch.tv/b

Every minute it records: temp-buffer bytes, number of segment files on disk,
protected segments, DB size, process RSS, interface download Mbps and the
number of ffmpeg/streamlink processes, to data/soak/soak_<ts>.csv. At the end it
fails if unprotected buffer storage or file counts grew beyond the bound, or
RSS grew more than 300 MB.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import psutil  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from liveclip.app import create_app  # noqa: E402
from liveclip.context import AppContext  # noqa: E402
from liveclip.fixtures import generate  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=2.0)
    ap.add_argument("--real", nargs="*", default=None, help="real stream URLs instead of fixtures")
    ap.add_argument("--save-every", type=float, default=600, help="manual 30s save every N seconds (exercises protection)")
    a = ap.parse_args()

    data = ROOT / "data" / "soak"
    fixtures = ROOT / "fixtures" / "media"
    if a.real is None:
        generate(fixtures, duration=120, burst=70)
    ctx = AppContext(data_dir=data, fixtures_dir=fixtures, config_overrides={"allow_local_sources": True})
    app = create_app(ctx, serve_frontend=False)
    out = data / f"soak_{int(time.time())}.csv"
    proc = psutil.Process()
    with TestClient(app) as c, open(out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["elapsed_s", "temp_bytes", "segment_files", "protected_segments", "db_bytes", "rss_mb", "net_mbps",
                    "ffmpeg_procs", "streamlink_procs", "clips"])
        urls = a.real or [f"local:fixture_{x}" for x in "abcd"]
        ids = []
        for i, u in enumerate(urls[:4]):
            ids.append(c.post("/api/streams", json={"name": f"Soak {i + 1}", "url": u, "start": True}).json()["id"])
        t0 = time.time()
        last_save = t0
        net0 = psutil.net_io_counters().bytes_recv
        rss0 = None
        rows = []
        while time.time() - t0 < a.hours * 3600:
            time.sleep(60)
            el = time.time() - t0
            if time.time() - last_save > a.save_every:
                for sid in ids:
                    c.post(f"/api/streams/{sid}/save", json={"seconds": 30})
                last_save = time.time()
            disk = ctx.retention.disk_status()
            files = sum(1 for _ in ctx.paths.recordings.rglob("seg_*.ts"))
            prot = sum(ctx.retention.buffer_status(l.session_id)["protected_segments"] for l in ctx.manager.sessions.values())
            net1 = psutil.net_io_counters().bytes_recv
            mbps = (net1 - net0) * 8 / 60 / 1e6
            net0 = net1
            kids = proc.children(recursive=True)
            rss = proc.memory_info().rss / 1e6
            rss0 = rss0 or rss
            row = [round(el), disk["temp_bytes"], files, prot, ctx.paths.db_file.stat().st_size, round(rss, 1), round(mbps, 2),
                   sum(1 for k in kids if "ffmpeg" in k.name().lower()), sum(1 for k in kids if "streamlink" in k.name().lower()),
                   ctx.db.scalar("SELECT COUNT(*) FROM clips")]
            rows.append(row)
            w.writerow(row)
            fh.flush()
            print(row, flush=True)
        c.post("/api/streams/stop-all")

    # bound: retention window worth of segments per stream (+ protected + in-progress)
    seg = ctx.config.buffer.segment_seconds
    bound = len(ids) * (ctx.config.buffer.retention_seconds / seg + 3)
    late = rows[len(rows) // 2:]
    worst_files = max(r[2] - r[3] for r in late)
    rss_growth = rows[-1][5] - rows[0][5]
    print(f"\nCSV: {out}\nmax unprotected segment files (2nd half): {worst_files} (bound {bound:.0f})\nRSS growth: {rss_growth:.0f} MB")
    ok = worst_files <= bound and rss_growth < 300 and all(r[1] <= ctx.config.buffer.max_temp_bytes for r in rows)
    print("SOAK PASS" if ok else "SOAK FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
