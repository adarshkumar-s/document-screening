"""Canonical, read-only parcel resolution for the land-record map.

This module deliberately does not replace the existing mapping or Land
Intelligence stores. It resolves only parcels visible to the current caller
and never persists derived centroids/geocodes from a map lookup.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query

import mapping
from server import get_current_user, get_db

router = APIRouter(prefix="/api/parcels", tags=["Parcel Locator"])


def _norm(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _land_norm(value: Any) -> str:
    out = []
    for ch in str(value or "").strip():
        try:
            out.append(str(__import__("unicodedata").digit(ch)))
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
    """Return the parcel IDs visible through the canonical document RBAC.

    Admin/reviewer roles retain the existing map behaviour and can inspect the
    reference parcel register. Data officers/viewers are restricted to parcels
    linked to documents they are already allowed to see.
    """
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
    xs = [float(p[0]) for p in ring if len(p) >= 2]
    ys = [float(p[1]) for p in ring if len(p) >= 2]
    if not xs or not ys:
        return None
    return [sum(xs) / len(xs), sum(ys) / len(ys)]


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
        centroid = _ring_point(ring)
        if centroid and _point_in_geometry(centroid, geometry):
            return centroid
        # For the simple project geometries, the first edge midpoint is a safe
        # fallback candidate; validate it before returning.
        if len(ring) >= 2:
            p = [(float(ring[0][0]) + float(ring[1][0])) / 2.0, (float(ring[0][1]) + float(ring[1][1])) / 2.0]
            if _point_in_geometry(p, geometry):
                return p
        return centroid
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


@router.post("/resolve")
def resolve_parcel(payload: Dict[str, Any], user: Dict[str, Any] = Depends(get_current_user)):
    """Resolve an authorized parcel without persisting anything."""
    rows = _visible_properties(user)
    candidates: List[Any] = []
    property_id = str(payload.get("property_id") or payload.get("parcel_id") or "").strip()
    survey = str(payload.get("survey_number") or payload.get("survey") or payload.get("khasra") or "").strip()
    village = str(payload.get("village") or "").strip()
    district = str(payload.get("district") or "").strip()

    if property_id:
        candidates = [row for row in rows if str(row["property_id"]) == property_id or str(row["parcel_id"]) == property_id]
    else:
        candidates = _filter_rows(rows, q=survey, village=village, district=district, survey=survey)

    if not candidates:
        raise HTTPException(status_code=404, detail="No authorized parcel matched the supplied identity.")
    if len(candidates) > 1 and not village:
        return {"status": "AMBIGUOUS", "matches": [_row_payload(row) for row in candidates[:25]], "total": len(candidates)}
    if len(candidates) > 1:
        exact = [row for row in candidates if village and _norm(row["village"]) == _norm(village)]
        if len(exact) == 1:
            candidates = exact
        elif len(exact) > 1:
            return {"status": "AMBIGUOUS", "matches": [_row_payload(row) for row in exact[:25]], "total": len(exact)}

    item = _row_payload(candidates[0])
    return {
        "status": "RESOLVED",
        "persisted": False,
        "read_only": True,
        "parcel": item,
    }


@router.post("/locate")
def locate_point(payload: Dict[str, Any], user: Dict[str, Any] = Depends(get_current_user)):
    """Return the authorized parcel containing a map click, if any."""
    try:
        point = [float(payload["longitude"]), float(payload["latitude"])]
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="latitude and longitude are required.")
    for row in _visible_properties(user):
        geometry = _json(row["geometry"])
        if _point_in_geometry(point, geometry):
            return {"status": "RESOLVED", "persisted": False, "read_only": True, "parcel": _row_payload(row)}
    return {"status": "NOT_FOUND", "message": "No authorized parcel found at this location."}
