"""
Inventory service: items, stock levels and the stock ledger.

Stock rule: inventory_items.quantity only ever changes through
adjust_stock() / set_stock() here. Every change is

  * relative (``quantity = quantity + delta``) so a POS sale or another
    terminal committing in between is never overwritten by a stale value,
  * logged as one inventory_transactions row (reason, user, source), and
  * taken under the write lock (BEGIN IMMEDIATE) when a decision depends on
    the current quantity (negative guard, absolute counts).

Conventions (same as pos_registers / package_imports):
  * every function takes an open sqlite3 connection;
  * no Flask request/session/current_user in here: the acting user is passed
    in explicitly (as the string the ledger stores: username for most flows);
  * config values are passed in by the caller, so tests can stub config at
    the route module as before;
  * functions that write commit their own transaction unless the caller
    already has one open, in which case they join it (see write_transaction).
"""
import json
import logging
import sqlite3
import threading
import time
from collections import namedtuple
from contextlib import contextmanager
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Reasons written to inventory_transactions (shown in item history and used
# by the overview: 'Sold/Consumed' and 'Damaged' count as movement).
REASON_INITIAL = 'Initial Stock'
REASON_SOLD = 'Sold/Consumed'
REASON_RECEIVED_SCAN = 'Received from Scan'
REASON_OOS_SCAN = 'Marked OOS from Scan'
REASON_AUDIT = 'Live Audit Correction'
REASON_CSV_IMPORT = 'CSV Import'

ATTENTION_SNOOZE_DAYS = 5

# Packages in these states are no longer "incoming" for the items list.
_NOT_INCOMING = ('received', 'refunded', 'return_pending', 'returned', 'archived')


class InventoryError(Exception):
    """Base class for inventory rule violations (message is user-facing)."""


class ItemNotFound(InventoryError):
    def __init__(self, item_id=None):
        super().__init__('Item not found')
        self.item_id = item_id


class InsufficientStock(InventoryError):
    def __init__(self, requested, available):
        super().__init__(f"Cannot reduce by {abs(requested)} - only {available} in stock")
        self.requested = requested
        self.available = available


class DuplicateSku(InventoryError):
    def __init__(self, sku):
        super().__init__(f"SKU '{sku}' already exists")
        self.sku = sku


class TransactionNotFound(InventoryError):
    def __init__(self, tx_id=None):
        super().__init__('Transaction not found')
        self.tx_id = tx_id


# Result of a stock change.
StockChange = namedtuple('StockChange', 'item_id old_quantity new_quantity delta name sku')


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

@contextmanager
def write_transaction(conn):
    """Run the block under BEGIN IMMEDIATE and commit it, or join the
    caller's already-open transaction (then the caller commits).

    BEGIN IMMEDIATE takes SQLite's write lock up front, so a read made inside
    the block (current quantity, existing row) is still true when the write
    lands.
    """
    if conn.in_transaction:
        yield conn
        return
    conn.execute('BEGIN IMMEDIATE')
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    else:
        conn.commit()


# ---------------------------------------------------------------------------
# Stock
# ---------------------------------------------------------------------------

def _log_transaction(conn, item_id, delta, reason, user, source):
    conn.execute('''
        INSERT INTO inventory_transactions
            (inventory_item_id, quantity_change, reason, user_id, source_tracking)
        VALUES (?, ?, ?, ?, ?)
    ''', (item_id, delta, reason, user, source))


def _apply_delta(conn, item_id, delta):
    conn.execute('''
        UPDATE inventory_items
        SET quantity = quantity + ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
    ''', (delta, item_id))


def adjust_stock(conn, item_id, delta, reason, user=None, source=None, *, allow_negative=False):
    """Change an item's stock by `delta` (positive adds, negative removes).

    Raises ItemNotFound, or InsufficientStock when the change would take the
    quantity below zero (unless allow_negative: POS sales and imports never
    refused). A zero delta changes nothing and logs nothing.
    Returns a StockChange.
    """
    delta = int(delta)
    with write_transaction(conn):
        row = conn.execute('SELECT quantity, name, sku FROM inventory_items WHERE id = ?',
                           (item_id,)).fetchone()
        if row is None:
            raise ItemNotFound(item_id)
        old = row['quantity'] or 0
        if delta < 0 and not allow_negative and old + delta < 0:
            raise InsufficientStock(delta, old)
        if delta != 0:
            _apply_delta(conn, item_id, delta)
            _log_transaction(conn, item_id, delta, reason, user, source)
    return StockChange(item_id, old, old + delta, delta, row['name'], row['sku'])


def set_stock(conn, item_id, new_quantity, reason, user=None, source=None):
    """Bring an item's stock to an absolute count (audit count, mark out of
    stock). Applied as a relative change computed under the write lock, so it
    is logged like any other change. Returns a StockChange."""
    new_quantity = int(new_quantity)
    with write_transaction(conn):
        row = conn.execute('SELECT quantity FROM inventory_items WHERE id = ?',
                           (item_id,)).fetchone()
        if row is None:
            raise ItemNotFound(item_id)
        return adjust_stock(conn, item_id, new_quantity - (row['quantity'] or 0), reason,
                            user, source, allow_negative=True)


def mark_out_of_stock(conn, item_id, user=None, reason=REASON_OOS_SCAN):
    """Zero an item's stock (logged as one change). Returns a StockChange."""
    return set_stock(conn, item_id, 0, reason, user)


def revert_transaction(conn, tx_id):
    """Undo a ledger row: reverse its quantity change and delete the row
    (the history then reads as if it never happened). Returns the item id."""
    with write_transaction(conn):
        tx = conn.execute('SELECT inventory_item_id, quantity_change FROM inventory_transactions WHERE id = ?',
                          (tx_id,)).fetchone()
        if tx is None:
            raise TransactionNotFound(tx_id)
        item_id = tx['inventory_item_id']
        _apply_delta(conn, item_id, -tx['quantity_change'])
        conn.execute('DELETE FROM inventory_transactions WHERE id = ?', (tx_id,))
    return item_id


def item_transactions(conn, item_id, limit=50):
    return conn.execute('''
        SELECT * FROM inventory_transactions
        WHERE inventory_item_id = ?
        ORDER BY created_at DESC LIMIT ?
    ''', (item_id, limit)).fetchall()


# ---------------------------------------------------------------------------
# Low-stock alerts
# ---------------------------------------------------------------------------

def _send_stock_webhook(url, name, sku, new_qty):
    try:
        import urllib.request
        status = "OUT OF STOCK" if new_qty == 0 else f"LOW STOCK ({new_qty} remaining)"
        msg = f"📦 **{status}**: {name} (SKU: {sku})"
        payload = {"content": msg, "text": msg}
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json', 'User-Agent': 'RSCP-Bot'}
        )
        urllib.request.urlopen(req, timeout=5)
        logger.info(f"Stock alert sent for {sku}")
    except Exception as e:
        logger.error(f"Stock alert webhook error: {e}")


