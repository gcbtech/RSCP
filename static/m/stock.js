/*
 * Phone UI, Stock > Look up (templates/m/stock/lookup.html). Plain ES5.
 *
 * A scan or a typed search posts to /m/stock/find; the server renders an item
 * card (its buttons are static/m/stockcard.js) or a list of matches, swapped
 * in above the scanner card. Scans that arrive meanwhile wait their turn.
 */
(function () {
    'use strict';

    var PAGE = RSCP.pageData() || {};

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
        RSCPM.postJSON(PAGE.findUrl, { code: queue.shift() })
            .then(function (data) {
                result.innerHTML = data.html;
                result.hidden = false;
                byId('mScanStatus').classList.add('m-compact');
                RSCPScanner.feedback(data.feedback);
                window.scrollTo(0, 0);
            })
            .catch(function (err) {
                RSCPScanner.feedback('bad');
                RSCPM.toast(err.message);
            })
            .then(function () { busy = false; next(); });
    }

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
        }
    });
})();
