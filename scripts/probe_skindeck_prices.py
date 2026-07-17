from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db  # noqa: E402


API_URL = "https://api.skindeck.com/secure/market"
GAME_ID = "730"
STAR = "\u2605"


@dataclass
class ItemProbe:
    name: str
    status: str
    exact_matches: int = 0
    result_count: int = 0
    search_name: str = ""
    best_offer_price: float | None = None
    best_market_price: float | None = None
    candidates: tuple[str, ...] = ()
    message: str = ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe Skindeck /secure/market prices for basket items.")
    parser.add_argument("--limit", type=int, default=0, help="Number of active basket items to test. Default 0 means all.")
    parser.add_argument("--item", action="append", default=[], help="Specific market_hash_name to test. Can be repeated.")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds.")
    parser.add_argument("--per-page", type=int, default=10, help="Skindeck perPage value.")
    args = parser.parse_args()

    load_env()
    api_key = os.getenv("SKINDECK_API_KEY", "").strip()
    api_secret = os.getenv("SKINDECK_API_SECRET", "").strip()
    if not api_key:
        print("Missing SKINDECK_API_KEY. Add it to .env or set it in the shell environment.")
        return 1
    if api_secret:
        print("SKINDECK_API_SECRET is configured but is not used by GET /secure/market.")

    names = args.item or load_basket_names(args.limit)
    if not names:
        print("No active basket items found.")
        return 1

    probes: list[ItemProbe] = []
    fatal_errors = 0
    for name in names:
        probe = probe_item(name, api_key, timeout=args.timeout, per_page=args.per_page)
        probes.append(probe)
        print(format_probe(probe))
        if probe.status in {"auth_error", "network_error", "http_error"}:
            fatal_errors += 1
            if probe.status == "auth_error":
                break

    available = sum(1 for probe in probes if probe.status == "ok")
    missing = sum(1 for probe in probes if probe.status == "missing")
    errors = sum(1 for probe in probes if probe.status.endswith("_error"))
    print()
    print(f"Summary: available {available}/{len(probes)}, missing {missing}, errors {errors}")

    if probes and fatal_errors == len(probes):
        return 1
    return 1 if any(probe.status == "auth_error" for probe in probes) else 0


def load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(ROOT / ".env")


def load_basket_names(limit: int) -> list[str]:
    db.init_db()
    items = db.get_adapter_items()
    selected = items if limit <= 0 else items[:limit]
    return [item.market_hash_name for item in selected]


def probe_item(name: str, api_key: str, *, timeout: float, per_page: int) -> ItemProbe:
    probe = request_item(name, name, api_key, timeout=timeout, per_page=per_page)
    if probe.status != "missing" or not has_leading_star(name):
        return probe

    stripped_name = strip_leading_star(name)
    stripped_probe = request_item(name, stripped_name, api_key, timeout=timeout, per_page=per_page)
    if stripped_probe.status == "ok":
        stripped_probe.message = "Matched after searching without leading star; Skindeck may omit star in names."
        return stripped_probe
    if stripped_probe.status == "missing":
        original_count = probe.result_count
        probe.result_count += stripped_probe.result_count
        probe.candidates = unique_names((*probe.candidates, *stripped_probe.candidates))
        probe.message = (
            "No exact match with original star name or stripped star name. "
            f"Original results={original_count}, stripped results={stripped_probe.result_count}."
        )
    return probe


