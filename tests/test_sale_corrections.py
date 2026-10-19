"""Finished sales are permanent (schema v23).

Edit Sale and Void record corrections beside a sale instead of changing it:

  * triggers refuse UPDATE/DELETE on every sales table, and no code path
    writes them that way;
  * each Edit Sale save is exactly one correction with the right line and
    money deltas, a payments-ledger row for the money that changed hands,
    stock movements -- and the sale's own rows unchanged;
  * Void reverses the whole sale (stock and payments back), once, and a
    voided sale can't be edited or refunded;
  * refunds and corrections never pay back more than the sale's net;
  * reports count a correction on the day it is made, so the sale's own day
    never changes; the engine, dashboard, EOD email and drawer agree;
  * migration 23 runs on a real v22 database.
"""
import os
import re
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from flask import template_rendered

from app.services import inventory as inv
from app.services import pos_checkout, pos_ledger, pos_refunds, pos_sales
from app.services.db import get_db_connection
from app.services.migration import SALE_IMMUTABLE_TABLES
from app.services.pos_order_state import STATUS_SQL, order_state
from app.services.pos_pricing import PricingConfig
from tests.conftest import login, sales_unlocked

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
CASHIER = SimpleNamespace(id=2, username='cashier')
MANAGER = SimpleNamespace(id=3, username='manager')
ADMIN = SimpleNamespace(id=1, username='admin')
TAX10 = PricingConfig(tax_rate=0.10)
TAX10_CASH5 = PricingConfig(tax_rate=0.10, cash_discount_enabled=True, cash_discount_percent=5,
                            cash_discount_type='percent')


@pytest.fixture(autouse=True)
def denver(monkeypatch):
    """Pin the store's zone where the OS lets us (see test_pos_dashboard)."""
    if not hasattr(time, 'tzset'):
        yield
        return
    monkeypatch.setenv('TZ', 'America/Denver')
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def conn(fresh_app):
    c = get_db_connection(fresh_app.config['DATABASE'])
    c.isolation_level = None  # autocommit, as the POS routes use it
    yield c
    c.close()


def _item(conn, sku, price, qty=10, buy=0):
    return inv.create_item(conn, {'sku': sku, 'name': f'{sku} item', 'quantity': qty,
                                  'sell_price_cents': price, 'buy_price_cents': buy}, user='admin')


def _line(item_id, sku, price, qty=1):
    return {'inventory_item_id': item_id, 'sku': sku, 'name': f'{sku} item', 'quantity': qty,
            'unit_price_cents': price, 'discount_type': None, 'discount_percent': 0, 'discount_cents': 0,
            'line_total_cents': price * qty}


def _sell(conn, lines, method='card', config=TAX10, coupon=None, **payment):
    cart = {'items': lines, 'discount_type': None, 'discount_percent': 0, 'discount_cents': 0,
            'discount_reason': ''}
    if coupon:
        cart['applied_coupon'] = coupon
    return pos_checkout.complete_sale(conn, pos_checkout.SessionCart(cart),
                                      pos_checkout.Payment(method, **payment), CASHIER, config=config)


def _qty(conn, item_id):
    return conn.execute('SELECT quantity FROM inventory_items WHERE id = ?', (item_id,)).fetchone()[0]


def _lines(conn, order_id):
    return {r['sku']: r['id'] for r in conn.execute('SELECT id, sku FROM pos_order_items WHERE order_id = ?',
                                                    (order_id,))}


def _snapshot(conn, order_id):
    """Every column of the sale's own rows, as stored."""
    return {
        'order': [tuple(r) for r in conn.execute('SELECT * FROM pos_orders WHERE id = ?', (order_id,))],
        'lines': [tuple(r) for r in conn.execute('SELECT * FROM pos_order_items WHERE order_id = ? ORDER BY id',
                                                 (order_id,))],
        'payments': [tuple(r) for r in conn.execute('SELECT * FROM pos_payments WHERE order_id = ? '
                                                    'AND correction_id IS NULL ORDER BY id', (order_id,))],
        'coupon': [tuple(r) for r in conn.execute('SELECT * FROM pos_coupon_redemptions WHERE order_id = ?',
                                                  (order_id,))],
    }


def _corrections(conn, order_id):
    return [dict(r) for r in conn.execute('SELECT * FROM pos_sale_corrections WHERE order_id = ? ORDER BY id',
                                          (order_id,))]


def _correction_payments(conn, order_id):
    return [(r['method'], r['amount_cents'], r['correction_id']) for r in conn.execute(
        'SELECT * FROM pos_payments WHERE order_id = ? AND correction_id IS NOT NULL ORDER BY id', (order_id,))]


def _edit(conn, sale, lines, removed=(), **kw):
    return pos_sales.edit_sale(conn, sale.order_id,
                               [pos_sales.SaleLineEdit(str(i), str(q), p, n) for i, q, p, n in lines],
                               [str(r) for r in removed], MANAGER, **kw)


