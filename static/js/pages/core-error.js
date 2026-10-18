/* templates/error.html */
(function () {
    'use strict';
    RSCP.actions({
        goBack: function (el, event) {
            event.preventDefault();
            history.back();
        }
    });
})();
