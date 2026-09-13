"""Bridge saved Document Screening records into Land Intelligence.

The bridge never requires a second upload. It reads the already-persisted document,
resolves parcel identity with the existing Land Intelligence matcher, and returns
related saved records as investigation context.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException

from server import get_db, get_current_user
from land_intelligence import _resolve, _field_value

router = APIRouter(prefix="/api/land/intelligence", tags=["Land Intelligence"])


def _doc(row: Any) -> Dict[str, Any]:
    d = dict(row)
    try:
        d["fields"] = json.loads(d.get("fields") or "{}")
    except Exception:
        d["fields"] = {}
    return d


def _related_docs(source: Dict[str, Any], all_docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    sf = source.get("fields") or {}
    keys = ("survey_number", "gat_number", "khasra_number", "village", "tehsil", "district")
    sv = {_key: _field_value(sf, _key).lower() for _key in keys}
    out = []
    for d in all_docs:
        if str(d.get("id")) == str(source.get("id")):
            continue
        f = d.get("fields") or {}
        matches = [k for k in keys if sv[k] and _field_value(f, k).lower() == sv[k]]
        strong = [k for k in matches if k in ("survey_number", "gat_number", "khasra_number")]
        if strong or len(matches) >= 2:
            out.append({
                "id": d.get("id"),
                "filename": d.get("filename"),
                "doc_type": d.get("doc_type") or "Land Record",
                "status": d.get("status"),
                "owner": _field_value(f, "owner_name"),
                "survey": _field_value(f, "survey_number") or _field_value(f, "khasra_number"),
                "village": _field_value(f, "village"),
                "district": _field_value(f, "district"),
                "confidence": d.get("mean_conf") if d.get("mean_conf") is not None else d.get("ocr_confidence"),
                "matched_fields": matches,
            })
    return out[:20]


@router.get("/document/{document_id}")
def investigate_saved_document(document_id: str, _user=__import__("fastapi").Depends(get_current_user)):
    with get_db() as db:
        row = db.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Document record not found.")
        rows = db.execute("SELECT * FROM documents ORDER BY created_at ASC").fetchall()
    source = _doc(row)
    all_docs = [_doc(r) for r in rows]
    fields = source.get("fields") or {}
    resolution = _resolve(fields)
    related = _related_docs(source, all_docs)
    property_match = (resolution.get("matches") or [None])[0]
    return {
        "document": {
            "id": source.get("id"),
            "filename": source.get("filename"),
            "doc_type": source.get("doc_type") or "Land Record",
            "status": source.get("status"),
            "owner": _field_value(fields, "owner_name"),
            "survey": _field_value(fields, "survey_number") or _field_value(fields, "khasra_number"),
            "village": _field_value(fields, "village"),
            "district": _field_value(fields, "district"),
            "taluka": _field_value(fields, "tehsil") or _field_value(fields, "taluka"),
            "area": _field_value(fields, "area"),
            "confidence": source.get("mean_conf") if source.get("mean_conf") is not None else source.get("ocr_confidence"),
        },
        "resolution": resolution,
        "matches": resolution.get("matches") or [],
        "property": property_match.get("property") if property_match else None,
        "related_documents": related,
        "related_count": len(related),
        "reasons": resolution.get("reasons") or [],
        "investigation": {
            "source_of_truth": "saved_document_record",
            "second_upload_required": False,
            "human_verification_required": resolution.get("status") != "MATCH",
        },
    }
