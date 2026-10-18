/* POS register / sales page (templates/pos/sales.html + _cart_details.html).
   The cart panel (#posCartContainer) and flashes are hot-swapped from fetched HTML
   (scan queue, /pos/cart/fragment), so every cart control uses delegated data-action /
   data-change / data-keydown handlers registered below.
   Needs nav.js (rscpPoll) and the Bootstrap bundle. Server data: RSCP.pageData(). */
(function () {
    'use strict';

    const pageData = RSCP.pageData() || {};

    RSCP.actions({
        // _cart_details.html (also inside hot-swapped cart fragments)
        holdOrder: function () { holdOrder(); },
        submitForm: function (el) { el.form.submit(); },
        applyCouponFromModal: function () { applyCouponFromModal(); },
        removeCoupon: function () { removeCoupon(); },
        couponModalKeydown: function (el, e) {
            if (e.key === 'Enter') {
                e.preventDefault();
                applyCouponFromModal();
            }
        },
        // Autocomplete results (built below)
        selectItem: function (el) { selectItem(el.dataset.sku); },
        // Header / pairing modal
        reprintLastOrder: function () { reprintLastOrder(); },
        unSlaveDevice: function () { unSlaveDevice(); },
        claimPairingCode: function () { claimPairingCode(); },
        startHandheldPairing: function () { startHandheldPairing(); },
        cancelHandheldPairing: function () { cancelHandheldPairing(); },
        unpairPeripheral: function (el) { unpairPeripheral(el.dataset.peripheralId); }
    });

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

    const skuInput = document.getElementById('skuInput');
    const addForm = document.getElementById('addForm');
    const autocomplete = document.getElementById('autocompleteResults');
    let debounceTimer;

    // Ultra-Fast Asynchronous Scan Queue
    const scanQueue = [];
    let isScanning = false;

    async function processScanQueue() {
        if (isScanning || scanQueue.length === 0) return;
        isScanning = true;
        
        const sku = scanQueue.shift();
        
        try {
            const formData = new URLSearchParams();
            formData.append('csrf_token', document.querySelector('input[name="csrf_token"]').value);
            formData.append('sku', sku);
            formData.append('quantity', '1');
            
            // Fetch to standard POS cart add logic
            const response = await fetch(pageData.cartAddUrl, {
                method: 'POST',
                body: formData,
                headers: {
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'X-Requested-With': 'XMLHttpRequest',
                    'X-Terminal-Id': getTerminalId()
                }
            });
            
            // Fetch follows redirects by default! It gets the new sales HTML.
            if (response.ok) {
                const html = await response.text();
                const parser = new DOMParser();
                const doc = parser.parseFromString(html, 'text/html');
                
                // Seamlessly hot-swap the cart and flash messages
                const newCart = doc.getElementById('posCartContainer');
                const newFlashes = doc.getElementById('flashMessagesContainer');
                
                if (newCart) document.getElementById('posCartContainer').innerHTML = newCart.innerHTML;
                if (newFlashes) document.getElementById('flashMessagesContainer').innerHTML = newFlashes.innerHTML;
                
                // Update CSRF token in case it rotated
                const newCsrf = doc.querySelector('input[name="csrf_token"]');
                if (newCsrf) {
                    document.querySelectorAll('input[name="csrf_token"]').forEach(el => el.value = newCsrf.value);
                }
                
                // Audible scan result evaluation
                let isSuccess = true;
                if (newFlashes) {
                    const hasError = Array.from(newFlashes.querySelectorAll('.alert-warning, .alert-danger'))
                        .some(alert => alert.textContent.includes('Item not found') || alert.textContent.includes('Please enter a SKU'));
                    if (hasError) {
                        isSuccess = false;
                    }
                }
                playScanBeep(isSuccess);
            } else {
                console.error("Scan submission failed with status:", response.status);
                playScanBeep(false);
            }
        } catch (err) {
            console.error("Error processing scan queue", err);
            playScanBeep(false);
        } finally {
            isScanning = false;
            // Maintain relentless focus on the input box
            if (skuInput) skuInput.focus();
            // Process the next item in line seamlessly
            processScanQueue();
        }
    }

    // Zero-Reload Cart Synchronization: fetch just the rendered cart
    // fragment (not the whole page) and hot-swap it in place. Driven
    // by the register version poll when a paired scanner changes the cart.
    function syncCartDOM() {
        fetch('/pos/cart/fragment', {
            headers: { 'X-Terminal-Id': getTerminalId() },
            cache: 'no-store'
        })
        .then(response => {
            if (!response.ok) throw new Error('DOM sync failed');
            return response.text();
        })
        .then(html => {
            const parser = new DOMParser();
            const doc = parser.parseFromString(html, 'text/html');
            const newCart = doc.getElementById('posCartContainer');
            const newFlashes = doc.getElementById('flashMessagesContainer');

            if (newCart) {
                document.getElementById('posCartContainer').innerHTML = newCart.innerHTML;
            }
            if (newFlashes && newFlashes.innerHTML.trim()) {
                document.getElementById('flashMessagesContainer').innerHTML = newFlashes.innerHTML;
            }
        })
        .catch(err => {
            console.error('Cart fragment sync error:', err);
        });
    }

    // Auto-queue on barcode scan (ultra-fast input without reload)
    let lastKeyTime = 0;
    let keystrokeDelays = [];
    
    skuInput.addEventListener('keydown', (e) => {
        const now = Date.now();
        if (lastKeyTime > 0) {
            const delay = now - lastKeyTime;
            keystrokeDelays.push(delay);
            if (keystrokeDelays.length > 5) {
                keystrokeDelays.shift();
            }
        }
        lastKeyTime = now;

        if (e.key === 'Enter') {
            e.preventDefault();
            const val = skuInput.value.trim();
            if (val) {
                scanQueue.push(val);
                // Clear barcode input INSTANTLY so user can zap the next item
                skuInput.value = '';
                keystrokeDelays = []; // Reset for next scan
                lastKeyTime = 0;
                processScanQueue();
            }
        }
    });

    // Search autocomplete
    skuInput.addEventListener('input', () => {
        clearTimeout(debounceTimer);
        
        // Check if input is coming extremely fast (hardware barcode scanner)
        if (keystrokeDelays.length >= 3) {
            const averageDelay = keystrokeDelays.reduce((a, b) => a + b, 0) / keystrokeDelays.length;
            if (averageDelay < 35) {
                // Suppress autocomplete search to save rendering and API cycles
                autocomplete.classList.remove('show');
                return;
            }
        }
        
        const query = skuInput.value.trim();

        if (query.length < 2) {
            autocomplete.classList.remove('show');
            return;
        }

        debounceTimer = setTimeout(() => {
            fetch(`/pos/api/search?q=${encodeURIComponent(query)}`)
                .then(r => r.json())
                .then(data => {
                    if (data.items && data.items.length > 0) {
                        autocomplete.innerHTML = data.items.map(item => {
                            const priceClass = (!item.sell_price || item.sell_price <= 0) ? 'text-danger' : '';
                            const priceText = item.sell_price?.toFixed(2) || '0.00';
                            const priceWarning = (!item.sell_price || item.sell_price <= 0) ? '⚠️ ' : '';
                            return `
                            <div class="autocomplete-item" data-sku="${escapeHtml(item.sku)}" data-action="selectItem">
                                <div>
                                    <div>${escapeHtml(item.name)}</div>
                                    <div class="sku">${escapeHtml(item.sku)}</div>
                                </div>
                                <div>
                                    <span class="stock-${escapeHtml(item.stock_status)}">${escapeHtml(item.quantity)} in stock</span>
                                    <span class="ms-2 fw-bold ${priceClass}">${priceWarning}$${priceText}</span>
                                </div>
                            </div>
                        `}).join('');
                        autocomplete.classList.add('show');
                    } else {
                        autocomplete.classList.remove('show');
                    }
                });
        }, 200);
    });

    function selectItem(sku) {
        autocomplete.classList.remove('show');
        scanQueue.push(sku);
        skuInput.value = '';
        processScanQueue();
    }

    // Close autocomplete on outside click
    document.addEventListener('click', (e) => {
        if (!e.target.closest('.search-box')) {
            autocomplete.classList.remove('show');
        }
    });

    function holdOrder() {
        new bootstrap.Modal(document.getElementById('holdModal')).show();
    }

    // Coupon functionality
    function applyCoupon() {
        const code = document.getElementById('couponCode').value.trim().toUpperCase();
        const errorDiv = document.getElementById('couponError');

        if (!code) {
            showCouponError('Please enter a coupon code.');
            return;
        }

        // Server always uses the authoritative server-side cart via get_cart(),
        // so we only need to send the coupon code.
        fetch('/pos/api/coupon/validate', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': RSCP.csrfToken(),
                'X-Terminal-Id': getTerminalId()
            },
            body: JSON.stringify({ code: code })
        })
            .then(r => r.json())
            .then(data => {
                if (data.valid) {
                    // Store coupon in session and reload
                    fetch('/pos/api/coupon/apply', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'X-CSRFToken': RSCP.csrfToken(),
                            'X-Terminal-Id': getTerminalId()
                        },
                        body: JSON.stringify({
                            coupon_id: data.coupon.id,
                            code: data.coupon.code,
                            name: data.coupon.name,
                            discount: data.discount_amount
                        })
                    })
                        .then(() => location.reload());
                } else {
                    showCouponError(data.error);
                }
            })
            .catch(err => showCouponError('Error validating coupon.'));
    }

    function removeCoupon() {
        fetch('/pos/api/coupon/remove', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': RSCP.csrfToken(),
                'X-Terminal-Id': getTerminalId()
            }
        })
            .then(() => location.reload());
    }

    function showCouponError(msg) {
        const errorDiv = document.getElementById('couponError');
        errorDiv.textContent = msg;
        errorDiv.style.display = 'block';
        setTimeout(() => errorDiv.style.display = 'none', 4000);
    }

    // Allow Enter key to apply coupon
    document.getElementById('couponCode')?.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            applyCoupon();
        }
    });

    // Enter in the modal's coupon box: data-keydown="couponModalKeydown" (delegated, so it
    // also works after the cart panel is hot-swapped).

    // Modal version of apply coupon
    function applyCouponFromModal() {
        const code = document.getElementById('couponCodeModal').value.trim().toUpperCase();
        const errorDiv = document.getElementById('couponErrorModal');

        if (!code) {
            errorDiv.textContent = 'Please enter a coupon code.';
            errorDiv.style.display = 'block';
            setTimeout(() => errorDiv.style.display = 'none', 4000);
            return;
        }

        // Server always uses the authoritative server-side cart via get_cart()
        fetch('/pos/api/coupon/validate', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': RSCP.csrfToken(),
                'X-Terminal-Id': getTerminalId()
            },
            body: JSON.stringify({ code: code })
        })
            .then(r => r.json())
            .then(data => {
                if (data.valid) {
                    fetch('/pos/api/coupon/apply', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'X-CSRFToken': RSCP.csrfToken(),
                            'X-Terminal-Id': getTerminalId()
                        },
                        body: JSON.stringify({
                            coupon_id: data.coupon.id,
                            code: data.coupon.code,
                            name: data.coupon.name,
                            discount: data.discount_amount
                        })
                    })
                        .then(() => location.reload());
                } else {
                    errorDiv.textContent = data.error;
                    errorDiv.style.display = 'block';
                    setTimeout(() => errorDiv.style.display = 'none', 4000);
                }
            })
            .catch(err => {
                errorDiv.textContent = 'Error validating coupon.';
                errorDiv.style.display = 'block';
                setTimeout(() => errorDiv.style.display = 'none', 4000);
            });
    }

    // Persistent register identity (per browser instance, not synced by Chrome)
    function getTerminalId() {
        let id = localStorage.getItem('rscp_terminal_id');
        if (!id) {
            id = 'term_' + Date.now() + '_' + Math.random().toString(36).substr(2, 9);
            localStorage.setItem('rscp_terminal_id', id);
        }
        return id;
    }

    function getFriendlyName() {
        return localStorage.getItem('rscp_staff_friendly_name') || 'Register 1';
    }

    const REGISTER_POLL_MS = 2000;
    const PERIPHERAL_REFRESH_MS = 10000;
    const SLAVE_POLL_MS = 1500;
    let registerCartVersion = -1;

    // ============================================================
    // SLAVE MODE: this device holds a scanner pairing token and runs
    // the full POS page against a paired register's cart. Same
    // localStorage keys as /pos/scan, so a device paired there slaves
    // here automatically. The server session binding (enter-slave)
    // makes every cart op + checkout target the register with no
    // per-request header, so the normal POS code path is unchanged.
    // ============================================================
    const csrfToken = RSCP.csrfToken();

    function getScannerToken() { return localStorage.getItem('rscp_scanner_token'); }

    function getScannerPeripheralId() {
        let id = localStorage.getItem('rscp_scanner_peripheral_id');
        if (!id) {
            id = 'term_scan_' + Date.now() + '_' + Math.random().toString(36).substr(2, 9);
            localStorage.setItem('rscp_scanner_peripheral_id', id);
        }
        return id;
    }

    let slaveVersion = -1;
    let slavePollTimer = null;
    let handheldPairPollTimer = null;

    async function initSlaveMode(token) {
        // Present the handheld chrome, hide register-only controls
        document.getElementById('pairingControls').classList.add('d-none');
        const banner = document.getElementById('slaveBanner');
        banner.classList.remove('d-none');
        banner.classList.add('d-flex');
        document.getElementById('slaveRegisterName').textContent =
            localStorage.getItem('rscp_scanner_register_name') || 'register';

        // Bind this session to the register (validates the token too)
        try {
            const res = await fetch('/pos/api/scanner/enter-slave', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
                body: JSON.stringify({ token: token })
            });
            if (res.status === 403) { revokedExitSlave(); return; }
            const data = await res.json();
            if (!data.success) { revokedExitSlave(); return; }
            if (data.register_name) {
                localStorage.setItem('rscp_scanner_register_name', data.register_name);
                document.getElementById('slaveRegisterName').textContent = data.register_name;
            }
            // Session is now slaved -> pull the register's cart into the DOM
            syncCartDOM();
        } catch (err) {
            console.warn('enter-slave failed, will retry via poll:', err);
        }

        startSlavePolling(token);
    }

    async function startSlavePolling(token) {
        async function tick() {
            try {
                const res = await fetch(`/pos/api/peripheral/poll?since=${slaveVersion}`, {
                    headers: { 'X-Pairing-Token': token }, cache: 'no-store'
                });
                if (res.status === 403) {
                    let revoked = false;
                    try { revoked = (await res.json()).revoked === true; } catch (e) {}
                    if (revoked) { revokedExitSlave(); return; }
                } else if (res.ok) {
                    const data = await res.json();
                    if (data.success) {
                        if (slaveVersion === -1) {
                            slaveVersion = data.version;
                        } else if (data.changed && !userIsInteractingWithCart()) {
                            slaveVersion = data.version;
                            syncCartDOM();
                        }
                    }
                }
            } catch (err) { /* offline; retry */ }
            slavePollTimer = setTimeout(tick, SLAVE_POLL_MS);
        }
        tick();
    }

    function revokedExitSlave() {
        // The register unpaired us: clear local pairing, return to register mode
        if (slavePollTimer) clearTimeout(slavePollTimer);
        localStorage.removeItem('rscp_scanner_token');
        localStorage.removeItem('rscp_scanner_register_name');
        window.location.reload();
    }

    function unSlaveDevice() {
        if (!confirm('Stop using this device as a handheld? It will return to normal register mode.')) return;
        const token = getScannerToken();
        fetch('/pos/api/scanner/exit-slave', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
            body: JSON.stringify({ token: token, unpair: true })
        }).catch(() => {}).finally(() => {
            localStorage.removeItem('rscp_scanner_token');
            localStorage.removeItem('rscp_scanner_register_name');
            window.location.reload();
        });
    }

    // --- Become a handheld: show a code for a register to claim ---
    function startHandheldPairing() {
        const pid = getScannerPeripheralId();
        fetch('/pos/api/pairing/request-code', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
            body: JSON.stringify({ peripheral_id: pid, role: 'scanner' })
        })
        .then(r => r.json())
        .then(data => {
            if (!data.success) { alert('Could not get a pairing code: ' + (data.message || '')); return; }
            const code = data.pairing_code;
            document.getElementById('handheldPairCode').textContent = code.slice(0, 3) + ' ' + code.slice(3);
            document.getElementById('handheldPairIdle').style.display = 'none';
            document.getElementById('handheldPairActive').style.display = 'block';
            pollHandheldClaimed(pid);
        })
        .catch(err => alert('Error requesting pairing code: ' + err));
    }

    function pollHandheldClaimed(peripheralId) {
        if (handheldPairPollTimer) clearTimeout(handheldPairPollTimer);
        fetch(`/pos/api/pairing/status?peripheral_id=${encodeURIComponent(peripheralId)}`, { cache: 'no-store' })
        .then(r => r.json())
        .then(data => {
            if (data.success && data.paired && data.token) {
                localStorage.setItem('rscp_scanner_token', data.token);
                if (data.register_name) localStorage.setItem('rscp_scanner_register_name', data.register_name);
                fetch('/pos/api/pairing/ack', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
                    body: JSON.stringify({ peripheral_id: peripheralId, token: data.token })
                }).catch(() => {}).finally(() => window.location.reload());
                return;
            }
            handheldPairPollTimer = setTimeout(() => pollHandheldClaimed(peripheralId), 2000);
        })
        .catch(() => { handheldPairPollTimer = setTimeout(() => pollHandheldClaimed(peripheralId), 2000); });
    }

    function cancelHandheldPairing() {
        if (handheldPairPollTimer) clearTimeout(handheldPairPollTimer);
        document.getElementById('handheldPairActive').style.display = 'none';
        document.getElementById('handheldPairIdle').style.display = 'block';
    }

    // --- HELLO: announce this register (also binds the register id to
    // the server session so plain form posts hit the same cart) ---
    function registerHello(name) {
        return fetch('/pos/api/register/hello', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': RSCP.csrfToken()
            },
            body: JSON.stringify({
                register_id: getTerminalId(),
                friendly_name: name || getFriendlyName()
            })
        }).catch(err => console.warn('Register hello failed:', err));
    }

    // --- CART VERSION POLL: pick up changes made by paired scanners ---
    function userIsInteractingWithCart() {
        const container = document.getElementById('posCartContainer');
        const active = document.activeElement;
        if (active && container && container.contains(active) &&
            ['INPUT', 'TEXTAREA', 'SELECT'].includes(active.tagName)) return true;
        if (document.querySelector('.modal.show')) return true;
        return false;
    }

    async function pollRegisterCart(signal) {
        try {
            const res = await fetch(`/pos/api/register/poll?since=${registerCartVersion}`, {
                headers: { 'X-Terminal-Id': getTerminalId() },
                cache: 'no-store',
                signal
            });
            if (res.ok) {
                const data = await res.json();
                if (data.success) {
                    if (registerCartVersion === -1) {
                        // Baseline: this page's HTML was just rendered server-side
                        registerCartVersion = data.version;
                    } else if (data.changed && !userIsInteractingWithCart()) {
                        registerCartVersion = data.version;
                        syncCartDOM();
                    }
                    // If changed while the user is mid-interaction, the
                    // version stays stale and we retry next tick.
                }
            }
        } catch (err) { /* offline or timed out; retry next tick */ }
    }

    // --- PAIRED DEVICES: status badge + modal list ---
    function renderPeripherals(peripherals) {
        const list = document.getElementById('peripheralList');
        if (list) {
            if (!peripherals.length) {
                list.innerHTML = '<div class="text-muted small">No devices paired to this register yet.</div>';
            } else {
                list.innerHTML = peripherals.map(p => `
                    <div class="d-flex align-items-center justify-content-between bg-white border rounded p-2">
                        <div class="d-flex align-items-center gap-2">
                            <span>${p.role === 'display' ? '📺' : '🤳'}</span>
                            <div>
                                <div class="small fw-semibold">${escapeHtml(p.friendly_name)}</div>
                                <div class="text-muted" style="font-size: 0.7rem;">${p.connected ? '🟢 Connected' : '⚪ Offline'}</div>
                            </div>
                        </div>
                        <button type="button" class="btn btn-outline-danger btn-sm" data-peripheral-id="${escapeHtml(p.peripheral_id)}" data-action="unpairPeripheral">Unpair</button>
                    </div>
                `).join('');
            }
        }

        // Header badge
        const badge = document.getElementById('persistentDisplayBadge');
        if (!badge) return;
        const total = peripherals.length;
        const connected = peripherals.filter(p => p.connected).length;
        const offline = total - connected;
        badge.classList.remove('btn-outline-light', 'btn-outline-success', 'btn-outline-warning', 'btn-outline-danger');
        if (total === 0) {
            badge.classList.add('btn-outline-light');
            badge.innerHTML = `📺 Pair Devices ⚪`;
        } else if (offline === 0) {
            badge.classList.add('btn-outline-success');
            badge.innerHTML = `📺 Devices: ${connected} Connected 🟢`;
        } else if (connected > 0) {
            badge.classList.add('btn-outline-warning');
            badge.innerHTML = `📺 ${connected} Connected, ${offline} Offline ⚠️`;
        } else {
            badge.classList.add('btn-outline-danger');
            badge.innerHTML = `📺 Devices Offline 🔴`;
        }
    }

    function refreshPeripherals(signal) {
        return fetch(`/pos/api/register/peripherals?register_id=${encodeURIComponent(getTerminalId())}`, { signal })
            .then(r => r.json())
            .then(data => { if (data.success) renderPeripherals(data.peripherals || []); })
            .catch(err => console.warn('Peripheral status error:', err));
    }

    function unpairPeripheral(peripheralId) {
        if (!confirm('Unpair this device? Its screen will return to pairing mode.')) return;
        fetch('/pos/api/register/unpair', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': RSCP.csrfToken()
            },
            body: JSON.stringify({ peripheral_id: peripheralId, register_id: getTerminalId() })
        })
        .then(r => r.json())
        .then(() => refreshPeripherals())
        .catch(err => console.error('Unpair error:', err));
    }

    // --- CLAIM: pair a display or scanner by its 6-digit code ---
    function claimPairingCode() {
        const codeInput = document.getElementById('posDisplayPairCode');
        const errDiv = document.getElementById('persistentPairError');
        const succDiv = document.getElementById('persistentPairSuccess');
        if (!codeInput) return;

        const rawCode = codeInput.value.replace(/\D/g, '');
        if (rawCode.length !== 6) {
            if (errDiv) {
                errDiv.textContent = 'Please enter a valid 6-digit code (e.g. 123 456).';
                errDiv.style.display = 'block';
            }
            if (succDiv) succDiv.style.display = 'none';
            return;
        }

        if (errDiv) errDiv.style.display = 'none';
        if (succDiv) succDiv.style.display = 'none';

        fetch('/pos/api/register/claim-code', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': RSCP.csrfToken()
            },
            body: JSON.stringify({
                code: rawCode,
                register_id: getTerminalId(),
                friendly_name: getFriendlyName()
            })
        })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                if (succDiv) {
                    succDiv.textContent = data.message || 'Device paired!';
                    succDiv.style.display = 'block';
                }
                codeInput.value = '';
                refreshPeripherals();
                setTimeout(() => { if (succDiv) succDiv.style.display = 'none'; }, 4000);
            } else {
                if (errDiv) {
                    errDiv.textContent = data.message || 'Failed to pair device.';
                    errDiv.style.display = 'block';
                }
            }
        })
        .catch(err => {
            console.error('Error claiming pairing code:', err);
            if (errDiv) {
                errDiv.textContent = 'Network error pairing device.';
                errDiv.style.display = 'block';
            }
        });
    }

    // --- INIT ---
    document.addEventListener('DOMContentLoaded', () => {
        const friendlyNameInput = document.getElementById('staffFriendlyNameInput');
        const posDisplayPairCode = document.getElementById('posDisplayPairCode');
        const pairingMenuModalEl = document.getElementById('pairingMenuModal');

        // Register friendly name: load, debounce-save, propagate
        if (friendlyNameInput) {
            friendlyNameInput.value = getFriendlyName();
            let nameDebounce;
            friendlyNameInput.addEventListener('input', () => {
                clearTimeout(nameDebounce);
                nameDebounce = setTimeout(() => {
                    const newName = friendlyNameInput.value.trim() || 'Register 1';
                    localStorage.setItem('rscp_staff_friendly_name', newName);
                    registerHello(newName);
                }, 500);
            });
        }

        // Pairing code input: "123 456" formatting + Enter to pair
        if (posDisplayPairCode) {
            posDisplayPairCode.addEventListener('input', () => {
                let val = posDisplayPairCode.value.replace(/\D/g, '');
                if (val.length > 3) val = val.substring(0, 3) + ' ' + val.substring(3, 6);
                posDisplayPairCode.value = val;
            });
            posDisplayPairCode.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    claimPairingCode();
                }
            });
        }

        if (pairingMenuModalEl) {
            pairingMenuModalEl.addEventListener('shown.bs.modal', () => {
                refreshPeripherals();
                if (posDisplayPairCode) posDisplayPairCode.focus();
            });
            pairingMenuModalEl.addEventListener('hidden.bs.modal', () => {
                const errDiv = document.getElementById('persistentPairError');
                const succDiv = document.getElementById('persistentPairSuccess');
                if (errDiv) errDiv.style.display = 'none';
                if (succDiv) succDiv.style.display = 'none';
                if (posDisplayPairCode) posDisplayPairCode.value = '';
            });
        }

        // MODE SWITCH: a scanner pairing token means this device is a
        // handheld slaved to another register; otherwise it's a register.
        if (getScannerToken()) {
            initSlaveMode(getScannerToken());
        } else {
            // Announce this register, then start the sync + status loops
            // (they pause while the tab is hidden and catch up when it's shown)
            registerHello();
            rscpPoll(pollRegisterCart, REGISTER_POLL_MS);
            rscpPoll(refreshPeripherals, PERIPHERAL_REFRESH_MS);
        }
    });

    // --- AUTO-PRINT & REPRINT FUNCTIONALITY ---
    function printOrder(orderNumber) {
        if (!orderNumber) return;
        const printUrl = `/pos/receipt/${orderNumber}/print`;
        const iframe = document.createElement('iframe');
        iframe.style.position = 'fixed';
        iframe.style.width = '0';
        iframe.style.height = '0';
        iframe.style.border = '0';
        iframe.src = printUrl;
        document.body.appendChild(iframe);
        iframe.onload = function() {
            setTimeout(() => {
                document.body.removeChild(iframe);
            }, 60000); // 1-minute safety window
        };
    }

    function reprintLastOrder() {
        const lastOrder = localStorage.getItem('rscp_last_order');
        if (lastOrder) {
            printOrder(lastOrder);
        }
    }

    function initAutoPrintAndReprint() {
        const urlParams = new URLSearchParams(window.location.search);
        const autoPrintOrder = urlParams.get('auto_print_order');
        
        // 1. Save and clean URL instantly to prevent duplicate printing on F5/refresh
        if (autoPrintOrder) {
            localStorage.setItem('rscp_last_order', autoPrintOrder);
            const cleanUrl = window.location.pathname;
            window.history.replaceState({}, document.title, cleanUrl);
            
            // 2. Trigger auto print
            printOrder(autoPrintOrder);
        }
        
        // 3. Initialize reprint button visibility
        const lastOrder = localStorage.getItem('rscp_last_order');
        const reprintBtn = document.getElementById('reprintBtn');
        const reprintOrderNum = document.getElementById('reprintOrderNum');
        if (lastOrder && reprintBtn && reprintOrderNum) {
            reprintOrderNum.textContent = lastOrder;
            reprintBtn.style.display = 'inline-block';
        }
    }

    document.addEventListener('DOMContentLoaded', initAutoPrintAndReprint);
})();
