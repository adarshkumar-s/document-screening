"""Canonical, read-only parcel resolution for the land-record map.

This module deliberately does not replace the existing mapping or Land
Intelligence stores. It resolves only parcels visible to the current caller
and never persists derived centroids/geocodes from a map lookup.
"""
from __future__ import annotations

import json
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query

import mapping
from server import get_current_user, get_db

router = APIRouter(prefix="/api/parcels", tags=["Parcel Locator"])
# This router is mounted before mapping.map_router so the legacy map-properties
# endpoint cannot expose the unrestricted reference-property table.
map_router = APIRouter(prefix="/api/map", tags=["Parcel Map"])


def _norm(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _land_norm(value: Any) -> str:
    out = []
    for ch in str(value or "").strip():
        try:
            out.append(str(unicodedata.digit(ch)))
        except (TypeError, ValueError):
            out.append(ch)
    return "".join(out).replace(" ", "").casefold()


def _json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except Exception:
        return None


def _authorized_property_ids(user: Dict[str, Any]) -> Optional[set[str]]:
    """Return parcel IDs visible through the canonical document RBAC."""
    role = str(user.get("role") or "").upper()
    if role in {"ADMIN", "VERIFICATION_OFFICER"}:
        return None
    with get_db() as db:
        rows = db.execute(
            """SELECT DISTINCT pd.property_id, d.id, d.status, d.uploaded_by
               FROM property_documents pd
               JOIN documents d ON d.id = pd.document_id"""
        ).fetchall()
    visible = set()
    for row in rows:
        if mapping._map_document_visible(row, user):
            visible.add(str(row["property_id"]))
    return visible


def _visible_properties(user: Dict[str, Any]) -> List[Any]:
    allowed = _authorized_property_ids(user)
    with get_db() as db:
        rows = db.execute("SELECT * FROM properties ORDER BY village, survey_number, property_id").fetchall()
    if allowed is None:
        return rows
    return [row for row in rows if str(row["property_id"]) in allowed]


def _row_payload(row: Any) -> Dict[str, Any]:
    item = mapping._property_dict(row, include_geometry=True)
    geometry = item.get("geometry")
    centroid = item.get("centroid")
    inside = _surface_point(geometry)
    if not inside and isinstance(centroid, (list, tuple)) and len(centroid) >= 2:
        inside = [float(centroid[0]), float(centroid[1])]
    item["surface_point"] = inside
    item["location"] = {
        **(item.get("location") or {}),
        "status": item.get("location", {}).get("status") or ("REFERENCE" if geometry else "UNRESOLVED"),
        "source": item.get("location", {}).get("source") or item.get("geometry_source"),
        "centroid": centroid,
        "surface_point": inside,
    }
    item["quality"] = _geometry_quality(geometry, centroid)
    item["disclaimer"] = "Reference geometry is not an authoritative cadastral boundary."
    return item


def _ring_point(ring: Sequence[Sequence[float]]) -> Optional[List[float]]:
    if not ring:
        return None
    points = [p for p in ring if len(p) >= 2]
    if not points:
        return None
    return [sum(float(p[0]) for p in points) / len(points), sum(float(p[1]) for p in points) / len(points)]


def _point_in_ring(point: Sequence[float], ring: Sequence[Sequence[float]]) -> bool:
    if len(point) < 2 or len(ring) < 3:
        return False
    x, y = float(point[0]), float(point[1])
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = float(ring[i][0]), float(ring[i][1])
        xj, yj = float(ring[j][0]), float(ring[j][1])
        intersects = ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-30) + xi)
        if intersects:
            inside = not inside
        j = i
    return inside


def _point_in_geometry(point: Sequence[float], geometry: Optional[Dict[str, Any]]) -> bool:
    if not geometry:
        return False
    typ = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if typ == "Polygon":
        return bool(coords and _point_in_ring(point, coords[0]) and not any(_point_in_ring(point, hole) for hole in coords[1:]))
    if typ == "MultiPolygon":
        return any(_point_in_geometry(point, {"type": "Polygon", "coordinates": polygon}) for polygon in coords)
    return False


