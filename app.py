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
                    "max_pct":         <int|null>,           # 0..100
                    "peak_window_utc": <"HH:MM-HH:MM"|null>,
                    "peak_ts":         <"HHMMSS"|null>,      # exact frame when finescan ran
                    "scan_count":      <int>,                # # of POSH frames sampled
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

try:
    import numpy as np
    import pyart
except Exception as _pyart_err:  # noqa: BLE001
    pyart = None
    np = None
    _PYART_IMPORT_ERROR = repr(_pyart_err)
else:
    _PYART_IMPORT_ERROR = None

from wsr88d_sites import nearest_radar, haversine_mi

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
            # MRMS filenames use either "-YYYYMMDD-HHMMSS" or "_YYYYMMDD-HHMMSS"
            # depending on product naming (e.g. POSH_00.50_20240423-000032).
            m = re.search(r"[_-](\d{8})-(\d{6})\.grib2\.gz$", k)
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
            # Char before YYYYMMDD is "_" (e.g. MESH_Max_1440min_00.50_20240423-180000)
            m = re.search(rf"[_-]{yyyymmdd}-(\d{{6}})\.grib2\.gz$", h)
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
    """Return raw MRMS value (in product units) at the nearest grid cell.

    Sentinel handling:
        -1.0  = MRMS "no detection at this cell" — return 0.0 (real signal).
        <-2   = true missing-data flag (e.g. -999 / -3) — return None.
        None  = eccodes couldn't decode — return None.
    """
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
    if val is None:
        return None
    val = float(val)
    if val < -2:           # true missing-data flag
        return None
    if val < 0:            # MRMS no-detection sentinel (-1)
        return 0.0
    return val


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
    mesh_peak_idx = None
    for idx, (label, hour, minute) in enumerate(SAMPLE_HOURS):
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
                        "_hour": hour, "_minute": minute,
                        "_source": src, "_idx": idx})
        if v_in is not None and (mesh_peak_in is None or v_in > mesh_peak_in):
            mesh_peak_in = v_in
            mesh_peak_window = label
            mesh_peak_source = src
            mesh_peak_idx = idx

    # 3) POSH sampling — strategy depends on whether MESH detected hail.
    #
    # POSH is *instantaneous*; sampling at fixed 6-hour boundaries usually
    # misses the storm and reads 0% even when MESH > 0. To make POSH actually
    # useful, when MESH 360min identifies a peak window, we scan POSH at
    # 15-minute cadence inside that window only (24 fetches over 6h). On
    # silent days (MESH 0 everywhere) we fall back to the cheap 4-sample
    # schedule so we don't waste fetches.
    posh_max_pct = None
    posh_peak_window = None
    posh_peak_ts = None
    posh_source = None
    posh_scan_count = 0

    if mesh_peak_in and mesh_peak_in > 0 and mesh_peak_idx is not None:
        # Fine-grained POSH scan within the MESH peak window
        window_start_hr = mesh_peak_idx * 6
        window_end_hr = window_start_hr + 6
        scan_targets = []
        for h in range(window_start_hr, window_end_hr):
            for m in (0, 15, 30, 45):
                scan_targets.append((h, m))
        # Cap final hour at 23:58 to avoid running off the day
        if window_end_hr >= 24:
            scan_targets.append((23, 58))
        else:
            scan_targets.append((window_end_hr, 0))

        peak_label = SAMPLE_HOURS[mesh_peak_idx][0]
        for (h, m) in scan_targets:
            gp, src, hhmmss = fetch_posh_grib(date, h, m)
            if not gp:
                continue
            try:
                v = extract_value(gp, lat, lon)
                if v is None:
                    continue
                pct = max(0, min(100, int(round(v))))
                posh_scan_count += 1
                if posh_max_pct is None or pct > posh_max_pct:
                    posh_max_pct = pct
                    posh_peak_window = peak_label
                    posh_peak_ts = hhmmss
                    posh_source = src
            except Exception:
                LOG.exception(f"POSH extract failed @ {h:02d}:{m:02d}")

        # Stamp the peak-window sample so the public samples array reflects it
        if posh_max_pct is not None:
            for s in samples:
                if s["_idx"] == mesh_peak_idx:
                    s["posh_pct"] = posh_max_pct
                    break
    else:
        # Cheap fallback: instantaneous POSH at the 4 fixed sample times.
        for s in samples:
            gp, src, _ = fetch_posh_grib(date, s["_hour"], s["_minute"])
            if not gp:
                continue
            try:
                v = extract_value(gp, lat, lon)
                if v is None:
                    continue
                pct = max(0, min(100, int(round(v))))
                s["posh_pct"] = pct
                posh_scan_count += 1
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
            "peak_ts": posh_peak_ts,            # exact HHMMSS within window when fine-scan was used
            "scan_count": posh_scan_count,      # how many POSH frames were sampled
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


