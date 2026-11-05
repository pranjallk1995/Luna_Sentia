"""Tests for the sign-on, connector, and admin routes, with Google and the stores faked."""

import base64
import hashlib
import re
import unittest
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import httpx
from starlette.testclient import TestClient

from app.config import AuthConfig
from app.store import OWNER_ID, token_hash
from app.web import create_app

APP = "https://vita.lunaapps.fyi/"


class FakeStore:
    def __init__(self):
        self.rows = {}
        self.accounts = {"me@example.com": {"id": OWNER_ID, "email": "me@example.com", "active": True,
                                            "created_at": None, "last_seen_at": None, "removing": None}}
        self.hidden = set()

    def hidden_apps(self):
        return sorted(self.hidden)

    def hidden_from(self, user_id):
        return [] if user_id == OWNER_ID else self.hidden_apps()

    def set_app_hidden(self, app, hidden):
        (self.hidden.add if hidden else self.hidden.discard)(app)

    def user(self, user_id):
        return next((row for row in self.accounts.values() if row["id"] == user_id), None)

    def start_removal(self, user_id, apps):
        row = self.user(user_id)
        row["active"], row["removing"] = False, row["removing"] or list(apps)
        self.rows = {key: value for key, value in self.rows.items() if value["email"] != row["email"]}
        return row["removing"]

    def finish_removal(self, user_id, removed):
        row = self.user(user_id)
        row["removing"] = [app for app in row["removing"] if app not in removed]
        if not row["removing"]:
            del self.accounts[row["email"]]
        return row["removing"]

    def active_user(self, email):
        account = self.accounts.get(email)
        return account if account and account["active"] else None

    def users(self):
        return list(self.accounts.values())

    def add_user(self, email):
        account = self.accounts.setdefault(email, {"id": f"u{len(self.accounts)}", "email": email, "active": True,
                                                   "created_at": None, "last_seen_at": None, "removing": None})
        if account["removing"] is not None:
            raise ValueError("That person is still being deleted.")
        account["active"] = True

    def set_active(self, user_id, active):
        if user_id == OWNER_ID:
            raise ValueError("The owner cannot be disabled.")
        email = next(key for key, value in self.accounts.items() if value["id"] == user_id)
        self.accounts[email]["active"] = active
        if not active:
            self.rows = {key: row for key, row in self.rows.items() if row["email"] != email}

    def create(self, email, name, picture, lifetime):
        self.created = getattr(self, "created", 0) + 1
        token = f"token-{self.created}"
        self.rows[token_hash(token)] = {"email": email, "name": name, "picture": picture,
                                        "expires_at": datetime(2030, 1, 1, tzinfo=UTC)}
        return token

    def get(self, token):
        row = self.rows.get(token_hash(token))
        account = self.accounts.get(row["email"]) if row else None
        return {**row, "user_id": account["id"]} if account and account["active"] else None

    def delete(self, token):
        self.rows.pop(token_hash(token), None)


class FakeOAuth:
    """Keeps clients, codes, and tokens in memory, with the same use-once rules as the database."""

    def __init__(self):
        self.clients, self.codes, self.tokens, self.count = {}, {}, {}, 0

    def next(self, prefix):
        self.count += 1
        return f"{prefix}-{self.count}"

    def register(self, name, uris, with_secret):
        client_id, secret = self.next("client"), self.next("secret") if with_secret else None
        self.clients[client_id] = {"client_id": client_id, "client_name": name, "redirect_uris": uris,
                                   "secret_hash": token_hash(secret) if secret else None}
        return client_id, secret

    def client(self, client_id):
        return self.clients.get(client_id)

    def issue_code(self, grant, user_id):
        code = self.next("code")
        self.codes[code] = {**grant, "user_id": user_id}
        return code

    def take_code(self, code):
        return self.codes.pop(code, None)

    def issue_tokens(self, client_id, user_id, resource, scope):
        access, refresh = self.next("access"), self.next("refresh")
        row = {"client_id": client_id, "user_id": user_id, "resource": resource, "scope": scope}
        self.tokens[access], self.tokens[refresh] = {**row, "kind": "access"}, {**row, "kind": "refresh"}
        return {"access_token": access, "token_type": "Bearer", "expires_in": 3600, "refresh_token": refresh}

    def take_refresh(self, token):
        row = self.tokens.get(token)
        if row and row["kind"] == "refresh":
            return self.tokens.pop(token)
        return None

    def introspect(self, token):
        row = self.tokens.get(token)
        if not row or row["kind"] != "access":
            return None
        return {**row, "email": "me@example.com", "expires_at": datetime(2030, 1, 1, tzinfo=UTC)}

    def revoke_user(self, user_id):
        self.tokens = {key: row for key, row in self.tokens.items() if row["user_id"] != user_id}


