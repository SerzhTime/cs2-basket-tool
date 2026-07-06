from __future__ import annotations

import os
import re
import time
from typing import Iterable
from urllib.parse import quote

from .base import BasketItem, PriceResult


class PriceCompareWebAdapter:
    key = "pricecompare_web"
    name = os.getenv("PRICE_COMPARE_WEB_MARKETPLACE_NAME", "PriceCompare Web")
    requires_credentials = False

    def credentials_configured(self) -> bool:
        return bool(
            os.getenv("PRICE_COMPARE_WEB_URL_TEMPLATE")
            and (os.getenv("PRICE_COMPARE_WEB_PRICE_SELECTOR") or os.getenv("PRICE_COMPARE_WEB_PRICE_REGEX"))
        )

    def fetch_prices(self, items: Iterable[BasketItem]) -> list[PriceResult]:
        if not self.credentials_configured():
            return [
                PriceResult(
                    marketplace=self.name,
                    market_hash_name=item.market_hash_name,
                    price=None,
                    currency=os.getenv("PRICE_COMPARE_WEB_CURRENCY", "USD"),
                    fetch_status="error",
                    error_details=(
                        "Set PRICE_COMPARE_WEB_URL_TEMPLATE and either "
                        "PRICE_COMPARE_WEB_PRICE_SELECTOR or PRICE_COMPARE_WEB_PRICE_REGEX."
                    ),
                )
                for item in items
            ]

        import requests
        from bs4 import BeautifulSoup

        timeout = float(os.getenv("PRICE_COMPARE_WEB_TIMEOUT_SECONDS", "15"))
        delay = float(os.getenv("PRICE_COMPARE_WEB_DELAY_SECONDS", "0.5"))
        url_template = os.environ["PRICE_COMPARE_WEB_URL_TEMPLATE"]
        price_selector = os.getenv("PRICE_COMPARE_WEB_PRICE_SELECTOR")
        price_regex = os.getenv("PRICE_COMPARE_WEB_PRICE_REGEX")
        stock_selector = os.getenv("PRICE_COMPARE_WEB_STOCK_SELECTOR")
        currency = os.getenv("PRICE_COMPARE_WEB_CURRENCY", "USD")
        headers = {
            "User-Agent": os.getenv(
                "PRICE_COMPARE_WEB_USER_AGENT",
                "Mozilla/5.0 (compatible; local-cs2-basket-tool/1.0)",
            )
        }

        results: list[PriceResult] = []
        for item in items:
            url = url_template.format(
                item=quote(item.market_hash_name, safe=""),
                item_raw=item.market_hash_name,
            )
            try:
                response = requests.get(url, headers=headers, timeout=timeout)
                response.raise_for_status()
                soup = BeautifulSoup(response.text, "html.parser")
                price_text = _extract_text(soup, price_selector) if price_selector else response.text
                price = _parse_price(price_text, price_regex)
                stock_count = _parse_stock(_extract_text(soup, stock_selector)) if stock_selector else None
                results.append(
                    PriceResult(
                        marketplace=self.name,
                        market_hash_name=item.market_hash_name,
                        price=price,
                        currency=currency,
                        stock_count=stock_count,
                        fetch_status="ok" if price is not None else "missing",
                        error_details=None if price is not None else "Configured selector/regex did not return a price.",
                    )
                )
            except Exception as exc:
                results.append(
                    PriceResult(
                        marketplace=self.name,
                        market_hash_name=item.market_hash_name,
                        price=None,
                        currency=currency,
                        fetch_status="error",
                        error_details=str(exc),
                    )
                )
            if delay > 0:
                time.sleep(delay)
        return results


def _extract_text(soup, selector: str | None) -> str:
    if not selector:
        return ""
    node = soup.select_one(selector)
    return node.get_text(" ", strip=True) if node else ""


def _parse_price(text: str, regex: str | None) -> float | None:
    if regex:
        match = re.search(regex, text)
        if not match:
            return None
        text = match.group(1) if match.groups() else match.group(0)

    match = re.search(r"[-+]?\d[\d,]*(?:\.\d+)?", text)
    if not match:
        return None
    return float(match.group(0).replace(",", ""))


def _parse_stock(text: str) -> int | None:
    match = re.search(r"\d[\d,]*", text or "")
    if not match:
        return None
    return int(match.group(0).replace(",", ""))
