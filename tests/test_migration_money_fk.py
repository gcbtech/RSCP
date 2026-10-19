"""Schema migrations 15 (money as integer cents) and 16 (enforced foreign keys)
on a realistic pre-migration database: a v14 schema holding float dollars
(0.1 + 0.2 style sums, 19.995, split payments, a partial refund with tax, a
coupon, a cash discount, carts held on a register and as a held order) and
deliberate orphans."""
import json
import logging
import sqlite3
from datetime import date, datetime, time, timezone

import pytest

from app.services import migration

TODAY = date.today()


def _utc_at_local(hour):
    local = datetime.combine(TODAY, time(hour, 0)).astimezone()
    return local.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


NOON = None  # set in seed (local noon today, as UTC)


def _seed_v14(path):
    """A v14 database as 2.x/3.0-beta RSCP left it. Returns ids by name."""
    migration.run_migrations(path, target=14)
    noon, one_pm = _utc_at_local(12), _utc_at_local(13)
    c = sqlite3.connect(path)   # no foreign key enforcement, like every 2.x connection
    ids = {}
    c.executemany("INSERT INTO users (id, username, password_hash, is_admin, roles) VALUES (?, ?, 'x', ?, ?)",
                  [(1, 'admin', 1, '["super_admin"]'), (2, 'cashier', 0, '["operator"]'),
                   (3, 'manager', 0, '["pos_admin"]'), (4, 'parttimer', 0, '["operator"]')])
    c.executemany('''INSERT INTO inventory_items (id, sku, name, quantity, location_area, buy_price, sell_price,
                                                  sale_price, sale_enabled)
                     VALUES (?, ?, ?, ?, 'A', ?, ?, ?, ?)''', [
        (1, 'RSCP-ATO-0000-0001', 'Penny Candy', 50, 0.05, 0.1, 0.0, 0),
        (2, 'RSCP-ATO-0000-0002', 'Gum', 50, 0.07, 0.2, 0.0, 0),
        (3, 'RSCP-ATO-0000-0003', 'Odd Price', 5, 12.344999, 19.995, 17.5, 1),
        (4, 'RSCP-ATO-0000-0004', 'Cable', 20, 4.0, 25.0, None, 0),
        (5, 'RSCP-ATO-0000-0005', 'Mouse', 20, 3.33, 10.0, 0.0, 0),
        (6, 'RSCP-ATO-0000-0006', 'Pad', 20, None, 5.0, 0.0, 0),
    ])
    c.execute("UPDATE sqlite_sequence SET seq = 40 WHERE name = 'inventory_items'")
    # something added by hand on a real box: a column the migration does not
    # know, and an index on a money column it replaces
    c.execute("ALTER TABLE inventory_items ADD COLUMN shelf_note TEXT")
    c.execute("UPDATE inventory_items SET shelf_note = 'top shelf' WHERE id = 3")
    c.execute("CREATE INDEX idx_by_hand_sell_price ON inventory_items(sell_price)")

    def order(number, status, subtotal, rate, tax, discount, dtype, total, method, details, operator=1,
              cash_discount=0.0, created=None):
        return c.execute('''
            INSERT INTO pos_orders (order_number, status, subtotal, tax_rate, tax_amount, discount_amount,
                                    discount_type, total, payment_method, payment_details, operator_id,
                                    created_at, cash_discount)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (number, status, subtotal, rate, tax, discount, dtype, total, method, json.dumps(details), operator,
             created or noon, cash_discount)).lastrowid

    def line(order_id, item_id, sku, qty, unit, total, dtype=None, damount=0.0):
        return c.execute('''INSERT INTO pos_order_items (order_id, inventory_item_id, sku, name, quantity,
                                unit_price, discount_amount, discount_type, line_total)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                         (order_id, item_id, sku, f'{sku} item', qty, unit, damount, dtype, total)).lastrowid

    def pay(order_id, method, amount, tendered=None, change=0.0):
        c.execute('''INSERT INTO pos_payments (order_id, method, amount, tendered, change_given, created_at)
                     VALUES (?, ?, ?, ?, ?, ?)''', (order_id, method, amount, tendered, change, noon))

    # A: cash, 0.1 + 0.2 (subtotal stored as the float sum), 7% tax, $1 handed over
    a = ids['A'] = order('POS-A', 'completed', 0.1 + 0.2, 0.07, 0.02, 0, None, 0.32, 'cash',
                         {'tendered': 1.0, 'change': 0.6799999999999999, 'cash_discount': 0})
    line(a, 1, 'RSCP-ATO-0000-0001', 1, 0.1, 0.1)
    line(a, 2, 'RSCP-ATO-0000-0002', 1, 0.2, 0.2)
    pay(a, 'cash', 0.32, 1.0, 0.6799999999999999)
    # B: card, a 19.995 price (rounds half-up to $20.00), 8% tax
    b = ids['B'] = order('POS-B', 'completed', 19.995, 0.08, 1.6, 0, None, 21.6, 'card', {})
    line(b, 3, 'RSCP-ATO-0000-0003', 1, 19.995, 19.995)
    pay(b, 'card', 21.6)
    # C: split with a $5 coupon and a 3% cash discount on the $19.50 the cash paid
    cpn = ids['coupon'] = c.execute('''INSERT INTO pos_coupons (name, code, coupon_type, discount_type,
                                           discount_value, min_purchase, created_by)
                                       VALUES ('Save 5', 'SAVE5', 'generic', 'order_dollar', 5.0, 20.0, 1)''').lastrowid
    ids['pct_coupon'] = c.execute('''INSERT INTO pos_coupons (name, code, coupon_type, discount_type,
                                         discount_value) VALUES ('Eighth off', 'EIGHTH', 'generic',
                                         'order_percent', 12.5)''').lastrowid
    cc = ids['C'] = order('POS-C', 'completed', 50.0, 0.1, 4.5, 0, None, 48.91, 'split',
                          {'cash': 20.0, 'cards': [30.0], 'total_tendered': 50.0, 'change': 1.0899999999999999,
                           'cash_discount': 0.59}, operator=2, cash_discount=0.59)
    line(cc, 4, 'RSCP-ATO-0000-0004', 2, 25.0, 50.0)
    c.execute('''INSERT INTO pos_coupon_redemptions (coupon_id, order_id, serial_used, discount_applied, redeemed_by)
                 VALUES (?, ?, 'SAVE5', 5.0, 2)''', (cpn, cc))
    c.execute('UPDATE pos_coupons SET current_uses = 1 WHERE id = ?', (cpn,))
    pay(cc, 'card', 30.0)
    pay(cc, 'cash', 18.91, 20.0, 1.0899999999999999)
    # D: card, line discounts (10% and $1.50), 10% order discount, 7% tax; then a
    #    partial refund of one unit (paid share 8.67 incl. 0.57 tax)
    d = ids['D'] = order('POS-D', 'partial_refund', 30.5, 0.07, 1.92, 3.05, 'percent', 29.37, 'card', {})
    d_line = ids['D_line'] = line(d, 5, 'RSCP-ATO-0000-0005', 3, 10.0, 27.0, 'percent', 10.0)
    line(d, 6, 'RSCP-ATO-0000-0006', 1, 5.0, 3.5, 'fixed', 1.5)
    pay(d, 'card', 29.37)
    r = ids['D_refund'] = c.execute('''INSERT INTO pos_refunds (order_id, refund_type, amount, reason, manager_id,
                                          created_at, refund_method, tax_refunded)
                                       VALUES (?, 'partial', 8.67, 'defective', 3, ?, 'card', 0.57)''',
                                    (d, one_pm)).lastrowid
    c.execute('''INSERT INTO pos_refund_items (refund_id, order_item_id, quantity, amount, restock_action)
                 VALUES (?, ?, 1, 9.0, 'none')''', (r, d_line))
    # E: held (never counted)
    order('POS-E', 'held', 100.0, 0, 0, 0, None, 100.0, 'cash', {})
    # F: rung up by a user since deleted (orphan operator); a line for an item since deleted
    f = ids['F'] = order('POS-F', 'completed', 12.5, 0.0, 0.0, 0, None, 12.5, 'cash',
                         {'tendered': 20, 'change': 7.5, 'cash_discount': 0}, operator=99)
    ids['F_line'] = line(f, 999, 'RSCP-ATO-0000-0999', 1, 12.5, 12.5)
    pay(f, 'cash', 12.5, 20.0, 7.5)
    # G: edited after the sale (its stored parts no longer add up): logged, kept
    g = ids['G'] = order('POS-G', 'completed', 10.0, 0.0, 0.0, 0, None, 9.0, 'card', {})
    line(g, 6, 'RSCP-ATO-0000-0006', 2, 5.0, 10.0)
    pay(g, 'card', 9.0)

    # a payment row of an order that no longer exists, a refund approved by a deleted manager
    pay(777, 'cash', 3.0, 3.0, 0.0)
    ids['orphan_refund'] = c.execute('''INSERT INTO pos_refunds (order_id, refund_type, amount, reason, manager_id,
                                            created_at, refund_method, tax_refunded)
                                        VALUES (?, 'partial', 0.0, 'other', 98, ?, 'cash', 0.0)''',
                                     (b, '2020-01-01 12:00:00')).lastrowid
    c.execute('''INSERT INTO pos_coupon_redemptions (coupon_id, order_id, serial_used, discount_applied, redeemed_by)
                 VALUES (404, ?, 'GONE-1', 1.25, 1)''', (b,))

    # cash drawer: float $100, closed with an awkward float over/short
    c.execute('''INSERT INTO drawer_sessions (opened_at, opened_by, opening_float, closed_at, closed_by,
                                              counted_cash, expected_cash, over_short)
                 VALUES (?, 3, 100.0, ?, 99, 118.9, 118.91000000000001, -0.010000000000005116)''',
              (_utc_at_local(9), _utc_at_local(17)))

    # carts in dollars: one on a register, one held
    cart = {'items': [
        {'line_id': 'L1', 'inventory_item_id': 3, 'sku': 'RSCP-ATO-0000-0003', 'name': 'Odd Price', 'quantity': 2,
         'unit_price': 17.5, 'regular_price': 19.995, 'sell_price': 19.995, 'is_on_sale': True,
         'discount_amount': 10, 'discount_type': 'percent', 'line_total': 31.5},
        {'line_id': 'L2', 'inventory_item_id': None, 'sku': 'CUSTOM-1', 'name': 'Labor', 'quantity': 1,
         'unit_price': 0.1, 'sell_price': 0.1, 'discount_amount': 0.05, 'discount_type': 'fixed',
         'line_total': 0.05, 'is_custom': True}],
        'discount_amount': 2.5, 'discount_type': 'fixed', 'discount_reason': 'regular',
        'applied_coupon': {'id': cpn, 'code': 'SAVE5', 'name': 'Save 5', 'discount': 5.0, 'cannot_combine': False}}
    c.execute("INSERT INTO pos_registers (register_id, friendly_name, cart_json) VALUES ('reg-1', 'Front', ?)",
              (json.dumps(cart),))
    held = dict(cart, applied_coupon=None, discount_amount=15, discount_type='percent')
    ids['held'] = c.execute("INSERT INTO pos_held_orders (operator_id, cart_data, note) VALUES (2, ?, 'later')",
                            (json.dumps(held),)).lastrowid
    ids['held_orphan'] = c.execute("INSERT INTO pos_held_orders (operator_id, cart_data, note) "
                                   "VALUES (99, '{\"items\": []}', 'gone')").lastrowid

    # money settings
    for key, value in (('POS_MAX_DISCOUNT_AMOUNT', 25.5), ('CASH_DISCOUNT_ENABLED', True),
                       ('CASH_DISCOUNT_TYPE', 'percent'), ('CASH_DISCOUNT_AMOUNT', 3.0)):
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(value)))

    # non-financial orphans and the rest of the reference zoo
    c.execute("INSERT INTO time_entries (user_id, clock_in, clock_out) VALUES (99, '2026-01-05 15:00:00', NULL)")
    c.execute("INSERT INTO time_entries (user_id, clock_in, clock_out, edited_by) "
              "VALUES (4, '2026-01-05 15:00:00', '2026-01-05 23:00:00', 99)")
    c.execute("INSERT INTO scheduled_shifts (user_id, start_time, end_time) VALUES (99, '2026-01-06 15:00:00', "
              "'2026-01-06 23:00:00')")
    c.execute("INSERT INTO scheduled_shifts (user_id, start_time, end_time) VALUES (4, '2026-01-06 15:00:00', "
              "'2026-01-06 23:00:00')")
    c.execute("INSERT INTO recurring_shift_rules (user_id, day_of_week, start_time, end_time) VALUES (99, 0, "
              "'09:00', '17:00')")
    c.execute("INSERT INTO user_pto_balances (user_id, balance_hours) VALUES (99, 8.0)")
    c.execute("INSERT INTO pto_requests (user_id, request_type, date, hours_requested) VALUES (99, 'pto', "
              "'2026-01-07', 8)")
    c.execute("INSERT INTO pos_peripherals (peripheral_id, register_id, role, token_hash) "
              "VALUES ('disp-gone', 'gone-reg', 'display', 'h1')")
    c.execute("INSERT INTO pos_peripherals (peripheral_id, register_id, role, token_hash, paired_by) "
              "VALUES ('disp-ok', 'reg-1', 'display', 'h2', 99)")
    c.execute('INSERT INTO pos_coupon_items (coupon_id, item_id) VALUES (?, 999)', (cpn,))
    c.execute('INSERT INTO pos_coupon_items (coupon_id, item_id) VALUES (?, 4)', (cpn,))
    c.execute("INSERT INTO packages (id, tracking_number, item_name) VALUES (7, '1ZKEEP', 'Box')")
    c.execute("INSERT INTO history (package_id, user_id, action) VALUES (4242, 1, 'received')")
    c.execute("INSERT INTO history (package_id, user_id, action) VALUES (7, 99, 'received')")
    c.execute("INSERT INTO inventory_transactions (inventory_item_id, quantity_change, reason) "
              "VALUES (999, -1, 'Sold/Consumed')")
    c.execute("INSERT INTO audit_sessions (id, user_id, mode) VALUES (1, 'admin', 'item')")
    c.execute("INSERT INTO audit_records (session_id, item_id, sku, name) VALUES (555, 4, 'RSCP-ATO-0000-0004', "
              "'Cable')")
    c.execute("INSERT INTO pos_audit_log (action, user_id, user_name) VALUES ('delete_order', 99, 'oldboss')")
    c.execute("INSERT INTO notifications (user_id, title) VALUES (99, 'for nobody')")
    c.execute("INSERT INTO notifications (user_id, title) VALUES (2, 'for cashier')")
    c.commit()
    c.close()
    return ids


