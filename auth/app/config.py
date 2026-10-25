"""Settings for Luna Sentia's single sign-on service, read from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1"})


@dataclass(frozen=True)
class AuthConfig:
    database_url: str
    public_url: str
    client_id: str
    client_secret: str
    secret: str
    allowed_emails: frozenset[str]
    cookie_domain: str = ""
    session_days: int = 30
    cookie_name: str = "luna_sso"

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
        if not emails:
            raise RuntimeError("SENTIA_AUTH_ALLOWED_EMAILS must list at least one account.")
        return cls(
            database_url=required("DATABASE_URL"),
            public_url=required("SENTIA_AUTH_PUBLIC_URL").rstrip("/"),
            client_id=required("SENTIA_AUTH_GOOGLE_CLIENT_ID"),
            client_secret=required("SENTIA_AUTH_GOOGLE_CLIENT_SECRET"),
            secret=secret,
            allowed_emails=emails,
            cookie_domain=os.getenv("SENTIA_AUTH_COOKIE_DOMAIN", "").strip().lstrip(".").lower(),
            session_days=int(os.getenv("SENTIA_AUTH_SESSION_DAYS", "30")),
        )
