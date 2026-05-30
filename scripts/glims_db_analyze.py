import argparse
import warnings
warnings.filterwarnings("ignore")

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter
import os
import sys

# ── CLI ────────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="Analyze GLIMS glims_polygons.dbf")
parser.add_argument("--dbf", default="glims_polygons.dbf",
                    help="Path to glims_polygons.dbf (default: ./glims_polygons.dbf)")
parser.add_argument("--out", default="glims_analysis",
                    help="Output folder for plots and CSVs (default: ./glims_analysis)")
parser.add_argument("--sample", type=int, default=None,
                    help="Load only first N rows for quick testing on large file")
args = parser.parse_args()

os.makedirs(args.out, exist_ok=True)

# ── LOAD DBF ───────────────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print("  GLIMS DBF Analysis")
print(f"{'='*60}")
print(f"\n[1/7] Loading: {args.dbf}")

try:
    from simpledbf import Dbf5
except ImportError:
    sys.exit("ERROR: simpledbf not installed. Run: pip install simpledbf")

# DBF files from GLIMS/NSIDC are often Latin-1 or Windows-1252, not UTF-8.
# Try encodings in order until one works.
ENCODINGS = ["latin-1", "windows-1252", "utf-8", "utf-8-sig", "iso-8859-1"]
df = None
for enc in ENCODINGS:
    try:
        dbf = Dbf5(args.dbf, codec=enc)
        df = dbf.to_dataframe()
        print(f"      Encoding: {enc}  ✓")
        break
    except Exception:
        print(f"      Encoding: {enc}  ✗ — trying next...")

if df is None:
    sys.exit("ERROR: Could not decode DBF with any known encoding. "
             "Try opening in QGIS and re-saving as UTF-8.")

if args.sample:
    df = df.iloc[:args.sample]
    print(f"      Loaded sample of {len(df):,} rows")
else:
    print(f"      Loaded {len(df):,} rows, {len(df.columns)} columns")


# ── BASIC STRUCTURE ────────────────────────────────────────────────────────────
print(f"\n[2/7] Basic Structure")
print(f"      Rows    : {len(df):,}")
print(f"      Columns : {len(df.columns)}")
print(f"\n      Column names, dtypes, and null %:")
for col, dtype in df.dtypes.items():
    null_pct = df[col].isna().mean() * 100
    print(f"        {col:<20} {str(dtype):<12}  nulls: {null_pct:.1f}%")

# Strip whitespace from all string columns (DBF pads strings)
str_cols = df.select_dtypes(include="object").columns
df[str_cols] = df[str_cols].apply(lambda c: c.str.strip())

