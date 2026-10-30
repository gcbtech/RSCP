"""Phone UI (/m) in a phone-sized touch browser: the hardware scanner is
simulated by keystrokes into whatever has focus, as the FZ-N1's keyboard
wedge does."""
import os

import pytest

from tests.e2e import helpers

expect = pytest.importorskip('playwright.sync_api').expect

FZ_N1_UA = ('Mozilla/5.0 (Linux; Android 11; FZ-N1 Build/RKQ1.210924.001; wv) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Version/4.0 Chrome/143.0.7499.34 Mobile Safari/537.36')


def _phone(request, browser, e2e_server, user):
    """A signed-in user on a 360x600 touch screen (FZ-N1 in Fully Kiosk)."""
    context = browser.new_context(base_url=e2e_server.base_url, viewport={'width': 360, 'height': 600},
                                  is_mobile=True, has_touch=True, user_agent=FZ_N1_UA)
    context.set_default_timeout(15000)
    pg = context.new_page()
    pg.watcher = helpers.PageWatcher(pg, e2e_server.base_url)
    pg.on('dialog', lambda d: d.accept())
    helpers.login(pg, user)
    yield pg
    failed = getattr(request.node, 'rep_call', None)
    if failed is not None and failed.failed:
        try:
            pg.screenshot(path=os.path.join(e2e_server.artifacts, f'{request.node.name}.png'), full_page=True)
        except Exception:
            pass
    context.close()


@pytest.fixture
def phone(request, browser, e2e_server):
    yield from _phone(request, browser, e2e_server, 'cashier')


@pytest.fixture
def manager_phone(request, browser, e2e_server):
    yield from _phone(request, browser, e2e_server, 'admin')


def scan(page, code, suffix='Enter'):
    """What the scanner does: fast keystrokes into the focused element, then the suffix."""
    page.keyboard.type(code, delay=5)
    if suffix:
        page.keyboard.press(suffix)


def assert_listening(page):
    """The capture input holds the focus with inputmode="none" (static/m/scanner.js)."""
    expect(page.locator('#mScanStatus')).to_have_attribute('data-state', 'listening')
    expect(page.locator('#mScanCapture')).to_be_focused()
    expect(page.locator('#mScanCapture')).to_have_attribute('inputmode', 'none')


def open_scan_screen(page, path='/m/receive'):
    """Load a scanning screen: it listens straight away, no tap needed."""
    page.goto(path)
    helpers.settle(page)
    # The page script has attached the scanner. A script that failed to load (a dropped
    # connection) shows up here, not as a dead scan.
    expect(page.locator('#mScanStatus')).to_have_attribute('data-scanner', 'ready', timeout=5000)
    assert_listening(page)


def test_page_listens_on_load_and_taps_keep_the_focus(phone, e2e_server):
    """FZ-N1: the capture input is focused as the page loads (no tap), and taps
    that aren't on a text field never take the focus from it."""
    helpers.seed_package(e2e_server, '1ZPHONE00000000007', name='First Scan Box')
    helpers.seed_package(e2e_server, '1ZPHONE00000000008', name='Second Scan Box')
    open_scan_screen(phone)
    scan(phone, '1ZPHONE00000000007')
    expect(phone.locator('.m-banner-sub')).to_contain_text('1ZPHONE00000000007')

    phone.locator('.m-lbl').first.tap()                  # the background
    phone.locator('#mScanStatus .m-scanner-head').tap()  # the card
    assert_listening(phone)
    scan(phone, '1ZPHONE00000000008')
    expect(phone.locator('.m-banner-sub')).to_contain_text('1ZPHONE00000000008')
    phone.watcher.assert_clean()


def test_scan_with_no_text_marks_the_scanner_lost_and_a_tap_restarts_it(phone):
    """The FZ-N1's keyboard app sends keys with no text when it has no field to type
    into: say so, and reload on a tap (a fresh load gets the connection back)."""
    open_scan_screen(phone)
    phone.evaluate("""() => { const c = document.getElementById('mScanCapture');
        for (let i = 0; i < 12; i++) c.dispatchEvent(new KeyboardEvent('keydown', { key: 'Unidentified', bubbles: true }));
        c.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', keyCode: 13, bubbles: true })); }""")
    expect(phone.locator('#mScanStatus')).to_have_attribute('data-state', 'lost')
    expect(phone.get_by_text('Scanner stopped')).to_be_visible()
    with phone.expect_navigation():
        phone.locator('#mScanStatus .m-scanner-head').tap()
    expect(phone.locator('#mScanStatus')).to_have_attribute('data-scanner', 'ready', timeout=5000)
    assert_listening(phone)


