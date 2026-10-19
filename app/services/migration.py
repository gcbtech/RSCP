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


# --- v15 / v16: money as integer cents, then enforced foreign keys -----------
# Both are self-contained on purpose (frozen copies of the conversion rules):
# later changes to app code must not change what these steps do.

def _rebuild_table(conn, table, columns_sql, select_map, drop=(), tag='v15'):
    """The SQLite "12-step" table rebuild (https://www.sqlite.org/lang_altertable.html
    #otheralter), steps 3-8; the runner keeps foreign_keys OFF and the
    transaction open, and migration 16 runs PRAGMA foreign_key_check.

    columns_sql  the new table's column and constraint definitions
    select_map   {new column: SQL expression over the old table's columns}
                 for columns that are not copied as-is by name
    drop         old columns intentionally not copied (replaced via select_map)

    The table keeps its name, every row keeps its id/rowid, indexes and
    triggers are re-created from their stored SQL, and the AUTOINCREMENT
    high-water mark is kept. The old table is never renamed (that would
    rewrite REFERENCES in other tables); the new one is renamed into place.
    A column this code does not know about (added by hand, or by a version
    that never shipped here) is carried over as it is, with a warning, so no
    data is lost. An index that cannot be re-created (it named a replaced
    column) is skipped with a warning; a trigger that cannot is an error.
    """
    tmp = f'{table}__{tag}'
    old_info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()   # cid,name,type,notnull,dflt,pk
    old_cols = [r[1] for r in old_info]
    if not old_cols:
        return False
    conn.execute(f'DROP TABLE IF EXISTS "{tmp}"')
    conn.execute(f'CREATE TABLE "{tmp}" ({columns_sql})')
    new_cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{tmp}")').fetchall()]
    unknown = [r for r in old_info if r[1] not in new_cols and r[1] not in drop]
    if unknown:
        logger.warning(f"[Schema {tag}] {table}: keeping unexpected column(s) "
                       f"{[r[1] for r in unknown]} as they are")
        for _cid, name, ctype, _notnull, dflt, _pk in unknown:
            conn.execute(f'ALTER TABLE "{tmp}" ADD COLUMN "{name}" {ctype or ""}'
                         + (f' DEFAULT {dflt}' if dflt is not None else ''))
            new_cols.append(name)
    targets, exprs = [], []
    for col in new_cols:
        if col in select_map:
            targets.append(f'"{col}"')
            exprs.append(select_map[col])
        elif col in old_cols:
            targets.append(f'"{col}"')
            exprs.append(f'"{col}"')
    extras = [r[0] for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name = ? AND type IN ('index', 'trigger') "
        "AND sql IS NOT NULL", (table,)).fetchall()]
    seq = None
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'").fetchone():
        seq = conn.execute('SELECT seq FROM sqlite_sequence WHERE name = ?', (table,)).fetchone()
    conn.execute(f'INSERT INTO "{tmp}" ({", ".join(targets)}) SELECT {", ".join(exprs)} FROM "{table}"')
    conn.execute(f'DROP TABLE "{table}"')
    conn.execute(f'ALTER TABLE "{tmp}" RENAME TO "{table}"')
    for sql in extras:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError as e:
            if not sql.lstrip().upper().startswith(('CREATE INDEX', 'CREATE UNIQUE INDEX')):
                raise
            logger.warning(f"[Schema {tag}] {table}: index not re-created ({e}): {sql}")
    if seq:
        conn.execute('DELETE FROM sqlite_sequence WHERE name = ?', (table,))
        conn.execute(f'INSERT INTO sqlite_sequence (name, seq) VALUES (?, MAX(?, (SELECT COALESCE(MAX(rowid), 0) '
                     f'FROM "{table}")))', (table, seq[0]))
    return True


def _m15_cents(value):
    """Stored dollars (REAL, int or text) -> int cents, half-up; None for NULL,
    blank or junk. Decimal(str(x)) so 19.995 -> 2000 and 0.1+0.2 -> 30."""
    from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value).strip().replace('$', '').replace(',', ''))
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite():
        return None
    return int(d.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) * 100)


def _m15_cart(cart):
    """A cart dict saved in dollars -> cents (same rules as
    pos_pricing.upgrade_legacy_cart, frozen here). Returns True if changed."""
    if not isinstance(cart, dict):
        return False
    changed = False

    def discount(d):
        raw = d.pop('discount_amount', 0)
        dtype = d.get('discount_type')
        d['discount_percent'] = 0
        d['discount_cents'] = 0
        if not dtype or not raw:
            d['discount_type'] = None
            return
        if dtype == 'percent':
            try:
                d['discount_percent'] = float(raw)
            except (TypeError, ValueError):
                d['discount_type'] = None
        else:
            d['discount_type'] = 'fixed'
            d['discount_cents'] = _m15_cents(raw) or 0

    for item in cart.get('items') or []:
        if not isinstance(item, dict):
            continue
        for key in ('unit_price', 'regular_price', 'sell_price', 'line_total'):
            if key in item:
                value = item.pop(key)
                if f'{key}_cents' not in item:
                    cents = _m15_cents(value)
                    if cents is None and key in ('unit_price', 'line_total'):
                        cents = 0
                    item[f'{key}_cents'] = cents
                changed = True
        if 'discount_amount' in item:
            discount(item)
            changed = True
    if 'discount_amount' in cart:
        discount(cart)
        changed = True
    coupon = cart.get('applied_coupon')
    if isinstance(coupon, dict) and 'discount' in coupon:
        coupon.setdefault('discount_cents', _m15_cents(coupon.pop('discount')) or 0)
        changed = True
    return changed


