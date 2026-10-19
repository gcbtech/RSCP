"""Unit tests for the POS services: pos_pricing (pure money math, coupons),
pos_checkout, pos_refunds and pos_sales (DB: stock + ledger + payments)."""
import json
from datetime import date
from types import SimpleNamespace

import pytest

from app.services import inventory as inv
from app.services import pos_checkout, pos_pricing, pos_refunds, pos_registers, pos_sales
from app.services.db import get_db_connection
from app.services.pos_pricing import PricingConfig

CASHIER = SimpleNamespace(id=2, username='cashier')
MANAGER = SimpleNamespace(id=3, username='manager')
NO_TAX = PricingConfig(tax_rate=0.0)
TAX7 = PricingConfig(tax_rate=0.07)


@pytest.fixture
def conn(fresh_app):
    c = get_db_connection(fresh_app.config['DATABASE'])
    c.isolation_level = None  # as the checkout/refund routes use it
    yield c
    c.close()


def _item(conn, sku='WIDGET', price=1000, qty=10, **extra):
    fields = {'sku': sku, 'name': f'{sku} item', 'quantity': qty, 'sell_price_cents': price,
              'location_area': 'A'}
    fields.update(extra)
    return inv.create_item(conn, fields, user='admin')


def _line(item_id, sku='WIDGET', price=1000, qty=1, **extra):
    line = {'inventory_item_id': item_id, 'sku': sku, 'name': f'{sku} item', 'quantity': qty,
            'unit_price_cents': price, 'discount_type': None, 'discount_percent': 0, 'discount_cents': 0,
            'line_total_cents': price * qty}
    line.update(extra)
    return line


def _cart(*lines, **extra):
    cart = {'items': list(lines), 'discount_type': None, 'discount_percent': 0, 'discount_cents': 0,
            'discount_reason': ''}
    cart.update(extra)
    return cart


def _qty(conn, item_id):
    return conn.execute('SELECT quantity FROM inventory_items WHERE id = ?', (item_id,)).fetchone()[0]


def _ledger(conn, item_id):
    return [tuple(r) for r in conn.execute(
        'SELECT delta, reason, user, note FROM inventory_movements '
        'WHERE item_id = ? ORDER BY id', (item_id,))][1:]  # skip the initial stock row


def _payments(conn, order_id):
    return [tuple(r) for r in conn.execute(
        'SELECT method, amount_cents, tendered_cents, change_given_cents FROM pos_payments '
        'WHERE order_id = ? ORDER BY id',
        (order_id,))]


def _sell(conn, cart, method='cash', config=NO_TAX, **payment):
    return pos_checkout.complete_sale(conn, pos_checkout.SessionCart(cart),
                                      pos_checkout.Payment(method, **payment), CASHIER, config=config)


# --- pricing (pure) ------------------------------------------------------

def test_money_helpers_round_half_up():
    assert pos_pricing.calculate_tax(2600, 0.0725) == 189      # 188.5 -> 189
    assert pos_pricing.calculate_percentage(5000, 7.25) == 363  # 362.5 -> 363
    assert pos_pricing.calculate_percentage(0, 10) == 0
    assert pos_pricing.calculate_line_total(1000, 3) == 3000
    assert pos_pricing.calculate_line_total(1000, 3, 'percent', discount_percent=10) == 2700
    assert pos_pricing.calculate_line_total(1000, 1, 'fixed', discount_cents=1500) == 0


def test_parse_money():
    assert pos_pricing.parse_money('12.345') == 1235
    for bad in ('-1', 'abc', 'nan', 'inf', None):
        assert pos_pricing.parse_money(bad) is None
    assert pos_pricing.parse_money_field('  ', default=7) == 7
    assert pos_pricing.parse_money_field(None) is None


def test_cart_totals_order_discount_tax_and_cash_price():
    cart = _cart(_line(1, price=5000, qty=2), discount_percent=10, discount_type='percent')
    config = PricingConfig(tax_rate=0.07, cash_discount_enabled=True,
                           cash_discount_percent=3, cash_discount_type='percent')
    t = pos_pricing.cart_totals(cart, config)
    assert t['subtotal_cents'] == 10000 and t['order_discount_cents'] == 1000
    assert t['tax_amount_cents'] == 630 and t['card_total_cents'] == 9630
    assert t['cash_discount_cents'] == 270 and t['cash_total_cents'] == 9360
    assert t['tax_rate_pct'] == 7.0 and t['item_count'] == 1
    # the JSON the page scripts read is unchanged: dollars, the old keys
    j = pos_pricing.totals_json(t)
    assert (j['subtotal'], j['tax_amount'], j['card_total'], j['cash_total']) == (100.0, 6.3, 96.3, 93.6)
    assert j['items'][0]['unit_price'] == 50.0 and j['items'][0]['line_total'] == 100.0


