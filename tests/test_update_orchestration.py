from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch


APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import app
from adapters.base import BasketItem


class _Adapter:
    def __init__(self, name: str, *, error: Exception | None = None):
        self.name = name
        self.error = error

    def fetch_prices(self, items):
        if self.error:
            raise self.error
        return []


class UpdateOrchestrationTests(unittest.TestCase):
    def test_shared_providers_are_grouped(self):
        self.assertEqual(app.adapter_provider_group("csgoskins_exeskins"), "CSGOSKINS")
        self.assertEqual(app.adapter_provider_group("openskin_buff163"), "OpenSkin")
        self.assertEqual(app.adapter_provider_group("csfloat"), "csfloat")

    def test_adapter_exception_becomes_one_error_per_item(self):
        items = [
            BasketItem(item_id=1, market_hash_name="A"),
            BasketItem(item_id=2, market_hash_name="B"),
        ]
        completed = app.fetch_adapter_group([_Adapter("Example", error=RuntimeError("offline"))], items)

        self.assertEqual(len(completed), 1)
        results = completed[0]["results"]
        self.assertEqual([result.fetch_status for result in results], ["error", "error"])
        self.assertEqual([result.market_hash_name for result in results], ["A", "B"])
        self.assertTrue(all(result.error_details == "offline" for result in results))

    def test_update_steps_are_isolated_between_threads(self):
        barrier = threading.Barrier(2)
        outputs: dict[str, str | None] = {}

        def worker(label: str) -> None:
            app._UPDATE_STATE.steps = [{"step": label}]
            barrier.wait()
            outputs[label] = app.latest_update_step_details()

        threads = [threading.Thread(target=worker, args=(label,)) for label in ("one", "two")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(json.loads(outputs["one"]), [{"step": "one"}])
        self.assertEqual(json.loads(outputs["two"]), [{"step": "two"}])

    def test_parallel_snapshot_attempt_is_rejected_without_stale_steps(self):
        app._UPDATE_STATE.steps = [{"step": "stale"}]
        app._SNAPSHOT_UPDATE_LOCK.acquire()
        try:
            with self.assertRaisesRegex(app.SnapshotQualityError, "already running"):
                app.collect_snapshot()
        finally:
            app._SNAPSHOT_UPDATE_LOCK.release()

        self.assertIsNone(app.latest_update_step_details())

    def test_remote_snapshot_lock_rejects_cross_process_overlap(self):
        @contextmanager
        def unavailable_lock():
            yield False

        with patch("app.db.remote_price_update_lock", unavailable_lock):
            with self.assertRaisesRegex(app.SnapshotQualityError, "remote update"):
                app.collect_snapshot()


if __name__ == "__main__":
    unittest.main()
