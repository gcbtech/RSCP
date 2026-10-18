/* POS login (templates/pos/login.html): badge / PIN / password tabs and the PIN numpad. */
(function () {
    'use strict';

    RSCP.actions({
        showLogin: function (el) { showLogin(el.dataset.type, el); },
        focusBadgeInput: function () { document.getElementById('badgeInput').focus(); },
        // Badge scanners type the id and press Enter
        submitOnEnter: function (el, event) {
            if (event.key === 'Enter') { el.form.submit(); }
        },
        addDigit: function (el) { addDigit(Number(el.dataset.digit)); },
        clearPin: function () { clearPin(); },
        backspace: function () { backspace(); }
    });

    function showLogin(type, tab) {
        document.querySelectorAll('.login-tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.login-content').forEach(c => c.classList.remove('active'));

        tab.classList.add('active');
        document.getElementById(type + '-login').classList.add('active');

        // Focus appropriate input
        if (type === 'badge') {
            document.getElementById('badgeInput').focus();
        }
    }

    function addDigit(d) {
        const pin = document.getElementById('pinInput');
        if (pin.value.length < 6) {
            pin.value += d;
        }
    }

    function clearPin() {
        document.getElementById('pinInput').value = '';
    }

    function backspace() {
        const pin = document.getElementById('pinInput');
        pin.value = pin.value.slice(0, -1);
    }
})();
