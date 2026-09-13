"""Indexed candidate retrieval for Land Intelligence.

The legacy resolver scans every parcel in Python. This module narrows candidates in SQL
using the strongest available land identifiers, then applies the existing scoring logic
only to that small candidate set.
"""
from __future__ import annotations

from typing import Any, Dict, List

from land_intelligence import _field, _normal, _property
from server import get_db

IDENTIFIER_KEYS = ("survey_number", "gat_number", "khasra_number", "sub_division", "village", "taluka", "district")
STRONG_KEYS = ("survey_number", "gat_number", "khasra_number")
WEIGHTS = {
    "survey_number": 3, "gat_number": 3, "khasra_number": 3,
    "sub_division": 2, "village": 1, "taluka": 1, "district": 1,
}


def _candidate_rows(fields: Dict[str, Any]) -> List[Dict[str, Any]]:
    supplied = {k: _field(fields, k) for k in IDENTIFIER_KEYS if _field(fields, k)}
    if not supplied:
        return []

    clauses: List[str] = []
    params: List[Any] = []
    # Use exact normalized SQL equality for identity fields. This keeps the query
    # index-friendly and prevents dangerous fuzzy matches such as 103/2 vs 103/20.
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
    # OR retrieval gives a bounded candidate set; scoring below decides the winner.
    # A cap protects the request path if a source field is unexpectedly non-selective.
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
    tolerance = 0.05
    import os
    try:
        tolerance = float(os.getenv("LAND_AREA_TOLERANCE_HA", "0.05"))
    except ValueError:
        pass

    rows = _candidate_rows(fields)
    scored = []
    for row in rows:
        score = 0.0
        max_score = 0.0
        reasons: List[str] = []
        missing: List[str] = []
        conflicts: List[str] = []
        strong_conflict = False
        for key, value in supplied.items():
            weight = WEIGHTS[key]
            max_score += weight
            row_value = row.get(key)
            if not row_value:
                missing.append(key)
            elif _normal(row_value) == _normal(value):
                score += weight
                reasons.append(f"{key.replace('_',' ')} matched")
            else:
                conflicts.append(key)
                if key in STRONG_KEYS:
                    strong_conflict = True
        if area_num is not None and row.get("area") is not None:
            max_score += 1
            if abs(area_num - float(row["area"])) <= tolerance:
                score += 1
                reasons.append(f"area within {tolerance:g} tolerance")
            else:
                conflicts.append("area")
        elif area_num is not None:
            max_score += 1
            missing.append("area")
        if max_score > 0 and score > 0:
            scored.append((round((score / max_score) * 100), row, reasons, missing, conflicts, strong_conflict))

    scored.sort(key=lambda x: (x[0], len(x[2])), reverse=True)
    if not scored:
        return {"status":"NO MATCH","confidence":0,"matches":[],"reasons":["No parcel matched the supplied property identity or location fields."]}

    matches = []
    for pct, row, reasons, missing, conflicts, _ in scored[:5]:
        if pct >= 50:
            matches.append({"property": _property(row, False), "confidence": pct / 100,
                            "reasons": reasons, "missing_fields": missing, "conflicting_fields": conflicts})
    top_pct, _, top_reasons, top_missing, top_conflicts, top_strong_conflict = scored[0]
    if top_pct >= 95 and not top_strong_conflict and not top_conflicts:
        status = "MATCH"
    elif top_pct >= 50 and not top_strong_conflict:
        status = "POSSIBLE MATCH"
    else:
        status = "NO MATCH"
    return {"status":status,"confidence":top_pct/100,"matches":matches,"reasons":top_reasons,
            "missing_fields":top_missing,"conflicting_fields":top_conflicts}
