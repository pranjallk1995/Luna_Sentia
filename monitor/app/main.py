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
from app.mcp_server import create_mcp
from app.proxy import OllamaProxy
from app.residency import Residency
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


def app_models_entry(app: str, reply: dict | None, problem: str | None) -> dict:
    """One app's own models as the page shows them, or why they could not be read."""
    if reply is None:
        return {"app": app, "problem": problem, "models": [], "gpu": None}
    return {"app": app, "problem": None, "models": list(reply.get("models") or []), "gpu": reply.get("gpu")}


async def ask_app(client: httpx.AsyncClient, app: str, url: str, token: str) -> dict:
    if not token:
        return app_models_entry(app, None, f"LUNA{app.upper()}_API_TOKEN is not set in Luna_Sentia/.env")
    try:
        reply = await client.get(
            f"{url}/api/models",
            headers={"Authorization": f"Bearer {token}", "X-Luna-User": MonitorConfig.OWNER_ID},
        )
    except httpx.HTTPError:
        return app_models_entry(app, None, "not running")
    if reply.status_code == 401:
        return app_models_entry(app, None, f"refused: LUNA{app.upper()}_API_TOKEN differs from the app's")
    if reply.status_code == 404:
        return app_models_entry(app, None, "this version of the app does not report its models")
    try:
        reply.raise_for_status()
        return app_models_entry(app, reply.json(), None)
    except (httpx.HTTPError, ValueError):
        return app_models_entry(app, None, f"answered HTTP {reply.status_code}")


def remember_sizes(entries: list[dict], seen: dict[tuple[str, str], float]) -> list[dict]:
    """Give each app model the VRAM it was last seen to use, so an unloaded one still shows a size.

    Socia measures each model itself (``vram_mb``). Visia, Lexia and Vibia run one model in a
    process that reports only its whole share of the GPU, and only while loaded: that share is the
    model's size. ``seen`` keeps these for as long as the monitor runs.
    """
    for entry in entries:
        models = entry.get("models") or []
        held = (entry.get("gpu") or {}).get("reserved_mb")
        for model in models:
            key = (entry["app"], model.get("name", ""))
            if model.get("vram_mb"):
                seen[key] = model["vram_mb"]
            elif model.get("loaded") and held and len(models) == 1 and not (entry.get("gpu") or {}).get("measured"):
                seen[key] = held
            if not model.get("vram_mb") and key in seen:
                model["vram_mb"], model["vram_last_seen"] = seen[key], True
    return entries


async def watch_app_models(live: LiveState, config: MonitorConfig) -> None:
    """Ask Visia, Lexia, Vibia, and Socia about the models they run themselves, outside Ollama."""
    seen: dict[tuple[str, str], float] = {}
    async with httpx.AsyncClient(timeout=5.0) as client:
        while True:
            live.app_models = remember_sizes(list(await asyncio.gather(
                *(ask_app(client, app, url, token) for app, url, token in config.app_tokens)
            )), seen)
            await asyncio.sleep(MonitorConfig.APP_MODELS_SECONDS)


def installed_models(reply: dict) -> list[dict]:
    """Ollama's /api/tags: every model it has, loaded or not."""
    return [{
        "name": model.get("name") or model.get("model"),
        "size_mb": (model.get("size") or 0) / MB,
        "quantization": (model.get("details") or {}).get("quantization_level"),
        "parameters": (model.get("details") or {}).get("parameter_size"),
    } for model in reply.get("models") or []]


async def imagegen_models(client: httpx.AsyncClient, url: str) -> dict:
    """The image service's weights on disk, and whether it is making a picture now: each picture's two
    processes hold the GPU only while they run, so it holds nothing between pictures."""
    try:
        status = (await client.get(f"{url}/status")).json()
    except (httpx.HTTPError, ValueError):
        return {"problem": "not running", "files": []}
    problem = status.get("problem") or (None if status.get("ready") else "preparing its weights (first start)")
    running = status.get("running") or {}
    return {"problem": problem, "files": status.get("files") or [], "drawing": bool(running),
            "stage": running.get("status"), "vram_mb": running.get("vram_mb") or 0,
            "preparing": bool(status.get("preparing"))}


async def watch_inventory(live: LiveState, config: MonitorConfig, store: MonitorStore) -> None:
    """Every model there is, loaded or not, for the page's list of models not loaded now."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        while True:
            inventory: dict = {}
            try:
                reply = await client.get(f"{config.upstream_url}/api/tags")
                reply.raise_for_status()
                inventory["ollama"] = installed_models(reply.json())
            except (httpx.HTTPError, ValueError):
                inventory["ollama"] = []
            if config.imagegen_url:
                inventory["images"] = await imagegen_models(client, config.imagegen_url.rstrip("/"))
            try:
                inventory["usage"] = await asyncio.to_thread(store.model_usage)
            except Exception as error:  # noqa: BLE001 - the list goes without who called what
                logger.warning("Could not read which app called which model: %s", error)
                inventory["usage"] = (live.inventory or {}).get("usage", [])
            live.inventory = inventory
            await asyncio.sleep(MonitorConfig.INVENTORY_SECONDS)


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
    residency = Residency(live, config.upstream_url, config.resident_model, config.resident_context,
                          config.guest_keep_alive, config.resident_idle_seconds, config.imagegen_url)
    proxy = OllamaProxy(live, config.upstream_url, residency=residency)
    page = create_app(config, live, store)
    servers = [
        uvicorn.Server(uvicorn.Config(proxy, host="0.0.0.0", port=MonitorConfig.PROXY_PORT, timeout_keep_alive=75,
                                      log_level="warning", access_log=False)),
        uvicorn.Server(uvicorn.Config(page, host="0.0.0.0", port=MonitorConfig.UI_PORT, proxy_headers=True,
                                      forwarded_allow_ips="*", log_level="info")),
    ]
    servers.append(uvicorn.Server(uvicorn.Config(
        create_mcp(config, live, store, residency).streamable_http_app(), host="0.0.0.0", port=MonitorConfig.MCP_PORT,
        proxy_headers=True, forwarded_allow_ips="*", log_level="warning")))
    tasks = [asyncio.create_task(job) for job in (
        sample_gpu(live, GpuReader()), watch_models(live, config.upstream_url), watch_app_models(live, config),
        watch_inventory(live, config, store),
        flush(live, store, config), residency.run())]
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
