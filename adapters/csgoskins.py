from __future__ import annotations

import os
import random
import re
import time
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from .base import BasketItem, PriceResult


CSGOSKINS_MARKETS = [
    ("csgoskins_csmoney", "CS.MONEY", ["CS.MONEY"]),
    ("csgoskins_lis_skins", "LIS-SKINS", ["LIS-SKINS"]),
    ("csgoskins_aim_market", "Aim.market", ["Aim.market"]),
    ("csgoskins_skin_land", "Skin.Land", ["Skin.Land"]),
    ("csgoskins_skinbaron", "SkinBaron", ["SkinBaron"]),
    ("csgoskins_skins_com", "Skins.com", ["Skins.com"]),
    ("csgoskins_exeskins", "Exeskins", ["Exeskins"]),
    ("csgoskins_avan_market", "Avan.market", ["Avan.market"]),
    ("csgoskins_skinvault", "Skinvault", ["Skinvault"]),
    ("csgoskins_uuskins", "UUSKINS", ["UUSKINS"]),
    ("csgoskins_tradeit", "Tradeit.gg", ["Tradeit.gg"]),
    ("csgoskins_skinplace", "SkinPlace", ["SkinPlace", "Skin.Place"]),
    ("csgoskins_shadowpay", "ShadowPay", ["ShadowPay"]),
    ("csgoskins_skinswap", "SkinSwap", ["SkinSwap"]),
]

_PAGE_CACHE: dict[str, dict[str, "_Offer"] | Exception] = {}
_SESSION: requests.Session | None = None


class NoOffersParsedError(RuntimeError):
    pass


@dataclass(frozen=True)
class _Offer:
    marketplace: str
    price: float
    stock_count: int | None


@dataclass(frozen=True)
class CSGOSKINSOffer:
    marketplace: str
    price: float
    stock_count: int | None


class CSGOSKINSMarketplaceAdapter:
    requires_credentials = False

    def __init__(self, key: str, name: str, aliases: list[str]):
        self.key = key
        self.name = name
        self.aliases = aliases

    def credentials_configured(self) -> bool:
        return True

    def fetch_prices(self, items: Iterable[BasketItem]) -> list[PriceResult]:
        results: list[PriceResult] = []
        for item in items:
            if not item.price_compare_url:
                results.append(
                    PriceResult(
                        marketplace=self.name,
                        market_hash_name=item.market_hash_name,
                        price=None,
                        currency="USD",
                        fetch_status="missing",
                        error_details="No CSGOSKINS link is stored for this basket item.",
                    )
                )
                continue

            try:
                offers = _load_offers(item.price_compare_url)
            except Exception as exc:
                results.append(
                    PriceResult(
                        marketplace=self.name,
                        market_hash_name=item.market_hash_name,
                        price=None,
                        currency="USD",
                        fetch_status="error",
                        error_details=str(exc),
                    )
                )
                continue

            offer = _find_offer(offers, self.aliases)
            results.append(
                PriceResult(
                    marketplace=self.name,
                    market_hash_name=item.market_hash_name,
                    price=offer.price if offer else None,
                    currency="USD",
                    stock_count=offer.stock_count if offer else None,
                    fetch_status="ok" if offer else "missing",
                    error_details=None if offer else "CSGOSKINS page did not list this marketplace.",
                )
            )
        return results


def build_csgoskins_adapters() -> list[CSGOSKINSMarketplaceAdapter]:
    return [CSGOSKINSMarketplaceAdapter(key, name, aliases) for key, name, aliases in CSGOSKINS_MARKETS]


def clear_csgoskins_cache() -> None:
    _PAGE_CACHE.clear()


def csgoskins_offer(url: str, aliases: list[str]) -> CSGOSKINSOffer | None:
    offer = _find_offer(_load_offers(url), aliases)
    if offer is None:
        return None
    return CSGOSKINSOffer(
        marketplace=offer.marketplace,
        price=offer.price,
        stock_count=offer.stock_count,
    )


def _load_offers(url: str) -> dict[str, _Offer]:
    if url in _PAGE_CACHE:
        cached = _PAGE_CACHE[url]
        if isinstance(cached, Exception):
            raise cached
        return cached

    attempts = max(1, int(os.getenv("CSGOSKINS_RETRIES", "0")) + 1)
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response, offers = _fetch_and_parse_offers(url)
            _PAGE_CACHE[url] = offers
            return offers
        except Exception as exc:
            last_error = exc
            if attempt < attempts - 1:
                time.sleep(float(os.getenv("CSGOSKINS_RETRY_BACKOFF_SECONDS", "15")))
        finally:
            delay = float(os.getenv("CSGOSKINS_DELAY_SECONDS", "4.0"))
            jitter = float(os.getenv("CSGOSKINS_DELAY_JITTER_SECONDS", "4.0"))
            if delay > 0 or jitter > 0:
                time.sleep(delay + random.uniform(0, max(0.0, jitter)))

    error = last_error or RuntimeError("CSGOSKINS request failed.")
    _PAGE_CACHE[url] = error
    raise error


def _fetch_and_parse_offers(url: str) -> tuple[requests.Response, dict[str, _Offer]]:
    response = _fetch_page(url)
    offers = _parse_response_offers(response)
    if offers:
        return response, offers

    fallback_response = _fetch_no_offer_fallback(url, response)
    if fallback_response is not None:
        fallback_offers = _parse_response_offers(fallback_response)
        if fallback_offers:
            return fallback_response, fallback_offers
        response = fallback_response

    raise NoOffersParsedError(
        f"CSGOSKINS page returned no parseable marketplace offers "
        f"(url={response.url}, status={response.status_code}, length={len(response.text)})."
    )