def _surface_point(geometry: Optional[Dict[str, Any]]) -> Optional[List[float]]:
    if not geometry:
        return None
    typ = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if typ == "Polygon" and coords:
        ring = coords[0]
        candidate = _ring_point(ring)
        if candidate and _point_in_geometry(candidate, geometry):
            return candidate
        # Avoid returning an outside centroid as the map focus. Try edge
        # midpoints, then retain the centroid only as a labelled fallback.
        for index in range(max(0, len(ring) - 1)):
            if len(ring[index]) < 2 or len(ring[index + 1]) < 2:
                continue
            point = [
                (float(ring[index][0]) + float(ring[index + 1][0])) / 2.0,
                (float(ring[index][1]) + float(ring[index + 1][1])) / 2.0,
            ]
            if _point_in_geometry(point, geometry):
                return point
        return candidate
    if typ == "MultiPolygon" and coords:
        for polygon in coords:
            point = _surface_point({"type": "Polygon", "coordinates": polygon})
            if point:
                return point
    if typ == "Point" and coords:
        return [float(coords[0]), float(coords[1])]
    return None


def _geometry_quality(geometry: Optional[Dict[str, Any]], centroid: Any) -> Dict[str, Any]:
    if not geometry:
        return {"geometry": "MISSING", "centroid": "MISSING", "centroid_inside": None}
    centroid_inside = None
    if isinstance(centroid, (list, tuple)) and len(centroid) >= 2:
        centroid_inside = _point_in_geometry(centroid, geometry)
    return {
        "geometry": "AVAILABLE",
        "centroid": "AVAILABLE" if centroid is not None else "MISSING",
        "centroid_inside": centroid_inside,
        "surface_point": "AVAILABLE" if _surface_point(geometry) else "MISSING",
    }


def _matches(row: Any, q: str) -> bool:
    if not q:
        return True
    qn = _norm(q)
    ln = _land_norm(q)
    values = [
        row["property_id"], row["parcel_id"], row["district"], row["taluka"], row["village"],
        row["survey_number"], row["gat_number"], row["khasra_number"], row["sub_division"],
    ]
    return any(qn in _norm(v) for v in values) or any(ln and ln in _land_norm(v) for v in values)


def _filter_rows(rows: Iterable[Any], *, q: str = "", village: str = "", district: str = "", survey: str = "") -> List[Any]:
    result = []
    for row in rows:
        if q and not _matches(row, q):
            continue
        if village and _norm(row["village"]) != _norm(village):
            continue
        if district and _norm(row["district"]) != _norm(district):
            continue
        if survey and _land_norm(survey) not in _land_norm(row["survey_number"]):
            continue
        result.append(row)
    return result


@router.get("/search")
def search_parcels(
    q: str = Query("", max_length=200),
    village: str = Query("", max_length=200),
    district: str = Query("", max_length=200),
    survey: str = Query("", max_length=120),
    limit: int = Query(25, ge=1, le=100),
    user: Dict[str, Any] = Depends(get_current_user),
):
    rows = _filter_rows(_visible_properties(user), q=q, village=village, district=district, survey=survey)
    return {
        "results": [_row_payload(row) for row in rows[:limit]],
        "total": len(rows),
        "ambiguous": len(rows) > 1,
        "authorized_only": True,
    }


