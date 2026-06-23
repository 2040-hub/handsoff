/* HandsOff (再买剁手) — i18n manager.
 *
 * A tiny string table with {var} interpolation, a data-i18n /
 * data-i18n-placeholder / data-i18n-title / data-i18n-doctitle static applier, and
 * a persisted language choice. Supported languages: English (en) and Chinese (zh).
 * Uses its own localStorage key ('handsoff_lang') so the choice is independent of
 * any other app on the same origin.
 *
 * Proper nouns (GMGN, Axiom, Solscan, Helius, SOL) are intentionally left
 * untranslated. Bump the ?v=N on the <script> in discipline.html together with the
 * other assets whenever this file changes. */
(function (global) {
  'use strict';
  const STRINGS = {
    en: {
      /* ---- topbar ---- */
      pageTitle: 'HandsOff · Buy Discipline',
      brandTitle: 'HandsOff',
      brandSub: 'on-chain buy-discipline guard',
      live: 'Live',
      paused: 'Paused',
      liveTitle: 'Live streaming',
      statBuys: 'Buys',
      statTokens: 'Tokens',
      statWallets: 'Wallets',
      todayTitle: "Today's totals across your wallets",
      soundOn: 'Sound',
      soundOff: 'Muted',
      soundTitleOn: 'Sound on — click to mute',
      soundTitleOff: 'Sound off — click to enable',
      themeTitle: 'Toggle light / dark theme',
      langTitle: 'Language',

      /* ---- toolbar ---- */
      searchPlaceholder: 'Search name, symbol, contract, or wallet…',
      fieldDate: 'Date',
      fieldSort: 'Sort',
      dateClearTitle: 'Clear date',
      sortNewest: 'Newest first',
      sortOldest: 'Oldest first',
      sortAmountDesc: 'Biggest buy',
      sortMcapDesc: 'Market cap: high → low',
      sortMcapAsc: 'Market cap: low → high',

      /* ---- self-discipline panel (your wallets, today's combined buys) ---- */
      myselfToday: 'Today',
      myselfTitle: 'Your wallets — today’s buys (open on GMGN)',
      myselfPanelTitle: '{name} — today’s buys · open on GMGN',
      myselfLimits: 'soft {soft} · hard {hard}',
      myselfSoftOnly: 'soft {soft}',
      myselfHardOnly: 'hard {hard}',
      voiceSoft: 'Heads up. You have reached today’s soft buy limit. Trade with discipline.',
      voiceHard: 'Stop. You have reached today’s hard buy limit. Hands off the wallet.',
      toastSoft: '⚠️ Soft buy limit reached — stay disciplined',
      toastHard: '🛑 Hard buy limit reached — hands off!',

      /* ---- states / footer ---- */
      loading: 'Loading buys…',
      empty: 'No buys yet. A card appears for each buy from your monitored wallets.',
      footerSuffix: 'one card per wallet buy, grouped by token',
      errorMsg: 'Could not load buys. Retrying…',
      errorMsgKind: 'Could not load buys ({kind}). Retrying…',
      updatedDash: '—',
      updatedAt: 'updated {time}',

      /* ---- pager ---- */
      pageFirst: '« First',
      pagePrev: '‹ Prev',
      pageNext: 'Next ›',
      pageLast: 'Last »',
      pageFirstTitle: 'First page',
      pagePrevTitle: 'Previous page',
      pageNextTitle: 'Next page',
      pageLastTitle: 'Last page',
      pageInfo: 'Page {page} / {total}',

      /* ---- card ---- */
      buyBadge: 'Buy #{n}',
      buy: 'BUY',
      heroLabel: '{usd} · BUY',
      mcapBuy: 'MCap @ Buy',
      mcapNow: 'Current MCap',
      amount: 'Amount',
      price: 'Price',
      holds: 'Holds',
      upnl: 'uPnL',
      platform: 'Platform',
      walletTitle: 'View wallet on GMGN',
      unknownWallet: 'Wallet',
      unknownToken: 'Unknown',
      seen: 'seen {age}',
      txTitle: 'View transaction on Solscan',
      caCopyTitle: 'Click to copy CA',
      socialTwitter: 'Twitter / X',
      socialWebsite: 'Website',
      cardSearchX: 'Search',
      cardSearchXTitle: 'Search this contract on X (live)',

      /* ---- toasts / time ---- */
      toastNew: '🪓 New buy',
      toastNewOne: '🪓 {n} new buy',
      toastNewMany: '🪓 {n} new buys',
      muteTitle: 'Mute sound alerts for this token',
      unmuteTitle: 'Unmute sound alerts for this token',
      toastMuted: '🔕 Muted sound for this token',
      toastUnmuted: '🔔 Restored sound for this token',
      toastCopied: '📋 CA copied',
      toastCopyFailed: 'Copy failed',
      timeNone: '—',
      timeSecAgo: '{n}s ago',
      timeMinAgo: '{n}m ago',
      timeHourAgo: '{n}h ago',
      timeDayAgo: '{n}d ago',

      /* ---- wallet login (only when [WEB] enable_auth = true) ---- */
      walletChipTitle: 'Signed-in wallet',
      logout: 'Logout',
      loginPageTitle: 'HandsOff — Sign in',
      loginBrand: 'HandsOff',
      loginSub: 'Connect a whitelisted Solana wallet to continue',
      loginDetecting: 'Detecting wallets…',
      loginNote: 'Signing proves wallet ownership. <b>No transaction is sent</b> and no funds move. Only whitelisted wallets can access HandsOff.',
      walletDetected: 'Detected',
      walletOpenApp: 'Open app ↗',
      walletNotInstalled: 'Not installed',
      noWalletExt: 'No Solana wallet extension detected. Install Phantom, Solflare, or Backpack and reload this page.',
      tapWalletMobile: 'Tap a wallet to open this page inside its in-app browser, then sign in.',
      walletNotDetected: '{name} is not detected. Install its browser extension, then reload.',
      connectingTo: 'Connecting to {name}…',
      requestingChallenge: 'Requesting sign-in challenge…',
      approveSignature: 'Approve the signature request in {name}…',
      verifying: 'Verifying…',
      signedIn: 'Signed in. Redirecting…',
      notWhitelisted: '⛔ This wallet is not whitelisted for HandsOff.',
      verifyFailedPrefix: '❌ ',
      loginFailed: '❌ Login failed: {msg}',
      sigRejected: 'Signature request was rejected.',
      sigFailed: '❌ Could not sign the message with this wallet.',
      hzInvalidWallet: 'invalid wallet address',
      hzNonceExpired: 'challenge expired — try again',
      hzInvalidSignature: 'signature did not verify',
      hzNotWhitelisted: 'wallet not whitelisted',
      hzServerBusy: 'server busy — try again shortly',
      hzMissingParams: 'missing parameters',
      hzNonceFailed: 'could not start sign-in — try again',
      hzVerifyFailed: 'verification failed',
    },
    zh: {
      /* ---- 顶栏 ---- */
      pageTitle: '再买剁手 · 买入自律',
      brandTitle: '再买剁手',
      brandSub: '链上买入自律卫士',
      live: '实时',
      paused: '已暂停',
      liveTitle: '实时推送',
      statBuys: '买入',
      statTokens: '代币',
      statWallets: '钱包',
      todayTitle: '今日全部钱包数据',
      soundOn: '声音',
      soundOff: '已静音',
      soundTitleOn: '声音已开启 — 点击静音',
      soundTitleOff: '声音已关闭 — 点击开启',
      themeTitle: '切换明亮 / 黑暗模式',
      langTitle: '语言',

      /* ---- 工具栏 ---- */
      searchPlaceholder: '搜索名称、符号、合约或钱包…',
      fieldDate: '日期',
      fieldSort: '排序',
      dateClearTitle: '清除日期',
      sortNewest: '最新优先',
      sortOldest: '最旧优先',
      sortAmountDesc: '买入最大',
      sortMcapDesc: '市值：高 → 低',
      sortMcapAsc: '市值：低 → 高',

      /* ---- 自律面板（你的钱包今日合计买入） ---- */
      myselfToday: '今日',
      myselfTitle: '你的钱包 — 今日买入次数（在 GMGN 打开）',
      myselfPanelTitle: '{name} — 今日买入次数 · 在 GMGN 打开',
      myselfLimits: '软 {soft} · 硬 {hard}',
      myselfSoftOnly: '软 {soft}',
      myselfHardOnly: '硬 {hard}',
      voiceSoft: '注意，今日买入次数已达软限制，请保持理性交易。',
      voiceHard: '停手！今日买入次数已达硬限制，请立刻收手，别再剁手了。',
      toastSoft: '⚠️ 已达软限制——请保持理性',
      toastHard: '🛑 已达硬限制——快收手！',

      /* ---- 状态 / 页脚 ---- */
      loading: '正在加载买入…',
      empty: '暂无买入。你监控的钱包每发生一笔买入就会生成一张卡片。',
      footerSuffix: '每笔钱包买入一张卡片，按代币分组',
      errorMsg: '无法加载买入，正在重试…',
      errorMsgKind: '无法加载买入（{kind}），正在重试…',
      updatedDash: '—',
      updatedAt: '更新于 {time}',

      /* ---- 分页 ---- */
      pageFirst: '« 首页',
      pagePrev: '‹ 上一页',
      pageNext: '下一页 ›',
      pageLast: '末页 »',
      pageFirstTitle: '第一页',
      pagePrevTitle: '上一页',
      pageNextTitle: '下一页',
      pageLastTitle: '最后一页',
      pageInfo: '第 {page} / {total} 页',

      /* ---- 卡片 ---- */
      buyBadge: '买入 #{n}',
      buy: '买入',
      heroLabel: '{usd} · 买入',
      mcapBuy: '买入时市值',
      mcapNow: '当前市值',
      amount: '数量',
      price: '价格',
      holds: '持仓',
      upnl: '未实现盈亏',
      platform: '平台',
      walletTitle: '在 GMGN 查看钱包',
      unknownWallet: '钱包',
      unknownToken: '未知代币',
      seen: '发现于 {age}',
      txTitle: '在 Solscan 查看交易',
      caCopyTitle: '点击复制 CA',
      socialTwitter: 'Twitter / X',
      socialWebsite: '官网',
      cardSearchX: '搜索',
      cardSearchXTitle: '在 X 上实时搜索该合约',

      /* ---- 提示 / 时间 ---- */
      toastNew: '🪓 新买入',
      toastNewOne: '🪓 {n} 笔新买入',
      toastNewMany: '🪓 {n} 笔新买入',
      muteTitle: '屏蔽该代币的声音提醒',
      unmuteTitle: '恢复该代币的声音提醒',
      toastMuted: '🔕 已屏蔽该代币声音',
      toastUnmuted: '🔔 已恢复该代币声音',
      toastCopied: '📋 已复制 CA',
      toastCopyFailed: '复制失败',
      timeNone: '—',
      timeSecAgo: '{n} 秒前',
      timeMinAgo: '{n} 分钟前',
      timeHourAgo: '{n} 小时前',
      timeDayAgo: '{n} 天前',

      /* ---- 钱包登录（仅当 [WEB] enable_auth = true 时） ---- */
      walletChipTitle: '已登录钱包',
      logout: '退出登录',
      loginPageTitle: '再买剁手 — 登录',
      loginBrand: '再买剁手',
      loginSub: '连接白名单 Solana 钱包以继续',
      loginDetecting: '正在检测钱包…',
      loginNote: '签名用于证明钱包归属，<b>不会产生任何链上交易</b>，也不会转移任何资金。只有白名单中的钱包才能访问「再买剁手」。',
      walletDetected: '已检测到',
      walletOpenApp: '打开 App ↗',
      walletNotInstalled: '未安装',
      noWalletExt: '未检测到 Solana 钱包扩展。请安装 Phantom、Solflare 或 Backpack 后重新加载本页。',
      tapWalletMobile: '点击任一钱包，在其内置浏览器中打开本页面，然后登录。',
      walletNotDetected: '未检测到 {name}。请安装其浏览器扩展后重新加载。',
      connectingTo: '正在连接 {name}…',
      requestingChallenge: '正在请求登录挑战…',
      approveSignature: '请在 {name} 中批准签名请求…',
      verifying: '正在验证…',
      signedIn: '登录成功，正在跳转…',
      notWhitelisted: '⛔ 该钱包不在「再买剁手」白名单中。',
      verifyFailedPrefix: '❌ ',
      loginFailed: '❌ 登录失败：{msg}',
      sigRejected: '签名请求已被拒绝。',
      sigFailed: '❌ 无法使用该钱包完成签名。',
      hzInvalidWallet: '钱包地址无效',
      hzNonceExpired: '挑战已过期 — 请重试',
      hzInvalidSignature: '签名验证未通过',
      hzNotWhitelisted: '钱包不在白名单',
      hzServerBusy: '服务器繁忙 — 请稍后重试',
      hzMissingParams: '缺少参数',
      hzNonceFailed: '无法开始登录 — 请重试',
      hzVerifyFailed: '验证失败',
    }
  };

  const LS_KEY = 'handsoff_lang';

  function detect() {
    try {
      const saved = localStorage.getItem(LS_KEY);
      if (saved && STRINGS[saved]) return saved;
    } catch (e) { /* private mode — fall through */ }
    const nav = ((global.navigator && global.navigator.language) || 'en').toLowerCase();
    return nav.startsWith('zh') ? 'zh' : 'en';
  }

  const I18n = {
    lang: detect(),

    /** Translate `key`, substituting {vars}. Falls back to en, then the key. */
    t(key, vars) {
      const dict = STRINGS[this.lang] || STRINGS.en;
      let s = dict[key];
      if (s == null) s = STRINGS.en[key];
      if (s == null) s = key;
      if (vars) {
        Object.keys(vars).forEach((k) => {
          s = s.replace(new RegExp('\\{' + k + '\\}', 'g'), String(vars[k]));
        });
      }
      return s;
    },

    setLang(l) {
      if (!STRINGS[l]) return;
      this.lang = l;
      try { localStorage.setItem(LS_KEY, l); } catch (e) { /* ignore */ }
      document.dispatchEvent(new CustomEvent('langchange', { detail: { lang: l } }));
    },

    /** Apply translations to all [data-i18n*] elements currently in the DOM. */
    applyStaticText() {
      document.querySelectorAll('[data-i18n]').forEach((el) => {
        el.textContent = this.t(el.getAttribute('data-i18n'));
      });
      document.querySelectorAll('[data-i18n-placeholder]').forEach((el) => {
        el.placeholder = this.t(el.getAttribute('data-i18n-placeholder'));
      });
      // data-i18n-html allows the small set of strings carrying trusted, AUTHOR-
      // CONTROLLED inline markup (e.g. <b> in loginNote) to keep their formatting.
      // Values come ONLY from the STRINGS table above (never user data), so assigning
      // innerHTML here introduces no injection surface.
      document.querySelectorAll('[data-i18n-html]').forEach((el) => {
        el.innerHTML = this.t(el.getAttribute('data-i18n-html'));
      });
      document.querySelectorAll('[data-i18n-title]').forEach((el) => {
        el.title = this.t(el.getAttribute('data-i18n-title'));
      });
      // Keep <html lang> + document title in step for accessibility / SEO.
      try {
        document.documentElement.lang = this.lang === 'zh' ? 'zh-CN' : 'en';
        const titleKey = document.body && document.body.getAttribute('data-i18n-doctitle');
        if (titleKey) document.title = this.t(titleKey);
      } catch (e) { /* ignore */ }
    }
  };

  global.I18n = I18n;
})(window);