def notify_if_low_stock(conn, item_id, conf):
    """After stock went down: if the item has alerts enabled and is at or
    below its threshold, raise the in-app notification (NOTIFY_OOS /
    NOTIFY_LOW_STOCK) and fire the inventory webhook in the background.
    `conf` is the app config dict. Returns True if an alert condition held."""
    item = conn.execute('''
        SELECT name, sku, quantity, alert_enabled, alert_threshold
        FROM inventory_items WHERE id = ?
    ''', (item_id,)).fetchone()
    if not item or not item['alert_enabled']:
        return False
    new_qty = item['quantity']
    threshold = item['alert_threshold'] or 0
    if new_qty > threshold:
        return False

    conf = conf or {}
    is_oos = new_qty <= 0
    if (is_oos and conf.get('NOTIFY_OOS', False)) or (not is_oos and conf.get('NOTIFY_LOW_STOCK', False)):
        try:
            from app.routes.notifications import create_notification
            status = "OUT OF STOCK" if is_oos else f"LOW STOCK ({new_qty})"
            create_notification(
                user_id=None,  # All users
                title=f"📦 {status}: {item['name']}",
                message=f"SKU: {item['sku']} - {new_qty} remaining (threshold: {threshold})",
                notification_type='warning' if not is_oos else 'error',
                link=f"/inventory/edit/{item_id}"
            )
        except Exception as e:
            logger.error(f"In-app notification error: {e}")

    if conf.get('WEBHOOK_ENABLED_INVENTORY') and conf.get('WEBHOOK_URL_INVENTORY'):
        from app.utils.helpers import reveal_string
        try:
            url = reveal_string(conf['WEBHOOK_URL_INVENTORY'], conf.get('SECRET_KEY', 'dev_key'))
        except Exception as e:
            logger.error(f"Stock alert webhook error: {e}")
            url = ''
        if url.startswith('http'):
            threading.Thread(target=_send_stock_webhook,
                             args=(url, item['name'], item['sku'], new_qty), daemon=True).start()
    return True


# ---------------------------------------------------------------------------
# SKUs, lookup, pricing
# ---------------------------------------------------------------------------

