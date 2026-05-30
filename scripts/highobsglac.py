"""
GLIMS DBF Filter — Multi-Observation Glaciers
Filters glims_polygons.dbf to keep only glaciers with >= N observations.
Outputs a filtered CSV and a summary report.

Usage:
    python glims_filter_multiyear.py --dbf glims_polygons.dbf
    python glims_filter_multiyear.py --dbf glims_polygons.dbf --min_obs 6 --out my_output
    python glims_filter_multiyear.py --dbf glims_polygons.dbf --min_obs 4 --region "ALASKA"
"""

import argparse
import warnings
warnings.filterwarnings("ignore")

import pandas as pd
import numpy as np
import os
import sys
import time

# ── CLI ────────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(
    description="Filter GLIMS DBF to glaciers with >= N observations",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=__doc__
)
parser.add_argument("--dbf",      default="glims_polygons.dbf",
                    help="Path to glims_polygons.dbf (default: ./glims_polygons.dbf)")
parser.add_argument("--min_obs",  type=int, default=4,
                    help="Minimum number of observations per glacier (default: 4)")
parser.add_argument("--out",      default="glims_filtered",
                    help="Output folder (default: ./glims_filtered)")
parser.add_argument("--region",   default=None,
                    help="Optional: filter by geog_area substring, e.g. 'ALASKA' or 'ALPS'")
parser.add_argument("--sample",   type=int, default=None,
                    help="Load only first N rows (for quick testing)")
parser.add_argument("--line_type",default="glac_bound",
                    help="Keep only this line_type (default: glac_bound). Use 'all' for no filter.")
args = parser.parse_args()

os.makedirs(args.out, exist_ok=True)

SEP = "=" * 62

# ── LOAD ───────────────────────────────────────────────────────────────────────
print(f"\n{SEP}")
print("  GLIMS — Multi-Observation Glacier Filter")
print(SEP)
print(f"\n  Min observations : {args.min_obs}")
print(f"  Line type filter : {args.line_type}")
print(f"  Region filter    : {args.region or 'none'}")
print(f"  Output folder    : {args.out}/")

print(f"\n[1/5] Loading {args.dbf} ...")
t0 = time.time()

try:
    from simpledbf import Dbf5
except ImportError:
    sys.exit("ERROR: simpledbf not installed.  Run: pip install simpledbf")

# ── Encoding detection ─────────────────────────────────────────────────────────
# Strategy:
#   1. Use chardet to sniff the raw bytes of the DBF file (reads up to 500 KB
#      as a representative sample — full file can be 1.4 GB so we sample).
#   2. Validate the chardet guess by actually decoding a known text column
#      and checking for replacement characters (U+FFFD) or null runs.
#   3. Fall back through a priority list if validation fails.
#   NOTE: latin-1 / iso-8859-1 accept every byte (no UnicodeDecodeError),
#         so they can never "fail" — they must be validated, not just tried.

def _sniff_encoding(path, sample_bytes=500_000):
    """Run chardet on the first sample_bytes of the file."""
    try:
        import chardet
        with open(path, "rb") as f:
            raw = f.read(sample_bytes)
        result = chardet.detect(raw)
        enc    = result.get("encoding") or ""
        conf   = result.get("confidence", 0.0)
        return enc, conf
    except ImportError:
        return None, 0.0
    except Exception:
        return None, 0.0

def _validate_df(df, enc):
    """
    Check whether the loaded dataframe looks correctly decoded.
    Looks at string columns for:
      - Replacement character U+FFFD  (bytes decoded as wrong encoding)
      - More than 10% null / empty values in glac_id (structural problem)
    Returns True if the encoding looks valid.
    """
    str_cols = df.select_dtypes(include="object").columns
    for col in str_cols:
        sample = df[col].dropna().astype(str).head(5000)
        bad = sample.str.contains("\ufffd", regex=False).sum()
        if bad > len(sample) * 0.01:   # >1% replacement chars → bad encoding
            return False
    if "glac_id" in df.columns:
        null_frac = df["glac_id"].isna().mean()
        if null_frac > 0.10:
            return False
    return True