@app.route("/api/diag-fetch")
def diag_fetch():
    """Diagnostic: trace a full single-product fetch+extract for a
    (lat, lon, date, product, hhmm) tuple. Reports listing count, chosen URL,
    download size, gunzip bytes, eccodes nearest result, raw value, and any
    exception text. Use this when /api/event-detail returns all nulls so we
    can see exactly which step is failing.

    Usage: /api/diag-fetch?lat=&lon=&date=YYYY-MM-DD&product=MESH_Max_1440min_00.50&hhmm=2330
        hhmm optional — when omitted picks the latest available file.
    """
    out = {"steps": []}
    def step(name, **kw):
        out["steps"].append({"step": name, **kw})

    try:
        lat = float(request.args.get("lat", "32.0091"))
        lon = float(request.args.get("lon", "-97.1303"))
        date = dt.datetime.strptime(request.args.get("date", "2024-04-23"), "%Y-%m-%d").date()
        product = request.args.get("product", "MESH_Max_1440min_00.50")
        hhmm = request.args.get("hhmm") or None
    except (TypeError, ValueError) as e:
        return jsonify({"error": f"bad args: {e}"}), 400

    out["lat"] = lat
    out["lon"] = lon
    out["date"] = date.isoformat()
    out["product"] = product
    out["hhmm"] = hhmm

    # 1. Listing
    src, candidates = list_keys(product, date)
    step("list_keys", source=src, count=len(candidates),
         first=candidates[0] if candidates else None,
         last=candidates[-1] if candidates else None)
    if not candidates:
        return jsonify(out)

    # 2. Pick file
    if hhmm:
        target_min = int(hhmm[0:2]) * 60 + int(hhmm[2:4])
        chosen_ts, chosen_url = min(candidates, key=lambda c: abs(_hhmmss_to_minutes(c[0]) - target_min))
    else:
        chosen_ts, chosen_url = candidates[-1]
    step("pick_file", chosen_ts=chosen_ts, chosen_url=chosen_url)

    # 3. Download
    try:
        r = requests.get(chosen_url, timeout=HTTP_TIMEOUT, headers=HTTP_HEADERS)
        step("download", status=r.status_code, gz_bytes=len(r.content) if r.content else 0)
        if r.status_code != 200 or not r.content:
            return jsonify(out)
    except Exception as e:
        step("download", error=f"{type(e).__name__}: {e}")
        return jsonify(out)

    # 4. Gunzip
    try:
        raw = gzip.decompress(r.content)
        step("gunzip", grib_bytes=len(raw))
    except Exception as e:
        step("gunzip", error=f"{type(e).__name__}: {e}")
        return jsonify(out)

    # 5. Write to disk
    local = grib_local_path(product, date, chosen_ts)
    try:
        _ensure_dirs()
        with open(local, "wb") as f:
            f.write(raw)
        step("write_disk", path=local, bytes_on_disk=os.path.getsize(local))
    except Exception as e:
        step("write_disk", error=f"{type(e).__name__}: {e}")
        return jsonify(out)

    # 6. eccodes extract
    if eccodes is None:
        step("eccodes", error=f"eccodes not loaded: {_ECCODES_IMPORT_ERROR}")
        return jsonify(out)
    try:
        with open(local, "rb") as f:
            gid = eccodes.codes_grib_new_from_file(f)
            if gid is None:
                step("eccodes", error="codes_grib_new_from_file returned None")
                return jsonify(out)
            try:
                nearest = eccodes.codes_grib_find_nearest(gid, lat, lon)
                # Pull a few attributes for diagnostics
                try:
                    short = eccodes.codes_get(gid, "shortName")
                except Exception:
                    short = None
            finally:
                eccodes.codes_release(gid)
        if not nearest:
            step("eccodes", short_name=short, error="find_nearest returned empty")
            return jsonify(out)
        n0 = nearest[0]
        step("eccodes",
             short_name=short,
             nearest_lat=n0.get("lat"),
             nearest_lon=n0.get("lon"),
             nearest_distance=n0.get("distance"),
             value=n0.get("value"))
    except Exception as e:
        step("eccodes", error=f"{type(e).__name__}: {e}")

    return jsonify(out)


