/* HandsOff (再买剁手) — Buy-Discipline Stream frontend.
 *
 * Renders one card per BUY made by your monitored Solana wallets (parsed on the
 * server from Helius RPC). Buying the same token again makes a new card. A brand-new
 * card chimes with a rising sawtooth arpeggio; the chime fires only for cards whose
 * per-CA sequence falls in a configurable window (it starts at the Nth buy of a token
 * — chime_start_seq — and rings at most chime_max times per token), and a per-card
 * mute button silences a token entirely.
 *
 * The heart of the tool is the SELF-DISCIPLINE voice: whenever today's combined buy
 * count across your wallets reaches the configured soft / hard limit, the page speaks
 * a spoken reminder ("再买剁手" — hands off the wallet). Each play is claimed
 * atomically from the server so the per-day caps hold across reloads, tabs and
 * restarts, the same count never re-speaks, and the voice repeats once per further buy
 * up to the cap.
 *
 * It receives millisecond pushes over Server-Sent Events (/api/stream) and ALSO polls
 * /api/signals as a robust fallback, keeps streaming while the tab is hidden (a silent
 * audio keep-alive) so the chime/voice still play in the background, and drives
 * search / filter / sort entirely server-side.
 *
 * Frontend version: bump FRONTEND_VERSION + the ?v=N on discipline.html together
 * whenever the frontend changes (kept in lockstep). */
'use strict';

const FRONTEND_VERSION = '1.1.1';
const POLL_MS = 8000;             // fallback poll cadence (SSE is the fast path)
const STREAM_URL = '/api/stream'; // Server-Sent-Events endpoint (live push)
const PAGE_SIZE = 9;              // cards per page (discrete first/prev/next/last paging)
const SEARCH_DEBOUNCE_MS = 300;
const MUTE_KEY = 'handsoff_sound_muted';
// Per-CA chime mute: tokens the user flagged from a card's 🔔 button. Their future buy
// cards never chime (within the chime window). Persisted across reloads.
const MUTED_KEY = 'handsoff_muted_signals';
const MUTED_MAX = 1000;           // hard cap on stored muted-signal keys (most-recent kept)
const CHIME_DEFAULT = { enabled: true, start_seq: 1, max: 5 };

/* Short alias for the i18n lookup; resilient if i18n.js failed to load. */
function t(key, vars) {
  return (window.I18n && window.I18n.t) ? window.I18n.t(key, vars) : key;
}
function emptyHtml() {
  return '<div class="big">🪓</div><div>' + esc(t('empty')) + '</div>';
}

/* ----------------------------- state ----------------------------- */
const state = {
  q: '',
  date: '',
  autoDate: false,
  sort: 'time-newest',
  page: 1,
  total: 0,
  lastMaxId: null,
  primed: false,
  lastSig: '',
  muted: false,
  mutedSignals: new Set(),  // per-CA chime mutes (keys from muteKeyFor)
  audioCtx: null,
  // Flips true once a user gesture has primed window.speechSynthesis via ensureVoice().
  // Browsers (notably Chrome) silently DROP a speech utterance that is the page's first
  // and fires from a poll/SSE callback with no prior user activation — so a soft/hard
  // reminder is only treated as genuinely HEARD (and the tier marked spoken) once this
  // is true. Mirrors the AudioContext unlock the chime relies on, but for the Speech API.
  voiceUnlocked: false,
  pendingChime: false,
  chime: Object.assign({}, CHIME_DEFAULT),
  es: null,
  pollTimer: null,
  searchTimer: null,
  inFlight: false,
  pendingReload: false,     // a reload arrived mid-fetch → run once when it settles
  controller: null,
  lastSignals: [],
  view: 'loading',
  errorKind: null,
  lastUpdatedAt: null,
  // Self-discipline panel (your wallets' combined today count). `myself` is null until
  // /api/config reports a configured panel; `myselfCount` is the last-seen TODAY count;
  // `myselfPrimed` flips true after the first reading; `myselfSpokenLevel` is the highest
  // limit TIER whose voice was actually HEARD (0 none, 1 soft, 2 hard) — it advances ONLY
  // when a reminder was genuinely voiced (not merely reached), so an announce suppressed
  // by mute or a still-locked Speech API retries on the next poll/SSE until truly heard.
  myself: null,
  myselfCount: null,
  myselfPrimed: false,
  myselfSpokenLevel: 0,
  // Highest tier whose VISUAL toast has been shown — tracked apart from
  // `myselfSpokenLevel` so the on-screen warning appears once per tier (plus a re-warn on
  // every further live buy) even while the spoken voice is still being retried.
  myselfToastedLevel: 0,
  // Last-seen PERSISTED play-counts of the soft/hard reminder for the current local day,
  // mirrored from `voice_alerts` on /api/stats — a fast client-side budget check so the
  // page skips a needless claim once a tier's cap is exhausted; the SERVER claim
  // (/api/voice/claim) remains the authoritative, atomic gate. `alertsDay` is the day
  // these counts belong to; `max` of 0 means UNLIMITED.
  alertsPlayed: { soft: 0, hard: 0 },
  alertsMax: { soft: 0, hard: 0 },
  alertsDay: null,
  // Per-tier "a claim is currently outstanding" guard — the voice is attempted on EVERY
  // reading (level-triggered), so without this an SSE-driven reload and the 8s poll both
  // calling updateMyselfCount within one claim round-trip would each fire a SECOND claim
  // for the same count. Allowing only one in-flight claim per tier keeps it at most one
  // announce per count while the server stays the authoritative cap gate.
  voiceClaimInFlight: { soft: false, hard: false },
  // Highest buy count for which a server voice claim has already been ISSUED per tier
  // (-1 = none yet). Mirrors the server's persisted `last_count` so the page attempts
  // each count at most once and never spins denied claims at a steady, already-announced
  // count. Advanced only once the server has RESPONDED (so a transient network failure
  // still retries), and re-armed when the count drops (a new-day reset).
  lastClaimAttempt: { soft: -1, hard: -1 },
};

/* ----------------------------- DOM ----------------------------- */
const $ = (id) => document.getElementById(id);
const els = {};

