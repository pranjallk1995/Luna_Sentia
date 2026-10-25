"""Google sign-in with the OpenID Connect authorization-code flow."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


class GoogleSignIn:
    def __init__(self, client_id: str, client_secret: str, redirect_uri: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri

    def authorization_url(self, state: str) -> str:
        return AUTHORIZE_URL + "?" + urlencode(
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
        with httpx.Client(timeout=15) as client:
            token = client.post(
                TOKEN_URL,
                data={
                    "code": code,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "redirect_uri": self.redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            token.raise_for_status()
            profile = client.get(USERINFO_URL, headers={"Authorization": f"Bearer {token.json()['access_token']}"})
            profile.raise_for_status()
            return profile.json()
