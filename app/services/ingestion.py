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

    # Ensure types
    df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce")
    df["date"] = df["date"].astype(str)
    df["source_file"] = filename
    return df[CANON_COLUMNS]
