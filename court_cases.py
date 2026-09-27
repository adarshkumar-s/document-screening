"""Court case / litigation register for Land Intelligence.

This is an additive extension of the existing document-screening application.
It keeps litigation as a separate audited register keyed by survey number +
village, and exposes deterministic review signals rather than legal conclusions.
"""
from __future__ import annotations

import html
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from server import (
    ROLE_ADMIN,
    ROLE_DATA_OFFICER,
    ROLE_VERIFICATION_OFFICER,
    get_current_user,
    get_db,
    log_audit,
    require_roles,
)

router = APIRouter(tags=["Land Litigation"])
STAFF_ROLES = (ROLE_DATA_OFFICER, ROLE_VERIFICATION_OFFICER, ROLE_ADMIN)
REVIEWER_ROLES = (ROLE_VERIFICATION_OFFICER, ROLE_ADMIN)
CASE_TYPES = ("CIVIL", "CRIMINAL", "REVENUE", "POSSESSION", "TITLE", "OTHER")
CASE_STATUSES = ("ACTIVE", "DECIDED", "WITHDRAWN", "SETTLED")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS land_court_cases (
    id TEXT PRIMARY KEY,
    property_id TEXT,
    survey_number TEXT NOT NULL DEFAULT '',
    khasra_number TEXT DEFAULT '',
    village TEXT DEFAULT '',
    tehsil TEXT DEFAULT '',
    district TEXT DEFAULT '',
    case_number TEXT NOT NULL,
    case_type TEXT NOT NULL DEFAULT 'CIVIL',
    court_name TEXT NOT NULL DEFAULT '',
    filed_date TEXT DEFAULT '',
    closed_date TEXT DEFAULT '',
    next_hearing_date TEXT,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    stage TEXT DEFAULT '',
    parties TEXT DEFAULT '',
    petitioner TEXT DEFAULT '',
    respondent TEXT DEFAULT '',
    title TEXT DEFAULT '',
    issue_summary TEXT DEFAULT '',
    relief_sought TEXT DEFAULT '',
    decision_summary TEXT DEFAULT '',
    affects_transfer INTEGER NOT NULL DEFAULT 0,
    related_mutation_id TEXT,
    related_encumbrance_id TEXT,
    evidence_doc_ids TEXT DEFAULT '[]',
    notes TEXT DEFAULT '',
    created_by TEXT DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
)
"""

# Structured order log (DEMO-LI dataset and the /orders endpoint). Kept beside
# the register so a case's interim orders travel with it.
_ORDERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS land_case_orders (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    order_date TEXT DEFAULT '',
    order_type TEXT DEFAULT 'ORDER',
    summary TEXT DEFAULT '',
    created_by TEXT DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0
)
"""

#: Columns added after the register first shipped; existing databases are
#: migrated additively so older rows keep working (mirrors mapping.py).
_MIGRATION_COLUMNS = (
    ("property_id", "TEXT"),
    ("next_hearing_date", "TEXT"),
    ("stage", "TEXT DEFAULT ''"),
    ("petitioner", "TEXT DEFAULT ''"),
    ("respondent", "TEXT DEFAULT ''"),
    ("title", "TEXT DEFAULT ''"),
    ("issue_summary", "TEXT DEFAULT ''"),
    ("affects_transfer", "INTEGER NOT NULL DEFAULT 0"),
    ("related_mutation_id", "TEXT"),
    ("related_encumbrance_id", "TEXT"),
)


_schema_ready = False


def ensure_schema() -> None:
    """Create the register schema once per process.

    DDL (CREATE TABLE / CREATE INDEX) must never run inside a user request —
    CREATE INDEX can block for a long time on a large database. The flag turns
    every later call, including those inside request paths, into a no-op.
    """
    global _schema_ready
    if _schema_ready:
        return
    with get_db() as db:
        db.execute(_SCHEMA)
        try:
            db.execute(_ORDERS_SCHEMA)
            db.execute("CREATE INDEX IF NOT EXISTS idx_land_cases_land ON land_court_cases(survey_number, village)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_land_cases_status ON land_court_cases(status)")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_land_cases_case_number ON land_court_cases(case_number)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_land_case_orders_case ON land_case_orders(case_id)")
        except Exception:
            pass
        # Additive migration for registers created before the DEMO-LI merge.
        try:
            existing = {row["name"] for row in db.execute("PRAGMA table_info(land_court_cases)").fetchall()}
            for column, definition in _MIGRATION_COLUMNS:
                if column not in existing:
                    db.execute(f"ALTER TABLE land_court_cases ADD COLUMN {column} {definition}")
        except Exception:
            pass
    _schema_ready = True


