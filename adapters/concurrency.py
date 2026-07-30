from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Lock
import time
from typing import Callable, TypeVar


T = TypeVar("T")
R = TypeVar("R")


class RequestRateLimiter:
    """Thread-safe request-start limiter for a single adapter update."""

    def __init__(self, requests_per_second: float) -> None:
        self._interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self._lock = Lock()
        self._next_allowed = 0.0

    def wait(self) -> None:
        if not self._interval:
            return
        with self._lock:
            now = time.monotonic()
            target = max(now, self._next_allowed)
            self._next_allowed = target + self._interval
        if target > now:
            time.sleep(target - now)


def map_concurrently(items: list[T], workers: int, fetch: Callable[[T], R]) -> list[R]:
    """Fetch independent items concurrently while preserving basket order."""
    if workers <= 1 or len(items) <= 1:
        return [fetch(item) for item in items]
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="market-item") as executor:
        return list(executor.map(fetch, items))
