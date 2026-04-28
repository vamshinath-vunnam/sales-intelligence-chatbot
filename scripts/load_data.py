"""
load_data.py — One-time script to load sales_data.csv into sales.db (SQLite).
Run once before starting the app: python scripts/load_data.py
"""

import sqlite3
import pandas as pd
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
CSV_PATH = DATA_DIR / "sales_data.csv"
DB_PATH = DATA_DIR / "sales.db"


def load():
    df = pd.read_csv(CSV_PATH)

    # Validate expected columns
    expected = {"year", "month", "region", "sales_rep", "brand", "channel",
                "units_sold", "revenue_usd", "target_usd", "achieved_pct"}
    missing = expected - set(df.columns)
    if missing:
        raise ValueError(f"CSV missing columns: {missing}")

    conn = sqlite3.connect(DB_PATH)
    df.to_sql("sales", conn, if_exists="replace", index=False)
    conn.close()

    print(f"Loaded {len(df):,} rows into {DB_PATH}")
    print(f"Regions : {sorted(df['region'].unique())}")
    print(f"Brands  : {sorted(df['brand'].unique())}")
    print(f"Reps    : {sorted(df['sales_rep'].unique())}")
    print(f"Years   : {sorted(df['year'].unique())}")


if __name__ == "__main__":
    load()