def generate_sku(conn, category='ATO'):
    """Next free SKU in the format RSCP-XXX-XXXX-XXXX for a category code."""
    category = category.upper()[:3].ljust(3, 'X')
    result = conn.execute('''
        SELECT sku FROM inventory_items
        WHERE sku LIKE ?
        ORDER BY sku DESC LIMIT 1
    ''', (f'RSCP-{category}-%',)).fetchone()

    next_num = 1
    if result:
        try:
            parts = result['sku'].split('-')
            if len(parts) == 4:
                next_num = int(parts[2] + parts[3]) + 1
        except (ValueError, IndexError):
            next_num = 1

    for attempt in range(10):
        num_str = str(next_num + attempt).zfill(8)
        sku = f"RSCP-{category}-{num_str[:4]}-{num_str[4:]}"
        if not conn.execute('SELECT 1 FROM inventory_items WHERE sku = ?', (sku,)).fetchone():
            return sku

    # Fallback: use timestamp to ensure uniqueness
    timestamp = str(int(time.time()))[-8:]
    return f"RSCP-{category}-{timestamp[:4]}-{timestamp[4:]}"


def validate_location(area, aisle, shelf, bin_loc):
    """At least one location field is required."""
    return any([area, aisle, shelf, bin_loc])


def sku_in_use(conn, sku, exclude_id=None):
    if exclude_id is None:
        return conn.execute('SELECT 1 FROM inventory_items WHERE sku = ?', (sku,)).fetchone() is not None
    return conn.execute('SELECT 1 FROM inventory_items WHERE sku = ? AND id != ?',
                        (sku, exclude_id)).fetchone() is not None


def apply_sale_pricing(item, now=None):
    """Add regular_price / current_price / is_on_sale to an item dict from
    its sale_* fields (sale window strings are 'YYYY-MM-DDTHH:MM', local)."""
    now = now or datetime.now()
    item['regular_price'] = item['sell_price']
    item['is_on_sale'] = False
    item['current_price'] = item['sell_price']

    if item.get('sale_enabled'):
        try:
            start_valid = True
            end_valid = True
            if item.get('sale_start'):
                if now < datetime.strptime(item['sale_start'], '%Y-%m-%dT%H:%M'):
                    start_valid = False
            if item.get('sale_end'):
                if now > datetime.strptime(item['sale_end'], '%Y-%m-%dT%H:%M'):
                    end_valid = False
            if start_valid and end_valid:
                item['current_price'] = item['sale_price']
                item['is_on_sale'] = True
        except (ValueError, TypeError) as e:
            logger.warning(f"Error parsing sale dates for item {item.get('sku')}: {e}")
    return item


def find_item_by_code(conn, code, now=None):
    """Item dict (with sale pricing applied) by SKU, then UPC / part number
    from secondary_ids; None if nothing matches."""
    row = conn.execute('SELECT * FROM inventory_items WHERE sku = ?', (code,)).fetchone()
    if row:
        return apply_sale_pricing(dict(row), now)

    search = code.strip() if code else ""
    try:
        row = conn.execute('''
            SELECT * FROM inventory_items
            WHERE secondary_ids IS NOT NULL
              AND secondary_ids != '{}'
              AND (
                TRIM(json_extract(secondary_ids, '$.upc')) = ?
                OR TRIM(json_extract(secondary_ids, '$.part_number')) = ?
              )
        ''', (search, search)).fetchone()
        if row:
            return apply_sale_pricing(dict(row), now)
    except sqlite3.OperationalError as e:
        # json_extract unavailable (or malformed JSON somewhere): slow scan
        logger.warning(f"Native SQLite JSON search failed (falling back to slow scan): {e}")
        for row in conn.execute("SELECT * FROM inventory_items WHERE secondary_ids IS NOT NULL AND secondary_ids != '{}'").fetchall():
            try:
                ids = json.loads(row['secondary_ids']) if row['secondary_ids'] else {}
                if str(ids.get('upc', '')).strip() == code or str(ids.get('part_number', '')).strip() == code:
                    return apply_sale_pricing(dict(row), now)
            except (json.JSONDecodeError, AttributeError):
                continue
    return None


def get_item(conn, item_id):
    return conn.execute('SELECT * FROM inventory_items WHERE id = ?', (item_id,)).fetchone()


# ---------------------------------------------------------------------------
# Item create / update
# ---------------------------------------------------------------------------

def secondary_ids_json(upc=None, part_number=None):
    ids = {}
    if upc:
        ids['upc'] = upc
    if part_number:
        ids['part_number'] = part_number
    return json.dumps(ids) if ids else None


def amazon_image_url(asin):
    return f"https://images-na.ssl-images-amazon.com/images/P/{asin}.01._SX200_.jpg"


