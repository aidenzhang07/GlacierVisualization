from flask import Flask, abort, jsonify, render_template, request
import sqlite3
import os
import sys
import re
import json
import urllib.parse
import urllib.request
from urllib.error import HTTPError, URLError
from dotenv import load_dotenv

app = Flask(__name__)
DB_PATH = "glacier.db"
ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")

# Load environment variables from .env (if present)
load_dotenv(ENV_PATH)

# Remote data config. If you have moved assets to anonymous Azure Blob Storage,
# set AZURE_BLOB_BASE_URL to the container root.
BLOB_BASE_URL = os.getenv("AZURE_BLOB_BASE_URL", "https://glacierdata.blob.core.windows.net/glacieriq").rstrip("/")
GLACIER_MANIFEST_URL = os.getenv("GLACIER_MANIFEST_URL") or (
    f"{BLOB_BASE_URL}/glacier_data/manifest.json" if BLOB_BASE_URL else None
)
MBTILES_URL = os.getenv("MBTILES_URL") or (
    f"{BLOB_BASE_URL}/static/glacier.mbtiles" if BLOB_BASE_URL else None
)
REMOTE_TIF_YEARS = os.getenv("REMOTE_TIF_YEARS")

# Cache root for GeoTIFFs — local fallback only, blob-hosted files are preferred
CACHE_ROOT = os.path.join(os.path.dirname(__file__), "tifcache")

KNOWN_GLACIERS = {
    "Athabasca": {
        "latitude": 52.2159,
        "longitude": -117.2187,
        "country": "Canada",
        "description": "The Athabasca Glacier is a rapidly retreating glacier in the Canadian Rockies.",
        "bbox": [-117.29, 52.17, -117.15, 52.26],
        "glacier_id": "G242719E52168N",
    },
    "Rhône": {
        "latitude": 46.5419,
        "longitude": 8.4531,
        "country": "Switzerland",
        "description": "The Rhône Glacier feeds the Rhône River and has lost significant mass over the last decades.",
        "bbox": [8.37, 46.50, 8.52, 46.58],
        "glacier_id": "G008398E46623N",
    },
    "Perito Moreno": {
        "latitude": -50.4960,
        "longitude": -73.0526,
        "country": "Argentina",
        "description": "Perito Moreno is one of the few Patagonian glaciers that is still advancing, though it is thinning.",
        "bbox": [-73.13, -50.53, -72.98, -50.44],
        "glacier_id": "G286789E50565S",
    },
    # Mount Rainier as a glacier entry for 3D viewing
    "Mt Rainier": {
        "latitude": 46.8523,
        "longitude": -121.7603,
        "country": "USA",
        "description": "Mount Rainier hosts 25+ named glaciers — the most heavily glaciated peak in the contiguous US.",
        "bbox": [-121.85, 46.80, -121.65, 46.92],
        "glacier_id": None,  # No specific glacier ID for Mt Rainier as a whole
    },
}

# Map glacier names to their glacier IDs for highlighting
GLACIER_ID_MAPPING = {
    "Athabasca": "G242719E52168N",
    "Rhône": "G008398E46623N",
    "Perito Moreno": "G286789E50565S",
}

GLACIER_MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "glacier_data", "manifest.json")


def _is_url(value):
    return isinstance(value, str) and value.startswith(("http://", "https://"))


def _join_url(base, path):
    if not base:
        return None
    return urllib.parse.urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def _load_json_from_url(url):
    try:
        with urllib.request.urlopen(url) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return json.loads(response.read().decode(charset))
    except Exception as e:
        print(f"Error loading JSON from {url}: {e}", file=sys.stderr)
        return None


def _load_binary_from_url(url):
    try:
        with urllib.request.urlopen(url) as response:
            return response.read()
    except Exception as e:
        print(f"Error loading binary data from {url}: {e}", file=sys.stderr)
        return None


def _remote_file_exists(url):
    if not url:
        return False
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req) as response:
            return response.status < 400
    except HTTPError as e:
        if e.code == 405:
            try:
                with urllib.request.urlopen(url) as response:
                    return response.status < 400
            except Exception:
                return False
        return False
    except URLError:
        return False
    except ValueError:
        return False


