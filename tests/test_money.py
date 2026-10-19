"""Money-path tests for v3.0: checkout, refunds, sale edits, order numbers."""
import json
from datetime import date

import pytest

from tests.conftest import login


def _db(app):
    from app.services.db import get_db_connection
    return get_db_connection(app.config['DATABASE'])


def _set_tax(app, rate):
    from app.services import settings
    with app.app_context():
        settings.set('TAX_RATE', rate)


def _add_item(app, sku='WIDGET', price=1000, qty=10):
    conn = _db(app)
    cur = conn.execute('INSERT INTO inventory_items (sku, name, sell_price_cents, quantity) VALUES (?, ?, ?, ?)',
                       (sku, f'{sku} item', price, qty))
    conn.commit()
    item_id = cur.lastrowid
    conn.close()
    return item_id


def _put_cart(client, item_id, price=1000, qty=1, sku='WIDGET'):
    with client.session_transaction() as sess:
        sess['pos_cart'] = {
            'items': [{'inventory_item_id': item_id, 'sku': sku, 'name': f'{sku} item',
                       'quantity': qty, 'unit_price_cents': price, 'discount_type': None,
                       'discount_percent': 0, 'discount_cents': 0, 'line_total_cents': price * qty}],
            'discount_type': None, 'discount_percent': 0, 'discount_cents': 0, 'discount_reason': '',
        }


def _orders(app):
    conn = _db(app)
    rows = conn.execute('SELECT * FROM pos_orders ORDER BY id').fetchall()
    conn.close()
    return rows


# --- checkout ---------------------------------------------------------------

def test_checkout_rejects_short_cash(fresh_app, fresh_client):
    _set_tax(fresh_app, 0.07)
    item = _add_item(fresh_app)
    login(fresh_client, 2)
    _put_cart(fresh_client, item)
    fresh_client.post('/pos/checkout/process', data={'payment_method': 'cash', 'tendered': '5.00'})
    assert _orders(fresh_app) == []


def test_checkout_records_once_per_token(fresh_app, fresh_client):
    _set_tax(fresh_app, 0.07)
    item = _add_item(fresh_app)
    login(fresh_client, 2)
    form = {'payment_method': 'card', 'checkout_token': 'tok-1', 'expected_total': '10.70'}
    _put_cart(fresh_client, item)
    fresh_client.post('/pos/checkout/process', data=form)
    _put_cart(fresh_client, item)  # cart restored, as if the clear had failed
    fresh_client.post('/pos/checkout/process', data=form)
    orders = _orders(fresh_app)
    assert len(orders) == 1
    assert orders[0]['total_cents'] == 1070
    conn = _db(fresh_app)
    assert conn.execute('SELECT quantity FROM inventory_items WHERE id = ?', (item,)).fetchone()[0] == 9
    conn.close()


def test_checkout_refuses_when_cart_changed(fresh_app, fresh_client):
    _set_tax(fresh_app, 0.07)
    item = _add_item(fresh_app)
    login(fresh_client, 2)
    _put_cart(fresh_client, item, qty=2)  # $21.40 now, page showed $10.70
    fresh_client.post('/pos/checkout/process',
                      data={'payment_method': 'card', 'expected_total': '10.70'})
    assert _orders(fresh_app) == []


def test_split_payment_must_cover_total(fresh_app, fresh_client):
    _set_tax(fresh_app, 0.0)
    item = _add_item(fresh_app)
    login(fresh_client, 2)
    _put_cart(fresh_client, item)
    fresh_client.post('/pos/checkout/process',
                      data={'payment_method': 'split', 'cash_amount': '3', 'card_amount': ['4']})
    assert _orders(fresh_app) == []
    _put_cart(fresh_client, item)
    fresh_client.post('/pos/checkout/process',
                      data={'payment_method': 'split', 'cash_amount': '6', 'card_amount': ['4']})
    assert len(_orders(fresh_app)) == 1


