/* POS edit sale (templates/pos/edit_sale.html): live re-pricing as lines change, and
   removing lines. Tax rate and the sale's adjustments come from RSCP.pageData(). */
(function () {
    'use strict';

    const pageData = RSCP.pageData() || {};

    RSCP.actions({
        recalculate: function () { recalculate(); },
        removeItem: function (el) { removeItem(el.dataset.itemId); }
    });

    const taxRatePercent = pageData.taxRatePercent;
    const taxRate = taxRatePercent / 100;
    // The sale's own adjustments (see _sale_adjustments in management.py)
    const adj = pageData.adjustments;

    function recalculate() {
        let subtotal = 0;
        const rows = document.querySelectorAll('.item-row:not(.deleted)');

        rows.forEach(row => {
            const qty = parseInt(row.querySelector('input[name="item_quantity"]').value) || 0;
            const price = parseFloat(row.querySelector('input[name="item_price"]').value) || 0;
            let lineTotal = qty * price;
            const lineDisc = parseFloat(row.dataset.lineDiscount) || 0;
            if (lineDisc > 0) {
                lineTotal -= row.dataset.lineDiscountType === 'percent' ? lineTotal * lineDisc / 100 : lineDisc;
            }
            lineTotal = Math.round(Math.max(0, lineTotal) * 100) / 100;
            subtotal += lineTotal;

            const id = row.dataset.itemId;
            const lineTotalEl = document.getElementById('line-total-' + id);
            if (lineTotalEl) {
                lineTotalEl.textContent = '$' + lineTotal.toFixed(2);
            }
        });

        // Same re-pricing as the server (_reprice_sale)
        subtotal = Math.round(subtotal * 100) / 100;
        const discount = adj.discount_pct !== null
            ? Math.round(subtotal * adj.discount_pct * 100) / 100
            : Math.min(adj.discount_fixed, subtotal);
        const afterDiscount = Math.max(0, subtotal - discount);
        const afterCoupon = Math.max(0, afterDiscount - Math.min(adj.coupon, afterDiscount));
        const tax = Math.round(afterCoupon * adj.tax_rate * 100) / 100;
        const total = Math.round(Math.max(0, afterCoupon + tax - adj.cash_discount) * 100) / 100;

        document.getElementById('editSubtotal').textContent = '$' + subtotal.toFixed(2);
        document.getElementById('editTax').textContent = '$' + tax.toFixed(2);
        document.getElementById('editTotal').textContent = '$' + total.toFixed(2);
    }

    function removeItem(itemId) {
        const row = document.getElementById('item-row-' + itemId);
        if (!row) return;

        if (!confirm('Remove this item from the sale? Inventory will be restored.')) return;

        row.classList.add('deleted');
        // Remove the hidden item_id input so form doesn't submit this item
        const hiddenId = row.querySelector('input[name="item_id"]');
        if (hiddenId) hiddenId.remove();
        const nameInput = row.querySelector('input[name="item_name"]');
        if (nameInput) nameInput.remove();
        const qtyInput = row.querySelector('input[name="item_quantity"]');
        if (qtyInput) qtyInput.remove();
        const priceInput = row.querySelector('input[name="item_price"]');
        if (priceInput) priceInput.remove();
        const skuInput = row.querySelector('input[name="item_sku"]');
        if (skuInput) skuInput.remove();

        // Add hidden input for deleted item
        const container = document.getElementById('deletedItemsContainer');
        const hidden = document.createElement('input');
        hidden.type = 'hidden';
        hidden.name = 'deleted_items';
        hidden.value = itemId;
        container.appendChild(hidden);

        recalculate();
    }
})();
