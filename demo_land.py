"""Public, credential-free demo API backed only by synthetic project-owned data."""
from fastapi import APIRouter
from land_intelligence import DEMO_GEOJSON, DEMO_PARCELS

router = APIRouter(prefix="/api/demo-land", tags=["Demo Land Intelligence"])

DEMO_DOCUMENTS = {
    "DEMO-DOC-7-12": {
        "id":"DEMO-DOC-7-12","filename":"demo-7-12.pdf","doc_type":"Synthetic 7/12-style demo document",
        "status":"DEMO","ocr_confidence":0.91,"verification_status":"DEMO REVIEW","property_id":"DEMO-PROP-103-A",
        "fields":{"survey_number":"DEMO-103","village":"Demo Village","taluka":"Demo Taluka","district":"Demo District","area":2.40,"sub_division":"A"},
        "provenance":"Synthetic demo document; values are not copied from government records."
    },
    "DEMO-DOC-SALE": {
        "id":"DEMO-DOC-SALE","filename":"demo-sale-document.pdf","doc_type":"Synthetic sale document",
        "status":"DEMO","ocr_confidence":0.94,"verification_status":"DEMO REVIEW","property_id":"DEMO-PROP-103-A",
        "fields":{"survey_number":"DEMO-103","village":"Demo Village","taluka":"Demo Taluka","district":"Demo District","area":2.31,"sub_division":"A"},
        "provenance":"Synthetic demo document; values are not copied from government records."
    }
}

def _parcel(pid):
    for p in DEMO_PARCELS:
        if p["property_id"]==pid or p["parcel_id"]==pid: return p
    return None

@router.get("/health")
def health(): return {"status":"ok","demo":True,"credentials_required":False}

@router.get("/geojson")
def geojson(): return {**DEMO_GEOJSON,"metadata":{**DEMO_GEOJSON["metadata"],"label":"Demo / Synthetic Land Data","authoritative":False}}

@router.get("/dashboard")
def dashboard():
    return {"total_properties":len(DEMO_PARCELS),"documents_processed":len(DEMO_DOCUMENTS),"pending_verification":1,"review_required":1,"conflicts":0,"no_parcel_match":0,"low_confidence":1,"completed_cases":0,"demo_data":True}

@router.get("/properties")
def properties(q:str|None=None):
    rows=DEMO_PARCELS
    if q:
        needle=q.lower().strip(); rows=[p for p in rows if needle in " ".join(str(p.get(k) or "") for k in ("property_id","parcel_id","survey_number","gat_number","khasra_number","village")).lower()]
    return {"properties":rows,"total":len(rows)}

@router.get("/properties/{property_id}")
def property_detail(property_id:str):
    p=_parcel(property_id)
    if not p: return {"error":"Property not found"}
    docs=[d for d in DEMO_DOCUMENTS.values() if d["property_id"]==p["property_id"]]
    neighbours=[n for n in DEMO_PARCELS if n["property_id"]!=p["property_id"] and n["village"]==p["village"]]
    return {**p,"geometry":next((f["geometry"] for f in DEMO_GEOJSON["features"] if f["properties"]["property_id"]==p["property_id"]),None),"crs":"EPSG:4326","georeferenced":True,"data_source":"Synthetic/demo dataset","documents":docs,"neighbors":neighbours,"provenance":[{"field_name":"survey_number","value":p["survey_number"],"source":"Synthetic GIS dataset","confidence":0.99},{"field_name":"area","value":p["area"],"source":"Synthetic GIS dataset","confidence":p["geometry_confidence"]}],"timeline":[{"event_type":"DEMO_DATASET_CREATED","description":"Synthetic parcel created for demonstration.","source":"Project synthetic dataset"}]}

@router.get("/documents")
def documents(): return {"documents":list(DEMO_DOCUMENTS.values())}

@router.get("/compare/{doc_id}/{property_id}")
def compare(doc_id:str,property_id:str):
    d=DEMO_DOCUMENTS.get(doc_id); p=_parcel(property_id)
    if not d or not p: return {"error":"Demo document or property not found"}
    checks=[]
    for key,label in [("survey_number","Survey/Gat/Khasra"),("village","Village"),("taluka","Taluka"),("district","District"),("sub_division","Subdivision")]:
        dv=d["fields"].get(key); pv=p.get(key)
        checks.append({"field":key,"label":label,"document":dv,"parcel":pv,"status":"INSUFFICIENT EVIDENCE" if not dv or not pv else ("CONSISTENT" if str(dv).lower()==str(pv).lower() else "CONFLICT")})
    diff=round(abs(float(d["fields"]["area"])-p["area"]),2)
    checks.append({"field":"area","label":"Area","document":d["fields"]["area"],"parcel":p["area"],"difference":diff,"tolerance":0.05,"status":"CONSISTENT" if diff<=0.05 else "REVIEW REQUIRED"})
    overall="REVIEW REQUIRED" if any(c["status"]=="REVIEW REQUIRED" for c in checks) else ("CONFLICT" if any(c["status"]=="CONFLICT" for c in checks) else "CONSISTENT")
    return {"document_id":doc_id,"property_id":p["property_id"],"overall_status":overall,"checks":checks,"explanation":"Synthetic demo comparison only. It is not a legal ownership, authenticity, or fraud determination."}