def _parse_years_list(value):
    years = set()
    for part in (value or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                start, end = part.split("-", 1)
                years.update(range(int(start), int(end) + 1))
            except ValueError:
                continue
        else:
            try:
                years.add(int(part))
            except ValueError:
                continue
    return sorted(years)


def _remote_glacier_data_url(path):
    if not BLOB_BASE_URL:
        return None
    return _join_url(BLOB_BASE_URL, f"glacier_data/{path}")


def _remote_tiff_url(glacier_name, filename):
    if not BLOB_BASE_URL:
        return None
    encoded_name = urllib.parse.quote(glacier_name, safe="")
    encoded_file = urllib.parse.quote(filename, safe="")
    return _join_url(BLOB_BASE_URL, f"tifcache/{encoded_name}/{encoded_file}")


def load_glacier_manifest():
    if _is_url(GLACIER_MANIFEST_URL):
        manifest = _load_json_from_url(GLACIER_MANIFEST_URL)
        if manifest is not None:
            return manifest
    if os.path.exists(GLACIER_MANIFEST_PATH):
        with open(GLACIER_MANIFEST_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    if GLACIER_MANIFEST_URL:
        manifest = _load_json_from_url(GLACIER_MANIFEST_URL)
        if manifest is not None:
            return manifest
    return {"glaciers": {}}


GLACIER_MANIFEST = load_glacier_manifest()
GLACIER_IDS = set(GLACIER_MANIFEST.get("glaciers", {}).keys())


def get_glacier_manifest_entry(glac_id: str):
    return GLACIER_MANIFEST.get("glaciers", {}).get(glac_id)


def init_db():
    """Create the database and seed it with sample glacier data."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS glacier_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            year INTEGER NOT NULL,
            glacier_name TEXT NOT NULL,
            thickness_meters REAL NOT NULL
        )
    """)

    # Check if data already exists
    cursor.execute("SELECT COUNT(*) FROM glacier_data")
    if cursor.fetchone()[0] == 0:
        # Sample data: thickness of 3 famous glaciers over the decades
        sample_data = [
            # Athabasca Glacier (Canada)
            (1950, "Athabasca", 300.0),
            (1960, "Athabasca", 287.0),
            (1970, "Athabasca", 271.0),
            (1980, "Athabasca", 258.0),
            (1990, "Athabasca", 242.0),
            (2000, "Athabasca", 225.0),
            (2010, "Athabasca", 203.0),
            (2020, "Athabasca", 180.0),
            (2023, "Athabasca", 171.0),

            # Rhône Glacier (Switzerland)
            (1950, "Rhône",     250.0),
            (1960, "Rhône",     238.0),
            (1970, "Rhône",     224.0),
            (1980, "Rhône",     210.0),
            (1990, "Rhône",     194.0),
            (2000, "Rhône",     176.0),
            (2010, "Rhône",     154.0),
            (2020, "Rhône",     130.0),
            (2023, "Rhône",     121.0),

            # Perito Moreno (Argentina)
            (1950, "Perito Moreno", 700.0),
            (1960, "Perito Moreno", 695.0),
            (1970, "Perito Moreno", 688.0),
            (1980, "Perito Moreno", 680.0),
            (1990, "Perito Moreno", 673.0),
            (2000, "Perito Moreno", 665.0),
            (2010, "Perito Moreno", 652.0),
            (2020, "Perito Moreno", 640.0),
            (2023, "Perito Moreno", 635.0),
        ]
        cursor.executemany(
            "INSERT INTO glacier_data (year, glacier_name, thickness_meters) VALUES (?, ?, ?)",
            sample_data
        )
        print("✅ Database seeded with sample glacier data.")

    conn.commit()
    conn.close()


@app.route("/")
@app.route("/map")
def index():
    return render_template(
        "index.html",
        glacier_markers=KNOWN_GLACIERS,
        mbtiles_tile_url="/api/tiles/{z}/{x}/{y}.pbf",
    )


# ── MBTiles tile proxy ────────────────────────────────────────────────────────
_mbtiles_conn = None


def _get_mbtiles_conn():
    """Return a sqlite3 connection to the MBTiles file.

    Strategy:
      1. Check for a local static/glacier.mbtiles file first.
      2. Otherwise fetch from blob storage into a temp file and open it.
    """
    global _mbtiles_conn
    if _mbtiles_conn is not None:
        return _mbtiles_conn

    local_path = os.path.join(os.path.dirname(__file__), "static", "glacier.mbtiles")
    if os.path.exists(local_path):
        _mbtiles_conn = sqlite3.connect(local_path, check_same_thread=False)
        return _mbtiles_conn

    if MBTILES_URL:
        import tempfile
        print(f"Fetching MBTiles from {MBTILES_URL} ...", file=sys.stderr)
        data = _load_binary_from_url(MBTILES_URL)
        if data:
            tmp = tempfile.NamedTemporaryFile(suffix=".mbtiles", delete=False)
            tmp.write(data)
            tmp.flush()
            tmp.close()
            _mbtiles_conn = sqlite3.connect(tmp.name, check_same_thread=False)
            return _mbtiles_conn

    return None


@app.route("/api/tiles/<int:z>/<int:x>/<int:y>.pbf")
def mbtiles_tile(z, x, y):
    """Serve a single vector tile from the MBTiles file."""
    from flask import Response
    conn = _get_mbtiles_conn()
    if conn is None:
        abort(503)

    # MBTiles uses TMS y-axis (flipped vs XYZ)
    tms_y = (1 << z) - 1 - y

    try:
        row = conn.execute(
            "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
            (z, x, tms_y),
        ).fetchone()
    except sqlite3.Error as e:
        print(f"MBTiles query error: {e}", file=sys.stderr)
        abort(500)

    if row is None:
        abort(404)

    return Response(
        row[0],
        status=200,
        headers={
            "Content-Type": "application/x-protobuf",
            "Content-Encoding": "gzip",
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "public, max-age=86400",
        },
    )


@app.route("/about")
def about():
    return render_template("about.html")

@app.route("/api/glacier-info")
def glacier_info_api():
    """Return all known glaciers with their bbox for the frontend."""
    return jsonify(KNOWN_GLACIERS)


@app.route("/api/glaciers")
def get_glaciers():
    """Return all glacier data from the database as JSON."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # lets us access columns by name
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM glacier_data ORDER BY glacier_name, year")
    rows = cursor.fetchall()
    conn.close()

    data = [dict(row) for row in rows]
    return jsonify(data)


@app.route("/api/glaciers/<glacier_name>")
def get_glacier(glacier_name):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM glacier_data WHERE glacier_name = ? ORDER BY year",
        (glacier_name,)
    )
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return jsonify({"error": "Glacier not found"}), 404

    return jsonify([dict(row) for row in rows])


@app.route("/glacier/<glacier_name>")
def glacier_detail(glacier_name):
    glacier_info = KNOWN_GLACIERS.get(glacier_name)
    if not glacier_info:
        abort(404)
    return render_template(
        "detail.html",
        glacier_name=glacier_name,
        glacier_info=glacier_info
    )


@app.route("/api/glacier-manifest")
def glacier_manifest_api():
    # Return ALL glacier IDs from the manifest (glaciers with multiple observations)
    # These should be highlighted in magenta on the map
    import sys
    print(f"DEBUG: Returning {len(GLACIER_IDS)} glacier IDs from manifest", file=sys.stderr)
    
    # Return all glacier IDs sorted
    return jsonify({"glacier_ids": sorted(list(GLACIER_IDS))})

@app.route("/api/highlighted-glaciers-geojson")
def highlighted_glaciers_geojson():
    """Return GeoJSON for only the highlighted glaciers"""
    features = []
    
    for glacier_id in GLACIER_ID_MAPPING.values():
        if not glacier_id:
            continue
            
        # Get the manifest entry
        entry = get_glacier_manifest_entry(glacier_id)
        if not entry:
            continue
            
        local_path = os.path.join(os.path.dirname(__file__), "glacier_data", entry["path"])
        remote_url = _remote_glacier_data_url(entry["path"])
        glacier_data = None

        if os.path.exists(local_path):
            try:
                with open(local_path, 'r', encoding='utf-8') as f:
                    glacier_data = json.load(f)
            except Exception as e:
                print(f"Error loading {local_path}: {e}", file=sys.stderr)
        elif remote_url:
            glacier_data = _load_json_from_url(remote_url)

        if glacier_data and glacier_data.get('features'):
            features.extend(glacier_data['features'])
    
    return jsonify({
        "type": "FeatureCollection",
        "features": features
    })


@app.route("/api/glacier-transition-data/<glac_id>")
def glacier_transition_data(glac_id):
    entry = get_glacier_manifest_entry(glac_id)
    if not entry:
        return jsonify({"error": "Glacier not found"}), 404

    local_path = os.path.join(os.path.dirname(__file__), "glacier_data", entry["path"])
    remote_url = _remote_glacier_data_url(entry["path"])
    data = None

    if os.path.exists(local_path):
        with open(local_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    elif remote_url:
        data = _load_json_from_url(remote_url)

    if not data:
        return jsonify({"error": "Glacier data file missing"}), 404

    years = set()
    for feature in data.get("features", []):
        src_date = feature.get("properties", {}).get("src_date")
        if isinstance(src_date, str) and len(src_date) >= 4:
            years.add(src_date[:4])

    data["available_years"] = sorted(years)
    return jsonify(data)


@app.route("/glacier-transition/<glac_id>")
def glacier_transition(glac_id):
    if glac_id not in GLACIER_IDS:
        abort(404)
    return render_template("transition.html", glac_id=glac_id)


# ── GeoTIFF 3D viewer (standalone page) ──────────────────────────────
@app.route("/geotiff-viewer")
def geotiff_viewer():
    return render_template("geotiff_viewer.html")


# ── Glacier imagery APIs (multi-glacier, on-demand EE download) ─────
def _glacier_cache_dir(glacier_name):
    """Return the cache folder for a glacier (e.g. tifcache/Athabasca/)."""
    safe = re.sub(r"[^A-Za-z0-9_\-]", "_", glacier_name)
    return os.path.join(CACHE_ROOT, safe)


def _available_remote_years_for_glacier(glacier_name):
    if REMOTE_TIF_YEARS:
        return _parse_years_list(REMOTE_TIF_YEARS)

    years = []
    for year in range(1984, 2025):
        remote_url = _remote_tiff_url(glacier_name, f"{year}.tif")
        if remote_url and _remote_file_exists(remote_url):
            years.append(year)
    return years


def _available_years_from_cache(glacier_name, allow_remote=False):
    """List available TIFF years from blob storage only."""
    if allow_remote and BLOB_BASE_URL:
        return _available_remote_years_for_glacier(glacier_name)
    return []


# Deprecated: GeoTIFFs are now pre-generated and stored in Azure Blob Storage under tifcache/<glacier_name>/.
# The local cache is only a fallback for any files already present on disk.


@app.route("/api/glacier-image/years/<glacier_name>")
def glacier_image_years(glacier_name):
    """Return the list of years available for a glacier (cached + remote blob fallback)."""
    if glacier_name not in KNOWN_GLACIERS:
        return jsonify({"error": "Unknown glacier"}), 404

    years = _available_years_from_cache(glacier_name, allow_remote=True)
    return jsonify({"glacier": glacier_name, "years": years, "complete": len(years) > 0})


@app.route("/api/glacier-image/download/<glacier_name>", methods=["POST"])
def glacier_image_download(glacier_name):
    """Deprecated: remote TIFFs are pre-generated in Azure Blob Storage."""
    if glacier_name not in KNOWN_GLACIERS:
        return jsonify({"error": "Unknown glacier"}), 404
    return jsonify({
        "error": "Download not supported. Use blob-hosted TIFFs in tifcache/ instead.",
        "glacier": glacier_name,
    }), 400


@app.route("/api/glacier-image/status/<glacier_name>")
def glacier_image_status(glacier_name):
    """Deprecated: download status is no longer available."""
    if glacier_name not in KNOWN_GLACIERS:
        return jsonify({"error": "Unknown glacier"}), 404
    years = _available_years_from_cache(glacier_name, allow_remote=True)
    return jsonify({
        "glacier": glacier_name,
        "downloading": False,
        "years_ready": years,
        "complete": len(years) > 0,
    })


@app.route("/api/glacier-image/tile/<glacier_name>/<int:year>")
def glacier_image_tile(glacier_name, year):
    """Return a single year's RGB + elevation as base64 PNG for the 3D viewer."""
    import numpy as np
    import tifffile
    from PIL import Image
    import io, base64

    if glacier_name not in KNOWN_GLACIERS:
        return jsonify({"error": "Unknown glacier"}), 404

    remote_tif_url = _remote_tiff_url(glacier_name, f"{year}.tif")
    remote_dem_url = _remote_tiff_url(glacier_name, "dem.tif")

    def _open_remote_tiff(remote_url):
        if not remote_url:
            return None
        data = _load_binary_from_url(remote_url)
        if data is None:
            return None
        try:
            arr = tifffile.imread(io.BytesIO(data))
            arr = np.asarray(arr).astype(np.float32)
            if arr.ndim == 3 and arr.shape[0] == 3:
                # shape is (bands, height, width)
                arr = np.transpose(arr, (1, 2, 0))
            if arr.ndim == 3 and arr.shape[2] == 3:
                return arr
            if arr.ndim == 2:
                return arr
            if arr.ndim == 3 and arr.shape[2] == 4:
                return arr[..., :3]
            return arr
        except Exception as e:
            print(f"Error reading GeoTIFF from {remote_url}: {e}", file=sys.stderr)
            return None

    def _open_rgb_image(remote_url=None):
        rgb = _open_remote_tiff(remote_url)
        if rgb is None:
            return None
        if rgb.ndim == 3 and rgb.shape[2] >= 3:
            return [rgb[..., 0], rgb[..., 1], rgb[..., 2]]
        return None

    def _open_dem(remote_url=None):
        dem = _open_remote_tiff(remote_url)
        if dem is None:
            return None
        if dem.ndim == 2:
            return np.nan_to_num(dem, nan=0)
        if dem.ndim == 3 and dem.shape[2] == 1:
            return np.nan_to_num(dem[..., 0], nan=0)
        return None

    bands = _open_rgb_image(remote_tif_url)
    if bands is None:
        return jsonify({"error": f"No remote GeoTIFF image for {glacier_name} {year}"}), 404

    r, g, b = bands
    elevation = _open_dem(remote_dem_url)
    has_dem = elevation is not None

    def _normalize(band):
        band = np.nan_to_num(band, nan=0)
        nonzero = band[band > 0]
        lo, hi = (np.percentile(nonzero, (2, 98)) if len(nonzero) > 10
                  else (0, 1))
        band = np.clip((band - lo) / (hi - lo + 1e-8), 0, 1)
        return (band * 255).astype(np.uint8)

    r, g, b = _normalize(r), _normalize(g), _normalize(b)
    rgb = np.stack([r, g, b], axis=-1)

    h, w = rgb.shape[:2]
    max_dim = 512
    if max(h, w) > max_dim:
        from skimage.transform import resize
        new_h = max_dim if h > w else int(h * max_dim / w)
        new_w = max_dim if w > h else int(w * max_dim / h)
        rgb = resize(rgb, (new_h, new_w), preserve_range=True, anti_aliasing=True).astype(np.uint8)
        if has_dem:
            elevation = resize(elevation, (new_h, new_w), preserve_range=True, anti_aliasing=True)

    img = Image.fromarray(rgb)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()

    result = {
        "year": year,
        "glacier": glacier_name,
        "width": img.width,
        "height": img.height,
        "image": f"data:image/png;base64,{b64}",
        "hasDem": has_dem,
    }

    if has_dem:
        elev_lo = float(np.percentile(elevation[elevation > 0], 2)) if np.any(elevation > 0) else 0.0
        elev_hi = float(np.percentile(elevation[elevation > 0], 98)) if np.any(elevation > 0) else 1.0
        elevation_norm = np.clip((elevation - elev_lo) / (elev_hi - elev_lo + 1e-8), 0, 1)
        elev_img = Image.fromarray((elevation_norm * 255).astype(np.uint8), mode="L")
        elev_buf = io.BytesIO()
        elev_img.save(elev_buf, format="PNG")
        elev_b64 = base64.b64encode(elev_buf.getvalue()).decode()
        result["elevation"] = f"data:image/png;base64,{elev_b64}"
        result["elevMin"] = elev_lo
        result["elevMax"] = elev_hi

    return jsonify(result)


# ── Legacy: keep old Rainier endpoint working ────────────────────────
@app.route("/api/geotiff/years")
def geotiff_years():
    """Return available years (legacy — redirects to Mt Rainier cache)."""
    years = _available_years_from_cache("Mt Rainier", allow_remote=True)
    # Also check the old rainier_yearly_images folder
    old_dir = os.path.join(os.path.dirname(__file__), "rainier_yearly_images")
    if os.path.isdir(old_dir):
        for f in os.listdir(old_dir):
            m = re.search(r"mt_rainier_(\d{4})\.tif", f)
            if m:
                y = int(m.group(1))
                if y not in years:
                    years.append(y)
    return jsonify(sorted(years))

@app.route("/api/geotiff/<int:year>")
def geotiff_image_legacy(year):
    """Legacy endpoint — redirects to Mt Rainier glacier-image tile."""
    return glacier_image_tile("Mt Rainier", year)


if __name__ == "__main__":
    init_db()
    print("🌍 Starting Glacier Tracker...")
    print("👉 Open your browser at: http://localhost:5000")
    app.run(debug=True)
