"""
build_ref_glacier_db.py
────────────────────────────────────────────────────────────────────────────────
Searches the GLIMS polygon DBF files (north + south) for every glacier in
ref_glacier_names.txt and writes ALL matching records to:
  - ref_glaciers.sqlite   (SQLite database, queryable)
  - ref_glaciers_full.csv (flat CSV of every matched row)
  - ref_glaciers_latest.csv (one row per glacier — most recent observation only)
  - match_report.txt      (which names matched, which didn't, how many rows)

Matching strategy (in order):
  1. Exact match on glac_name (case-insensitive, whitespace-stripped)
  2. Partial / contains match on glac_name
  3. Exact match on glac_id if a GLIMS ID is present in the input CSV

Usage:
    python build_ref_glacier_db.py
    python build_ref_glacier_db.py --names ref_glacier_names.txt
    python build_ref_glacier_db.py --csv refglacierplus.csv --sample 500000
"""

import argparse, os, sys, time, sqlite3, csv, re
import warnings; warnings.filterwarnings("ignore")

# ── CLI ────────────────────────────────────────────────────────────────────────
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_GV_ROOT    = r"C:\Users\aiden\.git\GlacierVisualization"

parser = argparse.ArgumentParser(description="Match WGMS ref glaciers in GLIMS DBF")
parser.add_argument("--north", default=os.path.join(
    _GV_ROOT,
    "local-data",
    "glims_download_north"))
parser.add_argument("--south", default=os.path.join(
    _GV_ROOT,
    "local-data",
    "glims_download_south"))
parser.add_argument("--names", default=os.path.join(_SCRIPT_DIR, "ref_glacier_names.txt"),
                    help="Plain-text file, one glacier name per line")
parser.add_argument("--csv",   default=os.path.join(_SCRIPT_DIR, "refglacierplus.csv"),
                    help="Optional original CSV with GLIMS IDs for bonus matching")
parser.add_argument("--out",   default=_SCRIPT_DIR,
                    help="Output directory (default: same as script)")
parser.add_argument("--sample",type=int, default=None,
                    help="Load only first N rows from each DBF (for quick testing)")
args = parser.parse_args()

# ── IMPORTS ────────────────────────────────────────────────────────────────────
try:
    import pandas as pd
    import numpy as np
except ImportError:
    sys.exit("ERROR: pip install pandas numpy")

SEP = "=" * 66
t0  = time.time()

print(f"\n{SEP}")
print("  GLIMS DBF → Reference Glacier Database Builder")
print(SEP)

# ── LOAD REFERENCE NAMES ───────────────────────────────────────────────────────
print(f"\n[1/5] Loading reference glacier names from {args.names} ...")

if not os.path.exists(args.names):
    sys.exit(f"ERROR: names file not found: {args.names}")

with open(args.names, encoding="utf-8") as f:
    raw_names = [ln.strip() for ln in f if ln.strip()]

print(f"      {len(raw_names)} glacier names loaded")

