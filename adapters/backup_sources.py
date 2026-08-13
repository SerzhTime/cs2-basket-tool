from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from threading import Lock
from typing import Iterable
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from .base import BasketItem, PriceResult
from .concurrency import RequestRateLimiter, map_concurrently
from .csgoskins import csgoskins_offer


STEAMANALYST_HOST_MARKETS = {
    "tradeit.gg": "Tradeit.gg",
    "cs.money": "CS.MONEY",
    "dmarket.com": "DMarket",
    "lis-skins.com": "LIS-SKINS",
    "skin.land": "Skin.Land",
    "csfloat.com": "CSFloat",
    "waxpeer.com": "Waxpeer",
    "skinswap.com": "SkinSwap",
    "market.csgo.com": "Market.CSGO",
    "exeskins.com": "Exeskins",
    "skinbaron.de": "SkinBaron",
    "shadowpay.com": "ShadowPay",
    "uuskins.com": "UUSKINS",
}
CSGOSKINS_BACKUP_ALIASES = {
    "Buff163": ["BUFF163", "Buff163"],
    "YouPin": ["YouPin", "YOUPIN", "悠悠有品"],
    "Steam": ["Steam"],
}
PRICEEMPIRE_BACKUP_NAMES = {
    "Buff163": ["Buff.163", "BUFF163"],
    "YouPin": ["YouPin898"],
    "Steam": ["Steam"],
}

_STEAMANALYST_CACHE: dict[str, dict[str, "BackupOffer"] | Exception] = {}
_PRICEEMPIRE_CACHE: dict[str, dict[str, "BackupOffer"] | Exception] = {}
_STEAMANALYST_CACHE_LOCK = Lock()
_PRICEEMPIRE_CACHE_LOCK = Lock()


@dataclass(frozen=True)
class BackupOffer:
    marketplace: str
    price: float
    source: str


def apply_backup_prices(
    results: list[PriceResult],
    items: Iterable[BasketItem],
    *,
    deadline: float | None = None,
) -> list[PriceResult]:
    if os.getenv("STEAMANALYST_BACKUP_ENABLED", "1").strip().lower() in {"0", "false", "no"}:
        return results

    items_by_name = {item.market_hash_name: item for item in items}
    baseline_prices = {
        result.market_hash_name: float(result.price)
        for result in results
        if result.marketplace == "HaloSkins" and _successful(result)
    }

    candidates: list[tuple[int, PriceResult, BasketItem]] = []
    for index, result in enumerate(results):
        if _successful(result) or result.marketplace == "HaloSkins":
            continue
        item = items_by_name.get(result.market_hash_name)
        if item is None:
            continue
        candidates.append((index, result, item))

    updated = list(results)
    if not candidates:
        return updated

    workers = max(1, int(os.getenv("STEAMANALYST_MAX_WORKERS", "2")))
    delay = max(0.0, float(os.getenv("STEAMANALYST_DELAY_SECONDS", "0.75")))
    rate_limiter = RequestRateLimiter(1.0 / delay) if delay else None

    def resolve(candidate: tuple[int, PriceResult, BasketItem]) -> PriceResult | None:
        _index, result, item = candidate
        if deadline is not None and time.monotonic() >= deadline:
            return None

        offer = _backup_offer(item, result.marketplace, rate_limiter)
        if offer is None:
            return None

        baseline = baseline_prices.get(result.market_hash_name)
        if baseline is not None and not _passes_baseline_sanity(offer.price, baseline):
            return None

        return PriceResult(
            marketplace=result.marketplace,
            market_hash_name=result.market_hash_name,
            price=offer.price,
            currency="USD",
            stock_count=result.stock_count,
            fetch_status="ok",
            error_details=(
                f"Backup from {offer.source} after primary {result.fetch_status}: "
                f"{result.error_details or 'no primary price'}"
            ),
        )

    resolved = map_concurrently(candidates, workers, resolve)
    for (index, _result, _item), replacement in zip(candidates, resolved):
        if replacement is not None:
            updated[index] = replacement
    return updated


def clear_backup_cache() -> None:
    with _STEAMANALYST_CACHE_LOCK:
        _STEAMANALYST_CACHE.clear()
    with _PRICEEMPIRE_CACHE_LOCK:
        _PRICEEMPIRE_CACHE.clear()


def _backup_offer(item: BasketItem, marketplace: str, rate_limiter: RequestRateLimiter | None = None) -> BackupOffer | None:
    offer = _csgoskins_offer(item, marketplace)
    if offer is not None:
        return offer
    offer = _priceempire_offer(item, marketplace, rate_limiter)
    if offer is not None:
        return offer
    if item.steamanalyst_url:
        return _steamanalyst_offer(item.steamanalyst_url, marketplace, rate_limiter)
    return None


def _csgoskins_offer(item: BasketItem, marketplace: str) -> BackupOffer | None:
    aliases = CSGOSKINS_BACKUP_ALIASES.get(marketplace)
    if not aliases or not item.price_compare_url:
        return None
    try:
        offer = csgoskins_offer(item.price_compare_url, aliases)
    except Exception:
        return None
    if offer is None:
        return None
    return BackupOffer(marketplace=marketplace, price=offer.price, source=f"CSGOSKINS {offer.marketplace}")


