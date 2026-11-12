"""Tests for the monitor: parsing calls, the live state, the proxy, the page's gate, and the signals."""

import asyncio
import json
import unittest

import httpx
from starlette.testclient import TestClient

from app.calls import app_name, describe_request, timings
from app.config import MonitorConfig
from app.insights import busy_and_overlap, signals
from app.live import LiveState
from app.proxy import OllamaProxy
from app.web import create_app

MS = 1_000_000
CHAT_REPLY = {"model": "qwen3-vl:8b-instruct", "message": {"role": "assistant", "content": "{\"eval_count\": 999}"},
              "done": True, "total_duration": 3000 * MS, "load_duration": 100 * MS,
              "prompt_eval_count": 1200, "prompt_eval_duration": 400 * MS, "eval_count": 80, "eval_duration": 2000 * MS}


def config() -> MonitorConfig:
    return MonitorConfig(upstream_url="http://engine", database_url="postgresql://unused", auth_internal_url="http://auth",
                         auth_public_url="https://auth.example.com", public_url="https://monitor.example.com")


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class CallParsingTests(unittest.TestCase):
    def test_describes_a_vision_request_without_keeping_its_content(self):
        body = json.dumps({"model": "qwen3-vl:8b-instruct", "stream": False, "format": {"type": "object"},
                           "options": {"num_ctx": 16384}, "messages": [{"role": "user", "content": "x", "images": ["a", "b"]}]}).encode()
        self.assertEqual(describe_request("/api/chat", body), {"model": "qwen3-vl:8b-instruct", "streamed": False, "images": 2,
                                                               "num_ctx": 16384, "structured": True, "request_bytes": len(body)})

    def test_ollama_streams_by_default_but_embeddings_do_not(self):
        self.assertTrue(describe_request("/api/chat", b'{"model": "m"}')["streamed"])
        self.assertFalse(describe_request("/api/embed", b'{"model": "m"}')["streamed"])
        self.assertEqual(describe_request("/api/chat", b"not json")["model"], "unknown")

    def test_reads_ollamas_timings_but_not_lookalikes_inside_the_reply_text(self):
        found = timings(json.dumps(CHAT_REPLY).encode())
        self.assertEqual(found["eval_count"], 80)
        self.assertEqual(found["prompt_eval_duration"], 400 * MS)

    def test_names_the_app_from_its_container(self):
        self.assertEqual(app_name("luna_lexia-mcp-1.luna_sentia_network", "172.18.0.9"), "lexia")
        self.assertEqual(app_name("something-else.net", "172.18.0.9"), "something-else")
        self.assertEqual(app_name(None, "172.18.0.9"), "172.18.0.9")


class LiveStateTests(unittest.TestCase):
    def test_splits_a_call_into_waiting_loading_reading_and_writing(self):
        clock = Clock()
        live = LiveState(clock=clock)
        call = live.begin("lexia", "/api/chat", describe_request("/api/chat", b'{"model": "m"}'))
        clock.now += 4.0
        live.finish(call, 200, "ok", found=timings(json.dumps(CHAT_REPLY).encode()))
        self.assertAlmostEqual(call.wall_ms, 4000)
        self.assertAlmostEqual(call.wait_ms, 4000 - 100 - 400 - 2000)
        self.assertEqual((call.prompt_tokens, call.output_tokens), (1200, 80))
        self.assertEqual(live.take_pending()[0], [call])

    def test_counts_requests_waiting_behind_ollamas_one_at_a_time_limit(self):
        live = LiveState(parallel=1, clock=Clock())
        for app in ("lexia", "fiora", "visia"):
            live.begin(app, "/api/chat", {"model": "vl"})
        live.begin("lexia", "/api/embed", {"model": "embed"})
        self.assertEqual(live.queue(), (2, 2))
        self.assertEqual(live.begin("vibia", "/api/chat", {"model": "vl"}).ahead, 4)

    def test_stores_five_second_summaries_of_one_second_samples(self):
        clock = Clock(1000.0)
        live = LiveState(clock=clock)
        for util in (10, 30, 50, 70, 90, 20):
            live.add_sample({"gpu_util": util, "vram_used_mb": 5000, "vram_total_mb": 12000})
            clock.now += 1
        row = live.take_pending()[1][0]
        self.assertEqual(row["at"], 1000)
        self.assertEqual((row["gpu_util_avg"], row["gpu_util_max"]), (50, 90))

    def test_records_when_models_load_and_unload(self):
        live = LiveState(clock=Clock())
        live.set_models([{"name": "vl", "vram_mb": 6000}])
        live.set_models([])
        self.assertEqual([e["event"] for e in live.take_pending()[2]], ["loaded", "unloaded"])


