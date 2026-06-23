"""HandsOff (再买剁手) web authentication — Solana wallet login gated by a whitelist.

A nonce -> Ed25519 signature -> HMAC session-token flow that authorizes by membership
in ``whitelist_wallet_lists`` (config.ini). Importing this package requires ``pynacl``
+ ``base58`` (only needed when web auth is enabled); the web layer keeps working
without them as long as auth stays disabled.
"""
from __future__ import annotations

from .session import SessionManager, verify_solana_signature
from .whitelist import WalletWhitelist, is_valid_wallet
from .middleware import (
    COOKIE_NAME,
    auth_required,
    current_authorized_wallet,
    extract_token,
    get_current_wallet,
)
from .handlers import (
    handle_nonce,
    handle_verify,
    handle_me,
    handle_logout,
)

__all__ = [
    "SessionManager",
    "verify_solana_signature",
    "WalletWhitelist",
    "is_valid_wallet",
    "COOKIE_NAME",
    "auth_required",
    "current_authorized_wallet",
    "extract_token",
    "get_current_wallet",
    "handle_nonce",
    "handle_verify",
    "handle_me",
    "handle_logout",
]