document.addEventListener('DOMContentLoaded', async () => {
  els.grid = $('grid');
  els.loading = $('loading');
  els.empty = $('empty');
  els.search = $('search');
  els.date = $('date');
  els.dateClear = $('date-clear');
  els.sort = $('sort');
  els.sound = $('sound-toggle');
  els.live = $('live');
  els.pager = $('pager');
  els.pageFirst = $('page-first');
  els.pagePrev = $('page-prev');
  els.pageNext = $('page-next');
  els.pageLast = $('page-last');
  els.pageInfo = $('page-info');
  els.statBuys = $('stat-buys');
  els.statTokens = $('stat-tokens');
  els.statWallets = $('stat-wallets');
  els.toast = $('toast');
  els.version = $('version');
  els.updated = $('updated');
  els.themeToggle = $('theme-toggle');
  els.langZh = $('lang-zh');
  els.langEn = $('lang-en');
  els.myselfPanel = $('myself-panel');
  els.myselfName = $('myself-name');
  els.myselfCount = $('myself-count');
  els.myselfLimits = $('myself-limits');
  els.walletChip = $('wallet-chip');
  els.walletAddr = $('wallet-addr');
  els.logout = $('logout-btn');

  if (els.version) els.version.textContent = 'v' + FRONTEND_VERSION;

  initI18n();
  initTheme();

  // When wallet-auth is enabled the page must be signed in before it loads any data;
  // bootAuth redirects to /login otherwise. With auth disabled (/api/auth/me 404s)
  // it returns true and the page behaves exactly as the open localhost build.
  const authed = await bootAuth();
  if (!authed) return; // redirected to /login

  initSound();
  loadMutedSignals();   // restore per-CA chime mutes before the first render
  bindEvents();
  initDateFilter();
  await loadConfig();   // best-effort: prime chime + self-wallet panel
  loadStats();
  loadSignals(true);
  startPolling();
  startStream();

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      startBackgroundAudioKeepAlive();
    } else {
      stopBackgroundAudioKeepAlive();
      setLive(true);
      // A reminder queued while the tab was hidden may have been paused by the browser —
      // resume the Speech engine so it plays on return.
      try {
        if (window.speechSynthesis && window.speechSynthesis.paused) window.speechSynthesis.resume();
      } catch (e) { /* ignore */ }
      if (state.pendingChime) {
        state.pendingChime = false;
        if (!state.muted) playChime();
        showToast(t('toastNew'));
      }
      loadSignals(false);
      loadStats();   // refresh the self-wallet count + re-evaluate the soft/hard voice
      startPolling();
      startStream();
    }
  });

  if (document.hidden) startBackgroundAudioKeepAlive();
});

/* ----------------------------- auth ----------------------------- */
function redirectLogin() { window.location.href = '/login'; }

/* Returns true when the page may proceed (auth disabled, or signed in), false after
 * triggering a redirect to /login. Tolerant of every failure mode: a 404 means auth
 * is off (open mode); a network error also proceeds rather than locking the user out
 * of a local, auth-disabled build. */
async function bootAuth() {
  try {
    const r = await fetch('/api/auth/me', { cache: 'no-store' });
    if (r.status === 404) return true;          // auth disabled — open mode
    if (r.status === 401) { redirectLogin(); return false; }
    const d = await r.json().catch(() => null);
    if (d && d.success && d.wallet) { showWallet(d.wallet); return true; }
    if (d && d.auth_enabled === false) return true;
    redirectLogin();
    return false;
  } catch (e) {
    return true;
  }
}

function showWallet(addr) {
  if (els.walletChip && els.walletAddr) {
    els.walletAddr.textContent = addr.length > 10 ? addr.slice(0, 4) + '…' + addr.slice(-4) : addr;
    els.walletChip.title = addr;
    els.walletChip.classList.remove('hidden');
  }
  if (els.logout) {
    els.logout.classList.remove('hidden');
    els.logout.addEventListener('click', doLogout);
  }
}

async function doLogout() {
  stopStream();
  try { await fetch('/api/auth/logout', { method: 'POST', cache: 'no-store' }); } catch (e) { /* ignore */ }
  redirectLogin();
}

/* ----------------------------- theme & i18n ----------------------------- */
function initTheme() {
  if (window.Theme && els.themeToggle) Theme.bindToggle(els.themeToggle);
}

function initI18n() {
  applyLang();
  if (els.langZh) els.langZh.addEventListener('click', () => setLang('zh'));
  if (els.langEn) els.langEn.addEventListener('click', () => setLang('en'));
}

function setLang(lang) {
  if (window.I18n) I18n.setLang(lang);
  applyLang();
}

function applyLang() {
  if (window.I18n) I18n.applyStaticText();
  const lang = (window.I18n && I18n.lang) || 'en';
  if (els.langZh) els.langZh.classList.toggle('active', lang === 'zh');
  if (els.langEn) els.langEn.classList.toggle('active', lang === 'en');

  reflectSound();
  const live = !(els.live && els.live.classList.contains('paused'));
  setLive(live);
  refreshUpdatedLabel();
  renderMyselfStatic();   // re-localize the self-wallet panel's static labels

  if (state.view === 'error') {
    renderError(state.errorKind);
  } else if (state.view === 'cards' && state.lastSignals.length) {
    render(state.lastSignals, Infinity);
  } else if (state.view === 'empty') {
    els.empty.innerHTML = emptyHtml();
  }
}

/* ----------------------------- events ----------------------------- */
function bindEvents() {
  els.search.addEventListener('input', () => {
    clearTimeout(state.searchTimer);
    state.searchTimer = setTimeout(() => {
      state.q = els.search.value.trim();
      state.page = 1;
      loadSignals(true);
    }, SEARCH_DEBOUNCE_MS);
  });

  els.date.addEventListener('change', () => {
    state.date = els.date.value;
    state.autoDate = false;
    state.page = 1;
    loadSignals(true);
  });

  els.dateClear.addEventListener('click', () => {
    els.date.value = '';
    state.date = '';
    state.autoDate = false;
    state.page = 1;
    loadSignals(true);
  });

  els.sort.addEventListener('change', () => {
    state.sort = els.sort.value;
    state.page = 1;
    loadSignals(true);
  });

  els.sound.addEventListener('click', toggleSound);

  if (els.pageFirst) els.pageFirst.addEventListener('click', () => gotoPage(1));
  if (els.pagePrev) els.pagePrev.addEventListener('click', () => gotoPage(state.page - 1));
  if (els.pageNext) els.pageNext.addEventListener('click', () => gotoPage(state.page + 1));
  if (els.pageLast) els.pageLast.addEventListener('click', () => gotoPage(totalPages()));

  // Unlock BOTH the chime's AudioContext and the Speech API on the first user gesture.
  // speechSynthesis needs its own gesture-time warmup or the first soft/hard reminder —
  // fired later from a poll/SSE callback — is silently dropped by the browser.
  const unlock = () => {
    ensureAudio();
    ensureVoice();
    // Speech is now unlocked: flush a reminder the page may have wanted to speak while it
    // was still locked (e.g. opened already at/over a cap) right away.
    updateMyselfCount(state.myselfCount);
    document.removeEventListener('click', unlock);
    document.removeEventListener('keydown', unlock);
  };
  document.addEventListener('click', unlock);
  document.addEventListener('keydown', unlock);
}

/* ----------------------------- data ----------------------------- */
function buildQuery() {
  const p = new URLSearchParams();
  if (state.q) p.set('q', state.q);
  // A search spans the WHOLE database: drop the day scope while a query is active so the
  // auto "today" default can't hide older matches.
  if (state.date && !state.q) p.set('date', state.date);
  p.set('tz_offset', String(-new Date().getTimezoneOffset()));
  p.set('sort', state.sort);
  p.set('limit', String(PAGE_SIZE));
  p.set('offset', String((Math.max(1, state.page) - 1) * PAGE_SIZE));
  return p.toString();
}

/* Best-effort prime of the page config before the first signals load: the chime window
 * and the self-discipline panel descriptor. Reads an in-memory server dict. */
