import glob
import os
import sqlite3
import sys


COMPLEX_COLUMNS = (
    "complex_no", "name", "region_depth1", "region_depth2", "region_depth3",
    "total_households", "total_dongs", "completion_date", "construction_company",
    "heating_method", "heating_fuel", "parking_per_household", "far", "bcr",
    "latitude", "longitude", "last_updated",
)

PRICE_COLUMNS = (
    "complex_no", "date", "pyeong_type", "supply_area", "exclusive_area",
    "hallway_type", "room_bath", "trade_min_std", "trade_min_low", "trade_max",
    "trade_avg", "trade_count", "rent_min", "rent_max", "rent_avg", "rent_count",
    "gap", "jeonse_ratio",
)

PRICE_IDENTITY_COLUMNS = (
    "complex_no", "date", "pyeong_type", "supply_area", "exclusive_area",
)

NUMERIC_METADATA_COLUMNS = {
    "total_households", "total_dongs", "parking_per_household", "far", "bcr",
    "latitude", "longitude",
}


def _quoted(column):
    return f'"{column}"'


def _table_columns(cursor, schema, table):
    return {
        row[1]
        for row in cursor.execute(
            f"PRAGMA {_quoted(schema)}.table_info({_quoted(table)})"
        ).fetchall()
    }


def _require_columns(columns, required, table, db_file):
    missing = [column for column in required if column not in columns]
    if missing:
        raise sqlite3.DatabaseError(
            f"{db_file}: {table} is missing required columns: {', '.join(missing)}"
        )


def init_master_db(cursor):
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
    master_price_columns = _table_columns(cursor, "main", "prices")
    _require_columns(
        master_price_columns, PRICE_IDENTITY_COLUMNS, "prices", "master database"
    )
    identity = ", ".join(_quoted(column) for column in PRICE_IDENTITY_COLUMNS)
    duplicate = cursor.execute(
        f"SELECT {identity}, COUNT(*) FROM prices "
        f"GROUP BY {identity} HAVING COUNT(*) > 1 LIMIT 1"
    ).fetchone()
    if duplicate:
        raise RuntimeError(
            "duplicate canonical price snapshots exist; refusing automatic migration"
        )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_prices_complex_date "
        "ON prices (complex_no, date)"
    )
    cursor.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS uq_prices_canonical_snapshot "
        f"ON prices ({identity})"
    )
    null_safe_identity = ", ".join(
        f"ifnull({_quoted(column)}, '__merge_null__')"
        for column in PRICE_IDENTITY_COLUMNS
    )
    cursor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_prices_canonical_identity_nullsafe "
        f"ON prices ({null_safe_identity})"
    )


def _merge_complexes(cursor, db_file):
    master_columns = _table_columns(cursor, "main", "complexes")
    partial_columns = _table_columns(cursor, "partial", "complexes")
    _require_columns(partial_columns, ("complex_no",), "complexes", db_file)
    columns = [
        column for column in COMPLEX_COLUMNS
        if column in master_columns and column in partial_columns
    ]
    column_sql = ", ".join(_quoted(column) for column in columns)

    updates = []
    for column in columns:
        if column == "complex_no":
            continue
        quoted = _quoted(column)
        conditions = [
            f"excluded.{quoted} IS NULL",
            f"(typeof(excluded.{quoted}) = 'text' AND trim(excluded.{quoted}) = '')",
        ]
        if column in NUMERIC_METADATA_COLUMNS:
            conditions.append(
                f"(excluded.{quoted} = 0 AND complexes.{quoted} IS NOT NULL "
                f"AND complexes.{quoted} != 0)"
            )
        updates.append(
            f"{quoted} = CASE WHEN {' OR '.join(conditions)} "
            f"THEN complexes.{quoted} ELSE excluded.{quoted} END"
        )

    conflict_action = (
        "DO UPDATE SET " + ", ".join(updates) if updates else "DO NOTHING"
    )
    cursor.execute(
        f"INSERT INTO main.complexes ({column_sql}) "
        f"SELECT {column_sql} FROM partial.complexes WHERE 1 "
        f"ON CONFLICT(complex_no) {conflict_action}"
    )
    return cursor.rowcount


