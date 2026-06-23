"""aiohttp web server for HandsOff (再买剁手).

Runs INSIDE the poller's existing asyncio event loop (deliberately not
``web.run_app``) so it shares the loop + store. Serves the single-page UI and a
small, defensive JSON API:

    GET  /                     -> the discipline page (discipline.html)
    GET  /api/config           -> chime window + self-discipline panel descriptor
    GET  /api/signals          -> paged/filtered/sorted buy cards (+ chime + max_id)
    GET  /api/stats            -> today's totals + combined buy count + voice budget
    POST /api/voice/claim      -> atomic soft/hard spoken-reminder claim
    GET  /api/stream           -> Server-Sent-Events push ("a new buy arrived")
    GET  /api/health           -> liveness probe

No authentication: the server is meant to bind to localhost (127.0.0.1) for a single
operator. Bind to a non-loopback host only behind your own auth/proxy.

Every data endpoint returns a well-formed JSON envelope (``success`` plus a constant
key shape) and HTTP 200 even on bad input or a disabled store, so the frontend can
read keys blindly and simply retry. Only a genuine query crash uses 500.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import os
import socket
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

logger = logging.getLogger("HandsOffWeb")

_MAX_LIMIT = 1000
_SSE_HEARTBEAT_S = 15.0
_TZ_MIN, _TZ_MAX = -720, 840  # clamp for tz_offset (minutes east of UTC)


# --------------------------------------------------------------------------- #
# link builders (pure string interpolation — no network)
# --------------------------------------------------------------------------- #
def build_links(ca: str | None) -> dict[str, str]:
    """CA-derived trade/explorer links. Empty dict when no CA."""
    c = (ca or "").strip()
    if not c:
        return {}
    return {
        "axiom": f"https://axiom.trade/t/{c}",
        "gmgn": f"https://gmgn.ai/sol/token/{c}",
        "solscan": f"https://solscan.io/token/{c}",
        "dexscreener": f"https://dexscreener.com/solana/{c}",
        "photon": f"https://photon-sol.tinyastro.io/en/lp/{c}",
    }


def _enrich(signal: dict[str, Any]) -> dict[str, Any]:
    """Attach CA-derived links + wallet/tx explorer URLs to a raw buy card."""
    ca = (signal.get("ca") or "").strip()
    signal["links"] = build_links(ca)
    wallet = (signal.get("wallet") or "").strip()
    signal["wallet_url"] = (
        f"https://gmgn.ai/sol/address/{wallet}" if wallet else ""
    )
    tx = (signal.get("tx") or signal.get("sig") or "").strip()
    signal["tx_url"] = f"https://solscan.io/tx/{tx}" if tx else ""
    return signal


# --------------------------------------------------------------------------- #
# day / time helpers (tz_offset = minutes EAST of UTC; Beijing = +480)
# --------------------------------------------------------------------------- #
def _clamp_tz(off: Any) -> int:
    try:
        o = int(off)
    except (TypeError, ValueError):
        return 0
    return max(_TZ_MIN, min(_TZ_MAX, o))


def _day_bounds(date_str: str | None, tz_offset_min: int) -> tuple[int, int] | None:
    """Half-open [start, end) UTC-epoch window for the local day ``date_str``."""
    if not date_str:
        return None
    try:
        d = datetime.strptime(date_str.strip(), "%Y-%m-%d")
    except (ValueError, AttributeError):
        return None
    utc_midnight = calendar.timegm((d.year, d.month, d.day, 0, 0, 0, 0, 0, 0))
    start = utc_midnight - tz_offset_min * 60
    return start, start + 86400


def _today_key(tz_offset_min: int) -> str:
    off = _clamp_tz(tz_offset_min)
    local = datetime.now(timezone.utc).timestamp() + off * 60
    return datetime.fromtimestamp(local, timezone.utc).strftime("%Y-%m-%d")


def _int_param(request: web.Request, name: str, default: int) -> int:
    raw = request.query.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _str_param(request: web.Request, name: str, maxlen: int = 120) -> str:
    raw = request.query.get(name, "")
    s = (raw or "").strip()
    return s[:maxlen]


def _cap(value: Any) -> int:
    """Coerce a config number to a non-negative int (0 when missing / non-positive)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _short_addr(addr: str) -> str:
    a = (addr or "").strip()
    return (a[:4] + "…" + a[-4:]) if len(a) > 10 else a


