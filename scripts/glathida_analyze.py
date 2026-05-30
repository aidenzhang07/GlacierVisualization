"""
GlaThiDa TTT — Multi-Survey Glacier Filter
Finds glaciers with thickness measurements across >= N distinct years,
where each qualifying year is separated by at least 1 full calendar year
from the previous one, and has >= min_points thickness measurements.

Usage:
    python glathida_multiyear.py --csv TTT.csv
    python glathida_multiyear.py --csv TTT.csv --min_surveys 3 --min_points 50
    python glathida_multiyear.py --csv TTT.csv --min_surveys 2 --min_points 1
"""

import argparse
import pandas as pd
import numpy as np
import os
import sys
import time

# ── CLI ────────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(
    description="Filter GlaThiDa TTT to glaciers with thickness data across multiple years",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=__doc__
)
parser.add_argument("--csv",         default="TTT.csv",
                    help="Path to GlaThiDa TTT.csv (default: ./TTT.csv)")
parser.add_argument("--min_surveys", type=int, default=2,
                    help="Minimum number of qualifying years (default: 2)")
parser.add_argument("--min_points",  type=int, default=10,
                    help="Min thickness points per year to count it (default: 10)")
parser.add_argument("--out",         default="glathida_filtered",
                    help="Output folder (default: ./glathida_filtered)")
args = parser.parse_args()

os.makedirs(args.out, exist_ok=True)
SEP = "=" * 64
t0  = time.time()

print(f"\n{SEP}")
print("  GlaThiDa TTT — Multi-Year Thickness Filter")
print(SEP)
print(f"\n  Min qualifying years  : {args.min_surveys}")
print(f"  Min points/year       : {args.min_points}")
print(f"  Min gap between years : 1 year")

# ── LOAD ───────────────────────────────────────────────────────────────────────
print(f"\n[1/5] Loading {args.csv} ...")

if not os.path.exists(args.csv):
    sys.exit(f"ERROR: File not found: {args.csv}\n"
             "       Download TTT.csv from: https://doi.org/10.5904/wgms-glathida-2020-10")

try:
    df = pd.read_csv(args.csv, sep="\t", dtype=str, low_memory=False)
    if df.shape[1] < 5:
        df = pd.read_csv(args.csv, sep=",", dtype=str, low_memory=False)
except Exception as e:
    sys.exit(f"ERROR loading CSV: {e}")

df.columns = df.columns.str.strip().str.upper()
print(f"      Loaded   : {len(df):,} rows  |  {df.shape[1]} columns")
print(f"      Columns  : {list(df.columns)}")

# ── CAST TYPES ─────────────────────────────────────────────────────────────────
print(f"\n[2/5] Parsing and cleaning columns ...")

def to_num(col):
    return pd.to_numeric(col, errors="coerce")

df["POINT_LAT"] = to_num(df.get("POINT_LAT"))
df["POINT_LON"] = to_num(df.get("POINT_LON"))
df["THICKNESS"] = to_num(df.get("THICKNESS"))
df["ELEVATION"] = to_num(df.get("ELEVATION"))

# Handle GlaThiDa_ID column name variants
id_candidates = ["GLATHIDA_ID", "GlaThiDa_ID", "GLATHIDA_ID".upper()]
ID_COL = None
for c in df.columns:
    if c.upper() in [x.upper() for x in id_candidates]:
        ID_COL = c
        break
if ID_COL is None:
    sys.exit("ERROR: Cannot find GlaThiDa ID column. "
             f"Columns present: {list(df.columns)}")
df["GLATHIDA_ID"] = df[ID_COL].astype(str).str.strip()
ID_COL = "GLATHIDA_ID"

NAME_COL = "GLACIER_NAME" if "GLACIER_NAME" in df.columns else None

# ── ROBUST DATE PARSING ────────────────────────────────────────────────────────
# GlaThiDa dates arrive as YYYYMMDD but may be:
#   "20130222"   (ideal)
#   "20130222.0" (pandas read as float → str)
#   "2013-02-22" (ISO with hyphens)
#   "201302"     (partial, year+month only)
#   "2013"       (year only)

