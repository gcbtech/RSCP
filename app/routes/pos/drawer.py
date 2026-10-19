"""
POS Cash Drawer Sessions
A manager opens the drawer with a starting float and closes it with the
counted cash; expected = float + cash taken - cash refunded in between.
Optional: the POS keeps selling with no open session.
"""
import logging
from flask import request, redirect, url_for, flash, render_template
from flask_login import current_user, login_required

from app.routes.pos import pos_bp
from app.services.db import get_db_connection, get_request_db
from app.services import money
from app.services.pos_ledger import (
    get_open_session, session_figures, open_session, close_session
)
from app.utils.permissions import require_permission

logger = logging.getLogger(__name__)


def _money_form(name):
    """Cents for a dollar amount typed into the form; None if blank/invalid/negative."""
    return money.parse((request.form.get(name) or '').strip())


@pos_bp.route('/drawer')
@login_required
@require_permission('pos.manage')
def drawer():
    """Current drawer session (live expected cash) and recent sessions."""
    conn = get_request_db()
    current = get_open_session(conn)
    current = session_figures(conn, current) if current else None
    recent = conn.execute('''
        SELECT d.*, uo.username AS opened_by_name, uc.username AS closed_by_name
        FROM drawer_sessions d
        LEFT JOIN users uo ON uo.id = d.opened_by
        LEFT JOIN users uc ON uc.id = d.closed_by
        WHERE d.closed_at IS NOT NULL
        ORDER BY d.closed_at DESC, d.id DESC LIMIT 20
    ''').fetchall()
    return render_template('pos/drawer.html', current=current, recent=recent)


@pos_bp.route('/drawer/open', methods=['POST'])
@login_required
@require_permission('pos.manage')
def drawer_open():
    opening_float = _money_form('opening_float')
    if opening_float is None:
        flash('Enter the starting float as a dollar amount (0 or more).')
        return redirect(url_for('pos.drawer'))
    conn = get_db_connection()
    conn.isolation_level = None
    try:
        conn.execute('BEGIN IMMEDIATE')
        session_id = open_session(conn, current_user.id, opening_float,
                                  notes=(request.form.get('notes') or '').strip())
        conn.execute('COMMIT')
    except Exception:
        if conn.in_transaction:
            conn.execute('ROLLBACK')
        raise
    finally:
        conn.close()
    if session_id is None:
        flash('A drawer session is already open. Close it before opening a new one.')
    else:
        logger.info(f"Drawer session {session_id} opened by {current_user.username} "
                    f"with {money.fmt(opening_float)}")
        flash(f'Drawer opened with a {money.fmt(opening_float)} float.')
    return redirect(url_for('pos.drawer'))


@pos_bp.route('/drawer/close', methods=['POST'])
@login_required
@require_permission('pos.manage')
def drawer_close():
    counted = _money_form('counted_cash')
    if counted is None:
        flash('Enter the counted cash as a dollar amount (0 or more).')
        return redirect(url_for('pos.drawer'))
    try:
        session_id = int(request.form.get('session_id', ''))
    except ValueError:
        flash('No drawer session selected.')
        return redirect(url_for('pos.drawer'))
    conn = get_db_connection()
    conn.isolation_level = None
    try:
        conn.execute('BEGIN IMMEDIATE')
        result = close_session(conn, session_id, current_user.id, counted,
                               notes=(request.form.get('notes') or '').strip())
        conn.execute('COMMIT')
    except Exception:
        if conn.in_transaction:
            conn.execute('ROLLBACK')
        raise
    finally:
        conn.close()
    if result is None:
        flash('That drawer session is already closed.')
        return redirect(url_for('pos.drawer'))
    diff = result['over_short_cents']
    state = 'balanced' if diff == 0 else (f'over by {money.fmt(diff)}' if diff > 0 else f'short by {money.fmt(-diff)}')
    expected = money.fmt(result['expected_cash_cents'])
    logger.info(f"Drawer session {session_id} closed by {current_user.username}: counted {money.fmt(counted)}, "
                f"expected {expected} ({state})")
    flash(f"Drawer closed: counted {money.fmt(counted)}, expected {expected} ({state}).")
    return redirect(url_for('pos.drawer'))
