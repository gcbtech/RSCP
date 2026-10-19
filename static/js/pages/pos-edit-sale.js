/* POS edit sale (templates/pos/edit_sale.html): live re-pricing as lines change,
   removing lines, and what the correction pays back / collects. Tax rate and the
   sale's adjustments come from RSCP.pageData(). */
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
        // Coupon and cash discount shrink with the subtotal (see reprice_sale)
        const scale = adj.orig_subtotal > 0 ? Math.min(1, subtotal / adj.orig_subtotal) : 1;
        const coupon = Math.round(adj.coupon * scale * 100) / 100;
        const cashDiscount = Math.round(adj.cash_discount * scale * 100) / 100;
        const afterCoupon = Math.max(0, afterDiscount - Math.min(coupon, afterDiscount));
        // offsets: how far the sale's stored figures are from re-pricing its
        // current lines (zero for sales rung up at checkout); saving keeps them
        const tax = Math.round((afterCoupon * adj.tax_rate + (adj.tax_offset || 0)) * 100) / 100;
        const total = Math.round((Math.max(0, afterCoupon + Math.round(afterCoupon * adj.tax_rate * 100) / 100
            - cashDiscount) + (adj.total_offset || 0)) * 100) / 100;

        document.getElementById('editSubtotal').textContent = '$' + subtotal.toFixed(2);
        document.getElementById('editTax').textContent = '$' + tax.toFixed(2);
        document.getElementById('editTotal').textContent = '$' + total.toFixed(2);

        // What changes hands when this correction is saved
        const diff = Math.round((total - (adj.current_total || 0)) * 100) / 100;
        const label = document.getElementById('editDifferenceLabel');
        const value = document.getElementById('editDifference');
        if (label && value) {
            if (diff < 0) {
                label.textContent = 'Pay the customer back';
                value.textContent = '$' + (-diff).toFixed(2);
            } else if (diff > 0) {
                label.textContent = 'Collect from the customer';
                value.textContent = '$' + diff.toFixed(2);
            } else {
                label.textContent = 'Difference';
                value.textContent = 'No change';
            }
        }
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