def clean_date_str(raw):
    """
    Normalise any date variant to an 8-char YYYYMMDD string.
    Returns None if unparseable.
    """
    s = str(raw).strip()
    if s in ("nan", "None", "NaT", ""):
        return None
    s = s.split(".")[0]          # "20130222.0" → "20130222"
    s = s.replace("-", "")       # "2013-02-22" → "20130222"
    s = s.replace(" ", "")
    s = s[:8]                    # truncate anything longer
    if not s.isdigit() or len(s) < 4:
        return None
    s = s.ljust(8, "0")          # "2013" → "20130000", "201302" → "20130200"
    # Fix impossible day/month zeroes for parsing: pad to "01"
    year  = s[:4]
    month = s[4:6] if s[4:6] != "00" else "01"
    day   = s[6:8] if s[6:8] != "00" else "01"
    return f"{year}{month}{day}"

def extract_year(raw):
    """Return integer year from any raw date value, or None."""
    s = clean_date_str(raw)
    if s is None:
        return None
    try:
        return int(pd.to_datetime(s, format="%Y%m%d").year)
    except Exception:
        return None

df["SURVEY_YEAR"] = df["SURVEY_DATE"].apply(extract_year)

valid_years  = df["SURVEY_YEAR"].notna().sum()
valid_thick  = df["THICKNESS"].notna().sum()
valid_coords = (df["POINT_LAT"].notna() & df["POINT_LON"].notna()).sum()

print(f"      Valid survey years  : {valid_years:,} / {len(df):,}")
print(f"      Valid thickness pts : {valid_thick:,} / {len(df):,}")
print(f"      Valid coordinates   : {valid_coords:,} / {len(df):,}")

# ── YEAR-LEVEL SURVEY COUNTS ────────────────────────────────────────────────────
print(f"\n[3/5] Counting thickness points per glacier per year ...")

# Work only with rows that have both a year and a valid thickness
working = df[df["SURVEY_YEAR"].notna() & df["THICKNESS"].notna()].copy()
working["SURVEY_YEAR"] = working["SURVEY_YEAR"].astype(int)

# Count points per glacier per year
pts_per_year = (
    working.groupby([ID_COL, "SURVEY_YEAR"])
    .size()
    .rename("n_points")
    .reset_index()
)

# Keep only years that meet the minimum points threshold
pts_per_year = pts_per_year[pts_per_year["n_points"] >= args.min_points].copy()

# ── ENFORCE MINIMUM 1-YEAR GAP ─────────────────────────────────────────────────
print(f"\n[4/5] Enforcing >= 1 year gap between consecutive surveys ...")

def select_qualifying_years(year_series, min_gap=1):
    """
    Given a sorted list of years that each have enough points,
    greedily select years such that each selected year is at least
    min_gap year(s) after the previously selected one.
    Returns list of selected years.
    """
    years = sorted(year_series.tolist())
    selected = []
    for y in years:
        if not selected or (y - selected[-1]) >= min_gap:
            selected.append(y)
    return selected

# Apply gap filter per glacier
results = []
for gid, grp in pts_per_year.groupby(ID_COL):
    selected = select_qualifying_years(grp["SURVEY_YEAR"], min_gap=1)
    if len(selected) >= args.min_surveys:
        # Retrieve point counts for the selected years
        sel_grp = grp[grp["SURVEY_YEAR"].isin(selected)]
        results.append({
            ID_COL:           gid,
            "n_surveys":      len(selected),
            "qualifying_years": selected,
            "year_span":      selected[-1] - selected[0],
            "total_points":   int(sel_grp["n_points"].sum()),
            "min_pts_year":   int(sel_grp["n_points"].min()),
            "max_pts_year":   int(sel_grp["n_points"].max()),
        })

qualifying = pd.DataFrame(results)

print(f"      Total unique glaciers in TTT    : {df[ID_COL].nunique():,}")
print(f"      Years with >= {args.min_points} pts (before gap filter) : "
      f"{pts_per_year[ID_COL].nunique():,} glaciers")
print(f"      Qualifying after gap filter     : {len(qualifying):,} glaciers")

if len(qualifying) == 0:
    sys.exit("No glaciers qualify. Try lowering --min_surveys or --min_points.")

# Human-readable year list string  e.g.  "1990, 2000, 2010"
qualifying["survey_years_str"] = qualifying["qualifying_years"].apply(
    lambda yl: ", ".join(str(y) for y in yl)
)

# ── ENRICH WITH METADATA ───────────────────────────────────────────────────────
meta_cols = [ID_COL]
if NAME_COL:
    meta_cols.append(NAME_COL)
for c in ["POLITICAL_UNIT", "POINT_LAT", "POINT_LON", "THICKNESS"]:
    if c in df.columns:
        meta_cols.append(c)

