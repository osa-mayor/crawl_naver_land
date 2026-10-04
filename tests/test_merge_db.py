import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from merge_db import merge_databases


COMPLEX_COLUMNS = """
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
    last_updated TEXT
"""

PRICE_COLUMNS = """
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
    jeonse_ratio REAL
"""


def create_shard(path, *, complex_name="테스트", trade_avg=100, reduced=False):
    with sqlite3.connect(path) as conn:
        if reduced:
            conn.execute(
                "CREATE TABLE complexes (name TEXT, complex_no INTEGER PRIMARY KEY)"
            )
            conn.execute(
                """CREATE TABLE prices (
                    trade_avg INTEGER, exclusive_area REAL, supply_area REAL,
                    pyeong_type TEXT, date TEXT, complex_no INTEGER
                )"""
            )
        else:
            conn.execute(f"CREATE TABLE complexes ({COMPLEX_COLUMNS})")
            conn.execute(f"CREATE TABLE prices ({PRICE_COLUMNS})")

        conn.execute(
            "INSERT INTO complexes (complex_no, name) VALUES (?, ?)",
            (101, complex_name),
        )
        conn.execute(
            """INSERT INTO prices (
                complex_no, date, pyeong_type, supply_area, exclusive_area, trade_avg
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            (101, "2026-09-22", "84A", 110.1, 84.9, trade_avg),
        )


class MergeDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.master = self.root / "merged.db"

    def tearDown(self):
        self.tempdir.cleanup()

    def test_bad_first_shard_rolls_back_detaches_and_valid_shard_still_merges(self):
        bad = self.root / "db_shard_01.db"
        valid = self.root / "db_shard_02.db"
        with sqlite3.connect(bad) as conn:
            conn.execute("CREATE TABLE complexes (complex_no INTEGER PRIMARY KEY, name TEXT)")
            conn.execute("INSERT INTO complexes VALUES (999, '롤백 대상')")
            conn.execute("CREATE TABLE prices (complex_no INTEGER, date TEXT)")
        create_shard(valid)

        with self.assertRaises(RuntimeError):
            merge_databases(str(self.master), str(self.root / "db_shard_*.db"))

        with sqlite3.connect(self.master) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM prices").fetchone()[0], 1)
            self.assertIsNone(
                conn.execute(
                    "SELECT name FROM complexes WHERE complex_no = 999"
                ).fetchone()
            )
            attached = conn.execute("PRAGMA database_list").fetchall()
            self.assertEqual([row[1] for row in attached], ["main"])

    def test_repeat_merge_is_idempotent_for_canonical_price_identity(self):
        shard = self.root / "db_shard_01.db"
        create_shard(shard, trade_avg=100)

        merge_databases(str(self.master), str(shard))
        with sqlite3.connect(shard) as conn:
            conn.execute("UPDATE prices SET trade_avg = 125")
        merge_databases(str(self.master), str(shard))

        with sqlite3.connect(self.master) as conn:
            rows = conn.execute("SELECT trade_avg FROM prices").fetchall()
            self.assertEqual(rows, [(125,)])

    def test_repeat_merge_treats_null_identity_values_as_the_same_snapshot(self):
        shard = self.root / "db_shard_01.db"
        create_shard(shard)
        with sqlite3.connect(shard) as conn:
            conn.execute("UPDATE prices SET exclusive_area = NULL")

        merge_databases(str(self.master), str(shard))
        merge_databases(str(self.master), str(shard))

        with sqlite3.connect(self.master) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM prices").fetchone()[0], 1)

    def test_existing_duplicate_snapshots_fail_without_deleting_data(self):
        shard = self.root / "db_shard_01.db"
        create_shard(shard)
        with sqlite3.connect(self.master) as conn:
            conn.execute(f"CREATE TABLE complexes ({COMPLEX_COLUMNS})")
            conn.execute(f"CREATE TABLE prices ({PRICE_COLUMNS})")
            for trade_avg in (100, 125):
                conn.execute(
                    """INSERT INTO prices (
                        complex_no, date, pyeong_type, supply_area,
                        exclusive_area, trade_avg
                    ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (101, "2026-09-22", "84A", 110.1, 84.9, trade_avg),
                )

        with self.assertRaises(RuntimeError):
            merge_databases(str(self.master), str(shard))

        with sqlite3.connect(self.master) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM prices").fetchone()[0], 2)

    def test_no_matching_inputs_fails_without_creating_master(self):
        with self.assertRaises(FileNotFoundError):
            merge_databases(str(self.master), str(self.root / "missing_*.db"))

        self.assertFalse(self.master.exists())

    def test_cli_exits_nonzero_when_no_inputs_match(self):
        script = Path(__file__).resolve().parents[1] / "merge_db.py"
        result = subprocess.run(
            [sys.executable, str(script), str(self.root / "missing_*.db"), str(self.master)],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Merge complete", result.stdout)

    def test_cli_exits_nonzero_and_does_not_claim_completion_on_shard_failure(self):
        bad = self.root / "db_shard_01.db"
        valid = self.root / "db_shard_02.db"
        with sqlite3.connect(bad) as conn:
            conn.execute("CREATE TABLE unrelated (value TEXT)")
        create_shard(valid)
        script = Path(__file__).resolve().parents[1] / "merge_db.py"

        result = subprocess.run(
            [
                sys.executable,
                str(script),
                str(self.root / "db_shard_*.db"),
                str(self.master),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Merge complete", result.stdout)
        with sqlite3.connect(self.master) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM prices").fetchone()[0], 1)

    def test_quote_in_shard_path_is_supported_by_parameterized_attach(self):
        shard = self.root / "db_shard_quote's.db"
        create_shard(shard)

        merge_databases(str(self.master), str(shard))

        with sqlite3.connect(self.master) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM prices").fetchone()[0], 1)

    def test_reduced_reordered_compatible_schema_uses_explicit_columns(self):
        shard = self.root / "db_shard_reduced.db"
        create_shard(shard, reduced=True)

        merge_databases(str(self.master), str(shard))

        with sqlite3.connect(self.master) as conn:
            row = conn.execute(
                "SELECT complex_no, date, pyeong_type, trade_avg, rent_avg FROM prices"
            ).fetchone()
            self.assertEqual(row, (101, "2026-09-22", "84A", 100, None))

    def test_blank_shard_metadata_does_not_replace_existing_values(self):
        first = self.root / "first.db"
        second = self.root / "second.db"
        create_shard(first, complex_name="보존할 이름")
        create_shard(second, complex_name="")
        with sqlite3.connect(first) as conn:
            conn.execute("UPDATE complexes SET total_households = 777")
        with sqlite3.connect(second) as conn:
            conn.execute("UPDATE complexes SET total_households = 0")

        merge_databases(str(self.master), str(first))
        merge_databases(str(self.master), str(second))

        with sqlite3.connect(self.master) as conn:
            metadata = conn.execute(
                "SELECT name, total_households FROM complexes WHERE complex_no = 101"
            ).fetchone()
            self.assertEqual(metadata, ("보존할 이름", 777))


if __name__ == "__main__":
    unittest.main()
