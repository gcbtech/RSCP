"""Cart concurrency: a register and a paired handheld scanner on one sale.

Every cart change goes through one atomic load -> mutate -> save transaction
(BEGIN IMMEDIATE on the register row), and cart lines are addressed by a
stable line_id instead of their list index. These tests drive two real test
clients -- the register (X-Terminal-Id) and the scanner (X-Pairing-Token) --
against the same register, sequentially interleaved and genuinely
concurrently (threads, each with its own SQLite connection, which contend
for SQLite's write lock exactly like separate gunicorn worker processes).
"""
import re
import threading
import time

import pytest

from tests.conftest import login

REG = 'reg-conc'
JSON = {'X-Response-Format': 'json'}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _db(app):
    from app.services.db import get_db_connection
    return get_db_connection(app.config['DATABASE'])


def _add_items(app, *skus, price=1000, qty=100):
    conn = _db(app)
    for sku in skus:
        conn.execute('INSERT INTO inventory_items (sku, name, sell_price_cents, quantity) VALUES (?, ?, ?, ?)',
                     (sku, f'{sku} item', price, qty))
    conn.commit()
    conn.close()


def _set_tax(app, rate):
    from app.services import settings
    with app.app_context():
        settings.set('TAX_RATE', rate)


def _register(app):
    """The register: admin logged in, identifying itself by X-Terminal-Id."""
    client = app.test_client()
    login(client, 1)
    return client


def _reg_headers(**extra):
    return {'X-Terminal-Id': REG, **extra}


def _pair_scanner(app, register_client, peripheral_id='scan-conc'):
    """Pair a scanner to REG through the real flow; return a logged-in
    scanner client and its pairing token."""
    anon = app.test_client()
    code = anon.post('/pos/api/pairing/request-code',
                     json={'peripheral_id': peripheral_id, 'role': 'scanner'}).get_json()['pairing_code']
    claim = register_client.post('/pos/api/register/claim-code',
                                 json={'code': code, 'register_id': REG, 'friendly_name': 'Front'})
    assert claim.get_json()['success']
    token = anon.get(f'/pos/api/pairing/status?peripheral_id={peripheral_id}').get_json()['token']
    anon.post('/pos/api/pairing/ack', json={'peripheral_id': peripheral_id, 'token': token})

    scanner = app.test_client()
    login(scanner, 2)  # the staff member on the handheld is logged in too
    return scanner, token


def _scan_headers(token):
    return {'X-Pairing-Token': token, **JSON}


def _cart(app):
    from app.services import pos_registers
    conn = _db(app)
    cart, version = pos_registers.load_register_cart(conn, REG)
    conn.close()
    return cart, version


def _line(app, sku):
    cart, _ = _cart(app)
    return next(i for i in cart['items'] if i['sku'] == sku)


@pytest.fixture
def pair(fresh_app):
    _add_items(fresh_app, 'AAA', 'BBB', 'CCC', 'DDD')
    register = _register(fresh_app)
    scanner, token = _pair_scanner(fresh_app, register)
    return register, scanner, token


# ---------------------------------------------------------------------------
# line ids
# ---------------------------------------------------------------------------

def test_new_lines_get_unique_line_ids(fresh_app, pair):
    register, scanner, token = pair
    register.post('/pos/cart/add', data={'sku': 'AAA'}, headers=_reg_headers(**JSON))
    scanner.post('/pos/cart/add', data={'sku': 'BBB'}, headers=_scan_headers(token))
    cart, _ = _cart(fresh_app)
    ids = [i['line_id'] for i in cart['items']]
    assert len(ids) == 2 and len(set(ids)) == 2
    assert all(re.match(r'^[A-Za-z][A-Za-z0-9_-]+$', i) for i in ids)
    # Line ids are what the scanner sees in its poll payload
    poll = scanner.get('/pos/api/peripheral/poll?since=-1', headers={'X-Pairing-Token': token}).get_json()
    assert [i['line_id'] for i in poll['cart']['items']] == ids


