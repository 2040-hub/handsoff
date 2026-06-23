#!/usr/bin/env python3
"""HandsOff (再买剁手) — historical buy-data repair.

Re-fetches and re-parses buys that the OLD parser mis-valued and rewrites the row with
the corrected, post-fix numbers.

THE BUG IT FIXES
----------------
A bot- / router-routed buy (Trojan, Bonk, Padre, GMGN, Axiom, …) routes the SOL through
another account, so the watched wallet's OWN native balance only drops a fee-sized tip.
The pre-fix parser read that tip as the buy size, collapsing ``sol_amount`` ~100×
(e.g. a real 0.65 SOL buy stored as 0.006) and — because ``usd_amount``, ``price`` and
``market_cap`` derive linearly from it — collapsing all four money fields by the same
factor while ``token_amount`` / holdings stayed correct. The parser fix (counterparty-leg
recovery) corrected this going forward; this tool repairs the rows recorded BEFORE it.

HOW IT REPAIRS A ROW
--------------------
It re-runs the SAME parser (:func:`helius.parse_buy`) and the SAME valuation
(:func:`handsoff.value_buy`) the live recorder uses, so a repaired row is byte-for-byte
what the fixed poller would write today. The historical SOL/USD price is recovered from
the row's own ``usd_amount / sol_amount`` ratio — that ratio survives the collapse
intact — so the rebuilt USD value reflects buy-time economics, not today's SOL price; it
falls back to the live SOL price only when the row never stored a ``usd_amount``.

A row is rewritten only when a fresh parse materially RAISES the SOL leg
(``--min-ratio``, default 1.1×), so a genuinely tiny buy or an already-correct row is
left untouched. Idempotent: re-running after a repair is a no-op.

USAGE
-----
    python3 repair.py                  # DRY-RUN scan of dust-sized buys (<= 0.05 SOL)
    python3 repair.py --apply          # actually write the corrections
    python3 repair.py --sig <SIG>      # repair one transaction by signature
    python3 repair.py --ca <MINT>      # repair every buy of one mint
    python3 repair.py --max-sol 0.1    # widen the dust threshold for the scan
    python3 repair.py -c /path/config.ini --apply

Stop the live poller first (or be ready for brief SQLITE_BUSY); back up the DB
(``cp handsoff.db handsoff.db.bak``) before ``--apply``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import sys
from typing import Any

# The web layer + helius client live alongside this script; make sure they import
# whether repair.py is run from its own directory or elsewhere.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import aiohttp  # noqa: F401  (imported for a clear error if missing)
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "ERROR: aiohttp is required. Install dependencies:\n"
        "    pip install -r requirements.txt\n"
    )
    sys.exit(1)

from helius import HeliusClient, parse_buy  # noqa: E402
from handsoff import Config, value_buy, _setup_logging  # noqa: E402
from web_module import SignalStore  # noqa: E402

logger = logging.getLogger("HandsOffRepair")

DEFAULT_CONFIG = "config.ini"


def _f(value: Any) -> float | None:
    """Coerce to a finite float, else None. Never raises."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _fmt(value: Any) -> str:
    f = _f(value)
    if f is None:
        return "—"
    if f != 0 and abs(f) < 1e-4:
        return f"{f:.3e}"
    return f"{f:,.6g}"


class _Stats:
    __slots__ = ("scanned", "repaired", "skipped", "missing", "unparsed", "errors")

    def __init__(self) -> None:
        self.scanned = self.repaired = self.skipped = 0
        self.missing = self.unparsed = self.errors = 0


def _select_candidates(store: SignalStore, args: argparse.Namespace) -> list[dict[str, Any]]:
    """Resolve the work-list from the CLI target flags."""
    if args.sig:
        row = store.get_signal(args.sig)
        if not row:
            logger.error("No buy row found with sig=%s", args.sig)
            return []
        return [row]
    if args.ca:
        rows = store.signals_for_ca(args.ca, limit=args.limit)
        if not rows:
            logger.error("No buy rows found for ca=%s", args.ca)
        return rows
    return store.under_attributed_signals(max_sol=args.max_sol, limit=args.limit)