def _utc_at_local(day, hour):
    local = datetime.combine(day, datetime.min.time()).replace(hour=hour).astimezone()
    return local.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


# --- the coupon / cash-discount sale used by the Edit Sale cases ----------------
# 2 x WIDGET $10 + 1 x GADGET $30 = $50.00; $5 coupon -> $45.00; 10% tax $4.50;
# paid cash with a 5% cash discount ($2.25): $47.25.

@pytest.fixture
def coupon_sale(conn):
    widget = _item(conn, 'WIDGET', 1000)
    gadget = _item(conn, 'GADGET', 3000)
    coupon_id = conn.execute('''INSERT INTO pos_coupons (name, code, coupon_type, discount_type, discount_cents)
                                VALUES ('SAVE5', 'SAVE5', 'standard', 'order_dollar', 500)''').lastrowid
    sale = _sell(conn, [_line(widget, 'WIDGET', 1000, 2), _line(gadget, 'GADGET', 3000)], 'cash', TAX10_CASH5,
                 coupon={'id': coupon_id, 'code': 'SAVE5', 'name': 'SAVE5', 'discount_cents': 500},
                 tendered='50')
    assert (sale.total_cents, sale.cash_discount_cents) == (4725, 225)
    return SimpleNamespace(sale=sale, widget=widget, gadget=gadget, lines=_lines(conn, sale.order_id))


EDIT_CASES = {
    # name: (edits(lines) -> (kept, removed), expected deltas, stock change {sku: delta}, qty after {sku: qty})
    'remove a line': (
        lambda ln: ([(ln['WIDGET'], 2, '10.00', 'WIDGET item')], [ln['GADGET']]),
        {'subtotal': -3000, 'discount': 0, 'coupon': -300, 'cash_discount': -135, 'tax': -270, 'total': -2835},
        {'GADGET': 1}, {'WIDGET': 2, 'GADGET': 0}),
    'reduce a quantity': (
        lambda ln: ([(ln['WIDGET'], 1, '10.00', 'WIDGET item'), (ln['GADGET'], 1, '30.00', 'GADGET item')], []),
        {'subtotal': -1000, 'discount': 0, 'coupon': -100, 'cash_discount': -45, 'tax': -90, 'total': -945},
        {'WIDGET': 1}, {'WIDGET': 1, 'GADGET': 1}),
    'add quantity': (
        lambda ln: ([(ln['WIDGET'], 3, '10.00', 'WIDGET item'), (ln['GADGET'], 1, '30.00', 'GADGET item')], []),
        # above the sale's subtotal the coupon and cash discount don't grow
        {'subtotal': 1000, 'discount': 0, 'coupon': 0, 'cash_discount': 0, 'tax': 100, 'total': 1100},
        {'WIDGET': -1}, {'WIDGET': 3, 'GADGET': 1}),
    'change a price': (
        lambda ln: ([(ln['WIDGET'], 2, '10.00', 'WIDGET item'), (ln['GADGET'], 1, '25.00', 'GADGET item')], []),
        # coupon 500 * 4500/5000 = 450; cash discount 225 * 0.9 = 202.5 -> 203 (half-up)
        {'subtotal': -500, 'discount': 0, 'coupon': -50, 'cash_discount': -22, 'tax': -45, 'total': -473},
        {}, {'WIDGET': 2, 'GADGET': 1}),
}


@pytest.mark.parametrize('case', list(EDIT_CASES))
def test_each_edit_is_one_correction_and_leaves_the_sale_alone(conn, coupon_sale, case):
    edits, deltas, stock, qty_after = EDIT_CASES[case]
    sale, lines = coupon_sale.sale, coupon_sale.lines
    before = _snapshot(conn, sale.order_id)
    stock_before = {'WIDGET': _qty(conn, coupon_sale.widget), 'GADGET': _qty(conn, coupon_sale.gadget)}
    movements_before = conn.execute('SELECT MAX(id) FROM inventory_movements').fetchone()[0]

    kept, removed = edits(lines)
    res = _edit(conn, sale, kept, removed, reason='customer changed order')

    # Exactly one correction, with the money deltas
    corrections = _corrections(conn, sale.order_id)
    assert len(corrections) == 1
    c = corrections[0]
    assert {k: c[f'{k}_delta_cents'] for k in deltas} == deltas
    assert (c['kind'], c['user_id'], c['user_name'], c['reason']) == ('edit', 3, 'manager', 'customer changed order')
    assert res.correction_id == c['id'] and res.money_cents == deltas['total']
    assert res.new_total_cents == 4725 + deltas['total']
    # The money changed hands in cash (a cash sale), dated with the correction
    assert _correction_payments(conn, sale.order_id) == [('cash', deltas['total'], c['id'])]
    assert c['method'] == 'cash'
    paid_at = conn.execute('SELECT created_at FROM pos_payments WHERE correction_id = ?', (c['id'],)).fetchone()[0]
    assert paid_at == c['created_at']

    # Per-line changes; their shares add up to the correction's own deltas
    items = {r['order_item_id']: dict(r) for r in conn.execute(
        'SELECT * FROM pos_sale_correction_items WHERE correction_id = ?', (c['id'],))}
    assert sum(i['line_total_delta_cents'] for i in items.values()) == deltas['subtotal']
    assert sum(i['revenue_delta_cents'] for i in items.values()) == deltas['total'] - deltas['tax']
    assert sum(i['tax_delta_cents'] for i in items.values()) == deltas['tax']
    state = order_state(conn, sale.order_id)
    assert {ln['sku']: ln['quantity'] for ln in state['lines']} == qty_after
    assert state['net']['total_cents'] == 4725 + deltas['total']
    assert state['net']['coupon_cents'] == 500 + deltas['coupon']

    # Stock moved by the quantity change, referencing the order
    for sku, item in (('WIDGET', coupon_sale.widget), ('GADGET', coupon_sale.gadget)):
        assert _qty(conn, item) == stock_before[sku] + stock.get(sku, 0)
    moves = conn.execute('SELECT reason, ref_type, ref_id FROM inventory_movements WHERE id > ?',
                         (movements_before,)).fetchall()
    assert len(moves) == len(stock)
    assert all(tuple(m) == ('sale_edit', 'order', sale.order_id) for m in moves)

    # ... and the sale itself is exactly as it was rung up
    assert _snapshot(conn, sale.order_id) == before


