/* templates/settings.html (device settings) */
(function () {
    'use strict';

    // Init Report Context
    document.getElementById('report_source_url').value = window.location.href;
    document.getElementById('report_user_agent').value = navigator.userAgent;

    // Load current state
    const dm = localStorage.getItem('rscp_darkmode');
    const switchEl = document.getElementById('darkModeSwitch');

    if (dm === 'true') {
        document.body.classList.add('dark-mode');
        switchEl.checked = true;
    }

    // Handle Toggle
    switchEl.addEventListener('change', function () {
        const isDark = this.checked;
        localStorage.setItem('rscp_darkmode', isDark);

        if (isDark) document.body.classList.add('dark-mode');
        else document.body.classList.remove('dark-mode');
    });

    RSCP.actions({
        showReportModal: function () {
            new bootstrap.Modal(document.getElementById('reportModal')).show();
        }
    });
})();
