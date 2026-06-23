"""Robust SQLite store for HandsOff (再买剁手).

Persists three things permanently:

* ``signals``      — one row per parsed wallet BUY (the cards the page renders),
  deduplicated by transaction signature.
* ``tokens``       — a per-mint metadata + live-price cache (joined into the cards
  at read time so a metadata/price refresh is O(1) per token, not per buy).
* ``voice_alerts`` — the soft/hard spoken-reminder play ledger, keyed by local-day
  + tier, which makes the per-day caps atomic and idempotent across reloads, tabs
  and process restarts.

Plus a small ``meta`` key/value table the poller uses to persist its per-wallet
signature cursors so a restart neither replays history nor misses buys.

Concurrency model
-----------------
A SINGLE sqlite3 connection guarded by ONE re-entrant lock (``self._lock``). Every
read-decide-write — especially :meth:`claim_voice_alert` — runs inside the lock, so
concurrent browser tabs (served via ``asyncio.to_thread`` worker threads) can never
over-count or double-speak. ``check_same_thread=False`` is required because those
worker threads differ from the loop thread; the lock makes that safe.

Every public method is defensive: a disabled / unopenable database, a bad numeric,
or a SQLite error degrades to a safe default (empty list, zero, "not allowed")
rather than raising into the caller's hot path.
"""

from __future__ import annotations

import logging
import math
import os
import sqlite3
import threading
import time
from typing import Any

logger = logging.getLogger("HandsOffStore")

# Default sort + the allowed sort keys (mirrors the frontend's <select>). Unknown
# values fall back to the default.
_DEFAULT_SORT = "time-newest"
_SORT_SQL = {
    "time-newest": "s.created_at DESC, s.id DESC",
    "time-oldest": "s.created_at ASC, s.id ASC",
    "amount-desc": "(s.usd_amount IS NULL) ASC, s.usd_amount DESC, s.created_at DESC",
    "amount-asc": "(s.usd_amount IS NULL) ASC, s.usd_amount ASC, s.created_at DESC",
    "mcap-desc": "(COALESCE(t.current_market_cap, s.market_cap) IS NULL) ASC, "
                 "COALESCE(t.current_market_cap, s.market_cap) DESC, s.created_at DESC",
    "mcap-asc": "(COALESCE(t.current_market_cap, s.market_cap) IS NULL) ASC, "
                "COALESCE(t.current_market_cap, s.market_cap) ASC, s.created_at DESC",
}
_MAX_LIMIT = 1000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    ca                  TEXT PRIMARY KEY,
    name                TEXT,
    symbol              TEXT,
    icon                TEXT,
    twitter             TEXT,
    website             TEXT,
    platform            TEXT,
    decimals            INTEGER,
    supply              REAL,            -- raw total supply (NOT decimal-adjusted)
    current_market_cap  REAL,
    current_price       REAL,
    meta_updated_at     INTEGER,
    price_updated_at    INTEGER,
    created_at          INTEGER
);

CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sig             TEXT UNIQUE,         -- transaction signature (dedup key)
    ca              TEXT NOT NULL,
    seq             INTEGER NOT NULL DEFAULT 1,   -- advisory; recomputed at read time
    action          TEXT DEFAULT 'buy',
    wallet          TEXT,
    wallet_label    TEXT,
    name            TEXT,
    symbol          TEXT,
    platform        TEXT,
    sol_amount      REAL,
    fee_sol         REAL,
    usd_amount      REAL,
    token_amount    REAL,
    price           REAL,
    holds_amount    REAL,
    holds_pct       REAL,
    upnl            REAL,
    market_cap      REAL,               -- market cap AT buy time
    seen            TEXT,
    tx              TEXT,
    created_at      INTEGER NOT NULL,
    created_at_iso  TEXT,
    raw_text        TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_ca ON signals(ca);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);
CREATE INDEX IF NOT EXISTS idx_signals_wallet ON signals(wallet);
CREATE INDEX IF NOT EXISTS idx_signals_name ON signals(name);