# payment_details JSON keys that held dollars
_M15_DETAIL_KEYS = ('tendered', 'change', 'cash_discount', 'cash', 'total_tendered')

_M15_TABLES = {
    'inventory_items': ('''
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sku TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        quantity INTEGER DEFAULT 0,
        location_area TEXT,
        location_aisle TEXT,
        location_shelf TEXT,
        location_bin TEXT,
        asin TEXT,
        image_url TEXT,
        source_url TEXT,
        buy_price_cents INTEGER,
        sell_price_cents INTEGER,
        supplier TEXT,
        first_stock_date TEXT,
        resupply_interval INTEGER,
        keywords TEXT,
        secondary_ids TEXT,
        description TEXT,
        notes TEXT,
        addon_1 BOOLEAN DEFAULT 0,
        addon_2 BOOLEAN DEFAULT 0,
        alert_enabled BOOLEAN DEFAULT 0,
        alert_threshold INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        additional_images TEXT,
        sale_price_cents INTEGER DEFAULT 0,
        sale_start TEXT,
        sale_end TEXT,
        sale_enabled BOOLEAN DEFAULT 0,
        sale_end_on_stock BOOLEAN DEFAULT 0,
        is_legacy BOOLEAN DEFAULT 0,
        attention_snoozed_until TEXT
    ''', {
        'buy_price_cents': 'rscp_cents(buy_price)',
        'sell_price_cents': 'rscp_cents(sell_price)',
        'sale_price_cents': 'rscp_cents(sale_price)',
    }, ('buy_price', 'sell_price', 'sale_price')),

    # Sales tables: ON DELETE RESTRICT everywhere (a sale and everything
    # hanging off it is never removed by a cascade). Columns pointing at a
    # parent are nullable so migration 16 can detach orphans instead of
    # deleting financial rows.
    'pos_orders': ('''
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_number TEXT UNIQUE NOT NULL,
        status TEXT DEFAULT 'completed',
        subtotal_cents INTEGER NOT NULL,
        tax_rate REAL NOT NULL,
        tax_amount_cents INTEGER NOT NULL,
        discount_amount_cents INTEGER DEFAULT 0,
        discount_type TEXT,
        discount_reason TEXT,
        total_cents INTEGER NOT NULL,
        payment_method TEXT NOT NULL,
        payment_details TEXT,
        operator_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
        terminal_id TEXT DEFAULT 'POS-1',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        checkout_token TEXT,
        cash_discount_cents INTEGER DEFAULT 0
    ''', {
        'subtotal_cents': 'COALESCE(rscp_cents(subtotal), 0)',
        'tax_rate': 'COALESCE(tax_rate, 0)',
        'tax_amount_cents': 'COALESCE(rscp_cents(tax_amount), 0)',
        'discount_amount_cents': 'COALESCE(rscp_cents(discount_amount), 0)',
        'total_cents': 'COALESCE(rscp_cents(total), 0)',
        'cash_discount_cents': 'COALESCE(rscp_cents(cash_discount), 0)',
    }, ('subtotal', 'tax_amount', 'discount_amount', 'total', 'cash_discount')),

    # discount_amount held the line discount AS ENTERED: a percent when
    # discount_type was 'percent', else dollars. It is split in two.
    'pos_order_items': ('''
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER REFERENCES pos_orders(id) ON DELETE RESTRICT,
        inventory_item_id INTEGER REFERENCES inventory_items(id) ON DELETE RESTRICT,
        sku TEXT NOT NULL,
        name TEXT NOT NULL,
        quantity INTEGER NOT NULL,
        unit_price_cents INTEGER NOT NULL,
        discount_type TEXT,
        discount_percent REAL DEFAULT 0,
        discount_cents INTEGER DEFAULT 0,
        line_total_cents INTEGER NOT NULL
    ''', {
        'unit_price_cents': 'COALESCE(rscp_cents(unit_price), 0)',
        'discount_type': ("CASE WHEN discount_type IS NULL OR discount_type = '' THEN NULL "
                          "WHEN discount_type = 'percent' THEN 'percent' ELSE 'fixed' END"),
        'discount_percent': ("CASE WHEN discount_type = 'percent' THEN COALESCE(CAST(discount_amount AS REAL), 0) "
                             "ELSE 0 END"),
        'discount_cents': ("CASE WHEN discount_type IS NOT NULL AND discount_type NOT IN ('', 'percent') "
                           "THEN COALESCE(rscp_cents(discount_amount), 0) ELSE 0 END"),
        'line_total_cents': 'COALESCE(rscp_cents(line_total), 0)',
    }, ('unit_price', 'discount_amount', 'line_total')),

    'pos_refunds': ('''
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER REFERENCES pos_orders(id) ON DELETE RESTRICT,
        refund_type TEXT NOT NULL,
        amount_cents INTEGER NOT NULL,
        reason TEXT NOT NULL,
        reason_notes TEXT,
        items_restocked INTEGER DEFAULT 0,
        items_damaged INTEGER DEFAULT 0,
        manager_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        refund_method TEXT,
        tax_refunded_cents INTEGER
    ''', {
        'amount_cents': 'COALESCE(rscp_cents(amount), 0)',
        'tax_refunded_cents': 'rscp_cents(tax_refunded)',
    }, ('amount', 'tax_refunded')),

    'pos_refund_items': ('''
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        refund_id INTEGER REFERENCES pos_refunds(id) ON DELETE RESTRICT,
        order_item_id INTEGER REFERENCES pos_order_items(id) ON DELETE RESTRICT,
        quantity INTEGER NOT NULL,
        amount_cents INTEGER NOT NULL,
        restock_action TEXT DEFAULT 'none'
    ''', {
        'amount_cents': 'COALESCE(rscp_cents(amount), 0)',
    }, ('amount',)),

    # discount_value was dollars for the *_dollar types and a percent for the
    # rest: split in two.
    'pos_coupons': ('''
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        code TEXT UNIQUE NOT NULL,
        coupon_type TEXT NOT NULL,
        discount_type TEXT NOT NULL,
        discount_cents INTEGER,
        discount_percent REAL,
        buy_quantity INTEGER DEFAULT 1,
        get_quantity INTEGER DEFAULT 1,
        reward_item_id INTEGER REFERENCES inventory_items(id) ON DELETE SET NULL,
        min_purchase_cents INTEGER,
        max_uses INTEGER,
        current_uses INTEGER DEFAULT 0,
        start_date TEXT,
        end_date TEXT,
        cannot_combine BOOLEAN DEFAULT 0,
        active BOOLEAN DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        created_by INTEGER REFERENCES users(id) ON DELETE SET NULL
    ''', {
        'discount_cents': ("CASE WHEN discount_type IN ('order_dollar', 'item_dollar') "
                           "THEN COALESCE(rscp_cents(discount_value), 0) END"),
        'discount_percent': ("CASE WHEN discount_type NOT IN ('order_dollar', 'item_dollar') "
                             "THEN CAST(discount_value AS REAL) END"),
        'min_purchase_cents': 'rscp_cents(min_purchase)',
    }, ('discount_value', 'min_purchase')),

    'pos_coupon_redemptions': ('''
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        coupon_id INTEGER REFERENCES pos_coupons(id) ON DELETE RESTRICT,
        order_id INTEGER REFERENCES pos_orders(id) ON DELETE RESTRICT,
        serial_used TEXT,
        discount_applied_cents INTEGER,
        redeemed_at TEXT DEFAULT CURRENT_TIMESTAMP,
        redeemed_by INTEGER REFERENCES users(id) ON DELETE RESTRICT
    ''', {
        'discount_applied_cents': 'rscp_cents(discount_applied)',
    }, ('discount_applied',)),

    'pos_payments': ('''
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER REFERENCES pos_orders(id) ON DELETE RESTRICT,
        method TEXT NOT NULL,
        amount_cents INTEGER NOT NULL,
        tendered_cents INTEGER,
        change_given_cents INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    ''', {
        'amount_cents': 'COALESCE(rscp_cents(amount), 0)',
        'tendered_cents': 'rscp_cents(tendered)',
        'change_given_cents': 'COALESCE(rscp_cents(change_given), 0)',
    }, ('amount', 'tendered', 'change_given')),

    # register_id stays a plain TEXT tag: registers are never deleted and most
    # sessions have none.
    'drawer_sessions': ('''
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        register_id TEXT,
        opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        opened_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
        opening_float_cents INTEGER NOT NULL DEFAULT 0,
        closed_at TIMESTAMP,
        closed_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
        counted_cash_cents INTEGER,
        expected_cash_cents INTEGER,
        over_short_cents INTEGER,
        notes TEXT
    ''', {
        'opening_float_cents': 'COALESCE(rscp_cents(opening_float), 0)',
        'counted_cash_cents': 'rscp_cents(counted_cash)',
        'expected_cash_cents': 'rscp_cents(expected_cash)',
        'over_short_cents': 'rscp_cents(over_short)',
    }, ('opening_float', 'counted_cash', 'expected_cash', 'over_short')),
}


