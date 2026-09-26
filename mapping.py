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
import io
import json
import re
import time
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
)

map_router = APIRouter(prefix="/api/map", tags=["Document Map"])
document_history_router = APIRouter(prefix="/api", tags=["Document History"])

MAP_NOMINATIM_USER_AGENT = "Document-Screening-Portfolio-Map/1.0"
MAP_GEOCODE_TTL_SECONDS = 7 * 24 * 60 * 60
_map_geocode_cache: Dict[str, Dict[str, Any]] = {}
_map_last_geocode_request = 0.0


def _now() -> float:
    return time.time()


def reference_geometry_for_slot(slot: int, kind: str = "polygon") -> Dict[str, Any]:
    """Build reference geometry by translating the existing synthetic grid.

    The translation uses only the spacing already present between
    ``_SYNTHETIC_PROPERTIES``. It is not a geocode, not a surveyed boundary,
    and must never be copied into document latitude/longitude pins.
    ``kind`` is ``polygon``, ``point``, ``missing_centroid`` or ``unresolved``.
    """
    unresolved = {
        "geometry": None,
        "centroid": None,
        "geometry_source": "Synthetic demo parcel — reference geometry intentionally unresolved",
        "geometry_confidence": None,
        "location_status": "UNRESOLVED",
    }
    if str(kind or "").casefold() == "unresolved" or int(slot or 0) <= 0:
        return unresolved
    base = _SYNTHETIC_PROPERTIES[0]
    ring = base["geometry"]["coordinates"][0]
    base_centroid = base["centroid"]
    step_lon = round(_SYNTHETIC_PROPERTIES[1]["centroid"][0] - base_centroid[0], 5) or 0.004
    step_lat = round(_SYNTHETIC_PROPERTIES[2]["centroid"][1] - base_centroid[1], 5) or 0.00425
    column = (int(slot) - 1) % 4
    row = (int(slot) - 1) // 4
    # Start east of the existing DEMO-PROP block so these shapes do not sit on
    # those parcels or inherit their survey identity.
    dx = round((3 + column) * step_lon, 5)
    dy = round(row * step_lat, 5)
    translated = [[round(point[0] + dx, 5), round(point[1] + dy, 5)] for point in ring]
    centroid = [round(base_centroid[0] + dx, 5), round(base_centroid[1] + dy, 5)]
    source = "Project-owned synthetic reference geometry (existing demonstration grid; not an authoritative pin)"
    if str(kind).casefold() == "point":
        return {
            "geometry": {"type": "Point", "coordinates": centroid},
            "centroid": centroid,
            "geometry_source": source,
            "geometry_confidence": 0.9,
            "location_status": "REFERENCE_GEOMETRY",
        }
    return {
        "geometry": {"type": "Polygon", "coordinates": [translated]},
        "centroid": None if str(kind).casefold() == "missing_centroid" else centroid,
        "geometry_source": source,
        "geometry_confidence": 0.8 if str(kind).casefold() == "missing_centroid" else 0.95,
        "location_status": "REFERENCE_GEOMETRY",
    }


LAND_IDENTIFIER_FIELDS = ("survey_number", "gat_number", "khasra_number")


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


def _ensure_tables() -> None:
    """Create/migrate the local land schema and seed synthetic parcel geometry."""
    with get_db() as db:
        # The coordinates belong to the mapping layer only.  Existing OCR,
        # validation, and audit columns remain untouched.
        _ensure_document_column(db, "lat", "REAL")
        _ensure_document_column(db, "lon", "REAL")
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
    with get_db() as db:
        row = _property_row(db, property_id)
    return _property_dict(row) if row else None


