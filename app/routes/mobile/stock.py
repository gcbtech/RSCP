"""
Phone UI, Stock: look an item up (scan or search) and adjust it, the item
list, low stock, audits (start, count, finish, report) and a quick add form.

Writes go through app/services/inventory.py (adjust_stock, record_audit_count,
create_item), the same functions the desktop pages use; the stock buttons on
an item card call the desktop's /inventory/api/quick_adjust. Label printing
stays on the desktop (it drives a DYMO printer attached to a PC).
"""
import json
import logging
from datetime import date

from flask import abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user

from app.routes.inventory import CATEGORY_CODES
from app.routes.mobile import allowed_modules, mobile_bp
from app.services import inventory, money
from app.services.data_manager import load_config
from app.services.db import get_request_db
from app.utils.permissions import has_permission

logger = logging.getLogger(__name__)

ITEMS_PER_PAGE = 50
AUDIT_MODES = {
    'item': ('Item audit', 'Scan every unit; each scan counts one'),
    'shelf': ('Shelf audit', 'Scan each item once and confirm its count'),
}


def _require_stock():
    if 'stock' not in allowed_modules():
        abort(403)


def _low_threshold():
    return int((load_config() or {}).get('LOW_STOCK_THRESHOLD', 5))


def _attention_rows(conn):
    """Out of stock, or at/below the item's own threshold or (without one) the
    global LOW_STOCK_THRESHOLD: the same definition as the overview and Home."""
    t = _low_threshold()
    return conn.execute(f'''
        SELECT id, name, sku, quantity, alert_threshold FROM inventory_items
        {inventory._ATTENTION_WHERE}
        ORDER BY quantity ASC, name ASC LIMIT 300
    ''', (t, t)).fetchall()


def _page(template, section, **ctx):
    conn = get_request_db()
    return render_template(template, active_tab='stock', section=section,
                           low_count=len(_attention_rows(conn)),
                           can_manage=has_permission(current_user, 'inventory.manage'), **ctx)


def _location(item):
    return ' / '.join(filter(None, [item.get('location_area'), item.get('location_aisle'),
                                    item.get('location_shelf'), item.get('location_bin')]))


def _card_item(conn, row):
    """An inventory row as the item card needs it: sale pricing, location, UPC / part number."""
    item = inventory.apply_sale_pricing(dict(row))
    item['location'] = _location(item)
    try:
        ids = json.loads(item.get('secondary_ids') or '{}') or {}
    except (TypeError, ValueError):
        ids = {}
    item['upc'] = ids.get('upc')
    item['part_number'] = ids.get('part_number')
    return item


def _render_card(conn, row, movements=0):
    return render_template('m/stock/_item.html', item=_card_item(conn, row),
                           movements=inventory.item_movements(conn, row['id'], movements) if movements else [],
                           can_manage=has_permission(current_user, 'inventory.manage'))


# ---------------------------------------------------------------- lookup

@mobile_bp.route('/stock')
def stock():
    _require_stock()
    return _page('m/stock/lookup.html', 'lookup')


@mobile_bp.route('/stock/find', methods=['POST'])
def stock_find():
    """A scan or a typed search. JSON {code} -> {kind, feedback, html}:
    kind 'item' (exact SKU / UPC / part number, or the only search match),
    'list' (several matches) or 'none'."""
    _require_stock()
    code = str((request.get_json(silent=True) or {}).get('code') or '').strip()[:200]
    if not code:
        return jsonify(error='Nothing to look up'), 400
    conn = get_request_db()
    item = inventory.find_item_by_code(conn, code)
    if item is None:
        rows, total, _sort, _order = inventory.list_items(conn, search=code, per_page=20)
        if len(rows) == 1:
            item = rows[0]
        else:
            html = render_template('m/stock/_matches.html', rows=rows, total=total, query=code)
            return jsonify(kind='list' if rows else 'none', feedback='ok' if rows else 'bad', html=html)
    return jsonify(kind='item', feedback='ok', html=_render_card(conn, item, movements=5))


@mobile_bp.route('/stock/item/<int:item_id>')
def stock_item(item_id):
    _require_stock()
    conn = get_request_db()
    row = inventory.get_item(conn, item_id)
    if row is None:
        abort(404)
    return _page('m/stock/item.html', 'items', item=row, card=_render_card(conn, row, movements=10))


# ---------------------------------------------------------------- lists