async function loadConfig() {
  try {
    const resp = await fetch('/api/config', { cache: 'no-store' });
    if (resp.status === 401) { redirectLogin(); return; }
    const data = await resp.json().catch(() => null);
    if (!data || data.success === false) return;
    if (data.chime) applyChimeConfig(data.chime);
    applyMyselfConfig(data.myself);
  } catch (e) { /* keep defaults — panel/voice simply stay off */ }
}

/* Adopt the self-discipline descriptor from /api/config. Null / address-less input hides
 * the panel and disables the voice. Limits are sanitized to non-negative integers
 * (0 = that voice disabled). */
function applyMyselfConfig(m) {
  if (!m || typeof m !== 'object' || !m.address) {
    state.myself = null;
    if (els.myselfPanel) els.myselfPanel.classList.add('hidden');
    return;
  }
  const addr = String(m.address);
  const toCap = (v) => {
    const n = Number(v);
    return (Number.isFinite(n) && n > 0) ? Math.floor(n) : 0;
  };
  state.myself = {
    address: addr,
    label: String(m.label || '').trim() || shortCA(addr),
    url: safeUrl(m.url) || ('https://gmgn.ai/sol/address/' + encodeURIComponent(addr)),
    soft: toCap(m.soft),
    hard: toCap(m.hard),
    // Per-day spoken-reminder caps (0 = unlimited). Server enforces + persists; these are
    // the client's fast-path hint.
    softMax: toCap(m.soft_max),
    hardMax: toCap(m.hard_max),
  };
  state.alertsMax = { soft: state.myself.softMax, hard: state.myself.hardMax };
  if (els.myselfPanel) {
    els.myselfPanel.href = state.myself.url;
    els.myselfPanel.classList.remove('hidden');
  }
  renderMyselfStatic();
  renderMyselfCount(state.myselfCount);
}

/* Paint the self-wallet panel's STATIC text (label + soft/hard limit hint) in the current
 * language. Safe to call when no panel is configured. */
function renderMyselfStatic() {
  const m = state.myself;
  if (!m || !els.myselfPanel) return;
  if (els.myselfName) els.myselfName.textContent = m.label;
  if (els.myselfLimits) {
    let txt = '';
    if (m.soft > 0 && m.hard > 0) txt = t('myselfLimits', { soft: m.soft, hard: m.hard });
    else if (m.soft > 0) txt = t('myselfSoftOnly', { soft: m.soft });
    else if (m.hard > 0) txt = t('myselfHardOnly', { hard: m.hard });
    els.myselfLimits.textContent = txt;
  }
  const title = t('myselfPanelTitle', { name: m.label });
  els.myselfPanel.title = title;
  els.myselfPanel.setAttribute('aria-label', title);
}

/* Paint the today buy-count value + the panel's warn/danger level (independent of the
 * voice). `count` may be null (unknown → em dash, neutral level). */
function renderMyselfCount(count) {
  const m = state.myself;
  if (!m || !els.myselfPanel) return;
  const n = (count == null || isNaN(count)) ? null : Math.max(0, Math.floor(Number(count)));
  if (els.myselfCount) els.myselfCount.textContent = (n == null) ? '—' : fmtInt(n);
  let warn = false, danger = false;
  if (n != null) {
    if (m.hard > 0 && n >= m.hard) danger = true;
    else if (m.soft > 0 && n >= m.soft) warn = true;
  }
  els.myselfPanel.classList.toggle('warn', warn);
  els.myselfPanel.classList.toggle('danger', danger);
}

/* Map a today buy-count to the highest self-discipline limit TIER it has reached:
 * 2 = at/over the HARD cap, 1 = at/over the SOFT cap, 0 = under both (or caps off).
 * Mirrors maybeSpeakLimit's hard-over-soft precedence so the tier and the spoken phrase
 * always agree. */
function myselfLimitTier(count) {
  const m = state.myself;
  if (!m || count == null || isNaN(count)) return 0;
  const n = Math.max(0, Math.floor(Number(count)));
  if (m.hard > 0 && n >= m.hard) return 2;
  if (m.soft > 0 && n >= m.soft) return 1;
  return 0;
}

/* Fold a fresh today-count from /api/stats into the panel and fire the soft/hard reminder
 * whenever today's count sits at/over a cap. Two independent announcements run, each
 * re-warning on EVERY further over-limit buy as the count climbs live:
 *   • the VISUAL toast — shown once per tier (tracked by `myselfToastedLevel`) plus a
 *     re-warn on each live buy; AND
 *   • the SPOKEN voice — attempted on EVERY reading while a tier is reached, gated by
 *     maybeSpeakLimit's own idempotent per-count guard (`lastClaimAttempt`, mirroring the
 *     server's persisted last_count): it speaks each distinct buy count at most once, so
 *     re-calling it every poll/SSE simply retries any count not yet claimed and bails for
 *     one already spoken. A null count (read error) is ignored. */
function updateMyselfCount(raw) {
  if (!state.myself) return;
  const count = (raw == null || isNaN(raw)) ? null : Math.max(0, Math.floor(Number(raw)));
  renderMyselfCount(count);
  if (count == null) return;
  const prev = state.myselfCount;
  const tier = myselfLimitTier(count);
  // New-day re-arm: a count that has fallen below a previously announced tier lowers both
  // the spoken and toasted level so the next climb announces afresh.
  if (tier < state.myselfSpokenLevel) state.myselfSpokenLevel = tier;
  if (tier < state.myselfToastedLevel) state.myselfToastedLevel = tier;
  // A count that DROPPED (a new-day reset) re-arms the per-count claim guard.
  if (prev != null && count < prev) state.lastClaimAttempt = { soft: -1, hard: -1 };
  if (tier > 0) {
    const liveIncrease = (prev != null && count > prev);
    const firstReading = !state.myselfPrimed;
    const toastDue = (tier > state.myselfToastedLevel);
    // Visual toast: once per tier, plus a re-warn on every further live buy. Shown
    // regardless of mute / speech availability so the cap is never missed silently.
    if (toastDue || liveIncrease || firstReading) {
      showToast(t(tier >= 2 ? 'toastHard' : 'toastSoft'));
      if (tier > state.myselfToastedLevel) state.myselfToastedLevel = tier;
    }
    // Spoken voice: ALWAYS attempt a claim for the current count while a tier is reached —
    // LEVEL-triggered. maybeSpeakLimit is its own idempotent gate, so calling it on every
    // poll/SSE just retries any not-yet-claimed count and bails synchronously otherwise.
    maybeSpeakLimit(count).then((spoke) => {
      if (spoke && tier > state.myselfSpokenLevel) state.myselfSpokenLevel = tier;
    });
  }
  state.myselfCount = count;
  state.myselfPrimed = true;
}

/* Speak the hard (priority) or soft reminder once today's count has reached the configured
 * threshold. ASYNC: each play is claimed atomically from the server (claimVoiceAlert), the
 * authoritative gate that decides from its OWN recomputed buy count — it grants a play only
 * while the count has reached the threshold, is HIGHER than the last count already spoken
 * for that tier, and the per-tier play budget is not spent. Respects the global mute; hard
 * takes precedence. Resolves true ONLY when a reminder was actually voiced. */
