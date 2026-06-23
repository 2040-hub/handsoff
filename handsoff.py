#!/usr/bin/env python3
"""HandsOff (再买剁手) — on-chain buy-discipline guard.

Watches your own Solana wallets via Helius free RPC, cards every BUY on a live web
page, and SPEAKS a soft / hard reminder when today's combined buy count crosses the
limits you set — so when you reach for "just one more ape", the page tells you to put
your hands off the wallet.

Architecture (all in one asyncio event loop):

    Helius RPC poller ──record──▶ SQLite store ◀──read── aiohttp web server ──▶ browser
          │                          ▲                          │
          └── getSignaturesForAddress + getTransaction          └── /api/stream (SSE)
              getAsset (metadata/price)                              soft/hard voice

Run:  python3 handsoff.py        (config from ./config.ini)
      python3 handsoff.py -c /path/to/config.ini

Requires Python >= 3.11 (developed on 3.13). See README.md for setup.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import configparser
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Any

# The web layer lives in a sibling package; make sure it imports whether the script
# is run from its own directory or elsewhere.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import aiohttp  # noqa: F401  (imported for the clear error if missing)
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "ERROR: aiohttp is required. Install dependencies:\n"
        "    pip install -r requirements.txt\n"
    )
    sys.exit(1)

from helius import HeliusClient, parse_buy  # noqa: E402
from web_module import (  # noqa: E402
    DisciplineWebServer,
    SignalEventHub,
    SignalStore,
)

# Wallet-login auth is OPTIONAL and imported behind a guard: it needs pynacl + base58,
# which the open (no-auth) build does not. If auth is requested in config but this
# import failed, the server refuses to start the UI (fail closed) rather than serve an
# ungated page — see HandsOffMonitor._resolve_auth.
try:
    from auth import (  # noqa: E402
        SessionManager,
        WalletWhitelist,
        auth_required,
        current_authorized_wallet,
        handle_nonce,
        handle_verify,
        handle_me,
        handle_logout,
    )
    _AUTH_AVAILABLE = True
    _AUTH_IMPORT_ERROR: Exception | None = None
except Exception as _auth_import_exc:  # pragma: no cover - defensive
    SessionManager = None  # type: ignore[assignment]
    WalletWhitelist = None  # type: ignore[assignment]
    auth_required = None  # type: ignore[assignment]
    current_authorized_wallet = None  # type: ignore[assignment]
    handle_nonce = handle_verify = handle_me = handle_logout = None  # type: ignore[assignment]
    _AUTH_AVAILABLE = False
    _AUTH_IMPORT_ERROR = _auth_import_exc

logger = logging.getLogger("HandsOff")

DEFAULT_CONFIG = "config.ini"


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
def _getint(cfg: configparser.ConfigParser, section: str, key: str, default: int) -> int:
    try:
        return cfg.getint(section, key, fallback=default)
    except (ValueError, configparser.Error):
        return default


def _getfloat(cfg: configparser.ConfigParser, section: str, key: str, default: float) -> float:
    try:
        return cfg.getfloat(section, key, fallback=default)
    except (ValueError, configparser.Error):
        return default


def _getbool(cfg: configparser.ConfigParser, section: str, key: str, default: bool) -> bool:
    try:
        return cfg.getboolean(section, key, fallback=default)
    except (ValueError, configparser.Error):
        return default


def parse_wallets(raw: str) -> "collections.OrderedDict[str, str]":
    """Parse ``ADDR:label, ADDR2:label2`` into an ordered ``{address: label}``.

    The label is optional (``ADDR`` alone maps to ""). Splits on the FIRST ':' only so
    a label may contain ':'. Addresses are kept verbatim (Solana base58 is
    case-sensitive); the first occurrence of a repeated address wins. Never raises.
    """
    out: "collections.OrderedDict[str, str]" = collections.OrderedDict()
    if not raw:
        return out
    for part in str(raw).replace("\n", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            addr, label = part.split(":", 1)
        else:
            addr, label = part, ""
        addr = addr.strip()
        label = label.strip()
        if not addr or addr in out:
            continue
        out[addr] = label
    return out


class Config:
    """Validated view over config.ini."""

    def __init__(self, path: str) -> None:
        if not os.path.exists(path):
            raise SystemExit(
                f"Config file not found: {path}\n"
                f"Copy config.ini.template to config.ini and fill it in."
            )
        cfg = configparser.ConfigParser(interpolation=None)
        try:
            cfg.read(path, encoding="utf-8")
        except configparser.Error as exc:
            raise SystemExit(f"Could not parse {path}: {exc}")

        # [HELIUS]
        self.api_key = cfg.get("HELIUS", "api_key", fallback="").strip()
        self.rpc_url = cfg.get("HELIUS", "rpc_url", fallback="").strip() or None
        self.poll_interval = max(5, _getint(cfg, "HELIUS", "poll_interval", 25))
        self.signatures_limit = max(1, min(100, _getint(cfg, "HELIUS", "signatures_limit", 25)))
        self.max_pages = max(1, min(20, _getint(cfg, "HELIUS", "max_pages_per_poll", 4)))
        self.price_refresh_interval = max(30, _getint(cfg, "HELIUS", "price_refresh_interval", 300))
        self.metadata_ttl = max(0, _getint(cfg, "HELIUS", "metadata_ttl", 86400))
        self.price_ttl = max(0, _getint(cfg, "HELIUS", "price_ttl", 300))
        self.seed_silent = _getbool(cfg, "HELIUS", "seed_silent", True)
        self.min_sol = max(0.0, _getfloat(cfg, "HELIUS", "min_sol_amount", 0.0001))

        # [WALLETS]
        self.wallets = parse_wallets(cfg.get("WALLETS", "addresses", fallback=""))

        # [DISCIPLINE]
        self.soft_buy_amount = max(0, _getint(cfg, "DISCIPLINE", "soft_buy_amount", 0))
        self.hard_buy_amount = max(0, _getint(cfg, "DISCIPLINE", "hard_buy_amount", 0))
        self.soft_max_alerts = max(0, _getint(cfg, "DISCIPLINE", "soft_max_alerts", 0))
        self.hard_max_alerts = max(0, _getint(cfg, "DISCIPLINE", "hard_max_alerts", 0))
        self.label = cfg.get("DISCIPLINE", "label", fallback="").strip()
        self.gmgn_url = cfg.get("DISCIPLINE", "panel_url", fallback="").strip()
        # When true (default, the original behavior) every buy counts, so buying the
        # SAME token N times today adds N to the self-discipline count. When false, a
        # repeat buy of a CA already bought today does NOT count again — each distinct
        # CA counts once, so re-entering one position can't trip the soft/hard limit.
        self.enable_duplicate_buy = _getbool(cfg, "DISCIPLINE", "enable_duplicate_buy", True)

        # [CHIME]
        self.chime_enabled = _getbool(cfg, "CHIME", "enabled", True)
        self.chime_start_seq = max(1, _getint(cfg, "CHIME", "start_seq", 1))
        self.chime_max = max(0, _getint(cfg, "CHIME", "max", 5))

        # [WEB]
        self.web_host = cfg.get("WEB", "host", fallback="127.0.0.1").strip() or "127.0.0.1"
        self.web_port = _getint(cfg, "WEB", "port", 8787)
        self.db_path = cfg.get("WEB", "db_path", fallback="handsoff.db").strip() or "handsoff.db"
        # Optional Solana-wallet-whitelist login (see auth/). Off by default => the UI
        # is open on the bind address (keep it on 127.0.0.1).
        self.web_enable_auth = _getbool(cfg, "WEB", "enable_auth", False)
        self.web_whitelist_raw = cfg.get("WEB", "whitelist_wallet_lists", fallback="")
        self.web_session_secret = cfg.get("WEB", "session_secret", fallback="").strip()
        self.web_session_ttl = _getint(cfg, "WEB", "session_ttl", 604800)
        self.web_nonce_ttl = _getint(cfg, "WEB", "nonce_ttl", 300)

        # [GENERAL]
        self.log_level = cfg.get("GENERAL", "log_level", fallback="INFO").strip().upper()

    def validate(self) -> None:
        if not self.api_key:
            raise SystemExit(
                "No Helius api_key configured in [HELIUS]. Get a free key at "
                "https://dashboard.helius.dev and set api_key in config.ini."
            )
        if not self.wallets:
            raise SystemExit(
                "No wallets configured in [WALLETS] addresses. Add at least one "
                "ADDRESS:label entry to monitor."
            )

    @property
    def first_wallet(self) -> str:
        return next(iter(self.wallets), "")

    def server_config(self) -> dict[str, Any]:
        """The dict the web server reads for /api/config + voice claims."""
        addr = self.first_wallet
        label = self.label or ("My Wallets" if len(self.wallets) != 1
                               else (self.wallets[addr] or ""))
        return {
            "chime_enabled": self.chime_enabled,
            "chime_start_seq": self.chime_start_seq,
            "chime_max": self.chime_max,
            # The panel represents YOU; the count is combined across all wallets.
            "myself_address": addr,
            "myself_label": label,
            "myself_url": self.gmgn_url,
            "soft_buy_amount": self.soft_buy_amount,
            "hard_buy_amount": self.hard_buy_amount,
            "soft_max_alerts": self.soft_max_alerts,
            "hard_max_alerts": self.hard_max_alerts,
            # false => repeat buys of the same CA collapse to one in today's count.
            "enable_duplicate_buy": self.enable_duplicate_buy,
        }


# --------------------------------------------------------------------------- #
# buy valuation (shared by the live recorder and the repair tool)
# --------------------------------------------------------------------------- #
def value_buy(
    *,
    quote: str | None,
    stable_spent: Any = None,
    sol_amount: Any = None,
    sol_price: Any = None,
    token_amount: Any = None,
    supply: Any = None,
    decimals: Any = None,
    holds_amount: Any = None,
) -> dict[str, float | None]:
    """Derive a buy's USD value, unit price, market cap and holdings % from the parsed
    on-chain amounts plus a SOL/USD price — the single source of truth shared by the
    live recorder (:meth:`HandsOffMonitor._record_buy`) and the offline repair tool, so
    both value a buy identically.

    Pure and total: performs no I/O and never raises (all stateful inputs — the SOL
    price and the token's supply/decimals — are resolved by the caller and passed in).

    Every money field is derived STRICTLY from the buy itself:

    * ``usd_amount`` = the stable leg (USDC/USDT-quoted buy) else ``sol_amount × sol_price``
    * ``price``      = ``usd_amount ÷ token_amount``
    * ``market_cap`` = ``price × circulating_supply``  (``supply ÷ 10**decimals``)
    * ``holds_pct``  = ``holds_amount ÷ circulating_supply × 100``

    When an input is missing the dependent field stays ``None`` rather than being
    substituted with a live/current value — so the persisted "at buy time" price and
    market cap can never be contaminated by a later market price. ``circ`` is returned
    too for callers that want it. Falsy (incl. ``0``) inputs are treated as missing,
    mirroring the original recorder's truthiness gates.
    """
    # Circulating supply = raw supply / 10**decimals.
    circ: float | None = None
    if supply is not None and decimals is not None:
        try:
            d = int(decimals)
            # Guard the exponent: real SPL mints use a small u8. An absurd value from
            # hostile / garbled metadata would otherwise build a giant int.
            if 0 <= d <= 64:
                circ = float(supply) / (10 ** d)
                if circ <= 0:
                    circ = None
        except (TypeError, ValueError, ZeroDivisionError, OverflowError):
            circ = None

    # USD spent: a stable-quoted buy is valued by its stable leg; a SOL buy by the SOL
    # leg × the SOL/USD price.
    usd_amount: float | None = None
    if quote == "STABLE" and stable_spent:
        try:
            usd_amount = float(stable_spent)
        except (TypeError, ValueError):
            usd_amount = None
    elif sol_amount and sol_price:
        try:
            usd_amount = float(sol_amount) * float(sol_price)
        except (TypeError, ValueError):
            usd_amount = None

    # Unit price strictly from this buy (USD ÷ tokens received) — NO live-price fallback,
    # so the stored buy-time price stays a pure function of the buy.
    price: float | None = None
    if usd_amount and token_amount:
        try:
            price = usd_amount / float(token_amount)
        except (TypeError, ValueError, ZeroDivisionError):
            price = None

    market_cap: float | None = None
    if price and circ:
        try:
            market_cap = float(price) * circ
        except (TypeError, ValueError):
            market_cap = None

    holds_pct: float | None = None
    if holds_amount and circ:
        try:
            holds_pct = float(holds_amount) / circ * 100.0
        except (TypeError, ValueError, ZeroDivisionError):
            holds_pct = None

    return {
        "usd_amount": usd_amount,
        "price": price,
        "market_cap": market_cap,
        "holds_pct": holds_pct,
        "circ": circ,
    }


# --------------------------------------------------------------------------- #
# monitor
# --------------------------------------------------------------------------- #
class HandsOffMonitor:
    def __init__(self, config: Config) -> None:
        self.cfg = config
        self.store = SignalStore(config.db_path)
        self.hub = SignalEventHub()
        self.helius: HeliusClient | None = None
        self.web: DisciplineWebServer | None = None
        self._stop = asyncio.Event()
        # Bounded set of processed tx signatures (a sig can surface for >1 watched
        # wallet) so each buy is carded exactly once.
        self._processed: collections.OrderedDict[str, None] = collections.OrderedDict()
        self._processed_max = 8000
        self._sol_price: float | None = None
        self._sol_price_at = 0.0

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        if not self.store.ok:
            logger.warning(
                "Signal store unavailable (db_path=%s) — cards will not persist.",
                self.cfg.db_path,
            )
        self.helius = HeliusClient(self.cfg.api_key, rpc_url=self.cfg.rpc_url)
        static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
        auth_bundle, web_ok = self._resolve_auth()
        if web_ok:
            self.web = DisciplineWebServer(
                self.store,
                host=self.cfg.web_host,
                port=self.cfg.web_port,
                static_dir=static_dir,
                version="1.1.1",
                signal_hub=self.hub,
                config=self.cfg.server_config(),
                auth_bundle=auth_bundle,
            )
            try:
                await self.web.start()
            except OSError as exc:
                logger.error(
                    "Web server failed to bind %s:%d (%s) — continuing headless.",
                    self.cfg.web_host, self.cfg.web_port, exc,
                )
                self.web = None
        else:
            # _resolve_auth already logged WHY (fail-closed). Keep polling headlessly.
            self.web = None
            logger.warning(
                "Web UI NOT started (wallet-auth misconfigured) — the poller keeps "
                "running and buys are still persisted to SQLite."
            )

        await self._seed_cursors()

        tasks = [
            asyncio.create_task(self._poll_loop(), name="poll"),
            asyncio.create_task(self._price_loop(), name="price"),
        ]
        self._log_startup()
        try:
            await self._stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._shutdown()

    def request_stop(self) -> None:
        self._stop.set()

    def _resolve_auth(self) -> tuple[dict[str, Any] | None, bool]:
        """Build the wallet-auth bundle for the web server.

        Returns ``(bundle_or_None, ok_to_serve)``. When auth is OFF, returns
        ``(None, True)`` (serve openly). When auth is ON it FAILS CLOSED — refusing to
        serve the UI at all (``ok_to_serve=False``) rather than serve an ungated or
        forgeable page — if the auth deps are missing or ``session_secret`` is weak.
        An empty whitelist is allowed but warned (nobody can sign in until populated).
        """
        if not self.cfg.web_enable_auth:
            return None, True

        if not _AUTH_AVAILABLE:
            logger.error(
                "enable_auth=true but auth deps (pynacl/base58) failed to import: %s. "
                "Refusing to serve an UNGATED page — `pip install -r requirements.txt` "
                "or set enable_auth=false.",
                _AUTH_IMPORT_ERROR,
            )
            return None, False

        secret = self.cfg.web_session_secret or ""
        if (not secret) or ("CHANGE_ME" in secret) or (len(secret) < 16):
            logger.error(
                "enable_auth=true but [WEB] session_secret is unset, still the default, "
                "or shorter than 16 chars. Refusing to serve a gate whose session tokens "
                "could be forged — set a long random session_secret in config.ini. "
                "Generate one: python3 -c \"import secrets;print(secrets.token_urlsafe(48))\""
            )
            return None, False

        whitelist = WalletWhitelist(self.cfg.web_whitelist_raw)
        if whitelist.is_empty:
            logger.warning(
                "enable_auth=true but whitelist_wallet_lists is empty — NO wallet can "
                "sign in until you add addresses to config.ini."
            )
        session_mgr = SessionManager(
            secret=secret,
            session_ttl=self.cfg.web_session_ttl,
            nonce_ttl=self.cfg.web_nonce_ttl,
        )
        logger.info(
            "🔐 Web wallet login ENABLED — %d whitelisted wallet(s)", len(whitelist)
        )
        bundle = {
            "session_manager": session_mgr,
            "whitelist": whitelist,
            "guard": current_authorized_wallet,
            "auth_required": auth_required,
            "handlers": {
                "nonce": handle_nonce,
                "verify": handle_verify,
                "me": handle_me,
                "logout": handle_logout,
            },
        }
        return bundle, True

    async def _shutdown(self) -> None:
        logger.info("Shutting down…")
        if self.web is not None:
            try:
                await self.web.stop()
            except Exception:
                pass
        if self.helius is not None:
            try:
                await self.helius.close()
            except Exception:
                pass
        self.store.close()

    def _log_startup(self) -> None:
        logger.info("HandsOff (再买剁手) started")
        logger.info("  • Web UI: http://%s:%d", self.cfg.web_host, self.cfg.web_port)
        logger.info("  • Wallets: %d", len(self.cfg.wallets))
        for addr, label in self.cfg.wallets.items():
            logger.info("      - %s  %s", addr[:6] + "…" + addr[-4:], label or "")
        lim = []
        if self.cfg.soft_buy_amount > 0:
            s = f"soft={self.cfg.soft_buy_amount}"
            if self.cfg.soft_max_alerts > 0:
                s += f"×{self.cfg.soft_max_alerts}"
            lim.append(s)
        if self.cfg.hard_buy_amount > 0:
            h = f"hard={self.cfg.hard_buy_amount}"
            if self.cfg.hard_max_alerts > 0:
                h += f"×{self.cfg.hard_max_alerts}"
            lim.append(h)
        logger.info("  • Discipline: %s", ", ".join(lim) if lim else "no limits set")
        logger.info("  • Poll interval: %ds", self.cfg.poll_interval)

    # ------------------------------------------------------------------ #
    # cursor seeding (avoid replaying history on first run)
    # ------------------------------------------------------------------ #
    def _cursor_key(self, wallet: str) -> str:
        return f"cursor:{wallet}"

    async def _seed_cursors(self) -> None:
        if not self.cfg.seed_silent:
            return
        assert self.helius is not None
        for wallet in self.cfg.wallets:
            if self.store.get_meta(self._cursor_key(wallet)):
                continue
            try:
                sigs = await self.helius.get_signatures_for_address(wallet, limit=1)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("seed cursor for %s failed: %s", wallet[:6], exc)
                continue
            seed_sig = sigs[0].get("signature") if sigs else None
            if seed_sig:
                self.store.set_meta(self._cursor_key(wallet), seed_sig)
                logger.info("Seeded cursor for %s (no backfill)", wallet[:6] + "…")

    def _mark_processed(self, sig: str) -> bool:
        """Record a signature as processed; return True if it was NEW."""
        if sig in self._processed:
            self._processed.move_to_end(sig)
            return False
        self._processed[sig] = None
        while len(self._processed) > self._processed_max:
            self._processed.popitem(last=False)
        return True

    # ------------------------------------------------------------------ #
    # polling
    # ------------------------------------------------------------------ #
    async def _poll_loop(self) -> None:
        while not self._stop.is_set():
            for wallet, label in list(self.cfg.wallets.items()):
                if self._stop.is_set():
                    break
                try:
                    await self._poll_wallet(wallet, label)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.error("poll %s failed: %s", wallet[:6], exc, exc_info=True)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.poll_interval)
            except asyncio.TimeoutError:
                pass

    async def _poll_wallet(self, wallet: str, label: str) -> None:
        assert self.helius is not None
        cursor = self.store.get_meta(self._cursor_key(wallet))
        # Collect every signature NEWER than the cursor. getSignaturesForAddress returns
        # newest-first, bounded below by `until=cursor` and capped at `limit`; if a page
        # comes back full there may be more between it and the cursor, so page downward
        # with `before` (still bounded by `until`) until a short page or the page cap.
        new_sigs: list[dict[str, Any]] = []
        before: str | None = None
        for _ in range(self.cfg.max_pages):
            batch = await self.helius.get_signatures_for_address(
                wallet, until=cursor, before=before, limit=self.cfg.signatures_limit
            )
            if not batch:
                break
            new_sigs.extend(batch)
            if len(batch) < self.cfg.signatures_limit:
                break
            before = batch[-1].get("signature")
        if not new_sigs:
            return

        newest = new_sigs[0].get("signature")
        # Process oldest -> newest so cards land in chronological order.
        interrupted = False
        for entry in reversed(new_sigs):
            if self._stop.is_set():
                interrupted = True
                break
            sig = entry.get("signature")
            if not sig:
                continue
            if entry.get("err") is not None:
                continue
            if not self._mark_processed(sig):
                continue
            try:
                await self._handle_signature(sig, wallet, label)
            except Exception as exc:  # pragma: no cover - defensive
                logger.error("handle %s failed: %s", sig[:8], exc, exc_info=True)

        # Advance the cursor only when the whole batch was processed. On a mid-batch
        # stop we leave it unmoved so the next run re-fetches the unprocessed (older)
        # sigs; record_buy is idempotent on `sig`, so already-carded buys never dupe.
        if newest and not interrupted:
            self.store.set_meta(self._cursor_key(wallet), newest)

    async def _handle_signature(self, sig: str, wallet: str, label: str) -> None:
        assert self.helius is not None
        tx = await self.helius.get_transaction(sig)
        if not tx:
            return
        parsed = parse_buy(tx, wallet, min_sol=self.cfg.min_sol)
        if not parsed:
            return
        await self._record_buy(parsed, wallet, label)

    # ------------------------------------------------------------------ #
    # recording
    # ------------------------------------------------------------------ #
    async def _record_buy(self, parsed: dict[str, Any], wallet: str, label: str) -> None:
        mint = parsed.get("ca")
        if not mint:
            return
        meta = await self._ensure_token(mint, parsed.get("decimals"))

        decimals = parsed.get("decimals")
        if decimals is None:
            decimals = meta.get("decimals")

        token_amount = parsed.get("token_amount")
        sol_amount = parsed.get("sol_amount")
        quote = parsed.get("quote")
        holds_amount = parsed.get("holds_amount")

        # Resolve the SOL/USD price only when a SOL leg actually needs valuing — a
        # stable-quoted buy and an amount-less row never trigger a price fetch (matches
        # the prior behavior).
        sol_price = await self._sol_usd() if (quote != "STABLE" and sol_amount) else None

        # Single source of truth for the buy-time money fields (shared with repair.py).
        # NOTE: price / market_cap are derived STRICTLY from this buy — there is no
        # live-price fallback, so an "at buy time" column can never hold a current price.
        metrics = value_buy(
            quote=quote,
            stable_spent=parsed.get("stable_spent"),
            sol_amount=sol_amount,
            sol_price=sol_price,
            token_amount=token_amount,
            supply=meta.get("supply"),
            decimals=decimals,
            holds_amount=holds_amount,
        )
        usd_amount = metrics["usd_amount"]
        price = metrics["price"]
        market_cap = metrics["market_cap"]
        holds_pct = metrics["holds_pct"]

        created_at = int(parsed.get("created_at") or time.time())
        try:
            created_iso = datetime.fromtimestamp(created_at, timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            # Degenerate block time — fall back to now rather than dropping the buy.
            created_at = int(time.time())
            created_iso = datetime.fromtimestamp(created_at, timezone.utc).isoformat()

        res = self.store.record_buy(
            sig=parsed["sig"],
            ca=mint,
            wallet=wallet,
            wallet_label=label or None,
            name=meta.get("name"),
            symbol=meta.get("symbol"),
            platform=parsed.get("platform") or meta.get("platform"),
            sol_amount=sol_amount,
            fee_sol=parsed.get("fee_sol"),
            usd_amount=usd_amount,
            token_amount=token_amount,
            price=price,
            holds_amount=holds_amount,
            holds_pct=holds_pct,
            upnl=None,
            market_cap=market_cap,
            tx=parsed["sig"],
            created_at=created_at,
            created_at_iso=created_iso,
        )
        if res.get("inserted"):
            sym = meta.get("symbol") or mint[:6]
            logger.info(
                "BUY  %-10s by %s  %.4f SOL  (#%d this token)",
                sym, label or wallet[:6] + "…",
                float(sol_amount or 0), res.get("seq", 0),
            )
            # Wake every connected browser so the card + voice fire in milliseconds.
            try:
                self.hub.publish()
            except Exception:
                pass

    async def _ensure_token(self, mint: str, decimals_hint: Any = None) -> dict[str, Any]:
        """Return cached/fresh token metadata, fetching from Helius on first sight or
        when the cache is stale. Always returns a dict (possibly partial)."""
        assert self.helius is not None
        now = int(time.time())
        cached = self.store.get_token(mint) or {}
        meta_age = now - (cached.get("meta_updated_at") or 0)
        price_age = now - (cached.get("price_updated_at") or 0)
        need_meta = not cached or (self.cfg.metadata_ttl and meta_age >= self.cfg.metadata_ttl)
        need_price = (self.cfg.price_ttl == 0) or (price_age >= self.cfg.price_ttl) or not cached.get("current_market_cap")

        if need_meta or need_price:
            fetched = await self.helius.fetch_metadata(mint)
            if fetched:
                self.store.upsert_token(
                    mint,
                    name=fetched.get("name"),
                    symbol=fetched.get("symbol"),
                    icon=fetched.get("icon"),
                    twitter=fetched.get("twitter"),
                    website=fetched.get("website"),
                    decimals=fetched.get("decimals"),
                    supply=fetched.get("supply"),
                    current_price=fetched.get("price"),
                    current_market_cap=fetched.get("market_cap"),
                )
                cached = self.store.get_token(mint) or cached
                # Merge freshly-fetched fields not yet persisted (e.g. raw supply).
                for k in ("name", "symbol", "icon", "twitter", "website",
                          "decimals", "supply", "price", "market_cap"):
                    if fetched.get(k) is not None and not cached.get(_map_token_key(k)):
                        cached[_map_token_key(k)] = fetched[k]
        # Normalize the return to the keys _record_buy expects.
        return {
            "name": cached.get("name"),
            "symbol": cached.get("symbol"),
            "icon": cached.get("icon"),
            "twitter": cached.get("twitter"),
            "website": cached.get("website"),
            "platform": cached.get("platform"),
            "decimals": cached.get("decimals") if cached.get("decimals") is not None else decimals_hint,
            "supply": cached.get("supply"),
            "price": cached.get("current_price"),
            "market_cap": cached.get("current_market_cap"),
        }

    # ------------------------------------------------------------------ #
    # SOL price + periodic mcap refresh
    # ------------------------------------------------------------------ #
    async def _sol_usd(self) -> float | None:
        now = time.monotonic()
        if self._sol_price is not None and (now - self._sol_price_at) < 60:
            return self._sol_price
        assert self.helius is not None
        try:
            price = await self.helius.get_sol_price()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("SOL price fetch failed: %s", exc)
            price = None
        if price:
            self._sol_price = price
            self._sol_price_at = now
        return self._sol_price

    async def _price_loop(self) -> None:
        # Stagger the first refresh so startup isn't a thundering herd.
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=min(60, self.cfg.price_refresh_interval))
        except asyncio.TimeoutError:
            pass
        while not self._stop.is_set():
            try:
                await self._refresh_prices()
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("price refresh failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.price_refresh_interval)
            except asyncio.TimeoutError:
                pass

    async def _refresh_prices(self) -> None:
        assert self.helius is not None
        since = int(time.time()) - 86400
        mints = self.store.recent_mints(limit=30, since_epoch=since)
        for mint in mints:
            if self._stop.is_set():
                break
            fetched = await self.helius.fetch_metadata(mint)
            if fetched and (fetched.get("market_cap") or fetched.get("price")):
                self.store.upsert_token(
                    mint,
                    current_price=fetched.get("price"),
                    current_market_cap=fetched.get("market_cap"),
                )


def _map_token_key(fetched_key: str) -> str:
    """Map a fetch_metadata key to its tokens-table column name."""
    return {"price": "current_price", "market_cap": "current_market_cap"}.get(
        fetched_key, fetched_key
    )


# --------------------------------------------------------------------------- #
# entrypoint
# --------------------------------------------------------------------------- #
def _setup_logging(level: str) -> None:
    lvl = getattr(logging, level, logging.INFO)
    logging.basicConfig(
        level=lvl,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # aiohttp access logs are disabled at the server; keep its noise down anyway.
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


async def _amain(config: Config) -> None:
    monitor = HandsOffMonitor(config)
    loop = asyncio.get_running_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        s = getattr(signal, sig_name, None)
        if s is None:
            continue
        try:
            loop.add_signal_handler(s, monitor.request_stop)
        except (NotImplementedError, RuntimeError):
            # add_signal_handler is unavailable on some platforms (e.g. Windows).
            pass
    await monitor.run()


def main() -> None:
    parser = argparse.ArgumentParser(description="HandsOff (再买剁手) buy-discipline guard")
    parser.add_argument(
        "-c", "--config", default=DEFAULT_CONFIG,
        help=f"path to config.ini (default: {DEFAULT_CONFIG})",
    )
    args = parser.parse_args()

    config = Config(args.config)
    _setup_logging(config.log_level)
    config.validate()

    try:
        asyncio.run(_amain(config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
