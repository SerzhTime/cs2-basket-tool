from __future__ import annotations

import os
import time
import unittest
from unittest.mock import patch

from adapters.base import BasketItem
from adapters.concurrency import map_concurrently
from adapters.csfloat import CSFloatAdapter
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


if __name__ == "__main__":
    unittest.main()
