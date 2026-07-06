from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from urllib.parse import quote

import requests

from .base import BasketItem, PriceResult


PRICE_PATTERN = re.compile(
    r"(?:\$|USD\s*)(?P<price>[0-9][0-9,]*(?:\.[0-9]{1,2})?)",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class DirectPageResult:
    result: PriceResult
    page_unavailable: bool = False


def fetch_direct_market_page_price(
    marketplace: str,
    item: BasketItem,
    baseline_price: float | None,
) -> DirectPageResult:
    urls = _candidate_urls(marketplace, item)
    if not urls:
        return DirectPageResult(
            PriceResult(
                marketplace=marketplace,
                market_hash_name=item.market_hash_name,
                price=None,
                currency="USD",
                fetch_status="missing",
                error_details=f"No direct {marketplace} link is stored for this basket item.",
            )
        )

    errors: list[str] = []
    loaded_without_price: list[str] = []
    try:
        for url in urls:
            try:
                response = _session().get(
                    url,
                    timeout=float(os.getenv("DIRECT_MARKET_PAGE_TIMEOUT_SECONDS", "25")),
                    allow_redirects=True,
                )
                response.raise_for_status()
            except Exception as exc:
                errors.append(f"{url}: {exc}")
                continue

            price = _lowest_plausible_price(response.text, baseline_price)
            if price is None:
                loaded_without_price.append(response.url)
                continue

            return DirectPageResult(
                PriceResult(
                    marketplace=marketplace,
                    market_hash_name=item.market_hash_name,
                    price=price,
                    currency="USD",
                    stock_count=1,
                    fetch_status="ok",
                    error_details=f"Direct {marketplace} page repair from item URL.",
                )
            )
    finally:
        _delay()

    if loaded_without_price:
        return DirectPageResult(
            PriceResult(
                marketplace=marketplace,
                market_hash_name=item.market_hash_name,
                price=None,
                currency="USD",
                fetch_status="missing",
                error_details=(
                    f"Direct {marketplace} page loaded but no baseline-plausible USD price was parsed "
                    f"(checked: {', '.join(loaded_without_price[:3])})."
                ),
            ),
            page_unavailable=bool(errors),
        )

    return DirectPageResult(
        PriceResult(
            marketplace=marketplace,
            market_hash_name=item.market_hash_name,
            price=None,
            currency="USD",
            fetch_status="error",
            error_details=f"Direct {marketplace} page unavailable: {'; '.join(errors[:3])}",
        ),
        page_unavailable=True,
    )


def _candidate_urls(marketplace: str, item: BasketItem) -> list[str]:
    stored_url = item.marketplace_links.get(marketplace)
    candidates = [stored_url] if stored_url else []
    encoded_name = quote(item.market_hash_name, safe="")

    if marketplace == "Aim.market":
        candidates.append(f"https://aim.market/en/buy/csgo?search={encoded_name}")
    elif marketplace == "SkinSwap":
        candidates.append(f"https://skinswap.com/buy?search={encoded_name}")

    seen: set[str] = set()
    return [url for url in candidates if url and not (url in seen or seen.add(url))]


def _lowest_plausible_price(html: str, baseline_price: float | None) -> float | None:
    candidates = sorted(
        {
            float(match.group("price").replace(",", ""))
            for match in PRICE_PATTERN.finditer(html[: int(os.getenv("DIRECT_MARKET_PAGE_MAX_CHARS", "900000"))])
        }
    )
    candidates = [price for price in candidates if price > 0]
    if baseline_price is None or baseline_price <= 0:
        return candidates[0] if len(candidates) == 1 else None

    min_ratio = float(os.getenv("DIRECT_MARKET_MIN_BASELINE_RATIO", "0.5"))
    max_ratio = float(os.getenv("DIRECT_MARKET_MAX_BASELINE_RATIO", "2.0"))
    plausible = [
        price
        for price in candidates
        if min_ratio <= (price / baseline_price) <= max_ratio
    ]
    return plausible[0] if plausible else None


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": os.getenv(
                "DIRECT_MARKET_PAGE_USER_AGENT",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return session


def _delay() -> None:
    delay = float(os.getenv("DIRECT_MARKET_PAGE_DELAY_SECONDS", "0.75"))
    if delay > 0:
        time.sleep(delay)