def request_item(name: str, search_name: str, api_key: str, *, timeout: float, per_page: int) -> ItemProbe:
    url = f"{API_URL}?{urlencode({'search': search_name, 'game': GAME_ID, 'perPage': per_page})}"
    request = Request(
        url,
        headers={
            "api-key": api_key,
            "Accept": "application/json",
            "User-Agent": "cs2-basket-tool/1.0",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            payload = json.loads(body)
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        status = "auth_error" if exc.code in {401, 403} else "http_error"
        return ItemProbe(name=name, status=status, search_name=search_name, message=f"HTTP {exc.code}: {trim(body)}")
    except URLError as exc:
        reason = str(exc.reason)
        if "WinError 10013" in reason or "EACCES" in reason:
            reason += " (network blocked by current Codex sandbox; run this script from normal PowerShell)"
        return ItemProbe(name=name, status="network_error", search_name=search_name, message=reason)
    except TimeoutError as exc:
        return ItemProbe(name=name, status="network_error", search_name=search_name, message=f"Timeout: {exc}")
    except json.JSONDecodeError as exc:
        return ItemProbe(name=name, status="http_error", search_name=search_name, message=f"Invalid JSON: {exc}")

    if not payload.get("success", False):
        return ItemProbe(
            name=name,
            status="http_error",
            search_name=search_name,
            message=f"API success=false: {trim(json.dumps(payload))}",
        )

    raw_items = payload.get("items") or []
    if not isinstance(raw_items, list):
        return ItemProbe(name=name, status="http_error", search_name=search_name, message="Response items field is not a list.")

    exact = [item for item in raw_items if names_match(str(item.get("market_hash_name", "")), name, search_name)]
    if not exact:
        return ItemProbe(
            name=name,
            status="missing",
            result_count=len(raw_items),
            search_name=search_name,
            candidates=candidate_names(raw_items),
            message="No exact market_hash_name match in returned items.",
        )

    best_item = min(exact, key=lambda item: price_or_inf(read_offer_price(item)))
    return ItemProbe(
        name=name,
        status="ok",
        exact_matches=len(exact),
        result_count=len(raw_items),
        search_name=search_name,
        best_offer_price=read_offer_price(best_item),
        best_market_price=read_float(best_item.get("market_price")),
    )


def candidate_names(items: list[dict[str, Any]]) -> tuple[str, ...]:
    return unique_names(str(item.get("market_hash_name", "")).strip() for item in items)


def names_match(candidate: str, original_name: str, search_name: str) -> bool:
    candidate_key = normalize_name(candidate)
    return candidate_key in {normalize_name(original_name), normalize_name(search_name)}


def normalize_name(value: str) -> str:
    return " ".join(value.casefold().replace(STAR, "").split())


def has_leading_star(value: str) -> bool:
    return value.lstrip().startswith(STAR)


def strip_leading_star(value: str) -> str:
    return value.lstrip().removeprefix(STAR).strip()


def unique_names(values: Iterable[str]) -> tuple[str, ...]:
    seen = set()
    names = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        names.append(value)
    return tuple(names)


def read_offer_price(item: dict[str, Any]) -> float | None:
    offer = item.get("offer")
    if isinstance(offer, dict):
        return read_float(offer.get("price"))
    return None


def read_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def price_or_inf(value: float | None) -> float:
    return value if value is not None else float("inf")


def format_probe(probe: ItemProbe) -> str:
    if probe.status == "ok":
        offer = format_price(probe.best_offer_price)
        market = format_price(probe.best_market_price)
        search = f" search={probe.search_name}" if probe.search_name and probe.search_name != probe.name else ""
        note = f" | {probe.message}" if probe.message else ""
        return (
            f"OK      {probe.name} | results={probe.result_count} exact={probe.exact_matches} "
            f"best_offer={offer} market_price={market}{search}{note}"
        )
    if probe.status == "missing":
        candidates = "; ".join(probe.candidates[:8])
        more = f"; +{len(probe.candidates) - 8} more" if len(probe.candidates) > 8 else ""
        candidate_note = f" | candidates: {candidates}{more}" if candidates else ""
        return f"MISSING {probe.name} | results={probe.result_count} | {probe.message}{candidate_note}"
    return f"ERROR   {probe.name} | {probe.status} | {probe.message}"


def format_price(value: float | None) -> str:
    return "n/a" if value is None else f"${value:,.2f}"


def trim(value: str, limit: int = 500) -> str:
    value = " ".join(value.split())
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


if __name__ == "__main__":
    raise SystemExit(main())
