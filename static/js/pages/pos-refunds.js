/* POS refunds list (templates/pos/refunds.html): quick date-range filter buttons. */
(function () {
    'use strict';

    // Quick date filter buttons
    function setDateFilter(daysBack) {
        const form = document.querySelector('form[action*="refunds"]');
        const dateFrom = form.querySelector('input[name="date_from"]');
        const dateTo = form.querySelector('input[name="date_to"]');
        
        const today = new Date();
        const start = new Date();
        start.setDate(today.getDate() - daysBack);
        
        dateTo.value = today.toISOString().split('T')[0];
        dateFrom.value = start.toISOString().split('T')[0];
        form.submit();
    }
    
    document.getElementById('btnToday')?.addEventListener('click', () => setDateFilter(0));
    document.getElementById('btnWeek')?.addEventListener('click', () => setDateFilter(6));
    document.getElementById('btnMonth')?.addEventListener('click', () => setDateFilter(29));
})();
