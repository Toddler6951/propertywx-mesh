"""MESH point-query service.

Endpoint:
    GET /api/mesh?lat=<float>&lon=<float>&date=<YYYY-MM-DD>

Returns JSON:
    {
        "mesh_in": <float|null>,    # Maximum estimated hail size in inches at the point
        "source": <str|null>,       # "AWS" / "Iowa State" / "cache" / null when no data
        "lat_q": <float>,           # Quantized cache lat
        "lon_q": <float>,           # Quantized cache lon
        "note": <str|null>,
    }

Data flow:
    1. Round (lat, lon) to 0.01 degrees (~0.7 mi) for cache key.
    2. Look up SQLite cache. Hit → return.
    3. Miss → fetch MESH_Max_1440min GRIB2 for that date from AWS S3
       (noaa-mrms-pds, recent dates only) or fall back to Iowa State mtarchive
       (historical, ~2014+).
    4. Use eccodes to find the nearest grid value at (lat, lon).
    5. Convert mm -> inches.  Cache.  Return.

Notes:
    - MRMS MESH "Max_1440min" is a running 24-hour max; we request the file
      timestamped at the END of the requested UTC day.
    - Pre-2014 dates have no MESH coverage; we return None.
    - Future dates rejected.
"""

import datetime as dt
import gzip
import logging
import os
import sqlite3
import tempfile
from contextlib import closing

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

try:
    import eccodes
except ImportError:
    eccodes = None  # Lets the container come up even if libeccodes is missing — health check exposes the issue.

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
LOG = logging.getLogger("mesh")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s [%(levelname)s] %(message)s")

DB_PATH = os.environ.get("CACHE_DB", "/data/mesh_cache.db")
GRIB_CACHE_DIR = os.environ.get("GRIB_CACHE_DIR", "/data/grib_cache")
MESH_FIRST_DATE = dt.date(2014, 1, 1)  # earliest with reliable MRMS MESH archive
HTTP_TIMEOUT = 30  # seconds

# Allowed CORS origins — restrict to our domains.
ALLOWED_ORIGINS = [
    "https://propertywx.com",
    "https://app.propertywx.com",
    "https://insights.propertywx.com",
    "https://toddler6951.github.io",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]

# -----------------------------------------------------------------------------
# App + CORS
# -----------------------------------------------------------------------------
app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": ALLOWED_ORIGINS}}, supports_credentials=False)


# -----------------------------------------------------------------------------
# Cache (SQLite)
# -----------------------------------------------------------------------------
def _ensure_dirs():
    for d in (os.path.dirname(DB_PATH), GRIB_CACHE_DIR):
        if d and not os.path.exists(d):
            try:
                os.makedirs(d, exist_ok=True)
            except OSError as e:
                LOG.warning(f"cannot create {d}: {e}")


def _conn():
    _ensure_dirs()
    c = sqlite3.connect(DB_PATH)
    c.execute("""
        CREATE TABLE IF NOT EXISTS cache (
            lat_q REAL, lon_q REAL, date TEXT,
            mesh_in REAL, source TEXT, fetched_at TEXT,
            PRIMARY KEY (lat_q, lon_q, date)
        )
    """)
    return c


def cache_get(lat_q, lon_q, date_str):
    with closing(_conn()) as c:
        row = c.execute(
            "SELECT mesh_in, source FROM cache WHERE lat_q=? AND lon_q=? AND date=?",
            (lat_q, lon_q, date_str),
        ).fetchone()
    return row  # (mesh_in, source) or None


def cache_put(lat_q, lon_q, date_str, mesh_in, source):
    with closing(_conn()) as c:
        c.execute(
            "INSERT OR REPLACE INTO cache (lat_q, lon_q, date, mesh_in, source, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
            (lat_q, lon_q, date_str, mesh_in, source, dt.datetime.utcnow().isoformat()),
        )
        c.commit()


# -----------------------------------------------------------------------------
# GRIB2 fetch
# -----------------------------------------------------------------------------
# Try a sequence of timestamps near end-of-day (1440-min running max — last
# valid time of the requested UTC day captures everything within that day).
# If a particular minute is missing on the source, fall back to earlier ones.
TIMESTAMPS = ["235800", "230000", "225800", "220000", "120000"]


def grib_local_path(date):
    return os.path.join(GRIB_CACHE_DIR, f"MESH_{date.strftime('%Y%m%d')}.grib2")