def test_name_only_edit_moves_no_money_and_nothing_changed_records_nothing(conn, coupon_sale):
    sale, ln = coupon_sale.sale, coupon_sale.lines
    same = [(ln['WIDGET'], 2, '10.00', 'WIDGET item'), (ln['GADGET'], 1, '30.00', 'GADGET item')]
    res = _edit(conn, sale, same)
    assert res.changes == [] and res.correction_id is None
    assert _corrections(conn, sale.order_id) == []

    res = _edit(conn, sale, [(ln['WIDGET'], 2, '10.00', 'Blue widget'), (ln['GADGET'], 1, '30.00', 'GADGET item')])
    assert res.changes == ['Renamed: WIDGET item → Blue widget'] and res.money_cents == 0
    assert _correction_payments(conn, sale.order_id) == []
    c = _corrections(conn, sale.order_id)[0]
    assert (c['total_delta_cents'], c['method']) == (0, None)
    state = order_state(conn, sale.order_id)
    assert [ln['name'] for ln in state['lines']] == ['Blue widget', 'GADGET item']


def test_edits_start_from_the_net_state_and_stack(conn, coupon_sale):
    sale, ln = coupon_sale.sale, coupon_sale.lines
    _edit(conn, sale, [(ln['WIDGET'], 1, '10.00', 'WIDGET item'), (ln['GADGET'], 1, '30.00', 'GADGET item')])
    # The second edit sees qty 1 (not the 2 rung up); together they equal one big edit
    _edit(conn, sale, [(ln['WIDGET'], 1, '10.00', 'WIDGET item')], [ln['GADGET']])
    state = order_state(conn, sale.order_id)
    assert {x['sku']: x['quantity'] for x in state['lines']} == {'WIDGET': 1, 'GADGET': 0}
    adj = pos_sales.sale_adjustments(conn, state['order'])
    assert state['net']['total_cents'] == pos_sales.reprice_parts(1000, adj)['total'] == 945
    assert sum(p[1] for p in _correction_payments(conn, sale.order_id)) == 945 - 4725
    assert len(state['live_lines']) == 1


def test_edit_sale_route_flashes_what_changes_hands(conn, coupon_sale, fresh_client):
    sale, ln = coupon_sale.sale, coupon_sale.lines
    client = login(fresh_client, 3)
    page = client.get(f'/pos/management/edit-sale/{sale.order_id}').get_data(as_text=True)
    assert 'name="sale_date"' not in page and 'correction_method' in page
    client.post(f'/pos/management/edit-sale/{sale.order_id}', data={
        'item_id': [str(ln['WIDGET'])], 'item_quantity': ['2'], 'item_price': ['10.00'],
        'item_name': ['WIDGET item'], 'deleted_items': [str(ln['GADGET'])],
        'correction_method': 'card', 'reason': 'wrong item', 'sale_date': '2001-01-01'})
    with client.session_transaction() as sess:
        flashes = [m for _cat, m in sess['_flashes']]
    assert any('Pay the customer back $28.35 (Card)' in m for m in flashes), flashes
    assert _correction_payments(conn, sale.order_id)[0][:2] == ('card', -2835)
    # the sale keeps its date
    assert conn.execute('SELECT created_at FROM pos_orders WHERE id = ?', (sale.order_id,)).fetchone()[0] \
        == order_state(conn, sale.order_id)['order']['created_at']
    # The page now starts from the corrected sale
    page = client.get(f'/pos/management/edit-sale/{sale.order_id}').get_data(as_text=True)
    assert 'GADGET item' not in page.split('id="itemRows"')[1].split('totals-section')[0]
    assert '$18.90' in page


# --- triggers and the source scan ------------------------------------------------

