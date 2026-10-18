/* templates/timeclock/shifts.html */
(function () {
    'use strict';
    RSCP.actions({
        toggleRefDate: function (el) {
            document.getElementById('refDateDiv').style.display = el.value === 'biweekly' ? 'block' : 'none';
        }
    });
})();
