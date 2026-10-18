/*
 * Navigation sidebar + shared page helpers. Loaded by templates/_nav.html (so
 * every page that shows the nav gets it, including standalone ones like
 * scan.html). Requires rscp-common.js (loaded earlier, in <head>).
 *
 * Globals other pages use: rscpPoll, showToast, toggleDarkMode, toggleSidebar,
 * toggleNotifications, loadNotifications, openNotification, markAllRead.
 */

// Sidebar toggle persistence
(function () {
    'use strict';
    var sidebarKey = 'rscp_sidebar_hidden';
    if (localStorage.getItem(sidebarKey) === 'true') {
        document.body.classList.add('sidebar-hidden');
    }

    window.toggleSidebar = function () {
        document.body.classList.toggle('sidebar-hidden');
        localStorage.setItem(sidebarKey, document.body.classList.contains('sidebar-hidden'));
    };
})();

// Stale-backup warning for admins (app/backup.py); dismissed until the browser session ends
(function () {
    'use strict';
    var el = document.getElementById('backupWarning'), key = 'rscp_backup_warning_dismissed';
    if (!el) return;
    try { if (sessionStorage.getItem(key) === el.dataset.key) return; } catch (e) { }
    el.style.display = '';
    el.querySelector('.btn-close').addEventListener('click', function () {
        el.remove();
        try { sessionStorage.setItem(key, el.dataset.key); } catch (e) { }
    });
})();

// Dark Mode Persistence
(function () {
    'use strict';
    const darkModeKey = 'rscp_dark_mode';

    // Load saved preference on page load
    function loadDarkMode() {
        const savedMode = localStorage.getItem(darkModeKey);
        if (savedMode === 'true') {
            document.body.classList.add('dark-mode');
        } else if (savedMode === 'false') {
            document.body.classList.remove('dark-mode');
        }
        // If no saved preference, do nothing (use default)
    }

    // Save preference when toggled
    function saveDarkMode(enabled) {
        localStorage.setItem(darkModeKey, enabled.toString());
    }

    // Run on page load
    loadDarkMode();

    // Expose toggle function globally for any dark mode buttons
    window.toggleDarkMode = function () {
        document.body.classList.toggle('dark-mode');
        saveDarkMode(document.body.classList.contains('dark-mode'));
    };
})();

// Toast Notifications - Auto-dismiss flash messages
(function () {
    'use strict';
    function createToast(message, type) {
        const container = document.getElementById('toastContainer');
        if (!container) return;

        const bgClass = type === 'error' ? 'bg-danger' :
            type === 'warning' ? 'bg-warning text-dark' :
                type === 'info' ? 'bg-info text-dark' : 'bg-success';

        const toast = document.createElement('div');
        toast.className = `toast show align-items-center ${bgClass} text-white border-0`;
        toast.setAttribute('role', 'alert');
        toast.innerHTML = `
            <div class="d-flex">
                <div class="toast-body"></div>
                <button type="button" class="btn-close btn-close-white me-2 m-auto" data-action="dismissToast"></button>
            </div>
        `;
        toast.querySelector('.toast-body').textContent = message;

        container.appendChild(toast);

        // Auto-dismiss after 5 seconds
        setTimeout(() => {
            toast.classList.remove('show');
            setTimeout(() => toast.remove(), 300);
        }, 5000);
    }

    // Convert any existing flash messages to toasts on page load
    document.addEventListener('DOMContentLoaded', function () {
        const alerts = document.querySelectorAll('.alert-dismissible, .alert-toast');
        alerts.forEach(alert => {
            const message = alert.textContent.trim();
            const type = alert.classList.contains('alert-danger') ? 'error' :
                alert.classList.contains('alert-warning') ? 'warning' :
                    alert.classList.contains('alert-info') ? 'info' : 'success';
            createToast(message, type);
            alert.remove();
        });
    });

    // Expose globally for manual use
    window.showToast = createToast;
})();

