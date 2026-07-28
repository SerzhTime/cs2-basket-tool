from __future__ import annotations

import os

import db
from basket import load_basket_rows


def sync_basket_file() -> None:
    if db.BASKET_PATH.exists():
        db.insert_basket_items(load_basket_rows(db.BASKET_PATH))


def should_sync_basket_file_on_startup() -> bool:
    value = os.getenv("CS2DT_SYNC_BASKET_ON_STARTUP", "").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return not db.using_postgres()
