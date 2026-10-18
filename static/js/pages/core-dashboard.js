/* templates/dashboard.html (receiving dashboard) */
(function () {
    'use strict';

    // Live Dashboard Logic
    let sparkChart = null;

    function updateStats(signal) {
        return fetch('/api/dashboard_stats', { signal })
            .then(response => response.json())
            .then(data => {
                const s = data.stats;
                const g = data.graph; // Analytics Data

                // Update Numbers (Safe check)
                const setText = (id, txt) => { const el = document.getElementById(id); if (el) el.innerText = txt; };

                setText('val-expected', `${s.expected.scanned} / ${s.expected.total}`);
                setText('val-pastdue', s.past_due.count);
                setText('val-scanned', data.scans);
                setText('val-returns', s.returns.open);
                setText('val-refunded', s.refunded.count);

                // Update Colors (Classes)
                updateStatus('card-expected', s.expected.status);
                updateStatus('card-pastdue', s.past_due.status);
                updateStatus('card-returns', s.returns.status);
                updateStatus('card-refunded', s.refunded.status);

                // Render Sparkline if not already
                if (g && !sparkChart) {
                    const ctx = document.getElementById('scannedSparkline').getContext('2d');
                    sparkChart = new Chart(ctx, {
                        type: 'line',
                        data: {
                            labels: g.map(d => {
                                const parts = d.date.split('-');
                                return `${parts[1]}/${parts[2]}`; // MM/DD
                            }),
                            datasets: [{
                                data: g.map(d => d.count),
                                borderColor: '#007bff',
                                backgroundColor: 'rgba(0, 123, 255, 0.25)',
                                borderWidth: 2,
                                tension: 0.4,
                                fill: true,
                                pointRadius: 2, // Small dots
                                pointBackgroundColor: '#007bff'
                            }]
                        },
                        options: {
                            responsive: true,
                            maintainAspectRatio: false,
                            plugins: { legend: { display: false }, tooltip: { enabled: false } },
                            scales: {
                                x: {
                                    display: true, // Show Axis
                                    grid: { display: false, drawBorder: false },
                                    ticks: {
                                        font: { size: 9 },
                                        color: '#666',
                                        maxRotation: 0,
                                        autoSkip: true,
                                        maxTicksLimit: 7
                                    }
                                },
                                y: { display: false, beginAtZero: true }
                            },
                            layout: { padding: { left: -5, right: -5, bottom: 0 } }
                        }
                    });
                } else if (sparkChart && g) {
                    // Update Data
                    sparkChart.data.labels = g.map(d => {
                        const parts = d.date.split('-');
                        return `${parts[1]}/${parts[2]}`;
                    });
                    sparkChart.data.datasets[0].data = g.map(d => d.count);
                    sparkChart.update('none');
                }
            })
            .catch(e => console.error("Stats update failed", e));
    }

    // Init (refreshes every 5s while the tab is visible)
    rscpPoll(updateStats, 5000);

    function updateStatus(id, newStatus) {
        const el = document.getElementById(id);
        if (!el) return;
        // Remove old status classes
        el.classList.remove('status-green', 'status-red', 'status-gold');
        // Add new
        if (newStatus) el.classList.add(`status-${newStatus}`);
    }

    // --- TIMEZONE CHECK ---
    // compare server time with client time
    const serverTime = parseInt(RSCP.pageData().serverTs);
    const clientTime = new Date().getTime();
    const diff = Math.abs(serverTime - clientTime);
    if (diff > 1000 * 60 * 60) { // > 1 hour difference
        document.getElementById('tzAlert').style.display = 'block';
    }

    const u = localStorage.getItem('dockops_user');
    if (u) document.getElementById('welcomeUser').innerText = "Logged in as: " + u;
})();