@app.route("/api/probe-url")
def probe_url():
    """Diagnostic: do a raw GET against an arbitrary URL from inside the
    Railway container and report status + length + body snippet. Lets us
    figure out exactly why a directory listing comes back empty.
    Usage: /api/probe-url?u=<url-encoded>"""
    u = request.args.get("u", "")
    if not u:
        return jsonify({"error": "missing ?u=<url>"}), 400
    try:
        r = requests.get(u, timeout=20, headers=HTTP_HEADERS)
        body = r.text or ""
        return jsonify({
            "url": u,
            "status": r.status_code,
            "length": len(body),
            "snippet": body[:1500],
            "final_url": r.url,
        })
    except requests.RequestException as e:
        return jsonify({
            "url": u,
            "error": f"{type(e).__name__}: {e}",
        }), 502


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


# -----------------------------------------------------------------------------
# NEXRAD Level 2 — radar verification (Phase D)
# -----------------------------------------------------------------------------
NEXRAD_CACHE_DIR = os.environ.get("NEXRAD_CACHE_DIR", "/data/nexrad_cache")


def _bearing_range_km(lat1, lon1, lat2, lon2):
    """Return (range_km, azimuth_deg) from point 1 to point 2.

    Azimuth is great-circle bearing measured clockwise from true north — this
    matches the WSR-88D azimuth convention so we can directly compare to
    `radar.azimuth['data']`.
    """
    from math import asin, atan2, cos, degrees, radians, sin, sqrt
    R_km = 6371.0
    p1, p2 = radians(lat1), radians(lat2)
    dp = radians(lat2 - lat1)
    dl = radians(lon2 - lon1)
    a = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    range_km = 2 * R_km * asin(sqrt(a))
    y = sin(dl) * cos(p2)
    x = cos(p1) * sin(p2) - sin(p1) * cos(p2) * cos(dl)
    az = (degrees(atan2(y, x)) + 360.0) % 360.0
    return range_km, az