def test_legacy_cart_lines_get_stable_ids(fresh_app, pair):
    """Carts saved before line ids existed get deterministic ids on load
    (every worker computes the same ones), persisted by the next change."""
    from app.services import pos_registers
    register, scanner, token = pair
    legacy = pos_registers.empty_cart()
    for sku in ('AAA', 'BBB'):
        legacy['items'].append({'sku': sku, 'name': sku, 'quantity': 1, 'unit_price': 10.0,
                                'line_total': 10.0})
    conn = _db(fresh_app)
    pos_registers.save_register_cart(conn, REG, legacy)
    conn.close()

    first, _ = _cart(fresh_app)
    second, _ = _cart(fresh_app)
    ids = [i['line_id'] for i in first['items']]
    assert ids == [i['line_id'] for i in second['items']]
    assert all(ids) and len(set(ids)) == 2

    # The register page renders those ids; removing BBB by its id works and
    # persists AAA's id.
    page = register.get('/pos/', headers=_reg_headers()).get_data(as_text=True)
    assert f'/pos/cart/remove/{ids[1]}' in page
    resp = register.post(f'/pos/cart/remove/{ids[1]}', headers=_reg_headers(**JSON))
    assert resp.status_code == 200
    cart, _ = _cart(fresh_app)
    assert [i['line_id'] for i in cart['items']] == [ids[0]]
    conn = _db(fresh_app)
    raw = conn.execute('SELECT cart_json FROM pos_registers WHERE register_id = ?', (REG,)).fetchone()[0]
    conn.close()
    assert ids[0] in raw


def test_old_index_urls_never_hit_a_line(fresh_app, pair):
    """A stale pre-upgrade page posting /cart/remove/0 must not remove line 0."""
    register, scanner, token = pair
    register.post('/pos/cart/add', data={'sku': 'AAA'}, headers=_reg_headers(**JSON))
    resp = scanner.post('/pos/cart/remove/0', headers=_scan_headers(token))
    assert resp.status_code == 409
    assert resp.get_json()['error'] == 'line_not_found'
    assert len(_cart(fresh_app)[0]['items']) == 1


# ---------------------------------------------------------------------------
# interleaved register + scanner
# ---------------------------------------------------------------------------

def test_remove_by_line_id_after_other_device_shifted_the_list(fresh_app, pair):
    """The register rendered [AAA, BBB, CCC]; the scanner removes AAA (which
    used to shift every index). The register's remove of CCC must remove CCC,
    not BBB."""
    register, scanner, token = pair
    for sku in ('AAA', 'BBB', 'CCC'):
        register.post('/pos/cart/add', data={'sku': sku}, headers=_reg_headers(**JSON))
    rendered = {i['sku']: i['line_id'] for i in _cart(fresh_app)[0]['items']}
    _, rendered_version = _cart(fresh_app)

    resp = scanner.post(f"/pos/cart/remove/{rendered['AAA']}", headers=_scan_headers(token))
    assert resp.status_code == 200

    resp = register.post(f"/pos/cart/remove/{rendered['CCC']}",
                         data={'cart_version': rendered_version}, headers=_reg_headers(**JSON))
    assert resp.status_code == 200
    assert [i['sku'] for i in _cart(fresh_app)[0]['items']] == ['BBB']


def test_update_by_line_id_after_other_device_inserted_lines(fresh_app, pair):
    register, scanner, token = pair
    register.post('/pos/cart/add', data={'sku': 'AAA'}, headers=_reg_headers(**JSON))
    aaa = _line(fresh_app, 'AAA')['line_id']
    # scanner inserts more lines meanwhile
    scanner.post('/pos/cart/add', data={'sku': 'BBB'}, headers=_scan_headers(token))
    scanner.post('/pos/cart/add', data={'sku': 'CCC'}, headers=_scan_headers(token))

    resp = register.post(f'/pos/cart/update/{aaa}', data={'quantity': '4'}, headers=_reg_headers(**JSON))
    assert resp.status_code == 200
    qty = {i['sku']: i['quantity'] for i in _cart(fresh_app)[0]['items']}
    assert qty == {'AAA': 4, 'BBB': 1, 'CCC': 1}