def _get(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        if key in row.keys():
            return row[key]
    except Exception:
        return default
    return default


def _row_dict(row: Any) -> Dict[str, Any]:
    if isinstance(row, dict):
        return row
    return {key: row[key] for key in row.keys()}


def _survey_key(value: Any) -> str:
    return mapping._land_number(value).casefold()


def _row_survey_keys(row: Any) -> set:
    keys = set()
    for field in ("survey_number", "gat_number", "khasra_number"):
        key = _survey_key(_get(row, field))
        if key:
            keys.add(key)
    return keys


def _context_conflict(row: Any, *, village: str = "", district: str = "", tehsil: str = "") -> bool:
    row_village = _norm(_get(row, "village"))
    if village and row_village and row_village != _norm(village):
        return True
    row_district = _norm(_get(row, "district"))
    if district and row_district and row_district != _norm(district):
        return True
    row_tehsil = _norm(_get(row, "taluka") or _get(row, "tehsil"))
    if tehsil and row_tehsil and row_tehsil != _norm(tehsil):
        return True
    return False


def _identity_conflict(row: Any, *, survey: str = "", khasra: str = "", village: str = "", district: str = "", tehsil: str = "") -> bool:
    primary = _survey_key(survey) or _survey_key(khasra)
    if primary and primary not in _row_survey_keys(row):
        return True
    return _context_conflict(row, village=village, district=district, tehsil=tehsil)


def _land_id_for(row: Any) -> str:
    survey = str(_get(row, "survey_number") or _get(row, "khasra_number") or "").strip()
    village = str(_get(row, "village") or "").strip()
    if not survey or not village:
        return ""
    try:
        from land_intel import land_identity
    except Exception:
        return ""
    return str(land_identity(survey, village)[1] or "")


def _document_fields(document: Any) -> Dict[str, Any]:
    fields = _get(document, "fields")
    if isinstance(fields, str):
        fields = mapping._parse_json(fields, {})
    return fields if isinstance(fields, dict) else {}


def _document_identity(document: Any) -> Dict[str, str]:
    fields = _document_fields(document)
    return {
        "survey": mapping._field_value(fields, "survey_number"),
        "khasra": mapping._field_value(fields, "khasra_number"),
        "village": mapping._field_value(fields, "village"),
        "district": mapping._field_value(fields, "district"),
        "tehsil": mapping._field_value(fields, "tehsil", "taluka"),
    }


def _geometry_value(row: Any) -> Any:
    geometry = _get(row, "geometry")
    if isinstance(geometry, str):
        geometry = _json(geometry)
    return geometry if isinstance(geometry, dict) else None


def _has_geometry(row: Any) -> bool:
    geometry = _geometry_value(row)
    return bool(geometry and geometry.get("type") in {"Polygon", "MultiPolygon", "Point", "MultiPoint"})


def _authoritative_pin(row: Any) -> bool:
    if str(_get(row, "location_status") or "") == "EXACT_PIN":
        return True
    latitude = _get(row, "latitude")
    longitude = _get(row, "longitude")
    return latitude not in (None, "") and longitude not in (None, "")


def _location_classification(row: Any) -> Dict[str, Any]:
    has_geometry = _has_geometry(row)
    authoritative = _authoritative_pin(row)
    centroid = _json(_get(row, "centroid"))
    has_centroid = isinstance(centroid, (list, tuple)) and len(centroid) >= 2
    if authoritative:
        kind, label = "authoritative", "Verified location"
    elif has_geometry:
        kind, label = "reference_geometry", "Reference geometry"
    else:
        kind, label = "unresolved", "Location not available"
    if has_centroid:
        surface = "STORED_CENTROID"
    elif has_geometry:
        surface = "DERIVED_NOT_STORED"
    else:
        surface = "NOT_AVAILABLE"
    return {
        "kind": kind,
        "label": label,
        "authoritative": authoritative,
        "stored_latitude": mapping._number(_get(row, "latitude")),
        "stored_longitude": mapping._number(_get(row, "longitude")),
        "reference_geometry": "AVAILABLE" if has_geometry else "NOT_AVAILABLE",
        "derived_centroid": "AVAILABLE" if has_centroid else "NOT_AVAILABLE",
        "surface_point": surface,
        "geocoded_address": None,
    }


def _empty_spatial(document: Any = None) -> Dict[str, Any]:
    land_id = ""
    if document is not None:
        identity = _document_identity(document)
        if (identity["survey"] or identity["khasra"]) and identity["village"]:
            land_id = _land_id_for({
                "survey_number": identity["survey"] or identity["khasra"],
                "village": identity["village"],
            })
    return {
        "location_label": "Location not available",
        "location_kind": "unresolved",
        "has_geometry": False,
        "land_id": land_id or None,
        "parcel_id": None,
        "property_id": None,
        "authoritative": False,
    }


def _table_exists(db: Any, name: str) -> bool:
    try:
        if getattr(db, "is_pg", False):
            row = db.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name=? LIMIT 1",
                (name,),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (name,),
            ).fetchone()
        return bool(row)
    except Exception:
        return False


