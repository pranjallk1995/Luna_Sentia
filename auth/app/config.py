"""Settings for Luna Sentia's single sign-on service, read from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import ClassVar
from urllib.parse import urlparse

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1"})


@dataclass(frozen=True)
class AuthConfig:
    # The release this image holds, from the git tag; blank for local builds.
    VERSION: ClassVar[str] = os.getenv("LUNA_VERSION", "").strip()
    # The name the footers show.
    BRAND: ClassVar[str] = f"Luna Sentia · v{VERSION}" if VERSION else "Luna Sentia"
    GOOGLE_AUTHORIZE_URL: ClassVar[str] = "https://accounts.google.com/o/oauth2/v2/auth"
    GOOGLE_TOKEN_URL: ClassVar[str] = "https://oauth2.googleapis.com/token"
    GOOGLE_USERINFO_URL: ClassVar[str] = "https://openidconnect.googleapis.com/v1/userinfo"
    GOOGLE_TIMEOUT_SECONDS: ClassVar[float] = 15.0
    # Header the app UIs send their luna_sso token in when they call /session.
    SESSION_HEADER: ClassVar[str] = "X-Luna-Session"
    # Short-lived cookie that ties Google's callback to the browser that started the sign-in.
    STATE_COOKIE: ClassVar[str] = "luna_sso_state"
    STATE_MAX_AGE_SECONDS: ClassVar[int] = 600
    # MCP connectors: authorization codes are brief, and refreshing renews the refresh token.
    OAUTH_CODE_SECONDS: ClassVar[int] = 300
    OAUTH_ACCESS_SECONDS: ClassVar[int] = 3600
    OAUTH_REFRESH_DAYS: ClassVar[int] = 30
    OAUTH_CONSENT_SECONDS: ClassVar[int] = 600
    ADMIN_FORM_SECONDS: ClassVar[int] = 3600
    # The apps people use, which the owner can hide; each has a backend on the private network.
    APPS: ClassVar[tuple[str, ...]] = ("visia", "vita", "lumia", "fiora", "lexia", "vibia")
    REMOVE_TIMEOUT_SECONDS: ClassVar[float] = 60.0
    # A fresh clone's defaults: good on this machine only, refused once Sentia is public.
    DEV_SECRET: ClassVar[str] = "lunasentia-dev-auth-secret-localhost-only"
    # The owner's account when no owner email is set and Google is not configured.
    LOCAL_OWNER_EMAIL: ClassVar[str] = "owner@localhost"

    database_url: str
    public_url: str
    client_id: str
    client_secret: str
    secret: str
    cookie_domain: str = ""
    session_days: int = 30
    cookie_name: str = "luna_sso"
    # The account that owns the home's devices and manages the other accounts at /admin.
    owner_email: str = ""
    # Each app's private backend token, for deleting a person's data there.
    app_tokens: dict[str, str] = field(default_factory=dict)

    @property
    def google_configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def local_sign_in(self) -> bool:
        """Without Google, the owner signs in with one click, but only on this machine:
        Sentia's address is localhost and no cookie domain shares the session further."""
        host = (urlparse(self.public_url).hostname or "").lower()
        return not self.google_configured and not self.cookie_domain and host in LOCAL_HOSTS

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_url}/callback"

    @property
    def secure_cookies(self) -> bool:
        return self.public_url.startswith("https://")

    def allows_next(self, url: str) -> bool:
        """Only send people back to a Luna app: a host under the cookie domain, or localhost."""
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or not host:
            return False
        if host in LOCAL_HOSTS:
            return not self.cookie_domain
        if parsed.scheme != "https":
            return False
        return bool(self.cookie_domain) and (host == self.cookie_domain or host.endswith(f".{self.cookie_domain}"))

    def allows_resource(self, url: str) -> bool:
        """Connectors may only ask for tokens to Luna's own MCP servers."""
        return self.allows_next(url)

    @classmethod
    def from_env(cls) -> AuthConfig:
        def required(name: str) -> str:
            value = os.getenv(name, "").strip()
            if not value:
                raise RuntimeError(f"{name} is required.")
            return value

        secret = required("SENTIA_AUTH_SECRET")
        if len(secret) < 32:
            raise RuntimeError("SENTIA_AUTH_SECRET must contain at least 32 characters.")
        public_url = required("SENTIA_AUTH_PUBLIC_URL").rstrip("/")
        if secret == cls.DEV_SECRET and (urlparse(public_url).hostname or "").lower() not in LOCAL_HOSTS:
            raise RuntimeError("SENTIA_AUTH_SECRET is still the localhost default: set your own before going public.")
        client_id = os.getenv("SENTIA_AUTH_GOOGLE_CLIENT_ID", "").strip()
        client_secret = os.getenv("SENTIA_AUTH_GOOGLE_CLIENT_SECRET", "").strip()
        owner_email = os.getenv("SENTIA_AUTH_OWNER_EMAIL", "").strip().casefold()
        if not owner_email:
            # Google signs in real accounts, so one of them must be the owner.
            if client_id or client_secret:
                raise RuntimeError("SENTIA_AUTH_OWNER_EMAIL is required with Google sign-in.")
            owner_email = cls.LOCAL_OWNER_EMAIL
        return cls(
            database_url=required("DATABASE_URL"),
            public_url=public_url,
            client_id=client_id,
            client_secret=client_secret,
            secret=secret,
            app_tokens={app: os.getenv(f"LUNA{app.upper()}_API_TOKEN", "").strip() for app in cls.APPS},
            owner_email=owner_email,
            cookie_domain=os.getenv("SENTIA_AUTH_COOKIE_DOMAIN", "").strip().lstrip(".").lower(),
            session_days=int(os.getenv("SENTIA_AUTH_SESSION_DAYS", "30")),
        )
