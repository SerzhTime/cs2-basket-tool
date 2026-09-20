from __future__ import annotations

import unittest
from unittest.mock import patch

from adapters import cs2skins


class _Response:
    def __init__(self, text: str, url: str = "https://cs2skins.gg/test") -> None:
        self.text = text
        self.url = url


def _market_row(slug: str, name: str, price: float, stock: int) -> str:
    return f"""
    <div class="market-row">
      <div class="market-row__info"><div class="name">{name}</div><div class="stock">{stock:,} listings · 0% fee</div></div>
      <div class="price-wrap"><span class="price price--md">${price:,.2f}</span></div>
      <div class="cta-wrap"><a href="/go/{slug}/item">Visit</a></div>
    </div>
    """


class CS2SkinsTests(unittest.TestCase):
    def setUp(self) -> None:
        cs2skins.clear_cs2skins_cache()

    def tearDown(self) -> None:
        cs2skins.clear_cs2skins_cache()

    def test_routes_preserve_wear_and_cases(self) -> None:
        self.assertEqual(
            cs2skins._item_routes("M4A1-S | Printstream (Field-Tested)"),
            [("/browse/m4a1-s/printstream", {"wear": "field-tested"})],
        )
        self.assertEqual(
            cs2skins._item_routes("Recoil Case"),
            [("/browse/cases/recoil-case", {})],
        )
        self.assertEqual(cs2skins._item_routes("Sealed Dead Hand Terminal"), [])

    def test_generic_doppler_uses_four_phases(self) -> None:
        routes = cs2skins._item_routes("★ Butterfly Knife | Doppler (Factory New)")
        self.assertEqual(len(routes), 4)
        self.assertEqual(routes[0], ("/browse/butterfly-knife/doppler-phase-1", {"wear": "factory-new"}))
        self.assertEqual(routes[-1], ("/browse/butterfly-knife/doppler-phase-4", {"wear": "factory-new"}))

    def test_parser_returns_supported_market_prices_and_stock(self) -> None:
        html = _market_row("csmoneym", "CS.MONEY Market", 123.45, 1200) + _market_row(
            "unknown", "Unknown", 1.0, 2
        )
        offers = cs2skins._parse_offers(html)
        self.assertEqual(offers["csmoneym"].marketplace, "CS.MONEY")
        self.assertEqual(offers["csmoneym"].price, 123.45)
        self.assertEqual(offers["csmoneym"].stock_count, 1200)
        self.assertNotIn("unknown", offers)

    def test_doppler_keeps_each_market_minimum_across_phases(self) -> None:
        responses = [
            _Response(_market_row("csmoneym", "CS.MONEY Market", price, 1))
            for price in (120.0, 110.0, 115.0, 125.0)
        ]
        with patch("adapters.cs2skins._get", side_effect=responses) as get:
            offer = cs2skins.cs2skins_offer(
                "★ Butterfly Knife | Doppler (Factory New)", "CS.MONEY"
            )
        self.assertEqual(get.call_count, 4)
        self.assertIsNotNone(offer)
        self.assertEqual(offer.price, 110.0)


if __name__ == "__main__":
    unittest.main()