def amazon_product_url(asin):
    return f"https://www.amazon.com/dp/{asin}"


def _resolve_images(image_url, gallery, asin):
    # The first gallery image becomes the cover when there is none; an ASIN
    # supplies an Amazon cover as a last resort.
    gallery = list(gallery or [])
    if not image_url and gallery:
        image_url = gallery.pop(0)
    if asin and not image_url:
        image_url = amazon_image_url(asin)
    return image_url, gallery


def create_item(conn, fields, user=None, first_stock_date_default=None):
    """Insert an item and log its initial stock. Returns the new item id.

    fields: sku (required: generate one first), name, quantity, location_*,
    asin, image_url, additional_images (list), source_url, buy_price,
    sell_price, supplier, first_stock_date, resupply_interval,
    alert_threshold, upc, part_number, keywords, addon_1, addon_2,
    description, notes.
    Alerts are enabled exactly when alert_threshold > 0. An ASIN fills in a
    missing cover image and source URL. first_stock_date_default is used when
    the form left the date empty (TRACK_FIRST_STOCK_DATE).
    Raises DuplicateSku when the SKU is taken.
    """
    f = dict(fields)
    sku = f['sku']
    quantity = int(f.get('quantity') or 0)
    asin = f.get('asin') or None
    image_url, gallery = _resolve_images(f.get('image_url') or None, f.get('additional_images'), asin)
    source_url = f.get('source_url') or None
    if asin and not source_url:
        source_url = amazon_product_url(asin)
    alert_threshold = int(f.get('alert_threshold') or 0)

    try:
        with write_transaction(conn):
            cur = conn.execute('''
                INSERT INTO inventory_items
                (sku, name, quantity, location_area, location_aisle, location_shelf, location_bin,
                 asin, image_url, source_url, buy_price, sell_price, supplier, first_stock_date,
                 resupply_interval, alert_enabled, alert_threshold, secondary_ids, keywords, addon_1, addon_2,
                 description, additional_images, notes)
                VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (sku, f['name'], f.get('location_area') or None, f.get('location_aisle') or None,
                  f.get('location_shelf') or None, f.get('location_bin') or None, asin, image_url,
                  source_url, f.get('buy_price'), f.get('sell_price'), f.get('supplier') or None,
                  f.get('first_stock_date') or first_stock_date_default, f.get('resupply_interval'),
                  1 if alert_threshold > 0 else 0, alert_threshold,
                  secondary_ids_json(f.get('upc'), f.get('part_number')), f.get('keywords'),
                  1 if f.get('addon_1') else 0, 1 if f.get('addon_2') else 0,
                  f.get('description') or None, json.dumps(gallery) if gallery else None, f.get('notes')))
            item_id = cur.lastrowid
            if quantity > 0:
                adjust_stock(conn, item_id, quantity, REASON_INITIAL, user)
            elif quantity < 0:
                # Never refused before; kept, but there is nothing to "start" from.
                _apply_delta(conn, item_id, quantity)
    except sqlite3.IntegrityError as e:
        if 'UNIQUE constraint failed' in str(e) and 'sku' in str(e).lower():
            raise DuplicateSku(sku) from e
        raise
    return item_id


def update_item(conn, item_id, fields):
    """Update an item's details (never its stock). fields as create_item plus
    is_legacy; image_url / additional_images are the final cover and gallery
    (see item_images.apply_gallery_edits). An ASIN fills a missing cover but
    the source URL is left as the user set it. Raises DuplicateSku."""
    f = dict(fields)
    sku = f['sku']
    asin = f.get('asin') or None
    image_url, gallery = _resolve_images(f.get('image_url') or None, f.get('additional_images'), asin)
    alert_threshold = int(f.get('alert_threshold') or 0)
    with write_transaction(conn):
        if sku_in_use(conn, sku, exclude_id=item_id):
            raise DuplicateSku(sku)
        conn.execute('''
            UPDATE inventory_items SET
                sku = ?, name = ?, location_area = ?, location_aisle = ?,
                location_shelf = ?, location_bin = ?, asin = ?,
                buy_price = ?, sell_price = ?, supplier = ?,
                first_stock_date = ?, resupply_interval = ?, source_url = ?,
                image_url = ?, alert_enabled = ?, alert_threshold = ?,
                secondary_ids = ?, keywords = ?, addon_1 = ?, addon_2 = ?,
                description = ?, additional_images = ?, notes = ?,
                is_legacy = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (sku, f['name'], f.get('location_area') or None, f.get('location_aisle') or None,
              f.get('location_shelf') or None, f.get('location_bin') or None, asin,
              f.get('buy_price'), f.get('sell_price'), f.get('supplier') or None,
              f.get('first_stock_date') or None, f.get('resupply_interval'), f.get('source_url') or None,
              image_url, 1 if alert_threshold > 0 else 0, alert_threshold,
              secondary_ids_json(f.get('upc'), f.get('part_number')), f.get('keywords'),
              1 if f.get('addon_1') else 0, 1 if f.get('addon_2') else 0,
              f.get('description') or None, json.dumps(gallery) if gallery else None, f.get('notes'),
              1 if f.get('is_legacy') else 0, item_id))


