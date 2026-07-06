from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Protocol


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