# Priority order: chardet result first, then manual fallbacks.
# latin-1 is LAST because it silently accepts everything.
FALLBACK_ENCODINGS = ["utf-8", "utf-8-sig", "windows-1252", "cp1252",
                      "iso-8859-1", "latin-1"]

sniffed_enc, sniffed_conf = _sniff_encoding(args.dbf)
if sniffed_enc:
    print(f"      chardet guess : {sniffed_enc}  "
          f"(confidence {sniffed_conf*100:.0f}%)")
    # Normalise chardet output (it sometimes returns e.g. "ISO-8859-1")
    normalised = sniffed_enc.lower().replace("-", "").replace("_", "")
    # Build try-list: chardet suggestion first, then fallbacks (deduped)
    seen = set()
    encodings_to_try = []
    for e in [sniffed_enc] + FALLBACK_ENCODINGS:
        key = e.lower().replace("-","").replace("_","")
        if key not in seen:
            seen.add(key)
            encodings_to_try.append(e)
else:
    print("      chardet unavailable — using fallback list")
    encodings_to_try = FALLBACK_ENCODINGS

df = None
used_enc = None
for enc in encodings_to_try:
    try:
        dbf    = Dbf5(args.dbf, codec=enc)
        df_try = dbf.to_dataframe()
        if _validate_df(df_try, enc):
            df       = df_try
            used_enc = enc
            print(f"      Encoding  : {enc}  ✓  (validated)")
            break
        else:
            print(f"      Encoding  : {enc}  ✗  (loaded but failed validation — trying next)")
    except (UnicodeDecodeError, LookupError):
        print(f"      Encoding  : {enc}  ✗  (decode error — trying next)")
    except Exception as e:
        print(f"      Encoding  : {enc}  ✗  ({e})")

if df is None:
    sys.exit(
        "ERROR: Could not load DBF with any known encoding.\n"
        "       Install chardet for better detection:  pip install chardet\n"
        "       Or specify manually by editing FALLBACK_ENCODINGS in the script."
    )

# Install chardet reminder
try:
    import chardet
except ImportError:
    print("      Tip: pip install chardet  — enables automatic encoding detection")

if args.sample:
    df = df.iloc[:args.sample]
    print(f"      Sample   : first {args.sample:,} rows")

print(f"      Loaded   : {len(df):,} rows  |  {len(df.columns)} columns  "
      f"({time.time()-t0:.1f}s)")

# ── CLEAN ──────────────────────────────────────────────────────────────────────
print(f"\n[2/5] Cleaning ...")

# Strip whitespace from all string columns (DBF pads strings)
str_cols = df.select_dtypes(include="object").columns
df[str_cols] = df[str_cols].apply(lambda c: c.str.strip())

# Parse dates
df["src_date_parsed"] = pd.to_datetime(df["src_date"], errors="coerce")
df["year"]            = df["src_date_parsed"].dt.year

before = len(df)

# Filter by line_type
if args.line_type.lower() != "all":
    df = df[df["line_type"].str.lower() == args.line_type.lower()]
    print(f"      line_type='{args.line_type}' : {before:,} → {len(df):,} rows "
          f"(-{before - len(df):,})")
    before = len(df)

# Filter by region
if args.region:
    mask   = df["geog_area"].str.contains(args.region, case=False, na=False)
    df     = df[mask]
    print(f"      region='{args.region}'     : {before:,} → {len(df):,} rows "
          f"(-{before - len(df):,})")
    before = len(df)

# Drop rows with null glac_id
null_id = df["glac_id"].isna().sum()
if null_id:
    df    = df[df["glac_id"].notna()]
    print(f"      null glac_id removed : {null_id:,}")

# ── COUNT OBSERVATIONS PER GLACIER ────────────────────────────────────────────
print(f"\n[3/5] Counting observations per glacier ...")

obs_counts = df.groupby("glac_id").size().rename("n_obs")

total_glaciers  = obs_counts.shape[0]
qualify         = obs_counts[obs_counts >= args.min_obs]
n_qualify       = qualify.shape[0]

print(f"      Total unique glaciers   : {total_glaciers:,}")
print(f"      With >= {args.min_obs} obs          : {n_qualify:,}  "
      f"({n_qualify/total_glaciers*100:.1f}%)")
