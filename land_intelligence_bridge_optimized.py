"""Indexed saved-document investigation routes.

Avoids loading every saved document into Python. Candidate related records are retrieved
from SQL using the source record's strongest land identifiers, then ownership analysis
runs only on that bounded set.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException
from server import get_db, get_current_user, log_audit
from land_intelligence import _field_value, _record_year, _is_transfer_document, analyze_ownership_history
from land_intelligence_optimized import resolve_indexed

router = APIRouter(prefix="/api/land/intelligence", tags=["Land Intelligence"])


def _doc(row: Any) -> Dict[str, Any]:
    d=dict(row)
    try: d["fields"]=json.loads(d.get("fields") or "{}")
    except Exception: d["fields"]={}
    return d


def _v(fields: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value=_field_value(fields,key)
        if value: return value
    return ""


def _identity_values(d: Dict[str, Any]) -> Dict[str,str]:
    f=d.get("fields") or {}
    return {"survey_number":_v(f,"survey_number"),"gat_number":_v(f,"gat_number"),"khasra_number":_v(f,"khasra_number"),
            "village":_v(f,"village"),"taluka":_v(f,"taluka","tehsil"),"district":_v(f,"district"),
            "sub_division":_v(f,"sub_division","subdivision")}


def _related_docs(source: Dict[str,Any]) -> List[Dict[str,Any]]:
    identity=_identity_values(source)
    clauses=[]; params=[]
    for key in ("survey_number","gat_number","khasra_number","village"):
        value=identity.get(key)
        if value:
            clauses.append(f"LOWER(TRIM(json_extract(fields, '$.{key}'))) = LOWER(TRIM(?))")
            params.append(value)
    # SQLite JSON1 is normally available; if a deployment lacks it, fall back to a
    # bounded scan of only location candidates rather than the entire documents table.
    rows=[]
    with get_db() as db:
        try:
            if clauses:
                rows=db.execute("SELECT id,filename,doc_type,status,fields,mean_conf,created_at FROM documents WHERE id<>? AND ("+" OR ".join(clauses)+") ORDER BY created_at ASC LIMIT 100", tuple([source.get("id")]+params)).fetchall()
        except Exception:
            location=_identity_values(source).get("village") or _identity_values(source).get("district")
            if location:
                rows=db.execute("SELECT id,filename,doc_type,status,fields,mean_conf,created_at FROM documents WHERE id<>? ORDER BY created_at ASC LIMIT 250",(source.get("id"),)).fetchall()
    out=[]
    for row in rows:
        d=_doc(row); sf=identity; dv=_identity_values(d)
        matches=[k for k in sf if sf[k] and dv[k] and sf[k].strip().lower()==dv[k].strip().lower()]
        strong=[k for k in ("survey_number","gat_number","khasra_number") if k in matches]
        same_village=bool(sf["village"] and dv["village"] and sf["village"].strip().lower()==dv["village"].strip().lower())
        if strong or (len(matches)>=2 and same_village):
            f=d.get("fields") or {}
            out.append({"id":d.get("id"),"filename":d.get("filename"),"doc_type":d.get("doc_type") or "Land Record","status":d.get("status"),
                        "owner":_v(f,"owner_name","owner"),"survey":_v(f,"survey_number","gat_number","khasra_number"),"village":_v(f,"village"),
                        "taluka":_v(f,"taluka","tehsil"),"district":_v(f,"district"),"area":_v(f,"area"),"year":_record_year(d),
                        "matched_fields":matches,"transfer_document":_is_transfer_document(d)})
    out.sort(key=lambda x:((x.get("year") is None),x.get("year") or 9999,str(x.get("id"))))
    return out[:50]


def _load(document_id: str):
    with get_db() as db:
        row=db.execute("SELECT id,filename,doc_type,status,fields,mean_conf,created_at FROM documents WHERE id=?",(document_id,)).fetchone()
    if not row: raise HTTPException(404,"Document record not found.")
    source=_doc(row); fields=source.get("fields") or {}; resolution=resolve_indexed(fields)
    matches=resolution.get("matches") or []; prop=matches[0].get("property") if matches else None
    related=_related_docs(source)
    return source,fields,resolution,matches,prop,related


def _document_payload(source,fields):
    return {"id":source.get("id"),"filename":source.get("filename"),"doc_type":source.get("doc_type") or "Land Record","status":source.get("status"),
            "fields":fields,"owner":_v(fields,"owner_name","owner"),"survey":_v(fields,"survey_number","gat_number","khasra_number"),
            "village":_v(fields,"village"),"taluka":_v(fields,"taluka","tehsil"),"district":_v(fields,"district"),"area":_v(fields,"area"),
            "confidence":source.get("mean_conf"),"year":_record_year(source)}


def _base_investigation(resolution,prop,full=True):
    return {"source_of_truth":"saved_document_record","second_upload_required":False,
            "human_verification_required":resolution.get("status")!="MATCH","property_link_persisted":bool(prop and prop.get("property_id")),
            "legal_authority":False,"data_semantics":"Document evidence and project property candidates; not authoritative cadastral/legal proof.",
            "loading_mode":"full_investigation" if full else "fast_first_paint"}


@router.get("/document/{document_id}")
def investigate_saved_document(document_id:str,user=Depends(get_current_user)):
    source,fields,resolution,matches,prop,related=_load(document_id)
    docs=[source]
    related_ids=[str(x.get("id")) for x in related]
    if related_ids:
        with get_db() as db:
            placeholders=",".join("?" for _ in related_ids)
            rows=db.execute(f"SELECT id,filename,doc_type,status,fields,mean_conf,created_at FROM documents WHERE id IN ({placeholders})",tuple(related_ids)).fetchall()
        docs.extend(_doc(r) for r in rows)
    history=analyze_ownership_history(docs)
    linked_now=False
    if prop and prop.get("property_id"):
        with get_db() as db:
            exists=db.execute("SELECT 1 FROM property_documents WHERE property_id=? AND document_id=?",(prop["property_id"],document_id)).fetchone()
            if not exists:
                db.execute("INSERT INTO property_documents(property_id,document_id,source_type,linked_at) VALUES (?,?,?,?)",(prop["property_id"],document_id,"document_screening_resolution",time.time())); linked_now=True
            for d in docs[1:]:
                db.execute("INSERT OR IGNORE INTO property_documents(property_id,document_id,source_type,linked_at) VALUES (?,?,?,?)",(prop["property_id"],d.get("id"),"document_screening_resolution",time.time()))
        if linked_now:
            try: log_audit(user.get("full_name",user.get("email","user")),"DOCUMENT_PROPERTY_LINKED",f"Linked saved Document Screening record {document_id} to property {prop['property_id']}.",str(prop["property_id"]))
            except Exception: pass
    timeline=[]
    for r in sorted(docs,key=lambda x:((_record_year(x) is None),_record_year(x) or 9999,str(x.get("id")))):
        f=r.get("fields") or {}
        timeline.append({"document_id":r.get("id"),"filename":r.get("filename"),"doc_type":r.get("doc_type") or "Land Record","year":_record_year(r),
                         "owner":_v(f,"owner_name","owner"),"survey":_v(f,"survey_number","gat_number","khasra_number"),"village":_v(f,"village"),
                         "taluka":_v(f,"taluka","tehsil"),"district":_v(f,"district"),"area":_v(f,"area"),"status":r.get("status"),"transfer_document":_is_transfer_document(r)})
    findings=[{"type":f.get("type"),"severity":f.get("severity"),"title":f.get("title"),"reason":f.get("reason"),"from_document":f.get("from_document"),"to_document":f.get("to_document"),"human_action":f.get("human_action"),"evidence":f.get("evidence",[])} for f in history.get("findings",[])]
    return {"document":_document_payload(source,fields),"resolution":resolution,"matches":matches,"property":prop,"related_documents":related,"related_count":len(related),
            "timeline":timeline,"ownership_history":history,"findings":findings,"investigation":{**_base_investigation(resolution,prop),"human_verification_required":resolution.get("status")!="MATCH" or bool(findings)}}
