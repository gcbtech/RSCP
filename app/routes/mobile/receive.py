"""
Phone UI, Receive: scan packages in (and act on what was scanned), today's
expected list, past due, returns and refunds, history.

Writes go through app/services/receiving.py and inventory.adjust_stock, the
same functions the desktop pages use. The list queries mirror the desktop
pages' (app/routes/main.py).
"""
import logging
import re

from flask import abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user

from app.routes.mobile import allowed_modules, mobile_bp
from app.services import inventory, receiving
from app.services.data_manager import get_dashboard_stats, load_config
from app.services.db import get_request_db
from app.utils.helpers import guess_shipper
from app.utils.permissions import has_permission

logger = logging.getLogger(__name__)

# Values as on the desktop return form (templates/return_mode.html), so both
# record the same reasons.
RETURN_REASONS = (
    ('Defective', 'Defective'),
    ('Wrong Item', 'Wrong item'),
    ('Damaged', 'Damaged in transit'),
    ('No Longer Needed', 'No longer needed'),
    ('Other', 'Other'),
)

ADD_STOCK_MAX = 1000

# Scan result -> beep pattern (static/m/scanner.js) and session-list label
FEEDBACK = {'received': 'ok', 'inventory': 'ok', 'priority': 'priority',
            'duplicate': 'warn', 'unknown': 'bad', 'error': 'bad'}

HISTORY_ACTIONS = {
    'received': ('Received', 'ok'),
    'return_initiated': ('Return started', 'wn'),
    'refunded': ('Refunded', 'nu'),
}


def _require_receive():
    if 'receive' not in allowed_modules():
        abort(403)


def _inventory_on():
    return 'stock' in allowed_modules()


def _page(template, section, **ctx):
    return render_template(template, active_tab='receive', section=section,
                           counts=get_dashboard_stats(), **ctx)


# ---------------------------------------------------------------- scanning

@mobile_bp.route('/receive')
def receive():
    _require_receive()
    return _page('m/receive/scan.html', 'scan', inventory_on=_inventory_on())


def _scan_label(result):
    n = len(result.items)
    items = f"{n} item{'s' if n != 1 else ''}"
    return {
        'received': f'Received · {items}',
        'priority': f'Priority · {items}',
        'duplicate': 'Already scanned',
        'unknown': 'Not in manifest',
        'inventory': f"Stock item · {result.inventory_item['name']}" if result.inventory_item else 'Stock item',
    }[result.kind]


@mobile_bp.route('/receive/scan', methods=['POST'])
def receive_scan():
    """One scan from the phone. JSON {code} -> {kind, feedback, label, code, html}."""
    _require_receive()
    data = request.get_json(silent=True) or {}
    code = str(data.get('code') or '').strip()[:200]
    if not code:
        return jsonify(error='Nothing was scanned'), 400

    inventory_on = _inventory_on()
    try:
        result = receiving.receive_scan(get_request_db(), code, current_user.username,
                                        inventory_enabled=inventory_on, conf=load_config() or {})
    except Exception as e:
        logger.error(f'[Mobile] scan failed for {code!r}: {e}')
        html = render_template('m/receive/_result.html', kind='error', code=code, error=str(e))
        return jsonify(kind='error', feedback='bad', label='Scan failed', code=code, html=html)

    shown_code = result.inventory_item['sku'] if result.kind == 'inventory' else (result.tracking or code)
    html = render_template('m/receive/_result.html', kind=result.kind, code=shown_code, result=result,
                           inventory_on=inventory_on,
                           can_add_item=inventory_on and has_permission(current_user, 'inventory.manage'))
    expected = get_dashboard_stats()['expected']   # keeps the Expected chip current
    return jsonify(kind=result.kind, feedback=FEEDBACK[result.kind], label=_scan_label(result),
                   code=shown_code, html=html, expected=f"{expected['scanned']}/{expected['total']}")


@mobile_bp.route('/receive/add-stock', methods=['POST'])
def receive_add_stock():
    """Add a received package's units to the inventory item it restocks."""
    _require_receive()
    if not _inventory_on():
        abort(403)
    data = request.get_json(silent=True) or {}
    try:
        item_id = int(data.get('item_id'))
        qty = int(data.get('qty'))
    except (TypeError, ValueError):
        return jsonify(error='Missing item or quantity'), 400
    if not 1 <= qty <= ADD_STOCK_MAX:
        return jsonify(error=f'Quantity must be between 1 and {ADD_STOCK_MAX}'), 400
    tracking = str(data.get('tracking') or '').strip()[:100]

    conn = get_request_db()
    try:
        res = inventory.adjust_stock(conn, item_id, qty, inventory.REASON_RECEIVE, current_user.username,
                                     tracking, ref_type=inventory.REF_PACKAGE,
                                     ref_id=inventory.package_ref(conn, tracking))
    except inventory.ItemNotFound:
        return jsonify(error='That item no longer exists'), 404
    return jsonify(ok=True, new_quantity=res.new_quantity, name=res.name)


# ---------------------------------------------------------------- lists

@mobile_bp.route('/receive/expected')
def receive_expected():
    """Today's packages not scanned yet, grouped by shipper (as /expected)."""
    _require_receive()
    rows = get_request_db().execute(
        "SELECT item_name, tracking_number FROM packages WHERE status IN ('expected', 'on_time') "
        "AND date_scanned IS NULL AND date_expected = date('now', 'localtime') ORDER BY item_name").fetchall()
    groups = {}
    for r in rows:
        groups.setdefault(guess_shipper(r['tracking_number']), []).append(r)
    return _page('m/receive/expected.html', 'expected', groups=sorted(groups.items()), total=len(rows))


