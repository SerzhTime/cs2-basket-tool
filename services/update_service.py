from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import importlib
import json
from threading import Lock, local
import os
import time

import db
from adapters import PriceResult, build_adapter_registry
from adapters.backup_sources import apply_backup_prices, clear_backup_cache
from adapters.base import safe_error_details
from adapters.csgoskins import clear_csgoskins_cache
from calculations import BASELINE_MARKETPLACE


class SnapshotQualityError(RuntimeError):
    pass


_UPDATE_STATE = local()
_SNAPSHOT_UPDATE_LOCK = Lock()


def _update_steps() -> list[dict]:
    steps = getattr(_UPDATE_STATE, "steps", None)
    if steps is None:
        steps = []
        _UPDATE_STATE.steps = steps
    return steps


def latest_update_step_details() -> str | None:
    steps = _update_steps()
    return json.dumps(steps, separators=(",", ":")) if steps else None


def adapter_provider_group(adapter_key: str) -> str:
    if adapter_key.startswith("csgoskins_"):
        return "CSGOSKINS"
    if adapter_key.startswith("openskin_"):
        return "OpenSkin"
    return adapter_key


def fetch_adapter_group(adapters, items) -> list[dict]:
    completed: list[dict] = []
    for adapter in adapters:
        started = time.perf_counter()
        try:
            results = adapter.fetch_prices(items)
        except Exception as exc:
            results = [
                PriceResult(
                    marketplace=adapter.name,
                    market_hash_name=item.market_hash_name,
                    price=None,
                    currency="USD",
                    fetch_status="error",
                    error_details=safe_error_details(exc),
                )
                for item in items
            ]
        completed.append(
            {
                "adapter": adapter,
                "results": results,
                "duration_seconds": time.perf_counter() - started,
            }
        )
    return completed


def collect_snapshot(progress_callback=None) -> tuple[int, str, float]:
    _UPDATE_STATE.steps = []
    if not _SNAPSHOT_UPDATE_LOCK.acquire(blocking=False):
        raise SnapshotQualityError("Another price update is already running. Wait for it to finish and try again.")
    try:
        with db.remote_price_update_lock() as acquired:
            if not acquired:
                raise SnapshotQualityError(
                    "Another remote update or Neon synchronization is already running. "
                    "Wait for it to finish and try again."
                )
            return _collect_snapshot(progress_callback)
    finally:
        _SNAPSHOT_UPDATE_LOCK.release()