# --------------------------------------------------------------------------- #
# config payloads
# --------------------------------------------------------------------------- #
def _chime_payload(cfg: dict[str, Any]) -> dict[str, Any]:
    start = _cap(cfg.get("chime_start_seq", 1)) or 1
    mx = cfg.get("chime_max", 5)
    try:
        mx_i = int(mx)
    except (TypeError, ValueError):
        mx_i = 5
    if mx_i < 0:
        mx_i = 5
    return {
        "enabled": bool(cfg.get("chime_enabled", True)),
        "start_seq": start,
        "max": mx_i,
    }


def _myself_payload(cfg: dict[str, Any]) -> dict[str, Any] | None:
    addr = (cfg.get("myself_address") or "").strip()
    if not addr:
        return None
    label = (cfg.get("myself_label") or "").strip() or _short_addr(addr)
    url = (cfg.get("myself_url") or "").strip() or (
        f"https://gmgn.ai/sol/address/{addr}"
    )
    return {
        "address": addr,
        "label": label,
        "url": url,
        "soft": _cap(cfg.get("soft_buy_amount")),
        "hard": _cap(cfg.get("hard_buy_amount")),
        "soft_max": _cap(cfg.get("soft_max_alerts")),
        "hard_max": _cap(cfg.get("hard_max_alerts")),
    }


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #
async def _serve_page(request: web.Request, filename: str) -> web.Response:
    static_dir = request.app["static_dir"]
    page = os.path.join(static_dir, "pages", filename)
    try:
        html = await asyncio.to_thread(_read_text, page)
    except OSError:
        return web.Response(status=500, text=f"{filename} not found")
    return web.Response(
        text=html,
        content_type="text/html",
        charset="utf-8",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


async def handle_index(request: web.Request) -> web.Response:
    """Serve the discipline page; redirect to /login when auth is on and the visitor
    isn't signed in (the data APIs are 401-gated regardless)."""
    if request.app.get("enable_auth"):
        guard = request.app.get("guard")
        if guard is None or guard(request) is None:
            raise web.HTTPFound("/login")
    return await _serve_page(request, "discipline.html")


async def handle_login(request: web.Request) -> web.Response:
    """Serve the wallet-login page. If auth is off, bounce straight to the app."""
    if not request.app.get("enable_auth"):
        raise web.HTTPFound("/")
    return await _serve_page(request, "login.html")


async def handle_health(request: web.Request) -> web.Response:
    store = request.app["store"]
    return web.json_response(
        {"success": True, "ok": bool(getattr(store, "ok", False)),
         "version": request.app.get("version", "1")}
    )


async def handle_api_config(request: web.Request) -> web.Response:
    cfg = request.app.get("config") or {}
    return web.json_response(
        {
            "success": True,
            "chime": _chime_payload(cfg),
            "myself": _myself_payload(cfg),
        }
    )


async def handle_api_signals(request: web.Request) -> web.Response:
    store = request.app["store"]
    cfg = request.app.get("config") or {}
    chime = _chime_payload(cfg)
    if not getattr(store, "ok", False):
        return web.json_response(
            {"success": False, "error": "store_unavailable",
             "signals": [], "total": 0, "max_id": 0, "chime": chime}
        )

    q = _str_param(request, "q", 80)
    wallet = _str_param(request, "wallet", 64) or None
    tz = _clamp_tz(_int_param(request, "tz_offset", 0))
    sort = _str_param(request, "sort", 32) or "time-newest"
    limit = max(1, min(_MAX_LIMIT, _int_param(request, "limit", 60)))
    offset = max(0, _int_param(request, "offset", 0))

    start_epoch = end_epoch = None
    date = _str_param(request, "date", 10)
    if date:
        bounds = _day_bounds(date, tz)
        if bounds is None:
            return web.json_response(
                {"success": False, "error": "bad_date",
                 "signals": [], "total": 0, "max_id": 0, "chime": chime}
            )
        start_epoch, end_epoch = bounds

    try:
        result = await asyncio.to_thread(
            store.query,
            q=q, wallet=wallet, start_epoch=start_epoch, end_epoch=end_epoch,
            sort=sort, limit=limit, offset=offset,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("api/signals query crashed: %s", exc, exc_info=True)
        return web.json_response(
            {"success": False, "error": "query_failed",
             "signals": [], "total": 0, "max_id": 0, "chime": chime},
            status=500,
        )

    signals = [_enrich(s) for s in result.get("signals", [])]
    return web.json_response(
        {
            "success": True,
            "signals": signals,
            "count": len(signals),
            "total": result.get("total", 0),
            "max_id": result.get("max_id", 0),
            "offset": offset,
            "limit": limit,
            "sort": sort,
            "chime": chime,
            "server_time": int(datetime.now(timezone.utc).timestamp()),
        }
    )


async def handle_api_stats(request: web.Request) -> web.Response:
    store = request.app["store"]
    cfg = request.app.get("config") or {}
    if not getattr(store, "ok", False):
        return web.json_response({"success": False, "stats": {}})

    tz = _clamp_tz(_int_param(request, "tz_offset", 0))
    date = _str_param(request, "date", 10)
    start_epoch = end_epoch = None
    if date:
        bounds = _day_bounds(date, tz)
        if bounds is None:
            return web.json_response(
                {"success": False, "error": "bad_date", "stats": {}}
            )
        start_epoch, end_epoch = bounds

    try:
        stats = await asyncio.to_thread(store.stats, start_epoch, end_epoch)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("api/stats failed: %s", exc)
        stats = {"buys": 0, "tokens": 0, "wallets": 0, "volume": 0.0}

    panel_on = bool((cfg.get("myself_address") or "").strip())
    myself_buys: int | None = None
    voice_alerts: dict[str, Any] | None = None
    if panel_on:
        # The combined buy count across all monitored wallets in this window IS the
        # self-discipline count. When enable_duplicate_buy is false, repeat buys of a CA
        # already bought in the window collapse to one (count distinct CAs), so this is
        # computed via the SAME buys_count the voice claim uses — they can't diverge.
        enable_dup = bool(cfg.get("enable_duplicate_buy", True))
        try:
            myself_buys = await asyncio.to_thread(
                store.buys_count, start_epoch, end_epoch,
                count_distinct_ca=not enable_dup,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("api/stats discipline count failed: %s", exc)
            myself_buys = int(stats.get("buys", 0) or 0)
        day_key = date or _today_key(tz)
        try:
            played = await asyncio.to_thread(store.voice_alerts, day_key)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("api/stats voice_alerts read failed: %s", exc)
            played = {"soft": 0, "hard": 0}
        voice_alerts = {
            "day": day_key,
            "soft": {"played": played.get("soft", 0), "max": _cap(cfg.get("soft_max_alerts"))},
            "hard": {"played": played.get("hard", 0), "max": _cap(cfg.get("hard_max_alerts"))},
        }

    return web.json_response(
        {
            "success": True,
            "stats": stats,
            "myself_buys": myself_buys,
            "voice_alerts": voice_alerts,
            "date": date or None,
            "scope": "day" if date else "all",
        }
    )


async def handle_api_voice_claim(request: web.Request) -> web.Response:
    store = request.app["store"]
    cfg = request.app.get("config") or {}

    tier = _str_param(request, "tier", 8).lower()
    tz = _clamp_tz(_int_param(request, "tz_offset", 0))
    date = _str_param(request, "date", 10)

    addr = (cfg.get("myself_address") or "").strip()
    deny = {"success": True, "allowed": False, "played": 0, "max": 0, "count": 0}
    if not addr or tier not in ("soft", "hard"):
        return web.json_response(deny)

    if tier == "soft":
        threshold = _cap(cfg.get("soft_buy_amount"))
        max_alerts = _cap(cfg.get("soft_max_alerts"))
    else:
        threshold = _cap(cfg.get("hard_buy_amount"))
        max_alerts = _cap(cfg.get("hard_max_alerts"))

    day_key = date or _today_key(tz)
    bounds = _day_bounds(day_key, tz)
    # A client-supplied date that doesn't parse must NOT silently widen the window to
    # all-time (which could grant a play it shouldn't) — reject it like the other handlers.
    if date and bounds is None:
        deny["max"] = max_alerts
        return web.json_response(deny)
    start_epoch, end_epoch = bounds if bounds else (None, None)

    if not getattr(store, "ok", False):
        deny["max"] = max_alerts
        return web.json_response(deny)

    # enable_duplicate_buy=false => repeat buys of one CA count once toward the limit.
    enable_dup = bool(cfg.get("enable_duplicate_buy", True))
    try:
        count = await asyncio.to_thread(
            store.buys_count, start_epoch, end_epoch,
            count_distinct_ca=not enable_dup,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("api/voice/claim count read failed: %s", exc)
        count = 0

    try:
        result = await asyncio.to_thread(
            store.claim_voice_alert, day_key, tier, count, threshold, max_alerts
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("api/voice/claim failed: %s", exc)
        result = {"allowed": False, "played": 0, "max": max_alerts, "count": count}

    return web.json_response(
        {
            "success": True,
            "allowed": bool(result.get("allowed")),
            "played": int(result.get("played", 0) or 0),
            "max": int(result.get("max", max_alerts) or 0),
            "count": int(result.get("count", count) or 0),
            "tier": tier,
            "day": day_key,
        }
    )


async def handle_api_stream(request: web.Request) -> web.StreamResponse:
    hub = request.app.get("signal_hub")
    if hub is None:
        return web.json_response(
            {"success": False, "error": "stream_unavailable"}, status=503
        )
    event = hub.subscribe()
    if event is None:
        return web.json_response(
            {"success": False, "error": "stream_busy"}, status=503
        )

    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
    resp.enable_chunked_encoding()
    await resp.prepare(request)
    # Best-effort: flush each tiny event immediately.
    try:
        sock = request.transport.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception:
        pass

    shutdown: asyncio.Event = request.app["sse_shutdown"]
    last_gen = -1
    try:
        await resp.write(b": connected\nretry: 3000\n\n")
        last_gen = hub.generation
        await resp.write(
            f"event: signal\ndata: {{\"gen\": {last_gen}}}\n\n".encode("utf-8")
        )
        while not shutdown.is_set():
            try:
                await asyncio.wait_for(event.wait(), timeout=_SSE_HEARTBEAT_S)
            except asyncio.TimeoutError:
                await resp.write(b": ping\n\n")
                continue
            event.clear()
            if shutdown.is_set():
                break
            gen = hub.generation
            if gen != last_gen:
                last_gen = gen
                await resp.write(
                    f"event: signal\ndata: {{\"gen\": {gen}}}\n\n".encode("utf-8")
                )
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("SSE loop ended: %s", exc)
    finally:
        hub.unsubscribe(event)
        try:
            await resp.write_eof()
        except Exception:
            pass
    return resp


async def _on_shutdown(app: web.Application) -> None:
    """Wake every open SSE loop so it exits promptly during shutdown."""
    ev = app.get("sse_shutdown")
    if ev is not None:
        ev.set()
    hub = app.get("signal_hub")
    if hub is not None:
        try:
            hub.publish()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# server lifecycle
# --------------------------------------------------------------------------- #
class DisciplineWebServer:
    """Lifecycle wrapper around an aiohttp app, runner and TCP site. Runs inside the
    caller's asyncio loop; ``start()`` / ``stop()`` are idempotent."""

    def __init__(
        self,
        store: Any,
        *,
        host: str = "127.0.0.1",
        port: int = 8787,
        static_dir: str | None = None,
        version: str = "1",
        signal_hub: Any = None,
        config: dict[str, Any] | None = None,
        auth_bundle: dict[str, Any] | None = None,
    ) -> None:
        self.store = store
        self.host = host or "127.0.0.1"
        try:
            self.port = int(port)
        except (TypeError, ValueError):
            self.port = 8787
        if static_dir:
            self.static_dir = static_dir
        else:
            self.static_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static"
            )
        self.version = str(version)
        self.signal_hub = signal_hub
        self.config = dict(config) if config else {}
        # When set (built by handsoff.py only after pynacl/base58 import cleanly),
        # the page + data APIs are gated by a Solana-wallet whitelist login. None =
        # fully open (the original localhost behavior).
        self.auth_bundle = auth_bundle
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    def _build_app(self) -> web.Application:
        app = web.Application()
        app["store"] = self.store
        app["static_dir"] = self.static_dir
        app["version"] = self.version
        app["signal_hub"] = self.signal_hub
        app["config"] = self.config
        app["sse_shutdown"] = asyncio.Event()
        app.on_shutdown.append(_on_shutdown)

        bundle = self.auth_bundle
        enable_auth = bool(bundle)
        app["enable_auth"] = enable_auth

        if enable_auth:
            # Shared state used by the auth handlers, middleware and page gate.
            app["session_manager"] = bundle["session_manager"]
            app["whitelist"] = bundle["whitelist"]
            app["guard"] = bundle["guard"]
            protect = bundle["auth_required"]
            handlers = bundle["handlers"]
            # Public auth endpoints (no session required to reach them).
            app.router.add_post("/api/auth/nonce", handlers["nonce"])
            app.router.add_post("/api/auth/verify", handlers["verify"])
            app.router.add_get("/api/auth/me", handlers["me"])
            app.router.add_post("/api/auth/logout", handlers["logout"])
            app.router.add_get("/login", handle_login)
        else:
            # No-op pass-through so the route table is identical in shape.
            def protect(h):
                return h

        # The page gates itself (redirects to /login) when auth is on.
        app.router.add_get("/", handle_index)
        # Liveness stays public so health checks don't need a session.
        app.router.add_get("/api/health", handle_health)
        # Data API — protected only when auth is enabled.
        app.router.add_get("/api/config", protect(handle_api_config))
        app.router.add_get("/api/signals", protect(handle_api_signals))
        app.router.add_get("/api/stats", protect(handle_api_stats))
        app.router.add_post("/api/voice/claim", protect(handle_api_voice_claim))
        app.router.add_get("/api/stream", protect(handle_api_stream))

        # Only css/ and js/ are publicly mounted; pages/ is reachable only via the
        # routes above. Missing dirs only warn (assets 404).
        css = os.path.join(self.static_dir, "css")
        js = os.path.join(self.static_dir, "js")
        if os.path.isdir(css):
            app.router.add_static("/static/css/", css, show_index=False)
        else:
            logger.warning("static css dir missing: %s", css)
        if os.path.isdir(js):
            app.router.add_static("/static/js/", js, show_index=False)
        else:
            logger.warning("static js dir missing: %s", js)
        return app

    async def start(self) -> None:
        if self._runner is not None:
            return
        app = self._build_app()
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        self._runner = runner
        self._site = site
        auth_state = "wallet-auth ON" if self.auth_bundle else "open (no auth)"
        logger.info(
            "Web server listening on http://%s:%d (%s)", self.host, self.port, auth_state
        )

    async def stop(self) -> None:
        if self._site is not None:
            try:
                await self._site.stop()
            except Exception:
                pass
            self._site = None
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                pass
            self._runner = None
