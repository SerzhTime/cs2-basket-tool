from __future__ import annotations

import os
import time
from threading import Lock

import requests


class ExchangeRateError(RuntimeError):
    pass


DEFAULT_FALLBACK_USD_LATEST_URL = "https://api.frankfurter.dev/v1/latest?base=USD&symbols=CNY"
_RATE_CACHE: tuple[float, float, str] | None = None
_RATE_CACHE_LOCK = Lock()


def fetch_cny_to_usd_rate() -> float:
    """Return USD -> CNY conversion as CNY -> USD, without one provider blocking C5.

    C5 prices must never be treated as USD when conversion is unavailable. A
    current primary/fallback rate is preferred, then a recently fetched rate
    from this app process. The cache is deliberately bounded to prevent stale
    FX data from silently becoming permanent carry-forward data.
    """
    timeout = float(os.getenv("EXCHANGERATE_TIMEOUT_SECONDS", "15"))
    primary_url = os.getenv("EXCHANGERATE_USD_LATEST_URL", "").strip()
    fallback_url = os.getenv(
        "EXCHANGERATE_FALLBACK_USD_LATEST_URL", DEFAULT_FALLBACK_USD_LATEST_URL
    ).strip()
    urls = [("primary", primary_url), ("fallback", fallback_url)]
    errors: list[str] = []
    attempted_urls: set[str] = set()

    for source, url in urls:
        if not url or url in attempted_urls:
            continue
        attempted_urls.add(url)
        try:
            response = requests.get(url, timeout=timeout)
            response.raise_for_status()
            rate = _extract_cny_to_usd_rate(response.json())
        except Exception as exc:
            errors.append(f"{source}: {exc}")
            continue
        _save_cached_rate(rate, source)
        return rate

    cached = _cached_rate_if_fresh()
    if cached is not None:
        return cached

    detail = "; ".join(errors) or "no exchange-rate URL is configured"
    raise ExchangeRateError(f"USD/CNY conversion failed ({detail}).")


def _extract_cny_to_usd_rate(body: object) -> float:
    if not isinstance(body, dict):
        raise ExchangeRateError("Exchange-rate API response was not an object.")
    if body.get("result") and body.get("result") != "success":
        raise ExchangeRateError(str(body.get("error-type") or body.get("result")))

    # ExchangeRate-API: conversion_rates.CNY. Frankfurter: rates.CNY.
    rates = body.get("conversion_rates") or body.get("rates") or {}
    usd_to_cny = _float_or_none(rates.get("CNY") if isinstance(rates, dict) else None)
    if usd_to_cny is None or usd_to_cny <= 0:
        raise ExchangeRateError("Exchange-rate API response did not include a valid CNY rate.")
    return 1 / usd_to_cny


def _save_cached_rate(rate: float, source: str) -> None:
    global _RATE_CACHE
    with _RATE_CACHE_LOCK:
        _RATE_CACHE = (rate, time.monotonic(), source)


def _cached_rate_if_fresh() -> float | None:
    cache_seconds = max(0.0, float(os.getenv("EXCHANGERATE_CACHE_SECONDS", "21600")))
    with _RATE_CACHE_LOCK:
        cached = _RATE_CACHE
    if cached is None:
        return None
    rate, fetched_at, _source = cached
    return rate if time.monotonic() - fetched_at <= cache_seconds else None


def _clear_rate_cache() -> None:
    """Test helper: clear the process-local FX cache."""
    global _RATE_CACHE
    with _RATE_CACHE_LOCK:
        _RATE_CACHE = None


def _float_or_none(value) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
