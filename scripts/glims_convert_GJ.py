"""
GLIMS Full Conversion → GeoJSON
────────────────────────────────────────────────────────────────────────────────
Converts ALL GLIMS shapefiles (north + south hemispheres) into 4 clean polygon
GeoJSON files, plus unified files for points / lines / images.

Polygon output (polygons/):
  glaciers_extinct_latest.geojson      ← most recent outline per extinct glacier
  glaciers_extinct_historical.geojson  ← all older outlines of extinct glaciers
  glaciers_current_latest.geojson      ← most recent outline per active glacier
  glaciers_current_historical.geojson  ← all older outlines of active glaciers

Other outputs:
  points/glacier_points.geojson        ← center points + DBF attributes
  lines/glacier_lines.geojson
  images/glacier_images.geojson
  analysis/<name>_analysis.txt         ← per-file diagnostic reports
  manifest.json

Columns stripped from ALL polygon/point outputs:
  subm_id, analysts, rgi_gl_typ, chief_affl, primeclass,
  line_type, rec_status, _hemisphere, _year, _extinct, _is_latest

Usage:
    python glims_convert_GJ.py
    python glims_convert_GJ.py --simplify 0.0001
    python glims_convert_GJ.py --sample 50000
"""

import argparse, os, sys, time, json, warnings
warnings.filterwarnings("ignore")

# ── CLI ────────────────────────────────────────────────────────────────────────
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Data lives in GlacierVisualization/ alongside this script
_GV_ROOT    = r"C:\Users\aiden\.git\GlacierVisualization"

parser = argparse.ArgumentParser(description="Convert GLIMS shapefiles (N+S) to GeoJSON")
parser.add_argument("--north", default=os.path.join(
    _GV_ROOT,
    "NSIDC-0272_glims_db_north_20260505_v01.0",
    "glims_download_35490"))
parser.add_argument("--south", default=os.path.join(
    _GV_ROOT,
    "NSIDC-0272_glims_db_south_20260505_v01.0",
    "glims_download_40252"))
parser.add_argument("--out",       default=os.path.join(_SCRIPT_DIR, "geojson_output"))
parser.add_argument("--simplify",  type=float, default=0.0)
parser.add_argument("--sample",    type=int,   default=None)
args = parser.parse_args()

# ── IMPORTS ────────────────────────────────────────────────────────────────────
try:
    import geopandas as gpd
    import pandas as pd
    import numpy as np
except ImportError:
    sys.exit("ERROR: pip install geopandas pandas numpy")

SEP = "=" * 70
t0  = time.time()
HEMI_DIRS = {"north": args.north, "south": args.south}

# Whitelist: ONLY these columns kept in polygon outputs
# (geometry is always kept separately)
KEEP_COLS_BASE    = {"glac_id", "src_date", "glac_name", "area"}
KEEP_COLS_EXTINCT = KEEP_COLS_BASE | {"gone_date"}   # gone_date only for extinct
KEEP_COLS_CURRENT = KEEP_COLS_BASE                   # no gone_date for active

# Internal helper columns — always dropped before writing
HELPER_COLS = {"_hemisphere", "_year", "_extinct", "_is_latest", "line_type"}

# Metadata columns to strip from outputs (used when building attribute lookups)
STRIP_COLS = {"subm_id", "analysts", "rgi_gl_typ", "chief_affl", "primeclass",
              "rec_status", "submitters"}

for sub in ["polygons", "points", "lines", "images", "analysis"]:
    os.makedirs(os.path.join(args.out, sub), exist_ok=True)

print(f"\n{SEP}")
print("  GLIMS → GeoJSON  |  4-bucket polygon split  (N + S hemispheres)")
print(SEP)
for hemi, d in HEMI_DIRS.items():
    print(f"  [{hemi}] {'✓ found' if os.path.isdir(d) else '✗ NOT FOUND'}: {d}")
print(f"  Output    : {args.out}")
print(f"  Simplify  : {args.simplify or 'none'}")

if not any(os.path.isdir(d) for d in HEMI_DIRS.values()):
    sys.exit("ERROR: Neither hemisphere directory found. Check --north / --south.")

manifest = {
    "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "north_dir": args.north, "south_dir": args.south,
    "simplify":  args.simplify, "files": {}
}

# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def clean_strings(gdf):
    for col in gdf.select_dtypes(include="object").columns:
        try:
            gdf[col] = gdf[col].str.strip()
        except Exception:
            pass
    return gdf