meta = (
    df[df[ID_COL].isin(qualifying[ID_COL])][meta_cols]
    .groupby(ID_COL)
    .agg(
        **({ "glacier_name": (NAME_COL, "first") } if NAME_COL else {}),
        country        = ("POLITICAL_UNIT", "first"),
        center_lat     = ("POINT_LAT",      "mean"),
        center_lon     = ("POINT_LON",      "mean"),
        mean_thickness = ("THICKNESS",      "mean"),
        max_thickness  = ("THICKNESS",      "max"),
    )
    .reset_index()
)

result = qualifying.merge(meta, on=ID_COL, how="left")
result = result.sort_values(
    ["n_surveys", "year_span"], ascending=[False, False]
).reset_index(drop=True)
result.index += 1   # 1-based rank

# Drop list column before saving (already have string version)
result_save = result.drop(columns=["qualifying_years"])

# ── PRINT TOP 30 ───────────────────────────────────────────────────────────────
print(f"\n{'─'*64}")
print(f"  Top 30 qualifying glaciers  (of {len(qualifying):,}):")
print(f"{'─'*64}")

show = ["glacier_name", "country", "n_surveys", "survey_years_str",
        "year_span", "total_points", "mean_thickness", "max_thickness",
        "center_lat", "center_lon"]
show = [c for c in show if c in result_save.columns]

pd.set_option("display.max_rows",    35)
pd.set_option("display.max_columns", 15)
pd.set_option("display.width",       240)
pd.set_option("display.float_format", "{:.2f}".format)
print(result_save[show].head(30).to_string())

# ── SURVEY YEAR DISTRIBUTION ───────────────────────────────────────────────────
print(f"\n  Year distribution across all thickness points:")
yr_dist = working["SURVEY_YEAR"].value_counts().sort_index()
for yr, cnt in yr_dist.items():
    bar = "█" * int(cnt / yr_dist.max() * 40)
    print(f"    {yr}  {cnt:>8,}  {bar}")

# ── SAVE ───────────────────────────────────────────────────────────────────────
print(f"\n{'─'*64}")
print("  Saving outputs ...")

# 1. Per-glacier summary
p = f"{args.out}/glathida_multiyear_summary.csv"
result_save.to_csv(p, index_label="rank")
print(f"  ✓ glathida_multiyear_summary.csv        ({len(result_save):,} glaciers)")

# 2. All raw TTT points for qualifying glaciers
df_filt = df[df[ID_COL].isin(qualifying[ID_COL])].copy()
p = f"{args.out}/glathida_multiyear_points.csv"
df_filt.to_csv(p, index=False)
print(f"  ✓ glathida_multiyear_points.csv         ({len(df_filt):,} point rows)")

# 3. Per-glacier × qualifying-year breakdown (gap-filtered)
# Expand qualifying_years list back to one row per glacier-year
rows = []
for _, r in qualifying.iterrows():
    for y in r["qualifying_years"]:
        sub = pts_per_year[
            (pts_per_year[ID_COL] == r[ID_COL]) &
            (pts_per_year["SURVEY_YEAR"] == y)
        ]
        rows.append({
            ID_COL:      r[ID_COL],
            "year":      y,
            "n_points":  int(sub["n_points"].iloc[0]) if len(sub) else 0,
        })
detail = pd.DataFrame(rows).sort_values([ID_COL, "year"])
p = f"{args.out}/glathida_qualifying_year_detail.csv"
detail.to_csv(p, index=False)
print(f"  ✓ glathida_qualifying_year_detail.csv   "
      f"({len(detail):,} glacier×year rows)")

# 4. ID list
p = f"{args.out}/qualifying_glathida_ids.txt"
with open(p, "w") as f:
    for gid in sorted(qualifying[ID_COL].astype(str)):
        f.write(gid + "\n")
print(f"  ✓ qualifying_glathida_ids.txt           ({len(qualifying):,} IDs)")

# ── DONE ───────────────────────────────────────────────────────────────────────
print(f"\n{SEP}")
print(f"  Done!  {len(qualifying):,} glaciers with >= {args.min_surveys} years "
      f"(min {args.min_points} pts/year, >= 1 yr gap).")
print(f"  Elapsed: {time.time()-t0:.1f}s")
print(f"{SEP}\n")
print("  Next steps:")
print("  1. glathida_multiyear_summary.csv  → browse & pick glaciers for heatmaps")
print("  2. glathida_multiyear_points.csv   → load into heatmap/interpolation script")
print("  3. glathida_qualifying_year_detail.csv → exact years + point counts per glacier")
print()