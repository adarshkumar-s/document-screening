#!/usr/bin/env python3
"""Reproducible mapping performance benchmark on SYNTHETIC data.

All rows created here are clearly labelled synthetic fixtures for measurement
only — never presented as real land records. Run from the repository root:

    python scripts/map_perf_bench.py --sizes 100 1000 10000

Measured separately (spec §21):
  * map listing API latency (/api/map/records) — full page and bounding box,
  * database statement count during a listing request,
  * summary endpoint latency,
  * parcel candidate search latency (per-document, as the review queue does),
  * review-queue recompute latency,
  * geometry validation latency,
  * CSV + GeoJSON export time.

Environment assumptions: the same machine, an isolated temporary SQLite
database, one uvicorn worker via FastAPI TestClient (no network). Numbers are
local measurements, NOT production claims.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(prefix="map-bench-"), "bench.db"))
os.environ.setdefault("ADMIN_INITIAL_PASSWORD", "Bench@12345")
os.environ.setdefault("SA_AUTO_INVESTIGATE", "0")

from fastapi.testclient import TestClient  # noqa: E402

import mapping  # noqa: E402
import server  # noqa: E402
from main import app  # noqa: E402

SYNTHETIC_NOTE = "SYNTHETIC BENCHMARK DATA — not real land records"


def make_user() -> dict:
    email = f"bench-{uuid.uuid4().hex[:8]}@example.test"
    client.post("/api/auth/signup", json={"full_name": "Bench", "email": email, "password": "Bench Password 123!"})
    with server.get_db() as db:
        db.execute("UPDATE users SET role=? WHERE email=?", ("VERIFICATION_OFFICER", email))
    login = client.post("/api/auth/login", json={"email": email, "password": "Bench Password 123!"})
    return {"Authorization": "Bearer " + login.json()["token"]}


def seed(count: int) -> None:
    """Insert `count` synthetic documents plus reference parcels."""
    rng = random.Random(42)  # deterministic dataset
    now = 1_700_000_000.0
    with server.get_db() as db:
        for index in range(count):
            fields = {
                "owner_name": {"value": f"Synthetic Holder {index}", "confidence": 0.9},
                "survey_number": {"value": str(100000 + index), "confidence": 0.95},
                "village": {"value": f"SynthVillage{index % 50}", "confidence": 0.95},
                "tehsil": {"value": f"SynthTaluka{index % 10}", "confidence": 0.9},
                "district": {"value": f"SynthDistrict{index % 5}", "confidence": 0.9},
                "area": {"value": f"{rng.uniform(0.5, 5.0):.2f} ha", "confidence": 0.9},
            }
            lat = 20.0 + (index % 9000) / 1000.0
            lon = 70.0 + ((index // 9000) % 9000) / 1000.0
            db.execute(
                """INSERT OR REPLACE INTO documents
                (id,filename,doc_type,mean_conf,verdict,status,languages,pages,fields,validation,
                 ai_decision_support,ocr_text,cleaned_ocr_text,detected_language,original_fields,
                 uploaded_by,created_at,updated_at,lat,lon)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"BENCH-{index:06d}", f"bench-{index}.pdf", "Land Record", 90, "review", "APPROVED",
                 "[]", 1, json.dumps(fields), "{}", "{}", "synthetic", "", "eng", json.dumps(fields),
                 "bench@example.test", now + index, now + index, lat, lon),
            )
        parcel_count = min(count, 2000)
        for index in range(parcel_count):
            db.execute(
                """INSERT OR REPLACE INTO properties (property_id,parcel_id,district,taluka,village,
                   survey_number,area,area_unit,geometry,centroid,latitude,longitude,crs,georeferenced,
                   geometry_source,data_source,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"BENCH-PROP-{index:06d}", f"BENCH-PARCEL-{index:06d}",
                 f"SynthDistrict{index % 5}", f"SynthTaluka{index % 10}", f"SynthVillage{index % 50}",
                 str(100000 + index), 1.0, "ha",
                 json.dumps({"type": "Polygon", "coordinates": [[[70.0 + index * 1e-4, 20.0],
                                                                [70.0005 + index * 1e-4, 20.0],
                                                                [70.0005 + index * 1e-4, 20.0005],
                                                                [70.0 + index * 1e-4, 20.0005],
                                                                [70.0 + index * 1e-4, 20.0]]]}),
                 json.dumps([70.00025 + index * 1e-4, 20.00025]), 20.00025, 70.00025 + index * 1e-4,
                 "EPSG:4326", 1, SYNTHETIC_NOTE, SYNTHETIC_NOTE, now + index, now + index),
            )


def timed(fn, repeat: int = 5) -> dict:
    samples = []
    result = None
    for _ in range(repeat):
        start = time.perf_counter()
        result = fn()
        samples.append((time.perf_counter() - start) * 1000.0)
    status = getattr(result, "status_code", None)
    return {"p50_ms": round(statistics.median(samples), 1),
            "min_ms": round(min(samples), 1),
            "max_ms": round(max(samples), 1),
            "status": status}


def count_statements(fn, repeat: int = 3) -> int:
    counts = {"n": 0}
    original = server.DBConnection.execute
    def counting_execute(self, query, params=()):
        counts["n"] += 1
        return original(self, query, params)
    server.DBConnection.execute = counting_execute
    try:
        for _ in range(repeat):
            counts["n"] = 0
            fn()
    finally:
        server.DBConnection.execute = original
    return counts["n"]


def bench(size: int) -> None:
    headers = make_user()
    seed(size)
    mapping.ensure_schema()

    rows = []
    rows.append(("GET /api/map/records?limit=5000",
                 timed(lambda: client.get("/api/map/records?limit=5000", headers=headers))))
    rows.append(("GET /api/map/records bbox (viewport)",
                 timed(lambda: client.get(
                     "/api/map/records?min_lat=21&min_lon=71&max_lat=24&max_lon=74&limit=1000",
                     headers=headers))))
    rows.append(("GET /api/map/summary", timed(lambda: client.get("/api/map/summary", headers=headers))))
    rows.append(("GET /api/map/export.csv", timed(lambda: client.get("/api/map/export.csv", headers=headers), repeat=3)))
    rows.append(("GET /api/map/export.geojson", timed(lambda: client.get("/api/map/export.geojson", headers=headers), repeat=3)))
    rows.append(("GET /api/map/review-queue (recompute)",
                 timed(lambda: client.get("/api/map/review-queue", headers=headers), repeat=3)))
    doc_id = "BENCH-000000"
    rows.append(("GET parcel-candidates (single doc)",
                 timed(lambda: client.get(f"/api/map/records/{doc_id}/parcel-candidates", headers=headers))))
    rows.append(("GET spatial-checks (single doc)",
                 timed(lambda: client.get(f"/api/map/records/{doc_id}/spatial-checks", headers=headers))))

    ring = [[70.0, 20.0], [70.001, 20.0], [70.001, 20.001], [70.0, 20.001], [70.0, 20.0]]
    def validate_once():
        mapping._polygon_ring_from_geojson({"type": "Polygon", "coordinates": [ring]})
    rows.append(("geometry validation (pure fn)", timed(validate_once, repeat=50)))

    stmts = count_statements(lambda: client.get("/api/map/records?limit=5000", headers=headers), repeat=3)

    print(f"\n=== SYNTHETIC benchmark, dataset size {size} ({SYNTHETIC_NOTE}) ===")
    print(f"{'operation':44s} {'p50 ms':>8s} {'min ms':>8s} {'max ms':>8s} {'status':>6s}")
    for name, stats in rows:
        print(f"{name:44s} {stats['p50_ms']:8.1f} {stats['min_ms']:8.1f} {stats['max_ms']:8.1f} {str(stats['status']):>6s}")
    print(f"{'DB statements per listing request':44s} {stmts:8d}")


client = TestClient(app, raise_server_exceptions=False)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="*", type=int, default=[100, 1000, 10000])
    args = parser.parse_args()
    for dataset_size in args.sizes:
        bench(dataset_size)
    print("\nNOTE: local TestClient measurements on synthetic data; not production capacity claims.")
