# MESH point-query service for PropertyWX.
# Python + eccodes (system library) for GRIB2 parsing.
FROM python:3.12-slim

# eccodes: system library required by the Python bindings to parse GRIB2 files.
# build-essential + python3-dev: for any pip wheels that need compilation.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libeccodes0 libeccodes-tools \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

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
