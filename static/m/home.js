/*
 * Phone Home (templates/m/home.html): keeps today's numbers current. Fully
 * leaves Home open for hours, so every minute while the screen is on, and
 * as soon as it comes back on, the page is fetched again and #mHome is
 * swapped for the new copy. Plain ES5 (Chrome 70).
 */
(function () {
    'use strict';

    var EVERY_MS = 60000;
    var last = Date.now();
    var busy = false;

    function refresh() {
        if (busy || document.visibilityState === 'hidden') return;
        busy = true;
        fetch(location.pathname, { credentials: 'same-origin', headers: { Accept: 'text/html' } })
            .then(function (resp) { return resp.ok ? resp.text() : null; })
            .then(function (html) {
                // Signed out: the redirect lands on the sign-in page, which has no #mHome
                var fresh = html && new DOMParser().parseFromString(html, 'text/html').getElementById('mHome');
                var current = document.getElementById('mHome');
                if (fresh && current) current.innerHTML = fresh.innerHTML;
                last = Date.now();
            })
            .catch(function () { /* offline: try again next time */ })
            .then(function () { busy = false; });
    }

    setInterval(refresh, EVERY_MS);
    document.addEventListener('visibilitychange', function () {
        if (document.visibilityState === 'visible' && Date.now() - last > 15000) refresh();
    });
})();