async def _repair_row(
    row: dict[str, Any],
    *,
    helius: HeliusClient,
    store: SignalStore,
    cfg: Config,
    args: argparse.Namespace,
    stats: _Stats,
) -> None:
    sig = (row.get("sig") or "").strip()
    wallet = (row.get("wallet") or "").strip()
    ca = (row.get("ca") or "").strip()
    label = row.get("symbol") or row.get("name") or (ca[:6] if ca else "?")
    if not sig or not wallet:
        # Without the original signer we cannot re-attribute the buy.
        stats.skipped += 1
        logger.debug("skip %s: missing sig/wallet", sig[:8] or "?")
        return

    tx = await helius.get_transaction(sig)
    if not tx:
        stats.missing += 1
        logger.warning("skip %-10s %s…: transaction not found (RPC pruned?)", label, sig[:8])
        return

    parsed = parse_buy(tx, wallet, min_sol=cfg.min_sol)
    if not parsed:
        stats.unparsed += 1
        logger.info("skip %-10s %s…: no longer parses as a buy by this wallet", label, sig[:8])
        return

    new_sol = _f(parsed.get("sol_amount"))
    old_sol = _f(row.get("sol_amount"))
    if new_sol is None:
        # A re-parse with no SOL leg (e.g. now classed stable) — leave the row alone.
        stats.skipped += 1
        logger.debug("skip %s…: re-parse has no SOL leg", sig[:8])
        return
    # Only rewrite when the fresh parse MATERIALLY raises the SOL leg; an already-correct
    # or genuinely-tiny buy is left untouched (keeps the tool idempotent + conservative).
    if old_sol and new_sol < old_sol * args.min_ratio:
        stats.skipped += 1
        logger.debug(
            "skip %s…: sol %s -> %s not materially larger", sig[:8], _fmt(old_sol), _fmt(new_sol)
        )
        return

    # Recover the buy-time SOL/USD price from the row's own (collapsed) ratio — it is
    # preserved through the linear collapse — else fall back to the live price.
    old_usd = _f(row.get("usd_amount"))
    if old_usd and old_sol:
        sol_price: float | None = old_usd / old_sol
        price_src = "row"
    else:
        sol_price = await helius.get_sol_price()
        price_src = "live"

    if not sol_price:
        # No recoverable ratio AND the live price lookup failed/returned nothing. Skip
        # rather than rewrite a corrected SOL amount alongside a blanked USD value — that
        # would only make the row MORE inconsistent (and a SOL-price outage shouldn't be
        # able to wipe a column). Re-run later when pricing is available.
        stats.skipped += 1
        logger.warning(
            "skip %-10s %s…: no SOL/USD price available — not rewriting", label, sig[:8]
        )
        return

    # supply / decimals for the market-cap leg: prefer the cached token row, then the
    # parse, then a fresh metadata fetch. If still missing, market_cap is simply left
    # out of the update below (the existing value is preserved, never nulled).
    token = store.get_token(ca) or {}
    supply = token.get("supply")
    decimals = parsed.get("decimals")
    if decimals is None:
        decimals = token.get("decimals")
    if supply is None or decimals is None:
        fetched = await helius.fetch_metadata(ca)
        if supply is None:
            supply = fetched.get("supply")
        if decimals is None:
            decimals = fetched.get("decimals")

    metrics = value_buy(
        quote=parsed.get("quote"),
        stable_spent=parsed.get("stable_spent"),
        sol_amount=new_sol,
        sol_price=sol_price,
        token_amount=parsed.get("token_amount"),
        supply=supply,
        decimals=decimals,
        holds_amount=parsed.get("holds_amount"),
    )

    stats.scanned += 1
    arrow = "APPLY" if args.apply else "DRY  "
    logger.info(
        "%s %-10s %s…  sol %s -> %s  usd %s -> %s  px %s -> %s  mc %s -> %s  [solprice:%s]",
        arrow, label, sig[:8],
        _fmt(old_sol), _fmt(new_sol),
        _fmt(row.get("usd_amount")), _fmt(metrics["usd_amount"]),
        _fmt(row.get("price")), _fmt(metrics["price"]),
        _fmt(row.get("market_cap")), _fmt(metrics["market_cap"]),
        price_src,
    )

    if not args.apply:
        return
    # Only write fields we actually recomputed — a None metric is OMITTED so it can NEVER
    # overwrite a previously-populated column with NULL (e.g. market_cap when the token's
    # supply is momentarily unresolvable). sol_amount + usd_amount + price are guaranteed
    # present here (the SOL price resolved above); the rest are best-effort.
    candidate_fields: dict[str, Any] = {
        "sol_amount": new_sol,
        "fee_sol": parsed.get("fee_sol"),
        "usd_amount": metrics["usd_amount"],
        "token_amount": parsed.get("token_amount"),
        "price": metrics["price"],
        "holds_amount": parsed.get("holds_amount"),
        "market_cap": metrics["market_cap"],
        "holds_pct": metrics["holds_pct"],
    }
    fields = {k: v for k, v in candidate_fields.items() if v is not None}
    ok = store.update_buy(sig, **fields)
    if ok:
        stats.repaired += 1
    else:
        stats.errors += 1
        logger.warning("update failed for %s…", sig[:8])