async function maybeSpeakLimit(count) {
  if (state.muted) return false;      // honor the header sound toggle
  const m = state.myself;
  if (!m) return false;
  let tier, phraseKey;
  if (m.hard > 0 && count >= m.hard) { tier = 'hard'; phraseKey = 'voiceHard'; }
  else if (m.soft > 0 && count >= m.soft) { tier = 'soft'; phraseKey = 'voiceSoft'; }
  else return false;
  // Never burn a persisted play on an utterance that cannot actually be heard: the Speech
  // API must be unlocked by a prior gesture, and speechSynthesis must exist at all.
  if (!state.voiceUnlocked) return false;
  if (!window.speechSynthesis || typeof SpeechSynthesisUtterance === 'undefined') return false;
  // Only one claim outstanding per tier at a time.
  if (state.voiceClaimInFlight[tier]) return false;
  // Fast client-side budget check (skip the round-trip when clearly exhausted). The server
  // claim remains the authority; a cap of 0 means UNLIMITED.
  const cap = (state.alertsMax && state.alertsMax[tier]) || 0;
  if (cap > 0 && (state.alertsPlayed[tier] || 0) >= cap) return false;
  // Per-count idempotency hint: the server only speaks a count HIGHER than the last it
  // already spoke for this tier, so a claim for a count we've already attempted is a
  // guaranteed denial — skip it (and the every-poll retry it would cause).
  if (count <= state.lastClaimAttempt[tier]) return false;
  // Atomic, persisted claim — only speak if the server grants the play.
  state.voiceClaimInFlight[tier] = true;
  try {
    const res = await claimVoiceAlert(tier);
    // The server RESPONDED (granted or denied) → don't re-claim this same count; a null res
    // (network failure) leaves the guard so the next poll can retry.
    if (res) state.lastClaimAttempt[tier] = count;
    if (res && typeof res.played === 'number') state.alertsPlayed[tier] = res.played;
    if (res && typeof res.max === 'number') state.alertsMax[tier] = res.max;
    if (!res || !res.allowed) return false;
    const lang = (window.I18n && I18n.lang) || 'en';
    const code = lang === 'zh' ? 'zh-CN' : 'en-US';
    return speakVoice(t(phraseKey), code);
  } finally {
    state.voiceClaimInFlight[tier] = false;
  }
}

/* Atomically claim ONE play of the `tier` ('soft' | 'hard') reminder from the server
 * (POST /api/voice/claim). The server recomputes the buy count itself and grants the play
 * only while that count has reached the tier's threshold, is higher than the last count it
 * already spoke for, and the tier's play budget is not spent — then it persists the new
 * count + play tally. Returns `{ allowed, played, max }` on success, or null on any failure
 * so the caller treats it as "not granted" and stays silent. */
async function claimVoiceAlert(tier) {
  try {
    const p = new URLSearchParams();
    p.set('tier', tier);
    const today = todayLocal();
    if (today) {
      p.set('date', today);
      p.set('tz_offset', String(-new Date().getTimezoneOffset()));
    }
    const resp = await fetch('/api/voice/claim?' + p.toString(),
      { method: 'POST', cache: 'no-store' });
    if (resp.status === 401) { redirectLogin(); return null; }
    const data = await resp.json().catch(() => null);
    if (!data || data.success === false) return null;
    return {
      allowed: !!data.allowed,
      played: Math.max(0, Math.floor(Number(data.played)) || 0),
      max: Math.max(0, Math.floor(Number(data.max)) || 0),
    };
  } catch (e) { return null; }
}

/* Speak a short phrase via the Web Speech API. Best-effort: silently degrades when
 * speechSynthesis is unavailable or blocked (the toast is the visual fallback). Cancels any
 * queued utterance first so reminders never pile up — but DEFERS the speak() one tick past
 * the cancel() to dodge the Chrome bug where a same-tick cancel()→speak() pair drops the new
 * utterance, and resume()s first in case a backgrounded tab left the engine paused. */
function speakVoice(text, langCode) {
  try {
    const synth = window.speechSynthesis;
    if (!synth || typeof SpeechSynthesisUtterance === 'undefined') return false;
    const phrase = (text == null ? '' : String(text)).trim();
    if (!phrase) return false;
    const u = new SpeechSynthesisUtterance(phrase);
    u.lang = langCode || 'zh-CN';
    u.rate = 1.0; u.pitch = 1.0; u.volume = 1.0;
    try { synth.cancel(); } catch (e) { /* ignore */ }
    setTimeout(() => {
      try { synth.resume(); } catch (e) { /* ignore */ }
      try { synth.speak(u); } catch (e) { /* ignore */ }
    }, 0);
    return !!state.voiceUnlocked;
  } catch (e) { return false; }
}

function applyChimeConfig(c) {
  if (!c || typeof c !== 'object') return;
  const start = Number(c.start_seq);
  const max = Number(c.max);
  state.chime = {
    enabled: c.enabled !== false,
    start_seq: Number.isFinite(start) && start >= 1 ? Math.floor(start) : CHIME_DEFAULT.start_seq,
    max: Number.isFinite(max) && max >= 0 ? Math.floor(max) : CHIME_DEFAULT.max,
  };
}

async function loadSignals(reset) {
  if (maybeRollDate()) reset = true;
  if (reset) {
    state.pendingReload = false;
    if (state.controller) { try { state.controller.abort(); } catch (e) { /* noop */ } }
  } else if (state.inFlight) {
    // Coalesce instead of dropping: a live SSE push (or poll) that lands while a fetch is
    // in flight must not be lost — remember it and run exactly one more reload when the
    // current one settles, so a burst of rapid buys is never lost.
    state.pendingReload = true;
    return;
  }
  const controller = new AbortController();
  state.controller = controller;
  state.inFlight = true;
  if (reset) els.loading.classList.remove('hidden');

  try {
    const resp = await fetch('/api/signals?' + buildQuery(),
      { cache: 'no-store', signal: controller.signal });
    if (resp.status === 401) { redirectLogin(); return; }
    const data = await resp.json();
    if (!data || data.success === false) {
      renderError(data && data.error);
      return;
    }
    if (data.chime) applyChimeConfig(data.chime);
    const signals = Array.isArray(data.signals) ? data.signals : [];
    state.total = data.total || 0;
    const maxId = data.max_id || 0;
    const prevMax = state.lastMaxId;

    const pages = totalPages();
    if (state.page > pages) {
      state.page = pages;
      loadSignals(true);
      return;
    }

    // A brand-new card was recorded somewhere (the global max id grew). Chime ONLY for
    // genuinely-new cards whose per-CA seq falls in the configured window. Never on the
    // priming load, never on a filter change that returns the same max id.
    if (state.primed && prevMax != null && maxId > prevMax) {
      const fresh = signals.filter((s) => s && s.id != null && s.id > prevMax);
      const eligible = fresh.filter(chimeEligible);
      if (eligible.length && !state.muted && state.chime.enabled) {
        const heard = playChime();
        if (document.hidden && !heard) state.pendingChime = true;
      } else if (eligible.length && document.hidden && !state.muted && state.chime.enabled) {
        state.pendingChime = true;
      }
      showToast(fresh.length
        ? t(fresh.length > 1 ? 'toastNewMany' : 'toastNewOne', { n: fresh.length })
        : t('toastNew'));
    }

    const first = signals[0] && signals[0].id;
    const last = signals[signals.length - 1] && signals[signals.length - 1].id;
    const sig = maxId + '|' + state.total + '|' + signals.length + '|' + first + '|' + last;
    if (!reset && sig === state.lastSig) {
      markUpdated();
      return;
    }
    state.lastSig = sig;

    const freshThreshold = (state.primed && prevMax != null) ? prevMax : Infinity;
    render(signals, freshThreshold);

    state.lastMaxId = (prevMax == null) ? maxId : Math.max(prevMax, maxId);
    state.primed = true;
    markUpdated();
  } catch (err) {
    if (err && err.name === 'AbortError') return;
    renderError('network');
  } finally {
    if (state.controller === controller) {
      state.inFlight = false;
      els.loading.classList.add('hidden');
      if (state.pendingReload) {
        state.pendingReload = false;
        loadSignals(false);
      }
    }
  }
}