def test_cart_totals_fixed_discount_and_coupon_never_go_negative():
    cart = _cart(_line(1, price=500), discount_cents=800, discount_type='fixed',
                 applied_coupon={'code': 'X', 'discount_cents': 300})
    t = pos_pricing.cart_totals(cart, TAX7)
    assert t['order_discount_cents'] == 500 and t['coupon_discount_cents'] == 300
    assert t['tax_amount_cents'] == 0 and t['card_total_cents'] == 0
    assert not t['cash_discount_enabled'] and t['cash_total_cents'] == 0


def test_split_cash_discount_uses_cash_that_pays_the_sale():
    pct = PricingConfig(cash_discount_enabled=True, cash_discount_percent=3, cash_discount_type='percent')
    # $100 handed over for a $20 cash balance: 3% of $20
    assert pos_pricing.split_cash_discount(10000, [8000], 10000, pct) == 60
    fixed = PricingConfig(cash_discount_enabled=True, cash_discount_cents=500, cash_discount_type='fixed')
    assert pos_pricing.split_cash_discount(5000, [5000], 10000, fixed) == 250
    assert pos_pricing.split_cash_discount(5000, [], 10000, NO_TAX) == 0


def _coupon(conn, code='SAVE5', discount_type='order_dollar', value=500, **extra):
    fields = {'name': code, 'code': code, 'coupon_type': 'standard', 'discount_type': discount_type,
              ('discount_cents' if discount_type.endswith('_dollar') else 'discount_percent'): value}
    fields.update(extra)
    cols = ', '.join(fields)
    cur = conn.execute(f'INSERT INTO pos_coupons ({cols}) VALUES ({", ".join("?" * len(fields))})',
                       tuple(fields.values()))
    return cur.lastrowid


def test_evaluate_coupon_rules(conn):
    _coupon(conn, 'SAVE5', min_purchase_cents=2000)
    _coupon(conn, 'OLD', end_date='2000-01-01')
    _coupon(conn, 'USED', max_uses=1, current_uses=1)
    _coupon(conn, 'SOLO', cannot_combine=1)
    items = [_line(1, price=2500)]
    ok = pos_pricing.evaluate_coupon('SAVE5', items, 2500, conn)
    assert ok['valid'] and ok['discount_cents'] == 500
    assert pos_pricing.coupon_result_json(ok)['discount_amount'] == 5.0
    assert pos_pricing.evaluate_coupon('SAVE5', items, 1000, conn)['error'] == 'Minimum purchase of $20.00 required.'
    assert pos_pricing.evaluate_coupon('OLD', items, 2500, conn)['error'] == 'Coupon has expired.'
    assert pos_pricing.evaluate_coupon('USED', items, 2500, conn)['error'] == 'Coupon has reached maximum uses.'
    assert pos_pricing.evaluate_coupon('NOPE', items, 2500, conn)['error'] == 'Invalid coupon code.'
    combined = pos_pricing.evaluate_coupon('SOLO', items, 2500, conn,
                                           cart=_cart(*items, discount_cents=500, discount_type='fixed'))
    assert combined['error'] == 'This coupon cannot be combined with other discounts.'


def test_item_and_bogo_coupon_discounts(conn):
    a = _item(conn, 'AAA', 400)
    b = _item(conn, 'BBB', 600)
    pct = _coupon(conn, 'PCT', 'item_percent', 50)
    bogo = _coupon(conn, 'BOGO', 'bogo_free', 0, buy_quantity=1)
    for cid in (pct, bogo):
        conn.execute('INSERT INTO pos_coupon_items (coupon_id, item_id) VALUES (?, ?)', (cid, a))
    items = [_line(a, 'AAA', 400, qty=3), _line(b, 'BBB', 600)]
    assert pos_pricing.evaluate_coupon('PCT', items, 1800, conn)['discount_cents'] == 600
    assert pos_pricing.evaluate_coupon('BOGO', items, 1800, conn)['discount_cents'] == 400  # 3 // 2 free
    assert pos_pricing.evaluate_coupon('PCT', [_line(b, 'BBB', 600)], 600, conn)['error'] == \
        'Required item for this coupon is not in cart.'


