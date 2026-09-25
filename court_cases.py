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
    """Group the whole register by normalised survey number in ONE query.

    The land-risk engine scores many parcels per request; looking each parcel up
    separately would issue one query per land record (N+1). :func:`cases_for_land`
    then applies the register's own matching rule in memory, so the batch path
    and the per-parcel path can never disagree.
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for case in _all_cases():
        grouped.setdefault(_norm(case.get("survey_number") or case.get("khasra_number")), []).append(case)
    return grouped


def cases_for_land(land: Dict[str, Any], grouped: Optional[Dict[str, List[Dict[str, Any]]]] = None) -> List[Dict[str, Any]]:
    """Cases applicable to one land parcel.

    Same rule as :func:`list_cases`: the survey number must match, and a village
    only narrows the result when the register entry recorded one.
    """
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
    """CLEAR | PRIOR_LITIGATION | ACTIVE_LITIGATION for a set of cases."""
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
    # additive DEMO-LI fields (structured parties, hearing data, stay flag)
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
        # Staff may use this as a register search; viewers must scope to a land parcel.
        if user.get("role") not in REVIEWER_ROLES:
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
    # additive: expose the derived land record id so the UI can deep-link from
    # the litigation register to the full land record (same deterministic
    # identity scheme as every /api/land-records payload).
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
def get_court_case(case_id: str, user: Dict[str, Any] = Depends(get_current_user)):
    ensure_schema()
    with get_db() as db:
        row = db.execute("SELECT * FROM land_court_cases WHERE id=? OR case_number=?", (_s(case_id), _s(case_id))).fetchone()
        if not row:
            raise HTTPException(404, "Court case not found")
        case = _dict(row)
        case["orders"] = _case_orders(db, case["id"])
    return {"court_case": case}


@router.post("/api/court-cases/{case_id}/close")
def close_court_case(case_id: str, req: CourtCaseClose,
                     user: Dict[str, Any] = Depends(require_roles(*REVIEWER_ROLES))):
    ensure_schema()
    status_value = _s(req.status).upper()
    if status_value not in ("DECIDED", "WITHDRAWN", "SETTLED"):
        raise HTTPException(422, "Closing status must be DECIDED, WITHDRAWN or SETTLED")
    with get_db() as db:
        row = db.execute("SELECT * FROM land_court_cases WHERE id=?", (_s(case_id),)).fetchone()
        if not row:
            raise HTTPException(404, "Court case not found")
        if _s(row["status"]).upper() != "ACTIVE":
            raise HTTPException(409, "Only an ACTIVE court case can be closed")
        db.execute("UPDATE land_court_cases SET status=?, closed_date=?, decision_summary=?, notes=CASE WHEN ?<>'' THEN ? ELSE notes END, updated_at=? WHERE id=?",
                   (status_value,_s(req.closed_date),_s(req.decision_summary),_s(req.notes),_s(req.notes),time.time(),_s(case_id)))
    _audit(user, "COURT_CASE_CLOSED", f"Case {row['case_number']} closed as {status_value}")
    with get_db() as db:
        updated = db.execute("SELECT * FROM land_court_cases WHERE id=?", (_s(case_id),)).fetchone()
    return {"court_case": _dict(updated)}


class CourtCaseUpdate(BaseModel):
    """Amend a case's particulars. Closing/outcome transitions stay on /close."""
    case_type: Optional[str] = None
    court_name: Optional[str] = None
    filed_date: Optional[str] = None
    parties: Optional[str] = None
    relief_sought: Optional[str] = None
    evidence_doc_ids: Optional[List[str]] = None
    notes: Optional[str] = None
    # additive DEMO-LI fields
    petitioner: Optional[str] = None
    respondent: Optional[str] = None
    title: Optional[str] = None
    stage: Optional[str] = None
    issue_summary: Optional[str] = None
    next_hearing_date: Optional[str] = None
    affects_transfer: Optional[bool] = None


_UPDATABLE = ("case_type", "court_name", "filed_date", "parties", "relief_sought", "notes",
              "petitioner", "respondent", "title", "stage", "issue_summary")


