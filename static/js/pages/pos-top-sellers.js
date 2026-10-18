/* Top Sellers report (templates/pos/top_sellers.html). Needs Chart.js;
   the print button uses pos-mgmt-common.js. */
(function () {
    'use strict';

    const data = RSCP.pageData() || {};

    // Quick date range buttons
    (function () {
        const form = document.getElementById('filterForm');
        if (!form) return;
        const firstSale = data.firstSaleDate || null;
        const fmt = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;

        form.querySelectorAll('[data-range]').forEach(btn => btn.addEventListener('click', () => {
            const today = new Date();
            today.setHours(0, 0, 0, 0);
            let start = new Date(today), end = new Date(today);
            switch (btn.dataset.range) {
                case 'yesterday': start.setDate(today.getDate() - 1); end = new Date(start); break;
                case '7': start.setDate(today.getDate() - 6); break;
                case '30': start.setDate(today.getDate() - 29); break;
                case '90': start.setDate(today.getDate() - 89); break;
                case 'month': start = new Date(today.getFullYear(), today.getMonth(), 1); break;
                case 'last_month':
                    start = new Date(today.getFullYear(), today.getMonth() - 1, 1);
                    end = new Date(today.getFullYear(), today.getMonth(), 0);
                    break;
                case 'ytd': start = new Date(today.getFullYear(), 0, 1); break;
                case 'all': if (firstSale) start = new Date(firstSale + 'T00:00:00'); break;
            }
            form.elements.start.value = fmt(start);
            form.elements.end.value = fmt(end);
            form.submit();
        }));
    })();

    // Sales trend chart (the canvas is only rendered when there is trend data)
    const trend = data.trend || [];
    const canvas = document.getElementById('trendChart');
    if (trend.length && canvas) {
        new Chart(canvas, {
            data: {
                labels: trend.map(t => t.label),
                datasets: [
                    {
                        type: 'bar', label: 'Net Revenue', data: trend.map(t => t.revenue),
                        backgroundColor: 'rgba(99, 102, 241, 0.55)', yAxisID: 'y'
                    },
                    {
                        type: 'line', label: 'Net Units', data: trend.map(t => t.units),
                        borderColor: '#f59e0b', backgroundColor: '#f59e0b', tension: 0.3, yAxisID: 'y1'
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                scales: {
                    y: { beginAtZero: true, position: 'left', ticks: { callback: v => '$' + v.toLocaleString() } },
                    y1: { beginAtZero: true, position: 'right', grid: { drawOnChartArea: false }, ticks: { precision: 0 } }
                }
            }
        });
    }
})();