def test_triggers_refuse_update_and_delete_on_finished_sales(conn, coupon_sale):
    sale, ln = coupon_sale.sale, coupon_sale.lines
    _edit(conn, sale, [(ln['WIDGET'], 1, '10.00', 'WIDGET item'), (ln['GADGET'], 1, '30.00', 'GADGET item')])
    pos_refunds.process_refund(conn, sale.order_id, 'partial', 'other', MANAGER, items=[(str(ln['GADGET']), '1')])
    statements = [
        ('UPDATE pos_orders SET total_cents = 1 WHERE id = ?', sale.order_id),
        ("UPDATE pos_orders SET status = 'refunded' WHERE id = ?", sale.order_id),
        ("UPDATE pos_orders SET created_at = '2001-01-01 00:00:00' WHERE id = ?", sale.order_id),
        ('UPDATE pos_order_items SET quantity = 9 WHERE order_id = ?', sale.order_id),
        ('UPDATE pos_payments SET amount_cents = 1 WHERE order_id = ?', sale.order_id),
        ('UPDATE pos_coupon_redemptions SET discount_applied_cents = 1 WHERE order_id = ?', sale.order_id),
        ('UPDATE pos_refunds SET amount_cents = 1 WHERE order_id = ?', sale.order_id),
        ('UPDATE pos_sale_corrections SET total_delta_cents = 0 WHERE order_id = ?', sale.order_id),
        ('UPDATE pos_sale_correction_items SET qty_delta = 0 WHERE correction_id > ?', 0),
        ('UPDATE pos_refund_items SET quantity = 0 WHERE refund_id > ?', 0),
    ] + [(f'DELETE FROM {t}', None) for t in SALE_IMMUTABLE_TABLES]
    for sql, arg in statements:
        with pytest.raises(sqlite3.DatabaseError, match='permanent'):
            conn.execute(sql, () if arg is None else (arg,))
    for t in SALE_IMMUTABLE_TABLES:
        assert conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] > 0, t


def _python_sources():
    for top in ('app', 'scripts'):
        for dirpath, _dirs, files in os.walk(os.path.join(ROOT, top)):
            for name in files:
                if name.endswith('.py'):
                    path = os.path.join(dirpath, name)
                    with open(path, encoding='utf-8') as f:
                        yield os.path.relpath(path, ROOT).replace(os.sep, '/'), f.read()


def test_no_code_path_updates_or_deletes_a_finished_sale():
    tables = '|'.join(SALE_IMMUTABLE_TABLES)
    writes = re.compile(r'\b(UPDATE(\s+OR\s+\w+)?|DELETE\s+FROM|REPLACE\s+INTO|INSERT\s+OR\s+REPLACE\s+INTO)'
                        rf'\s+["`]?({tables})\b', re.I)
    found = sorted({path for path, src in _python_sources() if writes.search(src)})
    # Only the schema migrations (which ran before the sales became permanent)
    assert found == ['app/services/migration.py']


# --- void --------------------------------------------------------------------------

def test_void_reverses_everything_once(conn, fresh_client):
    widget = _item(conn, 'WIDGET', 1000)
    cable = _item(conn, 'CABLE', 2000)
    sale = _sell(conn, [_line(widget, 'WIDGET', 1000, 2), _line(cable, 'CABLE', 2000)], 'split', TAX10,
                 cash_amount='20', card_amounts=['24'])
    assert sale.total_cents == 4400
    before = _snapshot(conn, sale.order_id)
    res = pos_sales.void_order(conn, sale.order_number, True, ADMIN, reason='rung up twice')
    assert res.payments == {'card': -2400, 'cash': -2000} and res.restored_units == 3
    assert _qty(conn, widget) == 10 and _qty(conn, cable) == 10
    c = _corrections(conn, sale.order_id)[0]
    assert (c['kind'], c['total_delta_cents'], c['tax_delta_cents'], c['subtotal_delta_cents'], c['method']) == \
        ('void', -4400, -400, -4000, 'split')
    assert sorted(p[:2] for p in _correction_payments(conn, sale.order_id)) == [('card', -2400), ('cash', -2000)]
    assert _snapshot(conn, sale.order_id) == before

    state = order_state(conn, sale.order_id)
    assert state['voided'] and state['status'] == 'voided' and state['net']['total_cents'] == 0
    assert state['refundable_cents'] == 0 and all(ln['quantity'] == 0 for ln in state['lines'])
    movements = conn.execute("SELECT reason, ref_type, ref_id FROM inventory_movements "
                             "WHERE reason = 'sale_void'").fetchall()
    assert [tuple(m) for m in movements] == [('sale_void', 'order', sale.order_id)] * 2

    # Can't void twice, edit or refund a voided sale
    with pytest.raises(pos_sales.SaleVoided):
        pos_sales.void_order(conn, sale.order_number, True, ADMIN)
    with pytest.raises(pos_sales.SaleVoided):
        pos_sales.edit_sale(conn, sale.order_id, [], [], MANAGER)
    with pytest.raises(pos_refunds.SaleVoided):
        pos_refunds.process_refund(conn, sale.order_id, 'full', 'other', MANAGER)
    # ... and the database agrees: one void per sale
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO pos_sale_corrections (order_id, kind, user_id, user_name) "
                     "VALUES (?, 'void', 1, 'admin')", (sale.order_id,))
    assert len(_corrections(conn, sale.order_id)) == 1

    # The sale shows as voided wherever it is listed or opened
    client = login(fresh_client, 1)
    receipt = client.get(f'/pos/receipt/{sale.order_number}').get_data(as_text=True)
    assert 'Sale Voided' in receipt and 'rung up twice' in receipt and 'Void Sale' not in receipt
    assert '*** VOIDED ***' in client.get(f'/pos/receipt/{sale.order_number}/print').get_data(as_text=True)
    history = client.get('/pos/management/sales', query_string={'status': 'voided'}).get_data(as_text=True)
    assert sale.order_number in history and f'/pos/management/edit-sale/{sale.order_id}' not in history
    refund_page = client.get(f'/pos/refunds/order/{sale.order_number}').get_data(as_text=True)
    assert 'was voided' in refund_page and 'refundForm' not in refund_page
    resp = client.get(f'/pos/management/edit-sale/{sale.order_id}')
    assert resp.status_code == 302


