"""Accounts and sign-in sessions in PostgreSQL. Only a hash of each session token is stored."""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Any

import psycopg
from psycopg.rows import dict_row

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    token_hash char(64) PRIMARY KEY,
    email text NOT NULL,
    name text,
    picture text,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_expiry_idx ON sessions (expires_at);
-- Everyone who may sign in. Each app keeps a user's data in a schema and folder named
-- after ``id``; the one user with the id ``owner`` also controls the home's devices.
CREATE TABLE IF NOT EXISTS users (
    id text PRIMARY KEY,
    email text NOT NULL UNIQUE,
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);
"""

OWNER_ID = "owner"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class SessionStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.database_url, row_factory=dict_row, autocommit=True)

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(SCHEMA)

    def create(self, email: str, name: str | None, picture: str | None, lifetime: timedelta) -> str:
        token = secrets.token_urlsafe(32)
        with self._connect() as connection:
            connection.execute("DELETE FROM sessions WHERE expires_at < now()")
            connection.execute(
                """INSERT INTO sessions (token_hash, email, name, picture, expires_at)
                   VALUES (%s, %s, %s, %s, now() + %s)""",
                (token_hash(token), email, name, picture, lifetime),
            )
        return token

    def get(self, token: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            return connection.execute(
                """UPDATE sessions s SET last_seen_at = now()
                   FROM users u
                   WHERE s.token_hash = %s AND s.expires_at > now() AND u.email = s.email AND u.active
                   RETURNING u.id AS user_id, s.email, s.name, s.picture, s.expires_at""",
                (token_hash(token),),
            ).fetchone()

    def delete(self, token: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = %s", (token_hash(token),))

    # Accounts

    def sync_users(self, owner_email: str, allowed_emails: frozenset[str]) -> None:
        """Make sure the owner and every listed account exist; never removes anyone."""
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO users (id, email) VALUES (%s, %s)
                   ON CONFLICT (id) DO UPDATE SET email = EXCLUDED.email, active = true""",
                (OWNER_ID, owner_email),
            )
            for email in sorted(allowed_emails):
                connection.execute(
                    "INSERT INTO users (id, email) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (secrets.token_hex(8), email),
                )

    def active_user(self, email: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            return connection.execute(
                "SELECT id, email FROM users WHERE email = %s AND active", (email,)
            ).fetchone()

    def users(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return connection.execute(
                """SELECT u.id, u.email, u.active, u.created_at, max(s.last_seen_at) AS last_seen_at
                   FROM users u LEFT JOIN sessions s ON s.email = u.email
                   GROUP BY u.id ORDER BY u.id <> %s, u.created_at""",
                (OWNER_ID,),
            ).fetchall()

    def add_user(self, email: str) -> None:
        """Add an account, or let a disabled one sign in again."""
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO users (id, email) VALUES (%s, %s)
                   ON CONFLICT (email) DO UPDATE SET active = true""",
                (secrets.token_hex(8), email),
            )

    def set_active(self, user_id: str, active: bool) -> None:
        """Enable or disable an account; disabling ends its sessions. The owner stays active."""
        if user_id == OWNER_ID:
            raise ValueError("The owner cannot be disabled.")
        with self._connect() as connection:
            connection.execute("UPDATE users SET active = %s WHERE id = %s", (active, user_id))
            if not active:
                connection.execute(
                    "DELETE FROM sessions WHERE email = (SELECT email FROM users WHERE id = %s)", (user_id,)
                )
