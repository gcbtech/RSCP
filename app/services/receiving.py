"""
Receiving service: scanning packages in, returns, refunds and the admin
package edits.

packages.status only changes through this module (and package_imports'
date-driven upkeep). One shipment (tracking number) can carry several item
rows; receiving a tracking number receives all of them.

Status lifecycle:
    on_time / expected / past_due / pending  --scan-->  received
    any                                      --return-> return_pending
    any                                      --refund-> refunded
    received                                 --unreceive-> expected

Conventions as in app/services/inventory.py: functions take an open
connection, the acting user is passed explicitly (username), config values
come from the caller, writes run under write_transaction (BEGIN IMMEDIATE,
or join the caller's transaction).
"""
import datetime
import logging
import threading
from collections import namedtuple

from app.services import inventory
from app.services.inventory import write_transaction

logger = logging.getLogger(__name__)

# Statuses a scan moves to 'received' (others, e.g. return_pending, only get
# their scan time stamped).
RECEIVABLE_STATUSES = ('expected', 'past_due', 'pending', 'on_time')


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def clean_scan(raw):
    """Scanner/keyboard input -> the string to look up (spreadsheet-style
    quote and '=' prefixes stripped)."""
    return (raw or "").strip().replace('=', '').replace('"', '').replace("'", "")


def find_scan_packages(conn, raw):
    """Packages matching a scan. Returns (tracking, rows).

    Exact match first (case-insensitive). Long barcodes (FedEx and friends
    wrap the tracking number in extra digits) fall back to the longest known
    tracking number (> 6 chars) the scan ends with. `tracking` is the
    package's stored tracking number when something matched, else the
    cleaned scan.
    """
    cleaned = clean_scan(raw)
    rows = conn.execute("SELECT * FROM packages WHERE tracking_number = ? COLLATE NOCASE",
                        (cleaned,)).fetchall()
    if rows:
        return rows[0]['tracking_number'], rows

    if len(cleaned) > 8:
        candidates = conn.execute("""
            SELECT * FROM packages
            WHERE length(tracking_number) > 5
            AND instr(UPPER(?), UPPER(tracking_number)) > 0
        """, (cleaned,)).fetchall()
        best = None
        for c in candidates:
            t = c['tracking_number']
            if len(t) > 6 and cleaned.upper().endswith(t.upper()):
                if best is None or len(t) > len(best['tracking_number']):
                    best = c
        if best:
            real = best['tracking_number']
            return real, conn.execute("SELECT * FROM packages WHERE tracking_number = ?", (real,)).fetchall()
    return cleaned, []


def has_history(conn, tracking):
    """True if any package with this tracking number has a history entry."""
    res = conn.execute("SELECT count(*) as c FROM history h JOIN packages p ON h.package_id = p.id "
                       "WHERE p.tracking_number = ?", (tracking,)).fetchone()
    return res['c'] > 0


def _user_id(conn, username):
    res = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    return res['id'] if res else None


def _send_priority_webhook(tracking, item_name, quantity, user, webhook_url_enc, secret_key):
    from app.services.data_manager import send_priority_alert
    send_priority_alert(tracking, item_name, quantity, user, webhook_url_enc, secret_key)


def receive_packages(conn, tracking, item_name, quantity, user, conf=None):
    """Receive every package row with this tracking number: stamp the scan
    time, move open statuses to 'received', write a 'received' history row
    per package. An unknown tracking number becomes a new 'scan' package
    (SKU from product_mappings by name). Priority packages fire the receiving
    webhook (conf: WEBHOOK_ENABLED / WEBHOOK_URL / SECRET_KEY).
    Returns the number of package rows received."""
    priority_count = 0
    with write_transaction(conn):
        user_id = _user_id(conn, user)
        packages = conn.execute("SELECT id, status, priority, quantity FROM packages WHERE tracking_number = ?",
                                (tracking,)).fetchall()
        if packages:
            for pkg in packages:
                new_status = 'received' if pkg['status'] in RECEIVABLE_STATUSES else pkg['status']
                conn.execute("UPDATE packages SET date_scanned=CURRENT_TIMESTAMP, status=? WHERE id=?",
                             (new_status, pkg['id']))
                conn.execute("INSERT INTO history (package_id, user_id, action, details) VALUES (?, ?, ?, ?)",
                             (pkg['id'], user_id, 'received', f"Qty: {pkg['quantity'] or 1}"))
                if pkg['priority']:
                    priority_count += 1
            received = len(packages)
        else:
            mapping = conn.execute("SELECT inventory_sku FROM product_mappings WHERE package_name = ?",
                                   (item_name,)).fetchone()
            sku = mapping['inventory_sku'] if mapping else None
            # date_expected is a calendar date: the store's local date, not UTC
            # (CURRENT_DATE), or evening scans land on tomorrow.
            cur = conn.execute('''
                INSERT INTO packages (tracking_number, item_name, quantity, status, source, date_expected, date_scanned, sku)
                VALUES (?, ?, ?, 'received', 'scan', date('now', 'localtime'), CURRENT_TIMESTAMP, ?)
            ''', (tracking, item_name, quantity, sku))
            conn.execute("INSERT INTO history (package_id, user_id, action, details) VALUES (?, ?, ?, ?)",
                         (cur.lastrowid, user_id, 'received', f"Qty: {quantity}"))
            received = 1

    conf = conf or {}
    if priority_count and conf.get('WEBHOOK_ENABLED') and conf.get('WEBHOOK_URL'):
        for _ in range(priority_count):  # one alert per priority package, as before
            threading.Thread(target=_send_priority_webhook,
                             args=(tracking, item_name, quantity, user, conf.get('WEBHOOK_URL'),
                                   conf.get('SECRET_KEY', 'dev_key_fallback'))).start()
    return received