# Also load GLIMS IDs from the original CSV if available (bonus matching)
glims_id_map = {}   # name (upper) → glims_id
if os.path.exists(args.csv):
    with open(args.csv, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            gid = row.get("Glims_id","").strip()
            if gid:
                glims_id_map[row["Name"].strip().upper()] = gid
    print(f"      {len(glims_id_map)} GLIMS IDs loaded from {os.path.basename(args.csv)}")

# Normalise names for matching
def norm(s):
    """Lower-case, strip accents where possible, collapse whitespace."""
    s = str(s).strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s

name_norms    = {norm(n): n for n in raw_names}   # normalised → original
name_set      = set(name_norms.keys())
glims_id_set  = set(glims_id_map.values())

# ── LOAD GLIMS DBF FILES ───────────────────────────────────────────────────────
print(f"\n[2/5] Loading GLIMS DBF files ...")

ENCODINGS = ["latin-1", "windows-1252", "utf-8", "iso-8859-1"]

def load_dbf(path, sample=None):
    """Load a .dbf file with auto-encoding detection."""
    if not os.path.exists(path):
        print(f"      NOT FOUND: {path}")
        return None
    try:
        from simpledbf import Dbf5
    except ImportError:
        sys.exit("ERROR: pip install simpledbf")

    for enc in ENCODINGS:
        try:
            dbf = Dbf5(path, codec=enc)
            df  = dbf.to_dataframe()
            if sample:
                df = df.iloc[:sample]
            # Quick validation
            if df.select_dtypes(include="object").apply(
                    lambda c: c.str.contains("\ufffd", na=False).mean()
                ).max() < 0.01:
                print(f"      {os.path.basename(path)}: {len(df):,} rows  enc={enc}")
                return df
        except Exception:
            continue
    print(f"      FAILED to decode: {path}")
    return None

# Load polygon DBFs (main source of all attributes)
frames = []
for hemi, folder in [("north", args.north), ("south", args.south)]:
    if not os.path.isdir(folder):
        print(f"      [{hemi}] directory not found: {folder}")
        continue
    dbf_path = os.path.join(folder, "glims_polygons.dbf")
    df = load_dbf(dbf_path, sample=args.sample)
    if df is not None:
        df["_hemi"] = hemi
        frames.append(df)

if not frames:
    sys.exit("ERROR: No GLIMS DBF files could be loaded.")

glims = pd.concat(frames, ignore_index=True)
print(f"      Combined: {len(glims):,} total rows")

# Clean strings
str_cols = glims.select_dtypes(include="object").columns
glims[str_cols] = glims[str_cols].apply(lambda c: c.str.strip())

# Filter to glac_bound only (keeps one polygon type, reduces noise)
if "line_type" in glims.columns:
    before = len(glims)
    glims  = glims[glims["line_type"].str.lower() == "glac_bound"].reset_index(drop=True)
    print(f"      After glac_bound filter: {len(glims):,} rows ({before-len(glims):,} removed)")

# Parse year from src_date
def extract_year(raw):
    s = str(raw).strip().split(".")[0].replace("-","")
    s = "".join(c for c in s if c.isdigit())[:8]
    if len(s) < 4: return None
    try: return int(pd.to_datetime(s.ljust(8,"0"), format="%Y%m%d").year)
    except: return None

glims["_year"] = glims["src_date"].apply(extract_year) if "src_date" in glims.columns else None

# ── MATCH ──────────────────────────────────────────────────────────────────────
print(f"\n[3/5] Matching reference glaciers in GLIMS ...")

if "glac_name" not in glims.columns:
    sys.exit("ERROR: 'glac_name' column not found in GLIMS DBF.")

glims["_name_norm"] = glims["glac_name"].apply(norm)

matched_frames = []
match_log      = []   # list of dicts for the report

for orig_name in raw_names:
    n = norm(orig_name)
    glims_id = glims_id_map.get(orig_name.upper(), "")

    # Strategy 1: exact normalised name match
    mask = glims["_name_norm"] == n
    rows = glims[mask]

    # Strategy 2: exact match dropping trailing content in parentheses
    #   e.g. "COLUMBIA (2057)" → try matching "columbia"
    if len(rows) == 0:
        base = re.sub(r"\s*\(.*\)\s*$", "", n).strip()
        if base != n:
            mask = glims["_name_norm"] == base
            rows = glims[mask]
            if len(rows) > 0:
                match_log.append({"name": orig_name, "strategy": "strip-parens",
                                   "rows": len(rows), "ids": rows["glac_id"].unique().tolist()[:5]})

    # Strategy 3: partial/contains match (glims name contains the search name)
    if len(rows) == 0:
        mask = glims["_name_norm"].str.contains(re.escape(n), na=False)
        rows = glims[mask]
        if len(rows) > 0:
            match_log.append({"name": orig_name, "strategy": "contains",
                               "rows": len(rows), "ids": rows["glac_id"].unique().tolist()[:5]})

    # Strategy 4: GLIMS ID match (most precise — overrides everything)
    if glims_id and "glac_id" in glims.columns:
        id_rows = glims[glims["glac_id"] == glims_id]
        if len(id_rows) > 0:
            rows = id_rows
            match_log.append({"name": orig_name, "strategy": "glims-id",
                               "rows": len(rows), "ids": [glims_id]})

    if len(rows) > 0:
        rows = rows.copy()
        rows["_ref_name"] = orig_name           # preserve the reference name
        rows["_glims_id_input"] = glims_id      # preserve any input GLIMS ID
        matched_frames.append(rows)
        if not any(m["name"] == orig_name for m in match_log):
            match_log.append({"name": orig_name, "strategy": "exact",
                               "rows": len(rows), "ids": rows["glac_id"].unique().tolist()[:5]})
    else:
        match_log.append({"name": orig_name, "strategy": "NO MATCH", "rows": 0, "ids": []})

# Merge all matched rows
if not matched_frames:
    sys.exit("ERROR: No glaciers matched. Check name normalisation or DBF content.")

matched = pd.concat(matched_frames, ignore_index=True)
n_matched_glaciers = sum(1 for m in match_log if m["strategy"] != "NO MATCH")
n_no_match         = sum(1 for m in match_log if m["strategy"] == "NO MATCH")

print(f"      Matched    : {n_matched_glaciers} / {len(raw_names)} glaciers")
print(f"      No match   : {n_no_match}")
print(f"      Total rows : {len(matched):,}")

# ── BUILD OUTPUTS ──────────────────────────────────────────────────────────────
print(f"\n[4/5] Writing outputs to {args.out}/ ...")

# Drop helper columns before saving
HELPER = {"_name_norm", "_hemi"}
save_cols = [c for c in matched.columns if c not in HELPER]
out_df    = matched[save_cols].copy()

# Fix NaN for serialisation
for col in out_df.select_dtypes(include=["float64","float32"]).columns:
    out_df[col] = out_df[col].where(out_df[col].notna(), other=None)

# ── 1. Full CSV ────────────────────────────────────────────────────────────────
full_csv = os.path.join(args.out, "ref_glaciers_full.csv")
out_df.to_csv(full_csv, index=False, encoding="utf-8")
print(f"      ✓ ref_glaciers_full.csv          {len(out_df):>7,} rows  "
      f"{os.path.getsize(full_csv)/1024:.0f} KB")

# ── 2. Latest-only CSV ─────────────────────────────────────────────────────────
# One row per glacier: the most recent src_date observation
if "_year" in out_df.columns and "glac_id" in out_df.columns:
    latest = (out_df.sort_values("_year", ascending=False, na_position="last")
                    .drop_duplicates(subset=["glac_id"])
                    .sort_values(["_ref_name","_year"]))
else:
    latest = out_df.drop_duplicates(subset=["glac_id"])

latest_csv = os.path.join(args.out, "ref_glaciers_latest.csv")
latest.to_csv(latest_csv, index=False, encoding="utf-8")
print(f"      ✓ ref_glaciers_latest.csv        {len(latest):>7,} rows  "
      f"{os.path.getsize(latest_csv)/1024:.0f} KB")

# ── 3. SQLite database ─────────────────────────────────────────────────────────
db_path = os.path.join(args.out, "ref_glaciers.sqlite")
if os.path.exists(db_path):
    os.remove(db_path)

conn = sqlite3.connect(db_path)

# Main table: all polygon records
out_df.to_sql("glims_all", conn, if_exists="replace", index=False)

# Latest table: one row per glacier
latest.to_sql("glims_latest", conn, if_exists="replace", index=False)

# Reference list table (original input)
ref_list = pd.read_csv(args.csv, encoding="utf-8-sig") if os.path.exists(args.csv) else \
           pd.DataFrame({"Name": raw_names})
ref_list.to_sql("ref_list", conn, if_exists="replace", index=False)

# Match log table
match_df = pd.DataFrame(match_log)
match_df["ids"] = match_df["ids"].apply(str)  # serialise list
match_df.to_sql("match_log", conn, if_exists="replace", index=False)

# Useful indices
conn.execute("CREATE INDEX IF NOT EXISTS idx_glac_id   ON glims_all(glac_id)")
conn.execute("CREATE INDEX IF NOT EXISTS idx_ref_name  ON glims_all(_ref_name)")
conn.execute("CREATE INDEX IF NOT EXISTS idx_year      ON glims_all(_year)")
conn.execute("CREATE INDEX IF NOT EXISTS idx_lat_gl    ON glims_latest(glac_id)")
conn.commit()
conn.close()

print(f"      ✓ ref_glaciers.sqlite            4 tables  "
      f"{os.path.getsize(db_path)/1024:.0f} KB")
print(f"        Tables: glims_all · glims_latest · ref_list · match_log")

# ── 4. Match report ────────────────────────────────────────────────────────────
report_path = os.path.join(args.out, "match_report.txt")
W = 66
lines = []
lines.append("=" * W)
lines.append("  GLIMS Reference Glacier Match Report")
lines.append(f"  Generated: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
lines.append("=" * W)
lines.append(f"\n  Input names   : {len(raw_names)}")
lines.append(f"  Matched       : {n_matched_glaciers}")
lines.append(f"  No match      : {n_no_match}")
lines.append(f"  Total rows    : {len(out_df):,}")
lines.append(f"  Unique glac_id: {out_df['glac_id'].nunique() if 'glac_id' in out_df.columns else '—'}")

lines.append(f"\n{'─'*W}")
lines.append("  MATCHED GLACIERS")
lines.append(f"{'─'*W}")
for m in match_log:
    if m["strategy"] != "NO MATCH":
        ids_str = ", ".join(m["ids"][:3]) + ("..." if len(m["ids"]) > 3 else "")
        lines.append(f"  ✓  {m['name']:<35} {m['rows']:>4} rows  [{m['strategy']}]")
        if m["ids"]:
            lines.append(f"     glac_id(s): {ids_str}")

lines.append(f"\n{'─'*W}")
lines.append("  UNMATCHED GLACIERS")
lines.append(f"{'─'*W}")
unmatched = [m for m in match_log if m["strategy"] == "NO MATCH"]
if unmatched:
    for m in unmatched:
        lines.append(f"  ✗  {m['name']}")
    lines.append(f"\n  Tip: unmatched names are likely stored differently in GLIMS.")
    lines.append(f"  Try searching glims.org/maps/glims for the correct glac_name.")
else:
    lines.append("  All glaciers matched.")

lines.append(f"\n{'─'*W}")
lines.append("  OUTPUT FILES")
lines.append(f"{'─'*W}")
lines.append(f"  ref_glaciers_full.csv    — all polygon records ({len(out_df):,} rows)")
lines.append(f"  ref_glaciers_latest.csv  — one row per glacier ({len(latest):,} rows)")
lines.append(f"  ref_glaciers.sqlite      — SQLite, 4 tables")
lines.append(f"  match_report.txt         — this file")

lines.append(f"\n{'─'*W}")
lines.append("  QUICK QUERY EXAMPLES (SQLite)")
lines.append(f"{'─'*W}")
lines.append("""
  -- All records for a specific glacier
  SELECT * FROM glims_all WHERE _ref_name = 'STORGLACIÄREN';

  -- Latest outline per glacier with area
  SELECT _ref_name, glac_id, src_date, area, min_elev, max_elev
  FROM glims_latest ORDER BY area DESC;

  -- Glaciers with most observations
  SELECT _ref_name, COUNT(*) as n_obs
  FROM glims_all GROUP BY _ref_name ORDER BY n_obs DESC;

  -- Year coverage per glacier
  SELECT _ref_name, MIN(_year) as first, MAX(_year) as last,
         MAX(_year)-MIN(_year) as span, COUNT(*) as n_obs
  FROM glims_all WHERE _year IS NOT NULL
  GROUP BY _ref_name ORDER BY span DESC;

  -- Check match log
  SELECT name, strategy, rows FROM match_log ORDER BY rows DESC;
""")

lines.append("=" * W + "\n")

with open(report_path, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print(f"      ✓ match_report.txt")

# ── SUMMARY ────────────────────────────────────────────────────────────────────
print(f"\n[5/5] Summary")
print(f"{'─'*66}")

# Print a quick per-glacier row count table
conn2 = sqlite3.connect(db_path)
cur   = conn2.cursor()
cur.execute("SELECT _ref_name, COUNT(*) as n, MIN(_year), MAX(_year) "
            "FROM glims_all GROUP BY _ref_name ORDER BY _ref_name")
rows = cur.fetchall()
conn2.close()

print(f"  {'Glacier':<36} {'Rows':>5}  {'First':>5}  {'Last':>5}")
print(f"  {'─'*36}  {'─'*5}  {'─'*5}  {'─'*5}")
for ref_name, n, yr_min, yr_max in rows:
    yr_min = str(yr_min) if yr_min else "  ?"
    yr_max = str(yr_max) if yr_max else "  ?"
    print(f"  {ref_name:<36} {n:>5}  {yr_min:>5}  {yr_max:>5}")

print(f"\n{SEP}")
print(f"  Done!  {n_matched_glaciers}/{len(raw_names)} glaciers matched  "
      f"|  {len(out_df):,} total rows  |  {time.time()-t0:.0f}s")
print(f"  Output: {args.out}")
print(SEP + "\n")
