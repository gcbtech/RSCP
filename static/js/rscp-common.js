/*
 * Shared front-end helpers. Loaded in <head> by templates/base.html (and by
 * the few standalone pages), so page scripts can use them.
 *
 * Pages carry NO inline JavaScript (the Content-Security-Policy forbids it):
 *
 *   Page code    static/js/pages/<area>-<page>.js, loaded in {% block scripts %}.
 *
 *   Server data  {% block page_data %}
 *                  <script type="application/json" id="page-data">{{ {...}|tojson }}</script>
 *                {% endblock %}
 *                read with RSCP.pageData()  (JSON blocks are data, not code).
 *
 *   Events       instead of onclick="doThing(this)":
 *                  <button data-action="doThing" data-id="{{ item.id }}">
 *                and in the page script:
 *                  RSCP.actions({ doThing: function (el, event) { ... el.dataset.id ... } });
 *                Other events: data-change, data-input, data-submit, data-keyup,
 *                data-keydown, data-focusout (same handler signature). A submit handler may call
 *                event.preventDefault() to stop the submit.
 *
 *   Confirm      data-confirm="Delete this?" on a <form> (asked on submit) or a
 *                button/link (asked on click); cancelling stops the action.
 *
 *   CSRF         RSCP.csrfToken() for fetch headers: { 'X-CSRFToken': RSCP.csrfToken() }
 *
 * Handlers are looked up by name at event time, so actions registered after
 * the markup exists (or for elements added later via innerHTML) still work.
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

    var handlers = {};
    var pageDataCache = {};

    var RSCP = window.RSCP = window.RSCP || {};

    /** Register named event handlers used by data-action / data-change / ... */
    RSCP.actions = function (map) {
        Object.keys(map).forEach(function (name) { handlers[name] = map[name]; });
    };

    /** Parsed JSON from <script type="application/json" id="{id}"> (default "page-data"). */
    RSCP.pageData = function (id) {
        id = id || 'page-data';
        if (!(id in pageDataCache)) {
            var el = document.getElementById(id);
            try {
                pageDataCache[id] = el ? JSON.parse(el.textContent || 'null') : null;
            } catch (e) {
                console.error('[RSCP] invalid page data in #' + id, e);
                pageDataCache[id] = null;
            }
        }
        return pageDataCache[id];
    };

    /** The CSRF token for this session (from <meta name="csrf-token">). */
    RSCP.csrfToken = function () {
        var meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.getAttribute('content') : '';
    };

    function dispatch(attr, event) {
        var el = event.target && event.target.closest ? event.target.closest('[' + attr + ']') : null;
        if (!el) return;
        var name = el.getAttribute(attr);
        var fn = handlers[name];
        if (typeof fn !== 'function') {
            console.warn('[RSCP] no handler registered for ' + attr + '="' + name + '"');
            return;
        }
        fn.call(el, el, event);
    }

    function confirmGate(event) {
        var el = event.target && event.target.closest ? event.target.closest('[data-confirm]') : null;
        if (!el) return true;
        // Forms confirm on submit; buttons/links confirm on click.
        if (event.type === 'click' && el.tagName === 'FORM') return true;
        if (event.type === 'submit' && el.tagName !== 'FORM') return true;
        if (!window.confirm(el.getAttribute('data-confirm'))) {
            event.preventDefault();
            event.stopImmediatePropagation();
            return false;
        }
        return true;
    }

    // Capture phase for the confirm gate so a cancelled confirm stops everything else.
    document.addEventListener('click', confirmGate, true);
    document.addEventListener('submit', confirmGate, true);

    document.addEventListener('click', function (e) { dispatch('data-action', e); });
    document.addEventListener('change', function (e) { dispatch('data-change', e); });
    document.addEventListener('input', function (e) { dispatch('data-input', e); });
    document.addEventListener('submit', function (e) { dispatch('data-submit', e); });
    document.addEventListener('keyup', function (e) { dispatch('data-keyup', e); });
    document.addEventListener('keydown', function (e) { dispatch('data-keydown', e); });
    document.addEventListener('focusout', function (e) { dispatch('data-focusout', e); });
})();
