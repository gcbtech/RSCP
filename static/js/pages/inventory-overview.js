/* Inventory Overview (templates/inventory/overview.html). Needs Chart.js and
   window.rscpPoll (from _nav.html). */
(function () {
    'use strict';

    // Sales trend chart
    const ctx = document.getElementById('salesChart').getContext('2d');
    const salesData = (RSCP.pageData() || {}).salesTrend || [];

    new Chart(ctx, {
        type: 'line',
        data: {
            labels: salesData.map(d => d.date),
            datasets: [{
                label: 'Units Sold/Consumed',
                data: salesData.map(d => d.count),
                borderColor: '#6f42c1',
                backgroundColor: 'rgba(111, 66, 193, 0.1)',
                fill: true,
                tension: 0.3
            }]
        },
        options: {
            responsive: true,
            plugins: {
                legend: { display: false }
            },
            scales: {
                y: { beginAtZero: true }
            }
        }
    });

    // Auto-Refresh Stats (every 5s while the tab is visible)
    rscpPoll(function (signal) {
        return fetch('/inventory/api/overview', { signal })
            .then(r => r.json())
            .then(data => {
                if (data.total_items !== undefined) document.getElementById('stat_total_items').innerText = data.total_items;
                if (data.total_quantity !== undefined) document.getElementById('stat_total_quantity').innerText = data.total_quantity;
                if (data.last_audit !== undefined) document.getElementById('stat_last_audit').innerText = data.last_audit;
                if (data.low_stock !== undefined) document.getElementById('stat_low_stock').innerText = data.low_stock;
                if (data.out_of_stock !== undefined) document.getElementById('stat_out_of_stock').innerText = data.out_of_stock;
            })
            .catch(e => console.error("Stats refresh failed", e));
    }, 5000, { delayFirst: true });
})();