print(f"      Observation distribution:")

thresholds = [1, 2, 3, 4, 5, 6, 8, 10, 15, 20, 30]
for t in thresholds:
    n = (obs_counts >= t).sum()
    bar = "█" * int(n / total_glaciers * 30)
    print(f"        >= {t:>2} obs : {n:>7,}  ({n/total_glaciers*100:5.1f}%)  {bar}")

# ── FILTER ─────────────────────────────────────────────────────────────────────
print(f"\n[4/5] Filtering to glaciers with >= {args.min_obs} observations ...")

qualifying_ids  = qualify.index
df_filtered     = df[df["glac_id"].isin(qualifying_ids)].copy()
df_filtered["n_obs"] = df_filtered["glac_id"].map(obs_counts)

# Sort: most-observed first, then by glac_id and date
df_filtered = df_filtered.sort_values(
    ["n_obs", "glac_id", "src_date_parsed"],
    ascending=[False, True, True]
)

print(f"      Rows in filtered dataset : {len(df_filtered):,}")
print(f"      Unique glaciers kept     : {df_filtered['glac_id'].nunique():,}")

# ── SUMMARY TABLE PER GLACIER ──────────────────────────────────────────────────
print(f"\n[5/5] Building per-glacier summary ...")

# Compute per-glacier stats
grp = df_filtered.groupby("glac_id")

summary = pd.DataFrame({
    "n_obs"         : grp["src_date"].count(),
    "first_obs"     : grp["src_date_parsed"].min(),
    "last_obs"      : grp["src_date_parsed"].max(),
    "year_span"     : grp["year"].max() - grp["year"].min(),
    "years_list"    : grp["year"].apply(lambda x: sorted(x.dropna().astype(int).unique().tolist())),
    "mean_gap_yrs"  : grp["year"].apply(
                        lambda x: (x.dropna().sort_values().diff().mean())
                        if x.dropna().shape[0] > 1 else np.nan),
    "min_area"      : grp["area"].min(),
    "max_area"      : grp["area"].max(),
    "mean_area"     : grp["area"].mean(),
    "area_chg_pct"  : grp["area"].apply(
                        lambda x: (x.sort_index().iloc[-1] - x.sort_index().iloc[0])
                                  / x.sort_index().iloc[0] * 100
                        if x.notna().sum() > 1 else np.nan),
    "glac_name"     : grp["glac_name"].first(),
    "geog_area"     : grp["geog_area"].first(),
    "gtng_o1reg"    : grp["gtng_o1reg"].first(),
    "mean_elev"     : grp["mean_elev"].mean(),
    "surge_type"    : grp["surge_type"].first(),
}).reset_index()

summary["years_list"] = summary["years_list"].apply(str)
summary["area_chg_pct"] = summary["area_chg_pct"].round(2)
summary["mean_gap_yrs"]  = summary["mean_gap_yrs"].round(1)
summary["mean_area"]     = summary["mean_area"].round(4)

# Rank by n_obs descending
summary = summary.sort_values("n_obs", ascending=False).reset_index(drop=True)
summary.index += 1   # 1-based rank

# ── PRINT TOP 30 ───────────────────────────────────────────────────────────────
print(f"\n{'─'*62}")
print(f"  Top 30 most-observed glaciers (of {n_qualify:,} qualifying):")
print(f"{'─'*62}")

cols_show = ["glac_id","glac_name","n_obs","first_obs","last_obs",
             "year_span","mean_area","area_chg_pct","geog_area"]
top30 = summary.head(30)[cols_show].copy()
top30["first_obs"] = top30["first_obs"].dt.year.astype("Int64")
top30["last_obs"]  = top30["last_obs"].dt.year.astype("Int64")
top30["mean_area"] = top30["mean_area"].round(2)

pd.set_option("display.max_rows",   35)
pd.set_option("display.max_columns", 12)
pd.set_option("display.width",       200)
pd.set_option("display.float_format", "{:.2f}".format)
print(top30.to_string())

# ── SAVE OUTPUTS ───────────────────────────────────────────────────────────────
print(f"\n{'─'*62}")
print("  Saving outputs ...")

