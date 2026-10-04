import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import crawler


def price_row(**overrides):
    value = {
        "complex_id": 101,
        "아파트명": "테스트아파트",
        "시/도": "서울시",
        "시/군/구": "강남구",
        "읍/면/동": "테스트동",
        "총세대수": 0,
        "총동수": 0,
        "준공일": "",
        "건설사": "",
        "난방방식": "",
        "난방연료": "",
        "세대당주차대수": "",
        "용적률": "",
        "건폐율": "",
        "위도": "",
        "경도": "",
        "수집일": "2026-09-22",
        "타입": "84A",
        "공급면적": 84.9,
        "전용면적": 59.9,
        "현관구조": "계단식",
        "방/욕실": "3/2개",
        "매매 최저가 (일반)": "200,000",
        "매매 최저가 (저층)": "190,000",
        "매매 최고가": "250,000",
        "매매 평균가": "220,000",
        "매매 매물수 (전체)": 2,
        "전세 최저가": "100,000",
        "전세 최고가": "120,000",
        "전세 평균가": "110,000",
        "전세 매물수": 2,
        "갭": "100,000",
        "전세가율": "50.0%",
    }
    value.update(overrides)
    return value


class CrawlerStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "crawl.db"
        self.db_patch = patch.object(crawler, "DB_PATH", str(self.db_path))
        self.db_patch.start()
        crawler.init_db()

    def tearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def scalar(self, sql, params=()):
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(sql, params).fetchone()[0]

    def test_price_snapshot_upsert_is_idempotent_and_returns_committed_count(self):
        frame = pd.DataFrame([price_row()])

        self.assertEqual(crawler.save_to_db(frame), 1)
        self.assertEqual(crawler.save_to_db(frame), 1)

        self.assertEqual(self.scalar("select count(*) from prices"), 1)

    def test_price_save_does_not_replace_known_households_with_zero(self):
        crawler.save_complexes_metadata_only(
            {"101": {"name": "목록명", "households": 777}}
        )

        crawler.save_to_db(pd.DataFrame([price_row()]))

        self.assertEqual(
            self.scalar(
                "select total_households from complexes where complex_no = 101"
            ),
            777,
        )

    def test_metadata_upsert_does_not_replace_known_values_with_empty_values(self):
        crawler.save_complexes_metadata_only(
            {
                "101": {
                    "name": "정상명",
                    "households": 777,
                    "constructionCompany": "정상건설",
                }
            }
        )
        crawler.save_complexes_metadata_only(
            {"101": {"name": "", "households": 0, "constructionCompany": ""}}
        )

        with sqlite3.connect(self.db_path) as conn:
            actual = conn.execute(
                "select name, total_households, construction_company "
                "from complexes where complex_no = 101"
            ).fetchone()
        self.assertEqual(actual, ("정상명", 777, "정상건설"))

    def test_save_failure_rolls_back_and_reraises(self):
        frame = pd.DataFrame(
            [price_row(), price_row(타입="84B", 공급면적="not-a-number")]
        )

        with self.assertRaises(ValueError):
            crawler.save_to_db(frame)

        self.assertEqual(self.scalar("select count(*) from prices"), 0)
        self.assertEqual(self.scalar("select count(*) from complexes"), 0)

    def test_metadata_failure_rolls_back_and_reraises(self):
        metadata = {
            "101": {"name": "정상명", "households": 777},
            "not-an-integer": {"name": "실패명", "households": 100},
        }

        with self.assertRaises(ValueError):
            crawler.save_complexes_metadata_only(metadata)

        self.assertEqual(self.scalar("select count(*) from complexes"), 0)

    def test_init_db_refuses_legacy_duplicate_price_keys_without_deleting_data(self):
        self.db_path.unlink()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("create table complexes (complex_no integer primary key)")
            conn.execute(
                """
                create table prices (
                    id integer primary key autoincrement,
                    complex_no integer,
                    date text,
                    pyeong_type text,
                    supply_area real,
                    exclusive_area real
                )
                """
            )
            values = (101, "2026-09-22", "84A", 84.9, 59.9)
            conn.execute(
                "insert into prices "
                "(complex_no, date, pyeong_type, supply_area, exclusive_area) "
                "values (?, ?, ?, ?, ?)",
                values,
            )
            conn.execute(
                "insert into prices "
                "(complex_no, date, pyeong_type, supply_area, exclusive_area) "
                "values (?, ?, ?, ?, ?)",
                values,
            )

        with self.assertRaisesRegex(RuntimeError, "duplicate canonical price snapshots"):
            crawler.init_db()

        self.assertEqual(self.scalar("select count(*) from prices"), 2)


if __name__ == "__main__":
    unittest.main()
