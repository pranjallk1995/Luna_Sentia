FROM python:3.12-slim

WORKDIR /workspace/monitor

# uv installs the requirements; its cache mount reuses downloads across rebuilds.
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /bin/uv
ENV UV_LINK_MODE=copy PYTHONUNBUFFERED=1

COPY requirements.txt .

RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system -r requirements.txt

COPY app ./app
# The release, passed in when GitHub Actions builds the image; blank for local builds.
ARG LUNA_VERSION=
ENV LUNA_VERSION=$LUNA_VERSION

# 11434: the Ollama address the apps call; 8000: the monitor's page; 8188: ComfyUI's page, owner only;
# 8001: the owner's MCP server.
EXPOSE 11434 8000 8001 8188

CMD ["python", "-m", "app.main"]
