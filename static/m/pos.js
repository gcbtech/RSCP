/*
 * Phone UI, POS register (templates/m/pos/register.html). Plain ES5.
 *
 * Which cart, decided as the desktop POS page does, with its localStorage
 * keys (so a pairing made on either page holds on both):
 *   rscp_scanner_token set -> handheld: /pos/api/scanner/enter-slave binds this
 *                             session to that register's cart
 *   otherwise              -> its own register: /pos/api/register/hello with
 *                             rscp_terminal_id
 * Then the cart is fetched from /m/pos/cart. Every change posts to
 * /m/pos/cart/<action>, which runs the desktop's cart code and returns the
 * re-rendered cart. A version poll picks up changes made on another device.
 */
(function () {
    'use strict';

    var PAGE = RSCP.pageData() || {};
    var POLL_MS = 2500;

    function byId(id) { return document.getElementById(id); }

    var version = PAGE.version;
    var queue = [];
    var busy = false;
    var pending = null;        // an action waiting for a manager: {action, params, item, kind}
    var linkTimer = null;
    var manual = byId('mScanManual');

    // ------------------------------------------------------------ identity

    function storage(key, value) {
        try {
            if (value === undefined) return localStorage.getItem(key);
            if (value === null) localStorage.removeItem(key); else localStorage.setItem(key, value);
        } catch (e) { return null; }
        return null;
    }

    function newId(prefix) {
        return prefix + Date.now() + '_' + Math.random().toString(36).substr(2, 9);
    }

    function terminalId() {
        var id = storage('rscp_terminal_id');
        if (!id) { id = newId('term_'); storage('rscp_terminal_id', id); }
        return id;
    }

    function peripheralId() {
        var id = storage('rscp_scanner_peripheral_id');
        if (!id) { id = newId('term_scan_'); storage('rscp_scanner_peripheral_id', id); }
        return id;
    }

    function token() { return storage('rscp_scanner_token'); }

    function dropPairing() {
        storage('rscp_scanner_token', null);
        storage('rscp_scanner_register_name', null);
    }

    /** Bind this session to the right cart. Resolves 'ready', or 'reload' when
     *  the page was rendered for the other mode (handheld vs own cart). */
    function identify() {
        var t = token();
        var linkedShown = !!document.querySelector('.m-reg-pill.on');
        if (t) {
            return RSCPM.postJSON(PAGE.enterSlaveUrl, { token: t }).then(function (data) {
                if (data.register_name) storage('rscp_scanner_register_name', data.register_name);
                return linkedShown ? 'ready' : 'reload';
            }, function (err) {
                if (err.status !== 403) return 'ready';   // offline: keep the pairing, try again later
                dropPairing();                            // unpaired on the register
                return hello().then(function () { return linkedShown ? 'reload' : 'ready'; });
            });
        }
        if (linkedShown) {
            // The session rings into a register this browser holds no pairing for
            return RSCPM.postJSON(PAGE.exitSlaveUrl, {}).catch(function () {})
                .then(hello).then(function () { return 'reload'; });
        }
        return hello().then(function () { return 'ready'; });
    }

    function hello() {
        return RSCPM.postJSON(PAGE.helloUrl, { register_id: terminalId(), friendly_name: storage('rscp_staff_friendly_name') || null })
            .catch(function () { /* the session cart still works */ });
    }

    // ------------------------------------------------------------ cart

    function apply(data) {
        if (data.html !== undefined) byId('mCart').innerHTML = data.html;
        if (data.foot_html !== undefined) byId('mCartFoot').innerHTML = data.foot_html;
        if (data.held_html !== undefined) byId('mHeld').innerHTML = data.held_html;
        if (data.version !== undefined) version = data.version;
    }

    function refresh() {
        return fetch(PAGE.cartUrl, { credentials: 'same-origin', headers: { Accept: 'application/json' }, cache: 'no-store' })
            .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
            .then(apply)
            .catch(function () { RSCPM.toast('Couldn’t load the cart. Check the connection.'); });
    }

    function say(messages) {
        if (messages && messages.length) RSCPM.toast(messages.map(function (m) { return m.text; }).join(' · '));
    }

    /** POST one change; resolves with the server's answer (cart already redrawn). */
    function act(action, params, item) {
        var body = [];
        params = params || {};
        if (version !== null && version !== undefined) params.cart_version = version;
        Object.keys(params).forEach(function (k) {
            body.push(encodeURIComponent(k) + '=' + encodeURIComponent(params[k]));
        });
        var url = PAGE.actionUrl.replace('ACTION', action) + (item ? '/' + encodeURIComponent(item) : '');
        return fetch(url, {
            method: 'POST',
            credentials: 'same-origin',
            headers: {
                'Content-Type': 'application/x-www-form-urlencoded',
                Accept: 'application/json',
                'X-CSRFToken': RSCP.csrfToken(),
                'X-Response-Format': 'json'
            },
            body: body.join('&')
        }).then(function (r) {
            if (!r.ok) throw new Error('The server said ' + r.status);
            return r.json();
        }).then(function (data) {
            apply(data);
            if (data.error === 'manager_required') {
                askManager({ action: action, params: params, item: item });
            } else {
                say(data.messages);
            }
            return data;
        });
    }

    // ------------------------------------------------------------ scanning and search

    var scanner = RSCPScanner.attach({
        capture: byId('mScanCapture'),
        status: byId('mScanStatus'),
        fields: [manual],
        onScan: function (code) { queue.push(code); next(); }
    });

    function next() {
        if (busy || !queue.length) return;
        busy = true;
        var code = queue.shift();
        addCode(code).then(function () { busy = false; next(); });
    }

    function addCode(code) {
        byId('mScanStatus').classList.add('m-compact');
        return act('add', { sku: code }).then(function (data) {
            if (data.ok) {
                RSCPScanner.feedback('ok');
                return;
            }
            if (data.error === 'not_found') return search(code);
            RSCPScanner.feedback('bad');
        }).catch(function (err) {
            RSCPScanner.feedback('bad');
            RSCPM.toast(code + ' not added: ' + err.message);
        });
    }

    function search(text) {
        return fetch(PAGE.searchUrl + '?q=' + encodeURIComponent(text), { credentials: 'same-origin', headers: { Accept: 'application/json' } })
            .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
            .then(function (data) {
                var items = data.items || [];
                if (!items.length) {
                    RSCPScanner.feedback('bad');
                    RSCPM.toast('Nothing matches ' + text + '. Add it as a custom item?');
                    return;
                }
                RSCPScanner.feedback('warn');
                byId('mPickList').innerHTML = items.map(function (it) {
                    return '<button type="button" class="m-row" data-action="mPick" data-sku="' + escapeHtml(it.sku) + '">' +
                        '<span class="m-t"><span class="m-t1">' + escapeHtml(it.name) + '</span>' +
                        '<span class="m-t2">' + escapeHtml(it.sku) + ' · ' + escapeHtml(it.quantity) + ' in stock</span></span>' +
                        '<span class="m-pill m-tone-nu">$' + Number(it.sell_price || 0).toFixed(2) + '</span></button>';
                }).join('');
                RSCPM.openSheet(byId('mPickSheet'));
            })
            .catch(function () { RSCPM.toast('Search failed. Try again.'); });
    }

    // ------------------------------------------------------------ manager approval

    function askManager(what) {
        pending = what;
        byId('mManagerWhy').textContent = what.action === 'remove'
            ? 'Removing an item from the cart needs a manager.'
            : 'This discount is over the store’s limit.';
        byId('mManagerError').hidden = true;
        byId('mManagerPass').value = '';
        RSCPM.openSheet(byId('mManagerSheet'));
        byId('mManagerUser').focus();
    }

    // ------------------------------------------------------------ linking to a register (handheld)

    function pollLinked() {
        clearTimeout(linkTimer);
        fetch(PAGE.pairingStatusUrl + '?peripheral_id=' + encodeURIComponent(peripheralId()), { credentials: 'same-origin', cache: 'no-store' })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.success && data.paired && data.token) {
                    storage('rscp_scanner_token', data.token);
                    if (data.register_name) storage('rscp_scanner_register_name', data.register_name);
                    return RSCPM.postJSON(PAGE.pairingAckUrl, { peripheral_id: peripheralId(), token: data.token })
                        .catch(function () {})
                        .then(function () { return RSCPM.postJSON(PAGE.enterSlaveUrl, { token: data.token }); })
                        .then(function () { window.location.reload(); }, function () { window.location.reload(); });
                }
                linkTimer = setTimeout(pollLinked, 2000);
            })
            .catch(function () { linkTimer = setTimeout(pollLinked, 2000); });
    }

    function stopLinking() {
        clearTimeout(linkTimer);
        linkTimer = null;
        if (byId('mLinkActive')) { byId('mLinkActive').hidden = true; byId('mLinkIdle').hidden = false; }
    }

    if (byId('mRegisterSheet')) byId('mRegisterSheet').addEventListener('close', stopLinking);

    // ------------------------------------------------------------ changes made elsewhere

    function poll() {
        if (document.visibilityState === 'hidden' || document.querySelector('dialog[open]') || busy ||
                version === null || version === undefined) return;
        var t = token();
        var req = t
            ? fetch(PAGE.peripheralPollUrl + '?since=' + version, { headers: { 'X-Pairing-Token': t }, credentials: 'same-origin', cache: 'no-store' })
            : fetch(PAGE.registerPollUrl + '?since=' + version, { credentials: 'same-origin', cache: 'no-store' });
        req.then(function (r) {
            if (r.status === 403 && t) { dropPairing(); window.location.reload(); return null; }
            return r.ok ? r.json() : null;
        }).then(function (data) {
            if (data && data.success && data.changed) refresh();
        }).catch(function () { /* offline: next tick */ });
    }

    // ------------------------------------------------------------ actions

    function formValues(form) {
        var out = {};
        var fields = form.querySelectorAll('input, select, textarea');
        for (var i = 0; i < fields.length; i++) {
            var f = fields[i];
            if (!f.name || ((f.type === 'radio' || f.type === 'checkbox') && !f.checked)) continue;
            out[f.name] = f.value.trim();
        }
        return out;
    }

    RSCP.actions({
        mManualScan: function (form, event) {
            event.preventDefault();
            var code = manual.value.trim();
            manual.value = '';
            scanner.focus();
            if (code) { queue.push(code); next(); }
        },
        mTypeCode: function () {
            byId('mScanStatus').classList.remove('m-compact');
            manual.focus();
        },
        mPick: function (btn) {
            RSCPM.closeSheet(byId('mPickSheet'));
            act('add', { sku: btn.getAttribute('data-sku') }).then(function (data) {
                RSCPScanner.feedback(data.ok ? 'ok' : 'bad');
            });
        },
        mLineStep: function (btn) {
            act('update', { quantity_delta: btn.getAttribute('data-change') }, btn.getAttribute('data-line'))
                .catch(function (err) { RSCPM.toast(err.message); });
        },
        mLineRemove: function (btn) {
            act('remove', {}, btn.getAttribute('data-line')).catch(function (err) { RSCPM.toast(err.message); });
        },
        mClearCart: function (btn) {
            RSCPM.closeSheet(btn.closest('dialog'));
            if (!window.confirm('Clear the whole cart?')) return;
            act('clear', {}).catch(function (err) { RSCPM.toast(err.message); });
        },
        mCustomAdd: function (form, event) {
            event.preventDefault();
            var v = formValues(form);
            act('custom', v).then(function (data) {
                if (data.ok) { RSCPM.closeSheet(byId('mCustomSheet')); form.reset(); RSCPScanner.feedback('ok'); }
            }).catch(function (err) { RSCPM.toast(err.message); });
        },
        mDiscountApply: function (form, event) {
            event.preventDefault();
            var v = formValues(form);
            v.discount_amount = v.discount_amount || '0';
            act('discount', v).then(function (data) {
                if (data.ok) RSCPM.closeSheet(byId('mDiscountSheet'));
            }).catch(function (err) { RSCPM.toast(err.message); });
        },
        mCouponApply: function (form, event) {
            event.preventDefault();
            var code = byId('mCouponCode').value.trim();
            if (!code) return;
            RSCPM.postJSON(PAGE.couponApplyUrl, { code: code }).then(function () {
                RSCPM.closeSheet(byId('mDiscountSheet'));
                RSCPM.toast('Coupon ' + code + ' applied');
                return refresh();
            }).catch(function (err) { RSCPM.toast(err.message); });
        },
        mCouponRemove: function () {
            RSCPM.postJSON(PAGE.couponRemoveUrl, {}).then(function () {
                RSCPM.closeSheet(byId('mDiscountSheet'));
                return refresh();
            }).catch(function (err) { RSCPM.toast(err.message); });
        },
        mHold: function (form, event) {
            event.preventDefault();
            act('hold', formValues(form)).then(function (data) {
                if (data.ok) { RSCPM.closeSheet(byId('mHoldSheet')); form.reset(); }
            }).catch(function (err) { RSCPM.toast(err.message); });
        },
        mRecall: function (btn) {
            act('recall', {}, btn.getAttribute('data-id')).then(function (data) {
                if (data.ok) RSCPM.closeSheet(byId('mHeldSheet'));
            }).catch(function (err) { RSCPM.toast(err.message); });
        },
        mManagerApprove: function (form, event) {
            event.preventDefault();
            if (!pending) return;
            var what = pending;
            var url = what.action === 'remove' ? PAGE.voidApproveUrl : PAGE.discountApproveUrl;
            RSCPM.postJSON(url, { username: byId('mManagerUser').value.trim(), password: byId('mManagerPass').value })
                .then(function (data) {
                    if (!(data.valid || data.success)) throw new Error(data.error || 'Not approved');
                    byId('mManagerPass').value = '';
                    pending = null;
                    RSCPM.closeSheet(byId('mManagerSheet'));
                    return act(what.action, what.params, what.item);
                })
                .catch(function (err) {
                    byId('mManagerError').textContent = err.message;
                    byId('mManagerError').hidden = false;
                });
        },
        mLinkStart: function () {
            RSCPM.postJSON(PAGE.requestCodeUrl, { peripheral_id: peripheralId(), role: 'scanner' })
                .then(function (data) {
                    var code = String(data.pairing_code || '');
                    byId('mLinkCode').textContent = code.slice(0, 3) + ' ' + code.slice(3);
                    byId('mLinkIdle').hidden = true;
                    byId('mLinkActive').hidden = false;
                    pollLinked();
                })
                .catch(function (err) { RSCPM.toast('No pairing code: ' + err.message); });
        },
        mLinkCancel: function () {
            stopLinking();
        },
        mUnlink: function () {
            RSCPM.postJSON(PAGE.exitSlaveUrl, { token: token(), unpair: true })
                .catch(function () {})
                .then(function () { dropPairing(); window.location.reload(); });
        }
    });

    // ------------------------------------------------------------ start

    identify().then(function (state) {
        if (state === 'reload') window.location.reload();
        else refresh();
    });
    setInterval(poll, POLL_MS);
})();
