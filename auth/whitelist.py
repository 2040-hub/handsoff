"""Wallet whitelist for the HandsOff (再买剁手) web login.

Access to the web page is gated on a static allow-list of Solana wallet addresses
(``whitelist_wallet_lists`` in config.ini, comma-separated). Only a wallet whose
signature verifies AND whose address is in this list may obtain a session and view
the page.

Solana base58 addresses are CASE-SENSITIVE, so membership is an exact match — no
lowercasing. Empty / whitespace-only entries are dropped, and obviously invalid
entries (wrong length or non-base58 characters) are skipped with a warning so a
single typo can't silently widen or break access.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("HandsOffAuth")

# base58 alphabet (Bitcoin/Solana) — excludes 0 O I l.
_BASE58_CHARS = frozenset("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


def is_valid_wallet(addr: str) -> bool:
    """True for a plausibly-valid base58 Solana address (32-44 chars)."""
    if not isinstance(addr, str):
        return False
    if not (32 <= len(addr) <= 44):
        return False
    return all(c in _BASE58_CHARS for c in addr)


class WalletWhitelist:
    """Immutable, O(1)-membership allow-list of Solana wallet addresses.

    Parsed once at startup from the comma-separated config value. ``allows`` is the
    single membership predicate used by both the verify handler (at sign-in) and the
    auth middleware (on every protected request), so removing a wallet from the list
    and restarting immediately revokes its access.
    """

    def __init__(self, raw: str | None) -> None:
        self._raw = raw or ""
        valid: set[str] = set()
        dropped_invalid = 0
        # Allow commas AND newlines as separators (config values may wrap lines).
        for part in self._raw.replace("\n", ",").split(","):
            addr = part.strip()
            if not addr:
                continue
            if is_valid_wallet(addr):
                valid.add(addr)
            else:
                dropped_invalid += 1
                logger.warning(
                    "whitelist_wallet_lists: dropping invalid wallet entry %r "
                    "(not a 32-44 char base58 address)", addr[:64]
                )
        self._wallets: frozenset[str] = frozenset(valid)
        if dropped_invalid:
            logger.warning(
                "whitelist_wallet_lists: %d invalid entr%s ignored",
                dropped_invalid, "y" if dropped_invalid == 1 else "ies",
            )

    def allows(self, wallet: str | None) -> bool:
        """Exact (case-sensitive) membership test."""
        return isinstance(wallet, str) and wallet in self._wallets

    @property
    def is_empty(self) -> bool:
        return not self._wallets

    def __len__(self) -> int:
        return len(self._wallets)
