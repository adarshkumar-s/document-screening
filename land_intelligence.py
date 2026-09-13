"""Document-grounded synthetic land intelligence and mapping services.

The application treats parcel geometry and resolution as evidence, not as legal
ownership or cadastral truth.  The default records are project-owned synthetic
fixtures so the mapping workspace can run without a government data dependency.
Authorized GeoJSON imports remain explicitly labelled with provenance and are
never silently treated as authoritative.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import time
import unicodedata
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field, model_validator

try:
    from shapely.geometry import mapping as shapely_mapping
    from shapely.geometry import shape as shapely_shape
except Exception:  # pragma: no cover - the dependency is declared in requirements.txt
    shapely_mapping = None
    shapely_shape = None

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


router = APIRouter(prefix="/api/land", tags=["Land Intelligence"])

AREA_TOLERANCE_HA = float(os.getenv("LAND_AREA_TOLERANCE_HA", "0.05"))
MAX_IMPORT_BYTES = 10 * 1024 * 1024
MAX_IMPORT_FEATURES = 5000
NOMINATIM_USER_AGENT = "DILRMP-Land-Intelligence/1.0"
_last_nominatim_request = 0.0
_geocode_cache: Dict[str, Dict[str, Any]] = {}

IDENTITY_FIELDS = (
    "survey_number",
    "gat_number",
    "khasra_number",
    "sub_division",
    "village",
    "taluka",
    "district",
)
LAND_IDENTIFIER_FIELDS = ("survey_number", "gat_number", "khasra_number")


# These are intentionally schematic coordinates.  They are not copied from a
# government record and are only used to make the local demo map inspectable.
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


PROPERTY_COLUMNS = (
    "property_id", "parcel_id", "district", "taluka", "village", "survey_number",
    "gat_number", "khasra_number", "sub_division", "parent_property_id", "area",
    "area_unit", "geometry", "centroid", "latitude", "longitude", "crs", "georeferenced",
    "geometry_source", "geometry_confidence", "data_source", "source_confidence",
    "created_at", "updated_at",
)


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


class GeocodeRequest(BaseModel):
    village: str = Field(min_length=1, max_length=200)
    taluka: Optional[str] = Field(default=None, max_length=200)
    district: Optional[str] = Field(default=None, max_length=200)
    state: Optional[str] = Field(default=None, max_length=200)


class CaseCreate(BaseModel):
    property_id: str = Field(min_length=1, max_length=120)
    title: str = Field(default="Human verification required", max_length=240)
    findings: List[Dict[str, Any]] = Field(default_factory=list)
    warnings: List[Dict[str, Any]] = Field(default_factory=list)
    comparison_results: List[Dict[str, Any]] = Field(default_factory=list)
    assigned_officer: Optional[str] = Field(default=None, max_length=120)


def _now() -> float:
    return time.time()


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


def _ensure_tables() -> None:
    """Create/migrate the local land schema and seed synthetic parcel geometry."""
    with get_db() as db:
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


def _document_for_user(document_id: str, user: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    with get_db() as db:
        row = db.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Document record not found.")
    document = dict(row)
    if user:
        if user.get("role") == ROLE_VIEWER and document.get("status") != "APPROVED":
            raise HTTPException(status_code=403, detail="Viewer access is limited to approved records.")
        if user.get("role") == ROLE_DATA_OFFICER and document.get("uploaded_by") != user.get("email"):
            raise HTTPException(status_code=403, detail="Data Officers can only access their own submissions.")
    document["fields"] = _parse_json(document.get("fields"), {})
    return document


def _document_property_checks(document: Dict[str, Any], property_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    fields = document.get("fields") or {}
    checks: List[Dict[str, Any]] = []
    field_specs = (
        ("survey_number", "Survey number", ("survey_number",)),
        ("gat_number", "Gat number", ("gat_number",)),
        ("khasra_number", "Khasra number", ("khasra_number",)),
        ("sub_division", "Subdivision", ("sub_division", "subdivision")),
        ("village", "Village", ("village",)),
        ("taluka", "Taluka", ("taluka", "tehsil")),
        ("district", "District", ("district",)),
    )
    for key, label, keys in field_specs:
        document_value = _field_value(fields, *keys)
        parcel_value = str(property_data.get(key) or "")
        document_confidence = _field_confidence(fields, *keys)
        parcel_confidence = property_data.get("source_confidence")
        if not document_value or not parcel_value:
            status_value = "INSUFFICIENT EVIDENCE"
        elif _key_for(key, document_value) == _key_for(key, parcel_value):
            status_value = "CONSISTENT"
        else:
            status_value = "CONFLICT"
        checks.append({
            "field": key,
            "label": label,
            "document": document_value or None,
            "parcel": parcel_value or None,
            "status": status_value,
            "source": "Uploaded document → OCR" if document_value else "No extracted document value",
            "confidence": document_confidence if document_confidence is not None else parcel_confidence,
            "document_confidence": document_confidence,
            "parcel_confidence": parcel_confidence,
        })

    document_area = _number(_field_value(fields, "area", "land_area", "plot_area"))
    parcel_area = _number(property_data.get("area"))
    difference = round(abs(document_area - parcel_area), 2) if document_area is not None and parcel_area is not None else None
    area_status = "INSUFFICIENT EVIDENCE"
    if difference is not None:
        area_status = "CONSISTENT" if difference <= AREA_TOLERANCE_HA else "REVIEW REQUIRED"
    checks.append({
        "field": "area",
        "label": "Area",
        "document": document_area,
        "parcel": parcel_area,
        "difference": difference,
        "tolerance": AREA_TOLERANCE_HA,
        "status": area_status,
        "source": "Uploaded document → OCR" if document_area is not None else "No extracted document value",
        "confidence": _field_confidence(fields, "area", "land_area", "plot_area") or property_data.get("source_confidence"),
        "document_confidence": _field_confidence(fields, "area", "land_area", "plot_area"),
        "parcel_confidence": property_data.get("source_confidence"),
    })
    return checks


def compare_document_property(document: Dict[str, Any], property_data: Dict[str, Any]) -> Dict[str, Any]:
    checks = _document_property_checks(document, property_data)
    statuses = {check["status"] for check in checks}
    if "CONFLICT" in statuses:
        overall = "CONFLICT"
    elif "REVIEW REQUIRED" in statuses:
        overall = "REVIEW REQUIRED"
    elif "INSUFFICIENT EVIDENCE" in statuses:
        overall = "INSUFFICIENT EVIDENCE"
    else:
        overall = "CONSISTENT"
    return {
        "document_id": document.get("id"),
        "property_id": property_data.get("property_id"),
        "overall_status": overall,
        "identity_match": "CONFLICT" if "CONFLICT" in statuses else ("CONSISTENT" if overall == "CONSISTENT" else "REVIEW REQUIRED"),
        "area_difference": next((c.get("difference") for c in checks if c["field"] == "area"), None),
        "transfer_risk": "REVIEW REQUIRED" if _is_transfer_document(document) else "LOW",
        "checks": checks,
        "explanation": "Comparison is neutral evidence assistance. It is not a legal ownership, authenticity, or fraud determination.",
    }


def _features(rows: Sequence[Any]) -> List[Dict[str, Any]]:
    return [
        {
            "type": "Feature",
            "id": row["property_id"],
            "geometry": _geometry_from_row(row),
            "properties": {
                "property_id": row["property_id"],
                "parcel_id": row["parcel_id"],
                "district": row["district"],
                "taluka": row["taluka"],
                "village": row["village"],
                "survey_number": row["survey_number"],
                "gat_number": row["gat_number"],
                "khasra_number": row["khasra_number"],
                "sub_division": row["sub_division"],
                "area": row["area"],
                "area_unit": row["area_unit"],
                "geometry_confidence": row["geometry_confidence"],
                "data_source": row["data_source"],
                "authoritative": False,
            },
        }
        for row in rows
    ]


def _filtered_property_rows(
    q: Optional[str] = None,
    district: Optional[str] = None,
    taluka: Optional[str] = None,
    village: Optional[str] = None,
    limit: int = 500,
    offset: int = 0,
) -> Tuple[List[Any], int]:
    with get_db() as db:
        rows = db.execute("SELECT * FROM properties ORDER BY district,village,parcel_id").fetchall()
    needle = _normalise(q)
    filtered = []
    for row in rows:
        values = " ".join(str(row[key] or "") for key in ("property_id", "parcel_id", "survey_number", "gat_number", "khasra_number", "village", "taluka", "district"))
        if needle and needle not in _normalise(values):
            continue
        if district and _normalise(row["district"]) != _normalise(district):
            continue
        if taluka and _normalise(row["taluka"]) != _normalise(taluka):
            continue
        if village and _normalise(row["village"]) != _normalise(village):
            continue
        filtered.append(row)
    total = len(filtered)
    return filtered[max(0, offset): max(0, offset) + max(1, min(limit, 1000))], total


@router.get("/search")
def search_properties(q: str = Query("", max_length=200), limit: int = Query(100, ge=1, le=1000), user: Dict[str, Any] = Depends(get_current_user)):
    rows, total = _filtered_property_rows(q=q, limit=limit)
    return {"results": [_property_dict(row, include_geometry=False) for row in rows], "total": total}


@router.get("/properties")
def list_properties(
    q: Optional[str] = Query(None, max_length=200),
    district: Optional[str] = Query(None, max_length=200),
    taluka: Optional[str] = Query(None, max_length=200),
    village: Optional[str] = Query(None, max_length=200),
    limit: int = Query(500, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    user: Dict[str, Any] = Depends(get_current_user),
):
    rows, total = _filtered_property_rows(q, district, taluka, village, limit, offset)
    return {"properties": [_property_dict(row, include_geometry=False) for row in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/properties/{property_id}")
def property_detail(property_id: str, user: Dict[str, Any] = Depends(get_current_user)):
    property_data = _property_by_id(property_id)
    if not property_data:
        raise HTTPException(status_code=404, detail="Property not found.")
    with get_db() as db:
        provenance_rows = db.execute("SELECT field_name,value,source,confidence,created_at FROM provenance WHERE property_id=? ORDER BY field_name", (property_data["property_id"],)).fetchall()
        timeline_rows = db.execute("SELECT id,event_type,description,source,created_at FROM property_timeline WHERE property_id=? ORDER BY created_at ASC", (property_data["property_id"],)).fetchall()
        linked_rows = db.execute(
            "SELECT d.id,d.filename,d.doc_type,d.status,d.mean_conf,d.fields,d.created_at FROM documents d JOIN property_documents pd ON pd.document_id=d.id WHERE pd.property_id=? ORDER BY d.created_at ASC",
            (property_data["property_id"],),
        ).fetchall()
        neighbour_rows = db.execute(
            "SELECT * FROM properties WHERE property_id<>? AND village=? ORDER BY parcel_id",
            (property_data["property_id"], property_data.get("village")),
        ).fetchall()
    documents = []
    for row in linked_rows:
        item = dict(row)
        item["fields"] = _parse_json(item.get("fields"), {})
        documents.append(item)
    history = _history_for_property(property_data["property_id"])
    property_data.update({
        "provenance": [dict(row) for row in provenance_rows],
        "timeline": [dict(row) for row in timeline_rows],
        "documents": documents,
        "neighbors": [_property_dict(row, include_geometry=False) for row in neighbour_rows],
        "ownership_history": history,
        "findings": history.get("findings", []),
        "tasks": [],
        "legal_authority": False,
    })
    return property_data


@router.get("/geojson")
def property_geojson(
    district: Optional[str] = Query(None, max_length=200),
    taluka: Optional[str] = Query(None, max_length=200),
    village: Optional[str] = Query(None, max_length=200),
    user: Dict[str, Any] = Depends(get_current_user),
):
    rows, _ = _filtered_property_rows(district=district, taluka=taluka, village=village, limit=1000)
    return {
        "type": "FeatureCollection",
        "features": _features(rows),
        "metadata": {
            "label": "Project-owned synthetic/demo land data",
            "authoritative": False,
            "crs": "EPSG:4326",
            "source": "Synthetic/demo dataset or authorized local import",
        },
    }


@router.get("/map/records")
def map_records(
    district: Optional[str] = Query(None, max_length=200),
    taluka: Optional[str] = Query(None, max_length=200),
    village: Optional[str] = Query(None, max_length=200),
    user: Dict[str, Any] = Depends(get_current_user),
):
    rows, _ = _filtered_property_rows(district=district, taluka=taluka, village=village, limit=1000)
    return {
        "records": [
            {
                "property_id": row["property_id"],
                "parcel_id": row["parcel_id"],
                "survey_number": row["survey_number"],
                "district": row["district"],
                "taluka": row["taluka"],
                "village": row["village"],
                "location": _location_from_row(row),
            }
            for row in rows
        ],
        "metadata": {"authoritative": False, "source": "Synthetic/demo dataset or authorized local import"},
    }


@router.get("/geography")
def geography(user: Dict[str, Any] = Depends(get_current_user)):
    rows, _ = _filtered_property_rows(limit=1000)
    tree: Dict[str, Dict[str, Dict[str, Dict[str, Any]]]] = {}
    for row in rows:
        district = row["district"] or "Unknown district"
        taluka = row["taluka"] or "Unknown taluka"
        village = row["village"] or "Unknown village"
        tree.setdefault(district, {}).setdefault(taluka, {}).setdefault(village, {"parcel_count": 0})
        tree[district][taluka][village]["parcel_count"] += 1
    return {"geography": tree, "metadata": {"authoritative": False}}


@router.get("/village-sheet")
def village_sheet(
    district: Optional[str] = Query(None, max_length=200),
    taluka: Optional[str] = Query(None, max_length=200),
    village: Optional[str] = Query(None, max_length=200),
    user: Dict[str, Any] = Depends(get_current_user),
):
    rows, _ = _filtered_property_rows(district=district, taluka=taluka, village=village, limit=1000)
    return {
        "parcels": [_property_dict(row) for row in rows],
        "metadata": {
            "authoritative": False,
            "label": "Project-owned schematic village sheet; not an authoritative cadastral record.",
        },
    }


@router.get("/dashboard")
def land_dashboard(user: Dict[str, Any] = Depends(get_current_user)):
    with get_db() as db:
        total = db.execute("SELECT COUNT(*) AS c FROM properties").fetchone()["c"]
        docs = db.execute("SELECT COUNT(*) AS c FROM property_documents").fetchone()["c"]
        cases = db.execute("SELECT COUNT(*) AS c FROM verification_cases WHERE status NOT IN ('CLOSED','COMPLETED')").fetchone()["c"]
        findings = db.execute("SELECT COUNT(*) AS c FROM verification_findings WHERE status='OPEN'").fetchone()["c"]
    return {
        "total_properties": total,
        "documents_processed": docs,
        "pending_verification": cases,
        "review_required": findings,
        "conflicts": 0,
        "no_parcel_match": 0,
        "low_confidence": 1,
        "completed_cases": 0,
        "data_authoritative": False,
    }


@router.get("/investigate/{property_id}")
def investigate_property(property_id: str, user: Dict[str, Any] = Depends(get_current_user)):
    return property_detail(property_id, user)


@router.get("/resolve/document/{document_id}")
def resolve_document(document_id: str, user: Dict[str, Any] = Depends(get_current_user)):
    document = _document_for_user(document_id, user)
    return {"document_id": document_id, "resolution": _resolve(document.get("fields") or {}), "legal_authority": False}


@router.get("/compare/{document_id}/{property_id}")
def compare_route(document_id: str, property_id: str, user: Dict[str, Any] = Depends(get_current_user)):
    document = _document_for_user(document_id, user)
    property_data = _property_by_id(property_id)
    if not property_data:
        raise HTTPException(status_code=404, detail="Property not found.")
    return compare_document_property(document, property_data)


@router.post("/cases")
def create_case(req: CaseCreate, user: Dict[str, Any] = Depends(require_roles(ROLE_VERIFICATION_OFFICER, ROLE_ADMIN))):
    property_data = _property_by_id(req.property_id)
    if not property_data:
        raise HTTPException(status_code=404, detail="Property not found.")
    case_id = "CASE-" + uuid.uuid4().hex[:10].upper()
    now = _now()
    with get_db() as db:
        db.execute(
            """INSERT INTO verification_cases(case_id,property_id,status,assigned_officer,findings,warnings,comparison_results,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (case_id, property_data["property_id"], "OPEN", req.assigned_officer,
             _json(req.findings), _json(req.warnings), _json(req.comparison_results), now, now),
        )
        for finding in req.findings:
            db.execute(
                """INSERT INTO verification_findings(finding_id,property_id,case_id,finding_type,severity,status,title,evidence,created_by,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                ("FND-" + uuid.uuid4().hex[:10].upper(), property_data["property_id"], case_id,
                 str(finding.get("type") or finding.get("finding_type") or "REVIEW"),
                 str(finding.get("severity") or "WARNING"), "OPEN",
                 str(finding.get("title") or req.title), _json(finding), user.get("full_name", "user"), now, now),
            )
        db.execute(
            "INSERT INTO property_timeline(id,property_id,event_type,description,source,created_at) VALUES (?,?,?,?,?,?)",
            (uuid.uuid4().hex, property_data["property_id"], "VERIFICATION_CASE_CREATED", req.title, "Human verification workflow", now),
        )
    log_audit(user.get("full_name", user.get("email", "user")), "VERIFICATION_CASE_CREATED", f"Created verification case {case_id}.", property_data["property_id"])
    return get_case(case_id, user)


def _case_item(row: Any) -> Dict[str, Any]:
    item = dict(row)
    for key in ("findings", "warnings", "comparison_results"):
        item[key] = _parse_json(item.get(key), [])
    return item


def get_case(case_id: str, user: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    with get_db() as db:
        row = db.execute("SELECT * FROM verification_cases WHERE case_id=?", (case_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Verification case not found.")
        tasks = db.execute("SELECT * FROM verification_tasks WHERE case_id=? ORDER BY created_at", (case_id,)).fetchall()
    item = _case_item(row)
    item["tasks"] = [dict(task) for task in tasks]
    item["legal_authority"] = False
    return item


@router.get("/cases")
def list_cases(status_filter: Optional[str] = Query(None, alias="status"), user: Dict[str, Any] = Depends(get_current_user)):
    with get_db() as db:
        if status_filter:
            rows = db.execute("SELECT * FROM verification_cases WHERE status=? ORDER BY updated_at DESC", (status_filter.upper(),)).fetchall()
        else:
            rows = db.execute("SELECT * FROM verification_cases ORDER BY updated_at DESC").fetchall()
    return {"cases": [_case_item(row) for row in rows], "total": len(rows)}


@router.get("/cases/{case_id}")
def case_detail(case_id: str, user: Dict[str, Any] = Depends(get_current_user)):
    return get_case(case_id, user)


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


@router.put("/properties/{property_id}/location")
@router.post("/properties/{property_id}/location")
def set_property_location(property_id: str, request: LocationUpdate, user: Dict[str, Any] = Depends(get_current_user)):
    property_data = _apply_location(property_id, request, user)
    return {"property": property_data, "location": property_data.get("location")}


@router.delete("/properties/{property_id}/location")
def clear_property_location(property_id: str, user: Dict[str, Any] = Depends(get_current_user)):
    property_data = _apply_location(property_id, LocationUpdate(reason="Cleared by authorized reviewer"), user)
    return {"property": property_data, "location": property_data.get("location")}


@router.post("/geocode")
def geocode(request: GeocodeRequest, user: Dict[str, Any] = Depends(get_current_user)):
    global _last_nominatim_request
    parts = [request.village, request.taluka, request.district, request.state, "India"]
    query = ", ".join(part for part in parts if part)
    cache_key = _normalise(query)
    cached = _geocode_cache.get(cache_key)
    if cached is not None:
        return {**cached, "cached": True}
    with get_db() as db:
        row = db.execute("SELECT * FROM geocode_cache WHERE cache_key=?", (cache_key,)).fetchone()
    if row:
        cached_result = {
            "status": row["status"], "latitude": row["latitude"], "longitude": row["longitude"],
            "display_name": row["display_name"], "query": query,
        }
        _geocode_cache[cache_key] = cached_result
        return {**cached_result, "cached": True}

    elapsed = _now() - _last_nominatim_request
    if _last_nominatim_request and elapsed < 1.0:
        time.sleep(max(0.0, 1.0 - elapsed))
    params = urllib.parse.urlencode({"q": query, "format": "jsonv2", "limit": 1, "countrycodes": "in"})
    request_obj = urllib.request.Request(
        "https://nominatim.openstreetmap.org/search?" + params,
        headers={"User-Agent": NOMINATIM_USER_AGENT, "Accept": "application/json"},
    )
    _last_nominatim_request = _now()
    status_value = "UNRESOLVED"
    latitude = longitude = display_name = None
    try:
        with urllib.request.urlopen(request_obj, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if isinstance(payload, list) and payload:
            latitude = _number(payload[0].get("lat"))
            longitude = _number(payload[0].get("lon"))
            display_name = payload[0].get("display_name")
            if latitude is not None and longitude is not None:
                status_value = "RESOLVED"
    except Exception:
        # A geocoder outage must not result in fabricated coordinates.
        status_value = "UNRESOLVED"
    result = {"status": status_value, "latitude": latitude, "longitude": longitude, "display_name": display_name, "query": query}
    _geocode_cache[cache_key] = result
    with get_db() as db:
        db.execute(
            """INSERT INTO geocode_cache(cache_key,village,taluka,district,state,latitude,longitude,display_name,status,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET latitude=excluded.latitude,longitude=excluded.longitude,
               display_name=excluded.display_name,status=excluded.status,created_at=excluded.created_at""",
            (cache_key, request.village, request.taluka, request.district, request.state, latitude, longitude, display_name, status_value, _now()),
        )
    return {**result, "cached": False}


def _validate_geometry(feature: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    geometry = feature.get("geometry")
    if not isinstance(geometry, dict) or geometry.get("type") not in {"Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon"}:
        raise HTTPException(status_code=422, detail="Every GeoJSON feature must contain a supported geometry.")
    if shapely_shape is not None:
        try:
            geom = shapely_shape(geometry)
            if geom.is_empty or not geom.is_valid:
                raise HTTPException(status_code=422, detail="GeoJSON contains an empty or invalid geometry.")
            geometry = shapely_mapping(geom)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Invalid GeoJSON geometry: {exc}")
    return geometry, feature.get("properties") if isinstance(feature.get("properties"), dict) else {}


@router.post("/import-geojson")
async def import_geojson(file: UploadFile = File(...), user: Dict[str, Any] = Depends(get_current_user)):
    raw = await file.read(MAX_IMPORT_BYTES + 1)
    if len(raw) > MAX_IMPORT_BYTES:
        raise HTTPException(status_code=422, detail="GeoJSON file exceeds the 10 MB import limit.")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=422, detail="GeoJSON upload is not valid UTF-8 JSON.")
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        raise HTTPException(status_code=422, detail="Only GeoJSON FeatureCollection uploads are supported.")
    crs = payload.get("crs")
    if crs:
        name = ((crs.get("properties") or {}).get("name") if isinstance(crs, dict) else "") or ""
        if str(name).upper().replace("::", ":") not in {"EPSG:4326", "URN:OGC:DEF:CRS:OGC:1.3:CRS84", "CRS84"}:
            raise HTTPException(status_code=422, detail="Only EPSG:4326/CRS84 GeoJSON is supported; reproject the file before import.")
    features = payload.get("features")
    if not isinstance(features, list) or len(features) > MAX_IMPORT_FEATURES:
        raise HTTPException(status_code=422, detail="GeoJSON feature count is invalid or exceeds the import limit.")
    prepared = []
    for index, feature in enumerate(features):
        if not isinstance(feature, dict):
            raise HTTPException(status_code=422, detail=f"Feature {index + 1} is not an object.")
        geometry, properties = _validate_geometry(feature)
        parcel_id = str(properties.get("parcel_id") or properties.get("parcelId") or properties.get("property_id") or "").strip()
        if not parcel_id:
            raise HTTPException(status_code=422, detail=f"Feature {index + 1} is missing parcel_id/property_id.")
        prepared.append((parcel_id, geometry, properties))
    if user.get("role") not in {ROLE_VERIFICATION_OFFICER, ROLE_ADMIN}:
        raise HTTPException(status_code=403, detail="Only Verification Officers or Administrators can import parcel data.")

    now = _now()
    with get_db() as db:
        for parcel_id, geometry, properties in prepared:
            property_id = str(properties.get("property_id") or ("PROP-" + re.sub(r"[^A-Za-z0-9_-]", "-", parcel_id))).strip()
            area = _number(properties.get("area"))
            latitude = _number(properties.get("latitude"))
            longitude = _number(properties.get("longitude"))
            if latitude is None or longitude is None:
                try:
                    if shapely_shape is not None:
                        centroid = shapely_shape(geometry).centroid
                        longitude, latitude = centroid.x, centroid.y
                except Exception:
                    latitude = longitude = None
            db.execute(
                """INSERT INTO properties(property_id,parcel_id,district,taluka,village,survey_number,gat_number,khasra_number,
                   sub_division,parent_property_id,area,area_unit,geometry,centroid,latitude,longitude,crs,georeferenced,
                   geometry_source,geometry_confidence,data_source,source_confidence,created_at,updated_at,location_status,
                   location_source,location_confidence,location_base_latitude,location_base_longitude,location_base_source)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(property_id) DO UPDATE SET parcel_id=excluded.parcel_id,district=excluded.district,taluka=excluded.taluka,
                   village=excluded.village,survey_number=excluded.survey_number,gat_number=excluded.gat_number,khasra_number=excluded.khasra_number,
                   sub_division=excluded.sub_division,area=excluded.area,area_unit=excluded.area_unit,geometry=excluded.geometry,
                   centroid=excluded.centroid,latitude=excluded.latitude,longitude=excluded.longitude,crs=excluded.crs,georeferenced=1,
                   geometry_source=excluded.geometry_source,geometry_confidence=excluded.geometry_confidence,data_source=excluded.data_source,
                   source_confidence=excluded.source_confidence,updated_at=excluded.updated_at,location_status='PARCEL_GEOMETRY',
                   location_source=excluded.location_source,location_confidence=excluded.location_confidence,location_base_latitude=excluded.location_base_latitude,
                   location_base_longitude=excluded.location_base_longitude,location_base_source=excluded.location_base_source""",
                (property_id, parcel_id, properties.get("district"), properties.get("taluka") or properties.get("tehsil"), properties.get("village"),
                 properties.get("survey_number"), properties.get("gat_number"), properties.get("khasra_number"), properties.get("sub_division"),
                 properties.get("parent_property_id"), area, properties.get("area_unit") or "ha", _json(geometry),
                 _json([longitude, latitude]) if latitude is not None and longitude is not None else None, latitude, longitude, "EPSG:4326", 1,
                 f"GeoJSON import: {file.filename or 'uploaded file'}", float(properties.get("geometry_confidence") or 0.8),
                 str(properties.get("data_source") or "Authorized local GeoJSON import"), float(properties.get("source_confidence") or 0.8),
                 now, now, "PARCEL_GEOMETRY", f"GeoJSON import: {file.filename or 'uploaded file'}", float(properties.get("source_confidence") or 0.8),
                 latitude, longitude, f"GeoJSON import: {file.filename or 'uploaded file'}"),
            )
    return {"imported": len(prepared), "authoritative": False, "source": file.filename or "uploaded file"}


# Ensure the default application database has the mapping schema when the
# module is loaded. Tests and deployments that swap DB_PATH call this function
# again; all migrations are idempotent.
_ensure_tables()