def _m15_money_to_cents(conn):
    """Store every money amount as INTEGER cents in *_cents columns.

    Every money column (item prices, orders, order lines, refunds, coupons,
    redemptions, the payments ledger, drawer sessions) is rebuilt as an
    INTEGER *_cents column: cents = Decimal(str(value)) rounded half-up to the
    cent, never int(x * 100). Percent values (tax rate, percent discounts) are
    not money and keep their REAL columns. Mixed columns are split: an order
    line's discount_amount (a percent or dollars, by discount_type) becomes
    discount_percent / discount_cents, a coupon's discount_value becomes
    discount_cents (*_dollar types) / discount_percent.

    JSON holding dollars is converted too: carts on registers and held
    orders, and the money keys of pos_orders.payment_details (tendered ->
    tendered_cents, ...). Settings: POS_MAX_DISCOUNT_AMOUNT -> POS_MAX_DISCOUNT_CENTS,
    CASH_DISCOUNT_AMOUNT -> CASH_DISCOUNT_PERCENT or CASH_DISCOUNT_CENTS
    (by CASH_DISCOUNT_TYPE).

    The stored order total stays the truth: an order whose converted parts do
    not add up to its converted total is logged (never changed). Values that
    are not numbers are logged and stored as NULL (0 where the column is
    NOT NULL).
    """
    import json as _json
    bad = []

    def rscp_cents(value):
        cents = _m15_cents(value)
        if cents is None and value is not None and str(value).strip() != '':
            bad.append(value)
        return cents

    conn.create_function('rscp_cents', 1, rscp_cents, deterministic=True)

    for table, (columns_sql, select_map, drop) in _M15_TABLES.items():
        if _rebuild_table(conn, table, columns_sql, select_map, drop):
            logger.info(f"[Schema v15] {table}: money columns now integer cents")
    if bad:
        logger.warning(f"[Schema v15] {len(bad)} stored money value(s) were not numbers and became NULL/0: "
                       f"{bad[:20]!r}")

    # Carts held on registers and parked as held orders
    carts = 0
    for table, key, col in (('pos_registers', 'register_id', 'cart_json'),
                            ('pos_held_orders', 'id', 'cart_data')):
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone():
            continue
        for row_key, raw in conn.execute(f'SELECT {key}, {col} FROM {table}').fetchall():
            try:
                cart = _json.loads(raw) if raw else None
            except (TypeError, ValueError):
                logger.warning(f"[Schema v15] {table} {row_key}: cart is not JSON; left unchanged")
                continue
            if _m15_cart(cart):
                conn.execute(f'UPDATE {table} SET {col} = ? WHERE {key} = ?', (_json.dumps(cart), row_key))
                carts += 1
    logger.info(f"[Schema v15] converted {carts} saved cart(s) to cents")

    # payment_details JSON on orders
    details_converted = 0
    for oid, raw in conn.execute("SELECT id, payment_details FROM pos_orders "
                                 "WHERE payment_details IS NOT NULL AND payment_details != ''").fetchall():
        try:
            details = _json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(details, dict):
            continue
        changed = False
        for k in _M15_DETAIL_KEYS:
            if k in details:
                details[f'{k}_cents'] = _m15_cents(details.pop(k)) or 0
                changed = True
        if 'cards' in details:
            cards = details.pop('cards')
            details['cards_cents'] = [_m15_cents(c) or 0 for c in (cards if isinstance(cards, list) else [])]
            changed = True
        if changed:
            conn.execute('UPDATE pos_orders SET payment_details = ? WHERE id = ?', (_json.dumps(details), oid))
            details_converted += 1
    logger.info(f"[Schema v15] converted payment details of {details_converted} order(s) to cents")

    _m15_settings(conn)
    _m15_check_orders(conn)