def parse_nexrad_volume(url, radar_id, hhmmss, date, prop_lat, prop_lon,
                        radar_lat, radar_lon):
    """Download one NEXRAD Level 2 volume and extract dual-pol values at the
    gates surrounding the property. Returns dict with peak metrics or None.

    Strategy:
        1. Download to disk cache (volumes are reused across queries).
        2. Open with pyart.io.read_nexrad_archive — only the dual-pol fields.
        3. Find the lowest elevation sweep (closest to 0.5°).
        4. Compute property's range + azimuth from the radar.
        5. Find rays whose azimuth is within ~1.5° of the property bearing
           (typical WSR-88D beamwidth) and gates whose range is within ~2 km.
        6. Read Z / ZDR / CC / KDP at those (ray, gate) pairs.
        7. Pick the gate with peak reflectivity — that's our forensic gate.
    """
    if pyart is None or np is None:
        return None

    yyyymmdd = date.strftime("%Y%m%d")
    fname = f"{radar_id}{yyyymmdd}_{hhmmss}_V06"
    local_path = os.path.join(NEXRAD_CACHE_DIR, fname)

    if not os.path.exists(local_path):
        try:
            r = requests.get(url, timeout=60, headers=HTTP_HEADERS)
            if r.status_code != 200 or not r.content:
                LOG.info(f"NEXRAD volume {fname}: HTTP {r.status_code}")
                return None
            content = r.content
            if url.endswith(".gz"):
                try:
                    content = gzip.decompress(content)
                except OSError:
                    pass  # Some Unidata files are NOT gzipped despite history
            os.makedirs(NEXRAD_CACHE_DIR, exist_ok=True)
            with open(local_path, "wb") as f:
                f.write(content)
        except requests.RequestException as e:
            LOG.info(f"NEXRAD download {fname} failed: {type(e).__name__}: {e}")
            return None

    try:
        radar = pyart.io.read_nexrad_archive(
            local_path,
            include_fields=[
                "reflectivity",
                "differential_reflectivity",
                "cross_correlation_ratio",
                "specific_differential_phase",
            ],
        )
    except Exception as e:
        LOG.info(f"pyart read failed for {fname}: {type(e).__name__}: {e}")
        return None

    # Lowest elevation sweep — surface hail forensics live in 0.5° tilt
    elev_angles = radar.fixed_angle["data"]
    sweep_idx = int(np.argmin(np.abs(elev_angles - 0.5)))
    sweep_start, sweep_end = radar.get_start_end(sweep_idx)
    sweep_slc = slice(sweep_start, sweep_end + 1)

    range_km, az_deg = _bearing_range_km(radar_lat, radar_lon, prop_lat, prop_lon)

    # Azimuth band: ±1.5° around property bearing (covers ~3 rays at 1° spacing)
    sweep_az = radar.azimuth["data"][sweep_slc]
    az_diff = np.abs(((sweep_az - az_deg + 180.0) % 360.0) - 180.0)
    az_idx = np.where(az_diff < 1.5)[0]
    if az_idx.size == 0:
        return None

    # Range gates within 2 km of the property
    gate_range_m = radar.range["data"]
    rng_diff_km = np.abs(gate_range_m / 1000.0 - range_km)
    rng_idx = np.where(rng_diff_km < 2.0)[0]
    if rng_idx.size == 0:
        return None

    # Read all four dual-pol fields per gate so we can keep them co-located.
    # Reporting max Z from one gate and min CC from another gives incoherent
    # numbers (e.g. peak Z=60 with CC=0.21 from a different gate that's
    # actually biological scatter at low Z). Forensic claims need the FULL
    # dual-pol fingerprint AT a single gate.
    field_map = {
        "Z":   "reflectivity",
        "ZDR": "differential_reflectivity",
        "CC":  "cross_correlation_ratio",
        "KDP": "specific_differential_phase",
    }
    sweep_arrays = {}
    for short, full in field_map.items():
        if full in radar.fields:
            sweep_arrays[short] = radar.fields[full]["data"][sweep_slc]

    if "Z" not in sweep_arrays:
        return None

    def _val(arr, ai, ri):
        v = arr[ai, ri]
        if hasattr(v, "mask") and bool(v.mask):
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    per_gate = []
    for ai in az_idx:
        for ri in rng_idx:
            z = _val(sweep_arrays["Z"], ai, ri)
            if z is None:
                continue
            per_gate.append({
                "Z":   z,
                "ZDR": _val(sweep_arrays["ZDR"], ai, ri) if "ZDR" in sweep_arrays else None,
                "CC":  _val(sweep_arrays["CC"],  ai, ri) if "CC"  in sweep_arrays else None,
                "KDP": _val(sweep_arrays["KDP"], ai, ri) if "KDP" in sweep_arrays else None,
            })

    if not per_gate:
        return None

    # The forensic gate is the one with the most hail-like dual-pol signature
    # among gates with Z ≥ 50 dBZ (real precipitation). Hail-likeness is
    # primarily driven by |ZDR| being close to zero — tumbling, irregular
    # hailstones produce near-zero ZDR even when wet hail keeps CC high. If
    # no gate clears the 50 dBZ floor, fall back to the peak-Z gate so we
    # still report something coherent.
    candidates = [g for g in per_gate if g["Z"] >= 50.0]
    if candidates:
        def _hail_likeness_score(g):
            # Lower score = more hail-like. Primary term is |ZDR|, with
            # CC and KDP acting as tiebreakers.
            zdr_term = abs(g["ZDR"]) if g["ZDR"] is not None else 5.0
            cc_term  = (g["CC"] if g["CC"] is not None else 1.0) * 0.5
            return zdr_term + cc_term
        peak_gate = min(candidates, key=_hail_likeness_score)
    else:
        peak_gate = max(per_gate, key=lambda g: g["Z"])

    def _r(v, n=2):
        return None if v is None else round(v, n)

    return {
        "volume_ts": hhmmss,
        "volume_time_utc": f"{hhmmss[0:2]}:{hhmmss[2:4]}:{hhmmss[4:6]}",
        "sweep_elev_deg": round(float(elev_angles[sweep_idx]), 2),
        "gate_count": len(per_gate),
        "Z":   _r(peak_gate["Z"], 1),
        "ZDR": _r(peak_gate["ZDR"], 2),
        "CC":  _r(peak_gate["CC"], 3),
        "KDP": _r(peak_gate["KDP"], 2),
    }


