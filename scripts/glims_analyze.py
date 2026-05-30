"""
GLIMS Glacier Database - DBF Analysis Script
Reads glims_polygons.dbf directly (no geopandas / shapefile needed).
Usage: python glims_analyze.py --dbf /path/to/glims_polygons.dbf
"""

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

# ── DATE PARSING ───────────────────────────────────────────────────────────────
print(f"\n[3/7] Temporal Coverage (src_date)")

df["src_date_parsed"] = pd.to_datetime(df["src_date"], errors="coerce")
df["year"]   = df["src_date_parsed"].dt.year
df["decade"] = (df["year"] // 10 * 10).astype("Int64")

valid_dates = df["src_date_parsed"].notna().sum()
print(f"      Valid dates   : {valid_dates:,} / {len(df):,} ({valid_dates/len(df)*100:.1f}%)")
print(f"      Year range    : {int(df['year'].min())} – {int(df['year'].max())}")
print(f"      Unique years  : {df['year'].nunique()}")

year_counts = df["year"].value_counts().sort_index()
print(f"\n      Observations per year (top 20 by count):")
print(year_counts.nlargest(20).to_string())

# anlys_time as secondary date check
if "anlys_time" in df.columns:
    df["anlys_time_parsed"] = pd.to_datetime(df["anlys_time"], errors="coerce")
    print(f"\n      anlys_time range: "
          f"{df['anlys_time_parsed'].min()} → {df['anlys_time_parsed'].max()}")

# ── GLACIER IDs / TIME SERIES DEPTH ───────────────────────────────────────────
print(f"\n[4/7] Glacier ID Analysis (glac_id)")

n_unique = df["glac_id"].nunique()
obs_per_glacier = df.groupby("glac_id").size()

print(f"      Unique glaciers          : {n_unique:,}")
print(f"      Total records            : {len(df):,}")
print(f"      Avg observations/glacier : {obs_per_glacier.mean():.2f}")
print(f"      Max observations/glacier : {obs_per_glacier.max()}")
print(f"      Glaciers with 1 obs only : {(obs_per_glacier == 1).sum():,} "
      f"({(obs_per_glacier==1).mean()*100:.1f}%)")
print(f"      Glaciers with 5+ obs     : {(obs_per_glacier >= 5).sum():,}")
print(f"      Glaciers with 10+ obs    : {(obs_per_glacier >= 10).sum():,}")

# Time span per glacier
grp_dates = df.dropna(subset=["src_date_parsed"]).groupby("glac_id")["src_date_parsed"]
spans = (grp_dates.max() - grp_dates.min()).dt.days / 365.25
print(f"\n      Time span per multi-obs glacier:")
print(f"        Median : {spans[spans > 0].median():.1f} yrs")
print(f"        Max    : {spans.max():.1f} yrs")

# Example: one glacier's full time series
example_id = obs_per_glacier.idxmax()   # glacier with most observations
example = df[df["glac_id"] == example_id][
    ["glac_id", "glac_name", "src_date", "area", "min_elev", "max_elev", "mean_elev"]
].sort_values("src_date")
print(f"\n      Most-observed glacier: {example_id}  "
      f"({obs_per_glacier[example_id]} records)")
print(example.to_string(index=False))

# ── AREA ANALYSIS ──────────────────────────────────────────────────────────────
print(f"\n[5/7] Area Analysis")

area = df["area"].dropna()
print(f"      Non-null area values : {len(area):,}")
print(f"      Min    : {area.min():.6f} km²")
print(f"      Mean   : {area.mean():.4f} km²")
print(f"      Median : {area.median():.4f} km²")
print(f"      Max    : {area.max():.2f} km²")
print(f"      Total  : {area.sum():,.1f} km²")

# Mean area trend over time
multi_mask = df["glac_id"].map(obs_per_glacier) > 1
multi = df[multi_mask].dropna(subset=["src_date_parsed", "area"])
if len(multi) > 0:
    area_by_year = multi.groupby("year")["area"].mean()
    earliest, latest = int(area_by_year.index.min()), int(area_by_year.index.max())
    if earliest != latest:
        pct = (area_by_year[latest] - area_by_year[earliest]) / area_by_year[earliest] * 100
        print(f"\n      Mean area {earliest} : {area_by_year[earliest]:.4f} km²")
        print(f"      Mean area {latest} : {area_by_year[latest]:.4f} km²")
        print(f"      Trend (multi-obs glaciers): {pct:+.1f}%")

# ── CATEGORICAL FIELDS ─────────────────────────────────────────────────────────
print(f"\n[6/7] Categorical Fields")

cat_cols = ["line_type", "rec_status", "glac_stat", "rgi_gl_typ",
            "primeclass", "surge_type", "term_type", "conn_lvl"]
for col in cat_cols:
    if col not in df.columns:
        continue
    vc = df[col].value_counts(dropna=False).head(8)
    print(f"\n      {col}:")
    for val, cnt in vc.items():
        bar = "█" * int(cnt / len(df) * 40)
        print(f"        {str(val):<25} {cnt:>8,}  {bar}")

# ── GEOGRAPHIC COVERAGE ────────────────────────────────────────────────────────
print(f"\n[7/7] Geographic / Regional Coverage")

if "geog_area" in df.columns:
    rc = df["geog_area"].value_counts(dropna=False).head(20)
    print("      Top regions (geog_area):")
    for r, c in rc.items():
        print(f"        {str(r):<40} {c:>8,}")

if "gtng_o1reg" in df.columns:
    print("\n      RGI Order-1 region distribution:")
    print(df["gtng_o1reg"].value_counts(dropna=False).to_string())

if "gtng_o2reg" in df.columns:
    print("\n      RGI Order-2 region distribution (top 20):")
    print(df["gtng_o2reg"].value_counts(dropna=False).head(20).to_string())

# ── PLOTS ──────────────────────────────────────────────────────────────────────
print(f"\nGenerating plots → {args.out}/")

DARK    = "#0d1117"
ACCENT  = "#58a6ff"
ACCENT2 = "#f0883e"
ACCENT3 = "#3fb950"
ACCENT4 = "#bc8cff"
TEXT    = "#e6edf3"
GRID    = "#21262d"

plt.rcParams.update({
    "figure.facecolor": DARK,  "axes.facecolor": DARK,
    "axes.edgecolor":   GRID,  "axes.labelcolor": TEXT,
    "xtick.color":      TEXT,  "ytick.color":     TEXT,
    "text.color":       TEXT,  "grid.color":      GRID,
    "grid.linewidth":   0.5,   "font.family":     "monospace",
})

fmt_int = FuncFormatter(lambda x, _: f"{x:,.0f}")

# ── Plot 1: Observations per year ──────────────────────────────────────────────
fig, ax = plt.subplots(figsize=(14, 5))
yc = year_counts.reindex(
    range(int(year_counts.index.min()), int(year_counts.index.max()) + 1),
    fill_value=0)
ax.bar(yc.index, yc.values, color=ACCENT, width=0.8, alpha=0.9)
ax.set_title("Glacier Observations per Year (GLIMS)", fontsize=14, pad=12)
ax.set_xlabel("Year"); ax.set_ylabel("# Records")
ax.yaxis.set_major_formatter(fmt_int)
ax.grid(axis="y")
plt.tight_layout()
plt.savefig(f"{args.out}/01_obs_per_year.png", dpi=150, bbox_inches="tight")
plt.close()
print("  ✓ 01_obs_per_year.png")

# ── Plot 2: Observations-per-glacier histogram ─────────────────────────────────
fig, ax = plt.subplots(figsize=(10, 5))
cap = min(int(obs_per_glacier.max()) + 2, 52)
ax.hist(obs_per_glacier.clip(upper=cap - 1), bins=np.arange(1, cap),
        color=ACCENT2, edgecolor=DARK, alpha=0.9)
ax.set_title("Distribution: # Observations per Glacier", fontsize=14, pad=12)
ax.set_xlabel(f"# Observations (capped at {cap-1})")
ax.set_ylabel("# Glaciers")
ax.yaxis.set_major_formatter(fmt_int)
ax.grid(axis="y")
plt.tight_layout()
plt.savefig(f"{args.out}/02_obs_per_glacier.png", dpi=150, bbox_inches="tight")
plt.close()
print("  ✓ 02_obs_per_glacier.png")

# ── Plot 3: Area trends over time ──────────────────────────────────────────────
if "area" in df.columns:
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    ya = df.dropna(subset=["year", "area"]).groupby("year")["area"]
    axes[0].plot(ya.mean().index, ya.mean().values,
                 color=ACCENT3, linewidth=2, marker="o", markersize=3)
    axes[0].set_title("Mean Glacier Area per Year", fontsize=12)
    axes[0].set_xlabel("Year"); axes[0].set_ylabel("Mean Area (km²)")
    axes[0].grid()

    axes[1].plot(ya.sum().index, ya.sum().values / 1e6,
                 color=ACCENT2, linewidth=2, marker="o", markersize=3)
    axes[1].set_title("Total Recorded Glacier Area per Year", fontsize=12)
    axes[1].set_xlabel("Year"); axes[1].set_ylabel("Total Area (×10⁶ km²)")
    axes[1].grid()

    plt.suptitle("GLIMS Area Trends", fontsize=14, y=1.01)
    plt.tight_layout()
    plt.savefig(f"{args.out}/03_area_over_time.png", dpi=150, bbox_inches="tight")
    plt.close()
    print("  ✓ 03_area_over_time.png")

# ── Plot 4: Area distribution (log scale) ─────────────────────────────────────
fig, ax = plt.subplots(figsize=(10, 5))
ac = df["area"].dropna()
ac = ac[ac > 0]
ax.hist(np.log10(ac), bins=80, color=ACCENT, edgecolor=DARK, alpha=0.9)
ax.set_title("Glacier Area Distribution (log₁₀ scale)", fontsize=14, pad=12)
ax.set_xlabel("log₁₀(Area in km²)"); ax.set_ylabel("Count")
ticks = [-3, -2, -1, 0, 1, 2, 3, 4, 5]
ax.set_xticks(ticks); ax.set_xticklabels([f"10^{t}" for t in ticks])
ax.grid(axis="y")
plt.tight_layout()
plt.savefig(f"{args.out}/04_area_distribution.png", dpi=150, bbox_inches="tight")
plt.close()
print("  ✓ 04_area_distribution.png")

# ── Plot 5: line_type breakdown ────────────────────────────────────────────────
if "line_type" in df.columns:
    vc = df["line_type"].value_counts()
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.barh(vc.index, vc.values, color=ACCENT3, alpha=0.9)
    ax.set_title("Polygon Types (line_type)", fontsize=14, pad=12)
    ax.set_xlabel("Count")
    ax.xaxis.set_major_formatter(fmt_int)
    ax.grid(axis="x")
    plt.tight_layout()
    plt.savefig(f"{args.out}/05_line_type.png", dpi=150, bbox_inches="tight")
    plt.close()
    print("  ✓ 05_line_type.png")

# ── Plot 6: Top geographic regions ────────────────────────────────────────────
if "geog_area" in df.columns:
    tr = df["geog_area"].value_counts().dropna().head(15)
    fig, ax = plt.subplots(figsize=(12, 6))
    ax.barh(tr.index[::-1], tr.values[::-1], color=ACCENT2, alpha=0.9)
    ax.set_title("Top 15 Geographic Regions (geog_area)", fontsize=14, pad=12)
    ax.set_xlabel("# Records")
    ax.xaxis.set_major_formatter(fmt_int)
    ax.grid(axis="x")
    plt.tight_layout()
    plt.savefig(f"{args.out}/06_top_regions.png", dpi=150, bbox_inches="tight")
    plt.close()
    print("  ✓ 06_top_regions.png")

# ── Plot 7: Elevation summary ──────────────────────────────────────────────────
elev_cols = [c for c in ["min_elev", "mean_elev", "max_elev"] if c in df.columns]
if elev_cols:
    fig, ax = plt.subplots(figsize=(10, 5))
    colors = [ACCENT, ACCENT3, ACCENT2]
    for col, color in zip(elev_cols, colors):
        data = df[col].dropna()
        data = data[(data > -500) & (data < 9000)]  # sanity filter
        ax.hist(data, bins=80, alpha=0.6, label=col, color=color)
    ax.set_title("Glacier Elevation Distribution", fontsize=14, pad=12)
    ax.set_xlabel("Elevation (m)"); ax.set_ylabel("Count")
    ax.legend(); ax.grid(axis="y")
    plt.tight_layout()
    plt.savefig(f"{args.out}/07_elevations.png", dpi=150, bbox_inches="tight")
    plt.close()
    print("  ✓ 07_elevations.png")

# ── CSV SUMMARIES ──────────────────────────────────────────────────────────────
print("\nSaving CSV summaries...")

# Per-year summary
agg_year = df.dropna(subset=["year"]).groupby("year").agg(
    n_records        = ("glac_id", "count"),
    n_unique_glaciers= ("glac_id", "nunique"),
    mean_area        = ("area",     "mean"),
    total_area       = ("area",     "sum"),
    mean_min_elev    = ("min_elev", "mean"),
    mean_max_elev    = ("max_elev", "mean"),
    mean_mean_elev   = ("mean_elev","mean"),
).reset_index()
agg_year.to_csv(f"{args.out}/summary_by_year.csv", index=False)
print("  ✓ summary_by_year.csv")

# Per-glacier time series summary
agg_glac = df.dropna(subset=["glac_id"]).groupby("glac_id").agg(
    n_obs      = ("src_date",        "count"),
    first_obs  = ("src_date_parsed", "min"),
    last_obs   = ("src_date_parsed", "max"),
    min_area   = ("area",            "min"),
    max_area   = ("area",            "max"),
    mean_area  = ("area",            "mean"),
    geog_area  = ("geog_area",       "first"),
    glac_name  = ("glac_name",       "first"),
    gtng_o1reg = ("gtng_o1reg",      "first"),
).reset_index()
agg_glac["area_change_pct"] = (
    (agg_glac["min_area"] - agg_glac["max_area"]) /
     agg_glac["max_area"] * 100
)
agg_glac.to_csv(f"{args.out}/summary_by_glacier.csv", index=False)
print("  ✓ summary_by_glacier.csv")

# Most-changed glaciers
top_shrink = agg_glac[agg_glac["n_obs"] > 2].nsmallest(20, "area_change_pct")
top_shrink.to_csv(f"{args.out}/top_shrinking_glaciers.csv", index=False)
print("  ✓ top_shrinking_glaciers.csv")

# ── DONE ───────────────────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"  Analysis complete!  Outputs in: {args.out}/")
print(f"{'='*60}\n")
for f in sorted(os.listdir(args.out)):
    size = os.path.getsize(f"{args.out}/{f}")
    print(f"  {f:<45} {size/1024:.1f} KB")
print()
