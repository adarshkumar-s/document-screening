"""Public, credential-free demo API backed only by synthetic project-owned data."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Optional

from fastapi import APIRouter


DEMO_PARCELS = [
    {
        "property_id": "DEMO-PROP-103-A", "parcel_id": "DEMO-103-A", "survey_number": "DEMO-103",
        "gat_number": None, "khasra_number": None, "sub_division": "A", "parent_property_id": None,
        "village": "Demo Village", "taluka": "Demo Taluka", "district": "Demo District",
        "area": 2.31, "area_unit": "ha", "latitude": 28.6225, "longitude": 77.10375, "geometry_confidence": 0.99,
        "geometry": {"type": "Polygon", "coordinates": [[[77.1020, 28.6210], [77.1055, 28.6210], [77.1055, 28.6240], [77.1020, 28.6240], [77.1020, 28.6210]]]},
    },
    {
        "property_id": "DEMO-PROP-103-B", "parcel_id": "DEMO-103-B", "survey_number": "DEMO-103",
        "gat_number": None, "khasra_number": None, "sub_division": "B", "parent_property_id": "DEMO-PROP-103-A",
        "village": "Demo Village", "taluka": "Demo Taluka", "district": "Demo District",
        "area": 1.25, "area_unit": "ha", "latitude": 28.6225, "longitude": 77.10775, "geometry_confidence": 0.98,
        "geometry": {"type": "Polygon", "coordinates": [[[77.1060, 28.6210], [77.1095, 28.6210], [77.1095, 28.6240], [77.1060, 28.6240], [77.1060, 28.6210]]]},
    },
    {
        "property_id": "DEMO-PROP-104", "parcel_id": "DEMO-104", "survey_number": "DEMO-104",
        "gat_number": None, "khasra_number": None, "sub_division": None, "parent_property_id": None,
        "village": "Demo Village", "taluka": "Demo Taluka", "district": "Demo District",
        "area": 3.50, "area_unit": "ha", "latitude": 28.62675, "longitude": 77.10425, "geometry_confidence": 0.96,
        "geometry": {"type": "Polygon", "coordinates": [[[77.1020, 28.6250], [77.1065, 28.6250], [77.1065, 28.6285], [77.1020, 28.6285], [77.1020, 28.6250]]]},
    },
    {
        "property_id": "DEMO-PROP-105", "parcel_id": "DEMO-105", "survey_number": "DEMO-105",
        "gat_number": None, "khasra_number": None, "sub_division": None, "parent_property_id": None,
        "village": "Demo Village", "taluka": "Demo Taluka", "district": "Demo District",
        "area": 1.80, "area_unit": "ha", "latitude": 28.62675, "longitude": 77.10975, "geometry_confidence": 0.60,
        "geometry": {"type": "Polygon", "coordinates": [[[77.1080, 28.6250], [77.1115, 28.6250], [77.1115, 28.6285], [77.1080, 28.6285], [77.1080, 28.6250]]]},
    },
]

DEMO_GEOJSON = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "id": parcel["property_id"],
            "geometry": deepcopy(parcel["geometry"]),
            "properties": {
                key: value for key, value in parcel.items() if key not in {"geometry"}
            },
        }
        for parcel in DEMO_PARCELS
    ],
    "metadata": {
        "label": "Demo / Synthetic Land Data",
        "authoritative": False,
        "crs": "EPSG:4326",
    },
}

router = APIRouter(prefix="/api/demo-land", tags=["Demo Land Intelligence"])

DEMO_SCENARIOS = [
    {"id": "consistent", "label": "A · Consistent", "document_id": "DEMO-DOC-SALE", "property_id": "DEMO-PROP-103-A", "finding": "CONSISTENT"},
    {"id": "area-review", "label": "B · Area mismatch", "document_id": "DEMO-DOC-AREA-REVIEW", "property_id": "DEMO-PROP-104", "finding": "REVIEW REQUIRED"},
    {"id": "no-match", "label": "C · No parcel match", "document_id": "DEMO-DOC-NO-MATCH", "property_id": None, "finding": "NO MATCH"},
    {"id": "low-confidence", "label": "D · Low confidence", "document_id": "DEMO-DOC-LOW-CONFIDENCE", "property_id": "DEMO-PROP-105", "finding": "REVIEW REQUIRED"},
    {"id": "subdivision", "label": "E · Parent/subdivision review", "document_id": "DEMO-DOC-7-12", "property_id": "DEMO-PROP-103-A", "finding": "REVIEW REQUIRED"},
]

DEMO_DOCUMENTS = {
    "DEMO-DOC-7-12": {
        "id": "DEMO-DOC-7-12", "filename": "demo-7-12.pdf", "doc_type": "Synthetic 7/12-style demo document",
        "status": "DEMO", "ocr_confidence": 0.91, "verification_status": "DEMO REVIEW", "property_id": "DEMO-PROP-103-A",
        "fields": {"survey_number": "DEMO-103", "village": "Demo Village", "taluka": "Demo Taluka", "district": "Demo District", "area": 2.40, "sub_division": "A"},
        "provenance": "Synthetic demo document; values are not copied from government records.",
    },
    "DEMO-DOC-SALE": {
        "id": "DEMO-DOC-SALE", "filename": "demo-sale-document.pdf", "doc_type": "Synthetic sale document",
        "status": "DEMO", "ocr_confidence": 0.94, "verification_status": "DEMO REVIEW", "property_id": "DEMO-PROP-103-A",
        "fields": {"survey_number": "DEMO-103", "village": "Demo Village", "taluka": "Demo Taluka", "district": "Demo District", "area": 2.31, "sub_division": "A"},
        "provenance": "Synthetic demo document; values are not copied from government records.",
    },
    "DEMO-DOC-AREA-REVIEW": {
        "id": "DEMO-DOC-AREA-REVIEW", "filename": "demo-area-review.pdf", "doc_type": "Synthetic area-mismatch document",
        "status": "DEMO", "ocr_confidence": 0.88, "verification_status": "REVIEW REQUIRED", "property_id": "DEMO-PROP-104",
        "fields": {"survey_number": "DEMO-104", "village": "Demo Village", "taluka": "Demo Taluka", "district": "Demo District", "area": 3.55},
        "provenance": "Synthetic scenario document; values are not copied from government records.",
    },
    "DEMO-DOC-NO-MATCH": {
        "id": "DEMO-DOC-NO-MATCH", "filename": "demo-no-match.pdf", "doc_type": "Synthetic no-match document",
        "status": "DEMO", "ocr_confidence": 0.93, "verification_status": "NO PARCEL MATCH", "property_id": None,
        "fields": {"survey_number": "DEMO-999", "village": "Demo Village", "taluka": "Demo Taluka", "district": "Demo District", "area": 1.10},
        "provenance": "Synthetic scenario document; no corresponding demo parcel exists.",
    },
    "DEMO-DOC-LOW-CONFIDENCE": {
        "id": "DEMO-DOC-LOW-CONFIDENCE", "filename": "demo-low-confidence.pdf", "doc_type": "Synthetic low-confidence document",
        "status": "DEMO", "ocr_confidence": 0.42, "verification_status": "REVIEW REQUIRED", "property_id": "DEMO-PROP-105",
        "fields": {"survey_number": "DEMO-105", "village": "Demo Village", "taluka": "Demo Taluka", "district": "Demo District", "area": 1.80},
        "provenance": "Synthetic low-confidence scenario; confidence is intentionally low for demonstration.",
    },
}


def _parcel(property_id: str) -> Optional[dict]:
    return next((parcel for parcel in DEMO_PARCELS if parcel["property_id"] == property_id or parcel["parcel_id"] == property_id), None)


@router.get("/health")
def health():
    return {"status": "ok", "demo": True, "credentials_required": False}


@router.get("/geojson")
def geojson():
    return deepcopy(DEMO_GEOJSON)


@router.get("/dashboard")
def dashboard():
    return {
        "total_properties": len(DEMO_PARCELS), "documents_processed": len(DEMO_DOCUMENTS),
        "pending_verification": 1, "review_required": 1, "conflicts": 0, "no_parcel_match": 0,
        "low_confidence": 1, "completed_cases": 0, "demo_data": True,
    }


@router.get("/properties")
def properties(q: Optional[str] = None):
    rows = DEMO_PARCELS
    if q:
        needle = q.casefold().strip()
        rows = [
            parcel for parcel in rows
            if needle in " ".join(str(parcel.get(key) or "") for key in (
                "property_id", "parcel_id", "survey_number", "gat_number", "khasra_number", "village", "taluka", "district",
            )).casefold()
        ]
    return {"properties": deepcopy(rows), "total": len(rows)}


@router.get("/properties/{property_id}")
def property_detail(property_id: str):
    parcel = _parcel(property_id)
    if not parcel:
        return {"error": "Property not found"}
    documents = [deepcopy(document) for document in DEMO_DOCUMENTS.values() if document["property_id"] == parcel["property_id"]]
    return {
        **deepcopy(parcel),
        "crs": "EPSG:4326",
        "georeferenced": True,
        "location_status": "PARCEL_GEOMETRY",
        "location": {
            "status": "PARCEL_GEOMETRY", "latitude": parcel.get("latitude"), "longitude": parcel.get("longitude"),
            "source": "Project-owned synthetic geometry", "confidence": parcel.get("geometry_confidence"), "verified_by": None,
        },
        "data_source": "Synthetic/demo dataset",
        "documents": documents,
        "neighbors": [deepcopy(other) for other in DEMO_PARCELS if other["property_id"] != parcel["property_id"] and other["village"] == parcel["village"]],
        "provenance": [{"field_name": "survey_number", "value": parcel["survey_number"], "source": "Synthetic GIS dataset", "confidence": parcel["geometry_confidence"]}],
        "timeline": [{"event_type": "DEMO_DATASET_CREATED", "description": "Synthetic parcel created for demonstration.", "source": "Project synthetic dataset"}],
        "ownership_history": {"events": [], "findings": [], "relationships": [], "legal_authority": False},
        "findings": [], "tasks": [], "legal_authority": False,
    }


@router.get("/scenarios")
def scenarios():
    return {"demo_label": "DEMO / SYNTHETIC DATA", "scenarios": deepcopy(DEMO_SCENARIOS)}


@router.get("/scenario/{scenario_id}")
def scenario_detail(scenario_id: str):
    scenario = next((item for item in DEMO_SCENARIOS if item["id"] == scenario_id), None)
    if not scenario:
        return {"error": "Scenario not found"}
    document = deepcopy(DEMO_DOCUMENTS.get(scenario["document_id"]))
    property_data = deepcopy(_parcel(scenario["property_id"])) if scenario["property_id"] else None
    comparison = compare(scenario["document_id"], scenario["property_id"]) if property_data and document else None
    return {
        "scenario": deepcopy(scenario), "document": document, "property": property_data,
        "property_id": scenario["property_id"], "comparison": comparison,
    }


@router.get("/documents")
def documents():
    return {"documents": deepcopy(list(DEMO_DOCUMENTS.values()))}


@router.get("/compare/{doc_id}/{property_id}")
def compare(doc_id: str, property_id: str):
    document = DEMO_DOCUMENTS.get(doc_id)
    parcel = _parcel(property_id)
    if not document or not parcel:
        return {"error": "Demo document or property not found"}
    checks = []
    for key, label in (("survey_number", "Survey/Gat/Khasra"), ("village", "Village"), ("taluka", "Taluka"), ("district", "District"), ("sub_division", "Subdivision")):
        document_value = document["fields"].get(key)
        parcel_value = parcel.get(key)
        checks.append({
            "field": key, "label": label, "document": document_value, "parcel": parcel_value,
            "status": "INSUFFICIENT EVIDENCE" if not document_value or not parcel_value else (
                "CONSISTENT" if str(document_value).casefold() == str(parcel_value).casefold() else "CONFLICT"
            ),
        })
    difference = round(abs(float(document["fields"]["area"]) - float(parcel["area"])), 2)
    checks.append({
        "field": "area", "label": "Area", "document": document["fields"]["area"], "parcel": parcel["area"],
        "difference": difference, "tolerance": 0.05,
        "status": "CONSISTENT" if difference <= 0.05 else "REVIEW REQUIRED",
    })
    overall = "CONFLICT" if any(check["status"] == "CONFLICT" for check in checks) else (
        "REVIEW REQUIRED" if any(check["status"] == "REVIEW REQUIRED" for check in checks) else (
            "INSUFFICIENT EVIDENCE" if any(check["status"] == "INSUFFICIENT EVIDENCE" for check in checks) else "CONSISTENT"
        )
    )
    return {
        "document_id": doc_id, "property_id": parcel["property_id"], "overall_status": overall, "checks": checks,
        "explanation": "Synthetic demo comparison only. It is not a legal ownership, authenticity, or fraud determination.",
    }
