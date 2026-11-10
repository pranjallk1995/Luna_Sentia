# ComfyUI with PyTorch for CUDA 13, which supports the RTX 50 series (Blackwell).
# gcc: Triton compiles the int8 models' GPU kernels on first use and needs a C compiler.
# Luna Socia sends it Qwen-Image 2.1 workflows over its API; the models live in a volume
# filled by the comfyui-pull service (pull-models.sh).
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /bin/uv
ENV UV_LINK_MODE=copy PYTHONUNBUFFERED=1

ARG COMFYUI_VERSION=v0.42.1
RUN git clone --depth 1 --branch "$COMFYUI_VERSION" https://github.com/Comfy-Org/ComfyUI.git /opt/comfyui
WORKDIR /opt/comfyui

# PyTorch first, from its CUDA index; ComfyUI's own requirements then leave it in place.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system torch==2.14.1 torchvision --index-url https://download.pytorch.org/whl/cu130 \
    && uv pip install --system -r requirements.txt

ARG LUNA_VERSION=
ENV LUNA_VERSION=$LUNA_VERSION

EXPOSE 8188

# --disable-auto-launch: no browser in a container. Models stay in RAM between images
# only while nothing else needs it; Socia asks ComfyUI to free them after each image.
CMD ["python", "main.py", "--listen", "0.0.0.0", "--port", "8188", "--disable-auto-launch"]
