from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable

import requests

from .base import BasketItem, PriceResult


DEFAULT_ENDPOINT = "https://api.uuskins.com/api/vertex/commodity/query/openapi/sku/list"
MAX_NAMES_PER_REQUEST = 20


class UUSkinsAdapter:
    key = "uuskins"
    name = "UUSKINS"
    requires_credentials = True

    def credentials_configured(self) -> bool:
        return bool(os.getenv("UUSKINS_APP_KEY")) and bool(os.getenv("UUSKINS_PRIVATE_KEY"))

    def fetch_prices(self, items: Iterable[BasketItem]) -> list[PriceResult]:
        item_list = list(items)
        if not self.credentials_configured():
            return [_error(item, "UUSKINS_APP_KEY or UUSKINS_PRIVATE_KEY is not configured.") for item in item_list]

        try:
            groups = _fetch_groups(item_list)
        except Exception as exc:
            return [_error(item, f"UUSKINS API request failed: {exc}") for item in item_list]

        results: list[PriceResult] = []
        for item in item_list:
            skus = groups.get(item.market_hash_name) or []
            prices = [_float_or_none(sku.get("price")) for sku in skus if isinstance(sku, dict)]
            valid_prices = [price for price in prices if price is not None and price > 0]
            price = min(valid_prices) if valid_prices else None
            results.append(
                PriceResult(
                    marketplace=self.name,
                    market_hash_name=item.market_hash_name,
                    price=price,
                    currency="USD",
                    fetch_status="ok" if price is not None else "missing",
                    error_details=None if price is not None else "UUSKINS returned no exact listing price.",
                )
            )
        return results


def _fetch_groups(items: list[BasketItem]) -> dict[str, list[dict]]:
    app_key = os.environ["UUSKINS_APP_KEY"].strip()
    private_key = os.environ["UUSKINS_PRIVATE_KEY"].replace("\\n", "\n").strip()
    groups: dict[str, list[dict]] = {}

    for start in range(0, len(items), MAX_NAMES_PER_REQUEST):
        names = [item.market_hash_name for item in items[start : start + MAX_NAMES_PER_REQUEST]]
        unsigned: dict[str, object] = {
            "appKey": app_key,
            "itemSize": int(os.getenv("UUSKINS_ITEM_SIZE", "1")),
            "marketHashNameList": names,
            "marketHashNamePageIndex": 1,
            "marketHashNamePageSize": len(names),
        }
        payload = {**unsigned, "sign": _sign(unsigned, private_key)}
        response = requests.post(
            os.getenv("UUSKINS_API_URL", DEFAULT_ENDPOINT),
            json=payload,
            headers={"Accept": "application/json", "User-Agent": os.getenv("UUSKINS_USER_AGENT", "local-cs2-basket-tool/1.0")},
            timeout=float(os.getenv("UUSKINS_TIMEOUT_SECONDS", "30")),
        )
        response.raise_for_status()
        body = response.json()
        code = body.get("code") if isinstance(body, dict) else None
        if code not in (0, 200, "0", "200"):
            message = body.get("message") or body.get("msg") or "Unknown API error"
            raise RuntimeError(f"code={code}: {message}")
        data = body.get("data") if isinstance(body, dict) else None
        for group in data.get("items", []) if isinstance(data, dict) else []:
            if not isinstance(group, dict) or not group.get("marketHashName"):
                continue
            groups[str(group["marketHashName"])] = list(group.get("skus") or [])
    return groups


def _sign(payload: dict[str, object], private_key: str) -> str:
    openssl = os.getenv("OPENSSL_BIN") or shutil.which("openssl")
    if not openssl:
        windows_candidate = Path(r"C:\Program Files\OpenVPN\bin\openssl.exe")
        if windows_candidate.exists():
            openssl = str(windows_candidate)
    if not openssl:
        raise RuntimeError("OpenSSL is unavailable; set OPENSSL_BIN to an OpenSSL executable.")

    signing_text = "".join(
        f"{key}{json.dumps(payload[key], ensure_ascii=False, separators=(',', ':'))}"
        for key in sorted(payload)
        if payload[key] is not None
    )
    key_file = tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False, encoding="ascii")
    try:
        key_file.write(private_key + "\n")
        key_file.close()
        completed = subprocess.run(
            [openssl, "dgst", "-sha256", "-sign", key_file.name],
            input=signing_text.encode("utf-8"),
            capture_output=True,
            check=True,
        )
        return base64.b64encode(completed.stdout).decode("ascii")
    finally:
        Path(key_file.name).unlink(missing_ok=True)


def _float_or_none(value) -> float | None:
    try:
        return float(value) if value is not None and str(value).strip() else None
    except (TypeError, ValueError):
        return None


def _error(item: BasketItem, message: str) -> PriceResult:
    return PriceResult(
        marketplace="UUSKINS",
        market_hash_name=item.market_hash_name,
        price=None,
        currency="USD",
        fetch_status="error",
        error_details=message,
    )
