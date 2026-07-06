from __future__ import annotations

import pandas as pd

BASELINE_MARKETPLACE = "HaloSkins"
FALLBACK_MARKER_PREFIX = "__halo_fallback__"
HEADER_REPEAT_MARKER = "__header_repeat__"
PREFERRED_FALLBACK_MARKETS = {
    "Buff163": "C5Game",
    "YouPin": "C5Game",
}


def build_comparison_table(items, points, marketplace_order: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    item_rows = [
        {
            "item_id": int(item["item_id"]),
            "Name": item["market_hash_name"],
            "active": bool(item["active"]),
            "Multiplier": min(1000, max(1, int(item["multiplier"] or 1))),
        }
        for item in items
        if int(item["active"]) == 1
    ]
    price_map = {
        (point["market_hash_name"], point["marketplace"]): point
        for point in points
        if point["fetch_status"] == "ok" and point["normalized_price"] is not None
    }

    rows = []
    totals = {marketplace: 0.0 for marketplace in marketplace_order}
    counts = {marketplace: 0 for marketplace in marketplace_order}
    fallback_counts = {marketplace: 0 for marketplace in marketplace_order}
    total_items = len(item_rows)

    for item in item_rows:
        name = item["Name"]
        multiplier = item["Multiplier"]
        row = {"Name": name}
        baseline = _point_price(price_map.get((name, BASELINE_MARKETPLACE)))
        baseline_total = baseline * multiplier if baseline is not None else None
        row["HaloSkins single"] = baseline
        row["Multiplier"] = multiplier
        row["HaloSkins total"] = baseline_total
        if baseline is not None:
            totals[BASELINE_MARKETPLACE] += baseline_total
            counts[BASELINE_MARKETPLACE] += 1

        for marketplace in marketplace_order:
            if marketplace == BASELINE_MARKETPLACE:
                continue
            price = _point_price(price_map.get((name, marketplace)))
            fallback_price = _fallback_price(name, marketplace, price_map, baseline)
            used_fallback = price is None and fallback_price is not None
            market_total = fallback_price * multiplier if used_fallback else price * multiplier if price is not None else None
            row[marketplace] = market_total
            row[f"{marketplace} diff"] = _diff_percent(market_total, baseline_total)
            row[_fallback_marker(marketplace)] = used_fallback
            row[_fallback_marker(f"{marketplace} diff")] = used_fallback
            if price is not None:
                totals[marketplace] += market_total or 0
                counts[marketplace] += 1
            elif used_fallback:
                totals[marketplace] += market_total or 0
                fallback_counts[marketplace] += 1
        rows.append(row)

    sum_row = {"Name": "Basket total", "Multiplier": None}
    baseline_total = totals.get(BASELINE_MARKETPLACE) if counts.get(BASELINE_MARKETPLACE) else None
    sum_row["HaloSkins single"] = sum(
        _point_price(price_map.get((item["Name"], BASELINE_MARKETPLACE))) or 0
        for item in item_rows
    )
    sum_row["HaloSkins total"] = baseline_total
    for marketplace in marketplace_order:
        if marketplace == BASELINE_MARKETPLACE:
            continue
        market_total = totals[marketplace] if counts[marketplace] or fallback_counts[marketplace] else None
        sum_row[marketplace] = market_total
        sum_row[f"{marketplace} diff"] = _diff_percent(market_total, baseline_total)
        sum_row[_fallback_marker(marketplace)] = False
        sum_row[_fallback_marker(f"{marketplace} diff")] = False
    rows.append(sum_row)

    column_order = _comparison_column_order(marketplace_order, sum_row, baseline_total)
    header_row = {col: col for col in column_order}
    header_row[HEADER_REPEAT_MARKER] = True
    for marketplace in marketplace_order:
        if marketplace == BASELINE_MARKETPLACE:
            continue
        header_row[_fallback_marker(marketplace)] = False
        header_row[_fallback_marker(f"{marketplace} diff")] = False
    rows.append(header_row)

    coverage = pd.DataFrame(
        [
            {
                "Marketplace": marketplace,
                "Coverage": f"{counts[marketplace]}/{total_items}",
                "Available items": counts[marketplace],
                "Fallback items": fallback_counts[marketplace],
                "Basket items": total_items,
                "Total cost": totals[marketplace] if counts[marketplace] or fallback_counts[marketplace] else None,
            }
            for marketplace in marketplace_order
        ]
    )
    marker_cols = [col for col in rows[-1] if col.startswith(FALLBACK_MARKER_PREFIX)] + [HEADER_REPEAT_MARKER]
    ordered_cols = column_order + [col for col in marker_cols if col in rows[-1]]
    return pd.DataFrame(rows).reindex(columns=ordered_cols), coverage


def split_comparison_fixed_rows(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    fixed_mask = df["Name"].eq("Basket total") if "Name" in df.columns else pd.Series(False, index=df.index)
    if HEADER_REPEAT_MARKER in df.columns:
        fixed_mask = fixed_mask | df[HEADER_REPEAT_MARKER].fillna(False).astype(bool)
    return df.loc[~fixed_mask].copy(), df.loc[fixed_mask].copy()


def _point_price(point) -> float | None:
    if point is None:
        return None
    return float(point["normalized_price"])


def _fallback_price(
    market_hash_name: str,
    marketplace: str,
    price_map: dict,
    baseline_price: float | None,
) -> float | None:
    preferred_marketplace = PREFERRED_FALLBACK_MARKETS.get(marketplace)
    if preferred_marketplace:
        preferred_price = _point_price(price_map.get((market_hash_name, preferred_marketplace)))
        if preferred_price is not None:
            return preferred_price
    return baseline_price


def _diff_percent(price: float | None, baseline: float | None) -> float | None:
    if price is None or baseline is None or baseline == 0:
        return None
    return (price / baseline - 1.0) * 100.0


def _comparison_column_order(
    marketplace_order: list[str],
    sum_row: dict,
    baseline_total: float | None,
) -> list[str]:
    marketplace_pairs = []
    for marketplace in marketplace_order:
        if marketplace == BASELINE_MARKETPLACE:
            continue
        diff = _diff_percent(sum_row.get(marketplace), baseline_total)
        marketplace_pairs.append((marketplace, diff if diff is not None else float("inf")))

    cheaper = sorted((pair for pair in marketplace_pairs if pair[1] < 0), key=lambda pair: pair[1])
    not_cheaper = sorted((pair for pair in marketplace_pairs if pair[1] >= 0), key=lambda pair: pair[1])

    columns = ["Name", "HaloSkins single", "Multiplier"]
    for marketplace, _ in cheaper:
        columns.extend([marketplace, f"{marketplace} diff"])
    columns.append("HaloSkins total")
    for marketplace, _ in not_cheaper:
        columns.extend([marketplace, f"{marketplace} diff"])
    return columns


def _fallback_marker(column: str) -> str:
    return f"{FALLBACK_MARKER_PREFIX}{column}"
