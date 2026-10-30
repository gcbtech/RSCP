/*
 * Phone UI, POS checkout (templates/m/pos/checkout.html). Plain ES5.
 * Tender switch, quick cash, change due and the split remainder. Amounts on
 * screen are dollars; cardCents / cashCents (page data) are what's due by
 * card and in cash (the cash price when a cash discount applies).
 */
(function () {
    'use strict';

    var PAGE = RSCP.pageData() || {};
    var tender = 'cash';

    function byId(id) { return document.getElementById(id); }

    function cents(input) {
        var n = parseFloat(String(input.value).replace(/[^0-9.]/g, ''));
        return isNaN(n) ? 0 : Math.round(n * 100);
    }

    function dollars(c) { return (c / 100).toFixed(2); }

    function due() { return tender === 'cash' ? PAGE.cashCents : PAGE.cardCents; }

    function calc() {
        byId('mDue').textContent = '$' + dollars(due());
        if (PAGE.cashCents !== PAGE.cardCents) {
            byId('mDueNote').textContent = tender === 'cash'
                ? 'Cash price · card price $' + dollars(PAGE.cardCents)
                : 'Card price · cash price $' + dollars(PAGE.cashCents);
        }
        if (tender === 'cash') {
            var change = cents(byId('mTendered')) - due();
            byId('mChange').textContent = change >= 0 ? '$' + dollars(change) : 'Short $' + dollars(-change);
            byId('mChangeRow').classList.toggle('m-change-short', change < 0);
        } else if (tender === 'split') {
            // Against the card total; a cash discount on the cash part only lowers it
            var left = PAGE.cardCents - cents(byId('mSplitCash')) - cents(byId('mSplitCard'));
            byId('mRemain').textContent = left > 0 ? '$' + dollars(left) : (left < 0 ? 'Change $' + dollars(-left) : 'Paid in full');
            byId('mRemainRow').classList.toggle('m-change-short', left > 0);
        }
    }

    function setTender(t) {
        tender = t;
        byId('mPayMethod').value = t;
        var panes = document.querySelectorAll('.m-pay');
        for (var i = 0; i < panes.length; i++) {
            var on = panes[i].getAttribute('data-pay') === t;
            panes[i].hidden = !on;
            // Only the visible tender's fields are sent
            var inputs = panes[i].querySelectorAll('input');
            for (var j = 0; j < inputs.length; j++) inputs[j].disabled = !on;
        }
        calc();
    }

    RSCP.actions({
        mTender: function (radio) { if (radio.checked) setTender(radio.value); },
        mCalc: calc,
        mAddCash: function (btn) {
            var input = byId('mTendered');
            input.value = dollars(cents(input) + Number(btn.getAttribute('data-amount')) * 100);
            calc();
        },
        mExactCash: function () { byId('mTendered').value = dollars(due()); calc(); },
        mClearCash: function () { byId('mTendered').value = '0.00'; calc(); }
    });

    byId('mCheckoutForm').addEventListener('submit', function (event) {
        var problem = null;
        if (tender === 'cash' && cents(byId('mTendered')) < due()) problem = 'The cash tendered is less than what’s due. Tap Exact or add cash.';
        // Split: the cash part may earn a cash discount, so the server (pos_checkout.settle_tender)
        // decides whether it covers the total; here only an empty split is caught.
        if (tender === 'split' && cents(byId('mSplitCash')) + cents(byId('mSplitCard')) <= 0) problem = 'Enter the cash and card amounts.';
        if (problem) {
            event.preventDefault();
            RSCPM.toast(problem);
            return;
        }
        var btn = byId('mComplete');
        if (btn.disabled) { event.preventDefault(); return; }
        btn.disabled = true;       // the one-time token also stops a second sale
        btn.textContent = 'Completing…';
    });

    setTender('cash');
})();
