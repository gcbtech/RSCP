"""End-to-end flows through the real pages: login, receiving, inventory,
admin settings."""
import re

import pytest

from tests.e2e import helpers

expect = pytest.importorskip('playwright.sync_api').expect


def test_login_form_signs_in_and_out(page):
    page.goto('/receiving')
    assert '/login' in page.url  # guarded page bounces to the login form
    helpers.login(page, 'admin')
    page.goto('/receiving')
    assert '/login' not in page.url
    helpers.logout(page)
    page.goto('/receiving')
    assert '/login' in page.url
    page.watcher.assert_clean()


def test_wrong_password_is_refused(page):
    page.goto('/login')
    page.fill('input[name="username"]', 'cashier')
    page.fill('input[name="password"]', 'not-the-password')
    page.click('button[type="submit"]')
    page.wait_for_url(re.compile(r'/login'))
    assert 'Invalid username or password' in page.content()


def test_cashier_cannot_open_admin(cashier_page):
    page = cashier_page
    resp = page.goto('/admin/')
    assert resp.status == 403 or '/login' in page.url or 'admin' not in page.url
    page.watcher.take()  # the 403 itself is the expected outcome here


def test_receiving_scan_receives_package(admin_page, e2e_server):
    page = admin_page
    helpers.seed_package(e2e_server, '1ZE2E0000000000001', name='E2E Gadget', quantity=2)

    page.goto('/scan/receive')
    helpers.settle(page)
    page.fill('#scanInput', '1ZE2E0000000000001')
    page.press('#scanInput', 'Enter')

    card = page.locator('.item-container .item-card h2', has_text='E2E Gadget')
    card.wait_for()
    # Green = received (the page swaps body colour from the server's answer)
    expect(page.locator('body')).to_have_css('background-color', 'rgb(119, 221, 119)')
    status, scanned = helpers.package_status(e2e_server, '1ZE2E0000000000001')
    assert status == 'received' and scanned

    # Same scan again: duplicate (gold), still one receipt
    page.fill('#scanInput', '1ZE2E0000000000001')
    page.press('#scanInput', 'Enter')
    expect(page.locator('body')).to_have_css('background-color', 'rgb(255, 215, 0)')
    page.watcher.assert_clean()


def test_inventory_create_adjust_and_list(admin_page, e2e_server):
    page = admin_page
    name = 'E2E Created Widget'

    page.goto('/inventory/add')
    helpers.settle(page)
    page.fill('input[name="name"]', name)
    page.fill('input[name="quantity"]', '3')
    # Area is a Tom Select over the configured options
    area = page.locator('#locArea + .ts-wrapper .ts-control')
    area.click()
    page.locator('.ts-dropdown .option', has_text='Backroom').first.click()
    page.locator('form[action="/inventory/add"] button[type="submit"]').click()

    page.wait_for_url(re.compile(r'/inventory/items'))
    assert 'Item added: RSCP-' in page.content()
    item = helpers.item_by_name(e2e_server, name)
    assert item is not None and item['quantity'] == 3 and item['location_area'] == 'Backroom'

    # Adjust from the list: -2 through the row's modal
    page.goto('/inventory/items?q=' + name.replace(' ', '+'))
    helpers.settle(page)
    row = page.locator('tr', has_text=name).first
    row.locator(f'button[data-bs-target="#adjustModal{item["id"]}"]').click()
    modal = page.locator(f'#adjustModal{item["id"]}')
    modal.locator('input[name="quantity_change"]').wait_for(state='visible')
    modal.locator('input[name="quantity_change"]').fill('-2')
    modal.locator('button[type="submit"]', has_text='Apply').click()

    page.wait_for_load_state('load')
    assert 'Removed 2 units.' in page.content()
    assert helpers.item_by_name(e2e_server, name)['quantity'] == 1

    # The list shows the new quantity
    page.goto('/inventory/items?q=' + name.replace(' ', '+'))
    row = page.locator('tr', has_text=name).first
    assert re.search(re.escape(name) + r'\s+1\s', row.inner_text()), row.inner_text()  # name, then qty

    # Removing more than is in stock is refused
    page.locator(f'button[data-bs-target="#adjustModal{item["id"]}"]').click()
    modal.locator('input[name="quantity_change"]').wait_for(state='visible')
    modal.locator('input[name="quantity_change"]').fill('-5')
    modal.locator('button[type="submit"]', has_text='Apply').click()
    page.wait_for_load_state('load')
    assert 'Cannot reduce by 5 - only 1 in stock.' in page.content()
    assert helpers.item_by_name(e2e_server, name)['quantity'] == 1
    page.watcher.assert_clean()


def test_item_stock_history_and_reverse(admin_page, e2e_server):
    page = admin_page
    name = 'E2E History Widget'
    item_id = helpers.seed_item(e2e_server, name, quantity=5)
    with helpers.db(e2e_server) as conn:
        from app.services import inventory
        inventory.adjust_stock(conn, item_id, -2, inventory.REASON_DAMAGED, 'admin', 'dropped')

    page.goto(f'/inventory/edit/{item_id}')
    helpers.settle(page)
    history = page.locator('#stock-history')
    expect(history).to_contain_text('Stock history')
    expect(history).to_contain_text('Initial stock')
    expect(history).to_contain_text('Damaged')
    expect(history).to_contain_text('dropped')

    # Reverse the damage entry (confirmed): a new +2 entry, the original stays
    page.once('dialog', lambda dialog: dialog.accept())
    history.locator('button[title="Reverse this entry"]').first.click()
    page.wait_for_load_state('load')
    assert helpers.item_quantity(e2e_server, item_id) == 5
    page.goto(f'/inventory/edit/{item_id}')
    history = page.locator('#stock-history')
    expect(history).to_contain_text('Reversal')
    expect(history).to_contain_text('reversed by')
    page.watcher.assert_clean()


def test_admin_setting_persists(admin_page, e2e_server):
    page = admin_page
    page.goto('/admin/?tab=settings')
    helpers.settle(page)
    select = page.locator('select[name="SESSION_TIMEOUT_MINUTES"]')
    current = select.input_value()
    target = '60' if current != '60' else '120'
    select.select_option(target)
    select.locator('xpath=ancestor::form').locator('button[type="submit"]').click()
    page.wait_for_load_state('load')

    assert str(helpers.setting(e2e_server, 'SESSION_TIMEOUT_MINUTES')) == target
    page.goto('/admin/?tab=settings')
    assert page.locator('select[name="SESSION_TIMEOUT_MINUTES"]').input_value() == target
    page.watcher.assert_clean()