def test_scanning_keeps_working_after_clear(phone, e2e_server):
    """FZ-N1 report: after tapping Clear the scanner went deaf until a field was tapped."""
    helpers.seed_package(e2e_server, '1ZPHONE00000000001', name='Phone Gadget')
    helpers.seed_package(e2e_server, '1ZPHONE00000000002', name='Phone Widget')
    open_scan_screen(phone)

    scan(phone, '1ZPHONE00000000001')
    expect(phone.locator('.m-banner-title')).to_have_text('Received')
    phone.locator('#mSessionClear').tap()
    expect(phone.locator('#mSessionClear')).to_be_hidden()
    assert_listening(phone)

    scan(phone, '1ZPHONE00000000002')
    expect(phone.locator('.m-banner-sub')).to_contain_text('1ZPHONE00000000002')
    expect(phone.locator('#mSessionCount')).to_have_text('· 1')
    assert helpers.package_status(e2e_server, '1ZPHONE00000000002')[0] == 'received'
    phone.watcher.assert_clean()


def test_sheets_leave_the_focus_on_the_scanner(phone, e2e_server):
    """Sheets open non-modally on scanning pages (a modal one would take the focus,
    which the FZ-N1 can't get back without its keyboard); a scan meanwhile is ignored."""
    helpers.seed_package(e2e_server, '1ZPHONE00000000010', name='After Sheet Box')
    open_scan_screen(phone)
    phone.get_by_role('button', name='Notifications').tap()
    expect(phone.locator('#mNotifications')).to_be_visible()
    expect(phone.locator('#mScanCapture')).to_be_focused()
    expect(phone.locator('#mScanStatus')).to_have_attribute('data-state', 'paused')
    scan(phone, '1ZPHONE00000000010')
    expect(phone.locator('.m-banner-sub')).to_have_count(0)    # not received behind the sheet
    phone.locator('.m-sheet-backdrop').tap(position={'x': 180, 'y': 40})
    expect(phone.locator('#mNotifications')).to_be_hidden()
    assert_listening(phone)
    scan(phone, '1ZPHONE00000000010')
    expect(phone.locator('.m-banner-sub')).to_contain_text('1ZPHONE00000000010')
    phone.watcher.assert_clean()


def test_add_to_stock_then_stock_buttons_then_scan_again(phone, e2e_server):
    item_id = helpers.seed_item(e2e_server, 'Phone Charger', quantity=5, sku='PH-CHG')
    helpers.seed_package(e2e_server, '1ZPHONE00000000003', name='Phone Charger', quantity=2, sku='PH-CHG')
    helpers.seed_package(e2e_server, '1ZPHONE00000000004', name='Something Else')
    open_scan_screen(phone)

    scan(phone, '1ZPHONE00000000003')
    phone.get_by_role('button', name='Add 2 to stock').tap()
    expect(phone.locator('.m-match-done')).to_contain_text('now 7 in stock')
    expect(phone.get_by_role('button', name='Link item')).to_have_count(0)   # can't count it twice
    assert helpers.item_quantity(e2e_server, item_id) == 7

    scan(phone, 'PH-CHG')
    expect(phone.locator('.m-banner-title')).to_have_text('Stock item')
    phone.get_by_role('button', name='Add one to stock').tap()
    expect(phone.locator('[data-qty]')).to_have_text('8')

    assert_listening(phone)
    scan(phone, '1ZPHONE00000000004')
    expect(phone.locator('.m-banner-sub')).to_contain_text('1ZPHONE00000000004')
    phone.watcher.assert_clean()


