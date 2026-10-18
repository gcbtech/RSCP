/* templates/admin.html (+ rows from templates/_admin_table_rows.html) */
(function () {
    'use strict';

    // Server-provided values (see block page_data in admin.html)
    const ADMIN_DATA = RSCP.pageData();

    // Analytics Chart
    const ctx = document.getElementById('analyticsChart').getContext('2d');
    const rawData = ADMIN_DATA.graphData;
    const labels = rawData.map(d => d.date);
    const dataPoints = rawData.map(d => d.count);

    new Chart(ctx, {
        type: 'bar',
        data: {
            labels: labels,
            datasets: [{
                label: 'Packages Scanned',
                data: dataPoints,
                backgroundColor: 'rgba(52, 152, 219, 0.6)',
                borderColor: 'rgba(52, 152, 219, 1)',
                borderWidth: 1,
                borderRadius: 4
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: false } },
            scales: {
                y: { beginAtZero: true, grid: { color: '#f0f0f0' } },
                x: { grid: { display: false } }
            }
        }
    });

    // Live Update
    // Live Update
    function refreshPackages(signal) {
        const url = new URL(window.location.href);
        url.searchParams.set('partial', '1');
        return fetch(url, { signal })
            .then(r => r.ok ? r.text() : Promise.reject('Network error'))
            .then(html => {
                const tbody = document.getElementById('packagesBody');
                if (!tbody) return;

                const checked = tbody.querySelectorAll('input[type="checkbox"]:checked');

                // Check if user is editing a date (pencil icon clicked), sku, or has focus
                const isEditing = tbody.querySelector('.date-editor:not(.d-none)') || tbody.querySelector('.sku-editor:not(.d-none)');
                const hasFocus = tbody.contains(document.activeElement);

                // Only refresh if no items checked AND not editing
                if (checked.length === 0 && !isEditing && !hasFocus) {
                    tbody.innerHTML = html;
                }
            })
            .catch(err => console.error('Live update failed:', err));
    }

    rscpPoll(refreshPackages, 5000, { delayFirst: true });

    // Helpers
    function showImage(src) {
        document.getElementById('previewImage').src = src;
        new bootstrap.Modal(document.getElementById('imageModal')).show();
    }

    function toggleAll(source) {
        document.querySelectorAll('input[name="trackings"]').forEach(cb => cb.checked = source.checked);
    }

    function submitBulk(action) {
        const msgs = {
            'delete': 'DELETE selected items? This cannot be undone.',
            'mark_received': 'Mark selected as RECEIVED?',
            'unreceive': 'Un-receive selected items?',
            'mark_late': 'Mark selected as LATE?'
        };
        if (confirm(msgs[action] || 'Proceed with action?')) {
            document.getElementById('actionType').value = action;
            document.getElementById('bulkForm').submit();
        }
    }

    function toggleDateEdit(tracking) {
        document.getElementById('dateDisplay_' + tracking)?.classList.toggle('d-none');
        document.getElementById('dateEditor_' + tracking)?.classList.toggle('d-none');
    }

    function toggleSkuEdit(id) {
        const display = document.getElementById('skuDisplay_' + id);
        const editor = document.getElementById('skuEditor_' + id);
        if (display && editor) {
            display.classList.toggle('d-none');
            editor.classList.toggle('d-none');
        }
    }

    function toggleQtyEdit(id) {
        const display = document.getElementById('qtyDisplay_' + id);
        const editor = document.getElementById('qtyEditor_' + id);
        if (display && editor) {
            display.classList.toggle('d-none');
            editor.classList.toggle('d-none');
        }
    }

    function submitQty(id) {
        const input = document.getElementById('qtyInput_' + id);
        const val = input.value.trim();

        fetch('/admin/set_quantity/' + id, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/x-www-form-urlencoded',
                'X-CSRFToken': ADMIN_DATA.csrfToken
            },
            body: 'quantity=' + encodeURIComponent(val)
        })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    // Optimistic Update
                    const displayDiv = document.getElementById('qtyDisplay_' + id);
                    const displaySpan = displayDiv.querySelector('span');

                    displaySpan.innerHTML = `<code>${escapeHtml(val || 1)}</code>`;

                    // Close editor
                    toggleQtyEdit(id);

                    // Background refresh
                    refreshPackages();
                } else {
                    alert('Error: ' + (data.error || 'Unknown'));
                }
            })
            .catch(e => console.error(e));
    }

    function submitSku(id) {
        const input = document.getElementById('skuInput_' + id);
        const val = input.value.trim();

        fetch('/admin/set_sku/' + id, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/x-www-form-urlencoded',
                'X-CSRFToken': ADMIN_DATA.csrfToken
            },
            body: 'sku=' + encodeURIComponent(val)
        })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    // Optimistic Update
                    const displayDiv = document.getElementById('skuDisplay_' + id);
                    const displaySpan = displayDiv.querySelector('span');

                    if (val) {
                        displaySpan.innerHTML = `<code>${escapeHtml(val)}</code>`;
                    } else {
                        displaySpan.innerHTML = '<small class="text-muted">Set SKU</small>';
                    }

                    // Close editor
                    toggleSkuEdit(id);

                    // Background refresh
                    refreshPackages();
                } else {
                    alert('Error: ' + (data.error || 'Unknown'));
                }
            })
            .catch(e => console.error(e));
    }

    function toggleTrackingEdit(id) {
        const display = document.getElementById('trackingDisplay_' + id);
        const editor = document.getElementById('trackingEditor_' + id);
        if (display && editor) {
            display.classList.toggle('d-none');
            editor.classList.toggle('d-none');
        }
    }

    function submitTracking(id) {
        const input = document.getElementById('trackingInput_' + id);
        const val = input.value.trim();
        if (!val) return alert("Tracking number cannot be empty");

        fetch('/admin/set_tracking/' + id, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/x-www-form-urlencoded',
                'X-CSRFToken': ADMIN_DATA.csrfToken
            },
            body: 'tracking=' + encodeURIComponent(val)
        })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    // Optimistic Update
                    const displayDiv = document.getElementById('trackingDisplay_' + id);
                    const textDiv = displayDiv.querySelector('div');
                    textDiv.innerHTML = `${escapeHtml(val)} <small class="text-muted ms-1">✏️</small>`;

                    toggleTrackingEdit(id);
                    refreshPackages();
                } else {
                    alert('Error: ' + (data.error || 'Unknown'));
                }
            })
            .catch(e => console.error(e));
    }

    function submitDate(tracking) {
        const input = document.getElementById('date_' + tracking);
        if (!input?.value) return alert('Please select a date.');

        const form = document.createElement('form');
        form.method = 'POST';
        form.action = '/admin/set_date/' + tracking;

        const date = document.createElement('input');
        date.type = 'hidden';
        date.name = 'new_date';
        date.value = input.value;
        form.appendChild(date);

        const csrf = document.createElement('input');
        csrf.type = 'hidden';
        csrf.name = 'csrf_token';
        csrf.value = document.querySelector('input[name="csrf_token"]').value;
        form.appendChild(csrf);

        document.body.appendChild(form);
        form.submit();
    }

    function checkForUpdates() {
        const status = document.getElementById('updateStatus');
        const updateOptions = document.getElementById('updateOptions');
        const stableUpdate = document.getElementById('stableUpdate');
        const betaUpdate = document.getElementById('betaUpdate');

        status.style.display = 'block';
        status.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Checking...';
        updateOptions.style.display = 'none';
        stableUpdate.style.display = 'none';
        betaUpdate.style.display = 'none';

        fetch('/admin/check_update')
            .then(r => r.json())
            .then(data => {
                if (data.error) {
                    status.innerHTML = `<span class="text-danger">Error: ${escapeHtml(data.error)}</span>`;
                    return;
                }

                status.innerHTML = `Current: <strong>${escapeHtml(data.current_version)}</strong>`;
                updateOptions.style.display = 'block';

                // Show stable branch if available
                if (data.branches && data.branches.stable) {
                    const stable = data.branches.stable;
                    document.getElementById('stableVersion').textContent = `v${stable.version}`;
                    stableUpdate.style.display = 'block';

                    // Highlight if update is needed
                    if (stable.update_available) {
                        stableUpdate.classList.add('border-success');
                    }
                }

                // Show beta branch if available
                if (data.branches && data.branches.beta) {
                    const beta = data.branches.beta;
                    document.getElementById('betaVersion').textContent = `v${beta.version}`;
                    betaUpdate.style.display = 'block';
                }
            })
            .catch(err => status.innerHTML = `<span class="text-danger">Error: ${escapeHtml(err.message)}</span>`);
    }

    // --- Rollback (atomic-release installs) ---
    function loadReleaseInfo() {
        const section = document.getElementById('rollbackSection');
        if (!section) return;
        fetch('/admin/release_info')
            .then(r => r.json())
            .then(data => {
                // Only offered on the atomic-release layout with a prior release
                const prev = (data.layout === 'release' && Array.isArray(data.releases))
                    ? data.releases.find(rel => !rel.active) : null;
                if (prev) {
                    document.getElementById('rollbackTarget').textContent = prev.name;
                    section.style.display = 'block';
                } else {
                    section.style.display = 'none';
                }
            })
            .catch(() => { section.style.display = 'none'; });
    }

    function startRollback() {
        if (!confirm('Roll back to the previous release and restart?\n\nThis reverts application CODE only. Your database is shared and will NOT be reverted.')) return;
        showUpdateModal();
        document.getElementById('updateModalTitle').textContent = 'Rolling Back';
        document.getElementById('updateModalSubtitle').textContent = 'Reverting to the previous release...';
        document.getElementById('updateModalStatusBox').innerHTML = '[info] Requesting rollback...\n';

        const formData = new FormData();
        formData.append('csrf_token', document.querySelector('input[name="csrf_token"]').value);

        fetch('/admin/rollback', { method: 'POST', body: formData })
            .then(r => r.ok ? r.json() : r.json().then(d => { throw new Error(d.error || 'Server error'); }))
            .then(data => {
                if (data.error) {
                    showUpdateError(data.error);
                } else {
                    appendStatusLog('[info] ' + (data.message || 'Rollback initiated.') + ' Restarting...');
                    updatePollInterval = setInterval(pollUpdateStatus, 1500);
                }
            })
            .catch(err => showUpdateError('Failed to roll back: ' + err.message));
    }

    document.addEventListener('DOMContentLoaded', loadReleaseInfo);

    let updatePollInterval = null;
    let hasEnteredRestartingState = false;
    let reconnectAttempts = 0;
    let lastLoggedProgress = '';

    function showUpdateModal() {
        const modalEl = document.getElementById('updateProgressModal');
        const modal = new bootstrap.Modal(modalEl, {
            backdrop: 'static',
            keyboard: false
        });
        
        // Reset UI states
        document.getElementById('updateModalTitle').textContent = 'System Update in Progress';
        document.getElementById('updateModalSubtitle').textContent = 'Preparing system and checking update package...';
        
        const progressBar = document.getElementById('updateProgressBar');
        progressBar.style.width = '10%';
        progressBar.className = 'progress-bar progress-bar-striped progress-bar-animated bg-primary';
        progressBar.setAttribute('aria-valuenow', '10');
        
        document.getElementById('updateModalStatusBox').innerHTML = '[info] Requesting update...\n';
        document.getElementById('updateModalStatusBox').scrollTop = 0;
        document.getElementById('updateModalWarning').textContent = '⚠️ Do not refresh, close this page, or turn off the host server!';
        document.getElementById('updateModalWarning').className = 'small text-danger fw-semibold mb-0';
        document.getElementById('updateModalCloseBtn').style.display = 'none';
        
        hasEnteredRestartingState = false;
        reconnectAttempts = 0;
        lastLoggedProgress = '';
        
        modal.show();
    }

    function appendStatusLog(message) {
        const box = document.getElementById('updateModalStatusBox');
        const isAtBottom = box.scrollHeight - box.clientHeight <= box.scrollTop + 20;
        box.innerHTML += escapeHtml(message) + '\n';
        if (isAtBottom) {
            box.scrollTop = box.scrollHeight;
        }
    }

    function startUpdate(branch) {
        let confirmationText = branch === 'beta' 
            ? '⚠️ WARNING: Beta versions may contain bugs and unstable features.\n\nAre you sure you want to update to the Beta branch?'
            : 'Are you sure you want to update to the Stable branch?';
            
        if (!confirm(confirmationText)) return;
        
        showUpdateModal();
        
        const formData = new FormData();
        formData.append('branch', branch);
        formData.append('csrf_token', document.querySelector('input[name="csrf_token"]').value);
        
        fetch('/admin/update', {
            method: 'POST',
            body: formData
        })
        .then(r => {
            if (!r.ok) {
                return r.json().then(data => { throw new Error(data.error || 'Server error'); });
            }
            return r.json();
        })
        .then(data => {
            if (data.error) {
                showUpdateError(data.error);
            } else {
                appendStatusLog('[info] Update task scheduled. Polling status...');
                // Start polling status
                updatePollInterval = setInterval(pollUpdateStatus, 1500);
            }
        })
        .catch(err => {
            showUpdateError('Failed to initiate update: ' + err.message);
        });
    }

    function showUpdateError(errorMsg) {
        if (updatePollInterval) {
            clearInterval(updatePollInterval);
            updatePollInterval = null;
        }
        
        document.getElementById('updateModalTitle').textContent = 'Update Failed';
        document.getElementById('updateModalSubtitle').textContent = 'An error occurred during the update process.';
        
        const progressBar = document.getElementById('updateProgressBar');
        progressBar.style.width = '100%';
        progressBar.className = 'progress-bar bg-danger';
        progressBar.setAttribute('aria-valuenow', '100');
        
        appendStatusLog(`[error] ${errorMsg}`);
        
        document.getElementById('updateModalWarning').textContent = '❌ Update aborted. You can close this modal and try again.';
        document.getElementById('updateModalWarning').className = 'small text-danger fw-semibold mb-0';
        document.getElementById('updateModalCloseBtn').style.display = 'block';
    }

    function pollUpdateStatus() {
        fetch('/admin/update_status')
        .then(r => {
            if (!r.ok) {
                throw new Error(`HTTP ${r.status}`);
            }
            return r.json();
        })
        .then(data => {
            if (data.status === 'updating') {
                document.getElementById('updateModalSubtitle').textContent = data.progress || 'Applying updates...';
                
                // Increment progress bar based on status/log keywords for a nice visual progression
                let progress = 10;
                const logText = data.progress || '';
                if (logText.includes('Downloading')) {
                    progress = 30;
                } else if (logText.includes('Extracting')) {
                    progress = 50;
                } else if (logText.includes('Copying')) {
                    progress = 70;
                } else if (logText.includes('dependencies') || logText.includes('pip')) {
                    progress = 85;
                }
                
                document.getElementById('updateProgressBar').style.width = `${progress}%`;
                document.getElementById('updateProgressBar').setAttribute('aria-valuenow', progress);
                
                if (logText && logText !== lastLoggedProgress) {
                    appendStatusLog(`[progress] ${logText}`);
                    lastLoggedProgress = logText;
                }
                
            } else if (data.status === 'restarting') {
                hasEnteredRestartingState = true;
                document.getElementById('updateModalTitle').textContent = 'Restarting Services...';
                document.getElementById('updateModalSubtitle').textContent = 'Applying updates and restarting RSCP...';
                
                const progressBar = document.getElementById('updateProgressBar');
                progressBar.style.width = '100%';
                progressBar.className = 'progress-bar progress-bar-striped progress-bar-animated bg-success';
                progressBar.setAttribute('aria-valuenow', '100');
                
                if (data.progress && data.progress !== lastLoggedProgress) {
                    appendStatusLog(`[info] ${data.progress}`);
                    lastLoggedProgress = data.progress;
                }
                
                document.getElementById('updateModalWarning').textContent = '⌛ Waiting for server to boot up. Please do not close...';
                document.getElementById('updateModalWarning').className = 'small text-success fw-semibold mb-0';
                
            } else if (data.status === 'idle') {
                if (hasEnteredRestartingState) {
                    if (updatePollInterval) {
                        clearInterval(updatePollInterval);
                        updatePollInterval = null;
                    }
                    appendStatusLog('[info] Server is online! Update completed successfully.');
                    document.getElementById('updateModalSubtitle').textContent = 'Update successful! Reloading...';
                    
                    setTimeout(() => {
                        window.location.reload();
                    }, 1500);
                }
            } else if (data.status === 'success') {
                // 3.0 installs: the root updater confirmed /healthz on the new release
                if (updatePollInterval) {
                    clearInterval(updatePollInterval);
                    updatePollInterval = null;
                }
                appendStatusLog('[info] ' + (data.progress || 'Update completed successfully.'));
                document.getElementById('updateModalSubtitle').textContent = 'Done! Reloading...';
                setTimeout(() => { window.location.reload(); }, 1500);
            } else if (data.status === 'rolled_back') {
                showUpdateError(data.error || 'The new version failed its health check and was rolled back.');
            } else if (data.status === 'error') {
                showUpdateError(data.error || 'Unknown error occurred.');
            }
        })
        .catch(err => {
            // If we are in restarting state, connection failures are expected!
            if (hasEnteredRestartingState) {
                reconnectAttempts++;
                if (reconnectAttempts % 3 === 0) {
                    appendStatusLog(`[info] Reconnecting to server (attempt ${reconnectAttempts})...`);
                }
            } else {
                // If we got a connection error BEFORE restarting state, it could be a network glitch, let's log it
                appendStatusLog(`[warning] Connection error: ${err.message}. Retrying...`);
            }
        });
    }

    // Save auto-confirm import setting
    function saveAutoConfirmSetting(enabled) {
        fetch(ADMIN_DATA.settingsSaveUrl, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': document.querySelector('input[name="csrf_token"]')?.value || ''
            },
            body: JSON.stringify({ group: 'general', values: { AUTO_CONFIRM_IMPORT: !!enabled } })
        })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    window.showToast && window.showToast('Auto-confirm setting saved', 'success');
                }
            })
            .catch(err => console.error('Failed to save setting:', err));
    }

    // Auto-dismiss flash messages
    setTimeout(() => {
        document.querySelector('.alert-toast')?.remove();
    }, 5000);
    // Package thumbnails: swap in a placeholder once if the image fails to load
    // (error events don't bubble, so listen in the capture phase; images that
    // already failed before this script ran are fixed up below).
    function applyImageFallback(img) {
        const fallback = img.getAttribute('data-fallback-src');
        if (!fallback) return;
        img.removeAttribute('data-fallback-src');  // only once, like `this.onerror = null`
        img.src = fallback;
        img.style.border = '1px solid red';
    }
    document.addEventListener('error', function (e) {
        const img = e.target;
        if (img && img.tagName === 'IMG' && img.hasAttribute('data-fallback-src')) applyImageFallback(img);
    }, true);
    document.querySelectorAll('img[data-fallback-src]').forEach(img => {
        if (img.complete && img.naturalWidth === 0 && img.getAttribute('src')) applyImageFallback(img);
    });

    // Markup handlers (data-action / data-change / data-submit)
    RSCP.actions({
        submitForm: function (el) { el.form.submit(); },
        submitBulk: function (el) { submitBulk(el.dataset.bulk); },
        toggleAll: function (el) { toggleAll(el); },
        saveAutoConfirmSetting: function (el) { saveAutoConfirmSetting(el.checked); },
        checkForUpdates: function () { checkForUpdates(); },
        startUpdate: function (el, event) {
            event.preventDefault();
            startUpdate(el.dataset.branch);
        },
        startRollback: function () { startRollback(); },
        showImage: function (el, event) {
            event.preventDefault();
            showImage(el.dataset.src);
        },
        submitDate: function (el) { submitDate(el.dataset.tracking); },
        toggleDateEdit: function (el) { toggleDateEdit(el.dataset.tracking); },
        toggleQtyEdit: function (el) { toggleQtyEdit(el.dataset.id); },
        submitQty: function (el) { submitQty(el.dataset.id); },
        toggleSkuEdit: function (el) { toggleSkuEdit(el.dataset.id); },
        submitSku: function (el) { submitSku(el.dataset.id); },
        toggleTrackingEdit: function (el) { toggleTrackingEdit(el.dataset.id); },
        submitTracking: function (el) { submitTracking(el.dataset.id); }
    });
})();