def _links_for(db: Any, document_ids: Sequence[str]) -> Dict[str, str]:
    ids = [str(item) for item in document_ids if item]
    if not ids or not _table_exists(db, "property_documents"):
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = db.execute(
        f"SELECT document_id, property_id FROM property_documents WHERE document_id IN ({placeholders})",
        tuple(ids),
    ).fetchall()
    links: Dict[str, str] = {}
    for row in rows:
        links[str(_get(row, "document_id"))] = str(_get(row, "property_id") or "")
    return links


def match_properties(
    properties: Sequence[Any],
    *,
    parcel_id: str = "",
    property_id: str = "",
    land_id: str = "",
    survey: str = "",
    khasra: str = "",
    village: str = "",
    district: str = "",
    tehsil: str = "",
    linked_property_id: str = "",
) -> Tuple[List[Any], str]:
    """Match parcels by identity only. Owner name is never consulted."""
    explicit = str(parcel_id or property_id or "").strip()
    if explicit:
        hits = [
            row for row in properties
            if str(_get(row, "property_id") or "") == explicit or str(_get(row, "parcel_id") or "") == explicit
        ]
        return hits, "parcel_id"
    wanted_land = str(land_id or "").strip()
    if wanted_land:
        hits = [
            row for row in properties
            if _land_id_for(row) == wanted_land and not _context_conflict(row, village=village, district=district, tehsil=tehsil)
        ]
        return hits, "land_id"
    primary = _survey_key(survey) or _survey_key(khasra)
    if primary:
        hits = []
        for row in properties:
            if primary not in _row_survey_keys(row):
                continue
            if village and _norm(_get(row, "village")) != _norm(village):
                continue
            if _context_conflict(row, district=district, tehsil=tehsil):
                continue
            hits.append(row)
        if village or len(hits) <= 1:
            return hits, "survey_village"
        return hits, "ambiguous"
    linked = str(linked_property_id or "").strip()
    if linked:
        hits = [
            row for row in properties
            if str(_get(row, "property_id") or "") == linked
            and not _context_conflict(row, village=village, district=district, tehsil=tehsil)
        ]
        return hits, "linked_document"
    return [], "none"


def _summary_for_match(matches: Sequence[Any], document: Any = None) -> Dict[str, Any]:
    base = _empty_spatial(document)
    if len(matches) != 1:
        if len(matches) > 1:
            base["location_kind"] = "ambiguous"
        return base
    row = matches[0]
    classification = _location_classification(row)
    base.update({
        "location_label": classification["label"],
        "location_kind": classification["kind"],
        "has_geometry": classification["reference_geometry"] == "AVAILABLE",
        "land_id": _land_id_for(row) or base.get("land_id"),
        "parcel_id": _get(row, "parcel_id") or None,
        "property_id": _get(row, "property_id") or None,
        "authoritative": classification["authoritative"],
    })
    return base


def attach_document_spatial(documents: Sequence[Dict[str, Any]]) -> None:
    """Attach a label/id summary. Never include polygon rings in a list payload."""
    if not documents:
        return
    try:
        with get_db() as db:
            if not _table_exists(db, "properties"):
                properties: List[Any] = []
                links: Dict[str, str] = {}
            else:
                properties = db.execute(
                    """SELECT property_id, parcel_id, district, taluka, village, survey_number, gat_number,
                              khasra_number, sub_division, geometry, centroid, latitude, longitude, location_status
                       FROM properties"""
                ).fetchall()
                links = _links_for(db, [str(item.get("id")) for item in documents if item.get("id")])
    except Exception:
        properties, links = [], {}
    for document in documents:
        identity = _document_identity(document)
        matches, _reason = match_properties(
            properties,
            survey=identity["survey"],
            khasra=identity["khasra"],
            village=identity["village"],
            district=identity["district"],
            tehsil=identity["tehsil"],
            linked_property_id=links.get(str(document.get("id")), ""),
        )
        document["spatial"] = _summary_for_match(matches, document)


