/*
 * Phone UI, Receive > Scan (templates/m/receive/scan.html). Plain ES5.
 *
 * Each scan posts to /m/receive/scan; the server renders the result card
 * (templates/m/receive/_result.html) and this script swaps it in, beeps, and
 * adds a line to "This session" (kept per tab in sessionStorage). Scans that
 * arrive while one is being sent wait their turn, in order.
 */
(function () {
    'use strict';

    var PAGE = RSCP.pageData() || {};
    var SESSION_KEY = 'rscp_m_receive_session';
    var SESSION_MAX = 25;

    function byId(id) { return document.getElementById(id); }

    var result = byId('mResult');
    var manual = byId('mScanManual');
    var queue = [];
    var busy = false;

    var scanner = RSCPScanner.attach({
        capture: byId('mScanCapture'),
        status: byId('mScanStatus'),
        fields: [manual],
        onScan: function (code) { queue.push(code); next(); }
    });

    function next() {
        if (busy || !queue.length) return;
        busy = true;
        var code = queue.shift();
        RSCPM.postJSON(PAGE.scanUrl, { code: code })
            .then(show)
            .catch(function (err) {
                RSCPScanner.feedback('bad');
                RSCPM.toast('Scan of ' + code + ' not sent (' + err.message + '). Scan it again.');
            })
            .then(function () { busy = false; next(); });
    }

    function show(data) {
        result.innerHTML = data.html;
        result.hidden = false;
        byId('mScanStatus').classList.add('m-compact');
        var chip = document.querySelector('.m-chip[data-chip="expected"] b');
        if (chip && data.expected) chip.textContent = data.expected;
        RSCPScanner.feedback(data.feedback);
        remember({ kind: data.kind, label: data.label, code: data.code, t: Date.now() });
        window.scrollTo(0, 0);
    }

    // ------------------------------------------------------------ this session

    var LEAD = {
        received: ['m-lead-ok', '<path d="M20 6 9 17l-5-5"/>'],
        priority: ['m-lead-priority', '<path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2L12 17.3 6.4 20.2l1.1-6.2L3 9.6l6.2-.9z"/>'],
        duplicate: ['m-lead-dup', '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1"/>'],
        unknown: ['m-lead-bad', '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>'],
        error: ['m-lead-bad', '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>'],
        inventory: ['m-lead-info', '<rect x="3" y="4" width="18" height="5" rx="1"/><path d="M5 9v10a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V9"/><path d="M10 13h4"/>']
    };

    function loadSession() {
        try { return JSON.parse(sessionStorage.getItem(SESSION_KEY) || '[]'); } catch (e) { return []; }
    }

    function saveSession(list) {
        try { sessionStorage.setItem(SESSION_KEY, JSON.stringify(list)); } catch (e) { /* full or private: list still shows */ }
    }

    function clock(t) {
        var d = new Date(t);
        var h = d.getHours();
        var m = d.getMinutes();
        return ((h % 12) || 12) + ':' + (m < 10 ? '0' : '') + m + (h < 12 ? ' AM' : ' PM');
    }

    function renderSession(list) {
        var box = byId('mSession');
        byId('mSessionCount').textContent = list.length ? '· ' + list.length : '';
        byId('mSessionClear').hidden = !list.length;
        if (!list.length) {
            box.innerHTML = '<p class="m-empty">Scans you make here show up in this list.</p>';
            return;
        }
        box.innerHTML = list.map(function (s) {
            var lead = LEAD[s.kind] || LEAD.error;
            return '<div class="m-row">' +
                '<span class="m-lead m-lead-round ' + lead[0] + '"><svg class="m-ic" viewBox="0 0 24 24" fill="none" ' +
                'stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
                lead[1] + '</svg></span>' +
                '<span class="m-t"><span class="m-t1">' + escapeHtml(s.label) + '</span>' +
                '<span class="m-t2 m-mono">' + escapeHtml(s.code) + '</span></span>' +
                '<span class="m-t2 m-time">' + clock(s.t) + '</span></div>';
        }).join('');
    }

    function remember(entry) {
        var list = loadSession();
        list.unshift(entry);
        list = list.slice(0, SESSION_MAX);
        saveSession(list);
        renderSession(list);
    }

    renderSession(loadSession());

    // ------------------------------------------------------------ received: add to stock / link

    // Once a package's units are in stock, adding or linking again would count them twice
    function settle(note) {
        var actions = result.querySelectorAll('[data-action="mAddStock"], [data-action="mOpenLink"], [data-new-item]');
        for (var i = 0; i < actions.length; i++) actions[i].parentNode.removeChild(actions[i]);
        var rows = result.querySelectorAll('.m-grid2');
        for (var j = 0; j < rows.length; j++) if (!rows[j].children.length) rows[j].parentNode.removeChild(rows[j]);
        var p = document.createElement('p');
        p.className = 'm-match m-match-done';
        p.textContent = note;
        var card = result.querySelector('.m-card');
        if (card) card.appendChild(p);
    }

    var link = { tracking: '', name: '', sku: '', itemName: '' };
    var searchTimer = null;

    function linkResults(html) { byId('mLinkResults').innerHTML = html; }

    function pick(btn) {
        var rows = byId('mLinkResults').querySelectorAll('[data-action="mLinkPick"]');
        for (var i = 0; i < rows.length; i++) rows[i].setAttribute('aria-pressed', rows[i] === btn ? 'true' : 'false');
        link.sku = btn.getAttribute('data-sku');
        link.itemName = btn.getAttribute('data-name');
        byId('mLinkSave').disabled = false;
    }

    function search(autoPick) {
        var q = byId('mLinkQuery').value.trim();
        link.sku = '';
        byId('mLinkSave').disabled = true;
        if (q.length < 2) {
            linkResults('<p class="m-empty">Type at least 2 characters.</p>');
            return;
        }
        fetch(PAGE.searchUrl + '?q=' + encodeURIComponent(q), { credentials: 'same-origin', headers: { Accept: 'application/json' } })
            .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
            .then(function (items) {
                if (byId('mLinkQuery').value.trim() !== q) return;   // a newer search is on its way
                if (!items.length) {
                    linkResults('<p class="m-empty">No stock items match.</p>');
                    return;
                }
                linkResults(items.map(function (it) {
                    return '<button type="button" class="m-row m-pick" data-action="mLinkPick" aria-pressed="false"' +
                        ' data-sku="' + escapeHtml(it.sku) + '" data-name="' + escapeHtml(it.name) + '">' +
                        '<span class="m-t"><span class="m-t1">' + escapeHtml(it.name) + '</span>' +
                        '<span class="m-t2">' + escapeHtml(it.sku) + ' · ' + escapeHtml(it.quantity) + ' in stock</span></span>' +
                        '<span class="m-pick-mark" aria-hidden="true"></span></button>';
                }).join(''));
                if (autoPick && items.length === 1) pick(byId('mLinkResults').querySelector('[data-action="mLinkPick"]'));
            })
            .catch(function () { linkResults('<p class="m-empty">Search failed. Try again.</p>'); });
    }

    byId('mLinkQuery') && byId('mLinkQuery').addEventListener('input', function () {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(function () { search(false); }, 250);
    });

    // A scanned stock item's buttons (- / +, Set count, Out of stock) are static/m/stockcard.js

    // ------------------------------------------------------------ actions

    RSCP.actions({
        mManualScan: function (form, event) {
            event.preventDefault();
            var code = manual.value.trim();
            manual.value = '';
            scanner.focus();
            if (code) { queue.push(code); next(); }
        },
        mTypeCode: function () {
            byId('mScanStatus').classList.remove('m-compact');
            manual.focus();
            byId('mScanStatus').scrollIntoView({ block: 'nearest' });
        },
        mClearSession: function () {
            saveSession([]);
            renderSession([]);
        },

        mAddStock: function (btn) {
            if (btn.disabled) return;
            btn.disabled = true;
            var label = btn.innerHTML;
            btn.textContent = 'Adding…';
            RSCPM.postJSON(PAGE.addStockUrl, {
                item_id: Number(btn.getAttribute('data-item-id')),
                qty: Number(btn.getAttribute('data-qty')),
                tracking: btn.getAttribute('data-tracking')
            }).then(function (data) {
                var shown = result.querySelector('[data-match-qty]');
                if (shown) shown.textContent = data.new_quantity;
                settle('Added ' + btn.getAttribute('data-qty') + ' to ' + data.name + ' · now ' + data.new_quantity + ' in stock');
                RSCPScanner.feedback('ok');
            }).catch(function (err) {
                btn.disabled = false;
                btn.innerHTML = label;
                RSCPM.toast(err.message);
            });
        },

        mOpenLink: function (btn) {
            link.tracking = btn.getAttribute('data-tracking');
            link.name = btn.getAttribute('data-name');
            link.sku = '';
            byId('mLinkPackage').textContent = (link.name || 'Package') + ' · ' + link.tracking;
            byId('mLinkQuery').value = '';
            byId('mLinkSave').disabled = true;
            linkResults('<p class="m-empty">Type at least 2 characters.</p>');
            RSCPM.openSheet(byId('mLinkSheet'));   // a scan fills the search (data-scan-into); tap it to type
        },
        mLinkSearch: function (form, event) {
            event.preventDefault();
            clearTimeout(searchTimer);
            search(true);
        },
        mLinkPick: function (btn) {
            pick(btn);
        },
        mLinkSave: function (btn) {
            if (!link.sku) return;
            btn.disabled = true;
            RSCPM.postJSON(PAGE.linkUrl, {
                tracking: link.tracking,
                sku: link.sku,
                save_mapping: byId('mLinkRemember').checked,
                package_name: link.name
            }).then(function (data) {
                if (data.status !== 'success') throw new Error(data.message || 'Link failed');
                RSCPM.closeSheet(byId('mLinkSheet'));
                settle('Linked to ' + link.itemName + '. Units already received were added to its stock.');
                RSCPScanner.feedback('ok');
            }).catch(function (err) {
                btn.disabled = false;
                RSCPM.toast(err.message);
            });
        }
    });
})();
