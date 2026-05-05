"""MRMS forensic point-query service.

Endpoints:
    GET /api/mesh?lat=&lon=&date=YYYY-MM-DD
        Backward-compat single-value MESH 24h max lookup.

    GET /api/event-detail?lat=&lon=&date=YYYY-MM-DD
        Multi-signal forensic detail. Returns:
            {
                "lat_q": <float>, "lon_q": <float>, "date": <YYYY-MM-DD>,
                "mesh": {
                    "max_in":          <float|null>,   # 24h max (MESH_Max_1440min)
                    "peak_in":         <float|null>,   # max over sampled 60-min windows
                    "peak_window_utc": <"HH:MM-HH:MM"|null>,
                    "source":          <str|null>,
                },
                "posh": {
                    "max_pct":         <int|null>,     # 0..100
                    "peak_window_utc": <"HH:MM-HH:MM"|null>,
                    "source":          <str|null>,
                },
                "samples":  <list of (window_utc, mesh_in, posh_pct)>,
                "note":     <str|null>,
            }

Data sources:
    AWS noaa-mrms-pds S3 (recent dates) → Iowa State mtarchive (historical).

Caching:
    SQLite at /data/mesh_cache.db; one row per (lat_q, lon_q, date).
    GRIB files cached on disk at /data/grib_cache and reused for additional
    point lookups within the same date.
"""

import datetime as dt
import gzip
import json
import logging
import os
import re
import sqlite3
from contextlib import closing

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

try:
    import eccodes
except Exception as _ecc_err:  # noqa: BLE001
    eccodes = None
    _ECCODES_IMPORT_ERROR = repr(_ecc_err)
else:
    _ECCODES_IMPORT_ERROR = None

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
LOG = logging.getLogger("mesh")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s [%(levelname)s] %(message)s")

DB_PATH = os.environ.get("CACHE_DB", "/data/mesh_cache.db")
GRIB_CACHE_DIR = os.environ.get("GRIB_CACHE_DIR", "/data/grib_cache")
MESH_FIRST_DATE = dt.date(2014, 1, 1)
HTTP_TIMEOUT = 30

# Some archive hosts (Iowa State mtarchive in particular) reject the default
# python-requests User-Agent. Identify ourselves like a normal client.
HTTP_HEADERS = {
    "User-Agent": "PropertyWX/1.0 (+https://propertywx.com)",
    "Accept": "*/*",
}

# Sub-daily samples. Each tuple: (window_label, valid_hour, valid_minute).
# MESH_Max_360min is a 6-hour running max — sampling at end of each 6h bin
# gives non-overlapping full-day coverage with just 4 GRIB fetches per fresh
# date. Tiny edge effects at the boundaries (e.g. final bin actually covers
# 17:58→23:58) are inconsequential for forensic timing within ~6h precision.
SAMPLE_HOURS = [
    ("00:00-06:00", 6, 0),
    ("06:00-12:00", 12, 0),
    ("12:00-18:00", 18, 0),
    ("18:00-24:00", 23, 58),
]

