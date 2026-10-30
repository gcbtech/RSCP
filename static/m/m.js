/*
 * Phone UI shell (templates/m/base.html): bottom sheets, the theme switch,
 * notifications, the toast and the no-connection bar. Plain ES5 so old
 * Android WebViews run it.
 *
 *   data-action="mOpenSheet" data-sheet="<dialog id>"   open a sheet
 *   data-action="mCloseSheet"                          close the sheet it is in
 *   RSCPM.toast('Saved')                               short status message
 *   RSCPM.openSheet(dialog) / RSCPM.closeSheet(dialog) from page scripts
 *   RSCPM.postJSON(url, body) -> Promise of the JSON reply (rejects with
 *                                the server's error text on a non-2xx)
 */
(function () {
    'use strict';

    var RSCPM = window.RSCPM = window.RSCPM || {};
    var unread = 0;

    // Notification links to desktop pages that have a phone page of their own
    var PHONE_LINKS = [
        [/^\/inventory\/edit\/(\d+)$/, '/m/stock/item/$1']
    ];
    var NOTIF_TONES = { info: 'ac', warning: 'wn', error: 'bd', success: 'ok' };

    function byId(id) { return document.getElementById(id); }

    // ------------------------------------------------------------ sheets

    // On a scanning page (static/m/scanner.js sets RSCPM.scanner) sheets open
    // non-modally over a backdrop of our own: a modal sheet would take the
    // focus from the scanner's capture input, and the FZ-N1 can't get it back
    // without its keyboard popping up. The focus goes back to the capture
    // input as a sheet opens, and before it closes.
    var backdrop = null;

    function showBackdrop(on) {
        if (on && !backdrop) {
            backdrop = document.createElement('div');
            backdrop.className = 'm-sheet-backdrop';
            backdrop.addEventListener('click', function () {
                var d = document.querySelector('dialog.m-sheet[open]');
                if (d) closeSheet(d);
            });
            document.body.appendChild(backdrop);
        }
        if (backdrop) backdrop.hidden = !on;
    }

    function openSheet(dialog) {
        if (!dialog || dialog.open) return;
        if (RSCPM.scanner && typeof dialog.show === 'function') {
            showBackdrop(true);
            dialog.show();
            RSCPM.scanner.keep();
        } else if (typeof dialog.showModal === 'function') {
            dialog.showModal();
        } else {
            dialog.setAttribute('open', '');
        }
    }

    function closeSheet(dialog) {
        if (!dialog) return;
        if (RSCPM.scanner) RSCPM.scanner.keep();
        if (typeof dialog.close === 'function') dialog.close();
        else dialog.removeAttribute('open');
        if (!document.querySelector('dialog.m-sheet[open]')) showBackdrop(false);
    }

    // A tap on the dimmed backdrop lands on the <dialog> itself, outside its box
    document.addEventListener('click', function (event) {
        var d = event.target;
        if (!d || d.tagName !== 'DIALOG' || !d.classList.contains('m-sheet')) return;
        var r = d.getBoundingClientRect();
        if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) {
            closeSheet(d);
        }
    });

    // ------------------------------------------------------------ theme

    function syncThemeRadios() {
        var current = window.RSCPTheme ? window.RSCPTheme.pref() : 'dark';
        var radios = document.querySelectorAll('input[name="mTheme"]');
        for (var i = 0; i < radios.length; i++) radios[i].checked = radios[i].value === current;
    }

    // ------------------------------------------------------------ notifications

    function getJSON(url, options) {
        options = options || {};
        options.credentials = 'same-origin';
        options.headers = options.headers || {};
        options.headers.Accept = 'application/json';
        return fetch(url, options).then(function (resp) {
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            return resp.json();
        });
    }

    function setUnread(count) {
        unread = count || 0;
        var badge = byId('mBellBadge');
        var bell = byId('mBell');
        if (!badge || !bell) return;
        badge.hidden = unread <= 0;
        badge.textContent = unread > 99 ? '99+' : String(unread);
        bell.setAttribute('aria-label', unread > 0 ? 'Notifications, ' + unread + ' unread' : 'Notifications');
        var readAll = byId('mReadAll');
        if (readAll) readAll.disabled = unread <= 0;
    }

    // Notification times are stored in UTC ("YYYY-MM-DD HH:MM:SS")
    function timeAgo(value) {
        if (!value) return '';
        var when = new Date(String(value).replace(' ', 'T') + (/[zZ]|[+-]\d\d:?\d\d$/.test(value) ? '' : 'Z'));
        if (isNaN(when.getTime())) return '';
        var s = Math.floor((Date.now() - when.getTime()) / 1000);
        if (s < 60) return 'Just now';
        if (s < 3600) return Math.floor(s / 60) + 'm ago';
        if (s < 86400) return Math.floor(s / 3600) + 'h ago';
        if (s < 604800) return Math.floor(s / 86400) + 'd ago';
        return when.toLocaleDateString();
    }

    function isSafeLink(link) {
        return typeof link === 'string' && link.charAt(0) === '/' && link.charAt(1) !== '/';
    }

    function phoneLink(link) {
        for (var i = 0; i < PHONE_LINKS.length; i++) {
            if (PHONE_LINKS[i][0].test(link)) return link.replace(PHONE_LINKS[i][0], PHONE_LINKS[i][1]);
        }
        return link;
    }

    // Icons drawn by the server (templates/m/_sheets.html), keyed by data-icon
    var notifIcons = null;
    function notifIcon(name) {
        if (!notifIcons) {
            notifIcons = {};
            var tpl = byId('mNotifIcons');
            var spans = tpl && tpl.content ? tpl.content.querySelectorAll('[data-icon]') : [];
            for (var i = 0; i < spans.length; i++) notifIcons[spans[i].getAttribute('data-icon')] = spans[i].innerHTML;
        }
        return notifIcons[name] || '';
    }

    function renderNotifications(items) {
        var list = byId('mNotifList');
        if (!list) return;
        if (!items.length) {
            list.innerHTML = '<p class="m-empty">No notifications.</p>';
            return;
        }
        list.innerHTML = items.map(function (n) {
            var link = isSafeLink(n.link) ? phoneLink(n.link) : null;
            var desktop = link && link.indexOf('/m/') !== 0;
            var type = NOTIF_TONES[n.type] ? n.type : 'info';
            var tag = link ? 'a' : 'div';
            return '<' + tag + ' class="m-row m-notif' + (n.is_read ? '' : ' m-notif-unread') + '"' +
                (link ? ' href="' + escapeHtml(link) + '"' : '') + ' data-notif-id="' + escapeHtml(n.id) + '">' +
                '<span class="m-lead m-tone-' + NOTIF_TONES[type] + '">' + notifIcon(type) + '</span>' +
                '<span class="m-t"><span class="m-t1">' + (n.is_read ? '' : '<span class="m-sr">Unread: </span>') +
                escapeHtml(n.title) + '</span>' +
                (n.message ? '<span class="m-t2">' + escapeHtml(n.message) + '</span>' : '') +
                '<span class="m-notif-time">' + escapeHtml(timeAgo(n.created_at)) +
                (desktop ? ' · Opens the desktop page' : '') + '</span></span>' +
                (desktop ? notifIcon('desktop') : '') +
                '</' + tag + '>';
        }).join('');
    }

    function refreshCount() {
        var bell = byId('mBell');
        if (!bell || document.visibilityState === 'hidden') return;
        getJSON(bell.getAttribute('data-count-url'))
            .then(function (data) { setUnread(data.count); })
            .catch(function () { /* offline or signed out: keep the last count */ });
    }

    function loadNotifications() {
        var bell = byId('mBell');
        var list = byId('mNotifList');
        if (!bell || !list) return;
        list.innerHTML = '<p class="m-empty">Loading…</p>';
        getJSON(bell.getAttribute('data-list-url') + '?include_read=true&limit=30')
            .then(function (data) {
                renderNotifications(data.notifications || []);
                setUnread(data.unread_count);
            })
            .catch(function () { list.innerHTML = '<p class="m-empty">Couldn’t load notifications.</p>'; });
    }

    // Opening a notification marks it read (the link then navigates as usual)
    document.addEventListener('click', function (event) {
        var item = event.target.closest ? event.target.closest('[data-notif-id]') : null;
        if (!item || !item.classList.contains('m-notif-unread')) return;
        item.classList.remove('m-notif-unread');
        setUnread(unread - 1);
        var bell = byId('mBell');
        var url = bell.getAttribute('data-list-url').replace(/\/list$/, '/' + item.getAttribute('data-notif-id') + '/read');
        fetch(url, { method: 'POST', credentials: 'same-origin', keepalive: true,
                     headers: { 'X-CSRFToken': RSCP.csrfToken() } }).catch(function () {});
    });

    RSCPM.openSheet = openSheet;
    RSCPM.closeSheet = closeSheet;

    RSCPM.postJSON = function (url, body) {
        return fetch(url, {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'X-CSRFToken': RSCP.csrfToken() },
            body: JSON.stringify(body || {})
        }).then(function (resp) {
            return resp.json().catch(function () { return {}; }).then(function (data) {
                if (!resp.ok) {
                    var err = new Error(data.error || data.message || 'The server said ' + resp.status);
                    err.status = resp.status;
                    err.data = data;
                    throw err;
                }
                return data;
            });
        }, function () {
            throw new Error('No connection');
        });
    };

    // ------------------------------------------------------------ toast

    var toastTimer = null;
    RSCPM.toast = function (message) {
        var el = byId('mToast');
        if (!el) return;
        el.textContent = message;
        el.hidden = false;
        clearTimeout(toastTimer);
        toastTimer = setTimeout(function () { el.hidden = true; }, 3000);
    };

    // ------------------------------------------------------------ actions

    RSCP.actions({
        mOpenSheet: function (el) {
            if (el.getAttribute('data-sheet') === 'mAccount') syncThemeRadios();
            var from = el.closest('dialog');   // a sheet opened from another sheet replaces it
            if (from) closeSheet(from);
            openSheet(byId(el.getAttribute('data-sheet')));
        },
        mCloseSheet: function (el) {
            closeSheet(el.closest('dialog'));
        },
        mSetTheme: function (el) {
            if (el.checked && window.RSCPTheme) window.RSCPTheme.set(el.value);
        },
        mOpenNotifications: function () {
            openSheet(byId('mNotifications'));
            loadNotifications();
        },
        mMarkAllRead: function () {
            var bell = byId('mBell');
            getJSON(bell.getAttribute('data-read-all-url'), {
                method: 'POST', headers: { 'X-CSRFToken': RSCP.csrfToken() }
            }).then(function () {
                setUnread(0);
                var rows = document.querySelectorAll('.m-notif-unread');
                for (var i = 0; i < rows.length; i++) rows[i].classList.remove('m-notif-unread');
            }).catch(function () { RSCPM.toast('Couldn’t mark notifications read'); });
        }
    });

    // Section chips scroll sideways: bring the current one into view
    var chipOn = document.querySelector('.m-chips .m-chip.on');
    if (chipOn && chipOn.offsetLeft + chipOn.offsetWidth > chipOn.parentNode.clientWidth) {
        chipOn.parentNode.scrollLeft = chipOn.offsetLeft - 12;
    }

    refreshCount();
    setInterval(refreshCount, 60000);
    document.addEventListener('visibilitychange', refreshCount);

    // ------------------------------------------------------------ connection

    function showOffline() {
        var bar = byId('mOffline');
        if (bar) bar.hidden = navigator.onLine !== false;
    }
    window.addEventListener('online', showOffline);
    window.addEventListener('offline', showOffline);
    showOffline();
})();
