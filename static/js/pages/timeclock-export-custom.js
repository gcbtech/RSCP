/* templates/timeclock/export_custom.html */
(function () {
    'use strict';

    new TomSelect('#userSelect', {
        plugins: ['remove_button'],
        create: false
    });

    RSCP.actions({
        toggleCustomDates: function () {
            const val = document.getElementById('periodSelect').value;
            const customDiv = document.getElementById('customDates');
            const inputs = customDiv.querySelectorAll('input');

            if (val === 'custom') {
                customDiv.style.display = 'flex';
                inputs.forEach(i => i.required = true);
            } else {
                customDiv.style.display = 'none';
                inputs.forEach(i => i.required = false);
            }
        }
    });
})();
