"""
Tests for POS pairing v3 (register-hub architecture).

Covers the full peripheral lifecycle (code -> claim -> token -> poll),
authoritative revocation vs transient failure semantics (the contract the
customer displays' unattended auto-reconnect depends on), scanner
mutations landing in the register's cart, totals parity across surfaces,
and the one-time migration from the legacy pairing tables.
"""
import hashlib
import json
import os
import sys
import unittest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import MagicMock
sys.modules['authlib'] = MagicMock()
sys.modules['authlib.integrations'] = MagicMock()
sys.modules['authlib.integrations.flask_client'] = MagicMock()

from app import create_app
from app.services.db import get_db_connection


class PairingV3TestCase(unittest.TestCase):
    def setUp(self):
        self.db_path = os.path.join(os.path.dirname(__file__), 'test_pos_pairing.db')
        self._remove_db()

        self.app = create_app(test_config={
            'TESTING': True,
            'DATABASE': self.db_path,
            'SECRET_KEY': 'test_key',
            'WTF_CSRF_ENABLED': False,
            'RATELIMIT_ENABLED': False,
            'POS_ENABLED': True,
        })
        self.client = self.app.test_client()

        # NOTE: no long-lived app context here. Keeping one pushed makes
        # Flask reuse it for every test-client request, so flask_login's
        # per-request user cache on `g` leaks across requests.
        with self.app.app_context():
            from app.services.migration import ensure_db_ready
            ensure_db_ready()

            conn = get_db_connection()
            from werkzeug.security import generate_password_hash
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, is_admin, roles) VALUES (?, ?, 1, '[\"super_admin\"]')",
                ('tester', generate_password_hash('pw'))
            )
            self.user_id = cur.lastrowid
            conn.execute('''
                INSERT INTO inventory_items (sku, name, sell_price, quantity, sale_enabled, sale_price, sale_start, sale_end, sale_end_on_stock)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', ('TEST-SKU', 'Test Item', 10.0, 5, 0, 5.0, None, None, 0))
            conn.commit()
            conn.close()

    def tearDown(self):
        self._remove_db()

    def _remove_db(self):
        for suffix in ('', '-wal', '-shm'):
            path = self.db_path + suffix
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass

    def login(self, client=None):
        with (client or self.client).session_transaction() as sess:
            sess['_user_id'] = str(self.user_id)

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    def request_code(self, peripheral_id, role):
        resp = self.client.post('/pos/api/pairing/request-code',
                                json={'peripheral_id': peripheral_id, 'role': role})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(len(data['pairing_code']), 6)
        return data['pairing_code']

    def pair(self, peripheral_id, role, register_id='reg-1', register_name='Front Register'):
        """Full pairing flow; returns the peripheral's bearer token."""
        code = self.request_code(peripheral_id, role)

        self.login()
        resp = self.client.post('/pos/api/register/claim-code', json={
            'code': code,
            'register_id': register_id,
            'friendly_name': register_name,
        })
        self.assertEqual(resp.status_code, 200)
        claim = resp.get_json()
        self.assertTrue(claim['success'])
        self.assertEqual(claim['role'], role)

        resp = self.client.get(f'/pos/api/pairing/status?peripheral_id={peripheral_id}')
        status = resp.get_json()
        self.assertTrue(status['paired'])
        self.assertIn('token', status)
        token = status['token']

        resp = self.client.post('/pos/api/pairing/ack',
                                json={'peripheral_id': peripheral_id, 'token': token})
        self.assertTrue(resp.get_json()['success'])
        return token

    def poll(self, token, since=-1, client=None):
        c = client or self.client
        return c.get(f'/pos/api/peripheral/poll?since={since}',
                     headers={'X-Pairing-Token': token})

    # -----------------------------------------------------------------
    # POS auth redirect contract
    # -----------------------------------------------------------------

    def test_pos_login_redirect_endpoint_exists(self):
        """check_pos_enabled (and the permission decorators) redirect
        unauthenticated users to the login endpoint. That endpoint is
        'auth.login'. A stale 'main.login' reference builds fine under
        TESTING (which short-circuits the redirect) but raises BuildError
        -> HTTP 500 in production, which is exactly how /pos/scan broke on
        first deploy. Lock the endpoint name so it can't regress silently."""
        endpoints = {r.endpoint for r in self.app.url_map.iter_rules()}
        self.assertIn('auth.login', endpoints)
        self.assertNotIn('main.login', endpoints)
        with self.app.test_request_context('/pos/scan'):
            from flask import url_for
            # The redirect passes next=request.url, so it must build with it
            self.assertIn('/login', url_for('auth.login', next='/pos/scan'))

        # Guard the actual references: a url_for('main.login') anywhere in the
        # auth-gate paths is a latent 500 no request test can see under TESTING.
        app_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app')
        for rel in ('routes/pos/core.py', 'utils/permissions.py'):
            src = open(os.path.join(app_dir, rel.replace('/', os.sep)), encoding='utf-8').read()
            self.assertNotIn("main.login", src,
                             f"{rel} references non-existent endpoint 'main.login' (use 'auth.login')")

    # -----------------------------------------------------------------
    # Pairing lifecycle
    # -----------------------------------------------------------------

    def test_display_pairing_lifecycle(self):
        token = self.pair('disp-1', 'display')

        # Token was delivered once and acked: status must not expose it again
        status = self.client.get('/pos/api/pairing/status?peripheral_id=disp-1').get_json()
        self.assertTrue(status['paired'])
        self.assertNotIn('token', status)

        # Token stored hashed, never in plaintext
        with self.app.app_context():
            conn = get_db_connection()
            row = conn.execute('SELECT token_hash, pending_token FROM pos_peripherals WHERE peripheral_id = ?',
                               ('disp-1',)).fetchone()
            conn.close()
        self.assertEqual(row['token_hash'], hashlib.sha256(token.encode()).hexdigest())
        self.assertIsNone(row['pending_token'])

        # First poll returns the (empty) cart and the register name
        data = self.poll(token).get_json()
        self.assertTrue(data['success'])
        self.assertTrue(data['changed'])
        self.assertEqual(data['register_name'], 'Front Register')
        self.assertEqual(data['cart']['items'], [])

    def test_expired_code_rejected(self):
        code = self.request_code('disp-exp', 'display')
        with self.app.app_context():
            conn = get_db_connection()
            conn.execute("UPDATE pos_pairing_codes SET created_at = datetime('now', '-11 minutes') WHERE code = ?",
                         (code,))
            conn.commit()
            conn.close()

        self.login()
        resp = self.client.post('/pos/api/register/claim-code', json={
            'code': code, 'register_id': 'reg-1', 'friendly_name': 'R1'})
        self.assertEqual(resp.status_code, 404)

    def test_code_rerequest_replaces_old_code(self):
        code1 = self.request_code('disp-2', 'display')
        code2 = self.request_code('disp-2', 'display')
        self.assertNotEqual(code1, code2)

        self.login()
        # Old code is gone; new code works
        resp = self.client.post('/pos/api/register/claim-code', json={
            'code': code1, 'register_id': 'reg-1', 'friendly_name': 'R1'})
        self.assertEqual(resp.status_code, 404)
        resp = self.client.post('/pos/api/register/claim-code', json={
            'code': code2, 'register_id': 'reg-1', 'friendly_name': 'R1'})
        self.assertTrue(resp.get_json()['success'])

    # -----------------------------------------------------------------
    # Poll semantics & auto-reconnect contract
    # -----------------------------------------------------------------

    def test_poll_version_semantics(self):
        token = self.pair('disp-3', 'display')
        first = self.poll(token).get_json()
        version = first['version']

        # Nothing changed: cheap response, no cart payload
        again = self.poll(token, since=version).get_json()
        self.assertFalse(again['changed'])
        self.assertNotIn('cart', again)

        # Register cart changes -> next poll delivers the new cart
        from app.services import pos_registers
        with self.app.app_context():
            conn = get_db_connection()
            cart = pos_registers.empty_cart()
            cart['items'].append({'sku': 'TEST-SKU', 'name': 'Test Item', 'quantity': 2,
                                  'unit_price': 10.0, 'line_total': 20.0})
            pos_registers.save_register_cart(conn, 'reg-1', cart)
            conn.close()

        changed = self.poll(token, since=version).get_json()
        self.assertTrue(changed['changed'])
        self.assertGreater(changed['version'], version)
        self.assertEqual(len(changed['cart']['items']), 1)
        self.assertAlmostEqual(changed['cart']['subtotal'], 20.0)

    def test_reconnect_with_fresh_session(self):
        """A display that reboots overnight has no cookies, only its token.
        Pairing state lives server-side, so polling must just work."""
        token = self.pair('disp-4', 'display')

        fresh_client = self.app.test_client()  # no session, no login
        data = self.poll(token, client=fresh_client).get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['register_name'], 'Front Register')

    def test_revoked_token_gets_authoritative_403(self):
        token = self.pair('disp-5', 'display')

        self.login()
        resp = self.client.post('/pos/api/register/unpair',
                                json={'peripheral_id': 'disp-5', 'register_id': 'reg-1'})
        self.assertTrue(resp.get_json()['success'])

        resp = self.poll(token)
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(resp.get_json()['revoked'])

    def test_peripheral_self_unpair(self):
        token = self.pair('disp-6', 'display')
        resp = self.client.post('/pos/api/peripheral/unpair', json={},
                                headers={'X-Pairing-Token': token})
        self.assertTrue(resp.get_json()['success'])
        self.assertEqual(self.poll(token).status_code, 403)

    # -----------------------------------------------------------------
    # Scanner: mutations land in the register's cart
    # -----------------------------------------------------------------

    def test_scanner_add_lands_in_register_cart(self):
        display_token = self.pair('disp-7', 'display', register_id='reg-2', register_name='Register 2')
        scanner_token = self.pair('scan-1', 'scanner', register_id='reg-2', register_name='Register 2')

        self.login()
        resp = self.client.post('/pos/cart/add',
                                data={'sku': 'TEST-SKU', 'quantity': 1},
                                headers={'X-Pairing-Token': scanner_token,
                                         'X-Response-Format': 'json'})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['item_count'], 1)
        self.assertAlmostEqual(data['subtotal'], 10.0)

        # The paired customer display sees the scanner's item
        display_view = self.poll(display_token).get_json()
        self.assertEqual(len(display_view['cart']['items']), 1)
        self.assertEqual(display_view['cart']['items'][0]['sku'], 'TEST-SKU')

        # And the register's version poll reports the change
        reg_poll = self.client.get('/pos/api/register/poll?since=-1',
                                   headers={'X-Terminal-Id': 'reg-2'}).get_json()
        self.assertTrue(reg_poll['changed'])
        self.assertGreaterEqual(reg_poll['version'], 1)

    def test_slaved_handheld_full_pos_flow(self):
        """A logged-in handheld with a scanner token can enter 'slave' mode:
        the session binds to the register, and plain cart posts + checkout
        (no X-Pairing-Token header) route to the register's cart."""
        scanner_token = self.pair('scan-slave', 'scanner', register_id='reg-slave',
                                   register_name='Slave Register')

        # The handheld is a SEPARATE logged-in device (its own session),
        # exactly like a real FZ-N1 that isn't the register that claimed it.
        hh = self.app.test_client()
        self.login(hh)

        # Enter slave mode -> session bound to the register
        resp = hh.post('/pos/api/scanner/enter-slave', json={'token': scanner_token})
        self.assertEqual(resp.status_code, 200)
        d = resp.get_json()
        self.assertTrue(d['success'])
        self.assertEqual(d['register_id'], 'reg-slave')
        self.assertEqual(d['register_name'], 'Slave Register')

        # A plain cart add (NO token header) now lands in the register cart
        resp = hh.post('/pos/cart/add',
                       data={'sku': 'TEST-SKU', 'quantity': 2},
                       headers={'X-Response-Format': 'json'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['item_count'], 1)

        # The register machine sees the handheld's item
        from app.services import pos_registers
        with self.app.app_context():
            conn = get_db_connection()
            cart, _v = pos_registers.load_register_cart(conn, 'reg-slave')
            conn.close()
        self.assertEqual(len(cart['items']), 1)
        self.assertEqual(cart['items'][0]['quantity'], 2)

        # The /pos/ sales page renders in the slave session without error
        self.assertEqual(hh.get('/pos/').status_code, 200)

        # Exit slave -> the handheld's own session cart no longer routes to
        # the register (this device never claimed a register, so it's empty)
        resp = hh.post('/pos/api/scanner/exit-slave', json={'token': scanner_token})
        self.assertTrue(resp.get_json()['success'])
        after = hh.get('/pos/api/cart').get_json()
        self.assertEqual(after['item_count'], 0)

    def test_enter_slave_rejects_display_token(self):
        display_token = self.pair('disp-slave', 'display', register_id='reg-ds')
        self.login()
        resp = self.client.post('/pos/api/scanner/enter-slave', json={'token': display_token})
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(resp.get_json()['revoked'])

    def test_enter_slave_rejects_revoked_token(self):
        scanner_token = self.pair('scan-rev', 'scanner', register_id='reg-rev')
        self.login()
        self.client.post('/pos/api/register/unpair',
                         json={'peripheral_id': 'scan-rev', 'register_id': 'reg-rev'})
        resp = self.client.post('/pos/api/scanner/enter-slave', json={'token': scanner_token})
        self.assertEqual(resp.status_code, 403)

    def test_revoked_scanner_cannot_mutate(self):
        scanner_token = self.pair('scan-2', 'scanner', register_id='reg-3')
        self.login()
        self.client.post('/pos/api/register/unpair',
                         json={'peripheral_id': 'scan-2', 'register_id': 'reg-3'})

        resp = self.client.post('/pos/cart/add',
                                data={'sku': 'TEST-SKU', 'quantity': 1},
                                headers={'X-Pairing-Token': scanner_token,
                                         'X-Response-Format': 'json'})
        self.assertEqual(resp.status_code, 401)
        self.assertTrue(resp.get_json()['revoked'])

    def test_display_token_cannot_mutate(self):
        display_token = self.pair('disp-8', 'display', register_id='reg-4')
        self.login()
        resp = self.client.post('/pos/cart/add',
                                data={'sku': 'TEST-SKU', 'quantity': 1},
                                headers={'X-Pairing-Token': display_token,
                                         'X-Response-Format': 'json'})
        self.assertEqual(resp.status_code, 401)

    # -----------------------------------------------------------------
    # Totals parity (coupon + cash discount on every surface)
    # -----------------------------------------------------------------

    def test_totals_parity_with_coupon_and_cash_discount(self):
        token = self.pair('disp-9', 'display', register_id='reg-5')

        from app.routes.pos.core import set_pos_setting
        from app.services import pos_registers
        with self.app.app_context():
            set_pos_setting('TAX_RATE', '0.10')
            set_pos_setting('CASH_DISCOUNT_ENABLED', 'true')
            set_pos_setting('CASH_DISCOUNT_TYPE', 'percent')
            set_pos_setting('CASH_DISCOUNT_AMOUNT', '5')

            conn = get_db_connection()
            cart = pos_registers.empty_cart()
            cart['items'].append({'sku': 'TEST-SKU', 'name': 'Test Item', 'quantity': 10,
                                  'unit_price': 10.0, 'line_total': 100.0})
            cart['discount_amount'] = 10
            cart['discount_type'] = 'fixed'
            cart['applied_coupon'] = {'code': 'SAVE5', 'name': 'Save 5', 'discount': 5.0}
            pos_registers.save_register_cart(conn, 'reg-5', cart)
            conn.close()

        display = self.poll(token).get_json()['cart']
        # 100 - 10 (order) - 5 (coupon) = 85 taxable; 10% tax = 8.50
        self.assertAlmostEqual(display['subtotal'], 100.0)
        self.assertAlmostEqual(display['order_discount'], 10.0)
        self.assertAlmostEqual(display['coupon_discount'], 5.0)
        self.assertAlmostEqual(display['tax_amount'], 8.50)
        self.assertAlmostEqual(display['card_total'], 93.50)
        self.assertTrue(display['cash_discount_enabled'])
        # 5% of 85 = 4.25 cash discount
        self.assertAlmostEqual(display['cash_discount_value'], 4.25)
        self.assertAlmostEqual(display['cash_total'], 89.25)

        # The register's own /api/cart must agree exactly (the old system
        # showed different totals on the register vs the display when a
        # coupon was applied).
        self.login()
        register_view = self.client.get('/pos/api/cart',
                                        headers={'X-Terminal-Id': 'reg-5'}).get_json()
        for key in ('subtotal', 'order_discount', 'coupon_discount', 'tax_amount',
                    'card_total', 'cash_discount_value', 'cash_total'):
            self.assertAlmostEqual(register_view[key], display[key], msg=key)

    # -----------------------------------------------------------------
    # Legacy migration & shims
    # -----------------------------------------------------------------

    def test_legacy_pairing_migration(self):
        with self.app.app_context():
            self._run_legacy_migration_setup()

        # The device's stored token still works: v3 poll AND legacy shim
        data = self.poll('legacy-token-abc123').get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['register_name'], 'Old Register')

        shim = self.client.get(
            '/pos/api/customer-display/cart?customer_terminal_token=legacy-token-abc123').get_json()
        self.assertTrue(shim['success'])
        self.assertTrue(shim['paired'])
        self.assertEqual(shim['staff_friendly_name'], 'Old Register')
        self.assertIn('card_total', shim['cart'])

    def _run_legacy_migration_setup(self):
        conn = get_db_connection()
        conn.execute('''
            CREATE TABLE IF NOT EXISTS pos_customer_display_pairings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_terminal_id TEXT UNIQUE NOT NULL,
                customer_terminal_token TEXT UNIQUE NOT NULL,
                staff_terminal_id TEXT NOT NULL,
                staff_friendly_name TEXT,
                customer_last_seen TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS pos_active_terminals (
                terminal_id TEXT PRIMARY KEY,
                friendly_name TEXT NOT NULL,
                cart_data TEXT,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        old_token = 'legacy-token-abc123'
        conn.execute('''INSERT INTO pos_customer_display_pairings
                        (customer_terminal_id, customer_terminal_token, staff_terminal_id, staff_friendly_name)
                        VALUES (?, ?, ?, ?)''',
                     ('old-disp', old_token, 'old-reg', 'Old Register'))
        conn.execute('''INSERT INTO pos_active_terminals (terminal_id, friendly_name, cart_data)
                        VALUES (?, ?, ?)''',
                     ('old-reg', 'Old Register', json.dumps({'items': []})))
        conn.commit()

        from app.services.migration import _migrate_legacy_pairing_tables
        _migrate_legacy_pairing_tables(conn)

        # Peripheral migrated with hashed token; legacy tables dropped
        row = conn.execute('SELECT register_id, role, token_hash FROM pos_peripherals WHERE peripheral_id = ?',
                           ('old-disp',)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row['register_id'], 'old-reg')
        self.assertEqual(row['role'], 'display')
        self.assertEqual(row['token_hash'], hashlib.sha256(old_token.encode()).hexdigest())
        for legacy in ('pos_customer_display_pairings', 'pos_active_terminals'):
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                  (legacy,)).fetchone()
            self.assertIsNone(exists, f'{legacy} should be dropped')
        conn.close()

    def test_register_hello_and_peripheral_listing(self):
        token = self.pair('disp-10', 'display', register_id='reg-6', register_name='Back Register')
        self.login()
        self.client.post('/pos/api/register/hello',
                         json={'register_id': 'reg-6', 'friendly_name': 'Back Register'})

        # Peripheral appears in the register's device list; polling marks it connected
        self.poll(token)
        listing = self.client.get('/pos/api/register/peripherals?register_id=reg-6').get_json()
        self.assertTrue(listing['success'])
        self.assertEqual(len(listing['peripherals']), 1)
        p = listing['peripherals'][0]
        self.assertEqual(p['peripheral_id'], 'disp-10')
        self.assertEqual(p['role'], 'display')
        self.assertTrue(p['connected'])


if __name__ == '__main__':
    unittest.main()