// Notification System
(function () {
    'use strict';
    let notificationDropdownOpen = false;

    window.toggleNotifications = function () {
        const dropdown = document.getElementById('notificationDropdown');
        if (!dropdown) return;

        notificationDropdownOpen = !notificationDropdownOpen;
        dropdown.style.display = notificationDropdownOpen ? 'block' : 'none';

        if (notificationDropdownOpen) {
            window.loadNotifications();
        }
    };

    window.loadNotifications = function () {
        const list = document.getElementById('notificationList');
        if (!list) return;

        fetch('/api/notifications/list?include_read=true&limit=15')
            .then(r => r.json())
            .then(data => {
                if (data.notifications && data.notifications.length > 0) {
                    list.innerHTML = data.notifications.map(n => `
                        <div class="notification-item ${n.is_read ? '' : 'unread'} notification-type-${escapeHtml(n.type)}"
                             data-id="${escapeHtml(n.id)}" data-link="${escapeHtml(n.link || '')}"
                             data-action="openNotification">
                            <div class="title">${escapeHtml(n.title)}</div>
                            ${n.message ? `<div class="message">${escapeHtml(n.message)}</div>` : ''}
                            <div class="time">${formatTimeAgo(n.created_at)}</div>
                        </div>
                    `).join('');
                } else {
                    list.innerHTML = '<div class="text-center text-muted py-4">No notifications</div>';
                }
                updateBadge(data.unread_count);
            })
            .catch(() => {
                list.innerHTML = '<div class="text-center text-muted py-3">Could not load notifications</div>';
            });
    };

    window.openNotification = function (id, link) {
        // Mark as read
        fetch(`/api/notifications/${id}/read`, { method: 'POST', headers: { 'X-CSRFToken': RSCP.csrfToken() } })
            .then(() => {
                if (link) {
                    window.location.href = link;
                } else {
                    window.loadNotifications();
                }
            });
    };

    window.markAllRead = function () {
        fetch('/api/notifications/read-all', { method: 'POST', headers: { 'X-CSRFToken': RSCP.csrfToken() } })
            .then(() => window.loadNotifications());
    };

    function updateBadge(count) {
        const badge = document.getElementById('notificationBadge');
        if (badge) {
            badge.textContent = count > 99 ? '99+' : count;
            badge.style.display = count > 0 ? 'inline-block' : 'none';
        }
    }

    function formatTimeAgo(dateStr) {
        const date = new Date(dateStr);
        const now = new Date();
        const diff = Math.floor((now - date) / 1000);

        if (diff < 60) return 'Just now';
        if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
        if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
        if (diff < 604800) return Math.floor(diff / 86400) + 'd ago';
        return date.toLocaleDateString();
    }

    // Close dropdown when clicking outside
    document.addEventListener('click', function (e) {
        const wrapper = document.querySelector('.notification-wrapper');
        if (wrapper && !wrapper.contains(e.target) && notificationDropdownOpen) {
            window.toggleNotifications();
        }
    });

    // Load unread count on page load
    document.addEventListener('DOMContentLoaded', function () {
        fetch('/api/notifications/count')
            .then(r => r.json())
            .then(data => updateBadge(data.count))
            .catch(() => { });
    });
})();

// Nav click handlers (data-action="..." in _nav.html and in markup built above)
RSCP.actions({
    toggleSidebar: function () { window.toggleSidebar(); },
    toggleDarkMode: function () { window.toggleDarkMode(); },
    toggleNotifications: function () { window.toggleNotifications(); },
    markAllRead: function () { window.markAllRead(); },
    openNotification: function (el) { window.openNotification(el.dataset.id, el.dataset.link); },
    dismissToast: function (el) { el.parentElement.parentElement.remove(); }
});

// Background polling for live-updating pages. Each run waits for the previous one to
// finish, a request that hangs is aborted after `timeoutMs`, and polling pauses while
// the tab is hidden, then runs again the moment the tab is shown. (setInterval + fetch
// keeps starting requests whether or not the last ones finished, so a tab left open
// can build a queue of stuck requests that the next click has to wait behind.)
//   task(signal) -> Promise   pass `signal` to fetch()
//   options.delayFirst        wait one interval before the first run (data already on the page)
window.rscpPoll = function (task, intervalMs, options) {
    'use strict';
    options = options || {};
    const timeoutMs = options.timeoutMs || 10000;
    let timer = null;
    let running = false;

    async function run() {
        clearTimeout(timer);
        timer = null;
        if (running || document.visibilityState === 'hidden') return;
        running = true;
        const controller = new AbortController();
        const abortTimer = setTimeout(() => controller.abort(), timeoutMs);
        try {
            await task(controller.signal);
        } catch (e) {
            if (e.name !== 'AbortError') console.warn('[rscpPoll]', e);
        } finally {
            clearTimeout(abortTimer);
            running = false;
            if (document.visibilityState !== 'hidden') timer = setTimeout(run, intervalMs);
        }
    }

    document.addEventListener('visibilitychange', function () {
        if (document.visibilityState === 'visible') run();
    });
    if (options.delayFirst) timer = setTimeout(run, intervalMs);
    else run();
    return { refresh: run };
};