def extract_year(raw):
    """Robustly parse any GLIMS date variant → int year or None."""
    s = str(raw).strip()
    if s in ("nan", "None", "NaT", ""):
        return None
    # Strip float suffix, hyphens, spaces; keep only digits
    s = s.split(".")[0].replace("-", "").replace(" ", "")
    s = "".join(c for c in s if c.isdigit())[:8]
    if len(s) < 4:
        return None
    s = s.ljust(8, "0")
    year  = s[:4]
    month = s[4:6] if s[4:6] not in ("00", "") else "01"
    day   = s[6:8] if s[6:8] not in ("00", "") else "01"
    try:
        return int(pd.to_datetime(f"{year}{month}{day}", format="%Y%m%d").year)
    except Exception:
        return None


def is_extinct(row):
    """Return True if this record represents an extinct glacier."""
    stat = str(row.get("glac_stat", "")).strip().lower()
    if stat in ("0", "extinct", "gone", "disappeared"):
        return True
    gone = str(row.get("gone_date", "")).strip()
    if gone and gone.lower() not in ("nan", "none", "", "0", "00000000"):
        return True
    return False


def label_latest(gdf, id_col="glac_id", year_col="_year"):
    """
    Add boolean column '_is_latest' = True for exactly one row per glacier:
    the row with the highest _year value.

    Critical implementation notes
    ──────────────────────────────
    • gdf MUST have a unique RangeIndex (call reset_index(drop=True) first).
    • We use groupby + idxmax() which returns the INTEGER LABEL of the max-year
      row directly — no head(1) / isin() trickery that breaks on non-unique idx.
    • Glaciers with no valid year get their first-occurring row flagged as latest.
    """
    assert gdf.index.is_unique, "label_latest requires a unique index — call reset_index first"

    gdf = gdf.copy()
    gdf["_is_latest"] = False

    if id_col not in gdf.columns:
        gdf["_is_latest"] = True
        return gdf

    # ── dated rows: pick index of max year per glacier ────────────────────────
    dated   = gdf[gdf[year_col].notna()]
    undated = gdf[gdf[year_col].isna()]

    if len(dated):
        # idxmax returns a Series: glac_id → index-label of the max-year row
        latest_labels = dated.groupby(id_col)[year_col].idxmax()
        gdf.loc[latest_labels.values, "_is_latest"] = True

    if len(undated):
        # For undated glaciers — first occurrence per glac_id becomes "latest"
        first_labels = undated.groupby(id_col).apply(lambda x: x.index[0])
        # pandas may return a MultiIndex if the group has sub-index; flatten
        if hasattr(first_labels, "values"):
            gdf.loc[first_labels.values, "_is_latest"] = True

    return gdf


def sanitise_and_keep(gdf, keep_cols):
    """
    Prepare a GeoDataFrame for GeoJSON export using a WHITELIST approach:
      1. Keep ONLY columns in keep_cols + geometry.
      2. Drop null / empty geometries.
      3. Ensure EPSG:4326.
      4. Replace float NaN / inf → None (serialises as JSON null).
      5. Drop all internal helper columns (_*).
      6. Reset to clean RangeIndex.
      7. Case-insensitive column matching (handles shapefile column name variations).
    """
    out = gdf.copy()

    # Build case-insensitive mapping: lowercase → actual column name
    col_map = {c.lower(): c for c in out.columns}

    # Drop ALL internal helper columns first (case-insensitive)
    helper_lower = {h.lower() for h in HELPER_COLS}
    helper_drop = [col_map[h] for h in helper_lower if h in col_map]
    out = out.drop(columns=helper_drop, errors="ignore")

    # Keep ONLY whitelisted columns + geometry (case-insensitive matching)
    keep_lower = {k.lower() for k in keep_cols}
    allowed_cols = [col_map.get(k, k) for k in keep_lower if k in col_map]
    if "geometry" in out.columns:
        allowed_cols.append("geometry")
    out = out[[c for c in out.columns if c in allowed_cols]]

    # Drop bad geometries
    out = out[out.geometry.notna()]
    out = out[~out.geometry.is_empty]

    # CRS
    if out.crs is None:
        out = out.set_crs("EPSG:4326")
    elif str(out.crs).upper() not in ("EPSG:4326", "WGS84"):
        out = out.to_crs("EPSG:4326")

    # Fix float columns: NaN / inf → None
    for col in out.select_dtypes(include=["float32", "float64"]).columns:
        out[col] = out[col].apply(
            lambda x: None if (
                x is None or
                (isinstance(x, float) and (x != x or
                 x == float("inf") or x == float("-inf")))
            ) else float(round(x, 6))
        )

    # Fix nullable-integer columns
    for col in out.select_dtypes(include=["Int64","Int32","Int16",
                                          "UInt64","UInt32"]).columns:
        out[col] = out[col].astype(object).where(out[col].notna(), other=None)

    out = out.reset_index(drop=True)
    return out


