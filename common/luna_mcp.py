"""An MCP server that knows who is calling. Identical in every Luna app.

MCP clients sign in through Luna Sentia and send its access token; each tool call then
runs as that person. REST routes under ``/api/`` require this app's private token and
the ``X-Luna-User`` header set by its UI gateway or a companion app. Only the paths an
app lists as open (public links, single-use links, OAuth callbacks) skip that, and those
routes say whose data they act on themselves.
"""

import hashlib
import hmac
import os
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from typing import Any

import anyio
import httpx
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core import tenancy

TOKEN_RECHECK_SECONDS = 30
TOKEN_CACHE_ENTRIES = 256
INTROSPECT_TIMEOUT_SECONDS = 5.0
# What a companion app's token may reach, as (method, path).
COMPANION_ROUTES = frozenset(
    {("POST", "/api/companion/images"), ("POST", "/api/companion/images/tags"), ("GET", "/api/metadata")}
)


class SentiaTokenVerifier:
    """Ask Luna Sentia whose access token this is, and keep the answer briefly.

    A disabled account, or an app just hidden, therefore loses access within ``TOKEN_RECHECK_SECONDS``.
    """

    def __init__(
        self, sso_internal_url: str, resource: str, transport: httpx.AsyncBaseTransport | None = None, app: str = ""
    ) -> None:
        self.url = f"{sso_internal_url.rstrip('/')}/oauth/introspect"
        self.resource = resource.rstrip("/")
        self.app = app
        self.client = httpx.AsyncClient(timeout=INTROSPECT_TIMEOUT_SECONDS, transport=transport)
        self.known: OrderedDict[str, tuple[float, AccessToken | None]] = OrderedDict()

    async def verify_token(self, token: str) -> AccessToken | None:
        key = hashlib.sha256(token.encode()).hexdigest()
        cached = self.known.get(key)
        if cached and time.monotonic() - cached[0] < TOKEN_RECHECK_SECONDS:
            return cached[1] if self.unexpired(cached[1]) else None
        try:
            response = await self.client.post(self.url, data={"token": token})
            response.raise_for_status()
            answer = response.json()
        except (httpx.HTTPError, ValueError):
            return None  # Not remembered, so access returns as soon as Sentia does.
        access = self.access_token(token, answer)
        self.known[key] = (time.monotonic(), access)
        self.known.move_to_end(key)
        while len(self.known) > TOKEN_CACHE_ENTRIES:
            self.known.popitem(last=False)
        return access

    @staticmethod
    def unexpired(access: AccessToken | None) -> bool:
        return access is not None and (access.expires_at is None or access.expires_at > time.time())

    def access_token(self, token: str, answer: dict[str, Any]) -> AccessToken | None:
        if answer.get("active") is not True or not tenancy.is_valid(answer.get("sub")):
            return None
        # The owner has hidden this app from this person.
        if self.app in answer.get("hidden_apps", []):
            return None
        # A token asked for on another Luna server is not valid here.
        resource = answer.get("resource")
        if resource and resource.rstrip("/") != self.resource:
            return None
        return AccessToken(
            token=token,
            client_id=str(answer.get("client_id", "")),
            scopes=[],
            expires_at=answer.get("exp"),
            resource=resource,
            subject=answer["sub"],
        )


class RestUsers:
    """ASGI middleware: ``/api/`` needs a private token and says which person it acts for."""

    def __init__(
        self, app: Any, api_token: str, companion_token: str = "", open_paths: Sequence[str] = ()
    ) -> None:
        self.app = app
        self.api_token = api_token
        self.companion_token = companion_token
        self.open_paths = tuple(open_paths)

    def allowed(self, method: str, path: str, credential: str) -> bool:
        if credential and hmac.compare_digest(credential, self.api_token):
            return True
        return bool(
            credential
            and self.companion_token
            and (method, path) in COMPANION_ROUTES
            and hmac.compare_digest(credential, self.companion_token)
        )

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith("/api/") or path.startswith(self.open_paths or ("\0",)):
            await self.app(scope, receive, send)
            return
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope["headers"]}
        kind, _, credential = headers.get("authorization", "").partition(" ")
        if kind.lower() != "bearer" or not self.allowed(scope["method"], path, credential):
            await JSONResponse(
                {"error": "Authentication required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
            )(scope, receive, send)
            return
        user_id = headers.get(tenancy.USER_HEADER, "")
        if not tenancy.is_valid(user_id):
            await JSONResponse({"error": "No user given."}, status_code=400)(scope, receive, send)
            return
        with tenancy.acting_as(user_id):
            await self.app(scope, receive, send)


class LunaMCP(FastMCP):
    """FastMCP whose tools run as the person behind the access token."""

    def __init__(
        self,
        name: str,
        *,
        app: str,
        public_url: str,
        api_token: str,
        remove_user: Callable[[str], None],
        companion_token: str = "",
        open_paths: Sequence[str] = (),
        **settings: Any,
    ) -> None:
        issuer = os.getenv("LUNA_SSO_URL", "").strip().rstrip("/")
        if not issuer:
            raise RuntimeError("LUNA_SSO_URL must be the public address of Luna Sentia's auth service.")
        internal = os.getenv("LUNA_SSO_INTERNAL_URL", "").strip() or "http://luna-sentia-auth:8000"
        resource = f"{public_url.rstrip('/')}/mcp"
        self.remove_user = remove_user
        self.rest_settings = {"api_token": api_token, "companion_token": companion_token, "open_paths": open_paths}
        super().__init__(
            name,
            token_verifier=SentiaTokenVerifier(internal, resource, app=app),
            # The verifier compares resources itself, and tolerates clients that send none.
            auth=AuthSettings(
                issuer_url=AnyHttpUrl(issuer),
                resource_server_url=AnyHttpUrl(resource),
                validate_token_resource=False,
            ),
            **settings,
        )
        self.custom_route("/api/users/{user_id}", methods=["DELETE"])(self.delete_user)
        self.ui_url = os.getenv("UI_PUBLIC_URL", "").strip().rstrip("/")
        self.tool()(self.get_ui_link)

    def get_ui_link(self) -> dict[str, str]:
        """Return the link to this app's web UI.

        Give it to the user when they ask where to see or manage this app's data, or when
        something is easier to do on the page. They sign in there with their Luna account.
        """
        if not self.ui_url:
            raise ValueError("This app's web address is not set; set UI_PUBLIC_URL and restart it.")
        return {"app": self.name, "url": self.ui_url}

    async def delete_user(self, request: Request) -> Response:
        """For Luna Sentia, when the owner deletes someone: remove all they have here."""
        if not tenancy.is_owner():
            return JSONResponse({"error": "Only the owner deletes people."}, status_code=403)
        user_id = request.path_params["user_id"]
        if not tenancy.is_valid(user_id) or user_id == tenancy.OWNER:
            return JSONResponse({"error": "That person cannot be deleted."}, status_code=400)
        await anyio.to_thread.run_sync(self.remove_user, user_id)
        return JSONResponse({"status": "removed"})

    def caller(self) -> str:
        """The signed-in person, read from this request; sessions outlive any one request."""
        request = self.get_context().request_context.request
        user = request.scope.get("user") if request is not None else None
        access = getattr(user, "access_token", None)
        subject = getattr(access, "subject", None)
        if not tenancy.is_valid(subject):
            raise tenancy.NoUserError("This request has no signed-in user.")
        return subject

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        with tenancy.acting_as(self.caller()):
            return await super().call_tool(name, arguments)

    def streamable_http_app(self) -> Starlette:
        app = super().streamable_http_app()
        app.add_middleware(RestUsers, **self.rest_settings)
        return app