@router.put("/api/court-cases/{case_id}")
def update_court_case(case_id: str, req: CourtCaseUpdate,
                     user: Dict[str, Any] = Depends(require_roles(*STAFF_ROLES))):
    """Correct a registered case. Data Officers may only amend their own
    entries; only an ACTIVE case may be amended (closed records are immutable)."""
    ensure_schema()
    with get_db() as db:
        row = db.execute("SELECT * FROM land_court_cases WHERE id=?", (_s(case_id),)).fetchone()
        if not row:
            raise HTTPException(404, "Court case not found")
        case = _dict(row)
        if user.get("role") == ROLE_DATA_OFFICER:
            if case.get("created_by") != user.get("email"):
                raise HTTPException(403, "Data Officers can only amend court cases they registered")
            if _s(case.get("status")).upper() != "ACTIVE":
                raise HTTPException(409, "A closed court case can only be amended by a reviewer")
        updates = {key: _s(value) for key, value in req.model_dump().items()
                   if key in _UPDATABLE and value is not None}
        if req.case_type is not None:
            case_type = _s(req.case_type).upper() or "CIVIL"
            if case_type not in CASE_TYPES:
                raise HTTPException(422, f"Invalid case type. Use one of {CASE_TYPES}")
            updates["case_type"] = case_type
        if req.evidence_doc_ids is not None:
            _validate_evidence(req.evidence_doc_ids, user)
            updates["evidence_doc_ids"] = __import__("json").dumps(req.evidence_doc_ids)
        if req.next_hearing_date is not None:
            updates["next_hearing_date"] = _s(req.next_hearing_date) or None
        if req.affects_transfer is not None:
            updates["affects_transfer"] = 1 if req.affects_transfer else 0
        if not updates:
            raise HTTPException(422, "No changes supplied")
        assignments = ", ".join(f"{column}=?" for column in updates)
        db.execute(f"UPDATE land_court_cases SET {assignments}, updated_at=? WHERE id=?",
                   (*updates.values(), time.time(), _s(case_id)))
        changed = ", ".join(sorted(updates))
    _audit(user, "COURT_CASE_UPDATED", f"Case {case.get('case_number')} amended ({changed})")
    with get_db() as db:
        updated = db.execute("SELECT * FROM land_court_cases WHERE id=?", (_s(case_id),)).fetchone()
    return {"court_case": _dict(updated)}


class CaseOrderCreate(BaseModel):
    order_date: str = ""
    order_type: str = "ORDER"
    summary: str


CASE_ORDER_TYPES = ("HEARING", "ORDER", "INJUNCTION", "COMMISSION", "DECREE", "DISPOSAL", "WITHDRAWAL", "ADJOURNMENT", "OTHER")


@router.post("/api/court-cases/{case_id}/orders")
def add_case_order(case_id: str, req: CaseOrderCreate,
                   user: Dict[str, Any] = Depends(require_roles(*REVIEWER_ROLES))):
    """Append an interim order / hearing note to a case's structured log."""
    ensure_schema()
    order_type = _s(req.order_type).upper() or "ORDER"
    if order_type not in CASE_ORDER_TYPES:
        raise HTTPException(422, f"Invalid order type. Use one of {CASE_ORDER_TYPES}")
    summary = _s(req.summary)
    if not summary:
        raise HTTPException(422, "An order summary is required")
    with get_db() as db:
        row = db.execute("SELECT * FROM land_court_cases WHERE id=? OR case_number=?", (_s(case_id), _s(case_id))).fetchone()
        if not row:
            raise HTTPException(404, "Court case not found")
        db.execute("INSERT INTO land_case_orders (id, case_id, order_date, order_type, summary, created_by, created_at) VALUES (?,?,?,?,?,?,?)",
                   (uuid.uuid4().hex[:12], row["id"], _s(req.order_date), order_type, summary, user.get("email") or "", time.time()))
        case = _dict(row)
        case["orders"] = _case_orders(db, row["id"])
    _audit(user, "COURT_CASE_ORDER_ADDED",
           f"Order ({order_type}, {_s(req.order_date) or 'undated'}) added to court case {_s(row['case_number'])}: {summary[:120]}")
    return {"court_case": case}


