import io
import pandas as pd

CANON_COLUMNS = [
    "date","category","subcategory","description","quantity","unit","geo","source_file"
]

def parse_csv(file_bytes: bytes, filename: str) -> pd.DataFrame:
    df = pd.read_csv(io.BytesIO(file_bytes))
    # Normalise headers
    df.columns = [c.strip().lower() for c in df.columns]
    # Minimal mapping heuristics
    mapping = {
        "amount":"quantity",
        "qty":"quantity",
        "country":"geo",
        "region":"geo",
    }
    df = df.rename(columns={k:v for k,v in mapping.items() if k in df.columns})

    for col in CANON_COLUMNS:
        if col not in df.columns:
            df[col] = None

    # Normalise string columns: missing cells become "" — never the string
    # "nan", which would defeat exact factor matching downstream.
    for col in ("category", "subcategory", "description", "unit", "geo"):
        df[col] = df[col].fillna("").astype(str).str.strip()
        df.loc[df[col].str.lower() == "nan", col] = ""

    # Ensure types
    df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce")
    # Normalise dates to zero-padded ISO (YYYY-MM-DD) with a FIXED per-row policy:
    # ISO formats first, then day-first (DD/MM/YYYY, documented UK/EU convention).
    # Never column-wide inference (pd.to_datetime on the whole column can parse the
    # SAME string month-first or day-first depending on sibling rows — a silent
    # date swap). Unparseable/missing -> "" (never the string "nan").
    df["date"] = df["date"].map(_normalise_date)
    df["source_file"] = filename
    return df[CANON_COLUMNS]


_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y")


def _normalise_date(value) -> str:
    """One row, one deterministic answer: ISO first, then day-first; else ''."""
    from datetime import datetime
    s = str(value).strip() if value is not None else ""
    if not s or s.lower() in ("nan", "nat", "none"):
        return ""
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""
