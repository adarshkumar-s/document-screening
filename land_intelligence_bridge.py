"""Document Screening -> Land Intelligence integration.

A saved Document Screening record is the source of truth. This bridge resolves that
record to a property candidate, discovers related saved records, and exposes the
investigation view without requiring a second upload.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple
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
    # tehsil is accepted as an input alias; taluka is the canonical Land Intelligence field.
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


@router.get("/document/{document_id}")
def investigate_saved_document(document_id: str, user=Depends(get_current_user)):
    with get_db() as db:
        row = db.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Document record not found.")
        all_rows = db.execute("SELECT id,filename,doc_type,status,fields,mean_conf,created_at,ocr_text FROM documents ORDER BY created_at ASC").fetchall()
    source = _doc(row)
    all_docs = [_doc(r) for r in all_rows]
    fields = source.get("fields") or {}
    resolution = _resolve(fields)
    matches = resolution.get("matches") or []
    top_match = matches[0] if matches else None
    prop = _property_from_match(top_match)
    related = _related_docs(source, all_docs)

    # Build the investigation set from the same saved screening records. This is deliberately
    # independent of the synthetic property table, so the investigation remains document-grounded.
    investigation_records = [source]
    seen = {str(source.get("id"))}
    for item in related:
        did = str(item.get("id"))
        if did in seen:
            continue
        match = next((d for d in all_docs if str(d.get("id")) == did), None)
        if match:
            investigation_records.append(match)
            seen.add(did)
    history = analyze_ownership_history(investigation_records)

    linked_now = False
    if prop and prop.get("property_id"):
        with get_db() as db:
            linked_now = _ensure_link(db, str(prop["property_id"]), str(document_id))
            # Persist the other related documents to the same resolved property when they
            # independently share the same strong land identity.
            for item in related:
                if item.get("id") is None:
                    continue
                rd = next((d for d in all_docs if str(d.get("id")) == str(item["id"])), None)
                if not rd:
                    continue
                rv = _identity_values(rd)
                pv = _identity_values(source)
                strong = any(pv[k] and rv[k] and _norm(pv[k]) == _norm(rv[k]) for k in ("survey_number", "gat_number", "khasra_number"))
                if strong:
                    _ensure_link(db, str(prop["property_id"]), str(item["id"]))
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
            "confidence": source.get("mean_conf") if source.get("mean_conf") is not None else source.get("ocr_confidence"),
            "year": _record_year(source),
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
