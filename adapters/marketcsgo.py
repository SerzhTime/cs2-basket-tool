from __future__ import annotations

import os
from typing import Iterable

import requests

from .base import BasketItem, PriceResult


class MarketCSGOAdapter:
    key = "marketcsgo"
    name = "Market.CSGO"
    requires_credentials = False

    def credentials_configured(self) -> bool:
        return True

    def fetch_prices(self, items: Iterable[BasketItem]) -> list[PriceResult]:
        item_list = list(items)
        currency = os.getenv("MARKETCSGO_CURRENCY", "USD").upper()
        try:
            response = requests.get(
                os.getenv("MARKETCSGO_PRICES_URL", f"https://market.csgo.com/api/v2/prices/{currency}.json"),
                timeout=float(os.getenv("MARKETCSGO_TIMEOUT_SECONDS", "40")),
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            return [_error(item, str(exc), currency) for item in item_list]

        if body.get("success") is False:
            message = body.get("error") or body.get("message") or "Market.CSGO returned success=false."
            return [_error(item, message, currency) for item in item_list]

        by_name = {
            row.get("market_hash_name"): row
            for row in body.get("items", [])
            if isinstance(row, dict) and row.get("market_hash_name")
        }

        results = []
        for item in item_list:
            row = by_name.get(item.market_hash_name) or {}
            price = _float_or_none(row.get("price"))
            results.append(
                PriceResult(
                    marketplace=self.name,
                    market_hash_name=item.market_hash_name,
                    price=price,
                    currency=currency,
                    stock_count=_int_or_none(row.get("volume")),
                    fetch_status="ok" if price is not None else "missing",
                    error_details=None if price is not None else "Market.CSGO did not return this exact market_hash_name.",
                )
            )
        return results


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


def _error(item: BasketItem, message: str, currency: str) -> PriceResult:
    return PriceResult(
        marketplace="Market.CSGO",
        market_hash_name=item.market_hash_name,
        price=None,
        currency=currency,
        fetch_status="error",
        error_details=message,
    )