@mobile_bp.route('/receive/past-due')
def receive_past_due():
    _require_receive()
    rows = get_request_db().execute(
        "SELECT item_name, tracking_number, date_expected FROM packages "
        "WHERE status = 'past_due' AND date_scanned IS NULL ORDER BY date_expected, item_name").fetchall()
    return _page('m/receive/past_due.html', 'past_due', rows=rows)


def _parse_return_details(details):
    """'Reason: Defective, New Track: 1Z...' (receiving.initiate_return) -> (reason, return tracking)."""
    m = re.match(r'Reason: (.*?), New Track: (.*)$', details or '')
    if not m:
        return None, None
    return m.group(1) or None, (m.group(2) if m.group(2) not in ('', 'None') else None)


@mobile_bp.route('/receive/returns')
def receive_returns():
    _require_receive()
    rows = get_request_db().execute('''
        SELECT p.item_name, p.tracking_number,
               (SELECT h.details FROM history h WHERE h.package_id = p.id AND h.action = 'return_initiated'
                ORDER BY h.timestamp DESC LIMIT 1) AS details
        FROM packages p WHERE p.status = 'return_pending' ORDER BY p.item_name
    ''').fetchall()
    returns = []
    for r in rows:
        reason, return_tracking = _parse_return_details(r['details'])
        returns.append({'name': r['item_name'], 'tracking': r['tracking_number'],
                        'reason': reason, 'return_tracking': return_tracking})
    return _page('m/receive/returns.html', 'returns', returns=returns)


@mobile_bp.route('/receive/returns/refunded', methods=['POST'])
def receive_mark_refunded():
    _require_receive()
    tracking = (request.form.get('tracking') or '').strip()
    if tracking:
        receiving.mark_refunded(get_request_db(), tracking, current_user.username)
        flash(f'Marked refunded: {tracking}')
    return redirect(url_for('mobile.receive_returns'))


@mobile_bp.route('/receive/return', methods=['GET', 'POST'])
def receive_start_return():
    """Start a return: find the package, then record the reason (as /return_mode)."""
    _require_receive()
    conn = get_request_db()
    if request.method == 'POST':
        original = (request.form.get('original_tracking') or '').strip()
        reason = request.form.get('reason') or ''
        if not original or reason not in dict(RETURN_REASONS):
            flash('Pick a package and a reason.', 'danger')
            return redirect(url_for('mobile.receive_start_return'))
        receiving.initiate_return(conn, original, (request.form.get('return_tracking') or '').strip(),
                                  reason, current_user.username)
        flash(f'Return started: {original}')
        return redirect(url_for('mobile.receive_returns'))

    picked = None
    track = (request.args.get('track') or '').strip()
    query = (request.args.get('q') or '').strip()
    if track:
        picked = conn.execute("SELECT item_name, tracking_number, status FROM packages WHERE tracking_number = ?",
                              (track,)).fetchone()
    if query:
        like = f'%{query}%'
        rows = conn.execute('''
            SELECT item_name, tracking_number, date_expected FROM packages
            WHERE (item_name LIKE ? OR tracking_number LIKE ?) AND status NOT IN ('refunded', 'return_pending')
            ORDER BY date_expected DESC LIMIT 50
        ''', (like, like)).fetchall()
    else:
        rows = conn.execute('''
            SELECT item_name, tracking_number, date_expected FROM packages
            WHERE status NOT IN ('return_pending', 'refunded') ORDER BY date_expected DESC LIMIT 20
        ''').fetchall()
    return _page('m/receive/start_return.html', 'returns', picked=picked, query=query, rows=rows,
                 reasons=RETURN_REASONS)


@mobile_bp.route('/receive/history')
def receive_history():
    _require_receive()
    query = (request.args.get('q') or '').strip()
    period = request.args.get('period') if request.args.get('period') in ('today', 'week') else 'all'
    sql = '''
        SELECT h.timestamp, h.action, h.details, u.username, p.tracking_number, p.item_name
        FROM history h LEFT JOIN users u ON h.user_id = u.id LEFT JOIN packages p ON h.package_id = p.id
        WHERE 1 = 1
    '''
    params = []
    if query:
        sql += ' AND (p.tracking_number LIKE ? OR p.item_name LIKE ?)'
        params += [f'%{query}%', f'%{query}%']
    if period == 'today':
        sql += " AND date(h.timestamp, 'localtime') = date('now', 'localtime')"
    elif period == 'week':
        sql += " AND h.timestamp >= datetime('now', '-7 days')"
    sql += ' ORDER BY h.timestamp DESC LIMIT ?'
    params.append(200 if (query or period != 'all') else 100)

    entries = []
    for r in get_request_db().execute(sql, params).fetchall():
        label, tone = HISTORY_ACTIONS.get(r['action'], ((r['action'] or 'Change').replace('_', ' ').capitalize(), 'nu'))
        qty = r['details'].split(': ', 1)[1] if r['details'] and r['details'].startswith('Qty:') else None
        entries.append({'when': r['timestamp'], 'label': label, 'tone': tone, 'qty': qty,
                        'user': r['username'] or 'Unknown', 'tracking': r['tracking_number'] or 'Deleted package',
                        'name': r['item_name'] or 'Unknown item'})
    return _page('m/receive/history.html', 'history', entries=entries, query=query, period=period)