def render_radar_png(local_path, prop_lat, prop_lon, radar_lat, radar_lon,
                     range_mi=30, peak_label=None):
    """Render a forensic PNG of the lowest-elev reflectivity sweep with the
    property pin overlaid. Cropped to ~range_mi around the property so the
    storm structure is visible rather than the radar's full ~250 km reach.

    Returns a base64-encoded PNG string, or None on error. Uses matplotlib's
    Agg backend so no display server is required on Railway.
    """
    if pyart is None or np is None:
        return None
    try:
        import base64
        import io
        from math import cos, radians, sin

        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        radar = pyart.io.read_nexrad_archive(local_path, include_fields=["reflectivity"])
        elev_angles = radar.fixed_angle["data"]
        sweep_idx = int(np.argmin(np.abs(elev_angles - 0.5)))

        fig, ax = plt.subplots(figsize=(8, 8), dpi=100)
        display = pyart.graph.RadarDisplay(radar)
        display.plot_ppi(
            "reflectivity", sweep=sweep_idx,
            cmap="pyart_NWSRef", vmin=-20, vmax=80,
            colorbar_label="Reflectivity (dBZ)",
            ax=ax, title_flag=False,
        )

        # Compute property's (x, y) offset from radar in km — pyart's PPI
        # plots use east/north from the radar as (x, y).
        range_km, az_deg = _bearing_range_km(radar_lat, radar_lon, prop_lat, prop_lon)
        az_rad = radians(az_deg)
        x_km = range_km * sin(az_rad)
        y_km = range_km * cos(az_rad)

        ax.plot(x_km, y_km, marker="*", color="yellow", markersize=24,
                markeredgecolor="black", markeredgewidth=2, linestyle="None", zorder=10)
        ax.annotate(
            "Property", (x_km, y_km),
            textcoords="offset points", xytext=(14, 10),
            fontsize=11, fontweight="bold", color="black",
            bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="black", alpha=0.9),
            zorder=11,
        )

        crop_km = range_mi * 1.609344
        ax.set_xlim(x_km - crop_km, x_km + crop_km)
        ax.set_ylim(y_km - crop_km, y_km + crop_km)
        ax.set_aspect("equal")
        title_radar = radar.metadata.get("instrument_name", "")
        title_elev = float(elev_angles[sweep_idx])
        title = f"{title_radar} reflectivity @ {title_elev:.1f}°"
        if peak_label:
            title += f"  ·  {peak_label}"
        ax.set_title(title, fontsize=11)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", dpi=100)
        plt.close(fig)
        buf.seek(0)
        return base64.b64encode(buf.read()).decode("ascii")
    except Exception as e:  # noqa: BLE001
        LOG.exception(f"render_radar_png failed: {e}")
        return None


def classify_hail_signature(z, zdr, cc, kdp, beam_height_ft):
    """Rule-based hail classification from dual-pol values at the forensic
    gate. Returns (hsda_class, hca_class, narrative).

    Thresholds adapted from Heinselman & Ryzhkov 2006 ("Validation of
    Polarimetric Hail Detection") and Park et al. 2009 ("HCA on the WSR-88D"),
    with **ZDR promoted as the primary discriminator** based on the
    observation that wet/melting hail can keep CC > 0.95 (water coating makes
    scattering more coherent) while pure raindrops at high Z always show
    ZDR > 1.5 dB. ZDR near zero at high reflectivity is essentially
    pathognomonic for hail.

    Decision order:
        1. Z ≥ 50 + ZDR ≈ 0 → hail (size by Z tier). CC depression strengthens
           confidence but is not required (covers wet-hail case).
        2. Z ≥ 50 + ZDR > 1.5 → heavy rain (large oblate drops).
        3. Otherwise: weak signal or mix; classify accordingly.
    """
    if z is None:
        return ("Unknown", "Unknown", "Insufficient reflectivity at property gates.")

    # Primary discriminator: ZDR
    near_zero_zdr = zdr is not None and -1.0 <= zdr <= 1.5
    high_zdr      = zdr is not None and zdr > 2.0
    # Secondary: CC depression. Used to strengthen narrative; not required
    # to call hail because of the wet-hail case.
    cc_depressed   = cc is not None and cc < 0.95
    cc_strong_dep  = cc is not None and cc < 0.85

    # Hail signatures (ZDR-primary)
    if z is not None and near_zero_zdr:
        if z >= 65:
            cc_note = f", CC={cc:.2f} ({'strong depression' if cc_strong_dep else 'depressed' if cc_depressed else 'high — wet-hail mix likely'})" if cc is not None else ""
            return ("Giant (>2\")", "Large Hail",
                    f"Z={z:.0f} dBZ + ZDR={zdr:.1f} dB (≈0){cc_note} — giant-hail signature.")
        if z >= 60:
            cc_note = f", CC={cc:.2f}" if cc is not None else ""
            return ("Large (1-2\")", "Large Hail",
                    f"Z={z:.0f} dBZ + ZDR={zdr:.1f} dB (≈0){cc_note} — large-hail signature.")
        if z >= 55:
            cc_note = f", CC={cc:.2f}" if cc is not None else ""
            return ("Large (1-2\")", "Hail",
                    f"Z={z:.0f} dBZ + ZDR={zdr:.1f} dB (≈0){cc_note} — hail signature; size at lower bound of Large.")
        if z >= 50:
            cc_note = f", CC={cc:.2f}" if cc is not None else ""
            return ("Small (<1\")", "Hail / Rain Mix",
                    f"Z={z:.0f} dBZ + ZDR={zdr:.1f} dB (≈0){cc_note} — small hail or hail-rain mix.")

    # Heavy rain (high ZDR rules it out as hail)
    if z is not None and z >= 50 and high_zdr:
        cc_note = f", CC={cc:.2f}" if cc is not None else ""
        return ("None", "Heavy Rain",
                f"Z={z:.0f} dBZ + ZDR={zdr:.1f} dB (>2 = oblate raindrops){cc_note} — heavy-rain signature, no hail.")

    # Mid-range ZDR with high Z — ambiguous; CC depression tilts toward hail-mix
    if z is not None and z >= 55 and cc_depressed:
        return ("Small (<1\")", "Hail / Rain Mix",
                f"Z={z:.0f} dBZ + CC={cc:.2f} (depressed) — hail-rain mix likely.")

    if z is not None and z >= 50:
        zdr_part = f", ZDR={zdr:.1f} dB" if zdr is not None else ""
        cc_part  = f", CC={cc:.2f}" if cc is not None else ""
        return ("None", "Heavy Precipitation",
                f"Z={z:.0f} dBZ{zdr_part}{cc_part} — strong precipitation, no clear hail signature.")

    return ("None", "Light/Moderate Precip",
            f"Z={z:.0f} dBZ — no significant precipitation at property gate.")


