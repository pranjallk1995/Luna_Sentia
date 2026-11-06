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
-- Set while someone is being deleted: the apps that have not yet removed their data.
ALTER TABLE users ADD COLUMN IF NOT EXISTS removing text[];
-- Apps the owner has hidden from everyone else.
CREATE TABLE IF NOT EXISTS hidden_apps (app text PRIMARY KEY);
-- Each person's PIN for the hidden items in Visia, Lexia, and Vibia, stored only as a hash.
-- failed_attempts counts wrong guesses since the last right one; locked_until stops guessing.
CREATE TABLE IF NOT EXISTS hidden_pins (
    user_id text PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    salt bytea NOT NULL,
    pin_hash bytea NOT NULL,
    iterations integer NOT NULL,
    failed_attempts integer NOT NULL DEFAULT 0,
    locked_until timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
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
                   RETURNING u.id AS user_id, s.email, s.name, s.picture, s.expires_at,
                             s.created_at AS signed_in_at""",
                (token_hash(token),),
            ).fetchone()

    def delete(self, token: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = %s", (token_hash(token),))

    # Accounts

    def sync_owner(self, owner_email: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO users (id, email) VALUES (%s, %s)
                   ON CONFLICT (id) DO UPDATE SET email = EXCLUDED.email, active = true""",
                (OWNER_ID, owner_email),
            )

    def active_user(self, email: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            return connection.execute(
                "SELECT id, email FROM users WHERE email = %s AND active", (email,)
            ).fetchone()

    def users(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return connection.execute(
                """SELECT u.id, u.email, u.active, u.removing, u.created_at, max(s.last_seen_at) AS last_seen_at
                   FROM users u LEFT JOIN sessions s ON s.email = u.email
                   GROUP BY u.id ORDER BY u.id <> %s, u.created_at""",
                (OWNER_ID,),
            ).fetchall()

    def add_user(self, email: str) -> None:
        """Add an account, or let a disabled one sign in again."""
        with self._connect() as connection:
            added = connection.execute(
                """INSERT INTO users (id, email) VALUES (%s, %s)
                   ON CONFLICT (email) DO UPDATE SET active = true WHERE users.removing IS NULL
                   RETURNING id""",
                (secrets.token_hex(8), email),
            ).fetchone()
        if added is None:
            raise ValueError("That person is still being deleted. Finish that first.")

    def set_active(self, user_id: str, active: bool) -> None:
        """Enable or disable an account; disabling ends its sessions. The owner stays active."""
        if user_id == OWNER_ID:
            raise ValueError("The owner cannot be disabled.")
        with self._connect() as connection:
            connection.execute("UPDATE users SET active = %s WHERE id = %s AND removing IS NULL", (active, user_id))
            if not active:
                connection.execute(
                    "DELETE FROM sessions WHERE email = (SELECT email FROM users WHERE id = %s)", (user_id,)
                )

    def user(self, user_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            return connection.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()

    def start_removal(self, user_id: str, apps: tuple[str, ...]) -> list[str]:
        """Shut someone out for good; returns the apps still holding their data."""
        if user_id == OWNER_ID:
            raise ValueError("The owner cannot be deleted.")
        with self._connect() as connection:
            row = connection.execute(
                """UPDATE users SET active = false, removing = COALESCE(removing, %s)
                   WHERE id = %s RETURNING email, removing""",
                (list(apps), user_id),
            ).fetchone()
            if row is None:
                raise ValueError("That person no longer exists.")
            connection.execute("DELETE FROM sessions WHERE email = %s", (row["email"],))
        return row["removing"]

    def finish_removal(self, user_id: str, removed: list[str]) -> list[str]:
        """Record the apps that removed their data; the account goes once none are left."""
        with self._connect() as connection:
            row = connection.execute(
                """UPDATE users SET removing = ARRAY(SELECT unnest(removing) EXCEPT SELECT unnest(%s::text[]))
                   WHERE id = %s RETURNING removing""",
                (removed, user_id),
            ).fetchone()
            remaining = row["removing"] if row else []
            if row and not remaining:
                connection.execute("DELETE FROM users WHERE id = %s", (user_id,))
        return remaining

    # Hidden apps

    def hidden_apps(self) -> list[str]:
        with self._connect() as connection:
            return [row["app"] for row in connection.execute("SELECT app FROM hidden_apps ORDER BY app").fetchall()]

    def hidden_from(self, user_id: str) -> list[str]:
        """The apps this person cannot use: none for the owner, who hides them."""
        return [] if user_id == OWNER_ID else self.hidden_apps()

    def set_app_hidden(self, app: str, hidden: bool) -> None:
        with self._connect() as connection:
            if hidden:
                connection.execute("INSERT INTO hidden_apps (app) VALUES (%s) ON CONFLICT DO NOTHING", (app,))
            else:
                connection.execute("DELETE FROM hidden_apps WHERE app = %s", (app,))

    # Hidden-items PINs

    def pin_state(self, user_id: str) -> dict[str, Any] | None:
        """Whether this person has a PIN, and how long it stays locked; None without one."""
        with self._connect() as connection:
            return connection.execute(
                """SELECT updated_at,
                          GREATEST(EXTRACT(EPOCH FROM locked_until - now()), 0)::int AS locked_seconds
                   FROM hidden_pins WHERE user_id = %s""",
                (user_id,),
            ).fetchone()

    def set_pin(self, user_id: str, salt: bytes, pin_hash: bytes, iterations: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO hidden_pins (user_id, salt, pin_hash, iterations) VALUES (%s, %s, %s, %s)
                   ON CONFLICT (user_id) DO UPDATE SET salt = EXCLUDED.salt, pin_hash = EXCLUDED.pin_hash,
                       iterations = EXCLUDED.iterations, failed_attempts = 0, locked_until = NULL,
                       updated_at = now()""",
                (user_id, salt, pin_hash, iterations),
            )

    def take_pin_attempt(self, user_id: str, limit: int, lock: timedelta) -> dict[str, Any] | None:
        """Count one guess before it is checked, so parallel guesses cannot pass the limit.

        Returns the stored hash, or None while locked or without a PIN. The guess that
        reaches ``limit`` locks the PIN; once that lock ends, each wrong guess locks it again.
        """
        with self._connect() as connection:
            return connection.execute(
                """UPDATE hidden_pins SET failed_attempts = failed_attempts + 1,
                       locked_until = CASE WHEN failed_attempts + 1 >= %s THEN now() + %s END
                   WHERE user_id = %s AND (locked_until IS NULL OR locked_until <= now())
                   RETURNING salt, pin_hash, iterations, GREATEST(%s - failed_attempts, 0) AS attempts_left""",
                (limit, lock, user_id, limit),
            ).fetchone()

    def clear_pin_attempts(self, user_id: str) -> None:
        """A right guess: forget the wrong ones and any lock they caused."""
        with self._connect() as connection:
            connection.execute(
                "UPDATE hidden_pins SET failed_attempts = 0, locked_until = NULL WHERE user_id = %s", (user_id,)
            )
