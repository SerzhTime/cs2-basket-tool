from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Iterable, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_SENSITIVE_QUERY_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "app_key",
    "app-key",
    "key",
    "secret",
    "sign",
    "signature",
    "token",
    "webapi_token",
}
_SENSITIVE_ENV_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "SIGNATURE", "PRIVATE")


@dataclass(frozen=True)
class BasketItem:
    item_id: int
    market_hash_name: str
    price_compare_url: str | None = None
    priceempire_url: str | None = None
    steamanalyst_url: str | None = None
    marketplace_links: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PriceResult:
    marketplace: str
    market_hash_name: str
    price: float | None
    currency: str = "USD"
    stock_count: int | None = None
    fetch_status: str = "ok"
    error_details: str | None = None


class MarketplaceAdapter(Protocol):
    key: str
    name: str
    requires_credentials: bool

    def credentials_configured(self) -> bool:
        ...

    def fetch_prices(self, items: Iterable[BasketItem]) -> list[PriceResult]:
        ...


def safe_error_details(error: object) -> str:
    """Return an adapter error message without credential-bearing URLs or secrets."""
    message = str(error)
    message = _URL_PATTERN.sub(_redact_url, message)
    for name, value in os.environ.items():
        if (
            value
            and len(value) >= 8
            and any(marker in name.upper() for marker in _SENSITIVE_ENV_MARKERS)
        ):
            message = message.replace(value, "[REDACTED]")
    return message


def _redact_url(match: re.Match[str]) -> str:
    raw_url = match.group(0)
    trailing = ""
    while raw_url and raw_url[-1] in ".,;:)]}":
        trailing = raw_url[-1] + trailing
        raw_url = raw_url[:-1]

    try:
        parts = urlsplit(raw_url)
        query = parse_qsl(parts.query, keep_blank_values=True)
        redacted_query = [
            (key, "[REDACTED]" if key.lower() in _SENSITIVE_QUERY_KEYS else value)
            for key, value in query
        ]
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(redacted_query), parts.fragment)
        ) + trailing
    except ValueError:
        return raw_url + trailing
