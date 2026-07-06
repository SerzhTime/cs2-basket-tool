from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters import BasketItem, build_adapter_registry  # noqa: E402
from basket import load_basket_rows  # noqa: E402


BLOCK_PATTERNS = [
    "access denied",
    "attention required",
    "captcha",
    "cf-chl",
    "cloudflare",
    "forbidden",
    "just a moment",
    "rate limit",
    "request blocked",
    "unusual traffic",
]
PRICE_PATTERN = re.compile(r"(?:\$|USD\s*)\s*[0-9][0-9,]*(?:\.[0-9]{1,2})?", re.IGNORECASE)
API_ADAPTER_KEYS = [
    "haloskins",
    "csfloat",
    "c5game",
    "dmarket",
    "marketcsgo",
    "waxpeer",
    "openskin_skinport",
    "openskin_buff163",
    "openskin_youpin",
    "openskin_steam",
]


@dataclass
class ProbeResult:
    source: str
    check_type: str
    status: str
    http_status: int | None = None
    response_length: int | None = None
    duration_ms: int | None = None
    item: str | None = None
    url: str | None = None
    final_url: str | None = None
    expected_item_found: bool | None = None
    price_found: bool | None = None
    block_detected: bool | None = None
    details: str | None = None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe marketplace/API reachability from local or cloud IPs without updating app data."
    )
    parser.add_argument("--items", type=int, default=3, help="Number of basket items to test.")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds.")
    parser.add_argument("--json", type=Path, default=None, help="Optional path for JSON report.")
    parser.add_argument("--markdown", type=Path, default=None, help="Optional path for Markdown report.")
    parser.add_argument("--no-api", action="store_true", help="Skip adapter-level API checks.")
    parser.add_argument("--no-pages", action="store_true", help="Skip raw page checks.")
    parser.add_argument("--fail-on-bad", action="store_true", help="Exit with code 1 when blocked/error/unusable checks exist.")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    items = load_probe_items(args.items)
    results: list[ProbeResult] = []

    if not args.no_api:
        results.extend(probe_api_adapters(items))
    if not args.no_pages:
        results.extend(probe_pages(items, args.timeout))

    print(render_markdown(results))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps([asdict(result) for result in results], indent=2), encoding="utf-8")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(render_markdown(results), encoding="utf-8")

    has_bad_result = any(result.status in {"blocked", "error", "unusable"} for result in results)
    return 1 if args.fail_on_bad and has_bad_result else 0


def load_probe_items(limit: int) -> list[BasketItem]:
    rows = load_basket_rows(ROOT / "data" / "basket.xlsx")
    selected = rows[: max(1, limit)]
    return [
        BasketItem(
            item_id=index + 1,
            market_hash_name=row["market_hash_name"],
            price_compare_url=row.get("price_compare_url"),
            priceempire_url=row.get("priceempire_url"),
            steamanalyst_url=row.get("steamanalyst_url"),
            marketplace_links=row.get("marketplace_links") or {},
        )
        for index, row in enumerate(selected)
    ]


def probe_api_adapters(items: list[BasketItem]) -> list[ProbeResult]:
    registry = build_adapter_registry()
    results: list[ProbeResult] = []
    for key in API_ADAPTER_KEYS:
        adapter = registry.get(key)
        if adapter is None:
            results.append(ProbeResult(source=key, check_type="api", status="skipped", details="Adapter not found."))
            continue
        if getattr(adapter, "requires_credentials", False) and not adapter.credentials_configured():
            results.append(
                ProbeResult(
                    source=adapter.name,
                    check_type="api",
                    status="skipped",
                    details="Required credentials are not configured in environment.",
                )
            )
            continue

        start = time.monotonic()
        try:
            price_results = adapter.fetch_prices(items)
            duration_ms = int((time.monotonic() - start) * 1000)
        except Exception as exc:
            results.append(
                ProbeResult(
                    source=adapter.name,
                    check_type="api",
                    status="error",
                    duration_ms=int((time.monotonic() - start) * 1000),
                    details=f"Adapter raised {type(exc).__name__}: {exc}",
                )
            )
            continue

        ok_count = sum(1 for result in price_results if result.fetch_status == "ok" and result.price is not None)
        error_count = sum(1 for result in price_results if result.fetch_status == "error")
        missing_count = sum(1 for result in price_results if result.fetch_status == "missing")
        status = "ok" if ok_count else "unusable"
        details = f"{ok_count} ok, {missing_count} missing, {error_count} error"
        if error_count and not ok_count:
            details = "; ".join(
                sorted({result.error_details or "unknown error" for result in price_results if result.fetch_status == "error"})
            )[:350]
        results.append(
            ProbeResult(
                source=adapter.name,
                check_type="api",
                status=status,
                duration_ms=duration_ms,
                details=details,
            )
        )
    return results


