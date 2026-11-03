"""OAuth 2.1 for MCP connectors: each person signs in and gets tokens tied to their account.

Clients such as Claude register themselves (RFC 7591), send the person here to approve
access, and exchange the result for an access token using PKCE. MCP servers learn whose
token it is from ``/oauth/introspect`` over the private network. Tokens are opaque and only
their hashes are stored. Every approval is shown to the person: registration is open, so
without that step any website could obtain a token for whoever opens its link.
"""

from __future__ import annotations

import base64
import hashlib
import html
import secrets
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode, urlparse

import psycopg
from itsdangerous import BadSignature, URLSafeTimedSerializer
from psycopg.rows import dict_row
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from app.config import LOCAL_HOSTS, AuthConfig
from app.pages import form_fields, page
from app.store import SessionStore, token_hash

SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id text PRIMARY KEY,
    client_name text NOT NULL,
    redirect_uris text[] NOT NULL,
    secret_hash char(64),
    created_at timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz
);
CREATE TABLE IF NOT EXISTS oauth_codes (
    code_hash char(64) PRIMARY KEY,
    client_id text NOT NULL REFERENCES oauth_clients ON DELETE CASCADE,
    user_id text NOT NULL REFERENCES users ON DELETE CASCADE,
    redirect_uri text NOT NULL,
    code_challenge text NOT NULL,
    resource text,
    scope text,
    expires_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_tokens (
    token_hash char(64) PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('access', 'refresh')),
    client_id text NOT NULL REFERENCES oauth_clients ON DELETE CASCADE,
    user_id text NOT NULL REFERENCES users ON DELETE CASCADE,
    resource text,
    scope text,
    expires_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS oauth_tokens_user_idx ON oauth_tokens (user_id);
"""

MAX_REDIRECT_URIS = 10
MAX_CLIENT_NAME_CHARS = 100
UNUSED_CLIENT_DAYS = 30


class OAuthStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.database_url, row_factory=dict_row, autocommit=True)

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(SCHEMA)

    def register(self, name: str, redirect_uris: list[str], with_secret: bool) -> tuple[str, str | None]:
        client_id, secret = secrets.token_urlsafe(24), secrets.token_urlsafe(32) if with_secret else None
        with self._connect() as connection:
            # Registration is open, so clients that never got as far as a token do not pile up.
            connection.execute(
                "DELETE FROM oauth_clients WHERE last_used_at IS NULL AND created_at < now() - %s",
                (timedelta(days=UNUSED_CLIENT_DAYS),),
            )
            connection.execute(
                "INSERT INTO oauth_clients (client_id, client_name, redirect_uris, secret_hash) VALUES (%s, %s, %s, %s)",
                (client_id, name, redirect_uris, token_hash(secret) if secret else None),
            )
        return client_id, secret

    def client(self, client_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            return connection.execute("SELECT * FROM oauth_clients WHERE client_id = %s", (client_id,)).fetchone()

    def issue_code(self, grant: dict[str, Any], user_id: str) -> str:
        code = secrets.token_urlsafe(32)
        with self._connect() as connection:
            connection.execute("DELETE FROM oauth_codes WHERE expires_at < now()")
            connection.execute(
                """INSERT INTO oauth_codes (code_hash, client_id, user_id, redirect_uri, code_challenge, resource, scope, expires_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, now() + %s)""",
                (token_hash(code), grant["client_id"], user_id, grant["redirect_uri"], grant["code_challenge"],
                 grant.get("resource"), grant.get("scope"), timedelta(seconds=AuthConfig.OAUTH_CODE_SECONDS)),
            )
        return code

    def take_code(self, code: str) -> dict[str, Any] | None:
        """Use up a code: it works once, whatever the outcome of the exchange."""
        with self._connect() as connection:
            return connection.execute(
                "DELETE FROM oauth_codes WHERE code_hash = %s AND expires_at > now() RETURNING *", (token_hash(code),)
            ).fetchone()

    def issue_tokens(self, client_id: str, user_id: str, resource: str | None, scope: str | None) -> dict[str, Any]:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        lifetimes = (
            (access, "access", timedelta(seconds=AuthConfig.OAUTH_ACCESS_SECONDS)),
            (refresh, "refresh", timedelta(days=AuthConfig.OAUTH_REFRESH_DAYS)),
        )
        with self._connect() as connection:
            connection.execute("DELETE FROM oauth_tokens WHERE expires_at < now()")
            for token, kind, lifetime in lifetimes:
                connection.execute(
                    """INSERT INTO oauth_tokens (token_hash, kind, client_id, user_id, resource, scope, expires_at)
                       VALUES (%s, %s, %s, %s, %s, %s, now() + %s)""",
                    (token_hash(token), kind, client_id, user_id, resource, scope, lifetime),
                )
            connection.execute("UPDATE oauth_clients SET last_used_at = now() WHERE client_id = %s", (client_id,))
        tokens = {"access_token": access, "token_type": "Bearer", "expires_in": AuthConfig.OAUTH_ACCESS_SECONDS,
                  "refresh_token": refresh}
        return {**tokens, "scope": scope} if scope else tokens

    def take_refresh(self, token: str) -> dict[str, Any] | None:
        """Use up a refresh token of an account that is still active."""
        with self._connect() as connection:
            return connection.execute(
                """DELETE FROM oauth_tokens t USING users u
                   WHERE t.token_hash = %s AND t.kind = 'refresh' AND t.expires_at > now()
                     AND u.id = t.user_id AND u.active
                   RETURNING t.*""",
                (token_hash(token),),
            ).fetchone()

    def introspect(self, token: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            return connection.execute(
                """SELECT t.client_id, t.user_id, u.email, t.resource, t.scope, t.expires_at
                   FROM oauth_tokens t JOIN users u ON u.id = t.user_id
                   WHERE t.token_hash = %s AND t.kind = 'access' AND t.expires_at > now() AND u.active""",
                (token_hash(token),),
            ).fetchone()

    def revoke_user(self, user_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM oauth_tokens WHERE user_id = %s", (user_id,))
            connection.execute("DELETE FROM oauth_codes WHERE user_id = %s", (user_id,))


def valid_redirect_uri(uri: Any) -> bool:
    """HTTPS, or plain HTTP back to this machine for desktop clients; never a fragment."""
    if not isinstance(uri, str):
        return False
    parsed = urlparse(uri)
    if parsed.fragment or not parsed.hostname:
        return False
    return parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname in LOCAL_HOSTS)


def same_challenge(verifier: str, challenge: str) -> bool:
    digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return secrets.compare_digest(digest, challenge)


def error(code: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status, headers={"Cache-Control": "no-store"})


def oauth_routes(config: AuthConfig, sessions: SessionStore, oauth: OAuthStore) -> list[Route]:
    consent_signer = URLSafeTimedSerializer(config.secret, salt="luna-oauth-consent")

    def metadata_document(request: Request) -> Response:
        base = config.public_url
        return JSONResponse(
            {
                "issuer": base,
                "authorization_endpoint": f"{base}/oauth/authorize",
                "token_endpoint": f"{base}/oauth/token",
                "registration_endpoint": f"{base}/oauth/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
            }
        )

    async def metadata(request: Request) -> Response:
        return metadata_document(request)

    async def register(request: Request) -> Response:
        try:
            body = await request.json()
        except ValueError:
            return error("invalid_client_metadata")
        uris = body.get("redirect_uris") if isinstance(body, dict) else None
        if not (isinstance(uris, list) and 0 < len(uris) <= MAX_REDIRECT_URIS and all(map(valid_redirect_uri, uris))):
            return error("invalid_redirect_uri")
        name = str(body.get("client_name") or "An MCP client")[:MAX_CLIENT_NAME_CHARS]
        method = body.get("token_endpoint_auth_method") or "none"
        if method not in {"none", "client_secret_post", "client_secret_basic"}:
            return error("invalid_client_metadata")
        client_id, secret = oauth.register(name, uris, method != "none")
        answer = {
            "client_id": client_id,
            "client_name": name,
            "redirect_uris": uris,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": method,
        }
        if secret:
            answer |= {"client_secret": secret, "client_secret_expires_at": 0}
        return JSONResponse(answer, status_code=201, headers={"Cache-Control": "no-store"})

    def back_to_client(redirect_uri: str, **params: str | None) -> RedirectResponse:
        query = urlencode({key: value for key, value in params.items() if value is not None})
        return RedirectResponse(f"{redirect_uri}{'&' if '?' in redirect_uri else '?'}{query}", 303)

    async def authorize(request: Request) -> Response:
        params = request.query_params
        client = oauth.client(params.get("client_id", ""))
        redirect_uri = params.get("redirect_uri", "")
        # Without a known client and its exact address there is nowhere safe to send an error.
        if client is None or redirect_uri not in client["redirect_uris"]:
            return page("Not a known app", "This app's sign-in details are not recognised. Add it again.", 400)
        state = params.get("state")
        if params.get("response_type") != "code":
            return back_to_client(redirect_uri, error="unsupported_response_type", state=state)
        if not params.get("code_challenge") or params.get("code_challenge_method") != "S256":
            return back_to_client(redirect_uri, error="invalid_request", state=state)
        resource = params.get("resource") or None
        if resource and not config.allows_resource(resource):
            return back_to_client(redirect_uri, error="invalid_target", state=state)
        token = request.cookies.get(config.cookie_name)
        user = sessions.get(token) if token else None
        if user is None:
            here = f"{config.public_url}/oauth/authorize?{urlencode(list(params.multi_items()))}"
            return RedirectResponse(f"/login?{urlencode({'next': here})}", 303)
        signed = consent_signer.dumps(
            {
                "session": token_hash(token),
                "client_id": client["client_id"],
                "redirect_uri": redirect_uri,
                "code_challenge": params["code_challenge"],
                "resource": resource,
                "scope": params.get("scope"),
                "state": state,
            }
        )
        target = html.escape(urlparse(resource).hostname) if resource else "your Luna apps"
        button = "padding:.6rem 1.2rem;margin-right:.5rem;border-radius:.4rem;border:0;font-size:1rem"
        return page(
            "Allow access?",
            f"<p><b>{html.escape(client['client_name'])}</b> wants to read and change your data in "
            f"<b>{target}</b>, as <b>{html.escape(user['email'])}</b>.</p>"
            f"<p>It will return to <code>{html.escape(urlparse(redirect_uri).netloc)}</code>. "
            "Only allow this if you just added this app yourself.</p>"
            "<form method='post' action='/oauth/authorize'>"
            f"<input type='hidden' name='request' value='{html.escape(signed, quote=True)}'>"
            f"<button style='{button};background:#a5b4fc' name='decision' value='allow'>Allow</button>"
            f"<button style='{button}' name='decision' value='deny'>Deny</button></form>",
        )

    async def decide(request: Request) -> Response:
        fields = await form_fields(request)
        token = request.cookies.get(config.cookie_name, "")
        user = sessions.get(token) if token else None
        try:
            grant = consent_signer.loads(fields.get("request", ""), max_age=AuthConfig.OAUTH_CONSENT_SECONDS)
        except BadSignature:
            grant = None
        if user is None or grant is None or not secrets.compare_digest(grant["session"], token_hash(token)):
            return page("Not allowed", "This approval expired. Start again from the app.", 400)
        if fields.get("decision") != "allow":
            return back_to_client(grant["redirect_uri"], error="access_denied", state=grant["state"])
        code = oauth.issue_code(grant, user["user_id"])
        return back_to_client(grant["redirect_uri"], code=code, state=grant["state"], iss=config.public_url)

    def client_is_authentic(request: Request, fields: dict[str, str], client: dict[str, Any]) -> bool:
        if client["secret_hash"] is None:
            return True
        secret = fields.get("client_secret", "")
        scheme, _, credential = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() == "basic":
            try:
                _, _, secret = base64.b64decode(credential).decode().partition(":")
            except ValueError:
                return False
        return bool(secret) and secrets.compare_digest(token_hash(secret), client["secret_hash"])

    def client_id_of(request: Request, fields: dict[str, str]) -> str:
        scheme, _, credential = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() == "basic":
            try:
                return base64.b64decode(credential).decode().partition(":")[0]
            except ValueError:
                return ""
        return fields.get("client_id", "")

    async def token_endpoint(request: Request) -> Response:
        fields = await form_fields(request)
        client = oauth.client(client_id_of(request, fields))
        if client is None or not client_is_authentic(request, fields, client):
            return error("invalid_client", 401)
        grant_type = fields.get("grant_type")
        if grant_type == "authorization_code":
            grant = oauth.take_code(fields.get("code", ""))
            if (
                grant is None
                or grant["client_id"] != client["client_id"]
                or grant["redirect_uri"] != fields.get("redirect_uri")
                or not same_challenge(fields.get("code_verifier", ""), grant["code_challenge"])
            ):
                return error("invalid_grant")
        elif grant_type == "refresh_token":
            grant = oauth.take_refresh(fields.get("refresh_token", ""))
            if grant is None or grant["client_id"] != client["client_id"]:
                return error("invalid_grant")
        else:
            return error("unsupported_grant_type")
        requested = fields.get("resource")
        if requested and grant["resource"] and requested != grant["resource"]:
            return error("invalid_target")
        tokens = oauth.issue_tokens(client["client_id"], grant["user_id"], grant["resource"], grant["scope"])
        return JSONResponse(tokens, headers={"Cache-Control": "no-store"})

    async def introspect(request: Request) -> Response:
        """For MCP servers: whose access token this is. Holding a token is what lets you ask."""
        row = oauth.introspect((await form_fields(request)).get("token", ""))
        if row is None:
            return JSONResponse({"active": False}, headers={"Cache-Control": "no-store"})
        return JSONResponse(
            {"active": True, "sub": row["user_id"], "email": row["email"], "client_id": row["client_id"],
             "resource": row["resource"], "scope": row["scope"], "exp": int(row["expires_at"].timestamp())},
            headers={"Cache-Control": "no-store"},
        )

    return [
        Route("/.well-known/oauth-authorization-server", metadata),
        Route("/.well-known/openid-configuration", metadata),
        Route("/oauth/register", register, methods=["POST"]),
        Route("/oauth/authorize", authorize, methods=["GET"]),
        Route("/oauth/authorize", decide, methods=["POST"]),
        Route("/oauth/token", token_endpoint, methods=["POST"]),
        Route("/oauth/introspect", introspect, methods=["POST"]),
    ]