def fetch_mesh_grib(date):
    """Download MESH_Max_1440min GRIB2 for the given UTC date.

    Returns (path_to_grib_file, source_name) or (None, None).
    Caches the decompressed GRIB2 to disk so multiple lat/lon lookups for the
    same date hit the local copy.
    """
    local = grib_local_path(date)
    if os.path.exists(local):
        return local, "cache"

    yyyymmdd = date.strftime("%Y%m%d")
    yyyy, mm, dd = date.strftime("%Y"), date.strftime("%m"), date.strftime("%d")

    sources = [
        ("AWS",
         lambda ts: f"https://noaa-mrms-pds.s3.amazonaws.com/CONUS/MESH_Max_1440min_00.50/{yyyymmdd}/MRMS_MESH_Max_1440min_00.50_{yyyymmdd}-{ts}.grib2.gz"),
        ("Iowa State",
         lambda ts: f"https://mtarchive.geol.iastate.edu/{yyyy}/{mm}/{dd}/mrms/ncep/MESH_Max_1440min/MESH_Max_1440min_00.50_{yyyymmdd}-{ts}.grib2.gz"),
    ]
    for source_name, url_fn in sources:
        for ts in TIMESTAMPS:
            url = url_fn(ts)
            try:
                r = requests.get(url, timeout=HTTP_TIMEOUT)
                if r.status_code == 200 and r.content:
                    LOG.info(f"fetched MESH from {source_name}: {url} ({len(r.content)} bytes)")
                    raw = gzip.decompress(r.content)
                    _ensure_dirs()
                    with open(local, "wb") as f:
                        f.write(raw)
                    return local, source_name
                LOG.debug(f"  {url} -> HTTP {r.status_code}")
            except requests.RequestException as e:
                LOG.debug(f"  {url} -> {e}")
                continue
    return None, None


# -----------------------------------------------------------------------------
# Point extraction (eccodes)
# -----------------------------------------------------------------------------
def extract_value(grib_path, lat, lon):
    """Return MRMS MESH value (in mm) at the nearest grid cell, or None."""
    if eccodes is None:
        raise RuntimeError("eccodes Python bindings not installed")
    with open(grib_path, "rb") as f:
        gid = eccodes.codes_grib_new_from_file(f)
        if gid is None:
            return None
        try:
            nearest = eccodes.codes_grib_find_nearest(gid, lat, lon)
        finally:
            eccodes.codes_release(gid)
    if not nearest:
        return None
    # find_nearest returns a list of dicts; default 1 nearest
    val = nearest[0]["value"]
    # MRMS uses -3 (or sometimes -999) for missing/out-of-coverage
    if val is None or val < 0:
        return None
    return float(val)


# -----------------------------------------------------------------------------
# Old-grib-file cleanup
# -----------------------------------------------------------------------------
def cleanup_old_grib_files(max_age_days=30):
    """Delete cached GRIB files older than max_age_days. Runs on each cold start."""
    if not os.path.isdir(GRIB_CACHE_DIR):
        return
    cutoff = dt.datetime.utcnow().timestamp() - (max_age_days * 86400)
    for fn in os.listdir(GRIB_CACHE_DIR):
        path = os.path.join(GRIB_CACHE_DIR, fn)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                LOG.info(f"cleaned up old grib: {fn}")
        except OSError:
            pass


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
@app.route("/api/mesh")
def mesh_lookup():
    try:
        lat = float(request.args.get("lat", ""))
        lon = float(request.args.get("lon", ""))
        date_str = request.args.get("date", "")
        date = dt.datetime.strptime(date_str, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid lat/lon/date. Use lat=<deg>&lon=<deg>&date=YYYY-MM-DD."}), 400

    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return jsonify({"error": "Coordinates out of range."}), 400

    if date < MESH_FIRST_DATE:
        return jsonify({
            "mesh_in": None, "source": None,
            "note": f"MRMS MESH archive begins {MESH_FIRST_DATE.isoformat()}; pre-2014 dates not available.",
        })
    if date > dt.date.today():
        return jsonify({"error": "Future date."}), 400

    lat_q = round(lat, 2)
    lon_q = round(lon, 2)

    cached = cache_get(lat_q, lon_q, date_str)
    if cached is not None:
        mesh_in, source = cached
        return jsonify({"mesh_in": mesh_in, "source": (source or "no-data") + " (cached)",
                        "lat_q": lat_q, "lon_q": lon_q})

    grib_path, source = fetch_mesh_grib(date)
    if not grib_path:
        cache_put(lat_q, lon_q, date_str, None, "no-data")
        return jsonify({"mesh_in": None, "source": None,
                        "note": "MESH file not available for that date.",
                        "lat_q": lat_q, "lon_q": lon_q})

    try:
        val_mm = extract_value(grib_path, lat, lon)
    except Exception as e:
        LOG.exception("GRIB parse failed")
        return jsonify({"error": f"GRIB parse error: {e}"}), 500

    mesh_in = round(val_mm / 25.4, 2) if val_mm is not None else None
    cache_put(lat_q, lon_q, date_str, mesh_in, source)
    return jsonify({"mesh_in": mesh_in, "source": source, "lat_q": lat_q, "lon_q": lon_q})


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "eccodes": eccodes is not None,
        "cache_db": os.path.exists(DB_PATH),
        "version": eccodes.codes_get_api_version() if eccodes else None,
    })


@app.route("/")
def root():
    return jsonify({
        "service": "PropertyWX MESH lookup",
        "endpoints": {
            "GET /api/mesh": "params: lat, lon, date (YYYY-MM-DD); returns mesh_in + source",
            "GET /health": "service status",
        },
    })


cleanup_old_grib_files()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)
