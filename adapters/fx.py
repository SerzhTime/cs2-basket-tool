from __future__ import annotations

import os

import requests


class ExchangeRateError(RuntimeError):
    pass


def fetch_cny_to_usd_rate() -> float:
    url = os.getenv("EXCHANGERATE_USD_LATEST_URL")
    if not url:
        raise ExchangeRateError("EXCHANGERATE_USD_LATEST_URL is not configured.")

    try:
        response = requests.get(url, timeout=float(os.getenv("EXCHANGERATE_TIMEOUT_SECONDS", "15")))
        response.raise_for_status()
        body = response.json()
    except Exception as exc:
        raise ExchangeRateError(f"ExchangeRate API request failed: {exc}") from exc

    if body.get("result") and body.get("result") != "success":
        error = body.get("error-type") or body.get("result")
        raise ExchangeRateError(f"ExchangeRate API returned {error}.")

    rates = body.get("conversion_rates") or {}
    usd_to_cny = _float_or_none(rates.get("CNY"))
    if usd_to_cny is None or usd_to_cny <= 0:
        raise ExchangeRateError("ExchangeRate API response did not include a valid CNY rate.")

    return 1 / usd_to_cny


def _float_or_none(value) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
