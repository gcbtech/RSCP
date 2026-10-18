/* Live audit scanner (templates/inventory/audit_live.html). Needs the Bootstrap bundle. */
(function () {
    'use strict';

    const pageData = RSCP.pageData() || {};

    // State
    let currentItem = null;
    let localCount = 0;
    let pendingBarcode = null;
    const sessionId = pageData.sessionId;
    const sessionMode = pageData.sessionMode;
    const csrfToken = document.querySelector('input[name="csrf_token"]').value;
    const scanInput = document.getElementById('scanInput');

    // Modals (Init once)
    const unsavedModal = new bootstrap.Modal(document.getElementById('unsavedModal'));
    const manualModal = new bootstrap.Modal(document.getElementById('manualModal'));

    // Dark Mode
    if (localStorage.getItem('rscp_darkmode') === 'true') document.body.classList.add('dark-mode');

    // Focus Keeper
    const isModalOpen = () => document.querySelector('.modal.show') !== null;

    // Registered on document (after rscp-common.js's data-action dispatcher) so it
    // still runs AFTER the clicked control's handler, as with the old inline onclick.
    document.addEventListener('click', (e) => {
        if (!isModalOpen() && e.target.tagName !== 'BUTTON' && e.target.tagName !== 'INPUT') {
            scanInput.focus();
        }
    });

    scanInput.addEventListener('blur', () => {
        setTimeout(() => {
            if (!isModalOpen() && !blockKeyboard) scanInput.focus();
        }, 100);
    });

    // --- KEYBOARD BLOCKER LOGIC ---
    let blockKeyboard = localStorage.getItem('rscp_audit_block_kbd') === 'true';

    function applyKeyboardState() {
        const statusLabel = document.getElementById('kbdStatus');
        if (blockKeyboard) {
            statusLabel.innerText = 'KBD: HIDDEN';
            statusLabel.className = 'badge bg-danger ms-2';
            statusLabel.style.cursor = 'pointer';
        } else {
            statusLabel.innerText = 'KBD: AUTO';
            statusLabel.className = 'badge bg-success ms-2';
            statusLabel.style.cursor = 'pointer';
        }
        localStorage.setItem('rscp_audit_block_kbd', blockKeyboard);
        if (!blockKeyboard) scanInput.focus();
    }

    function toggleKeyboardBlock() {
        blockKeyboard = !blockKeyboard;
        applyKeyboardState();
        if (blockKeyboard) {
            scanInput.blur();
        } else {
            scanInput.focus();
        }
    }

    scanInput.addEventListener('focus', function () {
        if (blockKeyboard) {
            scanInput.setAttribute('readonly', 'readonly');
            setTimeout(() => { scanInput.removeAttribute('readonly'); }, 50);
        }
    });

    // Apply keyboard state on load
    applyKeyboardState();

    // Scan Handler
    scanInput.addEventListener('change', async (e) => {
        const barcode = e.target.value.trim();
        e.target.value = '';
        if (!barcode) return;

        // Handle Active Item State
        if (currentItem) {
            // ITEM MODE: Allow scanning same item to increment
            if (sessionMode === 'item' && barcode === currentItem.sku) {
                localCount++;
                updateUI();
                return;
            }

            // OTHERWISE: Unsaved Item Logic
            pendingBarcode = barcode;
            unsavedModal.show();
            return;
        }

        processScan(barcode);
    });

    async function processScan(barcode) {
        document.getElementById('loadingSpinner').classList.remove('d-none');
        document.getElementById('welcomeState').classList.add('d-none');
        document.getElementById('itemCard').classList.add('d-none');

        try {
            const res = await fetch('/inventory/audit/scan', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken
                },
                body: JSON.stringify({ session_id: sessionId, barcode: barcode })
            });

            const data = await res.json();
            document.getElementById('loadingSpinner').classList.add('d-none');

            if (!res.ok) {
                alert(data.error || 'Scan failed');
                if (!currentItem) document.getElementById('welcomeState').classList.remove('d-none');
                else document.getElementById('itemCard').classList.remove('d-none');
                return;
            }

            // New Item Loaded
            currentItem = data.item;

            // Initialize Count
            // If Item Mode, start at 1 (the scan we just did).
            // If Shelf Mode, we are verifying existing stock, so localCount isn't primarily displayed but we track it.
            localCount = 1;

            renderItem();

        } catch (err) {
            console.error(err);
            alert("Network Scan Error");
        }
    }

    function renderItem() {
        if (!currentItem) return;

        document.getElementById('itemName').innerText = currentItem.name;
        document.getElementById('itemSku').innerText = currentItem.sku;
        document.getElementById('systemQty').innerText = currentItem.quantity;

        // Show price if available
        const priceRow = document.getElementById('itemPriceRow');
        if (currentItem.sell_price && currentItem.sell_price > 0) {
            document.getElementById('itemPrice').innerText = '$' + parseFloat(currentItem.sell_price).toFixed(2);
            priceRow.style.display = 'block';
        } else {
            priceRow.style.display = 'none';
        }

        const img = document.getElementById('itemImg');
        if (currentItem.image_url) { img.src = currentItem.image_url; img.style.display = 'block'; }
        else { img.style.display = 'none'; }

        updateUI();
        document.getElementById('itemCard').classList.remove('d-none');
    }

    function updateUI() {
        // Update Match Button Text based on Mode
        const qtyDisplay = document.getElementById('matchQty');

        if (sessionMode === 'item') {
            // Item Mode: Show "Confirm Count (X)"
            // System Stock is reference. We are submitting OUR count.
            qtyDisplay.innerText = localCount;
            document.getElementById('instructionText').innerText = `Scanned: ${localCount} | System: ${currentItem.quantity}`;
        } else {
            // Shelf Mode: "Match (SystemQty)"
            qtyDisplay.innerText = currentItem.quantity;
            document.getElementById('instructionText').innerText = "Verify shelf quantity matches System Stock.";
        }
    }

    async function confirmMatch() {
        if (!currentItem) return;

        // In Item Mode, we submit localCount.
        // In Shelf Mode, "Match" implies confirming System Qty.
        const qtyToSubmit = (sessionMode === 'item') ? localCount : currentItem.quantity;

        await submitCount(qtyToSubmit);
    }

    function showManualInput() {
        const input = document.getElementById('manualQtyInput');
        input.value = '';
        manualModal.show();
        setTimeout(() => input.focus(), 500);

        // One-time listener addition (or re-addition safe)
        input.onkeydown = function (e) {
            if (e.key === 'Enter') {
                e.preventDefault();
                submitManualCount();
            }
        };
    }

    async function submitManualCount() {
        const qty = document.getElementById('manualQtyInput').value;
        if (qty === '') return;

        manualModal.hide();
        await submitCount(parseInt(qty));
    }

    async function submitCount(qty) {
        if (!currentItem) return;

        try {
            const res = await fetch('/inventory/audit/submit_count', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken
                },
                body: JSON.stringify({
                    session_id: sessionId,
                    item_id: currentItem.id,
                    count: qty
                })
            });

            if (res.ok) {
                // Success - reset state and allow next scan
                currentItem = null;
                localCount = 0;
                document.getElementById('itemCard').classList.add('d-none');
                document.getElementById('welcomeState').classList.remove('d-none');

                document.querySelector('#welcomeState h1').innerText = "Saved! Scan Next";
                let helperHtml = document.querySelector('#welcomeState p');
                if (helperHtml) {
                    helperHtml.innerHTML = '<span class="text-success fw-bold">✓ Inventory updated immediately</span>';
                }

                // Restore focus to scan input after a short delay
                setTimeout(() => scanInput.focus(), 200);
            } else {
                alert("Error saving count.");
            }

        } catch (err) {
            alert("Network Save Error");
        }
    }

    // --- Unsaved Handling ---

    function cancelNewScan() {
        pendingBarcode = null;
        unsavedModal.hide();
        scanInput.focus();
    }

    async function confirmAndProceed() {
        if (!pendingBarcode) return;

        // 1. Confirm current item
        await confirmMatch();

        // 2. Hide Modal
        unsavedModal.hide();

        // 3. Process new barcode
        setTimeout(() => {
            const bw = pendingBarcode;
            pendingBarcode = null;
            processScan(bw);
        }, 300);
    }

    RSCP.actions({
        toggleKeyboardBlock: toggleKeyboardBlock,
        confirmMatch: function () { confirmMatch(); },
        showManualInput: showManualInput,
        submitManualCount: function () { submitManualCount(); },
        cancelNewScan: cancelNewScan,
        confirmAndProceed: function () { confirmAndProceed(); },
        focusScanInput: function () { scanInput.focus(); }
    });
})();
