"""Document-first mapping API: only uses data already extracted from uploaded documents."""
from __future__ import annotations
import json, re
from typing import Any, Dict, Optional, Tuple
from fastapi import APIRouter, Depends, HTTPException
from server import get_current_user
from server import get_db

router = APIRouter(prefix="/api/land", tags=["Document Mapping"])


def _value(fields: Dict[str, Any], *keys: str):
    for key in keys:
        v = fields.get(key)
        if isinstance(v, dict): v = v.get("value") or v.get("text")
        if v not in (None, ""): return v
    return None


def _num(v):
    try: return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError): return None


def _coords(fields, text):
    lat, lon = _num(_value(fields,"latitude","lat")), _num(_value(fields,"longitude","lon","lng"))
    if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180: return lat, lon
    m = re.search(r"(?:lat(?:itude)?\s*[:=]\s*)(-?\d+(?:\.\d+)?)\D+(?:lon(?:gitude)?|lng)\s*[:=]\s*(-?\d+(?:\.\d+)?)", text or "", re.I)
    m = m or re.search(r"\b(-?\d{1,2}\.\d{3,})\s*[,;/]\s*(-?\d{2,3}\.\d{3,})\b", text or "")
    if not m: return None
    a,b=_num(m.group(1)),_num(m.group(2))
    return (a,b) if a is not None and b is not None and -90 <= a <= 90 and -180 <= b <= 180 else None


def _geometry(fields):
    raw=_value(fields,"geometry","geojson","geo_json")
    if isinstance(raw,str):
        try: raw=json.loads(raw)
        except Exception: return None
    if isinstance(raw,dict) and raw.get("type")=="Feature": raw=raw.get("geometry")
    return raw if isinstance(raw,dict) and raw.get("type") in {"Point","MultiPoint","LineString","MultiLineString","Polygon","MultiPolygon"} else None


def _bbox(g,p):
    pts=[[p[1],p[0]]] if p else []
    def walk(v):
        if isinstance(v,(list,tuple)):
            if len(v)>=2 and all(isinstance(x,(int,float)) for x in v[:2]): pts.append([float(v[0]),float(v[1])])
            else:
                for x in v: walk(x)
    if g: walk(g.get("coordinates"))
    if not pts: return None
    xs,ys=zip(*pts); return [min(xs),min(ys),max(xs),max(ys)]


def _doc(document_id):
    with get_db() as db:
        row=db.execute("SELECT id,filename,doc_type,status,fields,ocr_text,cleaned_ocr_text,mean_conf,created_at,updated_at FROM documents WHERE id=?",(document_id,)).fetchone()
    if not row: raise HTTPException(404,"Document record not found.")
    d=dict(row)
    try: d["fields"]=json.loads(d.get("fields") or "{}")
    except Exception: d["fields"]={}
    return d


def _mapping(d):
    f=d.get("fields") or {}; p=_coords(f,d.get("cleaned_ocr_text") or d.get("ocr_text") or ""); g=_geometry(f)
    loc={k:_value(f,*keys) for k,keys in {
        "district":("district",),"taluka":("taluka","tehsil"),"village":("village",),
        "survey_number":("survey_number",),"gat_number":("gat_number",),"khasra_number":("khasra_number",),
        "sub_division":("sub_division","subdivision"),"address":("address","property_address","location")}.items()}
    return {"available":bool(p or g),"method":"document_geometry" if g else ("document_coordinates" if p else "none"),"coordinates":{"latitude":p[0],"longitude":p[1]} if p else None,"geometry":g,"bbox":_bbox(g,p),"crs":_value(f,"crs","coordinate_reference_system") or ("EPSG:4326" if p else None),"location_fields":loc}


@router.get("/document-map/{document_id}")
def document_map(document_id:str,user=Depends(get_current_user)):
    d=_doc(document_id); m=_mapping(d)
    return {"document":{"id":d["id"],"filename":d.get("filename"),"doc_type":d.get("doc_type"),"status":d.get("status"),"confidence":d.get("mean_conf"),"created_at":d.get("created_at"),"updated_at":d.get("updated_at")},"source":"uploaded_document_record","mapping":{**m,"message":"Derived only from the uploaded document." if m["available"] else "No coordinates or geometry found. No location is fabricated."}}


@router.get("/document-map/documents")
def document_map_documents(q:Optional[str]=None,status:Optional[str]=None,mapped:Optional[bool]=None,user=Depends(get_current_user)):
    with get_db() as db: rows=db.execute("SELECT id,filename,doc_type,status,fields,ocr_text,cleaned_ocr_text,mean_conf,created_at,updated_at FROM documents ORDER BY id DESC").fetchall()
    out=[]; q=(q or "").lower().strip(); status=(status or "").lower().strip()
    for row in rows:
        d=dict(row)
        try:d["fields"]=json.loads(d.get("fields") or "{}")
        except Exception:d["fields"]={}
        m=_mapping(d); f=d["fields"]
        hay=" ".join(str(x or "") for x in [d.get("id"),d.get("filename"),d.get("doc_type"),d.get("status"),f.get("owner_name"),f.get("survey_number"),f.get("khasra_number"),f.get("village"),f.get("taluka"),f.get("district")]).lower()
        if q and q not in hay: continue
        if status and str(d.get("status") or "").lower()!=status: continue
        if mapped is True and not m["available"]: continue
        if mapped is False and m["available"]: continue
        out.append({"id":d["id"],"filename":d.get("filename"),"doc_type":d.get("doc_type"),"status":d.get("status"),"confidence":d.get("mean_conf"),"created_at":d.get("created_at"),"location":m["location_fields"],"mapped":m["available"],"mapping_method":m["method"]})
    return {"documents":out,"total":len(out)}


@router.get("/document-map/summary")
def document_map_summary(user=Depends(get_current_user)):
    with get_db() as db: rows=db.execute("SELECT id,filename,doc_type,status,fields,ocr_text,cleaned_ocr_text,mean_conf,created_at,updated_at FROM documents").fetchall()
    docs=[]
    for r in rows:
        d=dict(r)
        try:d["fields"]=json.loads(d.get("fields") or "{}")
        except Exception:d["fields"]={}
        docs.append(d)
    statuses={}
    for d in docs: statuses[str(d.get("status") or "UNKNOWN")]=statuses.get(str(d.get("status") or "UNKNOWN"),0)+1
    mapped=sum(1 for d in docs if _mapping(d)["available"]); low=sum(1 for d in docs if _num(d.get("mean_conf")) is not None and _num(d.get("mean_conf"))<70)
    return {"total_documents":len(docs),"mapped_documents":mapped,"unmapped_documents":len(docs)-mapped,"low_confidence":low,"status_counts":statuses}
