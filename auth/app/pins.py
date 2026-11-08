"""Each person's PIN for hidden items, which Luna Visia, Lexia, and Vibia check before showing them.

People set it at ``/pin``. Setting a first PIN, or one whose old PIN is forgotten, needs a
Google sign-in from the last few minutes, so a copied session cookie is not enough. The apps
call ``/pin/status`` and ``/pin/verify`` with the person's session token; wrong guesses lock
the PIN for a while, and only a hash of it is stored.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlparse

from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from app.config import AuthConfig
from app.pages import form_fields
from app.store import SessionStore, token_hash

PIN = re.compile(r"\d{6,12}")
ITERATIONS = 600_000
MAX_ATTEMPTS = 5
LOCK = timedelta(minutes=15)
# How recent a Google sign-in must be to set a PIN without the current one.
FRESH_SIGN_IN = timedelta(minutes=10)
FORM_SECONDS = 600

GOOGLE_MARK = (
    "<svg viewBox='0 0 24 24' aria-hidden='true'>"
    "<path fill='#4285F4' d='M21.6 12.23c0-.71-.06-1.4-.18-2.07H12v3.92h5.38a4.6 4.6 0 0 1-2 3.02v2.54h3.24c1.9-1.75 2.98-4.33 2.98-7.41z'/>"
    "<path fill='#34A853' d='M12 22c2.7 0 4.97-.9 6.62-2.43l-3.24-2.54c-.9.6-2.05.96-3.38.96-2.6 0-4.81-1.76-5.6-4.13H3.06v2.62A10 10 0 0 0 12 22z'/>"
    "<path fill='#FBBC05' d='M6.4 13.86A6 6 0 0 1 6.08 12c0-.65.11-1.28.32-1.86V7.52H3.06A10 10 0 0 0 2 12c0 1.61.39 3.14 1.06 4.48l3.34-2.62z'/>"
    "<path fill='#EA4335' d='M12 6.01c1.47 0 2.79.5 3.83 1.5l2.87-2.87A9.63 9.63 0 0 0 12 2a10 10 0 0 0-8.94 5.52l3.34 2.62C7.19 7.77 9.4 6.01 12 6.01z'/>"
    "</svg>"
)
LOCK_MARK = (
    "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' "
    "stroke-linejoin='round' aria-hidden='true'><rect x='4' y='11' width='16' height='10' rx='2'/>"
    "<path d='M8 11V7a4 4 0 0 1 8 0v4'/><circle cx='12' cy='16' r='1.3' fill='currentColor'/></svg>"
)
# The Luna apps' dark, monochrome look, so this page feels like part of them.
PIN_STYLE = """
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px 16px;color:#f5f5f5;
 font:14px/1.55 "Source Code Pro",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
 background:radial-gradient(circle at 82% -10%,rgba(255,255,255,.07),transparent 30rem),#111}
main{width:100%;max-width:26rem;padding:28px 24px;border:1px solid rgba(255,255,255,.14);border-radius:16px;
 background:rgba(255,255,255,.055);box-shadow:0 20px 60px rgba(0,0,0,.35)}
.mark{display:grid;place-items:center;width:52px;height:52px;margin:0 auto 14px;border:1px solid rgba(255,255,255,.18);
 border-radius:14px;background:rgba(255,255,255,.07)}