def test_scans_without_a_suffix_and_slow_typing(phone, e2e_server):
    helpers.seed_package(e2e_server, '1ZPHONE00000000005', name='No Suffix Box')
    open_scan_screen(phone)

    # A scanner set up without Enter: the fast burst is sent after a short pause
    scan(phone, '1ZPHONE00000000005', suffix=None)
    expect(phone.locator('.m-banner-sub')).to_contain_text('1ZPHONE00000000005')

    # Human typing in the visible field waits for Enter (the keyboard icon
    # brings the field back after a scan shrank the scanner card)
    phone.get_by_role('button', name='Type a tracking number').tap()
    expect(phone.locator('#mScanManual')).to_be_focused()
    phone.keyboard.type('1ZHALF', delay=150)
    phone.wait_for_timeout(600)
    expect(phone.locator('.m-banner-sub')).to_contain_text('1ZPHONE00000000005')
    assert phone.locator('#mScanManual').input_value() == '1ZHALF'
    phone.locator('.m-banner-title').tap()       # tapping away ends the typing
    assert_listening(phone)
    phone.watcher.assert_clean()


def test_link_sheet_picks_a_scanned_sku_and_closes_back_to_listening(phone, e2e_server):
    item_id = helpers.seed_item(e2e_server, 'Linkable Dock', quantity=1, sku='PH-DOCK')
    helpers.seed_package(e2e_server, '1ZPHONE00000000006', name='Mystery Dock', quantity=1)
    open_scan_screen(phone)

    scan(phone, '1ZPHONE00000000006')
    phone.get_by_role('button', name='Link item').tap()
    expect(phone.locator('#mLinkSheet')).to_be_visible()
    scan(phone, 'PH-DOCK')                                  # fills the sheet's search (data-scan-into)
    expect(phone.locator('[data-action="mLinkPick"][aria-pressed="true"]')).to_contain_text('Linkable Dock')
    phone.get_by_role('button', name='Link', exact=True).tap()
    expect(phone.locator('#mLinkSheet')).to_be_hidden()
    expect(phone.locator('.m-match-done')).to_contain_text('Linked to Linkable Dock')
    assert helpers.item_quantity(e2e_server, item_id) == 2

    assert_listening(phone)
    phone.watcher.assert_clean()


# ---------------------------------------------------------------- Stock

def test_stock_lookup_buttons_and_set_count(phone, e2e_server):
    item_id = helpers.seed_item(e2e_server, 'Lookup Keyboard', quantity=6, sku='PH-KBD')
    open_scan_screen(phone, '/m/stock')

    scan(phone, 'PH-KBD')
    card = phone.locator('[data-stock-item]')
    expect(card.locator('.m-item-name')).to_have_text('Lookup Keyboard')
    expect(card.locator('[data-qty]')).to_have_text('6')
    phone.get_by_role('button', name='Remove one from stock').tap()
    expect(card.locator('[data-qty]')).to_have_text('5')

    phone.get_by_role('button', name='Set count').tap()
    expect(phone.locator('#mCountSheet')).to_be_visible()
    phone.locator('#mCountInput').fill('20')
    phone.get_by_role('button', name='Save count').tap()
    expect(card.locator('[data-qty]')).to_have_text('20')
    assert helpers.item_quantity(e2e_server, item_id) == 20

    assert_listening(phone)                       # the sheet closed back to scanning
    scan(phone, 'Lookup')                         # a name: one match opens it too
    expect(card.locator('.m-item-name')).to_have_text('Lookup Keyboard')
    phone.watcher.assert_clean()


def test_item_audit_counts_repeat_scans_and_asks_before_switching(phone, e2e_server):
    first = helpers.seed_item(e2e_server, 'Audit Mouse', quantity=3, sku='PH-AUD-1')
    second = helpers.seed_item(e2e_server, 'Audit Pad', quantity=5, sku='PH-AUD-2')
    phone.goto('/m/stock/audit')
    phone.get_by_role('button', name='Item audit').tap()
    phone.wait_for_url('**/m/stock/audit/*')
    open_scan_screen(phone, phone.url)

    for _ in range(3):
        scan(phone, 'PH-AUD-1')
    expect(phone.locator('#mAuditCount')).to_have_text('3')

    scan(phone, 'PH-AUD-2')                       # a different item while one is unsaved
    expect(phone.locator('#mAuditSwitch')).to_be_visible()
    phone.get_by_role('button', name='Save, then next').tap()
    expect(phone.locator('#mAuditSummary')).to_have_text('1 counted · 0 off')
    expect(phone.locator('#mAuditName')).to_have_text('Audit Pad')

    phone.get_by_role('button', name='Save count 1').tap()
    expect(phone.locator('#mAuditSummary')).to_have_text('2 counted · 1 off')
    assert helpers.item_quantity(e2e_server, first) == 3
    assert helpers.item_quantity(e2e_server, second) == 1

    phone.get_by_role('button', name='Finish').tap()   # confirm() is accepted by the fixture
    phone.wait_for_url('**/report')
    expect(phone.get_by_text("Didn't match · 1")).to_be_visible()
    phone.watcher.assert_clean()


