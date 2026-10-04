"""Portfolio-derived document mapping and narrowly scoped compatibility support.

The public mapping surface is the document map and history workflow. A small
internal compatibility layer remains because the canonical OCR/AI/audit code
uses governed property resolution, ownership signals, and exact-location
helpers. Those helpers do not register the retired Land Intelligence routes or
serve the replacement map UI.
"""
from __future__ import annotations

import csv
import difflib
import hashlib
import io
import math
import os
import json
import re
import time
import threading
import unicodedata
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, model_validator

from server import (
    ROLE_ADMIN,
    ROLE_DATA_OFFICER,
    ROLE_VERIFICATION_OFFICER,
    ROLE_VIEWER,
    get_current_user,
    get_db,
    log_audit,
    require_roles,
    _parse_land_coordinate,
)

map_router = APIRouter(prefix="/api/map", tags=["Document Map"])
document_history_router = APIRouter(prefix="/api", tags=["Document History"])

# Unified record cap: listings, summaries, the review queue and every export
# must describe the SAME eligible dataset (audit defect D9). One constant keeps
# counts, clustering and exports consistent; responses expose it as
# `metadata.record_cap` so clients can detect truncation honestly.
MAP_RECORD_CAP = 10000
# Geometry ingestion limits (audit defect D4). The global request-body limit
# (security_hardening.MAX_REQUEST_BYTES) bounds payload size; this bounds the
# per-geometry vertex count so validation stays cheap and results deterministic.
MAP_MAX_IMPORT_VERTICES = 500
# Configurable recorded-vs-geometry area screening tolerance (percent).
# Local survey practices, map scale and source precision differ; the threshold
# is deliberately environment-configurable instead of a universal constant.
MAP_AREA_MISMATCH_PERCENT = float(os.getenv("MAP_AREA_MISMATCH_PERCENT", "25") or 25)
# Finding types this module generates and manages. Rows with other types (e.g.
# ai_governance's AI_PROPOSAL findings) or a non-null case_id are never touched
# by the map review-queue recompute (audit defect D3).
MAP_MANAGED_FINDING_TYPES = (
    "DUPLICATE_EXACT_LOCATION",
    "PIN_OUTSIDE_BOUNDARY",
    "PIN_OUTSIDE_REFERENCE",
    "RECORDED_AREA_MISMATCH",
    "REFERENCE_AREA_MISMATCH",
    "CONFLICTING_RECORDED_AREA",
    "DUPLICATE_SURVEY",
    "AMBIGUOUS_PARCEL",
)

MAP_NOMINATIM_USER_AGENT = os.getenv("MAP_GEOCODER_USER_AGENT", "Document-Screening-Portfolio-Map/1.0 (+https://github.com/adarshkumar-s/document-screening)")
MAP_GEOCODER_URL = os.getenv("MAP_GEOCODER_URL", "https://nominatim.openstreetmap.org/search")
MAP_GEOCODE_TTL_SECONDS = 7 * 24 * 60 * 60
_map_geocode_cache: Dict[str, Dict[str, Any]] = {}
_map_last_geocode_request = 0.0
_map_geocode_lock = threading.Lock()


def _now() -> float:
    return time.time()


LAND_IDENTIFIER_FIELDS = ("survey_number", "gat_number", "khasra_number")
# Cross-column aliases for strong identifiers: documents extract "gat no"
# into survey_number while the parcel table may store it in gat_number (and
# vice versa). Only consulted when the primary column is empty.
_SIBLING_IDENTIFIER_COLUMNS = {
    "survey_number": ("gat_number",),
    "gat_number": ("survey_number",),
    "khasra_number": ("gat_number",),
}


_SYNTHETIC_PROPERTIES = [
    {
        "property_id": "DEMO-PROP-103-A",
        "parcel_id": "DEMO-103-A",
        "district": "Demo District",
        "taluka": "Demo Taluka",
        "village": "Demo Village",
        "survey_number": "DEMO-103",
        "gat_number": None,
        "khasra_number": None,
        "sub_division": "A",
        "parent_property_id": None,
        "area": 2.31,
        "area_unit": "ha",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[77.1020, 28.6210], [77.1055, 28.6210], [77.1055, 28.6240], [77.1020, 28.6240], [77.1020, 28.6210]]],
        },
        "centroid": [77.10375, 28.6225],
        "latitude": 28.6225,
        "longitude": 77.10375,
        "crs": "EPSG:4326",
        "georeferenced": True,
        "geometry_source": "Project-owned synthetic geometry",
        "geometry_confidence": 0.99,
        "data_source": "Synthetic/demo dataset",
        "source_confidence": 0.99,
    },
    {
        "property_id": "DEMO-PROP-103-B",
        "parcel_id": "DEMO-103-B",
        "district": "Demo District",
        "taluka": "Demo Taluka",
        "village": "Demo Village",
        "survey_number": "DEMO-103",
        "gat_number": None,
        "khasra_number": None,
        "sub_division": "B",
        "parent_property_id": "DEMO-PROP-103-A",
        "area": 1.25,
        "area_unit": "ha",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[77.1060, 28.6210], [77.1095, 28.6210], [77.1095, 28.6240], [77.1060, 28.6240], [77.1060, 28.6210]]],
        },
        "centroid": [77.10775, 28.6225],
        "latitude": 28.6225,
        "longitude": 77.10775,
        "crs": "EPSG:4326",
        "georeferenced": True,
        "geometry_source": "Project-owned synthetic geometry",
        "geometry_confidence": 0.98,
        "data_source": "Synthetic/demo dataset",
        "source_confidence": 0.98,
    },
    {
        "property_id": "DEMO-PROP-104",
        "parcel_id": "DEMO-104",
        "district": "Demo District",
        "taluka": "Demo Taluka",
        "village": "Demo Village",
        "survey_number": "DEMO-104",
        "gat_number": None,
        "khasra_number": None,
        "sub_division": None,
        "parent_property_id": None,
        "area": 3.50,
        "area_unit": "ha",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[77.1020, 28.6250], [77.1065, 28.6250], [77.1065, 28.6285], [77.1020, 28.6285], [77.1020, 28.6250]]],
        },
        "centroid": [77.10425, 28.62675],
        "latitude": 28.62675,
        "longitude": 77.10425,
        "crs": "EPSG:4326",
        "georeferenced": True,
        "geometry_source": "Project-owned synthetic geometry",
        "geometry_confidence": 0.96,
        "data_source": "Synthetic/demo dataset",
        "source_confidence": 0.96,
    },
    {
        "property_id": "DEMO-PROP-105",
        "parcel_id": "DEMO-105",
        "district": "Demo District",
        "taluka": "Demo Taluka",
        "village": "Demo Village",
        "survey_number": "DEMO-105",
        "gat_number": None,
        "khasra_number": None,
        "sub_division": None,
        "parent_property_id": None,
        "area": 1.80,
        "area_unit": "ha",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[77.1080, 28.6250], [77.1115, 28.6250], [77.1115, 28.6285], [77.1080, 28.6285], [77.1080, 28.6250]]],
        },
        "centroid": [77.10975, 28.62675],
        "latitude": 28.62675,
        "longitude": 77.10975,
        "crs": "EPSG:4326",
        "georeferenced": True,
        "geometry_source": "Project-owned synthetic geometry",
        "geometry_confidence": 0.60,
        "data_source": "Synthetic/demo dataset",
        "source_confidence": 0.60,
    },
]


