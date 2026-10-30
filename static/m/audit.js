/*
 * Phone UI, Stock > live audit (templates/m/stock/audit_live.html). Plain ES5.
 *
 * One item at a time. A scan looks the item up (/m/stock/audit/<id>/scan);
 * saving posts its count (/count), which sets the item's stock at once and
 * returns the audit's totals and latest counts.
 *   shelf audit: "Yes, N on the shelf" confirms the system count, or enter the count.
 *   item audit:  every further scan of the same item adds one; save when done.
 * Scanning a different item while one is unsaved asks before saving it
 * (as the desktop audit does).
 */
(function () {
    'use strict';

    var PAGE = RSCP.pageData() || {};
    var ITEM_MODE = PAGE.mode === 'item';

    function byId(id) { return document.getElementById(id); }

    var card = byId('mAuditItem');
    var manual = byId('mScanManual');
    var current = null;     // the item being counted: {id, name, sku, quantity, location, image_url, codes}
    var count = 0;          // item audit: units scanned so far
    var pendingCode = null; // a scan waiting for "Save the last item first?"
    var queue = [];
    var busy = false;

    var scanner = RSCPScanner.attach({
        capture: byId('mScanCapture'),
        status: byId('mScanStatus'),
        fields: [manual],
        onScan: function (code) { queue.push(code); next(); }
    });

    function next() {
        if (busy || pendingCode !== null || !queue.length) return;
        var code = queue.shift();
        if (current) {
            if (ITEM_MODE && current.codes.indexOf(code) !== -1) {
                setCount(count + 1);
                RSCPScanner.feedback('ok');
                next();
                return;
            }
            pendingCode = code;
            byId('mAuditSwitchText').textContent = current.name + ': save a count of ' + defaultCount() + ', then look up ' + code + '.';
            RSCPM.openSheet(byId('mAuditSwitch'));
            return;
        }
        lookup(code);
    }

    function lookup(code) {
        busy = true;
        RSCPM.postJSON(PAGE.scanUrl, { code: code })
            .then(function (data) {
                current = data.item;
                current.codes = [code, data.item.sku];
                show(data.prev_count);
                RSCPScanner.feedback('ok');
            })
            .catch(function (err) {
                RSCPScanner.feedback('bad');
                RSCPM.toast(err.message);
            })
            .then(function () { busy = false; next(); });
    }

    function defaultCount() { return ITEM_MODE ? count : current.quantity; }

    function setCount(n) {
        count = Math.max(0, n);
        byId('mAuditCount').textContent = count;
        byId('mAuditSaveN').textContent = count;
    }

    function show(prevCount) {
        byId('mAuditName').textContent = current.name;
        byId('mAuditMeta').textContent = current.sku + (current.location ? ' · ' + current.location : '');
        var img = byId('mAuditImg');
        img.hidden = !current.image_url;
        byId('mAuditNoImg').hidden = !!current.image_url;
        if (current.image_url) img.src = current.image_url;
        var prev = byId('mAuditPrev');
        prev.hidden = prevCount === null || prevCount === undefined;
        prev.textContent = prev.hidden ? '' : 'Already counted in this audit: ' + prevCount + '. Saving again replaces it.';
        if (ITEM_MODE) {
            byId('mAuditSystem').textContent = 'scanned · system has ' + current.quantity;
            setCount(1);
        } else {
            byId('mAuditSystemNum').textContent = current.quantity;
            byId('mAuditMatchN').textContent = current.quantity;
        }
        card.hidden = false;
        byId('mScanStatus').classList.add('m-compact');
        window.scrollTo(0, 0);
    }

    function save(n, then) {
        if (!current || busy) return;
        busy = true;
        var item = current;
        RSCPM.postJSON(PAGE.countUrl, { item_id: item.id, count: n })
            .then(function (data) {
                current = null;
                card.hidden = true;
                byId('mAuditSummary').textContent = data.summary.counted + ' counted · ' + data.summary.off + ' off';
                byId('mAuditRecent').innerHTML = data.recent_html;
                RSCPScanner.feedback(n === item.quantity ? 'ok' : 'warn');
                RSCPM.toast('Saved ' + item.name + ': ' + n);
                if (then) then();
            })
            .catch(function (err) {
                RSCPScanner.feedback('bad');
                RSCPM.toast(err.message);
            })
            .then(function () { busy = false; next(); });
    }

    // "Back" on the switch sheet (or tapping outside it) drops the waiting scan
    byId('mAuditSwitch').addEventListener('close', function () {
        if (pendingCode !== null) {
            pendingCode = null;
            next();
        }
    });

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
        },
        mAuditStep: function (btn) {
            setCount(count + Number(btn.getAttribute('data-change')));
        },
        mAuditSave: function () {
            save(count);
        },
        mAuditMatch: function () {
            if (current) save(current.quantity);
        },
        mAuditOpenCount: function () {
            if (!current) return;
            byId('mCountItem').textContent = current.name + ' · system has ' + current.quantity;
            byId('mCountInput').value = defaultCount();
            RSCPM.openSheet(byId('mCountSheet'));
        },
        mCountStep: function (btn) {
            var input = byId('mCountInput');
            input.value = Math.max(0, (parseInt(input.value, 10) || 0) + Number(btn.getAttribute('data-change')));
        },
        mCountSave: function () {
            var n = parseInt(byId('mCountInput').value, 10);
            if (isNaN(n) || n < 0) {
                RSCPM.toast('Enter a whole number, 0 or more');
                return;
            }
            RSCPM.closeSheet(byId('mCountSheet'));
            save(n);
        },
        mAuditSwitchSave: function () {
            var code = pendingCode;
            pendingCode = null;   // so closing the sheet doesn't drop it
            RSCPM.closeSheet(byId('mAuditSwitch'));
            save(defaultCount(), function () { queue.unshift(code); });
        }
    });
})();
