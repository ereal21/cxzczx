import sqlite3

DB_PATH = "database.db"  # adjust if needed

def column_exists(cur, table, col):
    cur.execute(f"PRAGMA table_info({table})")
    return any(r[1] == col for r in cur.fetchall())

def add_column_if_missing(cur, table, col, ddl):
    if not column_exists(cur, table, col):
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        print(f"✅ Added {table}.{col}")
    else:
        print(f"ℹ️ {table}.{col} already exists")

def main():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # New columns required by ORM
    add_column_if_missing(cur, "used_promo_codes", "city", "city VARCHAR(100) NULL")
    add_column_if_missing(cur, "used_promo_codes", "district", "district VARCHAR(100) NULL")

    # Example of previous migration pattern (keep if relevant)
    try:
        add_column_if_missing(cur, "unfinished_operations", "message_id", "message_id INTEGER")
    except Exception as e:
        print(f"Note: could not check unfinished_operations.message_id: {e}")

    conn.commit()
    conn.close()
    print("✅ Migration complete.")

if __name__ == "__main__":
    main()
