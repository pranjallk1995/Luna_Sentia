"""Tests for the single sign-on routes, with Google and the session store faked."""

import unittest
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

from starlette.testclient import TestClient

from app.config import AuthConfig
from app.store import token_hash
from app.web import create_app

APP = "https://vita.lunaapps.fyi/"


class FakeStore:
    def __init__(self):
        self.rows = {}

    def create(self, email, name, picture, lifetime):
        token = f"token-{len(self.rows)}"
        self.rows[token_hash(token)] = {"email": email, "name": name, "picture": picture,
                                        "expires_at": datetime(2030, 1, 1, tzinfo=UTC)}
        return token

    def get(self, token):
        return self.rows.get(token_hash(token))

    def delete(self, token):
        self.rows.pop(token_hash(token), None)


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
        secret="x" * 32, allowed_emails=frozenset({"me@example.com"}), cookie_domain="lunaapps.fyi",
    )
    return AuthConfig(**{**values, **changes})


class SingleSignOnTests(unittest.TestCase):
    def setUp(self):
        self.store, self.google = FakeStore(), FakeGoogle()
        self.client = TestClient(create_app(config(), self.store, self.google), base_url="https://auth.lunaapps.fyi",
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


if __name__ == "__main__":
    unittest.main()