class FakeOllama:
    def __init__(self, reply: bytes, status: int = 200, delay: float = 0.0) -> None:
        self.reply, self.status, self.delay, self.seen = reply, status, delay, []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append((request.method, request.url.path, await request.aread()))
        if self.delay:
            await asyncio.sleep(self.delay)
        reply = self.reply

        async def chunks():
            # Streamed in pieces, as a real server sends it.
            for start in range(0, len(reply), 64):
                yield reply[start:start + 64]

        return httpx.Response(self.status, content=chunks(), headers={"content-type": "application/json"})


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, upstream: FakeOllama, path: str, body: bytes, method: str = "POST"):
        live = LiveState()
        proxy = OllamaProxy(live, "http://engine", transport=httpx.MockTransport(upstream),
                            resolve=lambda address: ("luna_fiora-mcp-1.luna_sentia_network", [], [address]))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy, client=("172.18.0.5", 1234)), base_url="http://ollama") as client:
            response = await client.request(method, path, content=body)
        return live, response

    async def test_passes_a_call_through_unchanged_and_records_its_timings(self):
        reply = json.dumps(CHAT_REPLY).encode()
        upstream = FakeOllama(reply)
        body = b'{"model": "qwen3-vl:8b-instruct", "stream": false, "messages": []}'
        live, response = await self.call(upstream, "/api/chat", body)
        self.assertEqual(response.content, reply)
        self.assertEqual(upstream.seen, [("POST", "/api/chat", body)])
        call = live.take_pending()[0][0]
        self.assertEqual((call.app, call.model, call.outcome, call.output_tokens), ("fiora", "qwen3-vl:8b-instruct", "ok", 80))

    async def test_does_not_record_calls_that_run_no_model(self):
        live, response = await self.call(FakeOllama(b'{"models": []}'), "/api/tags", b"", "GET")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(live.take_pending()[0], [])

    async def test_records_ollamas_refusals_as_failed(self):
        live, response = await self.call(FakeOllama(b'{"error": "model not found"}', 404), "/api/chat", b'{"model": "x"}')
        self.assertEqual(response.status_code, 404)
        call = live.take_pending()[0][0]
        self.assertEqual((call.outcome, call.status), ("failed", 404))
        self.assertIn("model not found", call.error)

    async def test_answers_502_when_ollama_is_unreachable(self):
        def down(request):
            raise httpx.ConnectError("refused")

        live = LiveState()
        proxy = OllamaProxy(live, "http://engine", transport=httpx.MockTransport(down), resolve=lambda a: (a, [], [a]))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy), base_url="http://ollama") as client:
            response = await client.post("/api/chat", content=b'{"model": "x"}')
        self.assertEqual(response.status_code, 502)
        self.assertEqual(live.take_pending()[0][0].outcome, "failed")


class FakeAuth:
    def __init__(self, users):
        self.users = users

    def __call__(self, request: httpx.Request) -> httpx.Response:
        user = self.users.get(request.headers.get("X-Luna-Session", ""))
        return httpx.Response(200, json=user) if user else httpx.Response(401, json={"error": "not signed in"})


class PageTests(unittest.TestCase):
    def client(self, cookie: str | None = None) -> TestClient:
        auth = FakeAuth({"owner-token": {"user_id": "owner"}, "guest-token": {"user_id": "u-2"}})
        app = create_app(config(), LiveState(), None, httpx.MockTransport(auth))
        client = TestClient(app, base_url="https://monitor.example.com", follow_redirects=False)
        if cookie:
            client.cookies.set("luna_sso", cookie)
        return client

    def test_sends_people_who_are_signed_out_to_sign_in(self):
        response = self.client().get("/")
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["location"].startswith("https://auth.example.com/login?next=https%3A%2F%2Fmonitor.example.com"))
        self.assertEqual(self.client().get("/api/history").status_code, 401)

    def test_shows_the_monitor_to_the_owner_only(self):
        self.assertEqual(self.client("guest-token").get("/").status_code, 403)
        self.assertEqual(self.client("guest-token").get("/api/history").status_code, 403)
        page = self.client("owner-token").get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Luna Sentia", page.text)

    def test_says_when_there_is_no_database(self):
        self.assertEqual(self.client("owner-token").get("/api/history").status_code, 503)