def write_geojson(gdf, path, desc="", keep_cols=None):
    """Whitelist-filter, sanitise, and write GeoJSON. Returns (n_features, size_mb)."""
    cols = keep_cols if keep_cols else KEEP_COLS_BASE
    out = sanitise_and_keep(gdf, cols)
    if len(out) == 0:
        print(f"      ⚠  {os.path.basename(path)} — 0 features after sanitise, skipping")
        return 0, 0.0

    out.to_file(path, driver="GeoJSON")
    mb    = os.path.getsize(path) / (1024 * 1024)
    fname = os.path.relpath(path, args.out).replace("\\", "/")
    print(f"      ✓  {os.path.basename(path):<52} {len(out):>8,} features  {mb:6.1f} MB")
    manifest["files"][fname] = {
        "description": desc, "features": len(out), "size_mb": round(mb, 2)
    }
    return len(out), mb


def load_layer(layer_name, hemi_dirs, sample=None):
    """Load a shapefile from both hemisphere dirs, merge, reproject to WGS84."""
    parts = []
    for hemi, folder in hemi_dirs.items():
        shp = os.path.join(folder, f"{layer_name}.shp")
        if not os.path.exists(shp):
            print(f"      [{hemi}] {layer_name}.shp — not found")
            continue
        try:
            g = gpd.read_file(shp, rows=sample)
            g["_hemisphere"] = hemi
            parts.append(g)
            print(f"      [{hemi}] {layer_name}.shp  →  {len(g):,} rows  CRS:{g.crs}")
        except Exception as e:
            print(f"      [{hemi}] ERROR: {e}")
    if not parts:
        return None
    merged = pd.concat(parts, ignore_index=True)   # ← always clean 0-based index
    gdf    = gpd.GeoDataFrame(merged, crs=parts[0].crs)
    if str(gdf.crs).upper() not in ("EPSG:4326", "WGS84"):
        gdf = gdf.to_crs("EPSG:4326")
    return clean_strings(gdf)


# ══════════════════════════════════════════════════════════════════════════════
# DIAGNOSTIC ANALYSIS WRITER
# ══════════════════════════════════════════════════════════════════════════════