class _Log(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())

    @property
    def text(self):
        return '\n'.join(self.lines)


@pytest.fixture
def migrated(tmp_path):
    path = str(tmp_path / 'v14.db')
    ids = _seed_v14(path)
    log = _Log()
    mlogger = logging.getLogger('app.services.migration')
    old_level = mlogger.level
    mlogger.addHandler(log)
    mlogger.setLevel(logging.INFO)
    try:
        migration.run_migrations(path)
    finally:
        mlogger.removeHandler(log)
        mlogger.setLevel(old_level)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    yield conn, ids, log
    conn.close()


def _row(conn, sql, *args):
    return conn.execute(sql, args).fetchone()


def test_schema_is_current_and_foreign_keys_are_clean(migrated):
    conn, _ids, _log = migrated
    assert conn.execute('PRAGMA user_version').fetchone()[0] == migration.LATEST_SCHEMA_VERSION >= 16
    assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
    # ...and the stock ledger (v20) starts from these items' quantities
    from app.services import inventory
    assert inventory.ledger_drift(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM inventory_movements WHERE reason = 'opening_balance'"
                        ).fetchone()[0] == conn.execute('SELECT COUNT(*) FROM inventory_items').fetchone()[0]
    for table, gone, new in (('pos_orders', 'total', 'total_cents'), ('inventory_items', 'sell_price',
                                                                      'sell_price_cents'),
                             ('pos_payments', 'amount', 'amount_cents'), ('pos_refunds', 'amount', 'amount_cents'),
                             ('pos_coupons', 'discount_value', 'discount_cents'),
                             ('drawer_sessions', 'over_short', 'over_short_cents')):
        cols = {r[1]: r[2] for r in conn.execute(f'PRAGMA table_info({table})')}
        assert gone not in cols and cols[new] == 'INTEGER', table
    # ids, AUTOINCREMENT high-water marks, triggers and indexes survive the rebuilds
    assert [r[0] for r in conn.execute('SELECT id FROM inventory_items ORDER BY id')] == [1, 2, 3, 4, 5, 6]
    assert _row(conn, "SELECT seq FROM sqlite_sequence WHERE name = 'inventory_items'")[0] == 40
    # an unexpected column is carried over; an index on a replaced column is dropped
    assert _row(conn, 'SELECT shelf_note FROM inventory_items WHERE id = 3')[0] == 'top shelf'
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('index', 'trigger')")}
    assert 'idx_by_hand_sell_price' not in names
    assert {'trg_inventory_attention_snooze_reset', 'idx_inventory_sku', 'idx_pos_order_number',
            'idx_pos_orders_checkout_token', 'idx_pos_payments_order', 'idx_trans_item',
            'idx_time_entries_user_date'} <= names


