"""The monitor's page and its data, for the owner only, behind Luna's single sign-on."""

from __future__ import annotations

import hashlib

import asyncio
import html
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from app.config import MonitorConfig
from app.insights import signals
from app.live import LiveState
from app.store import MonitorStore

STATIC = Path(__file__).parent / "static"


def asset_version() -> str:
    """A short hash of the page's own files, so a new release gets new asset URLs.

    Browsers and Cloudflare's cache keep .css and .js files; a changed URL makes
    them fetch the new ones at once instead of showing the old layout.
    """
    digest = hashlib.sha256()
    for name in ("app.js", "styles.css"):
        digest.update((STATIC / name).read_bytes())
    return digest.hexdigest()[:12]


class Assets(StaticFiles):
    """Static files that browsers check again before reusing."""

    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response
LIVE_TICK_SECONDS = 1.0
# The longest history range the page offers.
MAX_HISTORY_SECONDS = 31 * 86400


def message_page(title: str, message: str, status: int) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title>"
        "<body style='font-family:ui-monospace,monospace;background:#111;color:#f5f5f5;display:grid;place-items:center;"
        f"min-height:90vh;margin:0 16px'><main style='max-width:40rem'><h1>{html.escape(title)}</h1><p>{message}</p></main>",
        status_code=status,
    )


class Sessions:
    """Who a luna_sso cookie belongs to, asked of Sentia's auth service and remembered briefly."""

    def __init__(self, config: MonitorConfig, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.url = f"{config.auth_internal_url}/session"
        self.client = httpx.AsyncClient(transport=transport, timeout=5.0)
        self.known: dict[str, tuple[dict[str, Any] | None, float]] = {}

    async def user(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        cached = self.known.get(token)
        if cached and time.monotonic() - cached[1] < MonitorConfig.SESSION_RECHECK_SECONDS:
            return cached[0]
        try:
            response = await self.client.get(self.url, headers={MonitorConfig.SESSION_HEADER: token})
            user = response.json() if response.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            # Auth briefly unreachable: keep a session confirmed earlier, never invent one.
            return cached[0] if cached else None
        if len(self.known) > 256:
            self.known.clear()
        self.known[token] = (user, time.monotonic())
        return user


def live_signals(live: LiveState) -> dict[str, Any]:
    snapshot = live.snapshot()
    calls = snapshot["calls"]
    intervals = [{"started_at": c["started_at"], "finished_at": c["finished_at"]} for c in calls]
    intervals += [{"started_at": c["started_at"], "finished_at": snapshot["now"]} for c in snapshot["in_flight"]]
    return signals(calls, intervals, snapshot["samples"], [])


def create_app(config: MonitorConfig, live: LiveState, store: MonitorStore | None,
               auth_transport: httpx.AsyncBaseTransport | None = None) -> Starlette:
    sessions = Sessions(config, auth_transport)

    async def gate(request: Request) -> Response | None:
        """None for the owner; otherwise the page or error to answer with."""
        user = await sessions.user(request.cookies.get(MonitorConfig.COOKIE_NAME))
        wants_page = not request.url.path.startswith("/api/")
        if user is None:
            if wants_page:
                here = f"{config.public_url}{request.url.path}"
                return RedirectResponse(f"{config.auth_public_url}/login?next={quote(here, safe='')}", 303)
            return JSONResponse({"error": "not signed in"}, 401)
        if user.get("user_id") != MonitorConfig.OWNER_ID:
            if wants_page:
                return message_page("Owner only", "The model monitor shows the whole home's hardware, so only the owner can open it.", 403)
            return JSONResponse({"error": "owner only"}, 403)
        return None

    page = (STATIC / "index.html").read_text().replace("__ASSETS__", asset_version())

    async def index(request: Request) -> Response:
        return await gate(request) or HTMLResponse(page, headers={"Cache-Control": "no-store"})

    async def live_stream(request: Request) -> Response:
        if refused := await gate(request):
            return refused

        async def events():
            snapshot = live.snapshot()
            snapshot["signals"] = live_signals(live)
            yield f"event: snapshot\ndata: {json.dumps(snapshot)}\n\n"
            last_call, since = snapshot["last_call"], snapshot["samples"][-1]["at"] if snapshot["samples"] else 0.0
            while not await request.is_disconnected():
                await asyncio.sleep(LIVE_TICK_SECONDS)
                update = live.snapshot(after_call=last_call, since=since)
                update["signals"] = live_signals(live)
                last_call = update["last_call"] or last_call
                if update["samples"]:
                    since = update["samples"][-1]["at"]
                yield f"event: update\ndata: {json.dumps(update)}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    async def history(request: Request) -> Response:
        if refused := await gate(request):
            return refused
        if store is None:
            return JSONResponse({"error": "No database is configured."}, 503)
        try:
            end = float(request.query_params.get("end") or time.time())
            span = min(max(float(request.query_params.get("seconds", "3600")), 60.0), MAX_HISTORY_SECONDS)
        except ValueError:
            return JSONResponse({"error": "seconds and end must be numbers."}, 400)
        try:
            data = await asyncio.to_thread(store.history, end - span, end)
        except Exception as error:  # noqa: BLE001 - shown on the page
            return JSONResponse({"error": f"The monitor's database could not be read: {error}"}, 503)
        data["signals"] = signals(data["calls"], data.pop("intervals"), data["samples"], data["events"])
        data["start"], data["end"] = end - span, end
        return JSONResponse(data)

    async def logout(request: Request) -> Response:
        return RedirectResponse(f"{config.auth_public_url}/logout?next={quote(config.public_url + '/', safe='')}", 303)

    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    return Starlette(routes=[
        Route("/", index),
        Route("/api/live", live_stream),
        Route("/api/history", history),
        Route("/logout", logout),
        Route("/health", health),
        Mount("/static", Assets(directory=STATIC), name="static"),
    ])
