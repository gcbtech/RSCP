"""
Phone UI, POS: the register (scan, cart, custom item, discount, coupon, hold
and recall), checkout and the receipt.

Every cart change and every sale runs the desktop POS's own functions
(app/routes/pos/sales.py, checkout.py), called in-process, so cart rules,
stock warnings, discount limits, manager approvals, the cart lock and the
one-time checkout token are exactly the desktop's. This module only enforces
the POS access policy those views get from their blueprint, and turns their
redirects and flashes into phone pages and JSON.

Which cart: the same as the desktop POS page on this device. A phone paired
as a handheld rings into its register's cart (session['slaved_register_id']);
otherwise it is its own register (session['terminal_id'], set by
/pos/api/register/hello). static/m/pos.js uses the desktop page's
localStorage keys, so a pairing made on either page holds on both.
"""
import logging
import secrets
from urllib.parse import parse_qs, urlsplit

from flask import (abort, flash, get_flashed_messages, jsonify, make_response, redirect,
                   render_template, request, session, url_for)
from flask_login import current_user

from app.routes.mobile import allowed_modules, mobile_bp
from app.routes.pos import checkout as desktop_checkout
from app.routes.pos import sales as desktop_sales
from app.routes.pos.core import (allow_hold_orders, compute_cart_totals, current_cart_version, get_cart,
                                 pricing_config, require_manager_for_void)
from app.services import pos_registers
from app.services.db import get_request_db
from app.utils.permissions import has_permission

logger = logging.getLogger(__name__)

# Phone action -> desktop view. The first five answer JSON when asked
# (X-Response-Format: json, which pos.js sends); the rest flash and redirect.
CART_VIEWS = {
    'add': desktop_sales.cart_add,
    'update': desktop_sales.cart_update,
    'remove': desktop_sales.cart_remove,
    'clear': desktop_sales.cart_clear,
    'hold': desktop_sales.cart_hold,
    'custom': desktop_sales.cart_add_custom,
    'discount': desktop_sales.cart_discount,
    'recall': desktop_sales.cart_recall,
}
LINE_ACTIONS = ('update', 'remove')
MANAGER_NEEDED = 'Manager approval required'


def _require_pos():
    if 'pos' not in allowed_modules():
        abort(403)


def _has_register():
    """True once this session targets a register cart (handheld or own register)."""
    return bool(session.get('slaved_register_id') or session.get('terminal_id'))


def _linked_register():
    """Name of the register this phone rings into as a handheld, or None."""
    register_id = session.get('slaved_register_id')
    if not register_id:
        return None
    return pos_registers.get_register_name(get_request_db(), register_id) or 'Register'


def _cart_context(cart):
    totals = compute_cart_totals(cart)
    return {'cart': cart, 'totals': totals, 'cart_version': current_cart_version(),
            'allow_hold': allow_hold_orders()}


def _held_orders():
    if not allow_hold_orders():
        return []
    try:
        return get_request_db().execute('''
            SELECT h.id, h.note, h.created_at, u.username AS held_by
            FROM pos_held_orders h LEFT JOIN users u ON u.id = h.operator_id
            ORDER BY h.created_at DESC LIMIT 30
        ''').fetchall()
    except Exception as e:   # the table is created with the POS schema; never fail the page over it
        logger.warning(f'[Mobile] held orders unavailable: {e}')
        return []


def _cart_payload(extra=None):
    """The phone's cart, rendered, plus what the client needs to act on it."""
    cart = get_cart()
    ctx = _cart_context(cart)
    payload = {
        'html': render_template('m/pos/_cart.html', **ctx),
        'foot_html': render_template('m/pos/_cart_foot.html', **ctx),
        'version': ctx['cart_version'],
        'count': sum(int(i.get('quantity') or 0) for i in cart['items']),
        'held_html': render_template('m/pos/_held.html', held=_held_orders()),
    }
    payload.update(extra or {})
    return payload


# ---------------------------------------------------------------- register

@mobile_bp.route('/pos')
def pos():
    _require_pos()
    ctx = _cart_context(get_cart()) if _has_register() else {'cart': None, 'allow_hold': allow_hold_orders()}
    return render_template('m/pos/register.html', active_tab='pos', linked=_linked_register(),
                           held=_held_orders(), can_manage=has_permission(current_user, 'pos.manage'),
                           require_manager_void=require_manager_for_void(), **ctx)


