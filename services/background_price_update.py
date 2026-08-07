from __future__ import annotations

from dataclasses import asdict, dataclass
from threading import Lock, Thread
import time
from typing import Callable

from adapters.base import safe_error_details


@dataclass
class PriceUpdateState:
    job_id: int = 0
    status: str = "idle"
    received: int = 0
    total: int = 0
    market: str = ""
    snapshot_id: int | None = None
    timestamp: str | None = None
    success_rate: float | None = None
    error_details: str | None = None


class BackgroundPriceUpdate:
    """Run one manual update independently from Streamlit page reruns."""

    def __init__(
        self,
        collect_snapshot: Callable,
        latest_step_details: Callable[[], str | None],
        record_run: Callable[..., None],
        now: Callable[[], str],
    ) -> None:
        self._collect_snapshot = collect_snapshot
        self._latest_step_details = latest_step_details
        self._record_run = record_run
        self._now = now
        self._lock = Lock()
        self._state = PriceUpdateState()

    def start(self) -> bool:
        with self._lock:
            if self._state.status == "running":
                return False
            self._state = PriceUpdateState(job_id=self._state.job_id + 1, status="running")
            job_id = self._state.job_id
        Thread(target=self._run, args=(job_id,), daemon=True, name="manual-price-update").start()
        return True

    def snapshot(self) -> dict:
        with self._lock:
            return asdict(self._state)

    def _report_progress(self, job_id: int, received: int, total: int, market: str = "") -> None:
        with self._lock:
            if self._state.job_id != job_id or self._state.status != "running":
                return
            self._state.received = received
            self._state.total = total
            self._state.market = market

    def _finish(self, job_id: int, **values) -> None:
        with self._lock:
            if self._state.job_id != job_id:
                return
            for key, value in values.items():
                setattr(self._state, key, value)

    def _record(self, **kwargs) -> str | None:
        try:
            self._record_run(**kwargs)
        except Exception as exc:  # A completed snapshot remains usable if logging fails.
            return safe_error_details(exc)
        return None

    def _run(self, job_id: int) -> None:
        started_at = self._now()
        started_timer = time.perf_counter()
        try:
            snapshot_id, timestamp, success_rate = self._collect_snapshot(
                lambda received, total, market="": self._report_progress(job_id, received, total, market)
            )
        except Exception as exc:
            error_details = safe_error_details(exc)
            self._record(
                source="manual",
                started_at=started_at,
                finished_at=self._now(),
                duration_seconds=time.perf_counter() - started_timer,
                status="error",
                error_details=error_details,
                step_details=self._latest_step_details(),
            )
            self._finish(job_id, status="error", error_details=error_details)
            return

        record_error = self._record(
            source="manual",
            started_at=started_at,
            finished_at=self._now(),
            duration_seconds=time.perf_counter() - started_timer,
            status="ok",
            snapshot_id=snapshot_id,
            success_rate=success_rate,
            step_details=self._latest_step_details(),
        )
        self._finish(
            job_id,
            status="completed",
            snapshot_id=snapshot_id,
            timestamp=timestamp,
            success_rate=success_rate,
            error_details=record_error,
        )
