from __future__ import annotations

import os
from typing import Iterable

import requests

from .base import BasketItem, PriceResult
from .fx import ExchangeRateError, fetch_cny_to_usd_rate


class C5GameAdapter:
    key = "c5game"
    name = "C5Game"
    requires_credentials = True

    def credentials_configured(self) -> bool:
        return bool(_app_key() and os.getenv("EXCHANGERATE_USD_LATEST_URL"))

    def fetch_prices(self, items: Iterable[BasketItem]) -> list[PriceResult]:
        item_list = list(items)
        if not _app_key():
            return [
                PriceResult(
                    marketplace=self.name,
                    market_hash_name=item.market_hash_name,
                    price=None,
                    currency="USD",
                    fetch_status="error",
                    error_details="C5GAME_APP_KEY is not configured.",
                )
                for item in item_list
            ]

        try:
            cny_to_usd = fetch_cny_to_usd_rate()
        except ExchangeRateError as exc:
            return [
                PriceResult(
                    marketplace=self.name,
                    market_hash_name=item.market_hash_name,
                    price=None,
                    currency="USD",
                    fetch_status="error",
                    error_details=str(exc),
                )
                for item in item_list
            ]

        url = os.getenv("C5GAME_API_URL", "https://openapi.c5game.com/merchant/product/price/batch")
        params = {"app-key": _app_key()}
        payload = {
            "appId": int(os.getenv("C5GAME_APP_ID", "730")),
            "marketHashNames": [item.market_hash_name for item in item_list],
        }
        headers = {
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip, br, zstd, deflate",
        }

        try:
            response = requests.post(
                url,
                params=params,
                json=payload,
                headers=headers,
                timeout=float(os.getenv("C5GAME_TIMEOUT_SECONDS", "20")),
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            return [
                PriceResult(
                    marketplace=self.name,
                    market_hash_name=item.market_hash_name,
                    price=None,
                    currency="USD",
                    fetch_status="error",
                    error_details=str(exc),
                )
                for item in item_list
            ]

        if body.get("success") is False:
            error = body.get("errorMsg") or body.get("message") or "C5Game API returned success=false."
            return [
                PriceResult(
                    marketplace=self.name,
                    market_hash_name=item.market_hash_name,
                    price=None,
                    currency="USD",
                    fetch_status="error",
                    error_details=error,
                )
                for item in item_list
            ]

        data = body.get("data") or {}
        results = []
        for item in item_list:
            row = data.get(item.market_hash_name) or {}
            price = _float_or_none(row.get("price"))
            count = _int_or_none(row.get("count"))
            usd_price = price * cny_to_usd if price is not None else None
            results.append(
                PriceResult(
                    marketplace=self.name,
                    market_hash_name=item.market_hash_name,
                    price=usd_price,
                    currency="USD",
                    stock_count=count,
                    fetch_status="ok" if usd_price is not None else "missing",
                    error_details=None if price is not None else "C5Game did not return this exact marketHashName.",
                )
            )
        return results


def _app_key() -> str | None:
    return os.getenv("C5GAME_APP_KEY") or os.getenv("C5GAME_API_KEY")


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
