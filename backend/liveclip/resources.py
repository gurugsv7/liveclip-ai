"""System resource monitor: CPU, RAM, network download rate, NVIDIA GPU
(via nvidia-smi, no extra dependency), storage, and protective policies
(GPU thermal hold for renders, disk-pressure handling)."""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import time

import psutil

from .media import popen_flags

log = logging.getLogger("liveclip.resources")


class ResourceMonitor:
    def __init__(self, ctx):
        self.ctx = ctx
        self.snapshot: dict = {}
        self._last_net = None
        self._nvsmi = shutil.which("nvidia-smi")
        self._task: asyncio.Task | None = None
        self.gpu_hot = False

    def start(self) -> None:
        psutil.cpu_percent(None)
        self._task = asyncio.create_task(self._loop(), name="resources")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def _gpu(self) -> dict | None:
        if not self._nvsmi:
            return None
        try:
            p = subprocess.run([self._nvsmi, "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,"
                                "utilization.encoder,utilization.decoder", "--format=csv,noheader,nounits"],
                               capture_output=True, timeout=5, **popen_flags())
            name, util, used, total, temp, enc, dec = [x.strip() for x in p.stdout.decode().splitlines()[0].split(",")]
            return {"name": name, "util": float(util), "vram_used_mb": float(used), "vram_total_mb": float(total),
                    "temp_c": float(temp), "enc": float(enc) if enc not in ("N/A", "[N/A]") else None,
                    "dec": float(dec) if dec not in ("N/A", "[N/A]") else None}
        except Exception:
            return None

    def _sample(self) -> dict:
        now = time.time()
        net = psutil.net_io_counters()
        rx_mbps = None
        if self._last_net:
            dt = now - self._last_net[0]
            if dt > 0:
                rx_mbps = (net.bytes_recv - self._last_net[1]) * 8 / dt / 1e6
        self._last_net = (now, net.bytes_recv)
        vm = psutil.virtual_memory()
        proc = psutil.Process()
        gpu = self._gpu()
        disk = self.ctx.retention.disk_status()
        return {
            "cpu": psutil.cpu_percent(None), "ram_used": vm.used, "ram_total": vm.total, "ram_percent": vm.percent,
            "app_rss": proc.memory_info().rss, "net_rx_mbps": round(rx_mbps, 2) if rx_mbps is not None else None,
            "gpu": gpu, "disk": disk, "bandwidth": self.ctx.manager.bandwidth.status(),
            "jobs_running": dict(self.ctx.jobs.running), "ts": now,
        }

    def thermal_hold(self) -> bool:
        return self.gpu_hot

    async def _loop(self) -> None:
        while True:
            try:
                s = await asyncio.to_thread(self._sample)
                self.snapshot = s
                gpu = s.get("gpu")
                if gpu:
                    if gpu["temp_c"] >= 86:
                        self.gpu_hot = True
                        self.ctx.hub.warn("gpu_temp", "warn", f"GPU at {gpu['temp_c']:.0f}°C: exports paused until it cools below 80°C")
                    elif gpu["temp_c"] < 80 and self.gpu_hot:
                        self.gpu_hot = False
                        self.ctx.hub.clear_warning("gpu_temp")
                    if gpu["vram_total_mb"] and gpu["vram_used_mb"] / gpu["vram_total_mb"] > 0.95:
                        self.ctx.hub.warn("vram", "warn", "GPU memory nearly full; consider a smaller Whisper model")
                    else:
                        self.ctx.hub.clear_warning("vram")
                if s["cpu"] > 95:
                    self.ctx.hub.warn("cpu", "warn", "CPU saturated; capture may stall. Reduce streams or close other apps.")
                else:
                    self.ctx.hub.clear_warning("cpu")
                await self._disk_policy(s["disk"])
                self.ctx.hub.publish("resources", s)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("resource sample failed")
            await asyncio.sleep(3)

    async def _disk_policy(self, disk: dict) -> None:
        if disk["level"] == "critical":
            self.ctx.hub.warn("disk", "error", f"Only {disk['free'] / 1e9:.1f} GB free: stopping the lowest-priority stream")
            mgr = self.ctx.manager
            if len(mgr.sessions) > 0:
                worst = max(mgr.sessions.values(), key=lambda l: (l.streamer["priority"], l.streamer["slot"]))
                await mgr.stop(worst.streamer_id)
        elif disk["level"] == "warn":
            self.ctx.hub.warn("disk", "warn", f"Low disk space: {disk['free'] / 1e9:.1f} GB free")
        else:
            self.ctx.hub.clear_warning("disk")