def test_every_amount_converts_exactly(migrated):
    conn, ids, _log = migrated
    items = {r['id']: tuple(r)[1:] for r in conn.execute(
        'SELECT id, buy_price_cents, sell_price_cents, sale_price_cents FROM inventory_items')}
    assert items == {1: (5, 10, 0), 2: (7, 20, 0), 3: (1234, 2000, 1750), 4: (400, 2500, None),
                     5: (333, 1000, 0), 6: (None, 500, 0)}

    def order(name):
        return tuple(_row(conn, '''SELECT subtotal_cents, tax_amount_cents, discount_amount_cents, total_cents,
                                          cash_discount_cents FROM pos_orders WHERE id = ?''', ids[name]))
    assert order('A') == (30, 2, 0, 32, 0)                 # 0.1 + 0.2 stored as 0.30000000000000004
    assert order('B') == (2000, 160, 0, 2160, 0)           # 19.995 -> $20.00
    assert order('C') == (5000, 450, 0, 4891, 59)
    assert order('D') == (3050, 192, 305, 2937, 0)

    lines = [tuple(r) for r in conn.execute(
        '''SELECT unit_price_cents, discount_type, discount_percent, discount_cents, line_total_cents
           FROM pos_order_items WHERE order_id = ? ORDER BY id''', (ids['D'],))]
    assert lines == [(1000, 'percent', 10.0, 0, 2700), (500, 'fixed', 0.0, 150, 350)]

    payments = [tuple(r) for r in conn.execute(
        '''SELECT order_id, method, amount_cents, tendered_cents, change_given_cents FROM pos_payments
           WHERE order_id IN (?, ?) ORDER BY id''', (ids['A'], ids['C']))]
    assert payments == [(ids['A'], 'cash', 32, 100, 68), (ids['C'], 'card', 3000, None, 0),
                        (ids['C'], 'cash', 1891, 2000, 109)]
    assert json.loads(_row(conn, 'SELECT payment_details FROM pos_orders WHERE id = ?', ids['C'])[0]) == {
        'cash_cents': 2000, 'cards_cents': [3000], 'total_tendered_cents': 5000, 'change_cents': 109,
        'cash_discount_cents': 59}

    assert tuple(_row(conn, 'SELECT amount_cents, tax_refunded_cents FROM pos_refunds WHERE id = ?',
                      ids['D_refund'])) == (867, 57)
    assert _row(conn, 'SELECT amount_cents FROM pos_refund_items')[0] == 900
    assert tuple(_row(conn, 'SELECT discount_cents, discount_percent, min_purchase_cents FROM pos_coupons '
                            'WHERE id = ?', ids['coupon'])) == (500, None, 2000)
    assert tuple(_row(conn, 'SELECT discount_cents, discount_percent FROM pos_coupons WHERE id = ?',
                      ids['pct_coupon'])) == (None, 12.5)
    assert _row(conn, 'SELECT discount_applied_cents FROM pos_coupon_redemptions WHERE order_id = ?',
                ids['C'])[0] == 500
    assert tuple(_row(conn, '''SELECT opening_float_cents, counted_cash_cents, expected_cash_cents,
                                      over_short_cents FROM drawer_sessions''')) == (10000, 11890, 11891, -1)


