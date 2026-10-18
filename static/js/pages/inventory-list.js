/* Inventory Items list (templates/inventory/list.html + _list_rows.html, whose rows are
   re-fetched and swapped in via innerHTML, so all row controls use delegated data-action).
   Needs dymo-print.js, Tom Select, the Bootstrap bundle and inventory-common.js (dark mode,
   showImage / copyToClipboard actions). */
(function () {
    'use strict';

    const pageData = RSCP.pageData() || {};

    RSCP.actions({
        printAndSubmitAdjust: function (el, event) {
            printAndSubmitAdjust(el, event, el.dataset.sku, el.closest('form'));
        },
        promptPrintLabel: function (el) { promptPrintLabel(el.dataset.sku); },
        clearSelection: function () { clearSelection(); },
        // Pagination "Go to" box: Enter in the input, or the Go button next to it
        goToPageOnEnter: function (el, event) {
            if (event.key === 'Enter') { goToPage(el.value); }
        },
        goToPageFromInput: function (el) {
            const input = el.dataset.inputId ? document.getElementById(el.dataset.inputId) : el.previousElementSibling;
            goToPage(input.value);
        }
    });

    // Quick Add: suggested price hint from cost, using the same margin settings as the Add Item page
    (function () {
        const marginEnabled = !!pageData.quickAddMarginEnabled;
        const marginPercent = pageData.quickAddMarginPercent;
        const buyInput = document.getElementById('quickAddBuyPrice');
        const sellInput = document.getElementById('quickAddSellPrice');
        if (!buyInput || !sellInput) return; // Quick Add disabled

        function updateQuickAddSuggestedPrice() {
            if (!marginEnabled) return;

            const buyPrice = parseFloat(buyInput.value) || 0;

            if (buyPrice > 0 && !sellInput.value) {
                const suggestedPrice = (buyPrice * (1 + marginPercent / 100)).toFixed(2);
                sellInput.placeholder = '~$' + suggestedPrice;
            } else if (buyPrice === 0) {
                sellInput.placeholder = '0.00';
            }
        }

        buyInput.addEventListener('input', updateQuickAddSuggestedPrice);
        sellInput.addEventListener('input', updateQuickAddSuggestedPrice);
    })();

    async function printAndSubmitAdjust(btn, e, sku, formElement) {
        e.preventDefault();
        
        // Get quantity from the modal's form
        const qtyInput = formElement.querySelector('input[name="quantity_change"]');
        const qty = parseInt(qtyInput.value) || 0;
        
        if (qty > 0) {
            btn.disabled = true;
            btn.innerHTML = 'Printing...';
            document.body.style.cursor = 'wait';
            await printDymoLabel(sku, qty);
        } else {
            alert("Cannot print labels for negative or zero quantities.");
            return; // Stop form submission if they asked to print a negative value? Actually they shouldn't click "Save & Print" for negative. 
        }
        
        // Submit form to save stock changes
        formElement.submit();
    }

    async function promptPrintLabel(sku) {
        const qty = prompt(`How many labels would you like to print for SKU: ${sku}?`, "1");
        // If user clicked cancel, qty is null
        if (qty !== null) {
            const parsedQty = parseInt(qty);
            if (parsedQty > 0) {
                await printDymoLabel(sku, parsedQty);
            } else {
                alert("Please enter a valid quantity greater than 0.");
            }
        }
    }

    // Auto-select/focus quantity input when adjust modal is shown
    document.addEventListener('shown.bs.modal', function (event) {
        const modal = event.target;
        const qtyInput = modal.querySelector('input[name="quantity_change"]');
        if (qtyInput) {
            qtyInput.focus();
            qtyInput.select();
        }
    });

    // Smart Auto-Refresh Controller with Visibility & Idle Tracking
    (function () {
        const REFRESH_INTERVAL_MS = 5000;
        const IDLE_TIMEOUT_MS = 300000; // 5 minutes of inactivity
        
        let refreshTimer = null;
        let idleTimer = null;
        let isIdle = false;
        let refreshInFlight = false;

        // Trigger one AJAX live update row refresh (never overlaps; gives up after 10s)
        function triggerRefresh() {
            if (refreshInFlight) return;
            if (document.querySelector('.modal.show')) return;
            if (document.getElementById('searchInput').value.trim() !== '') return;
            if (document.querySelectorAll('.item-checkbox:checked').length > 0) return;

            const url = new URL(window.location.href);
            url.searchParams.set('partial', '1');

            refreshInFlight = true;
            const controller = new AbortController();
            const abortTimer = setTimeout(() => controller.abort(), 10000);
            fetch(url, { signal: controller.signal })
                .then(r => r.text())
                .then(html => {
                    const tbody = document.getElementById('inventoryBody');
                    if (tbody) tbody.innerHTML = html;
                })
                .catch(e => { if (e.name !== 'AbortError') console.error('[Auto-Refresh Error]', e); })
                .finally(() => { clearTimeout(abortTimer); refreshInFlight = false; });
        }

        function startTimer() {
            stopTimer();
            if (document.visibilityState === 'visible' && !isIdle) {
                refreshTimer = setInterval(triggerRefresh, REFRESH_INTERVAL_MS);
            }
        }

        function stopTimer() {
            if (refreshTimer) {
                clearInterval(refreshTimer);
                refreshTimer = null;
            }
        }

        function resetIdleTimer() {
            clearTimeout(idleTimer);
            
            // If we were idle, wake up and update immediately
            if (isIdle) {
                isIdle = false;
                console.log('[Auto-Refresh] User active, waking up...');
                triggerRefresh();
            }
            
            startTimer(); // Ensure timer is active
            idleTimer = setTimeout(() => {
                isIdle = true;
                stopTimer();
                console.log('[Auto-Refresh] Idle timeout reached. Auto-refresh paused.');
            }, IDLE_TIMEOUT_MS);
        }

        // Monitor Visibility Changes (tab minimized, locked, etc.)
        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'visible') {
                console.log('[Auto-Refresh] Tab active, resuming refresh.');
                resetIdleTimer();
            } else {
                console.log('[Auto-Refresh] Tab hidden, pausing refresh.');
                stopTimer();
            }
        });

        // Monitor User Activity (mouse, key, touch)
        const activityEvents = ['mousemove', 'mousedown', 'keydown', 'touchstart', 'scroll'];
        activityEvents.forEach(evtName => {
            document.addEventListener(evtName, resetIdleTimer, { passive: true });
        });

        // Initial startup
        resetIdleTimer();
    })();

    // Bulk Selection Logic
    document.addEventListener('change', function (e) {
        if (e.target.classList.contains('item-checkbox') || e.target.id === 'selectAll') {
            updateBulkToolbar();
        }

        if (e.target.id === 'selectAll') {
            const checkboxes = document.querySelectorAll('.item-checkbox');
            checkboxes.forEach(cb => cb.checked = e.target.checked);
            updateBulkToolbar();
        }
    });

    function updateBulkToolbar() {
        const checkboxes = document.querySelectorAll('.item-checkbox:checked');
        const count = checkboxes.length;
        const toolbar = document.getElementById('bulkActions');
        const countSpan = document.getElementById('selectedCount');

        countSpan.textContent = count;

        if (count > 0) {
            toolbar.style.display = 'block';
        } else {
            toolbar.style.display = 'none';
            document.getElementById('selectAll').checked = false;
        }
    }

    function clearSelection() {
        document.querySelectorAll('.item-checkbox').forEach(cb => cb.checked = false);
        document.getElementById('selectAll').checked = false;
        updateBulkToolbar();
    }

    // On Submit, move checked IDs to hidden inputs
    // (the bulk form only exists for inventory.manage users; it used to throw here
    // for everyone else, which skipped the rest of this script)
    const bulkActionForm = document.getElementById('bulkActionForm');
    if (bulkActionForm) bulkActionForm.addEventListener('submit', function (e) {
        const container = document.getElementById('hiddenInputsContainer');
        container.innerHTML = ''; // Clear old

        document.querySelectorAll('.item-checkbox:checked').forEach(cb => {
            const input = document.createElement('input');
            input.type = 'hidden';
            input.name = 'item_ids';
            input.value = cb.value;
            container.appendChild(input);
        });

        if (container.children.length === 0) {
            e.preventDefault();
            alert('Please select at least one item.');
        }
    });


    // Search filter with live server-side search
    const searchInput = document.getElementById('searchInput');
    let searchTimeout;

    // Unified SPA AJAX page loader
    function loadInventoryPage(url, pushState = true) {
        // Show a subtle progress indicator (e.g. dim the table body)
        const tbody = document.getElementById('inventoryBody');
        if (tbody) tbody.style.opacity = '0.5';

        fetch(url)
            .then(response => response.text())
            .then(html => {
                const parser = new DOMParser();
                const doc = parser.parseFromString(html, 'text/html');

                // Swap table body rows
                const newTbody = doc.getElementById('inventoryBody');
                if (newTbody && tbody) {
                    tbody.innerHTML = newTbody.innerHTML;
                    tbody.style.opacity = '1';
                }

                // Swap sort controls
                const oldSort = document.querySelector('.sort-controls');
                const newSort = doc.querySelector('.sort-controls');
                if (oldSort && newSort) {
                    oldSort.innerHTML = newSort.innerHTML;
                }

                // Swap pagination controls (top & bottom)
                ['Inventory pagination top', 'Inventory pagination'].forEach(label => {
                    const oldNav = document.querySelector(`nav[aria-label="${label}"]`);
                    const newNav = doc.querySelector(`nav[aria-label="${label}"]`);
                    if (oldNav && newNav) {
                        oldNav.innerHTML = newNav.innerHTML;
                        oldNav.style.display = '';
                    } else if (oldNav && !newNav) {
                        oldNav.style.display = 'none';
                    }
                });

                // Update values of hidden parameters in the form to reflect the new state
                const parsedUrl = new URL(url, window.location.origin);
                const qVal = parsedUrl.searchParams.get('q') || '';
                const showLegacyVal = parsedUrl.searchParams.get('show_legacy') || '0';
                const sortVal = parsedUrl.searchParams.get('sort') || 'name';
                const orderVal = parsedUrl.searchParams.get('order') || (sortVal === 'created_at' ? 'desc' : 'asc');

                const inputShowLegacy = document.getElementById('searchShowLegacy');
                if (inputShowLegacy) inputShowLegacy.value = showLegacyVal;
                const inputSort = document.getElementById('searchSort');
                if (inputSort) inputSort.value = sortVal;
                const inputOrder = document.getElementById('searchOrder');
                if (inputOrder) inputOrder.value = orderVal;

                // Ensure search input value is also in sync with URL
                const sInput = document.getElementById('searchInput');
                if (sInput && document.activeElement !== sInput && sInput.value !== qVal) {
                    sInput.value = qVal;
                }

                // Update browser address bar
                if (pushState) {
                    window.history.pushState(null, '', url);
                } else {
                    window.history.replaceState(null, '', url);
                }

            })
            .catch(err => {
                console.error('Error loading page:', err);
                if (tbody) tbody.style.opacity = '1';
            });
    }

    // Intercept all sorting, pagination, clear search, and legacy toggle clicks for SPA experience
    document.addEventListener('click', function (e) {
        // Find closest link
        const link = e.target.closest('a');
        if (!link) return;

        // Check if it's a sort button, a pagination button, or clear search button
        const isSortLink = link.closest('.sort-controls');
        const isPaginationLink = link.closest('nav[aria-label="Inventory pagination top"]') || 
                                 link.closest('nav[aria-label="Inventory pagination"]');
        const isClearLink = link.getAttribute('href') && link.getAttribute('href').includes('/inventory/items') && link.textContent.includes('✕');

        if (isSortLink || isPaginationLink || isClearLink) {
            // Skip if it's an edit link
            if (link.href.includes('/inventory/edit/')) {
                return;
            }
            
            // Prevent browser page reload
            e.preventDefault();

            // Load target page dynamically
            loadInventoryPage(link.href, true);
        }
    });

    // Intercept Search Form Submissions (pressing Enter or clicking Search button)
    document.querySelector('form[action*="/inventory/items"]').addEventListener('submit', function (e) {
        e.preventDefault();
        
        const form = this;
        const url = new URL(form.action, window.location.origin);
        const formData = new FormData(form);
        
        for (const [key, value] of formData.entries()) {
            if (value) {
                url.searchParams.set(key, value);
            } else {
                url.searchParams.delete(key);
            }
        }
        
        // Always start at page 1 for a new search/form submission
        url.searchParams.set('page', '1');

        loadInventoryPage(url.toString(), true);
    });

    // Handle browser Back/Forward navigation smoothly
    window.addEventListener('popstate', function () {
        loadInventoryPage(window.location.href, false);
    });

    // Typing event listener with debounce for live search
    searchInput.addEventListener('input', function () {
        const query = this.value;

        clearTimeout(searchTimeout);

        searchTimeout = setTimeout(() => {
            const url = new URL(window.location.href);
            if (query.trim()) {
                url.searchParams.set('q', query);
            } else {
                url.searchParams.delete('q');
            }
            url.searchParams.set('page', '1'); // Reset to page 1 on new search

            // Use replaceState to keep browser back/forward clean during active typing
            loadInventoryPage(url.toString(), false);
        }, 300);
    });

    // Custom reason toggle + direction-based filtering
    document.querySelectorAll('[id^="reasonSelect"]').forEach(select => {
        select.addEventListener('change', function () {
            const itemId = this.id.replace('reasonSelect', '');
            const customInput = document.getElementById('customReason' + itemId);
            if (this.value === 'custom') {
                customInput.style.display = 'block';
                customInput.name = 'reason';
                this.name = '';
            } else {
                customInput.style.display = 'none';
                customInput.name = '';
                this.name = 'reason';
            }
        });
    });

    // Filter reason options based on quantity direction
    document.addEventListener('input', function (e) {
        if (e.target.name !== 'quantity_change') return;
        
        const input = e.target;
        const modal = input.closest('.modal');
        if (!modal) return;
        
        const select = modal.querySelector('[id^="reasonSelect"]');
        if (!select) return;
        
        const val = parseInt(input.value, 10);
        let direction = 'all'; // default: show all
        if (val > 0) direction = 'add';
        else if (val < 0) direction = 'remove';
        
        const options = select.querySelectorAll('option');
        let firstVisibleValue = null;
        let currentVisible = false;
        
        options.forEach(opt => {
            const optDir = opt.getAttribute('data-direction') || 'both';
            const shouldShow = (direction === 'all') || (optDir === direction) || (optDir === 'both');
            
            opt.style.display = shouldShow ? '' : 'none';
            opt.disabled = !shouldShow;
            
            if (shouldShow && !firstVisibleValue) {
                firstVisibleValue = opt.value;
            }
            if (shouldShow && opt.value === select.value) {
                currentVisible = true;
            }
        });
        
        // If current selection is now hidden, switch to first visible option
        if (!currentVisible && firstVisibleValue) {
            select.value = firstVisibleValue;
            select.dispatchEvent(new Event('change'));
        }
    });

    // Hide Cost toggle (sticky via localStorage)
    (function() {
        const toggle = document.getElementById('hideCostToggle');
        const STORAGE_KEY = 'rscp_hide_cost';
        if (!toggle) return; // only rendered for inventory.manage users

        function applyCostVisibility(hide) {
            document.querySelectorAll('.col-cost').forEach(el => {
                el.style.display = hide ? 'none' : '';
            });
        }

        // Restore saved state
        const saved = localStorage.getItem(STORAGE_KEY) === 'true';
        toggle.checked = saved;
        if (saved) applyCostVisibility(true);

        toggle.addEventListener('change', function() {
            localStorage.setItem(STORAGE_KEY, this.checked);
            applyCostVisibility(this.checked);
        });

        // Re-apply after live-update refreshes table body
        const observer = new MutationObserver(() => {
            if (toggle.checked) applyCostVisibility(true);
        });
        const tbody = document.getElementById('inventoryBody');
        if (tbody) observer.observe(tbody, { childList: true });
    })();

    // Go to specific page
    function goToPage(pageNum) {
        const totalPages = pageData.totalPages;
        pageNum = parseInt(pageNum, 10);
        if (isNaN(pageNum) || pageNum < 1) pageNum = 1;
        if (pageNum > totalPages) pageNum = totalPages;
        const url = new URL(window.location.href);
        url.searchParams.set('page', pageNum);
        loadInventoryPage(url.toString(), true);
    }

    // Scroll position preservation
    // Use event delegation to capture clicks on edit links (works even for dynamically loaded content)
    document.addEventListener('click', function (e) {
        const link = e.target.closest('a[href*="/inventory/edit/"]');
        if (link) {
            localStorage.setItem('inventory_scroll_position', window.scrollY);
        }
    });

    // Restore scroll position on page load if coming back from edit
    // Use setTimeout to ensure page has fully rendered including images
    window.addEventListener('load', function () {
        const savedScrollPos = localStorage.getItem('inventory_scroll_position');
        if (savedScrollPos) {
            setTimeout(function () {
                window.scrollTo({
                    top: parseInt(savedScrollPos),
                    behavior: 'instant'
                });
                localStorage.removeItem('inventory_scroll_position');
            }, 100);
        }

        // Auto Copy SKU from URL (New Item)
        const urlParams = new URLSearchParams(window.location.search);
        const newSku = urlParams.get('new_sku');
        if (newSku) {
            navigator.clipboard.writeText(newSku).then(() => {
                // Remove param from URL without reload
                const newUrl = window.location.protocol + "//" + window.location.host + window.location.pathname;
                window.history.replaceState({}, document.title, newUrl);

                // Show a non-blocking toast notification
                const toastDiv = document.createElement('div');
                toastDiv.className = 'position-fixed bottom-0 end-0 p-3';
                toastDiv.style.zIndex = '5000';
                toastDiv.innerHTML = `
<div class="toast show align-items-center text-white bg-success border-0" role="alert">
    <div class="d-flex">
        <div class="toast-body">
            SKU <strong>${escapeHtml(newSku)}</strong> copied to clipboard!
        </div>
        <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button>
    </div>
</div>
`;
                document.body.appendChild(toastDiv);
                setTimeout(() => toastDiv.remove(), 4000);
            });
        }
    });

    document.addEventListener('DOMContentLoaded', function () {
        if (document.getElementById('quickAddLocation')) {
            new TomSelect('#quickAddLocation', {
                create: true,
                sortField: { field: "text", direction: "asc" },
                placeholder: 'Area...'
            });
        }
    });
})();
