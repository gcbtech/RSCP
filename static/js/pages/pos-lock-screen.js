/* POS lock screen (templates/pos/lock_screen.html): PIN pad, badge scan, Enter to unlock. */
(function () {
    'use strict';

    RSCP.actions({
        addDigit: function (el) { addDigit(el.dataset.digit); },
        clearPin: function () { clearPin(); },
        submitPin: function () { submitPin(); }
    });

    function addDigit(digit) {
        const pinInput = document.getElementById('pinInput');
        if (pinInput.value.length < 6) {
            pinInput.value += digit;
        }
    }

    function clearPin() {
        document.getElementById('pinInput').value = '';
    }

    function submitPin() {
        const pin = document.getElementById('pinInput').value;
        if (pin.length >= 4) {
            document.getElementById('unlockForm').submit();
        }
    }

    // Auto-submit on badge scan (enter key)
    document.getElementById('badgeInput').addEventListener('keypress', function (e) {
        if (e.key === 'Enter') {
            e.preventDefault();
            document.getElementById('unlockForm').submit();
        }
    });

    // Submit PIN on Enter key
    document.getElementById('pinInput').addEventListener('keypress', function (e) {
        if (e.key === 'Enter') {
            e.preventDefault();
            submitPin();
        }
    });

    // Focus PIN input on load
    document.getElementById('pinInput').focus();
})();