def test_void_route_without_restock_keeps_stock(conn, fresh_client):
    widget = _item(conn, 'WIDGET', 1000)
    sale = _sell(conn, [_line(widget, 'WIDGET', 1000, 2)], 'cash', TAX10, tendered='30')
    client = login(fresh_client, 1)
    client.post(f'/pos/order/{sale.order_number}/void', data={'reason': 'test'})
    assert _qty(conn, widget) == 8
    assert order_state(conn, sale.order_id)['voided']
    with client.session_transaction() as sess:
        assert any('Pay the customer back $22.00 cash' in m for _c, m in sess['_flashes'])


# --- refunds and corrections never pay back more than the net -------------------

def _plain_sale(conn):
    widget = conn.execute("SELECT id FROM inventory_items WHERE sku = 'WIDGET'").fetchone()
    widget = widget[0] if widget else _item(conn, 'WIDGET', 1000)
    gadget = conn.execute("SELECT id FROM inventory_items WHERE sku = 'GADGET'").fetchone()
    gadget = gadget[0] if gadget else _item(conn, 'GADGET', 3000)
    sale = _sell(conn, [_line(widget, 'WIDGET', 1000, 2), _line(gadget, 'GADGET', 3000)], 'card', TAX10)
    assert sale.total_cents == 5500
    return sale, _lines(conn, sale.order_id)


def test_refund_after_a_correction_is_capped_by_the_corrected_sale(conn):
    sale, ln = _plain_sale(conn)
    _edit(conn, sale, [(ln['WIDGET'], 2, '10.00', 'WIDGET item')], [ln['GADGET']])   # -$33.00 to card
    with pytest.raises(pos_refunds.OverRefund):
        pos_refunds.process_refund(conn, sale.order_id, 'partial', 'other', MANAGER,
                                   items=[(str(ln['GADGET']), '1')])
    one = pos_refunds.process_refund(conn, sale.order_id, 'partial', 'other', MANAGER,
                                     items=[(str(ln['WIDGET']), '1')])
    assert one.amount_cents == 1100 and one.tax_refunded_cents == 100
    rest = pos_refunds.process_refund(conn, sale.order_id, 'full', 'other', MANAGER)
    assert rest.amount_cents == 1100 and rest.status == 'refunded'
    state = order_state(conn, sale.order_id)
    assert state['refunded_cents'] == state['net']['total_cents'] == 2200
    assert state['refunded_tax_cents'] == state['net']['tax_cents'] == 200
    # Paid 55.00, got back 33.00 (correction) + 22.00 (refunds): never more than was paid
    paid_back = -sum(p[1] for p in _correction_payments(conn, sale.order_id)) + state['refunded_cents']
    assert paid_back == 5500 and state['status'] == 'refunded'
    with pytest.raises(pos_refunds.NothingToRefund):
        pos_refunds.process_refund(conn, sale.order_id, 'full', 'other', MANAGER)


def test_correction_after_a_refund_keeps_refunded_units_and_money(conn):
    sale, ln = _plain_sale(conn)
    pos_refunds.process_refund(conn, sale.order_id, 'partial', 'customer_changed_mind', MANAGER,
                               items=[(str(ln['WIDGET']), '1')])                     # $11.00 back
    with pytest.raises(pos_sales.RefundedLine, match='already refunded'):
        _edit(conn, sale, [(ln['GADGET'], 1, '30.00', 'GADGET item')], [ln['WIDGET']])
    with pytest.raises(pos_sales.RefundedLine, match="price can't be changed"):
        _edit(conn, sale, [(ln['WIDGET'], 2, '5.00', 'WIDGET item')])
    res = _edit(conn, sale, [(ln['WIDGET'], 2, '10.00', 'WIDGET item')], [ln['GADGET']])
    assert res.new_total_cents == 2200 and res.money_cents == -3300
    state = order_state(conn, sale.order_id)
    assert state['refundable_cents'] == 1100 and state['refundable_units'] == 1
    rest = pos_refunds.process_refund(conn, sale.order_id, 'full', 'other', MANAGER)
    assert rest.amount_cents == 1100
    assert order_state(conn, sale.order_id)['refundable_cents'] == 0


