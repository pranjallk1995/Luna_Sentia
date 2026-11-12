"""The monitor's MCP server: the GPU, the models on it, and every model call, read only.

MCP clients add ``SENTIA_MCP_PUBLIC_URL``/mcp as a connector and sign in through Luna
Sentia, like the apps' servers. Like the monitor's page, it shows the whole home's
hardware, so only the owner's access token is accepted. The tools read the monitor's
live state (one-second GPU samples for the last ten minutes, calls in flight, the
models Ollama has loaded) and its lunamonitor database (five-second samples and every
call); they never change anything and never see a prompt or a reply.
"""

from __future__ import annotations

import base64
import hashlib
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anyio
import httpx
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.types import Icon
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import FileResponse, Response

from app.config import MonitorConfig
from app.insights import signals
from app.live import LiveState
from app.store import MAX_HISTORY_SECONDS, MonitorStore

TOKEN_RECHECK_SECONDS = 30
TOKEN_CACHE_ENTRIES = 64
# The live window the status averages over.
RECENT_SECONDS = 60
MAX_CALLS = 500
# Sentia's logo for MCP clients, like every Luna app's server: white on transparent, each
# file under 10 KB. Listed by public URL (claude.ai only shows icons it can load), then
# the two small ones inline for clients that cannot reach the public address.
ASSETS = Path(__file__).parent / "assets"
ICON_SIZES = (48, 96, 192, 512)
INLINE_ICON_SIZES = (48, 96)
ICON_HEADERS = {"Cache-Control": "public, max-age=86400"}

INSTRUCTIONS = """Luna Sentia runs the home's shared models on one NVIDIA GPU: Ollama's models
(through the model monitor), ComfyUI's image model, and the models Visia, Lexia, Vibia,
and Socia run themselves. These tools read the monitor, for the owner only: get_gpu_status for now,
get_gpu_history for utilisation, VRAM, power, and temperature over time, list_model_calls
for individual model calls with their timings, and get_model_usage for totals per app and
model with signals on whether a batching server such as vLLM would help. Times are UTC.
Under WSL the driver cannot split VRAM by process, so vram_used_mb counts everything on
the card; ollama_vram_mb is Ollama's own share."""


def iso(at: float | None) -> str | None:
    return datetime.fromtimestamp(at, timezone.utc).isoformat(timespec="seconds") if at is not None else None


def rounded(value: Any) -> Any:
    return round(value, 1) if isinstance(value, float) else value


def tidy(row: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: rounded(row.get(key)) for key in keys}


def mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def peak(values: list[float]) -> float | None:
    return round(max(values), 1) if values else None


