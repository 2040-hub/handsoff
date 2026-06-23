/* HandsOff (再买剁手) — theme manager.
 *
 * Applies the saved/preferred theme ASAP (before first paint) so the page never
 * flashes the wrong theme. Load this in <head>, BEFORE the stylesheet. After the
 * DOM is ready, call Theme.bindToggle(buttonEl); the button glyph is set by JS to
 * 🌙 (dark active) or ☀️ (light active) and never relies on CSS to swap icons.
 *
 * Uses its own localStorage key so HandsOff keeps an independent theme choice from
 * any other app on the same origin. Default is DARK (the tech look) unless the user
 * picked light before, or the OS prefers light. */
(function (global) {
  'use strict';
  const LS_KEY = 'handsoff_theme';

  function detect() {
    try {
      const saved = localStorage.getItem(LS_KEY);
      if (saved === 'dark' || saved === 'light') return saved;
    } catch (e) { /* private mode — fall through to OS / default */ }
    try {
      if (global.matchMedia && global.matchMedia('(prefers-color-scheme: light)').matches) {
        return 'light';
      }
    } catch (e) { /* matchMedia unavailable — default dark */ }
    return 'dark';
  }

  function apply(theme) {
    const root = document.documentElement;
    root.setAttribute('data-theme', theme);
    // Keep native form controls (date picker, scrollbars) in step with the theme.
    root.style.colorScheme = theme;
  }

  const Theme = {
    current: detect(),
    _buttons: [],

    init() { apply(this.current); },

    set(theme) {
      if (theme !== 'dark' && theme !== 'light') return;
      this.current = theme;
      apply(theme);
      try { localStorage.setItem(LS_KEY, theme); } catch (e) { /* ignore */ }
      this._refreshButtons();
      document.dispatchEvent(new CustomEvent('themechange', { detail: { theme } }));
    },

    toggle() {
      this.set(this.current === 'dark' ? 'light' : 'dark');
    },

    _refreshButtons() {
      const icon = this.current === 'dark' ? '🌙' : '☀️';
      this._buttons.forEach((b) => { if (b) b.textContent = icon; });
    },

    bindToggle(btn) {
      if (!btn) return;
      if (this._buttons.indexOf(btn) === -1) this._buttons.push(btn);
      btn.textContent = this.current === 'dark' ? '🌙' : '☀️';
      const self = this;
      btn.addEventListener('click', function (e) {
        e.preventDefault();
        self.toggle();
      });
    }
  };

  // Apply immediately (we're in <head>, before the stylesheet renders).
  Theme.init();
  global.Theme = Theme;
})(window);
