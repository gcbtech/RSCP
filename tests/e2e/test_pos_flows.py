"""End-to-end POS flows in Chromium as a real cashier / manager: ring up,
discount, cash / split / card checkout, receipt, manager refund, and a
paired handheld scanner ringing into an open register.

Pages are driven by stable element ids / form field names (#skuInput,
#posCartContainer, #tenderedAmount, #changeAmount, #completeBtn,
input[name=...]) and the money is checked both on screen and in the
database (orders, payments ledger, stock)."""
import re

import pytest

from tests.e2e import helpers

expect = pytest.importorskip('playwright.sync_api').expect

TAX = 0.07


@pytest.fixture(scope='module', autouse=True)
def pos_settings(e2e_server):
    helpers.seed_settings(e2e_server, TAX_RATE=TAX, CASH_DISCOUNT_ENABLED=False,
                          AUTO_PRINT_RECEIPT=False, CASH_DRAWER_ENABLED=False)


@pytest.fixture
def new_page(browser, e2e_server):
    """Factory for extra pages in their own browser context (own cookies and
    localStorage: a second device)."""
    contexts = []

    def make():
        context = browser.new_context(base_url=e2e_server.base_url, viewport={'width': 1280, 'height': 900})
        context.set_default_timeout(15000)
        contexts.append(context)
        pg = context.new_page()
        pg.watcher = helpers.PageWatcher(pg, e2e_server.base_url)
        pg.on('dialog', lambda d: d.accept())
        return pg

    yield make
    for c in contexts:
        c.close()


# ---------------------------------------------------------------------------
# Page steps
# ---------------------------------------------------------------------------

def open_register(page):
    """The sales page, after it has announced its register (form posts then
    resolve to the same register cart as the page's AJAX calls)."""
    with page.expect_response(lambda r: '/pos/api/register/hello' in r.url):
        page.goto('/pos/')
    page.locator('#skuInput').wait_for()


def cart(page):
    return page.locator('#posCartContainer')


def ring_up(page, sku, name):
    """Scan / type a SKU into the sales input and wait for the cart line."""
    page.fill('#skuInput', sku)
    page.press('#skuInput', 'Enter')
    expect(cart(page).locator('.cart-item', has_text=name)).to_be_visible()


def apply_order_discount(page, amount, kind='percent', reason='E2E'):
    page.locator('#posCartContainer button[data-bs-target="#discountModal"]').click()
    modal = page.locator('#discountModal')
    modal.locator('input[name="discount_amount"]').wait_for(state='visible')
    modal.locator('input[name="discount_amount"]').fill(str(amount))
    modal.locator('select[name="discount_type"]').select_option(kind)
    modal.locator('input[name="discount_reason"]').fill(reason)
    with page.expect_navigation():
        modal.locator('button[type="submit"]').click()
    page.locator('#skuInput').wait_for()


def go_to_checkout(page):
    with page.expect_navigation(url=re.compile(r'/pos/checkout$')):
        cart(page).locator('a[href$="/pos/checkout"]').click()
    page.locator('#checkoutForm').wait_for()


def choose_payment(page, label):
    page.locator('.payment-tab', has_text=label).click()


def complete_sale(page):
    """Press Complete Sale; returns the order number from the receipt URL."""
    with page.expect_navigation(url=re.compile(r'/pos/receipt/POS-')):
        page.click('#completeBtn')
    return re.search(r'/pos/receipt/(POS-[0-9-]+)', page.url).group(1)


def expect_receipt(page, order_number, total):
    """The receipt page for this sale, showing its number and total."""
    expect(page.locator('.order-number')).to_have_text(order_number)
    expect(page.locator('.receipt-row.total')).to_contain_text(total)


# ---------------------------------------------------------------------------
# Flows
# ---------------------------------------------------------------------------

def test_cash_sale_with_order_discount_shows_change_and_receipt(cashier_page, e2e_server):
    page = cashier_page
    mug, mug_sku = helpers.seed_pos_item(e2e_server, 'E2E Cash Mug', 12.00, quantity=10)
    pen, pen_sku = helpers.seed_pos_item(e2e_server, 'E2E Cash Pen', 3.00, quantity=10)

    open_register(page)
    ring_up(page, mug_sku, 'E2E Cash Mug')
    ring_up(page, pen_sku, 'E2E Cash Pen')
    expect(cart(page)).to_contain_text('$15.00')                 # subtotal

    # Over the cashier's limit (10%): refused, nothing applied
    apply_order_discount(page, 50)
    expect(page.locator('body')).to_contain_text('Manager approval required')

    # Within the limit
    apply_order_discount(page, 10)
    expect(page.locator('body')).to_contain_text('Order discount applied.')
    # $15.00 - 10% = $13.50; tax 7% = $0.945 -> $0.95; total $14.45
    expect(cart(page)).to_contain_text('-$1.50')
    expect(cart(page)).to_contain_text('Checkout ($14.45)')

    go_to_checkout(page)
    choose_payment(page, 'Cash')
    page.fill('#tenderedAmount', '20')
    expect(page.locator('#changeAmount')).to_have_text('$5.55')

    order_number = complete_sale(page)
    expect_receipt(page, order_number, '$14.45')
    tendered = page.locator('.receipt-row', has_text='Cash Tendered')
    expect(tendered).to_contain_text('$20.00')
    expect(page.locator('.receipt-row', has_text='Change')).to_contain_text('$5.55')

    order, payments = helpers.order(e2e_server, order_number)
    assert (order['subtotal'], order['discount_amount'], order['tax_amount'], order['total']) == \
        (15.0, 1.5, 0.95, 14.45)
    assert payments == [('cash', 14.45, 20.0, 5.55)]
    assert helpers.item_quantity(e2e_server, mug) == 9
    assert helpers.item_quantity(e2e_server, pen) == 9
    page.watcher.assert_clean()


