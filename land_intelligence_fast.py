"""Fast saved-document payload for Land Intelligence first paint."""
from __future__ import annotations

import json
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Depends
from server import get_db, get_current_user
from land_intelligence import _field_value, _record_year
from land_intelligence_optimized import resolve_indexed

router = APIRouter(prefix="/api/land/intelligence", tags=["Land Intelligence"])


def _v(fields: Dict[str, Any], *keys: str) -> str:
    for k in keys:
        value = _field_value(fields, k)
        if value:
            return value
    return ""


def _doc(row: Any) -> Dict[str, Any]:
    d = dict(row)
    try:
        d["fields"] = json.loads(d.get("fields") or "{}")
    except Exception:
        d["fields"] = {}
    return d


@router.get("/fast-document/{document_id}")
def fast_saved_document(document_id: str, user=Depends(get_current_user)):
    with get_db() as db:
        row = db.execute(
            "SELECT id,filename,doc_type,status,fields,mean_conf,created_at FROM documents WHERE id=?",
            (document_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Document record not found.")

    source = _doc(row)
    fields = source.get("fields") or {}
    resolution = resolve_indexed(fields)
    matches = resolution.get("matches") or []
    prop = matches[0].get("property") if matches else None

    return {
        "document": {
            "id": source.get("id"), "filename": source.get("filename"),
            "doc_type": source.get("doc_type") or "Land Record", "status": source.get("status"),
            "fields": fields, "owner": _v(fields, "owner_name", "owner"),
            "survey": _v(fields, "survey_number", "gat_number", "khasra_number"),
            "village": _v(fields, "village"), "taluka": _v(fields, "taluka", "tehsil"),
            "district": _v(fields, "district"), "area": _v(fields, "area"),
            "confidence": source.get("mean_conf"), "year": _record_year(source),
        },
        "resolution": resolution, "matches": matches, "property": prop,
        "related_documents": [], "related_count": 0, "timeline": [],
        "ownership_history": {}, "findings": [],
        "investigation": {
            "source_of_truth": "saved_document_record", "second_upload_required": False,
            "human_verification_required": resolution.get("status") != "MATCH",
            "property_link_persisted": False, "legal_authority": False,
            "data_semantics": "Document evidence and project property candidates; not authoritative cadastral/legal proof.",
            "loading_mode": "fast_first_paint",
        },
    }