def test_saved_carts_and_settings_convert(migrated):
    conn, ids, _log = migrated
    cart = json.loads(_row(conn, "SELECT cart_json FROM pos_registers WHERE register_id = 'reg-1'")[0])
    l1, l2 = cart['items']
    assert (l1['unit_price_cents'], l1['regular_price_cents'], l1['sell_price_cents'], l1['line_total_cents'],
            l1['discount_type'], l1['discount_percent'], l1['discount_cents']) == (1750, 2000, 2000, 3150,
                                                                                  'percent', 10.0, 0)
    assert (l2['unit_price_cents'], l2['discount_type'], l2['discount_cents'], l2['line_total_cents']) == \
        (10, 'fixed', 5, 5)
    assert not any(k in l1 for k in ('unit_price', 'line_total', 'discount_amount', 'regular_price'))
    assert (cart['discount_type'], cart['discount_cents'], cart['discount_percent']) == ('fixed', 250, 0)
    assert cart['applied_coupon']['discount_cents'] == 500 and 'discount' not in cart['applied_coupon']
    assert 'discount_amount' not in cart
    held = json.loads(_row(conn, 'SELECT cart_data FROM pos_held_orders WHERE id = ?', ids['held'])[0])
    assert (held['discount_type'], held['discount_percent']) == ('percent', 15.0)
    assert held['items'][0]['unit_price_cents'] == 1750

    settings = {r[0]: json.loads(r[1]) for r in conn.execute('SELECT key, value FROM settings')}
    assert settings['POS_MAX_DISCOUNT_CENTS'] == 2550 and settings['CASH_DISCOUNT_PERCENT'] == 3.0
    assert 'POS_MAX_DISCOUNT_AMOUNT' not in settings and 'CASH_DISCOUNT_AMOUNT' not in settings


