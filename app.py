from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from html import escape
import json
import os
import re
import time

import pandas as pd
import plotly.express as px
import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv

import db
from adapters import PriceResult, build_adapter_registry
from adapters.backup_sources import apply_backup_prices, clear_backup_cache
from adapters.csgoskins import CSGOSKINS_MARKETS, clear_csgoskins_cache
from adapters.direct_market_pages import fetch_direct_market_page_price
from basket import load_basket_rows
from calculations import (
    BASELINE_MARKETPLACE,
    build_comparison_table,
    split_comparison_fixed_rows,
)


load_dotenv(db.APP_DIR / ".env")


def load_streamlit_secrets_into_env() -> None:
    try:
        secrets = st.secrets
        keys = list(secrets.keys())
    except Exception:
        return
    for key in keys:
        if key in os.environ:
            continue
        value = secrets.get(key)
        if isinstance(value, (str, int, float, bool)):
            os.environ[key] = str(value)


load_streamlit_secrets_into_env()
TABLE_BACKGROUND = "#f9fafb80"
UTC_PLUS_8 = timezone(timedelta(hours=8))
LOGO_DIR = db.APP_DIR / "assets" / "logos"
DEFAULT_COMPARISON_CACHE_KEY = "current_comparison_default_html"
MARKETPLACE_LOGO_FILES = {
    "HaloSkins": "haloskins.png",
    "CSFloat": "csfloat.png",
    "CS.MONEY": "cs_money.png",
    "Market.CSGO": "market_csgo.png",
    "DMarket": "dmarket.png",
    "LIS-SKINS": "lis_skins.png",
    "Tradeit.gg": "tradeit_gg.png",
    "SkinSwap": "skinswap.png",
    "Skin.Land": "skin_land.png",
    "Avan.market": "avan_market.png",
    "Aim.market": "aim_market.png",
    "SkinBaron": "skinbaron.png",
    "SkinPlace": "skinplace.png",
    "ShadowPay": "shadowpay.png",
    "WAXPEER": "waxpeer.png",
    "Waxpeer": "waxpeer.png",
    "Skins.com": "skins_com.png",
    "Skinvault": "skinvault.png",
    "UUSKINS": "uuskins.png",
    "Exeskins": "exeskins.png",
    "C5Game": "c5game.png",
    "Skinport": "skinport.webp",
    "Buff163": "buff163.webp",
    "YouPin": "youpin_clean.png",
    "Steam": "steam_clean.png",
}
API_REPAIR_MARKETPLACES = {
    BASELINE_MARKETPLACE,
    "C5Game",
    "CSFloat",
    "DMarket",
    "HaloSkins",
    "Market.CSGO",
    "Skinport",
    "Buff163",
    "YouPin",
    "Steam",
    "Waxpeer",
}
API_REPAIR_WITH_COMPARE_BACKUP = {"Buff163", "YouPin", "Steam"}
STEAM_MARKETPLACE = "Steam"
WRONG_PRICE_DIFF_THRESHOLD = 35.0


@st.cache_data(ttl=45, show_spinner=False)
def cached_latest_snapshot() -> dict | None:
    row = db.latest_snapshot()
    return dict(row) if row else None


@st.cache_resource(show_spinner=False)
def initialize_database() -> bool:
    db.init_db()
    return True


@st.cache_data(ttl=45, show_spinner=False)
def cached_latest_price_points(snapshot_id: int | None) -> list[dict]:
    if snapshot_id is None:
        return []
    return [dict(row) for row in db.price_points_for_snapshot(snapshot_id)]


@st.cache_data(ttl=45, show_spinner=False)
def cached_basket_items(active_only: bool = False) -> list[dict]:
    return [dict(row) for row in db.get_basket_items(active_only=active_only)]


@st.cache_data(ttl=45, show_spinner=False)
def cached_marketplaces() -> list[dict]:
    return [dict(row) for row in db.get_marketplaces()]


@st.cache_data(ttl=45, show_spinner=False)
def cached_history_totals(since_iso: str | None = None) -> list[dict]:
    return [dict(row) for row in db.history_totals(since_iso=since_iso)]


@st.cache_data(ttl=45, show_spinner=False)
def cached_update_runs() -> list[dict]:
    if not hasattr(db, "update_runs"):
        return []
    return [dict(row) for row in db.update_runs()]


@st.cache_data(ttl=45, show_spinner=False)
def cached_comparison_data(snapshot_id: int) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    items = cached_basket_items(active_only=False)
    points = cached_latest_price_points(snapshot_id)
    marketplace_order = enabled_marketplace_names()
    comparison, coverage = build_comparison_table(items, points, marketplace_order)
    return comparison, coverage, marketplace_order


def load_comparison_data_with_timing(snapshot_id: int) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    started = time.perf_counter()
    items = cached_basket_items(active_only=False)
    record_perf_metric("Current: load basket items", started)

    started = time.perf_counter()
    points = cached_latest_price_points(snapshot_id)
    record_perf_metric("Current: load price points", started)

    started = time.perf_counter()
    marketplace_order = enabled_marketplace_names()
    record_perf_metric("Current: load marketplace order", started)

    started = time.perf_counter()
    comparison, coverage = build_comparison_table(items, points, marketplace_order)
    record_perf_metric("Current: build comparison dataframe", started)
    return comparison, coverage, marketplace_order


@st.cache_data(ttl=45, show_spinner=False)
def cached_comparison_table_html(
    df: pd.DataFrame,
    column_widths: dict[str, int],
    *,
    fullscreen: bool,
    include_logos: bool,
) -> str:
    return render_comparison_table_html(
        df,
        column_widths,
        fullscreen=fullscreen,
        include_logos=include_logos,
    )


def clear_data_cache() -> None:
    st.cache_data.clear()


def record_perf_metric(label: str, started_at: float) -> None:
    metrics = st.session_state.setdefault("performance_metrics", [])
    metrics.append(
        {
            "step": label,
            "duration_ms": round((time.perf_counter() - started_at) * 1000, 1),
            "at": datetime.now(UTC_PLUS_8).strftime("%H:%M:%S"),
        }
    )
    del metrics[:-12]


def require_app_password() -> bool:
    password = os.getenv("APP_PASSWORD", "").strip()
    if not password:
        return True
    if st.session_state.get("app_authenticated"):
        return True

    st.title("CS2 Basket Price Comparison")
    st.caption("Enter the app password to continue.")
    entered = st.text_input("Password", type="password")
    if st.button("Unlock", type="primary"):
        if entered == password:
            st.session_state.app_authenticated = True
            st.rerun()
        st.error("Incorrect password.")
    return False


