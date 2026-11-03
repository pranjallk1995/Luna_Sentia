"""The small HTML pages and form parsing shared by the service's routes."""

from __future__ import annotations

import html
from urllib.parse import parse_qsl

from starlette.requests import Request
from starlette.responses import HTMLResponse


def page(title: str, message: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title>"
        "<body style='font-family:system-ui,sans-serif;background:#111;color:#f5f5f5;"
        "display:grid;place-items:center;min-height:90vh;margin:0 16px'>"
        # Cloudflare would otherwise hide email addresses behind a script on these pages.
        f"<main style='max-width:40rem'><h1>{html.escape(title)}</h1><!--email_off--><div>{message}</div><!--/email_off--></main>",
        status_code=status,
    )


async def form_fields(request: Request) -> dict[str, str]:
    """An URL-encoded form body as a dict."""
    return dict(parse_qsl((await request.body()).decode(), keep_blank_values=True))
