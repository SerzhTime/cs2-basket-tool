from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile
from xml.etree import ElementTree as ET


SPECIAL_LINK_COLUMNS = {
    "CSGOSKINS links",
    "PriceEmpire links",
    "SteamAnalyst links",
}


def load_basket_rows(path: Path) -> list[dict]:
    try:
        return _load_with_pandas(path)
    except Exception:
        return _load_with_stdlib(path)


def _load_with_pandas(path: Path) -> list[dict]:
    import pandas as pd

    df = pd.read_excel(path)
    if "market_hash_name" not in df.columns:
        raise ValueError("Excel file must contain a market_hash_name column.")

    rows = []
    for _, row in df.iterrows():
        name = str(row.get("market_hash_name", "")).strip()
        if not name or name.lower() == "nan":
            continue
        rows.append(
            {
                "rank": _maybe_int(row.get("Rank")),
                "market_hash_name": name,
                "source_amount": _maybe_float(_first_present(row, ["出售金额", "amount", "price"])),
                "price_compare_url": _clean_text(_first_present(row, ["CSGOSKINS links", "price_compare_url", "url"])),
                "priceempire_url": _clean_text(_first_present(row, ["PriceEmpire links", "priceempire_url"])),
                "steamanalyst_url": _clean_text(_first_present(row, ["SteamAnalyst links", "steamanalyst_url"])),
                "marketplace_links": _marketplace_links_from_pandas_row(row),
            }
        )
    return rows


def _load_with_stdlib(path: Path) -> list[dict]:
    ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with ZipFile(path) as zf:
        workbook = ET.fromstring(zf.read("xl/workbook.xml"))
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        relmap = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels}
        sheet = workbook.find("x:sheets/x:sheet", ns)
        if sheet is None:
            return []
        rel_id = sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]
        target = relmap[rel_id].lstrip("/")
        if not target.startswith("xl/"):
            target = f"xl/{target}"

        xml = ET.fromstring(zf.read(target))
        parsed_rows = []
        for row in xml.findall("x:sheetData/x:row", ns):
            parsed_rows.append([_cell_value(cell, ns) for cell in row.findall("x:c", ns)])

    if not parsed_rows:
        return []
    headers = parsed_rows[0]
    name_idx = headers.index("market_hash_name")
    rank_idx = headers.index("Rank") if "Rank" in headers else None
    amount_idx = 2 if len(headers) > 2 else None
    url_idx = _first_header_index(headers, ["CSGOSKINS links", "price_compare_url", "url"])
    priceempire_idx = _first_header_index(headers, ["PriceEmpire links", "priceempire_url"])
    steamanalyst_idx = _first_header_index(headers, ["SteamAnalyst links", "steamanalyst_url"])
    marketplace_link_indices = _marketplace_link_indices(headers)

    rows = []
    for raw in parsed_rows[1:]:
        name = raw[name_idx].strip() if len(raw) > name_idx else ""
        if not name:
            continue
        rows.append(
            {
                "rank": _maybe_int(raw[rank_idx]) if rank_idx is not None and len(raw) > rank_idx else None,
                "market_hash_name": name,
                "source_amount": _maybe_float(raw[amount_idx]) if amount_idx is not None and len(raw) > amount_idx else None,
                "price_compare_url": _clean_text(raw[url_idx]) if url_idx is not None and len(raw) > url_idx else None,
                "priceempire_url": _clean_text(raw[priceempire_idx]) if priceempire_idx is not None and len(raw) > priceempire_idx else None,
                "steamanalyst_url": _clean_text(raw[steamanalyst_idx]) if steamanalyst_idx is not None and len(raw) > steamanalyst_idx else None,
                "marketplace_links": {
                    marketplace: url
                    for marketplace, idx in marketplace_link_indices.items()
                    if len(raw) > idx and (url := _clean_text(raw[idx]))
                },
            }
        )
    return rows


def _marketplace_links_from_pandas_row(row) -> dict[str, str]:
    links: dict[str, str] = {}
    for column in row.index:
        if not isinstance(column, str) or not column.endswith(" link") or column in SPECIAL_LINK_COLUMNS:
            continue
        url = _clean_text(row.get(column))
        if url:
            links[column.removesuffix(" link")] = url
    return links


def _marketplace_link_indices(headers: list[str]) -> dict[str, int]:
    indices: dict[str, int] = {}
    for index, header in enumerate(headers):
        if isinstance(header, str) and header.endswith(" link") and header not in SPECIAL_LINK_COLUMNS:
            indices[header.removesuffix(" link")] = index
    return indices


def _cell_value(cell: ET.Element, ns: dict[str, str]) -> str:
    value = cell.find("x:v", ns)
    if value is not None:
        return value.text or ""
    inline_text = cell.find("x:is/x:t", ns)
    if inline_text is not None:
        return inline_text.text or ""
    return ""


def _first_present(row, columns: list[str]):
    for column in columns:
        if column in row:
            return row[column]
    return None


def _first_header_index(headers: list[str], columns: list[str]) -> int | None:
    for column in columns:
        if column in headers:
            return headers.index(column)
    return None


def _clean_text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    return text


def _maybe_int(value) -> int | None:
    try:
        if value is None or str(value).lower() == "nan" or str(value).strip() == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _maybe_float(value) -> float | None:
    try:
        if value is None or str(value).lower() == "nan" or str(value).strip() == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
