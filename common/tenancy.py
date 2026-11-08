"""Whose data the current request or job works on. Identical in every Luna app.

Every database connection and vault folder is chosen from this, and nothing falls back
to anyone when it is unset, so code that forgets to say who it acts for fails rather
than reading another person's data. New threads start with no user: pass one on with
``acting_as`` or ``contextvars.copy_context``.
"""

import re
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

# The one account that also controls the home's lights and speakers (Luna Sentia's users table).
OWNER = "owner"
USER_HEADER = "x-luna-user"
SCHEMA_PREFIX = "u_"
_VALID_USER = re.compile(r"[a-z0-9]{1,32}")
_current: ContextVar[str | None] = ContextVar("luna_user", default=None)
# People deleted while this process runs: work still in flight for them stops here.
_removed: set[str] = set()
# Only the test suites set this, so they can exercise services without naming a user.
_test_user: str | None = None


class NoUserError(RuntimeError):
    """Raised when code touches a person's data without saying whose."""


def is_valid(user_id: Any) -> bool:
    return isinstance(user_id, str) and _VALID_USER.fullmatch(user_id) is not None


def current() -> str:
    user_id = _current.get() or _test_user
    if user_id is None:
        raise NoUserError("No user is set for this request or job.")
    if user_id in _removed:
        raise NoUserError("This person has been deleted.")
    return user_id


@contextmanager
def acting_as(user_id: str) -> Iterator[None]:
    if not is_valid(user_id):
        raise ValueError("Not a valid user id.")
    if user_id in _removed:
        raise NoUserError("This person has been deleted.")
    token = _current.set(user_id)
    try:
        yield
    finally:
        _current.reset(token)


def space_name(user_id: str | None = None) -> str:
    """The name of a person's database schema and vault folder."""
    user_id = user_id or current()
    if not is_valid(user_id):
        raise ValueError("Not a valid user id.")
    return f"{SCHEMA_PREFIX}{user_id}"


def forbid(user_id: str) -> None:
    """Stop all work for a person being deleted, so nothing recreates their data."""
    if user_id == OWNER or not is_valid(user_id):
        raise ValueError("That person cannot be deleted.")
    _removed.add(user_id)


def is_owner() -> bool:
    return current() == OWNER


def require_owner(what: str) -> None:
    if not is_owner():
        raise PermissionError(f"{what} is only available to the owner of this Luna home.")


class PerUser:
    """One instance of something stateful per person, used like the instance itself."""

    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory
        self._instances: dict[str, Any] = {}
        self._lock = threading.Lock()

    def forget(self, user_id: str) -> None:
        with self._lock:
            self._instances.pop(user_id, None)

    def mine(self) -> Any:
        """The current person's instance."""
        user_id = current()
        with self._lock:
            if user_id not in self._instances:
                self._instances[user_id] = self._factory()
            return self._instances[user_id]

    def __getattr__(self, name: str) -> Any:
        return getattr(self.mine(), name)
