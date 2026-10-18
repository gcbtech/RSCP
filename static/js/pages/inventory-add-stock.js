/* Add Stock confirmation page (templates/inventory/add_stock.html). Needs dymo-print.js. */
(function () {
    'use strict';

    async function printAndSubmit(btn, e, sku, qty) {
        e.preventDefault();
        btn.disabled = true;
        btn.innerHTML = 'Printing...';
        document.body.style.cursor = 'wait';

        if (qty > 0) {
            await printDymoLabel(sku, qty);
        } else {
            alert("Cannot print labels for negative or zero quantity additions.");
        }

        // Submit form to save stock changes immediately after Dymo API is called
        document.querySelector('form').submit();
    }

    RSCP.actions({
        printAndSubmit: function (el, event) {
            printAndSubmit(el, event, el.dataset.sku, Number(el.dataset.qty));
        }
    });
})();
