/* POS checkout (templates/pos/checkout.html): tender entry, change due, split payments.
   Totals come from RSCP.pageData(); controls use data-action / data-input / data-change
   (added card rows are created below and use the same delegated handlers). */
(function () {
    'use strict';

    const pageData = RSCP.pageData() || {};

    RSCP.actions({
        showPayment: function (el) { showPayment(el.dataset.type, el); },
        addTendered: function (el) { addTendered(Number(el.dataset.amount)); },
        setTenderedExact: function () { setTendered(cashTotal); },
        clearTendered: function () { clearTendered(); },
        updateChange: function () { updateChange(); },
        // #splitCash and every .card-split input; updateSplit() tells them apart by class
        updateSplit: function (el) { updateSplit(el); },
        addCard: function () { addCard(); },
        removeCard: function (el) { el.parentElement.remove(); autoDistributeSplit(); },
        // Complete Sale: block the submit when the tender doesn't cover the total
        validateCheckout: function (el, event) {
            if (!validateCheckout()) event.preventDefault();
        }
    });

    const total = pageData.total;
    const cashDiscountValue = pageData.cashDiscountValue;
    const cashTotal = total - cashDiscountValue;  // Discounted total for cash payments

    // Cash discount on the cash that actually pays the sale (not change).
    function getSplitCashDiscount(cash, cards) {
        let splitCashDiscount = 0;
        const cashDiscountEnabled = !!pageData.cashDiscountEnabled;
        const cashDiscountAmount = pageData.cashDiscountAmount;
        const cashDiscountType = pageData.cashDiscountType;

        const cashBasis = Math.min(cash, Math.max(0, total - (cards || 0)));
        if (cashDiscountEnabled && cashBasis > 0 && cashDiscountAmount > 0) {
            if (cashDiscountType === 'percent') {
                splitCashDiscount = cashBasis * (cashDiscountAmount / 100);
            } else {
                // Fixed discount proportional to the cash portion
                const cashProportion = cashBasis / total;
                splitCashDiscount = Math.min(cashDiscountAmount * cashProportion, cashBasis);
            }
            splitCashDiscount = Math.round(splitCashDiscount * 100) / 100;
        }
        return splitCashDiscount;
    }

    // One click, one sale: lock the button once the form is really
    // submitting (the server also de-duplicates by checkout_token).
    document.addEventListener('DOMContentLoaded', function () {
        document.getElementById('checkoutForm').addEventListener('submit', function () {
            const btn = document.getElementById('completeBtn');
            btn.disabled = true;
            btn.textContent = 'Processing...';
        });
    });

    function validateCheckout() {
        const paymentMethod = document.getElementById('paymentMethod').value;

        if (paymentMethod === 'cash') {
            const tendered = parseFloat(document.getElementById('tenderedAmount').value) || 0;
            if (tendered < cashTotal - 0.01) {
                alert('Insufficient cash tendered. Please enter at least $' + cashTotal.toFixed(2));
                return false;
            }
        } else if (paymentMethod === 'split') {
            const cash = parseFloat(document.getElementById('splitCash').value) || 0;
            let cards = 0;
            document.querySelectorAll('.card-split').forEach(i => {
                cards += parseFloat(i.value) || 0;
            });

            const discount = getSplitCashDiscount(cash, cards);
            const requiredTotal = total - discount;

            if (cash + cards < requiredTotal - 0.01) {
                alert('Split payment total is less than the order amount. \n\nRequired: $' + requiredTotal.toFixed(2) + ' (with discount)\nEntered: $' + (cash + cards).toFixed(2));
                return false;
            }
        }
        return true;
    }

    function showPayment(type, tab) {
        document.querySelectorAll('.payment-tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.payment-content').forEach(c => c.classList.remove('active'));

        tab.classList.add('active');
        document.getElementById(type + '-content').classList.add('active');
        document.getElementById('paymentMethod').value = type;

        // Auto-fill split amounts when switching to split tab
        if (type === 'split') {
            autoDistributeSplit();
        }
    }

    function addTendered(amount) {
        // Add the amount to current tendered (accumulate bills)
        const input = document.getElementById('tenderedAmount');
        const current = parseFloat(input.value) || 0;
        input.value = (current + amount).toFixed(2);
        updateChange();
    }

    function setTendered(amount) {
        // Set exact amount (for "Exact" button)
        document.getElementById('tenderedAmount').value = amount.toFixed(2);
        updateChange();
    }

    function clearTendered() {
        document.getElementById('tenderedAmount').value = '0.00';
        updateChange();
    }

    function updateChange() {
        const tendered = parseFloat(document.getElementById('tenderedAmount').value) || 0;
        const change = tendered - cashTotal;  // Use discounted total for cash

        // Show negative if still owed, positive if change due
        if (change >= 0) {
            document.getElementById('changeAmount').textContent = '$' + change.toFixed(2);
        } else {
            document.getElementById('changeAmount').textContent = '-$' + Math.abs(change).toFixed(2);
        }

        const display = document.getElementById('changeDisplay');
        if (change >= 0) {
            display.style.background = '#f0fdf4';
            display.style.borderColor = '#16a34a';
        } else {
            display.style.background = '#fef2f2';
            display.style.borderColor = '#dc2626';
        }
    }

    // Auto-distribute remaining balance across card inputs
    function autoDistributeSplit() {
        const cash = parseFloat(document.getElementById('splitCash').value) || 0;
        const splitCashDiscount = getSplitCashDiscount(cash);
        const adjustedTotal = total - splitCashDiscount;
        const remaining = Math.max(0, adjustedTotal - cash);

        const cards = document.querySelectorAll('.card-split');
        if (cards.length === 0) return;

        const perCard = Math.floor(remaining / cards.length * 100) / 100;
        const lastCardAmount = Math.round((remaining - perCard * (cards.length - 1)) * 100) / 100;

        cards.forEach((card, i) => {
            if (i === cards.length - 1) {
                card.value = lastCardAmount.toFixed(2);
            } else {
                card.value = perCard.toFixed(2);
            }
        });

        updateSplitDisplay();
    }

    // Track which card the user is manually editing
    let manuallyEditedCard = null;

    function updateSplit(editedInput) {
        // If user is manually editing a card, adjust other cards
        if (editedInput && editedInput.classList.contains('card-split')) {
            manuallyEditedCard = editedInput;
            adjustOtherCards(editedInput);
        } else {
            // Cash changed — redistribute across all cards
            autoDistributeSplit();
        }
        updateSplitDisplay();
    }

    function adjustOtherCards(editedInput) {
        const cash = parseFloat(document.getElementById('splitCash').value) || 0;
        const splitCashDiscount = getSplitCashDiscount(cash);
        const adjustedTotal = total - splitCashDiscount;
        const remaining = Math.max(0, adjustedTotal - cash);

        const cards = document.querySelectorAll('.card-split');
        const editedValue = parseFloat(editedInput.value) || 0;

        if (cards.length === 1) {
            // Only one card — user is manually overriding, just update display
            return;
        }

        // Sum all manually-set cards (the one being edited)
        let otherCards = [];
        let editedTotal = 0;
        cards.forEach(card => {
            if (card === editedInput) {
                editedTotal += editedValue;
            } else {
                otherCards.push(card);
            }
        });

        // Distribute remainder across other cards
        const remainingForOthers = Math.max(0, remaining - editedTotal);
        if (otherCards.length === 1) {
            otherCards[0].value = remainingForOthers.toFixed(2);
        } else if (otherCards.length > 1) {
            const perCard = Math.floor(remainingForOthers / otherCards.length * 100) / 100;
            const lastAmount = Math.round((remainingForOthers - perCard * (otherCards.length - 1)) * 100) / 100;
            otherCards.forEach((card, i) => {
                if (i === otherCards.length - 1) {
                    card.value = lastAmount.toFixed(2);
                } else {
                    card.value = perCard.toFixed(2);
                }
            });
        }
    }

    function updateSplitDisplay() {
        const cash = parseFloat(document.getElementById('splitCash').value) || 0;
        let cards = 0;
        document.querySelectorAll('.card-split').forEach(i => {
            cards += parseFloat(i.value) || 0;
        });

        const splitCashDiscount = getSplitCashDiscount(cash, cards);
        const adjustedTotal = total - splitCashDiscount;
        const remaining = adjustedTotal - cash - cards;

        // Update display
        let displayText = '$' + remaining.toFixed(2);
        if (splitCashDiscount > 0) {
            displayText += ' (Cash discount: -$' + splitCashDiscount.toFixed(2) + ')';
        }
        document.getElementById('remainingAmount').innerHTML = displayText;

        const display = document.getElementById('splitRemaining');
        if (remaining <= 0.01) {
            display.style.background = '#f0fdf4';
            display.style.borderColor = '#16a34a';
        } else {
            display.style.background = '#fef3c7';
            display.style.borderColor = '#f59e0b';
        }
    }

    function addCard() {
        const count = document.querySelectorAll('.card-split').length + 1;
        const row = document.createElement('div');
        row.className = 'split-row';
        row.innerHTML = `
            <label style="width: 80px;">💳 Card ${count}:</label>
            <input type="number" name="card_amount" class="card-split" step="0.01" min="0" 
                   value="0" data-input="updateSplit">
            <button type="button" class="btn btn-sm btn-outline-danger" data-action="removeCard">×</button>
        `;
        document.getElementById('cardSplits').appendChild(row);
        // Redistribute evenly with the new card
        autoDistributeSplit();
    }

    // Initialize
    updateChange();
})();
