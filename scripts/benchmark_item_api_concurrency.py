from __future__ import annotations

"""Measure safe bounded concurrency for item-by-item marketplace APIs.

The probe reads active basket items and calls only read-only price endpoints. It
does not save prices, mutate database rows, or print credentials.
"""

import argparse
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

import db
from adapters.csfloat import CSFloatAdapter
from adapters.dmarket import DMarketAdapter
from adapters.skindeck import SkindeckAdapter


@dataclass
class Outcome:
    status: str
    duration_seconds: float
    details: str | None


class RateLimiter:
    def __init__(self, requests_per_second: float | None) -> None:
        self.interval = 1.0 / requests_per_second if requests_per_second else 0.0
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self) -> None:
        if not self.interval:
            return
        with self._lock:
            now = time.monotonic()
            target = max(now, self._next_allowed)
            self._next_allowed = target + self.interval
        delay = target - now
        if delay > 0:
            time.sleep(delay)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", choices=("csfloat", "dmarket", "skindeck", "all"), default="all")
    parser.add_argument("--items", type=int, default=12)
    parser.add_argument("--levels", default="1,2,4")
    parser.add_argument(
        "--dmarket-rps",
        type=float,
        default=8.0,
        help="Keep DMarket below its documented authenticated 10 RPS limit.",
    )
    return parser.parse_args()


def fetch_one(adapter, item, limiter: RateLimiter) -> Outcome:
    limiter.wait()
    started = time.perf_counter()
    try:
        result = adapter.fetch_prices([item])[0]
        return Outcome(result.fetch_status, time.perf_counter() - started, result.error_details)
    except Exception as exc:  # The benchmark must continue to classify failures.
        return Outcome("error", time.perf_counter() - started, str(exc))


def run_level(adapter, items, workers: int, limiter: RateLimiter) -> dict[str, object]:
    started = time.perf_counter()
    outcomes: list[Outcome] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"probe-{adapter.key}") as executor:
        futures = [executor.submit(fetch_one, adapter, item, limiter) for item in items]
        for future in as_completed(futures):
            outcomes.append(future.result())

    elapsed = time.perf_counter() - started
    errors = [outcome for outcome in outcomes if outcome.status == "error"]
    throttled = [outcome for outcome in outcomes if "429" in (outcome.details or "")]
    return {
        "workers": workers,
        "elapsed": elapsed,
        "ok": sum(outcome.status == "ok" for outcome in outcomes),
        "missing": sum(outcome.status == "missing" for outcome in outcomes),
        "errors": len(errors),
        "throttled": len(throttled),
        "average": sum(outcome.duration_seconds for outcome in outcomes) / len(outcomes),
        "details": next((outcome.details for outcome in errors if outcome.details), None),
    }


def main() -> int:
    args = parse_args()
    levels = sorted({max(1, int(value)) for value in args.levels.split(",") if value.strip()})
    db.init_db()
    items = db.get_adapter_items()[: max(1, args.items)]
    if not items:
        raise RuntimeError("No active basket items were found.")

    adapters = {
        "csfloat": CSFloatAdapter(),
        "dmarket": DMarketAdapter(),
        "skindeck": SkindeckAdapter(),
    }
    markets = adapters if args.market == "all" else {args.market: adapters[args.market]}
    print(f"Benchmarking {len(items)} active basket items at concurrency levels {levels}.")
    for key, adapter in markets.items():
        if not adapter.credentials_configured():
            print(f"{adapter.name}: skipped (credentials not configured)")
            continue
        print(f"\n{adapter.name}")
        for workers in levels:
            limiter = RateLimiter(args.dmarket_rps if key == "dmarket" else None)
            summary = run_level(adapter, items, workers, limiter)
            error_details = f" first_error={summary['details']!r}" if summary["details"] else ""
            print(
                f"  workers={summary['workers']}: {summary['elapsed']:.1f}s, "
                f"ok={summary['ok']}, missing={summary['missing']}, "
                f"errors={summary['errors']}, throttled={summary['throttled']}, "
                f"avg_request={summary['average']:.2f}s{error_details}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
