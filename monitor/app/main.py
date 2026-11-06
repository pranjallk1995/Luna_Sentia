"""Run the Ollama proxy and the monitor's page side by side, with the loops that measure and store.

The proxy must keep working whatever happens to the rest: a GPU without counters,
an unreachable database, or a failed measurement never stops a model call.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import httpx
import uvicorn

from app.config import MonitorConfig
from app.gpu import GpuReader
from app.live import LiveState
from app.proxy import OllamaProxy
from app.store import MonitorStore
from app.web import create_app

logger = logging.getLogger("monitor")
MB = 1024 * 1024


def loaded_models(reply: dict) -> list[dict]:
    """Ollama's /api/ps as the page shows it."""
    models = []
    for model in reply.get("models") or []:
        expires = model.get("expires_at")
        try:
            expires_at = datetime.fromisoformat(expires).timestamp() if expires else None
        except ValueError:
            expires_at = None
        models.append({
            "name": model.get("name") or model.get("model"),
            "vram_mb": (model.get("size_vram") or 0) / MB,
            "size_mb": (model.get("size") or 0) / MB,
            "context": model.get("context_length"),
            "expires_at": expires_at,
            "quantization": (model.get("details") or {}).get("quantization_level"),
            "parameters": (model.get("details") or {}).get("parameter_size"),
        })
    return models


async def sample_gpu(live: LiveState, gpu: GpuReader) -> None:
    while True:
        try:
            reading = await asyncio.to_thread(gpu.read)
            live.gpu_name, live.gpu_problem = gpu.name, gpu.problem
            live.add_sample(reading)
        except Exception:  # noqa: BLE001
            logger.exception("GPU sample failed")
        await asyncio.sleep(MonitorConfig.SAMPLE_SECONDS)


async def watch_models(live: LiveState, upstream: str) -> None:
    async with httpx.AsyncClient(base_url=upstream, timeout=5.0) as client:
        while True:
            try:
                reply = await client.get("/api/ps")
                reply.raise_for_status()
                live.set_models(loaded_models(reply.json()))
                live.engine_problem = None
            except (httpx.HTTPError, ValueError) as error:
                live.engine_problem = f"Ollama is not answering: {type(error).__name__}"
            await asyncio.sleep(MonitorConfig.MODELS_SECONDS)


async def flush(live: LiveState, store: MonitorStore, config: MonitorConfig) -> None:
    ready = False
    last_purge = 0.0
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(MonitorConfig.FLUSH_SECONDS)
        calls, samples, events = live.take_pending()
        try:
            if not ready:
                await asyncio.to_thread(store.initialize)
                ready = True
            await asyncio.to_thread(store.write, calls, samples, events)
            if loop.time() - last_purge > MonitorConfig.PURGE_SECONDS:
                await asyncio.to_thread(store.purge, config.sample_days, config.call_days)
                last_purge = loop.time()
        except Exception as error:  # noqa: BLE001 - keep the rows and try again
            logger.warning("Could not store monitor rows, will retry: %s", error)
            live.put_back(calls, samples, events)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # Polling Ollama every two seconds would otherwise log every request.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    config = MonitorConfig.from_env()
    live = LiveState(config.parallel)
    store = MonitorStore(config.database_url)
    proxy = OllamaProxy(live, config.upstream_url)
    page = create_app(config, live, store)
    servers = [
        uvicorn.Server(uvicorn.Config(proxy, host="0.0.0.0", port=MonitorConfig.PROXY_PORT, timeout_keep_alive=75,
                                      log_level="warning", access_log=False)),
        uvicorn.Server(uvicorn.Config(page, host="0.0.0.0", port=MonitorConfig.UI_PORT, proxy_headers=True,
                                      forwarded_allow_ips="*", log_level="info")),
    ]
    tasks = [asyncio.create_task(job) for job in (
        sample_gpu(live, GpuReader()), watch_models(live, config.upstream_url), flush(live, store, config))]
    try:
        await asyncio.gather(*(server.serve() for server in servers))
    finally:
        for task in tasks:
            task.cancel()
        calls, samples, events = live.take_pending()
        try:
            store.write(calls, samples, events)
        except Exception:  # noqa: BLE001
            logger.warning("Rows from the last few seconds were not stored.")


if __name__ == "__main__":
    asyncio.run(main())