/* True when a brand-new card should ring: its per-CA sequence is at or past the configured
 * start (the "Nth buy") and within the max-count window, so each token rings at most
 * `chime_max` times and never before the Nth buy. */
function chimeEligible(s) {
  if (isSignalMuted(s)) return false;   // a per-card-muted token never rings
  const win = state.chime || CHIME_DEFAULT;
  if (!win.enabled || win.max <= 0) return false;
  const seq = Number(s && s.seq);
  if (!Number.isFinite(seq)) return false;
  return seq >= win.start_seq && seq < win.start_seq + win.max;
}

/* ----------------------------- per-CA mute ----------------------------- */
/* Stable key for a token group: prefer the CA (shared by every buy of the token), fall back
 * to the card id. So muting any one card silences the chime for every future buy of that
 * token. */
function muteKeyFor(s) {
  if (!s) return '';
  const ca = (s.ca == null ? '' : String(s.ca)).trim();
  if (ca) return 'ca:' + ca;
  return s.id != null ? 'id:' + s.id : '';
}

function isSignalMuted(s) {
  const key = muteKeyFor(s);
  return !!key && state.mutedSignals.has(key);
}

function loadMutedSignals() {
  state.mutedSignals = new Set();
  try {
    const raw = localStorage.getItem(MUTED_KEY);
    if (!raw) return;
    const arr = JSON.parse(raw);
    if (Array.isArray(arr)) {
      for (const k of arr) {
        if (typeof k === 'string' && k) state.mutedSignals.add(k);
        if (state.mutedSignals.size >= MUTED_MAX) break;
      }
    }
  } catch (e) { /* private mode / malformed — start with an empty set */ }
}

function persistMutedSignals() {
  try {
    let arr = Array.from(state.mutedSignals);
    if (arr.length > MUTED_MAX) {
      arr = arr.slice(arr.length - MUTED_MAX);
      state.mutedSignals = new Set(arr);
    }
    localStorage.setItem(MUTED_KEY, JSON.stringify(arr));
  } catch (e) { /* storage disabled / quota — the mute still holds in-memory */ }
}

function toggleSignalMute(el) {
  if (!el) return;
  const key = el.getAttribute('data-mute-key');
  if (!key) return;
  const nowMuted = !state.mutedSignals.has(key);
  if (nowMuted) state.mutedSignals.add(key);
  else state.mutedSignals.delete(key);
  persistMutedSignals();
  applyMuteStateForKey(key, nowMuted);
  showToast(t(nowMuted ? 'toastMuted' : 'toastUnmuted'));
}

function applyMuteStateForKey(key, muted) {
  if (!els.grid) return;
  els.grid.querySelectorAll('.bull-mute').forEach((b) => {
    if (b.getAttribute('data-mute-key') !== key) return;
    b.classList.toggle('muted', muted);
    b.textContent = muted ? '🔕' : '🔔';
    b.title = muted ? t('unmuteTitle') : t('muteTitle');
    b.setAttribute('aria-pressed', muted ? 'true' : 'false');
    const card = b.closest('.bull-card');
    if (card) card.classList.toggle('signal-muted', muted);
  });
}

async function loadStats() {
  try {
    const p = new URLSearchParams();
    const today = todayLocal();
    if (today) {
      p.set('date', today);
      p.set('tz_offset', String(-new Date().getTimezoneOffset()));
    }
    const qs = p.toString();
    const resp = await fetch('/api/stats' + (qs ? '?' + qs : ''), { cache: 'no-store' });
    if (resp.status === 401) { redirectLogin(); return; }
    const data = await resp.json();
    const s = (data && data.stats) || {};
    if (els.statBuys) els.statBuys.textContent = fmtInt(s.buys);
    if (els.statTokens) els.statTokens.textContent = fmtInt(s.tokens);
    if (els.statWallets) els.statWallets.textContent = fmtInt(s.wallets);
    // Mirror the persisted soft/hard voice budget BEFORE re-evaluating the count so the cap
    // check sees the freshest server-side play-counts.
    if (state.myself && data) applyVoiceAlerts(data.voice_alerts);
    // Self-discipline today count (drives the panel + soft/hard voice). `myself_buys` may
    // be null on a read error.
    if (state.myself && data) updateMyselfCount(data.myself_buys);
  } catch (e) { /* header stats are best-effort */ }
}

/* Mirror the server's persisted soft/hard reminder play-counts + caps into the local budget
 * cache. This is only a fast-path hint — the authoritative enforcement is the server claim —
 * so it tolerates a missing / malformed payload. A `max` of 0 = unlimited. */
function applyVoiceAlerts(va) {
  if (!va || typeof va !== 'object') return;
  state.alertsDay = (va.day == null) ? state.alertsDay : String(va.day);
  const read = (o, fallbackMax) => {
    const x = (o && typeof o === 'object') ? o : {};
    const played = Math.max(0, Math.floor(Number(x.played)) || 0);
    let max = Math.floor(Number(x.max));
    if (!Number.isFinite(max) || max < 0) max = fallbackMax;
    return { played, max };
  };
  const soft = read(va.soft, state.alertsMax.soft || 0);
  const hard = read(va.hard, state.alertsMax.hard || 0);
  state.alertsPlayed = { soft: soft.played, hard: hard.played };
  state.alertsMax = { soft: soft.max, hard: hard.max };
}

/* ----------------------------- render ----------------------------- */
function render(signals, freshThreshold) {
  state.lastSignals = Array.isArray(signals) ? signals : [];
  if (!signals.length) {
    state.view = 'empty';
    els.grid.innerHTML = '';
    els.empty.innerHTML = emptyHtml();
    els.empty.classList.remove('hidden');
    els.pager.classList.add('hidden');
    return;
  }
  state.view = 'cards';
  els.empty.classList.add('hidden');
  els.grid.innerHTML = signals
    .map((s) => cardHtml(s, !!(s && s.id != null && s.id > freshThreshold)))
    .join('');
  renderPager();
}

function totalPages() {
  return Math.max(1, Math.ceil((state.total || 0) / PAGE_SIZE));
}

