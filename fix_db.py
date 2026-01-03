from app import create_app, get_db_connection

app = create_app()

with app.app_context():
    conn = get_db_connection()
    try:
        print("Creating scheduled_shifts table...")
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
        print("Done.")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        conn.close()
