# Luna Sentia's pictures: Qwen-Image 2.1 Turbo with diffusers, on PyTorch for CUDA 13 (RTX 50 series too).
# The weights are not in the image: the first start downloads and quantizes them into the volume
# (app/weights.py).
FROM python:3.12-slim

WORKDIR /workspace/imagegen

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /bin/uv
ENV UV_LINK_MODE=copy PYTHONUNBUFFERED=1 UV_HTTP_TIMEOUT=300 HF_HUB_DISABLE_TELEMETRY=1

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system torch==2.14.1 torchvision==0.29.1 --index-url https://download.pytorch.org/whl/cu130 \
    && uv pip install --system -r requirements.txt

COPY app ./app
ARG LUNA_VERSION=
ENV LUNA_VERSION=$LUNA_VERSION

EXPOSE 8000

CMD ["python", "-m", "app.server"]