function gotoPage(page) {
  const pages = totalPages();
  const next = Math.min(pages, Math.max(1, Math.floor(page) || 1));
  if (next === state.page) return;
  state.page = next;
  loadSignals(true);
}

function renderPager() {
  const pages = totalPages();
  if (state.page > pages) state.page = pages;
  if (pages <= 1 || !state.total) {
    els.pager.classList.add('hidden');
    return;
  }
  els.pager.classList.remove('hidden');
  if (els.pageInfo) {
    els.pageInfo.textContent = t('pageInfo', { page: state.page, total: pages });
  }
  const atFirst = state.page <= 1;
  const atLast = state.page >= pages;
  if (els.pageFirst) els.pageFirst.disabled = atFirst;
  if (els.pagePrev) els.pagePrev.disabled = atFirst;
  if (els.pageNext) els.pageNext.disabled = atLast;
  if (els.pageLast) els.pageLast.disabled = atLast;
}

function cardHtml(s, isFresh) {
  const name = esc(s.name || t('unknownToken'));
  const symbol = esc(s.symbol || s.name || '—');
  const links = s.links || {};

  const iconUrl = safeUrl(s.icon);
  const iconHtml = iconUrl
    ? `<img src="${esc(iconUrl)}" alt="" loading="lazy" referrerpolicy="no-referrer"
         onerror="this.parentNode.textContent='🪓'">`
    : '🪓';

  const twUrl = safeUrl(s.twitter);
  const webUrl = safeUrl(s.website);
  const socials = [];
  if (twUrl) socials.push(`<a class="social" href="${esc(twUrl)}" target="_blank" rel="noopener" title="${esc(t('socialTwitter'))}">𝕏</a>`);
  if (webUrl) socials.push(`<a class="social" href="${esc(webUrl)}" target="_blank" rel="noopener" title="${esc(t('socialWebsite'))}">🌐</a>`);
  const platformTag = s.platform ? `<span class="ray-platform" title="${esc(t('platform'))}">${esc(s.platform)}</span>` : '';

  // Badge: "Buy #N" where N is the per-CA sequence (1st buy = #1).
  const seq = Number(s.seq) || 1;
  const badge = `<div class="bull-badge buy">${esc(t('buyBadge', { n: seq }))}</div>`;

  // Wallet row: the configured note (or short address) + a clickable address.
  const wallet = (s.wallet == null ? '' : String(s.wallet)).trim();
  const walletLabel = esc(s.wallet_label || (wallet ? shortCA(wallet) : t('unknownWallet')));
  const walletUrl = safeUrl(s.wallet_url) || (wallet ? 'https://gmgn.ai/sol/address/' + encodeURIComponent(wallet) : '');
  const walletAddr = wallet ? `<span class="ray-wallet-addr">${esc(shortCA(wallet))}</span>` : '';
  const walletInner = `<span class="ray-wallet-ico" aria-hidden="true">👤</span>
      <span class="ray-wallet-label" title="${esc(s.wallet_label || wallet)}">${walletLabel}</span>
      ${walletAddr}`;
  const walletRow = walletUrl
    ? `<a class="ray-wallet" href="${esc(walletUrl)}" target="_blank" rel="noopener" title="${esc(t('walletTitle'))}">${walletInner}</a>`
    : `<div class="ray-wallet">${walletInner}</div>`;

  // Buy hero: SOL spent (big) + USD value.
  const solStr = (s.sol_amount != null) ? fmtSol(s.sol_amount) : '—';
  const usdStr = (s.usd_amount != null) ? fmtUsd(s.usd_amount) : '';
  const heroLabel = usdStr ? t('heroLabel', { usd: usdStr }) : t('buy');
  const hero = `<div class="mult-hero buy ray-hero">
      <div class="mult-val">${esc(solStr)}</div>
      <div class="mult-label">${esc(heroLabel)}</div>
    </div>`;

  const buyMcap = fmtMcap(s.market_cap);
  const curMcap = fmtMcap(s.current_market_cap);

  // Detail grid: token amount, unit price, holdings, uPnL.
  const upnl = s.upnl;
  const upnlCls = (upnl == null) ? 'dim' : (upnl >= 0 ? 'green' : 'red');
  const holdsStr = (s.holds_amount != null || s.holds_pct != null)
    ? (fmtAmount(s.holds_amount) + (s.holds_pct != null ? ` (${fmtPct(s.holds_pct)})` : ''))
    : '—';
  const meta = `<div class="ray-meta">
      <div class="ray-meta-item">
        <span class="rk">${esc(t('amount'))}</span>
        <span class="rv">${esc(fmtAmount(s.token_amount))}</span>
      </div>
      <div class="ray-meta-item">
        <span class="rk">${esc(t('price'))}</span>
        <span class="rv">${esc(fmtPrice(s.price))}</span>
      </div>
      <div class="ray-meta-item">
        <span class="rk">${esc(t('holds'))}</span>
        <span class="rv">${esc(holdsStr)}</span>
      </div>
      <div class="ray-meta-item">
        <span class="rk">${esc(t('upnl'))}</span>
        <span class="rv ${upnlCls}">${esc(upnl == null ? '—' : fmtUsdSigned(upnl))}</span>
      </div>
    </div>`;

  // Trade links — fixed order: GMGN, Axiom, Solscan, then X live search, then the tx.
  const trade = [];
  const gmgnUrl = safeUrl(links.gmgn);
  const axiomUrl = safeUrl(links.axiom);
  const solscanUrl = safeUrl(links.solscan);
  if (gmgnUrl) trade.push(`<a href="${esc(gmgnUrl)}" target="_blank" rel="noopener">GMGN</a>`);
  if (axiomUrl) trade.push(`<a class="axiom" href="${esc(axiomUrl)}" target="_blank" rel="noopener">Axiom</a>`);
  if (solscanUrl) trade.push(`<a href="${esc(solscanUrl)}" target="_blank" rel="noopener">Solscan</a>`);
  const xSearchUrl = s.ca
    ? 'https://x.com/search?q=' + encodeURIComponent(s.ca) + '&src=typed_query&f=live'
    : '';
  if (xSearchUrl) trade.push(`<a class="xsearch" href="${esc(xSearchUrl)}" target="_blank" rel="noopener" title="${esc(t('cardSearchXTitle'))}"><span class="x-ico" aria-hidden="true">𝕏</span>${esc(t('cardSearchX'))}</a>`);
  const txUrl = safeUrl(s.tx_url);
  if (txUrl) trade.push(`<a class="ray-tx" href="${esc(txUrl)}" target="_blank" rel="noopener" title="${esc(t('txTitle'))}">TX</a>`);

  const caAttr = s.ca ? esc(s.ca) : '';
  const caInner = s.ca
    ? `<span class="ca-copy" title="${esc(t('caCopyTitle'))}" data-ca="${caAttr}"
              onclick="copyCA(this)">${esc(shortCA(s.ca))}</span>`
    : `<span class="ca-copy dim">—</span>`;

  const seenStr = s.seen ? ` · ${esc(t('seen', { age: s.seen }))}` : '';

  // Per-card "mute sound for this token" toggle, keyed on the CA.
  const mkey = muteKeyFor(s);
  const muted = !!mkey && state.mutedSignals.has(mkey);
  const muteBtn = `<button class="bull-mute ${muted ? 'muted' : ''}" type="button"
        data-mute-key="${esc(mkey)}"
        title="${esc(muted ? t('unmuteTitle') : t('muteTitle'))}"
        aria-pressed="${muted ? 'true' : 'false'}"
        onclick="toggleSignalMute(this)">${muted ? '🔕' : '🔔'}</button>`;

  return `
  <div class="card bull-card ray-card ${isFresh ? 'fresh' : ''} ${muted ? 'signal-muted' : ''}" data-ca="${caAttr}">
    <div class="card-head">
      <div class="token-icon">${iconHtml}</div>
      <div class="head-main">
        <div class="token-name" title="${name}">${name}</div>
        <div class="symbol-row">
          <span class="token-symbol">$${symbol}</span>
          ${platformTag}
          <span class="socials">${socials.join('')}</span>
        </div>
      </div>
      <div class="head-actions">
        ${badge}
        ${muteBtn}
      </div>
    </div>

    ${walletRow}
    ${hero}

    <div class="stats-row">
      <div class="stat-box">
        <div class="k">${esc(t('mcapBuy'))}</div>
        <div class="v ${s.market_cap ? '' : 'dim'}">${buyMcap}</div>
      </div>
      <div class="stat-box">
        <div class="k">${esc(t('mcapNow'))}</div>
        <div class="v ${s.current_market_cap ? 'green' : 'dim'}" data-cur-mcap>${curMcap}</div>
      </div>
    </div>

    ${meta}

    <div class="card-foot">
      <div>
        ${caInner}
        <div class="time" title="${esc(s.created_at_iso || '')}">⏱ ${timeAgo(s.created_at)}${seenStr}</div>
      </div>
      <div class="trade">${trade.join('')}</div>
    </div>
  </div>`;
}