# 1. Filtered rows (all columns from original DBF + n_obs)
out_filtered = f"{args.out}/glims_multiyear_rows.csv"
df_filtered.to_csv(out_filtered, index=False)
sz = os.path.getsize(out_filtered) / 1024
print(f"  ✓ glims_multiyear_rows.csv          {sz:>8.0f} KB  "
      f"({len(df_filtered):,} rows — all polygon records for qualifying glaciers)")

# 2. Per-glacier summary
out_summary = f"{args.out}/glims_multiyear_summary.csv"
summary.to_csv(out_summary, index_label="rank")
sz = os.path.getsize(out_summary) / 1024
print(f"  ✓ glims_multiyear_summary.csv       {sz:>8.0f} KB  "
      f"({len(summary):,} glaciers — one row per glacier with stats)")

# 3. Top-50 most observed (easy to explore)
out_top = f"{args.out}/glims_top50_most_observed.csv"
summary.head(50).to_csv(out_top, index_label="rank")
sz = os.path.getsize(out_top) / 1024
print(f"  ✓ glims_top50_most_observed.csv     {sz:>8.0f} KB  "
      f"(top 50 by observation count — best slideshow candidates)")

# 4. GlacIDs-only list (useful for joining back to shapefile)
out_ids = f"{args.out}/qualifying_glac_ids.txt"
with open(out_ids, "w") as f:
    for gid in sorted(qualifying_ids):
        f.write(gid + "\n")
sz = os.path.getsize(out_ids) / 1024
print(f"  ✓ qualifying_glac_ids.txt           {sz:>8.0f} KB  "
      f"({len(qualifying_ids):,} glacier IDs — use to filter shapefile in QGIS or geopandas)")

# 5. Quick stats text report
out_report = f"{args.out}/filter_report.txt"
with open(out_report, "w") as f:
    f.write(f"GLIMS Multi-Observation Filter Report\n")
    f.write(f"{'='*45}\n\n")
    f.write(f"DBF file          : {args.dbf}\n")
    f.write(f"Min observations  : {args.min_obs}\n")
    f.write(f"Line type         : {args.line_type}\n")
    f.write(f"Region filter     : {args.region or 'none'}\n\n")
    f.write(f"Input rows        : {before:,}\n")
    f.write(f"Output rows       : {len(df_filtered):,}\n")
    f.write(f"Qualifying glaciers: {n_qualify:,} / {total_glaciers:,} "
            f"({n_qualify/total_glaciers*100:.1f}%)\n\n")
    f.write(f"Year range        : {int(df_filtered['year'].min())} – "
            f"{int(df_filtered['year'].max())}\n")
    f.write(f"Max obs/glacier   : {summary['n_obs'].max()}\n")
    f.write(f"Median obs        : {summary['n_obs'].median():.1f}\n\n")
    f.write(f"Observation count distribution:\n")
    for t in thresholds:
        n = (obs_counts >= t).sum()
        f.write(f"  >= {t:>2} : {n:>7,}  ({n/total_glaciers*100:5.1f}%)\n")
    f.write(f"\nTop 20 most-observed glaciers:\n")
    f.write(top30.head(20).to_string() + "\n")

print(f"  ✓ filter_report.txt                          (plain-text summary)")

# ── DONE ───────────────────────────────────────────────────────────────────────
print(f"\n{SEP}")
print(f"  Done!  {n_qualify:,} glaciers with >= {args.min_obs} observations.")
print(f"  Total elapsed: {time.time()-t0:.1f}s")
print(f"{SEP}\n")
print("  Next steps:")
print(f"  1. Open glims_multiyear_summary.csv to browse qualifying glaciers")
print(f"  2. Use glims_top50_most_observed.csv for slideshow candidates")
print(f"  3. Filter shapefile in Python:")
print(f"       import geopandas as gpd, pandas as pd")
print(f"       ids = pd.read_csv('{args.out}/qualifying_glac_ids.txt', header=None)[0]")
print(f"       gdf = gpd.read_file('glims_polygons.shp')")
print(f"       gdf_filtered = gdf[gdf['glac_id'].isin(ids)]")
print(f"  4. Or load in QGIS: Layer → Add Layer → Add Delimited Text Layer")
print(f"     → join to shapefile on glac_id\n")