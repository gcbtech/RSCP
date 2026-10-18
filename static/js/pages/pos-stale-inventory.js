/* Stale Inventory report (templates/pos/stale_inventory.html). Needs jQuery + DataTables. */
(function () {
    'use strict';

    $(document).ready(function () {
        $('#staleTable').DataTable({
            "pageLength": 50,
            "order": [[5, "desc"]], // Sort by Stale Score descending
            "columnDefs": [
                { "type": "num", "targets": [1, 2, 4, 5] }
            ]
        });
    });
})();