ScanResult = namedtuple('ScanResult', 'kind tracking rows items inventory_item inventory_match')
# kind: 'inventory' (scan is an inventory SKU/UPC, inventory_item set),
#       'unknown', 'duplicate', 'received', 'priority'


def _location(item):
    return ' / '.join(filter(None, [item['location_area'], item['location_aisle'],
                                    item['location_shelf'], item['location_bin']]))


def find_inventory_match(conn, package_row, name, asin):
    """Inventory item a received package most likely restocks: by the
    package's SKU (or its saved product mapping), then exact ASIN, then a
    name prefix (first 30 chars, case-insensitive). Returns a dict or None."""
    cols = 'id, name, sku, quantity, image_url'
    sku = package_row['sku'] if package_row is not None and 'sku' in package_row.keys() else None
    if not sku:
        mapping = conn.execute("SELECT inventory_sku FROM product_mappings WHERE package_name = ?", (name,)).fetchone()
        if mapping:
            sku = mapping['inventory_sku']
    if sku:
        match = conn.execute(f"SELECT {cols} FROM inventory_items WHERE sku = ?", (sku,)).fetchone()
        if match:
            return dict(match)
    if asin and asin.strip():
        match = conn.execute(f"SELECT {cols} FROM inventory_items WHERE asin = ?", (asin.strip(),)).fetchone()
        if match:
            return dict(match)
    if name and name.strip():
        prefix = name[:30].lower().rstrip('.').rstrip()
        match = conn.execute(f"SELECT {cols} FROM inventory_items WHERE LOWER(name) LIKE ? LIMIT 1",
                             (f'{prefix}%',)).fetchone()
        if match:
            return dict(match)
    return None


def receive_scan(conn, raw, user, inventory_enabled=False, conf=None):
    """Handle one scan on the receiving screen. Returns a ScanResult.

    Known tracking: receives all its packages unless they were all received
    already (then 'duplicate'). Unknown tracking: with inventory enabled the
    scan is tried as an inventory SKU / UPC / part number ('inventory'),
    else 'unknown'. After a normal (non-priority) receipt with inventory
    enabled, inventory_match suggests the item to restock.
    """
    tracking, rows = find_scan_packages(conn, raw)

    if not rows:
        if inventory_enabled:
            try:
                item = inventory.find_item_by_code(conn, (raw or ''))
            except Exception as e:
                logger.warning(f"Inventory SKU lookup failed during scan: {e}")
                item = None
            if item:
                inv = {
                    'id': item['id'], 'sku': item['sku'], 'name': item['name'],
                    'quantity': item['quantity'], 'image_url': item['image_url'],
                    'sell_price': item['sell_price'], 'location': _location(item),
                }
                return ScanResult('inventory', tracking, [], [], inv, None)
        return ScanResult('unknown', tracking, [], [], None, None)

    all_received = all(r['date_scanned'] for r in rows)
    if all_received and has_history(conn, tracking):
        kind = 'duplicate'
    else:
        receive_packages(conn, tracking, rows[0]['item_name'], str(rows[0]['quantity']), user, conf)
        is_priority = any(r['priority'] for r in rows if 'priority' in r.keys() and r['priority'])
        kind = 'priority' if is_priority else 'received'

    items = [{
        "name": r['item_name'],
        "image": r['image_url'],
        "subtext": tracking,
        "qty": r['quantity'] or 1,
        "image_url": r['image_url'],
        "asin": r['asin'] if 'asin' in r.keys() else '',
        "source_url": r['source_url'] if 'source_url' in r.keys() else '',
    } for r in rows]

    match = None
    if inventory_enabled and kind == 'received' and items:
        try:
            match = find_inventory_match(conn, rows[0], items[0].get('name') or '', items[0].get('asin') or '')
        except Exception as e:
            logger.error(f"Error checking inventory match: {e}")
    return ScanResult(kind, tracking, rows, items, None, match)