def _parse_response_offers(response: requests.Response) -> dict[str, _Offer]:
    return _parse_reader_offers(response.text) if _is_reader_url(response.url) else _parse_offers(response.text)


def _fetch_no_offer_fallback(url: str, response: requests.Response) -> requests.Response | None:
    mode = os.getenv("CSGOSKINS_FETCH_MODE", "auto").strip().lower()
    try:
        if _is_reader_url(response.url) and mode != "direct":
            return _get(url)
        if not _is_reader_url(response.url) and mode != "reader":
            return _get(_reader_url(url))
    except Exception:
        return None
    return None


def _fetch_page(url: str) -> requests.Response:
    mode = os.getenv("CSGOSKINS_FETCH_MODE", "auto").strip().lower()
    if mode == "reader":
        return _get(_reader_url(url))
    if mode == "direct":
        return _get(url)

    try:
        return _get(url)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status not in {403, 429}:
            raise
        return _get(_reader_url(url))


def _get(url: str) -> requests.Response:
    response = _session().get(url, timeout=float(os.getenv("CSGOSKINS_TIMEOUT_SECONDS", "30")))
    response.raise_for_status()
    return response


def _reader_url(url: str) -> str:
    if _is_reader_url(url):
        return url
    template = os.getenv("CSGOSKINS_READER_URL_TEMPLATE", "https://r.jina.ai/http://{url}")
    return template.format(url=url, url_encoded=quote(url, safe=""))


def _is_reader_url(url: str) -> bool:
    return "r.jina.ai" in url.lower()


def _session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session()
        _SESSION.headers.update(
            {
                "User-Agent": os.getenv(
                    "CSGOSKINS_USER_AGENT",
                    "Mozilla/5.0 (compatible; local-cs2-basket-tool/1.0)",
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://csgoskins.gg/",
            }
        )
    return _SESSION


def _parse_offers(html: str) -> dict[str, _Offer]:
    soup = BeautifulSoup(html, "html.parser")
    offers: dict[str, _Offer] = {}
    for node in soup.select(".active-offer"):
        text = _clean_space(node.get_text(" ", strip=True))
        price = _parse_price(text)
        if price is None:
            continue
        marketplace = _extract_marketplace(node, text)
        if not marketplace:
            continue
        offers[_normalize_marketplace(marketplace)] = _Offer(
            marketplace=marketplace,
            price=price,
            stock_count=_parse_stock(text),
        )
    return offers


def _parse_reader_offers(text: str) -> dict[str, _Offer]:
    offers: dict[str, _Offer] = {}
    pattern = re.compile(
        r"\)(?P<name>[^]\n]+)\]\(https://csgoskins\.gg/markets/(?P<slug>[^)]+)\)",
        flags=re.IGNORECASE,
    )
    matches = list(pattern.finditer(text))
    for index, match in enumerate(matches):
        body_start = match.end()
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[body_start:body_end]
        marketplace = _clean_space(match.group("name")) or _marketplace_from_slug(match.group("slug"))
        price = _parse_reader_price(body)
        if price is None:
            continue
        key = _normalize_marketplace(marketplace)
        if key in offers:
            continue
        offers[key] = _Offer(
            marketplace=marketplace,
            price=price,
            stock_count=_parse_reader_stock(body),
        )
    return offers


def _parse_reader_price(text: str) -> float | None:
    match = re.search(r"\bfrom\s+\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)", text, flags=re.IGNORECASE)
    if not match:
        return None
    return float(match.group(1).replace(",", ""))


def _parse_reader_stock(text: str) -> int | None:
    match = re.search(r"\bactive offers\s+([0-9][0-9,]*)\b", text, flags=re.IGNORECASE)
    if not match:
        match = re.search(r"\bactive offers\s+([0-9][0-9,]*)", _clean_space(text), flags=re.IGNORECASE)
    if not match:
        return None
    return int(match.group(1).replace(",", ""))


def _marketplace_from_slug(slug: str) -> str:
    slug_map = {
        "csmoney": "CS.MONEY",
        "lis-skins": "LIS-SKINS",
        "aimmarket": "Aim.market",
        "skinland": "Skin.Land",
        "skinbaron": "SkinBaron",
        "skinscom": "Skins.com",
        "exeskins": "Exeskins",
        "avanmarket": "Avan.market",
        "skinvault": "Skinvault",
        "uuskins": "UUSKINS",
        "tradeitgg": "Tradeit.gg",
        "skinplace": "SkinPlace",
        "shadowpay": "ShadowPay",
        "skinswap": "SkinSwap",
    }
    return slug_map.get(slug, slug)


def _extract_marketplace(node, text: str) -> str | None:
    name_node = node.select_one(".custom-underline")
    if name_node:
        name = _clean_space(name_node.get_text(" ", strip=True))
        if name:
            return name

    match = re.match(r"(.+?)\s+\d+(?:\.\d+)?\s+", text)
    if match:
        return match.group(1).strip()
    return None


def _find_offer(offers: dict[str, _Offer], aliases: list[str]) -> _Offer | None:
    for alias in aliases:
        offer = offers.get(_normalize_marketplace(alias))
        if offer:
            return offer
    return None


def _parse_price(text: str) -> float | None:
    match = re.search(r"\bfrom\s+\$([0-9][0-9,]*(?:\.[0-9]+)?)", text, flags=re.IGNORECASE)
    if not match:
        return None
    return float(match.group(1).replace(",", ""))


def _parse_stock(text: str) -> int | None:
    match = re.search(r"\bactive offers\s+([0-9][0-9,]*)\b", text, flags=re.IGNORECASE)
    if not match:
        return None
    return int(match.group(1).replace(",", ""))


def _normalize_marketplace(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _clean_space(value: str) -> str:
    return " ".join((value or "").split())