def main() -> None:
    st.set_page_config(page_title="CS2 Basket Price Comparison", layout="wide")
    if not require_app_password():
        return
    st.markdown(
        """
        <style>
        .block-container { padding-top: 1.45rem; }
        h1 {
            margin-bottom: 0.25rem !important;
        }
        div[data-testid="stTabs"] {
            margin-top: -0.2rem;
        }
        div[data-testid="stTabs"] [data-baseweb="tab-list"] {
            margin-top: 0;
        }
        div[data-testid="stDataFrame"] { font-size: 0.86rem; }
        div[data-baseweb="select"] [data-baseweb="tag"] {
            align-items: center !important;
            background: #374151 !important;
            border: 1px solid #4b5563 !important;
            border-radius: 7px !important;
            color: #f9fafb !important;
            display: inline-flex !important;
            font-weight: 700 !important;
            gap: 0.35rem !important;
        }
        div[data-baseweb="select"] [data-baseweb="tag"] span {
            color: #f9fafb !important;
        }
        div[data-baseweb="select"] [data-baseweb="tag"] svg {
            color: #f9fafb !important;
            fill: #f9fafb !important;
        }
        .cs2dt-tag-logo {
            border-radius: 4px;
            height: 16px;
            object-fit: contain;
            width: 16px;
        }
        .dashboard-meta {
            color: #64748b;
            font-size: 0.9rem;
            margin: -0.15rem 0 0.45rem 0;
        }
        .dashboard-meta .meta-status {
            color: #64748b;
            margin-left: 0.75rem;
        }
        .header-update-status {
            color: #64748b;
            font-size: 0.9rem;
            margin: -0.15rem 0 0.45rem 0;
            min-height: 1.2rem;
            text-align: left;
            white-space: nowrap;
        }
        .header-button-spacer {
            height: 2.0rem;
        }
        .kpi-grid {
            display: grid;
            gap: 0.65rem;
            grid-template-columns: repeat(6, minmax(0, 1fr));
            margin: 0.55rem 0 0.75rem 0;
        }
        .kpi-card {
            background: rgba(209, 213, 219, 0.82);
            border: 1px solid #cbd5e1;
            border-radius: 8px;
            min-height: 96px;
            padding: 0.75rem 0.8rem;
        }
        .kpi-title {
            color: #334155;
            font-size: 0.76rem;
            font-weight: 700;
            margin-bottom: 0.45rem;
        }
        .kpi-market-line {
            align-items: center;
            color: #334155;
            display: flex;
            font-size: 0.8rem;
            gap: 0.45rem;
            min-height: 24px;
        }
        .kpi-value-row {
            align-items: baseline;
            display: flex;
            gap: 0.65rem;
            justify-content: space-between;
            margin-top: 0.42rem;
        }
        .kpi-value {
            color: #0f172a;
            font-size: 1.08rem;
            font-weight: 800;
            line-height: 1.15;
            white-space: nowrap;
        }
        .kpi-sub {
            align-items: center;
            color: #475569;
            display: flex;
            font-size: 0.78rem;
            gap: 0.45rem;
            margin-top: 0.45rem;
        }
        .kpi-inline-sub {
            color: #64748b;
            flex: 0 0 auto;
            font-size: 0.74rem;
            font-weight: 700;
            white-space: nowrap;
        }
        .kpi-logo {
            height: 24px;
            object-fit: contain;
            width: 24px;
        }
        .kpi-diff.negative,
        .diff-negative {
            color: #059669 !important;
        }
        .kpi-diff.positive,
        .diff-positive {
            color: #dc2626 !important;
        }
        .table-legend {
            align-items: center;
            background: rgba(249, 250, 251, 0.8);
            border: 1px solid #e5e7eb;
            border-radius: 7px;
            color: #475569;
            display: flex;
            flex-wrap: wrap;
            gap: 1.4rem;
            margin-top: 0.55rem;
            padding: 0.65rem 0.85rem;
        }
        .comparison-title {
            color: #111827;
            font-size: 1.5rem;
            font-weight: 700;
            margin: 0.5rem 0 0.35rem 0;
        }
        div[data-testid="stDownloadButton"] button,
        div[data-testid="stButton"] button {
            min-height: 2.35rem;
        }
        div[data-baseweb="popover"],
        div[data-baseweb="popover"] ul,
        div[role="listbox"] {
            z-index: 1000003 !important;
        }
        .fullscreen-toolbar-spacer {
            display: none;
        }
        .fullscreen-toolbar-spacer.active {
            display: block;
            height: 52px;
        }
        .legend-dot {
            border-radius: 999px;
            display: inline-block;
            height: 10px;
            margin-right: 0.35rem;
            width: 10px;
        }
        .legend-green { background: #059669; }
        .legend-red { background: #dc2626; }
        .legend-gray { background: #9ca3af; }
        @media (max-width: 1100px) {
            .kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        }
        .comparison-table-scroll {
            border: 1px solid #e5e7eb;
            border-radius: 6px;
            max-height: 820px;
            overflow: auto;
            width: 100%;
        }
        .comparison-table-scroll.fullscreen-table {
            background: #ffffff;
            border-radius: 8px;
            box-shadow: 0 18px 60px rgba(15, 23, 42, 0.24);
            bottom: 18px;
            left: 18px;
            max-height: calc(100vh - 88px);
            position: fixed;
            right: 18px;
            top: 70px;
            width: auto;
            z-index: 999999;
        }
        .comparison-html-table {
            border-collapse: collapse;
            table-layout: fixed;
            font-size: 0.86rem;
            color: #111827;
        }
        .comparison-html-table th,
        .comparison-html-table td {
            border-right: 1px solid #e5e7eb;
            border-bottom: 1px solid #e5e7eb;
            height: 34px;
            padding: 0 8px;
            overflow: hidden;
            text-align: center;
            text-overflow: ellipsis;
            vertical-align: middle;
            white-space: nowrap;
        }
        .comparison-html-table th {
            background: rgba(209, 213, 219, 0.82);
            color: #6b7280;
            font-weight: 500;
            position: sticky;
            top: 0;
            z-index: 4;
        }
        .market-header {
            align-items: center;
            display: inline-flex;
            gap: 0.35rem;
            justify-content: center;
            max-width: 100%;
        }
        .market-logo {
            border-radius: 4px;
            flex: 0 0 auto;
            height: 18px;
            object-fit: contain;
            width: 18px;
        }
        .market-logo-fallback {
            align-items: center;
            background: #e5e7eb;
            border-radius: 4px;
            color: #374151;
            display: inline-flex;
            flex: 0 0 auto;
            font-size: 0.62rem;
            font-weight: 700;
            height: 18px;
            justify-content: center;
            width: 18px;
        }
        .header-label {
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .comparison-html-table td.neutral-cell {
            background: rgba(249, 250, 251, 0.50);
        }
        .comparison-html-table td.cheaper-market {
            background: rgba(249, 250, 251, 0.50);
        }
        .comparison-html-table td.expensive-market {
            background: rgba(249, 250, 251, 0.50);
        }
        .comparison-html-table td.halo-total {
            background: rgba(255, 243, 191, 0.70);
        }
        .comparison-html-table td.fallback-cell {
            color: #9ca3af;
        }
        .comparison-html-table tr.item-row td {
            font-weight: 600;
        }
        .comparison-html-table tr.sum-row td {
            border-top: 2px solid #999;
            bottom: 34px;
            font-weight: 700;
            position: sticky;
            z-index: 5;
        }
        .comparison-html-table tr.repeat-header-row td {
            bottom: 0;
            font-weight: 700;
            line-height: 1.2;
            position: sticky;
            white-space: normal;
            z-index: 5;
        }
        .comparison-html-table tr.sum-row td.neutral-cell,
        .comparison-html-table tr.repeat-header-row td.neutral-cell {
            background: rgba(209, 213, 219, 0.82);
        }
        .comparison-html-table tr.sum-row td.cheaper-market,
        .comparison-html-table tr.repeat-header-row td.cheaper-market {
            background: rgba(209, 213, 219, 0.82);
        }
        .comparison-html-table tr.sum-row td.expensive-market,
        .comparison-html-table tr.repeat-header-row td.expensive-market {
            background: rgba(209, 213, 219, 0.82);
        }
        .comparison-html-table tr.sum-row td.halo-total,
        .comparison-html-table tr.repeat-header-row td.halo-total {
            background: #fff3bf;
        }
        .tool-instructions {
            margin-top: 1.25rem;
            padding: 0.75rem 1rem 0.25rem 1rem;
        }
        .tool-instructions h3 {
            font-size: 1rem;
            margin: 0.75rem 0 0.75rem 0;
        }
        .tool-instructions p,
        .tool-instructions li {
            line-height: 1.55;
        }
        .comparison-footer-scroll {
            border: 1px solid #e5e7eb;
            border-radius: 0 0 6px 6px;
            overflow-x: auto;
            overflow-y: hidden;
            width: 100%;
            margin-top: -0.15rem;
        }
        .comparison-footer-table {
            border-collapse: collapse;
            table-layout: fixed;
            font-size: 0.86rem;
            color: #111827;
        }
        .comparison-footer-table td {
            border-right: 1px solid #e5e7eb;
            border-bottom: 1px solid #e5e7eb;
            height: 34px;
            padding: 0 8px;
            overflow: hidden;
            text-overflow: ellipsis;
            vertical-align: middle;
            text-align: center;
            background: rgba(249, 250, 251, 0.50);
        }
        .comparison-footer-table td.cheaper-market {
            background: rgba(220, 252, 231, 0.50) !important;
        }
        .comparison-footer-table td.expensive-market {
            background: rgba(254, 226, 226, 0.50) !important;
        }
        .comparison-footer-table tr.sum-row td {
            font-weight: 700;
            border-top: 2px solid #999;
            white-space: nowrap;
            background: rgba(209, 213, 219, 0.82) !important;
        }
        .comparison-footer-table tr.repeat-header-row td {
            font-weight: 700;
            background: rgba(209, 213, 219, 0.82) !important;
            white-space: normal;
            line-height: 1.2;
        }
        .comparison-footer-table td.halo-total {
            background: rgba(255, 243, 191, 0.70) !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    initialize_database()
    if not st.session_state.get("basket_file_synced"):
        sync_basket_file()
        st.session_state.basket_file_synced = True
        clear_data_cache()
    prepare_startup_neon_sync()

    title_cols = st.columns([7.2, 0.72, 0.82], vertical_alignment="top")
    with title_cols[0]:
        st.title("CS2 Basket Price Comparison")
    with title_cols[1]:
        st.markdown('<div class="header-button-spacer"></div>', unsafe_allow_html=True)
        sync_clicked = st.button(
            "Sync Neon",
            use_container_width=True,
            disabled=not local_neon_sync_available(),
            help=(
                "Two-way sync between local SQLite and Neon. "
                "Available only in local SQLite mode with DATABASE_URL configured."
            ),
        )
    with title_cols[2]:
        st.markdown('<div class="header-button-spacer"></div>', unsafe_allow_html=True)
        update_clicked = st.button("Update prices", type="primary", use_container_width=True)

    meta_cols = st.columns([7.2, 0.72, 0.82], vertical_alignment="top")
    with meta_cols[0]:
        render_last_updated_meta()
    with meta_cols[1]:
        header_status = None
        if sync_clicked:
            header_status = "Synchronizing local SQLite and Neon..."
        elif update_clicked:
            header_status = "Fetching enabled marketplace adapters..."
        elif neon_sync_due():
            header_status = "Synchronizing local SQLite and Neon..."
        render_update_status(header_status)
    if sync_clicked:
        perform_neon_sync("manual")
        st.rerun()
    if update_clicked:
        started_at = db.utc_now_iso()
        started_timer = time.perf_counter()
        try:
            snapshot_id, timestamp, success_rate = collect_snapshot()
        except SnapshotQualityError as exc:
            db.record_update_run(
                source="manual",
                started_at=started_at,
                finished_at=db.utc_now_iso(),
                duration_seconds=time.perf_counter() - started_timer,
                status="error",
                error_details=str(exc),
            )
            st.session_state.update_error = str(exc)
        except Exception as exc:
            db.record_update_run(
                source="manual",
                started_at=started_at,
                finished_at=db.utc_now_iso(),
                duration_seconds=time.perf_counter() - started_timer,
                status="error",
                error_details=str(exc),
            )
            raise
        else:
            db.record_update_run(
                source="manual",
                started_at=started_at,
                finished_at=db.utc_now_iso(),
                duration_seconds=time.perf_counter() - started_timer,
                status="ok",
                snapshot_id=snapshot_id,
                success_rate=success_rate,
            )
            st.session_state.update_notice = (
                f"Saved snapshot #{snapshot_id} at {format_timestamp_utc8(timestamp)} "
                f"({success_rate:.0%} data received)."
            )
            schedule_delayed_neon_sync()
            clear_data_cache()
        st.rerun()
    if "update_notice" in st.session_state:
        st.toast(st.session_state.pop("update_notice"))
    if "update_error" in st.session_state:
        st.error(st.session_state.pop("update_error"))
    if "sync_notice" in st.session_state:
        st.success(st.session_state.pop("sync_notice"))
    if "sync_error" in st.session_state:
        st.error(st.session_state.pop("sync_error"))
    render_pending_neon_sync_timer()

    page = st.segmented_control(
        "Page",
        [
            "Current Basket Comparison",
            "Historical Graph",
            "Basket Items",
            "Settings / Marketplaces",
        ],
        default="Current Basket Comparison",
        key="active_page",
        label_visibility="collapsed",
    )

    if page == "Current Basket Comparison":
        render_current_comparison()
        prewarm_inactive_page_caches()
    elif page == "Historical Graph":
        render_history()
    elif page == "Basket Items":
        render_basket_items()
    elif page == "Settings / Marketplaces":
        render_marketplace_settings()

    run_due_neon_sync()


def sync_basket_file() -> None:
    if db.BASKET_PATH.exists():
        rows = load_basket_rows(db.BASKET_PATH)
        db.insert_basket_items(rows)


def local_neon_sync_available() -> bool:
    return bool(db.postgres_database_url()) and not db.using_postgres()


def prepare_startup_neon_sync() -> None:
    if not local_neon_sync_available():
        return

    if not st.session_state.get("startup_neon_sync_done"):
        st.session_state.startup_neon_sync_done = True
        st.session_state.pending_startup_neon_sync = True


def run_due_neon_sync() -> None:
    if not local_neon_sync_available():
        return
    if st.session_state.pop("pending_startup_neon_sync", False):
        perform_neon_sync("startup")
        st.rerun()

    due_at = st.session_state.get("pending_neon_sync_at")
    if due_at and time.time() >= float(due_at):
        st.session_state.pop("pending_neon_sync_at", None)
        trigger = st.session_state.pop("pending_neon_sync_trigger", "startup")
        perform_neon_sync(trigger)
        st.rerun()


def neon_sync_due() -> bool:
    due_at = st.session_state.get("pending_neon_sync_at")
    return bool(due_at and local_neon_sync_available() and time.time() >= float(due_at))


def perform_neon_sync(trigger: str) -> None:
    if db.using_postgres():
        st.session_state.sync_notice = "Online app already uses Neon directly. No local SQLite sync is needed."
        return
    if not db.postgres_database_url():
        st.session_state.sync_error = "DATABASE_URL is not configured, so Neon sync cannot run."
        return

    try:
        counts = db.sync_sqlite_to_postgres()
    except Exception as exc:
        st.session_state.sync_error = str(exc)
        return

    st.session_state.sync_notice = (
        f"{sync_trigger_label(trigger)} sync completed: "
        f"pushed {counts['snapshots']} snapshots / {counts['price_points']} price points, "
        f"pulled {counts['pulled_snapshots']} snapshots / {counts['pulled_price_points']} price points."
    )
    clear_data_cache()


def sync_trigger_label(trigger: str) -> str:
    if trigger == "startup":
        return "Startup Neon"
    if trigger == "delayed":
        return "Post-update Neon"
    return "Neon"


def schedule_delayed_neon_sync() -> None:
    if local_neon_sync_available():
        st.session_state.pending_neon_sync_at = time.time() + 15 * 60
        st.session_state.pending_neon_sync_trigger = "delayed"


def render_pending_neon_sync_timer() -> None:
    due_at = st.session_state.get("pending_neon_sync_at")
    if not due_at or not local_neon_sync_available():
        return
    delay_ms = max(1000, int((float(due_at) - time.time()) * 1000))
    components.html(
        f"""
        <script>
        window.setTimeout(() => {{
            window.parent.location.reload();
        }}, {delay_ms});
        </script>
        """,
        height=0,
    )


def render_last_updated_meta(status: str | None = None) -> None:
    snapshot = cached_latest_snapshot()
    status_html = f'<span class="meta-status">{escape(status)}</span>' if status else ""
    if snapshot is None:
        st.markdown(
            f'<div class="dashboard-meta">Last updated: no saved snapshot yet.{status_html}</div>',
            unsafe_allow_html=True,
        )
        return
    st.markdown(
        f'<div class="dashboard-meta">Last updated: {escape(format_timestamp_utc8(snapshot["timestamp"]))}{status_html}</div>',
        unsafe_allow_html=True,
    )


def render_update_status(status: str | None = None) -> None:
    status_html = escape(status) if status else "&nbsp;"
    st.markdown(
        f'<div class="header-update-status">{status_html}</div>',
        unsafe_allow_html=True,
    )


def render_current_comparison() -> None:
    total_started = time.perf_counter()
    snapshot = cached_latest_snapshot()
    if snapshot is None:
        st.info("Click Update prices to create the first local snapshot.")
        return

    cached_shell = st.empty()
    display_cache = db.get_display_cache(DEFAULT_COMPARISON_CACHE_KEY)
    if display_cache and int(display_cache["snapshot_id"] or 0) == int(snapshot["snapshot_id"]):
        cached_shell.markdown(display_cache["payload"], unsafe_allow_html=True)

    loading_placeholder = st.empty()
    loading_placeholder.caption("Loading latest comparison data...")
    data_started = time.perf_counter()
    comparison, _, marketplace_order = load_comparison_data_with_timing(int(snapshot["snapshot_id"]))
    record_perf_metric("Current: load comparison data", data_started)
    loading_placeholder.empty()
    cached_shell.empty()
    market_options = [name for name in marketplace_order if name != BASELINE_MARKETPLACE]
    selected_markets = st.multiselect(
        "Select Marketplaces",
        market_options,
        default=market_options,
        help="This only changes the table display. Update prices still fetches all enabled marketplaces.",
    )
    render_marketplace_tag_logo_decorator(market_options)

    if "show_difference_only" not in st.session_state:
        st.session_state.show_difference_only = False
    if "fullscreen_table" not in st.session_state:
        st.session_state.fullscreen_table = False
    if "comparison_view_mode" not in st.session_state:
        st.session_state.comparison_view_mode = "All markets"

    render_kpi_cards(comparison, selected_markets)

    view_label_to_mode = {
        "All markets": "all",
        "Cheaper than HaloSkins": "cheaper",
        "More expensive than HaloSkins": "expensive",
    }
    st.markdown('<div class="comparison-title">Comparison Table</div>', unsafe_allow_html=True)
    with st.container(key="comparison_controls"):
        control_cols = st.columns([1.8, 1.2, 5.2, 0.32, 0.32], vertical_alignment="center")
        with control_cols[0]:
            selected_view_label = st.selectbox(
                "Market view",
                list(view_label_to_mode),
                key="comparison_view_mode",
                label_visibility="collapsed",
            )
        with control_cols[1]:
            if st.button(
                "Show full prices" if st.session_state.show_difference_only else "Show Difference Only",
                key="toggle_difference_only",
                use_container_width=True,
            ):
                st.session_state.show_difference_only = not st.session_state.show_difference_only
        mode = view_label_to_mode[selected_view_label]
        display_df = filtered_comparison(
            comparison,
            selected_markets,
            mode=mode,
            difference_only=st.session_state.show_difference_only,
        )
        with control_cols[3]:
            st.download_button(
                "CSV",
                data=export_comparison_csv(display_df),
                file_name=f"cs2_basket_comparison_{mode}.csv",
                mime="text/csv",
                key=f"export_comparison_{mode}",
                help="Export visible table as CSV",
                use_container_width=True,
            )
        with control_cols[4]:
            if st.button(
                "Exit" if st.session_state.fullscreen_table else "Full",
                key="toggle_fullscreen_table",
                help="Exit fullscreen table view" if st.session_state.fullscreen_table else "Open fullscreen table view",
                use_container_width=True,
            ):
                st.session_state.fullscreen_table = not st.session_state.fullscreen_table

    if st.session_state.fullscreen_table:
        render_fullscreen_table_css()

    if not selected_markets:
        st.info("Select at least one marketplace to show comparison columns.")
    else:
        comparison_body, _ = split_comparison_fixed_rows(display_df)
        visible_columns = [col for col in comparison_body.columns if not is_marker_column(col)]
        if len(visible_columns) <= 4:
            st.info("No marketplaces match this view.")
        else:
            column_widths = comparison_column_widths(visible_columns)
            html_started = time.perf_counter()
            table_html = cached_comparison_table_html(
                display_df,
                column_widths,
                fullscreen=st.session_state.fullscreen_table,
                include_logos=True,
            )
            record_perf_metric("Current: render comparison HTML", html_started)
            st.markdown(
                table_html,
                unsafe_allow_html=True,
            )
            if (
                mode == "all"
                and not st.session_state.show_difference_only
                and not st.session_state.fullscreen_table
                and set(selected_markets) == set(market_options)
                and (
                    not display_cache
                    or int(display_cache["snapshot_id"] or 0) != int(snapshot["snapshot_id"])
                    or display_cache["payload"] != table_html
                )
            ):
                db.save_display_cache(DEFAULT_COMPARISON_CACHE_KEY, int(snapshot["snapshot_id"]), table_html)
            render_table_legend()

    render_manual_market_repair(marketplace_order)
    render_tool_instructions()
    record_perf_metric("Current: total render", total_started)


def render_fullscreen_table_css() -> None:
    st.markdown(
        """
        <style>
        .st-key-comparison_controls {
            background: rgba(255, 255, 255, 0.96);
            border: 1px solid #e5e7eb;
            border-radius: 8px;
            box-shadow: 0 8px 26px rgba(15, 23, 42, 0.12);
            left: 18px;
            padding: 8px 10px;
            position: fixed;
            right: 18px;
            top: 10px;
            z-index: 1000002;
        }
        .st-key-comparison_controls [data-baseweb="select"] {
            position: relative;
            z-index: 1000004;
        }
        .st-key-comparison_controls div[data-testid="stDownloadButton"],
        .st-key-comparison_controls div[data-testid="stButton"] {
            position: relative;
            z-index: 1000004;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def prewarm_inactive_page_caches() -> None:
    snapshot = cached_latest_snapshot()
    if snapshot is None:
        return
    snapshot_id = int(snapshot["snapshot_id"])
    if st.session_state.get("prewarmed_snapshot_id") == snapshot_id:
        return

    placeholder = st.empty()
    placeholder.caption("Preparing other pages...")
    cached_update_runs()
    cached_basket_items(active_only=False)
    cached_marketplaces()
    if not db.using_postgres():
        cached_history_totals(since_iso=since_for_range("week"))
        cached_latest_price_points(snapshot_id)
    st.session_state.prewarmed_snapshot_id = snapshot_id
    placeholder.empty()


def is_marker_column(column: str) -> bool:
    return column.startswith("__halo_fallback__") or column == "__header_repeat__"


def comparison_column_widths(columns: list[str]) -> dict[str, int]:
    widths: dict[str, int] = {}
    for column in columns:
        if column == "Name":
            widths[column] = 310
        elif column == "HaloSkins single":
            widths[column] = 112
        elif column == "Multiplier":
            widths[column] = 90
        elif column.endswith(" diff"):
            widths[column] = 118
        else:
            widths[column] = 112
    return widths


def render_manual_market_repair(marketplace_order: list[str]) -> None:
    repair_options = enabled_repair_marketplaces(marketplace_order)
    if not repair_options:
        return

    st.markdown('<div style="height: 1.35rem;"></div>', unsafe_allow_html=True)
    st.markdown("#### Repair Missing or Wrong Prices")
    st.caption(
        "Use this when a marketplace has missing items or suspicious non-Steam prices more than 35% away from HaloSkins. "
        "It updates that same snapshot timestamp when new prices are found. API markets use only their API; "
        "non-API markets try direct item pages first and use third-party backups only when the direct page is unavailable."
    )
    repair_cols = st.columns([3, 1], vertical_alignment="bottom")
    with repair_cols[0]:
        selected = st.multiselect(
            "Marketplaces to repair",
            repair_options,
            default=[],
            key="manual_repair_marketplaces",
        )
    render_marketplace_tag_logo_decorator(repair_options)
    with repair_cols[1]:
        repair_clicked = st.button(
            "Update selected prices",
            key="manual_repair_button",
            use_container_width=True,
            disabled=not selected,
        )

    if "manual_repair_result" in st.session_state:
        st.success(st.session_state.pop("manual_repair_result"))
    if "manual_repair_error" in st.session_state:
        st.error(st.session_state.pop("manual_repair_error"))

    if repair_clicked:
        with st.spinner("Checking missing or suspicious marketplace prices..."):
            try:
                result_lines = repair_missing_market_prices(selected)
            except Exception as exc:
                st.session_state.manual_repair_error = str(exc)
            else:
                st.session_state.manual_repair_result = "\n".join(result_lines)
                clear_data_cache()
        st.rerun()


def enabled_repair_marketplaces(marketplace_order: list[str]) -> list[str]:
    registry = build_adapter_registry()
    enabled_keys = set(db.get_enabled_adapter_keys())
    enabled_names = {
        registry[key].name
        for key in enabled_keys
        if key in registry and registry[key].credentials_configured()
    }
    return [name for name in marketplace_order if name in enabled_names]


def repair_missing_market_prices(marketplaces: list[str]) -> list[str]:
    snapshot = db.latest_snapshot()
    if snapshot is None:
        raise SnapshotQualityError("No saved snapshot is available to repair.")

    registry = build_adapter_registry()
    enabled_keys = set(db.get_enabled_adapter_keys())
    adapters_by_name = {
        adapter.name: adapter
        for key, adapter in registry.items()
        if key in enabled_keys and adapter.credentials_configured()
    }
    items = db.get_adapter_items()
    points = db.latest_price_points()
    points_by_market_item = {
        (point["marketplace"], point["market_hash_name"]): point
        for point in points
    }
    item_by_name = {item.market_hash_name: item for item in items}
    baseline_results = existing_baseline_results(points, item_by_name)
    baseline_prices = {
        result.market_hash_name: float(result.price)
        for result in baseline_results
        if result.fetch_status == "ok" and result.price is not None
    }
    clear_backup_cache()
    clear_csgoskins_cache()

    result_lines: list[str] = []
    for marketplace in marketplaces:
        adapter = adapters_by_name.get(marketplace)
        if adapter is None:
            result_lines.append(f"{marketplace}: updated 0, not updated 0")
            continue

        repair_items = [
            item
            for item in items
            if price_point_needs_repair(
                points_by_market_item.get((marketplace, item.market_hash_name)),
                baseline_prices.get(item.market_hash_name),
                marketplace,
            )
        ]
        overwrite_names = {
            item.market_hash_name
            for item in repair_items
            if price_point_wrong(
                points_by_market_item.get((marketplace, item.market_hash_name)),
                baseline_prices.get(item.market_hash_name),
                marketplace,
            )
        }
        if not repair_items:
            result_lines.append(f"{marketplace}: updated 0, not updated 0")
            continue

        if marketplace in API_REPAIR_MARKETPLACES:
            candidate_results = fetch_repair_with_api(adapter, repair_items)
            if marketplace in API_REPAIR_WITH_COMPARE_BACKUP:
                candidate_results = [
                    result
                    for result in apply_backup_prices(
                        baseline_results + candidate_results,
                        repair_items,
                    )
                    if result.marketplace == marketplace
                ]
        else:
            csgoskins_retry_names = {
                item.market_hash_name
                for item in repair_items
                if should_retry_csgoskins_first(
                    points_by_market_item.get((marketplace, item.market_hash_name))
                )
            }
            candidate_results = fetch_repair_with_api(
                adapter,
                [item for item in repair_items if item.market_hash_name in csgoskins_retry_names],
            )
            direct_repair_items = [
                item for item in repair_items if item.market_hash_name not in csgoskins_retry_names
            ]
            if direct_repair_items:
                candidate_results.extend(
                    fetch_repair_with_direct_pages(
                        marketplace,
                        adapter,
                        direct_repair_items,
                        baseline_results,
                        baseline_prices,
                    )
                )

        updated = db.update_latest_repair_price_points(
            marketplace,
            candidate_results,
            overwrite_market_hash_names=overwrite_names,
        )
        result_lines.append(f"{marketplace}: updated {updated}, not updated {len(repair_items) - updated}")

    return result_lines


def fetch_repair_with_api(adapter, missing_items) -> list[PriceResult]:
    try:
        return adapter.fetch_prices(missing_items)
    except Exception as exc:
        return [
            PriceResult(
                marketplace=adapter.name,
                market_hash_name=item.market_hash_name,
                price=None,
                currency="USD",
                fetch_status="error",
                error_details=str(exc),
            )
            for item in missing_items
        ]


def fetch_repair_with_direct_pages(
    marketplace: str,
    fallback_adapter,
    missing_items,
    baseline_results: list[PriceResult],
    baseline_prices: dict[str, float],
) -> list[PriceResult]:
    direct_results: list[PriceResult] = []
    page_unavailable_items = []
    for item in missing_items:
        direct = fetch_direct_market_page_price(
            marketplace,
            item,
            baseline_prices.get(item.market_hash_name),
        )
        direct_results.append(direct.result)
        if direct.page_unavailable:
            page_unavailable_items.append(item)

    if not page_unavailable_items:
        return direct_results

    fallback_results = fetch_repair_with_api(fallback_adapter, page_unavailable_items)
    unavailable_names = {item.market_hash_name for item in page_unavailable_items}
    backup_candidates = [
        result
        for result in apply_backup_prices(
            [
                result
                for result in baseline_results
                if result.market_hash_name in unavailable_names
            ]
            + fallback_results,
            page_unavailable_items,
        )
        if result.marketplace == marketplace
    ]
    backup_by_name = {result.market_hash_name: result for result in backup_candidates}
    return [
        backup_by_name.get(result.market_hash_name, result)
        if result.market_hash_name in unavailable_names
        else result
        for result in direct_results
    ]


def price_point_missing(point) -> bool:
    if point is None:
        return True
    return point["fetch_status"] != "ok" or point["normalized_price"] is None


def price_point_needs_repair(point, baseline_price: float | None, marketplace: str) -> bool:
    return price_point_missing(point) or price_point_wrong(point, baseline_price, marketplace)


def price_point_wrong(point, baseline_price: float | None, marketplace: str) -> bool:
    if marketplace in {BASELINE_MARKETPLACE, STEAM_MARKETPLACE}:
        return False
    if point is None or baseline_price is None or baseline_price <= 0:
        return False
    if point["fetch_status"] != "ok" or point["normalized_price"] is None:
        return False
    diff = abs(float(point["normalized_price"]) / baseline_price - 1.0) * 100.0
    return diff > WRONG_PRICE_DIFF_THRESHOLD


def should_retry_csgoskins_first(point) -> bool:
    if point is None:
        return False
    error = (point["error_details"] or "").lower()
    return "csgoskins" in error or "r.jina.ai" in error


def existing_baseline_results(points, item_by_name: dict[str, object]) -> list[PriceResult]:
    results: list[PriceResult] = []
    for point in points:
        if point["marketplace"] != BASELINE_MARKETPLACE:
            continue
        if point["market_hash_name"] not in item_by_name:
            continue
        results.append(
            PriceResult(
                marketplace=BASELINE_MARKETPLACE,
                market_hash_name=point["market_hash_name"],
                price=point["price"],
                currency=point["currency"],
                stock_count=point["stock_count"],
                fetch_status=point["fetch_status"],
                error_details=point["error_details"],
            )
        )
    return results


def render_marketplace_tag_logo_decorator(marketplaces: list[str]) -> None:
    logo_map = {
        marketplace: marketplace_logo_src(marketplace)
        for marketplace in marketplaces
        if marketplace_logo_src(marketplace)
    }
    components.html(
        f"""
        <script>
        (function () {{
          const win = window.parent;
          const doc = win.document;
          const newLogoMap = {json.dumps(logo_map)};
          win.__cs2dtTagLogoMap = Object.assign({{}}, win.__cs2dtTagLogoMap || {{}}, newLogoMap);
          function normalize(text) {{
            return (text || "").replace(/\\u00d7/g, "").replace(/\\bDelete\\b/g, "").trim();
          }}
          function tagLabel(tag) {{
            const clone = tag.cloneNode(true);
            clone.querySelectorAll(".cs2dt-tag-logo, svg, button").forEach((node) => node.remove());
            return normalize(clone.textContent);
          }}
          function decorateTags() {{
            const logoMap = win.__cs2dtTagLogoMap || {{}};
            const tags = doc.querySelectorAll('[data-baseweb="tag"]');
            tags.forEach((tag) => {{
              const label = tagLabel(tag);
              const src = logoMap[label];
              const existing = tag.querySelector("img.cs2dt-tag-logo");
              if (!src) {{
                if (existing) existing.remove();
                return;
              }}
              if (existing && existing.getAttribute("src") === src) return;
              if (existing) existing.remove();
              const img = doc.createElement("img");
              img.className = "cs2dt-tag-logo";
              img.src = src;
              img.alt = label + " logo";
              tag.insertBefore(img, tag.firstChild);
            }});
          }}
          if (win.__cs2dtTagLogoObserver) {{
            win.__cs2dtTagLogoObserver.disconnect();
          }}
          if (win.__cs2dtTagLogoInterval) {{
            win.clearInterval(win.__cs2dtTagLogoInterval);
          }}
          decorateTags();
          win.__cs2dtTagLogoObserver = new MutationObserver(decorateTags);
          win.__cs2dtTagLogoObserver.observe(doc.body, {{
            childList: true,
            subtree: true,
            characterData: true,
          }});
          win.__cs2dtTagLogoInterval = win.setInterval(decorateTags, 1000);
        }})();
        </script>
        """,
        height=0,
        width=0,
    )


def render_kpi_cards(comparison: pd.DataFrame, selected_markets: list[str]) -> None:
    kpis = build_kpis(comparison, selected_markets)
    cards = [
        baseline_kpi_card_html(format_currency_value(kpis["halo_total"])),
        marketplace_kpi_card_html("Cheapest marketplace", kpis["cheapest"]),
        marketplace_kpi_card_html("Most expensive marketplace", kpis["most_expensive"]),
        marketplace_kpi_card_html("Steam price", kpis["steam"], empty_subtitle="No Steam price"),
        kpi_card_html("Cheaper than HaloSkins", str(kpis["cheaper_count"]), "Markets"),
        kpi_card_html("More expensive than HaloSkins", str(kpis["expensive_count"]), "Markets"),
    ]
    st.markdown(f'<div class="kpi-grid">{"".join(cards)}</div>', unsafe_allow_html=True)


def build_kpis(comparison: pd.DataFrame, selected_markets: list[str]) -> dict:
    sum_rows = comparison[comparison["Name"].eq("Basket total")] if "Name" in comparison.columns else pd.DataFrame()
    if sum_rows.empty:
        return {
            "halo_total": None,
            "cheapest": None,
            "most_expensive": None,
            "steam": None,
            "cheaper_count": 0,
            "expensive_count": 0,
        }

    sum_row = sum_rows.iloc[0]
    market_rows = []
    for marketplace in selected_markets:
        total = sum_row.get(marketplace)
        diff = sum_row.get(f"{marketplace} diff")
        if pd.isna(total) or pd.isna(diff):
            continue
        market_rows.append({"marketplace": marketplace, "total": float(total), "diff": float(diff)})

    cheaper = [row for row in market_rows if row["diff"] < 0]
    expensive = [row for row in market_rows if row["diff"] >= 0]
    non_steam_rows = [row for row in market_rows if row["marketplace"] != STEAM_MARKETPLACE]
    steam_row = next((row for row in market_rows if row["marketplace"] == STEAM_MARKETPLACE), None)
    if steam_row is None and STEAM_MARKETPLACE in comparison.columns:
        steam_total = sum_row.get(STEAM_MARKETPLACE)
        steam_diff = sum_row.get(f"{STEAM_MARKETPLACE} diff")
        if pd.notna(steam_total) and pd.notna(steam_diff):
            steam_row = {
                "marketplace": STEAM_MARKETPLACE,
                "total": float(steam_total),
                "diff": float(steam_diff),
            }
    return {
        "halo_total": sum_row.get("HaloSkins total"),
        "cheapest": min(market_rows, key=lambda row: row["total"]) if market_rows else None,
        "most_expensive": max(non_steam_rows, key=lambda row: row["total"]) if non_steam_rows else None,
        "steam": steam_row,
        "cheaper_count": len(cheaper),
        "expensive_count": len(expensive),
    }


def baseline_kpi_card_html(value: str) -> str:
    logo = marketplace_logo_src(BASELINE_MARKETPLACE)
    logo_html = (
        f'<img class="kpi-logo" src="{escape(logo)}" alt="{escape(BASELINE_MARKETPLACE)} logo">'
        if logo
        else ""
    )
    return (
        '<div class="kpi-card">'
        '<div class="kpi-title">Basket total price</div>'
        f'<div class="kpi-market-line">{logo_html}<strong>{escape(BASELINE_MARKETPLACE)}</strong></div>'
        '<div class="kpi-value-row">'
        f'<div class="kpi-value">{escape(value)}</div>'
        '<div class="kpi-inline-sub">Baseline</div>'
        '</div>'
        '</div>'
    )


def kpi_card_html(title: str, value: str, subtitle: str) -> str:
    return (
        '<div class="kpi-card">'
        f'<div class="kpi-title">{escape(title)}</div>'
        f'<div class="kpi-value">{escape(value)}</div>'
        f'<div class="kpi-sub">{escape(subtitle)}</div>'
        '</div>'
    )


def marketplace_kpi_card_html(title: str, row: dict | None, *, empty_subtitle: str = "No selected markets") -> str:
    if row is None:
        return kpi_card_html(title, "N/A", empty_subtitle)
    marketplace = row["marketplace"]
    logo = marketplace_logo_src(marketplace)
    logo_html = f'<img class="kpi-logo" src="{escape(logo)}" alt="{escape(marketplace)} logo">' if logo else ""
    diff_class = "negative" if row["diff"] < 0 else "positive" if row["diff"] > 0 else ""
    return (
        '<div class="kpi-card">'
        f'<div class="kpi-title">{escape(title)}</div>'
        f'<div class="kpi-market-line">{logo_html}<strong>{escape(marketplace)}</strong></div>'
        '<div class="kpi-value-row">'
        f'<div class="kpi-value">{escape(format_currency_value(row["total"]))}</div>'
        f'<div class="kpi-inline-sub kpi-diff {diff_class}">{escape(format_percent_value(row["diff"]))}</div>'
        '</div>'
        '</div>'
    )


def render_table_legend() -> None:
    st.markdown(
        """
        <div class="table-legend">
          <span><span class="legend-dot legend-green"></span>Negative % = cheaper than HaloSkins</span>
          <span><span class="legend-dot legend-red"></span>Positive % = more expensive</span>
          <span><span class="legend-dot legend-gray"></span>Gray = missing, using fallback price</span>
        </div>
        """,
        unsafe_allow_html=True,
    )


def filtered_comparison(
    df: pd.DataFrame,
    selected_markets: list[str],
    *,
    mode: str,
    difference_only: bool,
) -> pd.DataFrame:
    selected = set(selected_markets)
    markets = comparison_markets_for_mode(df, selected, mode)
    visible_columns = []
    for column in [col for col in df.columns if not is_marker_column(col)]:
        market = marketplace_for_column(column)
        if column in {"Name", "HaloSkins single", "Multiplier", "HaloSkins total"}:
            visible_columns.append(column)
        elif market in markets:
            if not difference_only or column.endswith(" diff"):
                visible_columns.append(column)

    marker_columns = ["__header_repeat__"] if "__header_repeat__" in df.columns else []
    for column in visible_columns:
        marker = f"__halo_fallback__{column}"
        if marker in df.columns:
            marker_columns.append(marker)
    return df.reindex(columns=visible_columns + marker_columns).copy()


def comparison_markets_for_mode(df: pd.DataFrame, selected: set[str], mode: str) -> list[str]:
    sum_rows = df[df["Name"].eq("Basket total")] if "Name" in df.columns else pd.DataFrame()
    sum_row = sum_rows.iloc[0] if not sum_rows.empty else None
    markets: list[str] = []
    for column in df.columns:
        if not column.endswith(" diff") or is_marker_column(column):
            continue
        market = column[: -len(" diff")]
        if market not in selected:
            continue
        diff = sum_row.get(column) if sum_row is not None else None
        if mode == "cheaper" and not (pd.notna(diff) and float(diff) < 0):
            continue
        if mode == "expensive" and not (pd.notna(diff) and float(diff) >= 0):
            continue
        markets.append(market)
    return markets


def marketplace_for_column(column: str) -> str | None:
    if column in {"HaloSkins single", "HaloSkins total"}:
        return BASELINE_MARKETPLACE
    if column.endswith(" diff"):
        return column[: -len(" diff")]
    if column in {"Name", "Multiplier"}:
        return None
    return column


def export_comparison_csv(df: pd.DataFrame) -> str:
    visible_columns = [col for col in df.columns if not is_marker_column(col)]
    export_df = df[visible_columns].copy()
    if "__header_repeat__" in df.columns:
        export_df = export_df.loc[~df["__header_repeat__"].fillna(False).astype(bool)].copy()
    for column in export_df.columns:
        export_df[column] = export_df[column].map(lambda value, col=column: format_footer_value(col, value))
    return export_df.to_csv(index=False, encoding="utf-8-sig")


def render_tool_instructions() -> None:
    st.markdown(
        """
        <div class="tool-instructions">
          <h3>How To Use</h3>
          <ol>
            <li>Open the tool and click <strong>Update prices</strong> to fetch the latest prices from enabled marketplaces.</li>
            <li>The table compares the same CS2 skin basket across marketplaces using <strong>HaloSkins as the baseline</strong>.</li>
            <li>Use horizontal scroll to view all marketplaces. The <strong>Basket total</strong> row shows total basket cost per marketplace.</li>
          </ol>

          <h3>How To Read The Table</h3>
          <ul>
            <li><strong>HaloSkins single</strong>: price of one item on HaloSkins.</li>
            <li><strong>Multiplier</strong>: quantity used for that item in the basket.</li>
            <li><strong>HaloSkins total</strong>: <code>HaloSkins single x Multiplier</code>.</li>
            <li>Other marketplace columns show the same item cost with the multiplier applied.</li>
            <li><strong>diff</strong> columns show how much cheaper or more expensive that market is compared with HaloSkins total.</li>
            <li>Negative <code>%</code> = cheaper than HaloSkins.</li>
            <li>Positive <code>%</code> = more expensive than HaloSkins.</li>
            <li>Markets cheaper than HaloSkins are placed to the left of <strong>HaloSkins total</strong>. More expensive markets are placed to the right.</li>
            <li>Light gray prices mean the item was missing on that marketplace, so the tool used a fallback price. For <strong>Buff163</strong> and <strong>YouPin</strong>, it uses <strong>C5Game</strong> first; if C5Game is unavailable, it uses HaloSkins. Other markets use HaloSkins fallback.</li>
          </ul>

          <h3>What Multipliers Mean</h3>
          <p>
            Multipliers make cheap and expensive skins comparable in basket weight. Each item quantity is chosen so its HaloSkins total gets close to about <strong>$5,500</strong>, capped at <strong>1,000 units</strong>.
            Example: a <code>$5</code> item may use <code>1,000x</code>, while a <code>$4,000</code> item stays <code>1x</code>.
          </p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def format_simple_table(df: pd.DataFrame, formats: dict | None = None):
    styler = (
        df.style.set_table_styles([{"selector": "th", "props": [("text-align", "center")]}])
        .set_properties(**{"background-color": TABLE_BACKGROUND, "text-align": "center"})
    )
    if formats:
        styler = styler.format(formats)
    return styler


def to_utc8_datetime_series(values: pd.Series) -> pd.Series:
    return pd.to_datetime(values, utc=True, format="ISO8601").dt.tz_convert(UTC_PLUS_8)


def format_timestamp_utc8(value) -> str:
    if value is None or pd.isna(value):
        return ""
    timestamp = pd.to_datetime(value, utc=True, format="ISO8601").tz_convert(UTC_PLUS_8)
    return timestamp.strftime("%Y-%m-%d %H:%M:%S UTC+8")


def render_comparison_table_html(
    df: pd.DataFrame,
    column_widths: dict[str, int],
    *,
    fullscreen: bool = False,
    include_logos: bool = False,
) -> str:
    columns = list(column_widths)
    width_total = sum(column_widths.values())
    colgroup = "".join(f'<col style="width: {width}px">' for width in column_widths.values())
    headers = "".join(f"<th>{render_header_label(column, include_logos=include_logos)}</th>" for column in columns)
    rows = []
    for _, row in df.iterrows():
        row_class = comparison_row_class(row)
        cells = []
        for column in columns:
            value_html = (
                render_header_label(column, include_logos=include_logos)
                if row_class == "repeat-header-row"
                else escape(format_footer_value(column, row.get(column)))
            )
            cells.append(
                f'<td class="{comparison_cell_class(column, columns, row)}">'
                f"{value_html}</td>"
            )
        rows.append(f'<tr class="{row_class}">{"".join(cells)}</tr>')
    return (
        f'<div class="comparison-table-scroll{" fullscreen-table" if fullscreen else ""}">'
        f'<table class="comparison-html-table" style="width: {width_total}px">'
        f"<colgroup>{colgroup}</colgroup><thead><tr>{headers}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def render_header_label(column: str, *, include_logos: bool) -> str:
    label = escape(column)
    if not include_logos:
        return label
    marketplace = marketplace_for_column(column)
    if not marketplace:
        return label
    logo = marketplace_logo_src(marketplace)
    logo_html = (
        f'<img class="market-logo" src="{escape(logo)}" alt="{escape(marketplace)} logo">'
        if logo
        else f'<span class="market-logo-fallback">{escape(marketplace_initials(marketplace))}</span>'
    )
    return f'<span class="market-header">{logo_html}<span class="header-label">{label}</span></span>'


def marketplace_logo_src(marketplace: str) -> str | None:
    filename = MARKETPLACE_LOGO_FILES.get(marketplace)
    if not filename:
        return None
    path = LOGO_DIR / filename
    if not path.exists():
        return None
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    mime_type = {
        ".jfif": "image/jpeg",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".svg": "image/svg+xml",
        ".webp": "image/webp",
    }.get(path.suffix.lower(), "application/octet-stream")
    return f"data:{mime_type};base64,{encoded}"


def marketplace_initials(name: str) -> str:
    parts = re.findall(r"[A-Za-z0-9]+", name)
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return "".join(part[0] for part in parts[:2]).upper()


def comparison_row_class(row) -> str:
    if is_true_marker(row.get("__header_repeat__", False)):
        return "repeat-header-row"
    if row.get("Name") == "Basket total":
        return "sum-row"
    return "item-row"


def comparison_cell_class(column: str, columns: list[str], row) -> str:
    classes = [footer_cell_class(column, columns) or "neutral-cell"]
    marker = f"__halo_fallback__{column}"
    if marker in row.index and is_true_marker(row.get(marker, False)):
        classes.append("fallback-cell")
    elif column.endswith(" diff"):
        value = row.get(column)
        if value is not None and pd.notna(value) and not isinstance(value, str):
            numeric_value = float(value)
            if numeric_value < 0:
                classes.append("diff-negative")
            elif numeric_value > 0:
                classes.append("diff-positive")
    return " ".join(classes)


def is_true_marker(value) -> bool:
    return isinstance(value, bool) and value


def footer_cell_class(column: str, columns: list[str]) -> str:
    if column == "HaloSkins total":
        return "halo-total"
    if column in {"Name", "HaloSkins single", "Multiplier"} or "HaloSkins total" not in columns:
        return ""
    halo_index = columns.index("HaloSkins total")
    column_index = columns.index(column)
    if column_index < halo_index:
        return "cheaper-market"
    return "expensive-market"


def format_footer_value(column: str, value) -> str:
    if value is None or pd.isna(value):
        return ""
    if isinstance(value, str):
        return value
    if column == "Multiplier":
        return f"{int(value):,}"
    if column.endswith(" diff"):
        return f"{value:,.2f}%"
    if column != "Name":
        return f"${value:,.2f}"
    return str(value)


def format_currency_value(value) -> str:
    if value is None or pd.isna(value):
        return "N/A"
    return f"${float(value):,.2f}"


def format_percent_value(value) -> str:
    if value is None or pd.isna(value):
        return "N/A"
    return f"{float(value):,.2f}%"


class SnapshotQualityError(RuntimeError):
    pass


def collect_snapshot() -> tuple[int, str, float]:
    registry = build_adapter_registry()
    clear_backup_cache()
    clear_csgoskins_cache()
    enabled_keys = db.get_enabled_adapter_keys()
    items = db.get_adapter_items()
    all_results: list[PriceResult] = []
    adapters = [registry[key] for key in enabled_keys if key in registry]
    expected_count = len(items) * len(adapters)
    if expected_count == 0:
        raise SnapshotQualityError("No active basket items or enabled marketplaces are available to update.")

    for adapter in adapters:
        try:
            all_results.extend(adapter.fetch_prices(items))
        except Exception as exc:
            all_results.extend(
                [
                    PriceResult(
                        marketplace=adapter.name,
                        market_hash_name=item.market_hash_name,
                        price=None,
                        currency="USD",
                        fetch_status="error",
                        error_details=str(exc),
                    )
                    for item in items
                ]
            )

    all_results = apply_backup_prices(all_results, items)

    success_count = sum(
        1
        for result in all_results
        if result.fetch_status == "ok" and db.normalize_to_usd(result.price, result.currency) is not None
    )
    success_rate = success_count / expected_count
    if success_rate < db.MIN_SNAPSHOT_SUCCESS_RATE:
        raise SnapshotQualityError(
            f"Update aborted: only {success_count}/{expected_count} prices "
            f"({success_rate:.0%}) were received. Previous successful data is still displayed."
        )

    recordable_results, skipped_marketplaces = filter_recordable_marketplaces(all_results, len(items))
    if not recordable_results:
        raise SnapshotQualityError(
            "Update aborted: every marketplace had too many missing/error rows. "
            "Previous successful data is still displayed."
        )

    snapshot_id, timestamp = db.save_snapshot_results(recordable_results)
    return snapshot_id, timestamp, success_rate


def filter_recordable_marketplaces(
    results: list[PriceResult],
    item_count: int,
) -> tuple[list[PriceResult], list[str]]:
    if item_count <= 0:
        return results, []

    grouped: dict[str, list[PriceResult]] = {}
    for result in results:
        grouped.setdefault(result.marketplace, []).append(result)

    kept: list[PriceResult] = []
    skipped: list[str] = []
    for marketplace, marketplace_results in grouped.items():
        ok_count = sum(
            1
            for result in marketplace_results
            if result.fetch_status == "ok" and db.normalize_to_usd(result.price, result.currency) is not None
        )
        error_count = sum(1 for result in marketplace_results if result.fetch_status == "error")
        error_rate = error_count / max(item_count, len(marketplace_results), 1)
        if (
            marketplace != BASELINE_MARKETPLACE
            and ok_count == 0
            and error_rate >= db.MAX_MARKETPLACE_FAILURE_RATE
        ):
            skipped.append(marketplace)
            continue
        kept.extend(marketplace_results)
    return kept, skipped


def render_fetch_status() -> None:
    st.subheader("Adapter Status")
    rows = [row for row in cached_marketplaces() if row["enabled"] or row["is_baseline"]]
    if not rows:
        st.caption("No marketplaces enabled.")
        return
    status_df = pd.DataFrame(rows)[["name", "last_status", "last_error", "updated_at"]]
    status_df["updated_at"] = status_df["updated_at"].map(format_timestamp_utc8)
    st.dataframe(format_simple_table(status_df), use_container_width=True, hide_index=True)


def render_history() -> None:
    range_label = st.segmented_control(
        "Time range",
        options=["day", "week", "month", "year", "all"],
        default="week",
    )
    since = since_for_range(range_label)
    rows = cached_history_totals(since_iso=since)
    if not rows:
        st.info("No historical snapshots match this time range.")
        render_update_runs_table()
        return

    hist = pd.DataFrame([dict(row) for row in rows])
    hist["timestamp_utc8"] = to_utc8_datetime_series(hist["timestamp"])
    enabled = set(enabled_marketplace_names())
    hist = hist[hist["marketplace"].isin(enabled)]
    if hist.empty:
        st.info("No enabled marketplaces have historical snapshots in this time range.")
        render_update_runs_table()
        return
    marketplaces = sorted(hist["marketplace"].unique().tolist())
    default_marketplaces = default_history_marketplaces(hist, marketplaces)
    selected = st.multiselect(
        "Marketplaces",
        marketplaces,
        default=default_marketplaces,
        key=f"history_marketplaces_{range_label}",
    )
    render_marketplace_tag_logo_decorator(marketplaces)
    if not selected:
        st.info("Select at least one marketplace.")
        render_update_runs_table()
        return

    chart_df = hist[hist["marketplace"].isin(selected)]
    fig = px.line(
        chart_df,
        x="timestamp_utc8",
        y="total_cost",
        color="marketplace",
        markers=True,
        labels={
            "timestamp_utc8": "Timestamp (UTC+8)",
            "total_cost": "Total basket cost (USD)",
            "marketplace": "Marketplace",
        },
    )
    fig.update_traces(
        hovertemplate=(
            "<b>%{fullData.name}</b><br>"
            "Timestamp (UTC+8): %{x}<br>"
            "Total basket cost: $%{y:,.2f}"
            "<extra></extra>"
        )
    )
    fig.update_layout(hovermode="closest", legend_title_text="")
    st.plotly_chart(fig, use_container_width=True)

    st.dataframe(
        format_simple_table(
            format_history_table(chart_df),
            {"total_cost": "${:,.2f}"},
        ),
        use_container_width=True,
        hide_index=True,
    )
    render_update_runs_table()


def default_history_marketplaces(hist: pd.DataFrame, marketplaces: list[str]) -> list[str]:
    latest = (
        hist.sort_values(["marketplace", "timestamp_utc8"])
        .groupby("marketplace", as_index=False)
        .tail(1)
    )
    latest = latest[latest["total_cost"].notna()]
    baseline_name = "HaloSkins"
    selected: list[str] = []
    if baseline_name in marketplaces:
        selected.append(baseline_name)

    ranked = latest[latest["marketplace"] != baseline_name].sort_values("total_cost")
    for marketplace in ranked.head(2)["marketplace"].tolist():
        if marketplace not in selected:
            selected.append(marketplace)
    ranked_most_expensive = ranked[ranked["marketplace"] != STEAM_MARKETPLACE]
    for marketplace in ranked_most_expensive.tail(2)["marketplace"].tolist():
        if marketplace not in selected:
            selected.append(marketplace)

    return [marketplace for marketplace in selected if marketplace in marketplaces] or marketplaces[:1]


def format_history_table(chart_df: pd.DataFrame) -> pd.DataFrame:
    table_df = chart_df[
        ["timestamp_utc8", "marketplace", "total_cost", "available_count", "fallback_count"]
    ].sort_values(["timestamp_utc8", "marketplace"], ascending=[False, True])
    table_df = table_df.rename(columns={"timestamp_utc8": "timestamp"})
    table_df["timestamp"] = table_df["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S UTC+8")
    return table_df


def render_update_runs_table() -> None:
    st.subheader("Update Run Log")
    if not hasattr(db, "update_runs"):
        st.caption("Update run logging is not available until the matching database module is deployed.")
        return
    rows = cached_update_runs()
    if not rows:
        st.caption("No manual or automatic update runs recorded yet.")
        return
    table_df = pd.DataFrame(rows)
    table_df["started_at"] = to_utc8_datetime_series(table_df["started_at"]).dt.strftime("%Y-%m-%d %H:%M:%S UTC+8")
    table_df["finished_at"] = to_utc8_datetime_series(table_df["finished_at"]).dt.strftime("%Y-%m-%d %H:%M:%S UTC+8")
    table_df["duration"] = table_df["duration_seconds"].map(format_duration_seconds)
    table_df["success_rate"] = table_df["success_rate"].map(
        lambda value: "" if pd.isna(value) else f"{float(value):.0%}"
    )
    table_df = table_df[
        [
            "started_at",
            "finished_at",
            "source",
            "duration",
            "status",
            "snapshot_id",
            "success_rate",
            "error_details",
        ]
    ].rename(
        columns={
            "started_at": "started",
            "finished_at": "finished",
            "snapshot_id": "snapshot",
            "success_rate": "data received",
            "error_details": "error",
        }
    )
    st.dataframe(format_simple_table(table_df), use_container_width=True, hide_index=True)


def format_duration_seconds(value: object) -> str:
    try:
        seconds = max(0.0, float(value))
    except (TypeError, ValueError):
        return ""
    minutes, rem = divmod(int(round(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {rem}s"
    if minutes:
        return f"{minutes}m {rem}s"
    return f"{rem}s"


def since_for_range(label: str | None) -> str | None:
    if label == "day":
        delta = timedelta(days=1)
    elif label == "week":
        delta = timedelta(weeks=1)
    elif label == "month":
        delta = timedelta(days=31)
    elif label == "year":
        delta = timedelta(days=365)
    else:
        return None
    return (datetime.now(timezone.utc) - delta).isoformat(timespec="seconds")


def render_basket_items() -> None:
    rows = cached_basket_items(active_only=False)
    if not rows:
        st.warning("No basket items are stored.")
        return

    df = pd.DataFrame(rows)
    df["active"] = df["active"].astype(bool)
    edited = st.data_editor(
        df[
            [
                "item_id",
                "market_hash_name",
                "active",
                "multiplier",
                "notes",
                "source_rank",
                "source_amount",
                "price_compare_url",
                "priceempire_url",
                "steamanalyst_url",
            ]
        ],
        use_container_width=True,
        hide_index=True,
        disabled=[
            "item_id",
            "market_hash_name",
            "source_rank",
            "source_amount",
            "price_compare_url",
            "priceempire_url",
            "steamanalyst_url",
        ],
        column_config={
            "item_id": st.column_config.NumberColumn("item_id", width="small"),
            "market_hash_name": st.column_config.TextColumn("market_hash_name", width="large"),
            "active": st.column_config.CheckboxColumn("active"),
            "multiplier": st.column_config.NumberColumn("multiplier", min_value=1, step=1),
            "notes": st.column_config.TextColumn("notes"),
            "source_rank": st.column_config.NumberColumn("source_rank", width="small"),
            "source_amount": st.column_config.NumberColumn("source_amount"),
            "price_compare_url": st.column_config.LinkColumn("CSGOSKINS link", width="medium"),
            "priceempire_url": st.column_config.LinkColumn("PriceEmpire link", width="medium"),
            "steamanalyst_url": st.column_config.LinkColumn("SteamAnalyst link", width="medium"),
        },
    )
    if st.button("Save basket item changes", type="primary"):
        db.update_basket_items(edited.to_dict("records"))
        clear_data_cache()
        st.success("Basket item changes saved.")
        st.rerun()

    render_item_update_status(rows)


def render_item_update_status(items: list[dict]) -> None:
    st.subheader("Last Update Item Status")
    snapshot = cached_latest_snapshot()
    if snapshot is None:
        st.caption("No saved snapshot yet.")
        return

    points = cached_latest_price_points(int(snapshot["snapshot_id"]))
    status_rows = build_item_update_status_rows(items, points, snapshot["timestamp"])
    if not status_rows:
        st.caption("No item status rows are available for the latest snapshot.")
        return
    st.dataframe(
        format_simple_table(pd.DataFrame(status_rows)),
        use_container_width=True,
        hide_index=True,
    )


def build_item_update_status_rows(items: list[dict], points, timestamp: str) -> list[dict]:
    csgoskins_marketplaces = {name for _, name, _ in CSGOSKINS_MARKETS}
    points_by_item_id: dict[int, list] = {}
    points_by_name: dict[str, list] = {}
    for point in points:
        if point["item_id"] is not None:
            points_by_item_id.setdefault(int(point["item_id"]), []).append(point)
        points_by_name.setdefault(point["market_hash_name"], []).append(point)

    rows = []
    for item in items:
        item_id = int(item["item_id"])
        name = item["market_hash_name"]
        item_points = points_by_item_id.get(item_id) or points_by_name.get(name, [])
        rows.append(
            {
                "item_id": item_id,
                "market_hash_name": name,
                "active": bool(item["active"]),
                "last_update": format_timestamp_utc8(timestamp),
                "status": item_update_status_label(item, item_points),
                "ok": sum(1 for point in item_points if point_success(point)),
                "missing": sum(1 for point in item_points if point["fetch_status"] == "missing"),
                "errors": sum(1 for point in item_points if point["fetch_status"] == "error"),
                "failure_details": summarize_item_failures(item_points, csgoskins_marketplaces),
                "missing_markets": summarize_market_list(
                    point["marketplace"] for point in item_points if point["fetch_status"] == "missing"
                ),
            }
        )
    return rows


def item_update_status_label(item: dict, item_points: list) -> str:
    if not bool(item["active"]):
        return "inactive"
    if not item_points:
        return "not fetched"
    ok_count = sum(1 for point in item_points if point_success(point))
    missing_count = sum(1 for point in item_points if point["fetch_status"] == "missing")
    error_count = sum(1 for point in item_points if point["fetch_status"] == "error")
    if error_count:
        return "failed" if ok_count == 0 else "partial"
    if missing_count:
        return "missing" if ok_count == 0 else "partial"
    return "ok"


def point_success(point) -> bool:
    return point["fetch_status"] == "ok" and point["normalized_price"] is not None


def summarize_item_failures(item_points: list, csgoskins_marketplaces: set[str]) -> str:
    grouped: dict[str, list[str]] = {}
    csgoskins_points = [point for point in item_points if point["marketplace"] in csgoskins_marketplaces]
    csgoskins_missing_not_listed = [
        point
        for point in csgoskins_points
        if point["fetch_status"] == "missing"
        and "did not list this marketplace" in (point["error_details"] or "").lower()
    ]
    likely_csgoskins_parse_gap = (
        len(csgoskins_missing_not_listed) >= 3
        and not any(point_success(point) for point in csgoskins_points)
    )
    for point in item_points:
        status = point["fetch_status"]
        if status not in {"error", "missing"}:
            continue
        marketplace = point["marketplace"]
        label = failure_reason_label(point, csgoskins_marketplaces, likely_csgoskins_parse_gap)
        grouped.setdefault(label, []).append(marketplace)
    return "; ".join(f"{label}: {summarize_market_list(markets)}" for label, markets in grouped.items())


def failure_reason_label(point, csgoskins_marketplaces: set[str], likely_csgoskins_parse_gap: bool = False) -> str:
    marketplace = point["marketplace"]
    status = point["fetch_status"]
    error = point["error_details"] or "no adapter reason stored"
    error_lower = error.lower()

    if marketplace in csgoskins_marketplaces:
        code = error_code(error)
        if status == "error":
            if code in {"401", "403"}:
                return f"CSGOSKINS restricted access {code}"
            if code == "429":
                return "CSGOSKINS bot/rate limit 429"
            if code:
                return f"CSGOSKINS fetch error {code}"
            if "no parseable marketplace offers" in error_lower:
                return "CSGOSKINS page fetched but no offers parsed"
            return f"CSGOSKINS fetch error ({short_error(error)})"
        if "no csgoskins link" in error_lower:
            return "CSGOSKINS missing link"
        if "did not list this marketplace" in error_lower:
            if likely_csgoskins_parse_gap:
                return "CSGOSKINS scrape/parser gap, markets not confirmed absent"
            return "CSGOSKINS page loaded, marketplace not listed"
        return f"CSGOSKINS missing ({short_error(error)})"

    if status == "error":
        code = error_code(error)
        prefix = f"API error {code}" if code else "API error"
        return f"{prefix} ({short_error(error)})"
    if "not configured" in error_lower:
        return f"API missing credentials/config ({short_error(error)})"
    if "no exact" in error_lower or "did not return this exact" in error_lower or "returned no exact" in error_lower:
        return "API returned no exact item/listing"
    if "selector" in error_lower or "regex" in error_lower:
        return "webpage parser found no price"
    return f"missing ({short_error(error)})"


def error_code(error: str) -> str | None:
    match = re.search(r"\b(4\d\d|5\d\d)\b", error or "")
    return match.group(1) if match else None


def short_error(error: str, limit: int = 80) -> str:
    error = " ".join(str(error).split())
    return error if len(error) <= limit else f"{error[: limit - 3]}..."


def summarize_market_list(markets) -> str:
    values = sorted({market for market in markets if market})
    if not values:
        return ""
    if len(values) <= 6:
        return ", ".join(values)
    return f"{', '.join(values[:6])}, +{len(values) - 6} more"


def render_marketplace_settings() -> None:
    registry = build_adapter_registry()
    rows = []
    for row in cached_marketplaces():
        adapter = registry.get(row["adapter_key"])
        rows.append(
            {
                "adapter_key": row["adapter_key"],
                "name": row["name"],
                "enabled": bool(row["enabled"] or row["is_baseline"]),
                "is_baseline": bool(row["is_baseline"]),
                "requires_credentials": bool(row["requires_credentials"]),
                "credentials_configured": bool(adapter.credentials_configured()) if adapter else False,
                "last_status": row["last_status"],
                "last_error": row["last_error"],
            }
        )

    df = pd.DataFrame(rows)
    edited = st.data_editor(
        df,
        use_container_width=True,
        hide_index=True,
        disabled=[
            "adapter_key",
            "name",
            "is_baseline",
            "requires_credentials",
            "credentials_configured",
            "last_status",
            "last_error",
        ],
        column_config={
            "enabled": st.column_config.CheckboxColumn("enabled"),
            "is_baseline": st.column_config.CheckboxColumn("baseline"),
            "requires_credentials": st.column_config.CheckboxColumn("needs credentials"),
            "credentials_configured": st.column_config.CheckboxColumn("credentials configured"),
        },
    )
    st.caption(f"{BASELINE_MARKETPLACE} is always enabled because all differences use it as the baseline.")
    if st.button("Save marketplace settings", type="primary"):
        db.update_marketplace_settings(edited.to_dict("records"))
        clear_data_cache()
        st.success("Marketplace settings saved.")
        st.rerun()

    render_marketplace_coverage()
    render_fetch_status()
    render_performance_diagnostics()


def render_performance_diagnostics() -> None:
    st.subheader("Performance Diagnostics")
    metrics = st.session_state.get("performance_metrics") or []
    if not metrics:
        st.caption("Open Current Basket Comparison once to collect render timing.")
        return
    st.dataframe(
        pd.DataFrame(metrics),
        use_container_width=True,
        hide_index=True,
    )


def render_marketplace_coverage() -> None:
    st.subheader("Marketplace Coverage")
    snapshot = cached_latest_snapshot()
    if snapshot is None:
        st.caption("No saved snapshot yet.")
        return

    _, coverage, _ = cached_comparison_data(int(snapshot["snapshot_id"]))
    st.dataframe(
        format_simple_table(coverage, {"Total cost": lambda value: "N/A" if pd.isna(value) else f"${value:,.2f}"}),
        use_container_width=True,
        hide_index=True,
    )


def enabled_marketplace_names() -> list[str]:
    rows = cached_marketplaces()
    names = [row["name"] for row in rows if row["enabled"] or row["is_baseline"]]
    if BASELINE_MARKETPLACE in names:
        names = [BASELINE_MARKETPLACE] + [name for name in names if name != BASELINE_MARKETPLACE]
    return names


if __name__ == "__main__":
    main()
