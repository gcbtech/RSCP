"""POS payments ledger, refund method/tax, the single report engine, drawer sessions, receipts."""
import json
import sqlite3
from datetime import date, datetime, time, timezone

import pytest
from flask import template_rendered

from tests.conftest import login, sales_unlocked

TODAY = date.today()


def _db(app):
    from app.services.db import get_db_connection
    return get_db_connection(app.config['DATABASE'])


def _utc_at_local(hour, minute=0):
    local = datetime.combine(TODAY, time(hour, minute)).astimezone()
    return local.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def _settings(app, **values):
    from app.routes.pos.core import set_pos_setting
    with app.app_context():
        for k, v in values.items():
            set_pos_setting(k, v)


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


def _payments(app, order_id=None):
    conn = _db(app)
    if order_id is None:
        rows = conn.execute('SELECT * FROM pos_payments ORDER BY id').fetchall()
    else:
        rows = conn.execute('SELECT * FROM pos_payments WHERE order_id = ? ORDER BY id', (order_id,)).fetchall()
    conn.close()
    return [(r['method'], r['amount_cents'], r['tendered_cents'], r['change_given_cents']) for r in rows]


def _insert_order(conn, number, total, tax, method, details, subtotal=None, discount=0, status='completed',
                  created_at=None, cash_discount=0):
    """An order row (money in cents; payment_details keys as checkout writes them)."""
    cur = conn.execute('''
        INSERT INTO pos_orders (order_number, status, subtotal_cents, tax_rate, tax_amount_cents,
                                discount_amount_cents, total_cents,
                                payment_method, payment_details, operator_id, created_at, cash_discount_cents)
        VALUES (?, ?, ?, 0.1, ?, ?, ?, ?, ?, 1, COALESCE(?, CURRENT_TIMESTAMP), ?)
    ''', (number, status, subtotal if subtotal is not None else total, tax, discount, total, method,
          details if isinstance(details, str) or details is None else json.dumps(details), created_at,
          cash_discount))
    return cur.lastrowid


def _insert_line(conn, order_id, qty, unit_price, line_total, sku='LINE'):
    return conn.execute('''INSERT INTO pos_order_items (order_id, sku, name, quantity, unit_price_cents,
                                                       line_total_cents)
                           VALUES (?, ?, ?, ?, ?, ?)''',
                        (order_id, sku, f'{sku} item', qty, unit_price, line_total)).lastrowid


# --- migration backfill -----------------------------------------------------

