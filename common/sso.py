"""Single sign-on through Luna Sentia's auth service, for a Luna app's UI gateway.

One Google sign-in at Sentia sets the ``luna_sso`` cookie for the parent domain
of every app. This module asks Sentia over the private network whose session
that cookie belongs to, and applies this app's own email allowlist. Signing out
at Sentia signs out of every Luna app.
"""

from __future__ import annotations

import re
import time
from collections import OrderedDict
from contextvars import ContextVar
from typing import Any
from urllib.parse import quote

import httpx

from config import SSOConfig


# The signed-in person this request acts for, and the header that tells the backend.
CURRENT_USER: ContextVar[str | None] = ContextVar("luna_user", default=None)
USER_HEADER = "X-Luna-User"
VALID_USER = re.compile(r"[a-z0-9]{1,32}")


def signed_in_user() -> str:
    """The current person's id; anything else fails rather than acting for someone else."""
    user_id = CURRENT_USER.get()
    if not user_id or not VALID_USER.fullmatch(user_id):
        raise httpx.RequestError("No signed-in user for this request.")
    return user_id


async def send_user(request: httpx.Request) -> None:
    """Tell the backend whose data a request is for; every backend call passes through here."""
    request.headers[USER_HEADER] = signed_in_user()


class LunaSingleSignOn:
    """Resolve a session cookie to a person, and build Sentia's sign-in and sign-out links."""

    def __init__(
        self,
        allowed_emails: frozenset[str],
        config: SSOConfig,
        transport: httpx.AsyncBaseTransport | None = None,
        app: str = "",
    ) -> None:
        self.allowed_emails = allowed_emails
        self.app = app
        self.public_url = config.public_url.rstrip("/")
        self.internal_url = config.internal_url.rstrip("/")
        self.client = httpx.AsyncClient(transport=transport, timeout=SSOConfig.LOOKUP_TIMEOUT_SECONDS)
        self.sessions: OrderedDict[str, dict[str, Any]] = OrderedDict()

    # Identity

    def is_allowed(self, user: dict[str, Any] | None) -> bool:
        email = str((user or {}).get("email", "")).strip().casefold()
        listed = "*" in self.allowed_emails or email in self.allowed_emails
        if self.app in (user or {}).get("hidden_apps", []):
            return False
        return bool(email and listed and (user or {}).get("email_verified") is True)

    async def lookup(self, token: str) -> dict[str, Any] | None:
        """Ask Sentia who the session belongs to; None when it is unknown or expired."""
        response = await self.client.get(
            f"{self.internal_url}/session", headers={SSOConfig.SESSION_HEADER: token}
        )
        if response.status_code == 401:
            return None
        response.raise_for_status()
        return response.json()

    async def user(self, token: str | None) -> dict[str, Any] | None:
        """The session's person, trusted for ``RECHECK_SECONDS`` before asking Sentia again."""
        if not token:
            return None
        cached = self.sessions.get(token)
        if cached and time.monotonic() - cached["checked"] < SSOConfig.RECHECK_SECONDS:
            return cached["user"]
        try:
            user = await self.lookup(token)
        except httpx.HTTPError:
            # Sentia briefly unreachable: keep a session confirmed earlier, never invent one.
            return cached["user"] if cached else None
        self.sessions[token] = {"user": user, "checked": time.monotonic()}
        self.sessions.move_to_end(token)
        while len(self.sessions) > SSOConfig.SESSION_CACHE_ENTRIES:
            self.sessions.popitem(last=False)
        return user

    # Addresses

    def login_url(self, here: str) -> str:
        return f"{self.public_url}/login?next={quote(here, safe='')}"

    def logout_url(self, here: str) -> str:
        return f"{self.public_url}/logout?next={quote(here, safe='')}"
