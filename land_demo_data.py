"""Synthetic Land Intelligence demo dataset (namespace ``DEMO-LI-``).

This module is DATA, not business logic. It seeds the EXISTING Land
Intelligence surfaces only:

  * screened documents (the canonical ``documents`` table — parcels are derived
    from ``survey_number + village`` exactly as the application already does);
  * the mutation register (``land_mutations`` + ``land_mutation_events``);
  * the encumbrance register (``land_encumbrances``);
  * the court-case register (``land_court_cases`` + ``land_case_orders``);
  * controlled parcel rows in the existing mapping layer (``properties``) so
    uploaded documents resolve through the existing ``mapping._resolve`` path.

Everything is fictional: persons are bare invented single names (Gautam,
Saurav, Adarsh, Shivangi, Kiran, Manohar, Sarita, Bhavesh, Yashoda, Pramila,
Devendra, Lakhan, Vrinda, Omkar, Girija, Shyam, Devaki, Nandkishor, Raghav,
Bhola); banks/courts/case numbers are invented; no real Aadhaar/PAN, deed,
case or survey identifiers are used. Nothing here is a real government record.

Seeding is EXPLICIT, deterministic, idempotent, tagged (``metadata.demo`` flag
on documents / ``DEMO-LI-`` id prefixes on registers), audited by the caller,
and removable via ``clear_all()`` without touching production data.
"""
from __future__ import annotations

import calendar
import json
import time
from typing import Any, Dict, List, Optional

from server import get_db
from land_intel import ensure_land_tables

DATASET = "DEMO-LI"
SEEDER = "demo-seeder@landrec.gov.in"

# All demo geography is fictional and deliberately distinct from the older
# S1-S10 demo dataset (Ambedarpur / Barkheda) so the two never interfere.
STATE = "Demo Pradesh"
DISTRICT = "Kishandham"
TEHSIL = "Hariharpur"
VILLAGE_DEVNAPUR = "Devnapur"
VILLAGE_SHANTIBAN = "Shantiban"

def _ts(date_text: str) -> float:
    """Deterministic epoch for a YYYY-MM-DD date (no wall-clock dependency)."""
    try:
        return float(calendar.timegm(time.strptime(date_text, "%Y-%m-%d"))) + 43200.0
    except Exception:
        return 0.0


def _fields(pairs: Dict[str, Any], confidence: float = 0.95) -> Dict[str, Any]:
    return {
        key: {"value": str(value), "confidence": confidence, "validation_status": "VALID" if confidence >= 0.8 else "WARNING",
              "validation_message": "" if confidence >= 0.8 else "Low OCR confidence — manual verification required"}
        for key, value in pairs.items()
    }


def _insert_document(doc_id: str, scenario: str, *, owner: str, survey: str, village: str,
                     year: str, area: str, status: str = "APPROVED", father: str = "",
                     doc_type: str = "Land Record", confidence: float = 0.95, mean_conf: int = 92,
                     filename: Optional[str] = None, ocr_text: str = "") -> None:
    fields = _fields({
        "owner_name": owner, "father_name": father, "survey_number": survey, "khasra_number": survey,
        "khata_number": f"KH-{survey.replace('/', '-')}", "plot_number": survey,
        "area": area, "village": village, "tehsil": TEHSIL, "district": DISTRICT, "state": STATE,
        "document_date": f"{year}-06-15", "land_class": "Agricultural", "ownership_type": "Bhumidar",
        "khatauni_year": year,
    }, confidence)
    validation = {"status": "VALID" if confidence >= 0.8 else "WARNING",
                  "issues": [] if confidence >= 0.8 else [{"field": "owner_name", "message": "Low OCR confidence — manual verification required"}]}
    created = _ts(f"{year}-07-01") if year.isdigit() else _ts("2026-01-01")
    metadata = {"demo": True, "dataset": DATASET, "scenario": scenario}
    with get_db() as db:
        db.execute(
            """INSERT OR REPLACE INTO documents
               (id, filename, doc_type, mean_conf, verdict, status, languages, pages, fields, validation,
                ai_decision_support, ocr_text, cleaned_ocr_text, detected_language, original_fields, metadata,
                uploaded_by, reviewer_comments, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (doc_id, filename or f"demo-li/{doc_id.lower()}.png", doc_type, mean_conf, "review", status, '["eng"]', 1,
             json.dumps(fields), json.dumps(validation), "{}",
             ocr_text or "SYNTHETIC DEMO DOCUMENT — NOT A REAL GOVERNMENT RECORD", "", "eng",
             json.dumps(fields), json.dumps(metadata), SEEDER, "", created, created),
        )


def _insert_mutation(mut_id: str, scenario: str, *, mutation_no: str, survey: str, village: str,
                     previous_owner: str, new_owner: str, status: str, reason: str = "SALE",
                     deed_no: str = "", deed_date: str = "", notes: str = "", decided_on: str = "") -> None:
    created = _ts(deed_date) if deed_date else _ts("2026-01-05")
    decided = _ts(decided_on) if decided_on else None
    metadata_notes = f"{notes} [synthetic demo record {DATASET} {scenario}]" if notes else f"[synthetic demo record {DATASET} {scenario}]"
    with get_db() as db:
        db.execute(
            """INSERT OR REPLACE INTO land_mutations
               (id, mutation_no, property_id, survey_number, khasra_number, village, tehsil, district,
                previous_owner, new_owner, reason_type, deed_no, deed_date, documents, document_checklist,
                status, risk_status, risk_payload, encumbrance_status, reviewer, reviewer_notes, decided_at,
                created_by, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mut_id, mutation_no, None, survey, survey, village, TEHSIL, DISTRICT,
             previous_owner, new_owner, reason, deed_no, deed_date, "[]", "[]", status, "UNKNOWN", "{}",
             "UNKNOWN", "demo-reviewer@landrec.gov.in", metadata_notes, decided, SEEDER, created, created),
        )
        db.execute(
            "INSERT OR REPLACE INTO land_mutation_events (id, mutation_id, status, note, actor, created_at) VALUES (?,?,?,?,?,?)",
            (f"{DATASET}-EV-{mut_id}", mut_id, status, notes or f"Synthetic demo mutation ({scenario})", "demo-seeder", created),
        )


