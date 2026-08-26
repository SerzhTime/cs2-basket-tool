from __future__ import annotations

import sqlite3
import os
import json
import hashlib
import time
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread
from typing import Iterable, Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from adapters import BasketItem, PriceResult, build_adapter_registry

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
DB_PATH = DATA_DIR / "price_history.sqlite"
BASKET_PATH = DATA_DIR / "basket.xlsx"
BASELINE_MARKETPLACE = "HaloSkins"
MIN_SNAPSHOT_SUCCESS_RATE = 0.6
MAX_MARKETPLACE_FAILURE_RATE = 0.4
REMOVED_MOCK_ADAPTER_KEYS = {
    "skinswap_mock",
    "csfloat_mock",
    "skinplace_mock",
    "whitemarket_mock",
    "csmoney_mock",
    "shadowpay_mock",
}
REMOVED_MOCK_MARKETPLACES = {
    "SkinSwap Mock",
    "CSFloat Mock",
    "Skin.Place Mock",
    "White.market",
    "CS.Money Mock",
    "ShadowPay Mock",
}
REMOTE_LOCK_HEARTBEAT_SECONDS = 30.0
NEON_SYNC_MANIFEST_PATH = APP_DIR / ".runtime" / "neon_snapshot_sync_manifest.json"
HISTORY_TIMEZONE = ZoneInfo("Asia/Singapore")


def postgres_database_url() -> str | None:
    value = os.getenv("DATABASE_URL")
    return value.strip() if value and value.strip() else None


def database_url() -> str | None:
    backend = os.getenv("DATABASE_BACKEND", "").strip().lower()
    if backend in {"sqlite", "local"}:
        if running_on_streamlit_cloud() and postgres_database_url() and not force_sqlite():
            return postgres_database_url()
        return None
    return postgres_database_url()


def using_postgres() -> bool:
    return bool(database_url())


def running_on_streamlit_cloud() -> bool:
    return APP_DIR.as_posix().startswith("/mount/src/") or bool(os.getenv("STREAMLIT_CLOUD"))


def force_sqlite() -> bool:
    return os.getenv("CS2DT_FORCE_SQLITE", "").strip().lower() in {"1", "true", "yes", "on"}


def _adapt_sql(sql: str, backend: str | None = None) -> str:
    is_postgres = backend == "postgres" if backend else using_postgres()
    if not is_postgres:
        return sql
    return sql.replace("?", "%s")


class DbConnection:
    def __init__(self, raw, backend: str):
        self.raw = raw
        self.backend = backend

    def execute(self, sql: str, params: Iterable[Any] | None = None):
        return self.raw.execute(_adapt_sql(sql, self.backend), tuple(params or ()))

    def executemany(self, sql: str, params):
        if self.backend == "sqlite":
            return self.raw.executemany(_adapt_sql(sql, self.backend), params)
        with self.raw.cursor() as cur:
            return cur.executemany(_adapt_sql(sql, self.backend), params)

    def executescript(self, sql: str) -> None:
        if self.backend == "sqlite":
            self.raw.executescript(sql)
            return
        # psycopg sends a parameter-free multi-statement string via the
        # simple query protocol in one round trip, instead of one per
        # semicolon-separated statement.
        self.raw.execute(sql)

    def commit(self) -> None:
        self.raw.commit()

    def close(self) -> None:
        self.raw.close()


@contextmanager
def connect():
    if using_postgres():
        import psycopg
        from psycopg.rows import dict_row

        con = psycopg.connect(database_url(), row_factory=dict_row)
        wrapped = DbConnection(con, "postgres")
    else:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(DB_PATH, timeout=float(os.getenv("SQLITE_TIMEOUT_SECONDS", "30")))
        con.row_factory = sqlite3.Row
        con.execute(f"PRAGMA busy_timeout = {int(float(os.getenv('SQLITE_TIMEOUT_SECONDS', '30')) * 1000)}")
        con.execute("PRAGMA journal_mode = WAL")
        wrapped = DbConnection(con, "sqlite")
    try:
        yield wrapped
        wrapped.commit()
    finally:
        wrapped.close()


@contextmanager
def connect_postgres():
    url = postgres_database_url()
    if not url:
        raise RuntimeError("DATABASE_URL is not configured.")
    import psycopg
    from psycopg.rows import dict_row

    con = psycopg.connect(url, row_factory=dict_row)
    wrapped = DbConnection(con, "postgres")
    try:
        yield wrapped
        wrapped.commit()
    finally:
        wrapped.close()


@contextmanager
def remote_price_update_lock():
    """Serialize price updates with Neon synchronization across processes."""
    if not using_postgres():
        yield True
        return

    import psycopg

    stop_heartbeat = Event()
    heartbeat: Thread | None = None
    with psycopg.connect(database_url(), autocommit=True) as con:
        acquired = bool(
            con.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s))",
                ("cs2dt_neon_sync",),
            ).fetchone()[0]
        )
        if acquired:
            heartbeat = Thread(
                target=_keep_remote_lock_connection_alive,
                args=(con, stop_heartbeat),
                daemon=True,
                name="cs2dt-neon-lock-heartbeat",
            )
            heartbeat.start()
        try:
            yield acquired
        finally:
            stop_heartbeat.set()
            if heartbeat is not None:
                heartbeat.join(timeout=5)
            if acquired:
                try:
                    con.execute(
                        "SELECT pg_advisory_unlock(hashtext(%s))",
                        ("cs2dt_neon_sync",),
                    )
                except psycopg.Error:
                    # A terminated PostgreSQL session has already released all
                    # session advisory locks. Do not turn a saved snapshot into
                    # a failed run only because explicit cleanup is impossible.
                    if not con.closed:
                        raise


def _keep_remote_lock_connection_alive(con, stop_event: Event) -> None:
    while not stop_event.wait(REMOTE_LOCK_HEARTBEAT_SECONDS):
        try:
            con.execute("SELECT 1")
        except Exception:
            return


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def init_db(*, migrate: bool = True, maintenance: bool = True) -> None:
    """Prepare the database for the application or a scheduled update.

    Schema migrations and history pruning are intentionally skipped by the
    scheduled updater.  They can take DDL locks or scan the full history while
    another client is using Neon; a scheduled update only needs an existing,
    usable schema.
    """
    with connect() as con:
        if migrate:
            con.executescript(_schema_sql())
            ensure_price_point_uniqueness(con)
            drop_redundant_price_point_indexes(con)
            ensure_column(con, "basket_items", "multiplier", "INTEGER NOT NULL DEFAULT 1")
            ensure_column(con, "basket_items", "price_compare_url", "TEXT")
            ensure_column(con, "basket_items", "priceempire_url", "TEXT")
            ensure_column(con, "basket_items", "steamanalyst_url", "TEXT")
            ensure_column(con, "basket_items", "marketplace_links_json", "TEXT")
            ensure_history_rollup_schema(con)
            ensure_update_run_uniqueness(con)
        else:
            # Fail quickly and clearly if the database was never initialized.
            con.execute("SELECT 1 FROM basket_items LIMIT 1")
        if maintenance:
            remove_mock_marketplaces(con)
            prune_low_quality_snapshots(con, MIN_SNAPSHOT_SUCCESS_RATE)
    seed_marketplaces()


