/* POS receipt page (templates/pos/receipt.html): Print button, plus the post-checkout
   ?auto_print=1 / ?open_drawer=1 hand-off (cash-drawer kick, printable view). */
(function () {
    'use strict';

    RSCP.actions({
        printPage: function () { window.print(); }
    });

    const urlParams = new URLSearchParams(window.location.search);
    const shouldAutoPrint = urlParams.get('auto_print') === '1';
    const shouldOpenDrawer = urlParams.get('open_drawer') === '1';

    // Clean up URL (remove query params) to prevent re-triggering on refresh
    if (shouldAutoPrint || shouldOpenDrawer) {
        const cleanUrl = window.location.pathname;
        window.history.replaceState({}, document.title, cleanUrl);
    }

    // Open cash drawer if requested
    if (shouldOpenDrawer) {
        // ELECTRON DESKTOP APP MODE
        if (window.rscp) {
            const savedPort = localStorage.getItem('pos_cash_drawer_port');
            // Default to HPCDPORT-1 if they haven't set it explicitly yet, as that's our target
            const port = savedPort || 'HPCDPORT-1';

            console.log(`Triggering Drawer (Electron) on port: ${port}`);
            // Fire and forget - no popup wanted
            window.rscp.openDrawer(port).catch(err => console.error('Electron Drawer Error:', err));
        }
        // WEB BROWSER MODE
        else {
            fetch('/pos/hardware/open_drawer', {
                method: 'POST', headers: { 'X-CSRFToken': RSCP.csrfToken() },
                credentials: 'same-origin'
            })
                .then(response => response.json())
                .then(data => {
                    if (!data.success) {
                        console.warn('Cash drawer failed:', data.message);
                    }
                })
                .catch(err => console.error('Drawer error:', err));
        }
    }

    // Auto-print if requested
    if (shouldAutoPrint) {
        // Small delay to ensure page is fully rendered
        setTimeout(function () {
            // Open printable view in new window
            const printUrl = window.location.pathname + '/print';
            const printWindow = window.open(printUrl, '_blank', 'width=400,height=600');

            if (printWindow) {
                // Wait for the print window to load, then print
                printWindow.onload = function () {
                    printWindow.print();
                    // Close after a brief delay to allow print dialog to appear
                    setTimeout(function () {
                        printWindow.close();
                    }, 500);
                };
            }
        }, 300);
    }
})();