def _m15_settings(conn):
    import json as _json
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'settings'").fetchone():
        return
    stored = {k: v for k, v in conn.execute(
        "SELECT key, value FROM settings WHERE key IN "
        "('POS_MAX_DISCOUNT_AMOUNT', 'CASH_DISCOUNT_AMOUNT', 'CASH_DISCOUNT_TYPE')").fetchall()}

    def decoded(key):
        try:
            return _json.loads(stored[key])
        except (TypeError, ValueError):
            return stored[key]

    def put(key, value):
        conn.execute("INSERT OR REPLACE INTO settings (key, value, updated_at, updated_by) "
                     "VALUES (?, ?, CURRENT_TIMESTAMP, 'migration')", (key, _json.dumps(value)))

    changed = False
    if 'POS_MAX_DISCOUNT_AMOUNT' in stored:
        cents = _m15_cents(decoded('POS_MAX_DISCOUNT_AMOUNT'))
        if cents is not None:
            put('POS_MAX_DISCOUNT_CENTS', cents)
        conn.execute("DELETE FROM settings WHERE key = 'POS_MAX_DISCOUNT_AMOUNT'")
        changed = True
    if 'CASH_DISCOUNT_AMOUNT' in stored:
        amount = decoded('CASH_DISCOUNT_AMOUNT')
        dtype = decoded('CASH_DISCOUNT_TYPE') if 'CASH_DISCOUNT_TYPE' in stored else 'percent'
        if dtype == 'fixed':
            cents = _m15_cents(amount)
            if cents is not None:
                put('CASH_DISCOUNT_CENTS', cents)
        else:
            try:
                put('CASH_DISCOUNT_PERCENT', float(amount))
            except (TypeError, ValueError):
                pass
        conn.execute("DELETE FROM settings WHERE key = 'CASH_DISCOUNT_AMOUNT'")
        changed = True
    if changed:
        logger.info("[Schema v15] money settings converted to cents")
        conn.execute("UPDATE settings_meta SET token = lower(hex(randomblob(16))) WHERE id = 1")