def _schema_sql(backend: str | None = None) -> str:
    is_postgres = backend == "postgres" if backend else using_postgres()
    if is_postgres:
        return """
            CREATE TABLE IF NOT EXISTS basket_items (
                item_id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                market_hash_name TEXT NOT NULL UNIQUE,
                active INTEGER NOT NULL DEFAULT 1,
                multiplier INTEGER NOT NULL DEFAULT 1,
                notes TEXT NOT NULL DEFAULT '',
                source_rank INTEGER,
                source_amount DOUBLE PRECISION,
                price_compare_url TEXT,
                priceempire_url TEXT,
                steamanalyst_url TEXT,
                marketplace_links_json TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS marketplaces (
                adapter_key TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                enabled INTEGER NOT NULL DEFAULT 1,
                is_baseline INTEGER NOT NULL DEFAULT 0,
                requires_credentials INTEGER NOT NULL DEFAULT 0,
                last_status TEXT,
                last_error TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS snapshots (
                snapshot_id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                timestamp TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS price_points (
                price_point_id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                snapshot_id INTEGER NOT NULL REFERENCES snapshots(snapshot_id),
                marketplace TEXT NOT NULL,
                item_id INTEGER REFERENCES basket_items(item_id),
                market_hash_name TEXT NOT NULL,
                price DOUBLE PRECISION,
                currency TEXT NOT NULL DEFAULT 'USD',
                normalized_price DOUBLE PRECISION,
                normalized_currency TEXT NOT NULL DEFAULT 'USD',
                stock_count INTEGER,
                fetch_status TEXT NOT NULL,
                error_details TEXT,
                timestamp TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_price_points_snapshot_market_item
                ON price_points(snapshot_id, marketplace, item_id);
            CREATE INDEX IF NOT EXISTS idx_price_points_history
                ON price_points(marketplace, timestamp);
            CREATE INDEX IF NOT EXISTS idx_snapshots_timestamp
                ON snapshots(timestamp DESC, snapshot_id DESC);
            CREATE INDEX IF NOT EXISTS idx_basket_items_active_order
                ON basket_items(active, source_rank, item_id);
            CREATE INDEX IF NOT EXISTS idx_marketplaces_enabled_order
                ON marketplaces(is_baseline, enabled, name);

            CREATE TABLE IF NOT EXISTS update_runs (
                update_run_id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                source TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                duration_seconds DOUBLE PRECISION NOT NULL,
                status TEXT NOT NULL,
                snapshot_id INTEGER,
                success_rate DOUBLE PRECISION,
                error_details TEXT,
                step_details TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_update_runs_started_at
                ON update_runs(started_at);

            CREATE TABLE IF NOT EXISTS display_cache (
                cache_key TEXT PRIMARY KEY,
                snapshot_id INTEGER,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS history_daily_totals (
                period_start TEXT NOT NULL,
                marketplace TEXT NOT NULL,
                average_total_cost DOUBLE PRECISION NOT NULL,
                min_total_cost DOUBLE PRECISION NOT NULL,
                max_total_cost DOUBLE PRECISION NOT NULL,
                sample_count INTEGER NOT NULL,
                first_sample_at TEXT NOT NULL,
                last_sample_at TEXT NOT NULL,
                aggregated_at TEXT NOT NULL,
                PRIMARY KEY (period_start, marketplace)
            );
            CREATE TABLE IF NOT EXISTS history_retention_state (
                state_key TEXT PRIMARY KEY,
                cutoff_timestamp TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                operation_id TEXT NOT NULL
            );
        """
    return """
        PRAGMA foreign_keys = ON;

        CREATE TABLE IF NOT EXISTS basket_items (
            item_id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_hash_name TEXT NOT NULL UNIQUE,
            active INTEGER NOT NULL DEFAULT 1,
            multiplier INTEGER NOT NULL DEFAULT 1,
            notes TEXT NOT NULL DEFAULT '',
            source_rank INTEGER,
            source_amount REAL,
            price_compare_url TEXT,
            priceempire_url TEXT,
            steamanalyst_url TEXT,
            marketplace_links_json TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS marketplaces (
            adapter_key TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            enabled INTEGER NOT NULL DEFAULT 1,
            is_baseline INTEGER NOT NULL DEFAULT 0,
            requires_credentials INTEGER NOT NULL DEFAULT 0,
            last_status TEXT,
            last_error TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS snapshots (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS price_points (
            price_point_id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id INTEGER NOT NULL,
            marketplace TEXT NOT NULL,
            item_id INTEGER,
            market_hash_name TEXT NOT NULL,
            price REAL,
            currency TEXT NOT NULL DEFAULT 'USD',
            normalized_price REAL,
            normalized_currency TEXT NOT NULL DEFAULT 'USD',
            stock_count INTEGER,
            fetch_status TEXT NOT NULL,
            error_details TEXT,
            timestamp TEXT NOT NULL,
            FOREIGN KEY(snapshot_id) REFERENCES snapshots(snapshot_id),
            FOREIGN KEY(item_id) REFERENCES basket_items(item_id)
        );

        CREATE INDEX IF NOT EXISTS idx_price_points_snapshot_market_item
            ON price_points(snapshot_id, marketplace, item_id);
        CREATE INDEX IF NOT EXISTS idx_price_points_history
            ON price_points(marketplace, timestamp);
        CREATE INDEX IF NOT EXISTS idx_snapshots_timestamp
            ON snapshots(timestamp DESC, snapshot_id DESC);
        CREATE INDEX IF NOT EXISTS idx_basket_items_active_order
            ON basket_items(active, source_rank, item_id);
        CREATE INDEX IF NOT EXISTS idx_marketplaces_enabled_order
            ON marketplaces(is_baseline, enabled, name);

        CREATE TABLE IF NOT EXISTS update_runs (
            update_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            duration_seconds REAL NOT NULL,
            status TEXT NOT NULL,
            snapshot_id INTEGER,
            success_rate REAL,
            error_details TEXT,
            step_details TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_update_runs_started_at
            ON update_runs(started_at);

        CREATE TABLE IF NOT EXISTS display_cache (
            cache_key TEXT PRIMARY KEY,
            snapshot_id INTEGER,
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS history_daily_totals (
            period_start TEXT NOT NULL,
            marketplace TEXT NOT NULL,
            average_total_cost REAL NOT NULL,
            min_total_cost REAL NOT NULL,
            max_total_cost REAL NOT NULL,
            sample_count INTEGER NOT NULL,
            first_sample_at TEXT NOT NULL,
            last_sample_at TEXT NOT NULL,
            aggregated_at TEXT NOT NULL,
            PRIMARY KEY (period_start, marketplace)
        );
        CREATE TABLE IF NOT EXISTS history_retention_state (
            state_key TEXT PRIMARY KEY,
            cutoff_timestamp TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            operation_id TEXT NOT NULL
        );
    """


def ensure_history_rollup_schema(con: DbConnection) -> None:
    columns = _table_column_names(con, "history_daily_totals")
    if "period_start" not in columns:
        # Earlier daily-only previews used calendar_day. They contain no source
        # data and cannot represent the new two-period-per-day contract; raw
        # snapshots remain available to rebuild the rollups correctly.
        con.execute("DROP TABLE IF EXISTS history_daily_totals")
        numeric_type = "DOUBLE PRECISION" if con.backend == "postgres" else "REAL"
        con.execute(
            f"""
            CREATE TABLE history_daily_totals (
                period_start TEXT NOT NULL,
                marketplace TEXT NOT NULL,
                average_total_cost {numeric_type} NOT NULL,
                min_total_cost {numeric_type} NOT NULL,
                max_total_cost {numeric_type} NOT NULL,
                sample_count INTEGER NOT NULL,
                first_sample_at TEXT NOT NULL,
                last_sample_at TEXT NOT NULL,
                aggregated_at TEXT NOT NULL,
                PRIMARY KEY (period_start, marketplace)
            )
            """
        )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_history_daily_totals_market_period "
        "ON history_daily_totals(marketplace, period_start)"
    )


def _table_column_names(con: DbConnection, table: str) -> set[str]:
    if con.backend == "postgres":
        rows = con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ?",
            (table,),
        ).fetchall()
        return {str(row["column_name"]) for row in rows}
    return {str(row["name"]) for row in con.execute(f"PRAGMA table_info({table})").fetchall()}

def ensure_column(con: DbConnection, table: str, column: str, definition: str) -> None:
    if using_postgres():
        row = con.execute(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = ? AND column_name = ?
            """,
            (table, column),
        ).fetchone()
        columns = {column} if row else set()
    else:
        columns = {row["name"] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {_pg_column_definition(definition)}")


def drop_redundant_price_point_indexes(con: DbConnection) -> None:
    """Drop price_points indexes superseded by other indexes on the same table.

    idx_price_points_snapshot(snapshot_id, marketplace) is a strict column
    prefix of idx_price_points_snapshot_market_item, and
    idx_price_points_snapshot_market_hash duplicates the column list of the
    uq_price_points_snapshot_market_hash unique index. Both are safe to drop:
    no query loses index coverage, only redundant on-disk index storage
    shrinks.
    """
    con.execute("DROP INDEX IF EXISTS idx_price_points_snapshot")
    con.execute("DROP INDEX IF EXISTS idx_price_points_snapshot_market_hash")


def ensure_price_point_uniqueness(con: DbConnection) -> None:
    if _price_point_unique_index_exists(con):
        return
    con.execute(
        """
        DELETE FROM price_points
        WHERE price_point_id IN (
            SELECT price_point_id
            FROM (
                SELECT
                    price_point_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY snapshot_id, marketplace, market_hash_name
                        ORDER BY price_point_id DESC
                    ) AS duplicate_rank
                FROM price_points
            ) ranked
            WHERE duplicate_rank > 1
        )
        """
    )
    con.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_price_points_snapshot_market_hash
        ON price_points(snapshot_id, marketplace, market_hash_name)
        """
    )


def _price_point_unique_index_exists(con: DbConnection) -> bool:
    if con.backend == "postgres":
        row = con.execute(
            """
            SELECT 1
            FROM pg_indexes
            WHERE schemaname = current_schema()
              AND indexname = ?
            """,
            ("uq_price_points_snapshot_market_hash",),
        ).fetchone()
        return row is not None
    return any(
        row["name"] == "uq_price_points_snapshot_market_hash"
        for row in con.execute("PRAGMA index_list(price_points)").fetchall()
    )


