"""Fast bridge from saved Document Screening records to Land Intelligence."""
from __future__ import annotations
import json
from typing import Any, Dict, List
from fastapi import APIRouter, HTTPException, Depends
from server import get_db, get_current_user
from land_intelligence import _resolve, _field_value

router = APIRouter(prefix="/api/land/intelligence", tags=["Land Intelligence"])

def _doc(row: Any) -> Dict[str, Any]:
    d=dict(row)
    try: d["fields"]=json.loads(d.get("fields") or "{}")
    except Exception: d["fields"]={}
    return d

def _v(fields: Dict[str,Any], *keys: str) -> str:
    for k in keys:
        x=_field_value(fields,k)
        if x: return x
    return ""

def _related_docs(source: Dict[str,Any], rows: List[Dict[str,Any]]) -> List[Dict[str,Any]]:
    sf=source.get("fields") or {}
    keys=("survey_number","gat_number","khasra_number","village","tehsil","district")
    sv={k:_field_value(sf,k).strip().lower() for k in keys}
    out=[]
    for d in rows:
        if str(d.get("id"))==str(source.get("id")): continue
        f=d.get("fields") or {}
        matches=[k for k in keys if sv[k] and _field_value(f,k).strip().lower()==sv[k]]
        if any(k in matches for k in ("survey_number","gat_number","khasra_number")) or len(matches)>=2:
            out.append({"id":d.get("id"),"filename":d.get("filename"),"doc_type":d.get("doc_type") or "Land Record","status":d.get("status"),"owner":_v(f,"owner_name","owner"),"survey":_v(f,"survey_number","gat_number","khasra_number"),"village":_v(f,"village"),"taluka":_v(f,"tehsil","taluka"),"district":_v(f,"district"),"matched_fields":matches})
    return out[:20]

@router.get("/document/{document_id}")
def investigate_saved_document(document_id: str, _user=Depends(get_current_user)):
    with get_db() as db:
        row=db.execute("SELECT * FROM documents WHERE id=?",(document_id,)).fetchone()
        if not row: raise HTTPException(404,"Document record not found.")
        rows=[dict(r) for r in db.execute("SELECT id,filename,doc_type,status,fields,mean_conf FROM documents ORDER BY created_at ASC").fetchall()]
    source=_doc(row); all_docs=[_doc(r) for r in rows]; fields=source.get("fields") or {}
    resolution=_resolve(fields); matches=resolution.get("matches") or []
    prop=matches[0].get("property") if matches and isinstance(matches[0],dict) else None
    if prop and isinstance(prop,dict): prop=dict(prop); prop["geometry"]=None
    related=_related_docs(source,all_docs)
    return {"document":{"id":source.get("id"),"filename":source.get("filename"),"doc_type":source.get("doc_type") or "Land Record","status":source.get("status"),"fields":fields,"owner":_v(fields,"owner_name","owner"),"survey":_v(fields,"survey_number","gat_number","khasra_number"),"village":_v(fields,"village"),"taluka":_v(fields,"tehsil","taluka"),"district":_v(fields,"district"),"area":_v(fields,"area"),"confidence":source.get("mean_conf") if source.get("mean_conf") is not None else source.get("ocr_confidence")},"resolution":resolution,"matches":matches,"property":prop,"related_documents":related,"related_count":len(related),"reasons":resolution.get("reasons") or [],"investigation":{"source_of_truth":"saved_document_record","second_upload_required":False,"human_verification_required":resolution.get("status")!="MATCH"}}