def link_package_to_item(conn, tracking, sku, user, save_mapping=False, package_name=None):
    """Tie a package (all rows of a tracking number) to an inventory SKU,
    optionally remembering the package name -> SKU mapping for future
    receipts. Units of rows already received are added to that item's stock
    (logged as received, with the tracking number). Returns units added."""
    added = 0
    with write_transaction(conn):
        conn.execute("UPDATE packages SET sku = ? WHERE tracking_number = ?", (sku, tracking))
        if save_mapping and package_name:
            existing = conn.execute("SELECT id FROM product_mappings WHERE package_name = ?",
                                    (package_name,)).fetchone()
            if existing:
                conn.execute("UPDATE product_mappings SET inventory_sku = ? WHERE id = ?", (sku, existing['id']))
            else:
                conn.execute("INSERT INTO product_mappings (package_name, inventory_sku) VALUES (?, ?)",
                             (package_name, sku))

        received_rows = conn.execute(
            "SELECT quantity FROM packages WHERE tracking_number = ? AND date_scanned IS NOT NULL",
            (tracking,)).fetchall()
        total_qty = sum(r['quantity'] or 0 for r in received_rows)
        logger.info(f"Link Item: Tracking={tracking}, SKU={sku}, ReceivedRows={len(received_rows)}, TotalQty={total_qty}")
        if total_qty > 0:
            item = conn.execute("SELECT id, name FROM inventory_items WHERE sku = ?", (sku,)).fetchone()
            if item:
                inventory.adjust_stock(conn, item['id'], total_qty, inventory.REASON_RECEIVED_SCAN,
                                       user, tracking)
                added = total_qty
                logger.info(f"Linked & Added Stock: ID={item['id']} ({item['name']}) += {total_qty}")
            else:
                logger.warning(f"Link Item: Inventory Item NOT FOUND for SKU={sku}")
        else:
            logger.warning(f"Link Item: No received packages found (or qty=0) for {tracking}. date_scanned might be NULL.")
    return added


# ---------------------------------------------------------------------------
# Returns and refunds
# ---------------------------------------------------------------------------

def _today():
    return datetime.date.today().strftime('%Y-%m-%d')


def initiate_return(conn, original_tracking, return_tracking, reason, user):
    """Mark a package return_pending and log the reason / return tracking."""
    with write_transaction(conn):
        conn.execute("UPDATE packages SET status='return_pending' WHERE tracking_number=?", (original_tracking,))
        res = conn.execute("SELECT id FROM packages WHERE tracking_number=?", (original_tracking,)).fetchone()
        if res:
            conn.execute('''
                INSERT INTO history (package_id, user_id, action, details)
                VALUES (?, (SELECT id FROM users WHERE username=?), ?, ?)
            ''', (res['id'], user, 'return_initiated', f"Reason: {reason}, New Track: {return_tracking}"))


def mark_refunded(conn, tracking, user=None, today=None):
    """Mark a package refunded (refund_date = today) and, with a user, log it."""
    with write_transaction(conn):
        conn.execute("UPDATE packages SET status='refunded', refund_date=? WHERE tracking_number=?",
                     (today or _today(), tracking))
        if user is not None:
            res = conn.execute("SELECT id FROM packages WHERE tracking_number=?", (tracking,)).fetchone()
            if res:
                conn.execute("INSERT INTO history (package_id, user_id, action) "
                             "VALUES (?, (SELECT id FROM users WHERE username=?), 'refunded')",
                             (res['id'], user))


def set_status(conn, tracking, status, today=None):
    """Admin override of a package's status. 'received' stamps the scan time
    (no history row), 'refunded' sets the refund date."""
    if status == 'received':
        with write_transaction(conn):
            conn.execute("UPDATE packages SET status='received', date_scanned=CURRENT_TIMESTAMP WHERE tracking_number=?",
                         (tracking,))
    elif status == 'refunded':
        mark_refunded(conn, tracking, today=today)
    else:
        with write_transaction(conn):
            conn.execute("UPDATE packages SET status=? WHERE tracking_number=?", (status, tracking))


def unreceive(conn, trackings):
    """Undo receipts: every row of each tracking number goes back to
    'expected' with no scan time, and its 'received' history is dropped."""
    with write_transaction(conn):
        for t in trackings:
            for row in conn.execute("SELECT id FROM packages WHERE tracking_number=?", (t,)).fetchall():
                conn.execute("DELETE FROM history WHERE package_id=? AND action='received'", (row['id'],))
                conn.execute("UPDATE packages SET status='expected', date_scanned=NULL WHERE id=?", (row['id'],))