def test_backfill_from_each_payment_type(fresh_app, tmp_path):
    """Migration 8 on a database at schema v7 (dollar REAL columns, as then)."""
    from app.services import migration
    from app.services.migration import _m8_pos_ledger
    db = str(tmp_path / 'v7.db')
    migration.run_migrations(db, target=7)
    conn = sqlite3.connect(db)

    def order(number, total, tax, method, details):
        return conn.execute('''
            INSERT INTO pos_orders (order_number, status, subtotal, tax_rate, tax_amount, discount_amount, total,
                                    payment_method, payment_details, operator_id)
            VALUES (?, 'completed', ?, 0.1, ?, 0, ?, ?, ?, 1)''',
            (number, total, tax, total, method,
             details if isinstance(details, str) or details is None else json.dumps(details))).lastrowid

    cash = order('B-1', 10.70, 0.70, 'cash', {'tendered': 20, 'change': 9.30, 'cash_discount': 0})
    card = order('B-2', 21.40, 1.40, 'card', {})
    split = order('B-3', 9.00, 0.0, 'split',
                  {'cash': 6, 'cards': [4], 'total_tendered': 10, 'change': 1, 'cash_discount': 0.5})
    broken = order('B-4', 5.00, 0.0, 'split', '{not json')
    exact = order('B-5', 7.00, 0.0, 'cash', None)
    # Full refunds carry their share of tax; 2.x partial refunds (below) were pre-tax
    conn.execute("INSERT INTO pos_refunds (order_id, refund_type, amount, reason, manager_id) "
                 "VALUES (?, 'full', 10.70, 'other', 1)", (cash,))
    conn.execute("INSERT INTO pos_refunds (order_id, refund_type, amount, reason, manager_id) "
                 "VALUES (?, 'full', 10.70, 'other', 1)", (card,))
    conn.execute("INSERT INTO pos_refunds (order_id, refund_type, amount, reason, manager_id) "
                 "VALUES (?, 'partial', 4.50, 'other', 1)", (split,))
    conn.commit()
    conn.close()

    migration.run_migrations(db, target=8)
    raw = sqlite3.connect(db, isolation_level=None)
    raw.execute('BEGIN IMMEDIATE')
    _m8_pos_ledger(raw)  # idempotent
    raw.execute('COMMIT')

    def payments(order_id):
        return [tuple(r) for r in raw.execute(
            'SELECT method, amount, tendered, change_given FROM pos_payments WHERE order_id = ? ORDER BY id',
            (order_id,))]

    assert payments(cash) == [('cash', 10.70, 20.0, 9.30)]
    assert payments(card) == [('card', 21.40, None, 0.0)]
    # split: card as given, cash applied = total - cards, change = handed - applied
    assert payments(split) == [('card', 4.0, None, 0.0), ('cash', 5.0, 6.0, 1.0)]
    assert payments(broken) == [('cash', 5.0, 5.0, 0.0)]
    assert payments(exact) == [('cash', 7.0, 7.0, 0.0)]

    refunds = raw.execute('SELECT order_id, refund_method, tax_refunded FROM pos_refunds ORDER BY id').fetchall()
    assert [tuple(r) for r in refunds] == [(cash, 'cash', 0.70), (card, 'card', 0.70), (split, 'cash', 0.0)]
    assert raw.execute('SELECT cash_discount FROM pos_orders WHERE id = ?', (split,)).fetchone()[0] == 0.5
    raw.close()


# --- checkout writes the ledger --------------------------------------------

def test_checkout_cash_records_tendered_and_change(fresh_app, fresh_client):
    _settings(fresh_app, TAX_RATE='0.07')
    item = _add_item(fresh_app)
    login(fresh_client, 2)
    _put_cart(fresh_client, item)
    resp = fresh_client.post('/pos/checkout/process', data={'payment_method': 'cash', 'tendered': '20.00'})
    # The sale lands on its receipt (no error page after a recorded sale)
    assert '/pos/receipt/' in resp.headers['Location']
    assert _payments(fresh_app) == [('cash', 1070, 2000, 930)]


def test_checkout_split_records_cash_applied(fresh_app, fresh_client):
    _settings(fresh_app, TAX_RATE='0.0')
    item = _add_item(fresh_app)
    login(fresh_client, 2)
    _put_cart(fresh_client, item)
    fresh_client.post('/pos/checkout/process',
                      data={'payment_method': 'split', 'cash_amount': '7', 'card_amount': ['4']})
    assert _payments(fresh_app) == [('card', 400, None, 0), ('cash', 600, 700, 100)]


def test_checkout_split_cards_cannot_exceed_total(fresh_app, fresh_client):
    _settings(fresh_app, TAX_RATE='0.0')
    item = _add_item(fresh_app)
    login(fresh_client, 2)
    _put_cart(fresh_client, item)
    fresh_client.post('/pos/checkout/process',
                      data={'payment_method': 'split', 'cash_amount': '0', 'card_amount': ['12']})
    assert _payments(fresh_app) == []


def test_checkout_records_cash_discount(fresh_app, fresh_client):
    _settings(fresh_app, TAX_RATE='0.0', CASH_DISCOUNT_ENABLED='true', CASH_DISCOUNT_PERCENT='10',
              CASH_DISCOUNT_TYPE='percent')
    item = _add_item(fresh_app)
    login(fresh_client, 2)
    _put_cart(fresh_client, item)
    resp = fresh_client.post('/pos/checkout/process', data={'payment_method': 'cash', 'tendered': '10'})
    conn = _db(fresh_app)
    order = conn.execute('SELECT * FROM pos_orders').fetchone()
    conn.close()
    assert order['total_cents'] == 900 and order['cash_discount_cents'] == 100
    assert _payments(fresh_app) == [('cash', 900, 1000, 100)]
    receipt = fresh_client.get(resp.headers['Location']).get_data(as_text=True)
    assert 'Cash Discount' in receipt and '1.00' in receipt


