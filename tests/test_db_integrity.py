from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
import unittest
from contextlib import contextmanager
from datetime import datetime


APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import db


class HistoryRetentionTests(unittest.TestCase):
    def setUp(self):
        raw = sqlite3.connect(":memory:")
        raw.row_factory = sqlite3.Row
        self.raw = raw
        self.con = db.DbConnection(raw, "sqlite")
        self.con.executescript(db._schema_sql("sqlite"))
        item = self.con.execute(
            "INSERT INTO basket_items (market_hash_name, active, multiplier, created_at) VALUES (?, 1, 1, ?) RETURNING item_id",
            ("Item", "2026-01-01T00:00:00+00:00"),
        ).fetchone()
        self.item_id = int(item["item_id"])
        self.con.execute(
            "INSERT INTO marketplaces (adapter_key, name, enabled, is_baseline, updated_at) VALUES (?, ?, 1, ?, ?)",
            ("haloskins", "HaloSkins", 1, "2026-01-01T00:00:00+00:00"),
        )

    def tearDown(self):
        self.raw.close()

    def _snapshot(self, timestamp: str, price: float) -> None:
        snapshot = self.con.execute("INSERT INTO snapshots(timestamp) VALUES (?) RETURNING snapshot_id", (timestamp,)).fetchone()
        self.con.execute(
            """
            INSERT INTO price_points (
                snapshot_id, marketplace, item_id, market_hash_name, price, currency,
                normalized_price, normalized_currency, fetch_status, timestamp
            ) VALUES (?, 'HaloSkins', ?, 'Item', ?, 'USD', ?, 'USD', 'ok', ?)
            """,
            (snapshot["snapshot_id"], self.item_id, price, price, timestamp),
        )

    def test_daily_calendar_day_and_min_average_max(self):
        self._snapshot("2026-08-20T16:00:00+00:00", 10.0)
        self._snapshot("2026-08-20T18:00:00+00:00", 14.0)
        old_connect = db.connect
        db.connect = lambda: self._connection_context()
        try:
            result = db.compact_history(now=__import__("datetime").datetime.fromisoformat("2026-08-22T00:00:00+00:00"))
        finally:
            db.connect = old_connect
        self.assertEqual(result["daily_totals"], 1)
        row = self.raw.execute("SELECT * FROM history_daily_totals").fetchone()
        self.assertEqual(row["period_start"], "2026-08-21T00:00:00+08:00")
        self.assertEqual(row["average_total_cost"], 12.0)
        self.assertEqual(row["min_total_cost"], 10.0)
        self.assertEqual(row["max_total_cost"], 14.0)
        self.assertEqual(row["sample_count"], 2)

    @contextmanager
    def _connection_context(self):
        yield self.con

    def test_dry_run_does_not_delete_raw_rows(self):
        self._snapshot("2026-08-20T16:00:00+00:00", 10.0)
        result = db.compact_history(now=__import__("datetime").datetime.fromisoformat("2026-08-22T00:00:00+00:00"), dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(self.raw.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 1)
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


class SnapshotsNeedingCheckTests(unittest.TestCase):
    def setUp(self):
        raw = sqlite3.connect(":memory:")
        raw.row_factory = sqlite3.Row
        self.raw = raw
        self.con = db.DbConnection(raw, "sqlite")
        self.con.executescript(db._schema_sql("sqlite"))

    def tearDown(self):
        self.raw.close()

    def _add_snapshot(self, timestamp: str, price: float) -> None:
        self.con.execute("INSERT INTO snapshots(timestamp) VALUES (?)", (timestamp,))
        snapshot_id = self.con.execute("SELECT snapshot_id FROM snapshots WHERE timestamp = ?", (timestamp,)).fetchone()[
            "snapshot_id"
        ]
        self.con.execute(
            """
            INSERT INTO price_points (
                snapshot_id, marketplace, market_hash_name, price, currency,
                normalized_price, normalized_currency, fetch_status, timestamp
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (snapshot_id, "HaloSkins", "Item", price, "USD", price, "USD", "ok", timestamp),
        )

    def test_empty_cache_checks_every_snapshot(self):
        self._add_snapshot("2026-01-01T00:00:00+00:00", 1.0)
        self._add_snapshot("2026-01-02T00:00:00+00:00", 2.0)

        checked = db._sqlite_snapshots_needing_check(self.raw, {})

        self.assertEqual(set(checked), {"2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00"})

    def test_cached_snapshots_are_skipped_except_latest(self):
        self._add_snapshot("2026-01-01T00:00:00+00:00", 1.0)
        self._add_snapshot("2026-01-02T00:00:00+00:00", 2.0)
        first_pass = db._sqlite_snapshots_needing_check(self.raw, {})
        cached_signatures = {ts: snap["signature"] for ts, snap in first_pass.items()}

        second_pass = db._sqlite_snapshots_needing_check(self.raw, cached_signatures)

        self.assertEqual(
            set(second_pass),
            {"2026-01-02T00:00:00+00:00"},
            "only the latest snapshot should be re-checked once everything is cached",
        )

    def test_new_snapshot_is_checked_and_previous_latest_is_skipped(self):
        self._add_snapshot("2026-01-01T00:00:00+00:00", 1.0)
        self._add_snapshot("2026-01-02T00:00:00+00:00", 2.0)
        first_pass = db._sqlite_snapshots_needing_check(self.raw, {})
        cached_signatures = {ts: snap["signature"] for ts, snap in first_pass.items()}

        self._add_snapshot("2026-01-03T00:00:00+00:00", 3.0)
        third_pass = db._sqlite_snapshots_needing_check(self.raw, cached_signatures)

        self.assertEqual(
            set(third_pass),
            {"2026-01-03T00:00:00+00:00"},
            "adding a new latest snapshot should not force a re-check of older cached ones",
        )


class UpdateRunUniquenessTests(unittest.TestCase):
    def setUp(self):
        raw = sqlite3.connect(":memory:")
        raw.row_factory = sqlite3.Row
        self.raw = raw
        self.con = db.DbConnection(raw, "sqlite")
        self.con.executescript(db._schema_sql("sqlite"))

    def tearDown(self):
        self.raw.close()

    def _insert_run(self, status: str) -> None:
        self.con.execute(
            """
            INSERT INTO update_runs (
                source, started_at, finished_at, duration_seconds, status
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            ("manual", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01+00:00", 1.0, status),
        )

    def test_migration_keeps_newest_duplicate_and_enforces_unique_key(self):
        self._insert_run("error")
        self._insert_run("ok")

        db.ensure_update_run_uniqueness(self.con)

        rows = self.con.execute("SELECT status FROM update_runs").fetchall()
        self.assertEqual([row["status"] for row in rows], ["ok"])
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_run("error")

    def test_existing_index_skips_repeat_migration(self):
        db.ensure_update_run_uniqueness(self.con)
        db.ensure_update_run_uniqueness(self.con)

        indexes = self.con.execute("PRAGMA index_list(update_runs)").fetchall()
        self.assertIn("uq_update_runs_source_started", {row["name"] for row in indexes})


class UpdateRunSyncCursorTests(unittest.TestCase):
    def setUp(self):
        source_raw = sqlite3.connect(":memory:")
        source_raw.row_factory = sqlite3.Row
        target_raw = sqlite3.connect(":memory:")
        target_raw.row_factory = sqlite3.Row
        self.source_raw = source_raw
        self.target_raw = target_raw
        self.source = source_raw
        self.target = db.DbConnection(target_raw, "sqlite")
        self.source.executescript(db._schema_sql("sqlite"))
        self.target.executescript(db._schema_sql("sqlite"))
        db.ensure_update_run_uniqueness(self.target)

    def tearDown(self):
        self.source_raw.close()
        self.target_raw.close()

    def _insert_local_run(self, started_at: str) -> None:
        self.source.execute(
            """
            INSERT INTO update_runs (
                source, started_at, finished_at, duration_seconds, status
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            ("manual", started_at, started_at, 1.0, "ok"),
        )

    def test_push_only_syncs_rows_after_cursor(self):
        self._insert_local_run("2026-01-01T00:00:00.000+00:00")
        self._insert_local_run("2026-01-01T00:01:00.000+00:00")

        counts = {"update_runs": 0}
        cursor = db._sync_update_runs_to_postgres(
            self.source, self.target, counts, since=None
        )

        self.assertEqual(counts["update_runs"], 2)
        self.assertEqual(cursor, "2026-01-01T00:01:00.000+00:00")
        pushed = self.target.execute("SELECT COUNT(*) c FROM update_runs").fetchone()["c"]
        self.assertEqual(pushed, 2)

        self._insert_local_run("2026-01-01T00:02:00.000+00:00")
        counts = {"update_runs": 0}
        cursor = db._sync_update_runs_to_postgres(
            self.source, self.target, counts, since=cursor
        )

        self.assertEqual(counts["update_runs"], 1, "only the new row since the cursor should be pushed")
        self.assertEqual(cursor, "2026-01-01T00:02:00.000+00:00")
        pushed = self.target.execute("SELECT COUNT(*) c FROM update_runs").fetchone()["c"]
        self.assertEqual(pushed, 3)

    def test_pull_uses_insert_or_ignore_and_cursor(self):
        self.target.execute(
            """
            INSERT INTO update_runs (
                source, started_at, finished_at, duration_seconds, status
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            ("sync", "2026-01-02T00:00:00.000+00:00", "2026-01-02T00:00:00.000+00:00", 1.0, "ok"),
        )

        counts = {"pulled_update_runs": 0}
        cursor = db._sync_missing_postgres_update_runs_to_sqlite(
            self.source, self.target, counts, since=None
        )

        self.assertEqual(cursor, "2026-01-02T00:00:00.000+00:00")
        pulled = self.source.execute("SELECT COUNT(*) c FROM update_runs").fetchone()["c"]
        self.assertEqual(pulled, 1)

        # A second pull with the same cursor should find nothing new.
        counts = {"pulled_update_runs": 0}
        cursor_again = db._sync_missing_postgres_update_runs_to_sqlite(
            self.source, self.target, counts, since=cursor
        )
        self.assertEqual(counts["pulled_update_runs"], 0)
        self.assertEqual(cursor_again, cursor)


if __name__ == "__main__":
    unittest.main()
