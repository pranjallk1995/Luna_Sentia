"""Settings for Luna Sentia's single sign-on service, read from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import ClassVar
from urllib.parse import urlparse

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1"})


@dataclass(frozen=True)
class AuthConfig:
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

    database_url: str
    public_url: str
    client_id: str
    client_secret: str
    secret: str
    allowed_emails: frozenset[str]
    cookie_domain: str = ""
    session_days: int = 30
    cookie_name: str = "luna_sso"
    # The account that owns the home's devices and manages the other accounts at /admin.
    owner_email: str = ""

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
        emails = frozenset(
            email.strip().casefold() for email in os.getenv("SENTIA_AUTH_ALLOWED_EMAILS", "").split(",") if email.strip()
        )
        owner_email = required("SENTIA_AUTH_OWNER_EMAIL").casefold()
        return cls(
            database_url=required("DATABASE_URL"),
            public_url=required("SENTIA_AUTH_PUBLIC_URL").rstrip("/"),
            client_id=required("SENTIA_AUTH_GOOGLE_CLIENT_ID"),
            client_secret=required("SENTIA_AUTH_GOOGLE_CLIENT_SECRET"),
            secret=secret,
            allowed_emails=emails,
            owner_email=owner_email,
            cookie_domain=os.getenv("SENTIA_AUTH_COOKIE_DOMAIN", "").strip().lstrip(".").lower(),
            session_days=int(os.getenv("SENTIA_AUTH_SESSION_DAYS", "30")),
        )
