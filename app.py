from flask import Flask, abort, jsonify, render_template, request
import sqlite3
import os
import threading
import re

app = Flask(__name__)
DB_PATH = "glacier.db"

# Cache root for downloaded GeoTIFFs — organized by glacier name
CACHE_ROOT = os.path.join(os.path.dirname(__file__), "tifcache")

# Track in-progress downloads so we don't duplicate EE calls
_download_locks = {}
_download_lock = threading.Lock()

KNOWN_GLACIERS = {
    "Athabasca": {
        "latitude": 52.2159,
        "longitude": -117.2187,
        "country": "Canada",
        "description": "The Athabasca Glacier is a rapidly retreating glacier in the Canadian Rockies.",
        "bbox": [-117.29, 52.17, -117.15, 52.26],
    },
    "Rhône": {
        "latitude": 46.5419,
        "longitude": 8.4531,
        "country": "Switzerland",
        "description": "The Rhône Glacier feeds the Rhône River and has lost significant mass over the last decades.",
        "bbox": [8.37, 46.50, 8.52, 46.58],
    },
    "Perito Moreno": {
        "latitude": -50.4960,
        "longitude": -73.0526,
        "country": "Argentina",
        "description": "Perito Moreno is one of the few Patagonian glaciers that is still advancing, though it is thinning.",
        "bbox": [-73.13, -50.53, -72.98, -50.44],
    },
    # Mount Rainier as a glacier entry for 3D viewing
    "Mt Rainier": {
        "latitude": 46.8523,
        "longitude": -121.7603,
        "country": "USA",
        "description": "Mount Rainier hosts 25+ named glaciers — the most heavily glaciated peak in the contiguous US.",
        "bbox": [-121.85, 46.80, -121.65, 46.92],
    },
}

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
def index():
    return render_template("index.html", glacier_markers=KNOWN_GLACIERS)

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



# ── GeoTIFF 3D viewer (standalone page) ──────────────────────────────
@app.route("/geotiff-viewer")
def geotiff_viewer():
    return render_template("geotiff_viewer.html")


# ── Glacier imagery APIs (multi-glacier, on-demand EE download) ─────
def _glacier_cache_dir(glacier_name):
    """Return the cache folder for a glacier (e.g. tifcache/Athabasca/)."""
    safe = re.sub(r"[^A-Za-z0-9_\-]", "_", glacier_name)
    return os.path.join(CACHE_ROOT, safe)


def _available_years_from_cache(glacier_name):
    """List years that already exist on disk for a glacier."""
    d = _glacier_cache_dir(glacier_name)
    years = []
    if os.path.isdir(d):
        for f in os.listdir(d):
            m = re.search(r"(\d{4})\.tif$", f)  # matches e.g. 2024.tif
            if m and not f.endswith("_dem.tif"):  # skip DEM files
                years.append(int(m.group(1)))
    return sorted(years)


