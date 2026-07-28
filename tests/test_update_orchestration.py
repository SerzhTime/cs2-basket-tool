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

from adapters.base import BasketItem, PriceResult
from services import update_service


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
        self.assertEqual(update_service.adapter_provider_group("csgoskins_exeskins"), "CSGOSKINS")
        self.assertEqual(update_service.adapter_provider_group("openskin_buff163"), "OpenSkin")
        self.assertEqual(update_service.adapter_provider_group("csfloat"), "csfloat")

    def test_adapter_exception_becomes_one_error_per_item(self):
        items = [
            BasketItem(item_id=1, market_hash_name="A"),
            BasketItem(item_id=2, market_hash_name="B"),
        ]
        completed = update_service.fetch_adapter_group(
            [_Adapter("Example", error=RuntimeError("offline"))],
            items,
        )

        self.assertEqual(len(completed), 1)
        results = completed[0]["results"]
        self.assertEqual([result.fetch_status for result in results], ["error", "error"])
        self.assertEqual([result.market_hash_name for result in results], ["A", "B"])
        self.assertTrue(all(result.error_details == "offline" for result in results))

    def test_update_steps_are_isolated_between_threads(self):
        barrier = threading.Barrier(2)
        outputs: dict[str, str | None] = {}

        def worker(label: str) -> None:
            update_service._UPDATE_STATE.steps = [{"step": label}]
            barrier.wait()
            outputs[label] = update_service.latest_update_step_details()

        threads = [threading.Thread(target=worker, args=(label,)) for label in ("one", "two")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(json.loads(outputs["one"]), [{"step": "one"}])
        self.assertEqual(json.loads(outputs["two"]), [{"step": "two"}])

    def test_parallel_snapshot_attempt_is_rejected_without_stale_steps(self):
        update_service._UPDATE_STATE.steps = [{"step": "stale"}]
        update_service._SNAPSHOT_UPDATE_LOCK.acquire()
        try:
            with self.assertRaisesRegex(update_service.SnapshotQualityError, "already running"):
                update_service.collect_snapshot()
        finally:
            update_service._SNAPSHOT_UPDATE_LOCK.release()

        self.assertIsNone(update_service.latest_update_step_details())

    def test_remote_snapshot_lock_rejects_cross_process_overlap(self):
        @contextmanager
        def unavailable_lock():
            yield False

        with patch("services.update_service.db.remote_price_update_lock", unavailable_lock):
            with self.assertRaisesRegex(update_service.SnapshotQualityError, "remote update"):
                update_service.collect_snapshot()

    def test_missing_baseline_items_requires_every_active_item(self):
        items = [
            BasketItem(item_id=1, market_hash_name="A"),
            BasketItem(item_id=2, market_hash_name="B"),
        ]
        results = [
            PriceResult(
                marketplace="HaloSkins",
                market_hash_name="A",
                price=10.0,
                currency="USD",
                fetch_status="ok",
            ),
            PriceResult(
                marketplace="HaloSkins",
                market_hash_name="B",
                price=None,
                currency="USD",
                fetch_status="missing",
            ),
        ]

        self.assertEqual(update_service.missing_baseline_items(results, items), ["B"])


if __name__ == "__main__":
    unittest.main()