def _m15_check_orders(conn):
    """Log (never fix) orders whose converted parts do not add up: lines vs
    subtotal, discounts + tax vs total, payments vs total. Sales edited after
    the fact legitimately differ (their coupon/cash discount scale down)."""
    rows = conn.execute('''
        SELECT o.id, o.order_number, o.subtotal_cents, o.discount_amount_cents, o.tax_amount_cents,
               o.total_cents, o.cash_discount_cents,
               (SELECT COALESCE(SUM(line_total_cents), 0) FROM pos_order_items WHERE order_id = o.id) AS lines,
               (SELECT COUNT(*) FROM pos_order_items WHERE order_id = o.id) AS line_count,
               (SELECT COALESCE(SUM(discount_applied_cents), 0) FROM pos_coupon_redemptions
                 WHERE order_id = o.id) AS coupon,
               (SELECT SUM(amount_cents) FROM pos_payments WHERE order_id = o.id) AS paid
        FROM pos_orders o
    ''').fetchall()
    mismatched = []
    for (oid, number, subtotal, discount, tax, total, cash_discount, lines, line_count, coupon, paid) in rows:
        problems = []
        if line_count and lines != subtotal:
            problems.append(f'lines {lines} != subtotal {subtotal}')
        expected = max(0, subtotal - (discount or 0) - (coupon or 0)) + tax - (cash_discount or 0)
        if expected != total:
            problems.append(f'subtotal - discounts + tax = {expected} != total {total}')
        if paid is not None and paid != total:
            problems.append(f'payments {paid} != total {total}')
        if problems:
            mismatched.append(f"{number}: {'; '.join(problems)}")
    if mismatched:
        logger.warning(f"[Schema v15] {len(mismatched)} order(s) whose converted parts do not add up "
                       f"to the stored total (the stored total is kept): " + ' | '.join(mismatched[:50]))
    else:
        logger.info(f"[Schema v15] all {len(rows)} order(s) add up to their stored totals")


# (table, column, parent table, parent column, action on orphans)
#   'null'   keep the row, clear the dangling reference (sales, payments,
#            refunds, drawer, audit and other records worth keeping)
#   'delete' remove the row (non-financial child rows with no meaning left)
_M16_ORPHANS = (
    ('pos_orders', 'operator_id', 'users', 'id', 'null'),
    ('pos_order_items', 'order_id', 'pos_orders', 'id', 'null'),
    ('pos_order_items', 'inventory_item_id', 'inventory_items', 'id', 'null'),
    ('pos_refunds', 'order_id', 'pos_orders', 'id', 'null'),
    ('pos_refunds', 'manager_id', 'users', 'id', 'null'),
    ('pos_refund_items', 'refund_id', 'pos_refunds', 'id', 'null'),
    ('pos_refund_items', 'order_item_id', 'pos_order_items', 'id', 'null'),
    ('pos_payments', 'order_id', 'pos_orders', 'id', 'null'),
    ('pos_coupon_redemptions', 'coupon_id', 'pos_coupons', 'id', 'null'),
    ('pos_coupon_redemptions', 'order_id', 'pos_orders', 'id', 'null'),
    ('pos_coupon_redemptions', 'redeemed_by', 'users', 'id', 'null'),
    ('drawer_sessions', 'opened_by', 'users', 'id', 'null'),
    ('drawer_sessions', 'closed_by', 'users', 'id', 'null'),
    ('pos_coupons', 'reward_item_id', 'inventory_items', 'id', 'null'),
    ('pos_coupons', 'created_by', 'users', 'id', 'null'),
    ('pos_audit_log', 'user_id', 'users', 'id', 'null'),
    ('inventory_transactions', 'inventory_item_id', 'inventory_items', 'id', 'null'),
    ('audit_records', 'session_id', 'audit_sessions', 'id', 'null'),
    ('audit_records', 'item_id', 'inventory_items', 'id', 'null'),
    ('history', 'package_id', 'packages', 'id', 'null'),
    ('history', 'user_id', 'users', 'id', 'null'),
    ('pos_held_orders', 'operator_id', 'users', 'id', 'null'),
    ('pos_peripherals', 'paired_by', 'users', 'id', 'null'),
    ('time_entries', 'edited_by', 'users', 'id', 'null'),
    ('federation_transfers', 'peer_id', 'federation_peers', 'id', 'null'),
    ('time_entries', 'user_id', 'users', 'id', 'delete'),
    ('scheduled_shifts', 'user_id', 'users', 'id', 'delete'),
    ('recurring_shift_rules', 'user_id', 'users', 'id', 'delete'),
    ('user_pto_balances', 'user_id', 'users', 'id', 'delete'),
    ('pto_requests', 'user_id', 'users', 'id', 'delete'),
    ('pos_peripherals', 'register_id', 'pos_registers', 'register_id', 'delete'),
    ('pos_coupon_items', 'coupon_id', 'pos_coupons', 'id', 'delete'),
    ('pos_coupon_items', 'item_id', 'inventory_items', 'id', 'delete'),
    ('notifications', 'user_id', 'users', 'id', 'delete'),
)

