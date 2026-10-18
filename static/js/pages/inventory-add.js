/* Add / Edit Item page (templates/inventory/add.html). Needs Tom Select and the
   Bootstrap bundle; inventory-common.js applies dark mode. */
(function () {
    'use strict';

    const pageData = RSCP.pageData() || {};
    const marginEnabled = !!pageData.marginEnabled;
    const marginPercent = pageData.marginPercent;

    // Tom Select Initialization for Location Dropdowns
    var allowCustomLocations = !!pageData.allowCustomLocations;

    document.querySelectorAll('.location-select').forEach(function (el) {
        new TomSelect(el, {
            create: allowCustomLocations,
            createOnBlur: allowCustomLocations,
            placeholder: 'Select or type...',
            allowEmptyOption: true,
            sortField: { field: 'text', direction: 'asc' }
        });
    });

    // Custom category toggle
    const catSelect = document.getElementById('categorySelect');
    const customCat = document.getElementById('customCategory');

    if (catSelect) {
        catSelect.addEventListener('change', function () {
            if (this.value === 'custom') {
                customCat.style.display = 'block';
                customCat.name = 'category';
                this.name = '';
            } else {
                customCat.style.display = 'none';
                customCat.name = '';
                this.name = 'category';
            }
        });
    }

    // SKU Regeneration function (edit mode)
    function regenerateSku() {
        const category = document.getElementById('newSkuCategory').value;

        // Call server to generate a new SKU
        fetch('/inventory/generate-sku?category=' + encodeURIComponent(category))
            .then(response => response.json())
            .then(data => {
                if (data.sku) {
                    document.getElementById('currentSku').value = data.sku;
                    document.getElementById('regenerateSkuFlag').value = 'true';
                    document.getElementById('newSkuCategoryHidden').value = category;

                    // Collapse the regenerate section
                    bootstrap.Collapse.getOrCreateInstance(document.getElementById('regenerateSkuSection')).hide();

                    alert('SKU updated to: ' + data.sku + '\n\nDon\'t forget to save the item!');
                } else {
                    alert('Error generating SKU: ' + (data.error || 'Unknown error'));
                }
            })
            .catch(err => {
                alert('Error: ' + err);
            });
    }

    // Fetch image from eBay/Amazon URL
    function fetchImageFromUrl() {
        const url = document.getElementById('sourceUrl').value.trim();
        const btn = document.getElementById('fetchImageBtn');

        if (!url) {
            alert('Please enter a product URL first');
            return;
        }

        // Show loading state
        const originalText = btn.innerHTML;
        btn.innerHTML = '⏳ Fetching...';
        btn.disabled = true;

        fetch('/inventory/fetch-image-from-url', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': RSCP.csrfToken()
            },
            body: JSON.stringify({ url: url })
        })
            .then(response => {
                // Parse JSON regardless of status to get error message
                return response.json().then(data => {
                    return { ok: response.ok, status: response.status, data: data };
                });
            })
            .then(result => {
                if (result.ok && result.data.image_url) {
                    // Success - update the hidden image URL input
                    const imageUrlInput = document.getElementById('imageUrlInput');
                    if (imageUrlInput) {
                        imageUrlInput.value = result.data.image_url;
                    }

                    // Update and show the preview
                    const preview = document.getElementById('imagePreview');
                    const previewContainer = document.getElementById('imagePreviewContainer');
                    if (preview) {
                        preview.src = result.data.image_url;
                    }
                    if (previewContainer) {
                        previewContainer.style.display = 'block';
                    }

                    alert('✅ Image found and applied!\n\nDon\'t forget to save the item.');
                } else {
                    // Error - show the error message
                    const errorMsg = result.data.error || 'Could not find an image at that URL.';
                    alert('❌ ' + errorMsg + '\n\nTry a different URL or enter the image URL manually.');
                }
            })
            .catch(err => {
                alert('❌ Error fetching image: ' + err.message);
            })
            .finally(() => {
                btn.innerHTML = originalText;
                btn.disabled = false;
            });
    }

    // Check if SKU already exists (client-side validation)
    function checkSkuExists() {
        const skuInput = document.getElementById('skuInput');
        const feedback = document.getElementById('skuFeedback');

        if (!skuInput || !feedback) return;

        const sku = skuInput.value.trim();
        if (!sku) {
            feedback.style.display = 'none';
            skuInput.classList.remove('is-invalid');
            return;
        }

        fetch('/inventory/check-sku-exists?sku=' + encodeURIComponent(sku))
            .then(response => response.json())
            .then(data => {
                if (data.exists) {
                    feedback.textContent = '⚠️ This SKU already exists! Please use a different SKU.';
                    feedback.style.display = 'block';
                    skuInput.classList.add('is-invalid');
                } else {
                    feedback.textContent = '✓ SKU is available';
                    feedback.style.display = 'block';
                    feedback.classList.remove('text-danger');
                    feedback.classList.add('text-success');
                    skuInput.classList.remove('is-invalid');
                    skuInput.classList.add('is-valid');

                    // Reset after 3 seconds
                    setTimeout(() => {
                        feedback.style.display = 'none';
                        feedback.classList.add('text-danger');
                        feedback.classList.remove('text-success');
                        skuInput.classList.remove('is-valid');
                    }, 3000);
                }
            })
            .catch(err => {
                console.error('Error checking SKU:', err);
            });
    }

    // Suggested price calculation based on margin settings
    function updateSuggestedPrice() {
        if (!marginEnabled) return;

        const buyPrice = parseFloat(document.getElementById('buyPriceInput').value) || 0;
        const sellInput = document.getElementById('sellPriceInput');

        if (buyPrice > 0 && !sellInput.value) {
            const suggestedPrice = (buyPrice * (1 + marginPercent / 100)).toFixed(2);
            sellInput.placeholder = '~$' + suggestedPrice;
        } else if (buyPrice === 0) {
            sellInput.placeholder = '0.00';
        }
    }

    // Initialize on page load
    updateSuggestedPrice();

    // Camera Logic
    let stream = null;
    let currentFacingMode = 'environment'; // default to rear camera

    function openCameraModal() {
        const modal = new bootstrap.Modal(document.getElementById('cameraModal'));
        modal.show();
        startCamera();

        // Stop camera when modal is closed
        document.getElementById('cameraModal').addEventListener('hidden.bs.modal', stopCamera);
    }

    async function startCamera() {
        try {
            if (stream) stopCamera();

            stream = await navigator.mediaDevices.getUserMedia({
                video: { facingMode: currentFacingMode }
            });
            const video = document.getElementById('cameraVideo');
            video.srcObject = stream;
        } catch (err) {
            console.error("Camera error:", err);
            alert("Could not access camera: " + err.message);
        }
    }

    function stopCamera() {
        if (stream) {
            stream.getTracks().forEach(track => track.stop());
            stream = null;
        }
    }

    function switchCamera() {
        currentFacingMode = currentFacingMode === 'environment' ? 'user' : 'environment';
        startCamera();
    }

    function capturePhoto() {
        const video = document.getElementById('cameraVideo');
        const canvas = document.getElementById('cameraCanvas');

        // Set canvas dimensions to match video
        canvas.width = video.videoWidth;
        canvas.height = video.videoHeight;

        const context = canvas.getContext('2d');
        context.drawImage(video, 0, 0, canvas.width, canvas.height);

        // Get base64 data
        const imageData = canvas.toDataURL('image/jpeg', 0.8);

        // Upload
        fetch('/inventory/upload_photo', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': RSCP.csrfToken()
            },
            body: JSON.stringify({ image: imageData })
        })
            .then(res => res.json())
            .then(data => {
                if (data.url) {
                    // Update preview and hidden input
                    document.getElementById('imageUrlInput').value = data.url;

                    const preview = document.getElementById('imagePreview');
                    preview.src = data.url;
                    document.getElementById('imagePreviewContainer').style.display = 'block';

                    // Close modal
                    const modalEl = document.getElementById('cameraModal');
                    const modal = bootstrap.Modal.getInstance(modalEl);
                    modal.hide();

                    // Cleanup
                    stopCamera();
                } else {
                    alert('Upload failed: ' + data.error);
                }
            })
            .catch(err => alert('Error uploading photo: ' + err));
    }

    // Swap cover image
    function setSwapCover(imgUrl) {
        document.getElementById('swapCoverInput').value = imgUrl;
        // Find the form and submit it
        const form = document.getElementById('swapCoverInput').closest('form');
        if (form) {
            form.submit();
        }
    }

    RSCP.actions({
        checkSkuExists: function () { checkSkuExists(); },
        regenerateSku: function () { regenerateSku(); },
        openCameraModal: function () { openCameraModal(); },
        updateSuggestedPrice: function () { updateSuggestedPrice(); },
        fetchImageFromUrl: function () { fetchImageFromUrl(); },
        setSwapCover: function (el) { setSwapCover(el.dataset.img); },
        switchCamera: function () { switchCamera(); },
        capturePhoto: function () { capturePhoto(); }
    });
})();
