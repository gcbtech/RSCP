import logging
import sqlite3
from app.services.db import get_db_connection, init_db, DB_PATH

logger = logging.getLogger(__name__)

def _safe_add_column(conn, table, column, column_def):
    """
    Safely adds a column to an existing table if it does not already exist.
    Checks column existence first via table_info to prevent redundant and slow ALTER TABLE statements.
    Retries automatically with randomized exponential backoff and jitter if the database is locked.
    """
    import time
    import random
    
    # 1. Read table columns with lock retries
    cursor = conn.cursor()
    cols = []
    for attempt in range(10):
        try:
            cursor.execute(f"PRAGMA table_info({table})")
            cols = [c[1] for c in cursor.fetchall()]
            break
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < 9:
                time.sleep(0.05 + random.random() * 0.15)
                continue
            raise
            
    if column in cols:
        return
        
    # 2. Alter table with lock retries
    sql = f"ALTER TABLE {table} ADD COLUMN {column} {column_def}"
    for attempt in range(10):
        try:
            conn.execute(sql)
            conn.commit()
            logger.info(f"Successfully added column '{column}' to table '{table}'.")
            return
        except sqlite3.OperationalError as e:
            err_msg = str(e).lower()
            if "duplicate column name" in err_msg or "already exists" in err_msg:
                return
            if "locked" in err_msg and attempt < 9:
                time.sleep(0.05 + random.random() * 0.15)
                continue
            logger.error(f"Error adding column '{column}' to table '{table}' (attempt {attempt}): {e}")
            raise