function renderError(kind) {
  state.view = 'error';
  state.errorKind = kind || null;
  state.lastSignals = [];
  els.empty.classList.remove('hidden');
  const msg = kind ? t('errorMsgKind', { kind: String(kind) }) : t('errorMsg');
  els.empty.innerHTML = '<div class="big">⚠️</div><div>' + esc(msg) + '</div>';
  els.grid.innerHTML = '';
  els.pager.classList.add('hidden');
}

/* ----------------------------- polling ----------------------------- */
function startPolling() {
  stopPolling();
  setLive(true);
  state.pollTimer = setInterval(() => {
    loadSignals(false);
    loadStats();
  }, POLL_MS);
}
function stopPolling() {
  if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
}

/* ----------------------------- live stream (SSE) ----------------------------- */
function startStream() {
  if (typeof EventSource === 'undefined') return;
  if (state.es && state.es.readyState !== EventSource.CLOSED) return;
  try {
    const es = new EventSource(STREAM_URL);
    state.es = es;
    es.addEventListener('signal', () => {
      loadSignals(false);
      loadStats();
    });
    es.onerror = () => {
      if (state.es && state.es.readyState === EventSource.CLOSED) {
        try { state.es.close(); } catch (e) { /* noop */ }
        state.es = null;
      }
    };
  } catch (e) {
    state.es = null;
  }
}
function stopStream() {
  if (state.es) {
    try { state.es.close(); } catch (e) { /* noop */ }
    state.es = null;
  }
}
function setLive(on) {
  if (!els.live) return;
  els.live.classList.toggle('paused', !on);
  const lbl = els.live.querySelector('.label');
  if (lbl) lbl.textContent = on ? t('live') : t('paused');
}
function markUpdated() {
  state.lastUpdatedAt = new Date();
  refreshUpdatedLabel();
}
function refreshUpdatedLabel() {
  if (!els.updated) return;
  els.updated.textContent = state.lastUpdatedAt
    ? t('updatedAt', { time: state.lastUpdatedAt.toLocaleTimeString() })
    : t('updatedDash');
}

/* ----------------------------- sound ----------------------------- */
function initSound() {
  try {
    state.muted = localStorage.getItem(MUTE_KEY) === '1';
  } catch (e) {
    state.muted = false;  // private mode / storage disabled — default to unmuted
  }
  reflectSound();
}
function reflectSound() {
  if (!els.sound) return;
  els.sound.classList.toggle('on', !state.muted);
  els.sound.classList.toggle('off', state.muted);
  els.sound.innerHTML = state.muted
    ? '🔇 <span>' + esc(t('soundOff')) + '</span>'
    : '🔊 <span>' + esc(t('soundOn')) + '</span>';
  els.sound.title = state.muted ? t('soundTitleOff') : t('soundTitleOn');
}
function toggleSound() {
  state.muted = !state.muted;
  try {
    localStorage.setItem(MUTE_KEY, state.muted ? '1' : '0');
  } catch (e) { /* storage disabled — the mute still holds in-memory */ }
  reflectSound();
  if (state.muted) {
    stopBackgroundAudioKeepAlive();
  } else {
    ensureAudio();
    ensureVoice();
    playChime();
    if (document.hidden) startBackgroundAudioKeepAlive();
    // Un-muting is a gesture too: speak now if today's count already sits at/over an
    // un-voiced cap (the reminder was held back while muted).
    updateMyselfCount(state.myselfCount);
  }
}
function ensureAudio() {
  try {
    if (!state.audioCtx) {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (AC) state.audioCtx = new AC();
    }
    if (state.audioCtx && state.audioCtx.state === 'suspended') state.audioCtx.resume();
  } catch (e) { /* audio unavailable — silently degrade */ }
}

/* Prime window.speechSynthesis from inside a user gesture so the soft/hard reminders
 * actually speak. The Speech API needs its OWN gesture-time warmup or a later reminder fired
 * from a poll/SSE callback is silently dropped (no error) by browsers that gate speech on
 * user activation. Fully guarded and a silent no-op where speech is unavailable. */
function ensureVoice() {
  try {
    const synth = window.speechSynthesis;
    if (!synth) return;
    try { if (synth.paused) synth.resume(); } catch (e) { /* ignore */ }
    try { if (typeof synth.getVoices === 'function') synth.getVoices(); } catch (e) { /* ignore */ }
    if (typeof SpeechSynthesisUtterance !== 'undefined') {
      const warm = new SpeechSynthesisUtterance(' ');
      warm.volume = 0;
      synth.speak(warm);
    }
    state.voiceUnlocked = true;
  } catch (e) { /* speech unavailable — silently degrade */ }
}

/* The DISTINCT HandsOff chime: a bright SAWTOOTH rising arpeggio (A5, E6, A6). Returns true
 * only when (almost certainly) audible, so a hidden tab can fall back to a guaranteed
 * on-return chime. */