def probe_pages(items: list[BasketItem], timeout: float) -> list[ProbeResult]:
    results: list[ProbeResult] = []
    for item in items:
        if item.price_compare_url:
            results.append(fetch_page("CSGOSKINS direct", item.price_compare_url, item, timeout))
            results.append(fetch_page("CSGOSKINS reader", reader_url(item.price_compare_url), item, timeout))
        if item.priceempire_url:
            results.append(fetch_page("PriceEmpire", item.priceempire_url, item, timeout))
        if item.steamanalyst_url:
            results.append(fetch_page("SteamAnalyst", item.steamanalyst_url, item, timeout))

        for marketplace, url in sorted((item.marketplace_links or {}).items()):
            if marketplace in {"C5Game"}:
                continue
            results.append(fetch_page(f"Direct {marketplace}", url, item, timeout))
    return results


def fetch_page(source: str, url: str, item: BasketItem, timeout: float) -> ProbeResult:
    headers = {
        "User-Agent": os.getenv(
            "CLOUD_PROBE_USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    start = time.monotonic()
    try:
        response = requests.get(url, headers=headers, timeout=timeout)
        text = response.text or ""
        duration_ms = int((time.monotonic() - start) * 1000)
        block_detected = detect_block(text, response.status_code)
        price_found = bool(PRICE_PATTERN.search(text))
        expected_item_found = item.market_hash_name.lower() in text.lower()
        status = classify_page(response.status_code, block_detected, price_found)
        return ProbeResult(
            source=source,
            check_type="page",
            status=status,
            http_status=response.status_code,
            response_length=len(text),
            duration_ms=duration_ms,
            item=item.market_hash_name,
            url=url,
            final_url=response.url,
            expected_item_found=expected_item_found,
            price_found=price_found,
            block_detected=block_detected,
            details=page_details(text, response.status_code),
        )
    except Exception as exc:
        return ProbeResult(
            source=source,
            check_type="page",
            status="error",
            duration_ms=int((time.monotonic() - start) * 1000),
            item=item.market_hash_name,
            url=url,
            details=f"{type(exc).__name__}: {exc}",
        )


def classify_page(status_code: int, block_detected: bool, price_found: bool) -> str:
    if status_code in {401, 403, 407, 409, 418, 429, 451, 503} or block_detected:
        return "blocked"
    if 200 <= status_code < 300 and price_found:
        return "ok"
    if 200 <= status_code < 400:
        return "unusable"
    return "error"


def detect_block(text: str, status_code: int) -> bool:
    if status_code in {401, 403, 407, 409, 418, 429, 451, 503}:
        return True
    lowered = text[:120000].lower()
    return any(pattern in lowered for pattern in BLOCK_PATTERNS)


def page_details(text: str, status_code: int) -> str:
    if status_code in {401, 403, 407, 409, 418, 429, 451, 503}:
        return f"HTTP {status_code} is commonly a block/rate-limit/login response."
    lowered = text[:120000].lower()
    found = [pattern for pattern in BLOCK_PATTERNS if pattern in lowered]
    if found:
        return f"Block indicators found: {', '.join(found[:4])}"
    if not PRICE_PATTERN.search(text):
        return "No USD price-like text found in returned body."
    return "Reachable and price-like text found."


def reader_url(url: str) -> str:
    template = os.getenv("CSGOSKINS_READER_URL_TEMPLATE", "https://r.jina.ai/http://{url}")
    return template.format(url=url, url_encoded=quote(url, safe=""))


def render_markdown(results: Iterable[ProbeResult]) -> str:
    rows = list(results)
    lines = [
        "# Cloud IP Probe Report",
        "",
        "| source | type | status | http | ms | len | price | block | details |",
        "| --- | --- | --- | ---: | ---: | ---: | --- | --- | --- |",
    ]
    for result in rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    md(result.source),
                    md(result.check_type),
                    md(result.status),
                    "" if result.http_status is None else str(result.http_status),
                    "" if result.duration_ms is None else str(result.duration_ms),
                    "" if result.response_length is None else str(result.response_length),
                    bool_text(result.price_found),
                    bool_text(result.block_detected),
                    md(result.details or ""),
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def md(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")[:500]


def bool_text(value: bool | None) -> str:
    if value is None:
        return ""
    return "yes" if value else "no"


if __name__ == "__main__":
    raise SystemExit(main())