def test_route_names_still_reexport_the_service():
    from app.routes.pos import core, coupons, checkout
    assert core.calculate_line_total is pos_pricing.calculate_line_total
    assert coupons.evaluate_coupon is pos_pricing.evaluate_coupon
    assert checkout._money_value('3.333') == 333


# --- checkout --------------------------------------------------------------

def test_cash_sale_records_order_payments_and_sells_stock(conn):
    item = _item(conn, qty=5)
    sale = _sell(conn, _cart(_line(item, qty=2)), 'cash', TAX7, tendered='30')
    assert sale.total_cents == 2140 and sale.payment_details['change_cents'] == 860
    assert sale.order_number == f"POS-{date.today():%Y%m%d}-001"
    order = conn.execute('SELECT * FROM pos_orders WHERE id = ?', (sale.order_id,)).fetchone()
    assert (order['subtotal_cents'], order['tax_amount_cents'], order['total_cents'],
            order['operator_id']) == (2000, 140, 2140, 2)
    assert _payments(conn, sale.order_id) == [('cash', 2140, 3000, 860)]
    assert _qty(conn, item) == 3
    assert _ledger(conn, item) == [(-2, 'sale', '2', sale.order_number)]
    assert not conn.in_transaction


def test_sale_may_take_stock_negative_and_ends_sale_on_zero(conn):
    item = _item(conn, qty=1)
    conn.execute('UPDATE inventory_items SET sale_enabled = 1, sale_end_on_stock = 1 WHERE id = ?', (item,))
    _sell(conn, _cart(_line(item, qty=3)), 'card')
    row = conn.execute('SELECT quantity, sale_enabled FROM inventory_items WHERE id = ?', (item,)).fetchone()
    assert (row['quantity'], row['sale_enabled']) == (-2, 0)


def test_sale_of_a_deleted_item_still_records(conn):
    sale = _sell(conn, _cart(_line(999999)), 'card')
    assert conn.execute('SELECT COUNT(*) FROM pos_order_items WHERE order_id = ? AND inventory_item_id IS NULL '
                        "AND sku = 'WIDGET'", (sale.order_id,)).fetchone()[0] == 1
    assert conn.execute('SELECT COUNT(*) FROM inventory_movements WHERE item_id = 999999'
                        ).fetchone()[0] == 0


def test_short_tender_and_bad_split_are_typed_errors_and_write_nothing(conn):
    item = _item(conn, qty=5)
    cart = _cart(_line(item))
    with pytest.raises(pos_checkout.TenderError) as e:
        _sell(conn, cart, 'cash', tendered='5')
    assert e.value.message == 'Cash tendered must be at least $10.00.'
    with pytest.raises(pos_checkout.TenderError) as e:
        _sell(conn, cart, 'split', cash_amount='2', card_amounts=['3'])
    assert e.value.message == 'Split payments add up to $5.00, less than the $10.00 total.'
    with pytest.raises(pos_checkout.TenderError) as e:
        _sell(conn, cart, 'split', cash_amount='0', card_amounts=['11'])
    assert e.value.message == 'Card amounts add up to more than the $10.00 total.'
    with pytest.raises(pos_checkout.TenderError):
        _sell(conn, cart, 'split', cash_amount='x')
    with pytest.raises(pos_checkout.UnknownPaymentMethod):
        _sell(conn, cart, 'bitcoin')
    with pytest.raises(pos_checkout.CartEmpty):
        _sell(conn, _cart())
    assert conn.execute('SELECT COUNT(*) FROM pos_orders').fetchone()[0] == 0
    assert _qty(conn, item) == 5 and not conn.in_transaction