def nexrad_list_volumes(radar_id, date):
    """Enumerate NEXRAD Level 2 volume keys for a radar+date.

    We use Unidata's public mirror (unidata-nexrad-level2) rather than NOAA's
    canonical bucket (noaa-nexrad-level2). Both contain the same data and
    follow identical key conventions, but only Unidata's bucket policy allows
    anonymous ListObjects — NOAA's blocks listing for unauthenticated callers
    even though individual GETs are public.

    Bucket layout:
        s3://unidata-nexrad-level2/<YYYY>/<MM>/<DD>/<RID>/<RID><YYYYMMDD>_<HHMMSS>_V06[.gz]

    Returns list of (hhmmss, full_url) sorted ascending. Filters out the
    "MDM" (metadata-only) and tar-archive sentinel files some radars emit.
    """
    yyyy = date.strftime("%Y")
    mm = date.strftime("%m")
    dd = date.strftime("%d")
    prefix = f"{yyyy}/{mm}/{dd}/{radar_id}/"
    list_url = (
        f"https://unidata-nexrad-level2.s3.amazonaws.com/"
        f"?list-type=2&prefix={prefix}&max-keys=1000"
    )
    try:
        r = requests.get(list_url, timeout=15, headers=HTTP_HEADERS)
        if r.status_code != 200:
            LOG.info(f"NEXRAD list returned {r.status_code} for {radar_id}/{yyyy}{mm}{dd}")
            return []
        keys = re.findall(r"<Key>([^<]+)</Key>", r.text)
        results = []
        for k in keys:
            # Skip MDM (metadata-only) and any non-V06 files
            if "MDM" in k or k.endswith(".tar"):
                continue
            # Filename: KFWS20240926_193245_V06 (sometimes .gz)
            m = re.search(r"_(\d{6})_V0[5-6]", k)
            if not m:
                continue
            results.append((m.group(1), f"https://unidata-nexrad-level2.s3.amazonaws.com/{k}"))
        results.sort()
        return results
    except requests.RequestException as e:
        LOG.info(f"NEXRAD list exception {radar_id}/{yyyy}{mm}{dd}: {type(e).__name__}: {e}")
        return []


def parse_window(s):
    """Parse a peak window string like '18:00-24:00' into (start_min, end_min)."""
    try:
        a, b = s.split("-")
        ah, am = a.split(":")
        bh, bm = b.split(":")
        start = int(ah) * 60 + int(am)
        end = int(bh) * 60 + int(bm)
        if end <= start:
            end = 24 * 60
        return start, end
    except Exception:
        return None


