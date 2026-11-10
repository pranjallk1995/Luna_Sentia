"""ComfyUI's own page, for the owner only, behind Luna's single sign-on.

ComfyUI has no sign-in: anyone who reaches it can run the GPU, read every picture, and
upload files. So it is published only through this gate, which checks the luna_sso
cookie like the monitor's page, then passes every request on to ComfyUI unchanged,
streamed both ways, and relays its websocket (live progress and previews). The cookie
never reaches ComfyUI. Luna Socia and the monitor's residency still call ComfyUI
directly on the private network.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from app.config import MonitorConfig
from app.web import Sessions, message_page

logger = logging.getLogger("monitor.comfyui")
# Headers that belong to one connection, not to the request or reply passed through.
HOP_BY_HOP = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
                        "transfer-encoding", "upgrade", "host", "content-length"})
METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


def same_site(origin: str | None, host: str | None, public_url: str) -> bool:
    """A request from this gate's own page, or one with no Origin (not sent by a browser script).

    The page's address is the configured public one, or the Host the request names.
    """
    return not origin or urlsplit(origin).netloc in {urlsplit(public_url).netloc, host or ""}


def create_gate(config: MonitorConfig, auth_transport: httpx.AsyncBaseTransport | None = None,
                comfyui_transport: httpx.AsyncBaseTransport | None = None) -> Starlette:
    sessions = Sessions(config, auth_transport)
    upstream = config.comfyui_url.rstrip("/")
    client = httpx.AsyncClient(base_url=upstream, transport=comfyui_transport,
                               timeout=httpx.Timeout(MonitorConfig.COMFYUI_GATE_TIMEOUT_SECONDS, connect=5.0))

    async def refusal(cookies: dict[str, str], path: str, wants_page: bool) -> Response | None:
        """None for the owner; otherwise what to answer with."""
        user = await sessions.user(cookies.get(MonitorConfig.COOKIE_NAME))
        if user is None:
            if wants_page:
                here = f"{config.comfyui_public_url}{path}"
                return RedirectResponse(f"{config.auth_public_url}/login?next={quote(here, safe='')}", 303)
            return JSONResponse({"error": "not signed in"}, 401)
        if user.get("user_id") != MonitorConfig.OWNER_ID:
            if wants_page:
                return message_page("Owner only", "ComfyUI runs the home's GPU and holds every picture, so only the owner can open it.", 403)
            return JSONResponse({"error": "owner only"}, 403)
        return None

    def upstream_headers(headers: Any) -> dict[str, str]:
        # ComfyUI refuses a request whose Origin is not its own host, so a browser's
        # Origin is rewritten to ComfyUI's; the gate has checked it already.
        passed = {name: value for name, value in headers.items() if name.lower() not in HOP_BY_HOP | {"cookie", "origin"}}
        if "origin" in {name.lower() for name in headers}:
            passed["Origin"] = upstream
        return passed

    async def proxy(request: Request) -> Response:
        wants_page = request.method == "GET" and "text/html" in request.headers.get("accept", "")
        if refused := await refusal(request.cookies, request.url.path, wants_page):
            return refused
        if not same_site(request.headers.get("origin"), request.headers.get("host"), config.comfyui_public_url):
            return JSONResponse({"error": "cross-site request"}, 403)
        outgoing = client.build_request(request.method, request.url.path, params=request.query_params.multi_items(),
                                        headers=upstream_headers(request.headers), content=await request.body())
        try:
            reply = await client.send(outgoing, stream=True)
        except httpx.HTTPError as error:
            return JSONResponse({"error": f"ComfyUI is not answering: {type(error).__name__}"}, 502)
        headers = {name: value for name, value in reply.headers.items() if name.lower() not in HOP_BY_HOP}
        return StreamingResponse(reply.aiter_raw(), status_code=reply.status_code, headers=headers,
                                 background=BackgroundTask(reply.aclose))

    async def websocket(socket: WebSocket) -> None:
        if await refusal(socket.cookies, socket.url.path, False) or not same_site(
                socket.headers.get("origin"), socket.headers.get("host"), config.comfyui_public_url):
            await socket.close(code=1008)
            return
        target = upstream.replace("http", "ws", 1) + socket.url.path + (f"?{socket.url.query}" if socket.url.query else "")
        try:
            remote = await connect(target, origin=upstream, max_size=None, open_timeout=5)
        except (OSError, ConnectionClosed, TimeoutError) as error:
            logger.warning("ComfyUI's websocket did not open: %s", error)
            await socket.close(code=1011)
            return
        await socket.accept()

        async def to_comfyui() -> None:
            while True:
                message = await socket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                await remote.send(message["bytes"] if message.get("bytes") is not None else message.get("text", ""))

        async def to_browser() -> None:
            async for message in remote:
                if isinstance(message, bytes):
                    await socket.send_bytes(message)
                else:
                    await socket.send_text(message)

        tasks = [asyncio.create_task(to_comfyui()), asyncio.create_task(to_browser())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except (WebSocketDisconnect, ConnectionClosed):
            pass
        finally:
            for task in tasks:
                task.cancel()
            await remote.close()
            try:
                await socket.close()
            except RuntimeError:
                pass  # already closed by the browser

    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    return Starlette(routes=[
        Route("/gate/health", health),
        # ComfyUI answers on its paths both bare and under /api.
        WebSocketRoute("/ws", websocket),
        WebSocketRoute("/api/ws", websocket),
        Route("/{path:path}", proxy, methods=METHODS),
    ])
