from __future__ import annotations

import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db  # noqa: E402
from adapters.base import safe_error_details  # noqa: E402
from services.basket_service import sync_basket_file  # noqa: E402
from services.update_service import (  # noqa: E402
    SnapshotQualityError,
    collect_snapshot,
    latest_update_step_details,
)


def main() -> int:
    startup_timer = time.perf_counter()
    print("[startup] Loading configuration", flush=True)
    load_dotenv(ROOT / ".env")
    print("[startup] Checking database readiness", flush=True)
    db.init_db(migrate=False, maintenance=False)
    print(f"[startup] Database ready after {time.perf_counter() - startup_timer:.1f}s", flush=True)
    print("[startup] Synchronizing basket file", flush=True)
    sync_basket_file()
    print(f"[startup] Basket ready after {time.perf_counter() - startup_timer:.1f}s", flush=True)
    started_at = db.utc_now_iso()
    started_timer = time.perf_counter()

    def report_progress(received: int, total: int, message: str) -> None:
        elapsed = time.perf_counter() - started_timer
        print(
            f"[{elapsed:7.1f}s] {received}/{total} prices received"
            f"{f' - {message}' if message else ''}",
            flush=True,
        )

    try:
        snapshot_id, timestamp, success_rate = collect_snapshot(progress_callback=report_progress)
    except SnapshotQualityError as exc:
        error_details = safe_error_details(exc)
        db.record_update_run(
            source="automatic",
            started_at=started_at,
            finished_at=db.utc_now_iso(),
            duration_seconds=time.perf_counter() - started_timer,
            status="error",
            error_details=error_details,
            step_details=latest_update_step_details(),
        )
        print(error_details)
        return 2
    except Exception as exc:
        error_details = safe_error_details(exc)
        db.record_update_run(
            source="automatic",
            started_at=started_at,
            finished_at=db.utc_now_iso(),
            duration_seconds=time.perf_counter() - started_timer,
            status="error",
            error_details=error_details,
            step_details=latest_update_step_details(),
        )
        raise
    db.record_update_run(
        source="automatic",
        started_at=started_at,
        finished_at=db.utc_now_iso(),
        duration_seconds=time.perf_counter() - started_timer,
        status="ok",
        snapshot_id=snapshot_id,
        success_rate=success_rate,
        step_details=latest_update_step_details(),
    )
    print(
        f"Saved snapshot #{snapshot_id} at {timestamp} "
        f"({success_rate:.0%} data received, postgres={db.using_postgres()})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