CREATE TABLE IF NOT EXISTS voice_alerts (
    day_key     TEXT NOT NULL,          -- viewer's LOCAL date 'YYYY-MM-DD'
    tier        TEXT NOT NULL,          -- 'soft' | 'hard'
    played      INTEGER NOT NULL DEFAULT 0,
    last_count  INTEGER NOT NULL DEFAULT 0,
    updated_at  INTEGER,
    PRIMARY KEY (day_key, tier)
);

CREATE TABLE IF NOT EXISTS meta (
    key     TEXT PRIMARY KEY,
    value   TEXT
);
"""


# --------------------------------------------------------------------------- #
# Small, total coercion helpers — never raise, reject NaN / inf.
# --------------------------------------------------------------------------- #
def _to_float(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _pos_float(value: Any) -> float | None:
    """Keep only strictly-positive floats (0 / negative / bad -> None = unknown)."""
    f = _to_float(value)
    if f is None or f <= 0:
        return None
    return f


def _to_int(value: Any) -> int | None:
    f = _to_float(value)
    if f is None:
        return None
    try:
        return int(f)
    except (TypeError, ValueError, OverflowError):
        return None


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    try:
        s = str(value).strip()
    except Exception:
        return None
    return s or None


def _clean_name(value: Any) -> str | None:
    """Like _clean_str but also maps the literal 'Unknown' sentinel to None, so a
    placeholder name never overwrites a good one through COALESCE."""
    s = _clean_str(value)
    if s is None:
        return None
    return None if s.lower() == "unknown" else s


def _norm_tier(value: Any) -> str | None:
    s = _clean_str(value)
    if s is None:
        return None
    s = s.lower()
    return s if s in ("soft", "hard") else None


# Sentinel for update_buy: distinguishes "leave this column untouched" from an
# explicit "set it to NULL" (which a plain None would be ambiguous about).
_UNSET: Any = object()


class SignalStore:
    """Thread-safe SQLite store. ``self.ok`` is False when persistence is disabled
    (empty path) or the database could not be opened — callers then degrade to safe
    no-ops."""

    def __init__(self, db_path: str | None) -> None:
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self.ok = False
        self.db_path = (db_path or "").strip()

        if not self.db_path:
            logger.info("Signal store disabled (no db_path configured)")
            return
        try:
            parent = os.path.dirname(os.path.abspath(self.db_path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            conn = sqlite3.connect(
                self.db_path, check_same_thread=False, timeout=30.0
            )
            conn.row_factory = sqlite3.Row
            # WAL + a generous busy timeout: concurrent readers (browser tabs) never
            # block the poller's writer, and a momentary lock retries instead of
            # erroring.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(_SCHEMA)
            conn.commit()
            self._conn = conn
            self.ok = True
        except (sqlite3.Error, OSError) as exc:
            logger.error("Signal store open failed (%s): %s", self.db_path, exc)
            self._conn = None
            self.ok = False

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.commit()
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None
            self.ok = False

    # ------------------------------------------------------------------ #
    # meta key/value (poller cursors)
    # ------------------------------------------------------------------ #
    def get_meta(self, key: str) -> str | None:
        k = _clean_str(key)
        if not k or not self.ok or self._conn is None:
            return None
        with self._lock:
            try:
                cur = self._conn.execute(
                    "SELECT value FROM meta WHERE key = ?", (k,)
                )
                row = cur.fetchone()
                return row["value"] if row else None
            except sqlite3.DatabaseError as exc:
                logger.debug("get_meta(%s) failed: %s", k, exc)
                return None

    def set_meta(self, key: str, value: str | None) -> None:
        k = _clean_str(key)
        if not k or not self.ok or self._conn is None:
            return
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (k, None if value is None else str(value)),
                )
                self._conn.commit()
            except sqlite3.DatabaseError as exc:
                logger.debug("set_meta(%s) failed: %s", k, exc)

    # ------------------------------------------------------------------ #
    # token metadata cache
    # ------------------------------------------------------------------ #
    def upsert_token(
        self,
        ca: str,
        *,
        name: Any = None,
        symbol: Any = None,
        icon: Any = None,
        twitter: Any = None,
        website: Any = None,
        platform: Any = None,
        decimals: Any = None,
        supply: Any = None,
        current_market_cap: Any = None,
        current_price: Any = None,
    ) -> None:
        """Insert or refresh a token's cached metadata. COALESCE-fills the slow-
        changing fields (so a later partial fetch never blanks a good value) and
        always overwrites the live ``current_market_cap`` / ``current_price`` when a
        fresh positive value is supplied."""
        cc = _clean_str(ca)
        if not cc or not self.ok or self._conn is None:
            return
        now = int(time.time())
        nm = _clean_name(name)
        sym = _clean_name(symbol)
        ico = _clean_str(icon)
        tw = _clean_str(twitter)
        web = _clean_str(website)
        plat = _clean_str(platform)
        dec = _to_int(decimals)
        sup = _pos_float(supply)
        cmc = _pos_float(current_market_cap)
        cpx = _pos_float(current_price)
        meta_ts = now if any(v is not None for v in (nm, sym, ico, tw, web, plat, dec, sup)) else None
        price_ts = now if (cmc is not None or cpx is not None) else None
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO tokens (ca, name, symbol, icon, twitter, website,
                        platform, decimals, supply, current_market_cap,
                        current_price, meta_updated_at, price_updated_at, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(ca) DO UPDATE SET
                        name = COALESCE(excluded.name, tokens.name),
                        symbol = COALESCE(excluded.symbol, tokens.symbol),
                        icon = COALESCE(excluded.icon, tokens.icon),
                        twitter = COALESCE(excluded.twitter, tokens.twitter),
                        website = COALESCE(excluded.website, tokens.website),
                        platform = COALESCE(excluded.platform, tokens.platform),
                        decimals = COALESCE(excluded.decimals, tokens.decimals),
                        supply = COALESCE(excluded.supply, tokens.supply),
                        current_market_cap = COALESCE(excluded.current_market_cap, tokens.current_market_cap),
                        current_price = COALESCE(excluded.current_price, tokens.current_price),
                        meta_updated_at = COALESCE(excluded.meta_updated_at, tokens.meta_updated_at),
                        price_updated_at = COALESCE(excluded.price_updated_at, tokens.price_updated_at)
                    """,
                    (cc, nm, sym, ico, tw, web, plat, dec, sup, cmc, cpx,
                     meta_ts, price_ts, now),
                )
                self._conn.commit()
            except sqlite3.DatabaseError as exc:
                logger.debug("upsert_token(%s) failed: %s", cc, exc)

    def get_token(self, ca: str) -> dict[str, Any] | None:
        cc = _clean_str(ca)
        if not cc or not self.ok or self._conn is None:
            return None
        with self._lock:
            try:
                cur = self._conn.execute("SELECT * FROM tokens WHERE ca = ?", (cc,))
                row = cur.fetchone()
                return dict(row) if row else None
            except sqlite3.DatabaseError as exc:
                logger.debug("get_token(%s) failed: %s", cc, exc)
                return None

    # ------------------------------------------------------------------ #
    # buy recording
    # ------------------------------------------------------------------ #
    def record_buy(
        self,
        *,
        sig: str,
        ca: str,
        action: Any = "buy",
        wallet: Any = None,
        wallet_label: Any = None,
        name: Any = None,
        symbol: Any = None,
        platform: Any = None,
        sol_amount: Any = None,
        fee_sol: Any = None,
        usd_amount: Any = None,
        token_amount: Any = None,
        price: Any = None,
        holds_amount: Any = None,
        holds_pct: Any = None,
        upnl: Any = None,
        market_cap: Any = None,
        seen: Any = None,
        tx: Any = None,
        created_at: Any = None,
        created_at_iso: Any = None,
        raw_text: Any = None,
    ) -> dict[str, int]:
        """Insert a buy (idempotent on ``sig``). Returns ``{inserted, seq, id}``.

        A blank signature or CA is rejected (returns inserted=False). On a duplicate
        signature the existing row is left untouched and its id/seq returned. ``seq``
        is computed at insert time as the buy's chronological rank within its CA group
        (it is also recomputed authoritatively at read time)."""
        out = {"inserted": False, "seq": 0, "id": 0}
        sg = _clean_str(sig)
        cc = _clean_str(ca)
        if not sg or not cc or not self.ok or self._conn is None:
            return out

        ts = _to_float(created_at)
        ts_i = int(ts) if ts is not None else int(time.time())
        act = _clean_str(action) or "buy"

        with self._lock:
            try:
                cur = self._conn.execute(
                    "SELECT id, seq FROM signals WHERE sig = ?", (sg,)
                )
                existing = cur.fetchone()
                if existing is not None:
                    out["id"] = _to_int(existing["id"]) or 0
                    out["seq"] = _to_int(existing["seq"]) or 0
                    return out

                # seq = 1 + count of same-CA buys strictly earlier in time. This is an
                # advisory value persisted on the row; read-time ROW_NUMBER finalizes the
                # authoritative per-CA sequence regardless of insert order.
                prior = self._conn.execute(
                    "SELECT COUNT(*) AS c FROM signals WHERE ca = ? AND created_at < ?",
                    (cc, ts_i),
                ).fetchone()
                seq = 1 + (_to_int(prior["c"]) or 0)

                cur = self._conn.execute(
                    """
                    INSERT INTO signals (sig, ca, seq, action, wallet, wallet_label,
                        name, symbol, platform, sol_amount, fee_sol, usd_amount,
                        token_amount, price, holds_amount, holds_pct, upnl,
                        market_cap, seen, tx, created_at, created_at_iso, raw_text)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        sg, cc, seq, act,
                        _clean_str(wallet), _clean_str(wallet_label),
                        _clean_name(name), _clean_name(symbol), _clean_str(platform),
                        _pos_float(sol_amount), _pos_float(fee_sol), _pos_float(usd_amount),
                        _pos_float(token_amount), _pos_float(price),
                        _pos_float(holds_amount), _pos_float(holds_pct), _to_float(upnl),
                        _pos_float(market_cap), _clean_str(seen), _clean_str(tx),
                        ts_i, _clean_str(created_at_iso), _clean_str(raw_text),
                    ),
                )
                self._conn.commit()
                out["inserted"] = True
                out["seq"] = seq
                out["id"] = int(cur.lastrowid or 0)
                return out
            except sqlite3.DatabaseError as exc:
                logger.warning("record_buy(sig=%s) failed: %s", sg, exc)
                return out

    # ------------------------------------------------------------------ #
    # buy repair (re-parse historical rows the old parser mis-valued)
    # ------------------------------------------------------------------ #
    def get_signal(self, sig: str) -> dict[str, Any] | None:
        """Return ONE raw signal row (no token join) by transaction signature, or
        None. Used by the repair tool to read a row's stored amounts before rewriting
        them. Defensive: a bad signature or DB error degrades to None."""
        sg = _clean_str(sig)
        if not sg or not self.ok or self._conn is None:
            return None
        with self._lock:
            try:
                cur = self._conn.execute("SELECT * FROM signals WHERE sig = ?", (sg,))
                row = cur.fetchone()
                return dict(row) if row else None
            except sqlite3.DatabaseError as exc:
                logger.debug("get_signal(%s) failed: %s", sg, exc)
                return None

    def under_attributed_signals(
        self, *, max_sol: float = 0.05, limit: int = _MAX_LIMIT
    ) -> list[dict[str, Any]]:
        """Raw signal rows whose stored SOL leg is dust-sized (``sol_amount <= max_sol``)
        — the repair tool's candidate set for re-fetching + re-parsing. A bot-/router-
        routed buy whose SOL leg the OLD parser collapsed to the wallet's fee-sized tip
        lands here; a genuinely tiny buy lands here too, so the repair tool only rewrites
        a row when a fresh parse materially raises the amount. Newest-first; never raises.
        """
        if not self.ok or self._conn is None:
            return []
        ms = _to_float(max_sol)
        if ms is None or ms <= 0:
            ms = 0.05
        lim = max(1, min(_MAX_LIMIT, _to_int(limit) or _MAX_LIMIT))
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT * FROM signals "
                    "WHERE sol_amount IS NOT NULL AND sol_amount <= ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (ms, lim),
                ).fetchall()
                return [dict(r) for r in rows]
            except sqlite3.DatabaseError as exc:
                logger.debug("under_attributed_signals failed: %s", exc)
                return []

    def signals_for_ca(self, ca: str, *, limit: int = _MAX_LIMIT) -> list[dict[str, Any]]:
        """Raw signal rows for one mint (newest-first). Repair-tool helper for the
        ``--ca`` target. Defensive: bad CA / DB error degrades to an empty list."""
        cc = _clean_str(ca)
        if not cc or not self.ok or self._conn is None:
            return []
        lim = max(1, min(_MAX_LIMIT, _to_int(limit) or _MAX_LIMIT))
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT * FROM signals WHERE ca = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (cc, lim),
                ).fetchall()
                return [dict(r) for r in rows]
            except sqlite3.DatabaseError as exc:
                logger.debug("signals_for_ca(%s) failed: %s", cc, exc)
                return []

    def update_buy(
        self,
        sig: str,
        *,
        sol_amount: Any = _UNSET,
        fee_sol: Any = _UNSET,
        usd_amount: Any = _UNSET,
        token_amount: Any = _UNSET,
        price: Any = _UNSET,
        holds_amount: Any = _UNSET,
        holds_pct: Any = _UNSET,
        market_cap: Any = _UNSET,
        upnl: Any = _UNSET,
    ) -> bool:
        """Rewrite the money / position fields of an existing buy, keyed by ``sig``.

        ONLY the fields explicitly passed are touched (others keep the sentinel and are
        left as-is). Each value is coerced through the SAME helper the insert path uses
        — ``_pos_float`` for the strictly-positive money/position columns (so 0 / negative
        / NaN / inf become NULL identically to a fresh insert) and ``_to_float`` for the
        signed ``upnl`` — so a repaired row is byte-for-byte what ``record_buy`` would
        have written. Returns True when a row was actually updated; never raises."""
        sg = _clean_str(sig)
        if not sg or not self.ok or self._conn is None:
            return False
        # (column, supplied-value, coercion) — only non-sentinel entries are applied.
        spec: list[tuple[str, Any, Any]] = [
            ("sol_amount", sol_amount, _pos_float),
            ("fee_sol", fee_sol, _pos_float),
            ("usd_amount", usd_amount, _pos_float),
            ("token_amount", token_amount, _pos_float),
            ("price", price, _pos_float),
            ("holds_amount", holds_amount, _pos_float),
            ("holds_pct", holds_pct, _pos_float),
            ("market_cap", market_cap, _pos_float),
            ("upnl", upnl, _to_float),
        ]
        sets: list[str] = []
        params: list[Any] = []
        for col, val, coerce in spec:
            if val is _UNSET:
                continue
            sets.append(f"{col} = ?")
            params.append(coerce(val))
        if not sets:
            return False
        with self._lock:
            try:
                cur = self._conn.execute(
                    f"UPDATE signals SET {', '.join(sets)} WHERE sig = ?",
                    (*params, sg),
                )
                self._conn.commit()
                return (cur.rowcount or 0) > 0
            except sqlite3.DatabaseError as exc:
                logger.warning("update_buy(sig=%s) failed: %s", sg, exc)
                return False

    # ------------------------------------------------------------------ #
    # queries
    # ------------------------------------------------------------------ #
    @staticmethod
    def _row_to_signal(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        # Prefer the freshest token-cache metadata (joined as t_*), fall back to the
        # value captured on the buy row.
        def pick(joined: str, base: str) -> Any:
            v = d.get(joined)
            return v if v is not None else d.get(base)

        return {
            "id": _to_int(d.get("id")),
            "sig": d.get("sig"),
            "ca": d.get("ca"),
            "seq": _to_int(d.get("rseq")) or _to_int(d.get("seq")) or 1,
            "action": d.get("action") or "buy",
            "wallet": d.get("wallet"),
            "wallet_label": d.get("wallet_label"),
            "name": pick("t_name", "name"),
            "symbol": pick("t_symbol", "symbol"),
            "platform": pick("t_platform", "platform"),
            "icon": d.get("t_icon"),
            "twitter": d.get("t_twitter"),
            "website": d.get("t_website"),
            "sol_amount": _to_float(d.get("sol_amount")),
            "fee_sol": _to_float(d.get("fee_sol")),
            "usd_amount": _to_float(d.get("usd_amount")),
            "token_amount": _to_float(d.get("token_amount")),
            "price": _to_float(d.get("price")),
            "holds_amount": _to_float(d.get("holds_amount")),
            "holds_pct": _to_float(d.get("holds_pct")),
            "upnl": _to_float(d.get("upnl")),
            "market_cap": _to_float(d.get("market_cap")),
            "current_market_cap": _to_float(d.get("t_current_market_cap")),
            "current_price": _to_float(d.get("t_current_price")),
            "seen": d.get("seen"),
            "tx": d.get("tx"),
            "created_at": _to_int(d.get("created_at")),
            "created_at_iso": d.get("created_at_iso"),
        }

    def query(
        self,
        *,
        q: str = "",
        wallet: str | None = None,
        start_epoch: int | None = None,
        end_epoch: int | None = None,
        sort: str = _DEFAULT_SORT,
        limit: int = 60,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Return ``{signals, total, max_id}`` for the buy cards matching the filters.

        ``max_id`` is the largest signal id in the WHOLE table (filter-independent) —
        the frontend's "is anything new?" chime cursor.
        """
        empty = {"signals": [], "total": 0, "max_id": 0}
        if not self.ok or self._conn is None:
            return empty

        where: list[str] = []
        params: list[Any] = []
        qc = _clean_str(q)
        if qc:
            like = f"%{qc}%"
            where.append(
                "(COALESCE(t.name, s.name) LIKE ? COLLATE NOCASE "
                "OR COALESCE(t.symbol, s.symbol) LIKE ? COLLATE NOCASE "
                "OR s.ca LIKE ? OR s.wallet LIKE ? "
                "OR s.wallet_label LIKE ? COLLATE NOCASE)"
            )
            params.extend([like, like, like, like, like])
        wc = _clean_str(wallet)
        if wc:
            where.append("s.wallet = ?")
            params.append(wc)
        se = _to_int(start_epoch)
        if se is not None:
            where.append("s.created_at >= ?")
            params.append(se)
        ee = _to_int(end_epoch)
        if ee is not None:
            where.append("s.created_at < ?")
            params.append(ee)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""

        order_sql = _SORT_SQL.get(sort, _SORT_SQL[_DEFAULT_SORT])
        lim = _to_int(limit) or 60
        lim = max(1, min(_MAX_LIMIT, lim))
        off = _to_int(offset) or 0
        off = max(0, off)

        with self._lock:
            try:
                total_row = self._conn.execute(
                    "SELECT COUNT(*) AS c FROM signals s "
                    "LEFT JOIN tokens t ON t.ca = s.ca" + where_sql,
                    params,
                ).fetchone()
                total = _to_int(total_row["c"]) or 0

                max_row = self._conn.execute(
                    "SELECT COALESCE(MAX(id), 0) AS m FROM signals"
                ).fetchone()
                max_id = _to_int(max_row["m"]) or 0

                # Recompute the per-CA buy sequence at read time so it is always
                # correct regardless of insert order.
                rows = self._conn.execute(
                    f"""
                    WITH ranked AS (
                        SELECT s.*,
                            ROW_NUMBER() OVER (
                                PARTITION BY s.ca
                                ORDER BY s.created_at, s.id
                            ) AS rseq
                        FROM signals s
                    )
                    SELECT s.*, s.rseq AS rseq,
                        t.name AS t_name, t.symbol AS t_symbol, t.icon AS t_icon,
                        t.twitter AS t_twitter, t.website AS t_website,
                        t.platform AS t_platform,
                        t.current_market_cap AS t_current_market_cap,
                        t.current_price AS t_current_price
                    FROM ranked s
                    LEFT JOIN tokens t ON t.ca = s.ca
                    {where_sql}
                    ORDER BY {order_sql}
                    LIMIT ? OFFSET ?
                    """,
                    [*params, lim, off],
                ).fetchall()
                signals = [self._row_to_signal(r) for r in rows]
                return {"signals": signals, "total": total, "max_id": max_id}
            except sqlite3.DatabaseError as exc:
                logger.error("query failed: %s", exc, exc_info=True)
                return empty

    def recent_mints(self, limit: int = 30, since_epoch: int | None = None) -> list[str]:
        """Distinct CAs from the most recent buys — the price refresher's work-list."""
        if not self.ok or self._conn is None:
            return []
        lim = max(1, min(_MAX_LIMIT, _to_int(limit) or 30))
        where = "WHERE ca IS NOT NULL AND ca != ''"
        params: list[Any] = []
        se = _to_int(since_epoch)
        if se is not None:
            where += " AND created_at >= ?"
            params.append(se)
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT ca, MAX(created_at) AS last FROM signals " + where +
                    " GROUP BY ca ORDER BY last DESC LIMIT ?",
                    [*params, lim],
                ).fetchall()
                return [r["ca"] for r in rows if r["ca"]]
            except sqlite3.DatabaseError:
                return []

    def latest_id(self) -> int:
        if not self.ok or self._conn is None:
            return 0
        with self._lock:
            try:
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(id), 0) AS m FROM signals"
                ).fetchone()
                return _to_int(row["m"]) or 0
            except sqlite3.DatabaseError:
                return 0

    def stats(
        self, start_epoch: int | None = None, end_epoch: int | None = None
    ) -> dict[str, Any]:
        """Return ``{buys, tokens, wallets, volume}`` over an optional half-open
        window. Buys are the total cards; that count IS the combined self-discipline
        buy count for the window."""
        out = {"buys": 0, "tokens": 0, "wallets": 0, "volume": 0.0}
        if not self.ok or self._conn is None:
            return out
        where: list[str] = []
        params: list[Any] = []
        se = _to_int(start_epoch)
        if se is not None:
            where.append("created_at >= ?")
            params.append(se)
        ee = _to_int(end_epoch)
        if ee is not None:
            where.append("created_at < ?")
            params.append(ee)
        wsql = (" WHERE " + " AND ".join(where)) if where else ""
        ca_w = wsql + (" AND " if wsql else " WHERE ") + "ca IS NOT NULL AND ca != ''"
        wallet_w = wsql + (" AND " if wsql else " WHERE ") + "wallet IS NOT NULL AND wallet != ''"
        with self._lock:
            try:
                out["buys"] = _to_int(
                    self._conn.execute(
                        "SELECT COUNT(*) c FROM signals" + wsql, params
                    ).fetchone()["c"]
                ) or 0
                out["tokens"] = _to_int(
                    self._conn.execute(
                        "SELECT COUNT(DISTINCT ca) c FROM signals" + ca_w, params
                    ).fetchone()["c"]
                ) or 0
                out["wallets"] = _to_int(
                    self._conn.execute(
                        "SELECT COUNT(DISTINCT wallet) c FROM signals" + wallet_w, params
                    ).fetchone()["c"]
                ) or 0
                out["volume"] = _to_float(
                    self._conn.execute(
                        "SELECT COALESCE(SUM(usd_amount), 0) v FROM signals" + wsql, params
                    ).fetchone()["v"]
                ) or 0.0
            except sqlite3.DatabaseError as exc:
                logger.debug("stats failed: %s", exc)
        return out

    def buys_count(
        self,
        start_epoch: int | None = None,
        end_epoch: int | None = None,
        *,
        count_distinct_ca: bool = False,
    ) -> int:
        """Total buy count over a window — the authoritative self-discipline count fed
        into :meth:`claim_voice_alert` (combined across ALL monitored wallets).

        ``count_distinct_ca`` mirrors the ``[DISCIPLINE] enable_duplicate_buy`` setting:

        * ``False`` (default, ``enable_duplicate_buy = true``) — every buy counts, so N
          buys of one token contribute N. This is the original behavior.
        * ``True`` (``enable_duplicate_buy = false``) — each token counts once no matter
          how many times it was bought today, so re-entering a position you already hold
          doesn't push you toward the soft/hard limit.
        """
        if not self.ok or self._conn is None:
            return 0
        where: list[str] = []
        params: list[Any] = []
        se = _to_int(start_epoch)
        if se is not None:
            where.append("created_at >= ?")
            params.append(se)
        ee = _to_int(end_epoch)
        if ee is not None:
            where.append("created_at < ?")
            params.append(ee)
        if count_distinct_ca:
            # ca is non-empty for every recorded buy (record_buy rejects blanks), but
            # guard anyway so a legacy / hand-edited NULL row can't skew the count.
            where.append("ca IS NOT NULL AND ca != ''")
            select = "COUNT(DISTINCT ca)"
        else:
            select = "COUNT(*)"
        wsql = (" WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            try:
                row = self._conn.execute(
                    f"SELECT {select} c FROM signals" + wsql, params
                ).fetchone()
                return _to_int(row["c"]) or 0
            except sqlite3.DatabaseError:
                return 0

    # ------------------------------------------------------------------ #
    # voice-alert ledger (soft/hard spoken-reminder caps)
    # ------------------------------------------------------------------ #
    def voice_alerts(self, day_key: str) -> dict[str, int]:
        """Return ``{soft, hard}`` = plays already used for that local day (0 when no
        row). Used by the page for a fast budget pre-check; the claim is authoritative.
        """
        out = {"soft": 0, "hard": 0}
        dk = _clean_str(day_key)
        if not dk or not self.ok or self._conn is None:
            return out
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT tier, played FROM voice_alerts WHERE day_key = ?", (dk,)
                ).fetchall()
                for r in rows:
                    tier = _norm_tier(r["tier"])
                    if tier in out:
                        out[tier] = max(0, _to_int(r["played"]) or 0)
            except sqlite3.DatabaseError as exc:
                logger.debug("voice_alerts(%s) failed: %s", dk, exc)
        return out

    def claim_voice_alert(
        self, day_key: str, tier: str, count: int, threshold: int, max_alerts: int
    ) -> dict[str, Any]:
        """Atomically claim ONE play of the soft/hard reminder.

        Grants a play (``allowed=True``, ``played += 1``, persists ``last_count =
        count``) ONLY when ALL hold:
          * the tier is enabled  (``threshold > 0``),
          * the count has reached it  (``count >= threshold``),
          * the count is a NEW higher buy than the last one already spoken for this
            tier  (``count > last_count``)  — so each buy speaks at most once and the
            same count never re-speaks across reloads/tabs/restarts, and
          * the per-day budget is not spent  (``max_alerts <= 0`` = unlimited, else
            ``played < max_alerts``).

        Otherwise nothing is written and ``allowed=False``. Always returns
        ``{allowed, played, max, count}``; never raises.
        """
        mx = _to_int(max_alerts) or 0
        if mx < 0:
            mx = 0
        cnt = _to_int(count) or 0
        if cnt < 0:
            cnt = 0
        thr = _to_int(threshold) or 0
        out: dict[str, Any] = {"allowed": False, "played": 0, "max": mx, "count": cnt}

        dk = _clean_str(day_key)
        norm = _norm_tier(tier)
        if not dk or norm is None or not self.ok or self._conn is None:
            return out
        if thr <= 0:  # tier disabled — never speak
            return out

        now = int(time.time())
        with self._lock:
            try:
                row = self._conn.execute(
                    "SELECT played, last_count FROM voice_alerts "
                    "WHERE day_key = ? AND tier = ?",
                    (dk, norm),
                ).fetchone()
                played = (_to_int(row["played"]) or 0) if row else 0
                if played < 0:
                    played = 0
                last_count = (_to_int(row["last_count"]) or 0) if row else 0
                if last_count < 0:
                    last_count = 0
                out["played"] = played

                reached = cnt >= thr
                fresh = cnt > last_count
                budget_left = (mx <= 0) or (played < mx)
                if reached and fresh and budget_left:
                    new_played = played + 1
                    self._conn.execute(
                        "INSERT INTO voice_alerts "
                        "(day_key, tier, played, last_count, updated_at) "
                        "VALUES (?,?,?,?,?) "
                        "ON CONFLICT(day_key, tier) DO UPDATE SET "
                        "played = excluded.played, "
                        "last_count = excluded.last_count, "
                        "updated_at = excluded.updated_at",
                        (dk, norm, new_played, cnt, now),
                    )
                    self._conn.commit()
                    out["allowed"] = True
                    out["played"] = new_played
                return out
            except sqlite3.DatabaseError as exc:
                logger.warning("claim_voice_alert(%s/%s) failed: %s", dk, norm, exc)
                return {"allowed": False, "played": 0, "max": mx, "count": cnt}