function playChime() {
  ensureAudio();
  const ctx = state.audioCtx;
  if (!ctx) return false;
  const audible = ctx.state === 'running';
  if (document.hidden && !audible) return false;
  try {
    const now = ctx.currentTime;
    [[880, 0], [1318.5, 0.08], [1760, 0.16]].forEach(([freq, off]) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'sawtooth';
      osc.frequency.value = freq;
      const tt = now + off;
      gain.gain.setValueAtTime(0.0001, tt);
      gain.gain.exponentialRampToValueAtTime(0.14, tt + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, tt + 0.18);
      osc.connect(gain).connect(ctx.destination);
      osc.start(tt);
      osc.stop(tt + 0.20);
    });
  } catch (e) { return false; }
  return audible;
}

/* Background audio keep-alive — loop a truly-silent WAV through an <audio> element while the
 * tab is hidden so the poll timer stays prompt and the chime/voice fire on time. Desktop
 * only; torn down on return or mute. */
let _keepAliveEl = null;
let _silentUrl = null;

function isMobileDevice() {
  try {
    const uad = navigator.userAgentData;
    if (uad && typeof uad.mobile === 'boolean') return uad.mobile;
  } catch (e) { /* fall through */ }
  const ua = navigator.userAgent || '';
  if (/Macintosh/.test(ua) && (navigator.maxTouchPoints || 0) > 1) return true;
  const touch = (navigator.maxTouchPoints || 0) > 0 || 'ontouchstart' in window;
  return touch && /Mobi|Android|iPhone|iPad|iPod|Windows Phone/i.test(ua);
}

function silentWavUrl() {
  if (_silentUrl) return _silentUrl;
  try {
    const sr = 8000, n = Math.floor(sr * 0.2), bytes = 44 + n * 2;
    const dv = new DataView(new ArrayBuffer(bytes));
    const put = (off, s) => { for (let i = 0; i < s.length; i++) dv.setUint8(off + i, s.charCodeAt(i)); };
    put(0, 'RIFF'); dv.setUint32(4, bytes - 8, true); put(8, 'WAVE');
    put(12, 'fmt '); dv.setUint32(16, 16, true);
    dv.setUint16(20, 1, true);
    dv.setUint16(22, 1, true);
    dv.setUint32(24, sr, true);
    dv.setUint32(28, sr * 2, true);
    dv.setUint16(32, 2, true);
    dv.setUint16(34, 16, true);
    put(36, 'data'); dv.setUint32(40, n * 2, true);
    _silentUrl = URL.createObjectURL(new Blob([dv.buffer], { type: 'audio/wav' }));
    return _silentUrl;
  } catch (e) { return null; }
}

function startBackgroundAudioKeepAlive() {
  if (state.muted) return;
  if (isMobileDevice()) return;
  ensureAudio();
  try {
    if (!_keepAliveEl) {
      const url = silentWavUrl();
      if (!url) return;
      _keepAliveEl = new Audio(url);
      _keepAliveEl.loop = true;
      _keepAliveEl.preload = 'auto';
    }
    const p = _keepAliveEl.play();
    if (p && typeof p.catch === 'function') p.catch(() => { /* autoplay gate */ });
  } catch (e) { /* keep-alive unavailable */ }
}

function stopBackgroundAudioKeepAlive() {
  if (_keepAliveEl) { try { _keepAliveEl.pause(); } catch (e) { /* ignore */ } }
}

/* ----------------------------- helpers ----------------------------- */
function todayLocal() {
  const d = new Date();
  if (isNaN(d.getTime())) return '';
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

function initDateFilter() {
  const today = todayLocal();
  if (!today) return;
  state.date = today;
  state.autoDate = true;
  if (els.date) {
    els.date.value = today;
    els.date.max = today;
  }
}

function maybeRollDate() {
  if (!state.autoDate) return false;
  const today = todayLocal();
  if (!today || today === state.date) return false;
  state.date = today;
  state.page = 1;
  if (els.date) {
    els.date.value = today;
    els.date.max = today;
  }
  return true;
}

function esc(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function safeUrl(u) {
  if (!u) return '';
  const s = String(u).trim();
  return /^https?:\/\//i.test(s) ? s : '';
}
function shortCA(ca) {
  if (!ca) return '—';
  return ca.length > 13 ? ca.slice(0, 6) + '…' + ca.slice(-4) : ca;
}
function fmtMcap(n) {
  if (n == null || isNaN(n) || n <= 0) return '—';
  if (n >= 1e9) return '$' + (n / 1e9).toFixed(2) + 'B';
  if (n >= 1e6) return '$' + (n / 1e6).toFixed(2) + 'M';
  if (n >= 1e3) return '$' + (n / 1e3).toFixed(1) + 'K';
  return '$' + Math.round(n).toLocaleString();
}
function fmtUsd(n) {
  if (n == null || isNaN(n)) return '—';
  if (n >= 1e6) return '$' + (n / 1e6).toFixed(2) + 'M';
  if (n >= 1e3) return '$' + (n / 1e3).toFixed(2) + 'K';
  return '$' + n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function fmtUsdSigned(n) {
  const v = Number(n);
  if (!Number.isFinite(v)) return '—';
  return (v < 0 ? '-' : '+') + fmtUsd(Math.abs(v));
}
function fmtSol(n) {
  const v = Number(n);
  if (!Number.isFinite(v)) return '—';
  let s = v >= 100 ? v.toFixed(2) : v.toFixed(3);
  s = s.replace(/\.?0+$/, '');
  return s + ' SOL';
}
function fmtAmount(n) {
  if (n == null || isNaN(n) || n < 0) return '—';
  if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(2) + 'K';
  return Math.round(n).toLocaleString();
}
function fmtPrice(n) {
  const v = Number(n);
  if (!Number.isFinite(v) || v <= 0) return '—';
  return '$' + v.toLocaleString('en-US', { maximumSignificantDigits: 4 });
}
function fmtPct(n) {
  const v = Number(n);
  if (!Number.isFinite(v) || v <= 0) return '—';
  return v.toFixed(1).replace(/\.0$/, '') + '%';
}
function fmtInt(n) {
  if (n == null || isNaN(n)) return '0';
  return Number(n).toLocaleString();
}
function timeAgo(epoch) {
  if (!epoch) return t('timeNone');
  const s = Math.max(0, Math.floor(Date.now() / 1000) - epoch);
  if (s < 60) return t('timeSecAgo', { n: s });
  if (s < 3600) return t('timeMinAgo', { n: Math.floor(s / 60) });
  if (s < 86400) return t('timeHourAgo', { n: Math.floor(s / 3600) });
  return t('timeDayAgo', { n: Math.floor(s / 86400) });
}
function copyCA(el) {
  const ca = el.getAttribute('data-ca');
  if (!ca) return;
  const done = () => showToast(t('toastCopied'));
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(ca).then(done).catch(() => fallbackCopy(ca, done));
  } else {
    fallbackCopy(ca, done);
  }
}
function fallbackCopy(text, cb) {
  try {
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    document.execCommand('copy'); document.body.removeChild(ta); cb();
  } catch (e) { showToast(t('toastCopyFailed')); }
}
let _toastTimer = null;
function showToast(msg) {
  if (!els.toast) return;
  els.toast.textContent = msg;
  els.toast.classList.add('show');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => els.toast.classList.remove('show'), 1800);
}

window.copyCA = copyCA;
window.toggleSignalMute = toggleSignalMute;
