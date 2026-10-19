"""
POS sign-out.

POS staff sign in at the normal /login (username + password, or Cloudflare
Access). The separate PIN/badge sign-in page (/pos/login) was removed in 3.0;
PIN/badge sign-in will be rebuilt from scratch (the users table keeps its PIN
and badge columns and their data for that, but nothing reads them).
"""
from flask import redirect, url_for

from app.routes.pos import pos_bp


@pos_bp.route('/logout')
def pos_logout():
    """Old POS sign-out URL: the normal logout clears the whole session
    (cart and manager approvals included) and pauses Cloudflare Access
    auto sign-in."""
    return redirect(url_for('auth.logout'))
