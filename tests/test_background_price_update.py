from __future__ import annotations

import time
import unittest

from services.background_price_update import BackgroundPriceUpdate


class BackgroundPriceUpdateTests(unittest.TestCase):
    def wait_for_completion(self, job: BackgroundPriceUpdate) -> dict:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            state = job.snapshot()
            if state["status"] != "running":
                return state
            time.sleep(0.01)
        self.fail("background update did not complete")

    def test_progress_and_success_are_recorded(self) -> None:
        recorded = []

        def collect(progress):
            progress(3, 10, "CSFloat")
            return 42, "2026-07-30T00:00:00+00:00", 0.9

        job = BackgroundPriceUpdate(
            collect_snapshot=collect,
            latest_step_details=lambda: "[]",
            record_run=lambda **kwargs: recorded.append(kwargs),
            now=lambda: "2026-07-30T00:00:00+00:00",
        )

        self.assertTrue(job.start())
        state = self.wait_for_completion(job)

        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["snapshot_id"], 42)
        self.assertEqual(recorded[0]["status"], "ok")
        self.assertEqual(recorded[0]["source"], "manual")

    def test_error_is_recorded_without_crashing_caller(self) -> None:
        recorded = []

        def collect(_progress):
            raise RuntimeError("request failed")

        job = BackgroundPriceUpdate(
            collect_snapshot=collect,
            latest_step_details=lambda: "[]",
            record_run=lambda **kwargs: recorded.append(kwargs),
            now=lambda: "2026-07-30T00:00:00+00:00",
        )

        self.assertTrue(job.start())
        state = self.wait_for_completion(job)

        self.assertEqual(state["status"], "error")
        self.assertIn("request failed", state["error_details"])
        self.assertEqual(recorded[0]["status"], "error")

    def test_second_start_is_rejected_while_running(self) -> None:
        def collect(_progress):
            time.sleep(0.05)
            return 1, "2026-07-30T00:00:00+00:00", 1.0

        job = BackgroundPriceUpdate(
            collect_snapshot=collect,
            latest_step_details=lambda: "[]",
            record_run=lambda **_kwargs: None,
            now=lambda: "2026-07-30T00:00:00+00:00",
        )

        self.assertTrue(job.start())
        self.assertFalse(job.start())
        self.wait_for_completion(job)


if __name__ == "__main__":
    unittest.main()