class LocationUpdate(BaseModel):
    """An exact pin update, or an empty body to restore the base location."""

    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    reason: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def coordinates_are_a_pair(self) -> "LocationUpdate":
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be supplied together")
        return self


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _parse_json(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except Exception:
        return fallback


def _field_value(fields: Optional[Dict[str, Any]], *keys: str) -> str:
    fields = fields or {}
    for key in keys:
        value = fields.get(key)
        if isinstance(value, dict):
            value = value.get("value", value.get("text", ""))
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return ""


def _field_confidence(fields: Optional[Dict[str, Any]], *keys: str) -> Optional[float]:
    fields = fields or {}
    for key in keys:
        value = fields.get(key)
        if isinstance(value, dict) and value.get("confidence") is not None:
            try:
                return float(value["confidence"])
            except (TypeError, ValueError):
                return None
        if value not in (None, ""):
            return None
    return None


def _normalise(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _land_number(value: Any) -> str:
    """Normalise Indic digits without collapsing cadastral separators."""
    text = str(value or "").strip()
    translated: List[str] = []
    for char in text:
        try:
            translated.append(str(unicodedata.digit(char)))
        except (TypeError, ValueError):
            translated.append(char)
    return "".join(translated).replace(" ", "")


def _key_for(field_name: str, value: Any) -> str:
    if field_name in LAND_IDENTIFIER_FIELDS:
        return _land_number(value).casefold()
    return _normalise(value)


def _number(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    text = str(value).replace(",", "")
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _record_year(record: Dict[str, Any]) -> Optional[int]:
    fields = record.get("fields") or {}
    candidates = (
        _field_value(fields, "khatauni_year", "year", "document_year"),
        _field_value(fields, "document_date", "registration_date", "date"),
        record.get("year"),
        record.get("created_at"),
    )
    for candidate in candidates:
        if candidate in (None, ""):
            continue
        match = re.search(r"\b(19\d{2}|20\d{2})\b", str(candidate))
        if match:
            return int(match.group(1))
        try:
            return int(float(candidate))
        except (TypeError, ValueError):
            pass
    return None


def _is_transfer_document(record: Dict[str, Any]) -> bool:
    text = " ".join(
        str(record.get(key) or "")
        for key in ("doc_type", "document_type", "filename", "ocr_text")
    ).casefold()
    return any(word in text for word in ("sale", "mutation", "transfer", "gift", "partition", "release", "conveyance"))


def _geometry_from_row(row: Any) -> Optional[Dict[str, Any]]:
    geometry = _parse_json(row["geometry"] if row and "geometry" in row.keys() else None, None)
    return geometry if isinstance(geometry, dict) else None


def _location_from_row(row: Any) -> Dict[str, Any]:
    status = row["location_status"] if "location_status" in row.keys() else None
    return {
        "status": status or ("PARCEL_GEOMETRY" if row["geometry"] else "UNRESOLVED"),
        "latitude": row["latitude"],
        "longitude": row["longitude"],
        "source": row["location_source"] if "location_source" in row.keys() else row["geometry_source"],
        "confidence": row["location_confidence"] if "location_confidence" in row.keys() else row["geometry_confidence"],
        "verified_by": row["location_verified_by"] if "location_verified_by" in row.keys() else None,
        "verified_at": row["location_verified_at"] if "location_verified_at" in row.keys() else None,
    }


def _property_dict(row: Any, *, include_geometry: bool = True) -> Dict[str, Any]:
    item = dict(row)
    item["geometry"] = _geometry_from_row(row) if include_geometry else None
    item["centroid"] = _parse_json(item.get("centroid"), item.get("centroid"))
    item["georeferenced"] = bool(item.get("georeferenced"))
    item["location"] = _location_from_row(row)
    item["resolution_status"] = item.get("resolution_status") or "MATCH"
    return item


def _property_row(db: Any, property_id: str) -> Any:
    return db.execute(
        "SELECT * FROM properties WHERE property_id=? OR parcel_id=? LIMIT 1",
        (property_id, property_id),
    ).fetchone()


def _ensure_column(db: Any, column: str, definition: str) -> None:
    try:
        if db.is_pg:
            db.execute(f"ALTER TABLE properties ADD COLUMN IF NOT EXISTS {column} {definition}")
            return
        columns = {row["name"] for row in db.execute("PRAGMA table_info(properties)").fetchall()}
        if column not in columns:
            db.execute(f"ALTER TABLE properties ADD COLUMN {column} {definition}")
    except Exception:
        # A concurrent startup may have added the column.  The subsequent
        # application query will surface a real schema problem if one remains.
        pass


def _ensure_document_column(db: Any, column: str, definition: str) -> None:
    """Add mapping-only document coordinates without touching OCR fields."""
    try:
        if db.is_pg:
            db.execute(f"ALTER TABLE documents ADD COLUMN IF NOT EXISTS {column} {definition}")
            return
        columns = {row["name"] for row in db.execute("PRAGMA table_info(documents)").fetchall()}
        if column not in columns:
            db.execute(f"ALTER TABLE documents ADD COLUMN {column} {definition}")
    except Exception:
        # Startup can race with another worker.  Keep the migration idempotent.
        pass


def _ensure_table_column(db: Any, table: str, column: str, definition: str) -> None:
    """Idempotently add a nullable column to any mapping-managed table."""
    try:
        if db.is_pg:
            db.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}")
            return
        columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    except Exception:
        # A concurrent startup may have added the column.
        pass


def _ensure_tables() -> None:
    """Create/migrate the local land schema and seed synthetic parcel geometry."""
    with get_db() as db:
        # The coordinates belong to the mapping layer only.  Existing OCR,
        # validation, and audit columns remain untouched.
        _ensure_document_column(db, "lat", "REAL")
        _ensure_document_column(db, "lon", "REAL")
        _ensure_document_column(db, "map_geometry", "TEXT")
        _ensure_document_column(db, "map_geometry_source", "TEXT")
        _ensure_document_column(db, "map_geometry_status", "TEXT")
        _ensure_document_column(db, "map_geometry_updated_at", "REAL")
        # Reason recorded with the CURRENT geometry (audit defect D8). The full
        # before/after chain lives in document_geometry_history.
        _ensure_document_column(db, "map_geometry_reason", "TEXT")
        _ensure_document_column(db, "location_accuracy_m", "REAL")
        _ensure_document_column(db, "location_source_detail", "TEXT")
        _ensure_document_column(db, "location_reason", "TEXT")
        # The audited exact-pin write path (set/clear document pin) stores the
        # acting reviewer and timestamp on the document row itself; without
        # these columns the UPDATE raises "no such column" and every pin
        # change fails.  Kept idempotent and mapping-only like the rest.
        _ensure_document_column(db, "location_verified_by", "TEXT")
        _ensure_document_column(db, "location_verified_at", "REAL")
        db.execute(
            """CREATE TABLE IF NOT EXISTS properties (
                property_id TEXT PRIMARY KEY,
                parcel_id TEXT UNIQUE NOT NULL,
                district TEXT,
                taluka TEXT,
                village TEXT,
                survey_number TEXT,
                gat_number TEXT,
                khasra_number TEXT,
                sub_division TEXT,
                parent_property_id TEXT,
                area REAL,
                area_unit TEXT,
                geometry TEXT,
                centroid TEXT,
                latitude REAL,
                longitude REAL,
                crs TEXT,
                georeferenced INTEGER NOT NULL DEFAULT 0,
                geometry_source TEXT,
                geometry_confidence REAL,
                data_source TEXT,
                source_confidence REAL,
                created_at REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0
            )"""
        )
        for column, definition in (
            ("location_status", "TEXT NOT NULL DEFAULT 'UNRESOLVED'"),
            ("location_source", "TEXT"),
            ("location_confidence", "REAL"),
            ("location_verified_by", "TEXT"),
            ("location_verified_at", "REAL"),
            ("location_updated_at", "REAL"),
            ("location_base_latitude", "REAL"),
            ("location_base_longitude", "REAL"),
            ("location_base_source", "TEXT"),
        ):
            _ensure_column(db, column, definition)

        db.execute(
            """CREATE TABLE IF NOT EXISTS property_documents (
                property_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                source_type TEXT NOT NULL DEFAULT 'uploaded_document',
                linked_at REAL NOT NULL,
                PRIMARY KEY(property_id, document_id)
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS provenance (
                id TEXT PRIMARY KEY,
                property_id TEXT NOT NULL,
                field_name TEXT NOT NULL,
                value TEXT,
                source TEXT NOT NULL,
                confidence REAL,
                created_at REAL NOT NULL
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS property_timeline (
                id TEXT PRIMARY KEY,
                property_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                description TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at REAL NOT NULL
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS verification_cases (
                case_id TEXT PRIMARY KEY,
                property_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN',
                assigned_officer TEXT,
                findings TEXT NOT NULL DEFAULT '[]',
                warnings TEXT NOT NULL DEFAULT '[]',
                comparison_results TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS verification_findings (
                finding_id TEXT PRIMARY KEY,
                property_id TEXT NOT NULL,
                case_id TEXT,
                finding_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN',
                title TEXT NOT NULL,
                evidence TEXT NOT NULL DEFAULT '{}',
                created_by TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS verification_tasks (
                task_id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL,
                property_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN',
                assigned_officer TEXT,
                title TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS geocode_cache (
                cache_key TEXT PRIMARY KEY,
                village TEXT NOT NULL,
                taluka TEXT,
                district TEXT,
                state TEXT,
                latitude REAL,
                longitude REAL,
                display_name TEXT,
                status TEXT NOT NULL,
                created_at REAL NOT NULL
            )"""
        )
        # Recoverable before/after geometry history for every write path
        # (draw, import, estimate, clear). Previous geometries are never lost
        # silently (audit defect D8).
        db.execute(
            """CREATE TABLE IF NOT EXISTS document_geometry_history (
                id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                action TEXT NOT NULL,
                previous_geometry TEXT,
                new_geometry TEXT,
                previous_source TEXT,
                new_source TEXT,
                reason TEXT,
                actor TEXT,
                created_at REAL NOT NULL
            )"""
        )
        for index_sql in (
            "CREATE INDEX IF NOT EXISTS idx_geom_history_document ON document_geometry_history(document_id, created_at)",
        ):
            db.execute(index_sql)
        # Resolution metadata for findings (audit defect D2): keeps an
        # auditable record of WHO resolved/ dismissed a finding and WHY.
        _ensure_table_column(db, "verification_findings", "resolution_note", "TEXT")
        _ensure_table_column(db, "verification_findings", "resolved_by", "TEXT")
        _ensure_table_column(db, "verification_findings", "resolved_at", "REAL")

        # Mapping lookup indexes keep parcel/reference queries off full-table scans.
        for index_sql in (
            "CREATE INDEX IF NOT EXISTS idx_properties_village ON properties(village)",
            "CREATE INDEX IF NOT EXISTS idx_properties_village_taluka ON properties(village, taluka)",
            "CREATE INDEX IF NOT EXISTS idx_properties_village_taluka_district ON properties(village, taluka, district)",
            "CREATE INDEX IF NOT EXISTS idx_properties_survey ON properties(survey_number)",
            "CREATE INDEX IF NOT EXISTS idx_property_documents_document ON property_documents(document_id)",
            "CREATE INDEX IF NOT EXISTS idx_property_documents_property ON property_documents(property_id)",
        ):
            db.execute(index_sql)

        now = _now()
        for seed in _SYNTHETIC_PROPERTIES:
            geometry = _json(seed["geometry"])
            centroid = _json(seed["centroid"])
            # Do not overwrite a human exact pin on subsequent application
            # startups.  New records are fully initialized with their base pin.
            db.execute(
                """INSERT INTO properties (
                    property_id,parcel_id,district,taluka,village,survey_number,gat_number,
                    khasra_number,sub_division,parent_property_id,area,area_unit,geometry,
                    centroid,latitude,longitude,crs,georeferenced,geometry_source,
                    geometry_confidence,data_source,source_confidence,created_at,updated_at,
                    location_status,location_source,location_confidence,location_base_latitude,
                    location_base_longitude,location_base_source
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(property_id) DO NOTHING""",
                (
                    seed["property_id"], seed["parcel_id"], seed["district"], seed["taluka"],
                    seed["village"], seed["survey_number"], seed["gat_number"], seed["khasra_number"],
                    seed["sub_division"], seed["parent_property_id"], seed["area"], seed["area_unit"],
                    geometry, centroid, seed["latitude"], seed["longitude"], seed["crs"],
                    int(seed["georeferenced"]), seed["geometry_source"], seed["geometry_confidence"],
                    seed["data_source"], seed["source_confidence"], now, now, "PARCEL_GEOMETRY",
                    seed["geometry_source"], seed["geometry_confidence"], seed["latitude"],
                    seed["longitude"], seed["geometry_source"],
                ),
            )
            db.execute(
                """INSERT INTO provenance(id,property_id,field_name,value,source,confidence,created_at)
                   VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING""",
                (f"PROV-{seed['property_id']}-IDENTITY", seed["property_id"], "survey_number",
                 seed["survey_number"], seed["data_source"], seed["source_confidence"], now),
            )
            db.execute(
                """INSERT INTO property_timeline(id,property_id,event_type,description,source,created_at)
                   VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING""",
                (f"TL-{seed['property_id']}-CREATED", seed["property_id"], "DEMO_DATASET_CREATED",
                 "Synthetic parcel created for demonstration; not an authoritative cadastral record.",
                 seed["data_source"], now),
            )


def _property_by_id(property_id: str) -> Optional[Dict[str, Any]]:
    ensure_schema()
    with get_db() as db:
        row = _property_row(db, property_id)
    return _property_dict(row) if row else None


def _all_property_rows() -> List[Any]:
    """Load the reference parcel table once so batch callers (review queue,
    findings recompute) do not re-query it per record (audit defect D11)."""
    ensure_schema()
    with get_db() as db:
        return db.execute("SELECT * FROM properties ORDER BY property_id").fetchall()


_INDEXED_FIELDS = (
    "survey_number", "gat_number", "khasra_number", "sub_division",
    "village", "taluka", "district",
)


def _build_property_index(rows: Sequence[Any]) -> Dict[str, Any]:
    """Inverted index over the reference parcels for O(1) candidate lookup.

    Without this, batch matching (the review queue) compares every record
    against every parcel and materialises each parcel's JSON geometry —
    measured at 24.7 s for 1 000 records / 2 000 parcels and >8 minutes for
    10 000 records (scripts/map_perf_bench.py). Keys mirror the SQL prefilter
    exactly (identifier columns with sibling aliases, geography columns), and
    the candidate loop below remains the source of truth for matching."""
    ident: Dict[Tuple[str, str], List[Any]] = {}
    geo: Dict[Tuple[str, str], List[Any]] = {}
    row_keys: Dict[int, Dict[str, str]] = {}
    for row in rows:
        keys: Dict[str, str] = {}
        for column in ("survey_number", "gat_number", "khasra_number"):
            value = row[column] if column in row.keys() else None
            if value:
                key = _land_number(value).casefold()
                ident.setdefault((column, key), []).append(row)
                keys[column] = key
        for column in ("village", "taluka", "district"):
            value = row[column] if column in row.keys() else None
            if value:
                key = _normalise(value)
                geo.setdefault((column, key), []).append(row)
                keys[column] = key
        value = row["sub_division"] if "sub_division" in row.keys() else None
        if value:
            keys["sub_division"] = _key_for("sub_division", value)
        # Normalised keys are computed ONCE per parcel here; the candidate loop
        # then compares precomputed strings instead of re-normalising every
        # document value per row (profiled at 32M _key_for calls per queue
        # recompute on a 10k dataset).
        row_keys[id(row)] = keys
    return {"ident": ident, "geo": geo, "rows": list(rows), "row_keys": row_keys}


def _indexed_candidates(index: Dict[str, Any], supplied: Dict[str, str]) -> List[Any]:
    """Rows that could satisfy at least one match clause, in stable order."""
    picked: List[Any] = []
    seen: set = set()

    def add(rows: Sequence[Any]) -> None:
        for row in rows:
            marker = id(row)
            if marker not in seen:
                seen.add(marker)
                picked.append(row)

    any_clause = False
    for field_name in LAND_IDENTIFIER_FIELDS:
        value = supplied.get(field_name)
        if not value:
            continue
        any_clause = True
        key = _land_number(value).casefold()
        for column in (field_name, *_SIBLING_IDENTIFIER_COLUMNS.get(field_name, ())):
            add(index["ident"].get((column, key), ()))
    for field_name, column in (("village", "village"), ("taluka", "taluka"), ("district", "district")):
        value = supplied.get(field_name)
        if not value:
            continue
        any_clause = True
        add(index["geo"].get((column, _normalise(value)), ()))
    if not any_clause:
        # Equivalent to the empty WHERE clause: every row is eligible (e.g. a
        # document that only carries a subdivision).
        return list(index["rows"])
    # No re-sort here: each bucket list was built in property_id order and the
    # union preserves that determinism; `_resolve` finishes with a total sort
    # (score, then property_id), so ranking never depends on input order.
    # Rebuilding an order map per call measured as the dominant queue cost.
    return picked


def _resolve(fields: Dict[str, Any], property_rows: Optional[Sequence[Any]] = None,
             property_index: Optional[Dict[str, Any]] = None,
             materialize_properties: bool = True) -> Dict[str, Any]:
    """Resolve extracted identity fields to controlled parcel candidates.

    Returns a bounded candidate list where each candidate separates:
      evidence.positive      — fields that matched the parcel record,
      evidence.contradictory — fields that conflict with the parcel record,
      evidence.missing       — identity fields with no usable value on either side,
      evidence.source_reliability — what the matched data source is (and is not).

    `resolution_status` is calibrated from evaluated evidence only:
      MATCH                — a strong cadastral identifier matched with no conflicts,
      AMBIGUOUS_MATCH      — candidates are too close to separate (human review),
      INSUFFICIENT_EVIDENCE— only weak geography matches; never auto-selectable,
      POSSIBLE MATCH       — strong match exists but evidence is incomplete,
      NO MATCH             — identifier conflicts or nothing matched,
      INSUFFICIENT DATA    — no identity fields were extracted.

    The legacy `status` key keeps its historical vocabulary for existing
    consumers (server.py property resolution, sa_investigation).
    """
    ensure_schema()
    supplied = {
        "survey_number": _field_value(fields, "survey_number"),
        "gat_number": _field_value(fields, "gat_number"),
        "khasra_number": _field_value(fields, "khasra_number"),
        "sub_division": _field_value(fields, "sub_division", "subdivision"),
        "village": _field_value(fields, "village"),
        "taluka": _field_value(fields, "taluka", "tehsil"),
        "district": _field_value(fields, "district"),
    }
    available = {key: value for key, value in supplied.items() if value}
    if not available:
        return {
            "status": "INSUFFICIENT DATA",
            "resolution_status": "INSUFFICIENT DATA",
            "confidence": 0.0,
            "matches": [],
            "conflicting_fields": [],
            "reasons": ["No controlled property identity fields were extracted."],
            "evidence_summary": {"positive": [], "contradictory": [], "missing": list(supplied), "source_reliability": None},
        }

    if property_index is not None:
        # Batch path: O(candidate) lookup instead of a full scan per record.
        rows = _indexed_candidates(property_index, available)
    elif property_rows is None:
        with get_db() as db:
            clauses: List[str] = []
            params: List[Any] = []
            for field_name in LAND_IDENTIFIER_FIELDS:
                value = supplied.get(field_name)
                if value:
                    key = _land_number(value).casefold()
                    sibling = _SIBLING_IDENTIFIER_COLUMNS.get(field_name, ())
                    cols = [field_name, *sibling]
                    clauses.append("(" + " OR ".join([f"LOWER(REPLACE(COALESCE({col}, ''), ' ', ''))=?" for col in cols]) + ")")
                    params.extend([key] * len(cols))
            for field_name, column in (("village", "village"), ("taluka", "taluka"), ("district", "district")):
                value = supplied.get(field_name)
                if value:
                    clauses.append(f"LOWER(TRIM(COALESCE({column}, ''))) = ?")
                    params.append(_normalise(value))
            where = " WHERE " + " OR ".join(clauses) if clauses else ""
            rows = db.execute("SELECT * FROM properties" + where + " ORDER BY property_id LIMIT 2000", tuple(params)).fetchall()
    else:
        # Preloaded rows: the candidate loop below re-checks every field, so a
        # full scan produces identical results to the SQL prefilter.
        rows = list(property_rows)

    # Document values are normalised ONCE; the loop below previously
    # re-normalised them per parcel row (the profiler recorded 32M helper
    # calls per 10k-record review-queue recompute).
    available_keys = {field: _key_for(field, value) for field, value in available.items()}
    row_key_map: Dict[int, Dict[str, str]] = property_index.get("row_keys", {}) if property_index else {}

    # Candidates accumulate as flat tuples — (score, status, matched,
    # conflicting, row). JSON-shaped dicts (reason strings, evidence lists,
    # geometry hydration) are built only for the top slice returned.
    cands: List[Tuple[float, str, List[str], List[str], Any]] = []
    for row in rows:
        rkeys = row_key_map.get(id(row))
        matched: List[str] = []
        conflicting: List[str] = []
        for field_name, document_value in available.items():
            if rkeys is not None:
                parcel_key = rkeys.get(field_name)
                if parcel_key is None:
                    # The same land number can live in a sibling column.
                    # Cross-check siblings ONLY when the primary column is
                    # empty so a genuine conflict can never be masked.
                    for sibling in _SIBLING_IDENTIFIER_COLUMNS.get(field_name, ()):
                        sibling_key = rkeys.get(sibling)
                        if sibling_key is not None and sibling_key == available_keys[field_name]:
                            matched.append(sibling)
                            break
                elif parcel_key == available_keys[field_name]:
                    matched.append(field_name)
                else:
                    conflicting.append(field_name)
                continue
            parcel_value = row[field_name] if field_name in row.keys() else None
            if parcel_value:
                if available_keys[field_name] == _key_for(field_name, parcel_value):
                    matched.append(field_name)
                else:
                    conflicting.append(field_name)
                continue
            for sibling in _SIBLING_IDENTIFIER_COLUMNS.get(field_name, ()):
                sibling_value = row[sibling] if sibling in row.keys() else None
                if sibling_value and available_keys[field_name] == _key_for(sibling, sibling_value):
                    matched.append(sibling)
                    break
        if not matched:
            continue
        strong_matches = [field for field in matched if field in LAND_IDENTIFIER_FIELDS]
        geography_matches = [field for field in matched if field in ("village", "taluka", "district")]
        score = min(1.0, (len(strong_matches) * 0.48) + (len(geography_matches) * 0.14) + (0.10 if "sub_division" in matched else 0))
        if strong_matches and not conflicting:
            status_value = "MATCH"
        elif strong_matches:
            status_value = "NO MATCH" if len(conflicting) >= 1 else "POSSIBLE MATCH"
        else:
            status_value = "POSSIBLE MATCH"
        cands.append((score, status_value, matched, conflicting, row))

    cands.sort(key=lambda item: (item[1] == "NO MATCH", -item[0], item[4]["property_id"]))

    if not cands:
        return {
            "status": "NO MATCH",
            "resolution_status": "NO MATCH",
            "confidence": 0.0,
            "matches": [],
            "conflicting_fields": [],
            "reasons": ["No controlled parcel matched the extracted identity."],
            "evidence_summary": {
                "positive": [],
                "contradictory": [],
                "missing": [field for field in supplied if field not in available],
                "source_reliability": "Project-owned controlled parcel register (reference data); "
                                      "not an official cadastral source.",
            },
        }

    def _build_candidate(item: Tuple[float, str, List[str], List[str], Any]) -> Dict[str, Any]:
        score, status_value, matched, conflicting, row = item
        strong_matches = [field for field in matched if field in LAND_IDENTIFIER_FIELDS]
        missing_evidence = [
            field for field in supplied
            if field not in matched and field not in conflicting
        ]
        candidate: Dict[str, Any] = {
            "score": round(score, 4),
            "confidence": round(min(0.99, max(score, 0.35 if strong_matches else 0.2)), 4),
            "matched_fields": matched,
            "conflicting_fields": conflicting,
            "reasons": [
                *(f"Matched {field}." for field in matched),
                *(f"Conflicting {field}." for field in conflicting),
                *(f"No usable {field} value on both sides." for field in missing_evidence),
            ],
            "status": status_value,
            # Evidence is exposed separately so callers never have to infer
            # "absence of conflict" from a single score (spec §4.7).
            "evidence": {
                "positive": list(matched),
                "contradictory": list(conflicting),
                "missing": missing_evidence,
                "source_reliability": "Project-owned controlled parcel register (reference data); "
                                      "not an official cadastral source.",
            },
        }
        if materialize_properties:
            # JSON geometry parsing happens only here — scoring-only callers
            # (review queue) skip it entirely.
            candidate["property"] = _property_dict(row)
        return candidate

    candidates = [_build_candidate(item) for item in cands[:10]]

    best = candidates[0]
    best_strong = [field for field in (best.get("matched_fields") or []) if field in LAND_IDENTIFIER_FIELDS]
    # Ambiguity ("several parcels equally plausible") only exists when a strong
    # cadastral identifier matched; weak geography-only crowds are
    # INSUFFICIENT_EVIDENCE, not AMBIGUOUS_MATCH.
    ambiguous = bool(best_strong) and len(cands) > 1 and best["score"] < 0.99 and (best["score"] - candidates[1]["score"]) < 0.12
    # resolution_status is derived ONLY from evaluated evidence:
    # * close candidates -> AMBIGUOUS_MATCH (human selection required),
    # * geography-only matches -> INSUFFICIENT_EVIDENCE (never a MATCH),
    # * identifier conflict on the best row -> NO MATCH,
    # * otherwise the legacy best status (MATCH / POSSIBLE MATCH).
    if ambiguous:
        resolution_status = "AMBIGUOUS_MATCH"
    elif not best_strong:
        resolution_status = "INSUFFICIENT_EVIDENCE"
    elif best["status"] == "NO MATCH":
        resolution_status = "NO MATCH"
    else:
        resolution_status = best["status"]
    status = "AMBIGUOUS_MATCH" if ambiguous else best["status"]
    confidence = min(best["confidence"], 0.60) if ambiguous else best["confidence"]
    reasons = [*best["reasons"], "Multiple controlled parcels are similarly plausible; human parcel selection is required."] if ambiguous else best["reasons"]
    if resolution_status == "INSUFFICIENT_EVIDENCE":
        reasons = [*reasons, "No strong cadastral identifier matched; this candidate list is insufficient evidence for an automatic link."]
    all_conflicts = list(best.get("conflicting_fields") or [])
    all_positive = list(best.get("matched_fields") or [])
    return {
        "status": status,
        "resolution_status": resolution_status,
        "confidence": confidence,
        "matches": candidates,
        "candidate_count": len(cands),
        "conflicting_fields": all_conflicts,
        "reasons": reasons,
        "evidence_summary": {
            "positive": all_positive,
            "contradictory": all_conflicts,
            "missing": best.get("evidence", {}).get("missing", []),
            "source_reliability": best.get("evidence", {}).get("source_reliability"),
        },
    }


def _same_property_context(first: Dict[str, Any], second: Dict[str, Any]) -> bool:
    a = first.get("fields") or {}
    b = second.get("fields") or {}
    for keys in (("village",), ("district",), ("taluka", "tehsil")):
        av = _field_value(a, *keys)
        bv = _field_value(b, *keys)
        if av and bv and _key_for(keys[0], av) != _key_for(keys[0], bv):
            return False
    identifiers = [
        (_field_value(a, "survey_number"), _field_value(b, "survey_number"), "survey_number"),
        (_field_value(a, "gat_number"), _field_value(b, "gat_number"), "gat_number"),
        (_field_value(a, "khasra_number"), _field_value(b, "khasra_number"), "khasra_number"),
    ]
    return any(left and right and _key_for(field, left) == _key_for(field, right) for left, right, field in identifiers)


def _finding(kind: str, severity: str, title: str, reason: str, evidence: Iterable[Any]) -> Dict[str, Any]:
    return {
        "type": kind,
        "finding_type": kind,
        "severity": severity,
        "title": title,
        "reason": reason,
        "human_action": "Verification required",
        "evidence": list(evidence),
    }


def analyze_ownership_history(records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Produce conservative, review-only ownership-history signals."""
    clean = [dict(record) for record in records if isinstance(record, dict)]
    clean.sort(key=lambda record: ((_record_year(record) is None), _record_year(record) or 9999, str(record.get("id"))))
    events: List[Dict[str, Any]] = []
    for record in clean:
        fields = record.get("fields") or {}
        events.append({
            "document_id": record.get("id"),
            "filename": record.get("filename"),
            "document_type": record.get("doc_type") or record.get("document_type") or "Land Record",
            "year": _record_year(record),
            "owner": _field_value(fields, "owner_name", "owner"),
            "survey_number": _field_value(fields, "survey_number", "gat_number"),
            "khasra_number": _field_value(fields, "khasra_number"),
            "area": _field_value(fields, "area", "land_area", "plot_area"),
            "verification_status": record.get("status") or "UNKNOWN",
            "transfer_document": _is_transfer_document(record),
        })

    result: Dict[str, Any] = {
        "assessment": "INSUFFICIENT_DATA" if not clean else "NO_ADVERSE_FINDING",
        "events": events,
        "findings": [],
        "relationships": [],
        "legal_authority": False,
        "disclaimer": "These are evidence-review signals only; they do not establish title or legal ownership.",
    }
    if len(clean) < 2:
        return result

    # Do not compare records with the same number from different villages or
    # districts.  A repeated cadastral number is not enough to join parcels.
    comparable = [clean[0]] + [record for record in clean[1:] if _same_property_context(clean[0], record)]
    if len(comparable) < 2:
        return result

    # Same-period conflicting owners are a high-severity review signal.
    for index, left in enumerate(comparable):
        left_year = _record_year(left)
        left_owner = _normalise(_field_value(left.get("fields"), "owner_name", "owner"))
        if not left_owner:
            continue
        for right in comparable[index + 1:]:
            if _record_year(right) != left_year or not left_year:
                continue
            right_owner = _normalise(_field_value(right.get("fields"), "owner_name", "owner"))
            if right_owner and right_owner != left_owner:
                result["findings"].append(_finding(
                    "OWNERSHIP_CONFLICT", "ERROR", "Conflicting owners in the same period",
                    "Two records for the same parcel and year name different record-holders.",
                    [left.get("id"), right.get("id")],
                ))
                result["assessment"] = "REVIEW_REQUIRED"
                return result

    # Adjacent records allow conservative transfer/duplicate/partition signals.
    for left, right in zip(comparable, comparable[1:]):
        left_fields = left.get("fields") or {}
        right_fields = right.get("fields") or {}
        left_owner_raw = _field_value(left_fields, "owner_name", "owner")
        right_owner_raw = _field_value(right_fields, "owner_name", "owner")
        left_owner = _normalise(left_owner_raw)
        right_owner = _normalise(right_owner_raw)
        left_khasra = _land_number(_field_value(left_fields, "khasra_number"))
        right_khasra = _land_number(_field_value(right_fields, "khasra_number"))
        left_area = _number(_field_value(left_fields, "area", "land_area", "plot_area"))
        right_area = _number(_field_value(right_fields, "area", "land_area", "plot_area"))

        if left_owner and right_owner and left_owner != right_owner:
            similarity = difflib.SequenceMatcher(None, left_owner, right_owner).ratio()
            if similarity >= 0.88:
                result["findings"].append(_finding(
                    "POSSIBLE_DUPLICATE_OCR_VARIATION", "WARNING", "Possible duplicate caused by OCR variation",
                    "Owner names are highly similar; this may be OCR variation rather than a transfer."
                    , [left.get("id"), right.get("id")],
                ))
            elif _is_transfer_document(right) or _is_transfer_document(left):
                result["findings"].append(_finding(
                    "TRANSFER_SUPPORTED", "WARNING", "Transfer candidate has supporting document evidence",
                    "A transfer-like document or OCR text appears between different record-holders; verification is still required.",
                    [left.get("id"), right.get("id")],
                ))
            else:
                result["findings"].append(_finding(
                    "TRANSFER_CANDIDATE", "WARNING", "Possible ownership transfer",
                    "Record-holder changed across chronological records without a supporting transfer document in the saved evidence.",
                    [left.get("id"), right.get("id")],
                ))
            result["assessment"] = "REVIEW_REQUIRED"

        if (
            left_owner and right_owner and left_owner == right_owner and left_khasra and right_khasra
            and right_khasra.startswith(left_khasra + "/")
            and left_area is not None and right_area is not None and right_area < left_area
        ):
            relation = {
                "relationship_type": "POSSIBLE_PREDECESSOR",
                "relation_type": "POSSIBLE_PREDECESSOR",
                "from_property_id": left.get("id"),
                "to_property_id": right.get("id"),
                "reason": "Later subdivision identifier and reduced area may represent a partition candidate.",
            }
            result["relationships"].append(relation)
            result["findings"].append(_finding(
                "PARTITION_CANDIDATE", "WARNING", "Possible partition or subdivision",
                "A later cadastral subdivision has the same record-holder and a smaller recorded area.",
                [left.get("id"), right.get("id")],
            ))
            result["assessment"] = "REVIEW_REQUIRED"

    return result


def _history_for_property(property_id: str) -> Dict[str, Any]:
    ensure_schema()
    with get_db() as db:
        rows = db.execute(
            """SELECT d.* FROM documents d JOIN property_documents pd ON pd.document_id=d.id
               WHERE pd.property_id=? ORDER BY d.created_at ASC""",
            (property_id,),
        ).fetchall()
    records: List[Dict[str, Any]] = []
    for row in rows:
        record = dict(row)
        record["fields"] = _parse_json(record.get("fields"), {})
        records.append(record)
    return analyze_ownership_history(records)


def _apply_location(property_id: str, request: LocationUpdate, actor: Dict[str, Any]) -> Dict[str, Any]:
    role = actor.get("role")
    if not role:
        identifier = str(actor.get("id") or actor.get("email") or "")
        with get_db() as db:
            actor_row = db.execute(
                "SELECT role FROM users WHERE id=? OR LOWER(email)=? LIMIT 1",
                (identifier, identifier.casefold()),
            ).fetchone()
        role = actor_row["role"] if actor_row else None
    if role not in {ROLE_VERIFICATION_OFFICER, ROLE_ADMIN}:
        raise HTTPException(status_code=403, detail="Only Verification Officers or Administrators can edit property locations.")
    with get_db() as db:
        row = _property_row(db, property_id)
        if not row:
            raise HTTPException(status_code=404, detail="Property not found.")
        canonical_id = row["property_id"]
        previous_latitude, previous_longitude = row["latitude"], row["longitude"]
        now = _now()
        if request.latitude is not None and request.longitude is not None:
            base_lat = row["location_base_latitude"]
            base_lon = row["location_base_longitude"]
            if base_lat is None or base_lon is None:
                base_lat, base_lon = row["latitude"], row["longitude"]
            db.execute(
                """UPDATE properties SET latitude=?,longitude=?,location_status='EXACT_PIN',location_source=?,location_confidence=1.0,
                   location_verified_by=?,location_verified_at=?,location_updated_at=?,location_base_latitude=?,location_base_longitude=?,
                   location_base_source=?,updated_at=? WHERE property_id=?""",
                (request.latitude, request.longitude, "Human verification pin", actor.get("email") or actor.get("full_name"),
                 now, now, base_lat, base_lon, row["location_base_source"] or row["geometry_source"], now, canonical_id),
            )
            detail = f"Property {canonical_id} location changed from ({previous_latitude}, {previous_longitude}) to ({request.latitude}, {request.longitude}). Reason: {request.reason or 'Not supplied'}"
            event_type = "LOCATION_PIN_SET"
        else:
            base_lat = row["location_base_latitude"]
            base_lon = row["location_base_longitude"]
            location_status = "PARCEL_GEOMETRY" if row["geometry"] else "UNRESOLVED"
            db.execute(
                """UPDATE properties SET latitude=?,longitude=?,location_status=?,location_source=?,location_confidence=?,
                   location_verified_by=NULL,location_verified_at=NULL,location_updated_at=?,updated_at=? WHERE property_id=?""",
                (base_lat, base_lon, location_status, row["location_base_source"] or row["geometry_source"],
                 row["geometry_confidence"], now, now, canonical_id),
            )
            detail = f"Property {canonical_id} exact pin cleared. Previous location was ({previous_latitude}, {previous_longitude}). Reason: {request.reason or 'Not supplied'}"
            event_type = "LOCATION_PIN_CLEARED"
        db.execute(
            "INSERT INTO property_timeline(id,property_id,event_type,description,source,created_at) VALUES (?,?,?,?,?,?)",
            (uuid.uuid4().hex, canonical_id, event_type, detail, "Human location verification", now),
        )
    log_audit(actor.get("full_name", actor.get("email", "user")), event_type, detail, canonical_id)
    return _property_by_id(canonical_id) or {}


def update_property_location(property_id: str, request: LocationUpdate, actor: Dict[str, Any]) -> Dict[str, Any]:
    ensure_schema()
    return _apply_location(property_id, request, actor)


def _map_document_visible(row: Any, user: Dict[str, Any]) -> bool:
    """Apply the same document visibility rules as the canonical API."""
    role = user.get("role")
    approved = str(row["status"] or "").upper() in {"APPROVED", "VERIFIED", "AUTO_APPROVED"}
    if role == ROLE_VIEWER:
        return approved
    if role == ROLE_DATA_OFFICER:
        return row["uploaded_by"] == user.get("email")
    return True


def _document_boundary(fields: Dict[str, Any], stored: Any = None) -> Tuple[Optional[Dict[str, Any]], str]:
    """Return an explicit saved boundary or a polygon from labelled OCR corners."""
    geometry = _parse_json(stored, None) if stored else None
    if isinstance(geometry, dict) and geometry.get("type") in {"Polygon", "MultiPolygon"}:
        return geometry, "Officer-digitized boundary"
    points: List[List[float]] = []
    for index in range(1, 6):
        field = fields.get(f"coordinate_{index}")
        value = field.get("value", "") if isinstance(field, dict) else field
        pair = _parse_land_coordinate(value)
        if pair:
            latitude, longitude = pair
            points.append([longitude, latitude])
    if len(points) < 3:
        return None, ""
    if points[0] != points[-1]:
        points.append(points[0])
    valid, _ = _ring_geometry_checks(points)
    if not valid:
        return None, ""
    return {"type": "Polygon", "coordinates": [points]}, "OCR-extracted printed coordinates"


def _map_document_item(row: Any) -> Dict[str, Any]:
    fields = _parse_json(row["fields"], {}) or {}
    lat = row["lat"] if "lat" in row.keys() else None
    lon = row["lon"] if "lon" in row.keys() else None
    has_exact_pin = lat is not None and lon is not None
    village = _field_value(fields, "village")
    validation = _parse_json(row["validation"], {}) if "validation" in row.keys() else {}
    if not isinstance(validation, dict):
        validation = {}
    persisted_source = row["location_source"] if "location_source" in row.keys() else None
    persisted_confidence = row["location_confidence"] if "location_confidence" in row.keys() else None
    verified_by = row["location_verified_by"] if "location_verified_by" in row.keys() else None
    verified_at = row["location_verified_at"] if "location_verified_at" in row.keys() else None
    location_status = "EXACT_PIN" if has_exact_pin else ("VILLAGE_LEVEL" if village else "UNRESOLVED")
    location_source = (persisted_source or "Authorised reviewer pin") if has_exact_pin else ("Document village fields; geocode on request" if village else None)
    geometry, geometry_source = _document_boundary(
        fields,
        row["map_geometry"] if "map_geometry" in row.keys() else None,
    )
    stored_geometry_source = row["map_geometry_source"] if "map_geometry_source" in row.keys() else None
    if stored_geometry_source:
        geometry_source = stored_geometry_source
    item = {
        "id": row["id"],
        "filename": row["filename"],
        "doc_type": row["doc_type"] or "Land Record",
        "status": row["status"],
        "geometry_status": row["map_geometry_status"] if "map_geometry_status" in row.keys() else None,
        "geometry_reason": row["map_geometry_reason"] if "map_geometry_reason" in row.keys() else None,
        "geometry_updated_at": row["map_geometry_updated_at"] if "map_geometry_updated_at" in row.keys() else None,
        "owner": _field_value(fields, "owner_name"),
        "father": _field_value(fields, "father_name"),
        "survey": _field_value(fields, "survey_number"),
        "khasra": _field_value(fields, "khasra_number"),
        "khata": _field_value(fields, "khata_number"),
        "plot": _field_value(fields, "plot_number"),
        "area": _field_value(fields, "area"),
        "north_boundary": _field_value(fields, "north_boundary"),
        "south_boundary": _field_value(fields, "south_boundary"),
        "east_boundary": _field_value(fields, "east_boundary"),
        "west_boundary": _field_value(fields, "west_boundary"),
        "boundary_completeness": float((_field_value(fields, "boundary_completeness") or 0) or 0),
        "village": village,
        "tehsil": _field_value(fields, "tehsil", "taluka"),
        "district": _field_value(fields, "district"),
        "state": _field_value(fields, "state"),
        "year": _field_value(fields, "khatauni_year", "year", "document_date"),
        "lat": float(lat) if lat is not None else None,
        "lon": float(lon) if lon is not None else None,
        "geometry": geometry,
        "geometry_source": geometry_source,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"] if "updated_at" in row.keys() else row["created_at"],
        "location_status": location_status,
        "location_state": "VERIFIED_LOCATION" if location_status == "EXACT_PIN" else ("APPROXIMATE_VILLAGE_LOCATION" if location_status == "VILLAGE_LEVEL" else "LOCATION_NOT_AVAILABLE"),
        "location_label": "VERIFIED LOCATION" if location_status == "EXACT_PIN" else ("APPROXIMATE — VILLAGE LOCATION" if location_status == "VILLAGE_LEVEL" else "LOCATION NOT AVAILABLE"),
        "location_source": location_source,
        "location_confidence": float(persisted_confidence) if persisted_confidence is not None else None,
        "location_accuracy_m": row["location_accuracy_m"] if "location_accuracy_m" in row.keys() else None,
        "location_verified_by": verified_by if has_exact_pin else None,
        "location_verified_at": verified_at if has_exact_pin else None,
        "location_reason": row["location_reason"] if "location_reason" in row.keys() else None,
        "location_source_detail": row["location_source_detail"] if "location_source_detail" in row.keys() else None,
        "location_provenance": {
            "status": location_status,
            "source": location_source,
            "confidence": float(persisted_confidence) if persisted_confidence is not None else None,
            "verified_by": verified_by if has_exact_pin else None,
            "verified_at": verified_at if has_exact_pin else None,
            "query": None,
        },
        "review_required": str(row["status"] or "").upper() not in {"APPROVED", "VERIFIED", "AUTO_APPROVED"},
        # Mean OCR confidence (percent) carried for land-risk quality signals.
        "mean_conf": row["mean_conf"] if "mean_conf" in row.keys() else None,
        "validation_issues": len(validation.get("issues") or []) if isinstance(validation.get("issues"), list) else 0,
    }
    return item


def _map_location_audit_context(document_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """Read reviewer/at metadata from the existing audit trail.

    Document coordinates remain the only persisted document location fields.
    Verification identity and time are intentionally derived from the existing
    audit table rather than duplicated in the OCR document schema.
    """
    ensure_schema()
    if not document_ids:
        return {}
    wanted = {str(value) for value in document_ids}
    # Restrict the scan to the requested documents (chunked for old SQLite
    # parameter limits) instead of reading the full location audit history
    # and filtering in Python (audit defect D11).
    rows: List[Any] = []
    wanted_list = sorted(wanted)
    with get_db() as db:
        for start in range(0, len(wanted_list), 400):
            chunk = wanted_list[start:start + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows.extend(db.execute(
                f"""SELECT doc_id, ts, username, action, detail
                    FROM audit
                    WHERE action IN ('location_set', 'location_cleared')
                      AND doc_id IN ({placeholders})
                    ORDER BY ts DESC, id DESC""",
                tuple(chunk),
            ).fetchall())
    result: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        doc_id = str(row["doc_id"] or "")
        if doc_id not in wanted or doc_id in result:
            continue
        result[doc_id] = {
            "verified_by": row["username"] if row["action"] == "location_set" else None,
            "verified_at": row["ts"] if row["action"] == "location_set" else None,
            "action": row["action"],
            "detail": row["detail"],
        }
    return result


def _map_property_context(document_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """Return explicitly linked reference geometry without making it authoritative.

    A property link is intentionally kept separate from a document's exact pin:
    synthetic/reference geometry must never turn an unresolved document into a
    verified parcel location.
    """
    ensure_schema()
    if not document_ids:
        return {}
    with get_db() as db:
        rows = db.execute(
            """SELECT pd.document_id, p.*
               FROM property_documents pd
               JOIN properties p ON p.property_id = pd.property_id
               ORDER BY pd.linked_at DESC"""
        ).fetchall()
    result: Dict[str, Dict[str, Any]] = {}
    wanted = {str(value) for value in document_ids}
    for row in rows:
        document_id = str(row["document_id"])
        if document_id in wanted and document_id not in result:
            result[document_id] = _property_dict(row)
    return result


# Columns the record builder, the visibility rules and the location contexts
# actually read. Deliberately excludes the heavy OCR payloads (ocr_text,
# cleaned_ocr_text, original_fields) and AI advice: grouping thousands of
# screened documents must not drag megabytes of scan text through every
# land-records / risk-review / detail request.
_MAP_RECORD_COLUMNS = (
    "id", "filename", "doc_type", "status", "fields", "validation", "mean_conf",
    "lat", "lon", "map_geometry", "map_geometry_source", "map_geometry_status", "map_geometry_reason", "map_geometry_updated_at",
    "location_accuracy_m", "location_source_detail", "location_reason",
    "location_verified_by", "location_verified_at", "uploaded_by", "created_at", "updated_at",
)



def _map_reference_property_fallback(records: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Attach a reference parcel only when survey + village identify one property."""
    if not records:
        return {}
    keys=[]
    for record in records:
        survey=_land_number(record.get("survey"))
        village=_normalise(record.get("village"))
        if survey and village:
            keys.append((survey.casefold(), village))
    if not keys:
        return {}
    result={}
    with get_db() as db:
        rows=db.execute("SELECT * FROM properties WHERE survey_number IS NOT NULL AND village IS NOT NULL").fetchall()
    grouped={}
    wanted=set(keys)
    for row in rows:
        key=(_land_number(row["survey_number"]).casefold(), _normalise(row["village"]))
        if key in wanted:
            grouped.setdefault(key, []).append(row)
    for record in records:
        key=(_land_number(record.get("survey")).casefold(), _normalise(record.get("village")))
        matches=grouped.get(key, [])
        if len(matches)==1:
            result[str(record["id"])]=_property_dict(matches[0])
    return result

def _map_visible_records(user: Dict[str, Any], *, limit: int = 1000, offset: int = 0,
                        q: str = "", district: str = "", tehsil: str = "", village: str = "",
                        status: str = "", bbox: Optional[Tuple[float, float, float, float]] = None) -> List[Dict[str, Any]]:
    """Role-filtered document records for the map, capped at MAP_RECORD_CAP.

    `location` filtering deliberately does NOT happen here: village-level vs
    unresolved is derived from the parsed village field in `_map_document_item`
    and applied by callers after this fetch (the old SQL `LIKE '%village%'`
    heuristic matched the JSON key name, not the value — audit defect D12).
    Ordering includes `id` as a tiebreaker so pagination is deterministic (D10).
    """
    ensure_schema()
    limit = max(1, min(int(limit), MAP_RECORD_CAP))
    offset = max(0, int(offset))
    with get_db() as db:
        conditions: List[str] = []
        params: List[Any] = []
        role = user.get("role")
        if role == ROLE_VIEWER:
            conditions.append("UPPER(status) IN ('APPROVED','VERIFIED','AUTO_APPROVED')")
        elif role == ROLE_DATA_OFFICER:
            conditions.append("uploaded_by = ?")
            params.append(user.get("email"))
        if status:
            conditions.append("UPPER(status) = UPPER(?)")
            params.append(status)
        if q:
            like = f"%{q.casefold()}%"
            conditions.append("(LOWER(filename) LIKE ? OR LOWER(doc_type) LIKE ? OR LOWER(id) LIKE ? OR LOWER(COALESCE(fields,'')) LIKE ?)")
            params.extend([like, like, like, like])
        for value in (village, tehsil, district):
            if value:
                like = f"%{value.casefold()}%"
                conditions.append("LOWER(COALESCE(fields,'')) LIKE ?")
                params.append(like)
        if bbox:
            min_lat, min_lon, max_lat, max_lon = bbox
            conditions.append("lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?")
            params.extend([min_lat, max_lat, min_lon, max_lon])
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = db.execute(
            f"SELECT {', '.join(_MAP_RECORD_COLUMNS)} FROM documents{where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            tuple(params + [limit, offset]),
        ).fetchall()
    visible = [row for row in rows if _map_document_visible(row, user)]
    records = [_map_document_item(row) for row in visible]
    document_ids = [str(record["id"]) for record in records]
    audit_context = _map_location_audit_context(document_ids)
    property_context = _map_property_context(document_ids)
    fallback_context = _map_reference_property_fallback(records)
    for record in records:
        audit_item = audit_context.get(str(record["id"]))
        has_coordinates = record.get("lat") is not None and record.get("lon") is not None
        record["location_audit_available"] = bool(audit_item)
        record["location_provenance"]["audit_available"] = bool(audit_item)
        if has_coordinates:
            if audit_item and audit_item.get("action") == "location_set":
                record["location_verified_by"] = audit_item.get("verified_by")
                record["location_verified_at"] = audit_item.get("verified_at")
                detail = str(audit_item.get("detail") or "")
                record["location_reason"] = detail.split(" Reason: ", 1)[1] if " Reason: " in detail else None
                record["location_provenance"].update({
                    "verified_by": record["location_verified_by"],
                    "verified_at": record["location_verified_at"],
                })
        property_item = property_context.get(str(record["id"])) or fallback_context.get(str(record["id"]))
        if not property_item:
            record["reference_geometry"] = None
            record["reference_property"] = None
            continue
        record["reference_geometry"] = property_item.get("geometry")
        record["reference_property"] = {
            "property_id": property_item.get("property_id"),
            "parcel_id": property_item.get("parcel_id"),
            "survey_number": property_item.get("survey_number"),
            "sub_division": property_item.get("sub_division"),
            "area": property_item.get("area"),
            "area_unit": property_item.get("area_unit"),
            "geometry_source": property_item.get("geometry_source"),
            "geometry_confidence": property_item.get("geometry_confidence"),
            "data_source": property_item.get("data_source"),
            "location": property_item.get("location"),
            "synthetic": "synthetic" in str(property_item.get("data_source") or "").casefold()
                or "synthetic" in str(property_item.get("geometry_source") or "").casefold(),
        }
    return records


def _map_record_by_id(user: Dict[str, Any], doc_id: str) -> Optional[Dict[str, Any]]:
    """Fetch ONE record with the same visibility rules as the list endpoint.

    Detail endpoints (spatial checks, layers, parcel candidates) previously
    loaded up to MAP_RECORD_CAP records to find a single row (audit defect
    D11); this keeps their semantics with a single indexed query."""
    ensure_schema()
    with get_db() as db:
        conditions = ["id = ?"]
        params: List[Any] = [str(doc_id)]
        role = user.get("role")
        if role == ROLE_VIEWER:
            conditions.append("UPPER(status) IN ('APPROVED','VERIFIED','AUTO_APPROVED')")
        elif role == ROLE_DATA_OFFICER:
            conditions.append("uploaded_by = ?")
            params.append(user.get("email"))
        row = db.execute(
            f"SELECT {', '.join(_MAP_RECORD_COLUMNS)} FROM documents WHERE {' AND '.join(conditions)} LIMIT 1",
            tuple(params),
        ).fetchone()
    if not row or not _map_document_visible(row, user):
        return None
    record = _map_document_item(row)
    document_ids = [str(record["id"])]
    audit_context = _map_location_audit_context(document_ids)
    property_context = _map_property_context(document_ids)
    fallback_context = _map_reference_property_fallback([record])
    audit_item = audit_context.get(document_ids[0])
    has_coordinates = record.get("lat") is not None and record.get("lon") is not None
    record["location_audit_available"] = bool(audit_item)
    record["location_provenance"]["audit_available"] = bool(audit_item)
    if has_coordinates and audit_item and audit_item.get("action") == "location_set":
        record["location_verified_by"] = audit_item.get("verified_by")
        record["location_verified_at"] = audit_item.get("verified_at")
        detail = str(audit_item.get("detail") or "")
        record["location_reason"] = detail.split(" Reason: ", 1)[1] if " Reason: " in detail else None
        record["location_provenance"].update({
            "verified_by": record["location_verified_by"],
            "verified_at": record["location_verified_at"],
        })
    property_item = property_context.get(document_ids[0]) or fallback_context.get(document_ids[0])
    if not property_item:
        record["reference_geometry"] = None
        record["reference_property"] = None
    else:
        record["reference_geometry"] = property_item.get("geometry")
        record["reference_property"] = {
            "property_id": property_item.get("property_id"),
            "parcel_id": property_item.get("parcel_id"),
            "survey_number": property_item.get("survey_number"),
            "sub_division": property_item.get("sub_division"),
            "area": property_item.get("area"),
            "area_unit": property_item.get("area_unit"),
            "geometry_source": property_item.get("geometry_source"),
            "geometry_confidence": property_item.get("geometry_confidence"),
            "data_source": property_item.get("data_source"),
            "location": property_item.get("location"),
            "synthetic": "synthetic" in str(property_item.get("data_source") or "").casefold()
                or "synthetic" in str(property_item.get("geometry_source") or "").casefold(),
        }
    return record


def _map_record_matches(record: Dict[str, Any], *, q: str = "", district: str = "", tehsil: str = "", village: str = "", status: str = "", location: str = "") -> bool:
    if q and q not in " ".join(str(record.get(key) or "") for key in (
        "id", "filename", "owner", "father", "survey", "khasra", "khata", "plot", "area",
        "village", "tehsil", "district", "state", "doc_type", "status")).casefold():
        return False
    for key, expected in (("district", district), ("tehsil", tehsil), ("village", village), ("status", status)):
        if expected and _normalise(record.get(key)) != _normalise(expected):
            return False
    if location and _normalise(record.get("location_status")) != _normalise(location):
        return False
    return True


def _map_summary(records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    statuses = {"EXACT_PIN": 0, "VILLAGE_LEVEL": 0, "UNRESOLVED": 0}
    for record in records:
        statuses[record.get("location_status") or "UNRESOLVED"] = statuses.get(record.get("location_status") or "UNRESOLVED", 0) + 1
    total = len(records)
    exact = statuses.get("EXACT_PIN", 0)
    return {
        "records": total,
        "exact_pins": exact,
        "village_level": statuses.get("VILLAGE_LEVEL", 0),
        "unresolved": statuses.get("UNRESOLVED", 0),
        # Coverage is based only on persisted, reviewer-set coordinates. A
        # village name or a reference polygon is not counted as an exact map.
        "mapped_records": exact,
        "mapped_percent": round((exact / total) * 100, 1) if total else 0.0,
        "location_coverage_percent": round((exact / total) * 100, 1) if total else 0.0,
        "review_required": sum(1 for record in records if record.get("review_required")),
        "approved": sum(1 for record in records if not record.get("review_required")),
        "villages": len({normalise for normalise in (_normalise(record.get("village")) for record in records) if normalise}),
        "districts": len({normalise for normalise in (_normalise(record.get("district")) for record in records) if normalise}),
        "surveys": len({normalise for normalise in (_land_number(record.get("survey")) for record in records) if normalise}),
    }


@map_router.get("/records")
def map_records(
    q: str = Query("", max_length=200),
    district: str = Query("", max_length=200),
    tehsil: str = Query("", max_length=200),
    village: str = Query("", max_length=200),
    status: str = Query("", max_length=80),
    location: str = Query("", max_length=40),
    limit: int = Query(5000, ge=1, le=MAP_RECORD_CAP),
    offset: int = Query(0, ge=0),
    min_lat: Optional[float] = Query(None, ge=-90, le=90),
    min_lon: Optional[float] = Query(None, ge=-180, le=180),
    max_lat: Optional[float] = Query(None, ge=-90, le=90),
    max_lon: Optional[float] = Query(None, ge=-180, le=180),
    user: Dict[str, Any] = Depends(get_current_user),
):
    """Return Portfolio-style document map records with server-side filters.

    Exact coordinates are human-set pins. Records without a pin remain useful
    because their village/district fields can be geocoded and cached at
    village level. The summary metadata lets a future client render a map
    dashboard without downloading a second dataset.

    Pagination is deterministic (`created_at DESC, id DESC`); `offset` walks
    the same ordered set. `metadata.record_cap` / `metadata.truncated` disclose
    the unified dataset cap shared with summaries and exports.
    """
    bbox = None
    if None not in (min_lat, min_lon, max_lat, max_lon):
        if min_lat > max_lat or min_lon > max_lon:
            raise HTTPException(status_code=400, detail="Invalid map bounding box.")
        bbox = (min_lat, min_lon, max_lat, max_lon)
    # SQL applies role/status/bbox narrowing; exact field semantics are applied before pagination.
    # The bounded candidate window prevents unbounded OCR payload reads while avoiding the old
    # "LIMIT first, filter later" bug that could hide matching records.
    candidate_limit = MAP_RECORD_CAP
    candidate_records = _map_visible_records(
        user, limit=candidate_limit, offset=0, q=q, district=district, tehsil=tehsil,
        village=village, status=status, bbox=bbox,
    )
    filtered = [record for record in candidate_records if _map_record_matches(
        record, q=_normalise(q), district=district, tehsil=tehsil, village=village,
        status=status, location=location)]
    window_saturated = len(candidate_records) >= candidate_limit
    page = filtered[offset:offset + limit]
    return {
        "records": page,
        "total": len(filtered),
        "offset": offset,
        "has_more": (offset + len(page)) < len(filtered),
        "metadata": {
            "authoritative": False,
            "source": "Screened document fields and authorised reviewer pins",
            "summary": _map_summary(filtered),
            "filters": {"q": q, "district": district, "tehsil": tehsil, "village": village, "status": status, "location": location},
            "candidate_window": len(candidate_records),
            "record_cap": candidate_limit,
            "truncated": window_saturated,
            # `total` counts the full eligible set only inside the candidate
            # window; when the window saturated it is a lower bound.
            "total_is_lower_bound": window_saturated,
        },
    }


@map_router.get("/conflicts")
def map_conflicts(user: Dict[str, Any] = Depends(get_current_user)):
    """Return conservative spatial screening signals, never legal conclusions."""
    records = _map_visible_records(user, limit=MAP_RECORD_CAP)
    conflicts = []
    seen = {}
    for record in records:
        lat, lon = record.get("lat"), record.get("lon")
        if lat is None or lon is None:
            continue
        key = (round(float(lat), 6), round(float(lon), 6))
        if key in seen:
            conflicts.append({"type": "DUPLICATE_EXACT_LOCATION", "severity": "WARNING",
                              "record_ids": [seen[key], str(record["id"])],
                              "reason": "Multiple records share the same persisted exact location; verify whether this is intentional."})
        else:
            seen[key] = str(record["id"])
    return {"conflicts": conflicts, "count": len(conflicts),
            "disclaimer": "Spatial conflicts are screening signals only and do not establish boundary, title, possession, or legal status."}




def _geo_points(geometry: Optional[Dict[str, Any]]) -> List[List[Tuple[float, float]]]:
    if not isinstance(geometry, dict):
        return []
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "Polygon":
        return [[(float(p[0]), float(p[1])) for p in ring] for ring in coords if isinstance(ring, list) and len(ring) >= 4]
    if gtype == "MultiPolygon":
        rings = []
        for polygon in coords:
            if isinstance(polygon, list):
                rings.extend([[(float(p[0]), float(p[1])) for p in ring] for ring in polygon if isinstance(ring, list) and len(ring) >= 4])
        return rings
    return []


def _polygon_area(ring: Sequence[Tuple[float, float]]) -> float:
    if len(ring) < 3:
        return 0.0
    return abs(sum(ring[i][0] * ring[(i + 1) % len(ring)][1] - ring[(i + 1) % len(ring)][0] * ring[i][1] for i in range(len(ring))) / 2.0)


def _bbox_of_geometry(geometry: Optional[Dict[str, Any]]) -> Optional[Tuple[float, float, float, float]]:
    rings = _geo_points(geometry)
    pts = [point for ring in rings for point in ring]
    if not pts:
        return None
    xs, ys = zip(*pts)
    return min(xs), min(ys), max(xs), max(ys)


def _bbox_overlap_ratio(first: Optional[Dict[str, Any]], second: Optional[Dict[str, Any]]) -> Optional[float]:
    a, b = _bbox_of_geometry(first), _bbox_of_geometry(second)
    if not a or not b:
        return None
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    area_a = max(0.0, (a[2] - a[0]) * (a[3] - a[1]))
    area_b = max(0.0, (b[2] - b[0]) * (b[3] - b[1]))
    if not area_a or not area_b:
        return 0.0
    return round(inter / min(area_a, area_b), 4)


def _point_in_ring(point: Tuple[float, float], ring: Sequence[Tuple[float, float]]) -> bool:
    x, y = point
    inside = False
    for i in range(len(ring)):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % len(ring)]
        if ((y1 > y) != (y2 > y)):
            cross_x = (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-30) + x1
            if x < cross_x:
                inside = not inside
    return inside


def _geometry_polygons(geometry: Optional[Dict[str, Any]]) -> List[List[List[Tuple[float, float]]]]:
    """Preserve GeoJSON polygon/ring structure so holes are not filled."""
    if not isinstance(geometry, dict):
        return []
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    raw_polygons = [coords] if gtype == "Polygon" else (coords if gtype == "MultiPolygon" else [])
    polygons: List[List[List[Tuple[float, float]]]] = []
    try:
        for polygon in raw_polygons:
            if not isinstance(polygon, list):
                continue
            rings = []
            for ring in polygon:
                if not isinstance(ring, list) or len(ring) < 4:
                    continue
                points = [(float(p[0]), float(p[1])) for p in ring]
                if all(math.isfinite(x) and math.isfinite(y) for x, y in points):
                    rings.append(points)
            if rings:
                polygons.append(rings)
    except (TypeError, ValueError, IndexError, OverflowError):
        return []
    return polygons


def _point_in_geometry(lat: float, lon: float, geometry: Optional[Dict[str, Any]]) -> Optional[bool]:
    polygons = _geometry_polygons(geometry)
    if not polygons:
        return None
    point = (lon, lat)
    # Each polygon's first ring is its exterior; subsequent rings are holes.
    for rings in polygons:
        if _point_in_ring(point, rings[0]) and not any(
            _point_in_ring(point, hole) for hole in rings[1:]
        ):
            return True
    return False


def _recorded_area_m2(value: Any) -> Optional[float]:
    raw = str(value or "").strip().casefold()
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(sq\.?\s*ft|sqft|ft2|acre|acres|hectare|hectares|ha|m2|sq\.?\s*m|sqm)?", raw)
    if not match:
        return None
    number = float(match.group(1))
    unit = (match.group(2) or "").replace(" ", "").replace(".", "")
    if unit in {"acre", "acres"}: return number * 4046.8564224
    if unit in {"hectare", "hectares", "ha"}: return number * 10000.0
    if unit in {"sqft", "ft2"}: return number * 0.09290304
    return number

def _geometry_area_m2(geometry: Optional[Dict[str, Any]]) -> Optional[float]:
    polygons = _geometry_polygons(geometry)
    if not polygons:
        return None
    # Approximate projected area, subtracting holes from their own polygon.
    total = 0.0
    for rings in polygons:
        total += max(0.0, _boundary_area_m2(rings[0]) - sum(
            _boundary_area_m2(ring) for ring in rings[1:]
        ))
    return round(total, 2)

def _reference_comparison(record: Dict[str, Any]) -> Dict[str, Any]:
    reference = record.get("reference_geometry")
    boundary = record.get("geometry")
    recorded_area = _recorded_area_m2(record.get("area"))
    boundary_area = _geometry_area_m2(boundary)
    reference_area = _geometry_area_m2(reference)
    result: Dict[str, Any] = {
        "available": bool(reference or boundary),
        "screening_only": True,
        "recorded_area_m2": recorded_area,
        "boundary_area_m2": boundary_area,
        "reference_area_m2": reference_area,
        "recorded_vs_boundary_difference_percent": None,
        "recorded_vs_reference_difference_percent": None,
        "boundary_vs_reference_difference_percent": None,
        "area_status": "UNAVAILABLE",
        "bbox_overlap_ratio": None,
        "pin_inside_boundary": None,
        "pin_inside_reference": None,
        "outside_reference": None,
        "boundary_evidence": {
            "north": record.get("north_boundary") or None,
            "south": record.get("south_boundary") or None,
            "east": record.get("east_boundary") or None,
            "west": record.get("west_boundary") or None,
            "completeness": round(float(record.get("boundary_completeness") or 0), 3),
        },
        "findings": [],
    }
    if recorded_area and boundary_area:
        delta = abs(boundary_area - recorded_area) / recorded_area * 100
        result["recorded_vs_boundary_difference_percent"] = round(delta, 2)
        result["findings"].append({
            "type": "AREA_MISMATCH" if delta > 5 else "AREA_ALIGNED",
            "severity": "review" if delta > 5 else "info",
            "message": f"Mapped boundary differs from recorded area by {delta:.2f}%.",
        })
        result["area_status"] = "REVIEW_REQUIRED" if delta > 5 else "ALIGNED"
    elif recorded_area and reference_area:
        delta = abs(reference_area - recorded_area) / recorded_area * 100
        result["recorded_vs_reference_difference_percent"] = round(delta, 2)
        result["findings"].append({
            "type": "REFERENCE_AREA_MISMATCH" if delta > 5 else "REFERENCE_AREA_ALIGNED",
            "severity": "review" if delta > 5 else "info",
            "message": f"Reference parcel area differs from the record by {delta:.2f}%.",
        })
        result["area_status"] = "REVIEW_REQUIRED" if delta > 5 else "ALIGNED"
    if boundary_area and reference_area:
        result["boundary_vs_reference_difference_percent"] = round(abs(boundary_area - reference_area) / reference_area * 100, 2)
        result["bbox_overlap_ratio"] = _bbox_overlap_ratio(boundary, reference)
    if record.get("lat") is not None and record.get("lon") is not None:
        result["pin_inside_boundary"] = _point_in_geometry(float(record["lat"]), float(record["lon"]), boundary)
        result["pin_inside_reference"] = _point_in_geometry(float(record["lat"]), float(record["lon"]), reference)
        result["outside_reference"] = result["pin_inside_reference"] is False if reference else None
    missing_sides = [side for side in ("north", "south", "east", "west") if not result["boundary_evidence"].get(side)]
    if missing_sides:
        result["findings"].append({
            "type": "INCOMPLETE_BOUNDARY_DESCRIPTION",
            "severity": "review" if result["boundary_evidence"]["completeness"] > 0 else "info",
            "message": "Missing cardinal boundary descriptions: " + ", ".join(missing_sides) + ".",
        })
    return result


def _candidate_parcels_for_record(record: Dict[str, Any], limit: int = 10,
                                  property_rows: Optional[Sequence[Any]] = None,
                                  property_index: Optional[Dict[str, Any]] = None,
                                  materialize_properties: bool = True) -> List[Dict[str, Any]]:
    fields = {
        "survey_number": record.get("survey"),
        "khasra_number": record.get("khasra"),
        "village": record.get("village"),
        "taluka": record.get("tehsil"),
        "district": record.get("district"),
    }
    resolved = _resolve(fields, property_rows=property_rows, property_index=property_index,
                        materialize_properties=materialize_properties)
    return (resolved.get("matches") or [])[:limit]


def _record_reference_property(record_id: str) -> Optional[str]:
    with get_db() as db:
        row = db.execute("SELECT property_id FROM property_documents WHERE document_id=? ORDER BY linked_at DESC LIMIT 1", (record_id,)).fetchone()
    return row["property_id"] if row else None


@map_router.get("/records/{doc_id}/parcel-candidates")
def map_parcel_candidates(doc_id: str, user: Dict[str, Any] = Depends(require_roles(ROLE_VERIFICATION_OFFICER, ROLE_ADMIN))):
    record = _map_record_by_id(user, doc_id)
    if not record:
        raise HTTPException(404, "Record not found or access denied.")
    fields = {
        "survey_number": record.get("survey"),
        "khasra_number": record.get("khasra"),
        "village": record.get("village"),
        "taluka": record.get("tehsil"),
        "district": record.get("district"),
    }
    resolved = _resolve(fields)
    candidates = (resolved.get("matches") or [])[:10]
    # The status comes from evaluated evidence (strong identifier vs geography
    # vs ambiguity), never from how many rows a query happened to return (D7).
    return {"document_id": doc_id,
            "resolution_status": resolved.get("resolution_status") or "NO MATCH",
            "evidence_summary": resolved.get("evidence_summary"),
            "reasons": resolved.get("reasons") or [],
            "candidates": candidates, "screening_only": True}


@map_router.put("/records/{doc_id}/parcel-selection")
def map_select_parcel(doc_id: str, payload: Dict[str, Any],
                      user: Dict[str, Any] = Depends(require_roles(ROLE_VERIFICATION_OFFICER, ROLE_ADMIN))):
    property_id = str(payload.get("property_id") or "").strip()
    reason = str(payload.get("reason") or "").strip()[:500]
    if not property_id:
        raise HTTPException(400, "property_id is required.")
    with get_db() as db:
        document = db.execute("SELECT id, fields FROM documents WHERE id=?", (doc_id,)).fetchone()
        property_row = _property_row(db, property_id)
        if not document or not property_row:
            raise HTTPException(404, "Document or parcel not found.")
        fields = _parse_json(document["fields"], {}) or {}
        supplied = {
            "survey_number": _field_value(fields, "survey_number"),
            "khasra_number": _field_value(fields, "khasra_number"),
            "village": _field_value(fields, "village"),
            "taluka": _field_value(fields, "taluka", "tehsil"),
            "district": _field_value(fields, "district"),
        }
        mismatches = []
        for key in ("village", "taluka", "district"):
            if supplied[key] and _normalise(supplied[key]) != _normalise(property_row[key] or ""):
                mismatches.append(key)
        if supplied["survey_number"] and _land_number(supplied["survey_number"]).casefold() != _land_number(property_row["survey_number"] or property_row["gat_number"] or "").casefold():
            mismatches.append("survey_number")
        if mismatches:
            raise HTTPException(409, {"detail": "Selected parcel conflicts with extracted document identity.", "conflicting_fields": mismatches})
        previous = db.execute("SELECT property_id FROM property_documents WHERE document_id=? ORDER BY linked_at DESC LIMIT 1", (doc_id,)).fetchone()
        db.execute("DELETE FROM property_documents WHERE document_id=?", (doc_id,))
        now = _now()
        db.execute("INSERT INTO property_documents(property_id,document_id,source_type,linked_at) VALUES (?,?,?,?)",
                   (property_id, doc_id, "human_selected_parcel", now))
        db.execute("INSERT INTO provenance(id,property_id,field_name,value,source,confidence,created_at) VALUES (?,?,?,?,?,?,?)",
                   (uuid.uuid4().hex, property_id, "document_parcel_selection", _json({"document_id": doc_id, "previous_property_id": previous["property_id"] if previous else None, "reason": reason}), "Human parcel selection", 1.0, now))
        db.execute("INSERT INTO property_timeline(id,property_id,event_type,description,source,created_at) VALUES (?,?,?,?,?,?)",
                   (uuid.uuid4().hex, property_id, "PARCEL_SELECTED", f"Document {doc_id} was explicitly linked to parcel {property_id}.", "Human parcel verification", now))
    actor = user.get("full_name") or user.get("email") or "user"
    log_audit(actor, "parcel_selected", f"Document {doc_id} linked to parcel {property_id}. Previous={previous['property_id'] if previous else 'none'}. Reason: {reason or 'Not supplied'}", doc_id)
    return {"ok": True, "document_id": doc_id, "property_id": property_id, "previous_property_id": previous["property_id"] if previous else None}


def _finding_fingerprint(finding_type: str, evidence: Sequence[Any]) -> str:
    """Stable identity for a finding: type + sorted evidence identifiers.

    The same condition keeps the same fingerprint across recomputations, so a
    human resolution persists instead of resurrecting as a new open row (audit
    defect D2)."""
    payload = f"{finding_type}|" + ",".join(sorted(str(item) for item in evidence or []))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _refresh_review_findings(user: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Recompute this module's spatial review signals against stored rows.

    Detection rules (each reproducible from record data only):

    * PIN_OUTSIDE_BOUNDARY (ERROR) — reviewer pin outside a saved officer boundary.
    * PIN_OUTSIDE_REFERENCE (WARNING) — reviewer pin outside reference geometry.
    * RECORDED_AREA_MISMATCH (WARNING) — recorded area vs mapped boundary area
      differs by more than MAP_AREA_MISMATCH_PERCENT (configurable, default 25%).
    * REFERENCE_AREA_MISMATCH (WARNING) — mapped boundary vs reference geometry
      area differs beyond the same tolerance.
    * DUPLICATE_EXACT_LOCATION (WARNING) — two+ records at identical coordinates.
    * CONFLICTING_RECORDED_AREA (WARNING) — same survey+village, different areas.
    * DUPLICATE_SURVEY (INFO) — same survey+village on multiple documents.
    * AMBIGUOUS_PARCEL (ERROR) — several parcels score within 0.12 of each other.

    Reconciliation rules:
    * Only rows this module manages (finding_type in MAP_MANAGED_FINDING_TYPES,
      case_id IS NULL) are ever read or written — ai_governance's findings are
      untouched (audit defect D3).
    * A detected condition keeps its existing row id; RESOLVED/DISMISSED
      decisions survive recomputation while the evidence set is unchanged.
    * OPEN rows whose condition disappeared are SUPERSEDED with a note, never
      silently deleted, preserving resolution history.
    """
    ensure_schema()
    records = _map_visible_records(user, limit=MAP_RECORD_CAP)
    findings: List[Dict[str, Any]] = []
    by_location: Dict[Tuple[float, float], List[Dict[str, Any]]] = {}
    by_survey: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for record in records:
        if record.get("lat") is not None and record.get("lon") is not None:
            by_location.setdefault((round(float(record["lat"]), 6), round(float(record["lon"]), 6)), []).append(record)
        if record.get("survey"):
            by_survey.setdefault((_land_number(record.get("survey")).casefold(), _normalise(record.get("village"))), []).append(record)
        comparison = _reference_comparison(record)
        if comparison.get("pin_inside_boundary") is False:
            findings.append(_finding("PIN_OUTSIDE_BOUNDARY", "ERROR", "Location is outside saved boundary", "The exact reviewer pin is outside the saved officer boundary; review the pin and boundary.", [record["id"]]))
        if comparison.get("outside_reference") is True:
            findings.append(_finding("PIN_OUTSIDE_REFERENCE", "WARNING", "Location is outside reference geometry", "The exact pin falls outside the stored reference shape. Reference geometry is a screening signal only.", [record["id"]]))
        boundary_delta = comparison.get("recorded_vs_boundary_difference_percent")
        if boundary_delta is not None and boundary_delta > MAP_AREA_MISMATCH_PERCENT:
            findings.append(_finding(
                "RECORDED_AREA_MISMATCH", "WARNING", "Recorded area differs from mapped boundary",
                f"The recorded area and the mapped boundary area differ by {boundary_delta:.1f}% "
                f"(screening tolerance {MAP_AREA_MISMATCH_PERCENT:g}%). Verify units and source records; "
                "this is a measurement comparison, not a title statement.",
                [record["id"]]))
        reference_delta = comparison.get("boundary_vs_reference_difference_percent")
        if reference_delta is not None and reference_delta > MAP_AREA_MISMATCH_PERCENT:
            findings.append(_finding(
                "REFERENCE_AREA_MISMATCH", "WARNING", "Boundary/reference area differs materially",
                f"Saved boundary and reference geometry differ by {reference_delta:.1f}% "
                f"(screening tolerance {MAP_AREA_MISMATCH_PERCENT:g}% in simple planar screening).",
                [record["id"]]))
    for key, group in by_location.items():
        if len(group) > 1:
            findings.append(_finding("DUPLICATE_EXACT_LOCATION", "WARNING", "Multiple records share an exact location", "More than one visible document is pinned at the same coordinates.", [item["id"] for item in group]))
    for key, group in by_survey.items():
        areas = {str(item.get("area") or "").strip() for item in group if str(item.get("area") or "").strip()}
        if len(group) > 1 and len(areas) > 1:
            findings.append(_finding("CONFLICTING_RECORDED_AREA", "WARNING", "Same survey has conflicting recorded areas", "Documents with the same survey and village contain different area strings; review source records and units.", [item["id"] for item in group]))
        if len(group) > 1:
            findings.append(_finding("DUPLICATE_SURVEY", "INFO", "Multiple records share survey and village", "Repeated survey/village identifiers may be legitimate history or duplicates; verify document relationships.", [item["id"] for item in group]))
    # Ambiguous parcel candidates are derived per document against ONE shared
    # index built from the reference table (keeps the queue O(records)).
    property_index = _build_property_index(_all_property_rows())
    for record in records:
        candidates = _candidate_parcels_for_record(record, 3, property_index=property_index,
                                                  materialize_properties=False)
        if len(candidates) > 1:
            scores = [float(item.get("score") or 0) for item in candidates]
            strong_top = bool(set(candidates[0].get("matched_fields") or []) & set(LAND_IDENTIFIER_FIELDS))
            # A strong cadastral identifier must be in play — crowds of
            # geography-only candidates are INSUFFICIENT_EVIDENCE, not
            # ambiguity (mirrors _resolve's resolution rules).
            if strong_top and scores[0] < 0.99 and (scores[0] - scores[1]) < 0.12:
                findings.append(_finding("AMBIGUOUS_PARCEL", "ERROR", "Multiple parcels are similarly plausible", "A human reviewer must select the canonical parcel before relying on parcel-linked analysis.", [record["id"]]))

    # Deduplicate per (type, evidence) within this recomputation.
    deduped: List[Dict[str, Any]] = []
    seen_fps: set = set()
    for finding in findings:
        finding["fingerprint"] = _finding_fingerprint(finding["finding_type"], finding.get("evidence") or [])
        if finding["fingerprint"] in seen_fps:
            continue
        seen_fps.add(finding["fingerprint"])
        deduped.append(finding)
    findings = deduped

    now = _now()
    actor = user.get("email") or user.get("full_name") or "system"
    # Document -> property link lookup once per recompute (was one query per
    # newly inserted finding).
    doc_property_link: Dict[str, str] = {}
    with get_db() as db:
        for link_row in db.execute(
            "SELECT document_id, property_id FROM property_documents ORDER BY linked_at DESC"
        ).fetchall():
            doc_property_link.setdefault(str(link_row["document_id"]), str(link_row["property_id"]))
    placeholders = ",".join("?" for _ in MAP_MANAGED_FINDING_TYPES)
    with get_db() as db:
        existing_rows = db.execute(
            f"SELECT finding_id, finding_type, status, evidence FROM verification_findings "
            f"WHERE case_id IS NULL AND finding_type IN ({placeholders})",
            tuple(MAP_MANAGED_FINDING_TYPES),
        ).fetchall()
        existing_by_fp: Dict[str, Any] = {}
        for row in existing_rows:
            stored = _parse_json(row["evidence"], {}) or {}
            stored_evidence = stored.get("evidence") if isinstance(stored, dict) else None
            fp = _finding_fingerprint(row["finding_type"], stored_evidence or [])
            existing_by_fp[fp] = row

        for finding in findings:
            fp = finding["fingerprint"]
            existing = existing_by_fp.get(fp)
            if existing is None:
                fid = uuid.uuid4().hex
                # verification_findings.property_id is NOT NULL; documents that
                # are not linked to any reference parcel store "" (D1 fix) —
                # a missing parcel link must never crash the queue.
                property_id = doc_property_link.get(str(finding["evidence"][0])) if finding.get("evidence") else None
                db.execute("""INSERT INTO verification_findings
                    (finding_id,property_id,case_id,status,finding_type,severity,title,evidence,created_by,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (fid, property_id or "", None, "OPEN", finding["finding_type"], finding["severity"], finding["title"],
                     _json(finding), actor, now, now))
                finding["finding_id"] = fid
                finding["id"] = fid
                finding["status"] = "OPEN"
            else:
                previous_status = str(existing["status"] or "OPEN").upper()
                new_status = previous_status if previous_status in {"RESOLVED", "DISMISSED"} else "OPEN"
                stored_payload = _parse_json(existing["evidence"], None)
                if new_status != previous_status:
                    db.execute("UPDATE verification_findings SET status=?, evidence=?, title=?, updated_at=? WHERE finding_id=?",
                               (new_status, _json(finding), finding["title"], now, existing["finding_id"]))
                elif stored_payload != finding:
                    # Only rewrite rows whose evidence/title actually changed;
                    # warm recomputes were re-UPDATEing every open row.
                    db.execute("UPDATE verification_findings SET evidence=?, title=?, updated_at=? WHERE finding_id=?",
                               (_json(finding), finding["title"], now, existing["finding_id"]))
                finding["finding_id"] = existing["finding_id"]
                finding["id"] = existing["finding_id"]
                finding["status"] = new_status

        # Close map-managed OPEN rows whose condition is gone; keep them as
        # SUPERSEDED with a reason so the resolution trail stays auditable.
        for row in existing_rows:
            if str(row["status"] or "").upper() != "OPEN":
                continue
            stored = _parse_json(row["evidence"], {}) or {}
            stored_evidence = stored.get("evidence") if isinstance(stored, dict) else None
            fp = _finding_fingerprint(row["finding_type"], stored_evidence or [])
            if fp not in seen_fps:
                db.execute(
                    "UPDATE verification_findings SET status='SUPERSEDED', resolution_note=?, updated_at=? WHERE finding_id=? AND status='OPEN'",
                    ("Recomputed: condition no longer present after a data change.", now, row["finding_id"]),
                )
    return findings


@map_router.get("/review-queue")
def map_review_queue(
    status: str = Query("open", max_length=20),
    severity: str = Query("", max_length=20),
    finding_type: str = Query("", max_length=60, alias="type"),
    user: Dict[str, Any] = Depends(require_roles(ROLE_VERIFICATION_OFFICER, ROLE_ADMIN)),
):
    """Recomputed spatial review queue with persistent resolution state.

    `status` selects which stored states are returned (open, resolved,
    dismissed, superseded, all; default open). Filters are applied AFTER
    recomputation so every response reflects current record data."""
    findings = _refresh_review_findings(user)
    counts = {"open": 0, "resolved": 0, "dismissed": 0, "superseded": 0}
    for finding in findings:
        key = str(finding.get("status") or "OPEN").lower()
        counts[key] = counts.get(key, 0) + 1
    wanted = str(status or "open").lower()
    if wanted not in {"open", "resolved", "dismissed", "superseded", "all"}:
        raise HTTPException(status_code=400, detail="status must be open, resolved, dismissed, superseded or all")
    selected = [
        finding for finding in findings
        if wanted == "all" or str(finding.get("status") or "OPEN").lower() == wanted
    ]
    if severity:
        wanted_severity = severity.upper()
        if wanted_severity not in {"INFO", "WARNING", "ERROR"}:
            raise HTTPException(status_code=400, detail="severity must be INFO, WARNING or ERROR")
        selected = [finding for finding in selected if str(finding.get("severity") or "").upper() == wanted_severity]
    if finding_type:
        selected = [finding for finding in selected if str(finding.get("finding_type") or "") == finding_type]
    return {
        "findings": selected,
        "count": len(selected),
        "counts": counts,
        "filters": {"status": wanted, "severity": severity.upper(), "type": finding_type},
        "record_cap": MAP_RECORD_CAP,
        "disclaimer": "Review signals require human verification; they are not legal conclusions.",
    }


@map_router.post("/review-queue/{finding_id}/resolve")
def map_resolve_finding(finding_id: str, payload: Dict[str, Any],
                        user: Dict[str, Any] = Depends(require_roles(ROLE_VERIFICATION_OFFICER, ROLE_ADMIN))):
    status_value = str(payload.get("status") or "RESOLVED").upper()
    if status_value not in {"RESOLVED", "DISMISSED"}:
        raise HTTPException(400, "status must be RESOLVED or DISMISSED")
    note = str(payload.get("note") or "").strip()[:500]
    actor = user.get("full_name") or user.get("email") or "user"
    with get_db() as db:
        row = db.execute("SELECT finding_id, finding_type FROM verification_findings WHERE finding_id=?", (finding_id,)).fetchone()
        # Only findings this module manages are resolvable here; foreign rows
        # (e.g. AI governance cases) keep their own workflow and are hidden.
        if not row or row["finding_type"] not in MAP_MANAGED_FINDING_TYPES:
            raise HTTPException(404, "Finding not found.")
        db.execute(
            "UPDATE verification_findings SET status=?, resolution_note=?, resolved_by=?, resolved_at=?, updated_at=? WHERE finding_id=?",
            (status_value, note or None, actor, _now(), _now(), finding_id),
        )
    log_audit(actor, "spatial_finding_resolved", f"{finding_id} -> {status_value}. Note: {note or 'none'}", None)
    return {"ok": True, "finding_id": finding_id, "status": status_value}


@map_router.get("/records/{doc_id}/spatial-checks")
def map_spatial_checks(doc_id: str, user: Dict[str, Any] = Depends(get_current_user)):
    record = _map_record_by_id(user, doc_id)
    if not record:
        raise HTTPException(404, "Record not found or access denied.")
    comparison = _reference_comparison(record)
    return {
        "document_id": doc_id,
        "comparison": comparison,
        "geometry_source": record.get("geometry_source"),
        "reference_source": (record.get("reference_property") or {}).get("geometry_source"),
        "screening_only": True,
    }


@map_router.get("/records/{doc_id}/layers")
def map_record_layers(doc_id: str, user: Dict[str, Any] = Depends(get_current_user)):
    record = _map_record_by_id(user, doc_id)
    if not record:
        raise HTTPException(404, "Record not found or access denied.")
    cases: List[Dict[str, Any]] = []
    try:
        from court_cases import list_cases, litigation_verdict_for
        cases = list_cases(record.get("survey") or record.get("khasra") or "", record.get("village") or "")
        for case in cases:
            case["litigation_state"] = "ACTIVE" if str(case.get("status") or "").upper() == "ACTIVE" else "PRIOR"
    except Exception:
        cases = []
    timeline = []
    property_id = _record_reference_property(doc_id)
    if property_id:
        with get_db() as db:
            timeline = [dict(row) for row in db.execute(
                "SELECT event_type,description,source,created_at FROM property_timeline WHERE property_id=? ORDER BY created_at ASC",
                (property_id,)).fetchall()]
    return {
        "document": {key: record.get(key) for key in ("id","survey","village","owner","area","location_status")},
        "court_cases": cases,
        "litigation_verdict": ("ACTIVE_LITIGATION" if any(str(c.get("status")).upper()=="ACTIVE" for c in cases) else ("PRIOR_LITIGATION" if cases else "CLEAR")),
        "property_timeline": timeline,
        "screening_only": True,
    }


@map_router.get("/geojson")
def map_geojson(user: Dict[str, Any] = Depends(get_current_user)):
    records = _map_visible_records(user, limit=MAP_RECORD_CAP)
    features = []
    for record in records:
        geometry = record.get("geometry")
        if not geometry:
            coordinate = (float(record["lat"]), float(record["lon"])) if record.get("lat") is not None and record.get("lon") is not None else None
            geometry = {"type": "Point", "coordinates": [coordinate[1], coordinate[0]]} if coordinate else None
        if not geometry:
            continue
        properties = {key: record.get(key) for key in (
            "id","filename","status","owner","survey","khasra","village","tehsil","district",
            "location_status","location_source","location_accuracy_m","geometry_source")}
        properties["screening_only"] = True
        features.append({"type":"Feature","id":record["id"],"geometry":geometry,"properties":properties})
    return {"type":"FeatureCollection","features":features,
            "metadata":{"authoritative":False,"screening_only":True,
                        "crs":"urn:ogc:def:crs:OGC:1.3:CRS84",
                        "record_count":len(features),
                        "record_cap":MAP_RECORD_CAP,
                        "truncated":len(records) >= MAP_RECORD_CAP,
                        "note":"Coordinates are WGS84 longitude/latitude. Geometry provenance is per-feature geometry_source; screening data only."}}


@map_router.get("/export.geojson")
def map_export_geojson(user: Dict[str, Any] = Depends(get_current_user)):
    payload = map_geojson(user)
    return Response(content=_json(payload), media_type="application/geo+json", headers={
        "Content-Disposition":"attachment; filename=land-map.geojson","Cache-Control":"no-store"})


@map_router.get("/export.kml")
def map_export_kml(user: Dict[str, Any] = Depends(get_current_user)):
    records = _map_visible_records(user, limit=MAP_RECORD_CAP)
    placemarks = []
    for record in records:
        coordinate = (float(record["lat"]), float(record["lon"])) if record.get("lat") is not None and record.get("lon") is not None else None
        geometry = record.get("geometry")
        description = esc_kml(" | ".join(f"{key}: {record.get(key) or ''}" for key in ("survey","village","owner","location_status")))
        if geometry and geometry.get("type") == "Polygon":
            ring = geometry.get("coordinates", [[]])[0]
            coords = " ".join(f"{point[0]},{point[1]},0" for point in ring)
            shape = f"<Polygon><outerBoundaryIs><LinearRing><coordinates>{coords}</coordinates></LinearRing></outerBoundaryIs></Polygon>"
        elif coordinate:
            shape = f"<Point><coordinates>{coordinate[1]},{coordinate[0]},0</coordinates></Point>"
        else:
            continue
        placemarks.append(f"<Placemark><name>{esc_kml(record.get('id'))}</name><description>{description}</description>{shape}</Placemark>")
    body = "".join(placemarks)
    kml = '<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>Land Map</name>' + body + "</Document></kml>"
    return Response(content=kml, media_type="application/vnd.google-earth.kml+xml", headers={"Content-Disposition":"attachment; filename=land-map.kml","Cache-Control":"no-store"})


def esc_kml(value: Any) -> str:
    import html as _html
    return _html.escape(str(value or ""), quote=True)


@map_router.get("/spatial-summary")
def map_spatial_summary(user: Dict[str, Any] = Depends(get_current_user)):
    records = _map_visible_records(user, limit=MAP_RECORD_CAP)
    counts = {"verified": 0, "village_approx": 0, "unresolved": 0, "boundaries": 0, "reference_boundaries": 0}
    for record in records:
        if record.get("lat") is not None and record.get("lon") is not None: counts["verified"] += 1
        elif record.get("village"): counts["village_approx"] += 1
        else: counts["unresolved"] += 1
        if record.get("geometry"): counts["boundaries"] += 1
        if record.get("reference_geometry"): counts["reference_boundaries"] += 1
    return {"counts": counts, "screening_only": True}

@map_router.get("/summary")
def map_summary(user: Dict[str, Any] = Depends(get_current_user)):
    """Compact map dashboard metrics for the portal and integrations.

    Uses the same MAP_RECORD_CAP dataset as /records and the exports so the
    dashboard never describes a different population than the map (D9)."""
    records = _map_visible_records(user, limit=MAP_RECORD_CAP)
    return {"summary": _map_summary(records),
            "metadata": {"authoritative": False, "record_cap": MAP_RECORD_CAP,
                         "truncated": len(records) >= MAP_RECORD_CAP}}


@map_router.get("/properties")
def map_properties(
    village: str = Query("", max_length=200),
    tehsil: str = Query("", max_length=200),
    district: str = Query("", max_length=200),
    survey: str = Query("", max_length=120),
    limit: int = Query(500, ge=1, le=5000),
    user: Dict[str, Any] = Depends(get_current_user),
):
    """Return explicitly stored reference geometry for the Village Sheet.

    The response is deliberately labelled non-authoritative.  These records
    are useful for orientation and comparison only; they are never presented
    as legal cadastral boundaries or as a substitute for a document pin.
    """
    ensure_schema()
    where_parts: List[str] = []
    params: List[Any] = []
    for column, value in (("village", village), ("taluka", tehsil), ("district", district)):
        if value:
            where_parts.append(f"LOWER(TRIM(COALESCE({column}, ''))) = ?")
            params.append(_normalise(value))
    if survey:
        where_parts.append("LOWER(REPLACE(COALESCE(survey_number,''), ' ', '')) LIKE ?")
        params.append(f"%{_land_number(survey).casefold()}%")
    where = " WHERE " + " AND ".join(where_parts) if where_parts else ""
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM properties" + where + " ORDER BY village, survey_number, property_id LIMIT ?",
            tuple(params + [limit]),
        ).fetchall()
    filtered = []
    for row in rows:
        if village and _normalise(row["village"]) != _normalise(village):
            continue
        if tehsil and _normalise(row["taluka"]) != _normalise(tehsil):
            continue
        if district and _normalise(row["district"]) != _normalise(district):
            continue
        if survey and _land_number(survey).casefold() not in _land_number(row["survey_number"]).casefold():
            continue
        item = _property_dict(row)
        item["synthetic"] = (
            "synthetic" in str(item.get("data_source") or "").casefold()
            or "synthetic" in str(item.get("geometry_source") or "").casefold()
        )
        item["authoritative"] = False
        item["disclaimer"] = "Reference geometry only; not an authoritative cadastral boundary or legal title."
        filtered.append(item)
    return {
        "properties": filtered[:limit],
        "total": len(filtered),
        "metadata": {
            "authoritative": False,
            "source": "Project-owned reference geometry",
            "disclaimer": "Reference geometry only; not an authoritative cadastral boundary or legal title.",
        },
    }


def _csv_safe(value: Any) -> Any:
    """Neutralise spreadsheet formula injection without losing content.

    Cells beginning with `=`, `+`, `-`, `@`, tab or CR are prefixed with a
    single quote so Excel/Sheets treat them as text (OWASP CSV injection
    guidance). Numeric fields are passed through unchanged."""
    if isinstance(value, (int, float)) or value is None:
        return value
    text = str(value)
    if text and text[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


@map_router.get("/export.csv")
def map_export_csv(user: Dict[str, Any] = Depends(get_current_user)):
    """Download a review-friendly map register without exposing hidden records."""
    ensure_schema()
    records = _map_visible_records(user, limit=MAP_RECORD_CAP)
    output = io.StringIO(newline="")
    output.write("\ufeff")
    writer = csv.DictWriter(output, fieldnames=(
        "id", "filename", "doc_type", "status", "owner", "survey", "khasra", "khata", "plot",
        "area", "village", "tehsil", "district", "state", "year", "lat", "lon",
        "location_status", "location_state", "location_label", "location_source", "location_source_detail", "location_accuracy_m", "location_reason", "location_confidence",
        "location_verified_by", "location_verified_at", "location_audit_available", "review_required"), extrasaction="ignore")
    writer.writeheader()
    for record in records:
        writer.writerow({key: _csv_safe(record.get(key)) for key in writer.fieldnames})
    truncated = len(records) >= MAP_RECORD_CAP
    return Response(content=output.getvalue(), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": "attachment; filename=land-map-register.csv",
        "Cache-Control": "no-store",
        # Export population metadata so the user can see exactly what the file
        # contains (same unified cap as /records, /summary and GeoJSON).
        "X-Map-Record-Count": str(len(records)),
        "X-Map-Record-Cap": str(MAP_RECORD_CAP),
        "X-Map-Truncated": "true" if truncated else "false",
    })


@map_router.put("/records/{doc_id}/location")
def map_set_document_location(
    doc_id: str,
    payload: Dict[str, Any],
    user: Dict[str, Any] = Depends(require_roles(ROLE_VERIFICATION_OFFICER, ROLE_ADMIN)),
):
    """Set or clear an exact document pin with an audit event."""
    raw_lat = payload.get("lat", payload.get("latitude"))
    raw_lon = payload.get("lon", payload.get("longitude"))
    raw_accuracy = payload.get("accuracy_m")
    accuracy_m = None
    if raw_accuracy not in (None, ""):
        try:
            accuracy_m = float(raw_accuracy)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="accuracy_m must be a non-negative number.")
        if accuracy_m < 0 or accuracy_m > 100000:
            raise HTTPException(status_code=400, detail="accuracy_m must be between 0 and 100000 metres.")
    reason = str(payload.get("reason") or "").strip()[:500]
    if (raw_lat is None) != (raw_lon is None):
        raise HTTPException(status_code=400, detail="lat and lon must be supplied together (or both be null).")
    actor = user.get("full_name", user.get("email", "user"))
    changed_at = _now()
    with get_db() as db:
        row = db.execute("SELECT id, lat, lon, updated_at FROM documents WHERE id=?", (doc_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Record not found.")
        _assert_not_stale(row, payload)
        previous = (row["lat"], row["lon"])
        if raw_lat is None:
            db.execute(
                "UPDATE documents SET lat=NULL, lon=NULL, location_accuracy_m=NULL, location_source_detail=NULL, location_reason=NULL, location_verified_by=NULL, location_verified_at=NULL, updated_at=? WHERE id=?",
                (changed_at, doc_id),
            )
            detail = "Document GIS pin cleared; previous coordinates were (%s, %s)." % previous
            if reason:
                detail += " Reason: %s" % reason
            action = "location_cleared"
            result = {
                "ok": True, "lat": None, "lon": None,
                "location_status": "LOCATION_NOT_AVAILABLE",
                "location_source": None,
                "location_verified_by": None,
                "location_verified_at": None,
                "updated_at": changed_at,
            }
        else:
            try:
                latitude, longitude = float(raw_lat), float(raw_lon)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="lat/lon must be numbers (or null to clear).")
            if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
                raise HTTPException(status_code=400, detail="lat must be -90..90 and lon -180..180.")
            db.execute(
                "UPDATE documents SET lat=?, lon=?, location_accuracy_m=?, location_source_detail=?, location_reason=?, location_verified_by=?, location_verified_at=?, updated_at=? WHERE id=?",
                (latitude, longitude, accuracy_m, "Authorised reviewer pin", reason or None, actor, changed_at, changed_at, doc_id),
            )
            detail = "Document GIS pin changed from (%s, %s) to (%.6f, %.6f)." % (previous[0], previous[1], latitude, longitude)
            if reason:
                detail += " Reason: %s" % reason
            action = "location_set"
            result = {
                "ok": True, "lat": latitude, "lon": longitude, "accuracy_m": accuracy_m,
                "location_status": "VERIFIED_LOCATION",
                "location_source": "Authorised reviewer pin",
                "location_verified_by": actor,
                "location_verified_at": changed_at,
                "updated_at": changed_at,
            }
    log_audit(actor, action, detail, doc_id)
    return result


def _ring_geometry_checks(points):
    """Structural validation for a closed polygon ring.

    Rejects non-finite coordinates first (audit defect D4): NaN/Infinity
    comparisons are always False, so every later check would silently pass and
    a poisoned ring could reach the database or crash response serialisation.
    """
    if len(points) < 4 or points[0] != points[-1]:
        return False, "Boundary ring must be closed."
    for point in points:
        if len(point) < 2:
            return False, "Each boundary corner needs a longitude and latitude."
        try:
            lon, lat = float(point[0]), float(point[1])
        except (TypeError, ValueError):
            return False, "Boundary coordinates must be numeric."
        if not (math.isfinite(lon) and math.isfinite(lat)):
            return False, "Boundary coordinates must be finite numbers (no NaN or infinity)."
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return False, "Boundary coordinates are outside valid latitude/longitude ranges."
    vertices = [(float(p[0]), float(p[1])) for p in points[:-1]]
    area2 = sum(vertices[i][0]*vertices[(i+1)%len(vertices)][1] - vertices[(i+1)%len(vertices)][0]*vertices[i][1] for i in range(len(vertices)))
    if not math.isfinite(area2) or abs(area2) < 1e-12:
        return False, "Boundary area is zero or too small."
    def orient(a,b,d):
        return (b[0]-a[0])*(d[1]-a[1])-(b[1]-a[1])*(d[0]-a[0])
    def crosses(a,b,d,e):
        o1,o2,o3,o4=orient(a,b,d),orient(a,b,e),orient(d,e,a),orient(d,e,b)
        return ((o1>0>o2) or (o1<0<o2)) and ((o3>0>o4) or (o3<0<o4))
    for i in range(len(vertices)):
        a,b=vertices[i],vertices[(i+1)%len(vertices)]
        for j in range(i+1,len(vertices)):
            if j in {i-1,i,i+1,len(vertices)-1}: continue
            if crosses(a,b,vertices[j],vertices[(j+1)%len(vertices)]):
                return False, "Boundary edges cross; draw a simple polygon."
    return True, ""

def _record_geometry_change(
    db: Any,
    doc_id: str,
    action: str,
    previous_geometry: Optional[str],
    new_geometry: Optional[str],
    previous_source: Optional[str],
    new_source: Optional[str],
    reason: str,
    actor: str,
    created_at: float,
) -> None:
    """Persist a recoverable before/after geometry change (audit defect D8).

    Runs inside the caller's transaction so a failed write never leaves a
    history entry without its geometry update (or vice versa)."""
    db.execute(
        """INSERT INTO document_geometry_history
           (id,document_id,action,previous_geometry,new_geometry,previous_source,new_source,reason,actor,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (uuid.uuid4().hex, doc_id, action, previous_geometry, new_geometry,
         previous_source, new_source, (reason or "")[:500] or None, actor, created_at),
    )


def _assert_not_stale(row: Any, payload: Dict[str, Any], label: str = "record") -> None:
    """Optimistic concurrency: optional `expected_updated_at` guards against
    overwriting a newer edit made by another reviewer (audit defect D13).

    Omitted/None keeps the legacy last-write-wins behaviour, so existing
    clients remain compatible."""
    expected = payload.get("expected_updated_at")
    if expected in (None, "", 0):
        return
    try:
        expected_value = float(expected)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="expected_updated_at must be a numeric timestamp.")
    current = row["updated_at"] if "updated_at" in row.keys() else None
    try:
        current_value = float(current) if current is not None else None
    except (TypeError, ValueError):
        current_value = None
    if current_value is None or abs(current_value - expected_value) > 1e-6:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "STALE_RECORD",
                "detail": f"This {label} was modified by someone else since you loaded it. Reload and re-apply your change.",
                "current_updated_at": current_value,
            },
        )


@map_router.put("/records/{doc_id}/boundary")
def map_set_document_boundary(
    doc_id: str,
    payload: Dict[str, Any],
    user: Dict[str, Any] = Depends(require_roles(ROLE_VERIFICATION_OFFICER, ROLE_ADMIN)),
):
    """Persist an officer-traced plot boundary without changing OCR fields."""
    raw_points = payload.get("points")
    reason = str(payload.get("reason") or "").strip()[:500]
    if not isinstance(raw_points, list) or not 3 <= len(raw_points) <= 50:
        raise HTTPException(status_code=400, detail="A boundary needs 3 to 50 valid corner points.")
    points: List[List[float]] = []
    for raw in raw_points:
        try:
            if isinstance(raw, dict):
                latitude, longitude = float(raw.get("lat")), float(raw.get("lon"))
            else:
                latitude, longitude = float(raw[0]), float(raw[1])
        except (TypeError, ValueError, IndexError):
            raise HTTPException(status_code=400, detail="Each boundary corner must have numeric latitude and longitude.")
        if not (math.isfinite(latitude) and math.isfinite(longitude)):
            raise HTTPException(status_code=400, detail="Boundary coordinates must be finite numbers (no NaN or infinity).")
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise HTTPException(status_code=400, detail="Boundary coordinates are outside valid latitude/longitude ranges.")
        points.append([round(longitude, 7), round(latitude, 7)])
    if len({(point[0], point[1]) for point in points}) < 3:
        raise HTTPException(status_code=400, detail="A boundary requires at least three distinct corners.")
    if points[0] != points[-1]:
        points.append(points[0])
    valid, error = _ring_geometry_checks(points)
    if not valid:
        raise HTTPException(status_code=400, detail=error)
    geometry = {"type": "Polygon", "coordinates": [points]}
    geometry_json = _json(geometry)
    actor = user.get("full_name", user.get("email", "user"))
    changed_at = _now()
    with get_db() as db:
        row = db.execute("SELECT id, map_geometry, map_geometry_source, updated_at FROM documents WHERE id=?", (doc_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Record not found.")
        _assert_not_stale(row, payload)
        previous_geometry = row["map_geometry"] if "map_geometry" in row.keys() else None
        previous_source = row["map_geometry_source"] if "map_geometry_source" in row.keys() else None
        db.execute(
            """UPDATE documents SET map_geometry=?, map_geometry_source=?, map_geometry_status='VERIFIED_BOUNDARY',
               map_geometry_reason=?, map_geometry_updated_at=?, updated_at=? WHERE id=?""",
            (geometry_json, "Officer-digitized boundary", reason or None, changed_at, changed_at, doc_id),
        )
        _record_geometry_change(db, doc_id, "boundary_set", previous_geometry, geometry_json,
                                previous_source, "Officer-digitized boundary", reason, actor, changed_at)
    detail = f"Officer-digitized mapping boundary saved with {len(points) - 1} corners."
    if reason:
        detail += f" Reason: {reason}"
    log_audit(actor, "boundary_set", detail, doc_id)
    return {"ok": True, "geometry": geometry, "geometry_source": "Officer-digitized boundary", "updated_at": changed_at}



def _boundary_area_m2(points: Sequence[Sequence[float]]) -> float:
    if len(points) < 4:
        return 0.0
    lat0 = sum(float(p[1]) for p in points[:-1]) / max(1, len(points) - 1)
    mlat = 111320.0
    mlon = 111320.0 * max(0.05, abs(__import__("math").cos(__import__("math").radians(lat0))))
    area2 = 0.0
    for i in range(len(points) - 1):
        x1, y1 = float(points[i][0]) * mlon, float(points[i][1]) * mlat
        x2, y2 = float(points[i + 1][0]) * mlon, float(points[i + 1][1]) * mlat
        area2 += x1 * y2 - x2 * y1
    return abs(area2) / 2.0

def _area_m2_from_record(row: Any) -> Optional[float]:
    fields = _parse_json(row["fields"] if "fields" in row.keys() else None, {}) or {}
    raw = _field_value(fields, "area")
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(sq\.?\s*ft|sqft|ft2|acre|acres|hectare|hectares|ha|m2|sq\.?\s*m|sqm)?", raw.casefold())
    if not match:
        return None
    value = float(match.group(1))
    unit = (match.group(2) or "").replace(" ", "").replace(".", "")
    if unit in {"acre","acres"}: return value * 4046.8564224
    if unit in {"hectare","hectares","ha"}: return value * 10000.0
    if unit in {"sqft","ft2","sqft"}: return value * 0.09290304
    return value

@map_router.post("/records/{doc_id}/boundary/estimate")
def map_estimate_document_boundary(
    doc_id: str,
    payload: Optional[Dict[str, Any]] = None,
    user: Dict[str, Any] = Depends(require_roles(ROLE_VERIFICATION_OFFICER, ROLE_ADMIN)),
):
    """Create a clearly labelled screening estimate from recorded area + location."""
    import math
    payload = payload or {}
    with get_db() as db:
        row = db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Record not found.")
    _assert_not_stale(row, payload)
    area_m2 = _area_m2_from_record(row)
    if not area_m2 or area_m2 <= 0:
        raise HTTPException(status_code=400, detail="A numeric recorded area is required to estimate a boundary.")
    lat = row["lat"] if "lat" in row.keys() else None
    lon = row["lon"] if "lon" in row.keys() else None
    if lat is None or lon is None:
        raise HTTPException(status_code=400, detail="Set an exact map location before estimating a boundary.")
    side = math.sqrt(area_m2)
    dlat = side / 111320.0 / 2.0
    dlon = side / (111320.0 * max(0.05, abs(math.cos(math.radians(float(lat))))) * 2.0)
    ring = [[float(lon)-dlon, float(lat)-dlat], [float(lon)+dlon, float(lat)-dlat],
            [float(lon)+dlon, float(lat)+dlat], [float(lon)-dlon, float(lat)+dlat],
            [float(lon)-dlon, float(lat)-dlat]]
    geometry = {"type":"Polygon","coordinates":[ring]}
    reason = str(payload.get("reason") or "").strip()[:500]
    now = _now()
    actor = user.get("full_name", user.get("email", "user"))
    with get_db() as db:
        current = db.execute("SELECT map_geometry, map_geometry_source FROM documents WHERE id=?", (doc_id,)).fetchone()
        previous_geometry = current["map_geometry"] if current and "map_geometry" in current.keys() else None
        previous_source = current["map_geometry_source"] if current and "map_geometry_source" in current.keys() else None
        db.execute("UPDATE documents SET map_geometry=?, map_geometry_source=?, map_geometry_status='ESTIMATED_BOUNDARY', map_geometry_reason=?, map_geometry_updated_at=?, updated_at=? WHERE id=?",
                   (_json(geometry), "Area-based estimate (screening only)", reason or None, now, now, doc_id))
        _record_geometry_change(db, doc_id, "boundary_estimated", previous_geometry, _json(geometry),
                                previous_source, "Area-based estimate (screening only)", reason, actor, now)
    log_audit(actor, "boundary_estimated", f"Area-based screening boundary estimated from {area_m2:.2f} m²." + (f" Reason: {reason}" if reason else ""), doc_id)
    return {"ok":True,"geometry":geometry,"geometry_source":"Area-based estimate (screening only)","source":"estimated","area_m2":round(area_m2,2),"updated_at":now}

def _polygon_ring_from_geojson(geom: Dict[str, Any]) -> List[List[float]]:
    """Validate an incoming GeoJSON Polygon with the same rules as every other
    write path (audit defect D4).

    Field-specific errors, explicit finite/range checks, closure, vertex cap,
    and self-intersection screening. Returns the closed ring as [lon, lat]
    pairs; raises HTTPException(400) on any violation.
    """
    coords = geom.get("coordinates")
    if not isinstance(coords, list) or not coords:
        raise HTTPException(status_code=400, detail="geometry.coordinates: a Polygon needs an outer ring.")
    if not isinstance(coords[0], list):
        raise HTTPException(status_code=400, detail="geometry.coordinates[0]: outer ring must be a list of positions.")
    if len(coords) > 1:
        raise HTTPException(status_code=400, detail="geometry.coordinates: interior rings (holes) are not supported by this importer; supply a single outer ring.")
    if len(coords[0]) > MAP_MAX_IMPORT_VERTICES + 1:
        raise HTTPException(
            status_code=400,
            detail=f"geometry.coordinates[0]: too many vertices ({len(coords[0])}); the limit is {MAP_MAX_IMPORT_VERTICES} (simplify before import).",
        )
    ring: List[List[float]] = []
    for index, point in enumerate(coords[0]):
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            raise HTTPException(status_code=400, detail=f"geometry.coordinates[0][{index}]: each position needs at least [longitude, latitude].")
        try:
            lon, lat = float(point[0]), float(point[1])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"geometry.coordinates[0][{index}]: coordinates must be numeric.")
        if not (math.isfinite(lon) and math.isfinite(lat)):
            raise HTTPException(status_code=400, detail=f"geometry.coordinates[0][{index}]: coordinates must be finite numbers (no NaN or infinity).")
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise HTTPException(
                status_code=400,
                detail=f"geometry.coordinates[0][{index}]: ({lon}, {lat}) is outside the valid WGS84 range (longitude ±180, latitude ±90).",
            )
        ring.append([round(lon, 7), round(lat, 7)])
    if len(ring) < 4:
        raise HTTPException(status_code=400, detail="geometry.coordinates[0]: a Polygon needs at least three corners (four positions when closed).")
    if ring[0] != ring[-1]:
        ring.append(list(ring[0]))
    # Drop consecutive duplicates (a common export artefact) before validation.
    deduped = [ring[0]]
    for point in ring[1:]:
        if point != deduped[-1]:
            deduped.append(point)
    ring = deduped
    if len(ring) < 4:
        raise HTTPException(status_code=400, detail="geometry.coordinates[0]: ring has fewer than three distinct corners.")
    valid, error = _ring_geometry_checks(ring)
    if not valid:
        raise HTTPException(status_code=400, detail=f"geometry.coordinates[0]: {error}")
    return ring


@map_router.post("/records/{doc_id}/boundary/import")
def map_import_document_boundary(
    doc_id: str,
    payload: Dict[str, Any],
    user: Dict[str, Any] = Depends(require_roles(ROLE_VERIFICATION_OFFICER, ROLE_ADMIN)),
):
    geo = payload.get("geojson")
    if not isinstance(geo, dict):
        raise HTTPException(status_code=400, detail="GeoJSON object is required.")
    geom = geo.get("geometry") if geo.get("type") == "Feature" else geo
    if not isinstance(geom, dict) or geom.get("type") != "Polygon":
        raise HTTPException(status_code=400, detail="Only GeoJSON Polygon geometry is supported.")
    if not isinstance(geom.get("coordinates"), list):
        raise HTTPException(status_code=400, detail="geometry.coordinates: Polygon coordinates are required.")
    ring = _polygon_ring_from_geojson(geom)
    reason = str(payload.get("reason") or "").strip()[:500]
    geometry = {"type": "Polygon", "coordinates": [ring]}
    now = _now()
    actor = user.get("full_name", user.get("email", "user"))
    with get_db() as db:
        row = db.execute("SELECT id, map_geometry, map_geometry_source, updated_at FROM documents WHERE id=?", (doc_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Record not found.")
        _assert_not_stale(row, payload)
        previous_geometry = row["map_geometry"] if "map_geometry" in row.keys() else None
        previous_source = row["map_geometry_source"] if "map_geometry_source" in row.keys() else None
        db.execute("UPDATE documents SET map_geometry=?, map_geometry_source=?, map_geometry_status='IMPORTED_BOUNDARY', map_geometry_reason=?, map_geometry_updated_at=?, updated_at=? WHERE id=?",
                   (_json(geometry), "Imported survey GeoJSON", reason or None, now, now, doc_id))
        _record_geometry_change(db, doc_id, "boundary_imported", previous_geometry, _json(geometry),
                                previous_source, "Imported survey GeoJSON", reason, actor, now)
    log_audit(actor, "boundary_imported",
              f"Imported survey polygon with {len(ring) - 1} corners." + (f" Reason: {reason}" if reason else ""),
              doc_id)
    return {"ok": True, "geometry": geometry, "geometry_source": "Imported survey GeoJSON", "source": "imported", "updated_at": now}

@map_router.post("/records/{doc_id}/boundary/clear")
def map_clear_document_boundary(
    doc_id: str,
    payload: Optional[Dict[str, Any]] = None,
    user: Dict[str, Any] = Depends(require_roles(ROLE_VERIFICATION_OFFICER, ROLE_ADMIN)),
):
    payload = payload or {}
    reason = str(payload.get("reason") or "").strip()[:500]
    now = _now()
    actor = user.get("full_name", user.get("email", "user"))
    with get_db() as db:
        row = db.execute("SELECT id, map_geometry, map_geometry_source, updated_at FROM documents WHERE id=?", (doc_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Record not found.")
        _assert_not_stale(row, payload)
        previous_geometry = row["map_geometry"] if "map_geometry" in row.keys() else None
        previous_source = row["map_geometry_source"] if "map_geometry_source" in row.keys() else None
        db.execute("UPDATE documents SET map_geometry=NULL, map_geometry_source=NULL, map_geometry_status=NULL, map_geometry_reason=?, map_geometry_updated_at=NULL, updated_at=? WHERE id=?",
                   (reason or None, now, doc_id))
        _record_geometry_change(db, doc_id, "boundary_cleared", previous_geometry, None,
                                previous_source, None, reason, actor, now)
    log_audit(actor, "boundary_cleared",
              "Mapping boundary cleared." + (f" Reason: {reason}" if reason else ""),
              doc_id)
    return {"ok": True, "updated_at": now}


@map_router.get("/records/{doc_id}/boundary/history")
def map_boundary_history(
    doc_id: str,
    user: Dict[str, Any] = Depends(get_current_user),
):
    """Recoverable before/after geometry history for one record (D8).

    Read access follows the canonical document visibility rules; the currently
    stored geometry is included so a reviewer can compare versions."""
    ensure_schema()
    with get_db() as db:
        row = db.execute("SELECT id, status, uploaded_by, map_geometry, map_geometry_source, map_geometry_status, map_geometry_reason, map_geometry_updated_at FROM documents WHERE id=?", (doc_id,)).fetchone()
        if not row or not _map_document_visible(row, user):
            raise HTTPException(status_code=404, detail="Record not found or access denied.")
        history_rows = db.execute(
            """SELECT id, action, previous_geometry, new_geometry, previous_source, new_source, reason, actor, created_at
               FROM document_geometry_history WHERE document_id=? ORDER BY created_at DESC, id DESC""",
            (doc_id,),
        ).fetchall()
    return {
        "document_id": doc_id,
        "current": {
            "geometry": _parse_json(row["map_geometry"], None) if "map_geometry" in row.keys() else None,
            "source": row["map_geometry_source"] if "map_geometry_source" in row.keys() else None,
            "status": row["map_geometry_status"] if "map_geometry_status" in row.keys() else None,
            "reason": row["map_geometry_reason"] if "map_geometry_reason" in row.keys() else None,
            "updated_at": row["map_geometry_updated_at"] if "map_geometry_updated_at" in row.keys() else None,
        },
        "items": [
            {
                "id": item["id"],
                "action": item["action"],
                "previous_geometry": _parse_json(item["previous_geometry"], None),
                "new_geometry": _parse_json(item["new_geometry"], None),
                "previous_source": item["previous_source"],
                "new_source": item["new_source"],
                "reason": item["reason"],
                "actor": item["actor"],
                "created_at": item["created_at"],
            }
            for item in history_rows
        ],
        "disclaimer": "Geometry history records editor actions; it does not certify any boundary as an official cadastral record.",
    }

def _public_geocode_result(value: Dict[str, Any]) -> Dict[str, Any]:
    return {key: item for key, item in value.items() if not key.startswith("_")}


@map_router.post("/geocode")
def map_geocode(payload: Dict[str, Any], user: Dict[str, Any] = Depends(get_current_user)):
    """Locate an explicitly selected record, with the land_records-style cascade.

    Keep legacy query callers working. Structured callers get village-first
    lookup and a clearly labelled district fallback, never an invented parcel pin.
    """
    ensure_schema()
    query = str(payload.get("query") or "").strip()
    if query:
        if len(query) > 200:
            raise HTTPException(status_code=400, detail="query too long (max 200 chars)")
        # Free-form OCR/document text must never be sent to a third-party geocoder.
        # Only place-shaped legacy queries are accepted; structured place fields below
        # are preferred for new clients.
        sensitive_markers = (
            "owner", "father", "applicant", "petitioner", "respondent", "case number",
            "court", "document", "doc id", "account", "phone", "mobile", "email",
            "khasra", "khata", "mutation", "encumbrance", "address"
        )
        if any(marker in query.casefold() for marker in sensitive_markers):
            raise HTTPException(status_code=400, detail="Use village, tehsil, district and state fields for geocoding; private document/person data is not accepted.")
        parts = [part.strip() for part in re.split(r",|\\n", query) if part.strip()]
        if len(parts) > 5:
            raise HTTPException(status_code=400, detail="Place query may contain at most five place components.")
        result = _map_geocode_query(query)
        log_audit(user.get("full_name", user.get("email", "user")), "geocode_requested",
                  "Map geocode requested for a place-only query; no parcel coordinate was created.",
                  None)
        return result
    village, tehsil, district, state = (
        str(payload.get(name) or "").strip() for name in ("village", "tehsil", "district", "state")
    )
    if not (village or district) or sum(map(len, (village, tehsil, district, state))) > 200:
        raise HTTPException(status_code=400, detail="village/district required (max 200 chars combined)")
    levels = []
    if village:
        if tehsil:
            levels.append(([village, tehsil, district, state], "village"))
        levels.extend([([village, district, state], "village"),
                       ([village, district], "village"), ([village, state], "village")])
    if district:
        levels.append(([district, state], "district"))
    attempted = set()
    result = {"lat": None, "lon": None}
    for parts, level in levels:
        query = ", ".join([part for part in parts if part] + ["India"])
        if query in attempted:
            continue
        attempted.add(query)
        result = _map_geocode_query(query, state=state, district=district)
        if result.get("lat") is not None and result.get("lon") is not None:
            log_audit(user.get("full_name", user.get("email", "user")), "geocode_resolved",
                      f"Map place geocode resolved at {level} level; no parcel coordinate was created.", None)
            return {**result, "match_level": level, "approximate": True}
        # An offline provider cannot improve with four more requests.
        if result.get("unavailable"):
            break
    return {**result, "match_level": "unresolved"}


def _geocode_candidate_matches(item: Dict[str, Any], state: str, district: str) -> bool:
    address = item.get("address") or {}
    display = _normalise(item.get("display_name") or "")
    def matches(wanted, candidates):
        wanted = _normalise(wanted)
        return not wanted or any(wanted in _normalise(value) for value in candidates if value)
    # Never accept a same-named village in a conflicting state/district.
    # Display-name fallback supports providers with incomplete address objects.
    return matches(state, [address.get("state") or display]) and matches(
        district, [address.get("county"), address.get("state_district"),
                   address.get("city_district"), address.get("district"), display])


def _map_geocode_query(query: str, *, state: str = "", district: str = "") -> Dict[str, Any]:
    global _map_last_geocode_request
    # Structured matches must not reuse an unchecked legacy first-result cache.
    key = _normalise(query) if not (state or district) else json.dumps(
        ["scoped", _normalise(query), _normalise(state), _normalise(district)], ensure_ascii=False)
    cached = _map_geocode_cache.get(key)
    if cached is not None and (_now() - float(cached.get("_cached_at", 0))) < MAP_GEOCODE_TTL_SECONDS:
        return {**_public_geocode_result(cached), "cached": True}
    _map_geocode_cache.pop(key, None)
    # Reuse the persistent land cache where possible, but do not alter its
    # structured response shape.
    with get_db() as db:
        cached_row = db.execute("SELECT latitude, longitude, display_name, status, created_at FROM geocode_cache WHERE cache_key=?", (key,)).fetchone()
    if cached_row and cached_row["status"] == "RESOLVED" and (_now() - float(cached_row["created_at"] or 0)) < MAP_GEOCODE_TTL_SECONDS:
        cached_result = {
            "query": query,
            "lat": cached_row["latitude"],
            "lon": cached_row["longitude"],
            "display_name": cached_row["display_name"],
            "source": "Nominatim persistent cache",
        }
        _map_geocode_cache[key] = {**cached_result, "_cached_at": _now()}
        return {**cached_result, "cached": True}

    params = urllib.parse.urlencode({"format": "jsonv2", "limit": 5, "addressdetails": 1,
                                     "q": query, "countrycodes": "in"})
    request_obj = urllib.request.Request(
        MAP_GEOCODER_URL + "?" + params,
        headers={"User-Agent": MAP_NOMINATIM_USER_AGENT, "Accept": "application/json"},
    )
    try:
        # Thread-safe throttling: two simultaneous record clicks must not send
        # concurrent requests to Nominatim from FastAPI's worker pool.
        with _map_geocode_lock:
            elapsed = _now() - _map_last_geocode_request
            if _map_last_geocode_request and elapsed < 1.1:
                time.sleep(1.1 - elapsed)
            _map_last_geocode_request = _now()
            with urllib.request.urlopen(request_obj, timeout=10) as response:
                data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return {"query": query, "lat": None, "lon": None, "display_name": None,
                "error": "geocoding unavailable (offline?) - try again later", "cached": False,
                "unavailable": True}
    result = {"query": query, "lat": None, "lon": None, "display_name": None,
              "error": "no match found for '%s'" % query}
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or not _geocode_candidate_matches(item, state, district):
            continue
        latitude, longitude = _number(item.get("lat")), _number(item.get("lon"))
        if latitude is None or longitude is None or not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            continue
        result = {"query": query, "lat": latitude, "lon": longitude,
                  "display_name": item.get("display_name"), "source": "Nominatim", "cached": False}
        break
    # Negative results get a short cache, not a seven-day permanent dead end.
    ttl_offset = 0 if result["lat"] is not None else MAP_GEOCODE_TTL_SECONDS - 60
    _map_geocode_cache[key] = {**{k: v for k, v in result.items() if k != "cached"}, "_cached_at": _now() - ttl_offset}
    # Persist only valid coordinate results in the shared cache. An unresolved
    # network response should be retried later rather than frozen indefinitely.
    if result.get("lat") is not None and result.get("lon") is not None:
        with get_db() as db:
            db.execute(
                """INSERT INTO geocode_cache(cache_key,village,taluka,district,state,latitude,longitude,display_name,status,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET latitude=excluded.latitude,
                   longitude=excluded.longitude,display_name=excluded.display_name,status=excluded.status,created_at=excluded.created_at""",
                (key, query, "", "", "", result["lat"], result["lon"], result.get("display_name"), "RESOLVED", _now()),
            )
    return result


def _history_item(row: Any) -> Dict[str, Any]:
    item = _map_document_item(row)
    audit_item = _map_location_audit_context([str(item["id"])]).get(str(item["id"]))
    if item["lat"] is not None and item["lon"] is not None and audit_item and audit_item.get("action") == "location_set":
        item["location_verified_by"] = audit_item.get("verified_by")
        item["location_verified_at"] = audit_item.get("verified_at")
    item["location_audit_available"] = bool(audit_item)
    return {
        "id": item["id"], "filename": item["filename"], "doc_type": item["doc_type"],
        "status": item["status"], "owner": item["owner"], "father": item["father"],
        "survey": item["survey"], "khasra": item["khasra"], "khata": item["khata"],
        "area": item["area"], "village": item["village"], "tehsil": item["tehsil"],
        "district": item["district"], "state": item["state"], "year": item["year"],
        "lat": item["lat"], "lon": item["lon"], "location_status": item["location_status"],
        "location_state": item["location_state"], "location_label": item["location_label"],
        "location_source": item["location_source"], "location_confidence": item["location_confidence"],
        "location_verified_by": item.get("location_verified_by"), "location_verified_at": item.get("location_verified_at"),
        "location_audit_available": item["location_audit_available"],
        "created_at": item["created_at"],
    }


def _history_summary(items: Sequence[Dict[str, Any]], ownership: Dict[str, Any]) -> Dict[str, Any]:
    owners = []
    changes = []
    transfer_documents = []
    previous_owner = ""
    for item in items:
        owner = str(item.get("owner") or "").strip()
        if owner and _normalise(owner) not in {_normalise(value) for value in owners}:
            owners.append(owner)
        if owner and previous_owner and _normalise(owner) != _normalise(previous_owner):
            changes.append({"from": previous_owner, "to": owner, "document_id": item.get("id"), "year": item.get("year")})
        if owner:
            previous_owner = owner
        if _is_transfer_document(item):
            transfer_documents.append(item.get("id"))
    return {
        "record_count": len(items),
        "owners": owners,
        "owner_changes": changes,
        "transfer_documents": transfer_documents,
        "assessment": ownership.get("assessment", "INSUFFICIENT_DATA"),
        "review_required": bool(ownership.get("findings") or ownership.get("relationships")),
        "disclaimer": "A history signal supports review; it does not establish title or legal ownership.",
    }


@document_history_router.get("/documents/{doc_id}/history")
def document_history(doc_id: str, user: Dict[str, Any] = Depends(get_current_user)):
    """Return a complete survey/village passbook, including the current record."""
    ensure_schema()
    with get_db() as db:
        current = db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        if not current:
            raise HTTPException(status_code=404, detail="Document not found.")
        if not _map_document_visible(current, user):
            raise HTTPException(status_code=403, detail="You do not have access to this record.")
        current_fields = _parse_json(current["fields"], {}) or {}
        survey = _field_value(current_fields, "survey_number")
        village = _field_value(current_fields, "village")
        # Slim column set: the history view never needs the heavy OCR payloads
        # (audit defect D11); ordering includes id so equal timestamps stay stable.
        rows = db.execute(
            f"SELECT {', '.join(_MAP_RECORD_COLUMNS)} FROM documents ORDER BY created_at ASC, id ASC"
        ).fetchall()
    if survey:
        survey_key = _land_number(survey).casefold()
        village_key = _normalise(village)
        rows = [
            row for row in rows
            if _map_document_visible(row, user)
            and _land_number(_field_value(_parse_json(row["fields"], {}) or {}, "survey_number")).casefold() == survey_key
            and (not village_key or _normalise(_field_value(_parse_json(row["fields"], {}) or {}, "village")) == village_key)
        ]
    else:
        rows = [current]
    items = [_history_item(row) for row in rows]
    def _history_sort_key(item: Dict[str, Any]) -> Tuple[int, str]:
        match = re.search(r"\b(19\d{2}|20\d{2})\b", str(item.get("year") or ""))
        return (int(match.group(1)) if match else 9999, str(item.get("id") or ""))
    items.sort(key=_history_sort_key)
    # Keep the Portfolio passbook useful for transfer-aware review: ownership
    # changes are surfaced as deterministic signals, never auto-approved.
    ownership_records = []
    for row in rows:
        item = dict(row)
        item["fields"] = _parse_json(item.get("fields"), {}) or {}
        ownership_records.append(item)
    ownership_history = analyze_ownership_history(ownership_records) if ownership_records else {"assessment": "INSUFFICIENT_DATA", "events": [], "findings": [], "relationships": []}
    return {"survey": survey, "village": village, "current_id": doc_id,
            "items": items, "including_current": any(item["id"] == doc_id for item in items),
            "ownership_history": ownership_history,
            "history_summary": _history_summary(items, ownership_history)}



# Run the compatibility schema migration once at import AND lazily per active
# database. Tests and tools swap server.DB_PATH at runtime; the import-time call
# alone left later databases without the mapping tables ("no such table:
# properties"), which broke parcel resolution and the map workspace.
_ENSURED_DB_KEYS: set = set()


def _db_schema_key() -> str:
    import server
    return "|".join(str(getattr(server, name, "") or "")
                    for name in ("DATABASE_URL", "DB_PATH", "SQLITE_PATH"))


def ensure_schema() -> None:
    """Create/migrate the mapping schema for the CURRENT database (once per DB).

    Idempotent and free after the first call for a given database, so it is
    safe on hot request paths (no DDL is issued once the schema exists)."""
    key = _db_schema_key()
    if key in _ENSURED_DB_KEYS:
        return
    _ensure_tables()
    _ENSURED_DB_KEYS.add(key)


ensure_schema()
