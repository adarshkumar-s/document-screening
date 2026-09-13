"""Land Document Intelligence + GIS Mapping + Verification domain layer.

This module intentionally uses only local/open-source primitives. The parcel dataset
seeded here is synthetic and must never be represented as authoritative cadastral data.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import time
import uuid
import difflib
import re
import unicodedata
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

try:
    from shapely.geometry import shape, mapping, Point
    from shapely.validation import explain_validity
    HAS_SHAPELY = True
except Exception:
    HAS_SHAPELY = False

from server import (
    BASE_DIR, app, get_db, get_current_user, log_audit,
    require_roles, ROLE_ADMIN, ROLE_DATA_OFFICER, ROLE_VERIFICATION_OFFICER,
)

LAND_SCHEMA_VERSION = "1.1"
LOCATION_STATES = {"EXACT_PIN", "VILLAGE_LEVEL", "PARCEL_GEOMETRY", "UNRESOLVED"}
NOMINATIM_CACHE_DAYS = 7
_nominatim_lock = threading.Lock()
_last_nominatim_request = 0.0
MATCH_STATUSES = {"MATCH", "POSSIBLE MATCH", "NO MATCH", "INSUFFICIENT DATA"}
CASE_STATUSES = {"OPEN", "UNDER_REVIEW", "NEEDS_CORRECTION", "VERIFIED", "CLOSED"}

DEMO_PARCELS = [
    {"property_id":"DEMO-PROP-103-A","parcel_id":"DEMO-103-A","district":"Demo District","taluka":"Demo Taluka","village":"Demo Village","survey_number":"DEMO-103","gat_number":None,"khasra_number":None,"sub_division":"A","parent_property_id":"DEMO-PROP-103","area":2.31,"area_unit":"ha","latitude":28.6214,"longitude":77.1045,"geometry_source":"Demo GIS dataset","geometry_confidence":0.94},
    {"property_id":"DEMO-PROP-103-B","parcel_id":"DEMO-103-B","district":"Demo District","taluka":"Demo Taluka","village":"Demo Village","survey_number":"DEMO-103","gat_number":None,"khasra_number":None,"sub_division":"B","parent_property_id":"DEMO-PROP-103","area":2.69,"area_unit":"ha","latitude":28.6219,"longitude":77.1060,"geometry_source":"Demo GIS dataset","geometry_confidence":0.94},
    {"property_id":"DEMO-PROP-104","parcel_id":"DEMO-104","district":"Demo District","taluka":"Demo Taluka","village":"Demo Village","survey_number":"DEMO-104","gat_number":None,"khasra_number":None,"sub_division":None,"parent_property_id":None,"area":3.20,"area_unit":"ha","latitude":28.6201,"longitude":77.1080,"geometry_source":"Demo GIS dataset","geometry_confidence":0.93},
    {"property_id":"DEMO-PROP-105","parcel_id":"DEMO-105","district":"Demo District","taluka":"Demo Taluka","village":"Demo Village","survey_number":"DEMO-105","gat_number":None,"khasra_number":None,"sub_division":None,"parent_property_id":None,"area":1.80,"area_unit":"ha","latitude":28.6240,"longitude":77.1070,"geometry_source":"Demo GIS dataset","geometry_confidence":0.92},
]

DEMO_GEOJSON = {
    "type":"FeatureCollection",
    "name":"Demo Synthetic Cadastral Dataset",
    "metadata":{"source":"Synthetic project-owned demo data","license":"Project demo data","georeferenced":True,"crs":"EPSG:4326"},
    "features":[
        {"type":"Feature","properties":{"property_id":"DEMO-PROP-103-A","parcel_id":"DEMO-103-A","survey_number":"DEMO-103","sub_division":"A","area":2.31},"geometry":{"type":"Polygon","coordinates":[[[77.101,28.6195],[77.1055,28.6195],[77.1055,28.6225],[77.101,28.6225],[77.101,28.6195]]]}},
        {"type":"Feature","properties":{"property_id":"DEMO-PROP-103-B","parcel_id":"DEMO-103-B","survey_number":"DEMO-103","sub_division":"B","area":2.69},"geometry":{"type":"Polygon","coordinates":[[[77.1055,28.6195],[77.109,28.6195],[77.109,28.6225],[77.1055,28.6225],[77.1055,28.6195]]]}},
        {"type":"Feature","properties":{"property_id":"DEMO-PROP-104","parcel_id":"DEMO-104","survey_number":"DEMO-104","area":3.20},"geometry":{"type":"Polygon","coordinates":[[[77.101,28.6225],[77.106,28.6225],[77.106,28.625],[77.101,28.625],[77.101,28.6225]]]}},
        {"type":"Feature","properties":{"property_id":"DEMO-PROP-105","parcel_id":"DEMO-105","survey_number":"DEMO-105","area":1.80},"geometry":{"type":"Polygon","coordinates":[[[77.106,28.6225],[77.110,28.6225],[77.110,28.625],[77.106,28.625],[77.106,28.6225]]]}}
    ]
}


def _now() -> float:
    return time.time()


def _json(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def _field(fields: Dict[str, Any], name: str) -> Optional[str]:
    value = fields.get(name)
    if isinstance(value, dict):
        value = value.get("value")
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _normal(v: Any) -> str:
    return " ".join(str(v or "").strip().lower().split())


def _build_geometry(parcel_id: str) -> Optional[dict]:
    for feature in DEMO_GEOJSON["features"]:
        if feature["properties"]["parcel_id"] == parcel_id:
            return feature["geometry"]
    return None


def _ensure_tables() -> None:
    with get_db() as db:
        statements = [
            """CREATE TABLE IF NOT EXISTS properties (
                property_id TEXT PRIMARY KEY, parcel_id TEXT UNIQUE NOT NULL,
                district TEXT, taluka TEXT, village TEXT, survey_number TEXT,
                gat_number TEXT, khasra_number TEXT, sub_division TEXT,
                parent_property_id TEXT, area REAL, area_unit TEXT, geometry TEXT,
                centroid TEXT, latitude REAL, longitude REAL, crs TEXT,
                georeferenced INTEGER NOT NULL DEFAULT 0, geometry_source TEXT,
                geometry_confidence REAL, data_source TEXT, source_confidence REAL,
                location_status TEXT NOT NULL DEFAULT 'UNRESOLVED', location_source TEXT,
                location_confidence REAL, location_verified_by TEXT, location_verified_at REAL,
                location_updated_at REAL, location_base_latitude REAL, location_base_longitude REAL,
                location_base_source TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS property_documents (
                property_id TEXT NOT NULL, document_id TEXT NOT NULL,
                source_type TEXT NOT NULL DEFAULT 'uploaded_document',
                linked_at REAL NOT NULL, PRIMARY KEY(property_id, document_id)
            )""",
            """CREATE TABLE IF NOT EXISTS verification_cases (
                case_id TEXT PRIMARY KEY, property_id TEXT, status TEXT NOT NULL,
                assigned_officer TEXT, findings TEXT NOT NULL DEFAULT '[]',
                warnings TEXT NOT NULL DEFAULT '[]', comparison_results TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS verification_tasks (
                task_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN', assigned_to TEXT, created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS provenance (
                id TEXT PRIMARY KEY, property_id TEXT NOT NULL, field_name TEXT NOT NULL,
                value TEXT, source TEXT NOT NULL, confidence REAL, created_at REAL NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS property_timeline (
                id TEXT PRIMARY KEY, property_id TEXT NOT NULL, event_type TEXT NOT NULL,
                description TEXT NOT NULL, source TEXT NOT NULL, created_at REAL NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS verification_findings (
                finding_id TEXT PRIMARY KEY, property_id TEXT NOT NULL, case_id TEXT,
                finding_type TEXT NOT NULL, severity TEXT NOT NULL DEFAULT 'REVIEW',
                status TEXT NOT NULL DEFAULT 'OPEN', title TEXT NOT NULL,
                evidence TEXT NOT NULL DEFAULT '{}', created_by TEXT, created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS dataset_sources (
                source_id TEXT PRIMARY KEY, source TEXT NOT NULL, license TEXT,
                crs TEXT, georeferenced INTEGER NOT NULL DEFAULT 0,
                confidence REAL, imported_at REAL NOT NULL
            )""",
        ]
        for stmt in statements:
            db.execute(stmt)
        _ensure_property_columns(db)
        _migrate_location_state(db)

        count = db.execute("SELECT COUNT(*) AS c FROM properties").fetchone()["c"]
        if count == 0:
            for p in DEMO_PARCELS:
                geom = _build_geometry(p["parcel_id"])
                centroid = {"latitude":p["latitude"],"longitude":p["longitude"]}
                db.execute("""INSERT INTO properties
                    (property_id,parcel_id,district,taluka,village,survey_number,gat_number,khasra_number,
                     sub_division,parent_property_id,area,area_unit,geometry,centroid,latitude,longitude,crs,
                     georeferenced,geometry_source,geometry_confidence,data_source,source_confidence,
                     location_status,location_source,location_confidence,location_base_latitude,location_base_longitude,location_base_source,
                     created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (p["property_id"],p["parcel_id"],p["district"],p["taluka"],p["village"],p["survey_number"],
                     p["gat_number"],p["khasra_number"],p["sub_division"],p["parent_property_id"],p["area"],p["area_unit"],
                     _json(geom),_json(centroid),p["latitude"],p["longitude"],"EPSG:4326",1,p["geometry_source"],
                     p["geometry_confidence"],"Synthetic/demo dataset",p["geometry_confidence"],"PARCEL_GEOMETRY",p["geometry_source"],p["geometry_confidence"],p["latitude"],p["longitude"],p["geometry_source"],_now(),_now()))
                for name, value, conf in [("survey_number",p["survey_number"],0.99),("village",p["village"],0.99),("taluka",p["taluka"],0.99),("district",p["district"],0.99),("area",p["area"],p["geometry_confidence"])]:
                    db.execute("INSERT INTO provenance (id,property_id,field_name,value,source,confidence,created_at) VALUES (?,?,?,?,?,?,?)",
                               (uuid.uuid4().hex,p["property_id"],name,str(value),"Synthetic GIS dataset",conf,_now()))
                db.execute("INSERT INTO property_timeline (id,property_id,event_type,description,source,created_at) VALUES (?,?,?,?,?,?)",
                           (uuid.uuid4().hex,p["property_id"],"DEMO_DATASET_CREATED","Synthetic parcel created for demonstration; not authoritative cadastral data.","Project synthetic dataset",_now()))


def _property(row: Any, include_geometry: bool = True) -> Dict[str, Any]:
    d = dict(row)
    d["geometry"] = json.loads(d["geometry"]) if d.get("geometry") and include_geometry else None
    d["centroid"] = json.loads(d["centroid"]) if d.get("centroid") else None
    d["georeferenced"] = bool(d.get("georeferenced"))
    return d


def _resolve(fields: Dict[str, Any]) -> Dict[str, Any]:
    identifier_keys = ("survey_number", "gat_number", "khasra_number", "village", "taluka", "district", "sub_division")
    supplied = {k: _field(fields, k) for k in identifier_keys if _field(fields, k)}
    area_value = _field(fields, "area")
    if not supplied and not area_value:
        return {"status":"INSUFFICIENT DATA","confidence":0,"matches":[],"reasons":["No property identifiers, location fields, or area were extracted."]}

    try:
        area_num = float(str(area_value).replace(",", "").split()[0]) if area_value else None
    except (TypeError, ValueError):
        area_num = None
    tolerance = float(os.getenv("LAND_AREA_TOLERANCE_HA", "0.05"))

    weights = {
        "survey_number": 3, "gat_number": 3, "khasra_number": 3,
        "sub_division": 2, "village": 1, "taluka": 1, "district": 1,
    }
    with get_db() as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM properties").fetchall()]

    scored = []
    for row in rows:
        score = 0.0
        max_score = 0.0
        reasons: List[str] = []
        missing: List[str] = []
        conflicts: List[str] = []
        strong_conflict = False

        for key, value in supplied.items():
            weight = weights[key]
            max_score += weight
            row_value = row.get(key)
            if not row_value:
                missing.append(key)
                continue
            if _normal(row_value) == _normal(value):
                score += weight
                reasons.append(f"{key.replace('_',' ')} matched")
            else:
                conflicts.append(key)
                if key in {"survey_number", "gat_number", "khasra_number"}:
                    strong_conflict = True

        if area_num is not None and row.get("area") is not None:
            max_score += 1
            difference = abs(area_num - float(row["area"]))
            if difference <= tolerance:
                score += 1
                reasons.append(f"area within {tolerance:g} tolerance")
            else:
                conflicts.append("area")
        elif area_num is not None:
            max_score += 1
            missing.append("area")

        if max_score <= 0 or score <= 0:
            continue
        pct = round((score / max_score) * 100)
        scored.append((pct, row, reasons, missing, conflicts, strong_conflict))

    scored.sort(key=lambda x: (x[0], len(x[2])), reverse=True)
    if not scored:
        return {"status":"NO MATCH","confidence":0,"matches":[],"reasons":["No parcel matched the supplied property identity or location fields."]}

    matches = []
    for pct, row, reasons, missing, conflicts, strong_conflict in scored[:5]:
        if pct >= 50:
            matches.append({
                "property": _property(row, False),
                "confidence": pct / 100,
                "reasons": reasons,
                "missing_fields": missing,
                "conflicting_fields": conflicts,
            })

    top_pct, _, top_reasons, top_missing, top_conflicts, top_strong_conflict = scored[0]
    if top_pct >= 95 and not top_strong_conflict and not top_conflicts:
        status = "MATCH"
    elif top_pct >= 50 and not top_strong_conflict:
        status = "POSSIBLE MATCH"
    else:
        status = "NO MATCH"

    return {
        "status": status,
        "confidence": top_pct / 100,
        "matches": matches,
        "reasons": top_reasons,
        "missing_fields": top_missing,
        "conflicting_fields": top_conflicts,
    }

class LocationUpdate(BaseModel):
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    reason: str = Field(default="", max_length=500)
    expected_location_updated_at: Optional[float] = None


class GeocodeRequest(BaseModel):
    village: Optional[str] = Field(default=None, max_length=160)
    taluka: Optional[str] = Field(default=None, max_length=160)
    district: Optional[str] = Field(default=None, max_length=160)
    property_id: Optional[str] = Field(default=None, max_length=160)


def _column_exists(db: Any, table: str, column: str) -> bool:
    if getattr(db, "is_pg", False):
        return bool(db.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name=? AND column_name=?",
            (table, column),
        ).fetchone())
    return any(str(r["name"]) == column for r in db.execute(f"PRAGMA table_info({table})").fetchall())


def _ensure_property_columns(db: Any) -> None:
    columns = {
        "location_status": "TEXT NOT NULL DEFAULT 'UNRESOLVED'",
        "location_source": "TEXT",
        "location_confidence": "REAL",
        "location_verified_by": "TEXT",
        "location_verified_at": "REAL",
        "location_updated_at": "REAL",
        "location_base_latitude": "REAL",
        "location_base_longitude": "REAL",
        "location_base_source": "TEXT",
    }
    for name, definition in columns.items():
        if not _column_exists(db, "properties", name):
            db.execute(f"ALTER TABLE properties ADD COLUMN {name} {definition}")


def _migrate_location_state(db: Any) -> None:
    db.execute("""UPDATE properties SET
        location_status='PARCEL_GEOMETRY',
        location_source=COALESCE(NULLIF(location_source,''), geometry_source, 'Parcel geometry'),
        location_confidence=COALESCE(location_confidence, geometry_confidence),
        location_base_latitude=COALESCE(location_base_latitude, latitude),
        location_base_longitude=COALESCE(location_base_longitude, longitude),
        location_base_source=COALESCE(NULLIF(location_base_source,''), geometry_source, 'Parcel geometry'),
        location_updated_at=COALESCE(location_updated_at, updated_at)
        WHERE geometry IS NOT NULL AND TRIM(geometry) <> '' AND latitude IS NOT NULL AND longitude IS NOT NULL
          AND (location_status IS NULL OR location_status='UNRESOLVED')""")
    db.execute("""UPDATE properties SET
        location_status='VILLAGE_LEVEL',
        location_source=COALESCE(NULLIF(location_source,''), 'Existing approximate location'),
        location_confidence=COALESCE(location_confidence, 0.5),
        location_base_latitude=COALESCE(location_base_latitude, latitude),
        location_base_longitude=COALESCE(location_base_longitude, longitude),
        location_base_source=COALESCE(NULLIF(location_base_source,''), 'Existing approximate location'),
        location_updated_at=COALESCE(location_updated_at, updated_at)
        WHERE geometry IS NULL AND latitude IS NOT NULL AND longitude IS NOT NULL
          AND (village IS NOT NULL OR district IS NOT NULL)
          AND (location_status IS NULL OR location_status='UNRESOLVED')""")


class CaseCreate(BaseModel):
    property_id: Optional[str] = None
    assigned_officer: Optional[str] = None
    findings: List[dict] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


_INDIC_DIGITS = str.maketrans({
    "٠":"0","١":"1","٢":"2","٣":"3","٤":"4","٥":"5","٦":"6","٧":"7","٨":"8","٩":"9",
    "۰":"0","۱":"1","۲":"2","۳":"3","۴":"4","۵":"5","۶":"6","۷":"7","۸":"8","۹":"9",
    "०":"0","१":"1","२":"2","३":"3","४":"4","५":"5","६":"6","७":"7","८":"8","९":"9",
})


def _clean_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    return " ".join(text.strip().split())


def _land_number(value: Any) -> str:
    return _clean_text(value).translate(_INDIC_DIGITS).lower()


def _owner_similarity(a: Any, b: Any) -> float:
    left = re.sub(r"[^a-z0-9\u0900-\u097f]+", " ", _clean_text(a).translate(_INDIC_DIGITS).lower()).strip()
    right = re.sub(r"[^a-z0-9\u0900-\u097f]+", " ", _clean_text(b).translate(_INDIC_DIGITS).lower()).strip()
    if not left or not right:
        return 0.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def _field_value(fields: Dict[str, Any], key: str) -> str:
    raw = fields.get(key, "")
    if isinstance(raw, dict):
        raw = raw.get("value", "")
    return _clean_text(raw)


def _record_year(record: Dict[str, Any]) -> Optional[int]:
    value = _field_value(record.get("fields", {}), "khatauni_year")
    if not value:
        value = _field_value(record.get("fields", {}), "document_date")
    years = [int(x) for x in re.findall(r"(?:19|20)\d{2}", value)]
    return min(years) if years else None


def _is_transfer_document(record: Dict[str, Any]) -> bool:
    doc_type = _clean_text(record.get("doc_type")).lower()
    text = _clean_text(record.get("ocr_text")).lower()
    return any(term in doc_type or term in text for term in (
        "mutation", "namantaran", "ferfar", "sale deed", "sale", "transfer", "registered deed"
    ))


def _same_land_identity(a: Dict[str, Any], b: Dict[str, Any]) -> Tuple[bool, List[str], List[str]]:
    af, bf = a.get("fields", {}), b.get("fields", {})
    compared = ("survey_number", "gat_number", "khasra_number", "village", "tehsil", "district")
    matches, differences = [], []
    for key in compared:
        av, bv = _field_value(af, key), _field_value(bf, key)
        if not av or not bv:
            continue
        if _land_number(av) == _land_number(bv):
            matches.append(key)
        else:
            differences.append(key)
    if _field_value(af, "village") and _field_value(bf, "village") and _land_number(_field_value(af, "village")) != _land_number(_field_value(bf, "village")):
        return False, matches, differences
    strong = any(k in matches for k in ("survey_number", "gat_number", "khasra_number"))
    return strong and bool(matches), matches, differences


def analyze_ownership_history(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    ordered = sorted(records or [], key=lambda r: (_record_year(r) or 9999, str(r.get("id") or "")))
    events, relationships, findings = [], [], []
    for record in ordered:
        fields = record.get("fields", {})
        events.append({
            "document_id": record.get("id"),
            "filename": record.get("filename"),
            "year": _record_year(record),
            "owner": _field_value(fields, "owner_name"),
            "survey_number": _field_value(fields, "survey_number"),
            "khasra_number": _field_value(fields, "khasra_number"),
            "village": _field_value(fields, "village"),
            "area": _field_value(fields, "area"),
            "document_type": record.get("doc_type") or "Land Record",
            "verification_status": record.get("status"),
        })

    transfer_docs = [r for r in ordered if _is_transfer_document(r)]
    for i, current in enumerate(ordered):
        for previous in reversed(ordered[:i]):
            same_land, matched_fields, differing_fields = _same_land_identity(previous, current)
            if not same_land:
                continue
            prev_owner = _field_value(previous.get("fields", {}), "owner_name")
            curr_owner = _field_value(current.get("fields", {}), "owner_name")
            if not prev_owner or not curr_owner:
                continue
            similarity = _owner_similarity(prev_owner, curr_owner)
            prev_year, curr_year = _record_year(previous), _record_year(current)
            area_changed = _land_number(_field_value(previous.get("fields", {}), "area")) != _land_number(_field_value(current.get("fields", {}), "area"))
            khasra_changed = _land_number(_field_value(previous.get("fields", {}), "khasra_number")) != _land_number(_field_value(current.get("fields", {}), "khasra_number"))
            same_owner = _land_number(prev_owner) == _land_number(curr_owner)
            if same_owner and (khasra_changed or area_changed) and prev_year and curr_year and prev_year != curr_year:
                classification, title, severity = "PARTITION_CANDIDATE", "Possible partition / subdivision", "WARNING"
                reason = "The same owner and parent land identity are retained while the khasra/sub-khasra or area changes across record years; this may represent a partition or subdivision rather than a duplicate."
            elif same_owner:
                continue
            elif similarity >= 0.80:
                classification, title, severity = "POSSIBLE_DUPLICATE_OCR_VARIATION", "Possible duplicate / OCR owner-name variation", "WARNING"
                reason = "Land identity matches and owner names are only slightly different; this is treated conservatively as a possible OCR variation, not a transfer."
            elif transfer_docs:
                classification, title, severity = "TRANSFER_SUPPORTED", "Ownership transfer supported by mutation/sale evidence", "INFO"
                reason = "Parcel identity is consistent, ownership changed, and a mutation/sale/transfer document is present. Human verification is still required."
            elif prev_year and curr_year and prev_year != curr_year:
                classification, title, severity = "TRANSFER_CANDIDATE", "Possible ownership transfer", "WARNING"
                reason = "Parcel identity is consistent and ownership changed across different record years, but supporting mutation/sale evidence was not found."
            else:
                classification, title, severity = "OWNERSHIP_CONFLICT", "Potential same-period ownership conflict", "ERROR"
                reason = "The same land identity has different owners in the same or unknown period without supporting transfer evidence."
            evidence = [{"kind":"FACT","field":f,"message":f"{f.replace('_',' ').title()} matched."} for f in matched_fields]
            evidence += [
                {"kind":"OBSERVATION","field":"owner_name","previous":prev_owner,"current":curr_owner,"similarity":round(similarity,3)},
                {"kind":"OBSERVATION","field":"record_year","previous":prev_year,"current":curr_year},
                {"kind":"INFERENCE","classification":classification,"reason":reason},
            ]
            if transfer_docs:
                evidence.append({"kind":"FACT","field":"supporting_transfer_document","document_ids":[r.get("id") for r in transfer_docs]})
            relationships.append({
                "from_document": previous.get("id"), "to_document": current.get("id"),
                "relationship_type": "TRANSFER_EVIDENCE" if classification == "TRANSFER_SUPPORTED" else "POSSIBLE_PREDECESSOR",
                "matched_fields": matched_fields, "differing_fields": differing_fields,
                "confidence": round(min(0.99, 0.55 + 0.08*len(matched_fields) + (0.2 if transfer_docs else 0.1 if prev_year and curr_year and prev_year != curr_year else 0)),3),
                "reasoning": reason, "human_verified": False, "evidence": evidence,
            })
            findings.append({
                "type":classification,"severity":severity,"title":title,"reason":reason,
                "evidence":evidence,"from_document":previous.get("id"),"to_document":current.get("id"),
                "human_action":"Verification required",
            })
            break
    return {"events":events,"relationships":relationships,"findings":findings,
            "assessment":"REVIEW_REQUIRED" if findings else "NO_OWNERSHIP_CHANGE_DETECTED",
            "legal_authority":False}


def _history_for_property(property_id: str) -> Dict[str, Any]:
    with get_db() as db:
        rows = db.execute("""SELECT d.* FROM property_documents pd
                             JOIN documents d ON d.id=pd.document_id
                             WHERE pd.property_id=? ORDER BY d.created_at ASC""",(property_id,)).fetchall()
    records=[{**dict(r),"fields":json.loads(r["fields"] or "{}")} for r in rows]
    return analyze_ownership_history(records)


def _location_state(row: Any) -> Dict[str, Any]:
    d = dict(row)
    state = d.get("location_status") or "UNRESOLVED"
    if state not in LOCATION_STATES:
        state = "UNRESOLVED"
    return {
        "status": state, "source": d.get("location_source"),
        "confidence": d.get("location_confidence"),
        "verified_by": d.get("location_verified_by"), "verified_at": d.get("location_verified_at"),
        "updated_at": d.get("location_updated_at"), "latitude": d.get("latitude"),
        "longitude": d.get("longitude"), "base_latitude": d.get("location_base_latitude"),
        "base_longitude": d.get("location_base_longitude"), "base_source": d.get("location_base_source"),
        "approximate": state == "VILLAGE_LEVEL", "exact": state == "EXACT_PIN",
    }


def _nominatim_query(village: Optional[str], taluka: Optional[str], district: Optional[str]) -> str:
    parts = [x.strip() for x in (village, taluka, district) if x and x.strip()]
    if not parts:
        raise HTTPException(422, "Village or district is required for geocoding.")
    return ", ".join(parts + ["India"])


def _geocode_village(village: Optional[str], taluka: Optional[str], district: Optional[str]) -> Dict[str, Any]:
    query = _nominatim_query(village, taluka, district)
    with get_db() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS geocode_cache (
            query TEXT PRIMARY KEY, latitude REAL, longitude REAL,
            display_name TEXT, fetched_at REAL NOT NULL
        )""")
        cached = db.execute("SELECT * FROM geocode_cache WHERE query=?", (query,)).fetchone()
    if cached and _now() - float(cached["fetched_at"]) < NOMINATIM_CACHE_DAYS * 86400:
        return {"status": "RESOLVED" if cached["latitude"] is not None else "UNRESOLVED",
                "latitude": cached["latitude"], "longitude": cached["longitude"],
                "display_name": cached["display_name"], "cached": True, "query": query}
    global _last_nominatim_request
    with _nominatim_lock:
        wait = 1.0 - (_now() - _last_nominatim_request)
        if wait > 0:
            time.sleep(wait)
        url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({
            "q": query, "format": "jsonv2", "limit": 1, "addressdetails": 1,
        })
        request = urllib.request.Request(url, headers={
            "User-Agent": os.getenv("NOMINATIM_USER_AGENT", "DILRMP-Land-Intelligence/1.0"),
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            _last_nominatim_request = _now()
            raise HTTPException(502, "Village geocoding service is temporarily unavailable.") from exc
        _last_nominatim_request = _now()
    result = payload[0] if payload else None
    lat = lon = display_name = None
    if result:
        try:
            lat, lon = float(result["lat"]), float(result["lon"])
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError
            display_name = str(result.get("display_name") or "")[:500]
        except (KeyError, TypeError, ValueError):
            lat = lon = display_name = None
    with get_db() as db:
        db.execute("INSERT OR REPLACE INTO geocode_cache(query,latitude,longitude,display_name,fetched_at) VALUES (?,?,?,?,?)",
                   (query, lat, lon, display_name, _now()))
    return {"status": "RESOLVED" if lat is not None else "UNRESOLVED",
            "latitude": lat, "longitude": lon, "display_name": display_name,
            "cached": False, "query": query}


def _apply_village_location(property_id: str, result: Dict[str, Any], actor: dict) -> Dict[str, Any]:
    if result["status"] != "RESOLVED":
        return result
    now = _now()
    with get_db() as db:
        row = db.execute("SELECT * FROM properties WHERE property_id=? OR parcel_id=?", (property_id, property_id)).fetchone()
        if not row:
            raise HTTPException(404, "Property not found")
        db.execute("""UPDATE properties SET latitude=?,longitude=?,location_status='VILLAGE_LEVEL',
                      location_source='OpenStreetMap Nominatim',location_confidence=0.5,
                      location_verified_by=NULL,location_verified_at=NULL,
                      location_base_latitude=?,location_base_longitude=?,location_base_source='OpenStreetMap Nominatim',
                      location_updated_at=?,updated_at=? WHERE property_id=?""",
                   (result["latitude"],result["longitude"],result["latitude"],result["longitude"],now,now,row["property_id"]))
    _record_timeline(row["property_id"], "LOCATION_GEOCODED",
                     "Village-level approximate location resolved from OpenStreetMap Nominatim (" + (result["display_name"] or result["query"]) + ").",
                     "OpenStreetMap Nominatim", now)
    log_audit(actor["full_name"], "LOCATION_GEOCODED",
              "Stored village-level approximate location for " + row["property_id"] + "; exact parcel location not established.",
              row["property_id"])
    return {**result, "property_id": row["property_id"], "location_status": "VILLAGE_LEVEL"}


@router.post("/geocode")
def geocode_location(req: GeocodeRequest, user: dict = Depends(get_current_user)):
    if req.property_id:
        if user["role"] not in {ROLE_VERIFICATION_OFFICER, ROLE_ADMIN}:
            raise HTTPException(403, "Only Verification Officers or Administrators may store a property location.")
        with get_db() as db:
            row = db.execute("SELECT * FROM properties WHERE property_id=? OR parcel_id=?", (req.property_id, req.property_id)).fetchone()
        if not row:
            raise HTTPException(404, "Property not found")
        village, taluka, district = req.village or row["village"], req.taluka or row["taluka"], req.district or row["district"]
    else:
        village, taluka, district = req.village, req.taluka, req.district
    result = _geocode_village(village, taluka, district)
    if req.property_id and result["status"] == "RESOLVED":
        result = _apply_village_location(req.property_id, result, user)
    return result


@router.get("/map/records")
def map_records(district: Optional[str]=None, taluka: Optional[str]=None, village: Optional[str]=None,
                q: Optional[str]=None, limit: int=250, user: dict=Depends(get_current_user)):
    limit = max(1, min(limit, 250))
    clauses, params = [], []
    for col, val in (("district",district),("taluka",taluka),("village",village)):
        if val:
            clauses.append("LOWER(" + col + ")=LOWER(?)"); params.append(val)
    if q:
        like=f"%{q.strip()}%"
        clauses.append("""(property_id LIKE ? OR parcel_id LIKE ? OR survey_number LIKE ? OR gat_number LIKE ?
                           OR khasra_number LIKE ? OR village LIKE ? OR taluka LIKE ? OR district LIKE ?)""")
        params.extend([like]*8)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_db() as db:
        rows = db.execute("SELECT * FROM properties" + where + " ORDER BY district,taluka,village,parcel_id LIMIT ?", tuple(params+[limit])).fetchall()
    return {"records":[_property(r, True) | {"location":_location_state(r)} for r in rows],
            "metadata":{"authoritative":False,"location_semantics":"EXACT_PIN is human-set; VILLAGE_LEVEL is approximate; PARCEL_GEOMETRY is dataset geometry; UNRESOLVED has no usable location."}}


@router.get("/geography")
def geography(user: dict=Depends(get_current_user)):
    with get_db() as db:
        rows=db.execute("SELECT district,taluka,village,COUNT(*) AS parcel_count FROM properties GROUP BY district,taluka,village ORDER BY district,taluka,village").fetchall()
    tree: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for r in rows:
        district, taluka, village = r["district"] or "", r["taluka"] or "", r["village"] or ""
        tree.setdefault(district, {}).setdefault(taluka, {})[village] = {"parcel_count": r["parcel_count"]}
    return {"geography": tree, "synthetic": True}


@router.get("/village-sheet")
def village_sheet(district: Optional[str]=None, taluka: Optional[str]=None, village: Optional[str]=None,
                  user: dict=Depends(get_current_user)):
    with get_db() as db:
        clauses, params = [], []
        for col, val in (("district",district),("taluka",taluka),("village",village)):
            if val:
                clauses.append("LOWER(" + col + ")=LOWER(?)"); params.append(val)
        where=(" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows=db.execute("SELECT property_id,parcel_id,survey_number,khasra_number,sub_division,parent_property_id,area,area_unit,geometry FROM properties" + where + " ORDER BY parcel_id",tuple(params)).fetchall()
    parcels=[]
    for r in rows:
        d=dict(r); d["geometry"]=json.loads(d["geometry"]) if d.get("geometry") else None; parcels.append(d)
    return {"parcels":parcels,"metadata":{"authoritative":False,"label":"Village Sheet / schematic parcel view","source":"Project-owned geometry; not an authoritative cadastral map."}}


@router.put("/properties/{property_id}/location")
def update_property_location(property_id: str, req: LocationUpdate,
                              user: dict=Depends(require_roles(ROLE_VERIFICATION_OFFICER, ROLE_ADMIN))):
    if (req.latitude is None) != (req.longitude is None):
        raise HTTPException(422, "Latitude and longitude must be supplied together, or both omitted to clear the exact pin.")
    with get_db() as db:
        row=db.execute("SELECT * FROM properties WHERE property_id=? OR parcel_id=?", (property_id, property_id)).fetchone()
        if not row:
            raise HTTPException(404, "Property not found")
        if req.expected_location_updated_at is not None:
            actual=row["location_updated_at"]
            if actual is None or abs(float(actual)-float(req.expected_location_updated_at)) > 1e-9:
                raise HTTPException(409, "Property location changed since this edit was prepared.")
        now=_now()
        if req.latitude is not None:
            if row["location_base_latitude"] is None and row["latitude"] is not None:
                db.execute("UPDATE properties SET location_base_latitude=?,location_base_longitude=?,location_base_source=? WHERE property_id=?",
                           (row["latitude"],row["longitude"],row["location_source"] or "Existing location",row["property_id"]))
            db.execute("""UPDATE properties SET latitude=?,longitude=?,location_status='EXACT_PIN',
                          location_source='Human-verified exact pin',location_confidence=1.0,
                          location_verified_by=?,location_verified_at=?,location_updated_at=?,updated_at=?
                          WHERE property_id=?""",
                       (req.latitude,req.longitude,user["email"],now,now,now,row["property_id"]))
            event="LOCATION_PIN_SET"
            detail="Exact pin changed for " + row["property_id"] + " from (" + str(row["latitude"]) + "," + str(row["longitude"]) + ") to (" + str(req.latitude) + "," + str(req.longitude) + ")."
        else:
            base_lat, base_lon = row["location_base_latitude"], row["location_base_longitude"]
            if row["geometry"]:
                state, source, conf = "PARCEL_GEOMETRY", row["geometry_source"] or "Parcel geometry", row["geometry_confidence"]
            elif base_lat is not None and base_lon is not None:
                state, source, conf = "VILLAGE_LEVEL", row["location_base_source"] or "Approximate location", 0.5
            else:
                state, source, conf = "UNRESOLVED", "No resolved location", None
            db.execute("""UPDATE properties SET latitude=?,longitude=?,location_status=?,location_source=?,
                          location_confidence=?,location_verified_by=NULL,location_verified_at=NULL,
                          location_updated_at=?,updated_at=? WHERE property_id=?""",
                       (base_lat,base_lon,state,source,conf,now,now,row["property_id"]))
            event="LOCATION_PIN_CLEARED"
            detail="Exact pin cleared for " + row["property_id"] + "; location restored to " + state + "."
    _record_timeline(row["property_id"], event, detail + ((" Reason: " + req.reason.strip()) if req.reason.strip() else ""), "Location verification workflow", now)
    log_audit(user["full_name"], event, detail + ((" Reason: " + req.reason.strip()) if req.reason.strip() else ""), row["property_id"])
    with get_db() as db:
        updated=db.execute("SELECT * FROM properties WHERE property_id=?", (row["property_id"],)).fetchone()
    return {"property":_property(updated, True),"location":_location_state(updated)}



router = APIRouter(prefix="/api/land", tags=["Land Intelligence"])

@router.get("/health")
def health():
    return {"status":"ok","schema_version":LAND_SCHEMA_VERSION,"demo_data":True,"shapely":HAS_SHAPELY}

@router.get("/geojson")
def parcel_geojson(limit: int = 250):
    limit=max(1,min(limit,250))
    with get_db() as db:
        rows=db.execute("SELECT * FROM properties ORDER BY parcel_id LIMIT ?",(limit,)).fetchall()
    features=[]
    for row in rows:
        p=_property(row,True)
        props={k:p.get(k) for k in ("property_id","parcel_id","district","taluka","village","survey_number","gat_number","khasra_number","sub_division","area","area_unit","geometry_source","geometry_confidence","data_source","source_confidence")}
        features.append({"type":"Feature","properties":props,"geometry":p["geometry"]})
    return {"type":"FeatureCollection","features":features,"metadata":{"label":"Demo / Synthetic Land Data","crs":"EPSG:4326","authoritative":False}}

@router.get("/properties")
def properties(q: Optional[str]=None, district: Optional[str]=None, village: Optional[str]=None, taluka: Optional[str]=None, survey_number: Optional[str]=None, gat_number: Optional[str]=None, khasra_number: Optional[str]=None, owner: Optional[str]=None, limit: int=100, offset: int=0, user: dict=Depends(get_current_user)):
    limit=max(1,min(limit,250)); offset=max(0,offset)
    clauses=[]; params=[]
    if q:
        like=f"%{q.strip()}%"; clauses.append("(property_id LIKE ? OR parcel_id LIKE ? OR survey_number LIKE ? OR gat_number LIKE ? OR khasra_number LIKE ? OR village LIKE ?)"); params += [like]*6
    for col,val in (("district",district),("village",village),("taluka",taluka),("survey_number",survey_number),("gat_number",gat_number),("khasra_number",khasra_number)):
        if val: clauses.append("LOWER(" + col + ")=LOWER(?)"); params.append(val)
    if owner:
        clauses.append("""EXISTS (
            SELECT 1 FROM property_documents pd JOIN documents d ON d.id=pd.document_id
            WHERE pd.property_id=properties.property_id AND LOWER(d.fields) LIKE LOWER(?)
        )""")
        params.append("%" + owner.strip() + "%")
    where=(" WHERE "+" AND ".join(clauses)) if clauses else ""
    with get_db() as db:
        rows=db.execute(f"SELECT * FROM properties{where} ORDER BY parcel_id LIMIT ? OFFSET ?",tuple(params+[limit,offset])).fetchall()
        total=db.execute(f"SELECT COUNT(*) AS c FROM properties{where}",tuple(params)).fetchone()["c"]
    return {"properties":[_property(r,False) for r in rows],"total":total,"limit":limit,"offset":offset}

@router.get("/properties/{property_id}")
def property_detail(property_id: str, user: dict=Depends(get_current_user)):
    with get_db() as db:
        row=db.execute("SELECT * FROM properties WHERE property_id=? OR parcel_id=?",(property_id,property_id)).fetchone()
        if not row: raise HTTPException(404,"Property not found")
        p=_property(row,True)
        docs=db.execute("""SELECT d.id,d.filename,d.doc_type,d.mean_conf,d.status,d.created_at
                          FROM property_documents pd JOIN documents d ON d.id=pd.document_id
                          WHERE pd.property_id=? ORDER BY d.created_at DESC""",(p["property_id"],)).fetchall()
        neighbors=[]
        if HAS_SHAPELY and p["geometry"]:
            geom=shape(p["geometry"])
            for nr in db.execute("SELECT * FROM properties WHERE property_id!=?",(p["property_id"],)).fetchall():
                ng=shape(json.loads(nr["geometry"])) if nr["geometry"] else None
                if ng and (geom.touches(ng) or geom.distance(ng)<0.0001): neighbors.append(_property(nr,False))
        prov=db.execute("SELECT field_name,value,source,confidence,created_at FROM provenance WHERE property_id=? ORDER BY created_at DESC",(p["property_id"],)).fetchall()
        timeline=db.execute("SELECT event_type,description,source,created_at FROM property_timeline WHERE property_id=? ORDER BY created_at ASC",(p["property_id"],)).fetchall()
    p["documents"] = [dict(d) for d in docs]
    p["neighbors"] = neighbors
    p["provenance"] = [dict(x) for x in prov]
    p["timeline"] = [dict(x) for x in timeline]
    p["ownership_history"] = _history_for_property(p["property_id"])
    return p


@router.get("/properties/{property_id}/history")
def property_ownership_history(property_id: str, user: dict = Depends(get_current_user)):
    with get_db() as db:
        row = db.execute("SELECT property_id FROM properties WHERE property_id=? OR parcel_id=?", (property_id, property_id)).fetchone()
        if not row:
            raise HTTPException(404, "Property not found")
    return _history_for_property(row["property_id"])


@router.get("/resolve/document/{doc_id}")
def resolve_document(doc_id: str, user: dict=Depends(get_current_user)):
    with get_db() as db:
        row=db.execute("SELECT * FROM documents WHERE id=?",(doc_id,)).fetchone()
        if not row: raise HTTPException(404,"Document not found")
        if user["role"]==ROLE_DATA_OFFICER and row["uploaded_by"]!=user["email"]: raise HTTPException(403,"Access denied")
        fields=json.loads(row["fields"] or "{}")
    result=_resolve(fields)
    if result["status"] in ("MATCH","POSSIBLE MATCH") and result["matches"]:
        prop_id=result["matches"][0]["property"]["property_id"]
        with get_db() as db:
            db.execute("INSERT OR IGNORE INTO property_documents(property_id,document_id,source_type,linked_at) VALUES (?,?,?,?)",(prop_id,doc_id,"uploaded_document",_now()))
            db.execute("INSERT INTO property_timeline(id,property_id,event_type,description,source,created_at) VALUES (?,?,?,?,?,?)",(uuid.uuid4().hex,prop_id,"PROPERTY_MATCHED",f"Document {doc_id} resolved to {prop_id} with {round(result['confidence']*100)}% confidence.","Property resolution service",_now()))
        log_audit(user["full_name"],"PROPERTY_RESOLVED",f"Resolved document #{doc_id} to {prop_id}: {result['status']}",doc_id)
        result["property_id"]=prop_id
    return result

@router.get("/compare/{doc_id}/{property_id}")
def compare_document_property(doc_id: str, property_id: str, user: dict=Depends(get_current_user)):
    with get_db() as db:
        d=db.execute("SELECT * FROM documents WHERE id=?",(doc_id,)).fetchone()
        p=db.execute("SELECT * FROM properties WHERE property_id=? OR parcel_id=?",(property_id,property_id)).fetchone()
        if not d or not p: raise HTTPException(404,"Document or property not found")
        if user["role"]==ROLE_DATA_OFFICER and d["uploaded_by"]!=user["email"]: raise HTTPException(403,"Access denied")
    fields=json.loads(d["fields"] or "{}"); prop=_property(p,False)

    def field_meta(name: str):
        raw=fields.get(name)
        if isinstance(raw, dict):
            return raw.get("value"), float(raw.get("confidence") or 0)
        return raw, 0.0

    def add_check(field: str, label: str, document_value: Any, property_value: Any, document_conf: float):
        parcel_conf=float(prop.get("source_confidence") or 0)
        parcel_source=prop.get("data_source") or prop.get("geometry_source") or "Parcel dataset"
        if not document_value or property_value is None or property_value == "":
            status_value="INSUFFICIENT EVIDENCE"
        else:
            status_value="CONSISTENT" if _normal(document_value)==_normal(property_value) else "CONFLICT"
        return {
            "field":field,"label":label,"document":document_value,"parcel":property_value,
            "status":status_value,"source":f"OCR / {prop.get('data_source') or 'parcel dataset'}",
            "document_source":"OCR-derived document field","parcel_source":parcel_source,
            "confidence":{"document":document_conf,"parcel":parcel_conf},
        }

    checks=[]
    for key,label in [
        ("district","District"),("taluka","Taluka"),("village","Village"),
        ("survey_number","Survey number"),("gat_number","Gat number"),
        ("khasra_number","Khasra number"),("sub_division","Subdivision")
    ]:
        dv,dc=field_meta(key)
        checks.append(add_check(key,label,dv,prop.get(key),dc))

    area_doc,area_conf=field_meta("area")
    area_num=None
    if area_doc:
        import re
        m=re.search(r"[0-9]+(?:\.[0-9]+)?",str(area_doc).replace(",",""))
        area_num=float(m.group()) if m else None
    if area_num is None or prop.get("area") is None:
        checks.append({
            "field":"area","label":"Area","document":area_num,"parcel":prop.get("area"),
            "status":"INSUFFICIENT EVIDENCE","source":"OCR-derived document field / parcel dataset",
            "document_source":"OCR-derived document field","parcel_source":prop.get("data_source") or "Parcel dataset",
            "confidence":{"document":area_conf,"parcel":float(prop.get("source_confidence") or 0)},
        })
    else:
        area_diff=round(abs(area_num-float(prop["area"])),4)
        tol=float(os.getenv("LAND_AREA_TOLERANCE_HA","0.05"))
        checks.append({
            "field":"area","label":"Area","status":"CONSISTENT" if area_diff<=tol else "REVIEW REQUIRED",
            "document":area_num,"parcel":prop["area"],"difference":area_diff,"tolerance":tol,
            "unit":prop["area_unit"],"source":"OCR-derived document field / parcel dataset",
            "document_source":"OCR-derived document field","parcel_source":prop.get("data_source") or "Parcel dataset",
            "confidence":{"document":area_conf,"parcel":float(prop.get("source_confidence") or 0)},
        })

    conflicts=sum(x["status"]=="CONFLICT" for x in checks)
    reviews=sum(x["status"]=="REVIEW REQUIRED" for x in checks)
    missing=sum(x["status"]=="INSUFFICIENT EVIDENCE" for x in checks)
    overall="CONFLICT" if conflicts else ("REVIEW REQUIRED" if reviews else ("INSUFFICIENT EVIDENCE" if missing else "CONSISTENT"))
    return {
        "document_id":doc_id,"property_id":prop["property_id"],"overall_status":overall,
        "checks":checks,
        "explanation":"Comparison is decision support only; it does not establish legal ownership, fraud, or authenticity.",
    }

@router.get("/dashboard")
def land_dashboard(user: dict=Depends(get_current_user)):
    with get_db() as db:
        total=db.execute("SELECT COUNT(*) c FROM properties").fetchone()["c"]
        docs=db.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"]
        cases=db.execute("SELECT COUNT(*) c FROM verification_cases").fetchone()["c"]
        pending=db.execute("SELECT COUNT(*) c FROM verification_cases WHERE status IN ('OPEN','UNDER_REVIEW','NEEDS_CORRECTION')").fetchone()["c"]
        linked=db.execute("SELECT COUNT(DISTINCT document_id) c FROM property_documents").fetchone()["c"]
        review_required=db.execute("SELECT COUNT(*) c FROM verification_findings WHERE status IN ('OPEN','ACKNOWLEDGED') AND severity='REVIEW'").fetchone()["c"]
        conflicts=db.execute("SELECT COUNT(*) c FROM verification_findings WHERE status IN ('OPEN','ACKNOWLEDGED') AND severity='CONFLICT'").fetchone()["c"]
        low_confidence=db.execute("SELECT COUNT(*) c FROM documents WHERE mean_conf < 65").fetchone()["c"]
    return {"total_properties":total,"documents_processed":docs,"pending_verification":pending,"review_required":review_required,"conflicts":conflicts,"no_parcel_match":max(0,docs-linked),"low_confidence":low_confidence,"completed_cases":max(0,cases-pending),"demo_data":True}

@router.get("/cases")
def list_cases(status: Optional[str]=None, user: dict=Depends(get_current_user)):
    with get_db() as db:
        if status:
            rows=db.execute("SELECT * FROM verification_cases WHERE status=? ORDER BY updated_at DESC",(status,)).fetchall()
        else: rows=db.execute("SELECT * FROM verification_cases ORDER BY updated_at DESC").fetchall()
    return {"cases":[dict(r) for r in rows]}

@router.post("/cases")
def create_case(req: CaseCreate, user: dict=Depends(require_roles(ROLE_DATA_OFFICER,ROLE_VERIFICATION_OFFICER,ROLE_ADMIN))):
    if req.property_id:
        with get_db() as db:
            if not db.execute("SELECT 1 FROM properties WHERE property_id=? OR parcel_id=?",(req.property_id,req.property_id)).fetchone(): raise HTTPException(404,"Property not found")
    case_id="CASE-"+uuid.uuid4().hex[:10].upper(); now=_now()
    with get_db() as db:
        db.execute("INSERT INTO verification_cases(case_id,property_id,status,assigned_officer,findings,warnings,comparison_results,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",(case_id,req.property_id,"OPEN",req.assigned_officer or user["email"],_json(req.findings),_json(req.warnings),"[]",now,now))
        for item in req.findings:
            if not isinstance(item, dict) or not item.get("title"):
                continue
            db.execute(
                "INSERT INTO verification_findings(finding_id,property_id,case_id,finding_type,severity,status,title,evidence,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("FND-" + uuid.uuid4().hex[:10].upper(), req.property_id, case_id, item.get("finding_type","REVIEW"), item.get("severity","REVIEW"), "OPEN", item["title"], _json(item.get("evidence",{})), user["email"], now, now),
            )
        if req.property_id: db.execute("INSERT INTO property_timeline(id,property_id,event_type,description,source,created_at) VALUES (?,?,?,?,?,?)",(uuid.uuid4().hex,req.property_id,"VERIFICATION_STARTED",f"Verification case {case_id} created.","Verification workflow",now))
    log_audit(user["full_name"],"CASE_CREATED",f"Created verification case {case_id}",case_id)
    return {"case_id":case_id,"status":"OPEN"}

@router.get("/cases/{case_id}")
def case_detail(case_id: str, user: dict=Depends(get_current_user)):
    with get_db() as db:
        row=db.execute("SELECT * FROM verification_cases WHERE case_id=?",(case_id,)).fetchone()
        if not row: raise HTTPException(404,"Case not found")
        tasks=db.execute("SELECT * FROM verification_tasks WHERE case_id=? ORDER BY created_at",(case_id,)).fetchall()
        findings=db.execute("SELECT * FROM verification_findings WHERE case_id=? ORDER BY created_at ASC",(case_id,)).fetchall()
        audits=db.execute("SELECT * FROM audit WHERE detail LIKE ? ORDER BY id DESC",(f"%{case_id}%",)).fetchall()
    result=dict(row); result["findings"]=json.loads(result["findings"] or "[]"); result["warnings"]=json.loads(result["warnings"] or "[]"); result["comparison_results"]=json.loads(result["comparison_results"] or "[]"); result["tasks"]= [dict(x) for x in tasks]; result["finding_records"]=[dict(x) for x in findings]; result["audit"]= [dict(x) for x in audits]
    return result

@router.post("/import-geojson")
async def import_geojson(
    file: UploadFile = File(...),
    source: str = "User-provided dataset",
    license_name: str = "Unspecified; verify before use",
    user: dict = Depends(require_roles(ROLE_DATA_OFFICER, ROLE_ADMIN)),
):
    raw=await file.read()
    if len(raw)>5*1024*1024: raise HTTPException(413,"GeoJSON exceeds 5 MB limit")
    try: data=json.loads(raw.decode("utf-8"))
    except Exception: raise HTTPException(422,"Invalid GeoJSON JSON")
    if data.get("type")!="FeatureCollection": raise HTTPException(422,"Expected a GeoJSON FeatureCollection")
    features=data.get("features") or []
    if len(features)>1000: raise HTTPException(422,"Too many features for one import")
    requested_crs = data.get("crs")
    if requested_crs:
        crs_name = ""
        if isinstance(requested_crs, dict):
            crs_name = str(((requested_crs.get("properties") or {}).get("name")) or "")
        if crs_name and crs_name.upper() not in {"EPSG:4326", "URN:OGC:DEF:CRS:OGC:1.3:CRS84"}:
            raise HTTPException(422,"Only WGS84/EPSG:4326 GeoJSON is supported.")
    imported=0; rejected=[]
    for idx,f in enumerate(features):
        geom=f.get("geometry"); props=f.get("properties") or {}
        if not geom: rejected.append({"index":idx,"reason":"Missing geometry"}); continue
        if HAS_SHAPELY:
            try:
                g=shape(geom)
                if g.is_empty or not g.is_valid or not g.geom_type in ("Polygon","MultiPolygon"): rejected.append({"index":idx,"reason":"Invalid polygon geometry"}); continue
                if not all(math.isfinite(x) for x in g.bounds): raise ValueError("Non-finite coordinates")
                minx,miny,maxx,maxy=g.bounds
                if minx < -180 or maxx > 180 or miny < -90 or maxy > 90:
                    raise ValueError("Coordinates fall outside WGS84 bounds")
                centroid=g.centroid
                lon,lat=centroid.x,centroid.y
            except Exception: rejected.append({"index":idx,"reason":"Invalid polygon geometry"}); continue
        else: rejected.append({"index":idx,"reason":"Shapely is required for safe polygon import"}); continue
        parcel_id=str(props.get("parcel_id") or props.get("property_id") or f"IMPORTED-{idx+1}").strip()
        property_id=str(props.get("property_id") or parcel_id).strip()
        area=props.get("area")
        try: area=float(area) if area is not None else None
        except Exception: area=None
        now=_now()
        with get_db() as db:
            db.execute("""INSERT INTO properties(property_id,parcel_id,district,taluka,village,survey_number,gat_number,khasra_number,sub_division,parent_property_id,area,area_unit,geometry,centroid,latitude,longitude,crs,georeferenced,geometry_source,geometry_confidence,data_source,source_confidence,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(property_id) DO UPDATE SET geometry=excluded.geometry,centroid=excluded.centroid,latitude=excluded.latitude,longitude=excluded.longitude,updated_at=excluded.updated_at""",
                (property_id,parcel_id,props.get("district"),props.get("taluka"),props.get("village"),props.get("survey_number"),props.get("gat_number"),props.get("khasra_number"),props.get("sub_division"),props.get("parent_property_id"),area,props.get("area_unit","ha"),_json(geom),_json({"latitude":lat,"longitude":lon}),lat,lon,props.get("crs","EPSG:4326"),1,props.get("geometry_source","Imported GeoJSON"),float(props.get("geometry_confidence",0.8)),props.get("data_source","User-provided dataset"),float(props.get("source_confidence",0.8)),now,now))
        imported+=1
    source_id = "SRC-" + uuid.uuid4().hex[:10].upper()
    with get_db() as db:
        db.execute(
            "INSERT INTO dataset_sources(source_id,source,license,crs,georeferenced,confidence,imported_at) VALUES (?,?,?,?,?,?,?)",
            (source_id, source.strip()[:200] or "User-provided dataset", license_name.strip()[:200], "EPSG:4326", 1, 0.8, _now()),
        )
    log_audit(user["full_name"],"GEOJSON_IMPORT",f"Imported {imported} parcels; rejected {len(rejected)} features")
    return {"imported":imported,"rejected":rejected,"source_id":source_id,"source":source,"license":license_name,"authoritative":False}



class FindingCreate(BaseModel):
    property_id: str
    case_id: Optional[str] = None
    finding_type: str
    severity: str = "REVIEW"
    title: str
    evidence: Dict[str, Any] = Field(default_factory=dict)

class FindingUpdate(BaseModel):
    status: str

class CaseStatusUpdate(BaseModel):
    status: str

class TaskCreate(BaseModel):
    title: str
    description: str = ""
    priority: str = "NORMAL"
    assigned_to: Optional[str] = None

class TaskUpdate(BaseModel):
    status: str
    description: Optional[str] = None


def _record_timeline(property_id: str, event_type: str, description: str, source: str, created_at: Optional[float] = None) -> None:
    with get_db() as db:
        db.execute(
            "INSERT INTO property_timeline(id,property_id,event_type,description,source,created_at) VALUES (?,?,?,?,?,?)",
            (uuid.uuid4().hex, property_id, event_type, description, source, created_at or _now()),
        )



@router.get("/search")
def unified_property_search(q: str = "", limit: int = 25, user: dict = Depends(get_current_user)):
    q = (q or "").strip()
    if not q:
        return {"results": []}
    limit = max(1, min(limit, 50))
    needle = f"%{q}%"
    with get_db() as db:
        rows = db.execute(
            """SELECT property_id, parcel_id, survey_number, gat_number, khasra_number,
                      village, taluka, district, area, area_unit
               FROM properties
               WHERE property_id LIKE ? OR parcel_id LIKE ? OR survey_number LIKE ?
                  OR gat_number LIKE ? OR khasra_number LIKE ? OR village LIKE ?
                  OR taluka LIKE ? OR district LIKE ?
               ORDER BY parcel_id LIMIT ?""",
            (needle, needle, needle, needle, needle, needle, needle, needle, limit),
        ).fetchall()
    return {"results": [dict(r) for r in rows], "query": q}

@router.get("/map-config")
def map_config():
    return {
        "tile_url": os.getenv("MAP_TILE_URL", "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"),
        "attribution": os.getenv("MAP_ATTRIBUTION", "© OpenStreetMap contributors"),
        "min_zoom": int(os.getenv("MAP_MIN_ZOOM", "3")),
        "max_zoom": int(os.getenv("MAP_MAX_ZOOM", "19")),
        "provider": "configurable",
        "offline_core": True,
    }

@router.get("/investigate/{property_id}")
def investigate_property(property_id: str, user: dict = Depends(get_current_user)):
    detail = property_detail(property_id, user)
    with get_db() as db:
        findings = db.execute("SELECT * FROM verification_findings WHERE property_id=? ORDER BY updated_at DESC", (detail["property_id"],)).fetchall()
        cases = db.execute("SELECT case_id,status,assigned_officer,created_at,updated_at FROM verification_cases WHERE property_id=? ORDER BY updated_at DESC", (detail["property_id"],)).fetchall()
    return {**detail, "findings": [dict(x) for x in findings], "cases": [dict(x) for x in cases]}

@router.get("/timeline/{property_id}")
def property_timeline(property_id: str, user: dict = Depends(get_current_user)):
    with get_db() as db:
        rows = db.execute("SELECT event_type,description,source,created_at FROM property_timeline WHERE property_id=? ORDER BY created_at ASC", (property_id,)).fetchall()
    return {"property_id": property_id, "timeline": [dict(x) for x in rows]}

@router.get("/provenance/{property_id}")
def property_provenance(property_id: str, user: dict = Depends(get_current_user)):
    with get_db() as db:
        rows = db.execute("SELECT field_name,value,source,confidence,created_at FROM provenance WHERE property_id=? ORDER BY created_at DESC", (property_id,)).fetchall()
    return {"property_id": property_id, "provenance": [dict(x) for x in rows]}



@router.get("/parent-consistency/{property_id}")
def parent_consistency(property_id: str, user: dict = Depends(get_current_user)):
    with get_db() as db:
        child = db.execute("SELECT * FROM properties WHERE property_id=? OR parcel_id=?", (property_id, property_id)).fetchone()
        if not child:
            raise HTTPException(404, "Property not found")
        parent_id = child["parent_property_id"]
        if not parent_id:
            return {"status":"INSUFFICIENT EVIDENCE","message":"No configured parent parcel for this property."}
        parent = db.execute("SELECT property_id,parcel_id,area,area_unit FROM properties WHERE property_id=?", (parent_id,)).fetchone()
        children = db.execute("SELECT property_id,parcel_id,area,area_unit FROM properties WHERE parent_property_id=?", (parent_id,)).fetchall()
    if not parent:
        return {"status":"INSUFFICIENT EVIDENCE","message":"Configured parent parcel is unavailable."}
    unit = parent["area_unit"] or "ha"
    compatible = [c for c in children if (c["area_unit"] or unit) == unit]
    child_total = round(sum(float(c["area"] or 0) for c in compatible), 4)
    difference = round(abs(float(parent["area"] or 0) - child_total), 4)
    tolerance = float(os.getenv("LAND_AREA_TOLERANCE_HA", "0.05"))
    return {"parent":dict(parent),"children":[dict(c) for c in children],"child_total":child_total,"difference":difference,"tolerance":tolerance,"status":"CONSISTENT" if difference <= tolerance else "REVIEW REQUIRED","explanation":"Derived area arithmetic only; not a legal survey conclusion."}

@router.get("/findings")
def list_findings(property_id: Optional[str] = None, status_filter: Optional[str] = None, user: dict = Depends(get_current_user)):
    clauses = []
    params: List[Any] = []
    if property_id:
        clauses.append("property_id=?")
        params.append(property_id)
    if status_filter:
        clauses.append("status=?")
        params.append(status_filter)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_db() as db:
        rows = db.execute(f"SELECT * FROM verification_findings{where} ORDER BY updated_at DESC", tuple(params)).fetchall()
    return {"findings": [dict(x) for x in rows]}

@router.post("/findings")
def create_finding(req: FindingCreate, user: dict = Depends(require_roles(ROLE_DATA_OFFICER, ROLE_VERIFICATION_OFFICER, ROLE_ADMIN))):
    with get_db() as db:
        prop = db.execute("SELECT property_id FROM properties WHERE property_id=? OR parcel_id=?", (req.property_id, req.property_id)).fetchone()
        if not prop:
            raise HTTPException(404, "Property not found")
    finding_id = "FND-" + uuid.uuid4().hex[:10].upper()
    now = _now()
    with get_db() as db:
        db.execute("INSERT INTO verification_findings(finding_id,property_id,case_id,finding_type,severity,status,title,evidence,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (finding_id, prop["property_id"], req.case_id, req.finding_type, req.severity.upper(), "OPEN", req.title, _json(req.evidence), user["email"], now, now))
    _record_timeline(prop["property_id"], "FINDING_CREATED", f"Finding {finding_id}: {req.title}", "Verification engine", now)
    log_audit(user["full_name"], "FINDING_CREATED", f"Created finding {finding_id} for {prop['property_id']}")
    return {"finding_id": finding_id, "status": "OPEN"}

@router.patch("/findings/{finding_id}")
def update_finding(finding_id: str, req: FindingUpdate, user: dict = Depends(require_roles(ROLE_VERIFICATION_OFFICER, ROLE_ADMIN))):
    allowed = {"OPEN", "ACKNOWLEDGED", "RESOLVED", "DISMISSED"}
    status_value = req.status.upper().strip()
    if status_value not in allowed:
        raise HTTPException(422, f"Unsupported finding status. Use one of {sorted(allowed)}.")
    with get_db() as db:
        row = db.execute("SELECT property_id FROM verification_findings WHERE finding_id=?", (finding_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Finding not found")
        db.execute("UPDATE verification_findings SET status=?,updated_at=? WHERE finding_id=?", (status_value, _now(), finding_id))
    _record_timeline(row["property_id"], "FINDING_STATUS_CHANGED", f"Finding {finding_id} changed to {status_value}", "Verification officer")
    log_audit(user["full_name"], "FINDING_STATUS_CHANGED", f"Finding {finding_id}: {status_value}")
    return {"finding_id": finding_id, "status": status_value}



@router.patch("/cases/{case_id}/status")
def update_case_status(case_id: str, req: CaseStatusUpdate, user: dict = Depends(require_roles(ROLE_VERIFICATION_OFFICER, ROLE_ADMIN))):
    status_value = req.status.upper().strip()
    if status_value not in CASE_STATUSES:
        raise HTTPException(422, f"Unsupported case status. Use one of {sorted(CASE_STATUSES)}.")
    with get_db() as db:
        row = db.execute("SELECT property_id FROM verification_cases WHERE case_id=?", (case_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Case not found")
        db.execute("UPDATE verification_cases SET status=?,updated_at=? WHERE case_id=?", (status_value, _now(), case_id))
    if row["property_id"]:
        _record_timeline(row["property_id"], "CASE_STATUS_CHANGED", f"Case {case_id} changed to {status_value}", "Verification workflow")
    log_audit(user["full_name"], "CASE_STATUS_CHANGED", f"Case {case_id}: {status_value}", case_id)
    return {"case_id": case_id, "status": status_value}

@router.post("/cases/{case_id}/tasks")
def create_case_task(case_id: str, req: TaskCreate, user: dict = Depends(require_roles(ROLE_DATA_OFFICER, ROLE_VERIFICATION_OFFICER, ROLE_ADMIN))):
    task_id = "TASK-" + uuid.uuid4().hex[:10].upper()
    now = _now()
    with get_db() as db:
        case = db.execute("SELECT property_id FROM verification_cases WHERE case_id=?", (case_id,)).fetchone()
        if not case:
            raise HTTPException(404, "Case not found")
        db.execute("INSERT INTO verification_tasks(task_id,case_id,title,status,assigned_to,created_at,updated_at) VALUES (?,?,?,?,?,?,?)", (task_id, case_id, req.title, "OPEN", req.assigned_to or user["email"], now, now))
    if case["property_id"]:
        _record_timeline(case["property_id"], "TASK_CREATED", f"Task {task_id}: {req.title}", "Verification workflow", now)
    log_audit(user["full_name"], "TASK_CREATED", f"Created task {task_id}", case_id)
    return {"task_id": task_id, "status": "OPEN"}

@router.patch("/tasks/{task_id}")
def update_case_task(task_id: str, req: TaskUpdate, user: dict = Depends(require_roles(ROLE_DATA_OFFICER, ROLE_VERIFICATION_OFFICER, ROLE_ADMIN))):
    allowed = {"OPEN", "IN_PROGRESS", "COMPLETED", "CANCELLED"}
    status_value = req.status.upper().strip()
    if status_value not in allowed:
        raise HTTPException(422, f"Unsupported task status. Use one of {sorted(allowed)}.")
    with get_db() as db:
        row = db.execute("SELECT case_id FROM verification_tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Task not found")
        db.execute("UPDATE verification_tasks SET status=?,updated_at=? WHERE task_id=?", (status_value, _now(), task_id))
    log_audit(user["full_name"], "TASK_STATUS_CHANGED", f"Task {task_id}: {status_value}", row["case_id"])
    return {"task_id": task_id, "status": status_value}

@router.get("/cases/{case_id}/tasks")
def list_case_tasks(case_id: str, user: dict = Depends(get_current_user)):
    with get_db() as db:
        rows = db.execute("SELECT * FROM verification_tasks WHERE case_id=? ORDER BY created_at", (case_id,)).fetchall()
    return {"tasks": [dict(x) for x in rows]}

_ensure_tables()
app.include_router(router)