def test_stale_line_gives_clear_error_and_current_cart(fresh_app, pair):
    register, scanner, token = pair
    register.post('/pos/cart/add', data={'sku': 'AAA'}, headers=_reg_headers(**JSON))
    register.post('/pos/cart/add', data={'sku': 'BBB'}, headers=_reg_headers(**JSON))
    aaa = _line(fresh_app, 'AAA')['line_id']
    _, seen_version = _cart(fresh_app)

    # The register removes AAA ...
    register.post(f'/pos/cart/remove/{aaa}', headers=_reg_headers(**JSON))
    # ... then the scanner, still showing AAA, tries to remove / change it.
    for url, data in ((f'/pos/cart/remove/{aaa}', {}),
                      (f'/pos/cart/update/{aaa}', {'quantity_delta': '1'})):
        data['cart_version'] = seen_version
        resp = scanner.post(url, data=data, headers=_scan_headers(token))
        assert resp.status_code == 409
        body = resp.get_json()
        assert body['success'] is False
        assert body['error'] == 'line_not_found'
        assert body['stale'] is True
        assert any('already removed' in m['text'] for m in body['messages'])
        # The current cart comes back, untouched
        assert [i['sku'] for i in body['items']] == ['BBB']
        assert body['version'] == _cart(fresh_app)[1]
    assert [i['sku'] for i in _cart(fresh_app)[0]['items']] == ['BBB']

    # Form-post flavour (register page): flash + redirect, nothing changed
    resp = register.post(f'/pos/cart/remove/{aaa}', headers=_reg_headers(), follow_redirects=True)
    assert 'already removed' in resp.get_data(as_text=True)
    assert [i['sku'] for i in _cart(fresh_app)[0]['items']] == ['BBB']


def test_stale_version_with_existing_line_still_proceeds(fresh_app, pair):
    register, scanner, token = pair
    register.post('/pos/cart/add', data={'sku': 'AAA'}, headers=_reg_headers(**JSON))
    aaa = _line(fresh_app, 'AAA')['line_id']
    _, seen_version = _cart(fresh_app)
    scanner.post('/pos/cart/add', data={'sku': 'BBB'}, headers=_scan_headers(token))  # version moves
    resp = scanner.post(f'/pos/cart/remove/{aaa}', data={'cart_version': seen_version},
                        headers=_scan_headers(token))
    assert resp.status_code == 200
    assert [i['sku'] for i in _cart(fresh_app)[0]['items']] == ['BBB']


def test_scanner_quantity_delta_does_not_clobber_register_add(fresh_app, pair):
    """Scanner shows AAA x1. The register scans AAA again (x2). The scanner's
    '+' must give 3, not the 2 its stale screen implied."""
    register, scanner, token = pair
    scanner.post('/pos/cart/add', data={'sku': 'AAA'}, headers=_scan_headers(token))
    aaa = _line(fresh_app, 'AAA')['line_id']
    register.post('/pos/cart/add', data={'sku': 'AAA'}, headers=_reg_headers(**JSON))
    resp = scanner.post(f'/pos/cart/update/{aaa}', data={'quantity_delta': '1'}, headers=_scan_headers(token))
    assert resp.get_json()['items'][0]['quantity'] == 3
    assert _line(fresh_app, 'AAA')['line_total_cents'] == 3000


def test_clear_from_stale_view_is_refused(fresh_app, pair):
    register, scanner, token = pair
    register.post('/pos/cart/add', data={'sku': 'AAA'}, headers=_reg_headers(**JSON))
    _, seen_version = _cart(fresh_app)
    scanner.post('/pos/cart/add', data={'sku': 'BBB'}, headers=_scan_headers(token))

    resp = register.post('/pos/cart/clear', data={'cart_version': seen_version},
                         headers=_reg_headers(**JSON))
    assert resp.status_code == 409
    assert resp.get_json()['error'] == 'cart_changed'
    assert len(_cart(fresh_app)[0]['items']) == 2

    # Clearing the cart actually on screen works
    _, current = _cart(fresh_app)
    resp = register.post('/pos/cart/clear', data={'cart_version': current}, headers=_reg_headers(**JSON))
    assert resp.status_code == 200
    assert _cart(fresh_app)[0]['items'] == []


