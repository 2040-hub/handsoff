"""Helius free-RPC client + buy parser for HandsOff (再买剁手).

Uses the FREE Helius plan exclusively:

* Standard Solana JSON-RPC (``getSignaturesForAddress`` + ``getTransaction`` with
  ``jsonParsed``) for the hot path — 1 credit/call, 10 req/s. Buys are detected from
  ``meta.pre/postTokenBalances`` + ``pre/postBalances``, so the logic is portable to
  any Solana RPC, not just Helius.
* DAS ``getAsset`` (``showFungible``) for per-mint metadata, decimals, raw supply and
  a cached USD price — 10 credits/call, cached aggressively.
* A DexScreener fallback (no key) for the freshest price / market cap on brand-new
  pump.fun mints that DAS has not priced yet.

A small async throttle keeps each surface under its free-tier per-second cap, with
exponential backoff + jitter on HTTP 429 / 5xx. Every method degrades to ``None`` /
empty rather than raising into the poller's loop.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import aiohttp

logger = logging.getLogger("HandsOffHelius")

WSOL_MINT = "So11111111111111111111111111111111111111112"
# Stable quote mints treated as "cash" (a stable->token swap is a buy, but not a SOL
# buy; we still card it, valuing it via the token side / DexScreener).
STABLE_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}
_QUOTE_MINTS = STABLE_MINTS | {WSOL_MINT}

# Best-effort platform labels inferred from on-chain program ids.
_PROGRAM_PLATFORM = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "Pump.fun",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "PumpSwap",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "Meteora",
}


class HeliusError(Exception):
    pass


class HeliusClient:
    """Async Helius/Solana RPC client. One per process; reuses a shared session."""

    def __init__(
        self,
        api_key: str,
        *,
        rpc_url: str | None = None,
        session: aiohttp.ClientSession | None = None,
        rpc_min_interval: float = 0.12,   # ~8 req/s (< the 10/s free cap)
        das_min_interval: float = 0.55,   # ~1.8 req/s (< the 2/s free cap)
        timeout: float = 20.0,
        max_retries: int = 4,
    ) -> None:
        self.api_key = (api_key or "").strip()
        base = (rpc_url or "https://mainnet.helius-rpc.com").strip().rstrip("/")
        self._rpc_endpoint = f"{base}/?api-key={self.api_key}" if self.api_key else base
        self._session = session
        self._owns_session = session is None
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._max_retries = max(1, int(max_retries))
        self._rpc_min = max(0.0, float(rpc_min_interval))
        self._das_min = max(0.0, float(das_min_interval))
        self._rpc_lock = asyncio.Lock()
        self._das_lock = asyncio.Lock()
        self._rpc_last = 0.0
        self._das_last = 0.0
        self._rpc_id = 0

    async def __aenter__(self) -> "HeliusClient":
        await self._ensure_session()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------ #
    # transport
    # ------------------------------------------------------------------ #
    async def _throttle(self, lock: asyncio.Lock, last_attr: str, min_interval: float) -> None:
        if min_interval <= 0:
            return
        async with lock:
            now = time.monotonic()
            last = getattr(self, last_attr)
            wait = min_interval - (now - last)
            if wait > 0:
                await asyncio.sleep(wait)
            setattr(self, last_attr, time.monotonic())

    async def _post(self, body: dict[str, Any], *, das: bool = False) -> Any:
        """POST a JSON-RPC body, returning ``result`` or raising HeliusError."""
        session = await self._ensure_session()
        lock, last_attr, min_iv = (
            (self._das_lock, "_das_last", self._das_min) if das
            else (self._rpc_lock, "_rpc_last", self._rpc_min)
        )
        delay = 0.6
        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            await self._throttle(lock, last_attr, min_iv)
            try:
                async with session.post(self._rpc_endpoint, json=body) as resp:
                    if resp.status in (429, 500, 502, 503, 504):
                        retry_after = resp.headers.get("Retry-After")
                        sleep_for = (
                            float(retry_after) if (retry_after and retry_after.isdigit())
                            else delay + random.random() * 0.3
                        )
                        logger.debug("RPC %s -> backoff %.2fs", resp.status, sleep_for)
                        await asyncio.sleep(sleep_for)
                        delay = min(delay * 2, 8.0)
                        continue
                    resp.raise_for_status()
                    data = await resp.json()
                if isinstance(data, dict) and data.get("error"):
                    # JSON-RPC error: retry transient ones, give up otherwise.
                    err = data["error"]
                    msg = err.get("message", "") if isinstance(err, dict) else str(err)
                    raise HeliusError(f"rpc error: {msg}")
                return data.get("result") if isinstance(data, dict) else None
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
                await asyncio.sleep(delay + random.random() * 0.3)
                delay = min(delay * 2, 8.0)
            except HeliusError as exc:
                last_exc = exc
                break
        raise HeliusError(f"RPC failed after retries: {last_exc}")

    def _next_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    async def _rpc(self, method: str, params: list[Any], *, das: bool = False) -> Any:
        return await self._post(
            {"jsonrpc": "2.0", "id": self._next_id(), "method": method, "params": params},
            das=das,
        )

    # ------------------------------------------------------------------ #
    # standard RPC
    # ------------------------------------------------------------------ #
    async def get_signatures_for_address(
        self, address: str, *, until: str | None = None,
        before: str | None = None, limit: int = 25
    ) -> list[dict[str, Any]]:
        opts: dict[str, Any] = {"limit": max(1, min(100, int(limit)))}
        if until:
            opts["until"] = until
        if before:
            opts["before"] = before
        try:
            res = await self._rpc("getSignaturesForAddress", [address, opts])
        except HeliusError as exc:
            logger.warning("getSignaturesForAddress(%s) failed: %s", address[:8], exc)
            return []
        return res if isinstance(res, list) else []

    async def get_transaction(self, signature: str) -> dict[str, Any] | None:
        try:
            res = await self._rpc(
                "getTransaction",
                [
                    signature,
                    {
                        "encoding": "jsonParsed",
                        "maxSupportedTransactionVersion": 0,
                        "commitment": "confirmed",
                    },
                ],
            )
        except HeliusError as exc:
            logger.debug("getTransaction(%s) failed: %s", signature[:8], exc)
            return None
        return res if isinstance(res, dict) else None

    async def get_token_supply(self, mint: str) -> dict[str, Any] | None:
        try:
            res = await self._rpc("getTokenSupply", [mint])
        except HeliusError as exc:
            logger.debug("getTokenSupply(%s) failed: %s", mint[:8], exc)
            return None
        if isinstance(res, dict):
            return res.get("value")
        return None

    # ------------------------------------------------------------------ #
    # DAS metadata / price
    # ------------------------------------------------------------------ #
    async def get_asset(self, mint: str) -> dict[str, Any] | None:
        try:
            res = await self._rpc(
                "getAsset",
                {"id": mint, "options": {"showFungible": True}},
                das=True,
            )
        except HeliusError as exc:
            logger.debug("getAsset(%s) failed: %s", mint[:8], exc)
            return None
        return res if isinstance(res, dict) else None

    async def fetch_metadata(self, mint: str) -> dict[str, Any]:
        """Resolve a mint's display metadata + supply/price. Returns a dict with any
        of: name, symbol, icon, twitter, website, decimals, supply (raw),
        price (USD), market_cap (USD). Missing pieces are simply absent."""
        out: dict[str, Any] = {}
        asset = await self.get_asset(mint)
        if asset:
            content = asset.get("content") or {}
            meta = content.get("metadata") or {}
            links = content.get("links") or {}
            files = content.get("files") or []
            name = (meta.get("name") or "").strip()
            symbol = (meta.get("symbol") or "").strip()
            if name:
                out["name"] = name
            if symbol:
                out["symbol"] = symbol
            icon = ""
            if isinstance(files, list) and files:
                f0 = files[0] if isinstance(files[0], dict) else {}
                icon = (f0.get("cdn_uri") or f0.get("uri") or "").strip()
            icon = icon or (links.get("image") or "").strip()
            if icon:
                out["icon"] = icon
            ext = (links.get("external_url") or "").strip()
            if ext:
                out["website"] = ext
            tinfo = asset.get("token_info") or {}
            dec = tinfo.get("decimals")
            if isinstance(dec, int):
                out["decimals"] = dec
            supply = tinfo.get("supply")
            if supply is not None:
                try:
                    out["supply"] = float(supply)
                except (TypeError, ValueError):
                    pass
            price_info = tinfo.get("price_info") or {}
            ppt = price_info.get("price_per_token")
            if ppt is not None:
                try:
                    p = float(ppt)
                    if p > 0:
                        out["price"] = p
                except (TypeError, ValueError):
                    pass
            # Socials live in the off-chain JSON for most pump.fun / Metaplex tokens.
            json_uri = (content.get("json_uri") or "").strip()
            if json_uri and (("twitter" not in out) or ("website" not in out)):
                socials = await self._fetch_offchain_socials(json_uri)
                for k, v in socials.items():
                    out.setdefault(k, v)

        # Market cap from DAS price + supply when both known.
        if out.get("price") and out.get("supply") and out.get("decimals") is not None:
            try:
                d = int(out["decimals"])
                # Bound the exponent (real SPL mints use a small u8) so absurd metadata
                # can't build a giant int when deriving circulating supply.
                if 0 <= d <= 64:
                    circ = float(out["supply"]) / (10 ** d)
                    if circ > 0:
                        out["market_cap"] = float(out["price"]) * circ
            except (TypeError, ValueError, ZeroDivisionError):
                pass

        # DexScreener fallback for a fresh price / market cap the cache missed.
        if not out.get("price") or not out.get("market_cap"):
            dex = await self.fetch_dexscreener(mint)
            if dex:
                if not out.get("price") and dex.get("price"):
                    out["price"] = dex["price"]
                if not out.get("market_cap") and dex.get("market_cap"):
                    out["market_cap"] = dex["market_cap"]
                for k in ("name", "symbol", "icon", "twitter", "website"):
                    if not out.get(k) and dex.get(k):
                        out[k] = dex[k]
        return out

    async def _fetch_offchain_socials(self, json_uri: str) -> dict[str, str]:
        out: dict[str, str] = {}
        try:
            session = await self._ensure_session()
            async with session.get(json_uri) as resp:
                if resp.status != 200:
                    return out
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return out
        if not isinstance(data, dict):
            return out
        ext = data.get("extensions") if isinstance(data.get("extensions"), dict) else {}

        def pick(*keys: str) -> str:
            for src in (data, ext):
                for k in keys:
                    v = src.get(k)
                    if isinstance(v, str) and v.strip():
                        return v.strip()
            return ""

        tw = pick("twitter", "twitter_url", "x")
        web = pick("website", "external_url", "homepage")
        if tw:
            out["twitter"] = tw
        if web:
            out["website"] = web
        return out

    async def fetch_dexscreener(self, mint: str) -> dict[str, Any] | None:
        """Freshest price / market cap (and a metadata backstop) from DexScreener."""
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        try:
            session = await self._ensure_session()
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return None
        pairs = data.get("pairs") if isinstance(data, dict) else None
        if not pairs:
            return None
        # Prefer the deepest-liquidity SOL pair.
        def liq(p: dict[str, Any]) -> float:
            try:
                return float((p.get("liquidity") or {}).get("usd") or 0)
            except (TypeError, ValueError):
                return 0.0
        pairs = [p for p in pairs if isinstance(p, dict)]
        if not pairs:
            return None
        best = max(pairs, key=liq)
        out: dict[str, Any] = {}
        try:
            px = float(best.get("priceUsd"))
            if px > 0:
                out["price"] = px
        except (TypeError, ValueError):
            pass
        mc = best.get("marketCap") or best.get("fdv")
        try:
            mcf = float(mc)
            if mcf > 0:
                out["market_cap"] = mcf
        except (TypeError, ValueError):
            pass
        info = best.get("info") or {}
        img = (info.get("imageUrl") or "").strip()
        if img:
            out["icon"] = img
        base = best.get("baseToken") or {}
        if base.get("name"):
            out["name"] = str(base["name"]).strip()
        if base.get("symbol"):
            out["symbol"] = str(base["symbol"]).strip()
        # Socials, if present.
        for s in (info.get("socials") or []):
            if not isinstance(s, dict):
                continue
            typ = (s.get("type") or "").lower()
            url_v = (s.get("url") or "").strip()
            if typ == "twitter" and url_v and "twitter" not in out:
                out["twitter"] = url_v
        for w in (info.get("websites") or []):
            if isinstance(w, dict) and w.get("url") and "website" not in out:
                out["website"] = str(w["url"]).strip()
        return out or None

    async def get_sol_price(self) -> float | None:
        """Current SOL/USD via DAS getAsset on WSOL (cached ~600s server-side)."""
        asset = await self.get_asset(WSOL_MINT)
        if asset:
            try:
                ppt = ((asset.get("token_info") or {}).get("price_info") or {}).get(
                    "price_per_token"
                )
                p = float(ppt)
                if p > 0:
                    return p
            except (TypeError, ValueError):
                pass
        dex = await self.fetch_dexscreener(WSOL_MINT)
        if dex and dex.get("price"):
            return float(dex["price"])
        return None


# --------------------------------------------------------------------------- #
# buy parsing (standard jsonParsed transaction)
# --------------------------------------------------------------------------- #
def _account_pubkey(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("pubkey") or "")
    return str(entry or "")


def _token_balance_map(entries: Any) -> dict[tuple[str, str], float]:
    """(owner, mint) -> uiAmount float, from pre/postTokenBalances."""
    out: dict[tuple[str, str], float] = {}
    if not isinstance(entries, list):
        return out
    for e in entries:
        if not isinstance(e, dict):
            continue
        owner = str(e.get("owner") or "")
        mint = str(e.get("mint") or "")
        if not owner or not mint:
            continue
        uia = (e.get("uiTokenAmount") or {}).get("uiAmount")
        try:
            val = float(uia) if uia is not None else 0.0
        except (TypeError, ValueError):
            val = 0.0
        out[(owner, mint)] = val
    return out


def _decimals_for(entries: Any, owner: str, mint: str) -> int | None:
    if not isinstance(entries, list):
        return None
    for e in entries:
        if not isinstance(e, dict):
            continue
        if str(e.get("owner") or "") == owner and str(e.get("mint") or "") == mint:
            dec = (e.get("uiTokenAmount") or {}).get("decimals")
            if isinstance(dec, int):
                return dec
    return None


def _balance_aligned_keys(tx: dict[str, Any], meta: dict[str, Any]) -> list[str]:
    """Return the account-key list aligned 1:1 with ``meta.pre/postBalances``.

    With ``jsonParsed`` encoding ``message.accountKeys`` already includes the addresses
    loaded from on-chain Address Lookup Tables (used by virtually every router / trading
    bot via *versioned* transactions), so it lines up with the balance arrays directly.
    For encodings that list only the static keys, the loaded addresses live in
    ``meta.loadedAddresses`` (writable first, then readonly) and are appended here so
    ``keys[i]`` always corresponds to ``preBalances[i]`` / ``postBalances[i]``.

    Getting this alignment right is what makes the per-wallet native-SOL delta correct
    when the buyer wallet is an ALT-loaded (non-static) account — otherwise its lamport
    change is read from the wrong slot and a real buy looks like fee-sized dust.
    """
    msg = (tx.get("transaction") or {}).get("message") or {}
    keys = [_account_pubkey(k) for k in (msg.get("accountKeys") or [])]
    n_bal = max(len(meta.get("preBalances") or []), len(meta.get("postBalances") or []))
    if len(keys) < n_bal:
        loaded = meta.get("loadedAddresses")
        if isinstance(loaded, dict):
            for bucket in ("writable", "readonly"):
                for k in (loaded.get(bucket) or []):
                    keys.append(_account_pubkey(k))
    return keys


def _token_entries(entries: Any) -> list[dict[str, Any]]:
    """Normalize pre/postTokenBalances into ``[{idx, owner, mint, ui}]`` records.

    Unlike :func:`_token_balance_map` this keeps each entry's ``accountIndex`` so the
    counterparty SOL-leg scan can skip the wallet's own token accounts. Totally
    defensive: malformed entries are dropped, never raised on."""
    out: list[dict[str, Any]] = []
    if not isinstance(entries, list):
        return out
    for e in entries:
        if not isinstance(e, dict):
            continue
        try:
            idx = int(e.get("accountIndex"))
        except (TypeError, ValueError):
            idx = -1
        uia = (e.get("uiTokenAmount") or {}).get("uiAmount")
        try:
            ui = float(uia) if uia is not None else 0.0
        except (TypeError, ValueError):
            ui = 0.0
        out.append({
            "idx": idx,
            "owner": str(e.get("owner") or ""),
            "mint": str(e.get("mint") or ""),
            "ui": ui,
        })
    return out


def _max_native_gain(meta: dict[str, Any], skip_indices: set[int]) -> float:
    """Largest native-SOL increase (in SOL) by any account NOT in ``skip_indices``.

    For a buy this is the SOL the bonding curve / creator vault received — i.e. the SOL
    leg of the swap as seen from the counterparty — independent of which wallet funded
    it. Used as a fallback to value a buy whose payer could not be attributed to the
    watched wallet (a bot- / third-party-funded buy). Returns 0.0 when nothing gained."""
    pre = meta.get("preBalances") or []
    post = meta.get("postBalances") or []
    best = 0.0
    for i in range(min(len(pre), len(post))):
        if i in skip_indices:
            continue
        try:
            gain = (int(post[i]) - int(pre[i])) / 1e9
        except (TypeError, ValueError):
            continue
        if gain > best:
            best = gain
    return best


def _is_signer(tx: dict[str, Any], wallet: str) -> bool:
    """True when ``wallet`` is a signer of the transaction (it authorized the action).

    A signer initiated the tx with their key, which distinguishes the wallet's OWN buy
    — even one whose SOL leg was routed/paid through another account — from a passive
    token arrival (airdrop / transfer-in), where the wallet signs nothing. Reads the
    ``signer`` flag on jsonParsed account entries, falling back to the first
    ``numRequiredSignatures`` static keys for non-jsonParsed encodings."""
    msg = (tx.get("transaction") or {}).get("message") or {}
    acct = msg.get("accountKeys") or []
    saw_flag = False
    for k in acct:
        if isinstance(k, dict) and "signer" in k:
            saw_flag = True
            if k.get("signer") and _account_pubkey(k) == wallet:
                return True
    if saw_flag:
        return False
    try:
        n = int((msg.get("header") or {}).get("numRequiredSignatures") or 0)
    except (TypeError, ValueError):
        n = 0
    return wallet in [_account_pubkey(k) for k in acct[: max(0, n)]]


def _infer_platform(tx: dict[str, Any]) -> str | None:
    msg = (tx.get("transaction") or {}).get("message") or {}
    instrs = msg.get("instructions") or []
    inner = tx.get("meta", {}).get("innerInstructions") or []
    pids: list[str] = []
    for ix in instrs:
        if isinstance(ix, dict) and ix.get("programId"):
            pids.append(str(ix["programId"]))
    for grp in inner:
        for ix in (grp.get("instructions") or []) if isinstance(grp, dict) else []:
            if isinstance(ix, dict) and ix.get("programId"):
                pids.append(str(ix["programId"]))
    for pid in pids:
        if pid in _PROGRAM_PLATFORM:
            return _PROGRAM_PLATFORM[pid]
    return None


def parse_buy(
    tx: dict[str, Any], wallet: str, *, min_sol: float = 0.0001
) -> dict[str, Any] | None:
    """Detect a BUY of an SPL token by ``wallet`` from a jsonParsed transaction.

    Returns ``None`` when the tx failed, is unrelated, or is not a buy. On a buy
    returns: sig, ca (mint), decimals, token_amount, sol_amount, fee_sol,
    holds_amount, created_at (blockTime), platform, quote ('SOL' | 'STABLE').
    """
    if not isinstance(tx, dict):
        return None
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return None

    inner_tx = tx.get("transaction") or {}
    sigs = inner_tx.get("signatures") or []
    signature = str(sigs[0]) if sigs else ""
    if not signature:
        return None

    # Account keys aligned 1:1 with pre/postBalances (incl. ALT-loaded addresses).
    keys = _balance_aligned_keys(tx, meta)

    pre_entries = _token_entries(meta.get("preTokenBalances"))
    post_entries = _token_entries(meta.get("postTokenBalances"))
    pre_tok = {(e["owner"], e["mint"]): e["ui"]
               for e in pre_entries if e["owner"] and e["mint"]}
    post_tok = {(e["owner"], e["mint"]): e["ui"]
                for e in post_entries if e["owner"] and e["mint"]}
    # Balance slots that are the wallet's OWN token accounts — excluded from the
    # counterparty SOL scan so a wrapped-SOL or change account is never mistaken for
    # the bonding curve.
    wallet_token_idx = {e["idx"] for e in pre_entries + post_entries
                        if e["owner"] == wallet and e["idx"] >= 0}

    # Token deltas for this wallet across all mints in the tx.
    mints = {m for (o, m) in {**pre_tok, **post_tok} if o == wallet}
    bought_mint: str | None = None
    bought_delta = 0.0
    wsol_spent = 0.0
    stable_spent = 0.0
    for mint in mints:
        delta = post_tok.get((wallet, mint), 0.0) - pre_tok.get((wallet, mint), 0.0)
        if mint == WSOL_MINT:
            if delta < 0:
                wsol_spent = max(wsol_spent, -delta)
            continue
        if mint in STABLE_MINTS:
            if delta < 0:
                stable_spent = max(stable_spent, -delta)
            continue
        if delta > bought_delta:
            bought_delta = delta
            bought_mint = mint

    if not bought_mint or bought_delta <= 0:
        return None

    # SOL spent natively (fee-inclusive) — read from the wallet's balance slot, which
    # now resolves correctly even when the wallet is an ALT-loaded account.
    fee_lamports = 0
    try:
        fee_lamports = int(meta.get("fee") or 0)
    except (TypeError, ValueError):
        fee_lamports = 0
    fee_sol = fee_lamports / 1e9

    native_spent = 0.0
    native_drop = 0.0   # the wallet's raw net lamport decrease (before fee), in SOL
    wallet_idx = keys.index(wallet) if wallet in keys else -1
    pre_bal = meta.get("preBalances") or []
    post_bal = meta.get("postBalances") or []
    if 0 <= wallet_idx < len(pre_bal) and wallet_idx < len(post_bal):
        try:
            native_drop = (int(pre_bal[wallet_idx]) - int(post_bal[wallet_idx])) / 1e9
        except (TypeError, ValueError):
            native_drop = 0.0
        if native_drop > 0:
            # Subtract the fee if this wallet paid it (fee payer = first key).
            native_spent = native_drop - (fee_sol if wallet_idx == 0 else 0.0)
            if native_spent < 0:
                native_spent = 0.0

    quote = "SOL"
    sol_amount = max(wsol_spent, native_spent)
    platform = _infer_platform(tx)

    # Fallback: when the watched wallet's OWN balances show only fee-sized dust, the SOL
    # leg may have been routed / funded through another account (common for trading-bot
    # buys) rather than leaving the wallet directly. Recover the amount from the
    # counterparty side — the SOL the bonding curve received, or the WSOL an AMM pool
    # received — so the buy is still valued by the SOL that actually changed hands. This
    # also covers a stable-FUNDED buy routed USDC/USDT -> SOL -> bonding curve: the wallet's
    # own native leg is just fees, but real SOL still reached the curve, so the buy is sized
    # by that recovered SOL leg. (A buy with NO SOL leg at all — a direct stable->token AMM
    # buy — finds nothing here and falls through to the stable branch below, valued by the
    # stable spent.) Hence this is no longer gated on stable_spent.
    #
    # This is deliberately conservative so it does not manufacture a buy out of a passive
    # token arrival (airdrop / transfer-in) and does not overwrite a correctly attributed
    # buy. It fires only when ALL hold:
    #   • the wallet's own measured spend is dust-sized (nothing substantial attributed);
    #   • the wallet actually PAID OUT a leg — its native balance net-decreased OR it spent
    #     wrapped SOL (WSOL) — which excludes sell / receive-only legs whose balance net-
    #     rises and excludes pure receipts that touch the wallet not at all (a SOL leg that
    #     is wrapped to WSOL with the native change / rent refunded still counts via WSOL);
    #   • there is real evidence this is the wallet's OWN swap — it either paid more than
    #     fees itself (native/WSOL outflow) OR the tx routes through a known DEX/router
    #     program AND the wallet signed it (a bare transfer to a non-signer can't qualify);
    #   • the recovered leg is both materially large AND dwarfs the wallet's measured
    #     spend (the ratio test), so a normally-attributed small buy — whose counterparty
    #     gain merely equals what the wallet paid — is left untouched, and rent-/fee-sized
    #     gains from unrelated accounts can't promote a non-buy.
    # Two residuals are accepted: (1) a single tx that bundles this buy with a *larger
    # unrelated* SOL movement could over-attribute (rare for a personal wallet, and far
    # less wrong than the dust value it replaces); (2) a buy fully funded by a third party
    # where the wallet pays nothing and signs nothing is indistinguishable from an airdrop
    # and is intentionally NOT valued.
    _ATTRIB_DUST = 0.05    # wallet-side spend at/below which we treat attribution as failed
    _MIN_SWAP = 0.02       # counterparty leg must clear rent/fee-sized noise to count
    _SWAP_RATIO = 4.0      # and must dwarf the wallet's own measured spend
    wallet_paid = native_spent > 0 or wsol_spent > 0
    wallet_swap = wallet_paid or (platform is not None and _is_signer(tx, wallet))
    # The wallet must have actually PAID OUT a leg of this swap — native SOL OR wrapped SOL
    # (WSOL). Gating on an outflow (rather than only on a NET-NATIVE decrease as before)
    # still excludes passive arrivals — an airdrop / transfer-in pays nothing, and a sell's
    # balances net-RISE — so the signer path cannot manufacture a buy out of a token that
    # merely lands in the wallet. But it no longer drops a real buy whose SOL was wrapped to
    # WSOL and whose native change / rent was refunded (native_drop <= 0 yet wsol_spent > 0),
    # which previously skipped recovery and stored the WSOL dust as the whole buy.
    wallet_outflow = native_drop > 0 or wsol_spent > 0
    if (quote == "SOL" and sol_amount < _ATTRIB_DUST
            and wallet_outflow and wallet_swap):
        skip = set(wallet_token_idx)
        if wallet_idx >= 0:
            skip.add(wallet_idx)
        counter_native = _max_native_gain(meta, skip)
        # Largest WSOL increase by a NON-wallet account (the WSOL an AMM pool / router
        # received for the swap). Keyed by account INDEX, not owner+mint: one pool / router
        # commonly owns several WSOL ATAs, and the owner-collapsed balance map keeps only
        # one ATA's pre-balance — making the receiving ATA's gain read as zero (or negative)
        # so the whole ~0.6-SOL leg vanishes. Per-index deltas over the union of pre/post
        # entries also tolerate an ATA present on only one side (opened or closed within the
        # tx); the missing side counts as 0.
        wsol_pre: dict[int, float] = {}
        wsol_post: dict[int, float] = {}
        wsol_owner: dict[int, str] = {}
        for e in pre_entries:
            if e["mint"] == WSOL_MINT and e["idx"] >= 0:
                wsol_pre[e["idx"]] = e["ui"]
                wsol_owner.setdefault(e["idx"], e["owner"])
        for e in post_entries:
            if e["mint"] == WSOL_MINT and e["idx"] >= 0:
                wsol_post[e["idx"]] = e["ui"]
                wsol_owner.setdefault(e["idx"], e["owner"])
        counter_wsol = 0.0
        for idx in wsol_pre.keys() | wsol_post.keys():
            owner = wsol_owner.get(idx, "")
            if not owner or owner == wallet:
                continue
            gain = wsol_post.get(idx, 0.0) - wsol_pre.get(idx, 0.0)
            if gain > counter_wsol:
                counter_wsol = gain
        swap_sol = max(counter_native, counter_wsol)
        if swap_sol >= _MIN_SWAP and swap_sol >= sol_amount * _SWAP_RATIO:
            sol_amount = swap_sol

    if quote == "SOL" and sol_amount < _ATTRIB_DUST and stable_spent > 0:
        # A stable-quoted buy (USDC/USDT) whose SOL leg, if any, was only fee-sized AND no
        # real SOL leg was recovered above — so the stable IS the payment. Value it via the
        # stable side; the dust native SOL is just fees / priority / tip. (Threshold is the
        # dust ceiling, not min_sol, so a fee-sized native leg can't keep it mislabeled SOL.)
        quote = "STABLE"
        sol_amount = 0.0
    if quote == "SOL" and sol_amount < min_sol:
        # No evidence of payment (token simply arrived) — not a buy.
        return None

    decimals = _decimals_for(meta.get("postTokenBalances"), wallet, bought_mint)
    holds_amount = post_tok.get((wallet, bought_mint), 0.0)
    block_time = tx.get("blockTime")
    try:
        created_at = int(block_time) if block_time is not None else int(time.time())
    except (TypeError, ValueError):
        created_at = int(time.time())

    return {
        "sig": signature,
        "ca": bought_mint,
        "decimals": decimals,
        "token_amount": bought_delta,
        "sol_amount": sol_amount if sol_amount > 0 else None,
        "fee_sol": fee_sol if fee_sol > 0 else None,
        "stable_spent": stable_spent if stable_spent > 0 else None,
        "holds_amount": holds_amount if holds_amount > 0 else None,
        "created_at": created_at,
        "platform": platform,
        "quote": quote,
    }