def _insert_encumbrance(enc_id: str, scenario: str, *, survey: str, village: str, lender: str,
                        reference: str, amount: Optional[float], start: str, status: str = "ACTIVE",
                        release: Optional[str] = None, owner: str = "", notes: str = "") -> None:
    created = _ts(start) if start else _ts("2026-01-05")
    with get_db() as db:
        db.execute(
            """INSERT OR REPLACE INTO land_encumbrances
               (id, property_id, survey_number, khasra_number, village, tehsil, district, owner_name,
                lender, reference_no, amount, start_date, release_date, status, evidence_doc_id, notes,
                created_by, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (enc_id, None, survey, survey, village, TEHSIL, DISTRICT, owner, lender, reference,
             amount, start, release, status, None, notes or f"Synthetic demo encumbrance ({scenario})",
             "demo-seeder", created, created),
        )


def _insert_case(case_id: str, scenario: str, *, case_no: str, survey: str, village: str,
                 court: str, case_type: str, title: str, petitioner: str, respondent: str,
                 filing_date: str, status: str, stage: str, issue: str,
                 next_hearing: Optional[str] = None, affects_transfer: bool = False,
                 related_mutation: str = "", notes: str = "", orders: Optional[List[Dict[str, str]]] = None) -> None:
    created = _ts(filing_date) if filing_date else _ts("2026-01-05")
    with get_db() as db:
        db.execute(
            """INSERT OR REPLACE INTO land_court_cases
               (id, case_no, property_id, survey_number, khasra_number, village, tehsil, district, court_name,
                case_type, title, petitioner, respondent, filing_date, status, stage, issue_summary,
                next_hearing_date, affects_transfer, related_mutation_id, related_encumbrance_id,
                evidence_doc_id, notes, created_by, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (case_id, case_no, None, survey, survey, village, TEHSIL, DISTRICT, court,
             case_type, title, petitioner, respondent, filing_date, status, stage, issue,
             next_hearing, 1 if affects_transfer else 0, related_mutation or None, None,
             None, notes or f"Synthetic demo litigation ({scenario})", SEEDER, created, created),
        )
        for index, order in enumerate(orders or [], start=1):
            db.execute(
                """INSERT OR REPLACE INTO land_case_orders (id, case_id, order_date, order_type, summary, created_by, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (f"{DATASET}-ORD-{case_id}-{index:02d}", case_id, order.get("date", ""), order.get("type", "ORDER"),
                 order.get("summary", ""), "demo-seeder", _ts(order.get("date") or "2026-01-05")),
            )


def _insert_property(index: int, scenario: str, *, survey: str, village: str, area_value: float) -> None:
    """Controlled parcel row so uploaded documents resolve through the EXISTING
    mapping property-resolution path (mapping._resolve) as well."""
    property_id = f"{DATASET}-PROP-{index:03d}"
    now = _ts("2026-01-01")
    with get_db() as db:
        db.execute(
            """INSERT INTO properties (
                property_id, parcel_id, district, taluka, village, survey_number, gat_number, khasra_number,
                sub_division, parent_property_id, area, area_unit, geometry, centroid, latitude, longitude,
                crs, georeferenced, geometry_source, geometry_confidence, data_source, source_confidence,
                created_at, updated_at, location_status, location_source, location_confidence,
                location_verified_by, location_verified_at, location_updated_at,
                location_base_latitude, location_base_longitude, location_base_source
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(property_id) DO NOTHING""",
            (property_id, f"{DATASET}-PARCEL-{index:03d}", DISTRICT, TEHSIL, village, survey, "", survey,
             "", None, area_value, "hectare", None, None, None, None,
             None, 0, "Synthetic demo parcel — coordinates intentionally unresolved", None,
             "Synthetic demo dataset (not a government record)", 1.0,
             now, now, "UNRESOLVED", "demo-dataset", None, None, None, None, None, None, "demo-dataset"),
        )
        db.execute(
            "INSERT OR IGNORE INTO provenance(id, property_id, field_name, value, source, confidence, created_at) VALUES (?,?,?,?,?,?,?)",
            (f"{DATASET}-PROV-{property_id}-IDENTITY", property_id, "survey_number", survey,
             "Synthetic demo dataset", 1.0, now),
        )
        db.execute(
            "INSERT OR IGNORE INTO property_timeline(id, property_id, event_type, description, source, created_at) VALUES (?,?,?,?,?,?)",
            (f"{DATASET}-TL-{property_id}-CREATED", property_id, "DEMO_DATASET_CREATED",
             f"Synthetic parcel added by the {DATASET} demo dataset; not an authoritative cadastral record.",
             "demo-dataset", now),
        )


# ---------------------------------------------------------------------------
# The dataset — 19 parcels across two fictional villages, grouped by scenario.
# Survey numbers are fictional and unique per village.
# ---------------------------------------------------------------------------

MUT_CASE = "MUT"
ENC_CASE = "ENC"
COURT_CASE = "COURT"
RISK_CASE = "RISK"


def seed_all() -> Dict[str, Any]:
    """Create the whole DEMO-LI dataset. Idempotent: re-running replaces the
    same fixed-id rows and never duplicates. Returns per-scenario artifacts."""
    ensure_land_tables()
    created: Dict[str, List[str]] = {}
    prop_index = 0

    def track(scenario: str, *artifact_ids: str) -> None:
        created.setdefault(scenario, [])
        created[scenario].extend(artifact_ids)

    # == MUTATION scenarios (village Devnapur) ==============================

    # DEMO-LI-MUT-001: original owner -> registered transfer -> mutation approved
    prop_index += 1; _insert_property(prop_index, "LI-MUT-001", survey="71/2", village=VILLAGE_DEVNAPUR, area_value=2.5)
    _insert_document("DEMO-LI-MUT-001-DOC1", "LI-MUT-001", owner="Gautam", father="Hukam",
                     survey="71/2", village=VILLAGE_DEVNAPUR, year="2016", area="2.50 ha")
    _insert_document("DEMO-LI-MUT-001-DOC2", "LI-MUT-001", owner="Saurav", father="Gautam",
                     survey="71/2", village=VILLAGE_DEVNAPUR, year="2024", area="2.50 ha",
                     doc_type="Sale Deed")
    _insert_mutation("DEMO-LI-MUT-001-M1", "LI-MUT-001", mutation_no="M/DEMO/2024/0041",
                     survey="71/2", village=VILLAGE_DEVNAPUR, previous_owner="Gautam", new_owner="Saurav",
                     status="COMPLETED", deed_no="DEMO-LI-DEED-2024-041", deed_date="2024-01-18",
                     decided_on="2024-02-15", notes="Registered sale deed verified; mutation approved and completed.")
    track("LI-MUT-001", "DEMO-LI-MUT-001-DOC1", "DEMO-LI-MUT-001-DOC2", "DEMO-LI-MUT-001-M1")

    # DEMO-LI-MUT-002: transfer application pending (UNDER_REVIEW)
    prop_index += 1; _insert_property(prop_index, "LI-MUT-002", survey="71/8", village=VILLAGE_DEVNAPUR, area_value=1.6)
    _insert_document("DEMO-LI-MUT-002-DOC1", "LI-MUT-002", owner="Devendra", father="Kishan",
                     survey="71/8", village=VILLAGE_DEVNAPUR, year="2018", area="1.60 ha")
    _insert_document("DEMO-LI-MUT-002-DOC2", "LI-MUT-002", owner="Bhavesh", father="Devendra",
                     survey="71/8", village=VILLAGE_DEVNAPUR, year="2025", area="1.60 ha",
                     doc_type="Sale Deed")
    _insert_mutation("DEMO-LI-MUT-002-M1", "LI-MUT-002", mutation_no="M/DEMO/2025/0011",
                     survey="71/8", village=VILLAGE_DEVNAPUR, previous_owner="Devendra", new_owner="Bhavesh",
                     status="UNDER_REVIEW", deed_no="DEMO-LI-DEED-2025-011", deed_date="2025-04-02",
                     notes="Transfer application received; awaiting revenue inspector field report.")
    track("LI-MUT-002", "DEMO-LI-MUT-002-DOC1", "DEMO-LI-MUT-002-DOC2", "DEMO-LI-MUT-002-M1")

    # DEMO-LI-MUT-003: mutation rejected — supporting documentation incomplete
    prop_index += 1; _insert_property(prop_index, "LI-MUT-003", survey="72/4", village=VILLAGE_DEVNAPUR, area_value=2.2)
    _insert_document("DEMO-LI-MUT-003-DOC1", "LI-MUT-003", owner="Manohar", father="Dayaram",
                     survey="72/4", village=VILLAGE_DEVNAPUR, year="2019", area="2.20 ha")
    _insert_document("DEMO-LI-MUT-003-DOC2", "LI-MUT-003", owner="Pramila", father="Manohar",
                     survey="72/4", village=VILLAGE_DEVNAPUR, year="2023", area="2.20 ha",
                     doc_type="Sale Deed")
    _insert_mutation("DEMO-LI-MUT-003-M1", "LI-MUT-003", mutation_no="M/DEMO/2023/0087",
                     survey="72/4", village=VILLAGE_DEVNAPUR, previous_owner="Manohar", new_owner="Pramila",
                     status="REJECTED", deed_no="DEMO-LI-DEED-2023-087", deed_date="2023-08-14",
                     decided_on="2023-10-02",
                     notes="Rejected: supporting documentation incomplete — certified copy of the sale deed and identity proof were never submitted.")
    track("LI-MUT-003", "DEMO-LI-MUT-003-DOC1", "DEMO-LI-MUT-003-DOC2", "DEMO-LI-MUT-003-M1")

    # DEMO-LI-MUT-004: applicant name does not match the source deed
    prop_index += 1; _insert_property(prop_index, "LI-MUT-004", survey="118", village=VILLAGE_SHANTIBAN, area_value=1.8)
    _insert_document("DEMO-LI-MUT-004-DOC1", "LI-MUT-004", owner="Manohar", father="Dayaram",
                     survey="118", village=VILLAGE_SHANTIBAN, year="2020", area="1.80 ha")
    _insert_document("DEMO-LI-MUT-004-DOC2", "LI-MUT-004", owner="Vrinda", father="Triloki",
                     survey="118", village=VILLAGE_SHANTIBAN, year="2024", area="1.80 ha",
                     doc_type="Sale Deed")
    _insert_mutation("DEMO-LI-MUT-004-M1", "LI-MUT-004", mutation_no="M/DEMO/2024/0152",
                     survey="118", village=VILLAGE_SHANTIBAN, previous_owner="Manohar", new_owner="Omkar",
                     status="UNDER_REVIEW", deed_no="DEMO-LI-DEED-2024-152", deed_date="2024-11-06",
                     notes="Name mismatch: application was filed in the name of Omkar while the source deed DEMO-LI-DEED-2024-152 names Vrinda as transferee. Clarification letter issued to the applicant; review pending.")
    track("LI-MUT-004", "DEMO-LI-MUT-004-DOC1", "DEMO-LI-MUT-004-DOC2", "DEMO-LI-MUT-004-M1")

    # DEMO-LI-MUT-005: several historical owners, two completed mutations
    prop_index += 1; _insert_property(prop_index, "LI-MUT-005", survey="72/14", village=VILLAGE_DEVNAPUR, area_value=3.05)
    _insert_document("DEMO-LI-MUT-005-DOC1", "LI-MUT-005", owner="Lakhan", father="Bedram",
                     survey="72/14", village=VILLAGE_DEVNAPUR, year="2012", area="3.05 ha")
    _insert_document("DEMO-LI-MUT-005-DOC2", "LI-MUT-005", owner="Kiran", father="Lakhan",
                     survey="72/14", village=VILLAGE_DEVNAPUR, year="2019", area="3.05 ha")
    _insert_document("DEMO-LI-MUT-005-DOC3", "LI-MUT-005", owner="Yashoda", father="Lakhan",
                     survey="72/14", village=VILLAGE_DEVNAPUR, year="2024", area="3.05 ha")
    _insert_mutation("DEMO-LI-MUT-005-M1", "LI-MUT-005", mutation_no="M/DEMO/2019/0044",
                     survey="72/14", village=VILLAGE_DEVNAPUR, previous_owner="Lakhan", new_owner="Kiran",
                     status="COMPLETED", deed_no="DEMO-LI-DEED-2019-044", deed_date="2019-05-21",
                     decided_on="2019-06-30", notes="Transfer by registered sale deed; mutation completed.")
    _insert_mutation("DEMO-LI-MUT-005-M2", "LI-MUT-005", mutation_no="M/DEMO/2024/0063",
                     survey="72/14", village=VILLAGE_DEVNAPUR, previous_owner="Kiran", new_owner="Yashoda",
                     status="COMPLETED", deed_no="DEMO-LI-DEED-2024-063", deed_date="2024-02-08",
                     decided_on="2024-03-10", notes="Transfer by registered sale deed; mutation completed.")
    track("LI-MUT-005", "DEMO-LI-MUT-005-DOC1", "DEMO-LI-MUT-005-DOC2", "DEMO-LI-MUT-005-DOC3",
          "DEMO-LI-MUT-005-M1", "DEMO-LI-MUT-005-M2")

    # == ENCUMBRANCE scenarios (village Shantiban) ==========================

    # DEMO-LI-ENC-001: no known encumbrance (clean)
    prop_index += 1; _insert_property(prop_index, "LI-ENC-001", survey="121", village=VILLAGE_SHANTIBAN, area_value=0.95)
    _insert_document("DEMO-LI-ENC-001-DOC1", "LI-ENC-001", owner="Kiran", father="Lakhan",
                     survey="121", village=VILLAGE_SHANTIBAN, year="2023", area="0.95 ha")
    track("LI-ENC-001", "DEMO-LI-ENC-001-DOC1")

    # DEMO-LI-ENC-002: existing bank mortgage (active)
    prop_index += 1; _insert_property(prop_index, "LI-ENC-002", survey="122", village=VILLAGE_SHANTIBAN, area_value=2.75)
    _insert_document("DEMO-LI-ENC-002-DOC1", "LI-ENC-002", owner="Manohar", father="Dayaram",
                     survey="122", village=VILLAGE_SHANTIBAN, year="2022", area="2.75 ha")
    _insert_encumbrance("DEMO-LI-ENC-002-E1", "LI-ENC-002", survey="122", village=VILLAGE_SHANTIBAN,
                        lender="Demo Gramin Bank (synthetic)", reference="DEMO-LN-2025-014", amount=650000.0,
                        start="2025-02-10", status="ACTIVE", owner="Manohar",
                        notes="Cash-credit mortgage of the recorded holding; synthetic loan reference.")
    track("LI-ENC-002", "DEMO-LI-ENC-002-DOC1", "DEMO-LI-ENC-002-E1")

    # DEMO-LI-ENC-003: registered lease against the property
    prop_index += 1; _insert_property(prop_index, "LI-ENC-003", survey="123", village=VILLAGE_SHANTIBAN, area_value=1.3)
    _insert_document("DEMO-LI-ENC-003-DOC1", "LI-ENC-003", owner="Sarita", father="Munna",
                     survey="123", village=VILLAGE_SHANTIBAN, year="2021", area="1.30 ha")
    _insert_encumbrance("DEMO-LI-ENC-003-E1", "LI-ENC-003", survey="123", village=VILLAGE_SHANTIBAN,
                        lender="Lakhan (lessee under registered lease)", reference="DEMO-LI-LEASE-003",
                        amount=None, start="2023-11-01", status="ACTIVE", owner="Sarita",
                        notes="Registered lease deed DEMO-LI-LEASE-003; term 2023-11-01 to 2026-10-31; recorded as an active encumbrance until expiry.")
    track("LI-ENC-003", "DEMO-LI-ENC-003-DOC1", "DEMO-LI-ENC-003-E1")

    # DEMO-LI-ENC-004: multiple encumbrances (two active + one released)
    prop_index += 1; _insert_property(prop_index, "LI-ENC-004", survey="124", village=VILLAGE_SHANTIBAN, area_value=4.1)
    _insert_document("DEMO-LI-ENC-004-DOC1", "LI-ENC-004", owner="Bhavesh", father="Devendra",
                     survey="124", village=VILLAGE_SHANTIBAN, year="2019", area="4.10 ha")
    _insert_encumbrance("DEMO-LI-ENC-004-E1", "LI-ENC-004", survey="124", village=VILLAGE_SHANTIBAN,
                        lender="Demo Gramin Bank (synthetic)", reference="DEMO-LN-2024-102", amount=400000.0,
                        start="2024-12-01", status="ACTIVE", owner="Bhavesh",
                        notes="Term-loan mortgage over the holding.")
    _insert_encumbrance("DEMO-LI-ENC-004-E2", "LI-ENC-004", survey="124", village=VILLAGE_SHANTIBAN,
                        lender="Kisan Sahkari Samiti (synthetic)", reference="DEMO-CH-2025-033", amount=125000.0,
                        start="2025-06-15", status="ACTIVE", owner="Bhavesh",
                        notes="Society charge recorded against produce share.")
    _insert_encumbrance("DEMO-LI-ENC-004-E3", "LI-ENC-004", survey="124", village=VILLAGE_SHANTIBAN,
                        lender="Nagar Finance (synthetic)", reference="DEMO-LN-2019-054", amount=220000.0,
                        start="2019-08-01", status="RELEASED", release="2023-03-30", owner="Bhavesh",
                        notes="Loan fully repaid; release recorded with reference to lender letter DEMO-LI-NOC-2019-054.")
    track("LI-ENC-004", "DEMO-LI-ENC-004-DOC1", "DEMO-LI-ENC-004-E1", "DEMO-LI-ENC-004-E2", "DEMO-LI-ENC-004-E3")

    # DEMO-LI-ENC-005: old encumbrance that has been released
    prop_index += 1; _insert_property(prop_index, "LI-ENC-005", survey="125", village=VILLAGE_SHANTIBAN, area_value=2.4)
    _insert_document("DEMO-LI-ENC-005-DOC1", "LI-ENC-005", owner="Yashoda", father="Lakhan",
                     survey="125", village=VILLAGE_SHANTIBAN, year="2018", area="2.40 ha")
    _insert_encumbrance("DEMO-LI-ENC-005-E1", "LI-ENC-005", survey="125", village=VILLAGE_SHANTIBAN,
                        lender="Purvi Vikas Bank (synthetic)", reference="DEMO-LN-2019-021", amount=300000.0,
                        start="2019-06-11", status="RELEASED", release="2023-10-05", owner="Yashoda",
                        notes="Loan fully repaid; bank release entry DEMO-LI-NOC-2023-021 recorded.")
    track("LI-ENC-005", "DEMO-LI-ENC-005-DOC1", "DEMO-LI-ENC-005-E1")

    # DEMO-LI-ENC-006: conflicting / unclear encumbrance information
    prop_index += 1; _insert_property(prop_index, "LI-ENC-006", survey="126", village=VILLAGE_SHANTIBAN, area_value=1.1)
    _insert_document("DEMO-LI-ENC-006-DOC1", "LI-ENC-006", owner="Girija", father="Shyam",
                     survey="126", village=VILLAGE_SHANTIBAN, year="2020", area="1.10 ha")
    _insert_encumbrance("DEMO-LI-ENC-006-E1", "LI-ENC-006", survey="126", village=VILLAGE_SHANTIBAN,
                        lender="Demo Gramin Bank (synthetic)", reference="DEMO-LN-2023-091", amount=180000.0,
                        start="2023-05-20", status="ACTIVE", owner="Girija",
                        notes="CONFLICTING INFORMATION: owner claims a no-dues letter was issued after repayment, but the branch register still shows ACTIVE. Status stands as registered until a verified release is produced.")
    _insert_encumbrance("DEMO-LI-ENC-006-E2", "LI-ENC-006", survey="126", village=VILLAGE_SHANTIBAN,
                        lender="Demo Gramin Bank (synthetic)", reference="DEMO-LN-2018-147", amount=90000.0,
                        start="2018-12-01", status="UNKNOWN", owner="Girija",
                        notes="UNCLEAR: older loan recorded against the adjoining khata part; branch merged and the original ledger could not be traced. Status deliberately left UNKNOWN pending verification.")
    track("LI-ENC-006", "DEMO-LI-ENC-006-DOC1", "DEMO-LI-ENC-006-E1", "DEMO-LI-ENC-006-E2")

    # == COURT CASE / LITIGATION scenarios ===================================

    # DEMO-LI-COURT-001: Adarsh v. Shivangi — ordinary civil title/boundary dispute (pending)
    prop_index += 1; _insert_property(prop_index, "LI-COURT-001", survey="131", village=VILLAGE_SHANTIBAN, area_value=2.6)
    _insert_document("DEMO-LI-COURT-001-DOC1", "LI-COURT-001", owner="Adarsh", father="Hari",
                     survey="131", village=VILLAGE_SHANTIBAN, year="2018", area="2.60 ha",
                     doc_type="Registered Sale Deed")
    _insert_document("DEMO-LI-COURT-001-DOC2", "LI-COURT-001", owner="Shivangi", father="Pramod",
                     survey="131", village=VILLAGE_SHANTIBAN, year="2021", area="2.60 ha",
                     doc_type="Registered Sale Deed")
    _insert_case("DEMO-LI-COURT-001-C1", "LI-COURT-001", case_no="DEMO-CS-2025-0142",
                 survey="131", village=VILLAGE_SHANTIBAN,
                 court="Civil Judge (Senior Division), Hariharpur (fictional demo court)",
                 case_type="CIVIL", title="Title and boundary suit over survey 131, village Shantiban",
                 petitioner="Adarsh", respondent="Shivangi", filing_date="2025-03-11", status="PENDING",
                 stage="Commission evidence",
                 issue="Ordinary civil property dispute: Adarsh claims ownership based on an earlier registered deed of 2018, while Shivangi disputes the claimed boundary and asserts rights under a later registered document of 2021. A civil suit concerning title and boundary rights is currently pending; the court has appointed a survey commissioner to demarcate the disputed boundary.",
                 next_hearing="2026-10-19", affects_transfer=False,
                 notes="Fictional demo litigation; parties are synthetic; not a real court record.",
                 orders=[
                     {"date": "2025-04-22", "type": "ORDER", "summary": "Written statements filed by both parties; the boundary objection raised by the defendant is registered as a counter-claim."},
                     {"date": "2025-07-15", "type": "COMMISSION", "summary": "Court-appointed survey commissioner directed to demarcate the disputed boundary and submit a report before the next hearing."},
                     {"date": "2026-08-14", "type": "HEARING", "summary": "Commission report received in part; parties directed to file objections within four weeks. Matter listed for further evidence."},
                 ])
    track("LI-COURT-001", "DEMO-LI-COURT-001-DOC1", "DEMO-LI-COURT-001-DOC2", "DEMO-LI-COURT-001-C1")

    # DEMO-LI-COURT-002: boundary dispute between adjoining owners (pending)
    prop_index += 1; _insert_property(prop_index, "LI-COURT-002", survey="132", village=VILLAGE_SHANTIBAN, area_value=0.85)
    _insert_document("DEMO-LI-COURT-002-DOC1", "LI-COURT-002", owner="Lakhan", father="Bedram",
                     survey="132", village=VILLAGE_SHANTIBAN, year="2017", area="0.85 ha")
    _insert_document("DEMO-LI-COURT-002-DOC2", "LI-COURT-002", owner="Lakhan", father="Bedram",
                     survey="132", village=VILLAGE_SHANTIBAN, year="2022", area="0.85 ha")
    _insert_case("DEMO-LI-COURT-002-C1", "LI-COURT-002", case_no="DEMO-CS-2025-0198",
                 survey="132", village=VILLAGE_SHANTIBAN,
                 court="Civil Judge (Junior Division), Hariharpur (fictional demo court)",
                 case_type="CIVIL", title="Boundary demarcation dispute — survey 132, village Shantiban",
                 petitioner="Lakhan", respondent="Raghav", filing_date="2025-06-09", status="PENDING",
                 stage="Framing of issues",
                 issue="Boundary dispute between the owners of adjoining parcels: Lakhan (survey 132) and Raghav (owner of the neighbouring parcel) disagree over the position of a boundary pillar noted in the village map. Suit for demarcation and permanent injunction is pending.",
                 next_hearing="2026-11-05", affects_transfer=False,
                 orders=[
                     {"date": "2025-08-21", "type": "HEARING", "summary": "Both parties produced certified copies of the village map; the court noted a discrepancy in the recorded pillar measurements."},
                     {"date": "2026-02-27", "type": "ORDER", "summary": "Issues framed; revenue records summoned for comparison with the field book."},
                 ])
    track("LI-COURT-002", "DEMO-LI-COURT-002-DOC1", "DEMO-LI-COURT-002-DOC2", "DEMO-LI-COURT-002-C1")

    # DEMO-LI-COURT-003: inheritance dispute with stay/injunction affecting transfer
    prop_index += 1; _insert_property(prop_index, "LI-COURT-003", survey="133", village=VILLAGE_SHANTIBAN, area_value=1.9)
    _insert_document("DEMO-LI-COURT-003-DOC1", "LI-COURT-003", owner="Shyam", father="Bedram",
                     survey="133", village=VILLAGE_SHANTIBAN, year="2015", area="1.90 ha")
    _insert_document("DEMO-LI-COURT-003-DOC2", "LI-COURT-003", owner="Girija", father="Shyam",
                     survey="133", village=VILLAGE_SHANTIBAN, year="2024", area="1.90 ha")
    _insert_mutation("DEMO-LI-COURT-003-M1", "LI-COURT-003", mutation_no="M/DEMO/2024/0201",
                     survey="133", village=VILLAGE_SHANTIBAN, previous_owner="Shyam", new_owner="Girija",
                     status="UNDER_REVIEW", reason="INHERITANCE", deed_no="", deed_date="",
                     notes="Inheritance mutation application held in abeyance pending the outcome of DEMO-CIVIL-2024-0087 (synthetic).")
    _insert_case("DEMO-LI-COURT-003-C1", "LI-COURT-003", case_no="DEMO-CIVIL-2024-0087",
                 survey="133", village=VILLAGE_SHANTIBAN,
                 court="District Judge Court, Kishandham (fictional demo court)",
                 case_type="CIVIL", title="Inheritance dispute over the ancestral holding of the late Shyam",
                 petitioner="Girija", respondent="Kiran", filing_date="2024-09-30", status="STAYED",
                 stage="Interim injunction in force",
                 issue="Inheritance dispute: Girija and Kiran each claim succession to the ancestral holding of the late Shyam (survey 133). The inheritance mutation is on hold pending the suit; an interim order keeps the state of affairs unchanged.",
                 next_hearing="2026-10-08", affects_transfer=True, related_mutation="DEMO-LI-COURT-003-M1",
                 orders=[
                     {"date": "2024-12-12", "type": "INJUNCTION", "summary": "Interim injunction: the parties are to maintain status quo; no transfer, lease or mutation of the suit land until further orders."},
                     {"date": "2025-06-18", "type": "HEARING", "summary": "Written arguments filed by both sides; matter posted for framing of additional issues on succession."},
                 ])
    track("LI-COURT-003", "DEMO-LI-COURT-003-DOC1", "DEMO-LI-COURT-003-DOC2",
          "DEMO-LI-COURT-003-M1", "DEMO-LI-COURT-003-C1")

    # DEMO-LI-COURT-004: case closed / resolved (disposed) — false-positive check
    prop_index += 1; _insert_property(prop_index, "LI-COURT-004", survey="134", village=VILLAGE_SHANTIBAN, area_value=1.15)
    _insert_document("DEMO-LI-COURT-004-DOC1", "LI-COURT-004", owner="Sarita", father="Munna",
                     survey="134", village=VILLAGE_SHANTIBAN, year="2021", area="1.15 ha")
    _insert_case("DEMO-LI-COURT-004-C1", "LI-COURT-004", case_no="DEMO-CIVIL-2021-0064",
                 survey="134", village=VILLAGE_SHANTIBAN,
                 court="Civil Judge (Junior Division), Hariharpur (fictional demo court)",
                 case_type="CIVIL", title="Possession suit over survey 134 — withdrawn",
                 petitioner="Sarita", respondent="Bhola", filing_date="2021-02-08", status="DISPOSED",
                 stage="",
                 issue="Possession suit filed by Sarita regarding a claimed occupation of part of survey 134. The matter was amicably resolved between the parties and the suit was withdrawn with liberty; nothing remains pending.",
                 next_hearing=None, affects_transfer=False,
                 orders=[
                     {"date": "2022-08-19", "type": "DISPOSAL", "summary": "Suit dismissed as withdrawn with liberty; decree drawn accordingly. No pending claim between the parties over this parcel."},
                 ])
    track("LI-COURT-004", "DEMO-LI-COURT-004-DOC1", "DEMO-LI-COURT-004-C1")

    # DEMO-LI-COURT-005: decided title suit -> mutation by court decree (completed)
    prop_index += 1; _insert_property(prop_index, "LI-COURT-005", survey="33/2", village=VILLAGE_DEVNAPUR, area_value=1.7)
    _insert_document("DEMO-LI-COURT-005-DOC1", "LI-COURT-005", owner="Devaki", father="Bhagwati",
                     survey="33/2", village=VILLAGE_DEVNAPUR, year="2014", area="1.70 ha")
    _insert_document("DEMO-LI-COURT-005-DOC2", "LI-COURT-005", owner="Nandkishor", father="Bhagwati",
                     survey="33/2", village=VILLAGE_DEVNAPUR, year="2023", area="1.70 ha")
    _insert_case("DEMO-LI-COURT-005-C1", "LI-COURT-005", case_no="DEMO-CIVIL-2022-0031",
                 survey="33/2", village=VILLAGE_DEVNAPUR,
                 court="District Judge Court, Kishandham (fictional demo court)",
                 case_type="CIVIL", title="Title suit over survey 33/2 — decided",
                 petitioner="Devaki", respondent="Nandkishor", filing_date="2022-05-16", status="DISPOSED",
                 stage="",
                 issue="Title suit concerning succession to the recorded holding of survey 33/2; the court declared Nandkishor entitled to the holding and directed mutation accordingly.",
                 next_hearing=None, affects_transfer=False, related_mutation="DEMO-LI-COURT-005-M1",
                 orders=[
                     {"date": "2023-01-27", "type": "DECREE", "summary": "Suit decreed in favour of Nandkishor; title declared, and the revenue authorities directed to record the change."},
                 ])
    _insert_mutation("DEMO-LI-COURT-005-M1", "LI-COURT-005", mutation_no="M/DEMO/2023/0015",
                     survey="33/2", village=VILLAGE_DEVNAPUR, previous_owner="Devaki", new_owner="Nandkishor",
                     status="COMPLETED", reason="COURT_DECREE", deed_no="DEMO-CIVIL-2022-0031",
                     deed_date="2023-01-27", decided_on="2023-02-10",
                     notes="Mutation effected on the strength of the decree in DEMO-CIVIL-2022-0031 (synthetic).")
    track("LI-COURT-005", "DEMO-LI-COURT-005-DOC1", "DEMO-LI-COURT-005-DOC2",
          "DEMO-LI-COURT-005-C1", "DEMO-LI-COURT-005-M1")

    # == RISK REVIEW scenarios (village Shantiban) ===========================

    # DEMO-LI-RISK-001: medium risk — area jump without partition/merger
    prop_index += 1; _insert_property(prop_index, "LI-RISK-001", survey="141", village=VILLAGE_SHANTIBAN, area_value=3.4)
    _insert_document("DEMO-LI-RISK-001-DOC1", "LI-RISK-001", owner="Lakhan", father="Bedram",
                     survey="141", village=VILLAGE_SHANTIBAN, year="2021", area="2.10 ha")
    _insert_document("DEMO-LI-RISK-001-DOC2", "LI-RISK-001", owner="Lakhan", father="Bedram",
                     survey="141", village=VILLAGE_SHANTIBAN, year="2024", area="3.40 ha")
    track("LI-RISK-001", "DEMO-LI-RISK-001-DOC1", "DEMO-LI-RISK-001-DOC2")

    # DEMO-LI-RISK-002: high risk — two deeds for the same year name different owners
    prop_index += 1; _insert_property(prop_index, "LI-RISK-002", survey="142", village=VILLAGE_SHANTIBAN, area_value=2.0)
    _insert_document("DEMO-LI-RISK-002-DOC1", "LI-RISK-002", owner="Omkar", father="Triloki",
                     survey="142", village=VILLAGE_SHANTIBAN, year="2024", area="2.00 ha",
                     doc_type="Registered Sale Deed", filename="demo-li/DEMO-LI-RISK-002-deed-A.png")
    _insert_document("DEMO-LI-RISK-002-DOC2", "LI-RISK-002", owner="Vrinda", father="Triloki",
                     survey="142", village=VILLAGE_SHANTIBAN, year="2024", area="2.00 ha",
                     doc_type="Registered Sale Deed", filename="demo-li/DEMO-LI-RISK-002-deed-B.png")
    track("LI-RISK-002", "DEMO-LI-RISK-002-DOC1", "DEMO-LI-RISK-002-DOC2")

    # DEMO-LI-RISK-003: several ordinary records combine into high overall risk
    # (a live loan + a registered sale executed while the loan was live + a
    # low-quality scan of the newest record)
    prop_index += 1; _insert_property(prop_index, "LI-RISK-003", survey="143", village=VILLAGE_SHANTIBAN, area_value=1.45)
    _insert_document("DEMO-LI-RISK-003-DOC1", "LI-RISK-003", owner="Manohar", father="Dayaram",
                     survey="143", village=VILLAGE_SHANTIBAN, year="2019", area="1.45 ha")
    _insert_document("DEMO-LI-RISK-003-DOC2", "LI-RISK-003", owner="Pramila", father="Manohar",
                     survey="143", village=VILLAGE_SHANTIBAN, year="2024", area="1.45 ha",
                     confidence=0.55, mean_conf=52)
    _insert_encumbrance("DEMO-LI-RISK-003-E1", "LI-RISK-003", survey="143", village=VILLAGE_SHANTIBAN,
                        lender="Nagar Finance (synthetic)", reference="DEMO-LN-2025-006", amount=150000.0,
                        start="2025-01-10", status="ACTIVE", owner="Manohar",
                        notes="Working-capital loan against the holding, recorded 2025-01-10.")
    _insert_mutation("DEMO-LI-RISK-003-M1", "LI-RISK-003", mutation_no="M/DEMO/2025/0066",
                     survey="143", village=VILLAGE_SHANTIBAN, previous_owner="Manohar", new_owner="Pramila",
                     status="COMPLETED", deed_no="DEMO-LI-DEED-2025-066", deed_date="2025-03-05",
                     decided_on="2025-04-02",
                     notes="Registered sale deed verified on its face; the register did not surface the lender's live charge at completion time (synthetic demo of compounding risk).")
    track("LI-RISK-003", "DEMO-LI-RISK-003-DOC1", "DEMO-LI-RISK-003-DOC2",
          "DEMO-LI-RISK-003-E1", "DEMO-LI-RISK-003-M1")

    return {"artifacts": created, "parcel_count": prop_index}


# ---------------------------------------------------------------------------
# removal (DEMO-LI artifacts only — production data is untouched)
# ---------------------------------------------------------------------------

def clear_all() -> Dict[str, int]:
    ensure_land_tables()
    removed = {"documents": 0, "encumbrances": 0, "mutations": 0, "events": 0, "court_cases": 0, "case_orders": 0,
               "properties": 0, "property_links": 0, "provenance": 0, "timeline": 0}
    with get_db() as db:
        doc_ids = [row["id"] for row in db.execute("SELECT id, metadata FROM documents").fetchall()
                   if _is_li_document(row["metadata"])]
        if doc_ids:
            placeholders = ",".join("?" for _ in doc_ids)
            db.execute(f"DELETE FROM property_documents WHERE document_id IN ({placeholders})", tuple(doc_ids))
            db.execute(f"DELETE FROM documents WHERE id IN ({placeholders})", tuple(doc_ids))
            removed["documents"] = len(doc_ids)
        removed["encumbrances"] = db.execute("DELETE FROM land_encumbrances WHERE id LIKE 'DEMO-LI-%'").rowcount or 0
        mutation_ids = [row["id"] for row in db.execute("SELECT id FROM land_mutations WHERE id LIKE 'DEMO-LI-%'").fetchall()]
        if mutation_ids:
            placeholders = ",".join("?" for _ in mutation_ids)
            db.execute(f"DELETE FROM land_mutation_events WHERE mutation_id IN ({placeholders})", tuple(mutation_ids))
            db.execute(f"DELETE FROM land_mutations WHERE id IN ({placeholders})", tuple(mutation_ids))
            removed["events"] = len(mutation_ids)
            removed["mutations"] = len(mutation_ids)
        removed["case_orders"] = db.execute("DELETE FROM land_case_orders WHERE id LIKE 'DEMO-LI-%'").rowcount or 0
        case_ids = [row["id"] for row in db.execute("SELECT id FROM land_court_cases WHERE id LIKE 'DEMO-LI-%'").fetchall()]
        if case_ids:
            placeholders = ",".join("?" for _ in case_ids)
            db.execute(f"DELETE FROM land_case_orders WHERE case_id IN ({placeholders})", tuple(case_ids))
            db.execute(f"DELETE FROM land_court_cases WHERE id IN ({placeholders})", tuple(case_ids))
            removed["court_cases"] = len(case_ids)
        property_ids = [row["property_id"] for row in db.execute("SELECT property_id FROM properties WHERE property_id LIKE 'DEMO-LI-%'").fetchall()]
        if property_ids:
            placeholders = ",".join("?" for _ in property_ids)
            db.execute(f"DELETE FROM property_documents WHERE property_id IN ({placeholders})", tuple(property_ids))
            db.execute(f"DELETE FROM provenance WHERE property_id LIKE 'DEMO-LI-%'")
            db.execute(f"DELETE FROM property_timeline WHERE property_id LIKE 'DEMO-LI-%'")
            db.execute(f"DELETE FROM properties WHERE property_id IN ({placeholders})", tuple(property_ids))
            removed["properties"] = len(property_ids)
    return removed


def _is_li_document(metadata_raw: Any) -> bool:
    try:
        metadata = json.loads(metadata_raw) if isinstance(metadata_raw, str) else (metadata_raw or {})
        return bool(metadata.get("demo")) and metadata.get("dataset") == DATASET
    except Exception:
        return False


if __name__ == "__main__":  # pragma: no cover - manual seeding convenience
    import server

    server.init_db()
    result = seed_all()
    total = sum(len(ids) for ids in result["artifacts"].values())
    print(f"Seeded {DATASET}: {result['parcel_count']} parcels, {total} artifacts across {len(result['artifacts'])} scenarios.")