async def _amain(cfg: Config, args: argparse.Namespace) -> int:
    store = SignalStore(cfg.db_path)
    if not store.ok:
        logger.error("Signal store unavailable (db_path=%s) — nothing to repair.", cfg.db_path)
        return 2

    candidates = _select_candidates(store, args)
    if not candidates:
        logger.info("No candidate rows to inspect.")
        store.close()
        return 0

    logger.info(
        "Inspecting %d candidate row(s) %s (min-ratio %.2f). %s",
        len(candidates),
        "[--apply: writing corrections]" if args.apply else "[DRY-RUN: no writes]",
        args.min_ratio,
        "Back up handsoff.db and stop the poller before --apply." if args.apply else "",
    )

    stats = _Stats()
    helius = HeliusClient(cfg.api_key, rpc_url=cfg.rpc_url)
    try:
        for row in candidates:
            try:
                await _repair_row(
                    row, helius=helius, store=store, cfg=cfg, args=args, stats=stats
                )
            except Exception as exc:  # pragma: no cover - defensive
                stats.errors += 1
                logger.error("repair %s… failed: %s", (row.get("sig") or "?")[:8], exc, exc_info=True)
    finally:
        await helius.close()
        store.close()

    logger.info(
        "Done. matched=%d  %s=%d  skipped=%d  tx-missing=%d  not-a-buy=%d  errors=%d",
        stats.scanned,
        "repaired" if args.apply else "would-repair",
        stats.repaired if args.apply else stats.scanned,
        stats.skipped, stats.missing, stats.unparsed, stats.errors,
    )
    if not args.apply and stats.scanned:
        logger.info("Re-run with --apply to write these %d correction(s).", stats.scanned)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="HandsOff (再买剁手) historical buy-data repair (re-parse + rewrite).",
    )
    parser.add_argument(
        "-c", "--config", default=DEFAULT_CONFIG,
        help=f"path to config.ini (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="actually write corrections (default: dry-run, show only)",
    )
    parser.add_argument("--sig", default="", help="repair only this transaction signature")
    parser.add_argument("--ca", default="", help="repair only buys of this mint (CA)")
    parser.add_argument(
        "--max-sol", type=float, default=0.05,
        help="scan rows with sol_amount <= this (default 0.05; ignored with --sig/--ca)",
    )
    parser.add_argument(
        "--min-ratio", type=float, default=1.1,
        help="rewrite only when re-parsed sol >= old sol × this (default 1.1)",
    )
    parser.add_argument(
        "--limit", type=int, default=1000,
        help="max rows to inspect (default 1000)",
    )
    args = parser.parse_args()
    if args.max_sol <= 0:
        args.max_sol = 0.05
    if args.min_ratio < 1.0:
        args.min_ratio = 1.0
    if args.limit < 1:
        # 0/negative would otherwise be read as "unset" by the store and fetch the max.
        args.limit = 1

    cfg = Config(args.config)
    _setup_logging(cfg.log_level)
    if not cfg.api_key:
        raise SystemExit(
            "No Helius api_key in [HELIUS] — repair needs it to re-fetch transactions. "
            "Set api_key in config.ini."
        )

    try:
        rc = asyncio.run(_amain(cfg, args))
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)


if __name__ == "__main__":
    main()