class FakeGoogle:
    def __init__(self):
        self.profile = {"email": "Me@Example.com", "email_verified": True, "name": "Me", "picture": None}

    def authorization_url(self, state):
        return f"https://accounts.google.test/auth?state={state}"

    def user(self, code):
        if code != "good":
            raise ValueError("bad code")
        return self.profile


def config(**changes):
    values = dict(
        database_url="postgresql://x", public_url="https://auth.lunaapps.fyi", client_id="id", client_secret="s",
        secret="x" * 32, cookie_domain="lunaapps.fyi", owner_email="me@example.com",
        app_tokens={app: "t" * 32 for app in AuthConfig.APPS},
    )
    return AuthConfig(**{**values, **changes})


class SingleSignOnTests(unittest.TestCase):
    def setUp(self):
        self.store, self.google, self.oauth = FakeStore(), FakeGoogle(), FakeOAuth()
        self.app_calls, self.apps_down = [], set()

        def app_backend(request):
            app = request.url.host.removeprefix("luna-").removesuffix("-mcp")
            self.app_calls.append((app, request.url.path, request.headers["authorization"], request.headers["x-luna-user"]))
            return httpx.Response(503 if app in self.apps_down else 200, json={})

        self.client = TestClient(create_app(config(), self.store, self.google, self.oauth, httpx.MockTransport(app_backend)), base_url="https://auth.lunaapps.fyi",
                                 follow_redirects=False)

    def sign_in(self, next_url=APP, code="good"):
        start = self.client.get("/login", params={"next": next_url})
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        return self.client.get("/callback", params={"code": code, "state": state})

    def test_sign_in_sets_a_parent_domain_cookie_and_returns_to_the_app(self):
        done = self.sign_in()
        self.assertEqual((done.status_code, done.headers["location"]), (303, APP))
        cookie = [value for key, value in done.headers.multi_items() if key == "set-cookie" and "luna_sso=" in value][0]
        self.assertIn("Domain=lunaapps.fyi", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)

    def test_apps_resolve_the_session_server_side(self):
        self.sign_in()
        token = self.client.cookies["luna_sso"]
        who = self.client.get("/session", headers={"X-Luna-Session": token})
        self.assertEqual((who.status_code, who.json()["email"]), (200, "me@example.com"))
        self.assertEqual(who.json()["user_id"], OWNER_ID)
        self.assertEqual(self.client.get("/session", headers={"X-Luna-Session": "forged"}).status_code, 401)

    def test_a_second_app_is_signed_in_without_google(self):
        self.sign_in()
        again = self.client.get("/login", params={"next": "https://lexia.lunaapps.fyi/"})
        self.assertEqual(again.headers["location"], "https://lexia.lunaapps.fyi/")

    def test_logout_ends_the_session_for_every_app(self):
        self.sign_in()
        token = self.client.cookies["luna_sso"]
        out = self.client.get("/logout", params={"next": APP})
        self.assertEqual(out.headers["location"], APP)
        self.assertEqual(self.client.get("/session", headers={"X-Luna-Session": token}).status_code, 401)

    def test_only_luna_apps_are_return_addresses(self):
        self.assertEqual(self.client.get("/login", params={"next": "https://evil.example/"}).status_code, 400)
        self.assertEqual(self.client.get("/login", params={"next": "http://vita.lunaapps.fyi/"}).status_code, 400)
        self.assertTrue(config().allows_next("https://lunaapps.fyi/"))
        self.assertFalse(config().allows_next("https://notlunaapps.fyi/"))
        self.assertTrue(config(cookie_domain="").allows_next("http://127.0.0.1:18502/"))

    def test_unlisted_or_unverified_accounts_are_refused(self):
        self.google.profile = {"email": "someone@else.com", "email_verified": True}
        self.assertEqual(self.sign_in().status_code, 403)
        self.google.profile = {"email": "me@example.com", "email_verified": False}
        self.assertEqual(self.sign_in().status_code, 403)
        self.assertEqual(self.store.rows, {})

    def test_state_must_match_this_browser(self):
        start = self.client.get("/login", params={"next": APP})
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        self.client.cookies.delete(AuthConfig.STATE_COOKIE)
        self.assertEqual(self.client.get("/callback", params={"code": "good", "state": state}).status_code, 400)
        self.assertEqual(self.client.get("/callback", params={"code": "good", "state": "forged"}).status_code, 400)
        self.assertEqual(self.sign_in(code="bad").status_code, 502)


