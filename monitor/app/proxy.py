"""Pass every app's Ollama request to the real server, timing the ones that run a model.

The apps keep calling ``luna-sentia-ollama:11434``; that name now points here,
and the real server answers at ``luna-sentia-ollama-engine``. Bytes pass through
unchanged in both directions, except a model request's ``keep_alive``, which the
residency policy sets (see residency.py). Only the end of each reply is kept, to read
Ollama's timings from; prompts, images, and replies are never stored.

When the app hangs up, the request to Ollama is closed too, so Ollama stops
working on it, as it would have without the monitor in between.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
from typing import Any

import httpx

from app.calls import MODEL_ENDPOINTS, app_name, describe_request, timings
from app.config import MonitorConfig
from app.live import LiveState
from app.residency import Residency

logger = logging.getLogger(__name__)
HOP_BY_HOP = frozenset({b"connection", b"keep-alive", b"transfer-encoding", b"te", b"trailer", b"upgrade", b"host", b"content-length"})
NAME_SECONDS = 600.0


class OllamaProxy:
    def __init__(self, live: LiveState, upstream_url: str, transport: httpx.AsyncBaseTransport | None = None,
                 resolve=socket.gethostbyaddr, residency: Residency | None = None) -> None:
        self.live = live
        self.residency = residency
        self.client = httpx.AsyncClient(
            base_url=upstream_url, transport=transport,
            timeout=httpx.Timeout(None, connect=MonitorConfig.CONNECT_TIMEOUT_SECONDS),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
        )
        self.resolve = resolve
        self._names: dict[str, tuple[str, float]] = {}

    async def caller(self, scope: dict[str, Any]) -> str:
        """The app behind a client address, from Docker's DNS, remembered for a while."""
        for key, value in scope.get("headers", []):
            if key == b"x-luna-app" and value:
                return value.decode(errors="replace")[:40].lower()
        address = (scope.get("client") or ("unknown", 0))[0]
        known = self._names.get(address)
        if known and time.monotonic() - known[1] < NAME_SECONDS:
            return known[0]
        try:
            hostname = (await asyncio.to_thread(self.resolve, address))[0]
        except (OSError, UnicodeError):
            hostname = None
        name = app_name(hostname, address)
        self._names[address] = (name, time.monotonic())
        return name

    async def __call__(self, scope: dict[str, Any], receive, send) -> None:
        if scope["type"] == "lifespan":
            while (message := await receive())["type"] != "lifespan.shutdown":
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
            await self.client.aclose()
            await send({"type": "lifespan.shutdown.complete"})
            return
        if scope["type"] != "http":
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        await self.relay(scope, bytes(body), receive, send)

    async def relay(self, scope: dict[str, Any], body: bytes, receive, send) -> None:
        path, method = scope["path"], scope["method"]
        if self.residency is not None and method == "POST":
            body = self.residency.rewrite(path, body)
        call = None
        if method == "POST" and path in MODEL_ENDPOINTS:
            call = self.live.begin(await self.caller(scope), path, describe_request(path, body))
        headers = [(key, value) for key, value in scope.get("headers", []) if key.lower() not in HOP_BY_HOP]
        query = scope.get("query_string", b"").decode()
        request = self.client.build_request(method, path + (f"?{query}" if query else ""), headers=headers, content=body)

        hung_up = asyncio.Event()

        async def watch() -> None:
            while (await receive())["type"] != "http.disconnect":
                pass
            hung_up.set()

        watcher = asyncio.create_task(watch())
        status, tail, size, first, outcome, error = None, bytearray(), 0, None, "ok", None
        response = None
        try:
            response = await self.until_hung_up(self.client.send(request, stream=True), hung_up)
            if response is None:
                outcome = "cancelled"
                return
            status = response.status_code
            await send({"type": "http.response.start", "status": status,
                        "headers": [(k, v) for k, v in response.headers.raw if k.lower() not in HOP_BY_HOP - {b"content-length"}]})
            chunks = response.aiter_raw()
            while True:
                chunk = await self.until_hung_up(anext(chunks, None), hung_up)
                if chunk is None:
                    if hung_up.is_set():
                        outcome = "cancelled"
                    break
                first = first or time.time()
                size += len(chunk)
                tail += chunk
                del tail[: -MonitorConfig.REPLY_TAIL_BYTES]
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            if outcome == "ok":
                await send({"type": "http.response.body", "body": b"", "more_body": False})
                if status >= 400:
                    outcome, error = "failed", bytes(tail[-500:]).decode(errors="replace")
        except httpx.HTTPError as problem:
            outcome, error = "failed", f"{type(problem).__name__}: {problem}"
            if status is None:
                await self.refuse(send, f"Luna Sentia could not reach Ollama: {problem}")
                status = 502
        finally:
            watcher.cancel()
            if self.residency is not None:
                self.residency.touched()
            if response is not None:
                await response.aclose()
            if call is not None:
                self.live.finish(call, status, outcome, bytes(tail), first, size, error,
                                 timings(bytes(tail)) if outcome == "ok" else None)

    @staticmethod
    async def until_hung_up(awaitable, hung_up: asyncio.Event):
        """The awaitable's result, or None once the app hangs up first."""
        task = asyncio.ensure_future(awaitable)
        stop = asyncio.ensure_future(hung_up.wait())
        done, _ = await asyncio.wait({task, stop}, return_when=asyncio.FIRST_COMPLETED)
        stop.cancel()
        if task in done:
            return task.result()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, httpx.HTTPError):
            pass
        return None

    @staticmethod
    async def refuse(send, message: str) -> None:
        body = json.dumps({"error": message}).encode()
        await send({"type": "http.response.start", "status": 502,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body, "more_body": False})
