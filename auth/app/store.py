"""Sign-in sessions in PostgreSQL. Only a hash of each session token is stored."""

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
"""


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
                """UPDATE sessions SET last_seen_at = now()
                   WHERE token_hash = %s AND expires_at > now()
                   RETURNING email, name, picture, expires_at""",
                (token_hash(token),),
            ).fetchone()

    def delete(self, token: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = %s", (token_hash(token),))