# --- refunds ----------------------------------------------------------------

def _seed_card_order(app):
    """Two $100 lines, $20 order discount, 7% tax: paid $192.60 by card."""
    from app.services.pos_ledger import write_payments
    conn = _db(app)
    order_id = _insert_order(conn, 'POS-R-1', 19260, 1260, 'card', {}, subtotal=20000, discount=2000)
    lines = [_insert_line(conn, order_id, 1, 10000, 10000) for _ in range(2)]
    write_payments(conn, order_id, [('card', 19260, None, 0)])
    conn.commit()
    conn.close()
    return order_id, lines


def test_refund_records_method_and_tax(fresh_app, fresh_client):
    order_id, (line1, line2) = _seed_card_order(fresh_app)
    login(fresh_client, 3)
    page = fresh_client.get('/pos/refunds/order/POS-R-1').get_data(as_text=True)
    assert 'name="refund_method"' in page and '<option value="card" selected>' in page
    fresh_client.post('/pos/refunds/process', data={
        'order_id': order_id, 'refund_type': 'partial', 'reason': 'customer_changed_mind',
        'refund_method': 'cash', 'item_id': [str(line1)], f'quantity_{line1}': '1'})
    fresh_client.post('/pos/refunds/process', data={   # no method chosen -> original (card)
        'order_id': order_id, 'refund_type': 'partial', 'reason': 'customer_changed_mind',
        'item_id': [str(line2)], f'quantity_{line2}': '1'})
    conn = _db(fresh_app)
    rows = [tuple(r) for r in conn.execute(
        'SELECT amount_cents, refund_method, tax_refunded_cents FROM pos_refunds ORDER BY id').fetchall()]
    items = conn.execute('SELECT amount_cents FROM pos_refund_items ORDER BY id').fetchall()
    conn.close()
    assert rows == [(9630, 'cash', 630), (9630, 'card', 630)]
    assert [r[0] for r in items] == [10000, 10000]   # refund item rows keep list value (Top Sellers ratios)


def test_split_order_refund_defaults_to_cash(fresh_app, fresh_client):
    conn = _db(fresh_app)
    order_id = _insert_order(conn, 'POS-S-1', 3300, 300, 'split', {'cash_cents': 1500, 'cards_cents': [2000]},
                             subtotal=3000)
    _insert_line(conn, order_id, 1, 3000, 3000)
    conn.commit()
    conn.close()
    login(fresh_client, 3)
    fresh_client.post('/pos/refunds/process', data={'order_id': order_id, 'refund_type': 'full', 'reason': 'other'})
    conn = _db(fresh_app)
    r = conn.execute('SELECT amount_cents, refund_method, tax_refunded_cents FROM pos_refunds').fetchone()
    conn.close()
    assert tuple(r) == (3300, 'cash', 300)


# --- the report engine --------------------------------------------------------