class SignalTests(unittest.TestCase):
    def test_measures_busy_and_overlapping_time(self):
        self.assertEqual(busy_and_overlap([(0, 10), (5, 15), (20, 25)]), (20, 5, 2))

    def calls(self, overlapping: bool):
        rows = []
        for i in range(30):
            start = i * (5 if overlapping else 12)
            rows.append({"started_at": start, "finished_at": start + 10, "outcome": "ok", "wall_ms": 10_000,
                         "wait_ms": 5000 if overlapping else 100, "load_ms": 50, "prompt_ms": 1000, "prompt_tokens": 1000,
                         "output_ms": 3900, "output_tokens": 100})
        return rows

    def test_favours_vllm_when_calls_overlap_and_queue_with_room_to_spare(self):
        rows = self.calls(True)
        result = signals(rows, rows, [{"vram_total_mb": 12000, "vram_used_mb": 6000}], [])
        self.assertTrue(result["enough"])
        self.assertIn("Worth trying vLLM", result["verdict"])

    def test_favours_ollama_when_calls_run_one_after_another(self):
        rows = self.calls(False)
        result = signals(rows, rows, [{"vram_total_mb": 12000, "vram_used_mb": 6000}], [])
        self.assertIn("Stay on Ollama: requests rarely overlap", result["verdict"])

    def test_no_room_outweighs_queueing(self):
        rows = self.calls(True)
        result = signals(rows, rows, [{"vram_total_mb": 12000, "vram_used_mb": 11000}], [])
        self.assertIn("too little VRAM", result["verdict"])

    def test_waits_for_enough_calls(self):
        self.assertFalse(signals([], [], [], [])["enough"])


if __name__ == "__main__":
    unittest.main()


class PlainRowsTests(unittest.TestCase):
    def test_numeric_values_become_floats_so_history_is_valid_json(self):
        import json
        from decimal import Decimal

        from app.store import plain

        rows = plain([{"app": "visia", "images_avg": Decimal("1.5"), "calls": 3, "wall_avg": 2.5, "num_ctx_max": None}])
        self.assertEqual(rows, [{"app": "visia", "images_avg": 1.5, "calls": 3, "wall_avg": 2.5, "num_ctx_max": None}])
        json.dumps(rows)


class AppModelsTests(unittest.TestCase):
    """Asking Visia, Lexia, Vibia, and Socia about the models they run themselves."""

    def ask(self, handler, token="t" * 32):
        import asyncio

        import httpx

        from app.main import ask_app

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await ask_app(client, "visia", "http://luna-visia-mcp:8000", token)

        return asyncio.run(run())

    def test_a_report_is_passed_on_and_asked_for_as_the_owner(self):
        import httpx

        seen = {}

        def handler(request):
            seen.update(path=request.url.path, user=request.headers["X-Luna-User"], auth=request.headers["Authorization"])
            return httpx.Response(200, json={"app": "visia", "models": [{"name": "siglip", "calls": 3}], "gpu": {"reserved_mb": 900}})

        entry = self.ask(handler)
        self.assertEqual(seen, {"path": "/api/models", "user": "owner", "auth": "Bearer " + "t" * 32})
        self.assertEqual((entry["problem"], entry["models"][0]["calls"], entry["gpu"]["reserved_mb"]), (None, 3, 900))

    def test_problems_say_what_to_fix(self):
        import httpx

        def refuse(request):
            return httpx.Response(401)

        def old(request):
            return httpx.Response(404)

        def down(request):
            raise httpx.ConnectError("no route")

        self.assertIn("differs", self.ask(refuse)["problem"])
        self.assertIn("does not report", self.ask(old)["problem"])
        self.assertEqual(self.ask(down)["problem"], "not running")
        self.assertIn("is not set", self.ask(refuse, token="")["problem"])


