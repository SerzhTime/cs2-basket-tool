from __future__ import annotations

import os
from typing import Iterable

import requests

from .base import BasketItem, PriceResult


class WaxpeerAdapter:
    key = "waxpeer"
    name = "Waxpeer"
    requires_credentials = True

    def credentials_configured(self) -> bool:
        return bool(os.getenv("WAXPEER_API_KEY"))

    def fetch_prices(self, items: Iterable[BasketItem]) -> list[PriceResult]:
        item_list = list(items)
        api_key = os.getenv("WAXPEER_API_KEY")
        if not api_key:
            return [_error(item, "WAXPEER_API_KEY is not configured.") for item in item_list]

        try:
            response = requests.get(
                os.getenv("WAXPEER_PRICES_URL", "https://api.waxpeer.com/v1/prices"),
                params={"game": os.getenv("WAXPEER_GAME", "csgo"), "key": api_key},
                timeout=float(os.getenv("WAXPEER_TIMEOUT_SECONDS", "30")),
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            return [_error(item, str(exc)) for item in item_list]

        if body.get("success") is False:
            message = body.get("msg") or body.get("message") or "Waxpeer API returned success=false."
            return [_error(item, message) for item in item_list]

        by_name = _items_by_name(body.get("items") or body.get("data") or [])
        price_divisor = float(os.getenv("WAXPEER_PRICE_DIVISOR", "1000"))
        results = []
        for item in item_list:
            row = by_name.get(item.market_hash_name) or {}
            price_cents = _float_or_none(row.get("min"))
            results.append(
                PriceResult(
                    marketplace=self.name,
                    market_hash_name=item.market_hash_name,
                    price=price_cents / price_divisor if price_cents is not None else None,
                    currency=os.getenv("WAXPEER_CURRENCY", "USD").upper(),
                    stock_count=_int_or_none(row.get("count")),
                    fetch_status="ok" if price_cents is not None else "missing",
                    error_details=None if price_cents is not None else "Waxpeer did not return this exact item name.",
                )
            )
        return results


def _items_by_name(items) -> dict[str, dict]:
    if isinstance(items, dict):
        return {str(name): row for name, row in items.items() if isinstance(row, dict)}
    if isinstance(items, list):
        return {row.get("name"): row for row in items if isinstance(row, dict) and row.get("name")}
    return {}


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
        marketplace="Waxpeer",
        market_hash_name=item.market_hash_name,
        price=None,
        currency=os.getenv("WAXPEER_CURRENCY", "USD").upper(),
        fetch_status="error",
        error_details=message,
    )
