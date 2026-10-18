/* Stock Alerts page (templates/inventory/alerts.html). */
(function () {
    'use strict';

    RSCP.actions({
        enableAll: function () {
            document.querySelectorAll('.alert-toggle').forEach(cb => cb.checked = true);
        },
        disableAll: function () {
            document.querySelectorAll('.alert-toggle').forEach(cb => cb.checked = false);
        }
    });
})();
