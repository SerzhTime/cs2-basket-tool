from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from adapters.base import safe_error_details


class AdapterErrorRedactionTests(unittest.TestCase):
    def test_redacts_sensitive_query_parameters(self) -> None:
        message = (
            "403 Client Error for url: "
            "https://example.test/prices?game=csgo&key=top-secret&access_token=token-value"
        )

        redacted = safe_error_details(message)

        self.assertNotIn("top-secret", redacted)
        self.assertNotIn("token-value", redacted)
        self.assertIn("game=csgo", redacted)
        self.assertIn("key=%5BREDACTED%5D", redacted)

    def test_redacts_configured_secrets_outside_urls(self) -> None:
        with patch.dict(os.environ, {"EXAMPLE_API_KEY": "secret-value-123"}, clear=False):
            redacted = safe_error_details("Remote API echoed secret-value-123 in its body")

        self.assertEqual("Remote API echoed [REDACTED] in its body", redacted)


if __name__ == "__main__":
    unittest.main()
