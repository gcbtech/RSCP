/*
 * Phone UI theme, per device: 'light', 'dark' (default) or 'auto' (match the
 * device). Loaded synchronously in <head> by templates/m/base.html so the
 * right colours are there before first paint. Separate from the desktop's
 * dark-mode keys on purpose.
 */
(function () {
    'use strict';
    var KEY = 'rscp_m_theme';

    function pref() {
        try {
            var p = localStorage.getItem(KEY);
            return p === 'light' || p === 'auto' ? p : 'dark';
        } catch (e) {
            return 'dark';
        }
    }

    function systemDark() {
        return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
    }

    function apply(p) {
        var theme = p === 'auto' ? (systemDark() ? 'dark' : 'light') : p;
        document.documentElement.setAttribute('data-theme', theme);
    }

    function set(p) {
        try { localStorage.setItem(KEY, p); } catch (e) { /* private mode: this page only */ }
        apply(p);
    }

    apply(pref());

    if (window.matchMedia) {
        var mq = window.matchMedia('(prefers-color-scheme: dark)');
        var onChange = function () { if (pref() === 'auto') apply('auto'); };
        if (mq.addEventListener) mq.addEventListener('change', onChange);
        else if (mq.addListener) mq.addListener(onChange);
    }

    window.RSCPTheme = { pref: pref, set: set };
})();