# Allowed CORS origins
ALLOWED_ORIGINS = [
    "https://propertywx.com",
    "https://app.propertywx.com",
    "https://insights.propertywx.com",
    "https://toddler6951.github.io",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": ALLOWED_ORIGINS}}, supports_credentials=False)


# -----------------------------------------------------------------------------
# Filesystem
# -----------------------------------------------------------------------------
def _ensure_dirs():
    for d in (os.path.dirname(DB_PATH), GRIB_CACHE_DIR):
        if d and not os.path.exists(d):
            try:
                os.makedirs(d, exist_ok=True)
            except OSError as e:
                LOG.warning(f"cannot create {d}: {e}")


# -----------------------------------------------------------------------------
# Cache (SQLite)
# -----------------------------------------------------------------------------
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
    c.execute("""
        CREATE TABLE IF NOT EXISTS detail_cache (
            lat_q REAL, lon_q REAL, date TEXT,
            detail_json TEXT, fetched_at TEXT,
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
    return row


def cache_put(lat_q, lon_q, date_str, mesh_in, source):
    with closing(_conn()) as c:
        c.execute(
            "INSERT OR REPLACE INTO cache (lat_q, lon_q, date, mesh_in, source, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
            (lat_q, lon_q, date_str, mesh_in, source, dt.datetime.utcnow().isoformat()),
        )
        c.commit()


def detail_cache_get(lat_q, lon_q, date_str):
    with closing(_conn()) as c:
        row = c.execute(
            "SELECT detail_json FROM detail_cache WHERE lat_q=? AND lon_q=? AND date=?",
            (lat_q, lon_q, date_str),
        ).fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return None


def detail_cache_put(lat_q, lon_q, date_str, detail):
    with closing(_conn()) as c:
        c.execute(
            "INSERT OR REPLACE INTO detail_cache (lat_q, lon_q, date, detail_json, fetched_at) VALUES (?, ?, ?, ?, ?)",
            (lat_q, lon_q, date_str, json.dumps(detail), dt.datetime.utcnow().isoformat()),
        )
        c.commit()


# -----------------------------------------------------------------------------
# Generic MRMS GRIB2 fetch
# -----------------------------------------------------------------------------
def grib_local_path(product_dir, date, hhmmss=None):
    """One file per (product, date, optional timestamp). Sub-daily samples
    use hhmmss to keep distinct files; daily products omit it."""
    suffix = f"_{hhmmss}" if hhmmss else ""
    return os.path.join(
        GRIB_CACHE_DIR,
        f"{product_dir}_{date.strftime('%Y%m%d')}{suffix}.grib2",
    )


def aws_list_keys(product_dir, date):
    """Enumerate MRMS files in noaa-mrms-pds for a product+date via S3 ListObjectsV2.

    Returns a list of (hhmmss_str, full_url) sorted ascending by timestamp.
    The PDS bucket retains roughly the most recent ~24-72 hours, so this will
    return [] for older dates — Iowa State is the archive fallback.
    """
    yyyymmdd = date.strftime("%Y%m%d")
    list_url = (
        f"https://noaa-mrms-pds.s3.amazonaws.com/"
        f"?list-type=2&prefix=CONUS/{product_dir}/{yyyymmdd}/&max-keys=1000"
    )
    try:
        r = requests.get(list_url, timeout=15, headers=HTTP_HEADERS)
        if r.status_code != 200:
            LOG.info(f"AWS list returned {r.status_code} for {product_dir}/{yyyymmdd}")
            return []
        keys = re.findall(r"<Key>([^<]+)</Key>", r.text)
        results = []
        for k in keys:
            if not k.endswith(".grib2.gz"):
                continue
            m = re.search(r"-(\d{8})-(\d{6})\.grib2\.gz$", k)
            if not m:
                continue
            results.append((m.group(2), f"https://noaa-mrms-pds.s3.amazonaws.com/{k}"))
        results.sort()  # ascending by hhmmss
        return results
    except requests.RequestException as e:
        LOG.info(f"AWS list exception for {product_dir}/{yyyymmdd}: {type(e).__name__}: {e}")
        return []


def iastate_list_keys(product_dir, date):
    """Enumerate MRMS files in the Iowa State Mesonet mtarchive for a
    product+date by parsing the Apache directory index.

    Iowa State preserves MRMS data going back several years — this is the
    archive fallback when AWS noaa-mrms-pds is empty (older than ~3 days).

    Naming gotcha: the *directory* name on Iowa State drops the `_00.50`
    height suffix, but the *file* name keeps it.
        AWS dir:    CONUS/MESH_Max_360min_00.50/20240423/
        IAState dir: 2024/04/23/mrms/ncep/MESH_Max_360min/
        Both files: MRMS_MESH_Max_360min_00.50_20240423-180000.grib2.gz

    Returns a list of (hhmmss_str, full_url) sorted ascending.
    """
    # Strip the height-level suffix for the directory portion of the URL.
    # Heights in MRMS file names are written as `_<HH.HH>` (e.g. `_00.50`).
    ia_dir = re.sub(r"_\d{2}\.\d{2}$", "", product_dir)

    yyyy = date.strftime("%Y")
    mm = date.strftime("%m")
    dd = date.strftime("%d")
    base_url = (
        f"https://mtarchive.geol.iastate.edu/"
        f"{yyyy}/{mm}/{dd}/mrms/ncep/{ia_dir}/"
    )
    try:
        r = requests.get(base_url, timeout=20, headers=HTTP_HEADERS)
        if r.status_code != 200:
            LOG.info(f"IAState list returned {r.status_code} for {ia_dir} {yyyy}/{mm}/{dd}")
            return []
        # Apache index lists files as <a href="MRMS_..._YYYYMMDD-HHMMSS.grib2.gz">
        hrefs = re.findall(r'href="([^"?][^"]*\.grib2\.gz)"', r.text)
        yyyymmdd = date.strftime("%Y%m%d")
        results = []
        for h in hrefs:
            m = re.search(rf"-{yyyymmdd}-(\d{{6}})\.grib2\.gz$", h)
            if not m:
                continue
            results.append((m.group(1), base_url + h))
        results.sort()
        return results
    except requests.RequestException as e:
        LOG.info(f"IAState list exception for {ia_dir} {yyyy}/{mm}/{dd}: {type(e).__name__}: {e}")
        return []


def list_keys(product_dir, date):
    """Unified MRMS file listing. Tries AWS noaa-mrms-pds first (fast, but
    only the last ~3 days). Falls back to Iowa State mtarchive for the deep
    historical archive.

    Returns (sources, candidates) where sources is "AWS" / "IAState" / None
    and candidates is the (hhmmss, url) list (possibly empty).
    """
    aws = aws_list_keys(product_dir, date)
    if aws:
        return "AWS", aws
    ia = iastate_list_keys(product_dir, date)
    if ia:
        return "IAState", ia
    return None, []


def _hhmmss_to_minutes(ts):
    """Convert HHMMSS string to minutes since midnight (ignoring seconds)."""
    return int(ts[0:2]) * 60 + int(ts[2:4])


def _fetch_grib(product_dir, date, target_hhmm=None, max_distance_min=30):
    """Robust fetch via directory listing. Picks the published file whose
    valid time is closest to target_hhmm (4-digit, e.g. '1800'); if target_hhmm
    is None, picks the latest available file (used for daily summary products).

    Tries AWS first, falls back to Iowa State for archives.

    Returns (local_path, source_name, hhmmss_used) or (None, None, None).
    """
    src_name, candidates = list_keys(product_dir, date)
    if not candidates:
        LOG.warning(f"no keys listed (AWS or IAState) for {product_dir} on {date.isoformat()}")
        return None, None, None

    if target_hhmm is None:
        chosen_ts, chosen_url = candidates[-1]  # latest
    else:
        target_min = int(target_hhmm[0:2]) * 60 + int(target_hhmm[2:4])
        chosen_ts, chosen_url = min(
            candidates,
            key=lambda c: abs(_hhmmss_to_minutes(c[0]) - target_min),
        )
        actual_min = _hhmmss_to_minutes(chosen_ts)
        if abs(actual_min - target_min) > max_distance_min:
            LOG.info(
                f"closest {product_dir} file ({chosen_ts}) is "
                f"{abs(actual_min - target_min)} min from target {target_hhmm} — skipping"
            )
            return None, None, None

    local = grib_local_path(product_dir, date, chosen_ts)
    if os.path.exists(local):
        return local, f"{src_name} (cache)", chosen_ts

    try:
        r = requests.get(chosen_url, timeout=HTTP_TIMEOUT, headers=HTTP_HEADERS)
        if r.status_code == 200 and r.content:
            LOG.info(f"fetched {product_dir} @ {chosen_ts} from {src_name}: {len(r.content)} bytes")
            raw = gzip.decompress(r.content)
            _ensure_dirs()
            with open(local, "wb") as f:
                f.write(raw)
            return local, src_name, chosen_ts
        LOG.info(f"  {chosen_url} -> HTTP {r.status_code}")
    except requests.RequestException as e:
        LOG.info(f"  {chosen_url} -> {type(e).__name__}: {e}")
    return None, None, None


# Daily 24h MESH max — picks the latest published file for the date.
def fetch_mesh_24h_grib(date):
    return _fetch_grib("MESH_Max_1440min_00.50", date, target_hhmm=None)


# 6-hour MESH max valid near HH:MM UTC.
def fetch_mesh_360min_grib(date, hour, minute):
    return _fetch_grib(
        "MESH_Max_360min_00.50", date,
        target_hhmm=f"{hour:02d}{minute:02d}",
    )


# Instantaneous POSH near HH:MM UTC.
def fetch_posh_grib(date, hour, minute):
    return _fetch_grib(
        "POSH_00.50", date,
        target_hhmm=f"{hour:02d}{minute:02d}",
    )


# -----------------------------------------------------------------------------
# Point extraction (eccodes)
# -----------------------------------------------------------------------------
def extract_value(grib_path, lat, lon):
    """Return raw MRMS value at the nearest grid cell, or None."""
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
    val = nearest[0]["value"]
    if val is None or val < 0:
        return None
    return float(val)


# -----------------------------------------------------------------------------
# High-level orchestration
# -----------------------------------------------------------------------------
def get_event_detail(lat, lon, date):
    """Build the full multi-signal forensic record for a (lat, lon, date)."""
    date_str = date.isoformat()

    # 1) Daily 24h MESH max
    mesh_max_in = None
    mesh_source = None
    grib_path, src, _ = fetch_mesh_24h_grib(date)
    if grib_path:
        try:
            v = extract_value(grib_path, lat, lon)
            if v is not None:
                mesh_max_in = round(v / 25.4, 2)
                mesh_source = src
        except Exception:
            LOG.exception("MESH 24h extract failed")

    # 2) Sub-daily MESH samples (6-hour running max, 4 per day)
    samples = []
    mesh_peak_in = None
    mesh_peak_window = None
    mesh_peak_source = None
    for label, hour, minute in SAMPLE_HOURS:
        gp, src, _ = fetch_mesh_360min_grib(date, hour, minute)
        v_in = None
        if gp:
            try:
                v_mm = extract_value(gp, lat, lon)
                if v_mm is not None:
                    v_in = round(v_mm / 25.4, 2)
            except Exception:
                LOG.exception(f"MESH 360min extract failed @ {label}")
        samples.append({"window_utc": label, "mesh_in": v_in, "posh_pct": None,
                        "_hour": hour, "_minute": minute, "_source": src})
        if v_in is not None and (mesh_peak_in is None or v_in > mesh_peak_in):
            mesh_peak_in = v_in
            mesh_peak_window = label
            mesh_peak_source = src

    # 3) POSH samples — instantaneous, taken at end of each MESH window
    posh_max_pct = None
    posh_peak_window = None
    posh_source = None
    for s in samples:
        gp, src, _ = fetch_posh_grib(date, s["_hour"], s["_minute"])
        if gp:
            try:
                v = extract_value(gp, lat, lon)
                if v is not None:
                    pct = max(0, min(100, int(round(v))))
                    s["posh_pct"] = pct
                    if posh_max_pct is None or pct > posh_max_pct:
                        posh_max_pct = pct
                        posh_peak_window = s["window_utc"]
                        posh_source = src
            except Exception:
                LOG.exception(f"POSH extract failed @ {s['window_utc']}")

    # Strip private keys before returning
    public_samples = [{k: v for k, v in s.items() if not k.startswith("_")} for s in samples]

    detail = {
        "lat_q": round(lat, 2),
        "lon_q": round(lon, 2),
        "date": date_str,
        "mesh": {
            "max_in": mesh_max_in,
            "peak_in": mesh_peak_in,
            "peak_window_utc": mesh_peak_window,
            "source": mesh_source or mesh_peak_source,
        },
        "posh": {
            "max_pct": posh_max_pct,
            "peak_window_utc": posh_peak_window,
            "source": posh_source,
        },
        "samples": public_samples,
        "note": None,
    }
    return detail


# -----------------------------------------------------------------------------
# Cleanup
# -----------------------------------------------------------------------------
def cleanup_old_grib_files(max_age_days=30):
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
def _parse_request_args():
    """Return (lat, lon, date, error_response_or_None)."""
    try:
        lat = float(request.args.get("lat", ""))
        lon = float(request.args.get("lon", ""))
        date_str = request.args.get("date", "")
        date = dt.datetime.strptime(date_str, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None, None, None, (jsonify({"error": "Invalid lat/lon/date. Use lat=<deg>&lon=<deg>&date=YYYY-MM-DD."}), 400)
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return None, None, None, (jsonify({"error": "Coordinates out of range."}), 400)
    if date > dt.date.today():
        return None, None, None, (jsonify({"error": "Future date."}), 400)
    return lat, lon, date, None


@app.route("/api/mesh")
def mesh_lookup():
    """Backward-compat: returns just the 24h MESH max for a (lat, lon, date)."""
    lat, lon, date, err = _parse_request_args()
    if err:
        return err

    if date < MESH_FIRST_DATE:
        return jsonify({
            "mesh_in": None, "source": None,
            "note": f"MRMS MESH archive begins {MESH_FIRST_DATE.isoformat()}; pre-2014 dates not available.",
        })

    lat_q = round(lat, 2)
    lon_q = round(lon, 2)
    date_str = date.isoformat()

    cached = cache_get(lat_q, lon_q, date_str)
    if cached is not None:
        mesh_in, source = cached
        return jsonify({"mesh_in": mesh_in, "source": (source or "no-data") + " (cached)",
                        "lat_q": lat_q, "lon_q": lon_q})

    grib_path, source, _ = fetch_mesh_24h_grib(date)
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


@app.route("/api/event-detail")
def event_detail():
    """Multi-signal forensic detail. Pulls daily MESH max + sub-daily MESH +
    POSH samples and returns a structured record. Caches as JSON."""
    lat, lon, date, err = _parse_request_args()
    if err:
        return err

    if date < MESH_FIRST_DATE:
        return jsonify({
            "lat_q": round(lat, 2), "lon_q": round(lon, 2), "date": date.isoformat(),
            "mesh": {"max_in": None, "peak_in": None, "peak_window_utc": None, "source": None},
            "posh": {"max_pct": None, "peak_window_utc": None, "source": None},
            "samples": [],
            "note": f"MRMS archive begins {MESH_FIRST_DATE.isoformat()}; pre-2014 dates not available.",
        })

    lat_q = round(lat, 2)
    lon_q = round(lon, 2)
    date_str = date.isoformat()

    refresh = request.args.get("refresh", "").lower() in ("1", "true", "yes")
    cached = None if refresh else detail_cache_get(lat_q, lon_q, date_str)
    if cached is not None:
        cached_copy = dict(cached)
        m = dict(cached_copy.get("mesh") or {})
        if m.get("source"):
            m["source"] = m["source"] + " (cached)"
        cached_copy["mesh"] = m
        return jsonify(cached_copy)

    try:
        detail = get_event_detail(lat, lon, date)
    except Exception as e:
        LOG.exception("event-detail orchestration failed")
        return jsonify({"error": f"Backend error: {e}"}), 500

    # Only cache if at least one signal succeeded; an all-null record likely
    # reflects a transient fetch failure rather than genuine no-data.
    has_signal = (
        (detail.get("mesh") or {}).get("max_in") is not None
        or (detail.get("mesh") or {}).get("peak_in") is not None
        or (detail.get("posh") or {}).get("max_pct") is not None
    )
    if has_signal:
        detail_cache_put(lat_q, lon_q, date_str, detail)
    else:
        detail["note"] = "No signal retrieved; result not cached. Retry or check backend logs for URL attempts."
    return jsonify(detail)


@app.route("/api/probe")
def probe():
    """Diagnostic: list what's available for each MRMS product on a given date
    via the AWS S3 ListObjectsV2 API. Returns the count + first/last timestamps
    + a few sample timestamps. Useful for confirming product naming + archive
    coverage without downloading any GRIB data.

    Usage:
        /api/probe?date=YYYY-MM-DD
    """
    try:
        date_str = request.args.get("date", "")
        date = dt.datetime.strptime(date_str, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return jsonify({"error": "Use date=YYYY-MM-DD."}), 400

    products = [
        "MESH_Max_1440min_00.50",
        "MESH_Max_360min_00.50",
        "MESH_Max_60min_00.50",
        "POSH_00.50",
        "MESH_00.50",
    ]
    results = []
    for p in products:
        aws_keys = aws_list_keys(p, date)
        ia_keys = iastate_list_keys(p, date) if not aws_keys else []
        keys = aws_keys or ia_keys
        used = "AWS" if aws_keys else ("IAState" if ia_keys else None)
        if keys:
            timestamps = [k[0] for k in keys]
            results.append({
                "product": p,
                "source":  used,
                "count":   len(keys),
                "first_ts": timestamps[0],
                "last_ts":  timestamps[-1],
                "samples":  timestamps[::max(1, len(timestamps) // 5)][:5],
            })
        else:
            results.append({"product": p, "source": None, "count": 0})
    return jsonify({"date": date.isoformat(), "results": results})


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "eccodes": eccodes is not None,
        "eccodes_error": _ECCODES_IMPORT_ERROR,
        "cache_db": os.path.exists(DB_PATH),
        "version": eccodes.codes_get_api_version() if eccodes else None,
    })


@app.route("/")
def root():
    return jsonify({
        "service": "PropertyWX MRMS forensic lookup",
        "endpoints": {
            "GET /api/mesh":         "params: lat, lon, date (YYYY-MM-DD); returns mesh_in (24h max) + source",
            "GET /api/event-detail": "params: lat, lon, date; returns full multi-signal record (mesh max + peak hour + posh)",
            "GET /health":           "service status",
        },
    })


try:
    cleanup_old_grib_files()
except Exception as _cleanup_err:  # noqa: BLE001
    LOG.warning(f"cleanup_old_grib_files skipped: {_cleanup_err!r}")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)