CLIENT_URI = "https://claude.ai/api/mcp/auth_callback"
RESOURCE = "https://vita-mcp.lunaapps.fyi/mcp"
VERIFIER = "v" * 50
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()


class ConnectorTests(SingleSignOnTests):
    def client_id(self, **body):
        answer = self.client.post("/oauth/register", json={"redirect_uris": [CLIENT_URI], "client_name": "Claude", **body})
        self.assertEqual(answer.status_code, 201)
        return answer.json()

    def authorize(self, client_id, **changes):
        params = {"client_id": client_id, "redirect_uri": CLIENT_URI, "response_type": "code", "state": "s1",
                  "code_challenge": CHALLENGE, "code_challenge_method": "S256", "resource": RESOURCE, **changes}
        return self.client.get("/oauth/authorize", params=params)

    def approve(self, client_id, decision="allow"):
        consent = self.authorize(client_id)
        signed = re.search(r"name='request' value='([^']+)'", consent.text)[1]
        return self.client.post("/oauth/authorize", data={"request": signed, "decision": decision})

    def code(self, client_id):
        return parse_qs(urlparse(self.approve(client_id).headers["location"]).query)["code"][0]

    def exchange(self, client_id, code, **changes):
        return self.client.post("/oauth/token", data={
            "grant_type": "authorization_code", "client_id": client_id, "code": code,
            "redirect_uri": CLIENT_URI, "code_verifier": VERIFIER, **changes})

    def test_metadata_describes_the_flow(self):
        document = self.client.get("/.well-known/oauth-authorization-server").json()
        self.assertEqual(document["issuer"], "https://auth.lunaapps.fyi")
        self.assertEqual(document["code_challenge_methods_supported"], ["S256"])

    def test_registration_refuses_unsafe_redirects(self):
        for uri in ("http://evil.example/cb", "https://ok.example/cb#frag", "javascript:alert(1)"):
            self.assertEqual(self.client.post("/oauth/register", json={"redirect_uris": [uri]}).status_code, 400)
        self.assertEqual(self.client.post("/oauth/register", json={"redirect_uris": []}).status_code, 400)

    def test_signed_out_people_sign_in_first(self):
        answer = self.authorize(self.client_id()["client_id"])
        self.assertEqual(answer.status_code, 303)
        self.assertTrue(answer.headers["location"].startswith("/login?next="))

    def test_full_flow_identifies_the_person_and_works_once(self):
        self.sign_in()
        client_id = self.client_id()["client_id"]
        approved = self.approve(client_id)
        location = urlparse(approved.headers["location"])
        self.assertEqual(f"{location.scheme}://{location.netloc}{location.path}", CLIENT_URI)
        query = parse_qs(location.query)
        self.assertEqual(query["state"], ["s1"])
        tokens = self.exchange(client_id, query["code"][0]).json()
        who = self.client.post("/oauth/introspect", data={"token": tokens["access_token"]}).json()
        self.assertEqual((who["active"], who["sub"], who["resource"]), (True, OWNER_ID, RESOURCE))
        self.assertEqual(self.exchange(client_id, query["code"][0]).status_code, 400)

    def test_consent_cannot_be_skipped_or_replayed_by_another_session(self):
        self.sign_in()
        client_id = self.client_id()["client_id"]
        consent = self.authorize(client_id)
        self.assertIn("Allow access?", consent.text)
        self.assertEqual(self.oauth.codes, {})
        signed = re.search(r"name='request' value='([^']+)'", consent.text)[1]
        self.client.get("/logout")
        self.sign_in()
        self.assertEqual(self.client.post("/oauth/authorize", data={"request": signed, "decision": "allow"}).status_code, 400)
        self.assertEqual(self.client.post("/oauth/authorize", data={"request": "forged", "decision": "allow"}).status_code, 400)
        self.assertEqual(self.oauth.codes, {})

    def test_denying_returns_an_error_and_no_code(self):
        self.sign_in()
        location = self.approve(self.client_id()["client_id"], "deny").headers["location"]
        self.assertIn("error=access_denied", location)
        self.assertEqual(self.oauth.codes, {})

    def test_exchange_requires_the_verifier_client_and_address(self):
        self.sign_in()
        client_id = self.client_id()["client_id"]
        self.assertEqual(self.exchange(client_id, self.code(client_id), code_verifier="wrong").status_code, 400)
        self.assertEqual(self.exchange(client_id, self.code(client_id), redirect_uri="https://x.example/").status_code, 400)
        other = self.client_id()["client_id"]
        self.assertEqual(self.exchange(other, self.code(client_id)).status_code, 400)
        self.assertEqual(self.exchange("nobody", self.code(client_id)).status_code, 401)

    def test_unknown_clients_and_addresses_are_never_redirected_to(self):
        self.sign_in()
        client_id = self.client_id()["client_id"]
        self.assertEqual(self.authorize("nobody").status_code, 400)
        self.assertEqual(self.authorize(client_id, redirect_uri="https://evil.example/cb").status_code, 400)

    def test_authorize_requires_pkce_and_a_luna_resource(self):
        self.sign_in()
        client_id = self.client_id()["client_id"]
        self.assertIn("error=invalid_request", self.authorize(client_id, code_challenge="").headers["location"])
        self.assertIn("error=invalid_request", self.authorize(client_id, code_challenge_method="plain").headers["location"])
        self.assertIn("error=invalid_target", self.authorize(client_id, resource="https://evil.example/mcp").headers["location"])

    def test_refresh_renews_tokens_and_the_old_one_stops_working(self):
        self.sign_in()
        client_id = self.client_id()["client_id"]
        first = self.exchange(client_id, self.code(client_id)).json()
        data = {"grant_type": "refresh_token", "client_id": client_id, "refresh_token": first["refresh_token"]}
        self.assertEqual(self.client.post("/oauth/token", data=data).status_code, 200)
        self.assertEqual(self.client.post("/oauth/token", data=data).status_code, 400)

    def test_a_refresh_token_is_not_an_access_token(self):
        self.sign_in()
        client_id = self.client_id()["client_id"]
        tokens = self.exchange(client_id, self.code(client_id)).json()
        self.assertFalse(self.client.post("/oauth/introspect", data={"token": tokens["refresh_token"]}).json()["active"])

    def test_a_client_with_a_secret_must_present_it(self):
        self.sign_in()
        registered = self.client_id(token_endpoint_auth_method="client_secret_post")
        client_id, secret = registered["client_id"], registered["client_secret"]
        self.assertEqual(self.exchange(client_id, self.code(client_id)).status_code, 401)
        self.assertEqual(self.exchange(client_id, self.code(client_id), client_secret=secret).status_code, 200)


