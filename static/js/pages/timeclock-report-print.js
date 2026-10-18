/* templates/timeclock/report_print.html (standalone print page) */
(function () {
    'use strict';
    RSCP.actions({
        print: function () { window.print(); },
        closeWindow: function () { window.close(); }
    });
})();
