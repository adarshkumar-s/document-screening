"""Document Screening -> Land Intelligence integration.

A saved Document Screening record is the source of truth. This bridge resolves that
record to a property candidate, discovers related saved records, and exposes the
investigation view without requiring a second upload.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List
from fastapi import APIRouter, HTTPException, Depends
from server import get_db, get_current_user, log_audit
from land_intelligence import _resolve, _field_value, analyze_ownership_history, _record_year, _is_transfer_document

router = APIRouter(prefix="/api/land/intelligence", tags=["Land Intelligence"])


def _doc(row: Any) -> Dict[str, Any]:
    d = dict(row)
    try:
        d["fields"] = json.loads(d.get("fields") or "{}")
    except Exception:
        d["fields"] = {}
    return d


def _v(fields: Dict[str, Any], *keys: str) -> str:
    for k in keys:
        x = _field_value(fields, k)
        if x:
            return x
    return ""


def _field(fields: Dict[str, Any], *keys: str) -> str:
    return _v(fields, *keys)


def _identity_values(d: Dict[str, Any]) -> Dict[str, str]:
    f = d.get("fields") or {}
    return {
        "survey_number": _field(f, "survey_number"),
        "gat_number": _field(f, "gat_number"),
        "khasra_number": _field(f, "khasra_number"),
        "village": _field(f, "village"),
        "taluka": _field(f, "taluka", "tehsil"),
        "district": _field(f, "district"),
        "sub_division": _field(f, "sub_division", "subdivision"),
    }


def _norm(v: Any) -> str:
    return " ".join(str(v or "").strip().lower().split())


def _related_docs(source: Dict[str, Any], rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    sv = _identity_values(source)
    out: List[Dict[str, Any]] = []
    for d in rows:
        if str(d.get("id")) == str(source.get("id")):
            continue
        dv = _identity_values(d)
        matches = [k for k in sv if sv[k] and dv[k] and _norm(sv[k]) == _norm(dv[k])]
        strong = [k for k in ("survey_number", "gat_number", "khasra_number") if k in matches]
        same_village = bool(sv["village"] and dv["village"] and _norm(sv["village"]) == _norm(dv["village"]))
        if strong or (len(matches) >= 2 and same_village):
            f = d.get("fields") or {}
            out.append({
                "id": d.get("id"),
                "filename": d.get("filename"),
                "doc_type": d.get("doc_type") or "Land Record",
                "status": d.get("status"),
                "owner": _v(f, "owner_name", "owner"),
                "survey": _v(f, "survey_number", "gat_number", "khasra_number"),
                "village": _v(f, "village"),
                "taluka": _v(f, "taluka", "tehsil"),
                "district": _v(f, "district"),
                "area": _v(f, "area"),
                "year": _record_year(d),
                "matched_fields": matches,
                "transfer_document": _is_transfer_document(d),
            })
    out.sort(key=lambda x: ((x.get("year") is None), x.get("year") or 9999, str(x.get("id"))))
    return out[:50]


def _ensure_link(db: Any, property_id: str, document_id: str) -> bool:
    row = db.execute("SELECT 1 FROM property_documents WHERE property_id=? AND document_id=?", (property_id, document_id)).fetchone()
    if row:
        return False
    db.execute("INSERT INTO property_documents(property_id,document_id,source_type,linked_at) VALUES (?,?,?,?)",
               (property_id, document_id, "document_screening_resolution", __import__("time").time()))
    return True


def _property_from_match(match: Dict[str, Any] | None) -> Dict[str, Any] | None:
    if not match or not isinstance(match.get("property"), dict):
        return None
    p = dict(match["property"])
    p["geometry"] = p.get("geometry")
    return p


def _resolve_saved_document(document_id: str):
    """Shared lightweight resolution used by the fast page load and full investigation."""
    with get_db() as db:
        row = db.execute(
            "SELECT id,filename,doc_type,status,fields,mean_conf,created_at FROM documents WHERE id=?",
            (document_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Document record not found.")
        all_rows = db.execute(
            "SELECT id,filename,doc_type,status,fields,mean_conf,created_at FROM documents ORDER BY created_at ASC"
        ).fetchall()
    source = _doc(row)
    all_docs = [_doc(r) for r in all_rows]
    fields = source.get("fields") or {}
    resolution = _resolve(fields)
    matches = resolution.get("matches") or []
    prop = _property_from_match(matches[0] if matches else None)
    related = _related_docs(source, all_docs)
    return source, all_docs, fields, resolution, matches, prop, related


@router.get("/document/{document_id}/fast")
def investigate_saved_document_fast(document_id: str, user=Depends(get_current_user)):
    """Fast first-paint payload: source record, parcel resolution and related-record summary only."""
    source, _all_docs, fields, resolution, matches, prop, related = _resolve_saved_document(document_id)
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
        "resolution": resolution,
        "matches": matches,
        "property": prop,
        "related_documents": related,
        "related_count": len(related),
        "timeline": [],
        "ownership_history": {},
        "findings": [],
        "investigation": {
            "source_of_truth": "saved_document_record",
            "second_upload_required": False,
            "human_verification_required": resolution.get("status") != "MATCH",
            "property_link_persisted": bool(prop and prop.get("property_id")),
            "legal_authority": False,
            "data_semantics": "Document evidence and project property candidates; not authoritative cadastral/legal proof.",
        },
    }


@router.get("/document/{document_id}")
def investigate_saved_document(document_id: str, user=Depends(get_current_user)):
    source, all_docs, fields, resolution, matches, prop, related = _resolve_saved_document(document_id)

    investigation_records = [source]
    docs_by_id = {str(d.get("id")): d for d in all_docs}
    seen = {str(source.get("id"))}
    for item in related:
        did = str(item.get("id"))
        if did in seen:
            continue
        match = docs_by_id.get(did)
        if match:
            investigation_records.append(match)
            seen.add(did)
    history = analyze_ownership_history(investigation_records)

    linked_now = False
    if prop and prop.get("property_id"):
        source_identity = _identity_values(source)
        with get_db() as db:
            linked_now = _ensure_link(db, str(prop["property_id"]), str(document_id))
            for item in related:
                did = item.get("id")
                rd = docs_by_id.get(str(did))
                if not rd:
                    continue
                rv = _identity_values(rd)
                strong = any(source_identity[k] and rv[k] and _norm(source_identity[k]) == _norm(rv[k]) for k in ("survey_number", "gat_number", "khasra_number"))
                if strong:
                    _ensure_link(db, str(prop["property_id"]), str(did))
        if linked_now:
            try:
                log_audit(user.get("full_name", user.get("email", "user")), "DOCUMENT_PROPERTY_LINKED",
                          f"Linked saved Document Screening record {document_id} to property {prop['property_id']}.",
                          str(prop["property_id"]))
            except Exception:
                pass

    timeline = []
    for r in sorted(investigation_records, key=lambda x: ((_record_year(x) is None), _record_year(x) or 9999, str(x.get("id")))):
        f = r.get("fields") or {}
        timeline.append({
            "document_id": r.get("id"), "filename": r.get("filename"),
            "doc_type": r.get("doc_type") or "Land Record", "year": _record_year(r),
            "owner": _v(f, "owner_name", "owner"),
            "survey": _v(f, "survey_number", "gat_number", "khasra_number"),
            "village": _v(f, "village"), "taluka": _v(f, "taluka", "tehsil"),
            "district": _v(f, "district"), "area": _v(f, "area"),
            "status": r.get("status"), "transfer_document": _is_transfer_document(r),
        })

    finding_summary = [{
        "type": f.get("type"), "severity": f.get("severity"), "title": f.get("title"),
        "reason": f.get("reason"), "from_document": f.get("from_document"), "to_document": f.get("to_document"),
        "human_action": f.get("human_action"), "evidence": f.get("evidence", []),
    } for f in history.get("findings", [])]

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
        "resolution": resolution,
        "matches": matches,
        "property": prop,
        "related_documents": related,
        "related_count": len(related),
        "timeline": timeline,
        "ownership_history": history,
        "findings": finding_summary,
        "investigation": {
            "source_of_truth": "saved_document_record",
            "second_upload_required": False,
            "human_verification_required": resolution.get("status") != "MATCH" or bool(finding_summary),
            "property_link_persisted": bool(prop and prop.get("property_id")),
            "legal_authority": False,
            "data_semantics": "Document evidence and project property candidates; not authoritative cadastral/legal proof.",
        },
    }