def annotate_map_records(records: Sequence[Dict[str, Any]]) -> None:
    """Label map records that already match a parcel. Does not add rings or write."""
    if not records:
        return
    try:
        with get_db() as db:
            if not _table_exists(db, "properties"):
                return
            properties = db.execute(
                """SELECT property_id, parcel_id, district, taluka, village, survey_number, gat_number,
                          khasra_number, sub_division, geometry, centroid, latitude, longitude, location_status
                   FROM properties"""
            ).fetchall()
            links = _links_for(db, [str(item.get("id")) for item in records if item.get("id")])
    except Exception:
        return
    for record in records:
        matches, _reason = match_properties(
            properties,
            survey=str(record.get("survey") or ""),
            khasra=str(record.get("khasra") or ""),
            village=str(record.get("village") or ""),
            district=str(record.get("district") or ""),
            tehsil=str(record.get("tehsil") or ""),
            linked_property_id=links.get(str(record.get("id")), ""),
        )
        summary = _summary_for_match(matches, {
            "fields": {
                "survey_number": {"value": record.get("survey")},
                "khasra_number": {"value": record.get("khasra")},
                "village": {"value": record.get("village")},
            }
        })
        if summary.get("land_id"):
            record["land_id"] = summary["land_id"]
        if summary.get("parcel_id"):
            record["parcel_id"] = summary["parcel_id"]
        if summary.get("property_id"):
            record["property_id"] = summary["property_id"]
        record["has_reference_geometry"] = bool(summary.get("has_geometry"))
        if record.get("lat") is not None and record.get("lon") is not None:
            continue
        if summary.get("has_geometry") and not summary.get("authoritative"):
            record["location_status"] = "REFERENCE_GEOMETRY"
            record["location_state"] = "REFERENCE_GEOMETRY"
            record["location_label"] = "Reference geometry"
            record["location_source"] = "Project-owned reference geometry"
            record["location_confidence"] = "REFERENCE"
            provenance = record.get("location_provenance")
            if isinstance(provenance, dict):
                provenance["source"] = "Project-owned reference geometry"
                provenance["authoritative"] = False


def _identity_stub(row: Any) -> Dict[str, Any]:
    return {
        "property_id": _get(row, "property_id"),
        "parcel_id": _get(row, "parcel_id"),
        "land_id": _land_id_for(row) or None,
        "survey_number": _get(row, "survey_number"),
        "khasra_number": _get(row, "khasra_number"),
        "village": _get(row, "village"),
        "taluka": _get(row, "taluka"),
        "district": _get(row, "district"),
        "sub_division": _get(row, "sub_division"),
        "has_geometry": _has_geometry(row),
    }


def _read_only_location(label: str, kind: str) -> Dict[str, Any]:
    return {
        "kind": kind,
        "label": label,
        "authoritative": False,
        "stored_latitude": None,
        "stored_longitude": None,
        "reference_geometry": "NOT_AVAILABLE",
        "derived_centroid": "NOT_AVAILABLE",
        "surface_point": "NOT_AVAILABLE",
        "geocoded_address": None,
    }


def _unresolved(message: str, document_id: str = "") -> Dict[str, Any]:
    return {
        "status": "UNRESOLVED",
        "persisted": False,
        "read_only": True,
        "parcel": None,
        "document_id": document_id or None,
        "location": _read_only_location("Location not available", "unresolved"),
        "message": message,
    }


def _conflict(message: str) -> Dict[str, Any]:
    return {
        "status": "CONFLICT",
        "persisted": False,
        "read_only": True,
        "parcel": None,
        "location": _read_only_location("Location not available", "unresolved"),
        "message": message,
    }


def _parcel_visible(user: Dict[str, Any], row: Any) -> bool:
    role = str(user.get("role") or "").upper()
    if role in {"ADMIN", "VERIFICATION_OFFICER"}:
        return True
    property_id = str(_get(row, "property_id") or "")
    if not property_id:
        return False
    allowed = _authorized_property_ids(user)
    if allowed is not None and property_id in allowed:
        return True
    with get_db() as db:
        if not _table_exists(db, "documents"):
            return False
        docs = db.execute("SELECT id, status, uploaded_by, fields FROM documents").fetchall()
    for doc in docs:
        if not mapping._map_document_visible(doc, user):
            continue
        identity = _document_identity(doc)
        matches, _reason = match_properties(
            [row],
            survey=identity["survey"],
            khasra=identity["khasra"],
            village=identity["village"],
            district=identity["district"],
            tehsil=identity["tehsil"],
        )
        if len(matches) == 1:
            return True
    return False