def test_correction_can_not_go_below_what_was_refunded(conn):
    widget = _item(conn, 'WIDGET', 1000)
    gadget = _item(conn, 'GADGET', 3000)
    cart_lines = [_line(widget, 'WIDGET', 1000), _line(gadget, 'GADGET', 3000)]
    cart = {'items': cart_lines, 'discount_type': 'fixed', 'discount_percent': 0, 'discount_cents': 1000,
            'discount_reason': 'loyal'}
    sale = pos_checkout.complete_sale(conn, pos_checkout.SessionCart(cart), pos_checkout.Payment('card'),
                                      CASHIER, config=TAX10)
    assert sale.total_cents == 3300   # 40 - 10 + 10% tax
    ln = _lines(conn, sale.order_id)
    r = pos_refunds.process_refund(conn, sale.order_id, 'partial', 'other', MANAGER, items=[(str(ln['GADGET']), '1')])
    assert r.amount_cents == 2475     # 30 x 33/40
    # Taking the widget off would leave $22.00 (the $10 discount stays): below the $24.75 refunded
    with pytest.raises(pos_sales.BelowRefunded):
        _edit(conn, sale, [(ln['GADGET'], 1, '30.00', 'GADGET item')], [ln['WIDGET']])
    assert _corrections(conn, sale.order_id) == []


def test_status_sql_matches_the_net_view(conn, fresh_client):
    _plain_sale(conn)                                # untouched
    b = _plain_sale(conn)                            # partly refunded
    pos_refunds.process_refund(conn, b[0].order_id, 'partial', 'other', MANAGER, items=[(str(b[1]['WIDGET']), '1')])
    c = _plain_sale(conn)                            # corrected, then the rest refunded
    _edit(conn, c[0], [(c[1]['WIDGET'], 2, '10.00', 'WIDGET item')], [c[1]['GADGET']])
    pos_refunds.process_refund(conn, c[0].order_id, 'full', 'other', MANAGER)
    d = _plain_sale(conn)                            # voided
    pos_sales.void_order(conn, d[0].order_number, True, ADMIN)
    rows = conn.execute(f'SELECT o.id, {STATUS_SQL} AS status FROM pos_orders o ORDER BY o.id').fetchall()
    assert [r['status'] for r in rows] == ['completed', 'partial_refund', 'refunded', 'voided']
    assert [order_state(conn, r['id'])['status'] for r in rows] == [r['status'] for r in rows]
    html = login(fresh_client, 3).get('/pos/management/sales').get_data(as_text=True)
    assert '$22.00' in html and 'corrected' in html   # sale C's total after the correction


# --- receipts show the original, then each change, then the net ------------------

def test_receipt_and_reprint_show_original_changes_and_net(conn, coupon_sale, fresh_client):
    sale, ln = coupon_sale.sale, coupon_sale.lines
    _edit(conn, sale, [(ln['WIDGET'], 2, '10.00', 'WIDGET item')], [ln['GADGET']], reason='no gadget')
    pos_refunds.process_refund(conn, sale.order_id, 'partial', 'other', MANAGER, items=[(str(ln['WIDGET']), '1')])
    client = login(fresh_client, 2)
    for url in (f'/pos/receipt/{sale.order_number}', f'/pos/receipt/{sale.order_number}/print'):
        html = client.get(url).get_data(as_text=True)
        original = html.index('$47.25')                       # the sale as rung up
        changes = html.index('Changes after the sale')
        assert original < changes
        assert 'Sale corrected' in html and 'manager' in html and 'no gadget' in html
        assert 'removed (was 1)' in html and '-$28.35' in html and 'Paid back to customer (Cash)' in html
        assert 'Refund' in html and '-$9.45' in html          # 1 of 2 widgets: half of $18.90
        assert 'Sale total now' in html and '$18.90' in html
        assert 'Net paid' in html and '$9.45' in html


def test_refund_page_counts_from_the_net(conn, fresh_client):
    sale, ln = _plain_sale(conn)
    _edit(conn, sale, [(ln['WIDGET'], 2, '10.00', 'WIDGET item')], [ln['GADGET']])
    html = login(fresh_client, 3).get(f'/pos/refunds/order/{sale.order_number}').get_data(as_text=True)
    assert 'Process FULL refund of $ 22.00?' in html and 'Left to refund' in html
    assert f'name="quantity_{ln["GADGET"]}"' not in html


# --- reports: a correction counts on its own day --------------------------------

