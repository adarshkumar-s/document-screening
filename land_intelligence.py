"""Land Document Intelligence + GIS Mapping + Verification domain layer.

This module intentionally uses only local/open-source primitives. The parcel dataset
seeded here is synthetic and must never be represented as authoritative cadastral data.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

try:
    from shapely.geometry import shape, mapping, Point
    from shapely.validation import explain_validity
    HAS_SHAPELY = True
except Exception:
    HAS_SHAPELY = False

from server import (
    BASE_DIR, app, get_db, get_current_user, log_audit,
    require_roles, ROLE_ADMIN, ROLE_DATA_OFFICER, ROLE_VERIFICATION_OFFICER,
)

LAND_SCHEMA_VERSION = "1.0"
MATCH_STATUSES = {"MATCH", "POSSIBLE MATCH", "NO MATCH", "INSUFFICIENT DATA"}
CASE_STATUSES = {"OPEN", "UNDER_REVIEW", "NEEDS_CORRECTION", "VERIFIED", "CLOSED"}

DEMO_PARCELS = [
    {"property_id":"DEMO-PROP-103-A","parcel_id":"DEMO-103-A","district":"Demo District","taluka":"Demo Taluka","village":"Demo Village","survey_number":"DEMO-103","gat_number":None,"khasra_number":None,"sub_division":"A","parent_property_id":"DEMO-PROP-103","area":2.31,"area_unit":"ha","latitude":28.6214,"longitude":77.1045,"geometry_source":"Demo GIS dataset","geometry_confidence":0.94},
    {"property_id":"DEMO-PROP-103-B","parcel_id":"DEMO-103-B","district":"Demo District","taluka":"Demo Taluka","village":"Demo Village","survey_number":"DEMO-103","gat_number":None,"khasra_number":None,"sub_division":"B","parent_property_id":"DEMO-PROP-103","area":2.69,"area_unit":"ha","latitude":28.6219,"longitude":77.1060,"geometry_source":"Demo GIS dataset","geometry_confidence":0.94},
    {"property_id":"DEMO-PROP-104","parcel_id":"DEMO-104","district":"Demo District","taluka":"Demo Taluka","village":"Demo Village","survey_number":"DEMO-104","gat_number":None,"khasra_number":None,"sub_division":None,"parent_property_id":None,"area":3.20,"area_unit":"ha","latitude":28.6201,"longitude":77.1080,"geometry_source":"Demo GIS dataset","geometry_confidence":0.93},
    {"property_id":"DEMO-PROP-105","parcel_id":"DEMO-105","district":"Demo District","taluka":"Demo Taluka","village":"Demo Village","survey_number":"DEMO-105","gat_number":None,"khasra_number":None,"sub_division":None,"parent_property_id":None,"area":1.80,"area_unit":"ha","latitude":28.6240,"longitude":77.1070,"geometry_source":"Demo GIS dataset","geometry_confidence":0.92},
]

DEMO_GEOJSON = {
    "type":"FeatureCollection",
    "name":"Demo Synthetic Cadastral Dataset",
    "metadata":{"source":"Synthetic project-owned demo data","license":"Project demo data","georeferenced":True,"crs":"EPSG:4326"},
    "features":[
        {"type":"Feature","properties":{"property_id":"DEMO-PROP-103-A","parcel_id":"DEMO-103-A","survey_number":"DEMO-103","sub_division":"A","area":2.31},"geometry":{"type":"Polygon","coordinates":[[[77.101,28.6195],[77.1055,28.6195],[77.1055,28.6225],[77.101,28.6225],[77.101,28.6195]]]}},
        {"type":"Feature","properties":{"property_id":"DEMO-PROP-103-B","parcel_id":"DEMO-103-B","survey_number":"DEMO-103","sub_division":"B","area":2.69},"geometry":{"type":"Polygon","coordinates":[[[77.1055,28.6195],[77.109,28.6195],[77.109,28.6225],[77.1055,28.6225],[77.1055,28.6195]]]}},
        {"type":"Feature","properties":{"property_id":"DEMO-PROP-104","parcel_id":"DEMO-104","survey_number":"DEMO-104","area":3.20},"geometry":{"type":"Polygon","coordinates":[[[77.101,28.6225],[77.106,28.6225],[77.106,28.625],[77.101,28.625],[77.101,28.6225]]]}},
        {"type":"Feature","properties":{"property_id":"DEMO-PROP-105","parcel_id":"DEMO-105","survey_number":"DEMO-105","area":1.80},"geometry":{"type":"Polygon","coordinates":[[[77.106,28.6225],[77.110,28.6225],[77.110,28.625],[77.106,28.625],[77.106,28.6225]]]}}
    ]
}


def _now() -> float:
    return time.time()


def _json(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def _field(fields: Dict[str, Any], name: str) -> Optional[str]:
    value = fields.get(name)
    if isinstance(value, dict):
        value = value.get("value")
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _normal(v: Any) -> str:
    return " ".join(str(v or "").strip().lower().split())


def _build_geometry(parcel_id: str) -> Optional[dict]:
    for feature in DEMO_GEOJSON["features"]:
        if feature["properties"]["parcel_id"] == parcel_id:
            return feature["geometry"]
    return None


def _ensure_tables() -> None:
    with get_db() as db:
        statements = [
            """CREATE TABLE IF NOT EXISTS properties (
                property_id TEXT PRIMARY KEY, parcel_id TEXT UNIQUE NOT NULL,
                district TEXT, taluka TEXT, village TEXT, survey_number TEXT,
                gat_number TEXT, khasra_number TEXT, sub_division TEXT,
                parent_property_id TEXT, area REAL, area_unit TEXT, geometry TEXT,
                centroid TEXT, latitude REAL, longitude REAL, crs TEXT,
                georeferenced INTEGER NOT NULL DEFAULT 0, geometry_source TEXT,
                geometry_confidence REAL, data_source TEXT, source_confidence REAL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS property_documents (
                property_id TEXT NOT NULL, document_id TEXT NOT NULL,
                source_type TEXT NOT NULL DEFAULT 'uploaded_document',
                linked_at REAL NOT NULL, PRIMARY KEY(property_id, document_id)
            )""",
            """CREATE TABLE IF NOT EXISTS verification_cases (
                case_id TEXT PRIMARY KEY, property_id TEXT, status TEXT NOT NULL,
                assigned_officer TEXT, findings TEXT NOT NULL DEFAULT '[]',
                warnings TEXT NOT NULL DEFAULT '[]', comparison_results TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS verification_tasks (
                task_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN', assigned_to TEXT, created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS provenance (
                id TEXT PRIMARY KEY, property_id TEXT NOT NULL, field_name TEXT NOT NULL,
                value TEXT, source TEXT NOT NULL, confidence REAL, created_at REAL NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS property_timeline (
                id TEXT PRIMARY KEY, property_id TEXT NOT NULL, event_type TEXT NOT NULL,
                description TEXT NOT NULL, source TEXT NOT NULL, created_at REAL NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS verification_findings (
                finding_id TEXT PRIMARY KEY, property_id TEXT NOT NULL, case_id TEXT,
                finding_type TEXT NOT NULL, severity TEXT NOT NULL DEFAULT 'REVIEW',
                status TEXT NOT NULL DEFAULT 'OPEN', title TEXT NOT NULL,
                evidence TEXT NOT NULL DEFAULT '{}', created_by TEXT, created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS dataset_sources (
                source_id TEXT PRIMARY KEY, source TEXT NOT NULL, license TEXT,
                crs TEXT, georeferenced INTEGER NOT NULL DEFAULT 0,
                confidence REAL, imported_at REAL NOT NULL
            )"",
        ]
        for stmt in statements:
            db.execute(stmt)

        count = db.execute("SELECT COUNT(*) AS c FROM properties").fetchone()["c"]
        if count == 0:
            for p in DEMO_PARCELS:
                geom = _build_geometry(p["parcel_id"])
                centroid = {"latitude":p["latitude"],"longitude":p["longitude"]}
                db.execute("""INSERT INTO properties
                    (property_id,parcel_id,district,taluka,village,survey_number,gat_number,khasra_number,
                     sub_division,parent_property_id,area,area_unit,geometry,centroid,latitude,longitude,crs,
                     georeferenced,geometry_source,geometry_confidence,data_source,source_confidence,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (p["property_id"],p["parcel_id"],p["district"],p["taluka"],p["village"],p["survey_number"],
                     p["gat_number"],p["khasra_number"],p["sub_division"],p["parent_property_id"],p["area"],p["area_unit"],
                     _json(geom),_json(centroid),p["latitude"],p["longitude"],"EPSG:4326",1,p["geometry_source"],
                     p["geometry_confidence"],"Synthetic/demo dataset",p["geometry_confidence"],_now(),_now()))
                for name, value, conf in [("survey_number",p["survey_number"],0.99),("village",p["village"],0.99),("taluka",p["taluka"],0.99),("district",p["district"],0.99),("area",p["area"],p["geometry_confidence"])]:
                    db.execute("INSERT INTO provenance (id,property_id,field_name,value,source,confidence,created_at) VALUES (?,?,?,?,?,?,?)",
                               (uuid.uuid4().hex,p["property_id"],name,str(value),"Synthetic GIS dataset",conf,_now()))
                db.execute("INSERT INTO property_timeline (id,property_id,event_type,description,source,created_at) VALUES (?,?,?,?,?,?)",
                           (uuid.uuid4().hex,p["property_id"],"DEMO_DATASET_CREATED","Synthetic parcel created for demonstration; not authoritative cadastral data.","Project synthetic dataset",_now()))


def _property(row: Any, include_geometry: bool = True) -> Dict[str, Any]:
    d = dict(row)
    d["geometry"] = json.loads(d["geometry"]) if d.get("geometry") and include_geometry else None
    d["centroid"] = json.loads(d["centroid"]) if d.get("centroid") else None
    d["georeferenced"] = bool(d.get("georeferenced"))
    return d


def _resolve(fields: Dict[str, Any]) -> Dict[str, Any]:
    identifiers = {k:_field(fields,k) for k in ("survey_number","gat_number","khasra_number","village","taluka","district")}
    supplied = {k:v for k,v in identifiers.items() if v}
    if not supplied:
        return {"status":"INSUFFICIENT DATA","confidence":0,"matches":[],"reasons":["No property identifiers or location fields were extracted."]}
    with get_db() as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM properties").fetchall()]
    scored=[]
    for row in rows:
        score=0; reasons=[]
        for k,v in supplied.items():
            rv=row.get(k)
            if rv is None: continue
            if _normal(rv)==_normal(v): score += 2 if k in ("survey_number","gat_number","khasra_number") else 1; reasons.append(f"{k.replace('_',' ')} matched")
        max_score = sum(2 if k in ("survey_number","gat_number","khasra_number") else 1 for k in supplied)
        pct = round((score/max_score)*100) if max_score else 0
        if score: scored.append((pct,row,reasons))
    scored.sort(key=lambda x:x[0],reverse=True)
    if not scored: return {"status":"NO MATCH","confidence":0,"matches":[],"reasons":["No parcel matched the supplied identifiers/location."]}
    top=scored[0]; matches=[]
    for pct,row,reasons in scored[:5]:
        if pct >= 70:
            matches.append({"property":_property(row,False),"confidence":pct/100,"reasons":reasons})
    if top[0] >= 95: status="MATCH"
    elif top[0] >= 50: status="POSSIBLE MATCH"
    else: status="NO MATCH"
    return {"status":status,"confidence":top[0]/100,"matches":matches,"reasons":top[2]}


class CaseCreate(BaseModel):
    property_id: Optional[str] = None
    assigned_officer: Optional[str] = None
    findings: List[dict] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)

router = APIRouter(prefix="/api/land", tags=["Land Intelligence"])

@router.get("/health")
def health():
    return {"status":"ok","schema_version":LAND_SCHEMA_VERSION,"demo_data":True,"shapely":HAS_SHAPELY}

@router.get("/geojson")
def parcel_geojson(limit: int = 250):
    limit=max(1,min(limit,250))
    with get_db() as db:
        rows=db.execute("SELECT * FROM properties ORDER BY parcel_id LIMIT ?",(limit,)).fetchall()
    features=[]
    for row in rows:
        p=_property(row,True)
        props={k:p.get(k) for k in ("property_id","parcel_id","district","taluka","village","survey_number","gat_number","khasra_number","sub_division","area","area_unit","geometry_source","geometry_confidence","data_source","source_confidence")}
        features.append({"type":"Feature","properties":props,"geometry":p["geometry"]})
    return {"type":"FeatureCollection","features":features,"metadata":{"label":"Demo / Synthetic Land Data","crs":"EPSG:4326","authoritative":False}}

@router.get("/properties")
def properties(q: Optional[str]=None, district: Optional[str]=None, village: Optional[str]=None, limit: int=100, offset: int=0, user: dict=Depends(get_current_user)):
    limit=max(1,min(limit,250)); offset=max(0,offset)
    clauses=[]; params=[]
    if q:
        like=f"%{q.strip()}%"; clauses.append("(property_id LIKE ? OR parcel_id LIKE ? OR survey_number LIKE ? OR gat_number LIKE ? OR khasra_number LIKE ? OR village LIKE ?)"); params += [like]*6
    for col,val in (("district",district),("village",village)):
        if val: clauses.append(f"LOWER({col})=LOWER(?)"); params.append(val)
    where=(" WHERE "+" AND ".join(clauses)) if clauses else ""
    with get_db() as db:
        rows=db.execute(f"SELECT * FROM properties{where} ORDER BY parcel_id LIMIT ? OFFSET ?",tuple(params+[limit,offset])).fetchall()
        total=db.execute(f"SELECT COUNT(*) AS c FROM properties{where}",tuple(params)).fetchone()["c"]
    return {"properties":[_property(r,False) for r in rows],"total":total,"limit":limit,"offset":offset}

@router.get("/properties/{property_id}")
def property_detail(property_id: str, user: dict=Depends(get_current_user)):
    with get_db() as db:
        row=db.execute("SELECT * FROM properties WHERE property_id=? OR parcel_id=?",(property_id,property_id)).fetchone()
        if not row: raise HTTPException(404,"Property not found")
        p=_property(row,True)
        docs=db.execute("""SELECT d.id,d.filename,d.doc_type,d.mean_conf,d.status,d.created_at
                          FROM property_documents pd JOIN documents d ON d.id=pd.document_id
                          WHERE pd.property_id=? ORDER BY d.created_at DESC""",(p["property_id"],)).fetchall()
        neighbors=[]
        if HAS_SHAPELY and p["geometry"]:
            geom=shape(p["geometry"])
            for nr in db.execute("SELECT * FROM properties WHERE property_id!=?",(p["property_id"],)).fetchall():
                ng=shape(json.loads(nr["geometry"])) if nr["geometry"] else None
                if ng and (geom.touches(ng) or geom.distance(ng)<0.0001): neighbors.append(_property(nr,False))
        prov=db.execute("SELECT field_name,value,source,confidence,created_at FROM provenance WHERE property_id=? ORDER BY created_at DESC",(p["property_id"],)).fetchall()
        timeline=db.execute("SELECT event_type,description,source,created_at FROM property_timeline WHERE property_id=? ORDER BY created_at ASC",(p["property_id"],)).fetchall()
    p["documents"] = [dict(d) for d in docs]
    p["neighbors"] = neighbors
    p["provenance"] = [dict(x) for x in prov]
    p["timeline"] = [dict(x) for x in timeline]
    return p

@router.get("/resolve/document/{doc_id}")
def resolve_document(doc_id: str, user: dict=Depends(get_current_user)):
    with get_db() as db:
        row=db.execute("SELECT * FROM documents WHERE id=?",(doc_id,)).fetchone()
        if not row: raise HTTPException(404,"Document not found")
        if user["role"]==ROLE_DATA_OFFICER and row["uploaded_by"]!=user["email"]: raise HTTPException(403,"Access denied")
        fields=json.loads(row["fields"] or "{}")
    result=_resolve(fields)
    if result["status"] in ("MATCH","POSSIBLE MATCH") and result["matches"]:
        prop_id=result["matches"][0]["property"]["property_id"]
        with get_db() as db:
            db.execute("INSERT OR IGNORE INTO property_documents(property_id,document_id,source_type,linked_at) VALUES (?,?,?,?)",(prop_id,doc_id,"uploaded_document",_now()))
            db.execute("INSERT INTO property_timeline(id,property_id,event_type,description,source,created_at) VALUES (?,?,?,?,?,?)",(uuid.uuid4().hex,prop_id,"PROPERTY_MATCHED",f"Document {doc_id} resolved to {prop_id} with {round(result['confidence']*100)}% confidence.","Property resolution service",_now()))
        log_audit(user["full_name"],"PROPERTY_RESOLVED",f"Resolved document #{doc_id} to {prop_id}: {result['status']}",doc_id)
        result["property_id"]=prop_id
    return result

@router.get("/compare/{doc_id}/{property_id}")
def compare_document_property(doc_id: str, property_id: str, user: dict=Depends(get_current_user)):
    with get_db() as db:
        d=db.execute("SELECT * FROM documents WHERE id=?",(doc_id,)).fetchone()
        p=db.execute("SELECT * FROM properties WHERE property_id=? OR parcel_id=?",(property_id,property_id)).fetchone()
        if not d or not p: raise HTTPException(404,"Document or property not found")
        if user["role"]==ROLE_DATA_OFFICER and d["uploaded_by"]!=user["email"]: raise HTTPException(403,"Access denied")
    fields=json.loads(d["fields"] or "{}"); prop=_property(p,False)
    checks=[]
    pairs=[("survey_number","Survey/Gat/Khasra",_field(fields,"survey_number") or _field(fields,"gat_number") or _field(fields,"khasra_number"),prop.get("survey_number") or prop.get("gat_number") or prop.get("khasra_number")),("village","Village",_field(fields,"village"),prop.get("village")),("taluka","Taluka",_field(fields,"taluka") or _field(fields,"tehsil"),prop.get("taluka")),("district","District",_field(fields,"district"),prop.get("district")),("sub_division","Subdivision",_field(fields,"sub_division"),prop.get("sub_division"))]
    for key,label,dv,pv in pairs:
        if not dv or not pv: checks.append({"field":key,"label":label,"status":"INSUFFICIENT EVIDENCE","document":dv,"parcel":pv}); continue
        checks.append({"field":key,"label":label,"status":"CONSISTENT" if _normal(dv)==_normal(pv) else "CONFLICT","document":dv,"parcel":pv})
    area_doc=_field(fields,"area")
    area_num=None
    if area_doc:
        import re
        m=re.search(r"[0-9]+(?:\.[0-9]+)?",area_doc.replace(",","")); area_num=float(m.group()) if m else None
    area_diff=None
    if area_num is not None and prop.get("area") is not None:
        area_diff=round(abs(area_num-prop["area"]),4)
        tol=float(os.getenv("LAND_AREA_TOLERANCE_HA","0.05"))
        checks.append({"field":"area","label":"Area","status":"CONSISTENT" if area_diff<=tol else "REVIEW REQUIRED","document":area_num,"parcel":prop["area"],"difference":area_diff,"tolerance":tol,"unit":prop["area_unit"]})
    conflicts=sum(x["status"]=="CONFLICT" for x in checks); reviews=sum(x["status"]=="REVIEW REQUIRED" for x in checks); missing=sum(x["status"]=="INSUFFICIENT EVIDENCE" for x in checks)
    overall="CONFLICT" if conflicts else ("REVIEW REQUIRED" if reviews else ("INSUFFICIENT EVIDENCE" if missing else "CONSISTENT"))
    return {"document_id":doc_id,"property_id":prop["property_id"],"overall_status":overall,"checks":checks,"explanation":"Comparison is decision support only; it does not establish legal ownership, fraud, or authenticity."}

@router.get("/dashboard")
def land_dashboard(user: dict=Depends(get_current_user)):
    with get_db() as db:
        total=db.execute("SELECT COUNT(*) c FROM properties").fetchone()["c"]
        docs=db.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"]
        cases=db.execute("SELECT COUNT(*) c FROM verification_cases").fetchone()["c"]
        pending=db.execute("SELECT COUNT(*) c FROM verification_cases WHERE status IN ('OPEN','UNDER_REVIEW','NEEDS_CORRECTION')").fetchone()["c"]
        linked=db.execute("SELECT COUNT(DISTINCT document_id) c FROM property_documents").fetchone()["c"]
        review_required=db.execute("SELECT COUNT(*) c FROM verification_findings WHERE status IN ('OPEN','ACKNOWLEDGED') AND severity='REVIEW'").fetchone()["c"]
        conflicts=db.execute("SELECT COUNT(*) c FROM verification_findings WHERE status IN ('OPEN','ACKNOWLEDGED') AND severity='CONFLICT'").fetchone()["c"]
        low_confidence=db.execute("SELECT COUNT(*) c FROM documents WHERE mean_conf < 65").fetchone()["c"]
    return {"total_properties":total,"documents_processed":docs,"pending_verification":pending,"review_required":review_required,"conflicts":conflicts,"no_parcel_match":max(0,docs-linked),"low_confidence":low_confidence,"completed_cases":max(0,cases-pending),"demo_data":True}

@router.get("/cases")
def list_cases(status: Optional[str]=None, user: dict=Depends(get_current_user)):
    with get_db() as db:
        if status:
            rows=db.execute("SELECT * FROM verification_cases WHERE status=? ORDER BY updated_at DESC",(status,)).fetchall()
        else: rows=db.execute("SELECT * FROM verification_cases ORDER BY updated_at DESC").fetchall()
    return {"cases":[dict(r) for r in rows]}

@router.post("/cases")
def create_case(req: CaseCreate, user: dict=Depends(require_roles(ROLE_DATA_OFFICER,ROLE_VERIFICATION_OFFICER,ROLE_ADMIN))):
    if req.property_id:
        with get_db() as db:
            if not db.execute("SELECT 1 FROM properties WHERE property_id=? OR parcel_id=?",(req.property_id,req.property_id)).fetchone(): raise HTTPException(404,"Property not found")
    case_id="CASE-"+uuid.uuid4().hex[:10].upper(); now=_now()
    with get_db() as db:
        db.execute("INSERT INTO verification_cases(case_id,property_id,status,assigned_officer,findings,warnings,comparison_results,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",(case_id,req.property_id,"OPEN",req.assigned_officer or user["email"],_json(req.findings),_json(req.warnings),"[]",now,now))
        if req.property_id: db.execute("INSERT INTO property_timeline(id,property_id,event_type,description,source,created_at) VALUES (?,?,?,?,?,?)",(uuid.uuid4().hex,req.property_id,"VERIFICATION_STARTED",f"Verification case {case_id} created.","Verification workflow",now))
    log_audit(user["full_name"],"CASE_CREATED",f"Created verification case {case_id}",case_id)
    return {"case_id":case_id,"status":"OPEN"}

@router.get("/cases/{case_id}")
def case_detail(case_id: str, user: dict=Depends(get_current_user)):
    with get_db() as db:
        row=db.execute("SELECT * FROM verification_cases WHERE case_id=?",(case_id,)).fetchone()
        if not row: raise HTTPException(404,"Case not found")
        tasks=db.execute("SELECT * FROM verification_tasks WHERE case_id=? ORDER BY created_at",(case_id,)).fetchall()
        audits=db.execute("SELECT * FROM audit WHERE detail LIKE ? ORDER BY id DESC",(f"%{case_id}%",)).fetchall()
    result=dict(row); result["findings"]=json.loads(result["findings"] or "[]"); result["warnings"]=json.loads(result["warnings"] or "[]"); result["comparison_results"]=json.loads(result["comparison_results"] or "[]"); result["tasks"]= [dict(x) for x in tasks]; result["audit"]= [dict(x) for x in audits]
    return result

@router.post("/import-geojson")
async def import_geojson(file: UploadFile=File(...), user: dict=Depends(require_roles(ROLE_DATA_OFFICER,ROLE_ADMIN))):
    raw=await file.read()
    if len(raw)>5*1024*1024: raise HTTPException(413,"GeoJSON exceeds 5 MB limit")
    try: data=json.loads(raw.decode("utf-8"))
    except Exception: raise HTTPException(422,"Invalid GeoJSON JSON")
    if data.get("type")!="FeatureCollection": raise HTTPException(422,"Expected a GeoJSON FeatureCollection")
    features=data.get("features") or []
    if len(features)>1000: raise HTTPException(422,"Too many features for one import")
    imported=0; rejected=[]
    for idx,f in enumerate(features):
        geom=f.get("geometry"); props=f.get("properties") or {}
        if not geom: rejected.append({"index":idx,"reason":"Missing geometry"}); continue
        if HAS_SHAPELY:
            try:
                g=shape(geom)
                if g.is_empty or not g.is_valid or not g.geom_type in ("Polygon","MultiPolygon"): rejected.append({"index":idx,"reason":explain_validity(g)}); continue
                if not all(math.isfinite(x) for x in g.bounds): raise ValueError("Non-finite coordinates")
                centroid=g.centroid
                lon,lat=centroid.x,centroid.y
            except Exception as exc: rejected.append({"index":idx,"reason":str(exc)}); continue
        else: rejected.append({"index":idx,"reason":"Shapely is required for safe polygon import"}); continue
        parcel_id=str(props.get("parcel_id") or props.get("property_id") or f"IMPORTED-{idx+1}").strip()
        property_id=str(props.get("property_id") or parcel_id).strip()
        area=props.get("area")
        try: area=float(area) if area is not None else None
        except Exception: area=None
        now=_now()
        with get_db() as db:
            db.execute("""INSERT INTO properties(property_id,parcel_id,district,taluka,village,survey_number,gat_number,khasra_number,sub_division,parent_property_id,area,area_unit,geometry,centroid,latitude,longitude,crs,georeferenced,geometry_source,geometry_confidence,data_source,source_confidence,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(property_id) DO UPDATE SET geometry=excluded.geometry,centroid=excluded.centroid,latitude=excluded.latitude,longitude=excluded.longitude,updated_at=excluded.updated_at""",
                (property_id,parcel_id,props.get("district"),props.get("taluka"),props.get("village"),props.get("survey_number"),props.get("gat_number"),props.get("khasra_number"),props.get("sub_division"),props.get("parent_property_id"),area,props.get("area_unit","ha"),_json(geom),_json({"latitude":lat,"longitude":lon}),lat,lon,props.get("crs","EPSG:4326"),1,props.get("geometry_source","Imported GeoJSON"),float(props.get("geometry_confidence",0.8)),props.get("data_source","User-provided dataset"),float(props.get("source_confidence",0.8)),now,now))
        imported+=1
    log_audit(user["full_name"],"GEOJSON_IMPORT",f"Imported {imported} parcels; rejected {len(rejected)} features")
    return {"imported":imported,"rejected":rejected,"source_license":"User-supplied; verify license before use","authoritative":False}



class FindingCreate(BaseModel):
    property_id: str
    case_id: Optional[str] = None
    finding_type: str
    severity: str = "REVIEW"
    title: str
    evidence: Dict[str, Any] = Field(default_factory=dict)

class FindingUpdate(BaseModel):
    status: str

class CaseStatusUpdate(BaseModel):
    status: str

class TaskCreate(BaseModel):
    title: str
    description: str = ""
    priority: str = "NORMAL"
    assigned_to: Optional[str] = None

class TaskUpdate(BaseModel):
    status: str
    description: Optional[str] = None


def _record_timeline(property_id: str, event_type: str, description: str, source: str, created_at: Optional[float] = None) -> None:
    with get_db() as db:
        db.execute(
            "INSERT INTO property_timeline(id,property_id,event_type,description,source,created_at) VALUES (?,?,?,?,?,?)",
            (uuid.uuid4().hex, property_id, event_type, description, source, created_at or _now()),
        )



@router.get("/search")
def unified_property_search(q: str = "", limit: int = 25, user: dict = Depends(get_current_user)):
    q = (q or "").strip()
    if not q:
        return {"results": []}
    limit = max(1, min(limit, 50))
    needle = f"%{q}%"
    with get_db() as db:
        rows = db.execute(
            """SELECT property_id, parcel_id, survey_number, gat_number, khasra_number,
                      village, taluka, district, area, area_unit
               FROM properties
               WHERE property_id LIKE ? OR parcel_id LIKE ? OR survey_number LIKE ?
                  OR gat_number LIKE ? OR khasra_number LIKE ? OR village LIKE ?
                  OR taluka LIKE ? OR district LIKE ?
               ORDER BY parcel_id LIMIT ?""",
            (needle, needle, needle, needle, needle, needle, needle, needle, limit),
        ).fetchall()
    return {"results": [dict(r) for r in rows], "query": q}

@router.get("/map-config")
def map_config():
    return {
        "tile_url": os.getenv("MAP_TILE_URL", "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"),
        "attribution": os.getenv("MAP_ATTRIBUTION", "© OpenStreetMap contributors"),
        "min_zoom": int(os.getenv("MAP_MIN_ZOOM", "3")),
        "max_zoom": int(os.getenv("MAP_MAX_ZOOM", "19")),
        "provider": "configurable",
        "offline_core": True,
    }

@router.get("/investigate/{property_id}")
def investigate_property(property_id: str, user: dict = Depends(get_current_user)):
    detail = property_detail(property_id, user)
    with get_db() as db:
        findings = db.execute("SELECT * FROM verification_findings WHERE property_id=? ORDER BY updated_at DESC", (detail["property_id"],)).fetchall()
        cases = db.execute("SELECT case_id,status,assigned_officer,created_at,updated_at FROM verification_cases WHERE property_id=? ORDER BY updated_at DESC", (detail["property_id"],)).fetchall()
    return {**detail, "findings": [dict(x) for x in findings], "cases": [dict(x) for x in cases]}

@router.get("/timeline/{property_id}")
def property_timeline(property_id: str, user: dict = Depends(get_current_user)):
    with get_db() as db:
        rows = db.execute("SELECT event_type,description,source,created_at FROM property_timeline WHERE property_id=? ORDER BY created_at ASC", (property_id,)).fetchall()
    return {"property_id": property_id, "timeline": [dict(x) for x in rows]}

@router.get("/provenance/{property_id}")
def property_provenance(property_id: str, user: dict = Depends(get_current_user)):
    with get_db() as db:
        rows = db.execute("SELECT field_name,value,source,confidence,created_at FROM provenance WHERE property_id=? ORDER BY created_at DESC", (property_id,)).fetchall()
    return {"property_id": property_id, "provenance": [dict(x) for x in rows]}



@router.get("/findings")
def list_findings(property_id: Optional[str] = None, status_filter: Optional[str] = None, user: dict = Depends(get_current_user)):
    clauses = []
    params: List[Any] = []
    if property_id:
        clauses.append("property_id=?")
        params.append(property_id)
    if status_filter:
        clauses.append("status=?")
        params.append(status_filter)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_db() as db:
        rows = db.execute(f"SELECT * FROM verification_findings{where} ORDER BY updated_at DESC", tuple(params)).fetchall()
    return {"findings": [dict(x) for x in rows]}

@router.post("/findings")
def create_finding(req: FindingCreate, user: dict = Depends(require_roles(ROLE_DATA_OFFICER, ROLE_VERIFICATION_OFFICER, ROLE_ADMIN))):
    with get_db() as db:
        prop = db.execute("SELECT property_id FROM properties WHERE property_id=? OR parcel_id=?", (req.property_id, req.property_id)).fetchone()
        if not prop:
            raise HTTPException(404, "Property not found")
    finding_id = "FND-" + uuid.uuid4().hex[:10].upper()
    now = _now()
    with get_db() as db:
        db.execute("INSERT INTO verification_findings(finding_id,property_id,case_id,finding_type,severity,status,title,evidence,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (finding_id, prop["property_id"], req.case_id, req.finding_type, req.severity.upper(), "OPEN", req.title, _json(req.evidence), user["email"], now, now))
    _record_timeline(prop["property_id"], "FINDING_CREATED", f"Finding {finding_id}: {req.title}", "Verification engine", now)
    log_audit(user["full_name"], "FINDING_CREATED", f"Created finding {finding_id} for {prop['property_id']}")
    return {"finding_id": finding_id, "status": "OPEN"}

@router.patch("/findings/{finding_id}")
def update_finding(finding_id: str, req: FindingUpdate, user: dict = Depends(require_roles(ROLE_VERIFICATION_OFFICER, ROLE_ADMIN))):
    allowed = {"OPEN", "ACKNOWLEDGED", "RESOLVED", "DISMISSED"}
    status_value = req.status.upper().strip()
    if status_value not in allowed:
        raise HTTPException(422, f"Unsupported finding status. Use one of {sorted(allowed)}.")
    with get_db() as db:
        row = db.execute("SELECT property_id FROM verification_findings WHERE finding_id=?", (finding_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Finding not found")
        db.execute("UPDATE verification_findings SET status=?,updated_at=? WHERE finding_id=?", (status_value, _now(), finding_id))
    _record_timeline(row["property_id"], "FINDING_STATUS_CHANGED", f"Finding {finding_id} changed to {status_value}", "Verification officer")
    log_audit(user["full_name"], "FINDING_STATUS_CHANGED", f"Finding {finding_id}: {status_value}")
    return {"finding_id": finding_id, "status": status_value}



@router.patch("/cases/{case_id}/status")
def update_case_status(case_id: str, req: CaseStatusUpdate, user: dict = Depends(require_roles(ROLE_VERIFICATION_OFFICER, ROLE_ADMIN))):
    status_value = req.status.upper().strip()
    if status_value not in CASE_STATUSES:
        raise HTTPException(422, f"Unsupported case status. Use one of {sorted(CASE_STATUSES)}.")
    with get_db() as db:
        row = db.execute("SELECT property_id FROM verification_cases WHERE case_id=?", (case_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Case not found")
        db.execute("UPDATE verification_cases SET status=?,updated_at=? WHERE case_id=?", (status_value, _now(), case_id))
    if row["property_id"]:
        _record_timeline(row["property_id"], "CASE_STATUS_CHANGED", f"Case {case_id} changed to {status_value}", "Verification workflow")
    log_audit(user["full_name"], "CASE_STATUS_CHANGED", f"Case {case_id}: {status_value}", case_id)
    return {"case_id": case_id, "status": status_value}

@router.post("/cases/{case_id}/tasks")
def create_case_task(case_id: str, req: TaskCreate, user: dict = Depends(require_roles(ROLE_DATA_OFFICER, ROLE_VERIFICATION_OFFICER, ROLE_ADMIN))):
    task_id = "TASK-" + uuid.uuid4().hex[:10].upper()
    now = _now()
    with get_db() as db:
        case = db.execute("SELECT property_id FROM verification_cases WHERE case_id=?", (case_id,)).fetchone()
        if not case:
            raise HTTPException(404, "Case not found")
        db.execute("INSERT INTO verification_tasks(task_id,case_id,title,status,assigned_to,created_at,updated_at) VALUES (?,?,?,?,?,?,?)", (task_id, case_id, req.title, "OPEN", req.assigned_to or user["email"], now, now))
    if case["property_id"]:
        _record_timeline(case["property_id"], "TASK_CREATED", f"Task {task_id}: {req.title}", "Verification workflow", now)
    log_audit(user["full_name"], "TASK_CREATED", f"Created task {task_id}", case_id)
    return {"task_id": task_id, "status": "OPEN"}

@router.patch("/tasks/{task_id}")
def update_case_task(task_id: str, req: TaskUpdate, user: dict = Depends(require_roles(ROLE_DATA_OFFICER, ROLE_VERIFICATION_OFFICER, ROLE_ADMIN))):
    allowed = {"OPEN", "IN_PROGRESS", "COMPLETED", "CANCELLED"}
    status_value = req.status.upper().strip()
    if status_value not in allowed:
        raise HTTPException(422, f"Unsupported task status. Use one of {sorted(allowed)}.")
    with get_db() as db:
        row = db.execute("SELECT case_id FROM verification_tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Task not found")
        db.execute("UPDATE verification_tasks SET status=?,updated_at=? WHERE task_id=?", (status_value, _now(), task_id))
    log_audit(user["full_name"], "TASK_STATUS_CHANGED", f"Task {task_id}: {status_value}", row["case_id"])
    return {"task_id": task_id, "status": status_value}

@router.get("/cases/{case_id}/tasks")
def list_case_tasks(case_id: str, user: dict = Depends(get_current_user)):
    with get_db() as db:
        rows = db.execute("SELECT * FROM verification_tasks WHERE case_id=? ORDER BY created_at", (case_id,)).fetchall()
    return {"tasks": [dict(x) for x in rows]}

_ensure_tables()
app.include_router(router)
