/* templates/scan.html (receiving scanner; standalone page) */
(function () {
    'use strict';

    const PAGE = RSCP.pageData();

    // --- SOUND SYNTHESIS (WEB AUDIO API) ---
    let audioCtx = null;
    function initAudioContext() {
        if (!audioCtx) {
            audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        }
        if (audioCtx.state === 'suspended') {
            audioCtx.resume();
        }
    }
    document.addEventListener('click', initAudioContext, { once: true });
    document.addEventListener('keydown', initAudioContext, { once: true });
    document.addEventListener('touchstart', initAudioContext, { once: true });

    function playScanBeep(success) {
        try {
            initAudioContext();
            if (!audioCtx) return;
            if (audioCtx.state === 'suspended') audioCtx.resume();
            
            const osc = audioCtx.createOscillator();
            const gain = audioCtx.createGain();
            osc.connect(gain);
            gain.connect(audioCtx.destination);
            
            const now = audioCtx.currentTime;
            if (success) {
                osc.type = 'sine';
                osc.frequency.setValueAtTime(1000, now);
                gain.gain.setValueAtTime(0.15, now);
                gain.gain.exponentialRampToValueAtTime(0.001, now + 0.12);
                osc.start(now);
                osc.stop(now + 0.12);
            } else {
                osc.type = 'sawtooth';
                osc.frequency.setValueAtTime(150, now);
                gain.gain.setValueAtTime(0.25, now);
                gain.gain.exponentialRampToValueAtTime(0.001, now + 0.25);
                osc.start(now);
                osc.stop(now + 0.25);
            }
        } catch (e) {
            console.warn('Audio synthesis block:', e);
        }
    }

    // --- SETUP ---
    // Log the item(s) this page was rendered for into the session's recent-scans list
    // (AJAX scans do the same from the response, in the submit handler below).
    PAGE.scannedItems.forEach(name => {
        const itemLog = { name: name, time: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) };
        let history = JSON.parse(sessionStorage.getItem('rscp_session') || "[]");
        if (history.length === 0 || history[0].name !== itemLog.name) {
            history.unshift(itemLog);
            if (history.length > 3) history.pop();
            sessionStorage.setItem('rscp_session', JSON.stringify(history));
        }
    });

    if (localStorage.getItem('rscp_darkmode') === 'true') document.body.classList.add('dark-mode');

    const u = localStorage.getItem('dockops_user');
    // User display is optional - server session handles authentication
    if (u) { document.getElementById('userInput').value = u; document.getElementById('userDisplay').innerText = u; }

    function renderHistory() {
        // Respect the HISTORY: OFF toggle. renderHistory() is called after
        // every scan, so without this guard a hidden history reappears on
        // the next scan.
        if (localStorage.getItem('rscp_hide_history') === 'true') {
            document.getElementById('sessionHistory').style.display = 'none';
            return;
        }
        const hist = JSON.parse(sessionStorage.getItem('rscp_session') || "[]");
        if (hist.length > 0) {
            document.getElementById('sessionHistory').style.display = 'block';
            document.getElementById('histList').innerHTML = hist.map(i => `<div>- ${escapeHtml(i.name)} (${escapeHtml(i.time)})</div>`).join('');
        } else {
            document.getElementById('sessionHistory').style.display = 'none';
        }
    }

    // V13.7 FIX: Pass event, stop propagation to prevent background focus listener
    function clearSession(e) {
        if (e) e.stopPropagation();
        sessionStorage.removeItem('rscp_session');
        renderHistory();
    }
    renderHistory();

    let isSubmitting = false;
    const input = document.getElementById('scanInput');
    const form = document.getElementById('scanForm');
    let scanTimeout = null;

    form.addEventListener('submit', async function (e) {
        e.preventDefault();
        if (isSubmitting) return;

        const val = input.value.trim();
        if (!val) return;

        isSubmitting = true;
        // Clear input box instantly so the user can scan the next item right away!
        input.value = '';

        try {
            const formData = new FormData(form);
            formData.set('tracking_input', val);

            const response = await fetch(form.action || '/scan', {
                method: 'POST',
                body: formData,
                headers: {
                    'X-Requested-With': 'XMLHttpRequest'
                }
            });

            if (response.ok) {
                const html = await response.text();
                const parser = new DOMParser();
                const doc = parser.parseFromString(html, 'text/html');

                const newBody = doc.body;
                const newBgColor = newBody.style.backgroundColor;
                const newClass = newBody.className;

                document.body.style.backgroundColor = newBgColor;
                document.body.className = newClass;

                const oldBanner = document.querySelector('.priority-banner');
                const newBanner = doc.querySelector('.priority-banner');
                if (oldBanner) oldBanner.remove();
                if (newBanner) {
                    document.body.insertBefore(newBanner, document.body.firstChild);
                }

                const newContainer = doc.querySelector('.item-container');
                if (newContainer) {
                    document.querySelector('.item-container').innerHTML = newContainer.innerHTML;
                }

                const newCsrf = doc.querySelector('input[name="csrf_token"]');
                if (newCsrf) {
                    document.querySelector('input[name="csrf_token"]').value = newCsrf.value;
                }

                const oldDebug = document.querySelector('div[style*="font-family: monospace"]');
                const newDebug = doc.querySelector('div[style*="font-family: monospace"]');
                if (oldDebug) oldDebug.remove();
                if (newDebug && newContainer) {
                    newContainer.parentNode.insertBefore(newDebug, newContainer);
                }

                if (newBgColor) {
                    const isSuccess = (newBgColor === "#77dd77" || newBgColor === "rgb(119, 221, 119)" ||
                                       newBgColor === "#C77DFF" || newBgColor === "rgb(199, 125, 255)" ||
                                       newBgColor === "#6f42c1" || newBgColor === "rgb(111, 66, 193)");
                    const isFailure = (newBgColor === "#ff6961" || newBgColor === "rgb(255, 105, 97)" ||
                                       newBgColor === "#ffd700" || newBgColor === "rgb(255, 215, 0)");
                    if (isSuccess || isFailure) {
                        playScanBeep(isSuccess);
                    }
                }

                if (newBgColor === "rgb(255, 105, 97)" || newBgColor === "#ff6961" || 
                    newBgColor === "rgb(255, 215, 0)" || newBgColor === "#ffd700") {
                    if (navigator.vibrate) navigator.vibrate([200, 100, 200]);
                }

                const newItems = doc.querySelectorAll('.item-card h2');
                if (newItems.length > 0) {
                    newItems.forEach(h2 => {
                        const itemName = h2.textContent.trim();
                        const itemLog = { name: itemName, time: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) };
                        let history = JSON.parse(sessionStorage.getItem('rscp_session') || "[]");
                        if (history.length === 0 || history[0].name !== itemLog.name) {
                            history.unshift(itemLog);
                            if (history.length > 3) history.pop();
                            sessionStorage.setItem('rscp_session', JSON.stringify(history));
                        }
                    });
                    renderHistory();
                }

            } else {
                console.error("AJAX Scan failed:", response.status);
                playScanBeep(false);
            }
        } catch (err) {
            console.error("AJAX Scan error:", err);
            playScanBeep(false);
        } finally {
            isSubmitting = false;
            input.focus();
        }
    });

    // --- KEYBOARD BLOCKER LOGIC ---
    let blockKeyboard = localStorage.getItem('rscp_block_kbd') === 'true';

    function applyKeyboardState() {
        const statusLabel = document.getElementById('kbdStatus');
        if (blockKeyboard) {
            statusLabel.innerText = "KBD: BLOCKED";
            statusLabel.style.color = "red";
        } else {
            statusLabel.innerText = "KBD: ALLOWED";
            statusLabel.style.color = "green";
        }
        localStorage.setItem('rscp_block_kbd', blockKeyboard);
        input.focus();
    }

    function toggleKeyboardBlock() {
        blockKeyboard = !blockKeyboard;
        applyKeyboardState();
    }

    // --- HISTORY DISPLAY TOGGLE ---
    let hideHistory = localStorage.getItem('rscp_hide_history') === 'true';

    function applyHistoryDisplayState() {
        const statusLabel = document.getElementById('histStatus');
        if (hideHistory) {
            statusLabel.innerText = "HISTORY: OFF";
            statusLabel.style.color = "red";
            document.getElementById('sessionHistory').style.display = 'none';
        } else {
            statusLabel.innerText = "HISTORY: ON";
            statusLabel.style.color = "green";
            renderHistory();
        }
        localStorage.setItem('rscp_hide_history', hideHistory);
    }

    function toggleHistoryDisplay() {
        hideHistory = !hideHistory;
        applyHistoryDisplayState();
    }

    window.onload = function () {
        applyKeyboardState();
        applyHistoryDisplayState();
        
        // Audible Feedback on scan result
        const bg = PAGE.color;
        if (bg) {
            const isSuccess = (bg === "#77dd77" || bg === "#C77DFF" || bg === "#6f42c1");
            const isFailure = (bg === "#ff6961" || bg === "#ffd700");
            if (isSuccess || isFailure) {
                playScanBeep(isSuccess);
            }
        }
        
        // V14.6: Haptic Feedback on Error/Duplicate
        if (bg === "#ff6961" || bg === "#ffd700") {
            if (navigator.vibrate) navigator.vibrate([200, 100, 200]);
        }
    };

    // V13.7 FIX: Exclude the clear button from the background focus listener
    document.addEventListener('click', function (e) {
        // Exclude modal interactions
        if (document.getElementById('linkModal').style.display === 'block') return;

        if (!isSubmitting && !e.target.classList.contains('mode-toggle') && !e.target.classList.contains('clear-btn')) {
            input.focus();
        }
    });

    input.addEventListener('focus', function () {
        if (blockKeyboard) {
            input.setAttribute('readonly', 'readonly');
            setTimeout(() => { input.removeAttribute('readonly'); }, 50);
        } else {
            input.removeAttribute('readonly');
        }
    });

    input.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') {
            e.preventDefault();
            clearTimeout(scanTimeout);
            const val = input.value.trim();
            if (val && !isSubmitting) {
                form.requestSubmit();
            }
        }
    });

    input.addEventListener('input', function () {
        clearTimeout(scanTimeout);
        scanTimeout = setTimeout(function () {
            if (input.value.length > 5 && !isSubmitting) form.requestSubmit();
        }, 100);
    });

    // Inventory Integration
    function addToInventory(asin, tracking, qty) {
        if (!asin) {
            // No ASIN - go directly to add form
            window.location.href = '/inventory/add?tracking=' + encodeURIComponent(tracking) + '&qty=' + qty;
            return;
        }

        // Check if ASIN exists in inventory
        fetch('/inventory/api/match/' + encodeURIComponent(asin))
            .then(response => response.json())
            .then(data => {
                if (data.found) {
                    // Item exists - prompt to add quantity
                    if (confirm('Add ' + qty + ' to existing inventory?\n\n' +
                        'Item: ' + data.item.name + '\n' +
                        'Current Qty: ' + data.item.quantity + '\n' +
                        'Location: ' + data.item.location)) {
                        // Add quantity via API
                        fetch('/inventory/api/add_quantity/' + data.item.id, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': RSCP.csrfToken() },
                            body: JSON.stringify({ quantity: qty, tracking: tracking })
                        })
                            .then(r => r.json())
                            .then(result => {
                                if (result.success) {
                                    alert('Updated! New quantity: ' + result.new_quantity);
                                } else {
                                    alert('Error: ' + (result.error || 'Unknown error'));
                                }
                            });
                    }
                } else {
                    // Not found - go to add form with ASIN prefilled
                    window.location.href = '/inventory/add?asin=' + encodeURIComponent(asin) +
                        '&tracking=' + encodeURIComponent(tracking) +
                        '&qty=' + qty;
                }
            })
            .catch(err => {
                console.error('Inventory check error:', err);
                // Fallback to add form
                window.location.href = '/inventory/add?asin=' + encodeURIComponent(asin) +
                    '&tracking=' + encodeURIComponent(tracking) +
                    '&qty=' + qty;
            });
    }

    // V1.16.1: Quick Inventory Adjustments from Scan
    const csrfToken = document.querySelector('input[name="csrf_token"]').value;

    function quickAdjust(itemId, change) {
        fetch('/inventory/api/quick_adjust/' + itemId, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrfToken
            },
            body: JSON.stringify({ change: change })
        })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    document.getElementById('invQty').textContent = data.new_quantity;
                    // Brief visual feedback
                    const qtyEl = document.getElementById('invQty');
                    qtyEl.style.color = change > 0 ? '#28a745' : '#dc3545';
                    setTimeout(() => { qtyEl.style.color = ''; }, 500);
                } else {
                    alert('Error: ' + (data.error || 'Unknown error'));
                }
            })
            .catch(err => alert('Request failed: ' + err));
    }

    // Custom quantity input via modal popup (mobile-friendly)
    var currentItemId = PAGE.currentItemId;
    var qtyBeforeEdit = 0;

    function promptCustomQty(itemId) {
        if (itemId) {
            currentItemId = itemId;
        }
        const current = parseInt(document.getElementById('invQty').textContent) || 0;
        qtyBeforeEdit = current;
        const qtyInput = document.getElementById('qtyInput');
        qtyInput.value = current;

        // Show modal
        document.getElementById('qtyEditBox').style.display = 'block';
        document.getElementById('qtyEditOverlay').style.display = 'block';

        // Focus and select after a small delay (helps mobile)
        setTimeout(() => {
            qtyInput.focus();
            qtyInput.select();
        }, 100);
    }

    function cancelQtyEdit() {
        document.getElementById('qtyEditBox').style.display = 'none';
        document.getElementById('qtyEditOverlay').style.display = 'none';
    }

    function applyQtyChange() {
        const qtyInput = document.getElementById('qtyInput');
        const newQty = parseInt(qtyInput.value);
        if (isNaN(newQty) || newQty < 0) {
            cancelQtyEdit();
            return;
        }

        const change = newQty - qtyBeforeEdit;
        cancelQtyEdit();

        if (change === 0) return;
        quickAdjust(currentItemId, change);
    }
    function markOOS(itemId) {
        if (!confirm('Mark this item as Out of Stock?')) return;

        fetch('/inventory/api/quick_adjust/' + itemId, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrfToken
            },
            body: JSON.stringify({ action: 'oos' })
        })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    document.getElementById('invQty').textContent = '0';
                    document.getElementById('invQty').style.color = '#dc3545';
                    alert('Item marked as Out of Stock');
                } else {
                    alert('Error: ' + (data.error || 'Unknown error'));
                }
            })
            .catch(err => alert('Request failed: ' + err));
    }

    // --- LINK ITEM LOGIC ---
    let currentLinkTracking = null;
    let currentLinkPackageName = null;
    let selectedLinkSku = null;

    function openLinkModal(tracking, name) {
        currentLinkTracking = tracking;
        currentLinkPackageName = name;
        selectedLinkSku = null;

        document.getElementById('pkgNameDisplay').innerText = name;
        document.getElementById('linkModal').style.display = 'block';
        document.getElementById('linkSearchInput').value = '';
        document.getElementById('linkSearchResults').style.display = 'none';
        document.getElementById('linkSelection').style.display = 'none';
        document.getElementById('btnSubmitLink').disabled = true;
        document.getElementById('btnSubmitLink').style.opacity = '0.5';
        document.getElementById('saveMappingCheckbox').checked = false;

        setTimeout(() => document.getElementById('linkSearchInput').focus(), 100);
    }

    function closeLinkModal() {
        document.getElementById('linkModal').style.display = 'none';
        input.focus();
    }

    let searchDebounce;
    function debounceSearch() {
        clearTimeout(searchDebounce);
        searchDebounce = setTimeout(performLinkSearch, 300);
    }

    function performLinkSearch() {
        const query = document.getElementById('linkSearchInput').value.trim();
        if (query.length < 2) return;

        fetch(`/inventory/items?q=${encodeURIComponent(query)}&page=1&per_page=10`)
            .then(r => r.text())
            .then(html => {
                // Note: fetching full HTML page logic (items.py) returns full page or partial rows
                // We need to parse this or update "items.py" to return JSON. 
                // To avoid backend complexity, we will cheat and use a regex or DOM parser if list returns items?
                // Actually, items.py returns `render_template`. We should modify items.py to return JSON if requested?
                // OR: Just create a new simple API endpoint. But wait, we can't edit backend easily without restart issues maybe.
                // Alternate: Parse the HTML. It's safe-ish.

                // BETTER: Use the existing rows, but we need to strip the layout.
                // Let's assume user types exact SKU or Name.
            })
            .catch(e => console.error(e));

        // HACK: Since we didn't make a JSON search endpoint, let's use a quick fetch to the existing list page with `partial=1`
        // But `list_items` returns `_list_rows.html`. We can parse that with DOMParser.

        fetch(`/inventory/items?q=${encodeURIComponent(query)}&partial=1`)
            .then(r => r.text())
            .then(html => {
                const parser = new DOMParser();
                const doc = parser.parseFromString('<table>' + html + '</table>', 'text/html');
                const rows = doc.querySelectorAll('tr');
                const resultsParams = [];

                rows.forEach(tr => {
                    // Col 3 is SKU (index 2), Col 4 is Name (index 3)
                    const cells = tr.querySelectorAll('td');
                    let sku = 'Unknown';
                    let name = 'Unknown';

                    if (cells.length >= 4) {
                        sku = cells[2].innerText.trim();
                        name = cells[3].innerText.trim();
                    }

                    resultsParams.push({ name, sku });
                });

                renderLinkResults(resultsParams);
            });
    }

    function renderLinkResults(items) {
        const container = document.getElementById('linkSearchResults');
        container.innerHTML = '';
        container.style.display = 'block';

        if (items.length === 0) {
            container.innerHTML = '<div style="padding:10px; color:#999; text-align:center;">No match found</div>';
            return;
        }

        items.forEach(item => {
            const div = document.createElement('div');
            div.style.padding = '10px';
            div.style.borderBottom = '1px solid #eee';
            div.style.cursor = 'pointer';
            div.innerHTML = `<strong>${escapeHtml(item.name)}</strong><br><small style="color:#666;">${escapeHtml(item.sku)}</small>`;
            div.onclick = () => selectInventoryItem(item);
            container.appendChild(div);
        });
    }

    function selectInventoryItem(item) {
        selectedLinkSku = item.sku;
        document.getElementById('selectedItemName').innerText = item.name;
        document.getElementById('selectedItemSku').innerText = item.sku;
        document.getElementById('linkSelection').style.display = 'block';
        document.getElementById('linkSearchResults').style.display = 'none';
        document.getElementById('btnSubmitLink').disabled = false;
        document.getElementById('btnSubmitLink').style.opacity = '1';
    }

    function submitLink() {
        if (!selectedLinkSku || !currentLinkTracking) return;
        const saveMapping = document.getElementById('saveMappingCheckbox').checked;

        fetch('/receiving/link_item', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrfToken
            },
            body: JSON.stringify({
                tracking: currentLinkTracking,
                sku: selectedLinkSku,
                save_mapping: saveMapping,
                package_name: currentLinkPackageName
            })
        })
            .then(r => r.json())
            .then(data => {
                if (data.status === 'success') {
                    // correct close
                    closeLinkModal();
                    alert("Linked successfully! Inventory updated.");
                    // Redirect to clean scan page to avoid "Duplicate" (Yellow) warning from re-submission
                    window.location.href = '/scan';
                } else {
                    alert("Error: " + data.message);
                }
            })
            .catch(e => alert("Network Error: " + e));
    }

    // Markup handlers (data-action / data-input)
    RSCP.actions({
        toggleKeyboardBlock: function () { toggleKeyboardBlock(); },
        toggleHistoryDisplay: function () { toggleHistoryDisplay(); },
        clearSession: function (el, event) {
            // Stop the document-level listeners (background focus, nav dropdown,
            // audio unlock) like the old inline handler's stopPropagation did.
            event.stopImmediatePropagation();
            clearSession();
        },
        openLinkModal: function (el) { openLinkModal(el.dataset.tracking, el.dataset.name); },
        closeLinkModal: function () { closeLinkModal(); },
        debounceSearch: function () { debounceSearch(); },
        submitLink: function () { submitLink(); },
        promptCustomQty: function (el) { promptCustomQty(el.dataset.id); },
        quickAdjust: function (el) { quickAdjust(el.dataset.id, parseInt(el.dataset.delta, 10)); },
        markOOS: function (el) { markOOS(el.dataset.id); },
        cancelQtyEdit: function () { cancelQtyEdit(); },
        applyQtyChange: function () { applyQtyChange(); }
    });
})();
