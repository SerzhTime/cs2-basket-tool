from __future__ import annotations

import os
from typing import Iterable

import requests

from .base import BasketItem, PriceResult


class HaloSkinsAdapter:
    key = "haloskins"
    name = "HaloSkins"
    requires_credentials = True

    def credentials_configured(self) -> bool:
        return bool(_url())

    def fetch_prices(self, items: Iterable[BasketItem]) -> list[PriceResult]:
        item_list = list(items)
        url = _url()
        if not url:
            return [
                PriceResult(
                    marketplace=self.name,
                    market_hash_name=item.market_hash_name,
                    price=None,
                    currency=_currency(),
                    fetch_status="error",
                    error_details="HALOSKINS_LOWEST_PRICE_URL or HALOSKINS_API_KEY is not configured.",
                )
                for item in item_list
            ]

        try:
            response = requests.get(
                url,
                headers={
                    "User-Agent": os.getenv(
                        "HALOSKINS_USER_AGENT",
                        "Mozilla/5.0 (compatible; local-cs2-basket-tool/1.0)",
                    )
                },
                timeout=float(os.getenv("HALOSKINS_TIMEOUT_SECONDS", "30")),
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            return [
                PriceResult(
                    marketplace=self.name,
                    market_hash_name=item.market_hash_name,
                    price=None,
                    currency=_currency(),
                    fetch_status="error",
                    error_details=str(exc),
                )
                for item in item_list
            ]

        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            return [
                PriceResult(
                    marketplace=self.name,
                    market_hash_name=item.market_hash_name,
                    price=None,
                    currency=_currency(),
                    fetch_status="error",
                    error_details="HaloSkins response did not contain a data list.",
                )
                for item in item_list
            ]

        by_name = {row.get("market_hash_name"): row for row in data if isinstance(row, dict)}
        results = []
        for item in item_list:
            row = by_name.get(item.market_hash_name) or {}
            price = _float_or_none(row.get("lowest_price"))
            quantity = _int_or_none(row.get("quantity"))
            results.append(
                PriceResult(
                    marketplace=self.name,
                    market_hash_name=item.market_hash_name,
                    price=price,
                    currency=_currency(),
                    stock_count=quantity,
                    fetch_status="ok" if price is not None else "missing",
                    error_details=None if price is not None else "HaloSkins did not return this exact market_hash_name.",
                )
            )
        return results


def _url() -> str | None:
    configured = os.getenv("HALOSKINS_LOWEST_PRICE_URL") or os.getenv("HALOSKINS_API_URL")
    if configured:
        return configured
    key = os.getenv("HALOSKINS_API_KEY")
    if key:
        return f"https://api.haloskins.com/steam-trade-center/sale/data/list?key={key}"
    return None


def _currency() -> str:
    return os.getenv("HALOSKINS_CURRENCY", "USD").upper()


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


__all__ = ["HaloSkinsAdapter"]
