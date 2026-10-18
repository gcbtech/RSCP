"""Regression tests for the second-round review findings."""
import pytest

from tests.conftest import login


def _db(app):
    from app.services.db import get_db_connection
    return get_db_connection(app.config['DATABASE'])


def _seed_cart_with_coupon(app, client):
    conn = _db(app)
    conn.execute("INSERT OR REPLACE INTO pos_settings (key, value) VALUES ('TAX_RATE', '0')")
    a = conn.execute("INSERT INTO inventory_items (sku, name, sell_price, quantity) VALUES ('CA', 'A', 30, 9)").lastrowid
    b = conn.execute("INSERT INTO inventory_items (sku, name, sell_price, quantity) VALUES ('CB', 'B', 4, 9)").lastrowid
    conn.execute("INSERT INTO pos_coupons (name, code, coupon_type, discount_type, discount_value, active) "
                 "VALUES ('Ten', 'TEN', 'standard', 'order_percent', 10, 1)")
    conn.commit()
    conn.close()
    with app.app_context():
        from app.routes.pos.core import set_pos_setting
        set_pos_setting('TAX_RATE', '0')
    login(client, 2)
    with client.session_transaction() as sess:
        sess['pos_cart'] = {
            'items': [
                {'line_id': 'L1', 'inventory_item_id': a, 'sku': 'CA', 'name': 'A', 'quantity': 1,
                 'unit_price': 30.0, 'discount_amount': 0, 'discount_type': None, 'line_total': 30.0},
                {'line_id': 'L2', 'inventory_item_id': b, 'sku': 'CB', 'name': 'B', 'quantity': 1,
                 'unit_price': 4.0, 'discount_amount': 0, 'discount_type': None, 'line_total': 4.0},
            ],
            'discount_amount': 0, 'discount_type': None, 'discount_reason': '',
        }
    r = client.post('/pos/api/coupon/apply', json={'code': 'TEN'})
    assert r.status_code == 200, r.get_data(as_text=True)


def test_coupon_discount_follows_cart_changes(fresh_app, fresh_client):
    """10% of $34 is $3.40; after removing the $4 line it must be 10% of $30."""
    _seed_cart_with_coupon(fresh_app, fresh_client)
    with fresh_client.session_transaction() as sess:
        assert sess['pos_cart']['applied_coupon']['discount'] == pytest.approx(3.40)
    fresh_client.post('/pos/cart/remove/L2', headers={'X-Response-Format': 'json'})
    with fresh_client.session_transaction() as sess:
        assert sess['pos_cart']['applied_coupon']['discount'] == pytest.approx(3.00)


def test_coupon_that_no_longer_qualifies_is_removed(fresh_app, fresh_client):
    _seed_cart_with_coupon(fresh_app, fresh_client)
    conn = _db(fresh_app)
    conn.execute("UPDATE pos_coupons SET min_purchase = 31 WHERE code = 'TEN'")
    conn.commit()
    conn.close()
    fresh_client.post('/pos/cart/remove/L2', headers={'X-Response-Format': 'json'})
    with fresh_client.session_transaction() as sess:
        assert sess['pos_cart'].get('applied_coupon') is None


def test_reprice_scales_coupon_and_cash_discount_with_removed_items():
    from app.services.pos_sales import reprice_sale
    adj = {'discount_pct': None, 'discount_fixed': 0.0, 'coupon': 10.0, 'cash_discount': 3.0,
           'tax_rate': 0.0, 'orig_subtotal': 100.0}
    # Same subtotal: unchanged
    assert reprice_sale(100.0, adj)[2] == pytest.approx(87.0)
    # Half the sale removed: coupon $5, cash discount $1.50
    assert reprice_sale(50.0, adj)[2] == pytest.approx(43.5)
    # Never grows when the subtotal goes up
    assert reprice_sale(150.0, adj)[2] == pytest.approx(137.0)


def test_add_item_with_junk_threshold_flashes_instead_of_500(fresh_client):
    login(fresh_client, 1)
    r = fresh_client.post('/inventory/add', data={'name': 'Widget', 'quantity': '1',
                                                  'alert_threshold': 'abc', 'category': 'ATO'})
    assert r.status_code in (200, 302)   # re-rendered or redirected with a message, never a 500
