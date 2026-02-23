import sqlite3
import re

DB_PATH = "2026-01-24_merged.db"

def fix_regions():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    print("🔍 Scanning for rows with empty region_depth1...")
    cur.execute("SELECT complex_no, region_depth3 FROM complexes WHERE region_depth1 = '' OR region_depth1 IS NULL")
    rows = cur.fetchall()
    
    print(f"🧩 Found {len(rows)} rows to fix.")
    
    count = 0
    for complex_no, full_addr in rows:
        if not full_addr: continue
        
        parts = full_addr.split()
        if len(parts) >= 2:
            r1 = parts[0]
            r2 = parts[1]
            r3 = " ".join(parts[2:]) if len(parts) > 2 else full_addr # Fallback if only 2 parts
            
            # Update
            cur.execute("""
            UPDATE complexes 
            SET region_depth1 = ?, region_depth2 = ?, region_depth3 = ?
            WHERE complex_no = ?
            """, (r1, r2, r3, complex_no))
            count += 1
            
            if count % 1000 == 0:
                print(f"✨ Fixed {count} rows...")
    
    conn.commit()
    conn.close()
    print(f"✅ Completed! Fixed {count} rows.")

if __name__ == "__main__":
    fix_regions()