def _render(app, client, path, template, **params):
    captured = []

    def record(sender, template, context, **extra):
        captured.append((template.name, context))
    template_rendered.connect(record, app)
    try:
        resp = client.get(path, query_string=params)
    finally:
        template_rendered.disconnect(record, app)
    assert resp.status_code == 200
    return next(c for name, c in captured if name == template)


def test_correction_on_a_later_day_counts_on_that_day_only(fresh_app, fresh_client, conn, coupon_sale):
    from app.services.pos_email import build_eod_email
    from app.services.pos_reports import build_sales_report, daily_sales_series, generate_top_sellers_data
    sale, ln = coupon_sale.sale, coupon_sale.lines
    today = date.today()
    yesterday = today - timedelta(days=1)
    with sales_unlocked(conn):    # the sale was rung up yesterday at noon
        at = _utc_at_local(yesterday, 12)
        conn.execute('UPDATE pos_orders SET created_at = ? WHERE id = ?', (at, sale.order_id))
        conn.execute('UPDATE pos_payments SET created_at = ? WHERE order_id = ?', (at, sale.order_id))
    with fresh_app.app_context():
        day_before = build_sales_report(yesterday, yesterday)
        top_before = generate_top_sellers_data(yesterday, yesterday, {})
    assert day_before['sales_total_cents'] == 4725

    drawer = pos_ledger.open_session(conn, 3, 10000)
    _edit(conn, sale, [(ln['WIDGET'], 2, '10.00', 'WIDGET item')], [ln['GADGET']])   # -$28.35 cash, today

    with fresh_app.app_context():
        day_after = build_sales_report(yesterday, yesterday)
        top_after = generate_top_sellers_data(yesterday, yesterday, {})
        r = build_sales_report(today, today)
        both = build_sales_report(yesterday, today)
        series = daily_sales_series(yesterday, today)
        top_today = generate_top_sellers_data(today, today, {})
    # Yesterday is exactly as it was (drawer sessions aside: one was opened today)
    strip = lambda rep: {k: v for k, v in rep.items() if k != 'drawer_sessions'}   # noqa: E731
    assert strip(day_after) == strip(day_before)
    assert top_after['summary'] == top_before['summary']

    # Today: no sales, one correction of -$28.35 (-$2.70 tax), paid back in cash
    assert r['order_count'] == 0 and r['sales_total_cents'] == 0
    assert r['corrections'] == {'count': 1, 'voids': 0, 'subtotal_cents': -3000, 'discount_cents': 0,
                                'coupon_cents': -300, 'cash_discount_cents': -135, 'tax_cents': -270,
                                'total_cents': -2835, 'net_cents': -2565, 'by_method_cents': {'cash': -2835}}
    assert r['net_revenue_cents'] == -2835 and r['tax_net_cents'] == -270
    assert r['net_sales_after_refunds_cents'] == -2565
    assert [(t['method'], t['collected_cents'], t['corrected_cents'], t['refunded_cents'], t['net_cents'])
            for t in r['tenders']] == [('cash', 0, -2835, 0, -2835)]
    assert r['expected_cash']['cash_corrections_cents'] == -2835
    assert r['expected_cash']['total_cents'] == 10000 - 2835
    # The drawer's expected cash includes the money paid back
    figures = pos_ledger.session_figures(conn, conn.execute('SELECT * FROM drawer_sessions WHERE id = ?',
                                                            (drawer,)).fetchone())
    assert figures['cash_corrections_cents'] == -2835 and figures['expected_cash_cents'] == 10000 - 2835
    # Over both days the sale nets out to what the customer finally paid
    assert both['net_revenue_cents'] == 4725 - 2835 == order_state(conn, sale.order_id)['net']['total_cents']
    assert [(s['day'], s['revenue_cents'], s['corrections_cents']) for s in series] == [
        (yesterday.isoformat(), 4725, 0), (today.isoformat(), 0, -2835)]
    # Top Sellers: the gadget comes off today, and today's item figures add up to the correction
    gadget = next(i for i in top_today['items'] if i['sku'] == 'GADGET')
    assert gadget['qty_sold'] == -1 and gadget['net_qty'] == -1 and gadget['orders'] == 0
    assert round(top_today['summary']['gross_revenue_cents']) == r['corrections']['net_cents']
    assert round(top_today['summary']['tax_cents']) == r['corrections']['tax_cents']

    # Dashboard and EOD email show the engine's numbers for both days
    login(fresh_client, 3)
    for day, engine in ((yesterday, day_after), (today, r)):
        dash = _render(fresh_app, fresh_client, '/pos/management', 'pos/management.html',
                       start_date=day.isoformat(), end_date=day.isoformat())
        with fresh_app.test_request_context():
            _subject, html, data = build_eod_email(day)
        assert strip(dash['report']) == strip(engine) == strip(data['report'])
    with fresh_app.test_request_context():
        _subject, html, _data = build_eod_email(today)
    assert 'Sale corrections (1)' in html and '$-28.35' in html
    page = fresh_client.get('/pos/management/reports/daily', query_string={'date': today.isoformat()})
    assert 'Sale corrections (1' in page.get_data(as_text=True)


