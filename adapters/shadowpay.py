from __future__ import annotations

import os
from typing import Iterable

import requests

from .base import BasketItem, PriceResult


class ShadowPayAdapter:
    key = "csgoskins_shadowpay"
    name = "ShadowPay"
    requires_credentials = True

    def credentials_configured(self) -> bool:
        return bool(os.getenv("SHADOWPAY_API_TOKEN"))

    def fetch_prices(self, items: Iterable[BasketItem]) -> list[PriceResult]:
        item_list = list(items)
        token = os.getenv("SHADOWPAY_API_TOKEN")
        if not token:
            return [
                _error(
                    item,
                    "SHADOWPAY_API_TOKEN is not configured. The Steam web access token is not accepted by the price endpoint.",
                )
                for item in item_list
            ]

        try:
            response = requests.get(
                os.getenv(
                    "SHADOWPAY_PRICES_URL",
                    "https://api.shadowpay.com/api/v2/merchant/items/prices",
                ),
                params={"project": os.getenv("SHADOWPAY_PROJECT", "csgo")},
                headers={"Authorization": f"Bearer {token}"},
                timeout=float(os.getenv("SHADOWPAY_TIMEOUT_SECONDS", "40")),
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            return [_error(item, str(exc)) for item in item_list]

        if body.get("status") == "error":
            message = body.get("error") or body.get("error_message") or "ShadowPay API returned status=error."
            return [_error(item, str(message)) for item in item_list]

        by_name = _lowest_prices_by_name(body.get("data") or [])
        results: list[PriceResult] = []
        for item in item_list:
            row = by_name.get(item.market_hash_name) or {}
            price = _float_or_none(row.get("price"))
            results.append(
                PriceResult(
                    marketplace=self.name,
                    market_hash_name=item.market_hash_name,
                    price=price,
                    currency="USD",
                    stock_count=_int_or_none(row.get("volume")),
                    fetch_status="ok" if price is not None else "missing",
                    error_details=None if price is not None else "ShadowPay returned no exact item price.",
                )
            )
        return results


def _lowest_prices_by_name(rows) -> dict[str, dict]:
    prices: dict[str, dict] = {}
    if not isinstance(rows, list):
        return prices
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("steam_market_hash_name")
        price = _float_or_none(row.get("price"))
        if not name or price is None:
            continue
        current = prices.get(name)
        if current is None or price < float(current["price"]):
            prices[name] = row
    return prices


def _float_or_none(value) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value) -> int | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _error(item: BasketItem, message: str) -> PriceResult:
    return PriceResult(
        marketplace="ShadowPay",
        market_hash_name=item.market_hash_name,
        price=None,
        currency="USD",
        fetch_status="error",
        error_details=message,
    )