class AdminTests(SingleSignOnTests):
    def form(self):
        return re.search(r"name='form' value='([^']+)'", self.client.get("/admin").text)[1]

    def test_add_then_disable_ends_their_access(self):
        self.sign_in()
        self.client.post("/admin/add", data={"form": self.form(), "email": "Friend@Example.com"})
        self.assertIn("friend@example.com", self.client.get("/admin").text)
        user_id = self.store.accounts["friend@example.com"]["id"]
        self.oauth.tokens["t"] = {"user_id": user_id, "kind": "access", "client_id": "c", "resource": None, "scope": None}
        self.client.post("/admin/disable", data={"form": self.form(), "user_id": user_id})
        self.assertIsNone(self.store.active_user("friend@example.com"))
        self.assertNotIn("t", self.oauth.tokens)

    def test_a_listed_person_gets_their_own_id_and_cannot_administer(self):
        self.store.add_user("friend@example.com")
        self.google.profile = {"email": "friend@example.com", "email_verified": True}
        self.sign_in()
        who = self.client.get("/session", headers={"X-Luna-Session": self.client.cookies["luna_sso"]}).json()
        self.assertNotEqual(who["user_id"], OWNER_ID)
        self.assertEqual(self.client.get("/admin").status_code, 403)
        self.assertEqual(self.client.post("/admin/add", data={"form": "x", "email": "a@b.co"}).status_code, 403)
        self.assertNotIn("a@b.co", self.store.accounts)

    def test_forms_need_a_token_and_the_owner_stays_active(self):
        self.sign_in()
        self.assertEqual(self.client.post("/admin/add", data={"form": "x", "email": "a@b.co"}).status_code, 403)
        self.assertEqual(self.client.post("/admin/disable", data={"form": self.form(), "user_id": OWNER_ID}).status_code, 400)
        self.assertEqual(self.client.post("/admin/add", data={"form": self.form(), "email": "nonsense"}).status_code, 400)