def _merge_prices(cursor, db_file):
    master_columns = _table_columns(cursor, "main", "prices")
    partial_columns = _table_columns(cursor, "partial", "prices")
    _require_columns(partial_columns, PRICE_IDENTITY_COLUMNS, "prices", db_file)
    columns = [
        column for column in PRICE_COLUMNS
        if column in master_columns and column in partial_columns
    ]
    column_sql = ", ".join(_quoted(column) for column in columns)
    update_columns = [
        column for column in columns if column not in PRICE_IDENTITY_COLUMNS
    ]
    conflict_action = "DO NOTHING"
    if update_columns:
        conflict_action = "DO UPDATE SET " + ", ".join(
            f"{_quoted(column)} = excluded.{_quoted(column)}"
            for column in update_columns
        )

    cursor.execute(
        f"INSERT INTO main.prices ({column_sql}) "
        f"SELECT {column_sql} FROM partial.prices WHERE 1 "
        f"ON CONFLICT {conflict_action}"
    )
    return cursor.rowcount


def merge_databases(master_db_path, partial_db_pattern):
    """Merge matching shard databases atomically, one shard at a time."""
    partial_dbs = sorted(
        db_file
        for db_file in glob.glob(partial_db_pattern)
        if os.path.abspath(db_file) != os.path.abspath(master_db_path)
    )
    if not partial_dbs:
        raise FileNotFoundError(
            f"No partial databases found to merge: {partial_db_pattern}"
        )

    print(
        f"🔄 Merging databases matching '{partial_db_pattern}' "
        f"into '{master_db_path}'..."
    )
    conn = sqlite3.connect(master_db_path)
    cursor = conn.cursor()
    failures = []
    complex_count = 0
    price_count = 0

    try:
        init_master_db(cursor)
        conn.commit()

        for db_file in partial_dbs:
            print(f"   ➕ Merging '{db_file}'...")
            attached = False
            shard_complex_count = 0
            shard_price_count = 0
            shard_error = None
            try:
                cursor.execute("ATTACH DATABASE ? AS partial", (db_file,))
                attached = True
                cursor.execute("BEGIN IMMEDIATE")
                shard_complex_count = _merge_complexes(cursor, db_file)
                shard_price_count = _merge_prices(cursor, db_file)
                conn.commit()
            except Exception as exc:
                conn.rollback()
                shard_error = exc
                failures.append((db_file, exc))
                print(f"      ❌ Failed to merge {db_file}: {exc}")
            finally:
                if attached:
                    try:
                        cursor.execute("DETACH DATABASE partial")
                    except Exception as detach_exc:
                        if shard_error is None:
                            failures.append((db_file, detach_exc))
                            print(f"      ❌ Failed to detach {db_file}: {detach_exc}")

            if shard_error is None and not any(
                failed_file == db_file for failed_file, _ in failures
            ):
                complex_count += shard_complex_count
                price_count += shard_price_count
                print(f"      ✅ Merged {db_file}")
    finally:
        conn.close()

    if failures:
        failed_files = ", ".join(db_file for db_file, _ in failures)
        raise RuntimeError(f"Failed to merge {len(failures)} shard(s): {failed_files}")

    print(f"🎉 Merge complete. Complexes: {complex_count}, Prices: {price_count}")


def main(argv=None):
    argv = sys.argv if argv is None else argv
    if len(argv) < 2:
        print("Usage: python merge_db.py <partial_db_pattern> [master_db_path]")
        return 1

    pattern = argv[1]
    master = argv[2] if len(argv) > 2 else "real_estate.db"
    try:
        merge_databases(master, pattern)
    except Exception as exc:
        print(f"❌ Merge failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
