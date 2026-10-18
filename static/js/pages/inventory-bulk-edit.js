/* Bulk Edit page (templates/inventory/bulk_edit.html). Needs Tom Select. */
(function () {
    'use strict';

    var data = RSCP.pageData() || {};

    // Init TomSelect for locations
    var allowCustomLocations = !!data.allowCustomLocations;
    document.querySelectorAll('.location-select').forEach(function (el) {
        new TomSelect(el, {
            create: allowCustomLocations,
            createOnBlur: allowCustomLocations,
            placeholder: '(Unchanged)',
            allowEmptyOption: true
        });
    });
})();