def _priceempire_offer(item: BasketItem, marketplace: str, rate_limiter: RequestRateLimiter | None) -> BackupOffer | None:
    names = PRICEEMPIRE_BACKUP_NAMES.get(marketplace)
    if not names or not item.priceempire_url:
        return None
    try:
        offers = _load_priceempire_offers(item.priceempire_url, rate_limiter)
    except Exception:
        return None
    for name in names:
        offer = offers.get(_normalize_marketplace(name))
        if offer is not None:
            return offer
    return None


def _load_priceempire_offers(url: str, rate_limiter: RequestRateLimiter | None) -> dict[str, BackupOffer]:
    with _PRICEEMPIRE_CACHE_LOCK:
        cached = _PRICEEMPIRE_CACHE.get(url)
    if cached is not None:
        if isinstance(cached, Exception):
            raise cached
        return cached

    if rate_limiter is not None:
        rate_limiter.wait()
    try:
        response = requests.get(
            url,
            headers={
                "User-Agent": os.getenv(
                    "PRICEEMPIRE_USER_AGENT",
                    "Mozilla/5.0 (compatible; local-cs2-basket-tool/1.0)",
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=float(os.getenv("PRICEEMPIRE_TIMEOUT_SECONDS", "30")),
        )
        response.raise_for_status()
        offers = _parse_priceempire_offers(response.text)
        if not offers:
            raise RuntimeError("PriceEmpire page returned no parseable listing offers.")
        with _PRICEEMPIRE_CACHE_LOCK:
            _PRICEEMPIRE_CACHE[url] = offers
        return offers
    except Exception as exc:
        with _PRICEEMPIRE_CACHE_LOCK:
            _PRICEEMPIRE_CACHE[url] = exc
        raise


def _parse_priceempire_offers(html: str) -> dict[str, BackupOffer]:
    soup = BeautifulSoup(html, "html.parser")
    offers: dict[str, BackupOffer] = {}
    for article in soup.select('article[aria-label^="Offer from "]'):
        label = article.get("aria-label") or ""
        marketplace = label.removeprefix("Offer from ").strip()
        if not marketplace:
            continue
        text = article.get_text(" ", strip=True)
        price = _parse_price(text)
        if price is None or price <= 0:
            continue
        offers[_normalize_marketplace(marketplace)] = BackupOffer(
            marketplace=marketplace,
            price=price,
            source=f"PriceEmpire {marketplace}",
        )
    return offers


def _steamanalyst_offer(url: str, marketplace: str, rate_limiter: RequestRateLimiter | None) -> BackupOffer | None:
    try:
        offers = _load_steamanalyst_offers(url, rate_limiter)
    except Exception:
        return None
    return offers.get(marketplace)


def _load_steamanalyst_offers(url: str, rate_limiter: RequestRateLimiter | None) -> dict[str, BackupOffer]:
    with _STEAMANALYST_CACHE_LOCK:
        cached = _STEAMANALYST_CACHE.get(url)
    if cached is not None:
        if isinstance(cached, Exception):
            raise cached
        return cached

    if rate_limiter is not None:
        rate_limiter.wait()
    try:
        response = requests.get(
            url,
            headers={
                "User-Agent": os.getenv(
                    "STEAMANALYST_USER_AGENT",
                    "Mozilla/5.0 (compatible; local-cs2-basket-tool/1.0)",
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=float(os.getenv("STEAMANALYST_TIMEOUT_SECONDS", "30")),
        )
        response.raise_for_status()
        offers = _parse_steamanalyst_offers(response.text)
        if not offers:
            raise RuntimeError("SteamAnalyst page returned no parseable marketplace rows.")
        with _STEAMANALYST_CACHE_LOCK:
            _STEAMANALYST_CACHE[url] = offers
        return offers
    except Exception as exc:
        with _STEAMANALYST_CACHE_LOCK:
            _STEAMANALYST_CACHE[url] = exc
        raise


def _parse_steamanalyst_offers(html: str) -> dict[str, BackupOffer]:
    soup = BeautifulSoup(html, "html.parser")
    offers: dict[str, BackupOffer] = {}
    for row in soup.select("a.markets-mobile-row"):
        marketplace = _marketplace_from_href(row.get("href") or "")
        if not marketplace:
            continue
        price = _parse_price(row.get_text(" ", strip=True))
        if price is None or price <= 0:
            continue
        offers[marketplace] = BackupOffer(marketplace=marketplace, price=price, source="SteamAnalyst")
    return offers


def _marketplace_from_href(href: str) -> str | None:
    host = urlparse(href).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    for domain, marketplace in STEAMANALYST_HOST_MARKETS.items():
        if host == domain or host.endswith(f".{domain}"):
            return marketplace
    return None


def _parse_price(text: str) -> float | None:
    match = re.search(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)", text or "")
    if not match:
        return None
    return float(match.group(1).replace(",", ""))


def _successful(result: PriceResult) -> bool:
    return result.fetch_status == "ok" and result.price is not None


def _passes_baseline_sanity(price: float, baseline: float) -> bool:
    if baseline <= 0:
        return True
    min_ratio = float(os.getenv("STEAMANALYST_BACKUP_MIN_BASELINE_RATIO", "0.1"))
    max_ratio = float(os.getenv("STEAMANALYST_BACKUP_MAX_BASELINE_RATIO", "4.0"))
    ratio = price / baseline
    return min_ratio <= ratio <= max_ratio


def _normalize_marketplace(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())
