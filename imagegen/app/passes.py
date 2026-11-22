"""Qwen-Image 2.1 Turbo in two processes, each run by the server and gone when it exits, so the GPU and the RAM
it used come back whole:

- read: the text encoder (Qwen3-VL 8B, 4-bit) reads the prompt and the reference pictures into a small file;
- draw: the diffusion model (int8) and the VAE turn that file, and the references' latents, into the picture.

Apart, neither needs more than about 8 GB of the GPU; together they would need about 13.

    python -m app.passes read|draw <job.json>
"""

from __future__ import annotations

import json
import resource
import sys
import threading
import time

import torch
from diffusers import QwenImage21Pipeline
from diffusers.pipelines.qwenimage21.pipeline_qwenimage21 import calculate_dimensions
from PIL import Image

from app.weights import PIPELINE, TEXT_ENCODER, TRANSFORMER


def report(**fields) -> None:
    """One JSON line on stdout, which the server reads and logs."""
    print(json.dumps(fields), flush=True)


def finished(stage: str, start: float) -> None:
    report(stage=stage, seconds=round(time.time() - start, 1),
           peak_gpu_gib=round(torch.cuda.max_memory_reserved() / 2**30, 2),
           peak_ram_gib=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20, 2))


def watch_memory(stage: str) -> None:
    """Say every second how much of the GPU this pass holds, for the monitor's list of models."""
    def run() -> None:
        while True:
            report(stage=stage, vram_mb=round(torch.cuda.memory_reserved() / 2**20))
            time.sleep(1)

    threading.Thread(target=run, daemon=True).start()


def references(pipe: QwenImage21Pipeline, job: dict) -> list[Image.Image]:
    """The references at about resolution x resolution pixels, keeping their shape, as the text encoder and
    the VAE both see them; the picture takes the first one's shape."""
    images = []
    for path in job["refs"]:
        image = Image.open(path).convert("RGBA")
        width, height, _ = calculate_dimensions(job["resolution"] ** 2, image.size[0] / image.size[1])
        images.append(pipe.image_processor.resize(image, width=width, height=height))
    return images


def read(job: dict) -> None:
    from transformers import Qwen3VLForConditionalGeneration

    start = time.time()
    watch_memory("read")
    encoder = Qwen3VLForConditionalGeneration.from_pretrained(TEXT_ENCODER, device_map="cuda")
    pipe = QwenImage21Pipeline.from_pretrained(PIPELINE, text_encoder=encoder, transformer=None, vae=None,
                                               dtype=torch.bfloat16)
    images = references(pipe, job)
    with torch.no_grad():
        embeds, mask, pad = pipe.encode_prompt(prompt=job["prompt"], image=images or None, device="cuda")
    torch.save({"embeds": embeds.cpu(), "mask": None if mask is None else mask.cpu(), "pad": pad.cpu()}, job["read"])
    finished("read", start)


def draw(job: dict) -> None:
    from diffusers import QwenImage21Transformer2DModel

    start = time.time()
    watch_memory("draw")
    transformer = QwenImage21Transformer2DModel.from_pretrained(TRANSFORMER, dtype=torch.bfloat16,
                                                                use_safetensors=False)
    pipe = QwenImage21Pipeline.from_pretrained(PIPELINE, text_encoder=None, transformer=transformer,
                                               dtype=torch.bfloat16)
    pipe.to("cuda")
    # Decoding a 1024-pixel picture in one piece needs about 5 GB more of the GPU; in tiles, a few hundred MB.
    pipe.vae.enable_tiling()
    saved = torch.load(job["read"])
    embeds = saved["embeds"].to("cuda", torch.bfloat16)
    mask = None if saved["mask"] is None else saved["mask"].to("cuda")
    pad = saved["pad"].to("cuda")
    # The pipeline takes the prompt's embeddings back but not which of them are the references' tokens:
    # hand it the read pass's three outputs as they were.
    pipe.encode_prompt = lambda **kwargs: (embeds, mask, pad)
    images = references(pipe, job)
    report(stage="loaded", seconds=round(time.time() - start, 1))

    def step(pipeline, index, timestep, kwargs):
        report(stage="step", step=index + 1, seconds=round(time.time() - start, 1))
        return kwargs

    image = pipe(prompt_embeds=embeds, prompt_embeds_mask=mask, image=images or None,
                 output_resolution=job["resolution"], generator=torch.Generator("cpu").manual_seed(job["seed"]),
                 use_kv_cache=False, callback_on_step_end=step).images[0]
    image.save(job["out"])
    finished("draw", start)


if __name__ == "__main__":
    {"read": read, "draw": draw}[sys.argv[1]](json.load(open(sys.argv[2])))