def ensure_update_run_uniqueness(con: DbConnection) -> None:
    if _update_run_unique_index_exists(con):
        return
    con.execute(
        """
        DELETE FROM update_runs
        WHERE update_run_id IN (
            SELECT update_run_id
            FROM (
                SELECT
                    update_run_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY source, started_at
                        ORDER BY update_run_id DESC
                    ) AS duplicate_rank
                FROM update_runs
            ) ranked
            WHERE duplicate_rank > 1
        )
        """
    )
    con.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_update_runs_source_started
        ON update_runs(source, started_at)
        """
    )


def _update_run_unique_index_exists(con: DbConnection) -> bool:
    if con.backend == "postgres":
        row = con.execute(
            """
            SELECT 1
            FROM pg_indexes
            WHERE schemaname = current_schema()
              AND indexname = ?
            """,
            ("uq_update_runs_source_started",),
        ).fetchone()
        return row is not None
    return any(
        row["name"] == "uq_update_runs_source_started"
        for row in con.execute("PRAGMA index_list(update_runs)").fetchall()
    )


def _pg_column_definition(definition: str) -> str:
    if not using_postgres():
        return definition
    return definition.replace("REAL", "DOUBLE PRECISION")


def remove_mock_marketplaces(con: DbConnection) -> None:
    key_placeholders = ",".join("?" for _ in REMOVED_MOCK_ADAPTER_KEYS)
    name_placeholders = ",".join("?" for _ in REMOVED_MOCK_MARKETPLACES)
    con.execute(
        f"DELETE FROM marketplaces WHERE adapter_key IN ({key_placeholders})",
        tuple(REMOVED_MOCK_ADAPTER_KEYS),
    )
    con.execute(
        f"DELETE FROM price_points WHERE marketplace IN ({name_placeholders})",
        tuple(REMOVED_MOCK_MARKETPLACES),
    )


def seed_marketplaces() -> None:
    registry = build_adapter_registry()
    defaults_enabled = {
        "haloskins",
        "csfloat",
        "waxpeer",
        "c5game",
        "dmarket",
        "marketcsgo",
        "skindeck",
        "uuskins",
        "openskin_skinport",
        "openskin_buff163",
        "openskin_youpin",
        "openskin_steam",
        "csgoskins_csmoney",
        "csgoskins_lis_skins",
        "csgoskins_aim_market",
        "csgoskins_skin_land",
        "csgoskins_skinbaron",
        "csgoskins_skins_com",
        "csgoskins_exeskins",
        "csgoskins_avan_market",
        "csgoskins_skinvault",
        "csgoskins_tradeit",
        "csgoskins_skinplace",
        "csgoskins_shadowpay",
        "csgoskins_skinswap",
    }
    now = utc_now_iso()
    with connect() as con:
        con.execute("DELETE FROM marketplaces WHERE adapter_key = ?", ("csgoskins_uuskins",))
        for key, adapter in registry.items():
            con.execute(
                """
                INSERT INTO marketplaces (
                    adapter_key, name, enabled, is_baseline,
                    requires_credentials, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(adapter_key) DO UPDATE SET
                    name = excluded.name,
                    is_baseline = excluded.is_baseline,
                    requires_credentials = excluded.requires_credentials
                """,
                (
                    key,
                    adapter.name,
                    1 if key in defaults_enabled else 0,
                    1 if adapter.name == BASELINE_MARKETPLACE else 0,
                    1 if adapter.requires_credentials else 0,
                    now,
                ),
            )


def basket_is_empty() -> bool:
    with connect() as con:
        row = con.execute("SELECT COUNT(*) AS c FROM basket_items").fetchone()
        return int(row["c"]) == 0


def _json_or_none(value) -> str | None:
    if not isinstance(value, dict) or not value:
        return None
    clean = {str(key): str(url) for key, url in value.items() if key and url}
    return json.dumps(clean, ensure_ascii=False, sort_keys=True) if clean else None


def _json_to_dict(value) -> dict[str, str]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(key): str(url) for key, url in parsed.items() if key and url}


def insert_basket_items(rows: Iterable[dict]) -> int:
    now = utc_now_iso()
    count = 0
    with connect() as con:
        for row in rows:
            name = str(row["market_hash_name"]).strip()
            if not name:
                continue
            cur = con.execute(
                """
                INSERT INTO basket_items (
                    market_hash_name, active, multiplier, notes, source_rank,
                    source_amount, price_compare_url, priceempire_url,
                    steamanalyst_url, marketplace_links_json, created_at
                )
                VALUES (?, 1, 1, '', ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(market_hash_name) DO UPDATE SET
                    source_rank = COALESCE(excluded.source_rank, basket_items.source_rank),
                    source_amount = COALESCE(excluded.source_amount, basket_items.source_amount),
                    price_compare_url = COALESCE(excluded.price_compare_url, basket_items.price_compare_url),
                    priceempire_url = COALESCE(excluded.priceempire_url, basket_items.priceempire_url),
                    steamanalyst_url = COALESCE(excluded.steamanalyst_url, basket_items.steamanalyst_url),
                    marketplace_links_json = COALESCE(excluded.marketplace_links_json, basket_items.marketplace_links_json)
                """,
                (
                    name,
                    row.get("rank"),
                    row.get("source_amount"),
                    row.get("price_compare_url"),
                    row.get("priceempire_url"),
                    row.get("steamanalyst_url"),
                    _json_or_none(row.get("marketplace_links")),
                    now,
                ),
            )
            if cur.rowcount:
                count += 1
    return count


def get_basket_items(active_only: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM basket_items"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY COALESCE(source_rank, item_id), item_id"
    with connect() as con:
        return list(con.execute(sql).fetchall())


def get_adapter_items() -> list[BasketItem]:
    return [
        BasketItem(
            item_id=int(row["item_id"]),
            market_hash_name=row["market_hash_name"],
            price_compare_url=row["price_compare_url"],
            priceempire_url=row["priceempire_url"],
            steamanalyst_url=row["steamanalyst_url"],
            marketplace_links=_json_to_dict(row["marketplace_links_json"]),
        )
        for row in get_basket_items(active_only=True)
    ]


def update_basket_items(rows: Iterable[dict]) -> None:
    with connect() as con:
        for row in rows:
            con.execute(
                """
                UPDATE basket_items
                SET active = ?, multiplier = ?, notes = ?
                WHERE item_id = ?
                """,
                (
                    1 if row.get("active") else 0,
                    min(1000, max(1, int(row.get("multiplier") or 1))),
                    row.get("notes") or "",
                    int(row["item_id"]),
                ),
            )


def get_marketplaces() -> list[sqlite3.Row]:
    order = "is_baseline DESC, LOWER(name)" if using_postgres() else "is_baseline DESC, name COLLATE NOCASE"
    with connect() as con:
        return list(
            con.execute(
                f"""
                SELECT *
                FROM marketplaces
                ORDER BY {order}
                """
            ).fetchall()
        )


def comparison_inputs_for_snapshot(snapshot_id: int) -> tuple[list[sqlite3.Row], list[sqlite3.Row], list[sqlite3.Row]]:
    marketplace_order = (
        "is_baseline DESC, LOWER(name)" if using_postgres() else "is_baseline DESC, name COLLATE NOCASE"
    )
    with connect() as con:
        items = list(
            con.execute(
                """
                SELECT item_id, market_hash_name, active, multiplier
                FROM basket_items
                ORDER BY COALESCE(source_rank, item_id), item_id
                """
            ).fetchall()
        )
        points = list(
            con.execute(
                """
                SELECT
                    marketplace,
                    market_hash_name,
                    fetch_status,
                    normalized_price
                FROM price_points
                WHERE snapshot_id = ?
                ORDER BY marketplace, market_hash_name
                """,
                (snapshot_id,),
            ).fetchall()
        )
        points = _overlay_recent_successful_prices(con, snapshot_id, points)
        marketplaces = list(
            con.execute(
                f"""
                SELECT *
                FROM marketplaces
                ORDER BY {marketplace_order}
                """
            ).fetchall()
        )
    return items, points, marketplaces


def _overlay_recent_successful_prices(
    con: DbConnection,
    snapshot_id: int,
    points: list[sqlite3.Row],
    max_age_hours: int = 24,
) -> list[dict[str, Any]]:
    snapshot = con.execute(
        "SELECT timestamp FROM snapshots WHERE snapshot_id = ?",
        (snapshot_id,),
    ).fetchone()
    if not snapshot:
        return [dict(point) for point in points]

    target_at = datetime.fromisoformat(str(snapshot["timestamp"]).replace("Z", "+00:00"))
    cutoff = (target_at - timedelta(hours=max_age_hours)).isoformat()
    output = [dict(point) for point in points]
    index = {
        (point["marketplace"], point["market_hash_name"]): position
        for position, point in enumerate(output)
    }
    successful = con.execute(
        """
        SELECT pp.marketplace, pp.market_hash_name, pp.normalized_price
        FROM price_points pp
        JOIN snapshots s ON s.snapshot_id = pp.snapshot_id
        WHERE s.timestamp < ?
          AND s.timestamp >= ?
          AND pp.fetch_status = 'ok'
          AND pp.normalized_price IS NOT NULL
        ORDER BY s.timestamp DESC, pp.price_point_id DESC
        """,
        (snapshot["timestamp"], cutoff),
    ).fetchall()
    seen: set[tuple[str, str]] = set()
    for row in successful:
        key = (row["marketplace"], row["market_hash_name"])
        if key in seen:
            continue
        seen.add(key)
        carried = {
            "marketplace": row["marketplace"],
            "market_hash_name": row["market_hash_name"],
            "fetch_status": "ok",
            "normalized_price": row["normalized_price"],
        }
        position = index.get(key)
        if position is None:
            index[key] = len(output)
            output.append(carried)
        elif output[position].get("fetch_status") != "ok" or output[position].get("normalized_price") is None:
            output[position] = carried
    return output


def carry_forward_recent_prices(
    results: Iterable[PriceResult],
    max_age_hours: int = 24,
) -> list[PriceResult]:
    output = list(results)
    if not output:
        return output

    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=max_age_hours)).isoformat()
    with connect() as con:
        rows = con.execute(
            """
            SELECT pp.marketplace, pp.market_hash_name, pp.normalized_price, s.timestamp
            FROM price_points pp
            JOIN snapshots s ON s.snapshot_id = pp.snapshot_id
            WHERE s.timestamp >= ?
              AND pp.fetch_status = 'ok'
              AND pp.normalized_price IS NOT NULL
            ORDER BY s.timestamp DESC, pp.price_point_id DESC
            """,
            (cutoff,),
        ).fetchall()

    latest: dict[tuple[str, str], tuple[float, str]] = {}
    for row in rows:
        key = (row["marketplace"], row["market_hash_name"])
        latest.setdefault(key, (float(row["normalized_price"]), str(row["timestamp"])))

    carried: list[PriceResult] = []
    for result in output:
        normalized = normalize_to_usd(result.price, result.currency)
        if result.fetch_status == "ok" and normalized is not None:
            carried.append(result)
            continue
        previous = latest.get((result.marketplace, result.market_hash_name))
        if previous is None:
            carried.append(result)
            continue
        price, timestamp = previous
        carried.append(
            PriceResult(
                marketplace=result.marketplace,
                market_hash_name=result.market_hash_name,
                price=price,
                currency="USD",
                stock_count=result.stock_count,
                fetch_status="ok",
                error_details=f"Last successful price carried forward from {timestamp} (24-hour limit).",
            )
        )
    return carried


def get_enabled_adapter_keys() -> list[str]:
    order = "is_baseline DESC, LOWER(name)" if using_postgres() else "is_baseline DESC, name COLLATE NOCASE"
    with connect() as con:
        rows = con.execute(
            f"""
            SELECT adapter_key
            FROM marketplaces
            WHERE enabled = 1 OR is_baseline = 1
            ORDER BY {order}
            """
        ).fetchall()
    return [row["adapter_key"] for row in rows]


def update_marketplace_settings(rows: Iterable[dict]) -> None:
    now = utc_now_iso()
    with connect() as con:
        for row in rows:
            enabled = 1 if row.get("enabled") or row.get("is_baseline") else 0
            con.execute(
                """
                UPDATE marketplaces
                SET enabled = ?, updated_at = ?
                WHERE adapter_key = ?
                """,
                (enabled, now, row["adapter_key"]),
            )


def save_snapshot_results(results: Iterable[PriceResult]) -> tuple[int, str]:
    result_rows = list(results)
    for attempt in range(3):
        try:
            return _save_snapshot_results_once(result_rows)
        except Exception as exc:
            if not using_postgres() or not _is_deadlock_error(exc) or attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))

    raise RuntimeError("Snapshot save failed.")


def _save_snapshot_results_once(results: list[PriceResult]) -> tuple[int, str]:
    timestamp = utc_now_iso()
    item_map = {row["market_hash_name"]: int(row["item_id"]) for row in get_basket_items()}
    status_by_marketplace: dict[str, list[tuple[str, str | None]]] = {}
    with connect() as con:
        if using_postgres():
            # collect_snapshot() already holds the cross-process
            # cs2dt_neon_sync session lock. Reacquiring that lock through
            # this separate connection would wait on our own session forever.
            cur = con.execute("INSERT INTO snapshots(timestamp) VALUES (?) RETURNING snapshot_id", (timestamp,))
            snapshot_id = int(cur.fetchone()["snapshot_id"])
        else:
            cur = con.execute("INSERT INTO snapshots(timestamp) VALUES (?)", (timestamp,))
            snapshot_id = int(cur.lastrowid)
        for result in results:
            normalized_price = normalize_to_usd(result.price, result.currency)
            con.execute(
                """
                INSERT INTO price_points (
                    snapshot_id, marketplace, item_id, market_hash_name,
                    price, currency, normalized_price, normalized_currency,
                    stock_count, fetch_status, error_details, timestamp
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'USD', ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    result.marketplace,
                    item_map.get(result.market_hash_name),
                    result.market_hash_name,
                    result.price,
                    result.currency,
                    normalized_price,
                    result.stock_count,
                    result.fetch_status,
                    result.error_details,
                    timestamp,
                ),
            )
            status_by_marketplace.setdefault(result.marketplace, []).append(
                (result.fetch_status, result.error_details)
            )

        for marketplace, statuses in status_by_marketplace.items():
            status, error = summarize_fetch_status(statuses)
            con.execute(
                """
                UPDATE marketplaces
                SET last_status = ?, last_error = ?, updated_at = ?
                WHERE name = ?
                """,
                (status, error, timestamp, marketplace),
            )
    return snapshot_id, timestamp


def update_marketplace_statuses_from_results(results: Iterable[PriceResult], timestamp: str | None = None) -> None:
    status_timestamp = timestamp or utc_now_iso()
    status_by_marketplace: dict[str, list[tuple[str, str | None]]] = {}
    for result in results:
        status_by_marketplace.setdefault(result.marketplace, []).append(
            (result.fetch_status, result.error_details)
        )
    if not status_by_marketplace:
        return

    with connect() as con:
        for marketplace, statuses in status_by_marketplace.items():
            status, error = summarize_fetch_status(statuses)
            con.execute(
                """
                UPDATE marketplaces
                SET last_status = ?, last_error = ?, updated_at = ?
                WHERE name = ?
                """,
                (status, error, status_timestamp, marketplace),
            )


def record_update_run(
    *,
    source: str,
    started_at: str,
    finished_at: str,
    duration_seconds: float,
    status: str,
    snapshot_id: int | None = None,
    success_rate: float | None = None,
    error_details: str | None = None,
    step_details: str | None = None,
) -> None:
    safe_source = source if source in {"manual", "automatic", "sync"} else "manual"
    with connect() as con:
        con.execute(
            """
            INSERT INTO update_runs (
                source, started_at, finished_at, duration_seconds, status,
                snapshot_id, success_rate, error_details, step_details
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                safe_source,
                started_at,
                finished_at,
                max(0.0, float(duration_seconds)),
                status,
                snapshot_id,
                success_rate,
                _none_if_blank(error_details),
                _none_if_blank(step_details),
            ),
        )


def update_runs(limit: int = 200) -> list[sqlite3.Row]:
    with connect() as con:
        return list(
            con.execute(
                """
                SELECT update_run_id, source, started_at, finished_at, duration_seconds,
                    status, snapshot_id, success_rate, error_details, step_details
                FROM update_runs
                ORDER BY started_at DESC, update_run_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        )


def get_display_cache(cache_key: str) -> sqlite3.Row | None:
    with connect() as con:
        return con.execute(
            """
            SELECT cache_key, snapshot_id, payload, updated_at
            FROM display_cache
            WHERE cache_key = ?
            """,
            (cache_key,),
        ).fetchone()


def save_display_cache(cache_key: str, snapshot_id: int | None, payload: str) -> None:
    with connect() as con:
        con.execute(
            """
            INSERT INTO display_cache(cache_key, snapshot_id, payload, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                snapshot_id = excluded.snapshot_id,
                payload = excluded.payload,
                updated_at = excluded.updated_at
            """,
            (cache_key, snapshot_id, payload, utc_now_iso()),
        )


def prune_low_quality_snapshots(
    con: DbConnection,
    min_success_rate: float = MIN_SNAPSHOT_SUCCESS_RATE,
) -> int:
    rows = con.execute(
        """
        SELECT
            s.snapshot_id,
            COUNT(pp.price_point_id) AS total_count,
            SUM(
                CASE
                    WHEN pp.fetch_status = 'ok' AND pp.normalized_price IS NOT NULL
                    THEN 1
                    ELSE 0
                END
            ) AS success_count
        FROM snapshots s
        LEFT JOIN price_points pp ON pp.snapshot_id = s.snapshot_id
        GROUP BY s.snapshot_id
        """
    ).fetchall()
    snapshot_ids = [
        int(row["snapshot_id"])
        for row in rows
        if int(row["total_count"] or 0) == 0
        or (float(row["success_count"] or 0) / float(row["total_count"])) < min_success_rate
    ]
    if not snapshot_ids:
        return 0
    placeholders = ",".join("?" for _ in snapshot_ids)
    con.execute(f"DELETE FROM price_points WHERE snapshot_id IN ({placeholders})", snapshot_ids)
    con.execute(f"DELETE FROM snapshots WHERE snapshot_id IN ({placeholders})", snapshot_ids)
    return len(snapshot_ids)


def summarize_fetch_status(statuses: list[tuple[str, str | None]]) -> tuple[str, str | None]:
    total = len(statuses)
    ok_count = sum(1 for status, _ in statuses if status == "ok")
    missing_count = sum(1 for status, _ in statuses if status == "missing")
    error_count = sum(1 for status, _ in statuses if status == "error")
    first_error = next((error for _, error in statuses if error), None)

    if ok_count == total:
        return "ok", None
    if error_count == total:
        return "error", first_error
    if missing_count == total:
        return "missing", first_error
    summary = f"{ok_count} ok, {missing_count} missing"
    if error_count:
        summary += f", {error_count} error"
    return "partial", summary


def normalize_to_usd(price: float | None, currency: str) -> float | None:
    if price is None:
        return None
    currency = (currency or "USD").upper()
    if currency == "USD":
        return price
    rate = os.getenv(f"FX_{currency}_TO_USD")
    if not rate:
        return None
    try:
        return price * float(rate)
    except ValueError:
        return None


def latest_snapshot() -> sqlite3.Row | None:
    with connect() as con:
        return con.execute(
            "SELECT * FROM snapshots ORDER BY timestamp DESC, snapshot_id DESC LIMIT 1"
        ).fetchone()


def latest_price_points() -> list[sqlite3.Row]:
    snapshot = latest_snapshot()
    if snapshot is None:
        return []
    return price_points_for_snapshot(int(snapshot["snapshot_id"]))


def price_points_for_snapshot(snapshot_id: int) -> list[sqlite3.Row]:
    with connect() as con:
        return list(
            con.execute(
                """
                SELECT pp.*, bi.active, bi.multiplier
                FROM price_points pp
                LEFT JOIN basket_items bi ON bi.item_id = pp.item_id
                WHERE pp.snapshot_id = ?
                ORDER BY pp.marketplace, pp.market_hash_name
                """,
                (snapshot_id,),
            ).fetchall()
        )


def update_latest_missing_price_points(marketplace: str, results: Iterable[PriceResult]) -> int:
    return update_latest_repair_price_points(marketplace, results)


def update_latest_repair_price_points(
    marketplace: str,
    results: Iterable[PriceResult],
    overwrite_market_hash_names: set[str] | None = None,
) -> int:
    snapshot = latest_snapshot()
    if snapshot is None:
        return 0

    overwrite_market_hash_names = overwrite_market_hash_names or set()
    item_map = {row["market_hash_name"]: int(row["item_id"]) for row in get_basket_items()}
    timestamp = snapshot["timestamp"]
    updated_count = 0
    with connect() as con:
        for result in results:
            if result.marketplace != marketplace:
                continue
            normalized_price = normalize_to_usd(result.price, result.currency)
            if result.fetch_status != "ok" or normalized_price is None:
                continue

            existing = con.execute(
                """
                SELECT fetch_status, normalized_price
                FROM price_points
                WHERE snapshot_id = ? AND marketplace = ? AND market_hash_name = ?
                """,
                (snapshot["snapshot_id"], marketplace, result.market_hash_name),
            ).fetchone()
            should_overwrite = result.market_hash_name in overwrite_market_hash_names
            if (
                existing
                and existing["fetch_status"] == "ok"
                and existing["normalized_price"] is not None
                and not should_overwrite
            ):
                continue

            if existing:
                con.execute(
                    """
                    UPDATE price_points
                    SET price = ?,
                        currency = ?,
                        normalized_price = ?,
                        normalized_currency = 'USD',
                        stock_count = ?,
                        fetch_status = ?,
                        error_details = ?,
                        timestamp = ?
                    WHERE snapshot_id = ? AND marketplace = ? AND market_hash_name = ?
                    """,
                    (
                        result.price,
                        result.currency,
                        normalized_price,
                        result.stock_count,
                        result.fetch_status,
                        result.error_details,
                        timestamp,
                        snapshot["snapshot_id"],
                        marketplace,
                        result.market_hash_name,
                    ),
                )
            else:
                con.execute(
                    """
                    INSERT INTO price_points (
                        snapshot_id, marketplace, item_id, market_hash_name,
                        price, currency, normalized_price, normalized_currency,
                        stock_count, fetch_status, error_details, timestamp
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'USD', ?, ?, ?, ?)
                    """,
                    (
                        snapshot["snapshot_id"],
                        marketplace,
                        item_map.get(result.market_hash_name),
                        result.market_hash_name,
                        result.price,
                        result.currency,
                        normalized_price,
                        result.stock_count,
                        result.fetch_status,
                        result.error_details,
                        timestamp,
                    ),
                )
            updated_count += 1

        refresh_marketplace_status(con, marketplace, int(snapshot["snapshot_id"]), timestamp)
    return updated_count


def sync_sqlite_to_postgres() -> dict[str, int]:
    if not DB_PATH.exists():
        raise RuntimeError(f"Local SQLite database not found: {DB_PATH}")
    if not postgres_database_url():
        raise RuntimeError("DATABASE_URL is not configured.")

    started = time.perf_counter()
    for attempt in range(3):
        try:
            counts = _sync_sqlite_to_postgres_once()
            counts["elapsed_seconds"] = time.perf_counter() - started
            return counts
        except Exception as exc:
            if not _is_deadlock_error(exc) or attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))

    raise RuntimeError("Neon sync failed.")