def test_report_totals_for_the_day_are_unchanged(migrated):
    """The day's End of Day figures equal what the pre-v15 dollar engine
    reported for this same data (checked against that code when this test was
    written: gross 127.80, discounts 14.39, net 113.66, tax 8.04, total 121.70,
    refunds 8.67 incl. 0.57 tax, net revenue 113.03, cash 31.73, card 89.97)."""
    from app.services.pos_reports import build_sales_report
    conn, _ids, _log = migrated
    r = build_sales_report(TODAY, TODAY, conn=conn)
    assert r['order_count'] == 6                        # A B C D F G (E is held)
    assert r['sales_total_cents'] == 32 + 2160 + 4891 + 2937 + 1250 + 900
    assert r['tax_collected_cents'] == 2 + 160 + 450 + 192
    assert r['gross_sales_cents'] == 30 + 2000 + 5000 + 3500 + 1250 + 1000
    # the coupon figure includes the redemption whose coupon is gone (kept, unlinked)
    assert r['discounts'] == {'line_cents': 150 + 300, 'order_cents': 305, 'coupon_cents': 500 + 125,
                              'cash_cents': 59, 'total_cents': 1439}
    assert (r['gross_sales_cents'], r['net_sales_cents'], r['tax_collected_cents'], r['sales_total_cents'],
            r['tax_net_cents'], r['net_revenue_cents'], r['net_sales_after_refunds_cents']) ==         (12780, 11366, 804, 12170, 747, 11303, 10556)
    assert r['expected_cash'] == {'opening_float_cents': 10000, 'cash_sales_cents': 3173,
                                  'cash_refunds_cents': 0, 'total_cents': 13173}
    assert r['refunds'] == {'count': 1, 'total_cents': 867, 'tax_cents': 57, 'net_cents': 810,
                            'by_method_cents': {'card': 867}}
    assert [(t['method'], t['collected_cents'], t['refunded_cents']) for t in r['tenders']] == [
        ('cash', 32 + 1891 + 1250, 0), ('card', 2160 + 3000 + 2937 + 900, 867)]
    assert r['net_revenue_cents'] == r['sales_total_cents'] - 867