def write_analysis(gdf, filepath, label):
    """Write a detailed diagnostic .txt report for one output bucket."""
    W  = 62
    LN = []

    def h(title):
        LN.append(f"\n{'─'*W}")
        LN.append(f"  {title}")
        LN.append(f"{'─'*W}")

    LN.append("=" * W)
    LN.append(f"  GLIMS GeoJSON — Diagnostic Analysis Report")
    LN.append(f"  File      : {os.path.basename(filepath).replace('_analysis.txt','')}")
    LN.append(f"  Bucket    : {label}")
    LN.append(f"  Generated : {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    LN.append("=" * W)

    h("ROW & FEATURE COUNTS")
    LN.append(f"  Total features        : {len(gdf):,}")
    if "glac_id" in gdf.columns:
        LN.append(f"  Unique glac_id        : {gdf['glac_id'].nunique():,}")
    if "_hemisphere" in gdf.columns:
        for hv, cnt in gdf["_hemisphere"].value_counts().items():
            LN.append(f"  Hemisphere [{hv}]      : {cnt:,}")

    h("DATE / YEAR COVERAGE  (src_date → _year)")
    if "_year" in gdf.columns:
        yr = gdf["_year"].dropna()
        n_null = gdf["_year"].isna().sum()
        LN.append(f"  Rows with valid year  : {len(yr):,} / {len(gdf):,}  "
                  f"({len(yr)/max(len(gdf),1)*100:.1f}%)")
        LN.append(f"  Rows with null year   : {n_null:,}")
        if len(yr):
            LN.append(f"  Year range            : {int(yr.min())} – {int(yr.max())}")
            LN.append(f"  Unique years          : {yr.nunique()}")
            LN.append(f"\n  Records per year:")
            yc = yr.astype(int).value_counts().sort_index()
            for y, c in yc.items():
                bar = "█" * int(c / yc.max() * 32)
                LN.append(f"    {y}  {c:>8,}  {bar}")
    else:
        LN.append("  (_year column not present in this bucket)")

    h("AREA  (km²)")
    if "area" in gdf.columns:
        a = pd.to_numeric(gdf["area"], errors="coerce").dropna()
        LN.append(f"  Non-null rows         : {len(a):,}  "
                  f"(null: {gdf['area'].isna().sum():,})")
        if len(a):
            LN.append(f"  Min                   : {a.min():.6f}")
            LN.append(f"  Median                : {a.median():.4f}")
            LN.append(f"  Mean                  : {a.mean():.4f}")
            LN.append(f"  Max                   : {a.max():.2f}")
            LN.append(f"  Total                 : {a.sum():,.1f}")
    else:
        LN.append("  (area column not present)")

    h("ELEVATION  (m)")
    for col, lbl in [("min_elev","Min"),("mean_elev","Mean"),("max_elev","Max")]:
        if col in gdf.columns:
            e = pd.to_numeric(gdf[col], errors="coerce").dropna()
            if len(e):
                LN.append(f"  {lbl:<8}: "
                           f"min={e.min():.0f}  mean={e.mean():.0f}  "
                           f"max={e.max():.0f}  nulls={gdf[col].isna().sum():,}")

    h("EXTINCT / STATUS")
    if "glac_stat" in gdf.columns:
        for v, c in gdf["glac_stat"].value_counts(dropna=False).items():
            LN.append(f"  glac_stat = {str(v):<14}: {c:,}")
    if "gone_date" in gdf.columns:
        nn = gdf["gone_date"].notna().sum()
        LN.append(f"  gone_date non-null    : {nn:,}")

    h("GEOGRAPHIC REGIONS  (top 20 — geog_area)")
    if "geog_area" in gdf.columns:
        for r, c in gdf["geog_area"].value_counts(dropna=False).head(20).items():
            LN.append(f"  {str(r):<38} {c:>8,}")
    else:
        LN.append("  (geog_area column not present)")

    h("GEOMETRY VALIDATION")
    null_g  = gdf.geometry.isna().sum()
    empty_g = gdf[gdf.geometry.notna()].geometry.is_empty.sum()
    try:
        invalid_g = (~gdf[gdf.geometry.notna()].geometry.is_valid).sum()
    except Exception:
        invalid_g = "n/a"
    LN.append(f"  Null geometry         : {null_g:,}")
    LN.append(f"  Empty geometry        : {empty_g:,}")
    LN.append(f"  Invalid geometry      : {invalid_g}")
    LN.append(f"  CRS                   : {gdf.crs}")

    h("COLUMN NULL SUMMARY  (non-geometry columns)")
    for col in gdf.columns:
        if col == "geometry":
            continue
        n   = gdf[col].isna().sum()
        pct = n / max(len(gdf), 1) * 100
        flag = "  ← >50% null" if pct > 50 else ""
        LN.append(f"  {col:<24} nulls: {n:>8,}  ({pct:5.1f}%){flag}")

    LN.append(f"\n{'='*W}")
    LN.append("  End of report")
    LN.append(f"{'='*W}\n")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(LN))
    print(f"        → analysis: {os.path.basename(filepath)}")


# ══════════════════════════════════════════════════════════════════════════════
# LAYER 1 — POLYGONS  (4-bucket split)
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'─'*70}")
print("  [LAYER 1/4]  glims_polygons  →  polygons/")
print(f"{'─'*70}")

poly = load_layer("glims_polygons", HEMI_DIRS, sample=args.sample)
poly_attrs = None

