/* templates/admin_logs.html */
(function () {
    'use strict';
    RSCP.actions({
        toggleTrace: function (el, event) {
            event.preventDefault();
            var trace = document.getElementById(el.dataset.target);
            if (trace.style.display === 'block') trace.style.display = 'none';
            else trace.style.display = 'block';
        }
    });
})();
