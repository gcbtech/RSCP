"""
The phone UI, served under /m (Panasonic FZ-N1 handhelds running Fully Kiosk,
whose start URL points here).

A separate front end on purpose: its own routes, templates (templates/m/)
and static files (static/m/), calling the same services as the desktop
pages. Nothing here changes a desktop page; scripts/desktop_parity.py
checks that before every release.

Manager and admin pages stay on the desktop; phone screens link to them.
One module per area: receive.py, stock.py, pos.py, timeclock.py.
"""
import logging

from flask import Blueprint, render_template
from flask_login import current_user

from app.services import inventory
from app.services import timeclock_hours as tc
from app.services.data_manager import get_dashboard_stats, load_config
from app.services.db import get_request_db
from app.utils.permissions import has_permission, protect_blueprint

logger = logging.getLogger(__name__)

mobile_bp = Blueprint('mobile', __name__, url_prefix='/m')
# Any signed-in user; each module checks its own permission (allowed_modules).
protect_blueprint(mobile_bp, None, module_name='Mobile')

# Bottom tab bar, after Home: key, label, endpoint, permission, module flag
TABS = (
    ('receive', 'Receive', 'mobile.receive', 'receiving.view', None),
    ('stock', 'Stock', 'mobile.stock', 'inventory.view', 'INVENTORY_ENABLED'),
    ('pos', 'POS', 'mobile.pos', 'pos.view', 'POS_ENABLED'),
    ('time', 'Time', 'mobile.time', 'timeclock.view', 'TIMECLOCK_ENABLED'),
)

def allowed_modules():
    """Keys of the modules the signed-in user can open (enabled + permitted)."""
    conf = load_config() or {}
    return {key for key, _label, _endpoint, permission, flag in TABS
            if (flag is None or conf.get(flag)) and has_permission(current_user, permission)}


@mobile_bp.context_processor
def inject_shell():
    modules = allowed_modules()
    return {'m_tabs': [tab for tab in TABS if tab[0] in modules]}


@mobile_bp.route('/')
def home():
    """Home: quick actions and today's numbers. static/m/home.js re-fetches
    this page every minute and swaps #mHome, so it stays current all day."""
    modules = allowed_modules()
    receiving = stock = audit = clock = team = None
    conn = get_request_db()
    if 'receive' in modules:
        receiving = get_dashboard_stats()
    if 'stock' in modules:
        conf = load_config() or {}
        stock = inventory.inventory_stats(conn, int(conf.get('LOW_STOCK_THRESHOLD', 5)))
        audit = conn.execute('''
            SELECT s.id, s.mode,
                   (SELECT COUNT(*) FROM audit_records r WHERE r.session_id = s.id) AS counted
            FROM audit_sessions s WHERE s.status = 'active'
            ORDER BY s.start_time DESC LIMIT 1
        ''').fetchone()
    if 'time' in modules:
        from app.routes.mobile import timeclock as m_time
        try:
            clock = m_time._status(conn)
        except Exception as e:      # an unreadable entry shouldn't take Home down
            logger.error(f'[Mobile] clock status unreadable: {e}')
            clock = {'state': 'unknown'}
        if has_permission(current_user, 'timeclock.manage'):
            team = m_time.team_summary(conn)
    return render_template('m/home.html', active_tab='home', modules=modules, today=tc.local_today(),
                           receiving=receiving, stock=stock, audit=audit, clock=clock, team=team)


# Area modules register their routes on mobile_bp (imported last: they use the helpers above)
from app.routes.mobile import pos, receive, stock, timeclock  # noqa: E402,F401