def _resolve(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve extracted identity fields to controlled parcel candidates."""
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
            "confidence": 0.0,
            "matches": [],
            "conflicting_fields": [],
            "reasons": ["No controlled property identity fields were extracted."],
        }

    with get_db() as db:
        rows = db.execute("SELECT * FROM properties ORDER BY property_id").fetchall()

    candidates: List[Dict[str, Any]] = []
    for row in rows:
        property_data = _property_dict(row)
        matched: List[str] = []
        conflicting: List[str] = []
        for field_name, document_value in available.items():
            parcel_value = row[field_name] if field_name in row.keys() else None
            if not parcel_value:
                continue
            if _key_for(field_name, document_value) == _key_for(field_name, parcel_value):
                matched.append(field_name)
            else:
                conflicting.append(field_name)
        strong_matches = [field for field in matched if field in LAND_IDENTIFIER_FIELDS]
        geography_matches = [field for field in matched if field in ("village", "taluka", "district")]
        if not matched:
            continue
        score = min(1.0, (len(strong_matches) * 0.48) + (len(geography_matches) * 0.14) + (0.10 if "sub_division" in matched else 0))
        if strong_matches and not conflicting:
            status_value = "MATCH" if len(strong_matches) >= 1 else "POSSIBLE MATCH"
        elif strong_matches:
            status_value = "NO MATCH" if len(conflicting) >= 1 else "POSSIBLE MATCH"
        else:
            status_value = "POSSIBLE MATCH"
        candidates.append({
            "property": property_data,
            "score": round(score, 4),
            "confidence": round(min(0.99, max(score, 0.35 if strong_matches else 0.2)), 4),
            "matched_fields": matched,
            "conflicting_fields": conflicting,
            "reasons": [
                *(f"Matched {field}." for field in matched),
                *(f"Conflicting {field}." for field in conflicting),
            ],
            "status": status_value,
        })

    candidates.sort(key=lambda item: (item["status"] == "NO MATCH", -item["score"], item["property"]["property_id"]))
    if not candidates:
        return {
            "status": "NO MATCH",
            "confidence": 0.0,
            "matches": [],
            "conflicting_fields": [],
            "reasons": ["No controlled parcel matched the extracted identity."],
        }

    best = candidates[0]
    all_conflicts = list(best.get("conflicting_fields") or [])
    return {
        "status": best["status"],
        "confidence": best["confidence"],
        "matches": candidates[:10],
        "conflicting_fields": all_conflicts,
        "reasons": best["reasons"],
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
    item = {
        "id": row["id"],
        "filename": row["filename"],
        "doc_type": row["doc_type"] or "Land Record",
        "status": row["status"],
        "owner": _field_value(fields, "owner_name"),
        "father": _field_value(fields, "father_name"),
        "survey": _field_value(fields, "survey_number"),
        "khasra": _field_value(fields, "khasra_number"),
        "khata": _field_value(fields, "khata_number"),
        "plot": _field_value(fields, "plot_number"),
        "area": _field_value(fields, "area"),
        "village": village,
        "tehsil": _field_value(fields, "tehsil", "taluka"),
        "district": _field_value(fields, "district"),
        "state": _field_value(fields, "state"),
        "year": _field_value(fields, "khatauni_year", "year", "document_date"),
        "lat": float(lat) if lat is not None else None,
        "lon": float(lon) if lon is not None else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"] if "updated_at" in row.keys() else row["created_at"],
        "location_status": location_status,
        "location_state": "VERIFIED_LOCATION" if location_status == "EXACT_PIN" else ("APPROXIMATE_VILLAGE_LOCATION" if location_status == "VILLAGE_LEVEL" else "LOCATION_NOT_AVAILABLE"),
        "location_label": "VERIFIED LOCATION" if location_status == "EXACT_PIN" else ("APPROXIMATE — VILLAGE LOCATION" if location_status == "VILLAGE_LEVEL" else "LOCATION NOT AVAILABLE"),
        "location_source": location_source,
        "location_confidence": float(persisted_confidence) if persisted_confidence is not None else None,
        "location_verified_by": verified_by if has_exact_pin else None,
        "location_verified_at": verified_at if has_exact_pin else None,
        "location_reason": row["location_reason"] if "location_reason" in row.keys() else None,
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
    if not document_ids:
        return {}
    wanted = {str(value) for value in document_ids}
    with get_db() as db:
        rows = db.execute(
            """SELECT doc_id, ts, username, action, detail
               FROM audit
               WHERE action IN ('location_set', 'location_cleared')
               ORDER BY ts DESC, id DESC"""
        ).fetchall()
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
    "lat", "lon", "uploaded_by", "created_at", "updated_at",
)


def _map_visible_records(user: Dict[str, Any]) -> List[Dict[str, Any]]:
    with get_db() as db:
        rows = db.execute(
            f"SELECT {', '.join(_MAP_RECORD_COLUMNS)} FROM documents ORDER BY created_at DESC LIMIT 10000"
        ).fetchall()
    visible = [row for row in rows if _map_document_visible(row, user)]
    records = [_map_document_item(row) for row in visible]
    document_ids = [str(record["id"]) for record in records]
    audit_context = _map_location_audit_context(document_ids)
    property_context = _map_property_context(document_ids)
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
        property_item = property_context.get(str(record["id"]))
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
        # Use geometry already loaded for this linked parcel. Do not query again
        # here: this function is also the land-record visibility index.
        if not has_coordinates and record.get("reference_geometry"):
            record["location_status"] = "REFERENCE_GEOMETRY"
            record["location_state"] = "REFERENCE_GEOMETRY"
            record["location_label"] = "Reference geometry"
            record["location_source"] = "Project-owned reference geometry"
            record["location_confidence"] = "REFERENCE"
            record["parcel_id"] = property_item.get("parcel_id")
            record["property_id"] = property_item.get("property_id")
            record["has_reference_geometry"] = True
            provenance = record.get("location_provenance")
            if isinstance(provenance, dict):
                provenance["source"] = "Project-owned reference geometry"
                provenance["authoritative"] = False
    return records


def _annotate_reference_labels(records: List[Dict[str, Any]]) -> None:
    """Identity labels for the map endpoints only. Not used by Land Intelligence lists."""
    try:
        import parcel_locator
        parcel_locator.annotate_map_records(records)
    except Exception:
        pass


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
    limit: int = Query(5000, ge=1, le=10000),
    user: Dict[str, Any] = Depends(get_current_user),
):
    """Return Portfolio-style document map records with server-side filters.

    Exact coordinates are human-set pins. Records without a pin remain useful
    because their village/district fields can be geocoded and cached at
    village level. The summary metadata lets a future client render a map
    dashboard without downloading a second dataset.
    """
    all_records = _map_visible_records(user)
    _annotate_reference_labels(all_records)
    filtered = [record for record in all_records if _map_record_matches(
        record, q=_normalise(q), district=district, tehsil=tehsil, village=village,
        status=status, location=location)]
    return {
        "records": filtered[:limit],
        "total": len(filtered),
        "metadata": {
            "authoritative": False,
            "source": "Screened document fields and authorised reviewer pins",
            "summary": _map_summary(all_records),
            "filters": {"q": q, "district": district, "tehsil": tehsil, "village": village, "status": status, "location": location},
        },
    }


@map_router.get("/summary")
def map_summary(user: Dict[str, Any] = Depends(get_current_user)):
    """Compact map dashboard metrics for the portal and integrations."""
    records = _map_visible_records(user)
    _annotate_reference_labels(records)
    return {"summary": _map_summary(records), "metadata": {"authoritative": False}}


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
    with get_db() as db:
        rows = db.execute("SELECT * FROM properties ORDER BY village, survey_number, property_id LIMIT 5000").fetchall()
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


@map_router.get("/export.csv")
def map_export_csv(user: Dict[str, Any] = Depends(get_current_user)):
    """Download a review-friendly map register without exposing hidden records."""
    output = io.StringIO(newline="")
    output.write("\\ufeff")
    writer = csv.DictWriter(output, fieldnames=(
        "id", "filename", "doc_type", "status", "owner", "survey", "khasra", "khata", "plot",
        "area", "village", "tehsil", "district", "state", "year", "lat", "lon",
        "location_status", "location_state", "location_label", "location_source", "location_confidence",
        "location_verified_by", "location_verified_at", "location_audit_available", "review_required"), extrasaction="ignore")
    writer.writeheader()
    exported = _map_visible_records(user)
    _annotate_reference_labels(exported)
    writer.writerows(exported)
    return Response(content=output.getvalue(), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": "attachment; filename=land-map-register.csv",
        "Cache-Control": "no-store",
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
    reason = str(payload.get("reason") or "").strip()[:500]
    if (raw_lat is None) != (raw_lon is None):
        raise HTTPException(status_code=400, detail="lat and lon must be supplied together (or both be null).")
    actor = user.get("full_name", user.get("email", "user"))
    changed_at = _now()
    with get_db() as db:
        row = db.execute("SELECT id, lat, lon FROM documents WHERE id=?", (doc_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Record not found.")
        previous = (row["lat"], row["lon"])
        if raw_lat is None:
            db.execute(
                "UPDATE documents SET lat=NULL, lon=NULL, updated_at=? WHERE id=?",
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
            }
        else:
            try:
                latitude, longitude = float(raw_lat), float(raw_lon)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="lat/lon must be numbers (or null to clear).")
            if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
                raise HTTPException(status_code=400, detail="lat must be -90..90 and lon -180..180.")
            db.execute(
                "UPDATE documents SET lat=?, lon=?, updated_at=? WHERE id=?",
                (latitude, longitude, changed_at, doc_id),
            )
            detail = "Document GIS pin changed from (%s, %s) to (%.6f, %.6f)." % (previous[0], previous[1], latitude, longitude)
            if reason:
                detail += " Reason: %s" % reason
            action = "location_set"
            result = {
                "ok": True, "lat": latitude, "lon": longitude,
                "location_status": "VERIFIED_LOCATION",
                "location_source": "Authorised reviewer pin",
                "location_verified_by": actor,
                "location_verified_at": changed_at,
            }
    log_audit(actor, action, detail, doc_id)
    return result


def _public_geocode_result(value: Dict[str, Any]) -> Dict[str, Any]:
    return {key: item for key, item in value.items() if not key.startswith("_")}


@map_router.post("/geocode")
def map_geocode(payload: Dict[str, Any], user: Dict[str, Any] = Depends(get_current_user)):
    """Geocode a free-form village query with a cached Nominatim request."""
    global _map_last_geocode_request
    query = str(payload.get("query") or "").strip()
    if not query or len(query) > 200:
        raise HTTPException(status_code=400, detail="query required (max 200 chars)")
    key = _normalise(query)
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

    elapsed = _now() - _map_last_geocode_request
    if _map_last_geocode_request and elapsed < 1.1:
        time.sleep(max(0.0, 1.1 - elapsed))
    params = urllib.parse.urlencode({"format": "jsonv2", "limit": 1, "q": query, "countrycodes": "in"})
    request_obj = urllib.request.Request(
        "https://nominatim.openstreetmap.org/search?" + params,
        headers={"User-Agent": MAP_NOMINATIM_USER_AGENT, "Accept": "application/json"},
    )
    _map_last_geocode_request = _now()
    data = []
    try:
        with urllib.request.urlopen(request_obj, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return {"query": query, "lat": None, "lon": None, "display_name": None,
                "error": "geocoding unavailable (offline?) - try again later", "cached": False}
    if not isinstance(data, list) or not data:
        result = {"query": query, "lat": None, "lon": None, "display_name": None,
                  "error": "no match found for '%s'" % query}
    else:
        latitude, longitude = _number(data[0].get("lat")), _number(data[0].get("lon"))
        result = {"query": query, "lat": latitude, "lon": longitude,
                  "display_name": data[0].get("display_name"), "source": "Nominatim", "cached": False}
    _map_geocode_cache[key] = {**{k: v for k, v in result.items() if k != "cached"}, "_cached_at": _now()}
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
    with get_db() as db:
        current = db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        if not current:
            raise HTTPException(status_code=404, detail="Document not found.")
        if not _map_document_visible(current, user):
            raise HTTPException(status_code=403, detail="You do not have access to this record.")
        current_fields = _parse_json(current["fields"], {}) or {}
        survey = _field_value(current_fields, "survey_number")
        village = _field_value(current_fields, "village")
        rows = db.execute("SELECT * FROM documents ORDER BY created_at ASC").fetchall()
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



# Run the compatibility schema migration once at import. The canonical app
# calls this again after test/deployment DB swaps; all operations are idempotent.
_ensure_tables()
