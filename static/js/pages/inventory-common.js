/*
 * Shared inventory page helpers. Load this in the scripts block, before the page script.
 *
 * - Applies dark mode (localStorage 'rscp_darkmode') when loaded.
 * - Actions:
 *     data-action="showImage" data-url="..." data-title="..."  image modal (#imageModal)
 *     data-action="copyToClipboard" data-text="..."            copy + "Copied!" tooltip
 */
(function () {
    'use strict';

    // Dark mode
    if (localStorage.getItem('rscp_darkmode') === 'true') {
        document.body.classList.add('dark-mode');
    }

    // Image modal
    function showImage(url, title) {
        document.getElementById('modalImage').src = url;
        document.getElementById('imageModalLabel').textContent = title || 'Image';
        new bootstrap.Modal(document.getElementById('imageModal')).show();
    }

    function copyToClipboard(text, element) {
        navigator.clipboard.writeText(text).then(function () {
            const originalTitle = element.getAttribute('title');

            // Visual feedback
            const tooltip = new bootstrap.Tooltip(element, {
                title: "Copied!",
                trigger: 'manual'
            });
            tooltip.show();

            setTimeout(() => {
                tooltip.dispose();
                element.setAttribute('title', originalTitle); // Restore original title
            }, 1000);
        }, function (err) {
            console.error('Could not copy text: ', err);
        });
    }

    window.RSCPInventory = { showImage: showImage, copyToClipboard: copyToClipboard };

    RSCP.actions({
        showImage: function (el, event) {
            event.preventDefault();
            showImage(el.dataset.url, el.dataset.title);
        },
        copyToClipboard: function (el) {
            copyToClipboard(el.dataset.text, el);
        }
    });
})();