def test_order_numbers_count_past_999(fresh_app):
    from app.routes.pos.core import generate_order_number
    prefix = f"POS-{date.today().strftime('%Y%m%d')}"
    conn = _db(fresh_app)
    for n in ('998', '999', '1000'):
        conn.execute("INSERT INTO pos_orders (order_number, status, subtotal_cents, tax_rate, tax_amount_cents, total_cents, "
                     "payment_method, operator_id) VALUES (?, 'completed', 0, 0, 0, 0, 'card', 1)",
                     (f'{prefix}-{n}',))
    conn.commit()
    assert generate_order_number(conn=conn) == f'{prefix}-1001'
    conn.close()


# --- refunds ----------------------------------------------------------------

def _seed_discounted_order(app):
    """Two $100 lines, $20 (10%) order discount, 7% tax: paid $192.60."""
    item = _add_item(app, 'GADGET', 10000, 5)
    conn = _db(app)
    cur = conn.execute('''
        INSERT INTO pos_orders (order_number, status, subtotal_cents, tax_rate, tax_amount_cents,
                                discount_amount_cents, discount_type, total_cents, payment_method, payment_details,
                                operator_id)
        VALUES ('POS-T-001', 'completed', 20000, 0.07, 1260, 2000, 'percent', 19260, 'card', '{}', 1)
    ''')
    order_id = cur.lastrowid
    ids = []
    for _ in range(2):
        c = conn.execute('''INSERT INTO pos_order_items (order_id, inventory_item_id, sku, name, quantity,
                                                        unit_price_cents, line_total_cents)
                            VALUES (?, ?, 'GADGET', 'GADGET item', 1, 10000, 10000)''', (order_id, item))
        ids.append(c.lastrowid)
    conn.commit()
    conn.close()
    return order_id, ids


def _refund_total(app, order_id):
    conn = _db(app)
    t = conn.execute('SELECT COALESCE(SUM(amount_cents), 0) FROM pos_refunds WHERE order_id = ?',
                     (order_id,)).fetchone()[0]
    # The status is derived from the refunds (the order row never changes)
    from app.services.pos_order_state import order_state
    status = order_state(conn, order_id)['status']
    conn.close()
    return t, status


def test_partial_refund_pays_share_of_what_was_paid(fresh_app, fresh_client):
    order_id, (line1, line2) = _seed_discounted_order(fresh_app)
    login(fresh_client, 3)  # POS manager
    fresh_client.post('/pos/refunds/process', data={
        'order_id': order_id, 'refund_type': 'partial', 'reason': 'customer_changed_mind',
        'item_id': [str(line1)], f'quantity_{line1}': '1'})
    assert _refund_total(fresh_app, order_id) == (9630, 'partial_refund')

    fresh_client.post('/pos/refunds/process', data={
        'order_id': order_id, 'refund_type': 'partial', 'reason': 'customer_changed_mind',
        'item_id': [str(line2)], f'quantity_{line2}': '1'})
    # Never more than was paid, and no stray cent left behind
    assert _refund_total(fresh_app, order_id) == (19260, 'refunded')


# --- sale edits -------------------------------------------------------------

def test_editing_a_sale_keeps_its_discount(fresh_app, fresh_client):
    order_id, (line1, line2) = _seed_discounted_order(fresh_app)
    login(fresh_client, 1)
    fresh_client.post(f'/pos/management/edit-sale/{order_id}', data={
        'item_id': [str(line1), str(line2)], 'item_quantity': ['1', '1'],
        'item_price': ['100', '100'], 'item_name': ['Renamed', 'GADGET item'],
        'item_sku': ['GADGET', 'GADGET']})
    order = _orders(fresh_app)[0]
    assert order['total_cents'] == 19260
    assert order['discount_amount_cents'] == 2000


def test_editing_a_refunded_sale_is_refused(fresh_app, fresh_client):
    order_id, (line1, _l2) = _seed_discounted_order(fresh_app)
    conn = _db(fresh_app)
    conn.execute("INSERT INTO pos_refunds (order_id, refund_type, amount_cents, reason, manager_id) "
                 "VALUES (?, 'partial', 1, 'other', 1)", (order_id,))
    conn.commit()
    conn.close()
    login(fresh_client, 1)
    fresh_client.post(f'/pos/management/edit-sale/{order_id}', data={
        'item_id': [str(line1)], 'item_quantity': ['5'], 'item_price': ['1'], 'item_name': ['x']})
    assert _orders(fresh_app)[0]['total_cents'] == 19260
