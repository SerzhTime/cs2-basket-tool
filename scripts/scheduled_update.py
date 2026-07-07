from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db  # noqa: E402
from app import SnapshotQualityError, collect_snapshot, sync_basket_file  # noqa: E402


def main() -> int:
    load_dotenv(ROOT / ".env")
    db.init_db()
    sync_basket_file()
    try:
        snapshot_id, timestamp, success_rate = collect_snapshot()
    except SnapshotQualityError as exc:
        print(str(exc))
        return 2
    print(
        f"Saved snapshot #{snapshot_id} at {timestamp} "
        f"({success_rate:.0%} data received, postgres={db.using_postgres()})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
