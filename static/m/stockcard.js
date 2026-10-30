/*
 * Phone UI: the stock buttons on an item card (templates/m/stock/_item.html,
 * and Receive's result for a scanned stock item): - / +, Set count (the sheet
 * in templates/m/_count_sheet.html) and Out of stock. Plain ES5.
 *
 * A card is <div data-stock-item="ID"> holding [data-qty] and .m-item-name;
 * cards may be swapped in after load. Page data needs adjustUrl, the
 * desktop's /inventory/api/quick_adjust/0 (0 is replaced by the item id).
 */
(function () {
    'use strict';

    var PAGE = RSCP.pageData() || {};
    var countCard = null;
    var countFrom = 0;

    function byId(id) { return document.getElementById(id); }

    function cardOf(el) { return el.closest('[data-stock-item]'); }

    function adjust(card, body, done) {
        var url = PAGE.adjustUrl.replace(/0$/, card.getAttribute('data-stock-item'));
        RSCPM.postJSON(url, body)
            .then(function (data) {
                card.querySelector('[data-qty]').textContent = data.new_quantity;
                if (done) done(data);
            })
            .catch(function (err) { RSCPM.toast(err.message); });
    }

    RSCP.actions({
        mAdjust: function (btn) {
            var card = cardOf(btn);
            if (card) adjust(card, { change: Number(btn.getAttribute('data-change')), source: 'scan' });
        },
        mMarkOOS: function (btn) {
            var card = cardOf(btn);
            if (card) adjust(card, { action: 'oos', source: 'scan' }, function () { RSCPM.toast('Marked out of stock'); });
        },
        mOpenCount: function (btn) {
            countCard = cardOf(btn);
            if (!countCard) return;
            countFrom = Number(countCard.querySelector('[data-qty]').textContent) || 0;
            byId('mCountItem').textContent = countCard.querySelector('.m-item-name').textContent;
            byId('mCountInput').value = countFrom;
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
            if (countCard && n !== countFrom) {
                adjust(countCard, { change: n - countFrom, source: 'scan' }, function () { RSCPM.toast('Count saved: ' + n); });
            }
        }
    });
})();
