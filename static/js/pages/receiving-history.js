/* templates/history.html */
(function () {
    'use strict';

    RSCP.actions({
        toggleFilters: function () {
            const card = document.getElementById('filterCard');
            const icon = document.getElementById('filterIcon');
            if (card.style.display === 'none') {
                card.style.display = 'block';
                icon.innerText = '▲';
            } else {
                card.style.display = 'none';
                icon.innerText = '▼';
            }
        }
    });

    // Legacy device dark-mode flag (set on /settings)
    if (localStorage.getItem('rscp_darkmode') === 'true') {
        document.body.style.backgroundColor = '#121212';
        document.body.style.color = '#e0e0e0';
        document.querySelectorAll('.list-group-item').forEach(el => {
            el.style.backgroundColor = '#1e1e1e';
            el.style.color = '#fff';
            el.style.borderColor = '#444';
            el.style.borderLeftColor = '#0d6efd';
        });
    }
})();
