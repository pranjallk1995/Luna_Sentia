"""Routes of the single sign-on service.

Every Luna app UI sends people to ``/login?next=<app url>``. After one Google
sign-in, the ``luna_sso`` cookie is set for the parent domain, so every app
receives it. Apps check it server-side with ``GET /session`` over the private
network; ``/logout`` ends the session for every app at once.
"""

from __future__ import annotations

import html
import secrets
from datetime import timedelta
from typing import Any

from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from app.config import AuthConfig
from app.google import GoogleSignIn
from app.store import SessionStore

STATE_COOKIE = "luna_sso_state"
STATE_MAX_AGE = 600
SESSION_HEADER = "X-Luna-Session"


def page(title: str, message: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title>"
        "<body style='font-family:system-ui,sans-serif;background:#111;color:#f5f5f5;"
        "display:grid;place-items:center;min-height:90vh;margin:0 16px'>"
        f"<main style='max-width:32rem'><h1>{html.escape(title)}</h1><p>{message}</p></main>",
        status_code=status,
    )


def create_app(config: AuthConfig, store: SessionStore, google: GoogleSignIn) -> Starlette:
    signer = URLSafeTimedSerializer(config.secret, salt="luna-sso-state")
    lifetime = timedelta(days=config.session_days)

    def session_cookie(response: Response, token: str | None) -> None:
        options: dict[str, Any] = {"path": "/", "httponly": True, "secure": config.secure_cookies, "samesite": "lax"}
        if config.cookie_domain:
            options["domain"] = config.cookie_domain
        if token is None:
            response.delete_cookie(config.cookie_name, **options)
        else:
            response.set_cookie(config.cookie_name, token, max_age=int(lifetime.total_seconds()), **options)

    def current(request: Request) -> dict[str, Any] | None:
        token = request.cookies.get(config.cookie_name)
        return store.get(token) if token else None

    def next_url(request: Request) -> str | None:
        target = request.query_params.get("next", "")
        return target if target and config.allows_next(target) else None

    async def home(request: Request) -> Response:
        user = current(request)
        if user is None:
            return page("Luna sign-in", "You are signed out of every Luna app.")
        return page(
            "Luna sign-in",
            f"Signed in to every Luna app as <b>{html.escape(user['email'])}</b>. "
            f"<a style='color:#a5b4fc' href='/logout'>Sign out everywhere</a>",
        )

    async def login(request: Request) -> Response:
        target = next_url(request)
        if request.query_params.get("next") and target is None:
            return page("Not a Luna app", "That return address is not a Luna app.", 400)
        if current(request) is not None:
            return RedirectResponse(target or "/", 303)
        nonce = secrets.token_urlsafe(16)
        response = RedirectResponse(google.authorization_url(signer.dumps({"next": target, "nonce": nonce})), 303)
        response.set_cookie(
            STATE_COOKIE, nonce, max_age=STATE_MAX_AGE, path="/", httponly=True,
            secure=config.secure_cookies, samesite="lax",
        )
        return response

    async def callback(request: Request) -> Response:
        if request.query_params.get("error"):
            return page("Not signed in", html.escape(request.query_params["error"]), 400)
        try:
            state = signer.loads(request.query_params.get("state", ""), max_age=STATE_MAX_AGE)
        except BadSignature:
            return page("Not signed in", "This sign-in link expired. Start again from the app.", 400)
        if not secrets.compare_digest(str(state.get("nonce")), request.cookies.get(STATE_COOKIE, "")):
            return page("Not signed in", "This sign-in did not start in this browser. Start again from the app.", 400)
        try:
            profile = google.user(request.query_params.get("code", ""))
        except Exception:  # noqa: BLE001 - shown to the person signing in
            return page("Not signed in", "Google did not confirm the sign-in. Start again from the app.", 502)
        email = str(profile.get("email", "")).strip().casefold()
        if not (email and profile.get("email_verified") is True and email in config.allowed_emails):
            return page("Not authorized", f"{html.escape(email or 'This account')} may not use the Luna apps.", 403)
        token = store.create(email, profile.get("name"), profile.get("picture"), lifetime)
        response = RedirectResponse(state.get("next") or "/", 303)
        session_cookie(response, token)
        response.delete_cookie(STATE_COOKIE, path="/")
        return response

    async def logout(request: Request) -> Response:
        token = request.cookies.get(config.cookie_name)
        if token:
            store.delete(token)
        target = next_url(request)
        response = RedirectResponse(target, 303) if target else page("Signed out", "You are signed out of every Luna app.")
        session_cookie(response, None)
        return response

    async def session(request: Request) -> Response:
        """For app servers: who the session token in ``X-Luna-Session`` belongs to."""
        token = request.headers.get(SESSION_HEADER, "")
        user = store.get(token) if token else None
        if user is None:
            return JSONResponse({"error": "not signed in"}, 401)
        return JSONResponse(
            {"email": user["email"], "email_verified": True, "name": user["name"], "picture": user["picture"],
             "expires_at": user["expires_at"].isoformat()}
        )

    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    return Starlette(
        routes=[
            Route("/", home),
            Route("/login", login),
            Route("/callback", callback),
            Route("/logout", logout),
            Route("/session", session),
            Route("/health", health),
        ]
    )
