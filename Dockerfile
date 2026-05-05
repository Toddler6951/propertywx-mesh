# MESH point-query service for PropertyWX.
# Python + eccodes (system library) for GRIB2 parsing.
FROM python:3.12-slim

# System libraries:
#   libeccodes0          — GRIB2 parsing for the MRMS pipeline (eccodes Python bindings).
#   libhdf5-dev / libnetcdf-dev — required by netCDF4 wheel build / arm-pyart for
#                          NEXRAD Level 2 archive parsing.
#   ca-certificates curl — TLS roots + the healthcheck shell command.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libeccodes0 libeccodes-tools \
        libhdf5-dev libnetcdf-dev \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all Python source. Glob picks up app.py + any helper modules
# (wsr88d_sites.py, etc.) without needing to update this list each time.
COPY *.py ./

# Persistent volume mount point (Railway: configure a Volume on /data).
RUN mkdir -p /data /data/grib_cache

# Healthcheck so Railway / your monitoring can verify the service.
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:${PORT:-8000}/health || exit 1

# Port. Railway sets $PORT; default 8000 for local.
ENV PORT=8000
EXPOSE 8000

# Use gunicorn for production (multi-worker, robust). 2 workers, 4 threads each
# is a fine default for a small service that's mostly I/O-bound on GRIB downloads.
CMD gunicorn --bind 0.0.0.0:${PORT:-8000} --workers 2 --threads 4 --timeout 120 app:app
