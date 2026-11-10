"""Which model lives on the GPU: one resident model stays, every other model visits.

The GPU (12 GB) holds Luna Socia's chat model or the apps' working models, not both.
The resident model (``SENTIA_RESIDENT_MODEL``) is asked to stay loaded for good; any
other model gets a short stay (``SENTIA_GUEST_KEEP_ALIVE``), whatever the app asked, so a Visia batch's vision
model leaves soon after the batch ends. Once nothing has used a model for a while, the
GPU has room again, and ComfyUI is not drawing a picture, the resident model is loaded
back, so Luna answers at once instead of after a reload.

Requests that ask for a model to leave (``keep_alive`` 0, as Socia sends before a
picture) are passed on unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

from app.calls import MODEL_ENDPOINTS
from app.live import LiveState

logger = logging.getLogger(__name__)
CHECK_SECONDS = 5.0
# Room left free beside the resident model, for the desktop and the CUDA contexts of
# processes that come and go.
MARGIN_MB = 300.0


def leaving_now(value: Any) -> bool:
    return value in (0, "0", "0s", "0m")


class Residency:
    def __init__(self, live: LiveState, upstream_url: str, resident: str, resident_context: int,
                 guest_keep_alive: str, idle_seconds: float, comfyui_url: str = "",
                 transport: httpx.AsyncBaseTransport | None = None, clock=time.monotonic) -> None:
        self.live = live
        self.resident = resident
        self.resident_context = resident_context
        self.guest_keep_alive = guest_keep_alive
        self.idle_seconds = idle_seconds
        self.comfyui_url = comfyui_url.rstrip("/")
        self.client = httpx.AsyncClient(base_url=upstream_url, transport=transport, timeout=httpx.Timeout(600.0, connect=10.0))
        self.clock = clock
        self.last_activity = clock()
        # The resident model's VRAM when last loaded, to know whether it fits again.
        self.resident_mb: float | None = None
        self.state = "idle"

    @staticmethod
    def same_model(a: str | None, b: str | None) -> bool:
        def full(name: str | None) -> str:
            name = (name or "").strip()
            return name if ":" in name else f"{name}:latest"
        return full(a) == full(b)

    def rewrite(self, path: str, body: bytes) -> bytes:
        """The request with the stay its model gets; anything unreadable passes unchanged."""
        self.last_activity = self.clock()
        if not self.resident or path not in MODEL_ENDPOINTS:
            return body
        try:
            request = json.loads(body)
        except ValueError:
            return body
        if not isinstance(request, dict) or not request.get("model"):
            return body
        asked = request.get("keep_alive")
        if leaving_now(asked):
            return body
        request["keep_alive"] = -1 if self.same_model(request["model"], self.resident) else self.guest_keep_alive
        return json.dumps(request).encode()

    def touched(self) -> None:
        self.last_activity = self.clock()

    # Bringing the resident model back

    def resident_loaded(self) -> bool:
        for model in self.live.models:
            if self.same_model(model.get("name"), self.resident):
                self.resident_mb = model.get("vram_mb") or self.resident_mb
                return True
        return False

    def free_vram_mb(self) -> float | None:
        sample = self.live.samples[-1] if self.live.samples else None
        if not sample or sample.get("vram_total_mb") is None or sample.get("vram_used_mb") is None:
            return None
        return sample["vram_total_mb"] - sample["vram_used_mb"]

    async def comfyui_busy(self) -> bool:
        if not self.comfyui_url:
            return False
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                queue = (await client.get(f"{self.comfyui_url}/queue")).json()
            return bool(queue.get("queue_running") or queue.get("queue_pending"))
        except (httpx.HTTPError, ValueError):
            return False

    async def why_not_now(self) -> str | None:
        """None when the resident model should be loaded now, else the reason to wait."""
        if not self.resident or self.resident_loaded():
            return "loaded"
        if self.live.in_flight:
            return "a model is working"
        if self.clock() - self.last_activity < self.idle_seconds:
            return "recently busy"
        # Visiting Ollama models still loaded count as room: Ollama unloads them to make
        # space, which is what should happen once the apps have gone quiet.
        free = self.free_vram_mb()
        visitors = sum(model.get("vram_mb") or 0 for model in self.live.models)
        if self.resident_mb and free is not None and free + visitors < self.resident_mb + MARGIN_MB:
            return "an app's own model still holds the GPU"
        if await self.comfyui_busy():
            return "ComfyUI is drawing"
        return None

    async def load(self) -> None:
        reply = await self.client.post("/api/generate", json={
            "model": self.resident, "keep_alive": -1, "options": {"num_ctx": self.resident_context}})
        reply.raise_for_status()

    async def run(self) -> None:
        while True:
            await asyncio.sleep(CHECK_SECONDS)
            try:
                reason = await self.why_not_now()
                if reason is None:
                    self.state = "loading"
                    logger.info("Loading the resident model %s back", self.resident)
                    await self.load()
                    self.touched()
                    reason = "loaded"
                self.state = reason
            except Exception as error:  # noqa: BLE001 - try again on the next check
                self.state = f"could not load: {type(error).__name__}"
                logger.warning("Could not load the resident model: %s", error)
