"""Deterministic demo/test scenarios for the Land Intelligence workflow.

Seeding is EXPLICIT, audited, and administrator-governed. Every artifact is
tagged (document metadata ``demo`` flag / ``DEMO-`` id prefixes) so it can be
wiped without touching production data. Scenarios mirror the acceptance list:
clean record, valid mutation, active encumbrance, ownership change without
mutation, conflicting history, area jump, pending mutation, rejected mutation,
low-quality OCR, conflicting duplicates.

No scenario depends on network access, randomness, or wall-clock ordering:
ids, dates and amounts are fixed constants.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server import (
    ROLE_ADMIN,
    get_db,
    require_roles,
)
from land_intel import _audit, ensure_land_tables

demo_router = APIRouter(prefix="/api/admin/demo", tags=["Demo Scenarios"])

DEMO_UPLOADER = "demo-seeder@landrec.gov.in"

# (scenario id, title, description, risk expectation)
SCENARIOS = [
    {"id": "S1", "title": "Clean land record", "expectation": "CLEAR — single consistent approved record, no encumbrance."},
    {"id": "S2", "title": "Ownership change with valid completed mutation", "expectation": "CLEAR — owner change is bridged by a COMPLETED mutation."},
    {"id": "S3", "title": "Active bank encumbrance", "expectation": "HIGH_RISK — ACTIVE_ENCUMBRANCE flag; mutation completion is gated."},
    {"id": "S4", "title": "Ownership change without mutation", "expectation": "REVIEW — OWNER_CHANGE_NO_MUTATION flag."},
    {"id": "S5", "title": "Conflicting historical document (same year, different owners)", "expectation": "HIGH_RISK — OWNER_CONFLICT_YEAR flag."},
    {"id": "S6", "title": "Large unexpected area change", "expectation": "REVIEW — AREA_JUMP flag (no partition/merger on file)."},
    {"id": "S7", "title": "Pending mutation application", "expectation": "CLEAR with PENDING_MUTATION info — mutation on file, review pending."},
    {"id": "S8", "title": "Rejected mutation", "expectation": "REVIEW — owner change covered only by a REJECTED mutation."},
    {"id": "S9", "title": "Low-quality / blurry OCR document", "expectation": "INFO — LOW_QUALITY_EXTRACTION; queue shows low confidence."},
    {"id": "S10", "title": "Conflicting duplicate record", "expectation": "REVIEW — DUPLICATE_CONFLICT (same owner/year, different areas)."},
]


# Spatial information for the EXISTING demo scenarios. Several demo lands get
# usable reference geometry / coordinates so the Locate workflow can be tested
# end to end (clean, mutation, encumbered, conflicting/high-risk, point-only
# and unresolved). These are the same demo parcels — no second dataset is
# created — and every row is tagged ``DEMO-LI-`` so ``clear_demo_data`` removes
# it without touching production data.
#
# Villages are the scenario villages (Ambedarpur / Barkheda) placed at fixed
# reference coordinates. The geometry is project-owned demo reference data, so
# it is labelled non-authoritative by the resolver.
DEMO_PARCELS: Dict[str, Dict[str, Any]] = {
    "S1": {  # clean record — reference polygon + stored centroid
        "survey": "201", "village": "Ambedarpur",
        "geometry": {"type": "Polygon", "coordinates": [[
            [77.1180, 28.6380], [77.1220, 28.6380], [77.1220, 28.6415], [77.1180, 28.6415], [77.1180, 28.6380],
        ]]},
        "centroid": {"latitude": 28.63975, "longitude": 77.1200},
    },
    "S2": {  # completed mutation — reference polygon + stored centroid
        "survey": "45/2", "village": "Barkheda",
        "geometry": {"type": "Polygon", "coordinates": [[
            [77.0740, 28.6080], [77.0785, 28.6080], [77.0785, 28.6115], [77.0740, 28.6115], [77.0740, 28.6080],
        ]]},
        "centroid": {"latitude": 28.60975, "longitude": 77.07625},
    },
    "S3": {  # active encumbrance (high risk) — reference polygon + stored centroid
        "survey": "103", "village": "Ambedarpur",
        "geometry": {"type": "Polygon", "coordinates": [[
            [77.1160, 28.6418], [77.1205, 28.6418], [77.1205, 28.6450], [77.1160, 28.6450], [77.1160, 28.6418],
        ]]},
        "centroid": {"latitude": 28.64340, "longitude": 77.11825},
    },
    "S4": {  # ownership change without mutation — point-only location
        "survey": "204", "village": "Barkheda",
        "latitude": 28.6075, "longitude": 77.0735,
    },
    "S5": {  # conflicting history (high risk) — polygon without centroid/coordinates;
             # the resolver derives and persists the centroid.
        "survey": "105", "village": "Ambedarpur",
        "geometry": {"type": "Polygon", "coordinates": [[
            [77.1130, 28.6370], [77.1170, 28.6370], [77.1170, 28.6400], [77.1130, 28.6400], [77.1130, 28.6370],
        ]]},
    },
    "S6": {  # unexpected area change — reference polygon
        "survey": "106", "village": "Barkheda",
        "geometry": {"type": "Polygon", "coordinates": [[
            [77.0790, 28.6075], [77.0840, 28.6075], [77.0840, 28.6110], [77.0790, 28.6110], [77.0790, 28.6075],
        ]]},
        "centroid": {"latitude": 28.60925, "longitude": 77.08150},
    },
    "S7": {  # pending mutation — point-only location
        "survey": "207", "village": "Ambedarpur",
        "latitude": 28.6455, "longitude": 77.1240,
    },
    "S8": {  # rejected mutation — reference polygon
        "survey": "208", "village": "Barkheda",
        "geometry": {"type": "Polygon", "coordinates": [[
            [77.0850, 28.6065], [77.0895, 28.6065], [77.0895, 28.6100], [77.0850, 28.6100], [77.0850, 28.6065],
        ]]},
        "centroid": {"latitude": 28.60825, "longitude": 77.08725},
    },
    # S9 (low-quality OCR) intentionally has NO spatial data: it is the demo
    # case for the explicit "Location unavailable — no verified coordinates"
    # state and the resolve-from-address fallback.
    "S10": {  # conflicting duplicate record — reference polygon
        "survey": "210", "village": "Barkheda",
        "geometry": {"type": "Polygon", "coordinates": [[
            [77.0900, 28.6130], [77.0940, 28.6130], [77.0940, 28.6165], [77.0900, 28.6165], [77.0900, 28.6130],
        ]]},
        "centroid": {"latitude": 28.61475, "longitude": 77.09200},
    },
}

DEMO_PARCEL_GEOMETRY_SOURCE = "Demo scenario reference geometry (non-authoritative)"
DEMO_PARCEL_DATA_SOURCE = "Demo scenario dataset (synthetic reference geometry)"


def _demo_parcel(scenario: str, *, documents: List[str]) -> Optional[str]:
    """Create/move the demo spatial record for one scenario and link its docs."""
    definition = DEMO_PARCELS.get(scenario)
    if not definition:
        return None
    property_id = f"DEMO-LI-{scenario}"
    geometry = definition.get("geometry")
    centroid = definition.get("centroid")
    latitude = definition.get("latitude")
    longitude = definition.get("longitude")
    if centroid and latitude is None and longitude is None:
        latitude, longitude = centroid["latitude"], centroid["longitude"]
    location_status = "PARCEL_GEOMETRY" if geometry else "STORED_POINT"
    now = time.time()
    with get_db() as db:
        db.execute(
            """INSERT OR REPLACE INTO properties
               (property_id, parcel_id, district, taluka, village, survey_number, gat_number, khasra_number,
                sub_division, parent_property_id, area, area_unit, geometry, centroid, latitude, longitude,
                crs, georeferenced, geometry_source, geometry_confidence, data_source, source_confidence,
                created_at, updated_at, location_status, location_source, location_confidence,
                location_base_latitude, location_base_longitude, location_base_source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (property_id, property_id, "Demo District", "Sadar", definition["village"], definition["survey"],
             None, definition["survey"], None, None, None, "ha",
             json.dumps(geometry) if geometry else None, json.dumps(centroid) if centroid else None,
             latitude, longitude, "EPSG:4326", 1, DEMO_PARCEL_GEOMETRY_SOURCE, 0.90,
             DEMO_PARCEL_DATA_SOURCE, 0.90, now, now, location_status, DEMO_PARCEL_GEOMETRY_SOURCE, 0.90,
             latitude, longitude, DEMO_PARCEL_GEOMETRY_SOURCE),
        )
        for document_id in documents:
            if not document_id or "-MUT-" in document_id or "-ENC-" in document_id:
                continue  # register artifacts, not screened documents
            db.execute(
                "INSERT OR REPLACE INTO property_documents(property_id, document_id, source_type, linked_at) VALUES (?,?,?,?)",
                (property_id, document_id, "demo_scenario", now),
            )
        db.execute(
            "INSERT OR REPLACE INTO provenance(id, property_id, field_name, value, source, confidence, created_at) VALUES (?,?,?,?,?,?,?)",
            (f"PROV-{property_id}-DEMO-LOCATION", property_id, "location", location_status,
             DEMO_PARCEL_DATA_SOURCE, 0.90, now),
        )
        db.execute(
            "INSERT OR REPLACE INTO property_timeline(id, property_id, event_type, description, source, created_at) VALUES (?,?,?,?,?,?)",
            (f"TL-{property_id}-DEMO-LOCATION", property_id, "DEMO_PARCEL_CREATED",
             "Demo scenario spatial record created for the Locate workflow; reference-only geometry.",
             DEMO_PARCEL_DATA_SOURCE, now),
        )
    return property_id


