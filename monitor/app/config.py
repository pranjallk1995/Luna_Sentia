"""Settings for Luna Sentia's model monitor, read from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True)
class MonitorConfig:
    # The release this image holds, from the git tag; blank for local builds.
    VERSION: ClassVar[str] = os.getenv("LUNA_VERSION", "").strip()
    # The name the footers show.
    BRAND: ClassVar[str] = f"Luna Sentia · v{VERSION}" if VERSION else "Luna Sentia"
    # The apps call Ollama here; the monitor passes each request on to the real server.
    PROXY_PORT: ClassVar[int] = 11434
    UI_PORT: ClassVar[int] = 8000
    # The owner's MCP server: the GPU and the model calls, read only (mcp_server.py).
    MCP_PORT: ClassVar[int] = 8001
    # Header the auth service reads a luna_sso token from, and the owner's fixed account id.
    SESSION_HEADER: ClassVar[str] = "X-Luna-Session"
    OWNER_ID: ClassVar[str] = "owner"
    COOKIE_NAME: ClassVar[str] = "luna_sso"
    SESSION_RECHECK_SECONDS: ClassVar[float] = 60.0
    # How often each thing is measured, and how much of it the live page keeps.
    SAMPLE_SECONDS: ClassVar[float] = 1.0
    MODELS_SECONDS: ClassVar[float] = 2.0
    # How often the apps are asked about the models they run themselves, outside Ollama.
    APP_MODELS_SECONDS: ClassVar[float] = 10.0
    # Ollama's installed models, the image model's files, and which apps called which model.
    INVENTORY_SECONDS: ClassVar[float] = 30.0
    # The apps with models of their own: Visia's SigLIP 2, Lexia's Docling, Vibia's CLAP,
    # and Socia's Whisper and Kokoro (on the CPU).
    APP_BACKENDS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("visia", "http://luna-visia-mcp:8000"),
        ("lexia", "http://luna-lexia-mcp:8000"),
        ("vibia", "http://luna-vibia-mcp:8000"),
        ("socia", "http://luna-socia-ui:8501"),
    )
    LIVE_SAMPLES: ClassVar[int] = 600  # ten minutes of one-second samples
    LIVE_CALLS: ClassVar[int] = 300
    # Samples are stored as five-second summaries; rows are written in batches.
    STORE_BUCKET_SECONDS: ClassVar[int] = 5
    FLUSH_SECONDS: ClassVar[float] = 10.0
    PURGE_SECONDS: ClassVar[float] = 3600.0
    # Rows kept in memory while the database is unreachable, before the oldest are dropped.
    PENDING_LIMIT: ClassVar[int] = 20_000
    # Only the end of a reply is kept, which is where Ollama puts its timings.
    REPLY_TAIL_BYTES: ClassVar[int] = 16_384
    CONNECT_TIMEOUT_SECONDS: ClassVar[float] = 10.0

    upstream_url: str
    database_url: str
    auth_internal_url: str
    auth_public_url: str
    public_url: str
    # Ollama's OLLAMA_NUM_PARALLEL: requests one model runs at once; the rest wait.
    parallel: int = 1
    sample_days: int = 30
    call_days: int = 365
    # The most the monitor's database may take on disk; past it the oldest rows go. 0: no limit.
    max_gb: float = 10.0
    # Each app's private API token (LUNA<APP>_API_TOKEN), to ask it about its own models.
    app_tokens: tuple[tuple[str, str, str], ...] = ()
    # The models that live on the GPU (comma-separated), each at the context its app asks for
    # (a different context would make Ollama load it again): one context for all, or one per
    # model in the same order. Blank turns residency off.
    resident_model: str = ""
    resident_context: str = "8192"
    # How long any other model stays loaded after its last request.
    guest_keep_alive: str = "2m"
    # Quiet seconds before the resident model is loaded back.
    resident_idle_seconds: float = 30.0
    # Sentia's image service, asked whether it is making a picture before the resident models come back.
    imagegen_url: str = ""
    # Where MCP clients reach that server: SENTIA_MCP_PUBLIC_URL, with /mcp after it.
    mcp_public_url: str = "http://localhost:18003"
    # The host's Docker volumes, mounted read-only, to measure each app's disk; blank: databases only.
    volumes_dir: str = ""

    @classmethod
    def from_env(cls) -> MonitorConfig:
        def required(name: str) -> str:
            value = os.getenv(name, "").strip()
            if not value:
                raise RuntimeError(f"{name} is required.")
            return value

        return cls(
            upstream_url=os.getenv("MONITOR_OLLAMA_URL", "http://luna-sentia-ollama-engine:11434").rstrip("/"),
            database_url=required("DATABASE_URL"),
            auth_internal_url=os.getenv("SENTIA_AUTH_INTERNAL_URL", "http://luna-sentia-auth:8000").rstrip("/"),
            auth_public_url=required("SENTIA_AUTH_PUBLIC_URL").rstrip("/"),
            public_url=required("SENTIA_MONITOR_PUBLIC_URL").rstrip("/"),
            parallel=max(int(os.getenv("OLLAMA_NUM_PARALLEL", "1") or 1), 1),
            sample_days=int(os.getenv("MONITOR_SAMPLE_DAYS", "30")),
            call_days=int(os.getenv("MONITOR_CALL_DAYS", "365")),
            max_gb=float(os.getenv("MONITOR_MAX_GB", "10") or 0),
            resident_model=os.getenv("SENTIA_RESIDENT_MODEL", "").strip(),
            resident_context=os.getenv("SENTIA_RESIDENT_CONTEXT", "").strip() or "8192",
            guest_keep_alive=os.getenv("SENTIA_GUEST_KEEP_ALIVE", "").strip() or "2m",
            resident_idle_seconds=float(os.getenv("SENTIA_RESIDENT_IDLE_SECONDS", "30") or 30),
            imagegen_url=os.getenv("SENTIA_IMAGEGEN_URL", "").strip(),
            mcp_public_url=(os.getenv("SENTIA_MCP_PUBLIC_URL", "").strip() or cls.mcp_public_url).rstrip("/"),
            volumes_dir=os.getenv("MONITOR_VOLUMES_DIR", "").strip(),
            app_tokens=tuple(
                (app, url, os.getenv(f"LUNA{app.upper()}_API_TOKEN", "").strip()) for app, url in cls.APP_BACKENDS
            ),
        )