def test_new_item_from_a_received_package_comes_back_to_scanning(manager_phone, e2e_server):
    phone = manager_phone
    helpers.seed_package(e2e_server, '1ZPHONE00000000009', name='Brand New Gadget', quantity=3)
    open_scan_screen(phone)
    scan(phone, '1ZPHONE00000000009')
    phone.get_by_role('link', name='New item').tap()
    phone.wait_for_url('**/m/stock/add?**')
    expect(phone.locator('#addName')).to_have_value('Brand New Gadget')
    expect(phone.locator('#addQty')).to_have_value('3')
    phone.locator('#addPrice').fill('12.50')
    phone.locator('#addShelf').fill('C2')
    phone.get_by_role('button', name='Add item').tap()
    phone.wait_for_url('**/m/receive')
    expect(phone.locator('.m-flash')).to_contain_text('Added Brand New Gadget')
    item = helpers.item_by_name(e2e_server, 'Brand New Gadget')
    assert item['quantity'] == 3 and item['sell_price_cents'] == 1250
    phone.watcher.assert_clean()


# ---------------------------------------------------------------- POS

def test_pos_scan_search_and_cash_sale(phone, e2e_server):
    ram_id, ram_sku = helpers.seed_pos_item(e2e_server, 'Phone POS RAM', 54.99, quantity=5)
    helpers.seed_pos_item(e2e_server, 'Phone POS Mouse', 14.99, quantity=3)
    open_scan_screen(phone, '/m/pos')
    expect(phone.locator('#mCart')).to_contain_text('Cart is empty')      # hello done, own cart

    scan(phone, ram_sku)
    expect(phone.locator('#mCart')).to_contain_text('Phone POS RAM')
    phone.get_by_role('button', name='One more Phone POS RAM').tap()
    expect(phone.locator('#mCart')).to_contain_text('2 items')

    scan(phone, 'Phone POS Mou')                   # not a code: pick from the matches
    expect(phone.locator('#mPickSheet')).to_be_visible()
    phone.locator('#mPickList').get_by_role('button').first.tap()
    expect(phone.locator('#mCart')).to_contain_text('Phone POS Mouse')

    phone.get_by_role('link', name='Checkout').tap()
    phone.wait_for_url('**/m/pos/checkout')
    phone.get_by_role('button', name='Exact').tap()
    expect(phone.locator('#mChange')).to_have_text('$0.00')
    phone.get_by_role('button', name='Complete sale').tap()
    phone.wait_for_url('**/m/pos/receipt/**')
    expect(phone.locator('.m-flash')).to_contain_text('completed')
    assert helpers.item_quantity(e2e_server, ram_id) == 3
    phone.watcher.assert_clean()


def test_pos_hold_and_recall(phone, e2e_server):
    _id, sku = helpers.seed_pos_item(e2e_server, 'Phone POS Hold Item', 9.99)
    open_scan_screen(phone, '/m/pos')
    expect(phone.locator('#mCart')).to_contain_text('Cart is empty')
    scan(phone, sku)
    expect(phone.locator('#mCart')).to_contain_text('Phone POS Hold Item')
    phone.get_by_role("button", name="Hold", exact=True).tap()
    phone.locator('#mHoldNote').fill('Phone customer')
    phone.get_by_role('button', name='Hold order').tap()
    expect(phone.locator('#mCart')).to_contain_text('Cart is empty')

    phone.get_by_role('button', name='More: custom item, discount, held orders, clear cart').tap()
    phone.locator('#mPosMenu').get_by_role('button', name='Held orders').tap()
    expect(phone.locator('#mHeld')).to_contain_text('Phone customer')
    phone.locator('#mHeld').get_by_role('button', name='Recall').first.tap()
    expect(phone.locator('#mCart')).to_contain_text('Phone POS Hold Item')
    phone.watcher.assert_clean()


# ---------------------------------------------------------------- Time