# Tables rebuilt by migration 16 for their REFERENCES / ON DELETE rules (the
# money tables got theirs in migration 15). Child columns of 'null' orphan
# rules are nullable.
_M16_TABLES = {
    # an item's stock ledger goes with the item (inventory.delete_item already
    # deleted it explicitly)
    'inventory_transactions': ('''
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        inventory_item_id INTEGER REFERENCES inventory_items(id) ON DELETE CASCADE,
        quantity_change INTEGER NOT NULL,
        reason TEXT DEFAULT 'Sold/Consumed',
        user_id TEXT,
        source_tracking TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    ''', {}, ()),
    'audit_records': ('''
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER REFERENCES audit_sessions(id) ON DELETE CASCADE,
        item_id INTEGER REFERENCES inventory_items(id) ON DELETE SET NULL,
        sku TEXT,
        name TEXT,
        expected_qty INTEGER,
        counted_qty INTEGER,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    ''', {}, ()),
    'history': ('''
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        package_id INTEGER REFERENCES packages(id) ON DELETE SET NULL,
        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        action TEXT NOT NULL,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        details TEXT
    ''', {}, ()),
    'pos_held_orders': ('''
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        operator_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        cart_data TEXT NOT NULL,
        note TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    ''', {}, ()),
    'pos_audit_log': ('''
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        action TEXT NOT NULL,
        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        user_name TEXT,
        target_type TEXT,
        target_id TEXT,
        details TEXT,
        ip_address TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    ''', {}, ()),
    'pos_peripherals': ('''
        peripheral_id TEXT PRIMARY KEY,
        register_id TEXT NOT NULL REFERENCES pos_registers(register_id) ON DELETE CASCADE,
        role TEXT NOT NULL CHECK (role IN ('display', 'scanner')),
        token_hash TEXT UNIQUE NOT NULL,
        pending_token TEXT,
        friendly_name TEXT,
        paired_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        paired_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_seen TIMESTAMP
    ''', {}, ()),
    'pos_coupon_items': ('''
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        coupon_id INTEGER NOT NULL REFERENCES pos_coupons(id) ON DELETE CASCADE,
        item_id INTEGER NOT NULL REFERENCES inventory_items(id) ON DELETE CASCADE
    ''', {}, ()),
    # Timeclock rows belong to their user: deleting a user deletes them
    # (orphans of deleted users are removed above).
    'time_entries': ('''
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        type TEXT DEFAULT 'shift',
        clock_in DATETIME NOT NULL,
        clock_out DATETIME,
        notes TEXT,
        edited_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    ''', {}, ()),
    'scheduled_shifts': ('''
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        start_time DATETIME NOT NULL,
        end_time DATETIME NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    ''', {}, ()),
    'recurring_shift_rules': ('''
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        day_of_week INTEGER NOT NULL,
        start_time TEXT NOT NULL,
        end_time TEXT NOT NULL,
        frequency TEXT DEFAULT 'weekly',
        reference_date DATE
    ''', {}, ()),
    'user_pto_balances': ('''
        user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        balance_hours REAL DEFAULT 0.0
    ''', {}, ()),
    'pto_requests': ('''
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        request_type TEXT NOT NULL,
        date DATE NOT NULL,
        start_time TIME,
        end_time TIME,
        hours_requested REAL NOT NULL,
        status TEXT DEFAULT 'pending',
        admin_notes TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    ''', {}, ()),
    'notifications': ('''
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        title TEXT NOT NULL,
        message TEXT,
        type TEXT DEFAULT 'info',
        link TEXT,
        is_read BOOLEAN DEFAULT 0,
        read_at TIMESTAMP
    ''', {}, ()),
    'federation_transfers': ('''
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        direction TEXT NOT NULL,
        peer_id INTEGER REFERENCES federation_peers(id) ON DELETE RESTRICT,
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
        notes TEXT
    ''', {}, ()),
}


