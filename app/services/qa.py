from typing import List, Tuple
import pandas as pd

def check_records(df: pd.DataFrame) -> Tuple[pd.DataFrame, List[str]]:
    issues = []
    # Basic checks
    if (df["quantity"] <= 0).any():
        bad = df[df["quantity"] <= 0].index.tolist()
        issues.append(f"Non-positive quantities in rows: {bad[:10]}{'...' if len(bad)>10 else ''}")
        df = df[df["quantity"] > 0]

    missing_date = df["date"].isna() | (df["date"].astype(str).str.strip() == "")
    if missing_date.any():
        rows = df[missing_date].index.tolist()
        issues.append(f"Missing/unparseable dates in rows: {rows[:10]}{'...' if len(rows)>10 else ''} "
                      f"(kept; excluded from period-scoped runs as data errors)")

    missing_unit = df["unit"].isna() | (df["unit"].astype(str).str.strip() == "")
    if missing_unit.any():
        rows = df[missing_unit].index.tolist()
        issues.append(f"Missing units in rows: {rows[:10]}{'...' if len(rows)>10 else ''}")
        df.loc[missing_unit, "unit"] = df.loc[missing_unit, "category"].map({
            "electricity":"kWh",
            "gas":"kWh",
            "diesel":"L",
            "flight":"pkm",
            "train":"pkm",
            "car":"km",
            "waste":"kg"
        }).fillna("unit")

    return df, issues