def mark_late(conn, trackings):
    if not trackings:
        return
    placeholders = ','.join(['?'] * len(trackings))
    with write_transaction(conn):
        conn.execute(f"UPDATE packages SET status='past_due' WHERE tracking_number IN ({placeholders})", list(trackings))


def bulk_receive(conn, trackings, user, conf=None):
    """Receive several tracking numbers (unknown ones become 'Bulk Item'
    packages, as a scan would create them)."""
    for t in trackings:
        receive_packages(conn, t, "Bulk Item", "1", user, conf)


# ---------------------------------------------------------------------------
# Admin package edits
# ---------------------------------------------------------------------------

def status_for_expected_date(date_input, today=None):
    """Initial status of a manual package from its expected date
    ('YYYY-MM-DD'): today -> expected, past -> past_due, else on_time."""
    today = today or datetime.date.today()
    if date_input:
        try:
            dt = datetime.datetime.strptime(date_input, '%Y-%m-%d').date()
            if dt == today:
                return 'expected'
            if dt < today:
                return 'past_due'
        except ValueError:
            pass
    return 'on_time'


def add_manual_package(conn, tracking, name, sku, date_input, priority, user, today=None):
    """Add a package by hand (recorded as a manual import). Returns its status."""
    from app.services import package_imports
    status = status_for_expected_date(date_input, today)
    with write_transaction(conn):
        conn.execute('''
            INSERT INTO packages (tracking_number, item_name, date_expected, quantity, status, source, priority, manual_date, sku)
            VALUES (?, ?, ?, 1, ?, 'manual', ?, ?, ?)
        ''', (tracking, name, date_input, status, 1 if priority else 0, date_input, sku))
        package_imports.record_import(conn, 'manual', None, 1, 0, user)
    return status


def set_package_sku(conn, package_id, sku):
    with write_transaction(conn):
        conn.execute("UPDATE packages SET sku = ? WHERE id = ?", (sku, package_id))


def set_package_quantity(conn, package_id, qty):
    if qty < 0:
        raise ValueError("Negative quantity")
    with write_transaction(conn):
        conn.execute("UPDATE packages SET quantity = ? WHERE id = ?", (qty, package_id))


def toggle_priority(conn, tracking):
    with write_transaction(conn):
        conn.execute("UPDATE packages SET priority = NOT COALESCE(priority, 0) WHERE tracking_number = ?", (tracking,))


def set_expected_date(conn, tracking, new_date):
    """Pin a package's expected date by hand and re-derive open statuses."""
    from app.services import package_imports
    with write_transaction(conn):
        conn.execute("UPDATE packages SET manual_date = ?, date_expected = ? WHERE tracking_number = ?",
                     (new_date, new_date, tracking))
        package_imports.refresh_package_statuses(conn)


class TrackingConflict(ValueError):
    """The same item already exists under the target tracking number."""


def set_package_tracking(conn, package_id, new_tracking):
    """Change one package row's tracking number. Several items may share a
    tracking number, the same item may not appear twice. Returns False when
    the package does not exist; raises TrackingConflict."""
    with write_transaction(conn):
        old = conn.execute("SELECT tracking_number, item_name FROM packages WHERE id = ?", (package_id,)).fetchone()
        if not old:
            return False
        if conn.execute("SELECT id FROM packages WHERE tracking_number = ? AND item_name IS ? AND id != ?",
                        (new_tracking, old['item_name'], package_id)).fetchone():
            raise TrackingConflict('This item already exists under that tracking number')
        conn.execute("UPDATE packages SET tracking_number = ? WHERE id = ?", (new_tracking, package_id))
    return True


def delete_package(conn, package_id):
    """Delete one package row and its history. Returns the deleted row
    (tracking_number, item_name) or None."""
    with write_transaction(conn):
        pkg = conn.execute("SELECT tracking_number, item_name FROM packages WHERE id = ?", (package_id,)).fetchone()
        conn.execute("DELETE FROM packages WHERE id = ?", (package_id,))
        conn.execute("DELETE FROM history WHERE package_id = ?", (package_id,))
    return pkg


def delete_by_tracking(conn, trackings):
    """Delete every package row of these tracking numbers (history is kept)."""
    if not trackings:
        return
    placeholders = ','.join(['?'] * len(trackings))
    with write_transaction(conn):
        conn.execute(f"DELETE FROM packages WHERE tracking_number IN ({placeholders})", list(trackings))


def clear_history(conn):
    with write_transaction(conn):
        conn.execute("DELETE FROM history")