class DeleteAndHideTests(AdminTests):
    def add_friend(self):
        self.sign_in()
        self.client.post("/admin/add", data={"form": self.form(), "email": "friend@example.com"})
        return self.store.accounts["friend@example.com"]["id"]

    def delete(self, user_id, confirmed=True):
        data = {"form": self.form(), "user_id": user_id, **({"confirmed": "yes"} if confirmed else {})}
        return self.client.post("/admin/delete", data=data)

    def test_delete_asks_first_and_changes_nothing_until_confirmed(self):
        user_id = self.add_friend()
        answer = self.delete(user_id, confirmed=False)
        self.assertIn("cannot be undone", answer.text)
        self.assertIn("friend@example.com", answer.text)
        self.assertEqual(self.app_calls, [])
        self.assertTrue(self.store.accounts["friend@example.com"]["active"])

    def test_delete_removes_data_from_every_app_then_the_account(self):
        user_id = self.add_friend()
        self.oauth.tokens["t"] = {"user_id": user_id, "kind": "access", "client_id": "c", "resource": None, "scope": None}
        self.assertEqual(self.delete(user_id).status_code, 303)
        self.assertEqual({call[0] for call in self.app_calls}, set(AuthConfig.APPS))
        for _, path, token, acting in self.app_calls:
            self.assertEqual((path, token, acting), (f"/api/users/{user_id}", "Bearer " + "t" * 32, OWNER_ID))
        self.assertNotIn("friend@example.com", self.store.accounts)
        self.assertNotIn("t", self.oauth.tokens)

    def test_a_stopped_app_keeps_the_delete_pending_and_a_retry_finishes_it(self):
        user_id = self.add_friend()
        self.apps_down = {"lexia"}
        answer = self.delete(user_id)
        self.assertEqual(answer.status_code, 409)
        self.assertIn("lexia", answer.text)
        row = self.store.accounts["friend@example.com"]
        self.assertFalse(row["active"])
        self.assertEqual(row["removing"], ["lexia"])
        self.assertIn("Retry delete", self.client.get("/admin").text)
        self.app_calls.clear()
        self.apps_down = set()
        self.assertEqual(self.delete(user_id).status_code, 303)
        self.assertEqual([call[0] for call in self.app_calls], ["lexia"])
        self.assertNotIn("friend@example.com", self.store.accounts)

    def test_the_owner_cannot_be_deleted_and_others_cannot_delete(self):
        self.sign_in()
        self.assertEqual(self.delete(OWNER_ID).status_code, 400)
        self.store.add_user("friend@example.com")
        form = self.form()
        self.client.get("/logout")
        self.client.cookies.clear()
        self.google.profile = {"email": "friend@example.com", "email_verified": True}
        self.sign_in()
        data = {"form": form, "user_id": OWNER_ID, "confirmed": "yes"}
        self.assertEqual(self.client.post("/admin/delete", data=data).status_code, 403)
        self.assertEqual(self.app_calls, [])

    def test_hiding_an_app_reaches_others_but_never_the_owner(self):
        user_id = self.add_friend()
        self.client.post("/admin/apps", data={"form": self.form(), "app": "lumia", "hidden": "yes"})
        page = self.client.get("/admin").text
        self.assertIn("<th>Status</th><th>Change to</th>", page)
        self.assertIn("Hidden from others", page)
        self.assertIn("<button>Show to everyone</button>", page)
        self.assertIn("<button>Hide from others</button>", page)
        mine = self.client.get("/session", headers={"X-Luna-Session": self.client.cookies["luna_sso"]}).json()
        self.assertEqual(mine["hidden_apps"], [])
        self.assertEqual(self.store.hidden_from(user_id), ["lumia"])
        self.client.post("/admin/apps", data={"form": self.form(), "app": "lumia", "hidden": "no"})
        self.assertEqual(self.store.hidden_from(user_id), [])

    def test_only_real_apps_can_be_hidden(self):
        self.sign_in()
        self.assertEqual(self.client.post("/admin/apps", data={"form": self.form(), "app": "nope", "hidden": "yes"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
