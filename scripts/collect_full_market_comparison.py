from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters.fx import fetch_cny_to_usd_rate  # noqa: E402
from adapters.haloskins import _url as haloskins_url  # noqa: E402
from adapters.uuskins import _sign  # noqa: E402


OUTPUT_PATH = ROOT / "outputs" / "full_market_comparison_raw.json"
UUSKINS_CATALOG_URL = "https://api.uuskins.com/api/vertex/commodity/query/market/openapi/list"


def main() -> None:
    load_dotenv(ROOT / ".env")
    required = {
        "HaloSkins": haloskins_url(),
        "C5Game": os.getenv("C5GAME_APP_KEY") or os.getenv("C5GAME_API_KEY"),
        "UUSKINS app key": os.getenv("UUSKINS_APP_KEY"),
        "UUSKINS private key": os.getenv("UUSKINS_PRIVATE_KEY"),
        "Exchange-rate URL": os.getenv("EXCHANGERATE_USD_LATEST_URL"),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise SystemExit(f"Missing configuration: {', '.join(missing)}")

    print("Fetching HaloSkins catalogue...")
    halo = fetch_haloskins()
    names = sorted(halo, key=str.casefold)
    print(f"HaloSkins names: {len(names):,}")

    print("Fetching C5Game prices...")
    c5 = fetch_c5game(names)
    print(f"C5Game exact matches: {len(c5):,}")

    print("Fetching UUSKINS catalogue...")
    uu = fetch_uuskins_catalogue()
    print(f"UUSKINS catalogue names: {len(uu):,}")

    rows = []
    for name in names:
        halo_row = halo[name]
        c5_row = c5.get(name, {})
        uu_row = uu.get(name, {})
        rows.append(
            {
                "market_hash_name": name,
                "haloskins_price_usd": float_or_none(halo_row.get("lowest_price")),
                "haloskins_quantity": int_or_none(halo_row.get("quantity")),
                "c5game_price_usd": float_or_none(c5_row.get("price_usd")),
                "c5game_count": int_or_none(c5_row.get("count")),
                "uuskins_price_usd": float_or_none(uu_row.get("min_price")),
                "uuskins_count": int_or_none(uu_row.get("count")),
            }
        )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_urls": {
            "haloskins": "https://api.haloskins.com/steam-trade-center/sale/data/list",
            "c5game": os.getenv("C5GAME_API_URL", "https://openapi.c5game.com/merchant/product/price/batch"),
            "uuskins": os.getenv("UUSKINS_CATALOG_URL", UUSKINS_CATALOG_URL),
        },
        "coverage": {
            "haloskins": len(halo),
            "c5game": len(c5),
            "uuskins_catalogue": len(uu),
            "uuskins_haloskins_overlap": sum(name in uu for name in names),
        },
        "rows": rows,
    }
    OUTPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved {len(rows):,} rows to {OUTPUT_PATH}")
    print(json.dumps(payload["coverage"], indent=2))


def fetch_haloskins() -> dict[str, dict]:
    headers = {
        "User-Agent": os.getenv(
            "HALOSKINS_USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
        ),
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.haloskins.com",
        "Referer": "https://www.haloskins.com/",
    }
    response = None
    for attempt in range(3):
        response = requests.get(
            haloskins_url(),
            headers=headers,
            timeout=float(os.getenv("HALOSKINS_TIMEOUT_SECONDS", "90")),
        )
        if response.status_code != 403 or attempt == 2:
            break
        time.sleep(2 ** attempt)
    assert response is not None
    if not response.ok:
        detail = " ".join(response.text.split())[:500]
        raise RuntimeError(
            f"HaloSkins returned HTTP {response.status_code}. "
            f"Response: {detail or '<empty body>'}"
        )
    body = response.json()
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        raise RuntimeError("HaloSkins response did not contain a data list.")
    result: dict[str, dict] = {}
    for row in data:
        name = row.get("market_hash_name") if isinstance(row, dict) else None
        price = float_or_none(row.get("lowest_price")) if isinstance(row, dict) else None
        if not name or price is None:
            continue
        existing = result.get(str(name))
        if existing is None or price < float(existing["lowest_price"]):
            result[str(name)] = row
    return result


def fetch_c5game(names: list[str]) -> dict[str, dict]:
    app_key = os.getenv("C5GAME_APP_KEY") or os.getenv("C5GAME_API_KEY")
    endpoint = os.getenv("C5GAME_API_URL", "https://openapi.c5game.com/merchant/product/price/batch")
    batch_size = int(os.getenv("FULL_COMPARE_C5_BATCH_SIZE", "200"))
    cny_to_usd = fetch_cny_to_usd_rate()
    result: dict[str, dict] = {}
    for start in range(0, len(names), batch_size):
        batch = names[start : start + batch_size]
        response = requests.post(
            endpoint,
            params={"app-key": app_key},
            json={"appId": 730, "marketHashNames": batch},
            headers={"Content-Type": "application/json", "Accept-Encoding": "gzip, br, zstd, deflate"},
            timeout=60,
        )
        response.raise_for_status()
        body = response.json()
        if body.get("success") is False:
            raise RuntimeError(body.get("errorMsg") or body.get("message") or "C5Game returned success=false")
        for name, row in (body.get("data") or {}).items():
            price_cny = float_or_none(row.get("price")) if isinstance(row, dict) else None
            if price_cny is not None:
                result[str(name)] = {**row, "price_usd": price_cny * cny_to_usd}
        print(f"  C5Game {min(start + len(batch), len(names)):,}/{len(names):,}")
    return result


def fetch_uuskins_catalogue() -> dict[str, dict]:
    app_key = os.environ["UUSKINS_APP_KEY"].strip()
    private_key = os.environ["UUSKINS_PRIVATE_KEY"].replace("\\n", "\n").strip()
    endpoint = os.getenv("UUSKINS_CATALOG_URL", UUSKINS_CATALOG_URL)
    page_size = int(os.getenv("FULL_COMPARE_UUSKINS_PAGE_SIZE", "1000"))
    max_pages = int(os.getenv("FULL_COMPARE_UUSKINS_MAX_PAGES", "45"))
    result: dict[str, dict] = {}

    for page_index in range(1, max_pages + 1):
        unsigned: dict[str, object] = {"appKey": app_key, "pageIndex": page_index, "pageSize": page_size}
        payload = {**unsigned, "sign": _sign(unsigned, private_key)}
        response = requests.post(endpoint, json=payload, timeout=60)
        response.raise_for_status()
        body = response.json()
        code = body.get("code") if isinstance(body, dict) else None
        if code not in (0, 200, "0", "200"):
            raise RuntimeError(f"UUSKINS code={code}: {body.get('message') or body.get('msg')}")
        data = body.get("data") if isinstance(body, dict) else None
        items = data.get("items", []) if isinstance(data, dict) else []
        for row in items:
            name = row.get("market_hash_name") if isinstance(row, dict) else None
            if name:
                result[str(name)] = row
        print(f"  UUSKINS page {page_index}: {len(items):,} rows ({len(result):,} total)")
        if len(items) < page_size:
            return result
    raise RuntimeError(
        f"UUSKINS catalogue did not finish within {max_pages} pages. "
        "Increase FULL_COMPARE_UUSKINS_PAGE_SIZE or resume after the daily limit resets."
    )


def float_or_none(value) -> float | None:
    try:
        return float(value) if value is not None and str(value).strip() else None
    except (TypeError, ValueError):
        return None


def int_or_none(value) -> int | None:
    try:
        return int(float(value)) if value is not None and str(value).strip() else None
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    main()
