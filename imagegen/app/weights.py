"""The image model's weights: downloaded once from Hugging Face and quantized to fit beside the rest of the GPU.

- text encoder: Qwen3-VL 8B, 17 GB in bf16, kept as 4-bit NF4 (bitsandbytes): 6 GB;
- diffusion model: 14 GB in bf16, kept as int8 weights (torchao): 6.7 GB;
- VAE, processor, scheduler and configs as they are: 0.7 GB.

Each bf16 part is deleted once its quantized copy is saved, so they hold 13.4 GB on disk after the
first start (which needs 31 GB free while it runs). A part already prepared is skipped.

    python -m app.weights     # what the server runs before taking pictures
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

ROOT = Path(os.getenv("IMAGEGEN_DATA", "/data"))
REPOSITORY = "Qwen/Qwen-Image-2.1-Turbo"
# The revision the quantizing and the passes were tested with.
REVISION = "fc13ba1986fcb905854dc8de2f52eee395f49b5b"
PIPELINE = ROOT / "weights" / "Qwen-Image-2.1-Turbo"
TEXT_ENCODER = ROOT / "weights" / "quantized" / "text_encoder_nf4"
TRANSFORMER = ROOT / "weights" / "quantized" / "transformer_int8wo"
CONFIGS = ["model_index.json", "LICENSE", "processor/*", "scheduler/*", "vae/*",
           "text_encoder/config.json", "text_encoder/generation_config.json", "transformer/config.json"]


def ready() -> bool:
    return (PIPELINE / "model_index.json").exists() and (PIPELINE / "vae").exists() \
        and (TEXT_ENCODER / "config.json").exists() and (TRANSFORMER / "config.json").exists()


def files() -> list[dict]:
    """What is on disk, for the monitor's list of models."""
    parts = [("text encoder", "Qwen3-VL 8B, NF4", TEXT_ENCODER), ("diffusion model", "Qwen-Image 2.1 Turbo, int8", TRANSFORMER),
             ("VAE", "Qwen-Image 2.1 VAE", PIPELINE / "vae")]
    return [{"kind": kind, "name": name, "size_mb": size(path) / 2**20 or None} for kind, name, path in parts if path.exists()]


def size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file()) if path.exists() else 0


def say(**fields) -> None:
    print(json.dumps({"stage": "prepare", **fields}), flush=True)


def download(patterns: list[str]) -> None:
    from huggingface_hub import snapshot_download

    snapshot_download(REPOSITORY, revision=REVISION, local_dir=PIPELINE, allow_patterns=patterns)


def save(model, target: Path, **options) -> None:
    """Save beside the target and rename, so a part cut short is never taken for a finished one."""
    partial = target.with_name(target.name + ".partial")
    shutil.rmtree(partial, ignore_errors=True)
    model.save_pretrained(partial, **options)
    partial.rename(target)


def prepare_text_encoder() -> None:
    import torch
    from transformers import BitsAndBytesConfig, Qwen3VLForConditionalGeneration

    start = time.time()
    say(part="text encoder", doing="downloading 17 GB")
    download(CONFIGS + ["text_encoder/*"])
    say(part="text encoder", doing="quantizing to 4-bit")
    config = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=torch.bfloat16,
                                bnb_4bit_use_double_quant=True)
    model = Qwen3VLForConditionalGeneration.from_pretrained(PIPELINE / "text_encoder", quantization_config=config,
                                                            dtype=torch.bfloat16, device_map="cuda")
    save(model, TEXT_ENCODER)
    (PIPELINE / "text_encoder" / "model.safetensors").unlink(missing_ok=True)
    say(part="text encoder", done_seconds=round(time.time() - start))


def prepare_transformer() -> None:
    import torch
    from diffusers import QwenImage21Transformer2DModel, TorchAoConfig
    from torchao.quantization import Int8WeightOnlyConfig

    start = time.time()
    say(part="diffusion model", doing="downloading 14 GB")
    download(CONFIGS + ["transformer/*"])
    say(part="diffusion model", doing="quantizing to int8")
    # On the CPU: in bf16 it is more than the GPU holds.
    model = QwenImage21Transformer2DModel.from_pretrained(PIPELINE / "transformer", dtype=torch.bfloat16,
                                                          quantization_config=TorchAoConfig(Int8WeightOnlyConfig()))
    # torchao's int8 tensors are saved with torch's own format; safetensors cannot hold them.
    save(model, TRANSFORMER, safe_serialization=False)
    for shard in (PIPELINE / "transformer").glob("diffusion_pytorch_model*"):
        shard.unlink()
    say(part="diffusion model", done_seconds=round(time.time() - start))


def prepare() -> None:
    if not (TEXT_ENCODER / "config.json").exists():
        prepare_text_encoder()
    if not (TRANSFORMER / "config.json").exists():
        prepare_transformer()
    if not ready():
        download(CONFIGS)
    say(done=True, gb_on_disk=round((size(PIPELINE) + size(TEXT_ENCODER) + size(TRANSFORMER)) / 2**30, 1))


if __name__ == "__main__":
    prepare()