def _seed_day(app, client):
    """A day with every kind of adjustment.

    A  cash : 2 x $10 with a $2 line discount, 10% tax, 5% cash discount -> $18.90 (tendered $20)
    B  card : 2 x $25, $5 order discount, $5 coupon, 10% tax            -> $44.00
    C  split: 1 x $30, 10% tax, $20 card + $15 cash handed               -> $33.00
    D  held : ignored
    Refunds: one unit of B to card ($22.00, $2.00 tax); all of C in cash ($33.00, $3.00 tax).
    Drawer: float $100 opened 9:00, closed 17:00 with $98.00 counted.
    """
    from app.services.pos_ledger import write_payments, allocate_payments
    noon = _utc_at_local(12)
    conn = _db(app)
    a = _insert_order(conn, 'D-A', 1890, 180, 'cash',
                      {'tendered_cents': 2000, 'change_cents': 110, 'cash_discount_cents': 90},
                      subtotal=1800, created_at=noon, cash_discount=90)
    _insert_line(conn, a, 2, 1000, 1800)
    b = _insert_order(conn, 'D-B', 4400, 400, 'card', {}, subtotal=5000, discount=500, created_at=noon)
    b_line = _insert_line(conn, b, 2, 2500, 5000)
    coupon = conn.execute("INSERT INTO pos_coupons (name, code, coupon_type, discount_type, discount_cents) "
                          "VALUES ('Save 5', 'SAVE5', 'generic', 'order_dollar', 500)").lastrowid
    conn.execute("INSERT INTO pos_coupon_redemptions (coupon_id, order_id, serial_used, discount_applied_cents, "
                 "redeemed_by) VALUES (?, ?, 'SAVE5', 500, 1)", (coupon, b))
    c = _insert_order(conn, 'D-C', 3300, 300, 'split', {'cash_cents': 1500, 'cards_cents': [2000]},
                      subtotal=3000, created_at=noon)
    _insert_line(conn, c, 1, 3000, 3000)
    d = _insert_order(conn, 'D-D', 10000, 0, 'cash', {}, status='held', created_at=noon)
    for oid in (a, b, c):
        o = conn.execute('SELECT payment_method, payment_details, total_cents FROM pos_orders WHERE id = ?',
                         (oid,)).fetchone()
        write_payments(conn, oid, allocate_payments(o[0], json.loads(o[1]), o[2]))
    conn.execute('''INSERT INTO drawer_sessions (opened_at, opened_by, opening_float_cents, closed_at, closed_by,
                                                counted_cash_cents, expected_cash_cents, over_short_cents)
                    VALUES (?, 3, 10000, ?, 3, 9800, 9890, -90)''', (_utc_at_local(9), _utc_at_local(17)))
    conn.commit()
    conn.close()

    login(client, 3)
    client.post('/pos/refunds/process', data={'order_id': b, 'refund_type': 'partial', 'reason': 'other',
                                              'item_id': [str(b_line)], f'quantity_{b_line}': '1'})
    client.post('/pos/refunds/process', data={'order_id': c, 'refund_type': 'full', 'reason': 'other'})
    conn = _db(app)
    with sales_unlocked(conn):
        conn.execute('UPDATE pos_refunds SET created_at = ?', (_utc_at_local(13),))
    conn.close()
    return a, b, c, d


def test_engine_numbers_for_a_seeded_day(fresh_app, fresh_client):
    from app.services.pos_reports import build_sales_report
    _seed_day(fresh_app, fresh_client)
    with fresh_app.app_context():
        r = build_sales_report(TODAY, TODAY)

    assert r['order_count'] == 3
    assert r['gross_sales_cents'] == 10000
    assert r['discounts'] == {'line_cents': 200, 'order_cents': 500, 'coupon_cents': 500, 'cash_cents': 90,
                              'total_cents': 1290}
    assert r['net_sales_cents'] == 8710
    assert r['gross_sales_cents'] - r['discounts']['total_cents'] == r['net_sales_cents']
    assert r['tax_collected_cents'] == 880
    assert r['sales_total_cents'] == 9590
    assert r['refunds'] == {'count': 2, 'total_cents': 5500, 'tax_cents': 500, 'net_cents': 5000,
                            'by_method_cents': {'card': 2200, 'cash': 3300}}
    assert r['tax_net_cents'] == 380
    assert r['net_revenue_cents'] == 4090
    assert r['net_sales_after_refunds_cents'] == 3710
    assert [(t['method'], t['count'], t['collected_cents'], t['refunded_cents'], t['net_cents'])
            for t in r['tenders']] == [
        ('cash', 2, 3190, 3300, -110),
        ('card', 2, 6400, 2200, 4200),
    ]
    assert r['expected_cash'] == {'opening_float_cents': 10000, 'cash_sales_cents': 3190,
                                  'cash_refunds_cents': 3300, 'total_cents': 9890}
    assert [(s['id'], s['over_short_cents']) for s in r['drawer_sessions']] == [(1, -90)]
    # legacy keys the Top Sellers reconciliation relies on
    assert r['summary']['gross_sales_cents'] == r['sales_total_cents'] and r['summary']['total_orders'] == 3


def _captured(app):
    captured = []

    def record(sender, template, context, **extra):
        captured.append((template.name, context))
    template_rendered.connect(record, app)
    return captured, record


