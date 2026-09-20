from __future__ import annotations

import os
import re
from dataclasses import dataclass
from threading import Lock
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .concurrency import RequestRateLimiter


BASE_URL = "https://cs2skins.gg"
MARKET_SLUGS = {
    "CS.MONEY": "csmoneym",
    "LIS-SKINS": "lisskins",
    "SkinBaron": "skinbaron",
    "Skins.com": "skins",
    "Exeskins": "exeskins",
    "Avan.market": "avanmarket",
    "Tradeit.gg": "tradeit",
    "SkinPlace": "skinplace",
    "ShadowPay": "shadowpay",
}
SLUG_MARKETS = {slug: market for market, slug in MARKET_SLUGS.items()}
WEARS = {
    "Factory New": "factory-new",
    "Minimal Wear": "minimal-wear",
    "Field-Tested": "field-tested",
    "Well-Worn": "well-worn",
    "Battle-Scarred": "battle-scarred",
}
_CACHE: dict[str, dict[str, "CS2SkinsOffer"] | Exception] = {}
_CACHE_LOCK = Lock()


class NoCS2SkinsOffersError(RuntimeError):
    pass


@dataclass(frozen=True)
class CS2SkinsOffer:
    marketplace: str
    price: float
    stock_count: int | None


def clear_cs2skins_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def cs2skins_offer(
    market_hash_name: str,
    marketplace: str,
    rate_limiter: RequestRateLimiter | None = None,
) -> CS2SkinsOffer | None:
    if os.getenv("CS2SKINS_BACKUP_ENABLED", "1").strip().lower() in {"0", "false", "no"}:
        return None
    slug = MARKET_SLUGS.get(marketplace)
    if slug is None:
        return None
    offers = _load_offers(market_hash_name, rate_limiter)
    return offers.get(slug)


def _load_offers(
    market_hash_name: str,
    rate_limiter: RequestRateLimiter | None,
) -> dict[str, CS2SkinsOffer]:
    with _CACHE_LOCK:
        cached = _CACHE.get(market_hash_name)
    if cached is not None:
        if isinstance(cached, Exception):
            raise cached
        return cached

    try:
        offers: dict[str, CS2SkinsOffer] = {}
        for path, params in _item_routes(market_hash_name):
            response = _get(path, params, rate_limiter)
            parsed = _parse_offers(response.text)
            for slug, offer in parsed.items():
                existing = offers.get(slug)
                if existing is None or offer.price < existing.price:
                    offers[slug] = offer
        if not offers:
            raise NoCS2SkinsOffersError(
                f"CS2Skins returned no supported marketplace offers for {market_hash_name}."
            )
        with _CACHE_LOCK:
            _CACHE[market_hash_name] = offers
        return offers
    except Exception as exc:
        with _CACHE_LOCK:
            _CACHE[market_hash_name] = exc
        raise


def _item_routes(market_hash_name: str) -> list[tuple[str, dict[str, str]]]:
    clean = market_hash_name.removeprefix("StatTrak™ ").removeprefix("Souvenir ").lstrip("★ ")
    wear_match = re.search(r"\s*\(([^)]+)\)\s*$", clean)
    wear = WEARS.get(wear_match.group(1), "") if wear_match else ""
    clean = re.sub(r"\s*\([^)]+\)\s*$", "", clean)
    params: dict[str, str] = {}
    if wear:
        params["wear"] = wear
    if market_hash_name.startswith("StatTrak™"):
        params["stattrak"] = "true"
    if market_hash_name.startswith("Souvenir"):
        params["souvenir"] = "true"

    if " | " in clean:
        weapon, finish = clean.split(" | ", 1)
        weapon_slug = _slugify(weapon)
        finish_slug = _slugify(finish)
        if finish in {"Doppler", "Gamma Doppler"}:
            return [
                (f"/browse/{weapon_slug}/{finish_slug}-phase-{phase}", dict(params))
                for phase in range(1, 5)
            ]
        return [(f"/browse/{weapon_slug}/{finish_slug}", params)]
    if clean.endswith(" Case"):
        return [(f"/browse/cases/{_slugify(clean)}", params)]

    # CS2Skins exposes prices for these cards in search, but not per-market
    # rows, so they cannot safely backfill marketplace-specific prices.
    return []


def _get(
    path: str,
    params: dict[str, str],
    rate_limiter: RequestRateLimiter | None,
) -> requests.Response:
    if rate_limiter is not None:
        rate_limiter.wait()
    response = requests.get(
        urljoin(os.getenv("CS2SKINS_BASE_URL", BASE_URL), path),
        params=params,
        headers={
            "User-Agent": os.getenv(
                "CS2SKINS_USER_AGENT",
                "Mozilla/5.0 (compatible; local-cs2-basket-tool/1.0)",
            ),
            "Accept": "text/html,application/xhtml+xml,*/*",
        },
        timeout=float(os.getenv("CS2SKINS_TIMEOUT_SECONDS", "30")),
    )
    response.raise_for_status()
    if "item not found" in response.text.lower():
        raise NoCS2SkinsOffersError(f"CS2Skins item route was not found: {response.url}")
    return response


def _parse_offers(html: str) -> dict[str, CS2SkinsOffer]:
    soup = BeautifulSoup(html, "html.parser")
    offers: dict[str, CS2SkinsOffer] = {}
    for row in soup.select("div.market-row"):
        anchor = row.select_one('a[href^="/go/"]')
        price_node = row.select_one(".price-wrap .price")
        if anchor is None or price_node is None:
            continue
        parts = str(anchor.get("href") or "").split("/")
        if len(parts) < 3:
            continue
        slug = parts[2]
        marketplace = SLUG_MARKETS.get(slug)
        if marketplace is None:
            continue
        price = _parse_price(price_node.get_text(" ", strip=True))
        if price is None or price <= 0:
            continue
        stock_node = row.select_one(".stock")
        stock_count = _parse_stock(stock_node.get_text(" ", strip=True) if stock_node else "")
        existing = offers.get(slug)
        offer = CS2SkinsOffer(marketplace, price, stock_count)
        if existing is None or offer.price < existing.price:
            offers[slug] = offer
    return offers


def _parse_price(text: str) -> float | None:
    match = re.search(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)", text or "")
    return float(match.group(1).replace(",", "")) if match else None


def _parse_stock(text: str) -> int | None:
    match = re.search(r"([0-9][0-9,]*)\s+listings?", text or "", flags=re.IGNORECASE)
    return int(match.group(1).replace(",", "")) if match else None


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
