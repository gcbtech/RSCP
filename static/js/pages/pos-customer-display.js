/* POS customer display (templates/pos/customer_display.html): public full-screen kiosk,
   no nav; rscp-common.js (escapeHtml, RSCP.actions, CSRF meta) comes from base.html. */
(function () {
    'use strict';

    RSCP.actions({
        unpairAndExit: function () { unpairAndExit(); }
    });


    // ============================================================
    // POS Customer Display (pairing v3)
    //
    // One self-scheduling poll loop is the entire sync engine.
    // Design rules that make overnight power-offs a non-event:
    //   1. Credentials live in localStorage and are wiped ONLY on an
    //      authoritative 403 {revoked} from the server (or the staff
    //      escape button) - never on network errors or 5xx.
    //   2. Every failure keeps retrying forever with capped backoff;
    //      the display can boot before the server and still recover.
    //   3. Waking up (tablet sleep, network back) triggers an
    //      immediate poll instead of waiting out the backoff.
    // ============================================================

    const csrfToken = RSCP.csrfToken();

    const POLL_MS = 1200;              // active cart poll cadence
    const BACKOFF_MAX_MS = 5000;       // never wait longer than this to retry
    const OVERLAY_AFTER_FAILURES = 4;  // show the reconnect overlay after ~5s of failures
    const PAIRING_STATUS_MS = 2000;    // "am I paired yet?" cadence
    const IDLE_RELOAD_HOURS = 24;      // kiosk hygiene reload (only when idle + connected)

    const LS_PERIPHERAL_ID = 'rscp_customer_terminal_id';
    const LS_TOKEN = 'rscp_customer_terminal_token';
    const LS_REGISTER_NAME = 'rscp_staff_friendly_name';

    let cartVersion = -1;
    let consecutiveFailures = 0;
    let pollTimer = null;
    let pairingTimers = [];
    let paired = false;
    const bootTime = Date.now();
    let lastCartEmpty = true;

    function getPeripheralId() {
        let id = localStorage.getItem(LS_PERIPHERAL_ID);
        if (!id) {
            id = 'term_cust_' + Date.now() + '_' + Math.random().toString(36).substr(2, 9);
            localStorage.setItem(LS_PERIPHERAL_ID, id);
        }
        return id;
    }

    function getToken() {
        return localStorage.getItem(LS_TOKEN);
    }

    function formatPrice(amount) {
        return '$' + (parseFloat(amount) || 0).toFixed(2);
    }

    function setStatusBadge(cls, text) {
        const badge = document.getElementById('connectionStatus');
        badge.className = 'connection-status ' + cls;
        badge.textContent = text;
    }

    // --- RENDER CART (consumes compute_cart_totals payload) ---
    function updateCartDisplay(cart) {
        const cartArea = document.getElementById('cartItems');
        const items = cart.items || [];
        lastCartEmpty = items.length === 0;

        if (items.length === 0) {
            cartArea.innerHTML = `
                <div class="empty-cart">
                    <div class="icon">🛒</div>
                    <div>Waiting for items...</div>
                </div>
            `;
            document.getElementById('subtotal').textContent = '$0.00';
            document.getElementById('discountRow').style.display = 'none';
            document.getElementById('couponRow').style.display = 'none';
            document.getElementById('tax').textContent = '$0.00';
            document.getElementById('taxLabel').textContent = 'Tax';
            document.getElementById('cardTotal').textContent = '$0.00';
            document.getElementById('cardTotalLabel').textContent = 'Total';
            document.getElementById('cashTotalRow').style.display = 'none';
            return;
        }

        let html = '';
        items.forEach(item => {
            const lineTotal = parseFloat(item.line_total) || 0;
            const hasItemDiscount = item.discount_amount && item.discount_type;
            html += `
                <div class="cart-item">
                    <div>
                        <div class="cart-item-name">${escapeHtml(item.name)}</div>
                        <div class="cart-item-qty">${escapeHtml(String(item.quantity))}x @ ${formatPrice(item.unit_price)}${hasItemDiscount ? ' · discounted' : ''}</div>
                    </div>
                    <div class="cart-item-price">${formatPrice(lineTotal)}</div>
                </div>
            `;
        });
        cartArea.innerHTML = html;

        document.getElementById('subtotal').textContent = formatPrice(cart.subtotal);

        // Order-level discount
        const orderDiscount = cart.order_discount || 0;
        document.getElementById('discount').textContent = '-' + formatPrice(orderDiscount);
        document.getElementById('discountRow').style.display = orderDiscount > 0 ? 'flex' : 'none';

        // Coupon (shown separately so customers see their code applied)
        const couponDiscount = cart.coupon_discount || 0;
        if (couponDiscount > 0) {
            const code = (cart.applied_coupon && cart.applied_coupon.code) ? ` (${escapeHtml(cart.applied_coupon.code)})` : '';
            document.getElementById('couponLabel').innerHTML = '🎟️ Coupon' + code;
            document.getElementById('coupon').textContent = '-' + formatPrice(couponDiscount);
            document.getElementById('couponRow').style.display = 'flex';
        } else {
            document.getElementById('couponRow').style.display = 'none';
        }

        // Tax
        document.getElementById('taxLabel').textContent = 'Tax (' + (cart.tax_rate_pct || 0).toFixed(2) + '%)';
        document.getElementById('tax').textContent = formatPrice(cart.tax_amount);

        // Dual pricing: Cash vs Card
        if (cart.cash_discount_enabled && cart.cash_discount_value > 0) {
            document.getElementById('cardTotalLabel').textContent = '💳 Card';
            document.getElementById('cardTotal').textContent = formatPrice(cart.card_total);
            document.getElementById('cashTotalRow').style.display = 'flex';
            document.getElementById('cashTotal').textContent = formatPrice(cart.cash_total);
        } else {
            document.getElementById('cardTotalLabel').textContent = 'Total';
            document.getElementById('cardTotal').textContent = formatPrice(cart.card_total);
            document.getElementById('cashTotalRow').style.display = 'none';
        }
    }

    // --- OVERLAY ---
    function showDisconnectOverlay(statusText) {
        document.getElementById('disconnectOverlay').style.display = 'flex';
        document.getElementById('recoveryStatus').textContent = statusText || 'Reconnecting automatically…';
    }

    function hideDisconnectOverlay() {
        document.getElementById('disconnectOverlay').style.display = 'none';
        document.getElementById('retryCount').textContent = '';
    }

    // ============================================================
    // ACTIVE MODE: versioned cart polling with capped backoff
    // ============================================================

    function scheduleNextPoll(delayMs) {
        if (pollTimer) clearTimeout(pollTimer);
        pollTimer = setTimeout(pollCart, delayMs);
    }

    async function pollCart() {
        const token = getToken();
        if (!token) return;

        try {
            const res = await fetch(`/pos/api/peripheral/poll?since=${cartVersion}`, {
                headers: { 'X-Pairing-Token': token },
                cache: 'no-store'
            });

            if (res.status === 403) {
                // Authoritative: this pairing was revoked at the register.
                // The ONLY error that drops credentials.
                let revoked = false;
                try { revoked = (await res.json()).revoked === true; } catch (e) { /* body wasn't JSON */ }
                if (revoked) {
                    console.warn('Pairing revoked by register. Returning to pairing screen.');
                    hardUnpair();
                    return;
                }
                throw new Error('HTTP 403');
            }

            if (!res.ok) throw new Error('HTTP ' + res.status);
            const data = await res.json();
            if (!data.success) throw new Error(data.message || 'Poll failed');

            // Recovered (or normal) - reset failure state
            consecutiveFailures = 0;
            hideDisconnectOverlay();
            setStatusBadge('connected', '🟢 Live');

            if (data.changed) {
                cartVersion = data.version;
                if (data.register_name) {
                    document.getElementById('merchantName').textContent = data.register_name;
                    localStorage.setItem(LS_REGISTER_NAME, data.register_name);
                }
                updateCartDisplay(data.cart || { items: [] });
            }

            maybeIdleReload();
            scheduleNextPoll(POLL_MS);
        } catch (err) {
            // Transient failure (server rebooting, network blip, tablet
            // just woke up...): keep credentials, keep retrying forever.
            consecutiveFailures++;
            const delay = Math.min(POLL_MS * Math.pow(1.5, consecutiveFailures), BACKOFF_MAX_MS);
            setStatusBadge('disconnected', '🔄 Reconnecting...');
            if (consecutiveFailures >= OVERLAY_AFTER_FAILURES) {
                showDisconnectOverlay('Connection lost. Reconnecting automatically…');
                document.getElementById('retryCount').textContent =
                    'Attempt ' + consecutiveFailures + ' — retrying every few seconds';
            }
            scheduleNextPoll(delay);
        }
    }

    // Kiosk hygiene: reload once a day, but only when nothing is
    // happening (empty cart, connected) so we never interrupt a sale.
    let lastIdleReloadCheck = Date.now();
    function maybeIdleReload() {
        const now = Date.now();
        if (now - lastIdleReloadCheck < 60 * 60 * 1000) return;
        lastIdleReloadCheck = now;
        if (now - bootTime > IDLE_RELOAD_HOURS * 3600 * 1000 && lastCartEmpty) {
            console.log('Daily maintenance reload');
            window.location.reload();
        }
    }

    function startActiveMode() {
        document.getElementById('pairingDisplay').style.display = 'none';
        document.getElementById('activeDisplay').style.display = 'flex';
        document.getElementById('merchantName').textContent =
            localStorage.getItem(LS_REGISTER_NAME) || 'Welcome';
        setStatusBadge('disconnected', '⚪ Connecting...');
        pollCart();
    }

    // ============================================================
    // PAIRING MODE: show a code, wait to be claimed
    // ============================================================

    function clearPairingTimers() {
        pairingTimers.forEach(t => clearTimeout(t));
        pairingTimers = [];
    }

    async function requestPairingCode(attempt) {
        attempt = attempt || 0;
        const peripheralId = getPeripheralId();

        try {
            const res = await fetch('/pos/api/pairing/request-code', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken
                },
                body: JSON.stringify({ peripheral_id: peripheralId, role: 'display' })
            });
            const data = await res.json();
            if (!res.ok || !data.success) throw new Error(data.message || 'code request failed');

            const code = data.pairing_code;
            document.getElementById('displayPairingCode').textContent =
                code.slice(0, 3) + ' ' + code.slice(3);
            document.getElementById('pairingStateText').textContent = 'Awaiting Register Link';
            setStatusBadge('connected', '🟢 Ready to pair');

            // Refresh the code at ~80% of its TTL so it is never stale on screen
            const ttlMs = (data.ttl_seconds || 600) * 1000;
            pairingTimers.push(setTimeout(() => requestPairingCode(0), ttlMs * 0.8));
        } catch (err) {
            // Server might still be booting - keep trying, show state
            console.warn('Pairing code request failed:', err);
            document.getElementById('displayPairingCode').textContent = '--- ---';
            document.getElementById('pairingStateText').textContent = 'Connecting to server…';
            setStatusBadge('disconnected', '🔄 Connecting...');
            const delay = Math.min(2000 * Math.pow(1.5, attempt), 15000);
            pairingTimers.push(setTimeout(() => requestPairingCode(attempt + 1), delay));
        }
    }

    async function pollPairingStatus() {
        const peripheralId = getPeripheralId();
        try {
            const res = await fetch(`/pos/api/pairing/status?peripheral_id=${encodeURIComponent(peripheralId)}`,
                { cache: 'no-store' });
            const data = await res.json();

            if (data.success && data.paired && data.token) {
                // Store credentials, ack so the server wipes its plaintext copy
                localStorage.setItem(LS_TOKEN, data.token);
                if (data.register_name) localStorage.setItem(LS_REGISTER_NAME, data.register_name);

                try {
                    await fetch('/pos/api/pairing/ack', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'X-CSRFToken': csrfToken
                        },
                        body: JSON.stringify({ peripheral_id: peripheralId, token: data.token })
                    });
                } catch (e) { /* non-fatal: ack retried implicitly by re-pairing */ }

                console.log('Paired! Switching to active display.');
                clearPairingTimers();
                window.location.reload();
                return;
            }
        } catch (err) {
            // keep polling
        }
        pairingTimers.push(setTimeout(pollPairingStatus, PAIRING_STATUS_MS));
    }

    function startPairingMode() {
        paired = false;
        document.getElementById('activeDisplay').style.display = 'none';
        document.getElementById('pairingDisplay').style.display = 'flex';
        document.getElementById('debugTerminalId').textContent = 'ID: ' + getPeripheralId();
        requestPairingCode(0);
        pollPairingStatus();
    }

    // ============================================================
    // UNPAIR (authoritative revoke or staff escape only)
    // ============================================================

    function hardUnpair() {
        if (pollTimer) clearTimeout(pollTimer);
        clearPairingTimers();
        localStorage.removeItem(LS_TOKEN);
        localStorage.removeItem(LS_REGISTER_NAME);
        localStorage.removeItem('rscp_staff_terminal_id'); // legacy key cleanup
        window.location.reload();
    }

    function unpairAndExit() {
        const token = getToken();
        if (token) {
            // Best-effort server-side cleanup; local wipe happens regardless
            fetch('/pos/api/peripheral/unpair', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken,
                    'X-Pairing-Token': token
                },
                body: '{}'
            }).catch(() => {}).finally(hardUnpair);
        } else {
            hardUnpair();
        }
    }

    // --- STAFF ESCAPE CONTROLS (triple-click the status badge) ---
    let clickCount = 0;
    let clickTimer = null;
    document.getElementById('connectionStatus').addEventListener('click', function () {
        clickCount++;
        if (clickTimer) clearTimeout(clickTimer);
        clickTimer = setTimeout(() => { clickCount = 0; }, 600);

        if (clickCount >= 3) {
            clickCount = 0;
            document.getElementById('exitControls').classList.toggle('visible');
        }
    });

    // ============================================================
    // WAKE-UPS: poll immediately when the tablet comes back
    // ============================================================
    function wakeUp() {
        if (!paired) return;
        console.log('Wake event - polling immediately');
        consecutiveFailures = 0;
        if (pollTimer) clearTimeout(pollTimer);
        pollCart();
    }

    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') wakeUp();
    });
    window.addEventListener('online', wakeUp);
    window.addEventListener('pageshow', (e) => { if (e.persisted) wakeUp(); });

    // ============================================================
    // BOOT
    // ============================================================
    (function initDisplay() {
        if (getToken()) {
            paired = true;
            startActiveMode();
        } else {
            startPairingMode();
        }
    })();
})();