@router.get("/api/land-records/{land_id}/litigation")
def land_litigation(land_id: str, user: Dict[str, Any] = Depends(get_current_user)):
    """Litigation summary keyed to the same LR-* identity used by Land Intelligence."""
    try:
        import land_intel
        land = land_intel._get_land(user, land_id)
    except Exception:
        land = None
    if not land:
        raise HTTPException(404, "Land record not found")
    cases = list_cases(land.get("survey") or "", land.get("village") or "")
    with get_db() as db:
        for case in cases:
            case["orders"] = _case_orders(db, _s(case.get("id")))
    active = [c for c in cases if _s(c.get("status")).upper() == "ACTIVE"]
    closed = [c for c in cases if _s(c.get("status")).upper() in CLOSED_STATUSES]
    return {
        "land_id": land_id,
        "survey": land.get("survey"),
        "village": land.get("village"),
        "active_count": len(active),
        "closed_count": len(closed),
        "court_cases": cases,
        "highest_severity": "HIGH" if active else ("INFO" if closed else "NONE"),
        "verdict": litigation_verdict_for(cases),
        "disclaimer": "Litigation status is based only on locally registered cases; it is not a court-certified search.",
    }


def litigation_flags(survey: str, village: str, mutations: List[Dict[str, Any]],
                     cases: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Deterministic review signals derived from the litigation register.

    `cases` may be supplied by a caller that already grouped the register (the
    land-risk engine scores many parcels at once and must not query per parcel).
    Severity mapping: an ACTIVE case is always HIGH; a transfer recorded while a
    case was pending is HIGH; closed cases stay visible as INFO for transparency.
    """
    cases = list_cases(survey, village) if cases is None else cases
    flags: List[Dict[str, Any]] = []
    active = [c for c in cases if _s(c.get("status")).upper() == "ACTIVE"]
    closed = [c for c in cases if _s(c.get("status")).upper() in CLOSED_STATUSES]
    for case in active:
        parties = _s(case.get("parties")) or " v. ".join(
            part for part in (_s(case.get("petitioner")), _s(case.get("respondent"))) if part)
        hearing = _s(case.get("next_hearing_date"))
        flags.append({"code":"ACTIVE_LITIGATION","severity":"HIGH","title":"Active court case on this land",
                      "detail":f"{case.get('case_number') or 'Case'} — {case.get('court_name') or 'court'} ({case.get('case_type') or 'type'})"
                               + (f", {parties}" if parties else "")
                               + (f", next hearing {hearing}" if hearing else "")
                               + ". Pending litigation must be reviewed before transfer.",
                      "evidence":[{"type":"court_case","ref":case.get("id"),"label":case.get("case_number")}]})
        if case.get("affects_transfer"):
            flags.append({"code":"TRANSFER_STAYED","severity":"HIGH","title":"Court stay/injunction recorded against transfer",
                          "detail":f"Case {case.get('case_number') or 'Case'} carries an interim order affecting transfer of this land. "
                                   "Any sale, gift, lease or mutation completion must wait until the order is vacated or the case is decided.",
                          "evidence":[{"type":"court_case","ref":case.get("id"),"label":case.get("case_number")}]})
        filed = _s(case.get("filed_date")); closed_date = _s(case.get("closed_date"))
        for mutation in mutations or []:
            deed = _s(mutation.get("deed_date"))
            if filed and deed and deed >= filed and (not closed_date or deed <= closed_date):
                flags.append({"code":"TRANSFER_DURING_LITIGATION","severity":"HIGH","title":"Transfer while litigation was pending",
                              "detail":f"Mutation {mutation.get('mutation_no') or mutation.get('id')} has deed date {deed} while case {case.get('case_number')} was pending.",
                              "evidence":[{"type":"court_case","ref":case.get("id"),"label":case.get("case_number")},{"type":"mutation","ref":mutation.get("id"),"label":mutation.get("mutation_no")}]})
                break
    if closed:
        flags.append({"code":"CLOSED_LITIGATION_ON_RECORD","severity":"INFO","title":"Prior litigation exists on this land",
                      "detail":f"{len(closed)} closed court case(s) remain on the record for transparency.",
                      "evidence":[{"type":"court_case","ref":c.get("id"),"label":c.get("case_number")} for c in closed[:5]]})
    return flags


@router.get("/api/land-records/{land_id}/litigation-risk")
def litigation_risk(land_id: str, user: Dict[str, Any] = Depends(get_current_user)):
    try:
        import land_intel
        land = land_intel._get_land(user, land_id)
    except Exception:
        land = None
    if not land:
        raise HTTPException(404, "Land record not found")
    _, mutations = land_intel._land_register_rows(land)
    flags = litigation_flags(land.get("survey") or "", land.get("village") or "", mutations)
    return {"land_id":land_id,"verdict":"HIGH_RISK" if any(f["severity"]=="HIGH" for f in flags) else ("REVIEW" if flags else "CLEAR"),"flags":flags}


@router.get("/litigation")
def litigation_page(user: Dict[str, Any] = Depends(get_current_user)):
    """Small responsive litigation workspace using the existing authenticated session."""
    return HTMLResponse("""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Land Litigation</title><style>body{font-family:system-ui;margin:0;background:#f4f7fb;color:#0f172a}.wrap{max-width:1100px;margin:auto;padding:24px}.card{background:#fff;border:1px solid #dbe3ee;border-radius:14px;padding:18px;margin:14px 0}.row{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}input,select,textarea,button{padding:10px;border:1px solid #cbd5e1;border-radius:8px;font:inherit}button{cursor:pointer;background:#0f3d5e;color:white}.danger{border-left:5px solid #dc2626}.muted{color:#64748b;font-size:13px}@media(max-width:700px){.row{grid-template-columns:1fr}}</style></head><body><div class='wrap'>
<h1>⚖️ Court Case / Litigation</h1><p class='muted'>Register and review court cases against land parcels. This is an internal register, not a court-certified search.</p>
<div class='card'><h3>Search land litigation</h3><div class='row'><input id='survey' placeholder='Survey / Khasra'><input id='village' placeholder='Village'><button onclick='load()'>Check</button></div></div>
<div id='out'></div>
<script>
async function load(){const s=document.getElementById('survey').value.trim(),v=document.getElementById('village').value.trim();if(!s){alert('Enter a survey number');return}const r=await fetch('/api/court-cases?survey='+encodeURIComponent(s)+'&village='+encodeURIComponent(v));const d=await r.json();let h='';if(d.active)h+='<div class="card danger"><b>🔴 ACTIVE LITIGATION</b><p>'+d.active+' active case(s) require review.</p></div>';if(!d.court_cases.length)h+='<div class="card">🟢 No court cases registered for this land.</div>';for(const c of d.court_cases){h+='<div class="card"><b>'+esc(c.case_number)+'</b> · '+esc(c.case_type)+' · '+esc(c.status)+'<p>'+esc(c.court_name)+' · filed '+esc(c.filed_date)+'</p><p>'+esc(c.parties)+'</p><p>'+esc(c.relief_sought)+'</p><p>'+esc(c.decision_summary)+'</p></div>'}document.getElementById('out').innerHTML=h}
function esc(s){return String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;')}
// Deep links from SA Investigation evidence (/litigation?survey=&village=&case=) prefill the search and run it.
(function(){const q=new URLSearchParams(window.location.search);const s=q.get('survey')||'';const v=q.get('village')||'';const c=q.get('case')||'';if(s){document.getElementById('survey').value=s;document.getElementById('village').value=v;load();}if(c){document.getElementById('out').insertAdjacentHTML('afterbegin','<p class="muted">Opened from an SA investigation for case '+esc(c)+'. Only cases registered in this system are shown.</p>');}})();
</script></div></body></html>""")

# Initialize schema on import so the first request is not responsible for the migration.
ensure_schema()
