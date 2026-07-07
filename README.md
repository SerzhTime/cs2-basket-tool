# CS2 Basket Price Comparison

Local-only CS2 skin basket price comparison app. HaloSkins is the baseline marketplace, and every non-baseline marketplace shows item-level and basket-level percentage differences against HaloSkins.

## What is included

- Streamlit web UI
- SQLite local history database
- First-run import from `data/basket.xlsx`
- Manual `Update prices` button only
- Snapshot history saved on every update
- Current comparison table with `Basket total`
- Historical Plotly line chart with day/week/month/year/all ranges
- Basket item enable/disable and notes
- Basket item integer multipliers for weighted basket comparison, capped at 1000
- Marketplace enable/disable settings
- Adapter interface for live API and page-backed marketplace sources
- Live HaloSkins lowest-price adapter
- Live C5Game batch-price adapter
- Live CSFloat lowest-listing adapter
- Live Waxpeer bulk-prices adapter
- Live DMarket lowest-offer adapter
- Live Market.CSGO bulk-prices adapter
- CSGOSKINS page-backed adapters for marketplaces without direct API access
- SteamAnalyst lower-priority backup for selected missing marketplace prices
- Generic webpage adapter scaffold for price-compare pages without APIs
- Cloud IP probe script and GitHub Actions workflow for checking online deployment compatibility before moving the full app
- Optional mixed local/cloud mode through `DATABASE_URL` and Neon/Postgres

## Setup

```powershell
cd C:\Users\c\Documents\Codex\2026-06-24\files-mentioned-by-the-user-cs2dt\outputs\cs2dt_basket_tool
C:\Users\c\AppData\Local\Programs\Python\Python314\python.exe -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\streamlit.exe run app.py
```

Open the local URL shown by Streamlit, usually `http://localhost:8501`.

To test whether APIs and marketplace pages tolerate cloud/datacenter IPs before deployment, see `CLOUD_PROBE.md`.

For the online Streamlit + Neon deployment checklist, see `DEPLOYMENT.md`.

## Mixed local/cloud database mode

By default the app uses local SQLite at `data/price_history.sqlite`. If `DATABASE_URL` is set in `.env`, GitHub Actions secrets, or Streamlit secrets, the same `db.py` interface uses Postgres instead. This lets the local app, online Streamlit app, and scheduled GitHub Actions updates read and write the same shared history.

One-time migration from local SQLite to Postgres:

```powershell
.\.venv\Scripts\python.exe scripts\migrate_sqlite_to_postgres.py
```

Manual cloud-compatible scheduled update entry point:

```powershell
.\.venv\Scripts\python.exe scripts\scheduled_update.py
```

The GitHub workflow `.github/workflows/scheduled-price-update.yml` is manual by default. Enable its commented `schedule` block only after a manual run writes a good snapshot.

## Data files

- `data/basket.xlsx` is copied from the provided workbook and synced on app startup.
- `data/price_history.sqlite` is created automatically.

The database keeps all historical snapshots. Disabling a basket item only affects future active-basket calculations; it does not delete old price points. Workbook sync preserves active status, notes, and multipliers, while updating rank, source amount, CSGOSKINS price-compare links, and backup price-compare links.

## Database schema

- `basket_items`: item key, active status, multiplier, notes, source rank, source amount
- `marketplaces`: adapter key, display name, enabled flag, baseline flag, credential requirement, last status
- `snapshots`: snapshot id and timestamp
- `price_points`: one marketplace/item result per snapshot, including price, currency, stock count, status, and error details

## Adapter contract

Each adapter returns normalized `PriceResult` rows:

- marketplace
- market_hash_name
- price
- currency
- stock_count
- fetch_status
- error_details

The app catches adapter failures independently so one failed marketplace does not block the whole snapshot.

## Current adapter status

- `HaloSkins`: live lowest-price list adapter, used as the baseline
- `CSFloat`: live lowest-listing adapter
- `Waxpeer`: live bulk-prices adapter. The API `min` value is treated as 1/1000 USD by default.
- `Market.CSGO`: live bulk-prices adapter using `/api/v2/prices/USD.json`
- `C5Game`: live batch-price adapter using realtime USD/CNY conversion from ExchangeRate API
- `DMarket`: signed live lowest-offer adapter using `/marketplace-api/v2/offers`; `priceCents` is treated as USD cents
- `CS.MONEY`, `LIS-SKINS`, `Aim.market`, `Skin.Land`, `SkinBaron`, `Skins.com`, `Exeskins`, `Avan.market`, `Skinvault`, `UUSKINS`, `Tradeit.gg`, `SkinPlace`, `ShadowPay`, `SkinSwap`: page-backed adapters using each basket row's CSGOSKINS link
- `PriceCompare Web`: planned webpage adapter, disabled by default; configure URL templates/selectors in `.env`

