/* Handheld line scanner (templates/pos/scan.html). Full-screen page without the nav;
   rscp-common.js comes from base.html. The cart list is re-rendered via innerHTML, so its
   buttons use delegated data-action handlers (line ids travel in data-* attributes). */
(function () {
    'use strict';

    RSCP.actions({
        changeQty: function (el) {
            changeQty(el.dataset.lineId, Number(el.dataset.delta), Number(el.dataset.qty));
        },
        removeItem: function (el) { removeItem(el.dataset.lineId); },
        holdCart: function () { holdCart(); },
        toggleMenu: function () { toggleMenu(); },
        clearCartRemote: function () { clearCartRemote(); },
        unpairScanner: function () { unpairScanner(); }
    });


    // ============================================================
    // Staff line-busting scanner (pairing v3)
    // Login-authenticated (this page sits behind the normal POS
    // auth), paired to one register by bearer token. All cart
    // mutations go through the standard /pos/cart/* routes with
    // X-Pairing-Token, so they land in the REGISTER's cart.
    // Reconnect rules match the customer display: credentials are
    // only dropped on an authoritative revoke.
    // ============================================================

    const csrfToken = RSCP.csrfToken();

    const LS_ID = 'rscp_scanner_peripheral_id';
    const LS_TOKEN = 'rscp_scanner_token';
    const LS_REG_NAME = 'rscp_scanner_register_name';

    const POLL_MS = 2000;
    const BACKOFF_MAX_MS = 6000;
    const PAIRING_STATUS_MS = 2000;

    let cartVersion = -1;
    let failures = 0;
    let pollTimer = null;
    let pairingTimers = [];
    let busy = false;

    function getPeripheralId() {
        let id = localStorage.getItem(LS_ID);
        if (!id) {
            id = 'term_scan_' + Date.now() + '_' + Math.random().toString(36).substr(2, 9);
            localStorage.setItem(LS_ID, id);
        }
        return id;
    }

    function getToken() { return localStorage.getItem(LS_TOKEN); }

    function fmt(v) { return '$' + (parseFloat(v) || 0).toFixed(2); }

    function esc(text) {
        return escapeHtml(text || '');
    }

    function lineIdOf(item) {
        return String(item.line_id || '').replace(/[^A-Za-z0-9_-]/g, '');
    }

    function toast(msg, ms) {
        const el = document.getElementById('toastMsg');
        el.textContent = msg;
        el.style.display = 'block';
        clearTimeout(el._t);
        el._t = setTimeout(() => { el.style.display = 'none'; }, ms || 2500);
    }

    function setConn(live, text) {
        const badge = document.getElementById('connBadge');
        badge.className = 'conn ' + (live ? 'live' : 'down');
        badge.textContent = text;
    }

    // --- BEEP ---
    let audioCtx = null;
    function beep(success) {
        try {
            if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
            if (audioCtx.state === 'suspended') audioCtx.resume();
            const osc = audioCtx.createOscillator();
            const gain = audioCtx.createGain();
            osc.connect(gain);
            gain.connect(audioCtx.destination);
            const now = audioCtx.currentTime;
            osc.type = success ? 'sine' : 'sawtooth';
            osc.frequency.setValueAtTime(success ? 1000 : 150, now);
            gain.gain.setValueAtTime(success ? 0.15 : 0.25, now);
            gain.gain.exponentialRampToValueAtTime(0.001, now + (success ? 0.12 : 0.25));
            osc.start(now);
            osc.stop(now + (success ? 0.12 : 0.25));
        } catch (e) { /* audio unavailable */ }
    }

    // --- RENDER (consumes compute_cart_totals payload) ---
    function renderCart(data) {
        if (typeof data.version === 'number') cartVersion = data.version;
        const items = data.items || [];
        const list = document.getElementById('itemList');

        if (!items.length) {
            list.innerHTML = '<div class="empty-hint">🛒 Cart is empty<br><small>Scan an item to start</small></div>';
        } else {
            // Lines are addressed by their stable line_id, never by list
            // position: the register may add/remove lines at any moment.
            list.innerHTML = items.map((item) => `
                <div class="scan-item">
                    <div class="info">
                        <div class="name">${esc(item.name)}</div>
                        <div class="meta">${fmt(item.unit_price)} each · ${fmt(item.line_total)}</div>
                    </div>
                    <div class="qty-controls">
                        <button type="button" data-action="changeQty" data-line-id="${lineIdOf(item)}" data-delta="-1" data-qty="${Number(item.quantity)}">−</button>
                        <span class="qty">${esc(String(item.quantity))}</span>
                        <button type="button" data-action="changeQty" data-line-id="${lineIdOf(item)}" data-delta="1" data-qty="${Number(item.quantity)}">+</button>
                        <button type="button" class="remove-btn" data-action="removeItem" data-line-id="${lineIdOf(item)}">✕</button>
                    </div>
                </div>
            `).join('');
        }

        document.getElementById('tSubtotal').textContent = fmt(data.subtotal);

        const discounts = (data.order_discount || 0) + (data.coupon_discount || 0);
        document.getElementById('tDiscount').textContent = '-' + fmt(discounts);
        document.getElementById('tDiscountRow').style.display = discounts > 0 ? 'flex' : 'none';

        document.getElementById('tTaxLabel').textContent = 'Tax (' + (data.tax_rate_pct || 0).toFixed(2) + '%)';
        document.getElementById('tTax').textContent = fmt(data.tax_amount);

        if (data.cash_discount_enabled && data.cash_discount_value > 0) {
            document.getElementById('tCashRow').style.display = 'flex';
            document.getElementById('tCash').textContent = fmt(data.cash_total);
            document.getElementById('tTotalLabel').textContent = '💳 Card';
        } else {
            document.getElementById('tCashRow').style.display = 'none';
            document.getElementById('tTotalLabel').textContent = 'Total';
        }
        document.getElementById('tTotal').textContent = fmt(data.card_total);
    }

    function showMessages(data) {
        (data.messages || []).forEach(m => {
            if (m.text) toast(m.text, 3000);
        });
    }

    // --- MUTATIONS (standard cart routes + pairing token) ---
    function revokedCheck(res, data) {
        if ((res.status === 401 || res.status === 403) && data && data.revoked) {
            toast('Pairing revoked — re-pair this scanner.');
            hardUnpair();
            return true;
        }
        return false;
    }

    async function mutate(url, formFields) {
        const token = getToken();
        if (!token) return null;
        const body = new URLSearchParams();
        body.append('csrf_token', csrfToken);
        Object.entries(formFields || {}).forEach(([k, v]) => body.append(k, v));

        const res = await fetch(url, {
            method: 'POST',
            body: body,
            headers: {
                'Content-Type': 'application/x-www-form-urlencoded',
                'X-Pairing-Token': token,
                'X-Response-Format': 'json'
            }
        });
        let data = null;
        try { data = await res.json(); } catch (e) { /* non-JSON (e.g. auth redirect) */ }
        if (data && revokedCheck(res, data)) return null;
        if (!data) throw new Error('Unexpected response ' + res.status);
        return data;
    }

    async function addSku(sku) {
        try {
            const data = await mutate('/pos/cart/add', { sku: sku, quantity: 1 });
            if (!data) return;
            renderCart(data);
            showMessages(data);
            beep(data.success && data.error !== 'not_found');
        } catch (err) {
            console.error('Add failed:', err);
            beep(false);
            toast('Scan failed — check connection');
        }
    }

    // Relative change (+1/-1) so a concurrent add of the same item at the
    // register isn't overwritten by this device's stale quantity.
    async function changeQty(lineId, delta, shownQty) {
        if (delta < 0 && shownQty + delta < 1) return removeItem(lineId);
        try {
            const data = await mutate('/pos/cart/update/' + encodeURIComponent(lineId),
                { quantity_delta: delta, cart_version: cartVersion });
            if (data) { renderCart(data); showMessages(data); }
        } catch (err) { toast('Update failed — check connection'); }
    }

    async function removeItem(lineId) {
        try {
            const data = await mutate('/pos/cart/remove/' + encodeURIComponent(lineId),
                { cart_version: cartVersion });
            if (!data) return;
            if (data.error === 'manager_required') {
                toast('Manager approval required — void this item at the register.');
                return;
            }
            renderCart(data);
            showMessages(data);
        } catch (err) { toast('Remove failed — check connection'); }
    }

    async function clearCartRemote() {
        toggleMenu();
        try {
            const data = await mutate('/pos/cart/clear', { cart_version: cartVersion });
            if (data) { renderCart(data); showMessages(data); }
        } catch (err) { toast('Clear failed — check connection'); }
    }

    async function holdCart() {
        const note = prompt('Note for this order (customer name, etc.):') || '';
        try {
            const data = await mutate('/pos/cart/hold', { note: note });
            if (!data) return;
            if (data.success) {
                renderCart(data);
                toast('✅ Held — recall it at any register');
            } else {
                showMessages(data);
            }
        } catch (err) { toast('Hold failed — check connection'); }
    }

    // --- SCAN INPUT (keyboard-wedge friendly) ---
    const scanInput = document.getElementById('scanInput');
    const scanQueue = [];
    let scanning = false;

    async function drainQueue() {
        if (scanning || !scanQueue.length) return;
        scanning = true;
        const sku = scanQueue.shift();
        try { await addSku(sku); }
        finally {
            scanning = false;
            scanInput.focus();
            drainQueue();
        }
    }

    scanInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            const val = scanInput.value.trim();
            scanInput.value = '';
            if (val) {
                scanQueue.push(val);
                drainQueue();
            }
        }
    });

    // --- SYNC POLL (register-side changes, presence, revoke detection) ---
    function scheduleNextPoll(delay) {
        if (pollTimer) clearTimeout(pollTimer);
        pollTimer = setTimeout(pollCart, delay);
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
                let d = null;
                try { d = await res.json(); } catch (e) {}
                if (d && d.revoked) { hardUnpair(); return; }
                throw new Error('403');
            }
            if (!res.ok) throw new Error('HTTP ' + res.status);
            const data = await res.json();
            if (!data.success) throw new Error(data.message || 'poll failed');

            failures = 0;
            setConn(true, '🟢 Live');
            if (data.changed) {
                cartVersion = data.version;
                if (data.register_name) {
                    document.getElementById('registerName').textContent = '🤳 → ' + data.register_name;
                    localStorage.setItem(LS_REG_NAME, data.register_name);
                }
                renderCart(data.cart || { items: [] });
            }
            scheduleNextPoll(POLL_MS);
        } catch (err) {
            failures++;
            setConn(false, '🔄 Reconnecting…');
            scheduleNextPoll(Math.min(POLL_MS * Math.pow(1.5, failures), BACKOFF_MAX_MS));
        }
    }

    // --- PAIRING MODE ---
    function clearPairingTimers() {
        pairingTimers.forEach(t => clearTimeout(t));
        pairingTimers = [];
    }

    async function requestPairingCode(attempt) {
        attempt = attempt || 0;
        try {
            const res = await fetch('/pos/api/pairing/request-code', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken
                },
                body: JSON.stringify({ peripheral_id: getPeripheralId(), role: 'scanner' })
            });
            const data = await res.json();
            if (!res.ok || !data.success) throw new Error(data.message || 'failed');
            const code = data.pairing_code;
            document.getElementById('pairingCode').textContent = code.slice(0, 3) + ' ' + code.slice(3);
            document.getElementById('pairingState').textContent = 'Awaiting Register Link';
            pairingTimers.push(setTimeout(() => requestPairingCode(0), (data.ttl_seconds || 600) * 800));
        } catch (err) {
            document.getElementById('pairingCode').textContent = '--- ---';
            document.getElementById('pairingState').textContent = 'Connecting to server…';
            pairingTimers.push(setTimeout(() => requestPairingCode(attempt + 1),
                Math.min(2000 * Math.pow(1.5, attempt), 15000)));
        }
    }

    async function pollPairingStatus() {
        try {
            const res = await fetch(`/pos/api/pairing/status?peripheral_id=${encodeURIComponent(getPeripheralId())}`,
                { cache: 'no-store' });
            const data = await res.json();
            if (data.success && data.paired && data.token) {
                localStorage.setItem(LS_TOKEN, data.token);
                if (data.register_name) localStorage.setItem(LS_REG_NAME, data.register_name);
                try {
                    await fetch('/pos/api/pairing/ack', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'X-CSRFToken': csrfToken
                        },
                        body: JSON.stringify({ peripheral_id: getPeripheralId(), token: data.token })
                    });
                } catch (e) {}
                clearPairingTimers();
                window.location.reload();
                return;
            }
        } catch (err) { /* keep polling */ }
        pairingTimers.push(setTimeout(pollPairingStatus, PAIRING_STATUS_MS));
    }

    // --- UNPAIR ---
    function hardUnpair() {
        if (pollTimer) clearTimeout(pollTimer);
        clearPairingTimers();
        localStorage.removeItem(LS_TOKEN);
        localStorage.removeItem(LS_REG_NAME);
        window.location.reload();
    }

    function unpairScanner() {
        if (!confirm('Unpair this scanner from its register?')) return;
        const token = getToken();
        if (token) {
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

    function toggleMenu() {
        document.getElementById('menuSheet').classList.toggle('open');
    }

    // --- WAKE-UPS ---
    function wakeUp() {
        if (!getToken()) return;
        failures = 0;
        if (pollTimer) clearTimeout(pollTimer);
        pollCart();
    }
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') wakeUp();
    });
    window.addEventListener('online', wakeUp);

    // --- BOOT ---
    (function init() {
        if (getToken()) {
            document.getElementById('scannerApp').style.display = 'flex';
            document.getElementById('pairingWrap').style.display = 'none';
            document.getElementById('registerName').textContent =
                '🤳 → ' + (localStorage.getItem(LS_REG_NAME) || 'Register');
            pollCart();
            scanInput.focus();
        } else {
            document.getElementById('scannerApp').style.display = 'none';
            document.getElementById('pairingWrap').style.display = 'flex';
            document.getElementById('scannerDebugId').textContent = 'ID: ' + getPeripheralId();
            requestPairingCode(0);
            pollPairingStatus();
        }
    })();
})();
