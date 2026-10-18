/* Receiving list pages (expected, past_due, open_returns, refunded):
 * legacy device dark-mode flag set on /settings. */
(function () {
    'use strict';
    if (localStorage.getItem('rscp_darkmode') === 'true') {
        document.body.style.backgroundColor = '#121212';
        document.body.style.color = '#e0e0e0';
    }
})();
