"""HTTP handlers for the HandsOff (再买剁手) web wallet login.

Endpoints (registered by the web server only when auth is enabled):
  POST /api/auth/nonce   { wallet }                   -> { nonce, message }
  POST /api/auth/verify  { wallet, nonce, signature } -> { success, token, wallet }
  GET  /api/auth/me                                   -> { wallet, auth_enabled }
  POST /api/auth/logout                               -> clears the session cookie

Step 3 of verify checks the wallet whitelist before issuing a session.
"""
from __future__ import annotations

import logging

from aiohttp import web

from .middleware import COOKIE_NAME, extract_token
from .session import verify_solana_signature
from .whitelist import is_valid_wallet

logger = logging.getLogger("HandsOffAuth")

# Brand string the wallet signs — distinct so a signature for one site is never
# replayable on another.
_LOGIN_BRAND = "HandsOff Login"


def _s(value: object) -> str:
    """Strip a value to text only when it actually IS a string. Any other JSON type
    (number / list / dict / bool) becomes "" so a hostile body like {"wallet": [1,2]}
    falls through to the normal invalid-param 400 path instead of raising
    AttributeError into a 500 — matching the module's fail-clean posture."""
    return value.strip() if isinstance(value, str) else ""


def _build_message(nonce: str, wallet: str) -> str:
    return (
        f"{_LOGIN_BRAND}\n"
        f"Wallet: {wallet}\n"
        f"Nonce: {nonce}\n"
        "Sign this message to prove wallet ownership. No transaction will be sent."
    )


async def handle_nonce(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "invalid_json"}, status=400)
    if not isinstance(data, dict):
        return web.json_response({"success": False, "error": "invalid_json"}, status=400)

    wallet = _s(data.get("wallet"))
    if not is_valid_wallet(wallet):
        return web.json_response({"success": False, "error": "invalid_wallet"}, status=400)

    session_mgr = request.app["session_manager"]
    try:
        nonce = session_mgr.create_nonce(wallet)
    except RuntimeError:
        return web.json_response({"success": False, "error": "server_busy"}, status=503)
    return web.json_response({
        "success": True,
        "nonce": nonce,
        "message": _build_message(nonce, wallet),
    })


async def handle_verify(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "invalid_json"}, status=400)
    if not isinstance(data, dict):
        return web.json_response({"success": False, "error": "invalid_json"}, status=400)

    wallet = _s(data.get("wallet"))
    nonce = _s(data.get("nonce"))
    signature = _s(data.get("signature"))

    if not is_valid_wallet(wallet) or not nonce or not signature:
        return web.json_response({"success": False, "error": "missing_params"}, status=400)

    session_mgr = request.app["session_manager"]
    whitelist = request.app["whitelist"]

    # 1. nonce must be valid and unconsumed (single-use, bound to this wallet)
    if not session_mgr.consume_nonce(nonce, wallet):
        return web.json_response(
            {"success": False, "error": "invalid_or_expired_nonce"}, status=400
        )

    # 2. verify the Ed25519 signature over the exact message we issued
    message = _build_message(nonce, wallet)
    if not verify_solana_signature(message, signature, wallet):
        return web.json_response({"success": False, "error": "invalid_signature"}, status=401)

    # 3. whitelist gate — only listed wallets may obtain a session
    if not whitelist.allows(wallet):
        logger.info("Login denied for non-whitelisted wallet %s", wallet)
        return web.json_response({"success": False, "error": "not_whitelisted"}, status=403)

    # 4. issue the session token (also set as an httpOnly cookie)
    token = session_mgr.issue_token(wallet)
    resp = web.json_response({"success": True, "token": token, "wallet": wallet})
    resp.set_cookie(
        COOKIE_NAME, token,
        max_age=session_mgr._session_ttl,
        httponly=True,
        samesite="Lax",
        path="/",
    )
    return resp


async def handle_me(request: web.Request) -> web.Response:
    session_mgr = request.app["session_manager"]
    whitelist = request.app.get("whitelist")
    wallet = session_mgr.verify_token(extract_token(request))
    # A valid token whose wallet is no longer whitelisted is treated as logged out.
    if not wallet or (whitelist is not None and not whitelist.allows(wallet)):
        return web.json_response(
            {"success": False, "error": "unauthorized", "auth_enabled": True}, status=401
        )
    return web.json_response({"success": True, "wallet": wallet, "auth_enabled": True})


async def handle_logout(request: web.Request) -> web.Response:
    resp = web.json_response({"success": True})
    resp.del_cookie(COOKIE_NAME, path="/")
    return resp
