/*
 * Shared inventory page helpers. Load this in the scripts block, before the page script.
 *
 * - Applies dark mode (localStorage 'rscp_darkmode') when loaded.
 * - Actions:
 *     data-action="showImage" data-url="..." data-title="..."  image modal (#imageModal)
 *     data-action="copyToClipboard" data-text="..."            copy + "Copied!" tooltip
 *     data-action="stockStep" data-delta="1|-1"                stock +/- inside a .stock-stepper
 *         (templates/inventory/_stock_stepper.html); POSTs /inventory/api/quick_adjust and
 *         updates the badge and any .stock-status[data-item-id] cell for that item.
 */
(function () {
    'use strict';

    // Dark mode
    if (localStorage.getItem('rscp_darkmode') === 'true') {
        document.body.classList.add('dark-mode');
    }

    // Image modal
    function showImage(url, title) {
        document.getElementById('modalImage').src = url;
        document.getElementById('imageModalLabel').textContent = title || 'Image';
        new bootstrap.Modal(document.getElementById('imageModal')).show();
    }

    function copyToClipboard(text, element) {
        navigator.clipboard.writeText(text).then(function () {
            const originalTitle = element.getAttribute('title');

            // Visual feedback
            const tooltip = new bootstrap.Tooltip(element, {
                title: "Copied!",
                trigger: 'manual'
            });
            tooltip.show();

            setTimeout(() => {
                tooltip.dispose();
                element.setAttribute('title', originalTitle); // Restore original title
            }, 1000);
        }, function (err) {
            console.error('Could not copy text: ', err);
        });
    }

    // Stock +/- (Items Needing Attention, Low Stock report)
    function renderStock(box, qty) {
        const threshold = parseInt(box.dataset.threshold, 10) || 0;
        const labels = box.dataset.labels === 'true';
        const state = qty <= 0 ? 'out' : (qty <= threshold ? 'low' : 'ok');
        const badge = box.querySelector('.stock-qty');
        badge.className = 'badge stock-qty ' +
            { out: 'bg-danger', low: 'bg-warning text-dark', ok: 'bg-success' }[state];
        badge.textContent = labels
            ? ({ out: 'Out of Stock', low: 'Low', ok: 'OK' }[state] + ' (' + qty + ')')
            : String(qty);
        box.querySelector('[data-delta="-1"]').disabled = qty <= 0;

        const status = document.querySelector('.stock-status[data-item-id="' + box.dataset.itemId + '"]');
        if (status) {
            const span = document.createElement('span');
            span.className = { out: 'text-danger', low: 'text-warning', ok: 'text-success' }[state];
            span.textContent = { out: 'Out of Stock', low: 'Low Stock', ok: 'Restocked' }[state];
            status.replaceChildren(span);
        }
    }

    function stockStep(btn) {
        const box = btn.closest('.stock-stepper');
        if (!box || box.dataset.busy) return;
        box.dataset.busy = '1';
        const buttons = box.querySelectorAll('button');
        buttons.forEach(function (b) { b.disabled = true; });
        let qty = null;

        fetch('/inventory/api/quick_adjust/' + box.dataset.itemId, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'X-CSRFToken': RSCP.csrfToken()
            },
            body: JSON.stringify({ change: parseInt(btn.dataset.delta, 10), source: box.dataset.source })
        })
            .then(function (r) {
                return r.json()
                    .catch(function () { return { error: 'The server answered ' + r.status + '.' }; })
                    .then(function (d) {
                        if (!r.ok || !d.success) throw new Error(d.error || 'Could not change the stock.');
                        return d;
                    });
            })
            .then(function (d) {
                qty = d.new_quantity;
                renderStock(box, qty);
            })
            .catch(function (err) {
                alert(err.message || 'Could not change the stock.');
            })
            .finally(function () {
                delete box.dataset.busy;
                buttons.forEach(function (b) { b.disabled = false; });
                if (qty === null) {
                    const shown = parseInt((box.querySelector('.stock-qty').textContent.match(/-?\d+/) || ['0'])[0], 10);
                    box.querySelector('[data-delta="-1"]').disabled = shown <= 0;
                } else {
                    box.querySelector('[data-delta="-1"]').disabled = qty <= 0;
                }
            });
    }

    window.RSCPInventory = { showImage: showImage, copyToClipboard: copyToClipboard };

    RSCP.actions({
        showImage: function (el, event) {
            event.preventDefault();
            showImage(el.dataset.url, el.dataset.title);
        },
        copyToClipboard: function (el) {
            copyToClipboard(el.dataset.text, el);
        },
        stockStep: function (el) {
            stockStep(el);
        }
    });
})();
