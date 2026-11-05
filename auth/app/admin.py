"""The owner's page for adding people to Luna and disabling them."""

from __future__ import annotations

import asyncio
import html
import logging
import re
import secrets

import httpx
from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from app.config import AuthConfig
from app.oauth import OAuthStore
from app.pages import form_fields, page
from app.store import OWNER_ID, SessionStore, token_hash

logger = logging.getLogger(__name__)
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


async def remove_from_apps(
    config: AuthConfig, user_id: str, apps: list[str], transport: httpx.AsyncBaseTransport | None
) -> list[str]:
    """Ask each app to delete this person's data; returns the apps that confirmed."""

    async def remove(client: httpx.AsyncClient, app: str) -> str | None:
        token = config.app_tokens.get(app)
        if not token:
            logger.warning("No API token for %s, so a person's data there stays until retried.", app)
            return None
        try:
            answer = await client.delete(
                f"http://luna-{app}-mcp:8000/api/users/{user_id}",
                headers={"Authorization": f"Bearer {token}", "X-Luna-User": OWNER_ID},
            )
            answer.raise_for_status()
        except httpx.HTTPError as problem:
            logger.warning("%s did not delete a person's data: %s", app, problem)
            return None
        return app

    async with httpx.AsyncClient(timeout=AuthConfig.REMOVE_TIMEOUT_SECONDS, transport=transport) as client:
        done = await asyncio.gather(*(remove(client, app) for app in apps))
    return [app for app in done if app]


def admin_routes(
    config: AuthConfig, sessions: SessionStore, oauth: OAuthStore, transport: httpx.AsyncBaseTransport | None = None
) -> list[Route]:
    signer = URLSafeTimedSerializer(config.secret, salt="luna-admin-form")

    def owner_session(request: Request) -> str | None:
        """The session token, if it belongs to the owner."""
        token = request.cookies.get(config.cookie_name, "")
        user = sessions.get(token) if token else None
        return token if user and user["user_id"] == OWNER_ID else None

    def form(signed: str, action: str, label: str, **fields: str) -> str:
        hidden = "".join(
            f"<input type='hidden' name='{name}' value='{html.escape(value, quote=True)}'>"
            for name, value in {"form": signed, **fields}.items()
        )
        return f"<form method='post' action='/admin/{action}' style='display:inline'>{hidden}<button>{label}</button></form> "

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
            elif user["removing"] is not None:
                action = form(signed, "delete", "Retry delete", user_id=user["id"], confirmed="yes")
            else:
                switch = "disable" if user["active"] else "enable"
                action = form(signed, switch, switch.capitalize(), user_id=user["id"])
                action += form(signed, "delete", "Delete", user_id=user["id"])
            seen = user["last_seen_at"].strftime("%Y-%m-%d %H:%M UTC") if user["last_seen_at"] else "Never"
            state = "Active" if user["active"] else "Disabled"
            if user["removing"] is not None:
                state = f"Deleting; waiting on {html.escape(', '.join(user['removing']))}"
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
        hidden = set(sessions.hidden_apps())
        app_rows = "".join(
            f"<tr><td>Luna {app.capitalize()}</td><td>{'Hidden from others' if app in hidden else 'Shown to everyone'}</td><td>"
            + form(signed, "apps", "Show to everyone" if app in hidden else "Hide from others", app=app, hidden="no" if app in hidden else "yes")
            + "</td></tr>"
            for app in AuthConfig.APPS
        )
        apps = (
            "<h2>Apps</h2><p>A hidden app disappears from the home page for everyone but you, and its "
            "UI and connector refuse them within a minute. Their data stays.</p>"
            f"<table><tr><th>App</th><th>Status</th><th>Change to</th></tr>{app_rows}</table>"
        )
        return page("Luna users", cell + table + add + apps)

    def valid_form(request: Request, fields: dict[str, str]) -> bool:
        token = owner_session(request)
        try:
            signed = signer.loads(fields.get("form", ""), max_age=AuthConfig.ADMIN_FORM_SECONDS)
        except BadSignature:
            return False
        return token is not None and secrets.compare_digest(signed["session"], token_hash(token))

    EXPIRED = "This page expired. <a style='color:#a5b4fc' href='/admin'>Reload it</a>."

    def change(apply):
        async def endpoint(request: Request) -> Response:
            fields = await form_fields(request)
            if not valid_form(request, fields):
                return page("Not allowed", EXPIRED, 403)
            try:
                apply(fields)
            except ValueError as problem:
                return page("Not changed", html.escape(str(problem)), 400)
            return RedirectResponse("/admin", 303)

        return endpoint

    async def delete(request: Request) -> Response:
        """Ask once to confirm, then shut the person out and remove their data from every app."""
        fields = await form_fields(request)
        if not valid_form(request, fields):
            return page("Not allowed", EXPIRED, 403)
        user_id = fields.get("user_id", "")
        target = sessions.user(user_id)
        if target is None or user_id == OWNER_ID:
            return page("Not changed", "That person cannot be deleted.", 400)
        if fields.get("confirmed") != "yes":
            email = html.escape(target["email"])
            return page(
                "Delete this person?",
                f"<p>This permanently deletes <b>{email}</b> and everything they have in every Luna app: "
                "their data, files, and public links. It cannot be undone.</p>"
                + form(fields["form"], "delete", "Delete for good", user_id=user_id, confirmed="yes")
                + "<a style='color:#a5b4fc' href='/admin'>Cancel</a>",
            )
        pending = sessions.start_removal(user_id, AuthConfig.APPS)
        oauth.revoke_user(user_id)
        removed = await remove_from_apps(config, user_id, pending, transport)
        remaining = sessions.finish_removal(user_id, removed)
        if remaining:
            return page(
                "Not fully deleted yet",
                f"{html.escape(target['email'])} can no longer sign in, but these apps did not confirm: "
                f"<b>{html.escape(', '.join(remaining))}</b>. Their data is still there. Start those apps, then "
                "<a style='color:#a5b4fc' href='/admin'>retry from the user list</a>.",
                # Not a 5xx: Cloudflare would show its own error page instead of this one.
                409,
            )
        return RedirectResponse("/admin", 303)

    def show_or_hide(fields: dict[str, str]) -> None:
        if fields.get("app") not in AuthConfig.APPS:
            raise ValueError("That is not a Luna app.")
        sessions.set_app_hidden(fields["app"], fields.get("hidden") == "yes")

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
        Route("/admin/delete", delete, methods=["POST"]),
        Route("/admin/apps", change(show_or_hide), methods=["POST"]),
    ]