def _sync_sqlite_to_postgres_once() -> dict[str, int]:
    counts = {
        "basket_items": 0,
        "marketplaces": 0,
        "snapshots": 0,
        "price_points": 0,
        "pulled_snapshots": 0,
        "pulled_price_points": 0,
        "update_runs": 0,
        "pulled_update_runs": 0,
        "replaced_snapshots": 0,
        "checked_snapshots": 0,
        "unchanged_snapshots": 0,
        "full_reconcile": 0,
        "daily_rollups": 0,
        "deleted_remote_snapshots": 0,
    }
    manifest = _load_neon_sync_manifest()
    force_full_reconcile = _neon_full_reconcile_due(manifest)
    counts["full_reconcile"] = int(force_full_reconcile)
    local_revision_before_sync = _local_sync_revision()
    local_data_changed = manifest.get("local_revision") != local_revision_before_sync
    source = sqlite3.connect(DB_PATH)
    source.row_factory = sqlite3.Row
    try:
        with connect_postgres() as target:
            _acquire_postgres_sync_lock(target)
            target.executescript(_schema_sql("postgres"))
            ensure_history_rollup_schema(target)
            ensure_price_point_uniqueness(target)
            ensure_update_run_uniqueness(target)
            target.execute("ALTER TABLE update_runs ADD COLUMN IF NOT EXISTS step_details TEXT")
            _sync_basket_items_to_postgres(source, target, counts)
            _sync_marketplaces_to_postgres(source, target, counts)
            _sync_daily_rollups_to_postgres(source, target, counts)
            retention_cutoff = _retention_watermark(source)
            if retention_cutoff:
                _delete_postgres_compacted_history(target, retention_cutoff, counts)
            update_runs_push_cursor = _sync_update_runs_to_postgres(
                source,
                target,
                counts,
                since=None if force_full_reconcile else manifest.get("update_runs_push_cursor"),
            )
            synced_manifest = manifest["snapshot_signatures"]
            if local_data_changed or force_full_reconcile:
                if force_full_reconcile:
                    local_snapshots = _sqlite_snapshot_states(source)
                    remote_signatures = _postgres_snapshot_signatures(target)
                else:
                    local_snapshots = _sqlite_snapshots_needing_check(source, manifest["snapshot_signatures"])
                    remote_signatures = {}
                remote_snapshots = _postgres_snapshot_metadata(
                    target, timestamps=None if force_full_reconcile else list(local_snapshots)
                )
                item_map = {
                    row["market_hash_name"]: int(row["item_id"])
                    for row in target.execute("SELECT item_id, market_hash_name FROM basket_items").fetchall()
                }
                counts["checked_snapshots"] = len(local_snapshots)
                for timestamp, local_snapshot in local_snapshots.items():
                    remote_snapshot = remote_snapshots.get(timestamp)
                    if force_full_reconcile:
                        needs_push = (
                            remote_snapshot is None
                            or remote_signatures.get(timestamp) != local_snapshot["signature"]
                        )
                    else:
                        needs_push = (
                            remote_snapshot is None
                            or int(remote_snapshot["point_count"]) != int(local_snapshot["point_count"])
                            or manifest["snapshot_signatures"].get(timestamp) != local_snapshot["signature"]
                        )
                    if not needs_push:
                        counts["unchanged_snapshots"] += 1
                        continue

                    if remote_snapshot is None:
                        cur = target.execute(
                            "INSERT INTO snapshots(timestamp) VALUES (?) RETURNING snapshot_id",
                            (timestamp,),
                        )
                        target_snapshot_id = int(cur.fetchone()["snapshot_id"])
                    else:
                        target_snapshot_id = int(remote_snapshot["snapshot_id"])
                        target.execute("DELETE FROM price_points WHERE snapshot_id = ?", (target_snapshot_id,))
                        counts["replaced_snapshots"] += 1
                    point_values = _sqlite_snapshot_point_values(source, int(local_snapshot["snapshot_id"]))
                    batch = [
                        (
                            target_snapshot_id,
                            values[0],
                            item_map.get(values[1]),
                            *values[1:],
                        )
                        for values in point_values
                    ]
                    if batch:
                        target.executemany(
                            """
                            INSERT INTO price_points (
                                snapshot_id, marketplace, item_id, market_hash_name,
                                price, currency, normalized_price, normalized_currency,
                                stock_count, fetch_status, error_details, timestamp
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            batch,
                        )
                    counts["snapshots"] += 1
                    counts["price_points"] += len(batch)
                if force_full_reconcile:
                    synced_manifest = {
                        timestamp: str(snapshot["signature"])
                        for timestamp, snapshot in local_snapshots.items()
                    }
                else:
                    synced_manifest = dict(manifest["snapshot_signatures"])
                    synced_manifest.update(
                        {
                            timestamp: str(snapshot["signature"])
                            for timestamp, snapshot in local_snapshots.items()
                        }
                    )
            _sync_missing_postgres_snapshots_to_sqlite(
                source, target, counts, retention_cutoff=retention_cutoff
            )
            update_runs_pull_cursor = _sync_missing_postgres_update_runs_to_sqlite(
                source,
                target,
                counts,
                since=None if force_full_reconcile else manifest.get("update_runs_pull_cursor"),
            )
            source.commit()
            if counts["pulled_snapshots"]:
                synced_manifest = _sqlite_snapshot_signatures(source)
    finally:
        source.close()
    _save_neon_sync_manifest(
        synced_manifest,
        full_reconciled_at=time.time() if force_full_reconcile else manifest.get("last_full_reconciled_at"),
        local_revision=_local_sync_revision(),
        update_runs_push_cursor=update_runs_push_cursor,
        update_runs_pull_cursor=update_runs_pull_cursor,
    )
    return counts


def _acquire_postgres_sync_lock(target: DbConnection) -> None:
    target.execute("SELECT pg_advisory_xact_lock(hashtext(?))", ("cs2dt_neon_sync",))


def _is_deadlock_error(exc: Exception) -> bool:
    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate == "40P01":
        return True
    return "deadlock detected" in str(exc).lower()


_SNAPSHOT_SIGNATURE_VERSION = 2


def _retention_watermark(con: DbConnection) -> str | None:
    row = con.execute(
        "SELECT cutoff_timestamp FROM history_retention_state WHERE state_key = ?",
        ("daily_compaction",),
    ).fetchone()
    return str(row["cutoff_timestamp"]) if row else None


def _delete_postgres_compacted_history(target: DbConnection, cutoff: str, counts: dict[str, int]) -> None:
    old_snapshots = target.execute(
        "SELECT snapshot_id FROM snapshots WHERE timestamp < ?",
        (cutoff,),
    ).fetchall()
    if not old_snapshots:
        return
    snapshot_ids = [int(row["snapshot_id"]) for row in old_snapshots]
    placeholders = ",".join("?" for _ in snapshot_ids)
    target.execute(
        f"DELETE FROM price_points WHERE snapshot_id IN ({placeholders})",
        snapshot_ids,
    )
    target.execute(
        f"DELETE FROM snapshots WHERE snapshot_id IN ({placeholders})",
        snapshot_ids,
    )
    counts["deleted_remote_snapshots"] = len(snapshot_ids)
def _sync_daily_rollups_to_postgres(source: sqlite3.Connection, target: DbConnection, counts: dict[str, int]) -> None:
    rows = source.execute("SELECT * FROM history_daily_totals ORDER BY period_start, marketplace").fetchall()
    if rows:
        target.executemany(
            """
            INSERT INTO history_daily_totals (
                period_start, marketplace, average_total_cost, min_total_cost,
                max_total_cost, sample_count, first_sample_at, last_sample_at, aggregated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(period_start, marketplace) DO UPDATE SET
                average_total_cost=excluded.average_total_cost,
                min_total_cost=excluded.min_total_cost,
                max_total_cost=excluded.max_total_cost,
                sample_count=excluded.sample_count,
                first_sample_at=excluded.first_sample_at,
                last_sample_at=excluded.last_sample_at,
                aggregated_at=excluded.aggregated_at
            """,
            [tuple(row) for row in rows],
        )
        counts["daily_rollups"] = len(rows)
    state = source.execute(
        "SELECT * FROM history_retention_state WHERE state_key = ?",
        ("daily_compaction",),
    ).fetchone()
    if state:
        target.execute(
            """
            INSERT INTO history_retention_state(state_key, cutoff_timestamp, completed_at, operation_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(state_key) DO UPDATE SET cutoff_timestamp=excluded.cutoff_timestamp,
                completed_at=excluded.completed_at, operation_id=excluded.operation_id
            """,
            tuple(state),
        )


def _neon_full_reconcile_due(manifest: dict) -> bool:
    if manifest.get("signature_version") != _SNAPSHOT_SIGNATURE_VERSION:
        return True
    try:
        last_full = float(manifest.get("last_full_reconciled_at") or 0)
        hours = max(1.0, float(os.getenv("NEON_FULL_RECONCILE_HOURS", "24")))
    except (TypeError, ValueError):
        return True
    return time.time() - last_full >= hours * 60.0 * 60.0


def _load_neon_sync_manifest() -> dict:
    try:
        payload = json.loads(NEON_SYNC_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    signatures = payload.get("snapshot_signatures")
    return {
        "snapshot_signatures": {
            str(timestamp): str(signature)
            for timestamp, signature in (signatures.items() if isinstance(signatures, dict) else [])
        },
        "last_full_reconciled_at": payload.get("last_full_reconciled_at"),
        "local_revision": payload.get("local_revision"),
        "update_runs_push_cursor": payload.get("update_runs_push_cursor"),
        "update_runs_pull_cursor": payload.get("update_runs_pull_cursor"),
        "signature_version": payload.get("signature_version"),
    }


def _save_neon_sync_manifest(
    snapshot_signatures: dict[str, str],
    *,
    full_reconciled_at,
    local_revision: dict[str, int],
    update_runs_push_cursor: str | None = None,
    update_runs_pull_cursor: str | None = None,
) -> None:
    payload = {
        "snapshot_signatures": snapshot_signatures,
        "last_full_reconciled_at": full_reconciled_at,
        "local_revision": local_revision,
        "update_runs_push_cursor": update_runs_push_cursor,
        "update_runs_pull_cursor": update_runs_pull_cursor,
        "signature_version": _SNAPSHOT_SIGNATURE_VERSION,
    }
    try:
        NEON_SYNC_MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = NEON_SYNC_MANIFEST_PATH.with_suffix(".tmp")
        temporary_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        temporary_path.replace(NEON_SYNC_MANIFEST_PATH)
    except OSError:
        # A failed manifest write only causes a future sync to reconcile again.
        pass


def _sqlite_snapshot_point_values(source: sqlite3.Connection, snapshot_id: int) -> list[tuple]:
    rows = source.execute(
        """
        SELECT marketplace, market_hash_name, price, currency, normalized_price,
            normalized_currency, stock_count, fetch_status, error_details, timestamp
        FROM price_points
        WHERE snapshot_id = ?
        ORDER BY marketplace, market_hash_name, price_point_id
        """,
        (snapshot_id,),
    ).fetchall()
    return [_price_point_signature_values(row) for row in rows]


def _sqlite_snapshot_signatures(source: sqlite3.Connection) -> dict[str, str]:
    return {
        timestamp: str(snapshot["signature"])
        for timestamp, snapshot in _sqlite_snapshot_states(source).items()
    }


def _sqlite_snapshot_states(source: sqlite3.Connection) -> dict[str, dict[str, int | str]]:
    snapshots: dict[str, dict[str, int | str]] = {}
    for snapshot in source.execute("SELECT snapshot_id, timestamp FROM snapshots ORDER BY snapshot_id"):
        values = _sqlite_snapshot_point_values(source, int(snapshot["snapshot_id"]))
        snapshots[str(snapshot["timestamp"])] = {
            "snapshot_id": int(snapshot["snapshot_id"]),
            "signature": _snapshot_signature(values),
            "point_count": len(values),
        }
    return snapshots


def _sqlite_snapshots_needing_check(
    source: sqlite3.Connection, cached_signatures: dict[str, str]
) -> dict[str, dict[str, int | str]]:
    """Re-hash only snapshots not yet in the manifest cache, plus the latest one.

    Snapshots are immutable once a newer one exists, except the single most
    recent snapshot which "Repair Missing Marketplace Prices" can update in
    place. Trusting the cache for everything else turns a full rescan of
    every historical snapshot into checking just what's new since the last
    sync (see db.py's Neon sync notes for the full reasoning).
    """
    local_ids = source.execute("SELECT snapshot_id, timestamp FROM snapshots ORDER BY snapshot_id").fetchall()
    latest_timestamp = str(local_ids[-1]["timestamp"]) if local_ids else None
    must_check_timestamps = {
        str(row["timestamp"])
        for row in local_ids
        if str(row["timestamp"]) not in cached_signatures or str(row["timestamp"]) == latest_timestamp
    }
    snapshots: dict[str, dict[str, int | str]] = {}
    for row in local_ids:
        timestamp = str(row["timestamp"])
        if timestamp not in must_check_timestamps:
            continue
        values = _sqlite_snapshot_point_values(source, int(row["snapshot_id"]))
        snapshots[timestamp] = {
            "snapshot_id": int(row["snapshot_id"]),
            "signature": _snapshot_signature(values),
            "point_count": len(values),
        }
    return snapshots


def _local_sync_revision() -> dict[str, int]:
    paths = (DB_PATH, DB_PATH.with_name(f"{DB_PATH.name}-wal"))
    revision: dict[str, int] = {}
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        revision[path.name] = int(stat.st_mtime_ns)
        revision[f"{path.name}:size"] = int(stat.st_size)
    return revision


def _postgres_snapshot_metadata(
    target: DbConnection, timestamps: list[str] | None = None
) -> dict[str, dict[str, int]]:
    if timestamps is not None and not timestamps:
        return {}
    query = """
        SELECT s.snapshot_id, s.timestamp, COUNT(pp.price_point_id) AS point_count
        FROM snapshots s
        LEFT JOIN price_points pp ON pp.snapshot_id = s.snapshot_id
    """
    params: tuple = ()
    if timestamps is not None:
        placeholders = ",".join("?" for _ in timestamps)
        query += f" WHERE s.timestamp IN ({placeholders})"
        params = tuple(timestamps)
    query += " GROUP BY s.snapshot_id, s.timestamp ORDER BY s.snapshot_id"
    rows = target.execute(query, params).fetchall()
    return {
        str(row["timestamp"]): {
            "snapshot_id": int(row["snapshot_id"]),
            "point_count": int(row["point_count"] or 0),
        }
        for row in rows
    }


_POSTGRES_SNAPSHOT_SIGNATURES_SQL = """
    SELECT s.timestamp,
           encode(
               sha256(
                   convert_to(
                       COALESCE(
                           string_agg(
                               pp.marketplace
                                   || chr(31) || pp.market_hash_name
                                   || chr(31) || CASE WHEN pp.price IS NULL THEN '' ELSE (trunc(pp.price * 1000000.0::float8)::bigint)::text END
                                   || chr(31) || COALESCE(NULLIF(pp.currency, ''), 'USD')
                                   || chr(31) || CASE WHEN pp.normalized_price IS NULL THEN '' ELSE (trunc(pp.normalized_price * 1000000.0::float8)::bigint)::text END
                                   || chr(31) || COALESCE(NULLIF(pp.normalized_currency, ''), 'USD')
                                   || chr(31) || CASE WHEN pp.stock_count IS NULL THEN '' ELSE pp.stock_count::text END
                                   || chr(31) || COALESCE(pp.fetch_status, '')
                                   || chr(31) || COALESCE(NULLIF(btrim(pp.error_details), ''), '')
                                   || chr(31) || pp.timestamp,
                               E'\n' ORDER BY pp.marketplace, pp.market_hash_name
                           ),
                           ''
                       ),
                       'UTF8'
                   )
               ),
               'hex'
           ) AS signature
    FROM snapshots s
    LEFT JOIN price_points pp ON pp.snapshot_id = s.snapshot_id
    GROUP BY s.snapshot_id, s.timestamp
    ORDER BY s.snapshot_id
"""


def _postgres_snapshot_signatures(target: DbConnection) -> dict[str, str]:
    """Per-snapshot signatures computed entirely on the Postgres side.

    Replaces the previous approach of SELECTing every ``price_points`` row back
    over the wire (350k+ rows on a ~500ms round-trip connection) and hashing in
    Python; that took minutes. This only returns one row per snapshot.
    """
    rows = target.execute(_POSTGRES_SNAPSHOT_SIGNATURES_SQL).fetchall()
    return {
        str(row["timestamp"]): row["signature"]
        for row in rows
        if row["signature"] is not None
    }


def _price_point_signature_values(row, *, timestamp_key: str = "timestamp") -> tuple:
    return (
        str(row["marketplace"]),
        str(row["market_hash_name"]),
        _float_or_none(row["price"]),
        row["currency"] or "USD",
        _float_or_none(row["normalized_price"]),
        row["normalized_currency"] or "USD",
        _int_or_none(row["stock_count"]),
        row["fetch_status"],
        _none_if_blank(row["error_details"]),
        row[timestamp_key],
    )


def _signature_line(values: tuple) -> str:
    """Canonical one-line encoding of a single price point.

    Must match the SQL used by ``_postgres_snapshot_signatures`` byte-for-byte:
    floats are encoded as integer micros (``trunc(value * 1e6)``) so the value
    is exact and identical on both SQLite (via Python) and Postgres (via SQL),
    rather than depending on either engine's float-to-text formatting.
    """
    (
        marketplace,
        market_hash_name,
        price,
        currency,
        normalized_price,
        normalized_currency,
        stock_count,
        fetch_status,
        error_details,
        point_timestamp,
    ) = values
    return "\x1f".join(
        (
            marketplace,
            market_hash_name,
            "" if price is None else str(int(price * 1_000_000.0)),
            currency,
            "" if normalized_price is None else str(int(normalized_price * 1_000_000.0)),
            normalized_currency,
            "" if stock_count is None else str(stock_count),
            fetch_status or "",
            (error_details or "").strip(),
            point_timestamp,
        )
    )


def _snapshot_signature(values: list[tuple]) -> str:
    return hashlib.sha256(
        "\n".join(_signature_line(row) for row in values).encode("utf-8")
    ).hexdigest()


def _sync_missing_postgres_snapshots_to_sqlite(
    target: sqlite3.Connection,
    source: DbConnection,
    counts: dict[str, int],
    *,
    retention_cutoff: str | None = None,
) -> None:
    local_timestamps = {
        row["timestamp"]
        for row in target.execute("SELECT timestamp FROM snapshots")
    }
    item_map = {
        row["market_hash_name"]: int(row["item_id"])
        for row in target.execute("SELECT item_id, market_hash_name FROM basket_items")
    }

    query = "SELECT * FROM snapshots"
    params: tuple = ()
    if retention_cutoff is not None:
        query += " WHERE timestamp >= ?"
        params = (retention_cutoff,)
    query += " ORDER BY timestamp, snapshot_id"
    rows = source.execute(query, params).fetchall()
    for snapshot in rows:
        timestamp = snapshot["timestamp"]
        if timestamp in local_timestamps:
            continue

        cur = target.execute("INSERT INTO snapshots(timestamp) VALUES (?)", (timestamp,))
        local_snapshot_id = int(cur.lastrowid)
        batch = []
        status_by_marketplace: dict[str, list[tuple[str, str | None]]] = {}
        for point in source.execute(
            "SELECT * FROM price_points WHERE snapshot_id = ? ORDER BY price_point_id",
            (snapshot["snapshot_id"],),
        ).fetchall():
            status_by_marketplace.setdefault(point["marketplace"], []).append(
                (point["fetch_status"], point["error_details"])
            )
            batch.append(
                (
                    local_snapshot_id,
                    point["marketplace"],
                    item_map.get(point["market_hash_name"]),
                    point["market_hash_name"],
                    _float_or_none(point["price"]),
                    point["currency"] or "USD",
                    _float_or_none(point["normalized_price"]),
                    point["normalized_currency"] or "USD",
                    _int_or_none(point["stock_count"]),
                    point["fetch_status"],
                    _none_if_blank(point["error_details"]),
                    point["timestamp"],
                )
            )

        if batch:
            target.executemany(
                """
                INSERT INTO price_points (
                    snapshot_id, marketplace, item_id, market_hash_name,
                    price, currency, normalized_price, normalized_currency,
                    stock_count, fetch_status, error_details, timestamp
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                batch,
            )
        for marketplace, statuses in status_by_marketplace.items():
            status, error = summarize_fetch_status(statuses)
            target.execute(
                """
                UPDATE marketplaces
                SET last_status = ?, last_error = ?, updated_at = ?
                WHERE name = ?
                """,
                (status, error, timestamp, marketplace),
            )
        local_timestamps.add(timestamp)
        counts["pulled_snapshots"] += 1
        counts["pulled_price_points"] += len(batch)


def _sync_update_runs_to_postgres(
    source: sqlite3.Connection,
    target: DbConnection,
    counts: dict[str, int],
    *,
    since: str | None,
) -> str | None:
    query = "SELECT * FROM update_runs"
    params: tuple = ()
    if since is not None:
        query += " WHERE started_at > ?"
        params = (since,)
    query += " ORDER BY started_at, update_run_id"
    rows = source.execute(query, params).fetchall()
    if not rows:
        return since

    batch = [
        (
            row["source"],
            row["started_at"],
            row["finished_at"],
            _float_or_none(row["duration_seconds"]) or 0.0,
            row["status"],
            _int_or_none(row["snapshot_id"]),
            _float_or_none(row["success_rate"]),
            _none_if_blank(row["error_details"]),
            _none_if_blank(row["step_details"]),
        )
        for row in rows
    ]
    target.executemany(
        """
        INSERT INTO update_runs (
            source, started_at, finished_at, duration_seconds, status,
            snapshot_id, success_rate, error_details, step_details
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (source, started_at) DO UPDATE SET
            finished_at = excluded.finished_at,
            duration_seconds = excluded.duration_seconds,
            status = excluded.status,
            snapshot_id = excluded.snapshot_id,
            success_rate = excluded.success_rate,
            error_details = excluded.error_details,
            step_details = excluded.step_details
        """,
        batch,
    )
    counts["update_runs"] += len(batch)
    return str(rows[-1]["started_at"])


def _sync_missing_postgres_update_runs_to_sqlite(
    target: sqlite3.Connection,
    source: DbConnection,
    counts: dict[str, int],
    *,
    since: str | None,
) -> str | None:
    query = "SELECT * FROM update_runs"
    params: tuple = ()
    if since is not None:
        query += " WHERE started_at > ?"
        params = (since,)
    query += " ORDER BY started_at, update_run_id"
    rows = source.execute(query, params).fetchall()
    if not rows:
        return since

    batch = [
        (
            row["source"],
            row["started_at"],
            row["finished_at"],
            _float_or_none(row["duration_seconds"]) or 0.0,
            row["status"],
            _int_or_none(row["snapshot_id"]),
            _float_or_none(row["success_rate"]),
            _none_if_blank(row["error_details"]),
            _none_if_blank(row["step_details"]),
        )
        for row in rows
    ]
    target.executemany(
        """
        INSERT OR IGNORE INTO update_runs (
            source, started_at, finished_at, duration_seconds, status,
            snapshot_id, success_rate, error_details, step_details
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        batch,
    )
    counts["pulled_update_runs"] += len(batch)
    return str(rows[-1]["started_at"])


def _sync_basket_items_to_postgres(source: sqlite3.Connection, target: DbConnection, counts: dict[str, int]) -> None:
    rows = source.execute("SELECT * FROM basket_items ORDER BY item_id").fetchall()
    if not rows:
        return
    batch = [
        (
            row["market_hash_name"],
            _int_or_default(row["active"], 1),
            _int_or_default(row["multiplier"], 1),
            row["notes"] or "",
            _int_or_none(row["source_rank"]),
            _float_or_none(row["source_amount"]),
            _none_if_blank(row["price_compare_url"]),
            _none_if_blank(row["priceempire_url"]),
            _none_if_blank(row["steamanalyst_url"]),
            _none_if_blank(row["marketplace_links_json"]),
            row["created_at"] or utc_now_iso(),
        )
        for row in rows
    ]
    target.executemany(
        """
        INSERT INTO basket_items (
            market_hash_name, active, multiplier, notes, source_rank,
            source_amount, price_compare_url, priceempire_url, steamanalyst_url,
            marketplace_links_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        batch,
    )
    counts["basket_items"] += len(batch)


def _sync_marketplaces_to_postgres(source: sqlite3.Connection, target: DbConnection, counts: dict[str, int]) -> None:
    rows = source.execute("SELECT * FROM marketplaces ORDER BY adapter_key").fetchall()
    if not rows:
        return
    batch = [
        (
            row["adapter_key"],
            row["name"],
            _int_or_default(row["enabled"], 1),
            _int_or_default(row["is_baseline"], 0),
            _int_or_default(row["requires_credentials"], 0),
            _none_if_blank(row["last_status"]),
            _none_if_blank(row["last_error"]),
            _none_if_blank(row["updated_at"]),
        )
        for row in rows
    ]
    target.executemany(
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
        batch,
    )
    counts["marketplaces"] += len(batch)


def _none_if_blank(value):
    if value is None:
        return None
    text = str(value)
    return None if text.strip() == "" else value


def _int_or_none(value) -> int | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _int_or_default(value, default: int) -> int:
    parsed = _int_or_none(value)
    return default if parsed is None else parsed


def _float_or_none(value) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def refresh_marketplace_status(
    con: DbConnection,
    marketplace: str,
    snapshot_id: int,
    timestamp: str,
) -> None:
    rows = con.execute(
        """
        SELECT fetch_status, error_details
        FROM price_points
        WHERE snapshot_id = ? AND marketplace = ?
        """,
        (snapshot_id, marketplace),
    ).fetchall()
    if not rows:
        return
    status, error = summarize_fetch_status(
        [(row["fetch_status"], row["error_details"]) for row in rows]
    )
    con.execute(
        """
        UPDATE marketplaces
        SET last_status = ?, last_error = ?, updated_at = ?
        WHERE name = ?
        """,
        (status, error, timestamp, marketplace),
    )


def _history_period_start(timestamp: str) -> str:
    parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    local = parsed.astimezone(HISTORY_TIMEZONE)
    hour = 0 if local.hour < 12 else 12
    return local.replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()


def _history_calendar_day(timestamp: str) -> str:
    parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(HISTORY_TIMEZONE).date().isoformat()


def _history_cutoff_iso(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return (current.astimezone(timezone.utc) - timedelta(hours=24)).isoformat(timespec="milliseconds")


def _eligible_compaction_snapshot_ids(con: DbConnection, cutoff: str) -> list[int]:
    rows = con.execute(
        "SELECT snapshot_id, timestamp FROM snapshots WHERE timestamp < ? ORDER BY snapshot_id",
        (cutoff,),
    ).fetchall()
    if not rows:
        return []
    latest_by_period: dict[str, str] = {}
    for row in rows:
        period = _history_period_start(row["timestamp"])
        latest_by_period[period] = max(latest_by_period.get(period, ""), str(row["timestamp"]))
    eligible_periods = {
        period for period, latest_timestamp in latest_by_period.items() if latest_timestamp < cutoff
    }
    return [int(row["snapshot_id"]) for row in rows if _history_period_start(row["timestamp"]) in eligible_periods]


def _history_total_rows_for_snapshot_ids(
    con: DbConnection,
    snapshot_ids: list[int],
) -> list[dict]:
    if not snapshot_ids:
        return []
    placeholders = ",".join("?" for _ in snapshot_ids)
    multiplier_expr = (
        "LEAST(GREATEST(COALESCE(bi.multiplier, 1), 1), 1000)"
        if con.backend == "postgres"
        else "MIN(MAX(COALESCE(bi.multiplier, 1), 1), 1000)"
    )
    rows = con.execute(
        f"""
        WITH snapshot_marketplaces AS (
            SELECT DISTINCT snapshot_id, marketplace FROM price_points
            WHERE snapshot_id IN ({placeholders})
        ), totals AS (
            SELECT s.snapshot_id, s.timestamp, sm.marketplace,
                SUM(COALESCE(pp.normalized_price,
                    CASE WHEN sm.marketplace IN ('Buff163', 'YouPin')
                         THEN COALESCE(c5game.normalized_price, baseline.normalized_price)
                         WHEN sm.marketplace != ? THEN baseline.normalized_price
                         ELSE NULL END) * {multiplier_expr}) AS total_cost
            FROM snapshots s
            JOIN snapshot_marketplaces sm ON sm.snapshot_id = s.snapshot_id
            JOIN basket_items bi ON bi.active = 1
            LEFT JOIN price_points pp ON pp.snapshot_id = s.snapshot_id
                AND pp.marketplace = sm.marketplace AND pp.item_id = bi.item_id
                AND pp.fetch_status = 'ok' AND pp.normalized_price IS NOT NULL
            LEFT JOIN price_points baseline ON baseline.snapshot_id = s.snapshot_id
                AND baseline.marketplace = ? AND baseline.item_id = bi.item_id
                AND baseline.fetch_status = 'ok' AND baseline.normalized_price IS NOT NULL
            LEFT JOIN price_points c5game ON c5game.snapshot_id = s.snapshot_id
                AND c5game.marketplace = 'C5Game' AND c5game.item_id = bi.item_id
                AND c5game.fetch_status = 'ok' AND c5game.normalized_price IS NOT NULL
            WHERE s.snapshot_id IN ({placeholders})
            GROUP BY s.snapshot_id, s.timestamp, sm.marketplace
        )
        SELECT * FROM totals WHERE total_cost IS NOT NULL
        ORDER BY timestamp, marketplace
        """,
        [*snapshot_ids, BASELINE_MARKETPLACE, BASELINE_MARKETPLACE, *snapshot_ids],
    ).fetchall()
    return [dict(row) for row in rows]

def _history_total_rows_for_snapshots(
    con: DbConnection,
    *,
    before_iso: str | None = None,
    since_iso: str | None = None,
) -> list[dict]:
    params: list[str] = [BASELINE_MARKETPLACE, BASELINE_MARKETPLACE, BASELINE_MARKETPLACE]
    where = "WHERE bi.active = 1"
    if before_iso:
        where += " AND s.timestamp < ?"
        params.append(before_iso)
    if since_iso:
        where += " AND s.timestamp >= ?"
        params.append(since_iso)
    multiplier_expr = (
        "LEAST(GREATEST(COALESCE(bi.multiplier, 1), 1), 1000)"
        if con.backend == "postgres"
        else "MIN(MAX(COALESCE(bi.multiplier, 1), 1), 1000)"
    )
    rows = con.execute(
        f"""
        WITH snapshot_marketplaces AS (
            SELECT DISTINCT snapshot_id, marketplace FROM price_points
        ), totals AS (
            SELECT s.snapshot_id, s.timestamp, sm.marketplace,
                SUM(COALESCE(pp.normalized_price,
                    CASE WHEN sm.marketplace IN ('Buff163', 'YouPin')
                         THEN COALESCE(c5game.normalized_price, baseline.normalized_price)
                         WHEN sm.marketplace != ? THEN baseline.normalized_price
                         ELSE NULL END) * {multiplier_expr}) AS total_cost,
                COUNT(pp.normalized_price) AS available_count,
                SUM(CASE WHEN sm.marketplace != ? AND pp.normalized_price IS NULL AND
                    (baseline.normalized_price IS NOT NULL OR
                     (sm.marketplace IN ('Buff163', 'YouPin') AND c5game.normalized_price IS NOT NULL))
                    THEN 1 ELSE 0 END) AS fallback_count
            FROM snapshots s
            JOIN snapshot_marketplaces sm ON sm.snapshot_id = s.snapshot_id
            JOIN basket_items bi ON bi.active = 1
            LEFT JOIN price_points pp ON pp.snapshot_id = s.snapshot_id
                AND pp.marketplace = sm.marketplace AND pp.item_id = bi.item_id
                AND pp.fetch_status = 'ok' AND pp.normalized_price IS NOT NULL
            LEFT JOIN price_points baseline ON baseline.snapshot_id = s.snapshot_id
                AND baseline.marketplace = ? AND baseline.item_id = bi.item_id
                AND baseline.fetch_status = 'ok' AND baseline.normalized_price IS NOT NULL
            LEFT JOIN price_points c5game ON c5game.snapshot_id = s.snapshot_id
                AND c5game.marketplace = 'C5Game' AND c5game.item_id = bi.item_id
                AND c5game.fetch_status = 'ok' AND c5game.normalized_price IS NOT NULL
            {where}
            GROUP BY s.snapshot_id, s.timestamp, sm.marketplace
        )
        SELECT * FROM totals WHERE total_cost IS NOT NULL
        ORDER BY timestamp, marketplace
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]
def history_totals(since_iso: str | None = None) -> list[dict]:
    with connect() as con:
        return _history_total_rows_for_snapshots(con, since_iso=since_iso)


def history_daily_display_totals(since_iso: str | None = None) -> list[dict]:
    since_period = _history_period_start(since_iso) if since_iso else None
    persisted = history_daily_totals(since_period=since_period)
    persisted_by_key = {(row["period_start"], row["marketplace"]): row for row in persisted}
    with connect() as con:
        raw_rows = _history_total_rows_for_snapshots(con, since_iso=since_iso)
    raw_grouped: dict[tuple[str, str], list[dict]] = {}
    for row in raw_rows:
        raw_grouped.setdefault((_history_period_start(row["timestamp"]), str(row["marketplace"])), []).append(row)
    combined = dict(persisted_by_key)
    for key, samples in raw_grouped.items():
        if key in combined:
            continue
        values = [float(sample["total_cost"]) for sample in samples]
        combined[key] = {
            "period_start": key[0],
            "marketplace": key[1],
            "average_total_cost": sum(values) / len(values),
            "min_total_cost": min(values),
            "max_total_cost": max(values),
            "sample_count": len(values),
            "first_sample_at": samples[0]["timestamp"],
            "last_sample_at": samples[-1]["timestamp"],
            "aggregated_at": None,
        }
    return [combined[key] for key in sorted(combined)]

def history_daily_totals(
    *,
    since_period: str | None = None,
    until_period: str | None = None,
) -> list[dict]:
    where = []
    params: list[str] = []
    if since_period:
        where.append("period_start >= ?")
        params.append(since_period)
    if until_period:
        where.append("period_start <= ?")
        params.append(until_period)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with connect() as con:
        return [dict(row) for row in con.execute(
            f"SELECT * FROM history_daily_totals {clause} ORDER BY period_start, marketplace",
            params,
        ).fetchall()]


def compact_history(
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    connection: DbConnection | None = None,
) -> dict[str, int | str | bool]:
    cutoff = _history_cutoff_iso(now)
    operation_id = uuid4().hex
    manager = connect() if connection is None else nullcontext(connection)
    with manager as con:
        old_snapshot_ids = _eligible_compaction_snapshot_ids(con, cutoff)
        rows = _history_total_rows_for_snapshot_ids(con, old_snapshot_ids)
        grouped: dict[tuple[str, str], list[dict]] = {}
        for row in rows:
            grouped.setdefault((_history_period_start(row["timestamp"]), str(row["marketplace"])), []).append(row)
        old_points = 0
        if old_snapshot_ids:
            placeholders = ",".join("?" for _ in old_snapshot_ids)
            old_points = int(con.execute(
                f"SELECT COUNT(*) AS n FROM price_points WHERE snapshot_id IN ({placeholders})",
                old_snapshot_ids,
            ).fetchone()["n"])
        if dry_run:
            return {
                "dry_run": True, "cutoff_timestamp": cutoff,
                "snapshots": len(old_snapshot_ids), "price_points": old_points,
                "daily_totals": len(grouped),
            }
        aggregated_at = utc_now_iso()
        for (period_start, marketplace), samples in grouped.items():
            values = [float(sample["total_cost"]) for sample in samples]
            con.execute(
                """
                INSERT INTO history_daily_totals (
                    period_start, marketplace, average_total_cost, min_total_cost,
                    max_total_cost, sample_count, first_sample_at, last_sample_at, aggregated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(period_start, marketplace) DO UPDATE SET
                    average_total_cost=excluded.average_total_cost,
                    min_total_cost=excluded.min_total_cost,
                    max_total_cost=excluded.max_total_cost,
                    sample_count=excluded.sample_count,
                    first_sample_at=excluded.first_sample_at,
                    last_sample_at=excluded.last_sample_at,
                    aggregated_at=excluded.aggregated_at
                """,
                (period_start, marketplace, sum(values) / len(values), min(values), max(values), len(values),
                 samples[0]["timestamp"], samples[-1]["timestamp"], aggregated_at),
            )
        if old_snapshot_ids:
            placeholders = ",".join("?" for _ in old_snapshot_ids)
            con.execute(f"DELETE FROM price_points WHERE snapshot_id IN ({placeholders})", old_snapshot_ids)
            con.execute(f"DELETE FROM snapshots WHERE snapshot_id IN ({placeholders})", old_snapshot_ids)
        if not dry_run:
            con.execute(
                """
                INSERT INTO history_retention_state(state_key, cutoff_timestamp, completed_at, operation_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(state_key) DO UPDATE SET cutoff_timestamp=excluded.cutoff_timestamp,
                    completed_at=excluded.completed_at, operation_id=excluded.operation_id
                """,
                ("daily_compaction", cutoff, aggregated_at, operation_id),
            )
    return {
        "dry_run": False, "cutoff_timestamp": cutoff,
        "snapshots": len(old_snapshot_ids), "price_points": old_points,
        "daily_totals": len(grouped),
    }



    params: list[str] = []
    where = "WHERE bi.active = 1"
    if since_iso:
        where += " AND s.timestamp >= ?"
        params.append(since_iso)
    multiplier_expr = (
        "LEAST(GREATEST(COALESCE(bi.multiplier, 1), 1), 1000)"
        if using_postgres()
        else "MIN(MAX(COALESCE(bi.multiplier, 1), 1), 1000)"
    )
    with connect() as con:
        return list(
            con.execute(
                f"""
                WITH snapshot_marketplaces AS (
                    SELECT DISTINCT snapshot_id, marketplace
                    FROM price_points
                ),
                totals AS (
                    SELECT
                        s.snapshot_id,
                        s.timestamp,
                        sm.marketplace,
                        SUM(
                            COALESCE(
                                pp.normalized_price,
                                CASE
                                    WHEN sm.marketplace IN ('Buff163', 'YouPin')
                                    THEN COALESCE(c5game.normalized_price, baseline.normalized_price)
                                    WHEN sm.marketplace != ? THEN baseline.normalized_price
                                    ELSE NULL
                                END
                            ) * {multiplier_expr}
                        ) AS total_cost,
                        COUNT(pp.normalized_price) AS available_count,
                        SUM(
                            CASE
                                WHEN sm.marketplace != ?
                                    AND pp.normalized_price IS NULL
                                    AND (
                                        baseline.normalized_price IS NOT NULL
                                        OR (
                                            sm.marketplace IN ('Buff163', 'YouPin')
                                            AND c5game.normalized_price IS NOT NULL
                                        )
                                    )
                                THEN 1
                                ELSE 0
                            END
                        ) AS fallback_count
                    FROM snapshots s
                    JOIN snapshot_marketplaces sm ON sm.snapshot_id = s.snapshot_id
                    JOIN basket_items bi ON bi.active = 1
                    LEFT JOIN price_points pp
                        ON pp.snapshot_id = s.snapshot_id
                        AND pp.marketplace = sm.marketplace
                        AND pp.item_id = bi.item_id
                        AND pp.fetch_status = 'ok'
                        AND pp.normalized_price IS NOT NULL
                    LEFT JOIN price_points baseline
                        ON baseline.snapshot_id = s.snapshot_id
                        AND baseline.marketplace = ?
                        AND baseline.item_id = bi.item_id
                        AND baseline.fetch_status = 'ok'
                        AND baseline.normalized_price IS NOT NULL
                    LEFT JOIN price_points c5game
                        ON c5game.snapshot_id = s.snapshot_id
                        AND c5game.marketplace = 'C5Game'
                        AND c5game.item_id = bi.item_id
                        AND c5game.fetch_status = 'ok'
                        AND c5game.normalized_price IS NOT NULL
                    {where}
                    GROUP BY s.snapshot_id, s.timestamp, sm.marketplace
                )
                SELECT *
                FROM totals
                WHERE total_cost IS NOT NULL
                ORDER BY timestamp, marketplace
                """,
                [BASELINE_MARKETPLACE, BASELINE_MARKETPLACE, BASELINE_MARKETPLACE, *params],
            ).fetchall()
        )
