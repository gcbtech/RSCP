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

def ensure_db_ready():
    """Initializes the database and applies any pending schema updates."""
    init_db()
    
    conn = get_db_connection()
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
