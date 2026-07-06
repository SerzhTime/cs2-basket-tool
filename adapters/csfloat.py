from __future__ import annotations

import os
from typing import Iterable

import requests

from .base import BasketItem, PriceResult


class CSFloatAdapter:
    key = "csfloat"
    name = "CSFloat"
    requires_credentials = True

    def credentials_configured(self) -> bool:
        return bool(os.getenv("CSFLOAT_API_KEY"))

    def fetch_prices(self, items: Iterable[BasketItem]) -> list[PriceResult]:
        item_list = list(items)
        api_key = os.getenv("CSFLOAT_API_KEY")
        if not api_key:
            return [_error(item, "CSFLOAT_API_KEY is not configured.") for item in item_list]

        results: list[PriceResult] = []
        headers = {"Authorization": api_key}
        timeout = float(os.getenv("CSFLOAT_TIMEOUT_SECONDS", "20"))
        limit = int(os.getenv("CSFLOAT_LIMIT", "5"))
        for item in item_list:
            try:
                response = requests.get(
                    os.getenv("CSFLOAT_LISTINGS_URL", "https://csfloat.com/api/v1/listings"),
                    params={
                        "limit": limit,
                        "sort_by": "lowest_price",
                        "type": "buy_now",
                        "market_hash_name": item.market_hash_name,
                    },
                    headers=headers,
                    timeout=timeout,
                )
                response.raise_for_status()
                body = response.json()
                listings = body.get("data") if isinstance(body, dict) else body
                listing = _first_exact_listing(listings or [], item.market_hash_name)
                price_cents = _float_or_none(listing.get("price") if listing else None)
                stock_count = len(listings or []) if isinstance(listings, list) else None
                results.append(
                    PriceResult(
                        marketplace=self.name,
                        market_hash_name=item.market_hash_name,
                        price=price_cents / 100.0 if price_cents is not None else None,
                        currency="USD",
                        stock_count=stock_count,
                        fetch_status="ok" if price_cents is not None else "missing",
                        error_details=None if price_cents is not None else "CSFloat returned no exact listing.",
                    )
                )
            except Exception as exc:
                results.append(_error(item, str(exc)))
        return results


def _first_exact_listing(listings, market_hash_name: str):
    if not isinstance(listings, list):
        return None
    for listing in listings:
        item = listing.get("item") if isinstance(listing, dict) else None
        if (
            isinstance(item, dict)
            and item.get("market_hash_name") == market_hash_name
            and listing.get("type") == "buy_now"
            and listing.get("state") == "listed"
        ):
            return listing
    return None


def _float_or_none(value) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _error(item: BasketItem, message: str) -> PriceResult:
    return PriceResult(
        marketplace="CSFloat",
        market_hash_name=item.market_hash_name,
        price=None,
        currency="USD",
        fetch_status="error",
        error_details=message,
    )
