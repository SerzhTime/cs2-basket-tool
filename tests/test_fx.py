from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from adapters import fx


class _Response:
    def __init__(self, body: object, error: Exception | None = None) -> None:
        self.body = body
        self.error = error

    def raise_for_status(self) -> None:
        if self.error:
            raise self.error

    def json(self) -> object:
        return self.body


class ExchangeRateTests(unittest.TestCase):
    def setUp(self) -> None:
        fx._clear_rate_cache()

    def tearDown(self) -> None:
        fx._clear_rate_cache()

    def test_uses_frankfurter_when_primary_is_rate_limited(self) -> None:
        primary_failure = _Response({}, RuntimeError("429 Too Many Requests"))
        fallback = _Response({"rates": {"CNY": 6.7078}})
        with patch.dict(
            os.environ,
            {
                "EXCHANGERATE_USD_LATEST_URL": "https://primary.invalid/latest",
                "EXCHANGERATE_FALLBACK_USD_LATEST_URL": "https://fallback.invalid/latest",
            },
            clear=False,
        ), patch("adapters.fx.requests.get", side_effect=[primary_failure, fallback]) as get:
            self.assertAlmostEqual(fx.fetch_cny_to_usd_rate(), 1 / 6.7078)
        self.assertEqual(get.call_count, 2)

    def test_uses_recent_cached_rate_after_both_providers_fail(self) -> None:
        good = _Response({"conversion_rates": {"CNY": 7.0}})
        bad = _Response({}, RuntimeError("unavailable"))
        with patch.dict(
            os.environ,
            {
                "EXCHANGERATE_USD_LATEST_URL": "https://primary.invalid/latest",
                "EXCHANGERATE_FALLBACK_USD_LATEST_URL": "https://fallback.invalid/latest",
                "EXCHANGERATE_CACHE_SECONDS": "21600",
            },
            clear=False,
        ), patch("adapters.fx.requests.get", side_effect=[good]):
            cached_rate = fx.fetch_cny_to_usd_rate()
        with patch("adapters.fx.requests.get", side_effect=[bad, bad]):
            self.assertEqual(fx.fetch_cny_to_usd_rate(), cached_rate)


if __name__ == "__main__":
    unittest.main()
