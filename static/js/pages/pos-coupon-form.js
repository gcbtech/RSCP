/* Create / Edit Coupon (templates/pos/coupon_form.html). Needs Tom Select.
   Page data: couponType / discountType of the coupon being edited (the type
   selects are only on the create form). */
(function () {
    'use strict';

    const data = RSCP.pageData() || {};

    // Dynamic form visibility based on selections
    const couponType = document.getElementById('couponType');
    const discountType = document.getElementById('discountType');
    const genericWarning = document.getElementById('genericWarning');
    const codeSection = document.getElementById('codeSection');
    const bogoSettings = document.getElementById('bogoSettings');
    const itemSelection = document.getElementById('itemSelection');
    const rewardItemSection = document.getElementById('rewardItemSection');
    const orderMinSection = document.getElementById('orderMinSection');
    const discountPrefix = document.getElementById('discountPrefix');
    const discountValueSection = document.getElementById('discountValueSection');
    const discountValueLabel = document.getElementById('discountValueLabel');

    function updateFormVisibility() {
        const type = couponType ? couponType.value : (data.couponType || 'generic');
        const dType = discountType ? discountType.value : (data.discountType || 'order_percent');

        // Coupon type handling
        if (genericWarning) {
            genericWarning.style.display = type === 'generic' ? 'block' : 'none';
        }
        if (codeSection) {
            codeSection.style.display = type === 'generic' ? 'block' : 'none';
        }

        // Discount type handling
        const isItemLevel = ['item_percent', 'item_dollar', 'bogo_free', 'bogo_percent', 'bogo_cross'].includes(dType);
        const isBogo = dType.startsWith('bogo');
        const isOrderLevel = ['order_percent', 'order_dollar'].includes(dType);
        const isPercent = dType.includes('percent');
        const isDollar = dType.includes('dollar');

        // Show/hide sections
        if (itemSelection) {
            itemSelection.classList.toggle('show', isItemLevel && dType !== 'bogo_cross');
        }
        if (bogoSettings) {
            bogoSettings.classList.toggle('show', isBogo);
        }
        if (rewardItemSection) {
            rewardItemSection.classList.toggle('show', dType === 'bogo_cross');
        }
        if (orderMinSection) {
            orderMinSection.classList.toggle('show', isOrderLevel);
        }

        // Update discount value label and prefix
        if (isBogo && dType === 'bogo_free') {
            discountValueSection.style.display = 'none';
        } else {
            discountValueSection.style.display = 'block';
            if (isPercent) {
                discountPrefix.textContent = '%';
                discountValueLabel.textContent = 'Discount Percentage *';
            } else if (isDollar) {
                discountPrefix.textContent = '$';
                discountValueLabel.textContent = 'Discount Amount *';
            }
        }
    }

    // Attach listeners
    if (couponType) couponType.addEventListener('change', updateFormVisibility);
    if (discountType) discountType.addEventListener('change', updateFormVisibility);

    // Initialize on page load
    updateFormVisibility();

    // Initialize Tom Select for item selection
    if (document.getElementById('targetItems')) {
        new TomSelect('#targetItems', {
            plugins: ['remove_button'],
            maxItems: null
        });
    }
})();