.mark svg{width:26px;height:26px}
h1{margin:0;text-align:center;font-size:1.25rem;letter-spacing:.01em}
.lead{margin:6px 0 18px;text-align:center;color:#a3a3a3;font-size:.8rem}
.chip{display:block;width:max-content;max-width:100%;margin:0 auto 18px;padding:3px 10px;border:1px solid rgba(255,255,255,.14);
 border-radius:999px;color:#a3a3a3;font-size:.72rem;text-align:center}
.chip.on{color:#86efac;border-color:rgba(134,239,172,.35);background:rgba(134,239,172,.07)}
label{display:block;margin:0 0 14px;color:#d4d4d4;font-size:.8rem}
label small{color:#8a8a8a}
input{display:block;width:100%;margin-top:6px;padding:10px 12px;border:1px solid rgba(255,255,255,.14);border-radius:10px;
 background:rgba(0,0,0,.25);color:#f5f5f5;font:inherit;font-size:1.15rem;letter-spacing:.35em;text-align:center}
input:focus{outline:none;border-color:rgba(255,255,255,.45);box-shadow:0 0 0 3px rgba(255,255,255,.08)}
hr{margin:6px 0 16px;border:0;border-top:1px solid rgba(255,255,255,.1)}
button,.button,.google{display:flex;gap:10px;align-items:center;justify-content:center;width:100%;min-height:42px;
 padding:9px 14px;border:1px solid rgba(255,255,255,.14);border-radius:10px;background:rgba(255,255,255,.07);
 color:#f5f5f5;font:inherit;font-weight:600;text-decoration:none;cursor:pointer}
button:hover,.button:hover{background:rgba(255,255,255,.12)}
.primary{background:#f5f5f5;color:#111;border-color:#f5f5f5}
.primary:hover{background:#e5e5e5}
.google{background:#fff;color:#1f1f1f;border-color:#fff}
.google svg{width:18px;height:18px}
.alert{margin:0 0 16px;padding:9px 12px;border:1px solid rgba(248,113,113,.45);border-radius:10px;
 background:rgba(127,29,29,.35);color:#fecaca;font-size:.8rem}
p{margin:0 0 14px;color:#d4d4d4;font-size:.82rem}
.quiet{margin:14px 0 0;color:#a3a3a3;font-size:.76rem;text-align:center}
a{color:#f5f5f5}
footer{margin-top:20px;text-align:center;font-size:.76rem;color:#a3a3a3}
footer a{color:#a3a3a3}
"""


def app_name(url: str) -> str:
    """"Luna Visia" for visia.lunaapps.fyi; plainer for anything else."""
    host = urlparse(url).hostname or ""
    first = host.split(".")[0]
    return f"Luna {first.capitalize()}" if first and first not in {"localhost", "127"} else "the app"


def digits_field(name: str, label: str, hint: str = "", focus: bool = False) -> str:
    extra = f" <small>{html.escape(hint)}</small>" if hint else ""
    return (
        f"<label>{html.escape(label)}{extra}<input name='{name}' type='password' inputmode='numeric' "
        f"autocomplete='off' pattern='[0-9]{{6,12}}' maxlength='12' required{' autofocus' if focus else ''}></label>"
    )


def pin_card(title: str, lead: str, body: str, allows_next, target: str | None, status: int = 200) -> HTMLResponse:
    back = (
        f"<footer><a href='{html.escape(target, quote=True)}'>&larr; Back to {html.escape(app_name(target))}</a></footer>"
        if target and allows_next(target) else "<footer><a href='/'>Luna sign-in</a></footer>"
    )
    return HTMLResponse(
        "<!doctype html><html lang='en'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{PIN_STYLE}</style>"
        # Cloudflare would otherwise hide email addresses behind a script on these pages.
        f"<body><main><!--email_off--><div class='mark'>{LOCK_MARK}</div><h1>{html.escape(title)}</h1>"
        f"<p class='lead'>{html.escape(lead)}</p>{body}{back}<footer>{AuthConfig.BRAND}</footer><!--/email_off--></main></body></html>",
        status_code=status,
    )


def hash_pin(pin: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, iterations)


def pin_routes(config: AuthConfig, store: SessionStore) -> list[Route]:
    signer = URLSafeTimedSerializer(config.secret, salt="luna-pin-form")

    def signed_in(token: str | None) -> dict[str, Any] | None:
        return store.get(token) if token else None

    def fresh(user: dict[str, Any]) -> bool:
        signed_in_at = user.get("signed_in_at")
        return signed_in_at is not None and datetime.now(UTC) - signed_in_at < FRESH_SIGN_IN

    def back_to(request: Request, fields: dict[str, str] | None = None) -> str | None:
        target = (fields or request.query_params).get("next", "")
        return target if target and config.allows_next(target) else None

    def here(target: str | None) -> str:
        return f"{config.public_url}/pin" + (f"?next={quote(target, safe='')}" if target else "")

    def check(user_id: str, pin: str) -> tuple[bool, dict[str, Any]]:
        """Whether ``pin`` is right, and what to tell the person when it is not."""
        stored = store.take_pin_attempt(user_id, MAX_ATTEMPTS, LOCK)
        if stored is None:
            state = store.pin_state(user_id)
            if state is None:
                return False, {"error": "Set a PIN first.", "status": 409}
            minutes = max(1, -(-state["locked_seconds"] // 60))
            return False, {"error": f"Too many wrong PINs. Try again in {minutes} minutes.",
                           "retry_after_seconds": state["locked_seconds"], "status": 423}
        if PIN.fullmatch(pin) and hmac.compare_digest(
            hash_pin(pin, bytes(stored["salt"]), stored["iterations"]), bytes(stored["pin_hash"])
        ):
            store.clear_pin_attempts(user_id)
            return True, {}
        left = stored["attempts_left"]
        if left == 0:
            return False, {"error": f"Wrong PIN. It is locked for {int(LOCK.total_seconds() // 60)} minutes.",
                           "retry_after_seconds": int(LOCK.total_seconds()), "status": 423}
        return False, {"error": f"Wrong PIN. {left} {'try' if left == 1 else 'tries'} left.",
                       "attempts_left": left, "status": 403}

    def settings_page(user: dict[str, Any], token: str, target: str | None, problem: str = "") -> Response:
        has_pin = store.pin_state(user["user_id"]) is not None
        signed = signer.dumps({"session": token_hash(token)})
        again = html.escape(f"/login?fresh=1&next={quote(here(target), safe='')}", quote=True)
        hidden = (
            f"<input type='hidden' name='form' value='{html.escape(signed, quote=True)}'>"
            + (f"<input type='hidden' name='next' value='{html.escape(target, quote=True)}'>" if target else "")
        )
        new = digits_field("pin", "New PIN", "6 to 12 digits", focus=fresh(user)) + digits_field("confirm", "New PIN again")
        if fresh(user):
            body = f"<form method='post' action='/pin'>{hidden}{new}<button class='primary'>Save PIN</button></form>"
        elif has_pin:
            body = (
                f"<form method='post' action='/pin'>{hidden}{digits_field('current', 'Current PIN', focus=True)}"
                f"<hr>{new}<button class='primary'>Change PIN</button></form>"
                f"<p class='quiet'>Forgot it? <a href='{again}'>Sign in with Google again</a> to set a new one. "
                "Your hidden items stay as they are.</p>"
            )
        else:
            body = (
                "<p>First, confirm it is you. This keeps someone with a copy of your session "
                "from setting a PIN for you.</p>"
                f"<a class='google' href='{again}'>{GOOGLE_MARK}Confirm with Google</a>"
            )
        chip = (
            "<span class='chip on'>PIN set</span>" if has_pin
            else "<span class='chip'>No PIN yet: hidden items stay locked</span>"
        )
        error = f"<p class='alert' role='alert'>{html.escape(problem)}</p>" if problem else ""
        return pin_card(
            "Hidden-items PIN",
            "One PIN for the hidden images, documents, and music in Luna Visia, Lexia, and Vibia.",
            f"{chip}{error}{body}",
            config.allows_next,
            target,
            400 if problem else 200,
        )

    async def settings(request: Request) -> Response:
        token = request.cookies.get(config.cookie_name)
        user = signed_in(token)
        target = back_to(request)
        if user is None:
            return RedirectResponse(f"/login?next={quote(here(target), safe='')}", 303)
        return settings_page(user, token, target)

    async def save(request: Request) -> Response:
        token = request.cookies.get(config.cookie_name)
        user = signed_in(token)
        fields = await form_fields(request)
        target = back_to(request, fields)
        try:
            signed = signer.loads(fields.get("form", ""), max_age=FORM_SECONDS)
        except BadSignature:
            signed = None
        if user is None or signed is None or not secrets.compare_digest(signed["session"], token_hash(token)):
            return pin_card("Not changed", "This page expired, so nothing was saved.",
                            f"<a class='button' href='{html.escape(here(target), quote=True)}'>Reload it</a>",
                            config.allows_next, None, 403)
        pin = fields.get("pin", "")
        if not PIN.fullmatch(pin):
            return settings_page(user, token, target, "A PIN is 6 to 12 digits.")
        if pin != fields.get("confirm"):
            return settings_page(user, token, target, "The two new PINs do not match.")
        if not fresh(user):
            if store.pin_state(user["user_id"]) is None:
                return settings_page(user, token, target, "Sign in with Google again to set your PIN.")
            right, problem = check(user["user_id"], fields.get("current", ""))
            if not right:
                return settings_page(user, token, target, problem["error"])
        salt = secrets.token_bytes(16)
        store.set_pin(user["user_id"], salt, hash_pin(pin, salt, ITERATIONS), ITERATIONS)
        return pin_card(
            "PIN saved",
            "Type it into the Hidden tab of Luna Visia, Lexia, or Vibia to see your hidden items.",
            "<span class='chip on'>PIN set</span>",
            config.allows_next,
            target,
        )

    def caller(request: Request) -> dict[str, Any] | None:
        return signed_in(request.headers.get(AuthConfig.SESSION_HEADER, ""))

    async def status(request: Request) -> Response:
        """For app servers: whether this person has a PIN, and whether it is locked."""
        user = caller(request)
        if user is None:
            return JSONResponse({"error": "not signed in"}, 401)
        state = store.pin_state(user["user_id"])
        return JSONResponse({
            "pin_set": state is not None,
            "locked_seconds": state["locked_seconds"] if state else 0,
            "settings_url": f"{config.public_url}/pin",
        })

    async def verify(request: Request) -> Response:
        """For app servers: check a PIN typed into an app's Hidden tab."""
        user = caller(request)
        if user is None:
            return JSONResponse({"error": "not signed in"}, 401)
        try:
            pin = str(json.loads(await request.body() or b"{}").get("pin", ""))
        except (ValueError, AttributeError):
            return JSONResponse({"error": "Send the PIN as JSON."}, 400)
        right, problem = check(user["user_id"], pin)
        if right:
            return JSONResponse({"ok": True})
        status_code = problem.pop("status")
        return JSONResponse(problem, status_code)

    return [
        Route("/pin", settings),
        Route("/pin", save, methods=["POST"]),
        Route("/pin/status", status),
        Route("/pin/verify", verify, methods=["POST"]),
    ]