def _fields(pairs: Dict[str, Any], confidence: float = 0.95) -> Dict[str, Any]:
    return {
        key: {"value": str(value), "confidence": confidence, "validation_status": "VALID", "validation_message": ""}
        for key, value in pairs.items()
    }


def _insert_document(doc_id: str, scenario: str, *, owner: str, survey: str, village: str,
                     tehsil: str, district: str, year: str, area: str, status: str,
                     father: str = "", doc_type: str = "Land Record", confidence: float = 0.95,
                     mean_conf: int = 92) -> None:
    fields = _fields({
        "owner_name": owner, "father_name": father, "survey_number": survey, "khasra_number": survey,
        "khata_number": str(int(survey.split("/")[0]) + 100) if survey.split("/")[0].isdigit() else survey,
        "area": area, "village": village, "tehsil": tehsil, "district": district, "state": "Demo Pradesh",
        "document_date": f"{year}-06-15", "land_class": "Agricultural", "ownership_type": "Bhumidar",
        "khatauni_year": year,
    }, confidence)
    validation = {"status": "VALID" if confidence >= 0.8 else "WARNING",
                  "issues": [] if confidence >= 0.8 else [{"field": "owner_name", "message": "Low OCR confidence — manual verification required"}]}
    if confidence < 0.8:
        for entry in fields.values():
            entry["validation_status"] = "WARNING"
            entry["validation_message"] = "Low OCR confidence — manual verification required"
    now = float(year) if year.isdigit() else time.time()
    with get_db() as db:
        db.execute(
            """INSERT OR REPLACE INTO documents
               (id, filename, doc_type, mean_conf, verdict, status, languages, pages, fields, validation,
                ai_decision_support, ocr_text, cleaned_ocr_text, detected_language, original_fields, metadata,
                uploaded_by, reviewer_comments, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (doc_id, f"demo-{scenario.lower()}-{doc_id}.pdf", doc_type, mean_conf, "review", status, '["eng"]', 1,
             json.dumps(fields), json.dumps(validation), "{}", "DEMO SCENARIO — synthetic OCR text", "", "eng",
             json.dumps(fields), json.dumps({"demo": True, "scenario": scenario}), DEMO_UPLOADER,
             "", now, now),
        )


def _demo_encumbrance(enc_id: str, scenario: str, *, survey: str, village: str, lender: str,
                      reference: str, amount: float, start: str, status: str = "ACTIVE",
                      release: Optional[str] = None, owner: str = "") -> None:
    now = time.time()
    with get_db() as db:
        db.execute(
            """INSERT OR REPLACE INTO land_encumbrances
               (id, property_id, survey_number, khasra_number, village, tehsil, district, owner_name,
                lender, reference_no, amount, start_date, release_date, status, evidence_doc_id, notes,
                created_by, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (enc_id, None, survey, survey, village, "Demo Tehsil", "Demo District", owner, lender, reference,
             amount, start, release, status, None, f"DEMO scenario {scenario}", "demo-seeder", now, now),
        )


def _demo_mutation(mut_id: str, scenario: str, *, mutation_no: str, survey: str, village: str,
                   previous_owner: str, new_owner: str, status: str, reason: str = "SALE",
                   deed_no: str = "", deed_date: str = "", notes: str = "") -> None:
    now = time.time()
    with get_db() as db:
        db.execute(
            """INSERT OR REPLACE INTO land_mutations
               (id, mutation_no, property_id, survey_number, khasra_number, village, tehsil, district,
                previous_owner, new_owner, reason_type, deed_no, deed_date, documents, document_checklist,
                status, risk_status, risk_payload, encumbrance_status, reviewer, reviewer_notes, decided_at,
                created_by, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mut_id, mutation_no, None, survey, survey, village, "Demo Tehsil", "Demo District",
             previous_owner, new_owner, reason, deed_no, deed_date, "[]", "[]", status, "UNKNOWN", "{}",
             "UNKNOWN", "demo-reviewer@landrec.gov.in", notes, now if status in {"COMPLETED", "REJECTED"} else None,
             "demo-seeder", now, now),
        )
        db.execute("INSERT OR REPLACE INTO land_mutation_events (id, mutation_id, status, note, actor, created_at) VALUES (?,?,?,?,?,?)",
                   (f"DEMO-EV-{mut_id}", mut_id, status, notes or f"Demo scenario {scenario}", "demo-seeder", now))


def seed_all() -> Dict[str, Any]:
    """Create all ten deterministic scenarios. Idempotent: reseeding replaces
    the previous demo dataset."""
    ensure_land_tables()
    created: Dict[str, List[str]] = {}

    # -- S1: clean record ----------------------------------------------------
    _insert_document("DEMO-S1-DOC1", "S1", owner="Sita Devi", survey="201", village="Ambedarpur",
                     tehsil="Sadar", district="Demo District", year="2022", area="2.50 ha", status="APPROVED",
                     father="Ram Prasad")
    created["S1"] = ["DEMO-S1-DOC1"]

    # -- S2: ownership change with a valid completed mutation -----------------
    _insert_document("DEMO-S2-DOC1", "S2", owner="Ram Swaroop Sharma", survey="45/2", village="Barkheda",
                     tehsil="Sadar", district="Demo District", year="2019", area="3.20 ha", status="APPROVED",
                     father="Hari Sharma")
    _insert_document("DEMO-S2-DOC2", "S2", owner="Amit Sharma", survey="45/2", village="Barkheda",
                     tehsil="Sadar", district="Demo District", year="2023", area="3.20 ha", status="APPROVED",
                     father="Ram Swaroop Sharma", doc_type="Sale Deed")
    _demo_mutation("DEMO-MUT-S2", "S2", mutation_no="M-2023-0001", survey="45/2", village="Barkheda",
                   previous_owner="Ram Swaroop Sharma", new_owner="Amit Sharma", status="COMPLETED",
                   deed_no="REG-2019-000342", deed_date="2023-02-10", notes="Registered sale deed verified; ownership transferred.")
    created["S2"] = ["DEMO-S2-DOC1", "DEMO-S2-DOC2", "DEMO-MUT-S2"]

    # -- S3: active bank encumbrance ------------------------------------------
    _insert_document("DEMO-S3-DOC1", "S3", owner="Mahesh Verma", survey="103", village="Ambedarpur",
                     tehsil="Sadar", district="Demo District", year="2021", area="1.75 ha", status="APPROVED",
                     father="Gopal Verma")
    _demo_encumbrance("DEMO-ENC-S3", "S3", survey="103", village="Ambedarpur", lender="Example Bank",
                      reference="LN-2026-00123", amount=850000.0, start="2024-11-05", status="ACTIVE",
                      owner="Mahesh Verma")
    created["S3"] = ["DEMO-S3-DOC1", "DEMO-ENC-S3"]

    # -- S4: ownership change WITHOUT mutation --------------------------------
    _insert_document("DEMO-S4-DOC1", "S4", owner="Harish Chandra", survey="204", village="Barkheda",
                     tehsil="Sadar", district="Demo District", year="2018", area="2.00 ha", status="APPROVED")
    _insert_document("DEMO-S4-DOC2", "S4", owner="Premwati Devi", survey="204", village="Barkheda",
                     tehsil="Sadar", district="Demo District", year="2022", area="2.00 ha", status="APPROVED")
    created["S4"] = ["DEMO-S4-DOC1", "DEMO-S4-DOC2"]

    # -- S5: conflicting historical document ----------------------------------
    _insert_document("DEMO-S5-DOC1", "S5", owner="Jagdish Yadav", survey="105", village="Ambedarpur",
                     tehsil="Sadar", district="Demo District", year="2021", area="1.60 ha", status="APPROVED")
    _insert_document("DEMO-S5-DOC2", "S5", owner="Kallu Yadav", survey="105", village="Ambedarpur",
                     tehsil="Sadar", district="Demo District", year="2021", area="1.60 ha", status="APPROVED")
    created["S5"] = ["DEMO-S5-DOC1", "DEMO-S5-DOC2"]

    # -- S6: large unexpected area change --------------------------------------
    _insert_document("DEMO-S6-DOC1", "S6", owner="Shyam Lal", survey="106", village="Barkheda",
                     tehsil="Sadar", district="Demo District", year="2019", area="2.50 ha", status="APPROVED")
    _insert_document("DEMO-S6-DOC2", "S6", owner="Shyam Lal", survey="106", village="Barkheda",
                     tehsil="Sadar", district="Demo District", year="2023", area="4.30 ha", status="APPROVED")
    created["S6"] = ["DEMO-S6-DOC1", "DEMO-S6-DOC2"]

    # -- S7: pending mutation ---------------------------------------------------
    _insert_document("DEMO-S7-DOC1", "S7", owner="Ram Swaroop Sharma", survey="207", village="Ambedarpur",
                     tehsil="Sadar", district="Demo District", year="2020", area="0.80 ha", status="APPROVED")
    _insert_document("DEMO-S7-DOC2", "S7", owner="Amit Sharma", survey="207", village="Ambedarpur",
                     tehsil="Sadar", district="Demo District", year="2024", area="0.80 ha", status="APPROVED",
                     doc_type="Sale Deed")
    _demo_mutation("DEMO-MUT-S7", "S7", mutation_no="M-2026-0012", survey="207", village="Ambedarpur",
                   previous_owner="Ram Swaroop Sharma", new_owner="Amit Sharma", status="UNDER_REVIEW",
                   deed_no="REG-2024-000518", deed_date="2024-08-19")
    created["S7"] = ["DEMO-S7-DOC1", "DEMO-S7-DOC2", "DEMO-MUT-S7"]

    # -- S8: rejected mutation ---------------------------------------------------
    _insert_document("DEMO-S8-DOC1", "S8", owner="Om Prakash", survey="208", village="Barkheda",
                     tehsil="Sadar", district="Demo District", year="2020", area="1.20 ha", status="APPROVED")
    _insert_document("DEMO-S8-DOC2", "S8", owner="Suresh Kumar", survey="208", village="Barkheda",
                     tehsil="Sadar", district="Demo District", year="2023", area="1.20 ha", status="APPROVED")
    _demo_mutation("DEMO-MUT-S8", "S8", mutation_no="M-2024-0002", survey="208", village="Barkheda",
                   previous_owner="Om Prakash", new_owner="Suresh Kumar", status="REJECTED",
                   deed_date="2023-09-02", notes="Rejected: sale deed signature could not be verified against the original.")
    created["S8"] = ["DEMO-S8-DOC1", "DEMO-S8-DOC2", "DEMO-MUT-S8"]

    # -- S9: low-quality / blurry OCR --------------------------------------------
    _insert_document("DEMO-S9-DOC1", "S9", owner="R?m Bah?dur Singh", survey="209", village="Ambedarpur",
                     tehsil="Sadar", district="Demo District", year="2023", area="2.20 ha", status="DRAFT",
                     confidence=0.42, mean_conf=44)
    created["S9"] = ["DEMO-S9-DOC1"]

    # -- S10: conflicting duplicate record ----------------------------------------
    _insert_document("DEMO-S10-DOC1", "S10", owner="Dinesh Chand", survey="210", village="Barkheda",
                     tehsil="Sadar", district="Demo District", year="2022", area="1.80 ha", status="APPROVED")
    _insert_document("DEMO-S10-DOC2", "S10", owner="Dinesh Chand", survey="210", village="Barkheda",
                     tehsil="Sadar", district="Demo District", year="2022", area="3.20 ha", status="APPROVED")
    created["S10"] = ["DEMO-S10-DOC1", "DEMO-S10-DOC2"]

    # -- Spatial records for the Locate workflow ------------------------------
    # Reference geometry / coordinates live on the same scenario parcels so that
    # Locate can be exercised for a clean parcel (S1), a mutation parcel (S2),
    # an encumbered/high-risk parcel (S3), a point-only parcel (S4, S7), a
    # geometry-without-centroid parcel (S5) and unresolved data (S9).
    for scenario, definition in DEMO_PARCELS.items():
        document_ids = [item for item in created.get(scenario, []) if item.startswith("DEMO-S")]
        property_id = _demo_parcel(scenario, documents=document_ids)
        if property_id:
            created.setdefault(scenario, []).append(property_id)

    return {"scenarios": sorted(created.keys()), "artifacts": created}


class SeedRequest(BaseModel):
    scenario: str = "all"


@demo_router.get("/scenarios")
def list_scenarios(user: Dict[str, Any] = Depends(require_roles(ROLE_ADMIN))):
    return {"scenarios": SCENARIOS}


@demo_router.post("/seed")
def seed_scenarios(req: SeedRequest, user: Dict[str, Any] = Depends(require_roles(ROLE_ADMIN))):
    """Seed deterministic demo scenarios (administrator only, audited)."""
    wanted = (req.scenario or "all").strip().upper()
    known = {item["id"] for item in SCENARIOS}
    if wanted != "ALL" and wanted not in known:
        raise HTTPException(status_code=422, detail=f"Unknown scenario '{req.scenario}'. Use 'all' or one of {sorted(known)}.")
    result = seed_all()  # deterministic dataset: seeding is all-or-nothing and idempotent
    _audit(user, "DEMO_DATA_SEEDED", f"Demo scenarios seeded ({wanted}); artifacts: {sum(len(v) for v in result['artifacts'].values())}")
    return {"status": "seeded", "requested": wanted, **result}


@demo_router.delete("/data")
def clear_demo_data(user: Dict[str, Any] = Depends(require_roles(ROLE_ADMIN))):
    """Remove ONLY demo-tagged artifacts. Production data is untouched."""
    ensure_land_tables()
    removed = {"documents": 0, "encumbrances": 0, "mutations": 0, "events": 0, "properties": 0}
    with get_db() as db:
        demo_doc_ids = [row["id"] for row in db.execute("SELECT id, metadata FROM documents").fetchall()
                        if _is_demo_metadata(row["metadata"])]
        if demo_doc_ids:
            placeholders = ",".join("?" for _ in demo_doc_ids)
            db.execute(f"DELETE FROM documents WHERE id IN ({placeholders})", tuple(demo_doc_ids))
            removed["documents"] = len(demo_doc_ids)
        counts = db.execute("DELETE FROM land_encumbrances WHERE id LIKE 'DEMO-%'").rowcount
        removed["encumbrances"] = counts or 0
        demo_mutation_ids = [row["id"] for row in db.execute("SELECT id FROM land_mutations WHERE id LIKE 'DEMO-%'").fetchall()]
        if demo_mutation_ids:
            placeholders = ",".join("?" for _ in demo_mutation_ids)
            db.execute(f"DELETE FROM land_mutation_events WHERE mutation_id IN ({placeholders})", tuple(demo_mutation_ids))
            db.execute(f"DELETE FROM land_mutations WHERE id IN ({placeholders})", tuple(demo_mutation_ids))
            removed["mutations"] = len(demo_mutation_ids)
        removed["events"] = removed["mutations"]
        demo_property_ids = [row["property_id"] for row in db.execute("SELECT property_id FROM properties WHERE property_id LIKE 'DEMO-LI-%'").fetchall()]
        removed["properties"] = len(demo_property_ids)
        for property_id in demo_property_ids:
            db.execute("DELETE FROM property_documents WHERE property_id = ?", (property_id,))
            db.execute("DELETE FROM property_timeline WHERE property_id = ?", (property_id,))
            db.execute("DELETE FROM provenance WHERE property_id = ?", (property_id,))
            db.execute("DELETE FROM properties WHERE property_id = ?", (property_id,))
    _audit(user, "DEMO_DATA_CLEARED", f"Demo data cleared: {removed}")
    return {"status": "cleared", "removed": removed}


def _is_demo_metadata(metadata_raw: Any) -> bool:
    try:
        metadata = json.loads(metadata_raw) if isinstance(metadata_raw, str) else (metadata_raw or {})
        return bool(metadata.get("demo"))
    except Exception:
        return False
