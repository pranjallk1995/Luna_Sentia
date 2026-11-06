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
