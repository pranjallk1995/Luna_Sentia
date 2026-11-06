"""Signals for whether a batching server such as vLLM would help, from the recorded pattern.

These are rules of thumb, shown with their numbers so the reasoning can be checked:

- **Overlap**: the share of busy time with two or more requests in flight. Ollama
  runs one Qwen3-VL request at a time, so overlapping requests queue; vLLM would
  run them together. Little overlap means batching has nothing to batch.
- **Waiting**: the share of all call time spent waiting rather than computing.
- **VRAM headroom**: the least free VRAM seen. vLLM reserves a fixed share of the
  card at start-up and keeps it, so it needs room that stays free.
- **Model loads**: how often Ollama loaded a model and how long that took. vLLM
  keeps its model loaded, which removes this cost but holds the memory.
"""

from __future__ import annotations

from typing import Any

MIN_CALLS = 20
MIN_BUSY_SECONDS = 60.0


def busy_and_overlap(intervals: list[tuple[float, float]]) -> tuple[float, float, int]:
    """Seconds with at least one call in flight, seconds with two or more, and the most at once."""
    edges = sorted([(start, 1) for start, end in intervals if end > start] + [(end, -1) for start, end in intervals if end > start],
                   key=lambda edge: (edge[0], edge[1]))
    busy = overlap = 0.0
    depth = peak = 0
    last = None
    for at, step in edges:
        if last is not None:
            if depth >= 1:
                busy += at - last
            if depth >= 2:
                overlap += at - last
        depth += step
        peak = max(peak, depth)
        last = at
    return busy, overlap, peak


def share(part: float, whole: float) -> float | None:
    return part / whole if whole > 0 else None


def rate(tokens: float, ms: float) -> float | None:
    return tokens / (ms / 1000) if ms > 0 else None


def signals(calls: list[dict[str, Any]], intervals: list[dict[str, Any]], samples: list[dict[str, Any]],
            events: list[dict[str, Any]]) -> dict[str, Any]:
    done = [call for call in calls if call.get("outcome") == "ok" and call.get("wall_ms") is not None]
    busy, overlap, peak = busy_and_overlap([(row["started_at"], row["finished_at"]) for row in intervals if row.get("finished_at")])
    wall = sum(call["wall_ms"] for call in done)
    wait = sum(call.get("wait_ms") or 0 for call in done)
    cold = [call for call in done if (call.get("load_ms") or 0) >= 1000]
    writing = [call for call in done if call.get("output_tokens") and call.get("output_ms")]
    reading = [call for call in done if call.get("prompt_tokens") and call.get("prompt_ms")]
    headrooms = [s["vram_total_mb"] - s["vram_used_mb"] for s in samples if s.get("vram_total_mb") and s.get("vram_used_mb") is not None]
    busy_util = [s["gpu_util"] for s in samples if s.get("gpu_util") is not None and (s.get("in_flight") or s.get("busy_pct"))]

    overlap_share = share(overlap, busy)
    wait_share = share(wait, wall)
    headroom = min(headrooms) if headrooms else None
    loads = sum(1 for event in events if event.get("event") == "loaded")

    findings = []

    def finding(key: str, label: str, value: Any, leaning: str, note: str) -> None:
        findings.append({"key": key, "label": label, "value": value, "leaning": leaning, "note": note})

    if overlap_share is not None:
        leaning = "vllm" if overlap_share >= 0.25 else "ollama" if overlap_share < 0.05 else "neutral"
        finding("overlap", "Busy time with requests overlapping", overlap_share, leaning,
                "Overlapping requests queue in Ollama; vLLM would run them together."
                if leaning == "vllm" else "Requests rarely overlap, so batching would have little to batch."
                if leaning == "ollama" else "Some overlap: batching would help at the busiest moments.")
    if wait_share is not None:
        leaning = "vllm" if wait_share >= 0.3 else "ollama" if wait_share < 0.1 else "neutral"
        finding("wait", "Call time spent waiting", wait_share, leaning,
                "Much of each call is queueing, which batching removes." if leaning == "vllm"
                else "Calls spend almost all their time computing, not queueing." if leaning == "ollama"
                else "Some queueing, mostly at busy moments.")
    if headroom is not None:
        leaning = "ollama" if headroom < 2048 else "vllm" if headroom >= 4096 else "neutral"
        finding("headroom", "Least free VRAM", headroom, leaning,
                "Too little room stays free for vLLM's fixed reservation without starving SigLIP, Docling, or CLAP."
                if leaning == "ollama" else "Enough VRAM stays free for vLLM to reserve its share."
                if leaning == "vllm" else "Tight: vLLM would fit only with a small reservation.")
    if done:
        cold_ms = sum(call["load_ms"] for call in cold)
        finding("loads", "Calls that waited for a model load", share(len(cold), len(done)),
                "vllm" if len(cold) / len(done) >= 0.1 else "neutral",
                f"{len(cold)} calls spent {cold_ms / 1000:.0f} s loading models ({loads} loads seen). "
                "vLLM keeps its model loaded, at the cost of holding the memory.")

    enough = len(done) >= MIN_CALLS and busy >= MIN_BUSY_SECONDS
    leaning = {f["key"]: f["leaning"] for f in findings}
    queueing = [leaning.get("overlap"), leaning.get("wait")]
    if not enough:
        verdict = "Not enough calls yet. Leave the monitor running through a few real imports before deciding."
    elif leaning.get("headroom") == "ollama":
        verdict = "Stay on Ollama for now: too little VRAM stays free for vLLM's fixed reservation."
    elif queueing.count("vllm") == 2:
        verdict = "Worth trying vLLM: requests overlap and queue, and there is room for its reservation."
    elif queueing.count("ollama") == 2:
        verdict = "Stay on Ollama: requests rarely overlap, so batching would have little to batch."
    else:
        verdict = "Mixed: batching would help at the busiest moments. Watch a busier stretch before deciding."

    return {
        "enough": enough,
        "verdict": verdict,
        "findings": findings,
        "calls": len(calls),
        "problems": sum(1 for call in calls if call.get("outcome") != "ok"),
        "busy_seconds": busy,
        "overlap_seconds": overlap,
        "peak_in_flight": peak,
        "output_rate": rate(sum(c["output_tokens"] for c in writing), sum(c["output_ms"] for c in writing)),
        "prompt_rate": rate(sum(c["prompt_tokens"] for c in reading), sum(c["prompt_ms"] for c in reading)),
        "gpu_util_busy": sum(busy_util) / len(busy_util) if busy_util else None,
        "model_loads": loads,
    }
