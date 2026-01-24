import sqlite3
import os
import glob
import sys

def init_master_db(cursor):
    # 1. Complex Info Table (Static Data)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS complexes (
        complex_no INTEGER PRIMARY KEY,
        name TEXT,
        region_depth1 TEXT,
        region_depth2 TEXT,
        region_depth3 TEXT,
        total_households INTEGER,
        total_dongs INTEGER,
        completion_date TEXT,
        construction_company TEXT,
        heating_method TEXT,
        heating_fuel TEXT,
        parking_per_household REAL,
        far REAL,
        bcr REAL,
        latitude REAL,
        longitude REAL,
        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # 2. Daily Price Table (Dynamic Data)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS prices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        complex_no INTEGER,
        date TEXT,
        pyeong_type TEXT,
        supply_area REAL,
        exclusive_area REAL,
        hallway_type TEXT,
        room_bath TEXT,
        trade_min_std INTEGER,
        trade_min_low INTEGER,
        trade_max INTEGER,
        trade_avg INTEGER,
        trade_count INTEGER,
        rent_min INTEGER,
        rent_max INTEGER,
        rent_avg INTEGER,
        rent_count INTEGER,
        gap INTEGER,
        jeonse_ratio REAL,
        FOREIGN KEY (complex_no) REFERENCES complexes (complex_no)
    )
    """)
    # Index for fast lookup
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_prices_complex_date ON prices (complex_no, date)")

def merge_databases(master_db_path, partial_db_pattern):
    """
    Merges partial SQLite databases into the master database.
    """
    print(f"🔄 Merging databases matching '{partial_db_pattern}' into '{master_db_path}'...")
    
    # Connect to Master DB
    conn = sqlite3.connect(master_db_path)
    cursor = conn.cursor()
    
    # Ensure Master Table Exists
    init_master_db(cursor)
    conn.commit()
    
    partial_dbs = glob.glob(partial_db_pattern)
    if not partial_dbs:
        print("⚠️ No partial databases found to merge.")
        return

    complex_count = 0
    price_count = 0

    for db_file in partial_dbs:
        if os.path.abspath(db_file) == os.path.abspath(master_db_path):
            continue
            
        print(f"   ➕ Merging '{db_file}'...")
        try:
            # Attach partial DB
            cursor.execute(f"ATTACH DATABASE '{db_file}' AS partial")
            
            # Merge Complexes (Upsert Logic)
            cursor.execute("INSERT OR REPLACE INTO main.complexes SELECT * FROM partial.complexes")
            complex_count += cursor.rowcount
            
            # Merge Prices (Insert Logic - IDs are auto-increment, so we ignore ID column mapping if needed? 
            # Or we just copy all columns except ID?
            # Actually, the partial DB has IDs starting from 1. 
            # If we just INSERT SELECT *, the IDs might conflict if not AUTOINCREMENT null handling.
            # Best practice: Specify columns excluding ID.
            # But getting column names dynamically avoids hardcoding.
            
            # Get columns for prices
            cursor.execute("PRAGMA table_info(prices)")
            columns = [col[1] for col in cursor.fetchall() if col[1] != 'id']
            cols_str = ", ".join(columns)
            
            cursor.execute(f"INSERT INTO main.prices ({cols_str}) SELECT {cols_str} FROM partial.prices")
            price_count += cursor.rowcount
            
            conn.commit()
            cursor.execute("DETACH DATABASE partial")
            print(f"      ✅ Merged {db_file}")
            
        except Exception as e:
            print(f"      ❌ Failed to merge {db_file}: {e}")

    conn.close()
    print(f"🎉 Merge complete. Complexes: {complex_count}, Prices: {price_count}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python merge_db.py <partial_db_pattern> [master_db_path]")
        sys.exit(1)
        
    pattern = sys.argv[1]
    master = sys.argv[2] if len(sys.argv) > 2 else "real_estate.db"
    
    merge_databases(master, pattern)