def _m16_table_exists(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (name,)).fetchone() is not None


def _m16_foreign_keys(conn):
    """Make the database's foreign keys enforceable (every connection now
    runs PRAGMA foreign_keys = ON).

    Orphans are dealt with first (counts logged per table/column): sales,
    payments, refunds, drawer, audit and other records worth keeping keep
    their row and lose only the dangling reference (denormalised sku/name/
    user_name text stays); non-financial child rows (timeclock shifts, PTO and
    balances of deleted users, pairings of deleted registers, coupon item
    links, notifications) are deleted. Tables whose REFERENCES or ON DELETE
    rules changed are rebuilt. Ends with PRAGMA foreign_key_check: anything
    left aborts the migration (the transaction rolls back).
    """
    for table, (columns_sql, select_map, drop) in _M16_TABLES.items():
        if _m16_table_exists(conn, table):
            _rebuild_table(conn, table, columns_sql, select_map, drop, tag='v16')

    total = 0
    for table, col, parent, pcol, action in _M16_ORPHANS:
        if not _m16_table_exists(conn, table):
            continue
        if col not in [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]:
            continue
        if _m16_table_exists(conn, parent):
            where = (f'"{col}" IS NOT NULL AND "{col}" NOT IN '
                     f'(SELECT "{pcol}" FROM "{parent}" WHERE "{pcol}" IS NOT NULL)')
        else:
            where = f'"{col}" IS NOT NULL'
        n = conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE {where}').fetchone()[0]
        if not n:
            continue
        if action == 'delete':
            conn.execute(f'DELETE FROM "{table}" WHERE {where}')
            logger.warning(f"[Schema v16] {table}: deleted {n} row(s) whose {col} pointed at a missing {parent}")
        else:
            conn.execute(f'UPDATE "{table}" SET "{col}" = NULL WHERE {where}')
            logger.warning(f"[Schema v16] {table}: cleared {col} on {n} row(s) pointing at a missing {parent} "
                           f"(rows kept)")
        total += n
    logger.info(f"[Schema v16] {total} orphaned reference(s) handled")

    problems = conn.execute('PRAGMA foreign_key_check').fetchall()
    if problems:
        sample = ', '.join(f'{r[0]} rowid {r[1]} -> {r[2]}' for r in problems[:20])
        raise RuntimeError(f"[Schema v16] {len(problems)} foreign key violation(s) remain: {sample}")
    logger.info("[Schema v16] PRAGMA foreign_key_check: clean")


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



# Also used by app/services/monitor.py (CREATE ... IF NOT EXISTS), for the
# same reason as JOB_TABLES_SQL.
MONITOR_TABLES_SQL = (
    '''CREATE TABLE IF NOT EXISTS monitor_alerts (
        key TEXT PRIMARY KEY,         -- e.g. job_failed:backup, display_offline:<peripheral id>
        kind TEXT NOT NULL,           -- job_failed | job_stale | display_offline
        subject TEXT NOT NULL,        -- job name or peripheral id
        title TEXT,                   -- what the alert email called it
        since TEXT,                   -- when the problem started, as the email showed it
        last_sent TEXT,               -- last alert email about it (UTC ISO)
        detail TEXT
    )''',
)


def _m14_monitor_alerts(conn):
    """Open alerts of the monitor job (app/services/monitor.py): one row per
    problem that has been emailed and not yet reported as recovered, so each
    problem is mailed once (plus a recovery mail), not on every check."""
    for sql in MONITOR_TABLES_SQL:
        conn.execute(sql)


# Notice for every administrator when migration 22 retires a configured SSO.
SSO_RETIRED_TITLE = 'Google sign-in (SSO) was removed: move staff to Cloudflare Access'
SSO_RETIRED_MESSAGE = (
    "RSCP 3.0 no longer has its own \"Sign in with Google\"; its SSO settings were deleted. "
    "Staff who used it can sign in with a password (Admin > Settings > User Management > Edit) "
    "or through Cloudflare Access: turn on Admin > Settings > Cloudflare Access Sign-in and set each "
    "person's sign-in email under User Management > Edit (emails that SSO used are already there). "
    "With Google as the Access login method, staff keep signing in with Google.")
SSO_RETIRED_LINK = '/admin/?tab=settings#cf-access-card'


