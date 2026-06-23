"""Session & nonce management for the HandsOff (再买剁手) web wallet login.

Flow:
  1. Client requests a nonce for wallet W.
  2. Server stores nonce -> (W, expires_at) in-memory.
  3. Client signs the login message with the wallet, sends the signature.
  4. Server verifies the Ed25519 signature AND that W is whitelisted, then issues a
     stateless HMAC-signed session token (also set as a cookie).

Session tokens are HMAC-signed and contain wallet_address, issued_at, expires_at —
no server-side session store is needed for verification, so a restart never
invalidates live sessions unless ``session_secret`` changes.

Requires ``pynacl`` + ``base58`` (only imported when web auth is enabled).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time

import base58
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

logger = logging.getLogger("HandsOffAuth")

# Maximum number of pending nonces before back-pressure kicks in (DoS protection).
_MAX_NONCES = 10000


class SessionManager:
    """In-memory nonce store + stateless HMAC session tokens.

    The aiohttp event loop is single-threaded, so the plain-dict nonce store is safe
    without a lock. Tokens are verified purely from their HMAC signature + embedded
    expiry.
    """

    def __init__(self, secret: str, session_ttl: int = 604800, nonce_ttl: int = 300):
        self._secret = (secret or "").encode("utf-8")
        try:
            self._session_ttl = max(60, int(session_ttl))
        except (TypeError, ValueError):
            self._session_ttl = 604800
        try:
            self._nonce_ttl = max(30, int(nonce_ttl))
        except (TypeError, ValueError):
            self._nonce_ttl = 300
        # nonce -> (wallet, expires_at)
        self._nonces: dict[str, tuple[str, float]] = {}

    # ---------------- Nonce ----------------
    def create_nonce(self, wallet: str) -> str:
        self._gc_nonces()
        if len(self._nonces) >= _MAX_NONCES:
            # Self-heal under a nonce flood: evict the oldest-expiring entries
            # instead of rejecting, so a burst of unauthenticated /nonce calls cannot
            # lock out legitimate logins.
            self._evict_oldest(max(1, _MAX_NONCES // 10))
            logger.warning(
                "Nonce store hit capacity (%d); evicted oldest entries to make room",
                _MAX_NONCES,
            )
        nonce = secrets.token_urlsafe(24)
        self._nonces[nonce] = (wallet, time.time() + self._nonce_ttl)
        return nonce

    def _evict_oldest(self, count: int) -> None:
        """Drop the ``count`` soonest-to-expire nonces (capacity back-pressure)."""
        if count <= 0 or not self._nonces:
            return
        oldest = sorted(self._nonces.items(), key=lambda kv: kv[1][1])[:count]
        for n, _ in oldest:
            self._nonces.pop(n, None)

    def consume_nonce(self, nonce: str, wallet: str) -> bool:
        """Single-use. Returns True iff the nonce is valid for this wallet."""
        self._gc_nonces()
        record = self._nonces.pop(nonce, None)
        if record is None:
            return False
        stored_wallet, expires_at = record
        if stored_wallet != wallet:
            return False
        if expires_at < time.time():
            return False
        return True

    def _gc_nonces(self) -> None:
        now = time.time()
        expired = [n for n, (_, exp) in self._nonces.items() if exp < now]
        for n in expired:
            self._nonces.pop(n, None)

    # ---------------- Session Token ----------------
    def issue_token(self, wallet: str) -> str:
        now = int(time.time())
        payload = {"w": wallet, "iat": now, "exp": now + self._session_ttl}
        payload_b = self._b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        sig = self._sign(payload_b)
        return f"{payload_b}.{sig}"

    def verify_token(self, token: str | None) -> str | None:
        """Return the wallet address if the token is valid, else None."""
        if not token:
            return None
        try:
            payload_b, sig = token.split(".", 1)
            expected = self._sign(payload_b)
            # hmac.compare_digest raises TypeError on non-ASCII str operands; a hostile
            # cookie/header byte must yield a clean None (-> 401), never an uncaught 500.
            if not hmac.compare_digest(expected, sig):
                return None
            payload = json.loads(self._b64d(payload_b))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("exp", 0) < int(time.time()):
            return None
        wallet = payload.get("w")
        return wallet if isinstance(wallet, str) and wallet else None

    def _sign(self, data: str) -> str:
        mac = hmac.new(self._secret, data.encode("utf-8"), hashlib.sha256).digest()
        return self._b64(mac)

    @staticmethod
    def _b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    @staticmethod
    def _b64d(s: str) -> bytes:
        pad = "=" * (-len(s) % 4)
        return base64.urlsafe_b64decode(s + pad)


# ---------------- Signature verification ----------------
def verify_solana_signature(message: str, signature_b58: str, wallet_address: str) -> bool:
    """Verify a Solana wallet Ed25519 signature.

    - ``message``: the plaintext message the wallet signed
    - ``signature_b58``: base58-encoded 64-byte signature
    - ``wallet_address``: base58-encoded 32-byte Solana public key

    Never raises — any malformed input returns False.
    """
    try:
        pubkey_bytes = base58.b58decode(wallet_address)
        if len(pubkey_bytes) != 32:
            return False
        sig_bytes = base58.b58decode(signature_b58)
        if len(sig_bytes) != 64:
            return False
        verify_key = VerifyKey(pubkey_bytes)
        try:
            verify_key.verify(message.encode("utf-8"), sig_bytes)
            return True
        except BadSignatureError:
            return False
    except Exception:
        return False
