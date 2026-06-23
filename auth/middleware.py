"""Auth middleware for the HandsOff (再买剁手) web display.

``auth_required`` protects the data/API routes: a request must carry a valid session
token (``Authorization: Bearer …`` or the ``handsoff_session`` cookie) for a wallet
that is STILL whitelisted. Re-checking the whitelist on every request (not just at
sign-in) means revoking a wallet + restart takes effect immediately and a leaked
token for a removed wallet stops working.

When auth is disabled the web server never wraps its handlers with this, so the
original open behavior is preserved exactly.
"""
from __future__ import annotations

from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any

from aiohttp import web

COOKIE_NAME = "handsoff_session"


def extract_token(request: web.Request) -> str | None:
    """Pull the session token from the Authorization header or cookie."""
    auth_hdr = request.headers.get("Authorization", "")
    if auth_hdr.startswith("Bearer "):
        tok = auth_hdr[7:].strip()
        if tok:
            return tok
    return request.cookies.get(COOKIE_NAME)


def current_authorized_wallet(request: web.Request) -> str | None:
    """Return the wallet iff it has a valid session AND is whitelisted, else None.

    Used by both the page gate (``/``) and ``auth_required``. Reads the
    ``session_manager`` / ``whitelist`` placed on the app by the web server.
    """
    sm = request.app.get("session_manager")
    if sm is None:
        return None
    wallet = sm.verify_token(extract_token(request))
    if not wallet:
        return None
    wl = request.app.get("whitelist")
    if wl is not None and not wl.allows(wallet):
        return None
    return wallet


def auth_required(
    handler: Callable[[web.Request], Coroutine[Any, Any, web.Response]],
) -> Callable[[web.Request], Coroutine[Any, Any, web.Response]]:
    @wraps(handler)
    async def wrapper(request: web.Request) -> web.Response:
        wallet = current_authorized_wallet(request)
        if not wallet:
            return web.json_response(
                {"success": False, "error": "unauthorized"}, status=401
            )
        request["wallet"] = wallet
        return await handler(request)

    return wrapper


def get_current_wallet(request: web.Request) -> str | None:
    return request.get("wallet")