def delete_item(conn, item_id):
    """Delete an item together with its stock ledger."""
    with write_transaction(conn):
        conn.execute('DELETE FROM inventory_transactions WHERE inventory_item_id = ?', (item_id,))
        conn.execute('DELETE FROM inventory_items WHERE id = ?', (item_id,))


def toggle_legacy(conn, item_id):
    """Flip an item's legacy flag. Returns (name, new_flag); raises ItemNotFound."""
    with write_transaction(conn):
        item = conn.execute('SELECT is_legacy, name FROM inventory_items WHERE id = ?', (item_id,)).fetchone()
        if not item:
            raise ItemNotFound(item_id)
        new_flag = 0 if (item['is_legacy'] or 0) else 1
        conn.execute('UPDATE inventory_items SET is_legacy = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                     (new_flag, item_id))
    return item['name'], new_flag


# Columns bulk edit may set (never quantity: stock goes through adjust_stock).
BULK_EDIT_COLUMNS = ('location_area', 'location_aisle', 'location_shelf', 'location_bin',
                     'buy_price', 'sell_price', 'keywords', 'alert_threshold', 'addon_1', 'addon_2')


def bulk_update_items(conn, item_ids, changes):
    """Set the same fields on many items. `changes` maps BULK_EDIT_COLUMNS to
    values; setting alert_threshold also sets alert_enabled (threshold > 0).
    Returns the number of item ids processed (0 when there is nothing to set)."""
    bad = set(changes) - set(BULK_EDIT_COLUMNS)
    if bad:
        raise ValueError(f"Not bulk-editable: {', '.join(sorted(bad))}")
    if not changes:
        return 0
    fields, params = [], []
    for col in BULK_EDIT_COLUMNS:
        if col in changes:
            fields.append(f"{col} = ?")
            params.append(changes[col])
            if col == 'alert_threshold':
                fields.append("alert_enabled = ?")
                params.append(1 if changes[col] > 0 else 0)
    fields.append("updated_at = CURRENT_TIMESTAMP")
    query = f"UPDATE inventory_items SET {', '.join(fields)} WHERE id = ?"
    with write_transaction(conn):
        for item_id in item_ids:
            conn.execute(query, params + [item_id])
    return len(item_ids)


def save_alert_settings(conn, settings):
    """settings: {item_id: (enabled: bool, threshold: int)}."""
    with write_transaction(conn):
        for item_id, (enabled, threshold) in settings.items():
            conn.execute('UPDATE inventory_items SET alert_enabled = ?, alert_threshold = ? WHERE id = ?',
                         (1 if enabled else 0, threshold, item_id))


# ---------------------------------------------------------------------------
# Audits
# ---------------------------------------------------------------------------

def record_audit_count(conn, session_id, item_id, count, user=None):
    """Apply a live audit count: the item's stock becomes `count` at once
    (logged as an audit correction when it differs) and the session's record
    for the item is created or updated. Raises ItemNotFound."""
    count = int(count)
    with write_transaction(conn):
        item = conn.execute('SELECT id, sku, name, quantity FROM inventory_items WHERE id = ?',
                            (item_id,)).fetchone()
        if not item:
            raise ItemNotFound(item_id)
        set_stock(conn, item_id, count, REASON_AUDIT, user)
        record = conn.execute('SELECT id FROM audit_records WHERE session_id = ? AND item_id = ?',
                              (session_id, item_id)).fetchone()
        if record:
            conn.execute('UPDATE audit_records SET counted_qty = ?, timestamp = CURRENT_TIMESTAMP WHERE id = ?',
                         (count, record['id']))
        else:
            conn.execute('''
                INSERT INTO audit_records (session_id, item_id, sku, name, expected_qty, counted_qty)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (session_id, item_id, item['sku'], item['name'], item['quantity'], count))


# ---------------------------------------------------------------------------
# CSV / Excel import
# ---------------------------------------------------------------------------

# CSV header (normalized: lower case, spaces -> underscores) -> field
IMPORT_COLUMN_MAP = {
    'sku': 'sku',
    'item_id': 'sku',       # eBay (old)
    'itemid': 'sku',        # eBay (current)
    'name': 'name',
    'item_name': 'name',    # eBay (old)
    'itemname': 'name',     # eBay (current)
    'title': 'name',
    'description': 'name',
    'qty': 'quantity',
    'quantity': 'quantity',
    'count': 'quantity',
    'area': 'location_area',
    'aisle': 'location_aisle',
    'shelf': 'location_shelf',
    'bin': 'location_bin',
    'cost': 'buy_price',
    'buy_price': 'buy_price',
    'total_price': 'buy_price',
    'ordertotal': 'buy_price',  # eBay
    'itemprice': 'buy_price',   # eBay
    'price': 'sell_price',
    'sell_price': 'sell_price',
    'supplier': 'supplier',
    'seller': 'supplier',
    'seller_name': 'supplier',
    'asin': 'asin',
    'upc': 'upc',
    'part_number': 'part_number',
    'keywords': 'keywords',
    'category': 'category',
}

ImportResult = namedtuple('ImportResult', 'added updated skipped errors')


def _map_import_row(row):
    data = {}
    for col, val in row.items():
        clean = str(col).strip().lower().replace(' ', '_')
        if clean in IMPORT_COLUMN_MAP:
            data[IMPORT_COLUMN_MAP[clean]] = val.strip() if val else None
    return data


def import_items(conn, rows, update_existing, user=None):
    """Import parsed spreadsheet rows (dicts keyed by header).

    Rows without SKU or name are skipped. New SKUs are inserted and their
    quantity logged as a 'CSV Import' change; existing SKUs are skipped, or
    with update_existing get their details refreshed and the row's quantity
    ADDED to stock (also logged). A bad row is reported (by spreadsheet row
    number) and rolled back on its own; the rest still import.
    Returns ImportResult(added, updated, skipped, errors).
    """
    added = updated = skipped = 0
    errors = []
    with write_transaction(conn):
        for i, row in enumerate(rows):
            conn.execute('SAVEPOINT import_row')
            try:
                data = _map_import_row(row)
                sku, name = data.get('sku'), data.get('name')
                if not sku or not name:
                    skipped += 1
                    conn.execute('RELEASE import_row')
                    continue

                quantity = int(data.get('quantity', 0) or 0)
                buy_price = float(data.get('buy_price', 0) or 0)
                sell_price = float(data.get('sell_price', 0) or 0)
                ids_json = secondary_ids_json(data.get('upc'), data.get('part_number'))

                existing = conn.execute("SELECT id FROM inventory_items WHERE sku = ?", (sku,)).fetchone()
                if existing:
                    if update_existing:
                        conn.execute('''
                            UPDATE inventory_items SET
                                name = ?,
                                buy_price = COALESCE(?, buy_price),
                                sell_price = COALESCE(?, sell_price),
                                location_area = COALESCE(?, location_area),
                                location_aisle = COALESCE(?, location_aisle),
                                location_shelf = COALESCE(?, location_shelf),
                                location_bin = COALESCE(?, location_bin),
                                keywords = COALESCE(?, keywords),
                                secondary_ids = COALESCE(?, secondary_ids),
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                        ''', (name, buy_price or None, sell_price or None,
                              data.get('location_area'), data.get('location_aisle'),
                              data.get('location_shelf'), data.get('location_bin'),
                              data.get('keywords'), ids_json, existing['id']))
                        adjust_stock(conn, existing['id'], quantity, REASON_CSV_IMPORT, user,
                                     allow_negative=True)
                        updated += 1
                    else:
                        skipped += 1
                else:
                    cur = conn.execute('''
                        INSERT INTO inventory_items (
                            sku, name, quantity, buy_price, sell_price,
                            location_area, location_aisle, location_shelf, location_bin,
                            supplier, asin, keywords, secondary_ids
                        ) VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (sku, name, buy_price, sell_price,
                          data.get('location_area'), data.get('location_aisle'),
                          data.get('location_shelf'), data.get('location_bin'),
                          data.get('supplier'), data.get('asin'),
                          data.get('keywords'), ids_json))
                    new_id = cur.lastrowid
                    if quantity:
                        _apply_delta(conn, new_id, quantity)
                    # Logged even for 0 so the item's history shows where it came from.
                    _log_transaction(conn, new_id, quantity, REASON_CSV_IMPORT, user, None)
                    added += 1
                conn.execute('RELEASE import_row')
            except Exception as row_e:
                conn.execute('ROLLBACK TO import_row')
                conn.execute('RELEASE import_row')
                errors.append(f"Row {i+2} Error: {row_e}")
    return ImportResult(added, updated, skipped, errors)


# ---------------------------------------------------------------------------
# Reporting: list, stats, attention
# ---------------------------------------------------------------------------

LIST_SORT_COLUMNS = ('name', 'sku', 'quantity', 'location_area', 'buy_price', 'sell_price',
                     'created_at', 'updated_at')


def list_items(conn, sort_by='name', order=None, search='', show_legacy=False, page=1, per_page=100):
    """One page of the items list. Returns (rows, total, sort_by, order).

    search: whitespace-separated tokens, each must match name, SKU, area,
    secondary IDs or keywords. Legacy items are hidden unless show_legacy.
    Location sorting walks area > aisle > shelf > bin so items in the same
    spot group together. Each row carries incoming_count: units on packages
    for its SKU that have not arrived yet.
    """
    if sort_by not in LIST_SORT_COLUMNS:
        sort_by = 'name'
    if order is None:
        order = 'desc' if sort_by == 'created_at' else 'asc'
    order_dir = 'ASC' if order.lower() == 'asc' else 'DESC'

    if sort_by == 'location_area':
        order_by = (
            f"COALESCE(location_area, '') {order_dir}, "
            f"COALESCE(location_aisle, '') {order_dir}, "
            f"COALESCE(location_shelf, '') {order_dir}, "
            f"COALESCE(location_bin, '') {order_dir}, "
            f"COALESCE(name, '') ASC"
        )
    else:
        order_by = f"{sort_by} {order_dir}"

    legacy_clause = "" if show_legacy else "AND COALESCE(is_legacy, 0) = 0"
    params = []
    if search:
        conditions = []
        for token in search.strip().split():
            pattern = f'%{token}%'
            conditions.append('''
                (COALESCE(name, '') LIKE ?
                 OR COALESCE(sku, '') LIKE ?
                 OR COALESCE(location_area, '') LIKE ?
                 OR COALESCE(secondary_ids, '') LIKE ?
                 OR COALESCE(keywords, '') LIKE ?)
            ''')
            params.extend([pattern] * 5)
        where = "WHERE (" + " AND ".join(conditions) + f") {legacy_clause}"
    else:
        where = "WHERE 1=1 " + legacy_clause

    total = conn.execute(f'SELECT COUNT(*) FROM inventory_items {where}', params).fetchone()[0]
    not_incoming = ', '.join(f"'{s}'" for s in _NOT_INCOMING)
    rows = conn.execute(f'''
        SELECT *,
        (SELECT SUM(quantity) FROM packages p
         WHERE p.sku = inventory_items.sku
         AND p.status NOT IN ({not_incoming})
        ) as incoming_count
        FROM inventory_items
        {where}
        ORDER BY {order_by}
        LIMIT ? OFFSET ?
    ''', params + [per_page, (page - 1) * per_page]).fetchall()
    return rows, total, sort_by, order


def low_stock_items(conn):
    """Non-legacy items that are out of stock or at/below their own threshold."""
    return conn.execute('''
        SELECT * FROM inventory_items
        WHERE COALESCE(is_legacy, 0) = 0
        AND (quantity <= 0 OR (quantity <= alert_threshold AND alert_threshold > 0))
        ORDER BY quantity ASC, name ASC
    ''').fetchall()


# An item "needs attention" when it is out of stock, or low: at/below its own
# alert threshold, or (no threshold of its own) at/below the global one.
_ATTENTION_WHERE = '''
    WHERE COALESCE(is_legacy, 0) = 0
    AND (
        quantity <= 0
        OR (
            quantity > 0 AND (
                (COALESCE(alert_threshold, 0) > 0 AND quantity <= alert_threshold)
                OR
                (COALESCE(alert_threshold, 0) = 0 AND ? > 0 AND quantity <= ?)
            )
        )
    )
'''


def _format_age(delta):
    if delta.days >= 1:
        return f"{delta.days} days"
    if delta.seconds < 3600:
        return f"{int(delta.seconds / 60)} mins"
    return f"{int(delta.seconds / 3600)} hours"


def inventory_stats(conn, low_threshold, now=None):
    """Overview numbers. low_threshold is the global LOW_STOCK_THRESHOLD."""
    now = now or datetime.now()
    stats = {}
    stats['total_items'] = conn.execute('SELECT COUNT(*) FROM inventory_items WHERE COALESCE(is_legacy, 0) = 0').fetchone()[0]
    stats['total_quantity'] = conn.execute('SELECT COALESCE(SUM(quantity), 0) FROM inventory_items WHERE COALESCE(is_legacy, 0) = 0').fetchone()[0]
    stats['out_of_stock'] = conn.execute('SELECT COUNT(*) FROM inventory_items WHERE quantity <= 0 AND COALESCE(is_legacy, 0) = 0').fetchone()[0]
    stats['low_stock'] = conn.execute('''
        SELECT COUNT(*) FROM inventory_items
        WHERE quantity > 0
        AND COALESCE(is_legacy, 0) = 0
        AND (
            (COALESCE(alert_threshold, 0) > 0 AND quantity <= alert_threshold)
            OR
            (COALESCE(alert_threshold, 0) = 0 AND ? > 0 AND quantity <= ?)
        )
    ''', (low_threshold, low_threshold)).fetchone()[0]

    financials = conn.execute('''
        SELECT SUM(quantity * sell_price) as revenue, SUM(quantity * buy_price) as cost
        FROM inventory_items
        WHERE quantity > 0 AND buy_price > 0 AND sell_price > 0
    ''').fetchone()
    revenue = financials['revenue'] or 0
    cost = financials['cost'] or 0
    potential_profit = revenue - cost
    stats['margin_percent'] = round((potential_profit / revenue) * 100, 1) if revenue > 0 else 0
    stats['potential_profit'] = potential_profit

    last_audit = conn.execute("SELECT end_time FROM audit_sessions WHERE status='completed' ORDER BY end_time DESC LIMIT 1").fetchone()
    stats['last_audit'] = "Never"
    if last_audit and last_audit['end_time']:
        audit_dt = datetime.strptime(str(last_audit['end_time']).split('.')[0], '%Y-%m-%d %H:%M:%S')
        stats['last_audit'] = _format_age(now - audit_dt)
    return stats


def overview_data(conn, low_threshold, now=None):
    """Top movers, attention list (minus snoozed items), snoozed count and the
    31-day sales trend for the inventory overview page."""
    now = now or datetime.now()
    today = now.date()
    thirty_days_ago = (today - timedelta(days=30)).strftime('%Y-%m-%d')
    top_movers = conn.execute('''
        SELECT i.id, i.name, i.sku, ABS(SUM(t.quantity_change)) as sold
        FROM inventory_transactions t
        JOIN inventory_items i ON t.inventory_item_id = i.id
        WHERE t.quantity_change < 0
          AND t.reason IN ('Sold/Consumed', 'Damaged')
          AND t.created_at > ?
        GROUP BY i.id
        ORDER BY sold DESC
        LIMIT 5
    ''', (thirty_days_ago,)).fetchall()

    now_str = now.strftime('%Y-%m-%d %H:%M:%S')
    attention_items = conn.execute(f'''
        SELECT id, name, quantity
        FROM inventory_items
        {_ATTENTION_WHERE}
        AND (attention_snoozed_until IS NULL OR attention_snoozed_until <= ?)
        ORDER BY quantity ASC, name ASC
        LIMIT 10
    ''', (low_threshold, low_threshold, now_str)).fetchall()
    snoozed_count = conn.execute(f'''
        SELECT COUNT(*) FROM inventory_items
        {_ATTENTION_WHERE}
        AND attention_snoozed_until > ?
    ''', (low_threshold, low_threshold, now_str)).fetchone()[0]

    sales = conn.execute('''
        SELECT date(created_at) as day, COALESCE(SUM(ABS(quantity_change)), 0) as cnt
        FROM inventory_transactions
        WHERE quantity_change < 0
          AND reason = 'Sold/Consumed'
          AND date(created_at) >= ?
        GROUP BY date(created_at)
    ''', (thirty_days_ago,)).fetchall()
    by_day = {row['day']: row['cnt'] for row in sales}
    sales_trend = []
    for i in range(30, -1, -1):
        day = (today - timedelta(days=i)).strftime('%Y-%m-%d')
        sales_trend.append({'date': day[-5:], 'count': by_day.get(day, 0)})

    return {'top_movers': top_movers, 'attention_items': attention_items,
            'snoozed_count': snoozed_count, 'sales_trend': sales_trend}


def snooze_attention(conn, item_id, days=ATTENTION_SNOOZE_DAYS, now=None):
    """Hide an item from 'Items Needing Attention' for `days` days.
    Returns (name, until); raises ItemNotFound."""
    until = (now or datetime.now()) + timedelta(days=days)
    with write_transaction(conn):
        item = conn.execute('SELECT name FROM inventory_items WHERE id = ?', (item_id,)).fetchone()
        if not item:
            raise ItemNotFound(item_id)
        conn.execute('UPDATE inventory_items SET attention_snoozed_until = ? WHERE id = ?',
                     (until.strftime('%Y-%m-%d %H:%M:%S'), item_id))
    return item['name'], until