def test_eod_email_print_view_and_page_use_the_same_numbers(fresh_app, fresh_client, monkeypatch):
    _seed_day(fresh_app, fresh_client)
    login(fresh_client, 1)
    captured, record = _captured(fresh_app)
    try:
        assert fresh_client.get('/pos/management/reports/daily/print').status_code == 200
        assert fresh_client.get('/pos/management/reports/daily').status_code == 200
        assert fresh_client.get('/pos/management/reports/custom',
                                query_string={'start': TODAY.isoformat(), 'end': TODAY.isoformat()}).status_code == 200
    finally:
        template_rendered.disconnect(record, fresh_app)
    contexts = {name: ctx for name, ctx in captured}
    print_report = contexts['pos/daily_report_print.html']['report']
    page_report = contexts['pos/daily_report.html']['report']
    custom_report = contexts['pos/custom_report.html']['report']

    from app.services.pos_email import build_eod_email
    with fresh_app.test_request_context():
        subject, html, data = build_eod_email(TODAY)
    assert data['report'] == print_report == page_report == custom_report
    assert print_report['net_revenue_cents'] == 4090

    # The rendered print view and email show the same figures
    print_html = fresh_client.get('/pos/management/reports/daily/print').get_data(as_text=True)
    for figure in ('40.90', '87.10', '0.90', '98.90'):
        assert figure in print_html and figure in html

    # Manual "Send Email Report" sends exactly that email
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context=None):
            pass

        def login(self, user, password):
            pass

        def sendmail(self, sender, recipients, message):
            sent.append(message)

    import app.services.pos_email as pos_email
    monkeypatch.setattr(pos_email.smtplib, 'SMTP', FakeSMTP)
    _settings(fresh_app, POS_EMAIL_HOST='smtp.test', POS_EMAIL_RECIPIENTS='owner@example.test')
    fresh_client.post('/pos/management/reports/daily/email', data={'date': TODAY.isoformat()})
    assert len(sent) == 1 and '40.90' in sent[0].replace('=\n', '')


# --- drawer sessions -----------------------------------------------------------

def test_drawer_session_expected_and_over_short(fresh_app, fresh_client):
    _settings(fresh_app, TAX_RATE='0.0')
    item = _add_item(fresh_app)
    login(fresh_client, 3)
    assert 'No cash drawer session is open' in fresh_client.get('/pos/').get_data(as_text=True)
    fresh_client.post('/pos/drawer/open', data={'opening_float': '100'})
    assert 'No cash drawer session is open' not in fresh_client.get('/pos/').get_data(as_text=True)
    fresh_client.post('/pos/drawer/open', data={'opening_float': '50'})   # already open: ignored

    _put_cart(fresh_client, item, qty=2)          # $20 cash
    fresh_client.post('/pos/checkout/process', data={'payment_method': 'cash', 'tendered': '50'})
    _put_cart(fresh_client, item)                 # $10 split: $4 card, $6 cash applied
    fresh_client.post('/pos/checkout/process',
                      data={'payment_method': 'split', 'cash_amount': '10', 'card_amount': ['4']})
    _put_cart(fresh_client, item)                 # $10 card, refunded in cash
    fresh_client.post('/pos/checkout/process', data={'payment_method': 'card'})
    conn = _db(fresh_app)
    card_order = conn.execute("SELECT id FROM pos_orders WHERE payment_method = 'card'").fetchone()[0]
    conn.close()
    fresh_client.post('/pos/refunds/process', data={'order_id': card_order, 'refund_type': 'full',
                                                    'reason': 'other', 'refund_method': 'cash'})

    page = fresh_client.get('/pos/drawer').get_data(as_text=True)
    assert '116.00' in page   # 100 + 20 + 6 - 10, live

    conn = _db(fresh_app)
    session_id = conn.execute('SELECT id FROM drawer_sessions').fetchone()[0]
    conn.close()
    fresh_client.post('/pos/drawer/close', data={'session_id': session_id, 'counted_cash': '115.50'})
    conn = _db(fresh_app)
    s = conn.execute('SELECT * FROM drawer_sessions').fetchall()
    conn.close()
    assert len(s) == 1
    assert (s[0]['opening_float_cents'], s[0]['expected_cash_cents'], s[0]['counted_cash_cents'],
            s[0]['over_short_cents']) == (10000, 11600, 11550, -50)
    assert s[0]['closed_by'] == 3


