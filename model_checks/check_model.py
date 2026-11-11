"""Smoke-test a model before it replaces another in SENTIA_MODELS: what every Luna app needs of it.

usage: python check_model.py <model> [--context 16384] [--resident huihui_ai/qwen3-abliterated:14b] [--chat]
Runs inside the monitor container (standard library only), so its calls go through the
monitor like the apps' calls and show on its page as "model-check":

    docker cp model_checks/check_model.py luna_sentia-monitor-1:/tmp/
    docker exec luna_sentia-monitor-1 python /tmp/check_model.py qwen3.5:9b

It checks that the model:
- can see images, call tools, and turn thinking off (Ollama's listed capabilities);
- reads a picture and answers in a JSON schema with think: false, as Visia, Vibia and
  Lexia call it, without returning any reasoning;
- calls a tool when asked, as Luna Socia's create_image needs;
- fits on the GPU at the apps' context (100% GPU in /api/ps);
- reports whether the resident chat model is still loaded afterwards.
With --chat (Socia's chat model, which never sees a picture: a vision model describes
photos for it), the image checks are skipped and the JSON check uses text.
Exits 1 if any check fails. It says nothing about the quality of answers; run each app's
model_evaluations for that.
"""

import argparse
import base64
import json
import os
import struct
import sys
import urllib.request
import zlib

parser = argparse.ArgumentParser()
parser.add_argument("model")
parser.add_argument("--context", type=int, default=16384, help="num_ctx the apps ask for (Socia: 8192)")
parser.add_argument("--resident", default="huihui_ai/qwen3-abliterated:14b", help="the model that should stay loaded ('' to skip)")
parser.add_argument("--url", default="http://luna-sentia-ollama:11434", help="Ollama, through the monitor")
parser.add_argument("--chat", action="store_true", help="a text-only chat model (Socia's): skip the image checks")
args = parser.parse_args()
ENGINE = os.getenv("MONITOR_OLLAMA_URL", args.url).rstrip("/")
failures: list[str] = []


def call(path: str, body: dict | None = None, base: str = args.url) -> dict:
    request = urllib.request.Request(
        f"{base.rstrip('/')}{path}", data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "X-Luna-App": "model-check"},
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        return json.loads(response.read())


def report(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f": {detail}" if detail else ""))
    if not ok:
        failures.append(name)


def red_square_png(size: int = 96) -> bytes:
    """A red square on white, drawn without any imaging library."""
    rows = b"".join(
        b"\x00" + b"".join(b"\xd0\x20\x20" if size // 4 <= x < 3 * size // 4 and size // 4 <= y < 3 * size // 4
                           else b"\xff\xff\xff" for x in range(size))
        for y in range(size)
    )
    chunk = lambda kind, data: struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))  # noqa: E731
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def loaded() -> dict:
    return {m["name"]: m for m in call("/api/ps", base=ENGINE).get("models") or []}


resident_before = bool(args.resident) and args.resident in loaded()
show = call("/api/show", {"model": args.model})
capabilities = set(show.get("capabilities") or [])
if args.chat:
    print(f"SKIP  vision: a chat model ({', '.join(sorted(capabilities))})")
else:
    report("vision", "vision" in capabilities, ", ".join(sorted(capabilities)))
report("tools", "tools" in capabilities)

schema = {"type": "object", "properties": {"color": {"type": "string"}, "shape": {"type": "string"},
                                           "tags": {"type": "array", "items": {"type": "string"}}},
          "required": ["color", "shape", "tags"]}
reply = call("/api/chat", {
    "model": args.model, "stream": False, "think": False, "format": schema,
    "options": {"temperature": 0, "num_ctx": args.context},
    "messages": [{"role": "user", "content": "Describe a red square: its color, its shape, and 3 tags."} if args.chat else
                 {"role": "user", "content": "Describe the main shape in this image: its color, its shape, and 3 tags.",
                  "images": [base64.b64encode(red_square_png()).decode()]}],
})
message = reply.get("message") or {}
try:
    answer = json.loads(message.get("content") or "")
    report("JSON schema", all(key in answer for key in schema["required"]), json.dumps(answer))
    if not args.chat:
        report("sees the image", "red" in str(answer.get("color", "")).lower() and "square" in json.dumps(answer).lower())
except json.JSONDecodeError:
    report("JSON schema", False, repr(message.get("content", ""))[:200])
report("thinking off", not message.get("thinking") and "<think>" not in (message.get("content") or ""),
       f"{len(message.get('thinking') or '')} characters of thinking")
if reply.get("eval_duration"):
    print(f"      speed: {reply['eval_count'] / reply['eval_duration'] * 1e9:.0f} tokens/s, "
          f"load {reply.get('load_duration', 0) / 1e9:.1f} s")

tool = {"type": "function", "function": {
    "name": "create_image", "description": "Take a picture to send.",
    "parameters": {"type": "object", "properties": {"description": {"type": "string"}}, "required": ["description"]}}}
reply = call("/api/chat", {
    "model": args.model, "stream": False, "think": False, "tools": [tool],
    "options": {"temperature": 0, "num_ctx": args.context},
    "messages": [{"role": "user", "content": "Send me a picture of a sunset over the sea."}],
})
calls = (reply.get("message") or {}).get("tool_calls") or []
report("tool call", any((c.get("function") or {}).get("name") == "create_image" for c in calls),
       json.dumps(calls)[:200])

model = loaded().get(args.model)
if model:
    share = model.get("size_vram", 0) / max(1, model.get("size", 1))
    report("fits the GPU", share >= 0.999, f"{share:.0%} of {model['size'] / 2**30:.1f} GB on the GPU at "
                                            f"{model.get('context_length', args.context)} context")
else:
    report("fits the GPU", False, "not loaded after the calls")
if args.resident:
    still = args.resident in loaded()
    detail = "still loaded" if still else ("pushed out; it reloads on Socia's next message" if resident_before
                                           else "was not loaded before the check")
    # Informational: on a 12 GB GPU a vision model and the resident one do not both fit,
    # so the resident model is expected to leave during a batch and return afterwards.
    print(f"INFO  resident {args.resident}: {detail}")

print(f"\n{len(failures)} check(s) failed" + (f": {', '.join(failures)}" if failures else ""))
sys.exit(1 if failures else 0)
