from __future__ import annotations

import base64
import os
from typing import Iterable

import requests

from .base import BasketItem, PriceResult


class SkinportAdapter:
    key = "openskin_skinport"
    name = "Skinport"
    requires_credentials = True

    def credentials_configured(self) -> bool:
        return bool(os.getenv("SKINPORT_CLIENT_ID")) and bool(os.getenv("SKINPORT_CLIENT_SECRET"))

    def fetch_prices(self, items: Iterable[BasketItem]) -> list[PriceResult]:
        item_list = list(items)
        if not self.credentials_configured():
            return [
                PriceResult(
                    marketplace=self.name,
                    market_hash_name=item.market_hash_name,
                    price=None,
                    currency="USD",
                    fetch_status="error",
                    error_details="SKINPORT_CLIENT_ID or SKINPORT_CLIENT_SECRET is not configured.",
                )
                for item in item_list
            ]

        try:
            rows = _fetch_skinport_items()
        except Exception as exc:
            return [
                PriceResult(
                    marketplace=self.name,
                    market_hash_name=item.market_hash_name,
                    price=None,
                    currency="USD",
                    fetch_status="error",
                    error_details=f"Skinport request failed: {exc}",
                )
                for item in item_list
            ]

        by_name = _lowest_rows_by_name(rows)
        results: list[PriceResult] = []
        for item in item_list:
            row = by_name.get(item.market_hash_name)
            price = _float_or_none(row.get("min_price") if row else None)
            quantity = _int_or_none(row.get("quantity") if row else None)
            currency = (row.get("currency") if row else None) or os.getenv("SKINPORT_CURRENCY", "USD")
            results.append(
                PriceResult(
                    marketplace=self.name,
                    market_hash_name=item.market_hash_name,
                    price=price,
                    currency=currency,
                    stock_count=quantity,
                    fetch_status="ok" if price is not None else "missing",
                    error_details=None if price is not None else "Skinport returned no min_price for this item.",
                )
            )
        return results


_CACHE: list[dict] | None = None


def _fetch_skinport_items() -> list[dict]:
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    response = requests.get(
        os.getenv("SKINPORT_ITEMS_URL", "https://api.skinport.com/v1/items"),
        params={
            "app_id": int(os.getenv("SKINPORT_APP_ID", "730")),
            "currency": os.getenv("SKINPORT_CURRENCY", "USD"),
            "tradable": int(os.getenv("SKINPORT_TRADABLE", "1")),
        },
        headers={
            "Authorization": f"Basic {_basic_auth_token()}",
            "Accept": "application/json",
            "Accept-Encoding": "br",
            "User-Agent": os.getenv("SKINPORT_USER_AGENT", "local-cs2-basket-tool/1.0"),
        },
        timeout=float(os.getenv("SKINPORT_TIMEOUT_SECONDS", "30")),
    )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, list):
        raise RuntimeError("Skinport response was not a list.")
    _CACHE = body
    return body


def _lowest_rows_by_name(rows: list[dict]) -> dict[str, dict]:
    by_name: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("market_hash_name")
        price = _float_or_none(row.get("min_price"))
        if not name or price is None:
            continue
        existing = by_name.get(name)
        existing_price = _float_or_none(existing.get("min_price") if existing else None)
        if existing is None or existing_price is None or price < existing_price:
            by_name[name] = row
    return by_name


def clear_skinport_cache() -> None:
    global _CACHE
    _CACHE = None


def _basic_auth_token() -> str:
    credentials = f"{os.environ['SKINPORT_CLIENT_ID']}:{os.environ['SKINPORT_CLIENT_SECRET']}"
    return base64.b64encode(credentials.encode()).decode()


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