def test_drawer_requires_manager(fresh_app, fresh_client):
    login(fresh_client, 2)
    fresh_client.post('/pos/drawer/open', data={'opening_float': '100'})
    conn = _db(fresh_app)
    assert conn.execute('SELECT COUNT(*) FROM drawer_sessions').fetchone()[0] == 0
    conn.close()
    assert 'No cash drawer session is open' not in fresh_client.get('/pos/').get_data(as_text=True)


# --- receipts and sale edits ---------------------------------------------------

def test_receipt_reprint_uses_stored_coupon_amount(fresh_app, fresh_client):
    item = _add_item(fresh_app, 'TARGET', 5000)
    conn = _db(fresh_app)
    cur = conn.execute('''INSERT INTO pos_coupons (code, name, coupon_type, discount_type, discount_cents)
                          VALUES ('TENOFF', 'Ten off', 'standard', 'item_dollar', 1000)''')
    coupon_id = cur.lastrowid
    conn.execute('INSERT INTO pos_coupon_items (coupon_id, item_id) VALUES (?, ?)', (coupon_id, item))
    order_id = _insert_order(conn, 'POS-C-1', 4500, 0, 'card', {}, subtotal=5000)
    line = _insert_line(conn, order_id, 1, 5000, 5000)
    with sales_unlocked(conn):
        conn.execute('UPDATE pos_order_items SET inventory_item_id = ? WHERE id = ?', (item, line))
    conn.execute("INSERT INTO pos_coupon_redemptions (coupon_id, order_id, serial_used, discount_applied_cents, "
                 "redeemed_by) VALUES (?, ?, 'TENOFF', 500, 1)", (coupon_id, order_id))
    # The coupon's definition changes after the sale
    conn.execute('UPDATE pos_coupons SET discount_cents = 2500 WHERE id = ?', (coupon_id,))
    conn.commit()
    conn.close()
    login(fresh_client, 2)
    for url in ('/pos/receipt/POS-C-1', '/pos/receipt/POS-C-1/print'):
        html = fresh_client.get(url).get_data(as_text=True)
        assert 'Coupon (TENOFF)' in html and '$5.00' in html
        assert '$25.00' not in html and '$10.00' not in html


def test_editing_a_sale_records_the_difference_as_a_payment(fresh_app, fresh_client):
    """The checkout's payments stay as they were; what the correction pays
    back is a payments row of its own (split sales: in cash by default)."""
    from app.services.pos_ledger import write_payments, allocate_payments
    conn = _db(fresh_app)
    details = {'cash_cents': 1500, 'cards_cents': [2000]}
    order_id = _insert_order(conn, 'POS-E-1', 3000, 0, 'split', details, subtotal=3000)
    with sales_unlocked(conn):
        conn.execute("UPDATE pos_orders SET tax_rate = 0 WHERE id = ?", (order_id,))
    line = _insert_line(conn, order_id, 1, 3000, 3000)
    write_payments(conn, order_id, allocate_payments('split', details, 3000))
    conn.commit()
    conn.close()
    assert _payments(fresh_app, order_id) == [('card', 2000, None, 0), ('cash', 1000, 1500, 500)]
    login(fresh_client, 1)
    fresh_client.post(f'/pos/management/edit-sale/{order_id}', data={
        'item_id': [str(line)], 'item_quantity': ['1'], 'item_price': ['25'], 'item_name': ['LINE item']})
    assert _payments(fresh_app, order_id) == [('card', 2000, None, 0), ('cash', 1000, 1500, 500),
                                              ('cash', -500, None, 0)]


def test_voiding_an_order_pays_back_its_payments(fresh_app, fresh_client):
    order_id, _lines = _seed_card_order(fresh_app)
    before = _payments(fresh_app, order_id)
    login(fresh_client, 1)
    fresh_client.post('/pos/order/POS-R-1/void', data={})
    after = _payments(fresh_app, order_id)
    assert after[:len(before)] == before
    assert sum(p[1] for p in after) == 0 and all(p[1] < 0 for p in after[len(before):])
