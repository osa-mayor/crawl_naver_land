import sqlite3
import os
import glob
import sys

def merge_databases(master_db_path, partial_db_pattern):
    """
    Merges partial SQLite databases into the master database.
    """
    print(f"🔄 Merging databases matching '{partial_db_pattern}' into '{master_db_path}'...")
    
    # Connect to Master DB
    conn = sqlite3.connect(master_db_path)
    cursor = conn.cursor()
    
    # Ensure Master Table Exists (Create if not exists)
    # We trust the crawler to create it, but good to be safe.
    # Actually, we can just attach and copy.
    
    partial_dbs = glob.glob(partial_db_pattern)
    if not partial_dbs:
        print("⚠️ No partial databases found to merge.")
        return

    for db_file in partial_dbs:
        if os.path.abspath(db_file) == os.path.abspath(master_db_path):
            continue
            
        print(f"   ➕ Merging '{db_file}'...")
        try:
            # Attach partial DB
            cursor.execute(f"ATTACH DATABASE '{db_file}' AS partial")
            
            # Copy Data (Assuming table name is 'real_estate')
            # Using INSERT OR IGNORE to avoid duplicates if any (though regions should be distinct)
            cursor.execute("INSERT OR IGNORE INTO main.real_estate SELECT * FROM partial.real_estate")
            
            conn.commit()
            cursor.execute("DETACH DATABASE partial")
            print(f"      ✅ Merged {db_file}")
            
        except Exception as e:
            print(f"      ❌ Failed to merge {db_file}: {e}")

    conn.close()
    print("🎉 Merge complete.")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python merge_db.py <partial_db_pattern> [master_db_path]")
        sys.exit(1)
        
    pattern = sys.argv[1]
    master = sys.argv[2] if len(sys.argv) > 2 else "real_estate.db"
    
    merge_databases(master, pattern)
