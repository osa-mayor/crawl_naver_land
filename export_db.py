import sqlite3
import pandas as pd
import argparse
import sys
import os
from datetime import datetime

def export_to_excel(db_path, output_path=None):
    if not os.path.exists(db_path):
        print(f"❌ Error: Database file not found at {db_path}")
        return False

    try:
        conn = sqlite3.connect(db_path)
        
        # Determine Table Name (Assuming 'real_estate')
        query = "SELECT * FROM real_estate"
        
        # Read Data
        df = pd.read_sql_query(query, conn)
        conn.close()
        
        if df.empty:
            print(f"⚠️ Warning: Database {db_path} is empty.")
            return False

        # Generate Output Filename if not provided
        if not output_path:
            date_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            db_name = os.path.splitext(os.path.basename(db_path))[0]
            output_path = f"export_{db_name}_{date_str}.xlsx"

        # Export to Excel
        print(f"📊 Exporting {len(df)} rows from '{db_path}' to '{output_path}'...")
        df.to_excel(output_path, index=False, engine='openpyxl')
        print(f"✅ Export Complete: {output_path}")
        return output_path

    except Exception as e:
        print(f"❌ Export Failed: {e}")
        return False

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export SQLite DB to Excel")
    parser.add_argument("db_path", help="Path to the SQLite database file")
    parser.add_argument("--output", "-o", help="Output Excel file path (optional)")

    args = parser.parse_args()
    
    export_to_excel(args.db_path, args.output)
