/* POS Management dashboard (templates/pos/management.html). Needs Chart.js. */
(function () {
    'use strict';

    const data = RSCP.pageData() || {};

    RSCP.actions({
        // Days preset dropdown
        handleDaysChange: function (el) {
            const value = el.value;
            if (value === 'custom') {
                document.getElementById('customRangeInputs').style.display = 'flex';
            } else {
                location.href = '?days=' + value;
            }
        },

        applyCustomRange: function () {
            const start = document.getElementById('startDate').value;
            const end = document.getElementById('endDate').value;
            if (start && end) {
                location.href = '?start_date=' + start + '&end_date=' + end;
            }
        }
    });

    // Sales Chart
    const salesData = data.salesData || [];
    const salesLabels = data.salesLabels || [];

    new Chart(document.getElementById('salesChart'), {
        type: 'line',
        data: {
            labels: salesLabels.map(d => {
                // d is YYYY-MM-DD. Parse manually to avoid timezone shift
                const [y, m, day] = d.split('-').map(Number);
                const date = new Date(y, m - 1, day);
                return date.toLocaleDateString();
            }),
            datasets: [{
                label: 'Revenue',
                data: salesData,
                borderColor: '#2563eb',
                backgroundColor: 'rgba(37, 99, 235, 0.1)',
                fill: true,
                tension: 0.3
            }]
        },
        options: {
            responsive: true,
            plugins: { legend: { display: false } },
            scales: {
                y: { beginAtZero: true, ticks: { callback: v => '$' + v } }
            }
        }
    });

    // Payment Chart
    const paymentLabels = data.paymentLabels || [];
    const paymentData = data.paymentData || [];

    new Chart(document.getElementById('paymentChart'), {
        type: 'doughnut',
        data: {
            labels: paymentLabels.map(l => l.charAt(0).toUpperCase() + l.slice(1)),
            datasets: [{
                data: paymentData,
                backgroundColor: ['#16a34a', '#2563eb', '#f59e0b']
            }]
        },
        options: {
            responsive: true,
            plugins: { legend: { position: 'bottom' } }
        }
    });
})();