def test_void_on_a_later_day_and_daily_items(fresh_app, fresh_client, conn):
    from app.services.pos_reports import build_sales_report
    widget = _item(conn, 'WIDGET', 1000)
    sale = _sell(conn, [_line(widget, 'WIDGET', 1000, 2)], 'card', TAX10)
    today = date.today()
    yesterday = today - timedelta(days=1)
    with sales_unlocked(conn):
        at = _utc_at_local(yesterday, 12)
        conn.execute('UPDATE pos_orders SET created_at = ? WHERE id = ?', (at, sale.order_id))
        conn.execute('UPDATE pos_payments SET created_at = ? WHERE order_id = ?', (at, sale.order_id))
    pos_sales.void_order(conn, sale.order_number, True, ADMIN)
    with fresh_app.app_context():
        y = build_sales_report(yesterday, yesterday)
        t = build_sales_report(today, today)
    assert (y['order_count'], y['sales_total_cents'], y['net_revenue_cents']) == (1, 2200, 2200)
    assert (t['corrections']['count'], t['corrections']['voids'], t['net_revenue_cents']) == (1, 1, -2200)
    assert t['tender_net_cents'] == {'card': -2200}
    html = login(fresh_client, 3).get('/pos/management/reports/daily-items',
                                      query_string={'date': today.isoformat()}).get_data(as_text=True)
    assert 'WIDGET' in html and '-2' in html


# --- migration 23 on a real v22 database ------------------------------------------

def test_migration_23_from_a_v22_database(tmp_path):
    from app.services import migration
    db = str(tmp_path / 'v22.db')
    migration.run_migrations(db, target=22)
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys = ON')
    assert not c.execute("SELECT 1 FROM sqlite_master WHERE name = 'pos_sale_corrections'").fetchone()
    c.execute("INSERT INTO users (id, username, password_hash, roles) VALUES (1, 'admin', 'x', '[\"super_admin\"]')")
    c.execute("INSERT INTO inventory_items (id, sku, name, quantity, sell_price_cents) VALUES (1, 'W', 'W', 5, 1000)")
    # A sale as 2.x/3.0 recorded it, and one edited in place back then (parts don't add up)
    for number, total in (('OLD-1', 2200), ('OLD-2', 1500)):
        oid = c.execute('''INSERT INTO pos_orders (order_number, status, subtotal_cents, tax_rate, tax_amount_cents,
                                                   total_cents, payment_method, payment_details, operator_id)
                           VALUES (?, 'completed', 2000, 0.1, 200, ?, 'cash', '{}', 1)''',
                        (number, total)).lastrowid
        c.execute('''INSERT INTO pos_order_items (order_id, inventory_item_id, sku, name, quantity, unit_price_cents,
                                                  line_total_cents) VALUES (?, 1, 'W', 'W', 2, 1000, 2000)''', (oid,))
        c.execute("INSERT INTO pos_payments (order_id, method, amount_cents) VALUES (?, 'cash', ?)", (oid, total))
    c.commit()
    before = [tuple(r) for r in c.execute('SELECT * FROM pos_orders ORDER BY id')]
    c.close()

    migration.run_migrations(db)
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys = ON')
    try:
        assert c.execute('PRAGMA user_version').fetchone()[0] == migration.LATEST_SCHEMA_VERSION >= 23
        assert [tuple(r) for r in c.execute('SELECT * FROM pos_orders ORDER BY id')] == before
        cols = [r[1] for r in c.execute('PRAGMA table_info(pos_payments)')]
        assert 'correction_id' in cols
        assert all(r['correction_id'] is None for r in c.execute('SELECT correction_id FROM pos_payments'))
        triggers = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
        for t in SALE_IMMUTABLE_TABLES:
            assert {f'trg_{t}_no_update', f'trg_{t}_no_delete'} <= triggers
        with pytest.raises(sqlite3.DatabaseError, match='permanent'):
            c.execute("UPDATE pos_orders SET total_cents = 0 WHERE order_number = 'OLD-1'")
        with pytest.raises(sqlite3.DatabaseError, match='permanent'):
            c.execute("DELETE FROM pos_payments")
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []
        # Old sales are corrected like new ones (the in-place-edited one keeps its odd total)
        c.isolation_level = None
        old2 = c.execute("SELECT id FROM pos_orders WHERE order_number = 'OLD-2'").fetchone()[0]
        line = c.execute('SELECT id FROM pos_order_items WHERE order_id = ?', (old2,)).fetchone()[0]
        res = pos_sales.edit_sale(c, old2, [pos_sales.SaleLineEdit(str(line), '1', '10.00', 'W')], [], ADMIN)
        assert res.money_cents == -1100 and res.new_total_cents == 400
        assert order_state(c, old2)['status'] == 'completed'
        assert order_state(c, old2)['original']['total_cents'] == 1500
    finally:
        c.close()
    migration.run_migrations(db)   # already current: a no-op
