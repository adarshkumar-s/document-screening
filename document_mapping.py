"""Document-first mapping API grounded only in uploaded screening records."""
from __future__ import annotations
import json
import re
from typing import Any, Dict, Optional
from fastapi import APIRouter, Depends, HTTPException
from server import get_current_user, get_db
router = APIRouter(prefix="/api/land", tags=["Document Mapping"])

def _value(fields: Dict[str, Any], *keys: str):
    for key in keys:
        value = fields.get(key)
        if isinstance(value, dict): value = value.get("value") or value.get("text")
        if value not in (None, ""): return value
    return None

def _num(value):
    try: return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError): return None

def _coords(fields: Dict[str, Any], text: str):
    lat=_num(_value(fields,"latitude","lat")); lon=_num(_value(fields,"longitude","lon","lng"))
    if lat is not None and lon is not None and -90<=lat<=90 and -180<=lon<=180: return lat,lon
    m=re.search(r"(?:lat(?:itude)?\s*[:=]\s*)(-?\d+(?:\.\d+)?)\D+(?:lon(?:gitude)?|lng)\s*[:=\s]*(-?\d+(?:\.\d+)?)",text or "",re.I)
    m=m or re.search(r"\b(-?\d{1,2}\.\d{3,})\s*[,;/]\s*(-?\d{2,3}\.\d{3,})\b",text or "")
    if not m:return None
    a,b=_num(m.group(1)),_num(m.group(2))
    return (a,b) if a is not None and b is not None and -90<=a<=90 and -180<=b<=180 else None

def _geometry(fields):
    raw=_value(fields,"geometry","geojson","geo_json")
    if isinstance(raw,str):
        try: raw=json.loads(raw)
        except Exception:return None
    if isinstance(raw,dict) and raw.get("type")=="Feature": raw=raw.get("geometry")
    return raw if isinstance(raw,dict) and raw.get("type") in {"Point","MultiPoint","LineString","MultiLineString","Polygon","MultiPolygon"} else None

def _bbox(geometry,point):
    points=[[point[1],point[0]]] if point else []
    def walk(v):
        if isinstance(v,(list,tuple)):
            if len(v)>=2 and all(isinstance(x,(int,float)) for x in v[:2]): points.append([float(v[0]),float(v[1])])
            else:
                for item in v: walk(item)
    if geometry: walk(geometry.get("coordinates"))
    if not points:return None
    xs,ys=zip(*points);return [min(xs),min(ys),max(xs),max(ys)]

def _doc(document_id):
    with get_db() as db: row=db.execute("SELECT id,filename,doc_type,status,fields,ocr_text,cleaned_ocr_text,mean_conf,created_at,updated_at FROM documents WHERE id=?",(document_id,)).fetchone()
    if not row: raise HTTPException(404,"Document record not found.")
    d=dict(row)
    try:d["fields"]=json.loads(d.get("fields") or "{}")
    except Exception:d["fields"]={}
    return d

def _mapping(d):
    fields=d.get("fields") or {}; text=d.get("cleaned_ocr_text") or d.get("ocr_text") or ""; point=_coords(fields,text); geometry=_geometry(fields)
    location={k:_value(fields,*keys) for k,keys in {"owner_name":("owner_name",),"district":("district",),"taluka":("taluka","tehsil"),"village":("village",),"survey_number":("survey_number",),"gat_number":("gat_number",),"khasra_number":("khasra_number",),"khata_number":("khata_number",),"plot_number":("plot_number",),"sub_division":("sub_division","subdivision"),"address":("address","property_address","location")}.items()}
    return {"available":bool(point or geometry),"method":"document_geometry" if geometry else ("document_coordinates" if point else "none"),"coordinates":{"latitude":point[0],"longitude":point[1]} if point else None,"geometry":geometry,"bbox":_bbox(geometry,point),"crs":_value(fields,"crs","coordinate_reference_system") or ("EPSG:4326" if point else None),"location_fields":location}

def _status_class(status):
    v=str(status or "unknown").lower()
    if any(x in v for x in ("reject","fail","fraud","conflict","risk")):return "attention"
    if any(x in v for x in ("verified","approved","complete","passed")):return "verified"
    return "review"

def _rows():
    with get_db() as db: rows=db.execute("SELECT id,filename,doc_type,status,fields,ocr_text,cleaned_ocr_text,mean_conf,created_at,updated_at FROM documents ORDER BY id DESC").fetchall()
    out=[]
    for row in rows:
        d=dict(row)
        try:d["fields"]=json.loads(d.get("fields") or "{}")
        except Exception:d["fields"]={}
        m=_mapping(d); out.append({"id":d["id"],"filename":d.get("filename"),"doc_type":d.get("doc_type"),"status":d.get("status"),"status_class":_status_class(d.get("status")),"confidence":d.get("mean_conf"),"created_at":d.get("created_at"),"location":m["location_fields"],"coordinates":m["coordinates"],"geometry":m["geometry"],"bbox":m["bbox"],"mapped":m["available"],"mapping_method":m["method"]})
    return out

@router.get("/document-map/documents")
def document_map_documents(q:Optional[str]=None,status:Optional[str]=None,mapped:Optional[bool]=None,user=Depends(get_current_user)):
    output=_rows(); query=(q or "").lower().strip(); sf=(status or "").lower().strip()
    if query: output=[d for d in output if query in json.dumps(d,default=str).lower()]
    if sf: output=[d for d in output if str(d.get("status") or "").lower()==sf]
    if mapped is not None: output=[d for d in output if d["mapped"] is mapped]
    return {"documents":output,"total":len(output)}

@router.get("/document-map/summary")
def document_map_summary(user=Depends(get_current_user)):
    docs=_rows(); counts={}
    for d in docs: counts[str(d.get("status") or "UNKNOWN")]=counts.get(str(d.get("status") or "UNKNOWN"),0)+1
    low=sum(1 for d in docs if _num(d.get("confidence")) is not None and _num(d.get("confidence"))<70)
    return {"total_documents":len(docs),"mapped_documents":sum(1 for d in docs if d["mapped"]),"unmapped_documents":sum(1 for d in docs if not d["mapped"]),"low_confidence":low,"status_counts":counts}

@router.get("/document-map/{document_id}")
def document_map(document_id:str,user=Depends(get_current_user)):
    d=_doc(document_id); m=_mapping(d)
    return {"document":{"id":d["id"],"filename":d.get("filename"),"doc_type":d.get("doc_type"),"status":d.get("status"),"status_class":_status_class(d.get("status")),"confidence":d.get("mean_conf"),"created_at":d.get("created_at"),"updated_at":d.get("updated_at")},"source":"uploaded_document_record","mapping":{**m,"message":"Derived only from the uploaded document." if m["available"] else "No coordinates or geometry found. No location is fabricated."}}
