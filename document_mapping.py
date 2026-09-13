"""Document-first mapping API.

The map is intentionally grounded only in data already stored for uploaded
screening documents. It never fabricates parcels or geocodes a place name.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException

from server import get_current_user, get_db

router = APIRouter(prefix="/api/land", tags=["Document Mapping"])


def _value(fields: Dict[str, Any], *keys: str):
    for key in keys:
        value = fields.get(key)
        if isinstance(value, dict):
            value = value.get("value") or value.get("text")
        if value not in (None, ""):
            return value
    return None


def _num(value):
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _coords(fields: Dict[str, Any], text: str):
    lat = _num(_value(fields, "latitude", "lat"))
    lon = _num(_value(fields, "longitude", "lon", "lng"))
    if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
        return lat, lon

    match = re.search(
        r"(?:lat(?:itude)?\s*[:=]\s*)(-?\d+(?:\.\d+)?)\D+"
        r"(?:lon(?:gitude)?|lng)\s*[:=]\s*(-?\d+(?:\.\d+)?)",
        text or "",
        re.I,
    )
    match = match or re.search(
        r"\b(-?\d{1,2}\.\d{3,})\s*[,;/]\s*(-?\d{2,3}\.\d{3,})\b",
        text or "",
    )
    if not match:
        return None
    first, second = _num(match.group(1)), _num(match.group(2))
    if first is None or second is None:
        return None
    return (first, second) if -90 <= first <= 90 and -180 <= second <= 180 else None


def _geometry(fields: Dict[str, Any]):
    raw = _value(fields, "geometry", "geojson", "geo_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None
    if isinstance(raw, dict) and raw.get("type") == "Feature":
        raw = raw.get("geometry")
    allowed = {"Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon"}
    return raw if isinstance(raw, dict) and raw.get("type") in allowed else None


def _bbox(geometry, point):
    points = [[point[1], point[0]]] if point else []

    def walk(value):
        if isinstance(value, (list, tuple)):
            if len(value) >= 2 and all(isinstance(x, (int, float)) for x in value[:2]):
                points.append([float(value[0]), float(value[1])])
            else:
                for item in value:
                    walk(item)

    if geometry:
        walk(geometry.get("coordinates"))
    if not points:
        return None
    xs, ys = zip(*points)
    return [min(xs), min(ys), max(xs), max(ys)]


def _doc(document_id):
    with get_db() as db:
        row = db.execute(
            "SELECT id,filename,doc_type,status,fields,ocr_text,cleaned_ocr_text,mean_conf,created_at,updated_at "
            "FROM documents WHERE id=?",
            (document_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Document record not found.")
    document = dict(row)
    try:
        document["fields"] = json.loads(document.get("fields") or "{}")
    except Exception:
        document["fields"] = {}
    return document


def _mapping(document):
    fields = document.get("fields") or {}
    text = document.get("cleaned_ocr_text") or document.get("ocr_text") or ""
    point = _coords(fields, text)
    geometry = _geometry(fields)
    location = {
        key: _value(fields, *keys)
        for key, keys in {
            "owner_name": ("owner_name",),
            "district": ("district",),
            "taluka": ("taluka", "tehsil"),
            "village": ("village",),
            "survey_number": ("survey_number",),
            "gat_number": ("gat_number",),
            "khasra_number": ("khasra_number",),
            "khata_number": ("khata_number",),
            "plot_number": ("plot_number",),
            "sub_division": ("sub_division", "subdivision"),
            "address": ("address", "property_address", "location"),
        }.items()
    }
    return {
        "available": bool(point or geometry),
        "method": "document_geometry" if geometry else ("document_coordinates" if point else "none"),
        "coordinates": {"latitude": point[0], "longitude": point[1]} if point else None,
        "geometry": geometry,
        "bbox": _bbox(geometry, point),
        "crs": _value(fields, "crs", "coordinate_reference_system") or ("EPSG:4326" if point else None),
        "location_fields": location,
    }


def _status_class(status: Any) -> str:
    value = str(status or "unknown").lower()
    if any(x in value for x in ("reject", "fail", "fraud", "conflict", "risk")):
        return "attention"
    if any(x in value for x in ("verified", "approved", "complete", "passed")):
        return "verified"
    return "review"


@router.get("/document-map/{document_id}")
def document_map(document_id: str, user=Depends(get_current_user)):
    document = _doc(document_id)
    mapping = _mapping(document)
    return {
        "document": {
            "id": document["id"],
            "filename": document.get("filename"),
            "doc_type": document.get("doc_type"),
            "status": document.get("status"),
            "status_class": _status_class(document.get("status")),
            "confidence": document.get("mean_conf"),
            "created_at": document.get("created_at"),
            "updated_at": document.get("updated_at"),
        },
        "source": "uploaded_document_record",
        "mapping": {
            **mapping,
            "message": (
                "Derived only from the uploaded document."
                if mapping["available"]
                else "No coordinates or geometry found. No location is fabricated."
            ),
        },
    }


@router.get("/document-map/documents")
def document_map_documents(
    q: Optional[str] = None,
    status: Optional[str] = None,
    mapped: Optional[bool] = None,
    user=Depends(get_current_user),
):
    with get_db() as db:
        rows = db.execute(
            "SELECT id,filename,doc_type,status,fields,ocr_text,cleaned_ocr_text,mean_conf,created_at,updated_at "
            "FROM documents ORDER BY id DESC"
        ).fetchall()

    output = []
    query = (q or "").lower().strip()
    status_filter = (status or "").lower().strip()
    for row in rows:
        document = dict(row)
        try:
            document["fields"] = json.loads(document.get("fields") or "{}")
        except Exception:
            document["fields"] = {}
        mapping = _mapping(document)
        fields = document["fields"]
        haystack = " ".join(
            str(x or "")
            for x in [
                document.get("id"), document.get("filename"), document.get("doc_type"),
                document.get("status"), fields.get("owner_name"), fields.get("survey_number"),
                fields.get("khasra_number"), fields.get("village"), fields.get("taluka"),
                fields.get("district"), fields.get("khata_number"), fields.get("plot_number"),
            ]
        ).lower()
        if query and query not in haystack:
            continue
        if status_filter and str(document.get("status") or "").lower() != status_filter:
            continue
        if mapped is True and not mapping["available"]:
            continue
        if mapped is False and mapping["available"]:
            continue
        output.append(
            {
                "id": document["id"],
                "filename": document.get("filename"),
                "doc_type": document.get("doc_type"),
                "status": document.get("status"),
                "status_class": _status_class(document.get("status")),
                "confidence": document.get("mean_conf"),
                "created_at": document.get("created_at"),
                "location": mapping["location_fields"],
                "coordinates": mapping["coordinates"],
                "geometry": mapping["geometry"],
                "bbox": mapping["bbox"],
                "mapped": mapping["available"],
                "mapping_method": mapping["method"],
            }
        )
    return {"documents": output, "total": len(output)}


@router.get("/document-map/summary")
def document_map_summary(user=Depends(get_current_user)):
    with get_db() as db:
        rows = db.execute(
            "SELECT id,filename,doc_type,status,fields,ocr_text,cleaned_ocr_text,mean_conf,created_at,updated_at "
            "FROM documents"
        ).fetchall()
    documents = []
    for row in rows:
        document = dict(row)
        try:
            document["fields"] = json.loads(document.get("fields") or "{}")
        except Exception:
            document["fields"] = {}
        documents.append(document)

    status_counts: Dict[str, int] = {}
    for document in documents:
        status = str(document.get("status") or "UNKNOWN")
        status_counts[status] = status_counts.get(status, 0) + 1

    mapped = sum(1 for document in documents if _mapping(document)["available"])
    low_confidence = sum(
        1 for document in documents
        if _num(document.get("mean_conf")) is not None and _num(document.get("mean_conf")) < 70
    )
    return {
        "total_documents": len(documents),
        "mapped_documents": mapped,
        "unmapped_documents": len(documents) - mapped,
        "low_confidence": low_confidence,
        "status_counts": status_counts,
    }
