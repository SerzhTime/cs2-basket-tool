from __future__ import annotations

import os
from typing import Any, Iterable

import requests

from .base import BasketItem, PriceResult
from .concurrency import RequestRateLimiter, map_concurrently


API_URL = "https://api.skindeck.com/secure/market"
GAME_ID = "730"
STAR = "\u2605"


class SkindeckAdapter:
    key = "skindeck"
    name = "Skindeck"
    requires_credentials = True

    def credentials_configured(self) -> bool:
        return bool(os.getenv("SKINDECK_API_KEY"))

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
                    error_details="SKINDECK_API_KEY is not configured.",
                )
                for item in item_list
            ]

        workers = max(1, int(os.getenv("SKINDECK_MAX_WORKERS", "2")))
        delay = max(0.0, float(os.getenv("SKINDECK_DELAY_SECONDS", "0.05")))
        rate_limiter = RequestRateLimiter(1.0 / delay) if delay else None
        return map_concurrently(
            item_list,
            workers,
            lambda item: self._fetch_item_price(item, rate_limiter),
        )

    def _fetch_item_price(self, item: BasketItem, rate_limiter: RequestRateLimiter | None) -> PriceResult:
        try:
            rows = _request_market(item.market_hash_name, rate_limiter)
            exact = _exact_rows(rows, item.market_hash_name, item.market_hash_name)
            if not exact and _has_leading_star(item.market_hash_name):
                search_name = _strip_leading_star(item.market_hash_name)
                rows = _request_market(search_name, rate_limiter)
                exact = _exact_rows(rows, item.market_hash_name, search_name)
        except Exception as exc:
            return PriceResult(
                marketplace=self.name,
                market_hash_name=item.market_hash_name,
                price=None,
                currency="USD",
                fetch_status="error",
                error_details=f"Skindeck request failed: {exc}",
            )

        if not exact:
            return PriceResult(
                marketplace=self.name,
                market_hash_name=item.market_hash_name,
                price=None,
                currency="USD",
                fetch_status="missing",
                error_details="Skindeck returned no exact market_hash_name offer.",
            )

        best = min(exact, key=lambda row: _price_or_inf(_offer_price(row)))
        price = _offer_price(best)
        return PriceResult(
            marketplace=self.name,
            market_hash_name=item.market_hash_name,
            price=price,
            currency="USD",
            stock_count=len(exact),
            fetch_status="ok" if price is not None else "missing",
            error_details=None if price is not None else "Skindeck exact match had no offer.price.",
        )


def _request_market(
    search_name: str,
    rate_limiter: RequestRateLimiter | None = None,
) -> list[dict[str, Any]]:
    if rate_limiter is not None:
        rate_limiter.wait()
    response = requests.get(
        os.getenv("SKINDECK_MARKET_URL", API_URL),
        params={
            "search": search_name,
            "game": os.getenv("SKINDECK_GAME_ID", GAME_ID),
            "perPage": int(os.getenv("SKINDECK_PER_PAGE", "10")),
        },
        headers={
            "api-key": os.environ["SKINDECK_API_KEY"],
            "Accept": "application/json",
            "User-Agent": os.getenv("SKINDECK_USER_AGENT", "local-cs2-basket-tool/1.0"),
        },
        timeout=float(os.getenv("SKINDECK_TIMEOUT_SECONDS", "30")),
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success", False):
        raise RuntimeError(f"Skindeck success=false: {payload}")
    rows = payload.get("items") or []
    if not isinstance(rows, list):
        raise RuntimeError("Skindeck response items field is not a list.")
    return [row for row in rows if isinstance(row, dict)]


def _exact_rows(rows: list[dict[str, Any]], original_name: str, search_name: str) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if _names_match(str(row.get("market_hash_name", "")), original_name, search_name)
        and _offer_price(row) is not None
    ]


def _names_match(candidate: str, original_name: str, search_name: str) -> bool:
    candidate_key = _normalize_name(candidate)
    return candidate_key in {_normalize_name(original_name), _normalize_name(search_name)}


def _normalize_name(value: str) -> str:
    return " ".join(value.casefold().replace(STAR, "").split())


def _has_leading_star(value: str) -> bool:
    return value.lstrip().startswith(STAR)


def _strip_leading_star(value: str) -> str:
    return value.lstrip().removeprefix(STAR).strip()


def _offer_price(row: dict[str, Any]) -> float | None:
    offer = row.get("offer")
    if not isinstance(offer, dict):
        return None
    return _float_or_none(offer.get("price"))


def _float_or_none(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _price_or_inf(value: float | None) -> float:
    return value if value is not None else float("inf")