def _s(value: Any) -> str:
    return str(value or "").strip()


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", _s(value)).casefold()


def _dict(row: Any) -> Optional[Dict[str, Any]]:
    return dict(row) if row else None


CLOSED_STATUSES = ("DECIDED", "SETTLED", "WITHDRAWN")
DEMO_ID_PREFIX = "DEMO-"  # mirrors the demo-tagging convention used by the
                          # mutation/encumbrance registers (demo_scenarios.py)


def _all_cases() -> List[Dict[str, Any]]:
    """Every registered case, newest filing first (single query)."""
    ensure_schema()
    with get_db() as db:
        rows = db.execute("SELECT * FROM land_court_cases ORDER BY filed_date DESC, created_at DESC").fetchall()
    return [_dict(row) for row in rows]


def _matches(case: Dict[str, Any], survey_n: str, village_n: str) -> bool:
    if _norm(case.get("survey_number")) != survey_n:
        return False
    case_village = _norm(case.get("village"))
    if village_n and case_village and case_village != village_n:
        return False
    return True


def list_cases(survey: str, village: str = "") -> List[Dict[str, Any]]:
    survey_n, village_n = _norm(survey), _norm(village)
    return [case for case in _all_cases() if _matches(case, survey_n, village_n)]


def cases_by_land() -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for case in _all_cases():
        grouped.setdefault(_norm(case.get("survey_number") or case.get("khasra_number")), []).append(case)
    return grouped


def cases_for_land(land: Dict[str, Any], grouped: Optional[Dict[str, List[Dict[str, Any]]]] = None) -> List[Dict[str, Any]]:
    survey_n, village_n = _norm(land.get("survey") or land.get("khasra")), _norm(land.get("village"))
    if not survey_n:
        return []
    if grouped is None:
        return list_cases(survey_n, village_n)
    return [case for case in grouped.get(survey_n, []) if not _norm(case.get("village")) or not village_n
            or _norm(case.get("village")) == village_n]


def active_cases(survey: str, village: str = "") -> List[Dict[str, Any]]:
    return [c for c in list_cases(survey, village) if _s(c.get("status")).upper() == "ACTIVE"]


def litigation_verdict_for(cases: List[Dict[str, Any]]) -> str:
    if any(_s(case.get("status")).upper() == "ACTIVE" for case in cases):
        return "ACTIVE_LITIGATION"
    return "PRIOR_LITIGATION" if cases else "CLEAR"


def _audit(user: Dict[str, Any], action: str, detail: str) -> None:
    try:
        log_audit(user.get("full_name") or user.get("email") or "SYSTEM", action, detail, None)
    except Exception:
        pass


class CourtCaseCreate(BaseModel):
    survey_number: str = ""
    khasra_number: str = ""
    village: str = ""
    tehsil: str = ""
    district: str = ""
    case_number: str
    case_type: str = "CIVIL"
    court_name: str = ""
    filed_date: str = ""
    parties: str = ""
    relief_sought: str = ""
    evidence_doc_ids: List[str] = []
    notes: str = ""
    petitioner: str = ""
    respondent: str = ""
    title: str = ""
    stage: str = ""
    issue_summary: str = ""
    next_hearing_date: Optional[str] = None
    affects_transfer: bool = False
    related_mutation_id: Optional[str] = None
    related_encumbrance_id: Optional[str] = None
    property_id: Optional[str] = None


class CourtCaseClose(BaseModel):
    status: str = "DECIDED"
    closed_date: str = ""
    decision_summary: str = ""
    notes: str = ""


def _validate_evidence(ids: List[str], user: Dict[str, Any]) -> None:
    if not ids:
        return
    with get_db() as db:
        for doc_id in ids:
            row = db.execute("SELECT id, status, uploaded_by FROM documents WHERE id=?", (_s(doc_id),)).fetchone()
            if not row:
                raise HTTPException(404, f"Evidence document {doc_id} not found")
            if user.get("role") == ROLE_DATA_OFFICER and row["uploaded_by"] != user.get("email"):
                raise HTTPException(403, "Data Officers can only reference their own evidence documents")


