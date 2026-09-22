"""Deterministic demo/test scenarios for the Land Intelligence workflow.

Seeding is EXPLICIT, audited, and administrator-governed. Every artifact is
tagged (document metadata ``demo`` flag / ``DEMO-`` id prefixes) so it can be
wiped without touching production data. Scenarios mirror the acceptance list:
clean record, valid mutation, active encumbrance, ownership change without
mutation, conflicting history, area jump, pending mutation, rejected mutation,
low-quality OCR, conflicting duplicates, and the six litigation situations
(active title suit, possession dispute, revenue dispute, decided, settled and
withdrawn proceedings).

No scenario depends on network access, randomness, or wall-clock ordering:
ids, dates and amounts are fixed constants.

The same code path serves the HTTP admin surface and ``scripts/seed_demo_data.py``.
``seed_all(dry_run=True)`` is a READ-ONLY projection of the plan, so the
"expected effect" shown before loading can never drift from what loading does.
"""
from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import server
from server import (
    ROLE_ADMIN,
    get_db,
    require_roles,
)
from land_intel import _audit, ensure_land_tables

demo_router = APIRouter(prefix="/api/admin/demo", tags=["Demo Scenarios"])

DEMO_UPLOADER = "demo-seeder@landrec.gov.in"
DEMO_ACTOR = "demo-seeder"
DEMO_ID_PREFIX = "DEMO-"
_ENSURE_CASES: Dict[str, bool] = {}
DEMO_TEHSIL = "Sadar"
DEMO_DISTRICT = "Demo District"
DEMO_STATE = "Demo Pradesh"

#: Entity kinds the demo dataset can produce, in dependency order.
DEMO_ENTITY_KINDS = ("documents", "mutations", "encumbrances", "court_cases")

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
    {"id": "S11", "title": "Active title litigation with a transfer inside the pending period",
     "expectation": "HIGH_RISK — ACTIVE_LITIGATION + TRANSFER_DURING_LITIGATION flags."},
    {"id": "S12", "title": "Possession dispute alongside a live mortgage",
     "expectation": "HIGH_RISK — ACTIVE_LITIGATION + ACTIVE_ENCUMBRANCE flags."},
    {"id": "S13", "title": "Revenue / land-record dispute before the revenue court",
     "expectation": "HIGH_RISK — a single ACTIVE_LITIGATION signal on an otherwise consistent parcel."},
    {"id": "S14", "title": "Litigation decided by the civil court",
     "expectation": "CLEAR with CLOSED_LITIGATION_ON_RECORD info — decree recorded, mutation completed after it."},
    {"id": "S15", "title": "Litigation settled, encumbrance released",
     "expectation": "CLEAR — prior suit and released loan stay visible as info only."},
    {"id": "S16", "title": "Withdrawn suit, rejected mutation and a live mortgage",
     "expectation": "HIGH_RISK — ACTIVE_ENCUMBRANCE + SALE_DURING_ENCUMBRANCE + ownership gap + prior suit."},
]


# ---------------------------------------------------------------------------
# production safety
# ---------------------------------------------------------------------------

def production_block_reason() -> Optional[str]:
    """Why demo writes are refused right now, or ``None`` when allowed.

    Read from the ``server`` module at call time so the guard always follows
    the live environment, and so tests can exercise it. There is deliberately
    NO override flag: production deployments cannot seed demo data, from the
    API or from the command line.
    """
    if getattr(server, "IS_PRODUCTION", False):
        return ("APP_ENV marks this deployment as production: demo seeding and demo wipes are "
                "refused. Run against a development/demo instance instead.")
    return None


def assert_demo_writes_allowed() -> None:
    reason = production_block_reason()
    if reason:
        raise HTTPException(status_code=403, detail=reason)


# ---------------------------------------------------------------------------
# dry-run projection (shared by --check-only, the admin UI and real seeding)
# ---------------------------------------------------------------------------

_DRY_RUN = False


@contextmanager
def _dry_run(enabled: bool = True) -> Iterator[None]:
    global _DRY_RUN
    previous, _DRY_RUN = _DRY_RUN, enabled
    try:
        yield
    finally:
        _DRY_RUN = previous


def _ensure_litigation_schema() -> bool:
    """Create the litigation table if the register module is available.

    Returns whether demo court cases can be written at all, so a deployment
    without the register still seeds the rest of the dataset instead of dying.
    """
    try:
        from court_cases import ensure_schema
        ensure_schema()
        return True
    except Exception as exc:  # pragma: no cover - optional module missing
        print(f"[DEMO WARNING] litigation register unavailable: {exc}")
        return False


