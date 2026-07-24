from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import requests
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
ENDPOINT = "https://api.uuskins.com/api/vertex/commodity/query/openapi/sku/list"


def json_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def signing_text(payload: dict[str, object]) -> str:
    return "".join(f"{key}{json_value(payload[key])}" for key in sorted(payload) if payload[key] is not None)


def sign(payload: dict[str, object], private_key: str) -> str:
    openssl = os.getenv("OPENSSL_BIN") or shutil.which("openssl")
    if not openssl:
        candidate = Path(r"C:\Program Files\OpenVPN\bin\openssl.exe")
        if candidate.exists():
            openssl = str(candidate)
    if not openssl:
        raise RuntimeError("OpenSSL is not available. Set OPENSSL_BIN to openssl.exe.")

    key_file = tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False, encoding="ascii")
    try:
        key_file.write(private_key.strip() + "\n")
        key_file.close()
        completed = subprocess.run(
            [openssl, "dgst", "-sha256", "-sign", key_file.name],
            input=signing_text(payload).encode("utf-8"),
            capture_output=True,
            check=True,
        )
        return base64.b64encode(completed.stdout).decode("ascii")
    finally:
        Path(key_file.name).unlink(missing_ok=True)


def basket_names() -> list[str]:
    import sys

    sys.path.insert(0, str(ROOT))
    import db

    db.init_db()
    return [str(row["market_hash_name"]) for row in db.get_basket_items(active_only=True)]


def main() -> None:
    load_dotenv(ROOT / ".env")
    app_key = os.getenv("UUSKINS_APP_KEY", "").strip()
    private_key = os.getenv("UUSKINS_PRIVATE_KEY", "").replace("\\n", "\n").strip()
    if not app_key or not private_key:
        raise SystemExit("Set UUSKINS_APP_KEY and UUSKINS_PRIVATE_KEY before running this probe.")

    names = basket_names()
    returned: dict[str, list[dict]] = {}
    for start in range(0, len(names), 20):
        batch = names[start : start + 20]
        unsigned: dict[str, object] = {
            "appKey": app_key,
            "itemSize": 1,
            "marketHashNameList": batch,
            # The live endpoint requires these even though list-based queries
            # do not use pagination to choose the requested names.
            "marketHashNamePageIndex": 1,
            "marketHashNamePageSize": len(batch),
        }
        payload = {**unsigned, "sign": sign(unsigned, private_key)}
        response = requests.post(ENDPOINT, json=payload, timeout=30)
        response.raise_for_status()
        body = response.json()
        code = body.get("code")
        if code not in (0, 200, "0", "200"):
            message = body.get("message") or body.get("msg") or "Unknown API error"
            raise RuntimeError(f"UUSKINS returned code={code}: {message}")
        for group in (body.get("data") or {}).get("items") or []:
            returned[str(group.get("marketHashName"))] = list(group.get("skus") or [])

    available = 0
    for name in names:
        skus = returned.get(name) or []
        if skus:
            available += 1
            print(f"OK      {name} | ${float(skus[0]['price']):,.2f} | listings_returned={len(skus)}")
        else:
            print(f"MISSING {name}")
    print(f"\nSummary: available {available}/{len(names)}, missing {len(names) - available}/{len(names)}")


if __name__ == "__main__":
    main()