def _m22_retire_oidc_sso(conn):
    """RSCP's own Google/OIDC sign-in was removed: delete its SSO_* settings
    (including the client secret).

    Cloudflare Access used SSO_ALLOWED_DOMAINS when its own domain list was
    blank; that restriction is kept by copying it into
    CF_ACCESS_ALLOWED_DOMAINS. If SSO was configured, every administrator
    gets a one-time notification on how staff sign in now. users.email stays
    (Cloudflare Access matches on it); users.auth_provider is left unused.
    Self-contained on purpose: the SSO_* keys are no longer in the settings
    registry.
    """
    import json
    has_settings = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'settings'").fetchone()
    if not has_settings:
        return
    rows = dict(conn.execute("SELECT key, value FROM settings WHERE key LIKE 'SSO!_%' ESCAPE '!'").fetchall())
    if not rows:
        return

    def value(key):
        raw = rows.get(key)
        try:
            return json.loads(raw) if raw is not None else None
        except (TypeError, ValueError):
            return raw

    def is_true(v):
        if isinstance(v, str):
            return v.strip().lower() in ('1', 'true', 'on', 'yes', 'y')
        return bool(v)

    def domain_list(v):
        if isinstance(v, str):
            v = v.split(',')
        if not isinstance(v, list):
            return []
        out = []
        for d in v:
            d = str(d).strip().lower()
            if d:
                d = d if d.startswith('@') else '@' + d
                if d not in out:
                    out.append(d)
        return out

    configured = is_true(value('SSO_ENABLED')) or bool(str(value('SSO_CLIENT_ID') or '').strip())

    sso_domains = domain_list(value('SSO_ALLOWED_DOMAINS'))
    if sso_domains:
        row = conn.execute("SELECT value FROM settings WHERE key = 'CF_ACCESS_ALLOWED_DOMAINS'").fetchone()
        try:
            cf_domains = domain_list(json.loads(row[0])) if row else []
        except (TypeError, ValueError):
            cf_domains = domain_list(row[0])
        if not cf_domains:
            conn.execute("INSERT OR REPLACE INTO settings (key, value, updated_at, updated_by) "
                         "VALUES ('CF_ACCESS_ALLOWED_DOMAINS', ?, CURRENT_TIMESTAMP, 'migration 22')",
                         (json.dumps(sso_domains),))

    conn.execute(f"DELETE FROM settings WHERE key IN ({', '.join('?' * len(rows))})", tuple(rows))
    # Tell every worker's settings cache to reload.
    conn.execute("UPDATE settings_meta SET token = lower(hex(randomblob(16))) WHERE id = 1")

    has_notifications = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'notifications'").fetchone()
    if not configured or not has_notifications:
        return
    for uid, is_admin, roles in conn.execute('SELECT id, is_admin, roles FROM users').fetchall():
        try:
            role_list = json.loads(roles) if roles else []
        except (TypeError, ValueError):
            role_list = []
        if is_true(is_admin) or (isinstance(role_list, list) and 'super_admin' in role_list):
            conn.execute("INSERT INTO notifications (user_id, title, message, type, link) "
                         "VALUES (?, ?, ?, 'warning', ?)",
                         (uid, SSO_RETIRED_TITLE, SSO_RETIRED_MESSAGE, SSO_RETIRED_LINK))


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
    (14, 'monitor_alerts table (email alerts)', _m14_monitor_alerts),
    (15, 'money as integer cents', _m15_money_to_cents),
    (16, 'foreign keys: orphans handled, REFERENCES rebuilt, foreign_key_check', _m16_foreign_keys),
    (22, 'retire Google/OIDC sign-in settings (feature removed)', _m22_retire_oidc_sso),
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


def run_migrations(db_path=None, target=None):
    """Bring the database fully up to date (or only up to schema version
    `target`, for tests that need an older schema). Raises on failure.

    Safe to call from every worker at startup: cheap (one PRAGMA read) when
    the schema is already current.
    """
    latest = LATEST_SCHEMA_VERSION if target is None else int(target)
    if db_path is None:
        db_path = DB_PATH
        try:  # honour a test/app-configured DATABASE, like get_db_connection()
            from flask import current_app
            db_path = current_app.config.get('DATABASE', DB_PATH)
        except RuntimeError:
            pass
    if db_path != ':memory:' and _user_version(db_path) >= latest:
        return

    with _MigrationLock(db_path):
        version = _user_version(db_path)
        if version >= latest:
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
        # Table rebuilds (v7, v10, v15, v16) need foreign keys OFF: the
        # "12-step" ALTER procedure. It can only be changed outside a
        # transaction, so it is set once for this connection; migration 16
        # ends with PRAGMA foreign_key_check. Every other connection RSCP
        # opens enforces foreign keys.
        conn.execute('PRAGMA foreign_keys = OFF')
        try:
            # Always apply in version order, whatever order the list was edited in
            for step, description, fn in sorted(SCHEMA_MIGRATIONS, key=lambda m: m[0]):
                if step <= version or step > latest:
                    continue
                logger.info(f"[Schema] Migrating to v{step}: {description}")
                conn.execute('BEGIN IMMEDIATE')
                try:
                    fn(conn)
                    conn.execute(f'PRAGMA user_version = {int(step)}')
                    conn.execute('COMMIT')
                except Exception:
                    conn.execute('ROLLBACK')
                    logger.exception(f"[Schema] Migration to v{step} ({description}) FAILED")
                    raise
                version = step
        finally:
            conn.close()
        logger.info(f"[Schema] Database is at schema v{version}")
