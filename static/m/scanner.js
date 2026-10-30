/*
 * Hardware barcode scanner for the phone UI (Panasonic FZ-N1 in Fully Kiosk).
 * One copy for every phone screen that scans. Plain ES5 (Chrome 70).
 *
 *   var scanner = RSCPScanner.attach({
 *       capture: document.getElementById('mScanCapture'), // hidden text input
 *       status: document.getElementById('mScanStatus'),   // gets data-state="listening|paused|lost"
 *       fields: [document.getElementById('mScanManual')], // visible inputs that also submit
 *       onScan: function (code, source) { ... }           // source: 'scanner' | 'field'
 *   });
 *   scanner.focus();   // after typing in a field: put the keyboard away, listen again
 *
 * How the FZ-N1 delivers a scan (seen over ADB, 2026-10-29): Panasonic's
 * barcode service hands the text to the on-screen keyboard app, which types
 * it into the focused text field and then sends Enter. It only types when
 * the keyboard app holds a text connection to that field. With no text
 * field focused, or a field focused the wrong way, the barcode is dropped
 * (the page sees keydown "Unidentified" and nothing else).
 *
 * Rules that keep that connection without the on-screen keyboard, whose
 * appearance brings Android's status and navigation bars back over Fully
 * (each one checked on the device):
 * - Focus the capture input while the page loads, before any tap. Chrome
 *   then opens a text connection without showing the keyboard. After the
 *   first tap, any focus from script shows it.
 * - Only then set inputmode="none". Focusing a field that already has it
 *   gets no text connection at all.
 * - Never let the focus fall to "nothing": that drops the connection, and
 *   it can't be won back without the keyboard. Taps that aren't on a text
 *   field keep the focus where it is (mousedown default prevented; the
 *   click still happens). Focus moves from text field to text field
 *   directly: back to the capture input when typing is done, and before a
 *   sheet closes (static/m/m.js opens sheets non-modally, so the capture
 *   input stays focusable behind them).
 * If the connection is lost anyway, the card says so and a tap on it
 * reloads the page, which starts it fresh.
 *
 * A scan ends at Enter or Tab, or after 250 ms of quiet. On a visible field
 * only a burst typed at scanner speed (keys under 40 ms apart) ends on
 * quiet, so a person typing slowly is never submitted half-way.
 */