def test_split_sale_ledger_and_cash_discount_on_cash_part(conn):
    item = _item(conn, price=10000)
    config = PricingConfig(cash_discount_enabled=True, cash_discount_percent=3, cash_discount_type='percent')
    sale = _sell(conn, _cart(_line(item, price=10000)), 'split', config,
                 cash_amount='50', card_amounts=['80'])
    # cash pays the $20 balance: 3% of $20 off
    assert sale.cash_discount_cents == 60 and sale.total_cents == 9940
    assert sale.payment_details['change_cents'] == 3060
    assert _payments(conn, sale.order_id) == [('card', 8000, None, 0), ('cash', 1940, 5000, 3060)]


def test_token_records_once(conn):
    item = _item(conn, qty=5)
    pay = pos_checkout.Payment('card')
    first = pos_checkout.complete_sale(conn, pos_checkout.SessionCart(_cart(_line(item))), pay,
                                       CASHIER, token='tok-1', config=NO_TAX)
    with pytest.raises(pos_checkout.AlreadyCompleted) as e:
        pos_checkout.complete_sale(conn, pos_checkout.SessionCart(_cart(_line(item))), pay,
                                   CASHIER, token='tok-1', config=NO_TAX)
    assert e.value.order_number == first.order_number and e.value.redirect_to == 'receipt'
    assert _qty(conn, item) == 4


def test_register_cart_is_read_and_emptied_under_the_lock(conn):
    item = _item(conn, qty=5)
    conn.execute('BEGIN')
    version = pos_registers.write_register_cart(conn, 'REG-1', _cart(_line(item)))
    conn.execute('COMMIT')
    source = pos_checkout.RegisterCart('REG-1')
    with pytest.raises(pos_checkout.CartChanged):
        pos_checkout.complete_sale(conn, source, pos_checkout.Payment('card'), CASHIER,
                                   expected_version=str(version - 1), config=NO_TAX)
    with pytest.raises(pos_checkout.CartChanged):
        pos_checkout.complete_sale(conn, source, pos_checkout.Payment('card'), CASHIER,
                                   expected_total='9.00', config=NO_TAX)
    sale = pos_checkout.complete_sale(conn, source, pos_checkout.Payment('card'), CASHIER,
                                      expected_version=str(version), expected_total='10.00',
                                      config=NO_TAX)
    cart, after = pos_registers.read_register_cart(conn, 'REG-1')
    assert cart['items'] == [] and after == version + 1
    assert sale.total_cents == 1000 and _qty(conn, item) == 4


def test_coupon_is_revalidated_and_redeemed(conn):
    item = _item(conn, price=3000)
    cid = _coupon(conn, 'SAVE5', min_purchase_cents=2000, max_uses=1)
    coupon = {'id': cid, 'code': 'SAVE5', 'name': 'SAVE5', 'discount_cents': 500}
    # Items were removed after applying: the coupon no longer qualifies
    with pytest.raises(pos_checkout.CouponInvalid) as e:
        _sell(conn, _cart(_line(item, price=1000), applied_coupon=dict(coupon)), 'card')
    assert e.value.message == 'Coupon removed: Minimum purchase of $20.00 required. Please review the total.'
    sale = _sell(conn, _cart(_line(item, price=3000), applied_coupon=dict(coupon)), 'card')
    assert sale.total_cents == 2500
    assert conn.execute('SELECT current_uses FROM pos_coupons WHERE id = ?', (cid,)).fetchone()[0] == 1
    red = conn.execute('SELECT discount_applied_cents, redeemed_by FROM pos_coupon_redemptions').fetchone()
    assert tuple(red) == (500, 2)


def test_order_number_counts_past_999(conn):
    today = date(2026, 1, 2)
    for n in ('999', '1000'):
        conn.execute("INSERT INTO pos_orders (order_number, status, subtotal_cents, tax_rate, tax_amount_cents, total_cents, "
                     "payment_method, operator_id) VALUES (?, 'completed', 0, 0, 0, 0, 'card', 1)",
                     (f'POS-20260102-{n}',))
    assert pos_checkout.next_order_number(conn, today) == 'POS-20260102-1001'
    assert pos_checkout.next_order_number(conn, date(2026, 1, 3)) == 'POS-20260103-001'


# --- refunds ---------------------------------------------------------------