@router.get("/api/court-cases")
def get_court_cases(survey: str = Query(""), village: str = Query(""),
                   status_filter: str = Query("", alias="status"),
                   q: str = Query(""), user: Dict[str, Any] = Depends(get_current_user)):
    ensure_schema()
    if not survey:
        if user.get("role") not in STAFF_ROLES:
            raise HTTPException(400, "survey number is required")
        items = _all_cases()
    else:
        items = list_cases(survey, village)
    wanted = _s(status_filter).upper()
    needle = _s(q).casefold()
    if wanted:
        items = [c for c in items if _s(c.get("status")).upper() == wanted]
    if needle:
        items = [c for c in items if needle in " ".join(str(c.get(k) or "") for k in
                  ("case_number", "court_name", "parties", "survey_number", "village", "district", "case_type", "status")).casefold()]
    try:
        from land_intel import land_identity
        for item in items:
            if isinstance(item, dict):
                try:
                    item["land_id"] = land_identity({"survey_number": item.get("survey_number"), "khasra_number": item.get("khasra_number"), "village": item.get("village")})
                except Exception:
                    pass
    except Exception:
        pass
    return {"court_cases": items, "active": sum(1 for c in items if _s(c.get("status")).upper() == "ACTIVE")}


@router.post("/api/court-cases")
def create_court_case(req: CourtCaseCreate, user: Dict[str, Any] = Depends(require_roles(*STAFF_ROLES))):
    ensure_schema()
    case_type = _s(req.case_type).upper() or "CIVIL"
    if case_type not in CASE_TYPES:
        raise HTTPException(422, f"Invalid case type. Use one of {CASE_TYPES}")
    if not _s(req.case_number):
        raise HTTPException(422, "Case number is required")
    survey = _s(req.survey_number) or _s(req.khasra_number)
    if not survey:
        raise HTTPException(422, "Survey/Khasra number is required")
    _validate_evidence(req.evidence_doc_ids, user)
    cid = uuid.uuid4().hex[:12]
    now = time.time()
    with get_db() as db:
        exists = db.execute("SELECT id FROM land_court_cases WHERE case_number=?", (_s(req.case_number),)).fetchone()
        if exists:
            raise HTTPException(409, "A court case with this case number is already registered")
        db.execute("""INSERT INTO land_court_cases
            (id,property_id,survey_number,khasra_number,village,tehsil,district,case_number,case_type,court_name,
             filed_date,closed_date,next_hearing_date,status,stage,parties,petitioner,respondent,title,issue_summary,
             relief_sought,decision_summary,affects_transfer,related_mutation_id,related_encumbrance_id,
             evidence_doc_ids,notes,created_by,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cid,_s(req.property_id) or None,survey,_s(req.khasra_number),_s(req.village),_s(req.tehsil),_s(req.district),
             _s(req.case_number),case_type,_s(req.court_name),_s(req.filed_date),"",
             _s(req.next_hearing_date) or None,"ACTIVE",_s(req.stage),_s(req.parties),_s(req.petitioner),
             _s(req.respondent),_s(req.title),_s(req.issue_summary),_s(req.relief_sought),"",
             1 if req.affects_transfer else 0,_s(req.related_mutation_id) or None,_s(req.related_encumbrance_id) or None,
             __import__("json").dumps(req.evidence_doc_ids),_s(req.notes),user.get("email") or "",now,now))
    _audit(user, "COURT_CASE_CREATED", f"Case {_s(req.case_number)} registered on survey {survey} ({_s(req.village)})")
    with get_db() as db:
        row = db.execute("SELECT * FROM land_court_cases WHERE id=?", (cid,)).fetchone()
    return {"court_case": _dict(row)}


def _case_orders(db: Any, case_id: str) -> List[Dict[str, Any]]:
    try:
        rows = db.execute("SELECT * FROM land_case_orders WHERE case_id=? ORDER BY COALESCE(order_date,'') ASC, created_at ASC",
                          (_s(case_id),)).fetchall()
    except Exception:
        return []
    return [dict(row) for row in rows]


@router.get("/api/court-cases/{case_id}")
def get_court_case(case_id: str, user: Dict[str, Any] = Depends(require_roles(*STAFF_ROLES))):
    """Return case details only to Land Intelligence staff.

    Parcel-scoped viewers use /api/land-records/{land_id}/litigation, which
    resolves the land record through the existing RBAC-aware resolver. The
    direct case-id endpoint is intentionally staff-only to prevent IDOR-style
    enumeration of parties, notes, evidence IDs and court details.
    """
    ensure_schema()
    with get_db() as db:
        row = db.execute("SELECT * FROM land_court_cases WHERE id=? OR case_number=?", (_s(case_id), _s(case_id))).fetchone()
        if not row:
            raise HTTPException(404, "Court case not found")
        case = _dict(row)
        case["orders"] = _case_orders(db, case["id"])
    return {"court_case": case}


class CourtCaseClose(BaseModel):
    status: str = "DECIDED"
    closed_date: str = ""
    decision_summary: str = ""
    notes: str = ""


# Initialize schema on import so the first request is not responsible for the migration.
ensure_schema()