def window(hours: float, until: str | None) -> tuple[float, float]:
    """The seconds since the epoch that ``hours`` back from ``until`` (or now) span."""
    if until:
        try:
            moment = datetime.fromisoformat(until.strip().replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("until must be an ISO 8601 time, like 2026-11-11T09:30:00Z.") from error
        end = (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).timestamp()
    else:
        end = time.time()
    if not hours or hours <= 0:
        raise ValueError("hours must be more than 0.")
    return end - min(hours * 3600, MAX_HISTORY_SECONDS), end


class OwnerTokenVerifier:
    """Ask Luna Sentia whose access token this is; only the owner's is accepted."""

    def __init__(self, auth_internal_url: str, resource: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.url = f"{auth_internal_url.rstrip('/')}/oauth/introspect"
        self.resource = resource.rstrip("/")
        self.client = httpx.AsyncClient(timeout=5.0, transport=transport)
        self.known: OrderedDict[str, tuple[float, AccessToken | None]] = OrderedDict()

    async def verify_token(self, token: str) -> AccessToken | None:
        key = hashlib.sha256(token.encode()).hexdigest()
        cached = self.known.get(key)
        if cached and time.monotonic() - cached[0] < TOKEN_RECHECK_SECONDS:
            access = cached[1]
            return access if access and (access.expires_at is None or access.expires_at > time.time()) else None
        try:
            response = await self.client.post(self.url, data={"token": token})
            response.raise_for_status()
            answer = response.json()
        except (httpx.HTTPError, ValueError):
            return None  # Not remembered, so access returns as soon as Sentia does.
        access = self.access_token(token, answer)
        self.known[key] = (time.monotonic(), access)
        self.known.move_to_end(key)
        while len(self.known) > TOKEN_CACHE_ENTRIES:
            self.known.popitem(last=False)
        return access

    def access_token(self, token: str, answer: dict[str, Any]) -> AccessToken | None:
        if answer.get("active") is not True or answer.get("sub") != MonitorConfig.OWNER_ID:
            return None
        # A token asked for on another Luna server is not valid here.
        resource = answer.get("resource")
        if resource and resource.rstrip("/") != self.resource:
            return None
        return AccessToken(token=token, client_id=str(answer.get("client_id", "")), scopes=[],
                           expires_at=answer.get("exp"), resource=resource, subject=answer["sub"])


class MonitorTools:
    """What the tools answer, apart from MCP, so tests can call it directly."""

    def __init__(self, config: MonitorConfig, live: LiveState, store: MonitorStore | None, residency: Any = None) -> None:
        self.config = config
        self.live = live
        self.store = store
        self.residency = residency

    async def read(self, method: str, *args: Any) -> Any:
        if self.store is None:
            raise ValueError("The monitor has no database, so it keeps no history.")
        try:
            return await anyio.to_thread.run_sync(lambda: getattr(self.store, method)(*args))
        except Exception as error:  # noqa: BLE001 - told to the caller
            raise ValueError(f"The monitor's database could not be read: {error}") from error

    def gpu_status(self) -> dict[str, Any]:
        snapshot = self.live.snapshot()
        samples = snapshot["samples"]
        latest = samples[-1] if samples else {}
        recent = [s for s in samples if s["at"] >= snapshot["now"] - RECENT_SECONDS]

        def values(key: str) -> list[float]:
            return [s[key] for s in recent if s.get(key) is not None]

        now = tidy(latest, "gpu_util", "mem_util", "vram_used_mb", "vram_total_mb", "ollama_vram_mb", "temp_c",
                   "power_w", "power_limit_w", "sm_clock_mhz", "mem_clock_mhz", "fan_pct")
        if latest.get("vram_total_mb") is not None and latest.get("vram_used_mb") is not None:
            now["vram_free_mb"] = round(latest["vram_total_mb"] - latest["vram_used_mb"], 1)
        status: dict[str, Any] = {
            "gpu": snapshot["gpu"]["name"],
            "problem": snapshot["gpu"]["problem"],
            "sampled_at": iso(latest.get("at")),
            "now": now,
            "last_minute": {
                "gpu_util_avg": mean(values("gpu_util")), "gpu_util_max": peak(values("gpu_util")),
                "vram_used_mb_max": peak(values("vram_used_mb")), "power_w_avg": mean(values("power_w")),
                "temp_c_max": peak(values("temp_c")),
                "busy_share": round(sum(1 for s in recent if s.get("in_flight")) / len(recent), 2) if recent else None,
            },
            "requests": {
                "running": latest.get("running", 0), "waiting": latest.get("waiting", 0),
                "in_flight": [{"app": c["app"], "model": c["model"], "endpoint": c["endpoint"],
                               "seconds": round(snapshot["now"] - c["started_at"], 1)} for c in snapshot["in_flight"]],
            },
            "ollama": {
                "problem": snapshot["engine_problem"],
                "loaded_models": [{**tidy(m, "name", "vram_mb", "size_mb", "context", "quantization", "parameters"),
                                   "unloads_at": iso(m.get("expires_at"))} for m in snapshot["models"]],
            },
            "models_outside_ollama": snapshot["app_models"],
        }
        if self.residency is not None and self.config.resident_model:
            status["resident_model"] = {"model": self.config.resident_model, "state": self.residency.state}
        return status

    async def gpu_history(self, hours: float, until: str | None) -> dict[str, Any]:
        start, end = window(hours, until)
        data = await self.read("gpu_history", start, end)
        samples = data["samples"]

        def values(key: str) -> list[float]:
            return [s[key] for s in samples if s.get(key) is not None]

        headroom = [s["vram_total_mb"] - s["vram_used_mb"] for s in samples
                    if s.get("vram_total_mb") is not None and s.get("vram_used_mb") is not None]
        return {
            "start": iso(start), "end": iso(end), "bucket_seconds": data["bucket_seconds"],
            "summary": {
                "gpu_util_avg": mean(values("gpu_util")), "gpu_util_max": peak(values("gpu_util_max")),
                "mem_util_avg": mean(values("mem_util")), "vram_used_mb_max": peak(values("vram_used_mb")),
                "vram_free_mb_min": round(min(headroom), 1) if headroom else None,
                "ollama_vram_mb_max": peak(values("ollama_vram_mb")), "power_w_avg": mean(values("power_w")),
                "temp_c_max": peak(values("temp_c")), "busy_pct_avg": mean(values("busy_pct")),
                "points": len(samples),
            },
            "points": [{"at": iso(s["at"]), **tidy(s, "gpu_util", "gpu_util_max", "mem_util", "vram_used_mb",
                                                    "vram_total_mb", "ollama_vram_mb", "power_w", "temp_c",
                                                    "sm_clock_mhz", "in_flight", "waiting", "busy_pct")}
                       for s in samples],
            "model_events": [{"at": iso(e["at"]), **tidy(e, "model", "event", "vram_mb", "context")} for e in data["events"]],
        }

    async def model_calls(self, hours: float, until: str | None, app: str | None, model: str | None,
                          problems_only: bool, limit: int) -> dict[str, Any]:
        start, end = window(hours, until)
        limit = min(max(int(limit), 1), MAX_CALLS)
        rows = await self.read("calls_between", start, end, (app or "").strip().lower() or None,
                               (model or "").strip() or None, problems_only, limit)
        calls = [{"started_at": iso(r["started_at"]), **tidy(
            r, "app", "model", "endpoint", "outcome", "status", "wall_ms", "wait_ms", "load_ms", "prompt_ms", "output_ms",
            "prompt_tokens", "output_tokens", "images", "num_ctx", "ahead", "error")} for r in rows]
        return {"start": iso(start), "end": iso(end), "shown": len(calls), "limit": limit, "calls": calls}

    async def model_usage(self, hours: float, until: str | None) -> dict[str, Any]:
        start, end = window(hours, until)
        data = await self.read("history", start, end)
        found = signals(data["calls"], data["intervals"], data["samples"], data["events"])
        groups = []
        for g in data["groups"]:
            output_ms, prompt_ms = g.get("output_ms") or 0, g.get("prompt_ms") or 0
            groups.append({
                **tidy(g, "app", "model", "calls", "problems", "wall_avg", "wall_p50", "wall_p95", "images_avg", "num_ctx_max"),
                "busy_seconds": round((g.get("wall_sum") or 0) / 1000, 1),
                "waiting_seconds": round((g.get("wait_sum") or 0) / 1000, 1),
                "loading_seconds": round((g.get("load_sum") or 0) / 1000, 1),
                "prompt_tokens": g.get("prompt_tokens"), "output_tokens": g.get("output_tokens"),
                "output_tokens_per_second": round(g["output_tokens"] / (output_ms / 1000), 1) if g.get("output_tokens") and output_ms else None,
                "prompt_tokens_per_second": round(g["prompt_tokens"] / (prompt_ms / 1000), 1) if g.get("prompt_tokens") and prompt_ms else None,
            })
        return {
            "start": iso(start), "end": iso(end), "calls_total": data["calls_total"], "by_app_and_model": groups,
            "signals": {key: rounded(value) for key, value in found.items() if key != "findings"}
                       | {"findings": [{k: rounded(v) for k, v in f.items()} for f in found["findings"]]},
        }


def icon_name(size: int) -> str:
    return f"icon-{size}.png"


def server_icons(public_url: str) -> list[Icon]:
    """The icons in the server's info: by URL when it has a public address, then inline."""
    public_url = public_url.rstrip("/")
    icons = [Icon(src=f"{public_url}/icons/{icon_name(size)}", mimeType="image/png", sizes=[f"{size}x{size}"])
             for size in ICON_SIZES if public_url]
    for size in INLINE_ICON_SIZES:
        data = base64.b64encode((ASSETS / icon_name(size)).read_bytes()).decode("ascii")
        icons.append(Icon(src=f"data:image/png;base64,{data}", mimeType="image/png", sizes=[f"{size}x{size}"]))
    return icons


def create_mcp(config: MonitorConfig, live: LiveState, store: MonitorStore | None, residency: Any = None,
               auth_transport: httpx.AsyncBaseTransport | None = None) -> FastMCP:
    resource = f"{config.mcp_public_url}/mcp"
    mcp = FastMCP(
        "LunaSentia",
        instructions=INSTRUCTIONS,
        token_verifier=OwnerTokenVerifier(config.auth_internal_url, resource, auth_transport),
        # The verifier compares resources itself, and tolerates clients that send none.
        auth=AuthSettings(issuer_url=AnyHttpUrl(config.auth_public_url), resource_server_url=AnyHttpUrl(resource),
                          validate_token_resource=False),
        host="0.0.0.0", port=MonitorConfig.MCP_PORT, stateless_http=True, json_response=True,
        website_url=config.public_url, icons=server_icons(config.mcp_public_url),
    )

    # The icons, without sign-in, for clients and favicon crawlers that fetch them.
    @mcp.custom_route("/favicon.ico", methods=["GET"])
    async def favicon(request: Request) -> Response:
        return FileResponse(ASSETS / "favicon.ico", media_type="image/x-icon", headers=ICON_HEADERS)

    @mcp.custom_route("/icons/{name}", methods=["GET"])
    async def icon(request: Request) -> Response:
        name = request.path_params["name"]
        if name not in {icon_name(size) for size in ICON_SIZES}:
            return Response("Not found", 404)
        return FileResponse(ASSETS / name, media_type="image/png", headers=ICON_HEADERS)
    tools = MonitorTools(config, live, store, residency)

    @mcp.tool()
    def get_gpu_status() -> dict[str, Any]:
        """The GPU right now: utilisation, VRAM (used, free, Ollama's share), temperature, power, and clocks,
        with last-minute averages and peaks, the model calls running and waiting, the models Ollama has
        loaded and when each unloads, the models Visia, Lexia, Vibia, and Socia run themselves, and whether the
        resident chat model is loaded."""
        return tools.gpu_status()

    @mcp.tool()
    async def get_gpu_history(hours: float = 1.0, until: str | None = None) -> dict[str, Any]:
        """The GPU over time from the stored samples: a summary (average and peak utilisation, peak VRAM,
        least free VRAM, average power, hottest temperature, share of time busy) and about 360 points.

        hours: how far back from ``until`` to look, up to 744 (31 days; samples are kept 30 days by default).
        until: the end, as ISO 8601 (UTC unless it says otherwise); now when left out.
        Also lists when Ollama loaded or unloaded each model in that time."""
        return await tools.gpu_history(hours, until)

    @mcp.tool()
    async def list_model_calls(hours: float = 1.0, until: str | None = None, app: str | None = None,
                               model: str | None = None, problems_only: bool = False, limit: int = 50) -> dict[str, Any]:
        """Model calls through the monitor, newest first, with their timings in milliseconds: waiting in the
        queue, loading the model, reading the prompt, and writing the reply, plus token counts. Never the
        prompt or the reply itself.

        app: only this app (visia, vita, lexia, socia, ...). model: only this Ollama model.
        problems_only: only calls that failed, were cancelled, or timed out. limit: up to 500."""
        return await tools.model_calls(hours, until, app, model, problems_only, limit)

    @mcp.tool()
    async def get_model_usage(hours: float = 24.0, until: str | None = None) -> dict[str, Any]:
        """Totals per app and model: calls, problems, typical and slow (p95) call times, time spent busy,
        waiting, and loading, tokens and tokens per second; with the monitor's signals (overlap, queueing,
        VRAM headroom, model loads) and its verdict on whether a batching server such as vLLM would help."""
        return await tools.model_usage(hours, until)

    @mcp.tool()
    def get_ui_link() -> dict[str, str]:
        """The link to the model monitor's page, with live charts of all of this. The owner signs in there."""
        return {"app": "Luna Sentia model monitor", "url": config.public_url}

    return mcp