def _two_line_sale(conn):
    """2 x $10 WIDGET + 1 x $20 GADGET, 7% tax: paid $42.80."""
    w = _item(conn, 'WIDGET', 1000, qty=10)
    g = _item(conn, 'GADGET', 2000, qty=10)
    sale = _sell(conn, _cart(_line(w, 'WIDGET', 1000, qty=2), _line(g, 'GADGET', 2000)), 'card', TAX7)
    lines = {r['sku']: r['id'] for r in conn.execute(
        'SELECT id, sku FROM pos_order_items WHERE order_id = ?', (sale.order_id,))}
    return sale, w, g, lines


def test_partial_refund_paid_share_tax_and_restock(conn):
    sale, w, g, lines = _two_line_sale(conn)
    res = pos_refunds.process_refund(conn, sale.order_id, 'partial', 'customer_changed_mind', MANAGER,
                                     items=[(str(lines['WIDGET']), '1')])
    assert res.amount_cents == 1070 and res.tax_refunded_cents == 70
    assert res.refund_method == 'card' and res.status == 'partial_refund'
    assert _qty(conn, w) == 9  # 10 - 2 sold + 1 restocked
    assert _ledger(conn, w)[-1] == (1, 'refund', '3', sale.order_number)
    assert not conn.in_transaction


def test_refund_caps_and_full_refund_takes_exactly_what_is_left(conn):
    sale, w, g, lines = _two_line_sale(conn)
    with pytest.raises(pos_refunds.OverRefund) as e:
        pos_refunds.process_refund(conn, sale.order_id, 'partial', 'defective', MANAGER,
                                   items=[(str(lines['GADGET']), '2')])
    assert e.value.message == 'Cannot refund 2 of that item — only 1 left to refund.'
    assert e.value.order_number == sale.order_number
    # defective: no restock by default
    pos_refunds.process_refund(conn, sale.order_id, 'partial', 'defective', MANAGER,
                               items=[(str(lines['GADGET']), '1')])
    assert _qty(conn, g) == 9
    res = pos_refunds.process_refund(conn, sale.order_id, 'full', 'other', MANAGER, refund_method='cash',
                                     restock_actions={str(lines['WIDGET']): 'restock'})
    total = conn.execute('SELECT SUM(amount_cents), SUM(tax_refunded_cents) FROM pos_refunds WHERE order_id = ?',
                         (sale.order_id,)).fetchone()
    assert tuple(total) == (4280, 280)
    assert res.status == 'refunded' and res.refund_method == 'cash'
    assert _qty(conn, w) == 10
    with pytest.raises(pos_refunds.NothingToRefund):
        pos_refunds.process_refund(conn, sale.order_id, 'full', 'other', MANAGER)
    with pytest.raises(pos_refunds.OrderNotFound):
        pos_refunds.process_refund(conn, 424242, 'full', 'other', MANAGER)
    with pytest.raises(pos_refunds.UnknownRefundMethod):
        pos_refunds.process_refund(conn, sale.order_id, 'full', 'other', MANAGER, refund_method='gold')


def test_refund_manager_falls_back_to_acting_user(conn):
    sale, w, g, lines = _two_line_sale(conn)
    pos_refunds.process_refund(conn, sale.order_id, 'partial', 'wrong_item', CASHIER,
                               items=[(str(lines['GADGET']), '1')], manager_username='manager')
    assert conn.execute('SELECT manager_id FROM pos_refunds').fetchone()[0] == 3
    assert _ledger(conn, g)[-1] == (1, 'refund', '3', sale.order_number)


# --- sale corrections / voids (the sale itself never changes) ------------------

def test_edit_sale_moves_stock_reprices_and_records_a_correction(conn):
    sale, w, g, lines = _two_line_sale(conn)
    before = [tuple(r) for r in conn.execute('SELECT * FROM pos_orders')]
    edits = [pos_sales.SaleLineEdit(str(lines['WIDGET']), '3', '10.00', 'WIDGET item')]
    res = pos_sales.edit_sale(conn, sale.order_id, edits, [str(lines['GADGET'])], MANAGER, ip='127.0.0.1')
    assert res.new_total_cents == 3210 and res.money_cents == -1070 and res.method == 'card'
    assert 'Removed: GADGET item x1' in res.changes and 'WIDGET item: qty 2 → 3' in res.changes
    assert _qty(conn, w) == 7 and _qty(conn, g) == 10
    assert _ledger(conn, w)[-1] == (-1, 'sale_edit', 'manager', f'{sale.order_number}: qty increased')
    assert _ledger(conn, g)[-1] == (1, 'sale_edit', 'manager', f'{sale.order_number}: item removed')
    # The checkout's payment stays; the difference is paid back on its own row
    assert _payments(conn, sale.order_id) == [('card', 4280, None, 0), ('card', -1070, None, 0)]
    assert [tuple(r) for r in conn.execute('SELECT * FROM pos_orders')] == before
    audit = conn.execute("SELECT details FROM pos_audit_log WHERE action = 'edit_sale'").fetchone()
    assert json.loads(audit[0])['new_total'] == 32.10   # the audit log stays human-readable dollars
    assert not conn.in_transaction