if poly is not None:

    # ── Hard require a clean RangeIndex from this point on ───────────────────
    poly = poly.reset_index(drop=True)

    # ── Filter: keep ONLY glac_bound line_type ───────────────────────────────
    if "line_type" in poly.columns:
        before = len(poly)
        poly   = poly[poly["line_type"].str.strip().str.lower() == "glac_bound"]
        poly   = poly.reset_index(drop=True)
        print(f"\n      glac_bound filter : {before:,} → {len(poly):,} rows")

    # ── Simplify geometry ────────────────────────────────────────────────────
    if args.simplify > 0:
        print(f"      Simplifying geometry (tol={args.simplify}) ...")
        poly["geometry"] = poly["geometry"].simplify(
            tolerance=args.simplify, preserve_topology=True)

    # ── Parse year + extinct flag ─────────────────────────────────────────────
    print("      Parsing src_date → _year ...")
    poly["_year"] = (
        poly["src_date"].apply(extract_year)
        if "src_date" in poly.columns else None
    )
    print("      Classifying extinct status ...")
    poly["_extinct"] = poly.apply(is_extinct, axis=1)

    # ── Split: extinct vs active ──────────────────────────────────────────────
    extinct = poly[poly["_extinct"]].reset_index(drop=True).copy()
    active  = poly[~poly["_extinct"]].reset_index(drop=True).copy()

    print(f"\n      Total (glac_bound)   : {len(poly):,}")
    print(f"      ├─ Extinct           : {len(extinct):,}")
    print(f"      └─ Active            : {len(active):,}")

    # ── Label latest per glacier — EXTINCT ───────────────────────────────────
    print("      Labelling extinct latest/historical ...")
    extinct = label_latest(extinct, id_col="glac_id", year_col="_year")
    ext_latest = extinct[extinct["_is_latest"]].reset_index(drop=True).copy()
    ext_hist   = extinct[~extinct["_is_latest"]].reset_index(drop=True).copy()

    # ── Label latest per glacier — ACTIVE ────────────────────────────────────
    print("      Labelling active latest/historical ...")
    active = label_latest(active, id_col="glac_id", year_col="_year")
    cur_latest = active[active["_is_latest"]].reset_index(drop=True).copy()
    cur_hist   = active[~active["_is_latest"]].reset_index(drop=True).copy()

    print(f"\n      Extinct  latest      : {len(ext_latest):,}")
    print(f"      Extinct  historical  : {len(ext_hist):,}")
    print(f"      Current  latest      : {len(cur_latest):,}")
    print(f"      Current  historical  : {len(cur_hist):,}")

    # ── Sanity check: no duplicate glac_id in latest buckets ─────────────────
    for name, bucket in [("ext_latest", ext_latest), ("cur_latest", cur_latest)]:
        if "glac_id" in bucket.columns:
            dupes = bucket["glac_id"].duplicated().sum()
            status = "✓ no dupes" if dupes == 0 else f"⚠ {dupes} duplicate glac_id"
            print(f"      {name} glac_id check : {status}")

    # ── Write 4 polygon GeoJSON files ─────────────────────────────────────────
    print()
    POLY_BUCKETS = [
        # (filename, dataframe, description, keep_cols_set)
        ("glaciers_extinct_latest.geojson",     ext_latest,
         "Most recent outline per extinct glacier",     KEEP_COLS_EXTINCT),
        ("glaciers_extinct_historical.geojson", ext_hist,
         "All historical outlines of extinct glaciers", KEEP_COLS_EXTINCT),
        ("glaciers_current_latest.geojson",     cur_latest,
         "Most recent outline per active glacier",      KEEP_COLS_CURRENT),
        ("glaciers_current_historical.geojson", cur_hist,
         "All historical outlines of active glaciers",  KEEP_COLS_CURRENT),
    ]

    for fname, bucket, desc, keep in POLY_BUCKETS:
        p  = os.path.join(args.out, "polygons", fname)
        write_geojson(bucket, p, desc, keep_cols=keep)
        ap = os.path.join(args.out, "analysis",
                          fname.replace(".geojson", "_analysis.txt"))
        write_analysis(bucket, ap, desc)

    # ── Attribute lookup for joining onto points / lines ──────────────────────
    if "glac_id" in poly.columns:
        # Columns we'll join: everything useful, minus geometry and helpers
        skip = {"geometry","_year","_extinct","_is_latest","_hemisphere"} | STRIP_COLS
        attr_cols = [c for c in poly.columns if c not in skip]
        poly_attrs = (
            poly[attr_cols + ["_year"]]
            .sort_values("_year", ascending=False, na_position="last")
            .drop_duplicates(subset=["glac_id"])
            .drop(columns=["_year"])
            .set_index("glac_id")
        )
        print(f"\n      Attribute lookup : {len(poly_attrs):,} unique glaciers")

else:
    print("      No polygon data found.")


# ══════════════════════════════════════════════════════════════════════════════
# LAYER 2 — POINTS
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'─'*70}")
print("  [LAYER 2/4]  glims_points  →  points/")
print(f"{'─'*70}")

pts = load_layer("glims_points", HEMI_DIRS, sample=args.sample)

