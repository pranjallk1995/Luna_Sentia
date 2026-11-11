"""Tests for the monitor's MCP server: only the owner's token, and what each tool answers."""

import asyncio
import json
import unittest

import httpx
from starlette.testclient import TestClient

from app.config import MonitorConfig
from app.live import LiveState
from app.mcp_server import MonitorTools, OwnerTokenVerifier, create_mcp, window

RESOURCE = "https://sentia-mcp.example.com/mcp"


def config(**changes) -> MonitorConfig:
    values = dict(upstream_url="http://engine", database_url="postgresql://unused", auth_internal_url="http://auth",
                  auth_public_url="https://auth.example.com", public_url="https://monitor.example.com",
                  mcp_public_url="https://sentia-mcp.example.com", resident_model="qwen3:14b")
    values.update(changes)
    return MonitorConfig(**values)


def introspection(answers: dict[str, dict]) -> httpx.MockTransport:
    def answer(request: httpx.Request) -> httpx.Response:
        token = dict(item.split("=") for item in request.content.decode().split("&"))["token"]
        return httpx.Response(200, json=answers.get(token, {"active": False}))
    return httpx.MockTransport(answer)


OWNER = {"active": True, "sub": "owner", "client_id": "claude", "resource": RESOURCE, "exp": 4_000_000_000}


class FakeStore:
    def __init__(self) -> None:
        self.asked: list[tuple] = []

    def gpu_history(self, start, end):
        self.asked.append(("gpu_history", start, end))
        return {"bucket_seconds": 10, "events": [{"at": 1000.0, "model": "qwen3:14b", "event": "loaded", "vram_mb": 9800.0, "context": 8192}],
                "samples": [{"at": 1000.0, "gpu_util": 20.0, "gpu_util_max": 60.0, "vram_used_mb": 10000.0, "vram_total_mb": 12288.0, "power_w": 90.0, "temp_c": 55.0},
                            {"at": 1010.0, "gpu_util": 80.0, "gpu_util_max": 99.0, "vram_used_mb": 11000.0, "vram_total_mb": 12288.0, "power_w": 200.0, "temp_c": 71.0}]}

    def calls_between(self, *args):
        self.asked.append(("calls_between", *args))
        return [{"started_at": 1000.0, "finished_at": 1003.0, "app": "socia", "model": "qwen3:14b", "outcome": "ok", "wall_ms": 3000.04}]

    def history(self, start, end):
        return {"calls": [], "intervals": [], "samples": [], "events": [], "calls_total": 3,
                "groups": [{"app": "socia", "model": "qwen3:14b", "calls": 3, "problems": 0, "wall_sum": 9000.0,
                            "wait_sum": 0.0, "load_sum": 1500.0, "output_ms": 4000.0, "output_tokens": 160, "prompt_ms": 0, "prompt_tokens": 0}]}


class BrokenStore:
    def gpu_history(self, start, end):
        raise OSError("connection refused")


class Residency:
    state = "resident"


def live_state() -> LiveState:
    live = LiveState(clock=lambda: 1000.0)
    live.gpu_name = "NVIDIA GeForce RTX 4070"
    live.set_models([{"name": "qwen3:14b", "vram_mb": 9800.0, "size_mb": 9800.0, "context": 8192, "expires_at": None}])
    live.add_sample({"gpu_util": 40.0, "vram_used_mb": 10500.0, "vram_total_mb": 12288.0, "temp_c": 60.0, "power_w": 120.04})
    live.begin("socia", "/api/chat", {"model": "qwen3:14b"})
    return live


class VerifierTests(unittest.TestCase):
    def verify(self, answers: dict, token: str):
        return asyncio.run(OwnerTokenVerifier("http://auth", RESOURCE, introspection(answers)).verify_token(token))

    def test_accepts_the_owners_token_for_this_server(self):
        self.assertEqual(self.verify({"t": OWNER}, "t").subject, "owner")

    def test_refuses_everyone_else_another_servers_token_and_an_inactive_one(self):
        self.assertIsNone(self.verify({"t": {**OWNER, "sub": "someone"}}, "t"))
        self.assertIsNone(self.verify({"t": {**OWNER, "resource": "https://vita-mcp.example.com/mcp"}}, "t"))
        self.assertIsNone(self.verify({}, "t"))