def _download_for_glacier(glacier_name):
    """
    Download Landsat RGB + SRTM DEM for a glacier using Earth Engine.
    Runs synchronously — only called once per glacier.
    """
    import ee
    import geemap

    ee.Authenticate()
    ee.Initialize(project='aidenglacierviewer')

    bbox = KNOWN_GLACIERS[glacier_name]["bbox"]
    aoi = ee.Geometry.BBox(*bbox)
    out_dir = _glacier_cache_dir(glacier_name)
    os.makedirs(out_dir, exist_ok=True)

    # Download DEM once (SRTM 30m)
    dem_path = os.path.join(out_dir, "dem.tif")
    if not os.path.exists(dem_path):
        print(f"  → Downloading DEM for {glacier_name}...")
        dem = ee.Image("USGS/SRTMGL1_003").clip(aoi)
        geemap.download_ee_image(image=dem, filename=dem_path, region=aoi, scale=30, crs="EPSG:4326")

    # Download yearly Landsat composites (high-summer, low-cloud)
    # Landsat 5 TM: 1984–2012, Landsat 7 ETM+: 1999–present (SLC-off after 2003),
    # Landsat 8 OLI: 2013–present, Landsat 9 OLI-2: 2021–present
    start_year = 1984
    end_year = 2024

    # Pre-compute Landsat 8/9 collection (2013+) — surface reflectance
    l8 = ee.ImageCollection("LANDSAT/LC08/C02/T1_L2")
    l9 = ee.ImageCollection("LANDSAT/LC09/C02/T1_L2")

    # Landsat 7 ETM+ collection (1999+)
    l7 = ee.ImageCollection("LANDSAT/LE07/C02/T1_L2")

    # Landsat 5 TM collection (1984–2012) — TOA reflectance
    l5 = ee.ImageCollection("LANDSAT/LT05/C02/T1_L2")

    for year in range(start_year, end_year + 1):
        out_path = os.path.join(out_dir, f"{year}.tif")
        if os.path.exists(out_path):
            continue  # already cached
        print(f"  → Downloading {glacier_name} {year}...")
        start_date = f"{year}-07-01"
        end_date = f"{year}-09-15"

        col = None
        bands = None
        scale_factor = 0.0000275

        if year >= 2021:
            # Landsat 9 OLI-2
            col = l9.filterBounds(aoi).filterDate(start_date, end_date).filter(ee.Filter.lt("CLOUD_COVER", 15))
            bands = ["SR_B4", "SR_B3", "SR_B2"]
        elif year >= 2013:
            # Landsat 8 OLI
            col = l8.filterBounds(aoi).filterDate(start_date, end_date).filter(ee.Filter.lt("CLOUD_COVER", 15))
            bands = ["SR_B4", "SR_B3", "SR_B2"]
        elif year >= 1999:
            # Landsat 7 ETM+
            col = l7.filterBounds(aoi).filterDate(start_date, end_date).filter(ee.Filter.lt("CLOUD_COVER", 15))
            bands = ["SR_B3", "SR_B2", "SR_B1"]
        else:
            # Landsat 5 TM (1984–1998)
            col = l5.filterBounds(aoi).filterDate(start_date, end_date).filter(ee.Filter.lt("CLOUD_COVER", 15))
            bands = ["SR_B3", "SR_B2", "SR_B1"]

        try:
            count = col.size().getInfo()
            if count == 0:
                print(f"    ⚠ No images for {glacier_name} {year}")
                continue
            composite = col.median().clip(aoi)
            visual = composite.select(bands).multiply(scale_factor)
            geemap.download_ee_image(image=visual, filename=out_path, region=aoi, scale=30, crs="EPSG:4326")
        except Exception as e:
            print(f"    ⚠ Failed {glacier_name} {year}: {e}")


@app.route("/api/glacier-image/years/<glacier_name>")
def glacier_image_years(glacier_name):
    """Return the list of years available for a glacier (cached + triggers download if empty)."""
    if glacier_name not in KNOWN_GLACIERS:
        return jsonify({"error": "Unknown glacier"}), 404

    years = _available_years_from_cache(glacier_name)
    return jsonify({"glacier": glacier_name, "years": years, "complete": len(years) >= 10})


@app.route("/api/glacier-image/download/<glacier_name>", methods=["POST"])
def glacier_image_download(glacier_name):
    """Trigger a background download of all years for a glacier. Idempotent."""
    if glacier_name not in KNOWN_GLACIERS:
        return jsonify({"error": "Unknown glacier"}), 404

    with _download_lock:
        if glacier_name in _download_locks:
            return jsonify({"status": "already_running"})
        _download_locks[glacier_name] = True

    def _run():
        try:
            _download_for_glacier(glacier_name)
        finally:
            with _download_lock:
                _download_locks.pop(glacier_name, None)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"status": "started"})


@app.route("/api/glacier-image/status/<glacier_name>")
def glacier_image_status(glacier_name):
    """Poll for download progress."""
    if glacier_name not in KNOWN_GLACIERS:
        return jsonify({"error": "Unknown glacier"}), 404
    with _download_lock:
        running = glacier_name in _download_locks
    years = _available_years_from_cache(glacier_name)
    return jsonify({
        "glacier": glacier_name,
        "downloading": running,
        "years_ready": years,
        "complete": running is False and len(years) > 0,
    })


@app.route("/api/glacier-image/tile/<glacier_name>/<int:year>")
def glacier_image_tile(glacier_name, year):
    """Return a single year's RGB + elevation as base64 PNG for the 3D viewer."""
    import rasterio
    import numpy as np
    from PIL import Image
    import io, base64

    if glacier_name not in KNOWN_GLACIERS:
        return jsonify({"error": "Unknown glacier"}), 404

    cache_dir = _glacier_cache_dir(glacier_name)
    tif_path = os.path.join(cache_dir, f"{year}.tif")
    dem_path = os.path.join(cache_dir, "dem.tif")

    if not os.path.exists(tif_path):
        return jsonify({"error": f"No cached image for {glacier_name} {year}"}), 404

    with rasterio.open(tif_path) as src:
        r = src.read(1).astype(np.float32)
        g = src.read(2).astype(np.float32)
        b = src.read(3).astype(np.float32)

    has_dem = os.path.exists(dem_path)
    if has_dem:
        with rasterio.open(dem_path) as dem_src:
            elevation = dem_src.read(1).astype(np.float32)
            elevation = np.nan_to_num(elevation, nan=0)

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
    years = _available_years_from_cache("Mt Rainier")
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