def _resolved_parcel(row: Any, document_id: str = "") -> Dict[str, Any]:
    item = _row_payload(row)
    classification = _location_classification(row)
    item["land_id"] = _land_id_for(row) or None
    item["document_id"] = document_id or None
    item["authoritative"] = classification["authoritative"]
    item["location"] = {**(item.get("location") or {}), **classification}
    return item


def resolve_for_user(user: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
    """Resolve one existing parcel. This function only reads; it never writes."""
    document_id = str(kwargs.get("document_id") or kwargs.get("open_record") or "").strip()
    parcel_id = str(kwargs.get("parcel_id") or "").strip()
    property_id = str(kwargs.get("property_id") or "").strip()
    land_id = str(kwargs.get("land_id") or "").strip()
    survey = str(kwargs.get("survey_number") or kwargs.get("survey") or "").strip()
    khasra = str(kwargs.get("khasra_number") or kwargs.get("khasra") or "").strip()
    village = str(kwargs.get("village") or "").strip()
    district = str(kwargs.get("district") or "").strip()
    tehsil = str(kwargs.get("tehsil") or kwargs.get("taluka") or "").strip()
    document = None
    document_identity: Dict[str, str] = {}
    linked = ""

    if document_id:
        with get_db() as db:
            row = db.execute("SELECT id, status, uploaded_by, fields FROM documents WHERE id=?", (document_id,)).fetchone()
            if row and _table_exists(db, "property_documents"):
                link = db.execute(
                    "SELECT property_id FROM property_documents WHERE document_id=? ORDER BY linked_at DESC LIMIT 1",
                    (document_id,),
                ).fetchone()
                linked = str(link["property_id"]) if link else ""
        if not row or not mapping._map_document_visible(row, user):
            raise HTTPException(status_code=404, detail="Record not found.")
        document = row
        document_identity = _document_identity(document)
        if survey and document_identity["survey"] and _survey_key(survey) != _survey_key(document_identity["survey"]):
            return _conflict("The document survey does not match the requested parcel identity. No coordinate was created.")
        if village and document_identity["village"] and _norm(village) != _norm(document_identity["village"]):
            return _conflict("The document village does not match the requested parcel identity. No coordinate was created.")
        survey = survey or document_identity["survey"]
        khasra = khasra or document_identity["khasra"]
        village = village or document_identity["village"]
        district = district or document_identity["district"]
        tehsil = tehsil or document_identity["tehsil"]

    if not any((parcel_id, property_id, land_id, survey, khasra, document_id)):
        raise HTTPException(status_code=400, detail="A parcel ID, land ID, document ID, or survey/khasra is required.")

    with get_db() as db:
        if not _table_exists(db, "properties"):
            return _unresolved("No existing parcel matches this record. No coordinate was created.", document_id)
        properties = db.execute("SELECT * FROM properties").fetchall()

    matches, reason = match_properties(
        properties,
        parcel_id=parcel_id,
        property_id=property_id,
        land_id=land_id,
        survey=survey,
        khasra=khasra,
        village=village,
        district=district,
        tehsil=tehsil,
        linked_property_id=linked if not any((parcel_id, property_id, land_id, survey, khasra)) else "",
    )
    if document and matches and (parcel_id or property_id or land_id):
        if any(_identity_conflict(
            row,
            survey=document_identity.get("survey", ""),
            khasra=document_identity.get("khasra", ""),
            village=document_identity.get("village", ""),
            district=document_identity.get("district", ""),
            tehsil=document_identity.get("tehsil", ""),
        ) for row in matches):
            return _conflict("The requested parcel does not match this record. No coordinate was created.")

    explicit = bool(parcel_id or property_id)
    if explicit and not matches:
        raise HTTPException(status_code=404, detail="Record not found.")
    if not matches:
        if linked and not any((parcel_id, property_id, land_id, survey, khasra)):
            matches = [row for row in properties if str(_get(row, "property_id") or "") == linked]
            reason = "linked_document"
        if not matches:
            return _unresolved(
                "No existing parcel matches this record by parcel ID, land ID, or survey/khasra and village. No coordinate was created.",
                document_id,
            )
    if len(matches) > 1 and linked:
        linked_hits = [row for row in matches if str(_get(row, "property_id") or "") == linked]
        if len(linked_hits) == 1:
            matches = linked_hits
            reason = "linked_document"
    if len(matches) > 1:
        return {
            "status": "AMBIGUOUS",
            "persisted": False,
            "read_only": True,
            "parcel": None,
            "matches": [_identity_stub(row) for row in matches[:25]],
            "total": len(matches),
            "message": "More than one existing parcel matches this identity. Locate did not choose one and did not create a coordinate.",
        }
    row = matches[0]
    if not document and not _parcel_visible(user, row):
        raise HTTPException(status_code=404, detail="Record not found.")
    parcel = _resolved_parcel(row, document_id)
    label = (parcel.get("location") or {}).get("label") or "Location not available"
    if parcel.get("geometry"):
        message = "Existing parcel reference geometry. No coordinate was created or stored."
    else:
        message = "Parcel identity matched an existing record, but no reference geometry is stored. No coordinate was created."
    return {
        "status": "RESOLVED",
        "persisted": False,
        "read_only": True,
        "reason": reason,
        "parcel": parcel,
        "location": parcel.get("location"),
        "message": message if label != "Verified location" else "Stored location was read. Locate did not change it.",
    }


@router.get("/for-document/{document_id}")
def parcel_for_document(document_id: str, user: Dict[str, Any] = Depends(get_current_user)):
    """Read-only parcel for one visible record. Hidden records do not reveal geometry."""
    return resolve_for_user(user, document_id=document_id)


@router.post("/resolve")
def resolve_parcel(payload: Dict[str, Any], user: Dict[str, Any] = Depends(get_current_user)):
    """Resolve an authorized parcel without persisting anything.

    Identity is parcel ID, land ID, or survey/khasra plus village and
    district/tehsil. Owner name is ignored even if a caller sends it.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="A parcel identity is required.")
    cleaned = {
        key: value for key, value in payload.items()
        if key not in {"owner", "owner_name", "holder", "name"}
    }
    return resolve_for_user(user, **cleaned)


@router.post("/locate")
def locate_point(payload: Dict[str, Any], user: Dict[str, Any] = Depends(get_current_user)):
    """Return the authorized parcel containing a map click, if any."""
    try:
        point = [float(payload["longitude"]), float(payload["latitude"])]
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="latitude and longitude are required.")
    for row in _visible_properties(user):
        if _point_in_geometry(point, _json(row["geometry"])):
            return {"status": "RESOLVED", "persisted": False, "read_only": True, "parcel": _row_payload(row)}
    return {"status": "NOT_FOUND", "message": "No authorized parcel found at this location."}


@map_router.get("/properties")
def secure_map_properties(
    village: str = Query("", max_length=200),
    tehsil: str = Query("", max_length=200),
    district: str = Query("", max_length=200),
    survey: str = Query("", max_length=120),
    q: str = Query("", max_length=200),
    limit: int = Query(500, ge=1, le=5000),
    user: Dict[str, Any] = Depends(get_current_user),
):
    """RBAC-filtered replacement for the legacy reference-property endpoint."""
    rows = _filter_rows(_visible_properties(user), q=q, village=village, district=district, survey=survey)
    return {
        "properties": [_row_payload(row) | {
            "synthetic": "synthetic" in str(row["data_source"] or "").casefold()
                or "synthetic" in str(row["geometry_source"] or "").casefold(),
            "authoritative": False,
        } for row in rows[:limit]],
        "total": len(rows),
        "metadata": {
            "authorized_only": True,
            "authoritative": False,
            "source": "Project-owned reference geometry",
            "disclaimer": "Reference geometry only; not an authoritative cadastral boundary or legal title.",
        },
    }


@map_router.get("/parcel-search")
def map_parcel_search(
    q: str = Query("", max_length=200),
    village: str = Query("", max_length=200),
    district: str = Query("", max_length=200),
    survey: str = Query("", max_length=120),
    limit: int = Query(25, ge=1, le=100),
    user: Dict[str, Any] = Depends(get_current_user),
):
    return search_parcels(q=q, village=village, district=district, survey=survey, limit=limit, user=user)