@mobile_bp.route('/pos/cart')
def pos_cart():
    """The cart as it is now (after pos.js has said hello / entered handheld mode)."""
    _require_pos()
    return jsonify(_cart_payload({'linked': _linked_register()}))


@mobile_bp.route('/pos/cart/<action>', methods=['POST'])
@mobile_bp.route('/pos/cart/<action>/<item>', methods=['POST'])
def pos_cart_action(action, item=None):
    """One cart change, run by the desktop view. JSON {ok, error, messages, html, version, ...}."""
    _require_pos()
    view = CART_VIEWS.get(action)
    if view is None:
        abort(404)
    kwargs = {}
    if action in LINE_ACTIONS:
        if not item:
            abort(404)
        kwargs['line_id'] = item
    elif action == 'recall':
        if not (item and item.isdigit()):
            abort(404)
        kwargs['held_id'] = int(item)

    before = get_cart()
    before_items = len(before['items'])
    resp = make_response(view(**kwargs))

    ok, error = True, None
    if resp.mimetype == 'application/json':
        data = resp.get_json(silent=True) or {}
        ok = bool(data.get('success', resp.status_code < 400))
        error = data.get('error')
        messages = data.get('messages') or []
    else:
        messages = [{'category': c, 'text': t} for c, t in get_flashed_messages(with_categories=True)]
    texts = ' '.join(m['text'] for m in messages)

    after = get_cart()
    if action == 'custom':
        ok = len(after['items']) > before_items
    elif action == 'discount':
        if MANAGER_NEEDED in texts:
            ok, error = False, 'manager_required'
        else:
            ok = 'discount applied' in texts.lower()
    elif action == 'recall':
        ok = bool(after['items']) and not before_items
    if error == 'manager_required':
        # the phone asks for the manager in a sheet; no toast for this one
        messages = [m for m in messages if MANAGER_NEEDED not in m['text']
                    and 'Manager authentication required' not in m['text']]
    return jsonify(_cart_payload({'ok': ok, 'error': error if not ok else None, 'messages': messages}))


# ---------------------------------------------------------------- checkout

def _phone_url(location, order_number=None):
    """The phone page for where the desktop checkout redirected."""
    parts = urlsplit(location or '')
    path, query = parts.path, parse_qs(parts.query)
    receipt_prefix = url_for('pos.receipt', order_number='X')[:-1]
    if path.startswith(receipt_prefix):
        return url_for('mobile.pos_receipt', order_number=path[len(receipt_prefix):])
    if query.get('auto_print_order'):        # AUTO_PRINT_RECEIPT: the phone shows the receipt instead
        return url_for('mobile.pos_receipt', order_number=query['auto_print_order'][0])
    if path == url_for('pos.checkout'):
        return url_for('mobile.pos_checkout')
    return url_for('mobile.pos')


@mobile_bp.route('/pos/checkout', methods=['GET', 'POST'])
def pos_checkout():
    _require_pos()
    if request.method == 'POST':
        # The desktop's checkout (pos_checkout.complete_sale): same fields,
        # same token, same messages; only the pages it leads to differ.
        resp = make_response(desktop_checkout.checkout_process())
        return redirect(_phone_url(resp.headers.get('Location')))

    cart = get_cart()
    if not cart['items']:
        flash('The cart is empty.')
        return redirect(url_for('mobile.pos'))
    totals = compute_cart_totals(cart)
    config = pricing_config()
    return render_template('m/pos/checkout.html', active_tab='pos', cart=cart, totals=totals,
                           cart_version=current_cart_version(), linked=_linked_register(),
                           cash_discount_type=config.cash_discount_type,
                           cash_discount_percent=config.cash_discount_percent,
                           # one per rendered checkout: a double tap or a retry records the sale once
                           checkout_token=secrets.token_urlsafe(16))


@mobile_bp.route('/pos/receipt/<order_number>')
def pos_receipt(order_number):
    _require_pos()
    conn = get_request_db()
    order = desktop_checkout._load_receipt_order(conn, order_number)
    if not order:
        flash('That sale was not found.', 'danger')
        return redirect(url_for('mobile.pos'))
    return render_template('m/pos/receipt.html', active_tab='pos', order=order,
                           **desktop_checkout._prepare_receipt_data(conn, order))
