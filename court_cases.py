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
from typing import Any, Dict, List, Optional

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
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    parties TEXT DEFAULT '',
    relief_sought TEXT DEFAULT '',
    decision_summary TEXT DEFAULT '',
    evidence_doc_ids TEXT DEFAULT '[]',
    notes TEXT DEFAULT '',
    created_by TEXT DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
)
"""


def ensure_schema() -> None:
    with get_db() as db:
        db.execute(_SCHEMA)
        try:
            db.execute("CREATE INDEX IF NOT EXISTS idx_land_cases_land ON land_court_cases(survey_number, village)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_land_cases_status ON land_court_cases(status)")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_land_cases_case_number ON land_court_cases(case_number)")
        except Exception:
            pass


def _s(value: Any) -> str:
    return str(value or "").strip()


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", _s(value)).casefold()


def _dict(row: Any) -> Optional[Dict[str, Any]]:
    return dict(row) if row else None


def list_cases(survey: str, village: str = "") -> List[Dict[str, Any]]:
    ensure_schema()
    survey_n, village_n = _norm(survey), _norm(village)
    with get_db() as db:
        rows = db.execute("SELECT * FROM land_court_cases ORDER BY filed_date DESC, created_at DESC").fetchall()
    result = []
    for row in rows:
        if _norm(row["survey_number"]) != survey_n:
            continue
        rv = _norm(row["village"])
        if village_n and rv and rv != village_n:
            continue
        result.append(_dict(row))
    return result


def active_cases(survey: str, village: str = "") -> List[Dict[str, Any]]:
    return [c for c in list_cases(survey, village) if _s(c.get("status")).upper() == "ACTIVE"]


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
        with get_db() as db:
            rows = db.execute("SELECT * FROM land_court_cases ORDER BY filed_date DESC, created_at DESC").fetchall()
        items = [_dict(r) for r in rows]
    else:
        items = list_cases(survey, village)
    wanted = _s(status_filter).upper()
    needle = _s(q).casefold()
    if wanted:
        items = [c for c in items if _s(c.get("status")).upper() == wanted]
    if needle:
        items = [c for c in items if needle in " ".join(str(c.get(k) or "") for k in
                  ("case_number", "court_name", "parties", "survey_number", "village", "district", "case_type", "status")).casefold()]
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
            (id,survey_number,khasra_number,village,tehsil,district,case_number,case_type,court_name,
             filed_date,closed_date,status,parties,relief_sought,decision_summary,evidence_doc_ids,notes,created_by,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cid,survey,_s(req.khasra_number),_s(req.village),_s(req.tehsil),_s(req.district),_s(req.case_number),
             case_type,_s(req.court_name),_s(req.filed_date),"","ACTIVE",_s(req.parties),_s(req.relief_sought),"",
             __import__("json").dumps(req.evidence_doc_ids),_s(req.notes),user.get("email") or "",now,now))
    _audit(user, "COURT_CASE_CREATED", f"Case {_s(req.case_number)} registered on survey {survey} ({_s(req.village)})")
    with get_db() as db:
        row = db.execute("SELECT * FROM land_court_cases WHERE id=?", (cid,)).fetchone()
    return {"court_case": _dict(row)}


@router.get("/api/court-cases/{case_id}")
def get_court_case(case_id: str, user: Dict[str, Any] = Depends(get_current_user)):
    ensure_schema()
    with get_db() as db:
        row = db.execute("SELECT * FROM land_court_cases WHERE id=?", (_s(case_id),)).fetchone()
    if not row:
        raise HTTPException(404, "Court case not found")
    return {"court_case": _dict(row)}


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
    active = [c for c in cases if _s(c.get("status")).upper() == "ACTIVE"]
    closed = [c for c in cases if _s(c.get("status")).upper() in {"DECIDED","WITHDRAWN","SETTLED"}]
    return {
        "land_id": land_id,
        "survey": land.get("survey"),
        "village": land.get("village"),
        "active_count": len(active),
        "closed_count": len(closed),
        "court_cases": cases,
        "verdict": "ACTIVE_LITIGATION" if active else ("PRIOR_LITIGATION" if closed else "CLEAR"),
        "disclaimer": "Litigation status is based only on locally registered cases; it is not a court-certified search.",
    }


def litigation_flags(survey: str, village: str, mutations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    cases = list_cases(survey, village)
    flags: List[Dict[str, Any]] = []
    active = [c for c in cases if _s(c.get("status")).upper() == "ACTIVE"]
    closed = [c for c in cases if _s(c.get("status")).upper() in {"DECIDED","WITHDRAWN","SETTLED"}]
    for case in active:
        flags.append({"code":"ACTIVE_LITIGATION","severity":"HIGH","title":"Active court case on this land",
                      "detail":f"{case.get('case_number') or 'Case'} — {case.get('court_name') or 'court'} ({case.get('case_type') or 'type'}). Pending litigation must be reviewed before transfer.",
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
</script></div></body></html>""")

# Initialize schema on import so the first request is not responsible for the migration.
ensure_schema()