def test_orphans_follow_the_policy(migrated):
    conn, ids, log = migrated
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
    # financial and audit rows are kept, only the dangling reference is cleared
    assert one('SELECT operator_id FROM pos_orders WHERE id = ?', ids['F']) is None
    assert one('SELECT total_cents FROM pos_orders WHERE id = ?', ids['F']) == 1250
    assert tuple(conn.execute('SELECT inventory_item_id, sku, name FROM pos_order_items WHERE id = ?',
                              (ids['F_line'],)).fetchone()) == (None, 'RSCP-ATO-0000-0999', 'RSCP-ATO-0000-0999 item')
    assert one('SELECT COUNT(*) FROM pos_payments WHERE order_id IS NULL AND amount_cents = 300') == 1
    assert one('SELECT manager_id FROM pos_refunds WHERE id = ?', ids['orphan_refund']) is None
    assert one("SELECT coupon_id FROM pos_coupon_redemptions WHERE serial_used = 'GONE-1'") is None
    assert tuple(conn.execute('SELECT opened_by, closed_by FROM drawer_sessions').fetchone()) == (3, None)
    assert one('SELECT operator_id FROM pos_held_orders WHERE id = ?', ids['held_orphan']) is None
    assert one("SELECT paired_by FROM pos_peripherals WHERE peripheral_id = 'disp-ok'") is None
    assert one('SELECT COUNT(*) FROM inventory_transactions WHERE inventory_item_id IS NULL') == 1
    assert one('SELECT COUNT(*) FROM audit_records WHERE session_id IS NULL') == 1
    assert one("SELECT user_id FROM pos_audit_log WHERE user_name = 'oldboss'") is None
    assert one('SELECT COUNT(*) FROM history WHERE package_id IS NULL') == 1
    assert one('SELECT user_id FROM history WHERE package_id = 7') is None
    assert one('SELECT edited_by FROM time_entries WHERE user_id = 4') is None
    # non-financial child rows of things that are gone are deleted
    for table in ('time_entries', 'scheduled_shifts', 'recurring_shift_rules', 'user_pto_balances',
                  'pto_requests', 'notifications'):
        assert one(f'SELECT COUNT(*) FROM {table} WHERE user_id = 99') == 0, table
    assert one('SELECT COUNT(*) FROM time_entries') == 1 and one('SELECT COUNT(*) FROM notifications') == 1
    assert one("SELECT COUNT(*) FROM pos_peripherals WHERE register_id = 'gone-reg'") == 0
    assert one('SELECT COUNT(*) FROM pos_coupon_items') == 1
    # counts are logged per table
    text = log.text
    assert 'pos_orders: cleared operator_id on 1 row(s)' in text
    assert 'time_entries: deleted 1 row(s)' in text
    assert 'PRAGMA foreign_key_check: clean' in text


