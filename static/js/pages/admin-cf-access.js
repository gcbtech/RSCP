/* Admin > Settings > Cloudflare Access: the Test button (GET /admin/cf-access/test). */
(function () {
    'use strict';

    function show(el, ok, html) {
        el.className = 'small mt-2 ' + (ok ? 'text-success' : 'text-danger');
        el.innerHTML = html;
    }

    RSCP.actions({
        cfAccessTest: function (button) {
            var out = document.getElementById('cfAccessTestResult');
            if (!out) return;
            out.className = 'small mt-2 text-muted';
            out.textContent = 'Testing...';
            fetch(button.dataset.url, { headers: { 'Accept': 'application/json' }, credentials: 'same-origin' })
                .then(function (r) { return r.json(); })
                .then(function (d) {
                    if (d.valid) {
                        show(out, true, 'Valid Access token for <b>' + escapeHtml(d.email) + '</b> &rarr; RSCP user <b>' +
                            escapeHtml(d.user) + '</b>.' + (d.would_sign_in ? '' :
                            ' Sign-in is switched off, so nobody is signed in with it yet.'));
                    } else if (!d.header_present) {
                        show(out, false, 'This request carried no Cloudflare Access token. ' +
                            'Open RSCP through the tunnel and try again.');
                    } else {
                        show(out, false, 'Token not accepted: ' + escapeHtml(d.error || 'unknown error') + '.');
                    }
                })
                .catch(function () { show(out, false, 'Test failed: could not reach RSCP.'); });
        }
    });
})();
