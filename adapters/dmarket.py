from __future__ import annotations

import os
import time
from typing import Iterable
from urllib.parse import quote, urlencode

import requests
from nacl.signing import SigningKey

from .base import BasketItem, PriceResult


class DMarketAdapter:
    key = "dmarket"
    name = "DMarket"
    requires_credentials = True

    def credentials_configured(self) -> bool:
        return bool(_public_key() and _secret_key_seed())

    def fetch_prices(self, items: Iterable[BasketItem]) -> list[PriceResult]:
        item_list = list(items)
        if not self.credentials_configured():
            return [
                _error(
                    item,
                    "DMARKET_PUBLIC_KEY and DMARKET_SECRET_KEY are required for marketplace-api/v2/offers.",
                )
                for item in item_list
            ]

        results: list[PriceResult] = []
        for item in item_list:
            try:
                listing = _find_lowest_exact_listing(
                    _dmarket_title(item.market_hash_name),
                )
                price_cents = _int_or_none(listing.get("priceCents") if listing else None)
                results.append(
                    PriceResult(
                        marketplace=self.name,
                        market_hash_name=item.market_hash_name,
                        price=price_cents / 100.0 if price_cents is not None else None,
                        currency="USD",
                        stock_count=1 if price_cents is not None else 0,
                        fetch_status="ok" if price_cents is not None else "missing",
                        error_details=None if price_cents is not None else "DMarket v2 returned no exact lowest offer.",
                    )
                )
            except Exception as exc:
                results.append(_error(item, str(exc)))
        return results


def _find_lowest_exact_listing(title: str):
    params = {
        "gameId": os.getenv("DMARKET_GAME_ID", "a8db"),
        "title": title,
        "limit": os.getenv("DMARKET_PAGE_LIMIT", "100"),
        "orderBy": "price",
        "orderDir": "asc",
    }
    max_pages = max(1, _int_or_none(os.getenv("DMARKET_MAX_PAGES_PER_ITEM")) or 3)
    for _ in range(max_pages):
        body = _signed_get(_path(), params)
        for listing in body.get("items") or []:
            if isinstance(listing, dict) and _listing_title(listing) == title:
                return listing
        cursor = body.get("cursor")
        if not cursor:
            return None
        params["cursor"] = str(cursor)
    return None


def _signed_get(path: str, params: dict[str, str]) -> dict:
    query = urlencode(params, quote_via=quote)
    route = f"{path}?{query}"
    timestamp = str(int(time.time()))
    signature_payload = f"GET{route}{timestamp}"
    signature = SigningKey(_secret_key_seed()).sign(signature_payload.encode()).signature.hex()
    response = requests.get(
        _base_url() + route,
        headers={
            "X-Api-Key": _public_key(),
            "X-Sign-Date": timestamp,
            "X-Request-Sign": f"dmar ed25519 {signature}",
            "User-Agent": os.getenv(
                "DMARKET_USER_AGENT",
                "Mozilla/5.0 (compatible; local-cs2-basket-tool/1.0)",
            ),
        },
        timeout=float(os.getenv("DMARKET_TIMEOUT_SECONDS", "20")),
    )
    response.raise_for_status()
    body = response.json()
    return body if isinstance(body, dict) else {}


def _listing_title(listing: dict) -> str | None:
    title = listing.get("title")
    if title:
        return str(title)
    attributes = listing.get("attributes")
    if isinstance(attributes, dict):
        value = attributes.get("title")
        return str(value) if value else None
    return None


def _dmarket_title(title: str) -> str:
    if title.startswith("бя "):
        return "★ " + title[3:]
    return title


def _public_key() -> str:
    return os.getenv("DMARKET_PUBLIC_KEY", "").strip().lower()


def _secret_key_seed() -> bytes:
    secret = os.getenv("DMARKET_SECRET_KEY", "").strip()
    try:
        secret_bytes = bytes.fromhex(secret)
    except ValueError:
        return b""
    if len(secret_bytes) >= 32:
        return secret_bytes[:32]
    return b""


def _base_url() -> str:
    return os.getenv("DMARKET_BASE_URL", "https://api.dmarket.com").rstrip("/")


def _path() -> str:
    return os.getenv("DMARKET_OFFERS_PATH", "/marketplace-api/v2/offers")


def _int_or_none(value) -> int | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _error(item: BasketItem, message: str) -> PriceResult:
    return PriceResult(
        marketplace="DMarket",
        market_hash_name=item.market_hash_name,
        price=None,
        currency="USD",
        fetch_status="error",
        error_details=message,
    )
