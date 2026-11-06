"""What one model call asked for, how long each part of it took, and who sent it.

Ollama reports its own timings at the end of every reply (the last line of a
stream, or the end of a single JSON reply), in nanoseconds:

- ``load_duration``: getting the model ready, including loading it into VRAM.
- ``prompt_eval_duration`` and ``prompt_eval_count``: reading the input. For a
  vision model this includes encoding the images.
- ``eval_duration`` and ``eval_count``: writing the reply, one token at a time.

The monitor also times the whole request itself. What its wall time has beyond
Ollama's parts is waiting: behind other requests for the same model, plus
request decoding and image preparation.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

# The endpoints that run a model; everything else (tags, ps, pull) passes through unrecorded.
MODEL_ENDPOINTS = frozenset(
    {"/api/chat", "/api/generate", "/api/embed", "/api/embeddings", "/v1/chat/completions", "/v1/completions", "/v1/embeddings"}
)
EMBEDDING_ENDPOINTS = frozenset({"/api/embed", "/api/embeddings", "/v1/embeddings"})
# Inside JSON a quote in a string is escaped, so these keys only match Ollama's own fields.
TIMING = re.compile(rb'"(total_duration|load_duration|prompt_eval_count|prompt_eval_duration|eval_count|eval_duration|prompt_tokens|completion_tokens)":\s*(\d+)')
APP_NAME = re.compile(r"luna[_-]?(visia|vita|lumia|fiora|lexia|vibia|unia|sentia)", re.IGNORECASE)
NS_PER_MS = 1_000_000


@dataclass
class Call:
    id: int
    started_at: float  # epoch seconds
    app: str
    endpoint: str
    model: str
    streamed: bool = False
    images: int = 0
    num_ctx: int | None = None
    structured: bool = False
    request_bytes: int = 0
    # Requests for any model already in flight when this one arrived.
    ahead: int = 0
    finished_at: float | None = None
    status: int | None = None
    outcome: str = "running"  # running, ok, failed, cancelled
    response_bytes: int = 0
    wall_ms: float | None = None
    ttfb_ms: float | None = None
    wait_ms: float | None = None
    load_ms: float | None = None
    prompt_ms: float | None = None
    output_ms: float | None = None
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    def view(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("extra")
        return data


def count_images(payload: dict[str, Any]) -> int:
    """Images in an Ollama request, or in an OpenAI-style one as image_url parts."""
    total = len(payload.get("images") or [])
    for message in payload.get("messages") or []:
        if not isinstance(message, dict):
            continue
        total += len(message.get("images") or [])
        content = message.get("content")
        if isinstance(content, list):
            total += sum(1 for part in content if isinstance(part, dict) and part.get("type") in {"image_url", "image"})
    return total


def describe_request(endpoint: str, body: bytes) -> dict[str, Any]:
    """The parts of a request that explain its cost; never the prompt or images themselves."""
    try:
        payload = json.loads(body) if body else {}
    except (ValueError, UnicodeDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    options = payload.get("options") if isinstance(payload.get("options"), dict) else {}
    num_ctx = options.get("num_ctx")
    # Ollama streams by default; the OpenAI-style endpoints do not.
    default_stream = endpoint.startswith("/api/") and endpoint not in EMBEDDING_ENDPOINTS
    return {
        "model": str(payload.get("model") or "unknown"),
        "streamed": bool(payload.get("stream", default_stream)),
        "images": count_images(payload),
        "num_ctx": int(num_ctx) if isinstance(num_ctx, (int, float)) else None,
        "structured": bool(payload.get("format") or payload.get("response_format")),
        "request_bytes": len(body),
    }


def timings(tail: bytes) -> dict[str, int]:
    """Ollama's timing fields from the end of a reply; the last value of each wins."""
    found: dict[str, int] = {}
    for key, value in TIMING.findall(tail):
        found[key.decode()] = int(value)
    return found


def apply_timings(call: Call, found: dict[str, int]) -> None:
    """Split the call's wall time into waiting, loading, reading the input, and writing."""
    load = found.get("load_duration", 0) / NS_PER_MS
    prompt = found.get("prompt_eval_duration")
    output = found.get("eval_duration")
    total = found.get("total_duration")
    prompt_ms = prompt / NS_PER_MS if prompt is not None else None
    output_ms = output / NS_PER_MS if output is not None else None
    # Embeddings report only a total: what is not loading is reading the input.
    if prompt_ms is None and total is not None:
        prompt_ms = max(total / NS_PER_MS - load - (output_ms or 0), 0.0)
    call.load_ms = load if found else None
    call.prompt_ms = prompt_ms
    call.output_ms = output_ms
    call.prompt_tokens = found.get("prompt_eval_count", found.get("prompt_tokens"))
    call.output_tokens = found.get("eval_count", found.get("completion_tokens"))
    if call.wall_ms is not None and found:
        call.wait_ms = max(call.wall_ms - load - (prompt_ms or 0) - (output_ms or 0), 0.0)


def app_name(hostname: str | None, address: str) -> str:
    """A friendly app name from the calling container's DNS name, such as luna_lexia-mcp-1."""
    match = APP_NAME.search(hostname or "")
    if match:
        return match.group(1).lower()
    if hostname:
        return hostname.split(".")[0]
    return address
