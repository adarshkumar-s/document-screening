"""Portfolio-derived document mapping and narrowly scoped compatibility support.

The public mapping surface is the document map and history workflow. A small
internal compatibility layer remains because the canonical OCR/AI/audit code
uses governed property resolution, ownership signals, and exact-location
helpers. Those helpers do not register the retired Land Intelligence routes or
serve the replacement map UI.
"""
from __future__ import annotations

import difflib
import json
import re
import time
import unicodedata
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from fastapi import APIRouter, Depends, HTTPException
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
_map_geocode_cache: Dict[str, Dict[str, Any]] = {}
_map_last_geocode_request = 0.0


def _now() -> float:
    return time.time()


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
        "village": _field_value(fields, "village"),
        "tehsil": _field_value(fields, "tehsil"),
        "district": _field_value(fields, "district"),
        "state": _field_value(fields, "state"),
        "year": _field_value(fields, "khatauni_year", "document_date"),
        "lat": row["lat"] if "lat" in row.keys() else None,
        "lon": row["lon"] if "lon" in row.keys() else None,
        "created_at": row["created_at"],
    }
    # Explicit nulls make the response stable for offline/village-level
    # records and match the reference map contract.
    item["lat"] = float(item["lat"]) if item["lat"] is not None else None
    item["lon"] = float(item["lon"]) if item["lon"] is not None else None
    return item


@map_router.get("/records")
def map_records(user: Dict[str, Any] = Depends(get_current_user)):
    """Return visible document records for the GIS view.

    Exact coordinates are human-set pins. Records without a pin remain useful
    to the client because their village/district fields can be geocoded and
    cached at village level, exactly as in the Portfolio implementation.
    """
    with get_db() as db:
        rows = db.execute("SELECT * FROM documents ORDER BY created_at DESC LIMIT 5000").fetchall()
    records = [_map_document_item(row) for row in rows if _map_document_visible(row, user)]
    return {"records": records, "total": len(records)}


@map_router.put("/records/{doc_id}/location")
def map_set_document_location(
    doc_id: str,
    payload: Dict[str, Any],
    user: Dict[str, Any] = Depends(require_roles(ROLE_VERIFICATION_OFFICER, ROLE_ADMIN)),
):
    """Set or clear an exact document pin with an audit event."""
    raw_lat = payload.get("lat", payload.get("latitude"))
    raw_lon = payload.get("lon", payload.get("longitude"))
    if (raw_lat is None) != (raw_lon is None):
        raise HTTPException(status_code=400, detail="lat and lon must be supplied together (or both be null).")
    with get_db() as db:
        row = db.execute("SELECT id, lat, lon FROM documents WHERE id=?", (doc_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Record not found.")
        previous = (row["lat"], row["lon"])
        if raw_lat is None:
            db.execute("UPDATE documents SET lat=NULL, lon=NULL, updated_at=? WHERE id=?", (_now(), doc_id))
            detail = "Document GIS pin cleared; previous coordinates were (%s, %s)." % previous
            action = "location_cleared"
            result = {"ok": True, "lat": None, "lon": None}
        else:
            try:
                latitude, longitude = float(raw_lat), float(raw_lon)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="lat/lon must be numbers (or null to clear).")
            if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
                raise HTTPException(status_code=400, detail="lat must be -90..90 and lon -180..180.")
            db.execute("UPDATE documents SET lat=?, lon=?, updated_at=? WHERE id=?", (latitude, longitude, _now(), doc_id))
            detail = "Document GIS pin changed from (%s, %s) to (%.6f, %.6f)." % (previous[0], previous[1], latitude, longitude)
            action = "location_set"
            result = {"ok": True, "lat": latitude, "lon": longitude}
    log_audit(user.get("full_name", user.get("email", "user")), action, detail, doc_id)
    return result


@map_router.post("/geocode")
def map_geocode(payload: Dict[str, Any], user: Dict[str, Any] = Depends(get_current_user)):
    """Geocode a free-form village query with a cached Nominatim request."""
    global _map_last_geocode_request
    query = str(payload.get("query") or "").strip()
    if not query or len(query) > 200:
        raise HTTPException(status_code=400, detail="query required (max 200 chars)")
    key = _normalise(query)
    cached = _map_geocode_cache.get(key)
    if cached is not None:
        return {**cached, "cached": True}
    # Reuse the persistent land cache where possible, but do not alter its
    # structured response shape.
    with get_db() as db:
        cached_row = db.execute("SELECT latitude, longitude, display_name, status FROM geocode_cache WHERE cache_key=?", (key,)).fetchone()
    if cached_row:
        cached_result = {
            "query": query,
            "lat": cached_row["latitude"],
            "lon": cached_row["longitude"],
            "display_name": cached_row["display_name"],
        }
        _map_geocode_cache[key] = cached_result
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
                  "display_name": data[0].get("display_name"), "cached": False}
    _map_geocode_cache[key] = {k: v for k, v in result.items() if k != "cached"}
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
    return {
        "id": item["id"], "filename": item["filename"], "doc_type": item["doc_type"],
        "status": item["status"], "owner": item["owner"], "father": item["father"],
        "survey": item["survey"], "khasra": item["khasra"], "khata": item["khata"],
        "area": item["area"], "village": item["village"], "tehsil": item["tehsil"],
        "district": item["district"], "state": item["state"], "year": item["year"],
        "lat": item["lat"], "lon": item["lon"], "created_at": item["created_at"],
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
            "ownership_history": ownership_history}



# Run the compatibility schema migration once at import. The canonical app
# calls this again after test/deployment DB swaps; all operations are idempotent.
_ensure_tables()