class AssetTests(unittest.TestCase):
    def test_the_page_names_its_assets_by_content_and_they_are_rechecked(self):
        from starlette.testclient import TestClient

        from app.web import Assets, asset_version

        version = asset_version()
        self.assertRegex(version, r"^[0-9a-f]{12}$")
        app = Assets(directory=str(__import__("app.web", fromlist=["STATIC"]).STATIC))
        response = TestClient(app).get("/styles.css")
        self.assertEqual(response.headers["cache-control"], "no-cache")
        page = (__import__("app.web", fromlist=["STATIC"]).STATIC / "index.html").read_text()
        self.assertIn("styles.css?v=__ASSETS__", page)
        self.assertIn("app.js?v=__ASSETS__", page)


class ResidencyTests(unittest.TestCase):
    def residency(self, models=(), samples=(), in_flight=False, comfy_queue=None, now=1000.0):
        from app.residency import Residency

        live = LiveState()
        live.models = list(models)
        live.samples.extend(samples)
        if in_flight:
            live.begin("visia", "/api/chat", {"model": "qwen3-vl:8b-instruct"})
        seen = []

        def engine(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={})

        clock = Clock(now)
        residency = Residency(live, "http://engine", "qwen3:14b", 8192, "2m", 30.0,
                              transport=httpx.MockTransport(engine), clock=clock)
        residency.comfyui_busy = lambda: asyncio.sleep(0, result=bool(comfy_queue))
        return residency, clock, seen

    def test_the_resident_stays_for_good_and_visitors_briefly(self):
        residency, _, _ = self.residency()
        stay = lambda model, **extra: json.loads(residency.rewrite("/api/chat", json.dumps({"model": model, **extra}).encode()))["keep_alive"]
        self.assertEqual(stay("qwen3:14b"), -1)
        self.assertEqual(stay("qwen3-vl:8b-instruct"), "2m")
        self.assertEqual(stay("qwen3-vl:8b-instruct", keep_alive=-1), "2m")
        # Asking a model to leave, as Socia does before a picture, passes unchanged.
        self.assertEqual(stay("qwen3:14b", keep_alive=0), 0)
        self.assertEqual(residency.rewrite("/api/tags", b"{}"), b"{}")
        self.assertEqual(residency.rewrite("/api/chat", b"not json"), b"not json")

    def test_the_resident_comes_back_only_when_the_gpu_is_quiet_and_has_room(self):
        quiet = 1000.0 + 31
        residency, clock, seen = self.residency()
        residency.last_activity = 1000.0
        clock.now = 1010.0
        self.assertEqual(asyncio.run(residency.why_not_now()), "recently busy")
        clock.now = quiet
        self.assertIsNone(asyncio.run(residency.why_not_now()))
        asyncio.run(residency.load())
        self.assertEqual(seen, [{"model": "qwen3:14b", "keep_alive": -1, "options": {"num_ctx": 8192}}])

        loaded, _, _ = self.residency(models=[{"name": "qwen3:14b", "vram_mb": 9600}])
        self.assertEqual(asyncio.run(loaded.why_not_now()), "loaded")
        busy, _, _ = self.residency(in_flight=True, now=quiet)
        self.assertEqual(asyncio.run(busy.why_not_now()), "a model is working")
        drawing, clock, _ = self.residency(comfy_queue=True)
        clock.now = quiet
        self.assertEqual(asyncio.run(drawing.why_not_now()), "ComfyUI is drawing")

        # SigLIP still holds 2 GB: 9.6 GB would not fit in the 8.8 GB left, so wait.
        crowded, clock, _ = self.residency(samples=[{"vram_total_mb": 12227, "vram_used_mb": 3400}])
        crowded.resident_mb = 9600
        clock.now = quiet
        self.assertEqual(asyncio.run(crowded.why_not_now()), "an app's own model still holds the GPU")
        # A visiting Ollama model counts as room: Ollama unloads it to fit the resident.
        crowded.live.models = [{"name": "qwen3-vl:8b-instruct", "vram_mb": 6600}]
        self.assertIsNone(asyncio.run(crowded.why_not_now()))


