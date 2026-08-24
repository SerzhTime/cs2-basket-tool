from __future__ import annotations

import time
import unittest

from services.background_neon_sync import BackgroundNeonSync


BASE_COUNTS = {
    "basket_items": 0,
    "marketplaces": 0,
    "snapshots": 2,
    "price_points": 20,
    "pulled_snapshots": 0,
    "pulled_price_points": 0,
    "update_runs": 5,
    "pulled_update_runs": 0,
    "replaced_snapshots": 0,
    "checked_snapshots": 3,
    "unchanged_snapshots": 1,
    "full_reconcile": 0,
    "elapsed_seconds": 0.42,
}


class BackgroundNeonSyncTests(unittest.TestCase):
    def wait_for_completion(self, job: BackgroundNeonSync) -> dict:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            state = job.snapshot()
            if state["status"] != "running":
                return state
            time.sleep(0.01)
        self.fail("background sync did not complete")

    def test_progress_and_success_are_recorded(self) -> None:
        recorded = []

        def sync():
            return dict(BASE_COUNTS)

        job = BackgroundNeonSync(
            sync=sync,
            record_run=lambda **kwargs: recorded.append(kwargs),
            now=lambda: "2026-07-30T00:00:00+00:00",
        )

        self.assertTrue(job.start("manual"))
        state = self.wait_for_completion(job)

        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["trigger"], "manual")
        self.assertEqual(state["counts"]["snapshots"], 2)
        self.assertEqual(recorded[0]["status"], "ok")
        self.assertEqual(recorded[0]["source"], "sync")

    def test_error_is_recorded_without_crashing_caller(self) -> None:
        recorded = []

        def sync():
            raise RuntimeError("connection failed")

        job = BackgroundNeonSync(
            sync=sync,
            record_run=lambda **kwargs: recorded.append(kwargs),
            now=lambda: "2026-07-30T00:00:00+00:00",
        )

        self.assertTrue(job.start("startup"))
        state = self.wait_for_completion(job)

        self.assertEqual(state["status"], "error")
        self.assertIn("connection failed", state["error_details"])
        self.assertEqual(recorded[0]["status"], "error")

    def test_completion_can_be_consumed_only_once(self) -> None:
        job = BackgroundNeonSync(
            sync=lambda: dict(BASE_COUNTS),
            record_run=lambda **_kwargs: None,
            now=lambda: "2026-07-30T00:00:00+00:00",
        )

        self.assertTrue(job.start("manual"))
        self.wait_for_completion(job)

        completed = job.consume_completion()
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["trigger"], "manual")
        self.assertIsNone(job.consume_completion())
        self.assertEqual(job.snapshot()["status"], "idle")

    def test_second_start_is_rejected_while_running(self) -> None:
        def sync():
            time.sleep(0.05)
            return dict(BASE_COUNTS)

        job = BackgroundNeonSync(
            sync=sync,
            record_run=lambda **_kwargs: None,
            now=lambda: "2026-07-30T00:00:00+00:00",
        )

        self.assertTrue(job.start("manual"))
        self.assertFalse(job.start("manual"))
        self.wait_for_completion(job)


if __name__ == "__main__":
    unittest.main()
