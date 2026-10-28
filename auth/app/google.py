"""Google sign-in with the OpenID Connect authorization-code flow."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import AuthConfig


class GoogleSignIn:
    def __init__(self, client_id: str, client_secret: str, redirect_uri: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri

    def authorization_url(self, state: str) -> str:
        return AuthConfig.GOOGLE_AUTHORIZE_URL + "?" + urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "response_type": "code",
                "scope": "openid email profile",
                "state": state,
                "prompt": "select_account",
            }
        )

    def user(self, code: str) -> dict[str, Any]:
        """Exchange the code and return Google's verified profile (email, email_verified, name, picture)."""
        with httpx.Client(timeout=AuthConfig.GOOGLE_TIMEOUT_SECONDS) as client:
            token = client.post(
                AuthConfig.GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "redirect_uri": self.redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            token.raise_for_status()
            profile = client.get(AuthConfig.GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {token.json()['access_token']}"})
            profile.raise_for_status()
            return profile.json()