class ComfyUIGateTests(unittest.TestCase):
    """ComfyUI's page behind the owner's sign-in, passed through unchanged."""

    def setUp(self):
        self.seen: list[httpx.Request] = []

    def comfyui(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        body = json.dumps({"path": request.url.path}).encode()

        async def stream():  # a streamed reply, as ComfyUI sends over the network
            yield body

        return httpx.Response(200, content=stream(), headers={"X-Comfy": "yes", "Content-Type": "application/json"})

    def client(self, cookie: str | None = None, comfyui=None, comfyui_url: str = "http://comfyui:8188") -> TestClient:
        from dataclasses import replace

        from app.comfyui_gate import create_gate
        auth = FakeAuth({"owner-token": {"user_id": "owner"}, "guest-token": {"user_id": "u-2"}})
        gate = create_gate(replace(config(), comfyui_url=comfyui_url, comfyui_public_url="https://comfyui.example.com"),
                           httpx.MockTransport(auth), httpx.MockTransport(comfyui or self.comfyui))
        client = TestClient(gate, base_url="https://comfyui.example.com", follow_redirects=False)
        if cookie:
            client.cookies.set("luna_sso", cookie)
        return client

    def test_sends_people_who_are_signed_out_to_sign_in(self):
        page = self.client().get("/", headers={"Accept": "text/html"})
        self.assertEqual(page.status_code, 303)
        self.assertTrue(page.headers["location"].startswith("https://auth.example.com/login?next=https%3A%2F%2Fcomfyui.example.com%2F"))
        self.assertEqual(self.client().get("/api/queue").status_code, 401)
        self.assertEqual(self.seen, [])

    def test_only_the_owner_reaches_comfyui(self):
        self.assertEqual(self.client("guest-token").get("/", headers={"Accept": "text/html"}).status_code, 403)
        self.assertEqual(self.client("guest-token").get("/api/queue").status_code, 403)
        self.assertEqual(self.seen, [])

    def test_passes_the_owners_requests_through_without_the_sign_in_cookie(self):
        reply = self.client("owner-token").post("/api/prompt?x=1", json={"prompt": {}},
                                                headers={"Origin": "https://comfyui.example.com"})
        self.assertEqual((reply.status_code, reply.json(), reply.headers["X-Comfy"]), (200, {"path": "/api/prompt"}, "yes"))
        sent = self.seen[0]
        self.assertEqual((sent.method, str(sent.url), json.loads(sent.content)), ("POST", "http://comfyui:8188/api/prompt?x=1", {"prompt": {}}))
        self.assertNotIn("cookie", sent.headers)
        self.assertEqual(sent.headers["origin"], "http://comfyui:8188")

    def test_refuses_requests_from_other_sites(self):
        reply = self.client("owner-token").post("/api/prompt", json={}, headers={"Origin": "https://evil.example.com"})
        self.assertEqual(reply.status_code, 403)
        self.assertEqual(self.seen, [])

    def test_says_so_when_comfyui_is_down(self):
        def down(request):
            raise httpx.ConnectError("refused", request=request)
        self.assertEqual(self.client("owner-token", comfyui=down).get("/api/queue").status_code, 502)

    def test_closes_the_websocket_of_someone_signed_out(self):
        from starlette.websockets import WebSocketDisconnect
        with self.assertRaises(WebSocketDisconnect) as closed:
            with self.client().websocket_connect("/ws?clientId=1"):
                pass
        self.assertEqual(closed.exception.code, 1008)

    def test_relays_comfyuis_websocket_both_ways(self):
        import threading

        from websockets.asyncio.server import serve

        ready, stop = threading.Event(), None
        port = {}

        async def echo(remote):
            await remote.send(json.dumps({"type": "status", "path": remote.request.path}))
            async for message in remote:
                await remote.send(message)

        def run():
            nonlocal stop

            async def main():
                nonlocal stop
                stop = asyncio.get_running_loop().create_future()
                async with serve(echo, "127.0.0.1", 0) as server:
                    port["n"] = server.sockets[0].getsockname()[1]
                    ready.set()
                    await stop

            loop = asyncio.new_event_loop()
            port["loop"] = loop
            loop.run_until_complete(main())

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        ready.wait(5)
        try:
            client = self.client("owner-token", comfyui_url=f"http://127.0.0.1:{port['n']}")
            headers = {"Origin": "https://comfyui.example.com", "Cookie": "luna_sso=owner-token"}
            with client.websocket_connect("/ws?clientId=7", headers=headers) as socket:
                self.assertEqual(json.loads(socket.receive_text()), {"type": "status", "path": "/ws?clientId=7"})
                socket.send_bytes(b"\x00preview")
                self.assertEqual(socket.receive_bytes(), b"\x00preview")
        finally:
            port["loop"].call_soon_threadsafe(stop.set_result, None)
            thread.join(5)