def test_orders_that_do_not_add_up_are_logged_not_changed(migrated):
    conn, ids, log = migrated
    assert 'POS-G: subtotal - discounts + tax = 1000 != total 900' in log.text
    assert 'POS-A' not in log.text.split('do not add up')[-1]
    assert conn.execute('SELECT total_cents FROM pos_orders WHERE id = ?', (ids['G'],)).fetchone()[0] == 900


def test_foreign_keys_are_enforced_afterwards(migrated, tmp_path):
    from app.services.db import get_db_connection
    conn, ids, _log = migrated
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    live = get_db_connection(path)
    try:
        assert live.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):          # sales keep their operator
            live.execute('DELETE FROM users WHERE id = 2')
        with pytest.raises(sqlite3.IntegrityError):          # an item with sales stays
            live.execute('DELETE FROM inventory_items WHERE id = 4')
        with pytest.raises(sqlite3.IntegrityError):          # no new dangling references
            live.execute("INSERT INTO pos_payments (order_id, method, amount_cents) VALUES (424242, 'cash', 1)")
        live.execute('DELETE FROM users WHERE id = 4')        # timeclock rows go with the user
        assert live.execute('SELECT COUNT(*) FROM time_entries WHERE user_id = 4').fetchone()[0] == 0
        assert live.execute('SELECT COUNT(*) FROM scheduled_shifts WHERE user_id = 4').fetchone()[0] == 0
        live.rollback()
    finally:
        live.close()


def test_migration_16_fails_loudly_when_a_violation_remains(tmp_path, monkeypatch):
    path = str(tmp_path / 'bad.db')
    _seed_v14(path)
    # pretend the orphan rules forgot pos_payments: its dangling order_id must abort v16
    monkeypatch.setattr(migration, '_M16_ORPHANS',
                        tuple(r for r in migration._M16_ORPHANS if r[0] != 'pos_payments'))
    with pytest.raises(RuntimeError, match='foreign key violation'):
        migration.run_migrations(path)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 15     # v16 rolled back
        cols = {r[1] for r in conn.execute('PRAGMA table_info(time_entries)')}
        assert conn.execute('SELECT COUNT(*) FROM time_entries WHERE user_id = 99').fetchone()[0] == 1
        assert 'user_id' in cols
    finally:
        conn.close()
