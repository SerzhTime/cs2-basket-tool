from __future__ import annotations

import os
from typing import Iterable

import requests

from .base import BasketItem, PriceResult


OPEN_SKIN_MARKETS = {
    "openskin_skinport": ("Skinport", "skinport"),
    "openskin_buff163": ("Buff163", "buff"),
    "openskin_youpin": ("YouPin", "youpin"),
    "openskin_steam": ("Steam", "steam"),
}

_BATCH_CACHE: dict[tuple[str, ...], dict] = {}


class OpenSkinAdapter:
    requires_credentials = False

    def __init__(self, key: str, name: str, source: str):
        self.key = key
        self.name = name
        self.source = source

    def credentials_configured(self) -> bool:
        return True

    def fetch_prices(self, items: Iterable[BasketItem]) -> list[PriceResult]:
        item_list = list(items)
        try:
            data = _fetch_batch(item_list)
        except Exception as exc:
            return [_error(self.name, item, f"OpenSkin batch request failed: {exc}") for item in item_list]

        results: list[PriceResult] = []
        for item in item_list:
            entry = data.get(item.market_hash_name)
            source_entry = entry.get(self.source) if isinstance(entry, dict) else None
            price = _float_or_none(source_entry.get("ask") if isinstance(source_entry, dict) else None)
            stock_count = _stock_count(source_entry if isinstance(source_entry, dict) else {})
            results.append(
                PriceResult(
                    marketplace=self.name,
                    market_hash_name=item.market_hash_name,
                    price=price,
                    currency="USD",
                    stock_count=stock_count,
                    fetch_status="ok" if price is not None else "missing",
                    error_details=None if price is not None else f"OpenSkin returned no {self.name} ask price.",
                )
            )
        return results


def build_openskin_adapters() -> list[OpenSkinAdapter]:
    return [OpenSkinAdapter(key, name, source) for key, (name, source) in OPEN_SKIN_MARKETS.items()]


def clear_openskin_cache() -> None:
    _BATCH_CACHE.clear()


def _fetch_batch(items: list[BasketItem]) -> dict:
    names = tuple(item.market_hash_name for item in items)
    if names in _BATCH_CACHE:
        return _BATCH_CACHE[names]

    response = requests.post(
        os.getenv("OPENSKIN_BATCH_URL", "https://api.openskin.dev/v1/prices/batch"),
        json={"items": list(names)},
        headers={"User-Agent": os.getenv("OPENSKIN_USER_AGENT", "local-cs2-basket-tool/1.0")},
        timeout=float(os.getenv("OPENSKIN_TIMEOUT_SECONDS", "40")),
    )
    response.raise_for_status()
    body = response.json()
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError("OpenSkin response did not include a data object.")
    _BATCH_CACHE[names] = data
    return data


def _stock_count(source_entry: dict) -> int | None:
    for key in ("ask_volume", "sell_order_count"):
        value = source_entry.get(key)
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            continue
    return None


def _float_or_none(value) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _error(marketplace: str, item: BasketItem, message: str) -> PriceResult:
    return PriceResult(
        marketplace=marketplace,
        market_hash_name=item.market_hash_name,
        price=None,
        currency="USD",
        fetch_status="error",
        error_details=message,
    )