class ToolTests(unittest.TestCase):
    def test_status_reads_the_live_gpu_models_and_calls(self):
        status = MonitorTools(config(), live_state(), None, Residency()).gpu_status()
        self.assertEqual(status["gpu"], "NVIDIA GeForce RTX 4070")
        self.assertEqual(status["now"]["gpu_util"], 40.0)
        self.assertEqual(status["now"]["vram_free_mb"], 1788.0)
        self.assertEqual(status["now"]["power_w"], 120.0)
        self.assertEqual(status["ollama"]["loaded_models"][0]["name"], "qwen3:14b")
        self.assertEqual(status["requests"]["in_flight"][0]["app"], "socia")
        self.assertEqual(status["resident_model"], {"model": "qwen3:14b", "state": "resident"})

    def test_history_summarises_the_stored_samples(self):
        store = FakeStore()
        found = asyncio.run(MonitorTools(config(), LiveState(), store).gpu_history(2, "2026-11-11T10:00:00Z"))
        self.assertEqual(found["summary"]["gpu_util_avg"], 50.0)
        self.assertEqual(found["summary"]["gpu_util_max"], 99.0)
        self.assertEqual(found["summary"]["vram_free_mb_min"], 1288.0)
        self.assertEqual(found["end"], "2026-11-11T10:00:00+00:00")
        self.assertEqual(store.asked[0][2] - store.asked[0][1], 7200)
        self.assertEqual(found["model_events"][0]["event"], "loaded")

    def test_calls_pass_their_filters_and_cap_the_limit(self):
        store = FakeStore()
        found = asyncio.run(MonitorTools(config(), LiveState(), store).model_calls(1, None, " Socia ", None, True, 9999))
        self.assertEqual(store.asked[0][3:], ("socia", None, True, 500))
        self.assertEqual(found["calls"][0]["wall_ms"], 3000.0)

    def test_usage_gives_rates_per_app_and_model(self):
        found = asyncio.run(MonitorTools(config(), LiveState(), FakeStore()).model_usage(24, None))
        group = found["by_app_and_model"][0]
        self.assertEqual((group["busy_seconds"], group["loading_seconds"], group["output_tokens_per_second"]), (9.0, 1.5, 40.0))
        self.assertIn("verdict", found["signals"])

    def test_explains_a_missing_or_unreadable_database_and_a_bad_window(self):
        with self.assertRaisesRegex(ValueError, "no database"):
            asyncio.run(MonitorTools(config(), LiveState(), None).gpu_history(1, None))
        with self.assertRaisesRegex(ValueError, "could not be read"):
            asyncio.run(MonitorTools(config(), LiveState(), BrokenStore()).gpu_history(1, None))
        with self.assertRaisesRegex(ValueError, "ISO 8601"):
            window(1, "yesterday")
        with self.assertRaisesRegex(ValueError, "more than 0"):
            window(0, None)
        start, end = window(10_000, None)
        self.assertEqual(end - start, 31 * 86400)


class ServerTests(unittest.TestCase):
    def client(self):
        mcp = create_mcp(config(), live_state(), FakeStore(), Residency(), introspection({"owner-token": OWNER}))
        return TestClient(mcp.streamable_http_app(), base_url="https://sentia-mcp.example.com")

    def call(self, client, token: str | None, method: str, params: dict | None = None) -> httpx.Response:
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return client.post("/mcp", headers=headers, content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}))

    def test_points_clients_to_sentias_sign_in(self):
        with self.client() as client:
            metadata = client.get("/.well-known/oauth-protected-resource/mcp").json()
            self.assertEqual(metadata["authorization_servers"], ["https://auth.example.com/"])
            self.assertEqual(self.call(client, None, "tools/list").status_code, 401)
            self.assertEqual(self.call(client, "someone-elses", "tools/list").status_code, 401)

    def test_the_owner_lists_and_calls_the_tools(self):
        with self.client() as client:
            listed = self.call(client, "owner-token", "tools/list").json()["result"]["tools"]
            self.assertEqual({tool["name"] for tool in listed},
                             {"get_gpu_status", "get_gpu_history", "list_model_calls", "get_model_usage", "get_ui_link"})
            reply = self.call(client, "owner-token", "tools/call", {"name": "get_gpu_status", "arguments": {}}).json()
            self.assertEqual(reply["result"]["structuredContent"]["now"]["gpu_util"], 40.0)


if __name__ == "__main__":
    unittest.main()
