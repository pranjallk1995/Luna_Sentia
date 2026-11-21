import asyncio
import base64
import json
import tempfile
import time
import unittest
from pathlib import Path

import httpx
from starlette.testclient import TestClient

from app.server import Pictures, create_app

PNG = b"\x89PNG\r\n\x1a\nfake"


class FakeOllama:
    """Holds two models until asked to unload them; the embedding one refuses generate, as Ollama's do."""

    def __init__(self):
        self.models = {"huihui_ai/qwen3-abliterated:8b", "qwen3-embedding:0.6b"}
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": name} for name in sorted(self.models)]})
        body = json.loads(request.content)
        if request.url.path == "/api/generate" and "embedding" in body["model"]:
            return httpx.Response(400, json={"error": "does not support generate"})
        self.models.discard(body["model"])
        return httpx.Response(200, json={})


class FakePasses:
    """Stands in for the read and draw processes: writes the picture, or fails or hangs when told to."""

    def __init__(self, fail: str = "", hang: bool = False):
        self.fail, self.hang, self.runs, self.jobs = fail, hang, [], []

    async def __call__(self, arguments, picture):
        self.runs.append(arguments[1] if len(arguments) > 1 else arguments[0])
        if arguments[0] != "app.passes":
            return 0
        job = json.loads(Path(arguments[2]).read_text())
        self.jobs.append(job)
        if self.hang:
            while picture.status != "cancelled":
                await asyncio.sleep(0.01)
            return -15
        if arguments[1] == self.fail:
            return 1
        if arguments[1] == "draw":
            Path(job["out"]).write_bytes(PNG)
        return 0


def wait_for(client, picture_id, *statuses):
    for _ in range(300):
        reply = client.get(f"/pictures/{picture_id}").json()
        if reply["status"] in statuses:
            return reply
        time.sleep(0.01)
    raise AssertionError(f"still {reply['status']}")


class PicturesTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.ollama = FakeOllama()

    def client(self, passes, ready=True):
        pictures = Pictures(Path(self.folder.name), "http://ollama", runner=passes,
                            transport=httpx.MockTransport(self.ollama), is_ready=lambda: ready)
        return TestClient(create_app(pictures)), pictures

    def test_a_picture_empties_ollama_then_reads_then_draws(self):
        passes = FakePasses()
        client, _ = self.client(passes)
        with client:
            reference = base64.b64encode(b"her face").decode()
            created = client.post("/pictures", json={"prompt": "a selfie", "references": [reference], "seed": 7})
            self.assertEqual(created.status_code, 202)
            done = wait_for(client, created.json()["id"], "done", "failed")
            self.assertEqual(done["status"], "done")
            self.assertEqual(passes.runs, ["read", "draw"])
            self.assertEqual(self.ollama.models, set())
            job = passes.jobs[0]
            self.assertEqual((job["prompt"], job["seed"], job["resolution"]), ("a selfie", 7, 1024))
            self.assertEqual(Path(job["refs"][0]).name, "reference1")
            image = client.get(f"/pictures/{done['id']}/image")
            self.assertEqual(image.content, PNG)
            # Fetched once, then gone.
            self.assertEqual(client.get(f"/pictures/{done['id']}").status_code, 404)

    def test_a_failed_pass_says_which(self):
        client, _ = self.client(FakePasses(fail="read"))
        with client:
            picture = client.post("/pictures", json={"prompt": "a cat"}).json()
            failed = wait_for(client, picture["id"], "failed", "done")
            self.assertIn("read pass", failed["error"])
            self.assertEqual(client.get(f"/pictures/{picture['id']}/image").status_code, 404)

    def test_a_cancelled_picture_stops_and_the_service_is_free_again(self):
        client, pictures = self.client(FakePasses(hang=True))
        with client:
            picture = client.post("/pictures", json={"prompt": "a cat"}).json()
            wait_for(client, picture["id"], "reading")
            self.assertTrue(client.get("/status").json()["busy"])
            client.delete(f"/pictures/{picture['id']}")
            wait_for(client, picture["id"], "cancelled")
            for _ in range(100):
                if not pictures.busy():
                    break
                time.sleep(0.01)
            self.assertFalse(client.get("/status").json()["busy"])

    def test_refuses_pictures_until_the_weights_are_ready(self):
        client, _ = self.client(FakePasses(), ready=False)
        with client:
            reply = client.post("/pictures", json={"prompt": "a cat"})
            self.assertEqual(reply.status_code, 503)
            self.assertIn("prepared", reply.json()["error"])

    def test_rejects_bad_requests(self):
        client, _ = self.client(FakePasses())
        with client:
            self.assertEqual(client.post("/pictures", json={"prompt": ""}).status_code, 400)
            self.assertEqual(client.post("/pictures", json={"prompt": "x", "references": ["not base64!"]}).status_code, 400)
            self.assertEqual(client.post("/pictures", json={"prompt": "x", "resolution": 9000}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