def test_clock_in_break_and_out(phone):
    phone.goto('/m/time')
    helpers.settle(phone)
    phone.get_by_role('button', name='Clock in').tap()
    expect(phone.locator('.m-flash')).to_have_text('Clocked in')
    expect(phone.locator('.m-clock-state')).to_contain_text('Working')
    expect(phone.locator('[data-since]')).to_have_text('0m')           # time.js counts it up
    phone.get_by_role('button', name='Start break').tap()
    expect(phone.locator('.m-clock-state')).to_contain_text('On break')
    phone.get_by_role('button', name='End break').tap()
    expect(phone.locator('.m-flash')).to_have_text('Break over, back to work')
    phone.get_by_role('button', name='Clock out').tap()                 # confirm() is accepted by the fixture
    expect(phone.locator('.m-clock-state')).to_contain_text('Clocked out')
    phone.watcher.assert_clean()


def test_time_off_form_asks_for_times_only_for_part_of_a_shift(phone):
    phone.goto('/m/time/off')
    helpers.settle(phone)
    expect(phone.locator('#mPtoTimes')).to_be_hidden()
    phone.get_by_text('Part of a shift').tap()
    expect(phone.locator('#mPtoTimes')).to_be_visible()
    expect(phone.locator('#mPtoStart')).to_have_attribute('required', '')
    phone.get_by_text('Whole shift').tap()
    expect(phone.locator('#mPtoTimes')).to_be_hidden()
    phone.watcher.assert_clean()


# ---------------------------------------------------------------- Home and shell

def _notify(server, user_id, title, link=None, kind='info', read=0):
    with helpers.db(server) as conn:
        conn.execute('INSERT INTO notifications (user_id, title, message, type, link, is_read) VALUES (?, ?, ?, ?, ?, ?)',
                     (user_id, title, 'E2E message', kind, link, read))
        conn.commit()


def test_notifications_open_phone_pages_and_mark_read(phone, e2e_server):
    item_id = helpers.seed_item(e2e_server, 'Phone Notif SODIMM', quantity=1)
    _notify(e2e_server, 2, 'Low stock: Phone Notif SODIMM', f'/inventory/edit/{item_id}', 'warning')
    _notify(e2e_server, 2, 'Package upload finished', '/admin#packages')
    _notify(e2e_server, 2, 'Backup finished', kind='success', read=1)
    phone.goto('/m/')
    helpers.settle(phone)
    expect(phone.locator('#mBellBadge')).to_have_text('2')
    phone.get_by_role('button', name='Notifications, 2 unread').tap()
    sheet = phone.locator('#mNotifications')
    expect(sheet.locator('.m-notif')).to_have_count(3)
    low = sheet.locator(f'a.m-notif[href="/m/stock/item/{item_id}"]')    # the phone's own item page
    expect(low.locator('.m-lead')).to_have_class('m-lead m-tone-wn')
    expect(sheet.locator('a.m-notif[href="/admin#packages"]')).to_contain_text('Opens the desktop page')
    expect(sheet.locator('.m-notif-unread')).to_have_count(2)

    sheet.get_by_role('button', name='Mark all read').tap()
    expect(sheet.locator('.m-notif-unread')).to_have_count(0)
    expect(sheet.get_by_role('button', name='Mark all read')).to_be_disabled()
    expect(phone.locator('#mBellBadge')).to_be_hidden()
    low.tap()
    expect(phone).to_have_url(f'{e2e_server.base_url}/m/stock/item/{item_id}')
    phone.watcher.assert_clean()


def test_offline_bar_follows_the_connection(phone):
    phone.goto('/m/')
    helpers.settle(phone)
    expect(phone.locator('#mOffline')).to_be_hidden()
    phone.context.set_offline(True)
    expect(phone.locator('#mOffline')).to_be_visible()
    phone.context.set_offline(False)
    expect(phone.locator('#mOffline')).to_be_hidden()


def test_home_keeps_its_numbers_current(phone, e2e_server):
    phone.clock.install()
    phone.goto('/m/')
    helpers.settle(phone)
    past_due = phone.locator('a.m-row[href="/m/receive/past-due"] .m-pill')
    before = int(past_due.inner_text())
    with helpers.db(e2e_server) as conn:
        conn.execute("INSERT INTO packages (tracking_number, item_name, date_expected, status) "
                     "VALUES ('1ZPHONELATE0001', 'Late', date('now', '-3 days'), 'past_due')")
        conn.commit()
    phone.clock.run_for(61000)                    # static/m/home.js: every minute
    expect(past_due).to_have_text(str(before + 1))
    phone.watcher.assert_clean()
