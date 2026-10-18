/* templates/timeclock/request_pto.html */
(function () {
    'use strict';
    RSCP.actions({
        togglePartial: function () {
            const type = document.getElementById('requestType').value;
            const partialDiv = document.getElementById('partialFields');
            const inputs = partialDiv.querySelectorAll('input');

            if (type === 'partial') {
                partialDiv.style.display = 'block';
                inputs.forEach(i => i.required = true);
            } else {
                partialDiv.style.display = 'none';
                inputs.forEach(i => i.required = false);
            }
        }
    });
})();