def _collect_snapshot(progress_callback=None) -> tuple[int, str, float]:
    run_started = time.perf_counter()
    update_steps = _update_steps()
    registry = build_adapter_registry()
    clear_backup_cache()
    clear_csgoskins_cache()
    enabled_keys = db.get_enabled_adapter_keys()
    items = db.get_adapter_items()
    all_results: list[PriceResult] = []
    enabled_adapters = [(key, registry[key]) for key in enabled_keys if key in registry]
    adapters = [adapter for _, adapter in enabled_adapters]
    expected_count = len(items) * len(adapters)
    if expected_count == 0:
        raise SnapshotQualityError("No active basket items or enabled marketplaces are available to update.")

    def report_progress(current_market: str = "") -> None:
        if progress_callback is None:
            return
        received_count = sum(
            1
            for result in all_results
            if result.fetch_status == "ok"
            and db.normalize_to_usd(result.price, result.currency) is not None
        )
        progress_callback(received_count, expected_count, current_market)

    def accept_completed_adapter(completed: dict, provider_group: str) -> None:
        adapter = completed["adapter"]
        adapter_results = completed["results"]
        all_results.extend(adapter_results)
        received = sum(
            result.fetch_status == "ok" and db.normalize_to_usd(result.price, result.currency) is not None
            for result in adapter_results
        )
        missing = sum(result.fetch_status == "missing" for result in adapter_results)
        errors = len(adapter_results) - received - missing
        update_steps.append(
            {
                "step": adapter.name,
                "received": received,
                "missing": missing,
                "errors": errors,
                "duration_seconds": round(completed["duration_seconds"], 3),
                "elapsed_seconds": round(time.perf_counter() - run_started, 3),
                "provider_group": provider_group,
            }
        )
        report_progress(f"Completed {adapter.name}")
        if progress_callback is not None:
            time.sleep(0.03)

    report_progress("Updating HaloSkins baseline")
    baseline_entries = [entry for entry in enabled_adapters if entry[1].name == BASELINE_MARKETPLACE]
    remaining_entries = [entry for entry in enabled_adapters if entry[1].name != BASELINE_MARKETPLACE]
    for completed in fetch_adapter_group([adapter for _, adapter in baseline_entries], items):
        accept_completed_adapter(completed, "Baseline")

    grouped_adapters: dict[str, list] = {}
    for key, adapter in remaining_entries:
        grouped_adapters.setdefault(adapter_provider_group(key), []).append(adapter)

    report_progress(f"Updating {len(grouped_adapters)} provider groups in parallel")
    worker_limit = max(1, int(os.getenv("PRICE_UPDATE_MAX_WORKERS", "12")))
    worker_count = min(worker_limit, max(1, len(grouped_adapters)))
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="price-provider") as executor:
        futures = {
            executor.submit(fetch_adapter_group, group_adapters, items): group_name
            for group_name, group_adapters in grouped_adapters.items()
        }
        for future in as_completed(futures):
            group_name = futures[future]
            for completed in future.result():
                accept_completed_adapter(completed, group_name)

    report_progress("Applying fallback prices")
    backup_started = time.perf_counter()
    backup_budget = max(0.0, float(os.getenv("PRICE_BACKUP_BUDGET_SECONDS", "480")))
    before_backup = _successful_result_count(all_results)
    all_results = apply_backup_prices(
        all_results,
        items,
        deadline=time.monotonic() + backup_budget,
    )
    after_backup = _successful_result_count(all_results)
    update_steps.append(
        {
            "step": "Fallback recovery",
            "received": after_backup - before_backup,
            "missing": None,
            "errors": None,
            "duration_seconds": round(time.perf_counter() - backup_started, 3),
            "elapsed_seconds": round(time.perf_counter() - run_started, 3),
            "provider_group": "Fallback",
        }
    )
    report_progress("Fallback recovery completed")

    success_count = _successful_result_count(all_results)
    success_rate = success_count / expected_count
    if success_rate < db.MIN_SNAPSHOT_SUCCESS_RATE:
        raise SnapshotQualityError(
            f"Update aborted: only {success_count}/{expected_count} prices "
            f"({success_rate:.0%}) were received. Previous successful data is still displayed."
        )

    report_progress("Applying 24-hour price carry-forward")
    carry_forward_started = time.perf_counter()
    all_results = _carry_forward_recent_prices(all_results)
    update_steps.append(
        {
            "step": "24-hour price carry-forward",
            "received": _successful_result_count(all_results),
            "missing": None,
            "errors": None,
            "duration_seconds": round(time.perf_counter() - carry_forward_started, 3),
            "elapsed_seconds": round(time.perf_counter() - run_started, 3),
            "provider_group": "Database",
        }
    )
    report_progress("24-hour price carry-forward completed")
    missing_baseline = missing_baseline_items(all_results, items)
    if missing_baseline:
        preview = ", ".join(missing_baseline[:3])
        suffix = f", +{len(missing_baseline) - 3} more" if len(missing_baseline) > 3 else ""
        raise SnapshotQualityError(
            "Update aborted: HaloSkins remains incomplete after the 24-hour carry-forward "
            f"({len(missing_baseline)}/{len(items)} missing: {preview}{suffix}). "
            "Previous successful data is still displayed."
        )

    recordable_results, skipped_marketplaces = filter_recordable_marketplaces(all_results, len(items))
    if not recordable_results:
        raise SnapshotQualityError(
            "Update aborted: every marketplace had too many missing/error rows. "
            "Previous successful data is still displayed."
        )

    report_progress("Saving snapshot")
    save_started = time.perf_counter()
    snapshot_id, timestamp = db.save_snapshot_results(recordable_results)
    update_steps.append(
        {
            "step": "Save snapshot",
            "received": len(recordable_results),
            "missing": None,
            "errors": None,
            "duration_seconds": round(time.perf_counter() - save_started, 3),
            "elapsed_seconds": round(time.perf_counter() - run_started, 3),
            "provider_group": "Database",
        }
    )
    report_progress(f"Snapshot #{snapshot_id} saved")
    if skipped_marketplaces:
        skipped = set(skipped_marketplaces)
        db.update_marketplace_statuses_from_results(
            [result for result in all_results if result.marketplace in skipped],
            timestamp,
        )
    return snapshot_id, timestamp, success_rate


def _successful_result_count(results: list[PriceResult]) -> int:
    return sum(
        1
        for result in results
        if result.fetch_status == "ok" and db.normalize_to_usd(result.price, result.currency) is not None
    )


def _carry_forward_recent_prices(results: list[PriceResult]) -> list[PriceResult]:
    carry_forward = getattr(db, "carry_forward_recent_prices", None)
    if carry_forward is None:
        importlib.invalidate_caches()
        carry_forward = getattr(importlib.reload(db), "carry_forward_recent_prices")
    return carry_forward(results)


def missing_baseline_items(results: list[PriceResult], items) -> list[str]:
    available = {
        result.market_hash_name
        for result in results
        if result.marketplace == BASELINE_MARKETPLACE
        and result.fetch_status == "ok"
        and db.normalize_to_usd(result.price, result.currency) is not None
    }
    return [item.market_hash_name for item in items if item.market_hash_name not in available]


def filter_recordable_marketplaces(
    results: list[PriceResult],
    item_count: int,
) -> tuple[list[PriceResult], list[str]]:
    if item_count <= 0:
        return results, []

    grouped: dict[str, list[PriceResult]] = {}
    for result in results:
        grouped.setdefault(result.marketplace, []).append(result)

    kept: list[PriceResult] = []
    skipped: list[str] = []
    for marketplace, marketplace_results in grouped.items():
        ok_count = _successful_result_count(marketplace_results)
        error_count = sum(1 for result in marketplace_results if result.fetch_status == "error")
        error_rate = error_count / max(item_count, len(marketplace_results), 1)
        if (
            marketplace != BASELINE_MARKETPLACE
            and ok_count == 0
            and error_rate >= db.MAX_MARKETPLACE_FAILURE_RATE
        ):
            skipped.append(marketplace)
            continue
        kept.extend(marketplace_results)
    return kept, skipped
