/* HandsOff (再买剁手) — wallet login.
 *
 * A nonce -> signMessage -> verify flow; the server authorizes by wallet whitelist.
 * Desktop uses the injected wallet extension (Phantom / Solflare / Backpack); mobile
 * offers "open in wallet app" deeplinks so the page runs inside the wallet's in-app
 * browser where window.solana exists.
 *
 * Versioned via ?v=N in login.html — bump together with discipline.js / discipline.css. */
'use strict';

const LOGIN_VERSION = '1.1.1';

/* Short i18n alias — resilient if i18n.js failed to load. */
function t(key, vars) {
  return (window.I18n && window.I18n.t) ? window.I18n.t(key, vars) : key;
}

/* ----------------------------- base58 ----------------------------- */
const B58_ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
function b58encode(bytes) {
  if (!bytes || bytes.length === 0) return '';
  const digits = [0];
  for (let i = 0; i < bytes.length; i++) {
    let carry = bytes[i];
    for (let j = 0; j < digits.length; j++) {
      carry += digits[j] << 8;
      digits[j] = carry % 58;
      carry = (carry / 58) | 0;
    }
    while (carry > 0) { digits.push(carry % 58); carry = (carry / 58) | 0; }
  }
  let str = '';
  for (let k = 0; k < bytes.length && bytes[k] === 0; k++) str += '1';
  for (let q = digits.length - 1; q >= 0; q--) str += B58_ALPHABET[digits[q]];
  return str;
}

/* ----------------------------- providers ----------------------------- */
const PROVIDERS = [
  {
    id: 'phantom', name: 'Phantom', icon: '👻',
    get: () => (window.phantom && window.phantom.solana && window.phantom.solana.isPhantom)
      ? window.phantom.solana
      : (window.solana && window.solana.isPhantom ? window.solana : null),
    deeplink: (u) => `https://phantom.app/ul/browse/${encodeURIComponent(u)}?ref=${encodeURIComponent(u)}`,
  },
  {
    id: 'solflare', name: 'Solflare', icon: '🔥',
    get: () => (window.solflare && window.solflare.isSolflare) ? window.solflare : null,
    deeplink: (u) => `https://solflare.com/ul/v1/browse/${encodeURIComponent(u)}?ref=${encodeURIComponent(u)}`,
  },
  {
    id: 'backpack', name: 'Backpack', icon: '🎒',
    get: () => (window.backpack && window.backpack.isBackpack) ? window.backpack : null,
    deeplink: (u) => `https://backpack.app/ul/v1/browse/${encodeURIComponent(u)}?ref=${encodeURIComponent(u)}`,
  },
];

function isMobile() {
  return /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent || '');
}
function siteURL() {
  return window.location.origin + '/login';
}

/* ----------------------------- DOM ----------------------------- */
const $ = (id) => document.getElementById(id);
let statusEl, listEl;

document.addEventListener('DOMContentLoaded', () => {
  statusEl = $('status');
  listEl = $('wallet-list');
  const v = $('version');
  if (v) v.textContent = 'v' + LOGIN_VERSION;
  initTheme();
  initI18n();
  // If already signed in, go straight to the app.
  checkExistingSession().then((authed) => {
    if (!authed) renderWallets();
  });
});

/* ----------------------------- theme & i18n ----------------------------- */
function initTheme() {
  if (window.Theme) Theme.bindToggle($('theme-toggle'));
}
function initI18n() {
  applyLang();
  const zh = $('lang-zh');
  const en = $('lang-en');
  if (zh) zh.addEventListener('click', () => { if (window.I18n) I18n.setLang('zh'); applyLang(); });
  if (en) en.addEventListener('click', () => { if (window.I18n) I18n.setLang('en'); applyLang(); });
}
function applyLang() {
  if (window.I18n) I18n.applyStaticText();
  const lang = (window.I18n && I18n.lang) || 'en';
  const zh = $('lang-zh');
  const en = $('lang-en');
  if (zh) zh.classList.toggle('active', lang === 'zh');
  if (en) en.classList.toggle('active', lang === 'en');
  // Re-render the wallet list so its JS-built labels switch language too. Skip mid-
  // login so a language tap can't tear down an in-flight sign-in.
  if (listEl && !busy) renderWallets();
}

async function checkExistingSession() {
  try {
    const r = await fetch('/api/auth/me', { cache: 'no-store' });
    if (r.ok) {
      const d = await r.json();
      if (d && d.success && d.wallet) {
        window.location.href = '/';
        return true;
      }
    }
  } catch (e) { /* offline / auth off — just show the wallet list */ }
  return false;
}

