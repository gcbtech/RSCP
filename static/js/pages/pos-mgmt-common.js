/* Shared actions for the POS management / report pages (templates/pos/*):
   print buttons, auto-submitting date pickers, the thermal-summary popup.
   Loaded by base.html pages and by the standalone daily_report_print.html. */
(function () {
    'use strict';

    RSCP.actions({
        // <button data-action="print">
        print: function () { window.print(); },

        // <button data-action="closeWindow"> (popup print views)
        closeWindow: function () { window.close(); },

        // <input data-change="submitForm">: submit the enclosing form on change
        submitForm: function (el) { if (el.form) el.form.submit(); },

        // <a href="..." data-action="openPopup" data-popup-name="x" data-popup-features="width=..,height=..">
        openPopup: function (el, event) {
            event.preventDefault();
            window.open(el.href, el.dataset.popupName || '_blank', el.dataset.popupFeatures || '');
        }
    });
})();
