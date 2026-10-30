/*
 * Phone Time pages (templates/m/time/): the running shift or break time,
 * the time-off form's whole/part switch, and one submit per tap.
 * Plain ES5 (Chrome 70).
 */
(function () {
    'use strict';

    var PAGE = RSCP.pageData() || {};
    // The phone's clock may be off: count from the server's time at render
    var offset = PAGE.nowMs ? PAGE.nowMs - Date.now() : 0;

    function tick() {
        var els = document.querySelectorAll('[data-since]');
        for (var i = 0; i < els.length; i++) {
            var mins = Math.max(0, Math.floor((Date.now() + offset - Number(els[i].getAttribute('data-since'))) / 60000));
            els[i].textContent = mins >= 60 ? Math.floor(mins / 60) + 'h ' + (mins % 60) + 'm' : mins + 'm';
        }
    }
    tick();
    setInterval(tick, 15000);

    // Time off: start and end times only for part of a shift
    function showTimes() {
        var form = document.getElementById('mPtoForm');
        if (!form) return;
        var partial = form.querySelector('input[name="request_type"][value="partial"]').checked;
        document.getElementById('mPtoTimes').hidden = !partial;
        document.getElementById('mPtoFullHint').hidden = partial;
        document.getElementById('mPtoStart').required = partial;
        document.getElementById('mPtoEnd').required = partial;
    }
    showTimes();

    RSCP.actions({
        mPtoType: showTimes
    });

    // A double tap sends one clock change or request (a cancelled confirm never gets here)
    document.addEventListener('submit', function (event) {
        if (event.defaultPrevented) return;
        var buttons = event.target.querySelectorAll('button[type="submit"]');
        setTimeout(function () {
            for (var i = 0; i < buttons.length; i++) buttons[i].disabled = true;
        }, 0);
    });
})();