(function () {
    'use strict';

    var FAST_GAP_MS = 40;
    var IDLE_SUBMIT_MS = 250;
    var MIN_BURST = 4;
    var INPUTMODE_DELAY_MS = 300;

    function isTextEntry(el) {
        if (!el || el === document.body) return false;
        var tag = el.tagName;
        return tag === 'TEXTAREA' || el.isContentEditable ||
            (tag === 'INPUT' && !/^(button|submit|reset|checkbox|radio|range|color|file|image|hidden)$/i.test(el.type));
    }

    /** Taps on these may take the focus: text fields, their labels, selects. */
    function takesFocus(el) {
        if (isTextEntry(el) || (el && el.tagName === 'SELECT')) return true;
        var label = el && el.closest ? el.closest('label') : null;
        return !!(label && label.control && (isTextEntry(label.control) || label.control.tagName === 'SELECT'));
    }

    function openSheet() {
        return document.querySelector('dialog[open]');
    }

    function isEnd(event) {
        return event.key === 'Enter' || event.keyCode === 13 || event.key === 'Tab' || event.keyCode === 9;
    }

    /** Keystroke timing: a burst at scanner speed, not a person typing. */
    function Burst() {
        this.stamps = [];
    }
    Burst.prototype.add = function () {
        var now = Date.now();
        if (this.stamps.length && now - this.stamps[this.stamps.length - 1] > 500) this.stamps = [];
        this.stamps.push(now);
    };
    Burst.prototype.fast = function () {
        var s = this.stamps;
        return s.length >= MIN_BURST && (s[s.length - 1] - s[0]) / (s.length - 1) < FAST_GAP_MS;
    };
    Burst.prototype.reset = function () {
        this.stamps = [];
    };

    function attach(opts) {
        var capture = opts.capture;
        var status = opts.status;
        var fields = (opts.fields || []).filter(Boolean);
        var lost = false;
        var deadKeys = 0;   // keydown "Unidentified" since the last real input: a scan with nowhere to go

        function setState(state) {
            if (status) status.setAttribute('data-state', state);
        }

        function refreshState() {
            var active = document.activeElement;
            if (lost) setState('lost');
            else if (document.visibilityState === 'hidden' || openSheet() ||
                     (isTextEntry(active) && active !== capture && fields.indexOf(active) === -1)) setState('paused');
            else setState('listening');
        }

        function focusCapture() {
            if (document.activeElement === capture) return;
            try { capture.focus({ preventScroll: true }); } catch (e) { capture.focus(); }
        }

        /** Back to the capture input, straight from wherever the focus is (never via nothing). */
        function keep() {
            focusCapture();
            refreshState();
        }

        function markLost() {
            if (lost) return;
            lost = true;
            refreshState();
            feedback('bad');
        }

        function emit(code, source) {
            code = (code || '').trim();
            if (!code) return;
            var sheet = openSheet();
            if (sheet && source === 'scanner') {
                // A sheet is up: its scan field gets the code (the link sheet's search), if it has one
                var into = sheet.querySelector('[data-scan-into]');
                if (into) {
                    into.value = code;
                    if (into.form) into.form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
                    else into.dispatchEvent(new Event('input', { bubbles: true }));
                } else {
                    feedback('warn');
                }
                return;
            }
            opts.onScan(code, source);
        }

        // ---------------------------------------------- the capture input and visible fields

        function watchField(input, anySpeed) {
            var burst = new Burst();
            var timer = null;
            input.setAttribute('autocomplete', 'off');
            input.setAttribute('autocapitalize', 'off');
            input.setAttribute('spellcheck', 'false');

            function submit() {
                clearTimeout(timer);
                burst.reset();
                var code = input.value;
                input.value = '';
                if (input !== capture) keep();   // typed: put the on-screen keyboard away
                emit(code, input === capture ? 'scanner' : 'field');
            }

            input.addEventListener('keydown', function (event) {
                if (!isEnd(event)) return;
                if (input.value.trim()) {
                    event.preventDefault();
                    submit();
                } else if (input === capture && deadKeys >= MIN_BURST) {
                    markLost();   // a whole scan arrived as keys with no text
                }
                deadKeys = 0;
            });
            input.addEventListener('input', function () {
                deadKeys = 0;
                burst.add();
                clearTimeout(timer);
                if (anySpeed || burst.fast()) timer = setTimeout(submit, IDLE_SUBMIT_MS);
            });
        }

        watchField(capture, true);         // only the scanner types here
        fields.forEach(function (input) { watchField(input, false); });

        // Keys with no text reaching something that isn't a text field: nothing to type into
        document.addEventListener('keydown', function (event) {
            if (event.key !== 'Unidentified') return;
            deadKeys++;
            if (!isTextEntry(event.target) && deadKeys >= MIN_BURST) markLost();
        }, true);

        // ---------------------------------------------- holding the focus

        // A tap that isn't on a text field leaves the focus where it is
        var focusAtTap = null;
        document.addEventListener('mousedown', function (event) {
            focusAtTap = document.activeElement;
            if (!takesFocus(event.target)) event.preventDefault();
        }, true);

        // After the tap's own handlers (rscp-common.js listens first): a tap
        // away from a visible field that was being typed in ends the typing,
        // straight back to the capture input (inside a sheet, typing goes on;
        // a field the tap's handler just focused is left alone). A tap on the
        // card after the connection was lost reloads the page.
        document.addEventListener('click', function (event) {
            var t = event.target;
            if (lost) {
                if (status && status.contains(t)) location.reload();
                return;
            }
            if (takesFocus(t) || (t.closest && t.closest('a[href]'))) return;
            var active = document.activeElement;
            var sheet = openSheet();
            if (active !== capture && active === focusAtTap && isTextEntry(active) &&
                !(sheet && sheet.contains(active))) keep();
            else refreshState();
        });

        // Anything else that takes the focus without being a text field hands it straight back
        document.addEventListener('focusin', function (event) {
            if (event.target !== capture && !takesFocus(event.target)) keep();
            else refreshState();
        });
        capture.addEventListener('blur', function (event) {
            // Focus dropping to nothing while the window is active (not the screen going off)
            if (!event.relatedTarget && document.hasFocus()) keep();
        });
        document.addEventListener('focusout', function () { setTimeout(refreshState, 0); });
        document.addEventListener('visibilitychange', refreshState);
        // A sheet opening or closing: dialogs have no open event, so watch their attribute
        if (window.MutationObserver) {
            new MutationObserver(refreshState).observe(document.body, { subtree: true, attributes: true, attributeFilter: ['open'] });
        }

        // Now, while the page loads and before any tap (see the rules above)
        capture.removeAttribute('inputmode');
        focusCapture();
        setTimeout(function () { capture.setAttribute('inputmode', 'none'); }, INPUTMODE_DELAY_MS);

        var api = {
            /** Back to scanning: a focused field lets go (its keyboard goes away). */
            focus: keep,
            keep: keep
        };
        window.RSCPM = window.RSCPM || {};
        window.RSCPM.scanner = api;   // m.js sheets hand the focus back through this
        refreshState();
        if (status) status.setAttribute('data-scanner', 'ready');
        return api;
    }

    // ------------------------------------------------------------ feedback

    var audioCtx = null;

    function tone(freq, start, length, type, volume) {
        var osc = audioCtx.createOscillator();
        var gain = audioCtx.createGain();
        osc.type = type;
        osc.frequency.setValueAtTime(freq, start);
        gain.gain.setValueAtTime(volume, start);
        gain.gain.exponentialRampToValueAtTime(0.001, start + length);
        osc.connect(gain);
        gain.connect(audioCtx.destination);
        osc.start(start);
        osc.stop(start + length);
    }

    var PATTERNS = {
        // sound: [frequency, offset s, length s, waveform, volume]; vibrate: ms pattern
        ok: { sound: [[1000, 0, 0.12, 'sine', 0.15]], vibrate: [60] },
        priority: { sound: [[1000, 0, 0.1, 'sine', 0.15], [1300, 0.14, 0.14, 'sine', 0.15]], vibrate: [60, 80, 60] },
        warn: { sound: [[520, 0, 0.12, 'square', 0.12], [520, 0.18, 0.12, 'square', 0.12]], vibrate: [120, 80, 120] },
        bad: { sound: [[150, 0, 0.3, 'sawtooth', 0.25]], vibrate: [200, 100, 200] }
    };

    /** Beep and buzz for a result: 'ok', 'priority', 'warn' or 'bad'. */
    function feedback(kind) {
        var p = PATTERNS[kind] || PATTERNS.ok;
        try {
            var Ctx = window.AudioContext || window.webkitAudioContext;
            if (Ctx) {
                audioCtx = audioCtx || new Ctx();
                if (audioCtx.state === 'suspended') audioCtx.resume();
                var now = audioCtx.currentTime;
                p.sound.forEach(function (s) { tone(s[0], now + s[1], s[2], s[3], s[4]); });
            }
        } catch (e) { /* no audio: vibration still works */ }
        if (navigator.vibrate) {
            try { navigator.vibrate(p.vibrate); } catch (e) { /* ignored */ }
        }
    }

    window.RSCPScanner = { attach: attach, feedback: feedback };
})();