if pts is not None:
    pts = pts.reset_index(drop=True)

    # Join polygon DBF attributes onto points via glac_id
    if poly_attrs is not None and "glac_id" in pts.columns:
        join_cols = [c for c in poly_attrs.columns if c not in pts.columns]
        if join_cols:
            pts = pts.join(poly_attrs[join_cols], on="glac_id", how="left")
            print(f"      Joined {len(join_cols)} polygon-DBF cols onto points")

    # Derived convenience columns
    if "src_date" in pts.columns:
        pts["obs_year"]   = pts["src_date"].apply(extract_year)
        pts["obs_decade"] = pts["obs_year"].apply(
            lambda y: f"{(int(y)//10)*10}s" if y else None)

    for col, alias in [("area",     "area_km2"),
                       ("mean_elev","mean_elev_m"),
                       ("min_elev", "min_elev_m"),
                       ("max_elev", "max_elev_m"),
                       ("length",   "length_km"),
                       ("width",    "width_km")]:
        if col in pts.columns:
            pts[alias] = pd.to_numeric(pts[col], errors="coerce").round(4)

    p = os.path.join(args.out, "points", "glacier_points.geojson")
    # For points: keep only the 5 base columns (no gone_date for points)
    write_geojson(pts, p,
                  "Glacier center points (N+S)",
                  keep_cols=KEEP_COLS_BASE)
    write_analysis(pts,
                   os.path.join(args.out, "analysis", "glacier_points_analysis.txt"),
                   "Glacier center points")
else:
    print("      No points data found.")


# ══════════════════════════════════════════════════════════════════════════════
# LAYER 3 — LINES
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'─'*70}")
print("  [LAYER 3/4]  glims_lines  →  lines/")
print(f"{'─'*70}")

lines = load_layer("glims_lines", HEMI_DIRS, sample=args.sample)
if lines is not None and len(lines) > 0:
    lines = lines.reset_index(drop=True)
    if poly_attrs is not None and "glac_id" in lines.columns:
        join_cols = [c for c in poly_attrs.columns if c not in lines.columns]
        if join_cols:
            lines = lines.join(poly_attrs[join_cols], on="glac_id", how="left")
    p = os.path.join(args.out, "lines", "glacier_lines.geojson")
    write_geojson(lines, p, "Glacier line features (N+S merged)")
    write_analysis(lines,
                   os.path.join(args.out,"analysis","glacier_lines_analysis.txt"),
                   "Glacier lines")
else:
    print("      No lines data found.")


# ══════════════════════════════════════════════════════════════════════════════
# LAYER 4 — IMAGES
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'─'*70}")
print("  [LAYER 4/4]  glims_images  →  images/")
print(f"{'─'*70}")

imgs = load_layer("glims_images", HEMI_DIRS, sample=args.sample)
if imgs is not None and len(imgs) > 0:
    imgs = imgs.reset_index(drop=True)
    p = os.path.join(args.out, "images", "glacier_images.geojson")
    write_geojson(imgs, p, "Satellite image footprints (N+S merged)")
    write_analysis(imgs,
                   os.path.join(args.out,"analysis","glacier_images_analysis.txt"),
                   "Satellite image footprints")
else:
    print("      No images data found.")


# ══════════════════════════════════════════════════════════════════════════════
# MANIFEST + FINAL SUMMARY
# ══════════════════════════════════════════════════════════════════════════════
with open(os.path.join(args.out, "manifest.json"), "w") as f:
    json.dump(manifest, f, indent=2)

total_features = sum(v["features"] for v in manifest["files"].values())
total_mb       = sum(v["size_mb"]  for v in manifest["files"].values())
elapsed        = time.time() - t0

print(f"\n{SEP}")
print(f"  Done!  Elapsed: {elapsed:.0f}s  ({elapsed/60:.1f} min)")
print(SEP)
print(f"\n  Output     : {args.out}")
print(f"  Files      : {len(manifest['files'])}  |  "
      f"Features: {total_features:,}  |  Total: {total_mb:.1f} MB\n")

current_sub = None
for fname, info in sorted(manifest["files"].items()):
    sub = fname.split("/")[0]
    if sub != current_sub:
        print(f"\n  [{sub}/]")
        current_sub = sub
    print(f"    {os.path.basename(fname):<54} "
          f"{info['features']:>8,} features  {info['size_mb']:6.1f} MB")

print(f"\n  Diagnostics → {os.path.join(args.out,'analysis')}/")
print(f"  Manifest    → {os.path.join(args.out,'manifest.json')}\n")