def test_versions_bump_once_per_change_and_not_for_noops(fresh_app, pair):
    register, scanner, token = pair
    _, v0 = _cart(fresh_app)
    scanner.post('/pos/cart/add', data={'sku': 'AAA'}, headers=_scan_headers(token))
    _, v1 = _cart(fresh_app)
    assert v1 == v0 + 1
    # removing a missing line / removing an absent coupon changes nothing
    scanner.post('/pos/cart/remove/Lnope', headers=_scan_headers(token))
    register.post('/pos/api/coupon/remove', headers=_reg_headers())
    assert _cart(fresh_app)[1] == v1
    # the register's version poll and the scanner poll both see the change
    reg_poll = register.get(f'/pos/api/register/poll?since={v0}', headers=_reg_headers()).get_json()
    assert reg_poll['changed'] and reg_poll['version'] == v1
    scan_poll = scanner.get(f'/pos/api/peripheral/poll?since={v0}',
                            headers={'X-Pairing-Token': token}).get_json()
    assert scan_poll['changed'] and scan_poll['version'] == v1


def test_hold_and_recall_are_atomic(fresh_app, pair):
    register, scanner, token = pair
    scanner.post('/pos/cart/add', data={'sku': 'AAA'}, headers=_scan_headers(token))
    resp = scanner.post('/pos/cart/hold', data={'note': 'Bob'}, headers=_scan_headers(token))
    assert resp.status_code == 200 and resp.get_json()['item_count'] == 0
    conn = _db(fresh_app)
    held_id = conn.execute('SELECT id FROM pos_held_orders').fetchone()[0]
    conn.close()

    # Recall is refused while a sale is in progress (it would discard it)
    register.post('/pos/cart/add', data={'sku': 'BBB'}, headers=_reg_headers(**JSON))
    register.post(f'/pos/cart/recall/{held_id}', headers=_reg_headers())
    assert [i['sku'] for i in _cart(fresh_app)[0]['items']] == ['BBB']

    register.post('/pos/cart/clear', headers=_reg_headers(**JSON))
    register.post(f'/pos/cart/recall/{held_id}', headers=_reg_headers())
    assert [i['sku'] for i in _cart(fresh_app)[0]['items']] == ['AAA']
    conn = _db(fresh_app)
    assert conn.execute('SELECT COUNT(*) FROM pos_held_orders').fetchone()[0] == 0
    conn.close()


# ---------------------------------------------------------------------------
# real concurrency
# ---------------------------------------------------------------------------

