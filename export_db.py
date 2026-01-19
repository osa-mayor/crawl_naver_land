import sqlite3
import pandas as pd
import os
from datetime import datetime

DB_PATH = "real_estate.db"

def list_columns():
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(real_estate)")
        columns = [info[1] for info in cursor.fetchall()]
        conn.close()
        return columns
    except:
        return []

def export_to_excel(query=None, filename=None):
    if not os.path.exists(DB_PATH):
        print("❌ Database not found. Run crawler_db.py first.")
        return

    conn = sqlite3.connect(DB_PATH)
    
    if not query:
        # Default: Export everything (Warning: Big)
        query = "SELECT * FROM real_estate"
    
    try:
        print(f"📊 Executing Query: {query}")
        df = pd.read_sql_query(query, conn)
        
        if df.empty:
            print("⚠️ No results found.")
            return

        if not filename:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"export_{timestamp}.xlsx"
        
        # Format Optimization (optional)
        # Re-apply area formatting if needed
        if "공급면적" in df.columns:
             df["공급면적(평)"] = df["공급면적"].apply(lambda x: f"{x:.1f} ({x/3.3058:.1f}평)" if pd.notnull(x) else "")

        df.to_excel(filename, index=False)
        print(f"✅ Exported {len(df)} rows to {filename}")
        
    except Exception as e:
        print(f"❌ Export Error: {e}")
    finally:
        conn.close()

def main():
    print("=== 🗄️ Real Estate DB Exporter ===")
    print("1. Export All Data")
    print("2. Export by Region (e.g. '강남구')")
    print("3. Custom SQL Query")
    
    choice = input("Select option (1-3): ").strip()
    
    if choice == "1":
        export_to_excel()
    elif choice == "2":
        region = input("Enter Region Name (e.g. 강남구, 가평군): ").strip()
        query = f"SELECT * FROM real_estate WHERE `시/군/구` LIKE '%{region}%' OR `읍/면/동` LIKE '%{region}%'"
        export_to_excel(query, filename=f"export_{region}.xlsx")
    elif choice == "3":
        cols = list_columns()
        print(f"ℹ️  Available Columns: {cols}")
        query = input("Enter SQL Query: ").strip()
        export_to_excel(query)
    else:
        print("Cancelled.")

if __name__ == "__main__":
    main()
