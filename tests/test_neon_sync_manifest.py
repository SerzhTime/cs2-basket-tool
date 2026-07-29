from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import db


class NeonSyncManifestTests(unittest.TestCase):
    def test_snapshot_signature_is_stable_and_detects_price_changes(self) -> None:
        original = [
            ("HaloSkins", "AK-47 | Slate (Battle-Scarred)", 3.2, "USD", 3.2, "USD", 1, "ok", None, "t"),
            ("CSFloat", "AK-47 | Slate (Battle-Scarred)", 3.1, "USD", 3.1, "USD", 1, "ok", None, "t"),
        ]
        self.assertEqual(db._snapshot_signature(original), db._snapshot_signature(list(original)))
        changed = [*original]
        changed[1] = (*changed[1][:2], 3.15, *changed[1][3:])
        self.assertNotEqual(db._snapshot_signature(original), db._snapshot_signature(changed))

    def test_manifest_round_trip_and_reconcile_interval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "manifest.json"
            with patch.object(db, "NEON_SYNC_MANIFEST_PATH", manifest_path):
                db._save_neon_sync_manifest(
                    {"snapshot": "fingerprint"},
                    full_reconciled_at=time.time(),
                    local_revision={"price_history.sqlite": 1},
                )
                manifest = db._load_neon_sync_manifest()

            self.assertEqual(manifest["snapshot_signatures"], {"snapshot": "fingerprint"})
            self.assertEqual(manifest["local_revision"], {"price_history.sqlite": 1})
            self.assertFalse(db._neon_full_reconcile_due(manifest))
            self.assertTrue(db._neon_full_reconcile_due({"last_full_reconciled_at": 0}))


if __name__ == "__main__":
    unittest.main()