def test_edit_sale_refusals_change_nothing(conn):
    sale, w, g, lines = _two_line_sale(conn)
    with pytest.raises(pos_sales.InvalidSaleItems):
        pos_sales.edit_sale(conn, sale.order_id,
                            [pos_sales.SaleLineEdit(str(lines['WIDGET']), '0', '10', '')], [], MANAGER)
    with pytest.raises(pos_sales.OrderNotFound):
        pos_sales.edit_sale(conn, 99999, [], [], MANAGER)
    pos_refunds.process_refund(conn, sale.order_id, 'partial', 'defective', MANAGER,
                               items=[(str(lines['GADGET']), '1')])
    # the refunded gadget can't come off the sale (refund the rest instead)
    with pytest.raises(pos_sales.RefundedLine):
        pos_sales.edit_sale(conn, sale.order_id, [], [str(lines['GADGET'])], MANAGER)
    with pytest.raises(pos_sales.UnknownMethod):
        pos_sales.edit_sale(conn, sale.order_id, [], [str(lines['WIDGET'])], MANAGER, method='gold')
    assert _qty(conn, w) == 8 and not conn.in_transaction
    assert conn.execute('SELECT COUNT(*) FROM pos_sale_corrections').fetchone()[0] == 0


def test_void_restores_units_and_pays_back_per_tender(conn):
    sale, w, g, lines = _two_line_sale(conn)
    admin = SimpleNamespace(id=1, username='admin')
    res = pos_sales.void_order(conn, sale.order_number, True, admin, reason='rung up twice')
    assert res.restored_units == 3 and res.payments == {'card': -4280} and res.money_cents == -4280
    assert _qty(conn, w) == 10 and _qty(conn, g) == 10
    assert _ledger(conn, w)[-1] == (2, 'sale_void', '1', f'{sale.order_number}: sale voided')
    # The sale stays, with the void beside it
    for table, n in (('pos_orders', 1), ('pos_order_items', 2), ('pos_payments', 2),
                     ('pos_sale_corrections', 1)):
        assert conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == n
    with pytest.raises(pos_sales.SaleVoided):
        pos_sales.void_order(conn, sale.order_number, True, admin)
    with pytest.raises(pos_sales.OrderNotFound):
        pos_sales.void_order(conn, 'NOPE-1', True, admin)


def test_void_is_refused_once_part_of_the_sale_was_refunded(conn):
    sale, w, g, lines = _two_line_sale(conn)
    pos_refunds.process_refund(conn, sale.order_id, 'partial', 'customer_changed_mind', MANAGER,
                               items=[(str(lines['WIDGET']), '1')])
    with pytest.raises(pos_sales.SaleHasRefunds):
        pos_sales.void_order(conn, sale.order_number, True, SimpleNamespace(id=1, username='admin'))
    assert _qty(conn, w) == 9 and not conn.in_transaction


def test_item_sale_fields(conn):
    item = _item(conn, 'ONSALE', qty=3)
    assert pos_sales.start_item_sale(conn, 'ONSALE', 750, '', None, end_on_stock=True) == item
    row = conn.execute('SELECT sale_price_cents, sale_enabled, sale_end_on_stock, sale_start, quantity '
                       'FROM inventory_items WHERE id = ?', (item,)).fetchone()
    assert tuple(row) == (750, 1, 1, None, 3)
    pos_sales.stop_item_sale(conn, item)
    assert conn.execute('SELECT sale_enabled FROM inventory_items WHERE id = ?', (item,)).fetchone()[0] == 0
    with pytest.raises(inv.ItemNotFound):
        pos_sales.start_item_sale(conn, 'NOPE', 100)
