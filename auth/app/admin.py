"""The owner's page for adding people to Luna and disabling them."""

from __future__ import annotations

import html
import re
import secrets

from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from app.config import AuthConfig
from app.oauth import OAuthStore
from app.pages import form_fields, page
from app.store import OWNER_ID, SessionStore, token_hash

EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def admin_routes(config: AuthConfig, sessions: SessionStore, oauth: OAuthStore) -> list[Route]:
    signer = URLSafeTimedSerializer(config.secret, salt="luna-admin-form")

    def owner_session(request: Request) -> str | None:
        """The session token, if it belongs to the owner."""
        token = request.cookies.get(config.cookie_name, "")
        user = sessions.get(token) if token else None
        return token if user and user["user_id"] == OWNER_ID else None

    def form(signed: str, action: str, label: str, user_id: str = "") -> str:
        return (
            f"<form method='post' action='/admin/{action}' style='display:inline'>"
            f"<input type='hidden' name='form' value='{html.escape(signed, quote=True)}'>"
            f"<input type='hidden' name='user_id' value='{html.escape(user_id, quote=True)}'>"
            f"<button>{label}</button></form>"
        )

    async def overview(request: Request) -> Response:
        token = owner_session(request)
        if token is None:
            if not request.cookies.get(config.cookie_name):
                return RedirectResponse(f"/login?next={config.public_url}/admin", 303)
            return page("Not allowed", "Only the owner manages Luna's users.", 403)
        signed = signer.dumps({"session": token_hash(token)})
        rows = []
        for user in sessions.users():
            if user["id"] == OWNER_ID:
                action = "Owner"
            elif user["active"]:
                action = form(signed, "disable", "Disable", user["id"])
            else:
                action = form(signed, "enable", "Enable", user["id"])
            seen = user["last_seen_at"].strftime("%Y-%m-%d %H:%M UTC") if user["last_seen_at"] else "Never"
            state = "Active" if user["active"] else "Disabled"
            rows.append(
                f"<tr><td>{html.escape(user['email'])}</td><td>{state}</td><td>{seen}</td><td>{action}</td></tr>"
            )
        cell = "<style>td,th{padding:.4rem .8rem .4rem 0;text-align:left;border-bottom:1px solid #333}</style>"
        add = (
            "<form method='post' action='/admin/add' style='margin-top:1.5rem'>"
            f"<input type='hidden' name='form' value='{html.escape(signed, quote=True)}'>"
            "<label>Google account <input name='email' type='email' required></label> <button>Add user</button></form>"
        )
        table = "<table><tr><th>Account</th><th>Status</th><th>Last seen</th><th></th></tr>" + "".join(rows) + "</table>"
        return page("Luna users", cell + table + add)

    def change(apply):
        async def endpoint(request: Request) -> Response:
            token = owner_session(request)
            fields = await form_fields(request)
            try:
                signed = signer.loads(fields.get("form", ""), max_age=AuthConfig.ADMIN_FORM_SECONDS)
            except BadSignature:
                signed = None
            if token is None or signed is None or not secrets.compare_digest(signed["session"], token_hash(token)):
                return page("Not allowed", "This page expired. <a style='color:#a5b4fc' href='/admin'>Reload it</a>.", 403)
            try:
                apply(fields)
            except ValueError as problem:
                return page("Not changed", html.escape(str(problem)), 400)
            return RedirectResponse("/admin", 303)

        return endpoint

    def add(fields: dict[str, str]) -> None:
        email = fields.get("email", "").strip().casefold()
        if not EMAIL.fullmatch(email):
            raise ValueError("That does not look like an email address.")
        sessions.add_user(email)

    def disable(fields: dict[str, str]) -> None:
        sessions.set_active(fields.get("user_id", ""), False)
        oauth.revoke_user(fields.get("user_id", ""))

    def enable(fields: dict[str, str]) -> None:
        sessions.set_active(fields.get("user_id", ""), True)

    return [
        Route("/admin", overview),
        Route("/admin/add", change(add), methods=["POST"]),
        Route("/admin/disable", change(disable), methods=["POST"]),
        Route("/admin/enable", change(enable), methods=["POST"]),
    ]