def test_concurrent_adds_from_register_and_scanner_lose_nothing(fresh_app, pair):
    """Register and scanner hammer the same cart at the same time from two
    threads (separate connections = separate 'workers'). Every add lands."""
    register, scanner, token = pair
    n = 15
    barrier = threading.Barrier(2)
    errors = []

    def run(client, headers, skus):
        try:
            barrier.wait()
            for i in range(n):
                resp = client.post('/pos/cart/add', data={'sku': skus[i % len(skus)]}, headers=headers)
                assert resp.status_code == 200, resp.status_code
        except Exception as e:  # surfaced below
            errors.append(e)

    threads = [
        threading.Thread(target=run, args=(register, _reg_headers(**JSON), ['AAA', 'BBB'])),
        threading.Thread(target=run, args=(scanner, _scan_headers(token), ['CCC', 'DDD', 'AAA'])),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors, errors

    cart, version = _cart(fresh_app)
    qty = {i['sku']: i['quantity'] for i in cart['items']}
    # register: AAA x8, BBB x7; scanner: CCC x5, DDD x5, AAA x5
    assert qty == {'AAA': 13, 'BBB': 7, 'CCC': 5, 'DDD': 5}
    assert sum(qty.values()) == 2 * n
    assert len({i['line_id'] for i in cart['items']}) == 4
    assert version >= 2 * n  # one bump per add


def test_mutate_register_cart_serializes_across_connections(fresh_app):
    """While one connection is inside its mutation, another connection's
    mutation waits for it instead of reading the old cart."""
    from app.services import pos_registers

    inside = threading.Event()
    release = threading.Event()
    result = {}

    def slow_add(cart):
        cart['items'].append({'line_id': pos_registers.new_line_id(), 'sku': 'SLOW', 'name': 'slow',
                              'quantity': 1, 'unit_price': 1.0, 'line_total': 1.0})
        inside.set()
        release.wait(10)

    def fast_add(cart):
        cart['items'].append({'line_id': pos_registers.new_line_id(), 'sku': 'FAST', 'name': 'fast',
                              'quantity': 1, 'unit_price': 1.0, 'line_total': 1.0})

    def worker(fn, key):
        conn = _db(fresh_app)
        try:
            result[key] = pos_registers.mutate_register_cart(conn, REG, fn)[1]
        finally:
            conn.close()

    t1 = threading.Thread(target=worker, args=(slow_add, 'slow'))
    t1.start()
    assert inside.wait(10)
    t2 = threading.Thread(target=worker, args=(fast_add, 'fast'))
    t2.start()
    time.sleep(0.3)
    assert 'fast' not in result  # blocked behind the slow mutation's lock
    release.set()
    t1.join(15)
    t2.join(15)

    cart, version = _cart(fresh_app)
    assert [i['sku'] for i in cart['items']] == ['SLOW', 'FAST']
    assert (result['slow'], result['fast'], version) == (1, 2, 2)


# ---------------------------------------------------------------------------
# checkout vs. the scanner
# ---------------------------------------------------------------------------

def _checkout_form(register):
    page = register.get('/pos/checkout', headers=_reg_headers()).get_data(as_text=True)
    total = re.search(r'name="expected_total" value="([^"]+)"', page).group(1)
    version = re.search(r'name="cart_version" value="([^"]+)"', page).group(1)
    token = re.search(r'name="checkout_token" value="([^"]+)"', page).group(1)
    return {'payment_method': 'card', 'expected_total': total, 'cart_version': version,
            'checkout_token': token}


def _orders(app):
    conn = _db(app)
    rows = conn.execute('SELECT * FROM pos_orders').fetchall()
    conn.close()
    return rows


def test_checkout_refused_when_scanner_added_after_page_opened(fresh_app, pair):
    _set_tax(fresh_app, 0.0)
    register, scanner, token = pair
    register.post('/pos/cart/add', data={'sku': 'AAA'}, headers=_reg_headers(**JSON))
    form = _checkout_form(register)
    scanner.post('/pos/cart/add', data={'sku': 'BBB'}, headers=_scan_headers(token))

    resp = register.post('/pos/checkout/process', data=form, headers=_reg_headers())
    assert resp.status_code == 302 and resp.headers['Location'].endswith('/pos/checkout')
    # The refusal is shown on the re-rendered checkout page (with the new total)
    page = register.get('/pos/checkout', headers=_reg_headers()).get_data(as_text=True)
    assert 'cart changed since this checkout page opened' in page
    assert 'name="expected_total" value="20.00"' in page
    assert _orders(fresh_app) == []
    assert sorted(i['sku'] for i in _cart(fresh_app)[0]['items']) == ['AAA', 'BBB']


def test_checkout_version_catches_change_with_same_total(fresh_app, pair):
    """expected_total alone can't see a swap of equal-priced items; the
    cart_version can."""
    _set_tax(fresh_app, 0.0)
    register, scanner, token = pair
    register.post('/pos/cart/add', data={'sku': 'AAA'}, headers=_reg_headers(**JSON))
    form = _checkout_form(register)
    aaa = _line(fresh_app, 'AAA')['line_id']
    scanner.post(f'/pos/cart/remove/{aaa}', headers=_scan_headers(token))
    scanner.post('/pos/cart/add', data={'sku': 'BBB'}, headers=_scan_headers(token))  # same $10

    register.post('/pos/checkout/process', data=form, headers=_reg_headers())
    assert _orders(fresh_app) == []


def test_checkout_sells_and_clears_exactly_the_same_cart(fresh_app, pair, monkeypatch):
    """A scanner add that arrives while the sale is being recorded must not
    be wiped by the post-checkout clear: it waits for the checkout's lock and
    lands in the next (empty) cart, unsold and still visible."""
    _set_tax(fresh_app, 0.0)
    register, scanner, token = pair
    register.post('/pos/cart/add', data={'sku': 'AAA'}, headers=_reg_headers(**JSON))
    form = _checkout_form(register)

    from app.routes.pos import checkout as checkout_mod
    real = checkout_mod.generate_order_number
    late = {}

    def scanner_add():
        late['resp'] = scanner.post('/pos/cart/add', data={'sku': 'BBB'}, headers=_scan_headers(token))

    def racing_order_number(*args, **kwargs):
        # Inside checkout's BEGIN IMMEDIATE: fire the scanner add now. It
        # blocks on the write lock until the checkout commits.
        t = threading.Thread(target=scanner_add)
        late['thread'] = t
        t.start()
        time.sleep(0.4)
        assert 'resp' not in late, 'scanner add should be waiting for the checkout lock'
        return real(*args, **kwargs)

    monkeypatch.setattr(checkout_mod, 'generate_order_number', racing_order_number)
    resp = register.post('/pos/checkout/process', data=form, headers=_reg_headers())
    assert resp.status_code == 302
    late['thread'].join(15)
    assert late['resp'].status_code == 200

    orders = _orders(fresh_app)
    assert len(orders) == 1
    conn = _db(fresh_app)
    sold = [r[0] for r in conn.execute('SELECT sku FROM pos_order_items WHERE order_id = ?',
                                       (orders[0]['id'],)).fetchall()]
    conn.close()
    assert sold == ['AAA']
    # The late item survived the clear and is waiting in the new sale
    assert [i['sku'] for i in _cart(fresh_app)[0]['items']] == ['BBB']


def test_checkout_clears_register_cart_and_bumps_version(fresh_app, pair):
    _set_tax(fresh_app, 0.0)
    register, scanner, token = pair
    scanner.post('/pos/cart/add', data={'sku': 'AAA'}, headers=_scan_headers(token))
    _, before = _cart(fresh_app)
    register.post('/pos/checkout/process', data=_checkout_form(register), headers=_reg_headers())
    assert len(_orders(fresh_app)) == 1
    cart, after = _cart(fresh_app)
    assert cart['items'] == [] and after == before + 1
    poll = scanner.get(f'/pos/api/peripheral/poll?since={before}', headers={'X-Pairing-Token': token}).get_json()
    assert poll['changed'] and poll['cart']['items'] == []


def test_session_fallback_cart_still_works(fresh_app):
    """No register identity: a per-session cart, still line-id addressed."""
    _add_items(fresh_app, 'AAA', 'BBB')
    client = fresh_app.test_client()
    login(client, 2)
    client.post('/pos/cart/add', data={'sku': 'AAA'}, headers=JSON)
    client.post('/pos/cart/add', data={'sku': 'BBB'}, headers=JSON)
    data = client.get('/pos/api/cart').get_json()
    lines = {i['sku']: i['line_id'] for i in data['items']}
    resp = client.post(f"/pos/cart/remove/{lines['AAA']}", headers=JSON)
    assert resp.status_code == 200 and resp.get_json()['version'] is None
    assert [i['sku'] for i in client.get('/pos/api/cart').get_json()['items']] == ['BBB']
    assert client.post(f"/pos/cart/remove/{lines['AAA']}", headers=JSON).status_code == 409