@app.route("/api/nexrad-verify")
def nexrad_verify():
    """NEXRAD Level 2 hail verification — Phase D scaffold.

    This first iteration does the *planning* (radar selection, volume listing,
    window narrowing) but does NOT yet parse the volumes. Returns the analysis
    plan so we can confirm radar choice + volume coverage on the test address
    before adding py-art. The next iteration will swap the placeholder
    `findings` block for real HCA/HSDA + dual-pol gate values.

    Query params:
        lat, lon, date (YYYY-MM-DD)        — required
        peak_window=HH:MM-HH:MM            — optional; if omitted we'll use the
                                              MESH peak window from event-detail
                                              cache, falling back to 12:00-24:00.
    """
    try:
        lat = float(request.args.get("lat"))
        lon = float(request.args.get("lon"))
        date = dt.datetime.strptime(request.args.get("date", ""), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return jsonify({"error": "Use lat=<deg>&lon=<deg>&date=YYYY-MM-DD."}), 400

    # 1) Pick nearest radar
    nr = nearest_radar(lat, lon, max_dist_mi=150.0)
    if not nr:
        return jsonify({
            "ok": False,
            "reason": "no_nearby_radar",
            "message": "No WSR-88D within 150 mi of the property — NEXRAD verification not available here.",
        })
    rid, rdist, rname, rlat, rlon, relev = nr

    # 2) Determine analysis time window. Order of preference:
    #    a) Explicit ?peak_window= query param
    #    b) Cached event-detail's MESH peak window for this exact (lat, lon, date)
    #    c) Full-day default (00:00-24:00) — the MESH_60min scan below will
    #       narrow to the actual storm hour. This is critical for early-morning
    #       events (storm before noon UTC) which a 12:00-24:00 default would miss.
    peak_window = request.args.get("peak_window", "").strip()
    win = parse_window(peak_window) if peak_window else None
    if not win:
        cached = detail_cache_get(round(lat, 2), round(lon, 2), date.isoformat())
        if cached and cached.get("mesh", {}).get("peak_window_utc"):
            peak_window = cached["mesh"]["peak_window_utc"]
            win = parse_window(peak_window)
        if not win:
            peak_window = "00:00-24:00"
            win = (0, 24 * 60)
    win_start, win_end = win

    # 3) List Level 2 volumes for that radar+date
    all_volumes = nexrad_list_volumes(rid, date)
    if not all_volumes:
        return jsonify({
            "ok": False,
            "reason": "no_volumes",
            "message": f"No Level 2 volumes available on AWS for {rid} on {date.isoformat()}.",
            "radar": {"id": rid, "name": rname, "distance_mi": round(rdist, 1)},
        })

    # 4) Filter to volumes inside the peak window, plus a small buffer
    BUFFER_MIN = 15
    in_window = []
    for hhmmss, url in all_volumes:
        v_min = int(hhmmss[0:2]) * 60 + int(hhmmss[2:4])
        if (win_start - BUFFER_MIN) <= v_min <= (win_end + BUFFER_MIN):
            in_window.append((hhmmss, url, v_min))

    # 5) Lock onto the actual storm hour using MESH_Max_60min.
    # MESH_Max_60min publishes the past hour's max — sampling at HH:00 gives
    # us the magnitude during HH-1 to HH. We scan within the peak window and
    # pick volumes within ±30 min of the hour with the highest at-property
    # MESH_60min reading. This collapses an 8-hour spread into a 1-hour band
    # right at the storm, which is what we need for a non-trivial Z signal.
    storm_hour_min = None     # midpoint (in minutes from midnight) of the storm hour
    storm_mesh_60_in = None
    if eccodes is not None and len(in_window) > 8:
        win_start_hr = max(0, win_start // 60)
        win_end_hr = min(23, max(win_start_hr, win_end // 60))
        best_v = -1.0
        best_h = None
        for h in range(win_start_hr, win_end_hr + 1):
            try:
                gp, _src, _ts = _fetch_grib("MESH_Max_60min_00.50", date,
                                            target_hhmm=f"{h:02d}00")
                if not gp:
                    continue
                v_mm = extract_value(gp, lat, lon)
                if v_mm is None:
                    continue
                v_in = v_mm / 25.4
                if v_in > best_v:
                    best_v = v_in
                    best_h = h
            except Exception:
                LOG.exception(f"MESH_60min scan failed at {h:02d}:00")
                continue
        if best_h is not None and best_v > 0:
            # MESH_60min at HH:00 represents [HH-1, HH], so storm midpoint is HH-30min
            storm_hour_min = best_h * 60 - 30
            storm_mesh_60_in = round(best_v, 2)

    # 6) Subsample. If we found a storm hour, pick volumes within ±30 min of it.
    # Otherwise fall back to evenly-spread picks across the window.
    MAX_VOLUMES = 8
    if storm_hour_min is not None:
        narrow = [v for v in in_window
                  if (storm_hour_min - 30) <= v[2] <= (storm_hour_min + 30)]
        # If our ±30 min window happens to be empty (storm-hour boundary case),
        # fall back to ±60 min before giving up.
        if not narrow:
            narrow = [v for v in in_window
                      if (storm_hour_min - 60) <= v[2] <= (storm_hour_min + 60)]
        candidates = narrow if narrow else in_window
    else:
        candidates = in_window

    if len(candidates) > MAX_VOLUMES:
        step = len(candidates) / MAX_VOLUMES
        picked = [candidates[int(i * step)] for i in range(MAX_VOLUMES)]
    else:
        picked = candidates

    # 6) Compute beam height at the property's range from the radar.
    # WSR-88D lowest tilt is 0.5°. Beam height (AGL) at range r (km) ≈
    # r * tan(elev) + r²/(2 * Re_eff) where Re_eff = 4/3 * 6371 km.
    from math import radians, tan
    range_km = rdist * 1.609344
    elev_rad = radians(0.5)
    Re_eff_km = (4.0 / 3.0) * 6371.0
    beam_height_km = range_km * tan(elev_rad) + (range_km ** 2) / (2.0 * Re_eff_km)
    beam_height_ft = beam_height_km * 3280.84

    # 7) Parse the picked volumes and pick the strongest forensic gate.
    findings = None
    if pyart is None:
        findings = {"error": "pyart not available in this build", "detail": _PYART_IMPORT_ERROR}
    else:
        per_volume = []
        for hhmmss, url, _vmin in picked:
            info = parse_nexrad_volume(url, rid, hhmmss, date, lat, lon, rlat, rlon)
            if info is not None:
                per_volume.append(info)

        if not per_volume:
            findings = {"error": "no_volumes_parsed",
                        "message": "All picked volumes either failed to download or had no gates near the property."}
        else:
            with_z = [v for v in per_volume if v.get("Z") is not None]
            if not with_z:
                findings = {"error": "no_reflectivity",
                            "message": "Volumes parsed but no reflectivity data at property gates.",
                            "volumes_analyzed": len(per_volume)}
            else:
                peak = max(with_z, key=lambda v: v["Z"])
                hsda, hca, narrative = classify_hail_signature(
                    peak.get("Z"), peak.get("ZDR"), peak.get("CC"), peak.get("KDP"),
                    beam_height_ft,
                )
                # Render the peak volume's lowest-elev reflectivity as a PNG,
                # cropped to ~30 mi around the property and pinned. Re-loads
                # the cached volume from disk; only one render per analysis.
                peak_path = os.path.join(
                    NEXRAD_CACHE_DIR,
                    f"{rid}{date.strftime('%Y%m%d')}_{peak['volume_ts']}_V06",
                )
                image_b64 = None
                if os.path.exists(peak_path):
                    image_b64 = render_radar_png(
                        peak_path, lat, lon, rlat, rlon, range_mi=30,
                        peak_label=f"{date.isoformat()} {peak['volume_time_utc']} UTC",
                    )

                findings = {
                    "peak_volume_ts": peak["volume_ts"],
                    "peak_volume_utc": peak["volume_time_utc"],
                    "gate_count": peak["gate_count"],
                    "sweep_elev_deg": peak["sweep_elev_deg"],
                    "Z":   peak.get("Z"),
                    "ZDR": peak.get("ZDR"),
                    "CC":  peak.get("CC"),
                    "KDP": peak.get("KDP"),
                    "hsda_class": hsda,
                    "hca_class":  hca,
                    "narrative":  narrative,
                    "volumes_analyzed": len(per_volume),
                    "image_b64": image_b64,
                    "image_format": "png" if image_b64 else None,
                }

    return jsonify({
        "ok": True,
        "phase": "complete" if (findings and "error" not in findings) else "partial",
        "lat_q": round(lat, 4),
        "lon_q": round(lon, 4),
        "date": date.isoformat(),
        "radar": {
            "id": rid,
            "name": rname,
            "lat": rlat,
            "lon": rlon,
            "elev_ft": relev,
            "distance_mi": round(rdist, 1),
            "beam_height_ft_at_property": round(beam_height_ft),
        },
        "window_utc": peak_window,
        "storm_hour_locked": (
            None if storm_hour_min is None
            else f"{storm_hour_min // 60:02d}:{storm_hour_min % 60:02d}"
        ),
        "storm_mesh_60min_in": storm_mesh_60_in,
        "volumes_total_for_day": len(all_volumes),
        "volumes_in_window": len(in_window),
        "volumes_picked": len(picked),
        "picked_timestamps": [v[0] for v in picked],
        "findings": findings,
    })


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "eccodes": eccodes is not None,
        "eccodes_error": _ECCODES_IMPORT_ERROR,
        "eccodes_version": eccodes.codes_get_api_version() if eccodes else None,
        "pyart": pyart is not None,
        "pyart_error": _PYART_IMPORT_ERROR,
        "pyart_version": getattr(pyart, "__version__", None) if pyart else None,
        "cache_db": os.path.exists(DB_PATH),
    })


@app.route("/")
def root():
    return jsonify({
        "service": "PropertyWX MRMS forensic lookup",
        "endpoints": {
            "GET /api/mesh":          "params: lat, lon, date (YYYY-MM-DD); returns mesh_in (24h max) + source",
            "GET /api/event-detail":  "params: lat, lon, date; returns full multi-signal record (mesh max + peak hour + posh)",
            "GET /api/nexrad-verify": "params: lat, lon, date, [peak_window]; NEXRAD Level 2 hail verification (Phase D)",
            "GET /health":            "service status",
        },
    })


try:
    cleanup_old_grib_files()
except Exception as _cleanup_err:  # noqa: BLE001
    LOG.warning(f"cleanup_old_grib_files skipped: {_cleanup_err!r}")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)
