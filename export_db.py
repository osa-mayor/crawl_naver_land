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
        cur = conn.cursor()
        
        # Check for tables
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='prices';")
        has_prices = cur.fetchone()
        
        if has_prices:
            print("ℹ️ Detected Normalized Schema (prices + complexes)")
            query = """
            SELECT
                c.region_depth1 AS "시/도",
                c.region_depth2 AS "시/군/구",
                c.region_depth3 AS "읍/면/동",
                c.name AS "아파트명",
                c.completion_date AS "준공일",
                c.total_households AS "총세대수",
                p.pyeong_type AS "타입",
                p.supply_area AS "공급면적",
                p.exclusive_area AS "전용면적",
                p.hallway_type AS "현관구조",
                p.room_bath AS "방/욕실",
                NULLIF(p.trade_min_std, 0) AS "매매 최저가 (일반)",
                NULLIF(p.trade_min_low, 0) AS "매매 최저가 (저층)",
                NULLIF(p.trade_max, 0) AS "매매 최고가",
                NULLIF(p.trade_avg, 0) AS "매매 평균가",
                NULLIF(p.trade_count, 0) AS "매매 매물수 (전체)",
                NULLIF(p.rent_min, 0) AS "전세 최저가",
                NULLIF(p.rent_max, 0) AS "전세 최고가",
                NULLIF(p.rent_avg, 0) AS "전세 평균가",
                NULLIF(p.rent_count, 0) AS "전세 매물수",
                NULLIF(p.gap, 0) AS "갭",
                NULLIF(p.jeonse_ratio, 0) || '%' AS "전세가율",
                c.total_dongs AS "총동수",
                c.construction_company AS "건설사",
                c.heating_method AS "난방방식",
                c.heating_fuel AS "난방연료",
                c.parking_per_household AS "세대당주차대수",
                c.far AS "용적률",
                c.bcr AS "건폐율",
                c.latitude AS "위도",
                c.longitude AS "경도",
                p.date AS "수집일",
                c.complex_no AS "complex_id"
            FROM prices p
            JOIN complexes c ON p.complex_no = c.complex_no
            ORDER BY p.date DESC, c.region_depth1, c.region_depth2, c.region_depth3, c.name
            """
        else:
            print("ℹ️ Detected Legacy Schema (real_estate)")
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
