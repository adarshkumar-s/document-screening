"""Indexed candidate retrieval for Land Intelligence."""
from __future__ import annotations

import threading
from typing import Any, Dict, List

from land_intelligence import _field, _normal, _property
from server import get_db

IDENTIFIER_KEYS = ("survey_number", "gat_number", "khasra_number", "sub_division", "village", "taluka", "district")
STRONG_KEYS = ("survey_number", "gat_number", "khasra_number")
WEIGHTS = {"survey_number":3,"gat_number":3,"khasra_number":3,"sub_division":2,"village":1,"taluka":1,"district":1}
_index_lock = threading.Lock()
_indexes_ready = False


def ensure_property_indexes() -> None:
    """Create resolver indexes during application startup, never per HTTP request."""
    global _indexes_ready
    if _indexes_ready:
        return
    with _index_lock:
        if _indexes_ready:
            return
        with get_db() as db:
            for name, column in (
                ("idx_properties_survey_norm", "survey_number"),
                ("idx_properties_gat_norm", "gat_number"),
                ("idx_properties_khasra_norm", "khasra_number"),
                ("idx_properties_subdivision_norm", "sub_division"),
                ("idx_properties_village_norm", "village"),
                ("idx_properties_taluka_norm", "taluka"),
                ("idx_properties_district_norm", "district"),
            ):
                db.execute(f"CREATE INDEX IF NOT EXISTS {name} ON properties (LOWER(TRIM({column})))")
        _indexes_ready = True


def _candidate_rows(fields: Dict[str, Any]) -> List[Dict[str, Any]]:
    supplied = {k: _field(fields, k) for k in IDENTIFIER_KEYS if _field(fields, k)}
    if not supplied:
        return []
    clauses: List[str] = []
    params: List[Any] = []
    for key in STRONG_KEYS:
        value = supplied.get(key)
        if value:
            clauses.append(f"LOWER(TRIM({key})) = LOWER(TRIM(?))")
            params.append(value)
    if not any(k in supplied for k in STRONG_KEYS):
        for key in ("village", "taluka", "district"):
            value = supplied.get(key)
            if value:
                clauses.append(f"LOWER(TRIM({key})) = LOWER(TRIM(?))")
                params.append(value)
    if not clauses:
        return []
    query = "SELECT * FROM properties WHERE " + " OR ".join(clauses) + " LIMIT 250"
    with get_db() as db:
        return [dict(r) for r in db.execute(query, tuple(params)).fetchall()]


def resolve_indexed(fields: Dict[str, Any]) -> Dict[str, Any]:
    supplied = {k: _field(fields, k) for k in IDENTIFIER_KEYS if _field(fields, k)}
    area_value = _field(fields, "area")
    if not supplied and not area_value:
        return {"status":"INSUFFICIENT DATA","confidence":0,"matches":[],"reasons":["No property identifiers, location fields, or area were extracted."]}
    try:
        area_num = float(str(area_value).replace(",", "").split()[0]) if area_value else None
    except (TypeError, ValueError):
        area_num = None
    import os
    try: tolerance = float(os.getenv("LAND_AREA_TOLERANCE_HA", "0.05"))
    except ValueError: tolerance = 0.05
    rows = _candidate_rows(fields)
    scored = []
    for row in rows:
        score = 0.0; max_score = 0.0; reasons=[]; missing=[]; conflicts=[]; strong_conflict=False
        for key, value in supplied.items():
            weight=WEIGHTS[key]; max_score += weight; row_value=row.get(key)
            if not row_value: missing.append(key)
            elif _normal(row_value) == _normal(value): score += weight; reasons.append(f"{key.replace('_',' ')} matched")
            else:
                conflicts.append(key)
                if key in STRONG_KEYS: strong_conflict=True
        if area_num is not None and row.get("area") is not None:
            max_score += 1
            if abs(area_num-float(row["area"])) <= tolerance: score += 1; reasons.append(f"area within {tolerance:g} tolerance")
            else: conflicts.append("area")
        elif area_num is not None: max_score += 1; missing.append("area")
        if max_score > 0 and score > 0:
            scored.append((round((score/max_score)*100),row,reasons,missing,conflicts,strong_conflict))
    scored.sort(key=lambda x:(x[0],len(x[2])),reverse=True)
    if not scored:
        return {"status":"NO MATCH","confidence":0,"matches":[],"reasons":["No parcel matched the supplied property identity or location fields."]}
    matches=[]
    for pct,row,reasons,missing,conflicts,_ in scored[:5]:
        if pct>=50:
            matches.append({"property":_property(row,False),"confidence":pct/100,"reasons":reasons,"missing_fields":missing,"conflicting_fields":conflicts})
    top_pct,_,top_reasons,top_missing,top_conflicts,top_strong_conflict=scored[0]
    status="MATCH" if top_pct>=95 and not top_strong_conflict and not top_conflicts else ("POSSIBLE MATCH" if top_pct>=50 and not top_strong_conflict else "NO MATCH")
    return {"status":status,"confidence":top_pct/100,"matches":matches,"reasons":top_reasons,"missing_fields":top_missing,"conflicting_fields":top_conflicts}
