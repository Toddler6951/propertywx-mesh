# PropertyWX MESH service

Tiny Python service that returns the **Maximum Estimated Size of Hail (MESH)**
at a point, on a given date, by reading NOAA MRMS GRIB2 files server-side.

Used by the [PropertyWX storm-claim tool](https://propertywx.com) to verify
spotter-report-based hail estimates against radar.

## Endpoint

```
GET /api/mesh?lat=<float>&lon=<float>&date=<YYYY-MM-DD>
```

Returns:

```json
{
  "mesh_in": 1.85,
  "source": "Iowa State",
  "lat_q": 32.78,
  "lon_q": -96.80
}
```

`mesh_in` is the maximum estimated hail diameter in inches, derived from radar
reflectivity, at the nearest 1km grid cell to the requested point. `null` when
no data is available (date too old, point outside CONUS coverage, or the
specific GRIB2 file wasn't on either source).

## How it works

1. Round (lat, lon) to 0.01° (~0.7 mi) for the cache key.
2. Look up SQLite cache. Hit → return immediately.
3. Miss → download `MESH_Max_1440min` GRIB2 for the date — first try AWS
   `noaa-mrms-pds` (recent only, ~last 30 days), then fall back to
   Iowa State `mtarchive` (archive going back to ~2014).
4. Use `eccodes` to find the nearest grid cell value.
5. Convert mm → inches; cache; return.

The `MESH_Max_1440min` product is a running 24-hour maximum, so the file
timestamped at the end of UTC day X captures all storms that occurred during
day X.

## Deploying to Railway

1. Push this folder to a new GitHub repo (e.g., `propertywx-mesh`).
2. [railway.com](https://railway.com) → New Project → Deploy from GitHub repo.
   Railway detects the `Dockerfile` automatically.
3. **Add a Volume** under the service settings. Mount path: `/data`.
   Without this, the cache DB and GRIB files reset on every redeploy.
4. **Add a custom domain**: Service → Settings → Networking → Custom Domain.
   Use `mesh.propertywx.com`. Railway gives you a CNAME target — add a CNAME
   record at Squarespace DNS pointing `mesh` → that target.
5. Wait for HTTPS provisioning (1–10 min after DNS propagates).
6. Visit `https://mesh.propertywx.com/health` — should return `{"status":"ok","eccodes":true,...}`.

## CORS

The service only allows requests from these origins (set in `app.py`):

- `https://propertywx.com`
- `https://app.propertywx.com`
- `https://insights.propertywx.com`
- `https://toddler6951.github.io`
- `http://localhost:8000` (local dev)

Add more in `ALLOWED_ORIGINS` if needed.

## Local development

```bash
docker build -t mesh-service .
docker run -p 8000:8000 mesh-service
# In another terminal:
python test.py
```

Or without Docker (requires `libeccodes0` installed at the system level):

```bash
pip install -r requirements.txt
python app.py
```

Visit `http://localhost:8000/health`.

## Cost

At PropertyWX's current scale (~500 users/month, ~1k MESH lookups/month
after caching), this fits inside Railway's $5/month base allocation easily —
the service spends 99%+ of its time idle.

## Limitations

- MRMS MESH archive begins **~January 2014**. Pre-2014 returns `null`.
- Coverage is **CONUS only** (continental U.S. + a buffer). Points in Alaska,
  Hawaii, Puerto Rico, etc. return `null`.
- The 1440-min product is a 24h running max; daily granularity, not hourly.
- Some specific dates / minutes may be missing on both sources due to
  data outages. The service tries several timestamps before giving up.
- Values < 0 mm are treated as "missing" and returned as `null`. MRMS uses
  -3 / -999 to indicate missing data.

## Methodology references

- [NOAA NSSL MRMS](https://www.nssl.noaa.gov/projects/mrms/) — system overview
- [MRMS GRIB2 Tables](https://www.nssl.noaa.gov/projects/mrms/operational/tables.php) — product specs
- [NOAA MRMS PDS on AWS](https://registry.opendata.aws/noaa-mrms-pds/) — recent archive
- [Iowa State mtarchive](https://mtarchive.geol.iastate.edu/) — historical archive
- [WDTD: Maximum Estimated Size of Hail (MESH)](https://vlab.noaa.gov/web/wdtd/-/maximum-estimated-size-of-hail-mes-2) — algorithm description