def _exists(table: str, row_id: str) -> bool:
    """Presence probe. Table names are internal constants, never user input."""
    if table not in {"documents", "land_mutations", "land_encumbrances", "land_court_cases"}:  # pragma: no cover
        raise ValueError(f"unexpected demo table {table!r}")
    try:
        with get_db() as db:
            return db.execute(f"SELECT 1 FROM {table} WHERE id=?", (row_id,)).fetchone() is not None
    except Exception:
        return False  # table not created yet on a virgin database


# ---------------------------------------------------------------------------
# artifact builders — all idempotent (insert-if-absent) and dry-run aware
# ---------------------------------------------------------------------------

def _fields(pairs: Dict[str, Any], confidence: float = 0.95) -> Dict[str, Any]:
    return {
        key: {"value": str(value), "confidence": confidence, "validation_status": "VALID", "validation_message": ""}
        for key, value in pairs.items()
    }


def _insert_document(doc_id: str, scenario: str, *, owner: str, survey: str, village: str,
                     tehsil: str, district: str, year: str, area: str, status: str,
                     father: str = "", doc_type: str = "Land Record", confidence: float = 0.95,
                     mean_conf: int = 92) -> bool:
    """Insert one synthetic screened document. Returns True when created."""
    fields = _fields({
        "owner_name": owner, "father_name": father, "survey_number": survey, "khasra_number": survey,
        "khata_number": str(int(survey.split("/")[0]) + 100) if survey.split("/")[0].isdigit() else survey,
        "area": area, "village": village, "tehsil": tehsil, "district": district, "state": DEMO_STATE,
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
    if _DRY_RUN:
        return not _exists("documents", doc_id)
    with get_db() as db:
        if db.execute("SELECT 1 FROM documents WHERE id=?", (doc_id,)).fetchone():
            return False
        db.execute(
            """INSERT INTO documents
               (id, filename, doc_type, mean_conf, verdict, status, languages, pages, fields, validation,
                ai_decision_support, ocr_text, cleaned_ocr_text, detected_language, original_fields, metadata,
                uploaded_by, reviewer_comments, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (doc_id, f"demo-{scenario.lower()}-{doc_id}.pdf", doc_type, mean_conf, "review", status, '["eng"]', 1,
             json.dumps(fields), json.dumps(validation), "{}", "DEMO SCENARIO — synthetic OCR text", "", "eng",
             json.dumps(fields), json.dumps({"demo": True, "scenario": scenario}), DEMO_UPLOADER,
             "", now, now),
        )
    return True


def _demo_encumbrance(enc_id: str, scenario: str, *, survey: str, village: str, lender: str,
                      reference: str, amount: float, start: str, status: str = "ACTIVE",
                      release: Optional[str] = None, owner: str = "") -> bool:
    now = time.time()
    if _DRY_RUN:
        return not _exists("land_encumbrances", enc_id)
    with get_db() as db:
        if db.execute("SELECT 1 FROM land_encumbrances WHERE id=?", (enc_id,)).fetchone():
            return False
        db.execute(
            """INSERT INTO land_encumbrances
               (id, property_id, survey_number, khasra_number, village, tehsil, district, owner_name,
                lender, reference_no, amount, start_date, release_date, status, evidence_doc_id, notes,
                created_by, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (enc_id, None, survey, survey, village, "Demo Tehsil", "Demo District", owner, lender, reference,
             amount, start, release, status, None, f"DEMO scenario {scenario}", DEMO_ACTOR, now, now),
        )
    return True


def _demo_mutation(mut_id: str, scenario: str, *, mutation_no: str, survey: str, village: str,
                   previous_owner: str, new_owner: str, status: str, reason: str = "SALE",
                   deed_no: str = "", deed_date: str = "", notes: str = "") -> bool:
    now = time.time()
    if _DRY_RUN:
        return not _exists("land_mutations", mut_id)
    with get_db() as db:
        if db.execute("SELECT 1 FROM land_mutations WHERE id=?", (mut_id,)).fetchone():
            return False
        # mutation_no is UNIQUE across the register: if the number is taken by
        # any row we do not own, skip cleanly. (Seeding must never modify or
        # delete a row it did not create.)
        if db.execute("SELECT 1 FROM land_mutations WHERE mutation_no=?", (mutation_no,)).fetchone():
            return False
        db.execute(
            """INSERT INTO land_mutations
               (id, mutation_no, property_id, survey_number, khasra_number, village, tehsil, district,
                previous_owner, new_owner, reason_type, deed_no, deed_date, documents, document_checklist,
                status, risk_status, risk_payload, encumbrance_status, reviewer, reviewer_notes, decided_at,
                created_by, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mut_id, mutation_no, None, survey, survey, village, DEMO_TEHSIL, DEMO_DISTRICT,
             previous_owner, new_owner, reason, deed_no, deed_date, "[]", "[]", status, "UNKNOWN", "{}",
             "UNKNOWN", "demo-reviewer@landrec.gov.in", notes, now if status in {"COMPLETED", "REJECTED"} else None,
             DEMO_ACTOR, now, now),
        )
        db.execute("INSERT OR REPLACE INTO land_mutation_events (id, mutation_id, status, note, actor, created_at) VALUES (?,?,?,?,?,?)",
                   (f"DEMO-EV-{mut_id}", mut_id, status, notes or f"Demo scenario {scenario}", DEMO_ACTOR, now))
    return True


def _demo_court_case(case_id: str, scenario: str, *, survey: str, village: str, case_number: str,
                     case_type: str, court_name: str, filed_date: str, parties: str, relief: str,
                     status: str = "ACTIVE", closed_date: str = "", decision: str = "",
                     evidence_doc_ids: Optional[List[str]] = None, notes: str = "") -> bool:
    """Register a synthetic court case against a demo parcel.

    Written through the litigation register's own schema so demo cases are
    indistinguishable (structurally) from operator-registered ones — that is
    what makes them useful for exercising the real screens.
    """
    if not _ENSURE_CASES.get("ready", False):
        if not _ensure_litigation_schema():
            return False
    if _DRY_RUN:
        return not _exists("land_court_cases", case_id)
    now = time.time()
    with get_db() as db:
        if db.execute("SELECT 1 FROM land_court_cases WHERE id=?", (case_id,)).fetchone():
            return False
        db.execute(
            """INSERT INTO land_court_cases
               (id, survey_number, khasra_number, village, tehsil, district, case_number, case_type,
                court_name, filed_date, closed_date, status, parties, relief_sought, decision_summary,
                evidence_doc_ids, notes, created_by, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (case_id, survey, survey, village, DEMO_TEHSIL, DEMO_DISTRICT, case_number, case_type, court_name,
             filed_date, closed_date, status, parties, relief, decision,
             json.dumps(evidence_doc_ids or []), notes or f"DEMO scenario {scenario}", DEMO_ACTOR, now, now),
        )
    return True


# ---------------------------------------------------------------------------
# the dataset — S1..S10 preserved verbatim, S11..S16 add litigation
# ---------------------------------------------------------------------------

class _Tally:
    """Accounts for the run: what the dataset contains, what was created now and
    what was skipped because it already exists (the idempotency proof)."""

    def __init__(self) -> None:
        self.created: Dict[str, List[str]] = {}
        self.skipped: Dict[str, List[str]] = {}
        self.dataset: Dict[str, Dict[str, int]] = {scenario["id"]: {kind: 0 for kind in DEMO_ENTITY_KINDS} for scenario in SCENARIOS}
        self.created_by_kind: Dict[str, Dict[str, int]] = {scenario["id"]: {kind: 0 for kind in DEMO_ENTITY_KINDS} for scenario in SCENARIOS}

    def note(self, scenario: str, kind: str, artifact_id: str, created: bool) -> None:
        self.dataset.setdefault(scenario, {k: 0 for k in DEMO_ENTITY_KINDS})[kind] += 1
        bucket = self.created if created else self.skipped
        bucket.setdefault(scenario, []).append(artifact_id)
        if created:
            self.created_by_kind.setdefault(scenario, {k: 0 for k in DEMO_ENTITY_KINDS})[kind] += 1

    @staticmethod
    def _totals(by_scenario: Dict[str, Dict[str, int]], created: Optional[Dict[str, List[str]]] = None) -> Dict[str, int]:
        totals = {kind: 0 for kind in DEMO_ENTITY_KINDS}
        for per_scenario in by_scenario.values():
            for kind, count in per_scenario.items():
                totals[kind] += count
        totals["land_records"] = sum(1 for per_scenario in by_scenario.values() if sum(per_scenario.values()))
        totals["total"] = sum(totals[kind] for kind in DEMO_ENTITY_KINDS)
        if created is not None:
            totals["total"] = sum(len(ids) for ids in created.values())
        return totals

    @property
    def dataset_totals(self) -> Dict[str, int]:
        return self._totals(self.dataset)

    @property
    def created_totals(self) -> Dict[str, int]:
        return self._totals(self.created_by_kind, self.created)


def _seed_document_scenarios(tally: _Tally) -> None:
    """S1..S10 — the original catalogue, unchanged."""

    def doc(scenario: str, doc_id: str, **kwargs: Any) -> None:
        kwargs.setdefault("tehsil", DEMO_TEHSIL)
        kwargs.setdefault("district", DEMO_DISTRICT)
        tally.note(scenario, "documents", doc_id, _insert_document(doc_id, scenario, **kwargs))

    def mut(scenario: str, mut_id: str, **kwargs: Any) -> None:
        tally.note(scenario, "mutations", mut_id, _demo_mutation(mut_id, scenario, **kwargs))

    def enc(scenario: str, enc_id: str, **kwargs: Any) -> None:
        tally.note(scenario, "encumbrances", enc_id, _demo_encumbrance(enc_id, scenario, **kwargs))

    # -- S1: clean record ----------------------------------------------------
    doc("S1", "DEMO-S1-DOC1", owner="Sita Devi", survey="201", village="Ambedarpur",
        year="2022", area="2.50 ha", status="APPROVED", father="Ram Prasad")

    # -- S2: ownership change with a valid completed mutation -----------------
    doc("S2", "DEMO-S2-DOC1", owner="Ram Swaroop Sharma", survey="45/2", village="Barkheda",
        year="2019", area="3.20 ha", status="APPROVED", father="Hari Sharma")
    doc("S2", "DEMO-S2-DOC2", owner="Amit Sharma", survey="45/2", village="Barkheda",
        year="2023", area="3.20 ha", status="APPROVED", father="Ram Swaroop Sharma", doc_type="Sale Deed")
    mut("S2", "DEMO-MUT-S2", mutation_no="DEMO-M-2023-0001", survey="45/2", village="Barkheda",
        previous_owner="Ram Swaroop Sharma", new_owner="Amit Sharma", status="COMPLETED",
        deed_no="REG-2019-000342", deed_date="2023-02-10",
        notes="Registered sale deed verified; ownership transferred.")

    # -- S3: active bank encumbrance ------------------------------------------
    doc("S3", "DEMO-S3-DOC1", owner="Mahesh Verma", survey="103", village="Ambedarpur",
        year="2021", area="1.75 ha", status="APPROVED", father="Gopal Verma")
    enc("S3", "DEMO-ENC-S3", survey="103", village="Ambedarpur", lender="Example Bank",
        reference="LN-2026-00123", amount=850000.0, start="2024-11-05", status="ACTIVE",
        owner="Mahesh Verma")

    # -- S4: ownership change WITHOUT mutation --------------------------------
    doc("S4", "DEMO-S4-DOC1", owner="Harish Chandra", survey="204", village="Barkheda",
        year="2018", area="2.00 ha", status="APPROVED")
    doc("S4", "DEMO-S4-DOC2", owner="Premwati Devi", survey="204", village="Barkheda",
        year="2022", area="2.00 ha", status="APPROVED")

    # -- S5: conflicting historical document ----------------------------------
    doc("S5", "DEMO-S5-DOC1", owner="Jagdish Yadav", survey="105", village="Ambedarpur",
        year="2021", area="1.60 ha", status="APPROVED")
    doc("S5", "DEMO-S5-DOC2", owner="Kallu Yadav", survey="105", village="Ambedarpur",
        year="2021", area="1.60 ha", status="APPROVED")

    # -- S6: large unexpected area change --------------------------------------
    doc("S6", "DEMO-S6-DOC1", owner="Shyam Lal", survey="106", village="Barkheda",
        year="2019", area="2.50 ha", status="APPROVED")
    doc("S6", "DEMO-S6-DOC2", owner="Shyam Lal", survey="106", village="Barkheda",
        year="2023", area="4.30 ha", status="APPROVED")

    # -- S7: pending mutation ---------------------------------------------------
    doc("S7", "DEMO-S7-DOC1", owner="Ram Swaroop Sharma", survey="207", village="Ambedarpur",
        year="2020", area="0.80 ha", status="APPROVED")
    doc("S7", "DEMO-S7-DOC2", owner="Amit Sharma", survey="207", village="Ambedarpur",
        year="2024", area="0.80 ha", status="APPROVED", doc_type="Sale Deed")
    mut("S7", "DEMO-MUT-S7", mutation_no="DEMO-M-2026-0012", survey="207", village="Ambedarpur",
        previous_owner="Ram Swaroop Sharma", new_owner="Amit Sharma", status="UNDER_REVIEW",
        deed_no="REG-2024-000518", deed_date="2024-08-19")

    # -- S8: rejected mutation ---------------------------------------------------
    doc("S8", "DEMO-S8-DOC1", owner="Om Prakash", survey="208", village="Barkheda",
        year="2020", area="1.20 ha", status="APPROVED")
    doc("S8", "DEMO-S8-DOC2", owner="Suresh Kumar", survey="208", village="Barkheda",
        year="2023", area="1.20 ha", status="APPROVED")
    mut("S8", "DEMO-MUT-S8", mutation_no="DEMO-M-2024-0002", survey="208", village="Barkheda",
        previous_owner="Om Prakash", new_owner="Suresh Kumar", status="REJECTED",
        deed_date="2023-09-02", notes="Rejected: sale deed signature could not be verified against the original.")

    # -- S9: low-quality / blurry OCR --------------------------------------------
    doc("S9", "DEMO-S9-DOC1", owner="R?m Bah?dur Singh", survey="209", village="Ambedarpur",
        year="2023", area="2.20 ha", status="DRAFT", confidence=0.42, mean_conf=44)

    # -- S10: conflicting duplicate record ----------------------------------------
    doc("S10", "DEMO-S10-DOC1", owner="Dinesh Chand", survey="210", village="Barkheda",
        year="2022", area="1.80 ha", status="APPROVED")
    doc("S10", "DEMO-S10-DOC2", owner="Dinesh Chand", survey="210", village="Barkheda",
        year="2022", area="3.20 ha", status="APPROVED")


def _seed_litigation_scenarios(tally: _Tally) -> None:
    """S11..S16 — litigation situations, linked to the same parcel identity
    (survey + village) that documents, mutations and encumbrances use, so every
    screen (drawer, risk, reports, PDF, timeline) resolves them together."""

    def doc(scenario: str, doc_id: str, **kwargs: Any) -> None:
        kwargs.setdefault("tehsil", DEMO_TEHSIL)
        kwargs.setdefault("district", DEMO_DISTRICT)
        tally.note(scenario, "documents", doc_id, _insert_document(doc_id, scenario, **kwargs))

    def mut(scenario: str, mut_id: str, **kwargs: Any) -> None:
        tally.note(scenario, "mutations", mut_id, _demo_mutation(mut_id, scenario, **kwargs))

    def enc(scenario: str, enc_id: str, **kwargs: Any) -> None:
        tally.note(scenario, "encumbrances", enc_id, _demo_encumbrance(enc_id, scenario, **kwargs))

    def case(scenario: str, case_id: str, **kwargs: Any) -> None:
        tally.note(scenario, "court_cases", case_id, _demo_court_case(case_id, scenario, **kwargs))

    # -- S11: active title suit, transfer recorded while it was pending -------
    doc("S11", "DEMO-S11-DOC1", owner="Kishan Lal Yadav", survey="311", village="Jayantipur",
        year="2019", area="1.60 ha", status="APPROVED", father="Sunder Lal")
    doc("S11", "DEMO-S11-DOC2", owner="Badri Narayan", survey="311", village="Jayantipur",
        year="2024", area="1.60 ha", status="APPROVED", father="Raghunath", doc_type="Sale Deed")
    mut("S11", "DEMO-MUT-S11", mutation_no="DEMO-M-2024-0007", survey="311", village="Jayantipur",
        previous_owner="Kishan Lal Yadav", new_owner="Badri Narayan", status="UNDER_REVIEW",
        deed_no="REG-2024-000871", deed_date="2024-05-12",
        notes="Sale deed registered while a title suit over the same survey number is pending.")
    case("S11", "DEMO-CC-S11", survey="311", village="Jayantipur", case_number="DEMO-CR-2023-0117",
         case_type="TITLE", court_name="Court of the District Judge, Demo District",
         filed_date="2023-08-04", parties="Kishan Lal Yadav v. Badri Narayan",
         relief="Declaration of title with permanent injunction against interference.",
         evidence_doc_ids=["DEMO-S11-DOC2"], status="ACTIVE")

    # -- S12: possession dispute over land that also carries a live mortgage --
    doc("S12", "DEMO-S12-DOC1", owner="Sushila Devi", survey="312", village="Khetanpur",
        year="2017", area="3.10 ha", status="APPROVED", father="Balbir Singh")
    doc("S12", "DEMO-S12-DOC2", owner="Sushila Devi", survey="312", village="Khetanpur",
        year="2021", area="3.10 ha", status="APPROVED", doc_type="Khatauni")
    enc("S12", "DEMO-ENC-S12", survey="312", village="Khetanpur", lender="Pragati Gramin Bank",
        reference="PGB/HL/2022/DEMO-441", amount=1250000.0, start="2022-02-18", status="ACTIVE",
        owner="Sushila Devi")
    case("S12", "DEMO-CC-S12", survey="312", village="Khetanpur", case_number="DEMO-CR-2023-0233",
         case_type="POSSESSION", court_name="Court of the Civil Judge (Junior Division), Khetanpur",
         filed_date="2023-11-21", parties="Ram Autar v. Sushila Devi",
         relief="Restoration of possession over khudkasth holding and mesne profits.",
         status="ACTIVE")

    # -- S13: revenue / land-record dispute on an otherwise consistent parcel --
    doc("S13", "DEMO-S13-DOC1", owner="Chunri Lal", survey="313", village="Rampur Kalan",
        year="2020", area="0.95 ha", status="APPROVED", father="Maghan Lal")
    doc("S13", "DEMO-S13-DOC2", owner="Chunri Lal", survey="313", village="Rampur Kalan",
        year="2020", area="0.95 ha", status="APPROVED", doc_type="Jamabandi")
    case("S13", "DEMO-CC-S13", survey="313", village="Rampur Kalan", case_number="DEMO-REV-2025-0041",
         case_type="REVENUE", court_name="Court of the Additional Collector (Revenue), Demo District",
         filed_date="2025-01-16", parties="Tehsildar, Sadar v. Chunri Lal",
         relief="Demand for arrears of land revenue and demarcation of the assessed holding.",
         evidence_doc_ids=["DEMO-S13-DOC1"], status="ACTIVE")

    # -- S14: decided litigation, mutation completed after the decree ---------
    doc("S14", "DEMO-S14-DOC1", owner="Gayatri Devi", survey="314", village="Sonbarsa",
        year="2018", area="2.40 ha", status="APPROVED", father="Shivdhar" )
    doc("S14", "DEMO-S14-DOC2", owner="Rekha Singh", survey="314", village="Sonbarsa",
        year="2022", area="2.40 ha", status="APPROVED", father="Gayatri Devi", doc_type="Sale Deed")
    mut("S14", "DEMO-MUT-S14", mutation_no="DEMO-M-2022-0004", survey="314", village="Sonbarsa",
        previous_owner="Gayatri Devi", new_owner="Rekha Singh", status="COMPLETED",
        reason="COURT_DECREE", deed_no="REG-2022-000119", deed_date="2022-03-09",
        notes="Mutation given effect in compliance with the civil court decree of 2021-12-10.")
    case("S14", "DEMO-CC-S14", survey="314", village="Sonbarsa", case_number="DEMO-CR-2019-0088",
         case_type="CIVIL", court_name="Court of the District Judge, Demo District",
         filed_date="2019-05-22", closed_date="2021-12-10", status="DECIDED",
         parties="Gayatri Devi v. Rekha Singh",
         relief="Suit for declaration and partition of the holding.",
         decision="Suit decreed in favour of Rekha Singh; revenue entries to be corrected per the decree.",
         evidence_doc_ids=["DEMO-S14-DOC2"])

    # -- S15: settled suit plus a released loan --------------------------------
    doc("S15", "DEMO-S15-DOC1", owner="Mohd. Irfan", survey="315", village="Amanpur",
        year="2016", area="1.10 ha", status="APPROVED", father="Mohd. Salim")
    doc("S15", "DEMO-S15-DOC2", owner="Shabnam Begum", survey="315", village="Amanpur",
        year="2021", area="1.10 ha", status="APPROVED", father="Mohd. Irfan", doc_type="Sale Deed")
    enc("S15", "DEMO-ENC-S15", survey="315", village="Amanpur", lender="Sindhu Finance Ltd",
        reference="SFL/MORT/2018/DEMO-77", amount=600000.0, start="2018-04-01", release="2020-09-30",
        status="RELEASED", owner="Mohd. Irfan")
    mut("S15", "DEMO-MUT-S15", mutation_no="DEMO-M-2021-0011", survey="315", village="Amanpur",
        previous_owner="Mohd. Irfan", new_owner="Shabnam Begum", status="COMPLETED",
        deed_no="REG-2021-000233", deed_date="2021-01-20",
        notes="Transfer after the compromise decree and after the mortgage was released.")
    case("S15", "DEMO-CC-S15", survey="315", village="Amanpur", case_number="DEMO-CR-2020-0154",
         case_type="CIVIL", court_name="Court of the Civil Judge (Senior Division), Amanpur",
         filed_date="2020-02-12", closed_date="2020-11-06", status="SETTLED",
         parties="Mohd. Irfan v. Shabnam Begum", relief="Partition declaration contested by co-sharers.",
         decision="Parties settled before the court; terms recorded and mutation proceedings allowed to continue.")

    # -- S16: withdrawn suit, rejected mutation and a live mortgage -----------
    doc("S16", "DEMO-S16-DOC1", owner="Virendra Pratap", survey="316", village="Bheluwadi",
        year="2015", area="4.05 ha", status="APPROVED", father="Thakur Vikram")
    doc("S16", "DEMO-S16-DOC2", owner="Ashok Kumar Sahu", survey="316", village="Bheluwadi",
        year="2023", area="4.05 ha", status="APPROVED", father="Ramashray Sahu", doc_type="Sale Deed")
    enc("S16", "DEMO-ENC-S16", survey="316", village="Bheluwadi", lender="Kaveri Urban Co-op Bank",
        reference="KUCB/LN/2023/DEMO-903", amount=2100000.0, start="2023-06-14", status="ACTIVE",
        owner="Virendra Pratap")
    mut("S16", "DEMO-MUT-S16", mutation_no="DEMO-M-2023-0021", survey="316", village="Bheluwadi",
        previous_owner="Virendra Pratap", new_owner="Ashok Kumar Sahu", status="REJECTED",
        deed_no="REG-2023-000644", deed_date="2023-07-02",
        notes="Rejected: no lender consent for transfer of mortgaged land; title suit withdrawn but not re-filed.")
    case("S16", "DEMO-CC-S16", survey="316", village="Bheluwadi", case_number="DEMO-CR-2022-0301",
         case_type="CIVIL", court_name="Court of the Civil Judge (Senior Division), Bheluwadi",
         filed_date="2022-10-05", closed_date="2023-03-27", status="WITHDRAWN",
         parties="Virendra Pratap v. Ashok Kumar Sahu", relief="Injunction against mutation proceedings.",
         decision="Suit withdrawn with permission to file afresh; no interim order continues.")


def populate(tally: _Tally) -> None:
    _seed_document_scenarios(tally)
    _seed_litigation_scenarios(tally)


def seed_all(dry_run: bool = False) -> Dict[str, Any]:
    """Create (or project) all sixteen deterministic scenarios.

    Idempotent: artifacts that already exist are reported under ``skipped`` and
    are never overwritten, so a second run creates nothing.
    """
    ensure_land_tables()
    _ENSURE_CASES["ready"] = _ensure_litigation_schema()
    tally = _Tally()
    with _dry_run(dry_run):
        populate(tally)
    return {
        "scenarios": sorted(tally.created.keys(), key=lambda value: int(value[1:])) if tally.created else [],
        "scenario_catalog": [item["id"] for item in SCENARIOS],
        "artifacts": tally.created,
        "skipped_artifacts": tally.skipped,
        "created": tally.created_totals,
        "counts": tally.created_totals,          # backwards-compatible alias
        "dataset": tally.dataset_totals,
        "counts_by_scenario": tally.dataset,
        "dry_run": dry_run,
    }


# ---------------------------------------------------------------------------
# current state of the demo dataset (used by --check-only and the admin UI)
# ---------------------------------------------------------------------------

def _is_demo_metadata(metadata_raw: Any) -> bool:
    try:
        metadata = json.loads(metadata_raw) if isinstance(metadata_raw, str) else (metadata_raw or {})
        return bool(metadata.get("demo"))
    except Exception:
        return False


def current_demo_counts() -> Dict[str, int]:
    """How many demo-tagged rows exist right now, per entity kind."""
    ensure_land_tables()
    _ENSURE_CASES["ready"] = _ensure_litigation_schema()
    counts = {kind: 0 for kind in DEMO_ENTITY_KINDS}
    with get_db() as db:
        counts["documents"] = sum(1 for row in db.execute(
            "SELECT metadata FROM documents WHERE metadata IS NOT NULL AND metadata <> '{}'")
            if _is_demo_metadata(row[0]))
        for key, table in (("mutations", "land_mutations"), ("encumbrances", "land_encumbrances"),
                          ("court_cases", "land_court_cases")):
            try:
                counts[key] = db.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE id LIKE ?", (DEMO_ID_PREFIX + "%",)).fetchone()[0]
            except Exception:
                counts[key] = 0
    counts["land_records"] = len(demo_land_identities())
    counts["total"] = sum(counts[kind] for kind in DEMO_ENTITY_KINDS)
    return counts


def demo_land_identities() -> List[str]:
    """Parcel identities (survey|village) the demo documents create — the land
    count shown in the confirmation panel."""
    identities: List[str] = []
    with get_db() as db:
        for row in db.execute("SELECT id, metadata, fields FROM documents WHERE metadata IS NOT NULL AND metadata <> '{}'").fetchall():
            if not _is_demo_metadata(row["metadata"]):
                continue
            try:
                fields = json.loads(row["fields"] or "{}")
            except Exception:
                continue
            survey = str((fields.get("survey_number") or {}).get("value") or "").strip()
            village = str((fields.get("village") or {}).get("value") or "").strip()
            identity = f"{survey}|{village}"
            if (survey or village) and identity not in identities:
                identities.append(identity)
    return identities


def preview() -> Dict[str, Any]:
    """Expected effect of a seed, plus what is already loaded. Read-only."""
    plan = seed_all(dry_run=True)
    current = current_demo_counts()
    dataset, created = plan["dataset"], plan["created"]
    return {
        "scenario_count": len(SCENARIOS),
        "scenarios": SCENARIOS,
        "dataset": dataset,
        "expected": dataset,
        "expected_by_scenario": plan["counts_by_scenario"],
        "present": current,
        "pending": {kind: max(dataset[kind] - created[kind], 0) for kind in DEMO_ENTITY_KINDS},
        "would_create": created["total"],
        "already_present": sum(len(ids) for ids in plan["skipped_artifacts"].values()),
        "complete": created["total"] == 0 and dataset["total"] == current["total"],
        "production_blocked": production_block_reason() is not None,
        "block_reason": production_block_reason(),
    }


# ---------------------------------------------------------------------------
# admin HTTP surface (administrator only, audited, production-safe)
# ---------------------------------------------------------------------------

class SeedRequest(BaseModel):
    scenario: str = "all"


@demo_router.get("/scenarios")
def list_scenarios(user: Dict[str, Any] = Depends(require_roles(ROLE_ADMIN))):
    return {"scenarios": SCENARIOS}


@demo_router.get("/preview")
def demo_preview(user: Dict[str, Any] = Depends(require_roles(ROLE_ADMIN))):
    """Show the effect before loading or removing anything."""
    return preview()


@demo_router.post("/seed")
def seed_scenarios(req: SeedRequest, user: Dict[str, Any] = Depends(require_roles(ROLE_ADMIN))):
    """Seed deterministic demo scenarios (administrator only, audited)."""
    assert_demo_writes_allowed()
    wanted = (req.scenario or "all").strip().upper()
    known = {item["id"] for item in SCENARIOS}
    if wanted != "ALL" and wanted not in known:
        raise HTTPException(status_code=422, detail=f"Unknown scenario '{req.scenario}'. Use 'all' or one of {sorted(known)}.")
    result = seed_all()  # deterministic dataset: seeding is all-or-nothing and idempotent
    _audit(user, "DEMO_DATA_SEEDED",
           f"Demo scenarios seeded ({wanted}): created {result['created']['total']}, "
           f"skipped {sum(len(ids) for ids in result['skipped_artifacts'].values())}")
    return {"status": "seeded", "requested": wanted, **result}


@demo_router.delete("/data")
def clear_demo_data(user: Dict[str, Any] = Depends(require_roles(ROLE_ADMIN))):
    """Remove ONLY demo-tagged artifacts. Production data is untouched."""
    assert_demo_writes_allowed()
    return clear_demo_dataset(actor=user.get("email") or "ADMIN")


def clear_demo_dataset(actor: str = "ADMIN") -> Dict[str, Any]:
    """Delete demo-tagged rows only. Shared by the HTTP surface and the script.

    Selection is by the demo identity of each row — document ``metadata.demo``
    for documents (which an operator may have re-verified) and the ``DEMO-`` id
    prefix for the registers — so a real record can never match.
    """
    ensure_land_tables()
    removed = {kind: 0 for kind in DEMO_ENTITY_KINDS}
    removed["events"] = 0
    with get_db() as db:
        demo_doc_ids = [row["id"] for row in db.execute(
            "SELECT id, metadata FROM documents WHERE metadata IS NOT NULL AND metadata <> '{}'").fetchall()
            if _is_demo_metadata(row["metadata"])]
        if demo_doc_ids:
            placeholders = ",".join("?" for _ in demo_doc_ids)
            db.execute(f"DELETE FROM documents WHERE id IN ({placeholders})", tuple(demo_doc_ids))
            removed["documents"] = len(demo_doc_ids)
        removed["encumbrances"] = db.execute("DELETE FROM land_encumbrances WHERE id LIKE ?",
                                            (DEMO_ID_PREFIX + "%",)).rowcount or 0
        demo_mutation_ids = [row["id"] for row in db.execute(
            "SELECT id FROM land_mutations WHERE id LIKE ?", (DEMO_ID_PREFIX + "%",)).fetchall()]
        if demo_mutation_ids:
            placeholders = ",".join("?" for _ in demo_mutation_ids)
            db.execute(f"DELETE FROM land_mutation_events WHERE mutation_id IN ({placeholders})", tuple(demo_mutation_ids))
            db.execute(f"DELETE FROM land_mutations WHERE id IN ({placeholders})", tuple(demo_mutation_ids))
            removed["mutations"] = len(demo_mutation_ids)
        removed["events"] = removed["mutations"]
        removed["court_cases"] = db.execute("DELETE FROM land_court_cases WHERE id LIKE ?",
                                            (DEMO_ID_PREFIX + "%",)).rowcount or 0
    _audit({"email": actor, "full_name": actor}, "DEMO_DATA_CLEARED", f"Demo data cleared: {removed}")
    return {"status": "cleared", "removed": removed}
