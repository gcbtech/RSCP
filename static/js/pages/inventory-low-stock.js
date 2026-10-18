/* Low Stock Report (templates/inventory/low_stock.html). Image modal and SKU copy
   actions come from inventory-common.js, which also applies dark mode. */
(function () {
    'use strict';

    // Dark mode check: drop the dark table header style (as before)
    if (localStorage.getItem('rscp_darkmode') === 'true') {
        const header = document.querySelector('.table-dark');
        if (header) header.classList.remove('table-dark');
    }
})();
