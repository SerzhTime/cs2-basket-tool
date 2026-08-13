from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from threading import Lock, Thread
import time
from typing import Callable

from adapters.base import safe_error_details


@dataclass
class NeonSyncState:
    job_id: int = 0
    status: str = "idle"
    trigger: str = ""
    counts: dict | None = None
    elapsed_seconds: float | None = None
    error_details: str | None = None


class BackgroundNeonSync:
    """Run one Neon synchronization independently from Streamlit page reruns."""

    def __init__(
        self,
        sync: Callable[[], dict],
        record_run: Callable[..., None],
        now: Callable[[], str],
    ) -> None:
        self._sync = sync
        self._record_run = record_run
        self._now = now
        self._lock = Lock()
        self._state = NeonSyncState()

    def start(self, trigger: str) -> bool:
        with self._lock:
            if self._state.status == "running":
                return False
            self._state = NeonSyncState(job_id=self._state.job_id + 1, status="running", trigger=trigger)
            job_id = self._state.job_id
        Thread(target=self._run, args=(job_id, trigger), daemon=True, name="neon-sync").start()
        return True

    def snapshot(self) -> dict:
        with self._lock:
            return asdict(self._state)

    def _finish(self, job_id: int, **values) -> None:
        with self._lock:
            if self._state.job_id != job_id:
                return
            for key, value in values.items():
                setattr(self._state, key, value)

    def _record(self, **kwargs) -> None:
        try:
            self._record_run(**kwargs)
        except Exception:
            # A completed sync stays usable even if logging the run failed.
            pass

    def _run(self, job_id: int, trigger: str) -> None:
        started_at = self._now()
        started_timer = time.perf_counter()
        try:
            counts = self._sync()
        except Exception as exc:
            error_details = safe_error_details(exc)
            self._record(
                source="sync",
                started_at=started_at,
                finished_at=self._now(),
                duration_seconds=time.perf_counter() - started_timer,
                status="error",
                error_details=error_details,
                step_details=json.dumps(
                    [{"provider_group": "Neon", "step": "Synchronization", "errors": error_details}]
                ),
            )
            self._finish(job_id, status="error", error_details=error_details)
            return

        elapsed_seconds = float(counts.get("elapsed_seconds", time.perf_counter() - started_timer))
        sync_step = "Full reconciliation" if counts.get("full_reconcile") else "Incremental synchronization"
        self._record(
            source="sync",
            started_at=started_at,
            finished_at=self._now(),
            duration_seconds=elapsed_seconds,
            status="ok",
            error_details=(
                f"checked {counts.get('checked_snapshots', 0)} snapshots, "
                f"unchanged {counts.get('unchanged_snapshots', 0)}"
            ),
            step_details=json.dumps(
                [
                    {
                        "provider_group": "Neon",
                        "step": sync_step,
                        "duration_seconds": elapsed_seconds,
                        "elapsed_seconds": elapsed_seconds,
                        "received": (
                            f"pushed {counts['snapshots']} snapshots / {counts['price_points']} price points; "
                            f"pulled {counts['pulled_snapshots']} snapshots / {counts['pulled_price_points']} price points"
                        ),
                        "missing": counts.get("unchanged_snapshots", 0),
                        "errors": "",
                    }
                ]
            ),
        )
        self._finish(job_id, status="completed", counts=counts, elapsed_seconds=elapsed_seconds)
