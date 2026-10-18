/* Thermal receipt print view (templates/pos/receipt_print.html, standalone 80mm page).
   Loaded at the end of <body>: opens the print dialog as soon as the receipt is parsed,
   both when opened directly and inside the register's hidden auto-print iframe. */
(function () {
    'use strict';

    window.print();
})();