@mobile_bp.route('/stock/items')
def stock_items():
    _require_stock()
    query = (request.args.get('q') or '').strip()
    page = max(1, request.args.get('page', 1, type=int))
    rows, total, _sort, _order = inventory.list_items(get_request_db(), search=query, page=page,
                                                      per_page=ITEMS_PER_PAGE)
    pages = max(1, -(-total // ITEMS_PER_PAGE))
    return _page('m/stock/items.html', 'items', rows=rows, total=total, query=query,
                 page=page, pages=pages)


@mobile_bp.route('/stock/low')
def stock_low():
    _require_stock()
    return _page('m/stock/low.html', 'low', rows=_attention_rows(get_request_db()),
                 threshold=_low_threshold())


# ---------------------------------------------------------------- audits

def _audit_session(conn, session_id, active=True):
    sess = conn.execute('SELECT * FROM audit_sessions WHERE id = ?', (session_id,)).fetchone()
    if sess is None or (active and sess['status'] != 'active'):
        return None
    return sess


def _audit_summary(conn, session_id):
    row = conn.execute('''
        SELECT COUNT(*) AS counted, COALESCE(SUM(counted_qty != expected_qty), 0) AS off
        FROM audit_records WHERE session_id = ?
    ''', (session_id,)).fetchone()
    recent = conn.execute('''
        SELECT name, sku, expected_qty, counted_qty FROM audit_records
        WHERE session_id = ? ORDER BY timestamp DESC, id DESC LIMIT 5
    ''', (session_id,)).fetchall()
    return {'counted': row['counted'], 'off': row['off']}, recent


@mobile_bp.route('/stock/audit')
def stock_audit():
    _require_stock()
    conn = get_request_db()
    active = conn.execute("SELECT * FROM audit_sessions WHERE status = 'active' "
                          "ORDER BY start_time DESC LIMIT 1").fetchone()
    summary = _audit_summary(conn, active['id'])[0] if active else None
    past = conn.execute("SELECT s.*, (SELECT COUNT(*) FROM audit_records r WHERE r.session_id = s.id) AS counted "
                        "FROM audit_sessions s WHERE s.status = 'completed' "
                        "ORDER BY s.end_time DESC LIMIT 10").fetchall()
    return _page('m/stock/audit.html', 'audit', active=active, summary=summary, past=past, modes=AUDIT_MODES)


@mobile_bp.route('/stock/audit/start', methods=['POST'])
def stock_audit_start():
    """Start an audit; one runs at a time (as /inventory/audit/start)."""
    _require_stock()
    mode = request.form.get('mode')
    if mode not in AUDIT_MODES:
        abort(400)
    conn = get_request_db()
    existing = conn.execute("SELECT id, mode FROM audit_sessions WHERE status = 'active' "
                            "ORDER BY start_time DESC LIMIT 1").fetchone()
    if existing:
        if existing['mode'] == mode:
            return redirect(url_for('mobile.stock_audit_live', session_id=existing['id']))
        flash(f"Finish the {AUDIT_MODES[existing['mode']][0].lower()} in progress first.", 'danger')
        return redirect(url_for('mobile.stock_audit'))
    cur = conn.execute('INSERT INTO audit_sessions (user_id, mode) VALUES (?, ?)', (current_user.username, mode))
    conn.commit()
    return redirect(url_for('mobile.stock_audit_live', session_id=cur.lastrowid))


@mobile_bp.route('/stock/audit/<int:session_id>')
def stock_audit_live(session_id):
    _require_stock()
    conn = get_request_db()
    sess = _audit_session(conn, session_id)
    if sess is None:
        flash('That audit is finished or does not exist.', 'danger')
        return redirect(url_for('mobile.stock_audit'))
    summary, recent = _audit_summary(conn, session_id)
    return render_template('m/stock/audit_live.html', active_tab='stock', sess=sess, mode=AUDIT_MODES[sess['mode']],
                           summary=summary, recent=recent)


@mobile_bp.route('/stock/audit/<int:session_id>/scan', methods=['POST'])
def stock_audit_scan(session_id):
    """JSON {code} -> the item to count (by SKU, UPC or part number)."""
    _require_stock()
    conn = get_request_db()
    if _audit_session(conn, session_id) is None:
        return jsonify(error='This audit is finished'), 409
    code = str((request.get_json(silent=True) or {}).get('code') or '').strip()[:200]
    item = inventory.find_item_by_code(conn, code) if code else None
    if item is None:
        return jsonify(error=f'No stock item has the code {code}'), 404
    prev = conn.execute('SELECT counted_qty FROM audit_records WHERE session_id = ? AND item_id = ?',
                        (session_id, item['id'])).fetchone()
    return jsonify(item={'id': item['id'], 'name': item['name'], 'sku': item['sku'],
                         'quantity': item['quantity'] or 0, 'image_url': item['image_url'] or '',
                         'location': _location(item)},
                   prev_count=prev['counted_qty'] if prev else None)


@mobile_bp.route('/stock/audit/<int:session_id>/count', methods=['POST'])
def stock_audit_count(session_id):
    """JSON {item_id, count}: the item's stock becomes the count (recorded on the audit)."""
    _require_stock()
    conn = get_request_db()
    if _audit_session(conn, session_id) is None:
        return jsonify(error='This audit is finished'), 409
    data = request.get_json(silent=True) or {}
    try:
        item_id = int(data.get('item_id'))
        count = int(data.get('count'))
    except (TypeError, ValueError):
        return jsonify(error='Missing item or count'), 400
    if not 0 <= count <= 100000:
        return jsonify(error='The count must be 0 or more'), 400
    try:
        inventory.record_audit_count(conn, session_id, item_id, count, current_user.username)
    except inventory.ItemNotFound:
        return jsonify(error='That item no longer exists'), 404
    summary, recent = _audit_summary(conn, session_id)
    return jsonify(ok=True, summary=summary,
                   recent_html=render_template('m/stock/_audit_recent.html', recent=recent))


@mobile_bp.route('/stock/audit/<int:session_id>/finish', methods=['POST'])
def stock_audit_finish(session_id):
    """Close the audit (counts were applied as they were made) and show its report."""
    _require_stock()
    conn = get_request_db()
    if _audit_session(conn, session_id) is not None:
        conn.execute("UPDATE audit_sessions SET status = 'completed', end_time = CURRENT_TIMESTAMP WHERE id = ?",
                     (session_id,))
        conn.commit()
    return redirect(url_for('mobile.stock_audit_report', session_id=session_id))


@mobile_bp.route('/stock/audit/<int:session_id>/report')
def stock_audit_report(session_id):
    _require_stock()
    conn = get_request_db()
    sess = _audit_session(conn, session_id, active=False)
    if sess is None:
        abort(404)
    records = conn.execute('''
        SELECT item_id, name, sku, expected_qty, counted_qty, (counted_qty - expected_qty) AS diff
        FROM audit_records WHERE session_id = ? ORDER BY ABS(counted_qty - expected_qty) DESC, name
    ''', (session_id,)).fetchall()
    off = [r for r in records if r['diff']]
    return _page('m/stock/audit_report.html', 'audit', sess=sess, mode=AUDIT_MODES[sess['mode']],
                 records=records, off=off)


# ---------------------------------------------------------------- add item

def _safe_next(target):
    return target if target and target.startswith('/m/') and not target.startswith('//') else None


@mobile_bp.route('/stock/add', methods=['GET', 'POST'])
def stock_add():
    """Quick add: the fields that matter at the shelf. Everything else is on
    the desktop item page. Prefilled from a received package (Receive's New item)."""
    _require_stock()
    if not has_permission(current_user, 'inventory.manage'):
        abort(403)
    conf = load_config() or {}
    areas = (conf.get('LOCATION_OPTIONS') or {}).get('area') or []
    source = request.form if request.method == 'POST' else request.args
    form = {key: (source.get(key) or '').strip() for key in
            ('name', 'qty', 'price', 'area', 'shelf', 'upc', 'sku', 'category',
             'tracking', 'image_url', 'asin', 'source_url', 'next')}
    form['category'] = form['category'] if form['category'] in CATEGORY_CODES else 'ATO'
    if request.method == 'GET':
        form['qty'] = form['qty'] or '1'
        return _page('m/stock/add.html', 'add', form=form, areas=areas, categories=CATEGORY_CODES, errors={})

    errors = {}
    if not form['name']:
        errors['name'] = 'Give the item a name.'
    try:
        qty = int(form['qty'] or 0)
        if qty < 0:
            raise ValueError
    except ValueError:
        errors['qty'] = 'Use a whole number, 0 or more.'
        qty = 0
    price_cents = money.parse(form['price']) if form['price'] else None
    if form['price'] and price_cents is None:
        errors['price'] = 'Use a price like 19.99.'
    if not (form['area'] or form['shelf']):
        errors['area'] = 'Pick an area or type a shelf.'
    if errors:
        return _page('m/stock/add.html', 'add', form=form, areas=areas, categories=CATEGORY_CODES,
                     errors=errors), 400

    conn = get_request_db()
    sku = form['sku'] or inventory.generate_sku(conn, form['category'])
    fields = {
        'sku': sku, 'name': form['name'], 'quantity': qty,
        'location_area': form['area'] or None, 'location_shelf': form['shelf'] or None,
        'sell_price_cents': price_cents, 'upc': form['upc'] or None,
        'image_url': form['image_url'] or None, 'asin': form['asin'] or None,
        'source_url': form['source_url'] or None, 'source_tracking': form['tracking'] or None,
        'first_stock_date': date.today().isoformat() if conf.get('TRACK_FIRST_STOCK_DATE') else None,
    }
    try:
        item_id = inventory.create_item(conn, fields, current_user.username)
    except inventory.DuplicateSku:
        errors['sku'] = f'The SKU {sku} is already in use.'
        return _page('m/stock/add.html', 'add', form=form, areas=areas, categories=CATEGORY_CODES,
                     errors=errors), 400
    flash(f"Added {form['name']} ({sku})")
    return redirect(_safe_next(form['next']) or url_for('mobile.stock_item', item_id=item_id))
