from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db  # noqa: E402


def main() -> int:
    load_dotenv(ROOT / ".env")
    if not db.using_postgres():
        print("DATABASE_URL is not configured. Refusing to migrate without a Postgres target.")
        return 1
    if not db.DB_PATH.exists():
        print(f"SQLite source not found: {db.DB_PATH}")
        return 1

    db.init_db()
    source = sqlite3.connect(db.DB_PATH)
    source.row_factory = sqlite3.Row
    try:
        counts = migrate(source)
    finally:
        source.close()

    print(
        "Migration complete: "
        f"{counts['basket_items']} basket_items, "
        f"{counts['marketplaces']} marketplaces, "
        f"{counts['snapshots']} snapshots, "
        f"{counts['price_points']} price_points."
    )
    return 0


def migrate(source: sqlite3.Connection) -> dict[str, int]:
    counts = {"basket_items": 0, "marketplaces": 0, "snapshots": 0, "price_points": 0}
    with db.connect() as target:
        for row in source.execute("SELECT * FROM basket_items ORDER BY item_id"):
            target.execute(
                """
                INSERT INTO basket_items (
                    item_id, market_hash_name, active, multiplier, notes, source_rank,
                    source_amount, price_compare_url, priceempire_url, steamanalyst_url,
                    marketplace_links_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(market_hash_name) DO UPDATE SET
                    active = excluded.active,
                    multiplier = excluded.multiplier,
                    notes = excluded.notes,
                    source_rank = excluded.source_rank,
                    source_amount = excluded.source_amount,
                    price_compare_url = excluded.price_compare_url,
                    priceempire_url = excluded.priceempire_url,
                    steamanalyst_url = excluded.steamanalyst_url,
                    marketplace_links_json = excluded.marketplace_links_json
                """,
                (
                    int(row["item_id"]),
                    row["market_hash_name"],
                    int_or_default(row["active"], 1),
                    int_or_default(row["multiplier"], 1),
                    row["notes"] or "",
                    int_or_none(row["source_rank"]),
                    float_or_none(row["source_amount"]),
                    none_if_blank(row["price_compare_url"]),
                    none_if_blank(row["priceempire_url"]),
                    none_if_blank(row["steamanalyst_url"]),
                    none_if_blank(row["marketplace_links_json"]),
                    row["created_at"] or db.utc_now_iso(),
                ),
            )
            counts["basket_items"] += 1

        for row in source.execute("SELECT * FROM marketplaces ORDER BY adapter_key"):
            target.execute(
                """
                INSERT INTO marketplaces (
                    adapter_key, name, enabled, is_baseline, requires_credentials,
                    last_status, last_error, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(adapter_key) DO UPDATE SET
                    name = excluded.name,
                    enabled = excluded.enabled,
                    is_baseline = excluded.is_baseline,
                    requires_credentials = excluded.requires_credentials,
                    last_status = excluded.last_status,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                """,
                (
                    row["adapter_key"],
                    row["name"],
                    int_or_default(row["enabled"], 1),
                    int_or_default(row["is_baseline"], 0),
                    int_or_default(row["requires_credentials"], 0),
                    none_if_blank(row["last_status"]),
                    none_if_blank(row["last_error"]),
                    none_if_blank(row["updated_at"]),
                ),
            )
            counts["marketplaces"] += 1

        for row in source.execute("SELECT * FROM snapshots ORDER BY snapshot_id"):
            target.execute(
                """
                INSERT INTO snapshots (snapshot_id, timestamp)
                VALUES (?, ?)
                ON CONFLICT(snapshot_id) DO UPDATE SET timestamp = excluded.timestamp
                """,
                (row["snapshot_id"], row["timestamp"]),
            )
            counts["snapshots"] += 1

        price_point_sql = """
            INSERT INTO price_points (
                price_point_id, snapshot_id, marketplace, item_id, market_hash_name,
                price, currency, normalized_price, normalized_currency, stock_count,
                fetch_status, error_details, timestamp
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(price_point_id) DO UPDATE SET
                snapshot_id = excluded.snapshot_id,
                marketplace = excluded.marketplace,
                item_id = excluded.item_id,
                market_hash_name = excluded.market_hash_name,
                price = excluded.price,
                currency = excluded.currency,
                normalized_price = excluded.normalized_price,
                normalized_currency = excluded.normalized_currency,
                stock_count = excluded.stock_count,
                fetch_status = excluded.fetch_status,
                error_details = excluded.error_details,
                timestamp = excluded.timestamp
        """
        batch = []
        for row in source.execute("SELECT * FROM price_points ORDER BY price_point_id"):
            batch.append(
                (
                    int(row["price_point_id"]),
                    int(row["snapshot_id"]),
                    row["marketplace"],
                    int_or_none(row["item_id"]),
                    row["market_hash_name"],
                    float_or_none(row["price"]),
                    row["currency"] or "USD",
                    float_or_none(row["normalized_price"]),
                    row["normalized_currency"] or "USD",
                    int_or_none(row["stock_count"]),
                    row["fetch_status"],
                    none_if_blank(row["error_details"]),
                    row["timestamp"],
                )
            )
            if len(batch) >= 1000:
                target.executemany(price_point_sql, batch)
                target.commit()
                counts["price_points"] += len(batch)
                batch.clear()
        if batch:
            target.executemany(price_point_sql, batch)
            target.commit()
            counts["price_points"] += len(batch)

        reset_identity(target, "basket_items", "item_id")
        reset_identity(target, "snapshots", "snapshot_id")
        reset_identity(target, "price_points", "price_point_id")
    return counts


def reset_identity(target: db.DbConnection, table: str, column: str) -> None:
    target.execute(
        f"""
        SELECT setval(
            pg_get_serial_sequence('{table}', '{column}'),
            COALESCE((SELECT MAX({column}) FROM {table}), 1),
            true
        )
        """
    )


def none_if_blank(value):
    if value is None:
        return None
    text = str(value)
    return None if text.strip() == "" else value


def int_or_none(value) -> int | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def int_or_default(value, default: int) -> int:
    parsed = int_or_none(value)
    return default if parsed is None else parsed


def float_or_none(value) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    raise SystemExit(main())