def ensure_db_ready(db_path=None):
    """Initializes the database and applies any pending schema updates."""
    init_db(db_path)

    conn = get_db_connection(db_path)
    try:
        # Schema Update Check (Phase 9 - PIN Support)
        _safe_add_column(conn, 'users', 'pin_hash', 'TEXT')
        

        # Add roles column to users table (for module access control)
        _safe_add_column(conn, 'users', 'roles', "TEXT DEFAULT '[]'")
        
        # Migrate existing users to have proper roles based on is_admin flag
        try:
            # Set super_admin for admins who don't have roles yet
            conn.execute("""
                UPDATE users 
                SET roles = '["super_admin"]' 
                WHERE is_admin = 1 AND (roles IS NULL OR roles = '[]' OR roles = '')
            """)
            # Set operator for non-admins who don't have roles yet
            conn.execute("""
                UPDATE users 
                SET roles = '["operator"]' 
                WHERE is_admin = 0 AND (roles IS NULL OR roles = '[]' OR roles = '')
            """)
            conn.commit()
            logger.info("Migrated existing users to role-based system.")
        except Exception as e:
            logger.warning(f"Role migration note: {e}")
        

        # V1.16.1: Add asin and source_url to packages table
        _safe_add_column(conn, 'packages', 'asin', 'TEXT')
        _safe_add_column(conn, 'packages', 'source_url', 'TEXT')
        
        # Inventory Module Tables (V1.16)
        _create_inventory_tables(conn)
        
        # Logging Table (V1.17)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS error_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                level TEXT,
                source TEXT,
                message TEXT,
                trace TEXT,
                user_id TEXT,
                status TEXT
            )
        ''')
        conn.commit()
        
        # Notifications Table (V1.20)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                user_id INTEGER,
                title TEXT NOT NULL,
                message TEXT,
                type TEXT DEFAULT 'info',
                link TEXT,
                is_read BOOLEAN DEFAULT 0,
                read_at TIMESTAMP
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_notifications_unread ON notifications(user_id, is_read)')
        conn.commit()
        
        # V1.18: Add badge_id to users table for POS badge scanning
        _safe_add_column(conn, 'users', 'badge_id', 'TEXT')
        try:
            conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_users_badge_id ON users(badge_id)')
            conn.commit()
        except Exception as e:
            logger.warning(f"Error creating unique index on users(badge_id): {e}")
        

        # POS Module Tables (V1.18)
        _create_pos_tables(conn)
        
        # _create_pos_coupon_tables(conn)
        _create_timeclock_tables(conn)
        _create_scheduled_shifts_table(conn)
        _create_scheduled_shifts_table(conn)
        _create_recurring_rules_table(conn)

        # V2.4.2: Add SSO fields to users table
        _safe_add_column(conn, 'users', 'email', 'TEXT')
        try:
            conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email)')
            conn.commit()
        except Exception as e:
            logger.warning(f"Error creating unique index on users(email): {e}")
        _safe_add_column(conn, 'users', 'auth_provider', 'TEXT')
            
        # V2.5.0: SKU Matching Support
        _safe_add_column(conn, 'packages', 'sku', 'TEXT')

        # Product Mappings Table (for recurring order matching)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS product_mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                package_name TEXT NOT NULL,
                inventory_sku TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_mapping_pkg_name ON product_mappings(package_name)')
        conn.commit()
            
    except Exception as e:
        logger.error(f"Migration Schema Check Error: {e}")
    finally:
        conn.close()


def _create_inventory_tables(conn):
    """Create inventory module tables if they don't exist."""
    try:
        # Inventory Items Table
        conn.execute('''
            CREATE TABLE IF NOT EXISTS inventory_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sku TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                quantity INTEGER DEFAULT 0,
                
                -- Location (at least one required, enforced in app)
                location_area TEXT,
                location_aisle TEXT,
                location_shelf TEXT,
                location_bin TEXT,
                
                -- Optional fields
                asin TEXT,
                image_url TEXT,
                source_url TEXT,
                buy_price REAL,
                sell_price REAL,
                supplier TEXT,
                first_stock_date TEXT,
                resupply_interval INTEGER,
                keywords TEXT,
                secondary_ids TEXT,
                description TEXT,
                notes TEXT,
                
                -- Item Addons (warranty/disclaimer)
                addon_1 BOOLEAN DEFAULT 0,
                addon_2 BOOLEAN DEFAULT 0,
                
                -- Alert settings
                alert_enabled BOOLEAN DEFAULT 0,
                alert_threshold INTEGER DEFAULT 0,
                
                -- Audit
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Migrations: Add columns if they don't exist (for existing installs)
        _safe_add_column(conn, 'inventory_items', 'image_url', 'TEXT')
        _safe_add_column(conn, 'inventory_items', 'source_url', 'TEXT')
        _safe_add_column(conn, 'inventory_items', 'alert_enabled', 'BOOLEAN DEFAULT 0')
        _safe_add_column(conn, 'inventory_items', 'alert_threshold', 'INTEGER DEFAULT 0')
        _safe_add_column(conn, 'inventory_items', 'buy_price', 'REAL DEFAULT 0.0')
        _safe_add_column(conn, 'inventory_items', 'sell_price', 'REAL DEFAULT 0.0')
        _safe_add_column(conn, 'inventory_items', 'keywords', 'TEXT')
        _safe_add_column(conn, 'inventory_items', 'secondary_ids', 'TEXT')
        _safe_add_column(conn, 'inventory_items', 'description', 'TEXT')
        _safe_add_column(conn, 'inventory_items', 'first_stock_date', 'TEXT')
        _safe_add_column(conn, 'inventory_items', 'additional_images', 'TEXT')
        _safe_add_column(conn, 'inventory_items', 'addon_1', 'BOOLEAN DEFAULT 0')
        _safe_add_column(conn, 'inventory_items', 'addon_2', 'BOOLEAN DEFAULT 0')
        _safe_add_column(conn, 'inventory_items', 'notes', 'TEXT')
        _safe_add_column(conn, 'inventory_items', 'sale_price', 'REAL DEFAULT 0.0')
        _safe_add_column(conn, 'inventory_items', 'sale_start', 'TEXT')
        _safe_add_column(conn, 'inventory_items', 'sale_end', 'TEXT')
        _safe_add_column(conn, 'inventory_items', 'sale_enabled', 'BOOLEAN DEFAULT 0')
        _safe_add_column(conn, 'inventory_items', 'sale_end_on_stock', 'BOOLEAN DEFAULT 0')
        _safe_add_column(conn, 'inventory_items', 'is_legacy', 'BOOLEAN DEFAULT 0')
        _safe_add_column(conn, 'inventory_items', 'attention_snoozed_until', 'TEXT')

        # Indexes for inventory_items
        conn.execute('CREATE INDEX IF NOT EXISTS idx_inventory_sku ON inventory_items(sku)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_inventory_asin ON inventory_items(asin)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_inventory_name ON inventory_items(name)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_inventory_quantity ON inventory_items(quantity)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_inventory_secondary ON inventory_items(secondary_ids)')
        conn.execute("CREATE INDEX IF NOT EXISTS idx_inventory_secondary_upc ON inventory_items(TRIM(json_extract(secondary_ids, '$.upc')))")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_inventory_secondary_part_number ON inventory_items(TRIM(json_extract(secondary_ids, '$.part_number')))")
        
        # Inventory Transactions Table (audit trail)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS inventory_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inventory_item_id INTEGER NOT NULL,
                quantity_change INTEGER NOT NULL,
                reason TEXT DEFAULT 'Sold/Consumed',
                user_id TEXT,
                source_tracking TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (inventory_item_id) REFERENCES inventory_items(id)
            )
        ''')
        
        # Performance: Indexes for inventory_transactions (used in sales trend queries)
        conn.execute('CREATE INDEX IF NOT EXISTS idx_trans_item ON inventory_transactions(inventory_item_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_trans_date ON inventory_transactions(created_at)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_trans_reason ON inventory_transactions(reason)')
        
        # Migrations: Add columns if they don't exist (for existing installs)
        _safe_add_column(conn, 'inventory_transactions', 'source_tracking', 'TEXT')
        
        # Audit Sessions Table
        conn.execute('''
            CREATE TABLE IF NOT EXISTS audit_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                end_time TIMESTAMP,
                user_id TEXT,
                mode TEXT NOT NULL, 
                status TEXT DEFAULT 'active'
            )
        ''')

        # Audit Records Table
        conn.execute('''
            CREATE TABLE IF NOT EXISTS audit_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                item_id INTEGER,
                sku TEXT,
                name TEXT,
                expected_qty INTEGER,
                counted_qty INTEGER,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(session_id) REFERENCES audit_sessions(id),
                FOREIGN KEY(item_id) REFERENCES inventory_items(id)
            )
        ''')

        # V2.6.8: Clear an item's "Needs Attention" snooze when stock is added or it runs out.
        # A trigger covers every code path that changes quantity (POS, refunds, audits, imports, ...).
        try:
            conn.execute('''
                CREATE TRIGGER IF NOT EXISTS trg_inventory_attention_snooze_reset
                AFTER UPDATE OF quantity ON inventory_items
                FOR EACH ROW
                WHEN NEW.attention_snoozed_until IS NOT NULL
                 AND (NEW.quantity > OLD.quantity OR (NEW.quantity <= 0 AND OLD.quantity > 0))
                BEGIN
                    UPDATE inventory_items SET attention_snoozed_until = NULL WHERE id = NEW.id;
                END
            ''')
        except sqlite3.OperationalError as e:
            logger.warning(f"Could not create attention snooze reset trigger: {e}")

        conn.commit()
        logger.info("Inventory tables initialized.")
        
    except Exception as e:
        logger.error(f"Error creating inventory tables: {e}")


def _migrate_legacy_pairing_tables(conn):
    """One-time migration from the two legacy POS pairing systems
    (session-code shared carts + plaintext-token display pairings) to the
    v3 register/peripheral model, then drops the legacy tables.

    Existing customer display pairings are preserved: tokens are re-stored
    as SHA-256 hashes, and the raw token each display already holds in its
    localStorage keeps working — displays do NOT need re-pairing after
    this upgrade.

    Idempotent: after the legacy tables are dropped this becomes a no-op.
    """
    import hashlib

    def table_exists(name):
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None

    try:
        # Registers: carry over friendly names and any live cart
        if table_exists('pos_active_terminals'):
            for row in conn.execute(
                'SELECT terminal_id, friendly_name, cart_data FROM pos_active_terminals'
            ).fetchall():
                conn.execute(
                    '''INSERT OR IGNORE INTO pos_registers (register_id, friendly_name, cart_json)
                       VALUES (?, ?, ?)''',
                    (row['terminal_id'], row['friendly_name'] or 'Register',
                     row['cart_data'] or '{"items": []}')
                )

        # Display pairings: hash tokens at rest; device-held tokens stay valid
        if table_exists('pos_customer_display_pairings'):
            for row in conn.execute(
                '''SELECT customer_terminal_id, customer_terminal_token,
                          staff_terminal_id, staff_friendly_name
                   FROM pos_customer_display_pairings'''
            ).fetchall():
                token_hash = hashlib.sha256(
                    row['customer_terminal_token'].encode('utf-8')
                ).hexdigest()
                conn.execute(
                    '''INSERT OR IGNORE INTO pos_peripherals
                       (peripheral_id, register_id, role, token_hash, friendly_name)
                       VALUES (?, ?, 'display', ?, 'Customer Display')''',
                    (row['customer_terminal_id'], row['staff_terminal_id'], token_hash)
                )
                # Ensure the paired register exists even if it never heartbeated
                conn.execute(
                    'INSERT OR IGNORE INTO pos_registers (register_id, friendly_name) VALUES (?, ?)',
                    (row['staff_terminal_id'], row['staff_friendly_name'] or 'Register')
                )

        for legacy in ('pos_customer_display_pairings', 'pos_display_pairing_codes',
                       'pos_paired_terminals', 'pos_terminal_sessions',
                       'pos_active_terminals'):
            conn.execute(f'DROP TABLE IF EXISTS {legacy}')

        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Legacy pairing migration error: {e}")


def _create_pos_tables(conn):
    """Create POS module tables if they don't exist."""
    try:
        # POS Settings Table (tax rate, feature toggles)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS pos_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        
        # Insert default settings if not present
        defaults = [
            ('TAX_RATE', '0.0'),
            ('REQUIRE_MANAGER_VOID', 'false'),
            ('ALLOW_HOLD_ORDERS', 'true'),
        ]
        for key, value in defaults:
            conn.execute('''
                INSERT OR IGNORE INTO pos_settings (key, value) VALUES (?, ?)
            ''', (key, value))
        
        # POS Orders Table (order header)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS pos_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_number TEXT UNIQUE NOT NULL,
                status TEXT DEFAULT 'completed',
                subtotal REAL NOT NULL,
                tax_rate REAL NOT NULL,
                tax_amount REAL NOT NULL,
                discount_amount REAL DEFAULT 0,
                discount_type TEXT,
                discount_reason TEXT,
                total REAL NOT NULL,
                
                payment_method TEXT NOT NULL,
                payment_details TEXT,
                
                operator_id INTEGER NOT NULL,
                terminal_id TEXT DEFAULT 'POS-1',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (operator_id) REFERENCES users(id)
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_pos_order_number ON pos_orders(order_number)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_pos_order_date ON pos_orders(created_at)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_pos_order_status ON pos_orders(status)')
        
        # POS Order Items Table (line items)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS pos_order_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                inventory_item_id INTEGER,
                sku TEXT NOT NULL,
                name TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                unit_price REAL NOT NULL,
                discount_amount REAL DEFAULT 0,
                discount_type TEXT,
                line_total REAL NOT NULL,
                FOREIGN KEY (order_id) REFERENCES pos_orders(id),
                FOREIGN KEY (inventory_item_id) REFERENCES inventory_items(id)
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_pos_item_order ON pos_order_items(order_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_pos_item_sku ON pos_order_items(sku)')
        
        # POS Refunds Table
        conn.execute('''
            CREATE TABLE IF NOT EXISTS pos_refunds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                refund_type TEXT NOT NULL,
                amount REAL NOT NULL,
                reason TEXT NOT NULL,
                reason_notes TEXT,
                items_restocked INTEGER DEFAULT 0,
                items_damaged INTEGER DEFAULT 0,
                
                manager_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (order_id) REFERENCES pos_orders(id),
                FOREIGN KEY (manager_id) REFERENCES users(id)
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_pos_refund_order ON pos_refunds(order_id)')
        
        # POS Refund Items Table (for partial refunds)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS pos_refund_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                refund_id INTEGER NOT NULL,
                order_item_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL,
                amount REAL NOT NULL,
                restock_action TEXT DEFAULT 'none',
                FOREIGN KEY (refund_id) REFERENCES pos_refunds(id),
                FOREIGN KEY (order_item_id) REFERENCES pos_order_items(id)
            )
        ''')
        
        # POS Held Orders Table (for hold/recall feature)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS pos_held_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operator_id INTEGER NOT NULL,
                cart_data TEXT NOT NULL,
                note TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (operator_id) REFERENCES users(id)
            )
        ''')
        
        # POS Audit Log Table (for full transaction audit trail)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS pos_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                user_id INTEGER,
                user_name TEXT,
                target_type TEXT,
                target_id TEXT,
                details TEXT,
                ip_address TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # =====================================================
        # POS Terminal Pairing v3 (register-hub architecture)
        # =====================================================
        # A register (the browser running /pos/) owns the authoritative
        # cart. Peripherals (customer displays and staff scanners) pair
        # to one register with a role and a persistent bearer token
        # (stored hashed) so they reconnect unattended after power-off.

        conn.execute('''
            CREATE TABLE IF NOT EXISTS pos_registers (
                register_id TEXT PRIMARY KEY,
                friendly_name TEXT NOT NULL DEFAULT 'Register',
                cart_json TEXT NOT NULL DEFAULT '{"items": []}',
                cart_version INTEGER NOT NULL DEFAULT 0,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        conn.execute('''
            CREATE TABLE IF NOT EXISTS pos_peripherals (
                peripheral_id TEXT PRIMARY KEY,
                register_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('display', 'scanner')),
                token_hash TEXT UNIQUE NOT NULL,
                pending_token TEXT,
                friendly_name TEXT,
                paired_by INTEGER,
                paired_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen TIMESTAMP,
                FOREIGN KEY (paired_by) REFERENCES users(id)
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_pos_periph_register ON pos_peripherals(register_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_pos_periph_token ON pos_peripherals(token_hash)')

        # Short-lived 6-digit codes shown on an unpaired peripheral,
        # claimed from a register. TTL enforced on read/insert.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS pos_pairing_codes (
                code TEXT PRIMARY KEY,
                peripheral_id TEXT UNIQUE NOT NULL,
                requested_role TEXT NOT NULL DEFAULT 'display'
                    CHECK (requested_role IN ('display', 'scanner')),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        conn.commit()

        # One-time carry-over from the legacy pairing systems, then drop them.
        _migrate_legacy_pairing_tables(conn)

        logger.info("POS tables and terminal pairing schemas initialized.")
        
        # ========================================
        # POS Coupons Tables
        # ========================================
        
        # Coupons Table - main coupon definitions
        conn.execute('''
            CREATE TABLE IF NOT EXISTS pos_coupons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                code TEXT UNIQUE NOT NULL,
                coupon_type TEXT NOT NULL,
                discount_type TEXT NOT NULL,
                discount_value REAL NOT NULL,
                
                buy_quantity INTEGER DEFAULT 1,
                get_quantity INTEGER DEFAULT 1,
                reward_item_id INTEGER,
                
                min_purchase REAL,
                max_uses INTEGER,
                current_uses INTEGER DEFAULT 0,
                
                start_date TEXT,
                end_date TEXT,
                cannot_combine BOOLEAN DEFAULT 0,
                
                active BOOLEAN DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                created_by INTEGER,
                
                FOREIGN KEY (reward_item_id) REFERENCES inventory_items(id),
                FOREIGN KEY (created_by) REFERENCES users(id)
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_pos_coupon_code ON pos_coupons(code)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_pos_coupon_active ON pos_coupons(active)')
        
        # Coupon Items Table - links coupons to specific items
        conn.execute('''
            CREATE TABLE IF NOT EXISTS pos_coupon_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                coupon_id INTEGER NOT NULL,
                item_id INTEGER NOT NULL,
                FOREIGN KEY (coupon_id) REFERENCES pos_coupons(id) ON DELETE CASCADE,
                FOREIGN KEY (item_id) REFERENCES inventory_items(id)
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_pos_coupon_items_coupon ON pos_coupon_items(coupon_id)')
        
        # Coupon Redemptions Table - track usage
        conn.execute('''
            CREATE TABLE IF NOT EXISTS pos_coupon_redemptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                coupon_id INTEGER NOT NULL,
                order_id INTEGER,
                serial_used TEXT,
                discount_applied REAL,
                redeemed_at TEXT DEFAULT CURRENT_TIMESTAMP,
                redeemed_by INTEGER,
                FOREIGN KEY (coupon_id) REFERENCES pos_coupons(id),
                FOREIGN KEY (order_id) REFERENCES pos_orders(id),
                FOREIGN KEY (redeemed_by) REFERENCES users(id)
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_pos_redemption_coupon ON pos_coupon_redemptions(coupon_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_pos_redemption_order ON pos_coupon_redemptions(order_id)')
        
        conn.commit()
        logger.info("POS Coupon tables initialized.")
        
        # ========================================
        # Federation Tables (Multi-Instance Linking)
        # ========================================
        
        # Federation Peers Table (linked instances)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS federation_peers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                api_key TEXT NOT NULL,
                remote_api_key TEXT,
                location_prefix TEXT,
                status TEXT DEFAULT 'pending',
                last_seen TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_by TEXT
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_federation_peer_url ON federation_peers(url)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_federation_peer_status ON federation_peers(status)')
        
        # Migrations: Add columns if they don't exist (for existing installs)
        _safe_add_column(conn, 'federation_peers', 'remote_api_key', 'TEXT')
        
        # Federation Transfers Table (pending/completed transfers)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS federation_transfers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                direction TEXT NOT NULL,
                peer_id INTEGER NOT NULL,
                item_id INTEGER,
                item_sku TEXT NOT NULL,
                item_data TEXT NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                status TEXT DEFAULT 'pending',
                requested_by TEXT,
                requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                approved_by TEXT,
                approved_at TIMESTAMP,
                expires_at TIMESTAMP,
                notes TEXT,
                FOREIGN KEY (peer_id) REFERENCES federation_peers(id)
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_federation_transfer_status ON federation_transfers(status)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_federation_transfer_peer ON federation_transfers(peer_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_federation_transfer_expires ON federation_transfers(expires_at)')
        
        conn.commit()
        logger.info("Federation tables initialized.")
        
    except Exception as e:
        logger.error(f"Error creating POS tables: {e}")


def _create_timeclock_tables(conn):
    """Create timeclock module tables if they don't exist."""
    try:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS time_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT DEFAULT 'shift',
                clock_in DATETIME NOT NULL,
                clock_out DATETIME,
                notes TEXT,
                edited_by INTEGER,
                created_at DATETIME DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY (user_id) REFERENCES users (id),
                FOREIGN KEY (edited_by) REFERENCES users (id)
            )
        ''')
        conn.commit()
        
        # Index for faster queries
        conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_time_entries_user_date ON time_entries(user_id, clock_in);
    ''')
        conn.commit()
        
        logger.info("Timeclock tables initialized.")
    except Exception as e:
        logger.error(f"Error creating timeclock tables: {e}")

def _create_scheduled_shifts_table(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS scheduled_shifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            start_time DATETIME NOT NULL,
            end_time DATETIME NOT NULL,
            created_at DATETIME DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY (user_id) REFERENCES users (id)
        );
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_shifts_user_start ON scheduled_shifts(user_id, start_time);
    ''')
    conn.commit()

def _create_recurring_rules_table(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS recurring_shift_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            day_of_week INTEGER NOT NULL, -- 0=Mon, 6=Sun
            start_time TEXT NOT NULL,     -- "09:00"
            end_time TEXT NOT NULL,       -- "17:00"
            frequency TEXT DEFAULT 'weekly',
            reference_date DATE,
            FOREIGN KEY (user_id) REFERENCES users (id)
        );
    ''')
    conn.commit()


# =============================================================================
# Versioned schema migrations
# =============================================================================
#
# ensure_db_ready() above is the idempotent *baseline*: it brings any database,
# however old, up to the v2.6.x schema. Everything after that is an ordered,
# numbered step tracked in PRAGMA user_version, so each step runs exactly once
# per database, inside its own transaction, and a failure is loud instead of
# leaving a half-migrated schema behind.
#
# run_migrations() is called synchronously from create_app() in every worker.
# An exclusive flock serialises the workers: the first one migrates, the rest
# block until it finishes and then see an up-to-date user_version and return
# immediately. No request is served until the schema is current.
#
# To change the schema: append a function to SCHEMA_MIGRATIONS. Never edit or
# reorder a step that has already shipped.

def _m2_pto_tables(conn):
    """PTO tables (previously created on demand inside timeclock routes)."""
    conn.execute('''
        CREATE TABLE IF NOT EXISTS user_pto_balances (
            user_id INTEGER PRIMARY KEY,
            balance_hours REAL DEFAULT 0.0,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS pto_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            request_type TEXT NOT NULL,
            date DATE NOT NULL,
            start_time TIME,
            end_time TIME,
            hours_requested REAL NOT NULL,
            status TEXT DEFAULT 'pending',
            admin_notes TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')


def _m3_hot_path_indexes(conn):
    """Indexes for queries that were doing full table scans."""
    # Barcode lookups use `tracking_number = ? COLLATE NOCASE`, which cannot
    # use the BINARY unique index; this one it can.
    conn.execute('CREATE INDEX IF NOT EXISTS idx_packages_tracking_nocase '
                 'ON packages(tracking_number COLLATE NOCASE)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_history_package ON history(package_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_history_action_ts ON history(action, timestamp)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_login_ip_ts ON login_attempts(ip_address, timestamp)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_pos_order_items_item ON pos_order_items(inventory_item_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_pos_refund_items_line ON pos_refund_items(order_item_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_audit_records_session ON audit_records(session_id)')


def _m4_rate_events(conn):
    """Shared (cross-worker) rate-limit ledger, see app/services/throttle.py."""
    conn.execute('''
        CREATE TABLE IF NOT EXISTS rate_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bucket TEXT NOT NULL,
            key TEXT NOT NULL,
            ts REAL NOT NULL
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_rate_events_lookup ON rate_events(bucket, key, ts)')


def _m5_checkout_token(conn):
    """Idempotency key for POS checkout: a double-submitted sale is recorded once."""
    _add_column_in_txn(conn, 'pos_orders', 'checkout_token', 'TEXT')
    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_pos_orders_checkout_token '
                 'ON pos_orders(checkout_token) WHERE checkout_token IS NOT NULL')


def _m6_settings_store(conn):
    """Typed settings table; imports config.json (minus bootstrap keys) and
    pos_settings. Both legacy stores are left untouched for rollback."""
    from app.services import settings as settings_store
    settings_store.create_tables(conn)
    settings_store.import_legacy(conn)


def _m11_reimport_encoded_settings(conn):
    """Re-run the legacy settings import for keys still missing.

    Migration 6 originally rejected 2.x's encoded webhook URLs (validated as
    plaintext URLs), leaving WEBHOOK_ENABLED on with no URL. INSERT OR IGNORE:
    nothing already in the settings table is touched.
    """
    from app.services import settings as settings_store
    settings_store.import_legacy(conn)


def _m9_unify_admin_roles(conn):
    """One permission model: admin == the super_admin role.

    Every user with is_admin=1 gets super_admin; roles JSON is normalized
    (lower-case, known roles only, de-duplicated, canonical order); legacy
    'user' (SSO auto-provisioning) becomes 'operator'; an account left with
    no known role gets 'operator' (least privilege); is_admin is rewritten as
    a mirror of super_admin. The role list is frozen here on purpose, so this
    step keeps meaning the same thing if the live permission map changes.
    """
    import json
    known = ('super_admin', 'inventory_admin', 'pos_admin', 'receiving_admin',
             'timeclock_admin', 'operator')
    aliases = {'user': 'operator'}

    def flag(value):
        if isinstance(value, str):
            return value.strip().lower() in ('1', 'true', 't', 'yes')
        return bool(value)

    for uid, is_admin, raw in conn.execute('SELECT id, is_admin, roles FROM users').fetchall():
        try:
            parsed = json.loads(raw) if raw else []
        except (ValueError, TypeError):
            parsed = []
        if isinstance(parsed, str):
            parsed = [parsed]
        if not isinstance(parsed, list):
            parsed = []
        found = set()
        for role in parsed:
            if isinstance(role, str):
                role = role.strip().lower()
                role = aliases.get(role, role)
                if role in known:
                    found.add(role)
        if flag(is_admin):
            found.add('super_admin')
        if not found:
            found.add('operator')
        roles = [r for r in known if r in found]
        new_raw = json.dumps(roles)
        new_admin = 1 if 'super_admin' in roles else 0
        if new_raw != raw or new_admin != is_admin:
            conn.execute('UPDATE users SET roles = ?, is_admin = ? WHERE id = ?',
                         (new_raw, new_admin, uid))


def _add_column_in_txn(conn, table, column, column_def):
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_def}")


# --- v7: timeclock times from naive local to UTC -----------------------------
# Business timezone the old values were written in. None = the server's system
# timezone (which is the business timezone). Tests set a zoneinfo.ZoneInfo.
_M7_LOCAL_TZ = None
_M7_COLUMNS = {
    'time_entries': ('clock_in', 'clock_out', 'created_at'),
    'scheduled_shifts': ('start_time', 'end_time', 'created_at'),
}
_M7_LOCALTIME_DEFAULT = r"\(\s*datetime\(\s*'now'\s*,\s*'localtime'\s*\)\s*\)"


def _m7_local_to_utc(value):
    """Naive business-local timestamp -> UTC 'YYYY-MM-DD HH:MM:SS' (None if unparseable).

    Uses the UTC offset in force on the value's own date, so January rows
    shift by 7 hours (MST) and July rows by 6 (MDT).
    """
    import datetime as dt
    if not isinstance(value, str):
        return None
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M',
                '%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M'):
        try:
            naive = dt.datetime.strptime(value.strip(), fmt).replace(microsecond=0)
            break
        except ValueError:
            continue
    else:
        return None
    if _M7_LOCAL_TZ is not None:
        aware = naive.replace(tzinfo=_M7_LOCAL_TZ)
    else:
        aware = naive.astimezone()  # system timezone rules for that date
    return aware.astimezone(dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def _m7_rebuild_with_utc_default(conn, table, create_sql):
    """Recreate `table` with created_at DEFAULT CURRENT_TIMESTAMP (SQLite cannot ALTER a default)."""
    import re
    tmp = f'{table}__m7'
    new_sql = re.sub(_M7_LOCALTIME_DEFAULT, 'CURRENT_TIMESTAMP', create_sql, flags=re.I)
    new_sql, n = re.subn(r'^\s*CREATE\s+TABLE\s+(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[^\s(]+)',
                         f'CREATE TABLE "{tmp}"', new_sql, count=1, flags=re.I)
    if n != 1:
        logger.warning(f"[Schema v7] Could not rebuild {table}; created_at default left as localtime")
        return
    extras = [r[0] for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name = ? AND type IN ('index', 'trigger') "
        "AND sql IS NOT NULL", (table,)).fetchall()]
    cols = ', '.join(f'"{r[1]}"' for r in conn.execute(f'PRAGMA table_info({table})').fetchall())
    seq = None
    try:
        seq = conn.execute('SELECT seq FROM sqlite_sequence WHERE name = ?', (table,)).fetchone()
    except sqlite3.OperationalError:
        pass  # no AUTOINCREMENT tables yet
    conn.execute(f'DROP TABLE IF EXISTS "{tmp}"')
    conn.execute(new_sql)
    conn.execute(f'INSERT INTO "{tmp}" ({cols}) SELECT {cols} FROM {table}')
    conn.execute(f'DROP TABLE {table}')
    conn.execute(f'ALTER TABLE "{tmp}" RENAME TO {table}')
    for sql in extras:
        conn.execute(sql)
    if seq:
        conn.execute('UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = ?', (seq[0], table))


def _m7_timeclock_utc(conn):
    """Timeclock times were naive local (datetime.now()); store UTC like everything else.

    Converts time_entries.clock_in/clock_out and scheduled_shifts.start_time/
    end_time, plus created_at where the column default was localtime. NULLs
    stay NULL; unparseable values are left unchanged and logged. The tables are
    then rebuilt so created_at defaults to CURRENT_TIMESTAMP (UTC). PTO dates
    and recurring-rule wall-clock times are local by design and not touched.
    """
    import re
    for table, wanted in _M7_COLUMNS.items():
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                           (table,)).fetchone()
        if not row:
            continue
        create_sql = row[0] or ''
        local_default = re.search(_M7_LOCALTIME_DEFAULT, create_sql, flags=re.I) is not None
        present = {r[1] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()}
        cols = [c for c in wanted if c in present and (c != 'created_at' or local_default)]
        converted = bad = 0
        if cols:
            rows = conn.execute(f"SELECT rowid, {', '.join(cols)} FROM {table}").fetchall()
            for r in rows:
                updates = {}
                for col, value in zip(cols, r[1:]):
                    if value is None:
                        continue
                    new = _m7_local_to_utc(value)
                    if new is None:
                        bad += 1
                        logger.warning(f"[Schema v7] {table} rowid {r[0]}: {col}={value!r} "
                                       f"is not a timestamp; left unchanged")
                        continue
                    updates[col] = new
                if updates:
                    sets = ', '.join(f'{c} = ?' for c in updates)
                    conn.execute(f'UPDATE {table} SET {sets} WHERE rowid = ?', (*updates.values(), r[0]))
                    converted += 1
        logger.info(f"[Schema v7] {table}: converted {converted} row(s) to UTC, "
                    f"{bad} value(s) left unchanged")
        if local_default:
            _m7_rebuild_with_utc_default(conn, table, create_sql)
def _m10_legacy_manifest_paths():
    import os
    from app import paths
    return [os.path.join(paths.DATA_DIR, 'manifest.csv'), os.path.join(paths.APP_DIR, 'manifest.csv')]


def _m10_tracking_alone_is_unique(conn):
    for idx in conn.execute("PRAGMA index_list(packages)").fetchall():
        if idx[2]:  # unique
            cols = [r[2] for r in conn.execute(f'PRAGMA index_info("{idx[1]}")').fetchall()]
            if cols == ['tracking_number']:
                return True
    return False


def _m10_rebuild_packages(conn):
    """Rebuild packages without UNIQUE(tracking_number): one shipment can hold
    several items (log_receipt receives every row of a tracking number), and
    the old constraint made the manifest sync abort on such files. Row
    identity becomes (tracking_number, item_name), enforced by the unique
    index idx_packages_tracking_item. ids, every column,
    indexes, triggers and the AUTOINCREMENT high-water mark are preserved."""
    cols = conn.execute("PRAGMA table_info(packages)").fetchall()  # cid,name,type,notnull,dflt,pk
    names = [c[1] for c in cols]
    seq = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'packages'").fetchone()
    extras = conn.execute("SELECT sql FROM sqlite_master WHERE tbl_name = 'packages' "
                          "AND type IN ('index', 'trigger') AND sql IS NOT NULL").fetchall()

    defs = []
    for _cid, name, ctype, notnull, dflt, pk in cols:
        if pk:
            defs.append(f'"{name}" INTEGER PRIMARY KEY AUTOINCREMENT')
            continue
        d = f'"{name}" {ctype or ""}'.rstrip()
        if notnull:
            d += ' NOT NULL'
        if dflt is not None:
            d += f' DEFAULT {dflt}'
        defs.append(d)
    if 'order_id' not in names:
        defs.append('"order_id" TEXT')
    # (the (tracking_number, item_name) rule is the named index created by the caller)

    collist = ', '.join(f'"{n}"' for n in names)
    conn.execute('DROP TABLE IF EXISTS packages_v10')
    conn.execute(f'CREATE TABLE packages_v10 ({", ".join(defs)})')
    conn.execute(f'INSERT INTO packages_v10 ({collist}) SELECT {collist} FROM packages')
    conn.execute('DROP TABLE packages')
    conn.execute('ALTER TABLE packages_v10 RENAME TO packages')
    for (sql,) in extras:
        conn.execute(sql)
    if seq:
        conn.execute("DELETE FROM sqlite_sequence WHERE name = 'packages'")
        conn.execute("INSERT INTO sqlite_sequence (name, seq) "
                     "VALUES ('packages', MAX(?, (SELECT COALESCE(MAX(id), 0) FROM packages)))",
                     (seq[0],))


def _m10_packages_db_only(conn):
    """Packages live only in the database (manifest.csv retired)."""
    import os
    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        if _m10_tracking_alone_is_unique(conn):
            _m10_rebuild_packages(conn)
        _add_column_in_txn(conn, 'packages', 'order_id', 'TEXT')
        conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_packages_tracking_item '
                     'ON packages(tracking_number, item_name)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_packages_tracking_nocase '
                     'ON packages(tracking_number COLLATE NOCASE)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_packages_order_id ON packages(order_id)')

        conn.execute('''
            CREATE TABLE IF NOT EXISTS package_imports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL CHECK (source IN ('upload', 'email', 'manual')),
                filename TEXT,
                rows_added INTEGER NOT NULL DEFAULT 0,
                rows_updated INTEGER NOT NULL DEFAULT 0,
                imported_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                user TEXT
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_package_imports_at ON package_imports(imported_at)')

        # The two email-ingest paths wrote different values; there is one now.
        conn.execute("UPDATE packages SET source = 'email' "
                     "WHERE source IN ('auto-email', 'auto_email', 'Auto-Email')")
        # 'incoming' was never moved to 'received' by a scan.
        conn.execute("UPDATE packages SET status = CASE WHEN date_scanned IS NULL "
                     "THEN 'on_time' ELSE 'received' END WHERE status = 'incoming'")

        from app.services import package_imports
        for row in conn.execute("SELECT id, tracking_number FROM packages "
                                "WHERE order_id IS NULL AND tracking_number LIKE 'ORDER-%'").fetchall():
            oid = package_imports.order_id_from_placeholder(row['tracking_number'])
            if oid:
                conn.execute('UPDATE packages SET order_id = ? WHERE id = ?', (oid, row['id']))

        # One-time import of a legacy manifest.csv. A bad file must not stop
        # the upgrade: its rows are rolled back and the file stays for review.
        seen = set()
        for path in _m10_legacy_manifest_paths():
            real = os.path.realpath(path)
            if real in seen or not os.path.isfile(path):
                continue
            seen.add(real)
            conn.execute('SAVEPOINT legacy_manifest')
            try:
                added = package_imports.import_legacy_manifest(conn, path)
                conn.execute('RELEASE legacy_manifest')
                logger.info(f"[Schema] Imported {added} rows from legacy {path} (file left in place)")
            except Exception as e:
                conn.execute('ROLLBACK TO legacy_manifest')
                conn.execute('RELEASE legacy_manifest')
                logger.error(f"[Schema] Could not import legacy {path}: {e}")
    finally:
        conn.row_factory = previous_factory
def _m8_pos_ledger(conn):
    """Payments ledger, refund method/tax, cash drawer sessions; backfill.

    Self-contained on purpose (a frozen copy of the allocation rules): later
    changes to app code must not change what this step does.

    pos_payments.amount is what the tender paid toward the sale; tendered and
    change_given are the cash handed over and handed back. Backfill rules:
      cash  -> one cash row: amount = total, tendered/change from the JSON
      card  -> one card row: amount = total
      split -> card rows as given (capped at what is left of the total), one
               cash row: applied = total - cards, tendered = cash handed,
               change = tendered - applied
    Refunds get refund_method (the order's method; split -> cash) and
    tax_refunded = amount * order tax / order total.
    """
    import json as _json
    from decimal import Decimal, ROUND_HALF_UP

    def money(v):
        try:
            d = Decimal(str(v))
        except Exception:
            return None
        if not d.is_finite():
            return None
        return float(d.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))

    conn.execute('''
        CREATE TABLE IF NOT EXISTS pos_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            method TEXT NOT NULL,
            amount REAL NOT NULL,
            tendered REAL,
            change_given REAL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (order_id) REFERENCES pos_orders(id)
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_pos_payments_order ON pos_payments(order_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_pos_payments_method_ts ON pos_payments(method, created_at)')
    _add_column_in_txn(conn, 'pos_orders', 'cash_discount', 'REAL DEFAULT 0')
    _add_column_in_txn(conn, 'pos_refunds', 'refund_method', 'TEXT')
    _add_column_in_txn(conn, 'pos_refunds', 'tax_refunded', 'REAL')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_pos_refunds_created ON pos_refunds(created_at)')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS drawer_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            register_id TEXT,   -- pos_registers.register_id is TEXT
            opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            opened_by INTEGER,
            opening_float REAL NOT NULL DEFAULT 0,
            closed_at TIMESTAMP,
            closed_by INTEGER,
            counted_cash REAL,
            expected_cash REAL,
            over_short REAL,
            notes TEXT
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_drawer_sessions_open ON drawer_sessions(closed_at, opened_at)')

    # Backfill orders that have no ledger rows yet (idempotent)
    orders = conn.execute('''
        SELECT id, payment_method, payment_details, total, created_at FROM pos_orders o
        WHERE NOT EXISTS (SELECT 1 FROM pos_payments p WHERE p.order_id = o.id)
    ''').fetchall()
    for order_id, method, details_raw, total, created_at in orders:
        try:
            details = _json.loads(details_raw) if details_raw else {}
        except (ValueError, TypeError):
            details = {}
        if not isinstance(details, dict):
            details = {}
        total = money(total) or 0.0
        rows = []  # (method, amount, tendered, change)
        if method == 'cash':
            tendered = money(details.get('tendered'))
            tendered = total if tendered is None or tendered < total else tendered
            rows.append(('cash', total, tendered, money(tendered - total)))
        elif method == 'split':
            cards = details.get('cards')
            cards = cards if isinstance(cards, list) else []
            remaining = total
            for c in cards:
                c = money(c)
                if c is None or c <= 0:
                    continue
                applied = min(c, remaining)
                rows.append(('card', applied, None, 0.0))
                remaining = money(remaining - applied)
            handed = money(details.get('cash', 0)) or 0.0
            handed = max(handed, 0.0)
            if remaining > 0 or handed > 0:
                tendered = max(handed, remaining)
                rows.append(('cash', remaining, tendered, money(tendered - remaining)))
        else:
            rows.append((method or 'card', total, None, 0.0))
        cash_discount = money(details.get('cash_discount', 0)) or 0.0
        if cash_discount:
            conn.execute('UPDATE pos_orders SET cash_discount = ? WHERE id = ?', (cash_discount, order_id))
        for m, amount, tendered, change in rows:
            conn.execute('''INSERT INTO pos_payments (order_id, method, amount, tendered, change_given, created_at)
                            VALUES (?, ?, ?, ?, ?, ?)''', (order_id, m, amount, tendered, change, created_at))

    # Backfill refunds (only rows not yet filled)
    refunds = conn.execute('''
        SELECT r.id, r.amount, r.refund_method, r.tax_refunded, o.payment_method, o.tax_amount, o.total,
               r.refund_type
        FROM pos_refunds r LEFT JOIN pos_orders o ON o.id = r.order_id
        WHERE r.refund_method IS NULL OR r.tax_refunded IS NULL
    ''').fetchall()
    for rid, amount, r_method, r_tax, o_method, o_tax, o_total, r_type in refunds:
        if r_method is None:
            r_method = 'card' if o_method == 'card' else 'cash'
        if r_tax is None:
            if r_type == 'partial':
                r_tax = 0.0   # 2.x partial refunds paid the pre-tax line total
            else:
                r_tax = money((amount or 0) * (o_tax or 0) / o_total) if o_total else 0.0
        conn.execute('UPDATE pos_refunds SET refund_method = ?, tax_refunded = ? WHERE id = ?',
                     (r_method, r_tax, rid))


def _m12_drop_email_ingest_settings(conn):
    """The Amazon/eBay email ingest was removed: delete its stored settings
    (including the IMAP password). Packages it created are real data and stay.
    Self-contained on purpose: the keys are no longer in the settings registry."""
    keys = ('EMAIL_INGEST_ENABLED', 'IMAP_SERVER', 'EMAIL_USER', 'EMAIL_PASS', 'EMAIL_PASSWORD')
    has_settings = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'settings'").fetchone()
    if not has_settings:
        return
    cur = conn.execute(f"DELETE FROM settings WHERE key IN ({', '.join('?' * len(keys))})", keys)
    if cur.rowcount:
        # Tell every worker's settings cache to reload.
        conn.execute("UPDATE settings_meta SET token = lower(hex(randomblob(16))) WHERE id = 1")


# Also used by app/jobs.py (CREATE ... IF NOT EXISTS), so a job started by a
# timer before the web service has migrated still has somewhere to write.
JOB_TABLES_SQL = (
    '''CREATE TABLE IF NOT EXISTS job_status (
        name TEXT PRIMARY KEY,
        last_started TEXT,
        last_finished TEXT,
        last_result TEXT,             -- ok | failed | running
        last_detail TEXT,
        last_trigger TEXT,
        duration_s REAL,
        last_success TEXT,
        last_failure TEXT,
        last_error TEXT,
        run_count INTEGER NOT NULL DEFAULT 0,
        failure_count INTEGER NOT NULL DEFAULT 0,
        consecutive_failures INTEGER NOT NULL DEFAULT 0
    )''',
    '''CREATE TABLE IF NOT EXISTS job_markers (
        name TEXT PRIMARY KEY,
        value TEXT,
        updated_at TEXT
    )''',
)


def _m13_job_status(conn):
    """Scheduled-job bookkeeping (app/jobs.py): last run/result per job for
    the admin "Scheduled jobs" panel, and markers that make jobs idempotent
    (the end-of-day email's "sent today" claim)."""
    for sql in JOB_TABLES_SQL:
        conn.execute(sql)


# (version, description, function). Version 1 is the ensure_db_ready() baseline.
SCHEMA_MIGRATIONS = [
    (2, 'PTO tables', _m2_pto_tables),
    (3, 'hot-path indexes', _m3_hot_path_indexes),
    (4, 'rate_events table', _m4_rate_events),
    (5, 'pos_orders.checkout_token', _m5_checkout_token),
    (6, 'settings table (config.json + pos_settings)', _m6_settings_store),
    (7, 'timeclock times to UTC', _m7_timeclock_utc),
    (8, 'POS payments ledger, refund method/tax, drawer sessions', _m8_pos_ledger),
    (9, 'admin flag folded into super_admin role', _m9_unify_admin_roles),
    (10, 'packages: database is the only source (manifest.csv retired)', _m10_packages_db_only),
    (11, 're-import encoded legacy settings (webhook URLs)', _m11_reimport_encoded_settings),
    (12, 'drop email-ingest settings (feature removed)', _m12_drop_email_ingest_settings),
    (13, 'job_status and job_markers tables (scheduled jobs)', _m13_job_status),
]
LATEST_SCHEMA_VERSION = max(v for v, _, _ in SCHEMA_MIGRATIONS)
assert len({v for v, _, _ in SCHEMA_MIGRATIONS}) == len(SCHEMA_MIGRATIONS), "duplicate schema version"


class _MigrationLock:
    """Exclusive cross-process lock next to the database (no-op without fcntl)."""

    def __init__(self, db_path):
        self.path = db_path + '.migrate.lock'
        self.fd = None

    def __enter__(self):
        try:
            import fcntl
        except ImportError:
            return self
        self.fd = open(self.path, 'a+')
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            self.fd.close()  # closing releases the flock
        return False


def _user_version(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        return conn.execute('PRAGMA user_version').fetchone()[0]
    finally:
        conn.close()


def run_migrations(db_path=None):
    """Bring the database fully up to date. Raises on failure.

    Safe to call from every worker at startup: cheap (one PRAGMA read) when
    the schema is already current.
    """
    if db_path is None:
        db_path = DB_PATH
        try:  # honour a test/app-configured DATABASE, like get_db_connection()
            from flask import current_app
            db_path = current_app.config.get('DATABASE', DB_PATH)
        except RuntimeError:
            pass
    if db_path != ':memory:' and _user_version(db_path) >= LATEST_SCHEMA_VERSION:
        return

    with _MigrationLock(db_path):
        version = _user_version(db_path)
        if version >= LATEST_SCHEMA_VERSION:
            return  # another worker finished while we waited for the lock

        if version < 1:
            logger.info("[Schema] Applying baseline schema")
            ensure_db_ready(db_path)
            conn = sqlite3.connect(db_path, timeout=30)
            try:
                conn.execute('PRAGMA user_version = 1')
            finally:
                conn.close()
            version = 1

        conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
        try:
            # Always apply in version order, whatever order the list was edited in
            for target, description, fn in sorted(SCHEMA_MIGRATIONS, key=lambda m: m[0]):
                if target <= version:
                    continue
                logger.info(f"[Schema] Migrating to v{target}: {description}")
                conn.execute('BEGIN IMMEDIATE')
                try:
                    fn(conn)
                    conn.execute(f'PRAGMA user_version = {int(target)}')
                    conn.execute('COMMIT')
                except Exception:
                    conn.execute('ROLLBACK')
                    logger.exception(f"[Schema] Migration to v{target} ({description}) FAILED")
                    raise
                version = target
        finally:
            conn.close()
        logger.info(f"[Schema] Database is at schema v{version}")
