"""Routes of the single sign-on service.

Every Luna app UI sends people to ``/login?next=<app url>``. After one Google
sign-in (or, on a fresh clone without Google configured, the owner's one-click sign-in on
localhost), the ``luna_sso`` cookie is set for the parent domain, so every app
receives it. Apps check it server-side with ``GET /session`` over the private
network; ``/logout`` ends the session for every app at once.
"""

from __future__ import annotations

import html
import secrets
from datetime import timedelta
from typing import Any

import httpx
from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from app.admin import admin_routes
from app.config import LOCAL_HOSTS, AuthConfig
from app.google import GoogleSignIn
from app.oauth import OAuthStore, oauth_routes
from app.pages import form_fields, page
from app.pins import pin_routes
from app.store import OWNER_ID, SessionStore


def create_app(
    config: AuthConfig,
    store: SessionStore,
    google: GoogleSignIn,
    oauth: OAuthStore,
    app_transport: httpx.AsyncBaseTransport | None = None,
) -> Starlette:
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
        admin = " <a style='color:#a5b4fc' href='/admin'>Manage users</a>" if user["user_id"] == OWNER_ID else ""
        return page(
            "Luna sign-in",
            f"Signed in to every Luna app as <b>{html.escape(user['email'])}</b>. "
            f"<a style='color:#a5b4fc' href='/logout'>Sign out everywhere</a>"
            f" <a style='color:#a5b4fc' href='/pin'>Hidden-items PIN</a>{admin}",
        )

    async def login(request: Request) -> Response:
        target = next_url(request)
        if request.query_params.get("next") and target is None:
            return page("Not a Luna app", "That return address is not a Luna app.", 400)
        # fresh=1 asks Google again even when signed in, to prove it is still this person.
        if current(request) is not None and request.query_params.get("fresh") != "1":
            return RedirectResponse(target or "/", 303)
        nonce = secrets.token_urlsafe(16)
        state = signer.dumps({"next": target, "nonce": nonce})
        if config.google_configured:
            response = RedirectResponse(google.authorization_url(state), 303)
        elif local(request):
            response = page(
                "Luna sign-in",
                "Google sign-in is not set up, so on this machine you sign in as the owner."
                "<form method='post' action='/login/owner' style='margin-top:1.2rem'>"
                f"<input type='hidden' name='state' value='{html.escape(state)}'>"
                "<button style='padding:.6rem 1.2rem;border-radius:.6rem;border:0;background:#f5f5f5;color:#111;"
                "font:inherit;font-weight:600;cursor:pointer'>Continue as owner</button></form>"
                "<p style='color:#a3a3a3;font-size:.85rem'>To sign in with Google instead, or from another "
                "device, set SENTIA_AUTH_GOOGLE_CLIENT_ID, SENTIA_AUTH_GOOGLE_CLIENT_SECRET and "
                "SENTIA_AUTH_OWNER_EMAIL in Luna_Sentia/.env.</p>",
            )
        else:
            return not_set_up()
        response.set_cookie(
            AuthConfig.STATE_COOKIE, nonce, max_age=AuthConfig.STATE_MAX_AGE_SECONDS, path="/", httponly=True,
            secure=config.secure_cookies, samesite="lax",
        )
        return response

    def local(request: Request) -> bool:
        """The owner's one-click sign-in: no Google, and both Sentia and this request on localhost."""
        return config.local_sign_in and (request.url.hostname or "").lower() in LOCAL_HOSTS

    def not_set_up() -> Response:
        return page("Sign-in is not set up",
                    "Set SENTIA_AUTH_GOOGLE_CLIENT_ID, SENTIA_AUTH_GOOGLE_CLIENT_SECRET and SENTIA_AUTH_OWNER_EMAIL "
                    "in Luna_Sentia/.env. Without them, only this machine's localhost can sign in.", 503)

    def signed_state(raw: str, request: Request) -> dict[str, Any] | None:
        """A sign-in's state, if it is fresh and started in this browser."""
        try:
            state = signer.loads(raw, max_age=AuthConfig.STATE_MAX_AGE_SECONDS)
        except BadSignature:
            return None
        if not secrets.compare_digest(str(state.get("nonce")), request.cookies.get(AuthConfig.STATE_COOKIE, "")):
            return None
        return state

    def signed_in(request: Request, state: dict[str, Any], email: str, name: str | None, picture: str | None) -> Response:
        previous = request.cookies.get(config.cookie_name)
        if previous:
            store.delete(previous)
        token = store.create(email, name, picture, lifetime)
        response = RedirectResponse(state.get("next") or "/", 303)
        session_cookie(response, token)
        response.delete_cookie(AuthConfig.STATE_COOKIE, path="/")
        return response

    async def owner_login(request: Request) -> Response:
        """The one-click sign-in's form posts here."""
        if config.google_configured or not local(request):
            return not_set_up()
        state = signed_state((await form_fields(request)).get("state", ""), request)
        if state is None:
            return page("Not signed in", "This sign-in expired or started in another browser. Start again from the app.", 400)
        return signed_in(request, state, config.owner_email, "Owner", None)

    async def callback(request: Request) -> Response:
        if request.query_params.get("error"):
            return page("Not signed in", html.escape(request.query_params["error"]), 400)
        if not config.google_configured:
            return not_set_up()
        try:
            state = signer.loads(request.query_params.get("state", ""), max_age=AuthConfig.STATE_MAX_AGE_SECONDS)
        except BadSignature:
            return page("Not signed in", "This sign-in link expired. Start again from the app.", 400)
        if not secrets.compare_digest(str(state.get("nonce")), request.cookies.get(AuthConfig.STATE_COOKIE, "")):
            return page("Not signed in", "This sign-in did not start in this browser. Start again from the app.", 400)
        try:
            profile = google.user(request.query_params.get("code", ""))
        except Exception:  # noqa: BLE001 - shown to the person signing in
            return page("Not signed in", "Google did not confirm the sign-in. Start again from the app.", 502)
        email = str(profile.get("email", "")).strip().casefold()
        if not (email and profile.get("email_verified") is True and store.active_user(email)):
            return page("Not authorized", f"{html.escape(email or 'This account')} may not use the Luna apps.", 403)
        return signed_in(request, state, email, profile.get("name"), profile.get("picture"))

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
        token = request.headers.get(AuthConfig.SESSION_HEADER, "")
        user = store.get(token) if token else None
        if user is None:
            return JSONResponse({"error": "not signed in"}, 401)
        return JSONResponse(
            {"user_id": user["user_id"], "hidden_apps": store.hidden_from(user["user_id"]),
             "email": user["email"], "email_verified": True, "name": user["name"],
             "picture": user["picture"],
             "expires_at": user["expires_at"].isoformat()}
        )

    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok", "version": AuthConfig.VERSION})

    return Starlette(
        routes=[
            Route("/", home),
            Route("/login", login),
            Route("/login/owner", owner_login, methods=["POST"]),
            Route("/callback", callback),
            Route("/logout", logout),
            Route("/session", session),
            Route("/health", health),
            *oauth_routes(config, store, oauth),
            *admin_routes(config, store, oauth, app_transport),
            *pin_routes(config, store),
        ]
    )
