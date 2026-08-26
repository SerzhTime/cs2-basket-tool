from __future__ import annotations

import os
import time
import unittest
from unittest.mock import patch

from adapters.base import BasketItem, PriceResult
from adapters.backup_sources import BackupOffer, _parse_priceempire_offers, apply_backup_prices, clear_backup_cache
from adapters.concurrency import map_concurrently
from adapters.csfloat import CSFloatAdapter
from adapters.csgoskins import (
    CSGOSKINSMarketplaceAdapter,
    clear_csgoskins_cache,
    csgoskins_fetch_diagnostics,
)
from adapters.dmarket import DMarketAdapter
from adapters.skindeck import SkindeckAdapter


ITEMS = [BasketItem(index, f"Item {index}") for index in range(1, 5)]


class _Response:
    def __init__(self, body: object) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._body


class ItemApiConcurrencyTests(unittest.TestCase):
    def test_map_concurrently_preserves_item_order(self) -> None:
        def fetch(value: int) -> int:
            time.sleep((4 - value) * 0.002)
            return value

        self.assertEqual(map_concurrently([1, 2, 3], 3, fetch), [1, 2, 3])

    def test_csfloat_parallel_fetch_preserves_result_order(self) -> None:
        def response(*_args, **kwargs):
            name = kwargs["params"]["market_hash_name"]
            return _Response(
                {"data": [{"price": 123, "type": "buy_now", "state": "listed", "item": {"market_hash_name": name}}]}
            )

        with patch.dict(os.environ, {"CSFLOAT_API_KEY": "test", "CSFLOAT_MAX_WORKERS": "4"}, clear=False), patch(
            "adapters.csfloat.requests.get", side_effect=response
        ):
            results = CSFloatAdapter().fetch_prices(ITEMS)

        self.assertEqual([result.market_hash_name for result in results], [item.market_hash_name for item in ITEMS])
        self.assertTrue(all(result.fetch_status == "ok" for result in results))

    def test_dmarket_parallel_fetch_preserves_result_order(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DMARKET_PUBLIC_KEY": "public",
                "DMARKET_SECRET_KEY": "00" * 32,
                "DMARKET_MAX_WORKERS": "4",
                "DMARKET_MAX_REQUESTS_PER_SECOND": "999",
            },
            clear=False,
        ), patch(
            "adapters.dmarket._find_lowest_exact_listing", return_value={"priceCents": 123}
        ):
            results = DMarketAdapter().fetch_prices(ITEMS)

        self.assertEqual([result.market_hash_name for result in results], [item.market_hash_name for item in ITEMS])
        self.assertTrue(all(result.fetch_status == "ok" for result in results))

    def test_skindeck_parallel_fetch_preserves_result_order(self) -> None:
        def rows(name: str, _rate_limiter):
            return [{"market_hash_name": name, "offer": {"price": 1.23}}]

        with patch.dict(
            os.environ,
            {"SKINDECK_API_KEY": "test", "SKINDECK_MAX_WORKERS": "2"},
            clear=False,
        ), patch("adapters.skindeck._request_market", side_effect=rows):
            results = SkindeckAdapter().fetch_prices(ITEMS)

        self.assertEqual([result.market_hash_name for result in results], [item.market_hash_name for item in ITEMS])
        self.assertTrue(all(result.fetch_status == "ok" for result in results))

    def test_csgoskins_diagnostics_reset_with_cache(self) -> None:
        clear_csgoskins_cache()
        self.assertEqual(csgoskins_fetch_diagnostics(), {"direct": 0, "reader": 0, "fallback": 0, "errors": 0})

        items = [
            BasketItem(index, f"Item {index}", price_compare_url=f"https://csgoskins.gg/item-{index}")
            for index in range(1, 5)
        ]

        def load_offers(url: str):
            return {"csmoney": type("Offer", (), {"marketplace": "CS.MONEY", "price": 1.23, "stock_count": 1})()}

        clear_csgoskins_cache()
        try:
            with patch.dict(
                os.environ,
                {"CSGOSKINS_MAX_WORKERS": "4", "CSGOSKINS_DELAY_SECONDS": "0", "CSGOSKINS_DELAY_JITTER_SECONDS": "0"},
                clear=False,
            ), patch("adapters.csgoskins._load_offers", side_effect=load_offers):
                adapter = CSGOSKINSMarketplaceAdapter("csgoskins_csmoney", "CS.MONEY", ["CS.MONEY"])
                results = adapter.fetch_prices(items)
        finally:
            clear_csgoskins_cache()

        self.assertEqual([result.market_hash_name for result in results], [item.market_hash_name for item in items])
        self.assertTrue(all(result.fetch_status == "ok" for result in results))

    def test_priceempire_listing_rows_are_parsed(self) -> None:
        html = """
            <div class="listing-row">
                <span class="listing-row__provider-name">CS.MONEY</span>
                <span class="listing-row__price">$1,851.02</span>
            </div>
            <div class="listing-row">
                <span class="listing-row__provider-name">Skinport</span>
                <span class="listing-row__price">$1,703.69</span>
            </div>
        """

        offers = _parse_priceempire_offers(html)

        self.assertEqual(offers["csmoney"], BackupOffer("CS.MONEY", 1851.02, "PriceEmpire CS.MONEY"))
        self.assertEqual(offers["skinport"], BackupOffer("Skinport", 1703.69, "PriceEmpire Skinport"))

    def test_backup_prices_resolve_concurrently_and_preserve_order(self) -> None:
        items = [
            BasketItem(index, f"Item {index}", steamanalyst_url=f"https://steamanalyst.com/item-{index}")
            for index in range(1, 5)
        ]
        results = [
            PriceResult(
                marketplace="Tradeit.gg",
                market_hash_name=item.market_hash_name,
                price=None,
                fetch_status="missing",
            )
            for item in items
        ]

        def load_offers(_url: str, _rate_limiter):
            return {"Tradeit.gg": BackupOffer(marketplace="Tradeit.gg", price=1.23, source="SteamAnalyst")}

        clear_backup_cache()
        try:
            with patch.dict(
                os.environ,
                {"STEAMANALYST_MAX_WORKERS": "4", "STEAMANALYST_DELAY_SECONDS": "0"},
                clear=False,
            ), patch("adapters.backup_sources._load_steamanalyst_offers", side_effect=load_offers):
                updated = apply_backup_prices(results, items)
        finally:
            clear_backup_cache()

        self.assertEqual([r.market_hash_name for r in updated], [item.market_hash_name for item in items])
        self.assertTrue(all(r.fetch_status == "ok" and r.price == 1.23 for r in updated))


if __name__ == "__main__":
    unittest.main()
