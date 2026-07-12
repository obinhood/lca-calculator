"""Ingestion QA: flag problems loudly, never silently drop or guess.

Earlier versions dropped non-positive rows and guessed missing units from the
category (gas billed in kWh vs m3 makes that a silent order-of-magnitude bet).
Now every row is KEPT and surfaced as an issue; the fail-closed calc engine
routes bad quantities/units into visible data/unit-error buckets instead.
"""
from typing import List, Tuple
import pandas as pd


def check_records(df: pd.DataFrame) -> Tuple[pd.DataFrame, List[str]]:
    issues = []

    def _rows(mask):
        r = df[mask].index.tolist()
        return f"{r[:10]}{'...' if len(r) > 10 else ''}"

    negative = df["quantity"].notna() & (df["quantity"] < 0)
    if negative.any():
        issues.append(f"Negative quantities in rows: {_rows(negative)} "
                      f"(kept; excluded from totals as data errors)")

    zero = df["quantity"].notna() & (df["quantity"] == 0)
    if zero.any():
        issues.append(f"Zero quantities in rows: {_rows(zero)} "
                      f"(kept; computed as zero emissions and counted as mapped — "
                      f"verify these are real zero-consumption records)")

    missing_qty = df["quantity"].isna()
    if missing_qty.any():
        issues.append(f"Missing quantities in rows: {_rows(missing_qty)} "
                      f"(kept; excluded from totals as data errors)")

    missing_date = df["date"].isna() | (df["date"].astype(str).str.strip() == "")
    if missing_date.any():
        issues.append(f"Missing/unparseable dates in rows: {_rows(missing_date)} "
                      f"(kept; excluded from period-scoped runs as data errors)")

    missing_unit = df["unit"].isna() | (df["unit"].astype(str).str.strip() == "")
    if missing_unit.any():
        issues.append(f"Missing units in rows: {_rows(missing_unit)} "
                      f"(kept; units are never guessed — these rows will fail "
                      f"unit conversion until corrected)")

    dup = df.duplicated(subset=["date", "category", "subcategory", "quantity", "unit",
                                "geo", "description"],
                        keep=False) & df["quantity"].notna()
    if dup.any():
        issues.append(f"Possible duplicate rows (same date/category/subcategory/"
                      f"quantity/unit): {_rows(dup)} (kept; review before reporting)")

    return df, issues
