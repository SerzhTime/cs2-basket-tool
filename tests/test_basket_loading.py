from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

import basket


class BasketLoadingTests(unittest.TestCase):
    def test_dependency_error_uses_stdlib_fallback(self) -> None:
        expected = [{"market_hash_name": "Example"}]
        with (
            patch("basket._load_with_pandas", side_effect=ImportError("openpyxl missing")),
            patch("basket._load_with_stdlib", return_value=expected) as fallback,
        ):
            result = basket.load_basket_rows(Path("basket.xlsx"))

        self.assertEqual(expected, result)
        fallback.assert_called_once()

    def test_workbook_validation_error_is_not_hidden(self) -> None:
        with (
            patch("basket._load_with_pandas", side_effect=ValueError("missing market_hash_name")),
            patch("basket._load_with_stdlib") as fallback,
        ):
            with self.assertRaisesRegex(ValueError, "missing market_hash_name"):
                basket.load_basket_rows(Path("basket.xlsx"))

        fallback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