/* ----------------------------- rendering ----------------------------- */
function renderWallets() {
  const mobile = isMobile();
  listEl.innerHTML = '';
  let anyProvider = false;

  PROVIDERS.forEach((w) => {
    const provider = safeGet(w);
    const btn = document.createElement('button');
    btn.className = 'wallet-btn';
    btn.type = 'button';

    if (provider) {
      anyProvider = true;
      btn.innerHTML = `<span class="wicon">${w.icon}</span><span class="wname">${esc(w.name)}</span>` +
        `<span class="wdot" title="${esc(t('walletDetected'))}"></span>`;
      btn.addEventListener('click', () => login(w, provider));
    } else if (mobile) {
      btn.innerHTML = `<span class="wicon">${w.icon}</span><span class="wname">${esc(w.name)}</span>` +
        `<span class="wopen">${esc(t('walletOpenApp'))}</span>`;
      btn.addEventListener('click', () => { window.location.href = w.deeplink(siteURL()); });
    } else {
      btn.classList.add('disabled');
      btn.innerHTML = `<span class="wicon">${w.icon}</span><span class="wname">${esc(w.name)}</span>` +
        `<span class="wmiss">${esc(t('walletNotInstalled'))}</span>`;
      btn.addEventListener('click', () => setStatus('warn',
        t('walletNotDetected', { name: w.name })));
    }
    listEl.appendChild(btn);
  });

  if (!anyProvider && !mobile) {
    setStatus('warn', t('noWalletExt'));
  } else if (mobile && !anyProvider) {
    setStatus('', t('tapWalletMobile'));
  }
}

function safeGet(w) {
  try { return w.get(); } catch (e) { return null; }
}

/* ----------------------------- login flow ----------------------------- */
let busy = false;
async function login(w, provider) {
  if (busy) return;
  busy = true;
  setStatus('info', t('connectingTo', { name: w.name }));
  try {
    // 1) connect -> wallet public key (base58)
    const conn = await provider.connect();
    const pk = (conn && conn.publicKey) || provider.publicKey;
    const wallet = pk && pk.toString ? pk.toString() : String(pk || '');
    if (!wallet) throw new Error('no_pubkey');

    // 2) request a nonce + the exact message to sign
    setStatus('info', t('requestingChallenge'));
    const nonceResp = await postJSON('/api/auth/nonce', { wallet });
    if (!nonceResp.ok || !nonceResp.body || !nonceResp.body.success) {
      throw new Error(humanize(nonceResp.body, t('hzNonceFailed')));
    }
    const { nonce, message } = nonceResp.body;

    // 3) sign the message
    setStatus('info', t('approveSignature', { name: w.name }));
    const encoded = new TextEncoder().encode(message);
    let signed;
    try {
      signed = await provider.signMessage(encoded, 'utf8');
    } catch (e) {
      // An explicit user rejection must NOT trigger a second prompt. Only fall back to
      // the no-arg form when the wallet rejected the display-encoding argument itself.
      if (isRejection(e)) throw new Error('signature_rejected');
      try { signed = await provider.signMessage(encoded); }
      catch (e2) {
        if (isRejection(e2)) throw new Error('signature_rejected');
        throw new Error('signature_failed');
      }
    }
    const sigBytes = signed && signed.signature ? signed.signature : signed;
    const signature = b58encode(sigBytes instanceof Uint8Array ? sigBytes : new Uint8Array(sigBytes));

    // 4) verify -> session cookie + redirect
    setStatus('info', t('verifying'));
    const verifyResp = await postJSON('/api/auth/verify', { wallet, nonce, signature });
    if (verifyResp.ok && verifyResp.body && verifyResp.body.success) {
      setStatus('ok', t('signedIn'));
      setTimeout(() => { window.location.href = '/'; }, 350);
      return;
    }
    const err = (verifyResp.body && verifyResp.body.error) || '';
    if (verifyResp.status === 403 || err === 'not_whitelisted') {
      setStatus('error', t('notWhitelisted'));
    } else {
      setStatus('error', t('verifyFailedPrefix') + humanize(verifyResp.body, t('hzVerifyFailed')));
    }
  } catch (e) {
    const msg = (e && e.message) || 'error';
    if (msg === 'signature_rejected') {
      setStatus('warn', t('sigRejected'));
    } else if (msg === 'signature_failed') {
      setStatus('error', t('sigFailed'));
    } else {
      setStatus('error', t('loginFailed', { msg: esc(msg) }));
    }
  } finally {
    busy = false;
  }
}

// True for an explicit user rejection (don't re-prompt). Phantom uses code 4001.
function isRejection(e) {
  if (!e) return false;
  if (e.code === 4001) return true;
  return /reject|denied|declined|cancel/i.test(e.message || '');
}

/* ----------------------------- helpers ----------------------------- */
async function postJSON(url, body) {
  try {
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      cache: 'no-store',
      body: JSON.stringify(body),
    });
    let parsed = null;
    try { parsed = await r.json(); } catch (e) { /* non-JSON */ }
    return { ok: r.ok, status: r.status, body: parsed };
  } catch (e) {
    return { ok: false, status: 0, body: null };
  }
}
function humanize(body, fallback) {
  const e = body && body.error;
  const map = {
    invalid_wallet: t('hzInvalidWallet'),
    invalid_or_expired_nonce: t('hzNonceExpired'),
    invalid_signature: t('hzInvalidSignature'),
    not_whitelisted: t('hzNotWhitelisted'),
    server_busy: t('hzServerBusy'),
    missing_params: t('hzMissingParams'),
  };
  return (e && (map[e] || e)) || fallback;
}
function setStatus(kind, msg) {
  if (!statusEl) return;
  statusEl.className = 'login-status ' + (kind || '');
  statusEl.textContent = msg;
}
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