For C5Game, the adapter sends the exact basket `market_hash_name` values as `marketHashNames` and reads one returned lowest price per exact item. The basket total is the sum of one price per active basket skin, not the total value of all platform listings for that skin type.

C5Game returns CNY prices, so the adapter fetches `EXCHANGERATE_USD_LATEST_URL` during every price update and stores converted USD values in SQLite. If that realtime conversion request fails, C5Game rows are saved as errors and excluded from totals rather than mixing CNY into USD basket comparisons.

If any other adapter returns non-USD prices, set the corresponding `FX_<CURRENCY>_TO_USD` value in `.env`. Without a conversion rate, the original price is stored in SQLite but excluded from USD comparison totals to avoid mixing currencies.

## Webpage-backed sources

When a marketplace has no usable API, add it as an adapter that fetches a price-compare page and parses the visible price into the same `PriceResult` shape as API adapters. The first generic scaffold is `PriceCompareWebAdapter`.

Example `.env` shape:

```dotenv
PRICE_COMPARE_WEB_MARKETPLACE_NAME=ExampleCompare
PRICE_COMPARE_WEB_URL_TEMPLATE=https://example.test/search?q={item}
PRICE_COMPARE_WEB_PRICE_SELECTOR=.result-row[data-market="Example"] .price
PRICE_COMPARE_WEB_STOCK_SELECTOR=.result-row[data-market="Example"] .stock
PRICE_COMPARE_WEB_CURRENCY=USD
```

Use `{item}` for URL-encoded exact `market_hash_name`, or `{item_raw}` only when the website accepts raw item names. If a page needs JavaScript rendering, login, anti-bot checks, or complex row matching, create a dedicated adapter instead of forcing it through the generic scaffold.

## CSGOSKINS page-backed sources

The CSGOSKINS adapters read the `CSGOSKINS links` column from `data/basket.xlsx`. During one update, each item page is fetched once and cached across all CSGOSKINS-backed marketplaces. Requests use randomized delays configured by:

```dotenv
CSGOSKINS_DELAY_SECONDS=4.0
CSGOSKINS_DELAY_JITTER_SECONDS=4.0
CSGOSKINS_RETRIES=0
```

If CSGOSKINS blocks requests or a marketplace is missing on a page, the row is stored as missing and the comparison table uses the HaloSkins fallback price for totals.

## Backup page sources

SteamAnalyst links are stored per basket item and used only after primary sources finish. The backup layer checks only item/marketplace results that are still missing or errored; it does not replace successful API or CSGOSKINS values. Parsed SteamAnalyst rows currently cover a subset of tracked markets including `CS.MONEY`, `DMarket`, `LIS-SKINS`, `Skin.Land`, `CSFloat`, `Waxpeer`, `Tradeit.gg`, and `SkinSwap`.

PriceEmpire links are stored from the workbook but are not active as a live backup yet. Direct requests currently return a challenge page, and reader output did not expose reliable marketplace prices during testing.

SteamAnalyst backup values are sanity-checked against HaloSkins when available to reduce phase/variant mismatch risk. Configure with:

```dotenv
STEAMANALYST_BACKUP_ENABLED=1
STEAMANALYST_BACKUP_MIN_BASELINE_RATIO=0.1
STEAMANALYST_BACKUP_MAX_BASELINE_RATIO=4.0
```

## Manual Missing-Price Repair

The Current Basket Comparison tab includes `Repair Missing Marketplace Prices` under the main table. It updates only missing rows in the latest snapshot and keeps the original snapshot timestamp, so the historical graph does not get a new point.

- API-backed markets use only their API adapters during repair.
- Non-API markets first use each item marketplace URL from `data/basket.xlsx`.
- Third-party price-compare backups are used only when the direct marketplace page is unavailable.
- Direct page prices are accepted only when they are plausible against the current HaloSkins item price, controlled by `DIRECT_MARKET_MIN_BASELINE_RATIO` and `DIRECT_MARKET_MAX_BASELINE_RATIO`.

Missing information needed for live integrations:

- DMarket Doppler/Gamma Doppler phase mapping if basket-level non-phase item names should match a specific DMarket phase listing instead of being marked missing.
- Any required item lookup format if exact `market_hash_name` is not accepted directly
- Price-compare website URL patterns, CSS selectors or JSON payload locations, rate limits, and whether pages require JavaScript rendering or login
