"""Local smoke test for the MESH service.

Usage:
    # In one terminal:
    python app.py

    # In another:
    python test.py
"""
import requests
import sys

BASE = "http://localhost:8000"

print("Health check...")
r = requests.get(f"{BASE}/health")
print(f"  {r.status_code}: {r.json()}")
assert r.status_code == 200, "Health check failed"

print("\nFuture date should 400:")
r = requests.get(f"{BASE}/api/mesh", params={"lat": 32.0, "lon": -97.0, "date": "2099-01-01"})
print(f"  {r.status_code}: {r.json()}")
assert r.status_code == 400, "Future date not rejected"

print("\nPre-2014 should return null:")
r = requests.get(f"{BASE}/api/mesh", params={"lat": 32.0, "lon": -97.0, "date": "2010-04-13"})
print(f"  {r.status_code}: {r.json()}")
assert r.status_code == 200 and r.json()["mesh_in"] is None

print("\nInvalid input should 400:")
r = requests.get(f"{BASE}/api/mesh", params={"lat": "abc", "lon": -97.0, "date": "2024-04-15"})
print(f"  {r.status_code}: {r.json()}")
assert r.status_code == 400

print("\nReal lookup (Texas, April 2024 hailstorm)...")
r = requests.get(f"{BASE}/api/mesh", params={"lat": 32.78, "lon": -96.80, "date": "2024-04-15"}, timeout=120)
print(f"  {r.status_code}: {r.json()}")
# Don't strictly assert on the value — depends on whether the file is reachable.

print("\nSecond lookup of same point (should be cached):")
r = requests.get(f"{BASE}/api/mesh", params={"lat": 32.78, "lon": -96.80, "date": "2024-04-15"}, timeout=10)
print(f"  {r.status_code}: {r.json()}")
assert "(cached)" in (r.json().get("source") or "")

print("\nAll tests passed.")
