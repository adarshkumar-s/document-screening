"""Local document mapping.

This is intentionally document-first: it maps only coordinates or GeoJSON geometry
actually present in the saved uploaded document record. It never invents parcels,
queries a geocoder, sends document contents to a map provider, or imports a
cadastral dataset.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from server import BASE_DIR, get_current_user, get_db

router = APIRouter(tags=["Document Mapping"])


def _value(fields: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = fields.get(key)
        if isinstance(value, dict):
            value = value.get("value") or value.get("text")
        if value not in (None, ""):
            return value
    return None


def _number(value: Any) -> Optional[float]:
    try:
        return float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None


def _coordinates(fields: Dict[str, Any], ocr: str) -> Optional[Tuple[float, float]]:
    lat = _number(_value(fields, "latitude", "lat"))
    lon = _number(_value(fields, "longitude", "lon", "lng"))
    if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
        return lat, lon
    # Coordinates sometimes survive OCR as "28.61, 77.20" or "Lat: ... Lon: ...".
    text = ocr or ""
    labelled = re.search(r"(?:lat(?:itude)?\s*[:=]\s*)(-?\d+(?:\.\d+)?)\D+(?:lon(?:gitude)?|lng)\s*[:=]\s*(-?\d+(?:\.\d+)?)", text, re.I)
    pair = labelled or re.search(r"\b(-?\d{1,2}\.\d{3,})\s*[,;/]\s*(-?\d{2,3}\.\d{3,})\b", text)
    if not pair:
        return None
    a, b = _number(pair.group(1)), _number(pair.group(2))
    if a is not None and b is not None and -90 <= a <= 90 and -180 <= b <= 180:
        return a, b
    return None


def _geometry(fields: Dict[str, Any]) -> Optional[dict]:
    raw = _value(fields, "geometry", "geojson", "geo_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None
    if not isinstance(raw, dict):
        return None
    if raw.get("type") == "Feature":
        raw = raw.get("geometry")
    if not isinstance(raw, dict) or raw.get("type") not in {"Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon"}:
        return None
    return raw


def _bbox(geometry: Optional[dict], point: Optional[Tuple[float, float]]) -> Optional[list]:
    coords = []
    if point:
        coords.append([point[1], point[0]])
    if geometry:
        def walk(value: Any) -> None:
            if isinstance(value, (list, tuple)):
                if len(value) >= 2 and all(isinstance(x, (int, float)) for x in value[:2]):
                    coords.append([float(value[0]), float(value[1])])
                else:
                    for child in value:
                        walk(child)
        walk(geometry.get("coordinates"))
    if not coords:
        return None
    xs, ys = zip(*coords)
    return [min(xs), min(ys), max(xs), max(ys)]


def _document(document_id: str) -> Dict[str, Any]:
    with get_db() as db:
        row = db.execute(
            "SELECT id,filename,doc_type,status,fields,ocr_text,cleaned_ocr_text,mean_conf,created_at,updated_at FROM documents WHERE id=?",
            (document_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Document record not found.")
    d = dict(row)
    try:
        d["fields"] = json.loads(d.get("fields") or "{}")
    except Exception:
        d["fields"] = {}
    return d


@router.get("/document-map/{document_id}")
def document_map(document_id: str, user=Depends(get_current_user)):
    d = _document(document_id)
    fields = d.get("fields") or {}
    point = _coordinates(fields, d.get("cleaned_ocr_text") or d.get("ocr_text") or "")
    geometry = _geometry(fields)
    location_fields = {
        "district": _value(fields, "district"),
        "taluka": _value(fields, "taluka", "tehsil"),
        "village": _value(fields, "village"),
        "survey_number": _value(fields, "survey_number"),
        "gat_number": _value(fields, "gat_number"),
        "khasra_number": _value(fields, "khasra_number"),
        "sub_division": _value(fields, "sub_division", "subdivision"),
        "address": _value(fields, "address", "property_address", "location"),
    }
    has_map_data = bool(point or geometry)
    return {
        "document": {
            "id": d["id"], "filename": d.get("filename"), "doc_type": d.get("doc_type"),
            "status": d.get("status"), "confidence": d.get("mean_conf"),
        },
        "source": "uploaded_document_record",
        "mapping": {
            "available": has_map_data,
            "method": "document_geometry" if geometry else ("document_coordinates" if point else "none"),
            "coordinates": {"latitude": point[0], "longitude": point[1]} if point else None,
            "geometry": geometry,
            "bbox": _bbox(geometry, point),
            "crs": _value(fields, "crs", "coordinate_reference_system") or ("EPSG:4326" if point else None),
            "location_fields": location_fields,
            "message": ("Map is derived only from location data contained in the uploaded document."
                         if has_map_data else
                         "No coordinates or GeoJSON geometry were found in this uploaded document. No location is fabricated."),
        },
    }


@router.get("/document-map", include_in_schema=False)
def document_map_ui():
    return FileResponse(f"{BASE_DIR}/document-map.html")


@router.get("/document-map.css", include_in_schema=False)
def document_map_css():
    return FileResponse(f"{BASE_DIR}/document-map.css", media_type="text/css")


@router.get("/document-map.js", include_in_schema=False)
def document_map_js():
    return FileResponse(f"{BASE_DIR}/document-map.js", media_type="application/javascript")
