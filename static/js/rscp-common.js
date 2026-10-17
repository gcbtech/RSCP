/*
 * Shared front-end helpers. Load in <head> so inline scripts can use them.
 */
(function () {
    'use strict';

    var HTML_ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

    /**
     * Escape a value for interpolation into HTML (text or quoted attribute).
     * Use for EVERY server- or user-supplied value placed in innerHTML:
     * item names, SKUs, device names, notes, error text.
     */
    window.escapeHtml = function (value) {
        return String(value === null || value === undefined ? '' : value)
            .replace(/[&<>"']/g, function (c) { return HTML_ESCAPES[c]; });
    };
})();
