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
    # ComfyUI's own page, behind the owner's sign-in (comfyui_gate.py).
    COMFYUI_GATE_PORT: ClassVar[int] = 8188
    # Longest wait for one ComfyUI reply through the gate; pictures are polled, not waited on.
    COMFYUI_GATE_TIMEOUT_SECONDS: ClassVar[float] = 300.0
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
    # The apps with models of their own: Visia's SigLIP 2, Lexia's Docling, Vibia's CLAP.
    APP_BACKENDS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("visia", "http://luna-visia-mcp:8000"),
        ("lexia", "http://luna-lexia-mcp:8000"),
        ("vibia", "http://luna-vibia-mcp:8000"),
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
    # Each app's private API token (LUNA<APP>_API_TOKEN), to ask it about its own models.
    app_tokens: tuple[tuple[str, str, str], ...] = ()
    # The model that lives on the GPU, at the context its app asks for (a different
    # context would make Ollama load it again); blank turns residency off.
    resident_model: str = ""
    resident_context: int = 8192
    # How long any other model stays loaded after its last request.
    guest_keep_alive: str = "2m"
    # Quiet seconds before the resident model is loaded back.
    resident_idle_seconds: float = 30.0
    # ComfyUI, asked whether it is drawing before the resident model comes back.
    comfyui_url: str = ""
    # Where people open ComfyUI's page through the gate: SENTIA_COMFYUI_PUBLIC_URL.
    comfyui_public_url: str = "http://localhost:19082"

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
            resident_model=os.getenv("SENTIA_RESIDENT_MODEL", "").strip(),
            resident_context=int(os.getenv("SENTIA_RESIDENT_CONTEXT", "8192") or 8192),
            guest_keep_alive=os.getenv("SENTIA_GUEST_KEEP_ALIVE", "").strip() or "2m",
            resident_idle_seconds=float(os.getenv("SENTIA_RESIDENT_IDLE_SECONDS", "30") or 30),
            comfyui_url=os.getenv("SENTIA_COMFYUI_URL", "").strip(),
            comfyui_public_url=(os.getenv("SENTIA_COMFYUI_PUBLIC_URL", "").strip() or cls.comfyui_public_url).rstrip("/"),
            app_tokens=tuple(
                (app, url, os.getenv(f"LUNA{app.upper()}_API_TOKEN", "").strip()) for app, url in cls.APP_BACKENDS
            ),
        )
