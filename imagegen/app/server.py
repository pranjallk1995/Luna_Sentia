"""Luna Sentia's pictures: Qwen-Image 2.1 Turbo with diffusers, one picture at a time.

Each picture takes the GPU to itself: Ollama's models are unloaded first, then the prompt is read in one
process and the picture drawn in the next (app/passes.py), each giving its memory back when it exits.
The monitor sees the picture in /status and waits until it is done to load the resident models again.

    POST   /pictures            {"prompt", "references": [base64], "resolution"?, "seed"?} -> 202 {"id"}
    GET    /pictures/{id}       {"status": queued|reading|drawing|done|failed|cancelled, "error", "seconds"}
    GET    /pictures/{id}/image the PNG, once done
    DELETE /pictures/{id}       takes it out of the queue, or stops it
    GET    /status              ready, busy, the queue, and the weights on disk (for the monitor)
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import random
import sys
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from app import weights

logger = logging.getLogger("imagegen")

MAX_REFERENCES = 16
# Finished pictures not fetched by then are deleted.
KEEP_SECONDS = 3600


@dataclass
class Picture:
    id: str
    prompt: str
    references: list[bytes]
    resolution: int
    seed: int
    status: str = "queued"
    error: str = ""
    created: float = field(default_factory=time.time)
    started: float = 0.0
    finished: float = 0.0
    process: asyncio.subprocess.Process | None = None

    def public(self) -> dict:
        seconds = (self.finished or time.time()) - self.started if self.started else 0.0
        return {"id": self.id, "status": self.status, "error": self.error, "seconds": round(seconds, 1)}


# Runs one pass (or the preparing) as its own process: (arguments, picture or None) -> exit code.
Runner = Callable[[list[str], "Picture | None"], Awaitable[int]]


class Pictures:
    def __init__(self, data: Path, ollama_url: str, runner: Runner | None = None,
                 transport: httpx.AsyncBaseTransport | None = None, is_ready: Callable[[], bool] = weights.ready) -> None:
        self.data = data
        self.ollama = httpx.AsyncClient(base_url=ollama_url.rstrip("/"), transport=transport, timeout=60.0)
        self.runner = runner or self.subprocess
        self.is_ready = is_ready
        self.preparing = False
        self.problem = ""
        self.pictures: dict[str, Picture] = {}
        self.queue: asyncio.Queue[Picture] = asyncio.Queue()
        self.running: Picture | None = None
        (data / "pictures").mkdir(parents=True, exist_ok=True)

    # The process each pass runs in.
    async def subprocess(self, arguments: list[str], picture: Picture | None) -> int:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", *arguments, cwd=str(Path(__file__).parent.parent),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        if picture:
            picture.process = process
        tail: list[str] = []
        assert process.stdout
        async for raw in process.stdout:
            line = raw.decode(errors="replace").rstrip()
            tail = (tail + [line])[-8:]
            if line.startswith("{"):
                logger.info("%s %s", picture.id[:8] if picture else "weights", line)
        code = await process.wait()
        if code and not (picture and picture.status == "cancelled"):
            logger.warning("%s failed:\n%s", " ".join(arguments), "\n".join(tail))
        return code

    def busy(self) -> bool:
        return self.preparing or self.running is not None or not self.queue.empty()

    def status(self) -> dict:
        return {"ready": self.is_ready(), "preparing": self.preparing, "problem": self.problem, "busy": self.busy(),
                "running": self.running.public() if self.running else None,
                "pending": [p.id for p in self.pictures.values() if p.status == "queued"],
                "files": weights.files()}

    async def empty_ollama(self) -> None:
        """Unload every model Ollama holds (Socia's resident ones too): the monitor loads them back after."""
        try:
            for _ in range(3):
                models = [m["name"] for m in (await self.ollama.get("/api/ps")).json().get("models") or []]
                if not models:
                    return
                for name in models:
                    reply = await self.ollama.post("/api/generate", json={"model": name, "keep_alive": 0})
                    if reply.status_code == 400:  # an embedding model takes no generate
                        await self.ollama.post("/api/embed", json={"model": name, "input": [], "keep_alive": 0})
                await asyncio.sleep(1)
        except (httpx.HTTPError, ValueError) as error:
            logger.warning("Could not empty Ollama before a picture: %s", error)

    async def prepare(self) -> None:
        """Download and quantize the weights on the first start; later starts find them ready."""
        if self.is_ready():
            return
        self.preparing = True
        try:
            await self.empty_ollama()
            if await self.runner(["app.weights"], None) != 0:
                self.problem = "preparing the image model failed; see the imagegen container's log"
        finally:
            self.preparing = False

    def add(self, prompt: str, references: list[bytes], resolution: int, seed: int | None) -> Picture:
        picture = Picture(uuid.uuid4().hex, prompt, references, resolution,
                          seed if seed is not None else random.randrange(2**32))
        self.pictures[picture.id] = picture
        self.queue.put_nowait(picture)
        return picture

    def folder(self, picture: Picture) -> Path:
        return self.data / "pictures" / picture.id

    def image(self, picture: Picture) -> Path:
        return self.folder(picture) / "picture.png"

    async def work(self) -> None:
        await self.prepare()
        while True:
            picture = await self.queue.get()
            if picture.status != "queued":
                continue
            self.running = picture
            try:
                await self.draw(picture)
            finally:
                self.running = None
                picture.process = None
                picture.finished = time.time()

    async def draw(self, picture: Picture) -> None:
        picture.started = time.time()
        folder = self.folder(picture)
        folder.mkdir(parents=True, exist_ok=True)
        refs = []
        for number, data in enumerate(picture.references, start=1):
            path = folder / f"reference{number}"
            path.write_bytes(data)
            refs.append(str(path))
        job = folder / "job.json"
        job.write_text(json.dumps({"prompt": picture.prompt, "refs": refs, "resolution": picture.resolution,
                                   "seed": picture.seed, "read": str(folder / "read.pt"), "out": str(self.image(picture))}))
        await self.empty_ollama()
        for stage, status in (("read", "reading"), ("draw", "drawing")):
            if picture.status == "cancelled":
                return
            picture.status = status
            code = await self.runner(["app.passes", stage, str(job)], picture)
            if picture.status == "cancelled":
                return
            if code != 0:
                picture.status, picture.error = "failed", f"the {stage} pass stopped (exit code {code})"
                return
        for path in [*map(Path, refs), folder / "read.pt", job]:
            path.unlink(missing_ok=True)
        picture.status = "done"
        logger.info("%s done in %.0f s", picture.id[:8], time.time() - picture.started)

    def cancel(self, picture: Picture) -> None:
        if picture.status in ("queued", "reading", "drawing"):
            picture.status = "cancelled"
            if picture.process and picture.process.returncode is None:
                picture.process.terminate()

    def forget(self, picture: Picture) -> None:
        self.pictures.pop(picture.id, None)
        for path in sorted(self.folder(picture).glob("*")):
            path.unlink(missing_ok=True)
        if self.folder(picture).exists():
            self.folder(picture).rmdir()

    async def sweep(self) -> None:
        while True:
            await asyncio.sleep(300)
            for picture in list(self.pictures.values()):
                if picture.finished and time.time() - picture.finished > KEEP_SECONDS:
                    self.forget(picture)


def create_app(pictures: Pictures | None = None) -> Starlette:
    pictures = pictures or Pictures(weights.ROOT, os.getenv("IMAGEGEN_OLLAMA_URL", "http://luna-sentia-ollama-engine:11434"))

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        tasks = [asyncio.create_task(pictures.work()), asyncio.create_task(pictures.sweep())]
        yield
        if pictures.running:
            pictures.cancel(pictures.running)
        for task in tasks:
            task.cancel()

    def found(request: Request) -> Picture | None:
        return pictures.pictures.get(request.path_params["id"])

    async def health(request: Request) -> Response:
        return JSONResponse({"ok": True, "ready": pictures.is_ready(), "preparing": pictures.preparing})

    async def status(request: Request) -> Response:
        return JSONResponse(pictures.status())

    async def create(request: Request) -> Response:
        if not pictures.is_ready():
            reason = pictures.problem or "the image model is still being downloaded and prepared (first start)"
            return JSONResponse({"error": reason}, status_code=503)
        try:
            body = await request.json()
            prompt = str(body["prompt"]).strip()
            references = [base64.b64decode(item, validate=True) for item in body.get("references") or []]
            resolution = int(body.get("resolution") or 1024)
            seed = None if body.get("seed") is None else int(body["seed"]) % 2**32
        except (ValueError, KeyError, TypeError, binascii.Error) as error:
            return JSONResponse({"error": f"bad request: {error}"}, status_code=400)
        if not prompt or len(references) > MAX_REFERENCES or not 256 <= resolution <= 2048:
            return JSONResponse({"error": f"a prompt, up to {MAX_REFERENCES} references, and a resolution of "
                                          "256 to 2048 pixels"}, status_code=400)
        picture = pictures.add(prompt, references, resolution, seed)
        return JSONResponse(picture.public(), status_code=202)

    async def show(request: Request) -> Response:
        picture = found(request)
        return JSONResponse(picture.public()) if picture else JSONResponse({"error": "no such picture"}, status_code=404)

    async def image(request: Request) -> Response:
        picture = found(request)
        if not picture or picture.status != "done" or not pictures.image(picture).exists():
            return JSONResponse({"error": "no finished picture"}, status_code=404)
        data = pictures.image(picture).read_bytes()
        pictures.forget(picture)
        return Response(data, media_type="image/png")

    async def remove(request: Request) -> Response:
        picture = found(request)
        if picture:
            pictures.cancel(picture)
        return JSONResponse({"ok": True})

    return Starlette(routes=[
        Route("/health", health), Route("/status", status),
        Route("/pictures", create, methods=["POST"]),
        Route("/pictures/{id}", show, methods=["GET"]), Route("/pictures/{id}", remove, methods=["DELETE"]),
        Route("/pictures/{id}/image", image),
    ], lifespan=lifespan)


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    uvicorn.run(create_app(), host="0.0.0.0", port=8000, log_level="warning")
