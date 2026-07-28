from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
import unittest


APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import db


class PricePointIntegrityTests(unittest.TestCase):
    def setUp(self):
        raw = sqlite3.connect(":memory:")
        raw.row_factory = sqlite3.Row
        self.raw = raw
        self.con = db.DbConnection(raw, "sqlite")
        self.con.executescript(db._schema_sql("sqlite"))
        self.con.execute("INSERT INTO snapshots(timestamp) VALUES (?)", ("2026-01-01T00:00:00+00:00",))

    def tearDown(self):
        self.raw.close()

    def _insert_point(self, normalized_price: float) -> None:
        self.con.execute(
            """
            INSERT INTO price_points (
                snapshot_id, marketplace, market_hash_name, price, currency,
                normalized_price, normalized_currency, fetch_status, timestamp
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (1, "Example", "Item", normalized_price, "USD", normalized_price, "USD", "ok", "ts"),
        )

    def test_migration_keeps_newest_duplicate_and_enforces_unique_key(self):
        self._insert_point(1.0)
        self._insert_point(2.0)

        db.ensure_price_point_uniqueness(self.con)

        rows = self.con.execute("SELECT normalized_price FROM price_points").fetchall()
        self.assertEqual([row["normalized_price"] for row in rows], [2.0])
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_point(3.0)

    def test_existing_index_skips_repeat_migration(self):
        db.ensure_price_point_uniqueness(self.con)
        db.ensure_price_point_uniqueness(self.con)

        indexes = self.con.execute("PRAGMA index_list(price_points)").fetchall()
        self.assertIn("uq_price_points_snapshot_market_hash", {row["name"] for row in indexes})


if __name__ == "__main__":
    unittest.main()
