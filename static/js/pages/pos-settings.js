/* POS Settings (templates/pos/settings.html): auto-email toggle, local cash
   drawer port (Desktop App or server ports) and the drawer test button. */
(function () {
    'use strict';

    document.getElementById('autoEmailCheck').addEventListener('change', function () {
        document.getElementById('autoEmailConfig').style.display = this.checked ? 'flex' : 'none';
    });

    // Local Hardware Settings Logic
    document.addEventListener('DOMContentLoaded', async function () {
        const select = document.getElementById('cashDrawerPort');
        const savedPort = localStorage.getItem('pos_cash_drawer_port');

        // --- ELECTRON MODE ---
        if (window.rscp) {
            console.log("Running in Desktop App Mode");

            try {
                // Fetch local ports from the Desktop App
                const result = await window.rscp.listPorts();

                if (result.success && select) {
                    // Clear server-side list options
                    select.innerHTML = '<option value="">-- Select Local Port --</option>';

                    if (result.ports.length === 0) {
                        const option = document.createElement('option');
                        option.text = "(No ports found)";
                        option.disabled = true;
                        select.appendChild(option);
                    }

                    result.ports.forEach(port => {
                        const option = document.createElement('option');
                        // Electron SerialPort returns 'path' (e.g., COM1)
                        option.value = port.path;
                        option.text = `${port.path} - ${port.manufacturer || 'Unknown Device'}`;

                        // Auto-select if it looks like our target
                        if (port.path.includes('HPCDPORT') && !savedPort) {
                            option.selected = true;
                        }
                        select.appendChild(option);
                    });

                    // Add Badge helper
                    const label = select.closest('.row').querySelector('label');
                    if (label) {
                        label.insertAdjacentHTML('beforeend', ' <span class="badge bg-primary ms-2" title="Scanning local computer">Desktop App Mode</span>');
                    }
                }
            } catch (e) {
                console.error("Failed to list Electron ports:", e);
                if (select) {
                    const option = document.createElement('option');
                    option.text = "Error scanning ports: " + e.message;
                    select.appendChild(option);
                }
            }
        }

        // Restore saved selection
        if (savedPort && select) {
            select.value = savedPort;
        }
    });

    // Intercept form submission to save to LocalStorage
    const forms = document.querySelectorAll('form');
    forms.forEach(form => {
        form.addEventListener('submit', function () {
            const portSelect = document.getElementById('cashDrawerPort');
            if (portSelect) {
                localStorage.setItem('pos_cash_drawer_port', portSelect.value);
            }
        });
    });

    function testDrawer() {
        let port = document.getElementById('cashDrawerPort').value;
        if (port === 'MANUAL_ENTRY') {
            port = document.getElementById('manualPortInput').value.trim();
        }

        if (!port) {
            alert('Please select or enter a port first');
            return;
        }

        // Save for convenience while testing
        localStorage.setItem('pos_cash_drawer_port', port);

        // Desktop App Mode
        if (window.rscp) {
            window.rscp.openDrawer(port).then(result => {
                if (result.success) {
                    alert('✓ Cash drawer opened (via Desktop App)!');
                } else {
                    alert('✗ Failed: ' + result.error);
                }
            });
            return;
        }

        // Web Mode
        fetch('/pos/hardware/test_drawer', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/x-www-form-urlencoded',
                'X-CSRFToken': RSCP.csrfToken(),
            },
            body: 'port=' + encodeURIComponent(port)
        })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    alert('✓ Cash drawer opened successfully!');
                } else {
                    alert('✗ Failed: ' + data.message);
                }
            })
            .catch(error => {
                alert('Error: ' + error);
            });
    }

    RSCP.actions({
        testDrawer: function () { testDrawer(); }
    });
})();
