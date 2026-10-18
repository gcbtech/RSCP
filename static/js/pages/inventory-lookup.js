/* Inventory Lookup (templates/inventory/lookup.html). Uses window.escapeHtml from rscp-common.js. */
(function () {
    'use strict';

    // Dark mode
    if (localStorage.getItem('rscp_dark_mode') === 'true') {
        document.body.classList.add('dark-mode');
    }

    const lookupInput = document.getElementById('lookupInput');
    const resultsContainer = document.getElementById('lookupResults');
    let searchTimeout;

    lookupInput.addEventListener('input', function () {
        const query = this.value.trim();
        clearTimeout(searchTimeout);

        if (!query) {
            resultsContainer.innerHTML = `
                <div class="lookup-status">
                    <span class="icon">🔍</span>
                    <div class="message">Scan a barcode or enter a search term to look up an item</div>
                </div>`;
            return;
        }

        // Show loading state
        resultsContainer.innerHTML = `
            <div class="lookup-status">
                <div class="lookup-spinner"></div>
                <div class="message mt-3">Searching...</div>
            </div>`;

        searchTimeout = setTimeout(() => {
            performLookup(query);
        }, 300);
    });

    // Also handle Enter key for instant search (barcode scanners often send Enter)
    lookupInput.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') {
            e.preventDefault();
            clearTimeout(searchTimeout);
            const query = this.value.trim();
            if (query) {
                performLookup(query);
            }
        }
    });

    function performLookup(query) {
        fetch('/inventory/lookup/search?q=' + encodeURIComponent(query))
            .then(r => r.json())
            .then(data => {
                if (!data.found) {
                    resultsContainer.innerHTML = `
                        <div class="lookup-status">
                            <span class="icon">📦</span>
                            <div class="message">No items found for "<strong>${escapeHtml(query)}</strong>"</div>
                        </div>`;
                    return;
                }

                if (data.multiple && data.items) {
                    renderMultipleResults(data.items);
                } else if (data.item) {
                    renderSingleResult(data.item);
                    lookupInput.value = '';
                }
            })
            .catch(err => {
                console.error('Lookup error:', err);
                resultsContainer.innerHTML = `
                    <div class="lookup-status">
                        <span class="icon">⚠️</span>
                        <div class="message">Error performing lookup. Please try again.</div>
                    </div>`;
            });
    }

    function renderSingleResult(item) {
        const priceDisplay = formatPrice(item);
        const imageHtml = item.image_url
            ? `<img src="${escapeHtml(item.image_url)}" alt="${escapeHtml(item.name)}">`
            : `<div class="no-image-placeholder">📦</div>`;

        let idsHtml = '';
        if (item.upc || item.part_number) {
            idsHtml = '<div class="item-ids">';
            if (item.upc) idsHtml += `<span>UPC: ${escapeHtml(item.upc)}</span>`;
            if (item.part_number) idsHtml += `<span>Part #: ${escapeHtml(item.part_number)}</span>`;
            idsHtml += '</div>';
        }

        resultsContainer.innerHTML = `
            <div class="lookup-result-card card">
                <div class="item-image-container">
                    ${imageHtml}
                </div>
                <div class="item-details">
                    <div class="item-name">${escapeHtml(item.name)}</div>
                    <div class="item-sku" data-sku="${escapeHtml(item.sku)}" data-action="copySku" title="Click to copy SKU">
                        <span class="copy-icon">📋</span> ${escapeHtml(item.sku)}
                    </div>
                    <div>${qtyBadge(item.quantity, item.alert_threshold)} ${item.location ? '<span class="item-location">📍 ' + escapeHtml(item.location) + '</span>' : ''}</div>
                    <div>
                        ${priceDisplay}
                    </div>
                    ${idsHtml}
                </div>
            </div>`;
    }

    function renderMultipleResults(items) {
        let html = '<div class="lookup-multi-list">';
        html += `<div class="text-muted mb-2 text-center"><small>${items.length} result${items.length !== 1 ? 's' : ''} found</small></div>`;

        items.forEach((item, idx) => {
            const imageHtml = item.image_url
                ? `<img src="${escapeHtml(item.image_url)}" alt="${escapeHtml(item.name)}">`
                : `<div class="no-img">📦</div>`;

            const priceClass = item.is_on_sale ? 'price-tag on-sale' : 'price-tag';
            const price = item.sell_price ? '$' + parseFloat(item.sell_price).toFixed(2) : 'N/A';

            const locHtml = item.location ? `<div class="loc-line">📍 ${escapeHtml(item.location)}</div>` : '';

            html += `
                <div class="lookup-multi-item" data-action="selectMultiItem" data-index="${idx}" style="animation-delay: ${idx * 0.05}s">
                    ${imageHtml}
                    <div class="item-info">
                        <div class="name">${escapeHtml(item.name)} ${qtyBadge(item.quantity, item.alert_threshold, true)}</div>
                        <div class="sku-line">${escapeHtml(item.sku)}</div>
                        ${locHtml}
                    </div>
                    <div class="${priceClass}">${price}</div>
                </div>`;
        });

        html += '</div>';
        resultsContainer.innerHTML = html;

        // Store items for selection
        window._lookupItems = items;
    }

    function selectMultiItem(index) {
        const item = window._lookupItems[index];
        if (item) {
            renderSingleResult(item);
            lookupInput.value = '';
        }
    }

    function formatPrice(item) {
        if (!item.sell_price && item.sell_price !== 0) {
            return '<span class="item-price">Price N/A</span>';
        }

        const price = parseFloat(item.sell_price).toFixed(2);

        if (item.is_on_sale && item.regular_price) {
            const regular = parseFloat(item.regular_price).toFixed(2);
            return `<span class="item-price on-sale">$${price}</span>
                    <span class="item-original-price">$${regular}</span>
                    <span class="sale-badge">Sale</span>`;
        }

        return `<span class="item-price">$${price}</span>`;
    }

    function qtyBadge(quantity, threshold, small) {
        const qty = parseInt(quantity) || 0;
        const thr = parseInt(threshold) || 0;
        let cls, label;
        if (qty <= 0) {
            cls = small ? 'qty-badge qty-oos' : 'item-qty qty-oos';
            label = 'Out of Stock';
        } else if (thr > 0 && qty <= thr) {
            cls = small ? 'qty-badge qty-low' : 'item-qty qty-low';
            label = qty + ' in stock';
        } else {
            cls = small ? 'qty-badge qty-in-stock' : 'item-qty qty-in-stock';
            label = qty + ' in stock';
        }
        return `<span class="${cls}">${label}</span>`;
    }

    function copySku(sku) {
        navigator.clipboard.writeText(sku).then(() => {
            const toast = document.getElementById('copiedToast');
            toast.textContent = '✓ SKU ' + sku + ' copied!';
            toast.classList.add('show');
            setTimeout(() => toast.classList.remove('show'), 1800);
        }).catch(err => {
            console.error('Copy failed:', err);
        });
    }

    // Auto-focus the search input
    lookupInput.focus();

    RSCP.actions({
        copySku: function (el) { copySku(el.dataset.sku); },
        selectMultiItem: function (el) { selectMultiItem(parseInt(el.dataset.index, 10)); }
    });
})();