def test_split_payment(cashier_page, e2e_server):
    page = cashier_page
    item, sku = helpers.seed_pos_item(e2e_server, 'E2E Split Kettle', 30.00, quantity=4)

    open_register(page)
    ring_up(page, sku, 'E2E Split Kettle')
    go_to_checkout(page)                               # $30.00 + 7% = $32.10
    choose_payment(page, 'Split')
    page.fill('#splitCash', '10')
    # The card share is filled in with what is left
    expect(page.locator('input.card-split').first).to_have_value('22.10')

    order_number = complete_sale(page)
    expect_receipt(page, order_number, '$32.10')

    order, payments = helpers.order(e2e_server, order_number)
    assert order['payment_method'] == 'split' and order['total'] == 32.10
    assert payments == [('card', 22.10, None, 0.0), ('cash', 10.0, 10.0, 0.0)]
    assert helpers.item_quantity(e2e_server, item) == 3
    page.watcher.assert_clean()


def test_card_sale_then_manager_refunds_one_item(page, e2e_server):
    lamp, lamp_sku = helpers.seed_pos_item(e2e_server, 'E2E Card Lamp', 25.00, quantity=5)
    bulb, bulb_sku = helpers.seed_pos_item(e2e_server, 'E2E Card Bulb', 5.00, quantity=5)

    # Cashier rings up a card sale ($30.00 + 7% = $32.10)
    helpers.login(page, 'cashier')
    open_register(page)
    ring_up(page, lamp_sku, 'E2E Card Lamp')
    ring_up(page, bulb_sku, 'E2E Card Bulb')
    go_to_checkout(page)                              # card is the default tender
    order_number = complete_sale(page)
    expect_receipt(page, order_number, '$32.10')
    _order, payments = helpers.order(e2e_server, order_number)
    assert payments == [('card', 32.10, None, 0.0)]
    assert helpers.item_quantity(e2e_server, bulb) == 4

    # Manager finds the sale on the refunds page and returns the bulb
    helpers.logout(page)
    helpers.login(page, 'manager')
    page.goto('/pos/refunds')
    with page.expect_navigation(url=re.compile(r'/pos/refunds/order/')):
        page.locator(f'a[href$="/pos/refunds/order/{order_number}"]').click()

    form = page.locator('#refundForm')
    row = form.locator('.item-row', has_text='E2E Card Bulb')
    row.locator('input[name="item_id"]').check()
    row.locator('input[name^="quantity_"]').fill('1')
    row.locator('select[name^="restock_"]').select_option('restock')
    form.locator('select[name="reason"]').select_option('customer_changed_mind')
    expect(form.locator('select[name="refund_method"]')).to_have_value('card')
    with page.expect_navigation():
        form.locator('button[name="refund_type"][value="partial"]').click()

    # Paid share of a $5.00 line on a $32.10 / $30.00 sale: $5.35
    # (this page shows no flash; the refund history and item rows do)
    body = page.locator('body')
    expect(body).to_contain_text('Refund History')
    expect(body).to_contain_text('-$5.35')
    expect(body).to_contain_text('Paid in card')
    expect(body).to_contain_text('incl. $0.35 tax')
    expect(form.locator('.item-row', has_text='E2E Card Bulb')).to_contain_text('(1 refunded)')
    assert helpers.item_quantity(e2e_server, bulb) == 5     # back on the shelf
    assert helpers.item_quantity(e2e_server, lamp) == 4
    page.watcher.assert_clean()


def test_paired_scanner_rings_into_open_register(cashier_page, new_page, e2e_server):
    register = cashier_page
    item, sku = helpers.seed_pos_item(e2e_server, 'E2E Scanner Cable', 8.00, quantity=6)

    # The handheld (its own device: own cookies + localStorage) shows a code
    scanner = new_page()
    helpers.login(scanner, 'cashier')
    scanner.goto('/pos/scan')
    expect(scanner.locator('#pairingCode')).to_have_text(re.compile(r'^\d{3} \d{3}$'))
    code = scanner.locator('#pairingCode').inner_text().replace(' ', '')

    # The register claims it from its pairing menu
    open_register(register)
    register.click('#persistentDisplayBadge')
    pair_input = register.locator('#posDisplayPairCode')
    pair_input.wait_for(state='visible')
    pair_input.fill(code)
    register.locator('#pairingMenuModal').get_by_role('button', name='Pair', exact=True).click()
    expect(register.locator('#persistentPairSuccess')).to_be_visible()
    # Close the menu: the register does not refresh its cart under an open modal
    register.keyboard.press('Escape')
    expect(register.locator('#pairingMenuModal')).to_be_hidden()

    # The handheld picks up its token and switches to scanning
    expect(scanner.locator('#scannerApp')).to_be_visible()
    scanner.fill('#scanInput', sku)
    scanner.press('#scanInput', 'Enter')
    expect(scanner.locator('#itemList')).to_contain_text('E2E Scanner Cable')

    # The open register shows the scanned item after its next poll
    expect(cart(register).locator('.cart-item', has_text='E2E Scanner Cable')).to_be_visible(timeout=15000)
    expect(cart(register)).to_contain_text('Checkout ($8.56)')

    # ...and can sell it
    go_to_checkout(register)
    order_number = complete_sale(register)
    expect_receipt(register, order_number, '$8.56')
    assert helpers.item_quantity(e2e_server, item) == 5
    # The handheld sees the register's cart emptied
    expect(scanner.locator('#itemList')).not_to_contain_text('E2E Scanner Cable', timeout=15000)

    register.watcher.assert_clean()
    scanner.watcher.assert_clean()
