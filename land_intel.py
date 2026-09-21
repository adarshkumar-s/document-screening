"""Land Intelligence core: encumbrances, mutations, the deterministic land-risk
engine, land-record detail aggregation, and verification reports.

This module EXTENDS the existing document-screening application; it does not
replace any canonical system. Authentication, roles, RBAC, the audit trail and
document visibility rules remain owned by ``server.py``; document/land joining
helpers and role-scoped record visibility are reused from ``mapping.py``.

Safety philosophy (mirrors the existing application charter):
  * The risk engine is DETERMINISTIC and rule-based. It produces workflow
    review signals with evidence. It never declares legal fraud and is never
    the final legal authority. Human review/approval stays authoritative.
  * Completing a mutation NEVER rewrites OCR document fields (the mapping
    layer's data boundary). Completion is recorded as audited register events.
  * Active encumbrances raise a hard safety gate before a mutation can be
    completed. The gate requires an explicit human confirmation; RBAC is
    never bypassed.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from server import (
    ROLE_ADMIN,
    ROLE_DATA_OFFICER,
    ROLE_VERIFICATION_OFFICER,
    ROLE_VIEWER,
    get_current_user,
    get_db,
    log_audit,
    require_roles,
)

import mapping

encumbrance_router = APIRouter(prefix="/api/encumbrances", tags=["Land Encumbrances"])
mutation_router = APIRouter(prefix="/api/mutations", tags=["Mutations"])
land_router = APIRouter(prefix="/api/land-records", tags=["Land Records"])
report_router = APIRouter(prefix="/api/reports", tags=["Verification Reports"])

STAFF_ROLES = (ROLE_DATA_OFFICER, ROLE_VERIFICATION_OFFICER, ROLE_ADMIN)
REVIEWER_ROLES = (ROLE_VERIFICATION_OFFICER, ROLE_ADMIN)

ENCUMBRANCE_STATUSES = ("ACTIVE", "RELEASED", "UNKNOWN")
MUTATION_STATUSES = ("RECEIVED", "UNDER_REVIEW", "VERIFIED", "COMPLETED", "REJECTED")
RISK_VERDICTS = ("CLEAR", "REVIEW", "HIGH_RISK")
TRANSFER_MUTATION_TYPES = ("SALE", "GIFT", "INHERITANCE", "PARTITION", "MERGER", "COURT_DECREE", "OTHER")

AREA_JUMP_RATIO = 0.15
CHAIN_GAP_YEARS = 10
OWNER_NAME_SIMILARITY = 0.82
SUSPICIOUS_MUTATION_WINDOW_DAYS = 180
SUSPICIOUS_MUTATION_COUNT = 3


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _now() -> float:
    return time.time()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _parse_json(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, (dict, list)) else fallback
    except Exception:
        return fallback


def _text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any) -> Optional[float]:
    raw = _text(value).replace(",", "")
    match = re.search(r"(\d+(?:\.\d+)?)", raw)
    try:
        return float(match.group(1)) if match else None
    except Exception:
        return None


def _year_of(value: Any) -> Optional[int]:
    match = re.search(r"(\d{4})", str(value or ""))
    return int(match.group(1)) if match else None


_DEVANAGARI_MAP = {
    "अ": "a", "आ": "a", "इ": "i", "ई": "ee", "उ": "u", "ऊ": "oo", "ए": "e",
    "ऐ": "ai", "ओ": "o", "औ": "au", "क": "k", "ख": "kh", "ग": "g", "घ": "gh",
    "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "ny", "ट": "t", "ठ": "thh",
    "ड": "d", "ढ": "dh", "ण": "n", "त": "t", "थ": "th", "द": "d", "ध": "dh",
    "न": "n", "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m", "य": "y",
    "र": "r", "ल": "l", "व": "v", "श": "sh", "ष": "sh", "स": "s", "ह": "h",
    "ा": "a", "ि": "i", "ी": "ee", "ु": "u", "ू": "oo", "े": "e", "ै": "ai",
    "ो": "o", "ौ": "au", "ँ": "n", "ं": "m", "ः": "h", "्": "",
}


def _owner_key(name: Any) -> str:
    """Script/spelling tolerant owner key (Devanagari transliterated)."""
    text = _text(name)
    if re.search(r"[\u0900-\u097F]", text):
        text = "".join(_DEVANAGARI_MAP.get(ch, ch) for ch in text)
    text = re.sub(r"[^a-z0-9]+", " ", text.casefold())
    return re.sub(r"\s+", " ", text).strip()


def _same_owner(first: Any, second: Any) -> bool:
    """True when two recorded owner names plausibly refer to the same person."""
    key_a, key_b = _owner_key(first), _owner_key(second)
    if not key_a or not key_b:
        return False
    if key_a == key_b:
        return True
    return difflib.SequenceMatcher(None, key_a, key_b).ratio() >= OWNER_NAME_SIMILARITY


def _normalise_land(value: Any) -> str:
    return re.sub(r"\s+", " ", _text(value)).casefold()


def _land_survey(record_or_fields: Any) -> str:
    if isinstance(record_or_fields, dict) and "survey" in record_or_fields and "fields" not in record_or_fields:
        source, keys = record_or_fields, ("survey", "khasra")
    else:
        fields = record_or_fields
        source, keys = fields, ("survey_number", "gat_number", "khasra_number", "plot_number")
    for key in keys:
        value = _text(source.get(key))
        if value:
            return value
    return ""


def _land_village(record_or_fields: Any) -> str:
    if isinstance(record_or_fields, dict) and "village" in record_or_fields and "fields" not in record_or_fields:
        return _text(record_or_fields.get("village"))
    return _text(record_or_fields.get("village"))


def land_identity(survey: Any, village: Any) -> Tuple[str, str]:
    """Deterministic land key + id for a survey/village pair."""
    key = f"{_normalise_land(survey)}|{_normalise_land(village)}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    return key, f"LR-{digest}"


def land_id_for_fields(fields: Dict[str, Any]) -> Tuple[str, str, str]:
    survey = _land_survey(fields)
    village = _land_village(fields)
    key, land_id = land_identity(survey, village)
    return land_id, key, survey


# ---------------------------------------------------------------------------
# schema (additive, idempotent, SQLite + PostgreSQL compatible)
# ---------------------------------------------------------------------------

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS land_encumbrances (
        id TEXT PRIMARY KEY,
        property_id TEXT,
        survey_number TEXT NOT NULL DEFAULT '',
        khasra_number TEXT DEFAULT '',
        village TEXT DEFAULT '',
        tehsil TEXT DEFAULT '',
        district TEXT DEFAULT '',
        owner_name TEXT DEFAULT '',
        lender TEXT NOT NULL DEFAULT '',
        reference_no TEXT DEFAULT '',
        amount REAL,
        start_date TEXT DEFAULT '',
        release_date TEXT,
        status TEXT NOT NULL DEFAULT 'ACTIVE',
        evidence_doc_id TEXT,
        notes TEXT DEFAULT '',
        created_by TEXT DEFAULT '',
        created_at REAL NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_land_encumbrances_land ON land_encumbrances(survey_number, village)",
    "CREATE INDEX IF NOT EXISTS idx_land_encumbrances_property ON land_encumbrances(property_id)",
    "CREATE INDEX IF NOT EXISTS idx_land_encumbrances_status ON land_encumbrances(status)",
    """
    CREATE TABLE IF NOT EXISTS land_mutations (
        id TEXT PRIMARY KEY,
        mutation_no TEXT UNIQUE NOT NULL,
        property_id TEXT,
        survey_number TEXT DEFAULT '',
        khasra_number TEXT DEFAULT '',
        village TEXT DEFAULT '',
        tehsil TEXT DEFAULT '',
        district TEXT DEFAULT '',
        previous_owner TEXT DEFAULT '',
        new_owner TEXT DEFAULT '',
        reason_type TEXT DEFAULT 'SALE',
        deed_no TEXT DEFAULT '',
        deed_date TEXT DEFAULT '',
        documents TEXT NOT NULL DEFAULT '[]',
        document_checklist TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'RECEIVED',
        risk_status TEXT DEFAULT 'UNKNOWN',
        risk_payload TEXT NOT NULL DEFAULT '{}',
        encumbrance_status TEXT DEFAULT 'UNKNOWN',
        reviewer TEXT DEFAULT '',
        reviewer_notes TEXT DEFAULT '',
        decided_at REAL,
        created_by TEXT DEFAULT '',
        created_at REAL NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_land_mutations_land ON land_mutations(survey_number, village)",
    "CREATE INDEX IF NOT EXISTS idx_land_mutations_status ON land_mutations(status)",
    """
    CREATE TABLE IF NOT EXISTS land_mutation_events (
        id TEXT PRIMARY KEY,
        mutation_id TEXT NOT NULL,
        status TEXT NOT NULL,
        note TEXT DEFAULT '',
        actor TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_land_mutation_events_mutation ON land_mutation_events(mutation_id)",
    """
    CREATE TABLE IF NOT EXISTS land_reports (
        id TEXT PRIMARY KEY,
        reference_no TEXT UNIQUE NOT NULL,
        land_id TEXT NOT NULL,
        document_id TEXT,
        payload TEXT NOT NULL DEFAULT '{}',
        generated_by TEXT DEFAULT '',
        generated_at REAL NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_land_reports_land ON land_reports(land_id)",
)

_schema_ready = False


def ensure_land_tables() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with get_db() as db:
        for statement in _SCHEMA_STATEMENTS:
            try:
                db.execute(statement)
            except Exception as exc:  # pragma: no cover - defensive for partial PG setups
                print(f"[LAND SCHEMA WARNING] {exc}")
    _schema_ready = True


# ---------------------------------------------------------------------------
# land record registry (documents grouped by survey + village identity)
# ---------------------------------------------------------------------------

def _visible_records(user: Dict[str, Any]) -> List[Dict[str, Any]]:
    ensure_land_tables()
    return mapping._map_visible_records(user)


def _land_of_record(record: Dict[str, Any]) -> Tuple[str, str]:
    survey = _text(record.get("survey")) or _text(record.get("khasra")) or _text(record.get("plot"))
    village = _text(record.get("village"))
    return land_identity(survey, village)


def _group_land_records(records: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    lands: Dict[str, Dict[str, Any]] = {}
    for record in records:
        survey = _text(record.get("survey")) or _text(record.get("khasra")) or _text(record.get("plot"))
        village = _text(record.get("village"))
        if not survey and not village:
            continue  # nothing identifies a parcel; never invent one
        key, land_id = land_identity(survey, village)
        land = lands.setdefault(land_id, {
            "land_id": land_id,
            "land_key": key,
            "survey": survey,
            "khasra": _text(record.get("khasra")),
            "village": village,
            "tehsil": _text(record.get("tehsil")),
            "district": _text(record.get("district")),
            "state": _text(record.get("state")),
            "records": [],
        })
        land["records"].append(record)
        # enrich geography from whichever record carries it
        for target, source_key in (("khasra", "khasra"), ("tehsil", "tehsil"), ("district", "district"), ("state", "state"), ("village", "village")):
            if not land.get(target):
                land[target] = _text(record.get(source_key))
        if not land["survey"]:
            land["survey"] = survey
    for land in lands.values():
        ordered = sorted(
            land["records"],
            key=lambda item: ((_year_of(item.get("year")) is None), _year_of(item.get("year")) or 0, item.get("created_at") or 0),
        )
        land["records"] = ordered
        latest = ordered[-1] if ordered else None
        land["record_count"] = len(ordered)
        land["current_owner"] = _text(latest.get("owner")) if latest else ""
        land["father"] = _text(latest.get("father")) if latest else ""
        land["area"] = _text(latest.get("area")) if latest else ""
        land["latest_year"] = _year_of(latest.get("year")) if latest else None
        land["latest_record_id"] = _text(latest.get("id")) if latest else ""
        land["approved_count"] = sum(1 for item in ordered if str(item.get("status") or "").upper() in {"APPROVED", "VERIFIED", "AUTO_APPROVED"})
        land["reference_record_id"] = land["latest_record_id"] or (_text(ordered[0]["id"]) if ordered else "")
        land["label"] = " · ".join(part for part in (land["survey"], land["village"]) if part) or land["land_id"]
    return lands


def register_lands() -> Dict[str, Dict[str, Any]]:
    """Lands that exist only in the encumbrance/mutation registers (no visible
    document yet). They participate in the registry with zero documents."""
    lands: Dict[str, Dict[str, Any]] = {}
    with get_db() as db:
        rows = db.execute("SELECT survey_number, khasra_number, village, tehsil, district FROM land_encumbrances").fetchall()
        rows += db.execute("SELECT survey_number, khasra_number, village, tehsil, district FROM land_mutations").fetchall()
    for row in rows:
        survey = _text(row["survey_number"]) or _text(row["khasra_number"])
        village = _text(row["village"])
        if not survey and not village:
            continue
        key, land_id = land_identity(survey, village)
        if land_id in lands:
            continue
        lands[land_id] = {
            "land_id": land_id, "land_key": key, "survey": survey, "khasra": _text(row["khasra_number"]),
            "village": village, "tehsil": _text(row["tehsil"]), "district": _text(row["district"]),
            "state": "", "records": [], "record_count": 0, "current_owner": "", "father": "", "area": "",
            "latest_year": None, "latest_record_id": "", "approved_count": 0, "reference_record_id": "",
            "label": " · ".join(part for part in (survey, village) if part) or land_id,
        }
    return lands


def _land_records_index(user: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    lands = _group_land_records(_visible_records(user))
    for land_id, land in register_lands().items():
        lands.setdefault(land_id, land)
    return lands


def _get_land(user: Dict[str, Any], land_id: str) -> Optional[Dict[str, Any]]:
    lands = _land_records_index(user)
    land = lands.get(_text(land_id))
    if not land:
        return None
    # attach identity snapshot for registers (never trust the client id alone)
    key, canonical_id = land_identity(land.get("survey"), land.get("village"))
    land["land_id"] = canonical_id
    land["land_key"] = key
    return land


def _land_of_document_record(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Single-record land shell (used when only one document is at hand)."""
    survey = _text(record.get("survey")) or _text(record.get("khasra")) or _text(record.get("plot"))
    village = _text(record.get("village"))
    if not survey and not village:
        return None
    key, land_id = land_identity(survey, village)
    return _group_land_records([record])[land_id]


def _land_register_rows(land: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Encumbrance + mutation rows that apply to this land (survey-level
    register entries match any village on the survey, mirroring land_records)."""
    survey = _text(land.get("survey"))
    village = _text(land.get("village"))
    with get_db() as db:
        enc_rows = db.execute("SELECT * FROM land_encumbrances ORDER BY COALESCE(start_date,'') DESC, created_at DESC").fetchall()
        mut_rows = db.execute("SELECT * FROM land_mutations ORDER BY created_at DESC").fetchall()

    def enc_matches(row: Any) -> bool:
        row_survey = _normalise_land(row["survey_number"] or row["khasra_number"])
        row_village = _normalise_land(row["village"])
        land_survey = _normalise_land(survey)
        land_village = _normalise_land(village)
        survey_match = bool(row_survey) and row_survey == land_survey
        if not survey_match:
            return False
        if row_village and land_village and row_village != land_village:
            return False
        return True

    def mut_matches(row: Any) -> bool:
        return enc_matches(row)

    encumbrances = [_encumbrance_dict(row) for row in enc_rows if enc_matches(row)]
    mutations = [_mutation_dict(row) for row in mut_rows if mut_matches(row)]
    return encumbrances, mutations


# ---------------------------------------------------------------------------
# audit helper
# ---------------------------------------------------------------------------

def _audit(user: Dict[str, Any], action: str, detail: str, doc_id: Optional[str] = None) -> None:
    try:
        log_audit(user.get("full_name") or user.get("email") or "SYSTEM", action, detail, doc_id)
    except Exception as exc:  # pragma: no cover - audit must never break workflow
        print(f"[LAND AUDIT WARNING] {exc}")


# ---------------------------------------------------------------------------
# encumbrances
# ---------------------------------------------------------------------------

class EncumbranceCreate(BaseModel):
    survey_number: str = ""
    khasra_number: str = ""
    village: str = ""
    tehsil: str = ""
    district: str = ""
    owner_name: str = ""
    lender: str
    reference_no: str = ""
    amount: Optional[float] = None
    start_date: str = ""
    status: str = "ACTIVE"
    evidence_doc_id: Optional[str] = None
    notes: str = ""
    property_id: Optional[str] = None


class EncumbranceUpdate(BaseModel):
    lender: Optional[str] = None
    reference_no: Optional[str] = None
    amount: Optional[float] = None
    start_date: Optional[str] = None
    status: Optional[str] = None
    owner_name: Optional[str] = None
    notes: Optional[str] = None
    evidence_doc_id: Optional[str] = None


class EncumbranceRelease(BaseModel):
    release_date: str = ""
    notes: str = ""


def _encumbrance_dict(row: Any) -> Dict[str, Any]:
    data = dict(row)
    data["amount"] = float(data["amount"]) if data.get("amount") is not None else None
    data["is_active"] = _text(data.get("status")).upper() == "ACTIVE"
    return data


def _get_encumbrance(db: Any, encumbrance_id: str) -> Optional[Dict[str, Any]]:
    row = db.execute("SELECT * FROM land_encumbrances WHERE id=?", (_text(encumbrance_id),)).fetchone()
    return _encumbrance_dict(row) if row else None


def active_encumbrances_for_land(land: Dict[str, Any]) -> List[Dict[str, Any]]:
    encumbrances, _ = _land_register_rows(land)
    return [item for item in encumbrances if _text(item.get("status")).upper() == "ACTIVE"]


@encumbrance_router.get("")
def list_encumbrances(
    status: str = Query(""),
    q: str = Query(""),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: Dict[str, Any] = Depends(get_current_user),
):
    ensure_land_tables()
    with get_db() as db:
        rows = db.execute("SELECT * FROM land_encumbrances ORDER BY created_at DESC").fetchall()
    items = [_encumbrance_dict(row) for row in rows]
    needle = _text(q).casefold()
    if needle:
        items = [item for item in items if needle in " ".join(str(item.get(key) or "") for key in (
            "lender", "reference_no", "survey_number", "khasra_number", "village", "district", "owner_name", "status")).casefold()]
    wanted = _text(status).upper()
    if wanted:
        items = [item for item in items if _text(item.get("status")).upper() == wanted]
    total = len(items)
    return {"encumbrances": items[offset:offset + limit], "total": total, "limit": limit, "offset": offset}


@encumbrance_router.get("/{encumbrance_id}")
def get_encumbrance(encumbrance_id: str, user: Dict[str, Any] = Depends(get_current_user)):
    ensure_land_tables()
    with get_db() as db:
        item = _get_encumbrance(db, encumbrance_id)
    if not item:
        raise HTTPException(status_code=404, detail="Encumbrance not found.")
    return {"encumbrance": item}


@encumbrance_router.post("")
def create_encumbrance(req: EncumbranceCreate, user: Dict[str, Any] = Depends(require_roles(*REVIEWER_ROLES))):
    ensure_land_tables()
    survey = _text(req.survey_number) or _text(req.khasra_number)
    if not survey and not _text(req.village):
        raise HTTPException(status_code=422, detail="A survey/khasra number or village is required to identify the land.")
    status_value = _text(req.status).upper() or "ACTIVE"
    if status_value not in ENCUMBRANCE_STATUSES:
        raise HTTPException(status_code=422, detail=f"Invalid encumbrance status. Use one of {ENCUMBRANCE_STATUSES}.")
    if req.evidence_doc_id:
        _assert_evidence_document(req.evidence_doc_id, user)
    encumbrance_id = uuid.uuid4().hex[:12]
    now = _now()
    with get_db() as db:
        db.execute(
            """INSERT INTO land_encumbrances
               (id, property_id, survey_number, khasra_number, village, tehsil, district, owner_name,
                lender, reference_no, amount, start_date, release_date, status, evidence_doc_id, notes,
                created_by, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (encumbrance_id, _text(req.property_id) or None, survey, _text(req.khasra_number), _text(req.village),
             _text(req.tehsil), _text(req.district), _text(req.owner_name), _text(req.lender),
             _text(req.reference_no), req.amount, _text(req.start_date),
             None if status_value != "RELEASED" else _text(req.start_date),
             status_value, _text(req.evidence_doc_id) or None, _text(req.notes),
             user.get("email") or user.get("full_name") or "", now, now),
        )
    _audit(user, "ENCUMBRANCE_CREATED",
           f"Encumbrance {encumbrance_id}: {_text(req.lender)} ref {_text(req.reference_no) or '—'} on survey {survey} ({_text(req.village)}) status {status_value}")
    with get_db() as db:
        item = _get_encumbrance(db, encumbrance_id)
    return {"encumbrance": item}


@encumbrance_router.put("/{encumbrance_id}")
def update_encumbrance(encumbrance_id: str, req: EncumbranceUpdate, user: Dict[str, Any] = Depends(require_roles(*REVIEWER_ROLES))):
    ensure_land_tables()
    with get_db() as db:
        item = _get_encumbrance(db, encumbrance_id)
        if not item:
            raise HTTPException(status_code=404, detail="Encumbrance not found.")
        changes: Dict[str, Any] = {}
        for field in ("lender", "reference_no", "owner_name", "notes", "evidence_doc_id"):
            value = getattr(req, field)
            if value is not None:
                changes[field] = _text(value) or None if field == "evidence_doc_id" else _text(value)
        if req.amount is not None:
            changes["amount"] = req.amount
        if req.start_date is not None:
            changes["start_date"] = _text(req.start_date)
        if req.status is not None:
            new_status = _text(req.status).upper()
            if new_status not in ENCUMBRANCE_STATUSES:
                raise HTTPException(status_code=422, detail=f"Invalid encumbrance status. Use one of {ENCUMBRANCE_STATUSES}.")
            if new_status == "RELEASED":
                raise HTTPException(status_code=400, detail="Use the dedicated release action to release an encumbrance.")
            changes["status"] = new_status
        if not changes:
            raise HTTPException(status_code=400, detail="No changes supplied.")
        if changes.get("evidence_doc_id"):
            _assert_evidence_document(changes["evidence_doc_id"], user)
        assignments = ", ".join(f"{column}=?" for column in changes)
        db.execute(f"UPDATE land_encumbrances SET {assignments}, updated_at=? WHERE id=?",
                   (*changes.values(), _now(), _text(encumbrance_id)))
    _audit(user, "ENCUMBRANCE_UPDATED",
           f"Encumbrance {encumbrance_id} updated: {', '.join(sorted(changes.keys()))}")
    with get_db() as db:
        return {"encumbrance": _get_encumbrance(db, encumbrance_id)}


@encumbrance_router.post("/{encumbrance_id}/release")
def release_encumbrance(encumbrance_id: str, req: EncumbranceRelease, user: Dict[str, Any] = Depends(require_roles(*REVIEWER_ROLES))):
    ensure_land_tables()
    with get_db() as db:
        item = _get_encumbrance(db, encumbrance_id)
        if not item:
            raise HTTPException(status_code=404, detail="Encumbrance not found.")
        if _text(item.get("status")).upper() != "ACTIVE":
            raise HTTPException(status_code=400, detail="Only an ACTIVE encumbrance can be released.")
        release_date = _text(req.release_date)
        notes = _text(req.notes)
        db.execute(
            "UPDATE land_encumbrances SET status='RELEASED', release_date=?, notes=COALESCE(NULLIF(?,''), notes), updated_at=? WHERE id=?",
            (release_date or None, notes, _now(), _text(encumbrance_id)),
        )
    _audit(user, "ENCUMBRANCE_RELEASED",
           f"Encumbrance {encumbrance_id} ({item.get('lender')} ref {item.get('reference_no')}) released on {release_date or 'unspecified date'}")
    with get_db() as db:
        return {"encumbrance": _get_encumbrance(db, encumbrance_id)}


def _assert_evidence_document(document_id: str, user: Dict[str, Any]) -> None:
    row = None
    with get_db() as db:
        row = db.execute("SELECT id, status, uploaded_by FROM documents WHERE id=?", (_text(document_id),)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Evidence document {document_id} not found.")
    role = user.get("role")
    if role == ROLE_VIEWER and _text(row["status"]).upper() not in {"APPROVED", "VERIFIED", "AUTO_APPROVED"}:
        raise HTTPException(status_code=403, detail="Viewer access is limited to approved evidence documents.")
    if role == ROLE_DATA_OFFICER and row["uploaded_by"] != user.get("email"):
        raise HTTPException(status_code=403, detail="Data Officers can only reference their own documents as evidence.")


# ---------------------------------------------------------------------------
# mutations
# ---------------------------------------------------------------------------

class MutationCreate(BaseModel):
    survey_number: str = ""
    khasra_number: str = ""
    village: str = ""
    tehsil: str = ""
    district: str = ""
    previous_owner: str = ""
    new_owner: str = ""
    reason_type: str = "SALE"
    deed_no: str = ""
    deed_date: str = ""
    documents: List[str] = []
    document_checklist: List[Dict[str, Any]] = []
    property_id: Optional[str] = None


class MutationReviewRequest(BaseModel):
    action: str
    notes: str = ""


class MutationCompleteRequest(BaseModel):
    notes: str = ""
    confirm_active_encumbrance: bool = False


def _mutation_dict(row: Any) -> Dict[str, Any]:
    data = dict(row)
    data["documents"] = _parse_json(data.get("documents"), [])
    data["document_checklist"] = _parse_json(data.get("document_checklist"), [])
    data["risk_payload"] = _parse_json(data.get("risk_payload"), {})
    data["risk_payload"].pop("records", None)  # keep queue payloads light
    return data


def _get_mutation(db: Any, mutation_id: str) -> Optional[Dict[str, Any]]:
    row = db.execute("SELECT * FROM land_mutations WHERE id=?", (_text(mutation_id),)).fetchone()
    return _mutation_dict(row) if row else None


def _next_mutation_no(db: Any) -> str:
    year = time.strftime("%Y")
    prefix = f"M-{year}-"
    rows = db.execute("SELECT mutation_no FROM land_mutations WHERE mutation_no LIKE ? ORDER BY mutation_no DESC LIMIT 1",
                      (prefix + "%",)).fetchall()
    highest = 0
    for row in rows:
        match = re.search(r"(\d+)$", _text(row["mutation_no"]))
        if match:
            highest = max(highest, int(match.group(1)))
    return f"{prefix}{highest + 1:04d}"


def _mutation_events(db: Any, mutation_id: str) -> List[Dict[str, Any]]:
    rows = db.execute("SELECT * FROM land_mutation_events WHERE mutation_id=? ORDER BY created_at ASC, id ASC",
                      (_text(mutation_id),)).fetchall()
    return [dict(row) for row in rows]


def _land_for_register_fields(survey: str, khasra: str, village: str) -> Optional[Dict[str, Any]]:
    effective_survey = _text(survey) or _text(khasra)
    key, land_id = land_identity(effective_survey, village)
    return {"land_id": land_id, "land_key": key, "survey": effective_survey, "khasra": _text(khasra), "village": _text(village)}


def refresh_mutation_intelligence(mutation: Dict[str, Any]) -> Dict[str, Any]:
    """Recompute the deterministic risk/encumbrance snapshot stored on the
    mutation row. Called on create, review transitions, and reads of detail."""
    land = _land_for_register_fields(mutation.get("survey_number"), mutation.get("khasra_number"), mutation.get("village"))
    if not land:
        return mutation
    try:
        risk = compute_land_risk(land)
    except Exception:
        risk = {"verdict": "UNKNOWN", "flags": []}
    encumbrances, _ = _land_register_rows(land)
    has_active = any(_text(item.get("status")).upper() == "ACTIVE" for item in encumbrances)
    encumbrance_status = "ACTIVE" if has_active else ("CLEAR" if encumbrances else "UNKNOWN")
    verdict = risk.get("verdict") or "UNKNOWN"
    with get_db() as db:
        db.execute(
            "UPDATE land_mutations SET risk_status=?, risk_payload=?, encumbrance_status=?, updated_at=? WHERE id=?",
            (verdict, _json(risk), encumbrance_status, _now(), mutation["id"]),
        )
    mutation = dict(mutation)
    mutation["risk_status"] = verdict
    mutation["risk_payload"] = risk
    mutation["encumbrance_status"] = encumbrance_status
    return mutation


def _append_mutation_event(db: Any, mutation_id: str, status_value: str, actor: str, note: str = "") -> None:
    db.execute(
        "INSERT INTO land_mutation_events (id, mutation_id, status, note, actor, created_at) VALUES (?,?,?,?,?,?)",
        (uuid.uuid4().hex[:12], _text(mutation_id), status_value, _text(note), actor or "SYSTEM", _now()),
    )


def _assert_mutation_documents(document_ids: Sequence[str], user: Dict[str, Any]) -> List[Dict[str, Any]]:
    checklist: List[Dict[str, Any]] = []
    with get_db() as db:
        for document_id in document_ids:
            row = db.execute("SELECT id, filename, doc_type, status, uploaded_by FROM documents WHERE id=?", (_text(document_id),)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail=f"Document {document_id} not found.")
            role = user.get("role")
            if role == ROLE_VIEWER and _text(row["status"]).upper() not in {"APPROVED", "VERIFIED", "AUTO_APPROVED"}:
                raise HTTPException(status_code=403, detail="Viewer access is limited to approved documents.")
            if role == ROLE_DATA_OFFICER and row["uploaded_by"] != user.get("email"):
                raise HTTPException(status_code=403, detail="Data Officers can only attach their own documents.")
            checklist.append({"document_id": row["id"], "filename": row["filename"], "doc_type": row["doc_type"], "status": row["status"]})
    return checklist


@mutation_router.post("")
def create_mutation(req: MutationCreate, user: Dict[str, Any] = Depends(require_roles(*STAFF_ROLES))):
    ensure_land_tables()
    survey = _text(req.survey_number) or _text(req.khasra_number)
    if not survey and not _text(req.village):
        raise HTTPException(status_code=422, detail="A survey/khasra number or village is required.")
    reason_type = _text(req.reason_type).upper() or "SALE"
    if reason_type not in TRANSFER_MUTATION_TYPES:
        raise HTTPException(status_code=422, detail=f"Invalid mutation type. Use one of {TRANSFER_MUTATION_TYPES}.")
    if not _text(req.new_owner):
        raise HTTPException(status_code=422, detail="The proposed new owner is required.")
    checklist = _assert_mutation_documents(req.documents, user) if req.documents else []
    mutation_id = uuid.uuid4().hex[:12]
    now = _now()
    with get_db() as db:
        mutation_no = _next_mutation_no(db)
        db.execute(
            """INSERT INTO land_mutations
               (id, mutation_no, property_id, survey_number, khasra_number, village, tehsil, district,
                previous_owner, new_owner, reason_type, deed_no, deed_date, documents, document_checklist,
                status, risk_status, risk_payload, encumbrance_status, reviewer, reviewer_notes, decided_at,
                created_by, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'RECEIVED','UNKNOWN','{}','UNKNOWN','','',NULL,?,?,?)""",
            (mutation_id, mutation_no, _text(req.property_id) or None, survey, _text(req.khasra_number),
             _text(req.village), _text(req.tehsil), _text(req.district), _text(req.previous_owner),
             _text(req.new_owner), reason_type, _text(req.deed_no), _text(req.deed_date),
             _json([_text(doc) for doc in req.documents]), _json(checklist),
             user.get("email") or user.get("full_name") or "", now, now),
        )
        _append_mutation_event(db, mutation_id, "RECEIVED", user.get("email") or "", "Mutation application received")
    _audit(user, "MUTATION_CREATED",
           f"Mutation {mutation_no} ({reason_type}) on survey {survey} ({_text(req.village)}): {_text(req.previous_owner) or '—'} -> {_text(req.new_owner)}")
    with get_db() as db:
        mutation = _get_mutation(db, mutation_id)
    mutation = refresh_mutation_intelligence(mutation)
    return {"mutation": mutation}


@mutation_router.get("")
def list_mutations(
    status: str = Query(""),
    q: str = Query(""),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: Dict[str, Any] = Depends(require_roles(*STAFF_ROLES)),
):
    ensure_land_tables()
    with get_db() as db:
        rows = db.execute("SELECT * FROM land_mutations ORDER BY created_at DESC").fetchall()
    items = [_mutation_dict(row) for row in rows]
    needle = _text(q).casefold()
    if needle:
        items = [item for item in items if needle in " ".join(str(item.get(key) or "") for key in (
            "mutation_no", "survey_number", "khasra_number", "village", "district", "previous_owner",
            "new_owner", "status", "reason_type")).casefold()]
    wanted = _text(status).upper()
    if wanted:
        items = [item for item in items if _text(item.get("status")).upper() == wanted]
    total = len(items)
    return {"mutations": items[offset:offset + limit], "total": total, "limit": limit, "offset": offset,
            "counts": _mutation_counts(items)}


def _mutation_counts(items: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    counts = {status_value: 0 for status_value in MUTATION_STATUSES}
    for item in items:
        key = _text(item.get("status")).upper()
        counts[key] = counts.get(key, 0) + 1
    return counts


@mutation_router.get("/{mutation_id}")
def get_mutation(mutation_id: str, user: Dict[str, Any] = Depends(require_roles(*STAFF_ROLES))):
    ensure_land_tables()
    with get_db() as db:
        mutation = _get_mutation(db, mutation_id)
        if not mutation:
            raise HTTPException(status_code=404, detail="Mutation not found.")
        events = _mutation_events(db, mutation_id)
    if _text(mutation.get("status")).upper() not in {"COMPLETED", "REJECTED"}:
        mutation = refresh_mutation_intelligence(mutation)
    return {"mutation": mutation, "events": events}


def _transition_mutation(mutation: Dict[str, Any], new_status: str, user: Dict[str, Any], notes: str) -> Dict[str, Any]:
    with get_db() as db:
        db.execute(
            "UPDATE land_mutations SET status=?, reviewer=?, reviewer_notes=CASE WHEN ?<>'' THEN ? ELSE reviewer_notes END, updated_at=? WHERE id=?",
            (new_status, user.get("email") or "", _text(notes), _text(notes), _now(), mutation["id"]),
        )
        _append_mutation_event(db, mutation["id"], new_status, user.get("email") or "", notes)
    _audit(user, "MUTATION_STATUS_CHANGED",
           f"Mutation {mutation.get('mutation_no')} moved {mutation.get('status')} -> {new_status}" + (f": {_text(notes)}" if _text(notes) else ""))
    with get_db() as db:
        return _get_mutation(db, mutation["id"])


@mutation_router.post("/{mutation_id}/review")
def review_mutation(mutation_id: str, req: MutationReviewRequest, user: Dict[str, Any] = Depends(require_roles(*REVIEWER_ROLES))):
    ensure_land_tables()
    action = _text(req.action).upper()
    transitions = {
        "START_REVIEW": "UNDER_REVIEW",
        "UNDER_REVIEW": "UNDER_REVIEW",
        "MARK_VERIFIED": "VERIFIED",
        "VERIFY": "VERIFIED",
        "REJECT": "REJECTED",
    }
    if action not in transitions:
        raise HTTPException(status_code=400, detail=f"Invalid review action. Use one of {sorted(transitions)}.")
    new_status = transitions[action]
    if action == "REJECT" and not _text(req.notes):
        raise HTTPException(status_code=400, detail="Comments are required when rejecting a mutation application.")
    with get_db() as db:
        mutation = _get_mutation(db, mutation_id)
    if not mutation:
        raise HTTPException(status_code=404, detail="Mutation not found.")
    current = _text(mutation.get("status")).upper()
    if current in {"COMPLETED", "REJECTED"}:
        raise HTTPException(status_code=400, detail=f"A {current} mutation can no longer be reviewed.")
    mutation = _transition_mutation(mutation, new_status, user, req.notes)
    mutation = refresh_mutation_intelligence(mutation)
    return {"mutation": mutation, "events": _mutation_events_db(mutation_id)}


def _mutation_events_db(mutation_id: str) -> List[Dict[str, Any]]:
    with get_db() as db:
        return _mutation_events(db, mutation_id)


def _encumbrance_gate_payload(mutation: Dict[str, Any]) -> Dict[str, Any]:
    land = _land_for_register_fields(mutation.get("survey_number"), mutation.get("khasra_number"), mutation.get("village"))
    encumbrances, _ = _land_register_rows(land) if land else ([], [])
    active = [item for item in encumbrances if _text(item.get("status")).upper() == "ACTIVE"]
    return {
        "code": "ACTIVE_ENCUMBRANCE",
        "message": "This land record currently has an active encumbrance. Review is required before completing the mutation.",
        "encumbrances": active,
    }


@mutation_router.post("/{mutation_id}/complete")
def complete_mutation(mutation_id: str, req: MutationCompleteRequest, user: Dict[str, Any] = Depends(require_roles(ROLE_ADMIN))):
    """Approve and complete a mutation. Human, administrator-governed action.

    SAFETY GATE: an active encumbrance blocks silent completion. The gate is
    only passed by an explicit confirmation flag sent by a human operator who
    has seen the lender/reference/amount evidence. RBAC is never bypassed and
    every attempt (passed or blocked) is audited."""
    ensure_land_tables()
    with get_db() as db:
        mutation = _get_mutation(db, mutation_id)
    if not mutation:
        raise HTTPException(status_code=404, detail="Mutation not found.")
    current = _text(mutation.get("status")).upper()
    if current in {"COMPLETED", "REJECTED"}:
        raise HTTPException(status_code=400, detail=f"Mutation is already {current}.")
    if current not in {"VERIFIED", "UNDER_REVIEW"}:
        raise HTTPException(status_code=400, detail="A mutation must be under review or verified before completion.")

    land = _land_for_register_fields(mutation.get("survey_number"), mutation.get("khasra_number"), mutation.get("village"))
    active = active_encumbrances_for_land(land) if land else []
    if active and not req.confirm_active_encumbrance:
        _audit(user, "MUTATION_SAFETY_GATE_BLOCKED",
               f"Completion of mutation {mutation.get('mutation_no')} blocked by an active encumbrance gate")
        raise HTTPException(status_code=409, detail=_encumbrance_gate_payload(mutation))

    with get_db() as db:
        db.execute(
            "UPDATE land_mutations SET status='COMPLETED', reviewer=?, reviewer_notes=CASE WHEN ?<>'' THEN ? ELSE reviewer_notes END, decided_at=?, updated_at=? WHERE id=?",
            (user.get("email") or "", _text(req.notes), _text(req.notes), _now(), _now(), mutation["id"]),
        )
        _append_mutation_event(db, mutation["id"], "COMPLETED", user.get("email") or "",
                               req.notes or ("Completed with explicit encumbrance confirmation" if active else "Completed"))
        # Register the ownership event on the property timeline (never rewrite OCR fields).
        property_id = _text(mutation.get("property_id"))
        if property_id:
            try:
                with get_db() as db2:
                    db2.execute(
                        "INSERT INTO property_timeline (id, property_id, event_type, description, source, created_at) VALUES (?,?,?,?,?,?)",
                        (uuid.uuid4().hex, property_id, "MUTATION_COMPLETED",
                         f"Mutation {mutation.get('mutation_no')} completed: {_text(mutation.get('previous_owner')) or '—'} -> {_text(mutation.get('new_owner'))}. Human-approved register event; document fields were not altered.",
                         "Mutation register", _now()),
                    )
            except Exception as exc:  # pragma: no cover
                print(f"[MUTATION TIMELINE WARNING] {exc}")
    _audit(user, "MUTATION_COMPLETED",
           f"Mutation {mutation.get('mutation_no')} completed: {_text(mutation.get('previous_owner')) or '—'} -> {_text(mutation.get('new_owner'))}"
           + (" with explicit active-encumbrance confirmation" if active else ""))
    with get_db() as db:
        completed = _get_mutation(db, mutation_id)
    completed = refresh_mutation_intelligence(completed)
    return {"mutation": completed, "events": _mutation_events_db(mutation_id)}


@mutation_router.get("/{mutation_id}/events")
def mutation_event_log(mutation_id: str, user: Dict[str, Any] = Depends(require_roles(*STAFF_ROLES))):
    return {"events": _mutation_events_db(mutation_id)}


# ---------------------------------------------------------------------------
# deterministic land risk engine
# ---------------------------------------------------------------------------

def _risk_flag(code: str, severity: str, title: str, detail: str,
               evidence: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    return {
        "code": code,
        "severity": severity,  # HIGH | REVIEW | INFO
        "title": title,
        "detail": detail,
        "evidence": evidence or [],
    }


def _doc_evidence(record: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "document",
        "ref": record.get("id"),
        "label": f"{record.get('filename') or record.get('id')} ({_text(record.get('owner')) or 'owner not extracted'}, {_year_of(record.get('year')) or 'year?'})",
    }


def _enc_evidence(item: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "encumbrance", "ref": item.get("id"),
            "label": f"{item.get('lender')} ref {item.get('reference_no') or '—'} ({_text(item.get('status'))})"}


def _mut_evidence(item: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "mutation", "ref": item.get("id"),
            "label": f"{item.get('mutation_no')} ({_text(item.get('status'))})"}


def _risk_records(land: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Document rows in risk-record shape (all documents of the land, any
    status — rejected/conflicting copies are themselves risk evidence)."""
    shaped: List[Dict[str, Any]] = []
    for record in land.get("records") or []:
        fields = record.get("fields") if isinstance(record.get("fields"), dict) else {}
        shaped.append({
            "id": record.get("id"),
            "filename": record.get("filename"),
            "status": record.get("status"),
            "doc_type": record.get("doc_type"),
            "year": record.get("year"),
            "owner": record.get("owner"),
            "area": record.get("area"),
            "mean_conf": record.get("mean_conf"),
            "fields": fields,
            "created_at": record.get("created_at"),
        })
    return shaped



def compute_land_risk(land: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic, rule-based land risk assessment.

    The engine answers: does local evidence (documents, the encumbrance
    register and the mutation register) contain conditions a human verifier
    must review? It is a workflow signal only — never a legal determination.
    """
    ensure_land_tables()
    encumbrances, mutations = _land_register_rows(land)
    records = _risk_records(land)
    flags: List[Dict[str, Any]] = []

    def add(code: str, severity: str, title: str, detail: str, evidence: Optional[List[Dict[str, Any]]] = None) -> None:
        flags.append(_risk_flag(code, severity, title, detail, evidence))

    by_year: Dict[int, List[Dict[str, Any]]] = {}
    for record in records:
        year = _year_of(record.get("year")) or _year_of((record.get("created_at") or ""))
        if year:
            by_year.setdefault(year, []).append(record)
    years = sorted(by_year)

    # ---- 1. active encumbrance -------------------------------------------
    active_encumbrances = [item for item in encumbrances if _text(item.get("status")).upper() == "ACTIVE"]
    for item in active_encumbrances:
        amount = item.get("amount")
        add("ACTIVE_ENCUMBRANCE", "HIGH",
            "Active bank encumbrance on this land",
            f"{item.get('lender') or 'A lender'} holds an active encumbrance"
            + (f" of ₹{float(amount):,.0f}" if amount else "")
            + f" (ref {item.get('reference_no') or 'no reference'}, started {item.get('start_date') or 'unknown date'}). "
              "A live loan must be released (bank NOC / settlement recorded) before the land can be treated as encumbrance-free or safely transferred.",
            [_enc_evidence(item)] + ([{"type": "document", "ref": item.get("evidence_doc_id"), "label": f"Evidence document {item.get('evidence_doc_id')}"}] if item.get("evidence_doc_id") else []))

    # ---- 2. transfer while encumbrance active ----------------------------
    for mutation in mutations:
        deed_date = _text(mutation.get("deed_date"))
        if not deed_date:
            continue
        for item in encumbrances:
            start = _text(item.get("start_date"))
            release = _text(item.get("release_date"))
            if start and deed_date >= start and (not release or deed_date <= release):
                add("SALE_DURING_ENCUMBRANCE", "HIGH",
                    "Transfer recorded while an encumbrance was live",
                    f"Mutation {mutation.get('mutation_no')} (deed dated {deed_date}) transferred this land while "
                    f"{item.get('lender') or 'the lender'}'s encumbrance (from {start}) was in force. The lender's consent/release must be verified.",
                    [_mut_evidence(mutation), _enc_evidence(item)])
                break

    # ---- 3. owner conflict within one year -------------------------------
    for year in years:
        live = [record for record in by_year[year] if _text(record.get("status")).upper() != "REJECTED"]
        distinct: List[Tuple[str, List[Dict[str, Any]]]] = []
        for record in live:
            owner = _text(record.get("owner"))
            if not owner:
                continue
            for group in distinct:
                if _same_owner(group[0], owner):
                    group[1].append(record)
                    break
            else:
                distinct.append((owner, [record]))
        if len(distinct) > 1:
            names = ", ".join(sorted({group[0] for group in distinct}))
            evidence = [_doc_evidence(record) for group in distinct for record in group[1]]
            add("OWNER_CONFLICT_YEAR", "HIGH",
                f"Conflicting owners recorded for {year}",
                f"Two or more live records for this land in {year} name different owners ({names}). Both cannot be correct; "
                "verify against the original record and reject the erroneous copy.",
                evidence)

    # ---- 4. owner change without a mutation ------------------------------
    def _latest_live_owner(year: int) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        for record in reversed(by_year.get(year) or []):
            if _text(record.get("status")).upper() != "REJECTED" and _text(record.get("owner")):
                return _text(record.get("owner")), record
        return None, None

    for previous_year, next_year in zip(years, years[1:]):
        previous_owner, previous_row = _latest_live_owner(previous_year)
        next_owner, next_row = _latest_live_owner(next_year)
        if not previous_owner or not next_owner or _same_owner(previous_owner, next_owner):
            continue
        bridged = False
        pending = False
        for mutation in mutations:
            if _same_owner(mutation.get("new_owner"), next_owner) and _same_owner(mutation.get("previous_owner"), previous_owner):
                status_value = _text(mutation.get("status")).upper()
                if status_value in {"COMPLETED", "VERIFIED", "UNDER_REVIEW", "RECEIVED"}:
                    bridged = True
                if status_value in {"RECEIVED", "UNDER_REVIEW"}:
                    pending = True
                break
        if bridged:
            if pending:
                add("PENDING_MUTATION", "INFO",
                    f"Ownership change ({previous_year} → {next_year}) has a pending mutation",
                    f"The recorded owner changed from {previous_owner} ({previous_year}) to {next_owner} ({next_year}); "
                    f"a mutation application is on file but not yet completed. Complete human review of the mutation.",
                    [_mut_evidence(mutation) for mutation in mutations if _same_owner(mutation.get("new_owner"), next_owner)])
            continue
        evidence = [_doc_evidence(next_row or {})]
        rejected = [mutation for mutation in mutations if _text(mutation.get("status")).upper() == "REJECTED"
                    and (_same_owner(mutation.get("new_owner"), next_owner) or _same_owner(mutation.get("previous_owner"), previous_owner))]
        if rejected:
            add("REJECTED_MUTATION", "INFO",
                "Ownership change covered only by a rejected mutation",
                f"The owner changed from {previous_owner} ({previous_year}) to {next_owner} ({next_year}); the related mutation "
                f"application {rejected[0].get('mutation_no')} was REJECTED. No valid mutation explains the change.",
                [_mut_evidence(mutation) for mutation in rejected] + evidence)
        add("OWNER_CHANGE_NO_MUTATION", "REVIEW",
            "Owner changed without a corresponding mutation",
            f"The recorded owner changed from {previous_owner} ({previous_year}) to {next_owner} ({next_year}) but no valid "
            "mutation (namantaran) on file explains the change. A sale/gift/inheritance record should exist — request or verify the deed.",
            evidence)

    # ---- 5. unusual area change ------------------------------------------
    for previous_year, next_year in zip(years, years[1:]):
        area_a = _number((by_year[previous_year][-1] if by_year[previous_year] else {}).get("area"))
        area_b = _number((by_year[next_year][-1] if by_year[next_year] else {}).get("area"))
        if area_a and area_b and area_a > 0 and abs(area_b - area_a) / area_a > AREA_JUMP_RATIO:
            bridged = any(_text(mutation.get("reason_type")).upper() in {"PARTITION", "MERGER"} for mutation in mutations)
            if not bridged:
                percent = round(100 * abs(area_b - area_a) / area_a)
                add("AREA_JUMP", "REVIEW",
                    f"Area differs by {percent}% from the previous record",
                    f"Recorded area moved from {by_year[previous_year][-1].get('area')} ({previous_year}) to "
                    f"{by_year[next_year][-1].get('area')} ({next_year}) — a change of {percent}% with no partition/merger "
                    "mutation on file. Verify against the survey map.",
                    [_doc_evidence(by_year[previous_year][-1]), _doc_evidence(by_year[next_year][-1])])

    # ---- 6. conflicting duplicate documents ------------------------------
    for year in years:
        live = [record for record in by_year[year] if _text(record.get("status")).upper() != "REJECTED"]
        for index, left in enumerate(live):
            for right in live[index + 1:]:
                if not _same_owner(left.get("owner"), right.get("owner")):
                    continue
                area_a, area_b = _number(left.get("area")), _number(right.get("area"))
                if area_a and area_b and area_a > 0 and abs(area_b - area_a) / area_a > AREA_JUMP_RATIO:
                    add("DUPLICATE_CONFLICT", "REVIEW",
                        f"Conflicting duplicate records for {year}",
                        f"Two live records of the same year and owner report materially different areas "
                        f"({left.get('area')} vs {right.get('area')}). One copy is likely erroneous or forged; verify the original.",
                        [_doc_evidence(left), _doc_evidence(right)])

    # ---- 7. rejected / conflicting historical documents ------------------
    for year in years:
        live = [record for record in by_year[year] if _text(record.get("status")).upper() != "REJECTED"]
        rejected = [record for record in by_year[year] if _text(record.get("status")).upper() == "REJECTED"]
        if live and rejected:
            distinct_owners = {_owner_key(record.get("owner")) for record in live + rejected if record.get("owner")}
            if len(distinct_owners) > 1:
                add("REJECTED_CONFLICT_COPY", "INFO",
                    f"Ownership dispute documented by a rejected copy ({year})",
                    "A conflicting record for this year was REJECTED with reviewer notes. The dispute is on record for "
                    "transparency; the live record currently stands.",
                    [_doc_evidence(record) for record in rejected])

    # ---- 8. missing ownership-chain years --------------------------------
    for previous_year, next_year in zip(years, years[1:]):
        if next_year - previous_year > CHAIN_GAP_YEARS:
            add("CHAIN_GAP", "INFO",
                f"Gap of {next_year - previous_year} years in the record chain",
                f"No records between {previous_year} and {next_year} in this passbook. Intermediate khatauni years may "
                "never have been scanned — worth knowing before a long-period title check.",
                [])

    # ---- 9. suspicious mutation sequence ---------------------------------
    completed = [mutation for mutation in mutations if _text(mutation.get("status")).upper() == "COMPLETED"]
    # 9a. rapid flipping of owners (A -> B then B -> A)
    for left in completed:
        for right in completed:
            if left["id"] >= right["id"]:
                continue
            if (_same_owner(left.get("new_owner"), right.get("previous_owner"))
                    and _same_owner(left.get("previous_owner"), right.get("new_owner"))):
                add("SUSPICIOUS_MUTATION_SEQUENCE", "HIGH",
                    "Ownership flipped back between the same parties",
                    f"Mutations {left.get('mutation_no')} and {right.get('mutation_no')} transfer ownership back and forth "
                    "between the same two parties. Such circular transfers are a known pattern in fraudulent transactions; "
                    "human verification of both deeds is required.",
                    [_mut_evidence(left), _mut_evidence(right)])
    # 9b. many completions in a short window
    now_seconds = _now()
    recent = []
    for mutation in completed:
        decided_at = mutation.get("decided_at") or mutation.get("updated_at") or mutation.get("created_at")
        if decided_at and now_seconds - float(decided_at) <= SUSPICIOUS_MUTATION_WINDOW_DAYS * 86400:
            recent.append(mutation)
    if len(recent) >= SUSPICIOUS_MUTATION_COUNT:
        add("SUSPICIOUS_MUTATION_SEQUENCE", "REVIEW",
            f"{len(recent)} mutations completed within {SUSPICIOUS_MUTATION_WINDOW_DAYS} days",
            "An unusually high number of ownership mutations were completed in a short window. Verify the underlying "
            "deeds and confirm each transfer with the parties.",
            [_mut_evidence(mutation) for mutation in recent])

    # ---- 10. low-quality extraction on the latest record ------------------
    latest = records[-1] if records else None
    if latest and _text(latest.get("status")).upper() not in {"REJECTED"}:
        confidences = [
            entry.get("confidence")
            for entry in (latest.get("fields") or {}).values()
            if isinstance(entry, dict) and isinstance(entry.get("confidence"), (int, float))
        ]
        mean_conf = latest.get("mean_conf")
        quality = (sum(confidences) / len(confidences)) if confidences else (float(mean_conf) / 100.0 if mean_conf else None)
        if quality is not None and quality < 0.6:
            add("LOW_QUALITY_EXTRACTION", "INFO",
                "Low-quality OCR extraction on the latest record",
                "The most recent record's extracted fields carry low OCR confidence. Re-verify against the scan before "
                "relying on this passbook entry.",
                [_doc_evidence(latest)])

    severity_order = {"HIGH": 0, "REVIEW": 1, "INFO": 2}
    flags.sort(key=lambda flag: (severity_order.get(flag["severity"], 3), flag["code"]))
    has_high = any(flag["severity"] == "HIGH" for flag in flags)
    has_review = any(flag["severity"] == "REVIEW" for flag in flags)
    verdict = "HIGH_RISK" if has_high else ("REVIEW" if has_review else "CLEAR")
    evidence_documents: List[str] = []
    for flag in flags:
        for item in flag["evidence"]:
            ref = item.get("ref")
            if item.get("type") == "document" and ref and ref not in evidence_documents:
                evidence_documents.append(str(ref))
    return {
        "land_id": land.get("land_id"),
        "survey": _text(land.get("survey")),
        "village": _text(land.get("village")),
        "verdict": verdict,
        "highest_severity": "HIGH" if has_high else ("REVIEW" if has_review else "NONE"),
        "counts": {
            "high": sum(1 for flag in flags if flag["severity"] == "HIGH"),
            "review": sum(1 for flag in flags if flag["severity"] == "REVIEW"),
            "info": sum(1 for flag in flags if flag["severity"] == "INFO"),
        },
        "flags": flags,
        "evidence_documents": evidence_documents,
        "inputs": {
            "records": len(records),
            "encumbrances": len(encumbrances),
            "active_encumbrances": len(active_encumbrances),
            "mutations": len(mutations),
        },
        "legal_authority": False,
        "disclaimer": "Deterministic review signals computed from local records. This is a workflow risk indicator, "
                      "not a legally authoritative fraud determination or title opinion.",
    }


# ---------------------------------------------------------------------------
# land-record APIs
# ---------------------------------------------------------------------------

def _register_states(land: Dict[str, Any]) -> Dict[str, Any]:
    encumbrances, mutations = _land_register_rows(land)
    active = [item for item in encumbrances if _text(item.get("status")).upper() == "ACTIVE"]
    return {
        "encumbrances": encumbrances,
        "active_encumbrances": active,
        "encumbrance_status": "ACTIVE" if active else ("CLEAR" if encumbrances else "NONE"),
        "mutations": mutations,
        "pending_mutations": [item for item in mutations if _text(item.get("status")).upper() in {"RECEIVED", "UNDER_REVIEW", "VERIFIED"}],
        "encumbrance_banner": _encumbrance_banner({"active_encumbrances": active, "encumbrances": encumbrances}),
    }


def _land_summary(land: Dict[str, Any]) -> Dict[str, Any]:
    state = _register_states(land)
    latest = (land.get("records") or [{}])[-1]
    return {
        "land_id": land["land_id"],
        "survey": land.get("survey"),
        "khasra": land.get("khasra"),
        "village": land.get("village"),
        "tehsil": land.get("tehsil"),
        "district": land.get("district"),
        "state": land.get("state"),
        "label": land.get("label"),
        "current_owner": land.get("current_owner"),
        "father": land.get("father"),
        "area": land.get("area"),
        "latest_year": land.get("latest_year"),
        "record_count": land.get("record_count", 0),
        "approved_count": land.get("approved_count", 0),
        "reference_record_id": land.get("reference_record_id"),
        "encumbrance_status": state["encumbrance_status"],
        "active_encumbrance_count": len(state["active_encumbrances"]),
        "pending_mutation_count": len(state["pending_mutations"]),
        "latest_status": _text(latest.get("status")),
        "encumbrance_banner": _encumbrance_banner(state),
    }


def _encumbrance_banner(state: Dict[str, Any]) -> Dict[str, Any]:
    active = state["active_encumbrances"]
    if active:
        item = active[0]
        return {
            "tone": "danger",
            "icon": "🔴",
            "text": "Active encumbrance",
            "lender": item.get("lender"),
            "reference": item.get("reference_no"),
            "amount": item.get("amount"),
            "encumbrance_id": item.get("id"),
        }
    if state["encumbrances"]:
        return {"tone": "ok", "icon": "🟢", "text": "Loans were registered but all are released", "lender": None, "reference": None, "amount": None, "encumbrance_id": None}
    return {"tone": "ok", "icon": "🟢", "text": "No active encumbrance", "lender": None, "reference": None, "amount": None, "encumbrance_id": None}


@land_router.get("")
def list_land_records(
    q: str = Query(""),
    village: str = Query(""),
    district: str = Query(""),
    encumbrance: str = Query(""),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: Dict[str, Any] = Depends(get_current_user),
):
    """Paginated land-record index grouped from the visible screened records."""
    lands = _land_records_index(user)
    items = [_land_summary(land) for land in lands.values()]
    needle = _text(q).casefold()
    if needle:
        items = [item for item in items if needle in " ".join(str(item.get(key) or "") for key in (
            "land_id", "survey", "khasra", "village", "tehsil", "district", "current_owner", "label")).casefold()]
    if _text(village):
        items = [item for item in items if _normalise_land(item.get("village")) == _normalise_land(village)]
    if _text(district):
        items = [item for item in items if _normalise_land(item.get("district")) == _normalise_land(district)]
    wanted = _text(encumbrance).upper()
    if wanted:
        items = [item for item in items if item.get("encumbrance_status") == wanted]
    items.sort(key=lambda item: (item.get("village") or "", item.get("survey") or ""))
    total = len(items)
    return {"land_records": items[offset:offset + limit], "total": total, "limit": limit, "offset": offset}


@land_router.get("/risk-review")
def risk_review(
    verdict: str = Query(""),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: Dict[str, Any] = Depends(require_roles(*REVIEWER_ROLES)),
):
    """Risk review workspace: computed verdicts for land records (paginated)."""
    lands = _land_records_index(user)
    results = []
    for land in lands.values():
        if not (land.get("records") or land.get("survey")):
            continue
        risk = compute_land_risk(land)
        summary = _land_summary(land)
        summary["risk"] = risk
        results.append(summary)
    wanted = _text(verdict).upper()
    if wanted:
        results = [item for item in results if item["risk"]["verdict"] == wanted]
    severity_rank = {"HIGH_RISK": 0, "REVIEW": 1, "CLEAR": 2}
    results.sort(key=lambda item: (severity_rank.get(item["risk"]["verdict"], 3), item.get("village") or "", item.get("survey") or ""))
    total = len(results)
    return {"land_records": results[offset:offset + limit], "total": total, "limit": limit, "offset": offset}


@land_router.get("/{land_id}")
def land_record_detail(land_id: str, user: Dict[str, Any] = Depends(get_current_user)):
    land = _get_land(user, land_id)
    if not land:
        raise HTTPException(status_code=404, detail="Land record not found.")
    risk = compute_land_risk(land)
    state = _register_states(land)
    documents = [_document_reference(record) for record in land.get("records") or []]
    ownership = _ownership_history(land, state["mutations"])
    detail = {
        "land_id": land["land_id"],
        "property": {
            "survey": land.get("survey"),
            "khasra": land.get("khasra"),
            "village": land.get("village"),
            "tehsil": land.get("tehsil"),
            "district": land.get("district"),
            "state": land.get("state"),
            "area": land.get("area"),
            "land_type": _latest_field(land, "land_class"),
        },
        "current_owner": {
            "owner": land.get("current_owner"),
            "father": land.get("father"),
            "ownership_type": _latest_field(land, "ownership_type"),
            "since_year": land.get("latest_year"),
        },
        "documents": documents,
        "ownership_history": ownership,
        "mutations": state["mutations"],
        "encumbrances": state["encumbrances"],
        "encumbrance_banner": state["encumbrance_banner"],
        "risk": risk,
        "map": {
            "focus_record_id": land.get("reference_record_id"),
            "url": f"/map?open_record={land.get('reference_record_id')}" if land.get("reference_record_id") else "/map",
            "note": "Map geometry is a project-owned, non-authoritative reference.",
        },
    }
    if user.get("role") == ROLE_ADMIN:
        detail["audit"] = _land_audit_trail(land)
    else:
        detail["audit"] = {"restricted": True, "reason": "The full audit trail is available to administrators."}
    return detail


def _document_reference(record: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": record.get("id"),
        "filename": record.get("filename"),
        "doc_type": record.get("doc_type"),
        "status": record.get("status"),
        "owner": record.get("owner"),
        "year": record.get("year"),
        "area": record.get("area"),
        "confidence": record.get("location_confidence"),
    }


def _latest_field(land: Dict[str, Any], key: str) -> str:
    for record in reversed(land.get("records") or []):
        fields = record.get("fields") if isinstance(record.get("fields"), dict) else {}
        entry = fields.get(key)
        value = entry.get("value") if isinstance(entry, dict) else entry
        if _text(value):
            return _text(value)
    return ""


def _ownership_history(land: Dict[str, Any], mutations: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for record in land.get("records") or []:
        events.append({
            "kind": "DOCUMENT",
            "year": _year_of(record.get("year")),
            "owner": _text(record.get("owner")),
            "father": _text(record.get("father")),
            "area": _text(record.get("area")),
            "document_id": record.get("id"),
            "filename": record.get("filename"),
            "status": record.get("status"),
            "doc_type": record.get("doc_type"),
            "created_at": record.get("created_at"),
        })
    for mutation in mutations:
        if _text(mutation.get("status")).upper() != "COMPLETED":
            continue
        events.append({
            "kind": "MUTATION",
            "year": _year_of(mutation.get("deed_date")) or _year_of(mutation.get("created_at")),
            "owner": _text(mutation.get("new_owner")),
            "father": "",
            "area": "",
            "document_id": None,
            "mutation_id": mutation.get("id"),
            "mutation_no": mutation.get("mutation_no"),
            "previous_owner": _text(mutation.get("previous_owner")),
            "status": "COMPLETED",
            "doc_type": "Mutation",
            "created_at": mutation.get("decided_at") or mutation.get("updated_at"),
        })
    events.sort(key=lambda event: ((event.get("year") is None), event.get("year") or 0, event.get("created_at") or 0))
    return events


def _land_audit_trail(land: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Recent audit entries that reference this land's documents or registers."""
    document_ids = {_text(record.get("id")) for record in land.get("records") or []}
    survey = _normalise_land(land.get("survey"))
    village = _normalise_land(land.get("village"))
    with get_db() as db:
        rows = db.execute("SELECT ts, username, action, detail, doc_id FROM audit ORDER BY ts DESC LIMIT 2000").fetchall()
    trail: List[Dict[str, Any]] = []
    for row in rows:
        detail = _text(row["detail"])
        doc_id = _text(row["doc_id"])
        matched_doc = doc_id in document_ids
        matched_register = (
            ("survey " in detail.casefold() and survey and survey in _normalise_land(detail))
            or (village and village in _normalise_land(detail) and survey and survey in _normalise_land(detail))
        )
        if matched_doc or matched_register:
            trail.append({"ts": row["ts"], "username": row["username"], "action": row["action"], "detail": detail, "doc_id": doc_id or None})
        if len(trail) >= 50:
            break
    return trail


@land_router.get("/{land_id}/risk")
def land_record_risk(land_id: str, user: Dict[str, Any] = Depends(get_current_user)):
    land = _get_land(user, land_id)
    if not land:
        raise HTTPException(status_code=404, detail="Land record not found.")
    return {"risk": compute_land_risk(land)}


@land_router.get("/{land_id}/encumbrances")
def land_record_encumbrances(land_id: str, user: Dict[str, Any] = Depends(get_current_user)):
    land = _get_land(user, land_id)
    if not land:
        raise HTTPException(status_code=404, detail="Land record not found.")
    encumbrances, _ = _land_register_rows(land)
    active = [item for item in encumbrances if _text(item.get("status")).upper() == "ACTIVE"]
    return {
        "encumbrances": encumbrances,
        "active_encumbrances": active,
        "encumbrance_status": "ACTIVE" if active else ("CLEAR" if encumbrances else "NONE"),
    }


@land_router.get("/{land_id}/mutations")
def land_record_mutations(land_id: str, user: Dict[str, Any] = Depends(get_current_user)):
    land = _get_land(user, land_id)
    if not land:
        raise HTTPException(status_code=404, detail="Land record not found.")
    _, mutations = _land_register_rows(land)
    return {"mutations": mutations}


# ---------------------------------------------------------------------------
# document-level integration (used by the existing verification flow)
# ---------------------------------------------------------------------------

def document_land_context(document: Dict[str, Any], user: Dict[str, Any]) -> Dict[str, Any]:
    """Risk + encumbrance context for one document, attached to the existing
    document-detail API so the verification queue sees land intelligence."""
    fields = document.get("fields") if isinstance(document.get("fields"), dict) else {}
    flat = {key: (entry.get("value") if isinstance(entry, dict) else entry) for key, entry in fields.items()}
    survey = _text(flat.get("survey_number")) or _text(flat.get("gat_number")) or _text(flat.get("khasra_number")) or _text(flat.get("plot_number"))
    village = _text(flat.get("village"))
    if not survey and not village:
        return {"matched": False}
    _, land_id = land_identity(survey, village)
    land = _land_records_index(user).get(land_id)
    if not land:
        return {"matched": False}
    risk = compute_land_risk(land)
    state = _register_states(land)
    context = {
        "matched": True,
        "land_id": land["land_id"],
        "survey": land.get("survey"),
        "village": land.get("village"),
        "encumbrance_banner": state["encumbrance_banner"],
        "active_encumbrance_count": len(state["active_encumbrances"]),
        "pending_mutation_count": len(state["pending_mutations"]),
        "risk_verdict": risk["verdict"],
        "risk_flags": [{"code": flag["code"], "severity": flag["severity"], "title": flag["title"]} for flag in risk["flags"]],
        "detail_url": f"/?land_id={land['land_id']}",
    }
    if user.get("role") in REVIEWER_ROLES:
        context["active_encumbrances"] = state["active_encumbrances"]
        context["recommendation"] = (
            "Do not approve a transfer while an active encumbrance exists; verify lender release first."
            if state["active_encumbrances"] and _is_transfer_document(document)
            else ("Human review required: " + ", ".join(flag["title"] for flag in risk["flags"][:3])
                  if risk["verdict"] != "CLEAR" else "No adverse land-level signals from the deterministic checks.")
        )
    return context


def _is_transfer_document(document: Dict[str, Any]) -> bool:
    doc_type = _text(document.get("doc_type")).casefold()
    return any(token in doc_type for token in ("transfer", "sale", "mutation", "deed"))


# ---------------------------------------------------------------------------
# Land Record Verification Report
# ---------------------------------------------------------------------------

def _next_report_reference(db: Any) -> str:
    year = time.strftime("%Y")
    prefix = f"LVR-{year}-"
    rows = db.execute("SELECT reference_no FROM land_reports WHERE reference_no LIKE ? ORDER BY reference_no DESC LIMIT 1",
                      (prefix + "%",)).fetchall()
    highest = 0
    for row in rows:
        match = re.search(r"(\d+)$", _text(row["reference_no"]))
        if match:
            highest = max(highest, int(match.group(1)))
    return f"{prefix}{highest + 1:06d}"


class ReportCreate(BaseModel):
    land_id: str
    document_id: Optional[str] = None


def _qr_png_data_uri(content: str) -> Optional[str]:
    """Machine-readable verification QR. Encodes the report reference URL only."""
    try:
        import qrcode  # optional dependency; report still renders without it
        import io

        image = qrcode.make(content, box_size=6, border=2)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        import base64
        return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    except Exception:
        return None


@report_router.post("/land-verification")
def generate_land_verification_report(req: ReportCreate, user: Dict[str, Any] = Depends(require_roles(*REVIEWER_ROLES))):
    ensure_land_tables()
    land = _get_land(user, req.land_id)
    if not land:
        raise HTTPException(status_code=404, detail="Land record not found.")
    document_id = _text(req.document_id) or land.get("reference_record_id") or ""
    if document_id:
        _assert_evidence_document(document_id, user)
    risk = compute_land_risk(land)
    state = _register_states(land)
    now = _now()
    with get_db() as db:
        reference = _next_report_reference(db)
        payload = {
            "reference_no": reference,
            "report_type": "LAND RECORD VERIFICATION REPORT",
            "document_id": document_id or None,
            "land_record_id": land["land_id"],
            "owner": land.get("current_owner"),
            "father": land.get("father"),
            "survey": land.get("survey"),
            "khasra": land.get("khasra"),
            "area": land.get("area"),
            "village": land.get("village"),
            "tehsil": land.get("tehsil"),
            "district": land.get("district"),
            "state": land.get("state"),
            "verification_status": (land.get("records") or [{}])[-1].get("status") if land.get("records") else None,
            "risk_status": risk["verdict"],
            "risk_flags": [{"code": flag["code"], "severity": flag["severity"], "title": flag["title"]} for flag in risk["flags"]],
            "encumbrance_status": state["encumbrance_status"],
            "active_encumbrances": [
                {"lender": item.get("lender"), "reference": item.get("reference_no"), "amount": item.get("amount"), "status": item.get("status")}
                for item in state["active_encumbrances"]
            ],
            "mutation_status": (max((_text(item.get("status")) for item in state["mutations"]), default="NONE")),
            "mutations": [{"mutation_no": item.get("mutation_no"), "status": item.get("status")} for item in state["mutations"]],
            "supporting_documents": [_document_reference(record) for record in land.get("records") or []],
            "generated_at": now,
            "reviewer": user.get("email") or user.get("full_name"),
            "legal_authority": False,
            "disclaimer": "Internal verification workflow report generated from screened documents. This is NOT an "
                          "official government land title certificate or encumbrance certificate.",
        }
        report_id = uuid.uuid4().hex[:12]
        db.execute(
            "INSERT INTO land_reports (id, reference_no, land_id, document_id, payload, generated_by, generated_at) VALUES (?,?,?,?,?,?,?)",
            (report_id, reference, land["land_id"], document_id or None, _json(payload), user.get("email") or "", now),
        )
    _audit(user, "REPORT_GENERATED", f"Land Record Verification Report {reference} generated for land {land['land_id']} (survey {land.get('survey')}, {land.get('village')})", document_id or None)
    return {"reference_no": reference, "report": payload, "html_url": f"/api/reports/land-verification/{reference}",
            "pdf_url": f"/api/reports/land-verification/{reference}/report.pdf",
            "qr_url": f"/api/reports/land-verification/{reference}/qr.png"}


@report_router.get("/land-verification/{reference}")
def get_land_verification_report(reference: str, format: str = Query("html"), user: Dict[str, Any] = Depends(require_roles(*REVIEWER_ROLES))):
    ensure_land_tables()
    with get_db() as db:
        row = db.execute("SELECT * FROM land_reports WHERE reference_no=?", (_text(reference),)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Report not found.")
    payload = _parse_json(row["payload"], {})
    _audit(user, "REPORT_VIEWED", f"Land Record Verification Report {reference} viewed")
    if _text(format).lower() == "json":
        return {"report": payload}
    return _render_report_html(payload)


def _render_report_html(payload: Dict[str, Any]):
    from fastapi.responses import HTMLResponse

    def esc(value: Any) -> str:
        return (str(value if value is not None else "—")
                .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))

    reference = payload.get("reference_no", "")
    verify_url = f"/api/reports/land-verification/{esc(reference)}?format=json"
    qr = _qr_png_data_uri(verify_url)
    verdict = _text(payload.get("risk_status")) or "UNKNOWN"
    verdict_color = {"CLEAR": "#15803d", "REVIEW": "#b45309", "HIGH_RISK": "#dc2626"}.get(verdict, "#475569")
    rows = [
        ("Document ID", payload.get("document_id")),
        ("Land record ID", payload.get("land_record_id")),
        ("Owner", payload.get("owner")),
        ("Father / guardian", payload.get("father")),
        ("Survey / Khasra", " / ".join(part for part in (payload.get("survey"), payload.get("khasra")) if part)),
        ("Area", payload.get("area")),
        ("Village", payload.get("village")),
        ("Tehsil", payload.get("tehsil")),
        ("District", payload.get("district")),
        ("Verification status", payload.get("verification_status")),
        ("Risk status", verdict),
        ("Encumbrance status", payload.get("encumbrance_status")),
        ("Mutation status", payload.get("mutation_status")),
        ("Reviewer", payload.get("reviewer")),
        ("Generated (UTC timestamp)", payload.get("generated_at")),
        ("Audit / reference ID", reference),
    ]
    flag_items = "".join(
        f"<li><b>{esc(flag.get('severity'))}</b> — {esc(flag.get('title'))} <code>{esc(flag.get('code'))}</code></li>"
        for flag in payload.get("risk_flags") or []
    ) or "<li>No risk signals recorded</li>"
    documents_items = "".join(
        f"<li><span class=\"mono\">{esc(doc.get('id'))}</span> — {esc(doc.get('filename'))} ({esc(doc.get('status'))})</li>"
        for doc in payload.get("supporting_documents") or []
    ) or "<li>No documents linked</li>"
    qr_block = (
        f"<img src=\"{qr}\" width=\"120\" height=\"120\" alt=\"Verification QR\"/>"
        if qr else "<div class=\"qr-fallback\">QR unavailable (qrcode library not installed)</div>"
    )
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"/>
<title>Land Record Verification Report {esc(reference)}</title>
<style>
  body {{ font-family: Georgia, 'Times New Roman', serif; color:#1e293b; margin:32px auto; max-width:820px; }}
  h1 {{ color:#1e3a8a; font-size:20px; letter-spacing:.4px; margin-bottom:4px; }}
  .kicker {{ font-size:11px; letter-spacing:2px; color:#b45309; font-weight:bold; }}
  table {{ border-collapse:collapse; width:100%; margin-top:16px; }}
  td {{ border:1px solid #cbd5e1; padding:7px 10px; font-size:13px; }}
  td.k {{ background:#f1f5f9; width:230px; font-weight:bold; }}
  .verdict {{ display:inline-block; padding:3px 10px; border-radius:4px; color:#fff; background:{verdict_color}; font-weight:bold; font-size:12px; }}
  .mono {{ font-family: ui-monospace, monospace; font-size:12px; }}
  ul {{ font-size:13px; }}
  .report-head {{ display:flex; justify-content:space-between; align-items:flex-start; gap:16px; border-bottom:3px solid #1e3a8a; padding-bottom:12px; }}
  .qr-note {{ font-size:10px; color:#64748b; max-width:140px; text-align:center; }}
  .disclaimer {{ margin-top:20px; border:1px solid #f59e0b; background:#fffbeb; padding:10px 12px; font-size:12px; }}
</style></head><body>
<div class="report-head">
  <div>
    <div class="kicker">DOCUMENT SCREENING · LAND INTELLIGENCE</div>
    <h1>LAND RECORD VERIFICATION REPORT</h1>
    <div class="mono">Reference: {esc(reference)}</div>
  </div>
  <div>{qr_block}<div class="qr-note">Machine-readable verification reference — identifies this report only.</div></div>
</div>
<table>
  {''.join(f'<tr><td class="k">{esc(label)}</td><td>{esc(value)}</td></tr>' for label, value in rows)}
</table>
<h3 style="color:#1e3a8a;font-size:14px;">Risk signals</h3>
<ul>{flag_items}</ul>
<h3 style="color:#1e3a8a;font-size:14px;">Supporting documents</h3>
<ul>{documents_items}</ul>
<div class="disclaimer"><b>Disclaimer.</b> {esc(payload.get('disclaimer'))}</div>
</body></html>"""
    return HTMLResponse(content=html)


@report_router.get("/land-verification/{reference}/qr.png")
def report_qr(reference: str, user: Dict[str, Any] = Depends(require_roles(*REVIEWER_ROLES))):
    from fastapi.responses import Response

    verify_url = f"/api/reports/land-verification/{_text(reference)}?format=json"
    try:
        import qrcode
        import io

        image = qrcode.make(verify_url, box_size=6, border=2)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return Response(content=buffer.getvalue(), media_type="image/png")
    except Exception:
        raise HTTPException(status_code=503, detail="QR generation requires the optional 'qrcode' dependency.")


@report_router.get("/land-verification/{reference}/report.pdf")
def report_pdf(reference: str, user: Dict[str, Any] = Depends(require_roles(*REVIEWER_ROLES))):
    """Land Record Verification Report as a PDF (reviewer/admin only, audited).

    The PDF carries the record/document ID, owner, survey/khasra, area,
    village/tehsil/district, verification/risk/encumbrance/mutation status,
    supporting documents, timestamp, reviewer, verification reference and a QR
    that encodes the safe report reference only."""
    from fastapi.responses import Response

    ensure_land_tables()
    with get_db() as db:
        row = db.execute("SELECT * FROM land_reports WHERE reference_no=?", (_text(reference),)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Report not found.")
    payload = _parse_json(row["payload"], {})
    try:
        from report_pdf import render_verification_report_pdf
        qr_png = None
        try:
            import io as _io

            import qrcode

            image = qrcode.make(f"/api/reports/land-verification/{_text(reference)}?format=json", box_size=4, border=2)
            buffer = _io.BytesIO()
            image.save(buffer, format="PNG")
            qr_png = buffer.getvalue()
        except Exception:
            qr_png = None
        content = render_verification_report_pdf(payload, qr_png)
    except Exception as exc:
        print(f"[REPORT PDF WARNING] {exc}")
        raise HTTPException(status_code=500, detail="PDF rendering failed.")
    _audit(user, "REPORT_PDF_DOWNLOADED", f"Land Record Verification Report {reference} downloaded as PDF")
    filename = f"land_verification_report_{_text(reference)}.pdf"
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )
