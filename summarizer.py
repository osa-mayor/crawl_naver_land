import pandas as pd
import glob
import os
from datetime import datetime

# ============================
# Configuration
# ============================
INPUT_PATTERN = "results/naver_land_result_*.xlsx"

def get_latest_file():
    list_of_files = glob.glob(INPUT_PATTERN)
    if not list_of_files:
        return None
    return max(list_of_files, key=os.path.getctime)

def clean_price(x):
    """Parse '172,000' or '-' back to integer"""
    if pd.isna(x) or str(x).strip() == '-':
        return 0
    # Remove commas
    clean_str = str(x).replace(',', '')
    try:
        return int(float(clean_str))
    except ValueError:
        return 0

def clean_area(x):
    try:
        # Handle "109 (33)" format or raw float
        x_str = str(x).split('(')[0].strip()
        return float(x_str)
    except:
        return 0.0

def main():
    latest_file = get_latest_file()
    if not latest_file:
        print("❌ 분석할 엑셀 파일을 찾을 수 없습니다.")
        return

    print(f"📂 최신 파일 로드 중: {latest_file}")
    
    try:
        df = pd.read_excel(latest_file, sheet_name='상세내역')
    except Exception as e:
        print(f"❌ 파일 읽기 실패: {e}")
        return

    if df.empty:
        print("⚠️ 데이터가 비어있습니다.")
        return

    print("📊 데이터 분석 및 요약 중...")

    # --- Preprocess (Unformat for Calculation) ---
    # Prices are already in Man-won units (string with commas), parse them back to numeric
    price_cols = [
        "매매 최저가 (일반)", "매매 최저가 (저층/탑층)", "매매 최고가", "매매 평균가", 
        "전세 최저가", "전세 최고가", "전세 평균가"
    ]
    
    for c in price_cols:
        if c in df.columns:
            df[c] = df[c].apply(clean_price)
            
    # Count columns (parse just in case, though usually int)
    count_cols = ["매매 매물수 (전체)", "전세 매물수", "총세대수"]
    for c in count_cols:
         if c in df.columns:
             # handle '7' or numeric
             df[c] = pd.to_numeric(df[c], errors='coerce').fillna(0)

    # Areas
    df["공급면적"] = df["공급면적"].apply(clean_area)
    df["전용면적"] = df["전용면적"].apply(clean_area)

    # --- Aggregation Logic (Copied from crawler.py & Refined) ---
    
    # [Fix] Convert 0 prices to NaN so 'min' ignores them (instead of selecting 0)
    for c in price_cols:
        if c in df.columns:
             df[c] = df[c].replace(0, pd.NA) 
    
    # Grouping Key: Exclusive Space / 2
    if "전용면적" in df.columns:
            df["PyeongGroup"] = df["전용면적"].agg(lambda x: int(x / 2))
    else:
            df["PyeongGroup"] = df["공급면적"].agg(lambda x: int(x / 2))

    agg_rules = {
        "총세대수": "first",
        "연식": "first",
        "매매 최저가 (일반)": "min",
        "전세 최저가": "min",
        "매매 매물수 (전체)": "sum",
        "전세 매물수": "sum",
        "공급면적": "mean",
        "전용면적": "mean",
        "링크": "first"
    }

    group_keys = ["시/도", "시/군/구", "읍/면/동", "아파트명", "PyeongGroup"]
    
    # Filter only keys that exist
    group_keys = [k for k in group_keys if k in df.columns]

    df_summary = df.groupby(group_keys).agg(agg_rules).reset_index()

    # --- Derived Metrics ---
    # Prices are in Man-won units.
    
    # Gap = Trade Min - Jeonse Min
    def calc_gap(row):
        t = row["매매 최저가 (일반)"]
        j = row["전세 최저가"]
        # Handle NaN/None
        if pd.notna(t) and pd.notna(j) and t > 0 and j > 0:
            return t - j
        return 0
        
    # Ratio = Jeonse Min / Trade Min * 100
    def calc_ratio(row):
            t = row["매매 최저가 (일반)"]
            j = row["전세 최저가"]
            # Handle NaN/None
            if pd.notna(t) and pd.notna(j) and t > 0 and j > 0:
                return (j / t) * 100
            return 0

    df_summary["갭"] = df_summary.apply(calc_gap, axis=1)
    df_summary["전세가율(최저)"] = df_summary.apply(calc_ratio, axis=1)

    # Metadata
    df_summary["수집일"] = datetime.now().strftime("%Y-%m-%d")
    
    # Format Pyeong
    df_summary["공급평형"] = df_summary["공급면적"].apply(lambda x: f"{x:.2f}")
    df_summary["전용평형"] = df_summary["전용면적"].apply(lambda x: f"{x:.2f}")
    
    # Link Formula
    if "링크" in df_summary.columns:
        df_summary["링크"] = df_summary["링크"].apply(lambda x: f'=HYPERLINK("{x}", "이동")')

    # --- Formatting for Output ---
    def format_val(x, is_ratio=False):
        if pd.isna(x) or x == 0 or x == 0.0:
            return ""
        if is_ratio:
            return f"{x:.1f}%"
        try:
            return f"{int(x):,}"
        except:
            return ""

    # Apply formatting
    p_cols_out = [
        "매매 최저가 (일반)", "전세 최저가", "갭"
    ]
    
    for c in p_cols_out:
        if c in df_summary.columns:
            df_summary[c] = df_summary[c].apply(lambda x: format_val(x))
            
    r_cols_out = ["전세가율(최저)"]
    for c in r_cols_out:
        if c in df_summary.columns:
            df_summary[c] = df_summary[c].apply(lambda x: format_val(x, is_ratio=True))

    # --- Column Selection & Renaming ---
    rename_map = {
        "매매 매물수 (전체)": "매매 매물수"
    }
    df_summary.rename(columns=rename_map, inplace=True)

    final_cols = [
        "시/도", "시/군/구", "읍/면/동", "아파트명", "총세대수", 
        "공급평형", "전용평형", "연식", 
        "매매 최저가 (일반)", "전세 최저가", 
        "매매 매물수", "전세 매물수", 
        "갭", "전세가율(최저)", 
        "링크", "수집일"
    ]
    
    # Ensure cols exist
    final_cols = [c for c in final_cols if c in df_summary.columns]
    df_summary = df_summary[final_cols]

    # Save
    summary_filename = f"summary_{os.path.basename(latest_file)}"
    
    print(f"💾 요약 파일 저장 중: {summary_filename}")
    with pd.ExcelWriter(summary_filename, engine='openpyxl') as writer:
        df_summary.to_excel(writer, sheet_name='평형별_요약', index=False)
        
    print("✅ 완료!")

if __name__ == "__main__":
    main()
