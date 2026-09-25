"""SA Investigation — automatic, explainable investigation of an uploaded land document.

SA (the Superior Assistant) does NOT replace the Land Intelligence systems. It
ORCHESTRATES the ones that already exist and EXPLAINS what they say:

* document fields / OCR text          -> ``documents`` table (server.py pipeline)
* parcel identity + register rows     -> ``land_intel`` (``land_record_detail``,
                                         ``compute_land_risk``, ``build_timeline``,
                                         ``_ownership_history``, ``_register_index``)
* controlled parcel candidates        -> ``mapping._resolve`` / ``property_documents``
* ownership-history signals           -> ``mapping.analyze_ownership_history``
* litigation register                 -> ``court_cases`` (``list_cases``, ``_all_cases``)
* owner-name comparison               -> ``land_intelligence.owner_match``
* background execution                -> ``assistant_tasks`` (request ids, run ids,
                                         heartbeat lease, retry, cancel, actor isolation)
* administrator decision              -> ``ai_governance`` proposal + password +
                                         CAS PROPOSED -> EXECUTING -> EXECUTED/FAILED

Hard rules implemented here:

* NO EVIDENCE = NO CLAIM. Every statement is FOUND / NOT FOUND IN SEARCHED
  SOURCES / POSSIBLE MATCH / CONFLICT / UNKNOWN / SOURCE UNAVAILABLE, and every
  finding carries the source records and deep links it was derived from.
* A POSSIBLE match is never upgraded to a fact by SA.
* SA never calls a mutation / land-changing operation. Approving an SA
  recommendation only records the ADMINISTRATOR's decision on the
  investigation (through the unchanged governance CAS); the document workflow
  and the registers remain authoritative and untouched.
* Findings, scenarios and the recommendation are computed from structured
  records with versioned deterministic rules — never invented by a model.
* Unfinished or failed investigations are never presented as complete.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import assistant_tasks

# ---------------------------------------------------------------------------
# lazy module access (server.py imports this module at the very end, and the
# Land Intelligence modules import server themselves)
# ---------------------------------------------------------------------------


def _server():
    import server
    return server


def _land_intel():
    import land_intel
    return land_intel


def _mapping():
    import mapping
    return mapping


def _court_cases():
    import court_cases
    return court_cases


def _land_intelligence():
    import land_intelligence
    return land_intelligence


def _governance():
    import ai_governance
    return ai_governance


# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

ENGINE_VERSION = "sa-investigation/1.0.0"
RULES_VERSION = "2026.09.1"
MODEL_INFO = {"provider": "deterministic-rules", "name": "sa-investigation-rules", "version": RULES_VERSION,
              "llm_used": False}
SURFACE = "SA_INVESTIGATION"
TASK_KIND = "INVESTIGATE_DOCUMENT"
ACTION_TYPE = "SA_INVESTIGATION_DECISION"

STATE_CREATED = "CREATED"
STATE_EXTRACTING = "EXTRACTING"
STATE_MATCHING = "MATCHING"
STATE_INVESTIGATING = "INVESTIGATING"
STATE_SCENARIO = "SCENARIO_ANALYSIS"
STATE_READY = "READY_FOR_REVIEW"
STATE_APPROVED = "APPROVED"
STATE_REJECTED = "REJECTED"
STATE_NEEDS_REVIEW = "NEEDS_REVIEW"
STATE_FAILED = "FAILED"

RUNNING_STATES = {STATE_CREATED, STATE_EXTRACTING, STATE_MATCHING, STATE_INVESTIGATING, STATE_SCENARIO}
DECIDABLE_STATES = {STATE_READY, STATE_NEEDS_REVIEW}
FINAL_STATES = {STATE_APPROVED, STATE_REJECTED}
COMPLETE_STATES = {STATE_READY, STATE_NEEDS_REVIEW, STATE_APPROVED, STATE_REJECTED}

DECISIONS = ("APPROVE", "REJECT", "NEEDS_REVIEW")
RECOMMENDATIONS = ("APPROVE", "REJECT", "FURTHER_VERIFICATION")
DECISION_FOR_RECOMMENDATION = {"APPROVE": "APPROVE", "REJECT": "REJECT", "FURTHER_VERIFICATION": "NEEDS_REVIEW"}
DECISION_STATE = {"APPROVE": STATE_APPROVED, "REJECT": STATE_REJECTED, "NEEDS_REVIEW": STATE_NEEDS_REVIEW}

SEVERITIES = ("INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL")
SEVERITY_RANK = {name: index for index, name in enumerate(SEVERITIES)}
ALERT_SEVERITIES = {"HIGH", "CRITICAL"}

MATCH_EXACT = "EXACT"
MATCH_STRONG = "STRONG"
MATCH_POSSIBLE = "POSSIBLE"
MATCH_CONFLICTING = "CONFLICTING"
MATCH_NONE = "NO_MATCH"
CERTAINTY_LABEL = {MATCH_EXACT: "Confirmed", MATCH_STRONG: "Strong match", MATCH_POSSIBLE: "Possible",
                   MATCH_CONFLICTING: "Conflict", MATCH_NONE: "Not found"}

STATUS_FOUND = "FOUND"
STATUS_NOT_FOUND = "NOT FOUND IN SEARCHED SOURCES"
STATUS_POSSIBLE = "POSSIBLE MATCH"
STATUS_CONFLICT = "CONFLICT"
STATUS_UNKNOWN = "UNKNOWN"
STATUS_UNAVAILABLE = "SOURCE UNAVAILABLE"
STATUS_NOT_SEARCHED = "NOT SEARCHED"

STRONG_IDENTIFIERS = ("parcel_id", "khasra_number", "survey_number", "registration_no", "mutation_no", "case_number")

#: Pipeline stages surfaced to the frontend (key, label, investigation state).
STAGES = (
    ("extract", "Extracting document...", STATE_EXTRACTING),
    ("parcel", "Finding parcel...", STATE_MATCHING),
    ("mutations", "Checking mutations...", STATE_INVESTIGATING),
    ("court_cases", "Checking court cases...", STATE_INVESTIGATING),
    ("ownership", "Building ownership history...", STATE_INVESTIGATING),
    ("scenarios", "Running scenarios...", STATE_SCENARIO),
    ("finalize", "Preparing investigation...", STATE_SCENARIO),
)
STAGE_LABEL = {key: label for key, label, _state in STAGES}

#: Existing land-risk engine flags folded into SA findings (never re-derived).
_RISK_FLAG_MAP = {
    "ACTIVE_ENCUMBRANCE": ("ENCUMBRANCE_ACTIVE", "HIGH"),
    "SALE_DURING_ENCUMBRANCE": ("ENCUMBRANCE_ACTIVE", "HIGH"),
    "OWNER_CONFLICT_YEAR": ("OWNERSHIP_MISMATCH", "HIGH"),
    "PENDING_MUTATION": ("MUTATION_MISMATCH", "MEDIUM"),
    "REJECTED_MUTATION": ("MUTATION_MISMATCH", "INFO"),
    "OWNER_CHANGE_NO_MUTATION": ("OWNERSHIP_GAP", "MEDIUM"),
    "AREA_JUMP": ("AREA_MISMATCH", "MEDIUM"),
    "DUPLICATE_CONFLICT": ("DUPLICATE_TRANSACTION", "MEDIUM"),
    "REJECTED_CONFLICT_COPY": ("DUPLICATE_TRANSACTION", "INFO"),
    "CHAIN_GAP": ("TIMELINE_ANOMALY", "LOW"),
    "SUSPICIOUS_MUTATION_SEQUENCE": ("TIMELINE_ANOMALY", None),
    "LOW_QUALITY_EXTRACTION": ("EXTRACTION_QUALITY", "INFO"),
    # litigation signals the risk engine takes from court_cases.litigation_flags
    "ACTIVE_LITIGATION": ("COURT_CASE_OVERLAP", "HIGH"),
    "TRANSFER_DURING_LITIGATION": ("COURT_CASE_OVERLAP", "HIGH"),
    "CLOSED_LITIGATION_ON_RECORD": ("COURT_CASE_OVERLAP", "INFO"),
}
_RISK_SEVERITY = {"HIGH": "HIGH", "REVIEW": "MEDIUM", "INFO": "INFO"}

#: Existing ownership-history analysis (mapping.analyze_ownership_history).
_OWNERSHIP_FINDING_MAP = {
    "OWNERSHIP_CONFLICT": ("OWNERSHIP_MISMATCH", "HIGH"),
    "POSSIBLE_DUPLICATE_OCR_VARIATION": ("DUPLICATE_TRANSACTION", "LOW"),
    "TRANSFER_SUPPORTED": ("OWNERSHIP_CONSISTENT", "INFO"),
    "TRANSFER_CANDIDATE": ("OWNERSHIP_GAP", "LOW"),
    "PARTITION_CANDIDATE": ("AREA_MISMATCH", "LOW"),
}

AREA_TOLERANCE = 0.15
AREA_SEVERE = 0.50

# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


def _env_float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def wait_seconds() -> float:
    """How long an API call waits for the worker before returning ``running``."""
    return _env_float("SA_INVESTIGATION_WAIT_SECONDS", 2.0, 0.05, 120.0)


def upload_wait_seconds() -> float:
    return _env_float("SA_INVESTIGATION_UPLOAD_WAIT_SECONDS", 0.2, 0.05, 60.0)


def source_timeout_seconds() -> float:
    return _env_float("SA_INVESTIGATION_SOURCE_TIMEOUT_SECONDS", 20.0, 0.5, 300.0)


def auto_investigate_enabled() -> bool:
    return str(os.getenv("SA_AUTO_INVESTIGATE", "1")).strip().lower() not in {"0", "false", "no", "off"}


# ---------------------------------------------------------------------------
# schema (additive, idempotent; DDL runs at import/startup, never per request)
# ---------------------------------------------------------------------------

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS land_investigations (
        investigation_id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL,
        investigation_version INTEGER NOT NULL DEFAULT 1,
        fingerprint TEXT DEFAULT '',
        fingerprint_basis TEXT DEFAULT '',
        state TEXT NOT NULL,
        progress TEXT NOT NULL DEFAULT '{}',
        request_id TEXT UNIQUE,
        run_token TEXT DEFAULT '',
        attempts INTEGER NOT NULL DEFAULT 0,
        trigger_kind TEXT DEFAULT 'MANUAL',
        created_by TEXT DEFAULT '',
        created_by_id TEXT DEFAULT '',
        created_by_role TEXT DEFAULT '',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        started_at REAL,
        completed_at REAL,
        failure TEXT DEFAULT '',
        land_id TEXT DEFAULT '',
        property_id TEXT DEFAULT '',
        match_class TEXT DEFAULT '',
        recommendation TEXT DEFAULT '',
        recommendation_payload TEXT NOT NULL DEFAULT '{}',
        confidence TEXT NOT NULL DEFAULT '{}',
        risk_verdict TEXT DEFAULT '',
        summary TEXT NOT NULL DEFAULT '{}',
        entities TEXT NOT NULL DEFAULT '[]',
        matches TEXT NOT NULL DEFAULT '[]',
        sources TEXT NOT NULL DEFAULT '{}',
        timeline TEXT NOT NULL DEFAULT '[]',
        scenarios TEXT NOT NULL DEFAULT '[]',
        evidence TEXT NOT NULL DEFAULT '[]',
        evidence_graph TEXT NOT NULL DEFAULT '{}',
        snapshot TEXT NOT NULL DEFAULT '{}',
        reproducibility TEXT NOT NULL DEFAULT '{}',
        proposal_id TEXT DEFAULT '',
        decision TEXT DEFAULT '',
        decision_note TEXT DEFAULT '',
        decision_override INTEGER NOT NULL DEFAULT 0,
        decided_by TEXT DEFAULT '',
        decided_at REAL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_land_investigations_document ON land_investigations(document_id, investigation_version)",
    "CREATE INDEX IF NOT EXISTS idx_land_investigations_fingerprint ON land_investigations(fingerprint)",
    "CREATE INDEX IF NOT EXISTS idx_land_investigations_state ON land_investigations(state, created_at)",
    """
    CREATE TABLE IF NOT EXISTS investigation_findings (
        finding_id TEXT PRIMARY KEY,
        investigation_id TEXT NOT NULL,
        finding_type TEXT NOT NULL,
        severity TEXT NOT NULL,
        confidence REAL NOT NULL DEFAULT 0,
        status_label TEXT DEFAULT '',
        title TEXT DEFAULT '',
        description TEXT DEFAULT '',
        evidence TEXT NOT NULL DEFAULT '[]',
        source_records TEXT NOT NULL DEFAULT '[]',
        pages TEXT NOT NULL DEFAULT '[]',
        why TEXT DEFAULT '',
        resolution TEXT NOT NULL DEFAULT '[]',
        is_alert INTEGER NOT NULL DEFAULT 0,
        origin TEXT DEFAULT '',
        rule_code TEXT DEFAULT '',
        affects TEXT NOT NULL DEFAULT '[]',
        created_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_investigation_findings_investigation ON investigation_findings(investigation_id)",
    "CREATE INDEX IF NOT EXISTS idx_investigation_findings_alert ON investigation_findings(is_alert, created_at)",
    """
    CREATE TABLE IF NOT EXISTS investigation_events (
        event_id TEXT PRIMARY KEY,
        investigation_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        actor TEXT DEFAULT '',
        detail TEXT DEFAULT '',
        created_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_investigation_events_investigation ON investigation_events(investigation_id, created_at)",
)
_tables_ready_for: Optional[str] = None
_tables_lock = threading.Lock()


def ensure_investigation_tables() -> None:
    """Create the investigation tables once per database (startup / DB swap)."""
    global _tables_ready_for
    s = _server()
    if _tables_ready_for == s.DB_PATH:
        return
    with _tables_lock:
        if _tables_ready_for == s.DB_PATH:
            return
        with s.get_db() as db:
            for statement in _SCHEMA:
                db.execute(statement)
        _tables_ready_for = s.DB_PATH


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _now() -> float:
    return time.time()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _parse_json(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return fallback


def _norm_text(value: Any) -> str:
    return re.sub(r"\s+", " ", _text(value)).casefold()


def _norm_code(value: Any) -> str:
    """Normalise registration / mutation / case numbers for comparison."""
    text = _text(value).upper()
    text = re.sub(r"\bOF\b", "/", text)
    text = re.sub(r"[\s.]+", "", text)
    text = re.sub(r"[^A-Z0-9/-]", "", text)
    return text.strip("/-")


def _number(value: Any) -> Optional[float]:
    return _land_intel()._number(value)


def _area_delta(first: Any, second: Any) -> Optional[float]:
    a, b = _number(first), _number(second)
    if a is None or b is None or a <= 0 or b <= 0:
        return None
    return abs(a - b) / max(a, b)


def _same_owner(first: Any, second: Any) -> bool:
    return _land_intel()._same_owner(first, second)


def _owner_similarity(first: Any, second: Any) -> Dict[str, Any]:
    return _land_intelligence().owner_match(first, second)


def _flat_fields(fields: Any) -> Dict[str, str]:
    flat: Dict[str, str] = {}
    if not isinstance(fields, dict):
        return flat
    for key, entry in fields.items():
        value = entry.get("value") if isinstance(entry, dict) else entry
        flat[str(key)] = _text(value)
    return flat


def _field_confidence(fields: Any, key: str) -> float:
    entry = fields.get(key) if isinstance(fields, dict) else None
    if isinstance(entry, dict):
        try:
            confidence = float(entry.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        return confidence / 100.0 if confidence > 1.0 else confidence
    return 1.0 if _text(entry) else 0.0


def _is_transfer(doc_type: Any) -> bool:
    lowered = _text(doc_type).casefold()
    return any(token in lowered for token in ("transfer", "sale", "deed", "conveyance", "gift"))


def _is_mutation_doc(doc_type: Any) -> bool:
    lowered = _text(doc_type).casefold()
    return any(token in lowered for token in ("mutation", "namantaran", "ferfar", "intkal"))


def _is_court_doc(doc_type: Any) -> bool:
    lowered = _text(doc_type).casefold()
    return any(token in lowered for token in ("court", "decree", "order", "judgment", "judgement"))


def _parse_date(value: Any) -> Optional[str]:
    """Return YYYY-MM-DD when the value carries a full date, else None."""
    text = _text(value)
    if not text:
        return None
    match = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if match:
        year, month, day = match.groups()
    else:
        match = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", text)
        if not match:
            return None
        day, month, year = match.groups()
    try:
        month_i, day_i = int(month), int(day)
        if not (1 <= month_i <= 12 and 1 <= day_i <= 31):
            return None
        return f"{int(year):04d}-{month_i:02d}-{day_i:02d}"
    except ValueError:
        return None


def _year_of(value: Any) -> Optional[int]:
    return _land_intel()._year_of(value)


def _audit(actor: Dict[str, Any], action: str, detail: str, doc_id: Optional[str] = None) -> None:
    """Audit through the existing trail. Never receives secrets (callers only
    pass identifiers, states and decision labels)."""
    try:
        _server().log_audit(actor.get("full_name") or actor.get("email") or "SA", action, detail, doc_id)
    except Exception as exc:  # pragma: no cover - audit must never break the workflow
        print(f"[SA INVESTIGATION AUDIT WARNING] {exc}")


def _event(investigation_id: str, actor: Dict[str, Any], event_type: str, detail: str,
           document_id: Optional[str] = None, audit_action: Optional[str] = None) -> None:
    ensure_investigation_tables()
    try:
        with _server().get_db() as db:
            db.execute(
                "INSERT INTO investigation_events(event_id, investigation_id, event_type, actor, detail, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (uuid.uuid4().hex, investigation_id, event_type,
                 actor.get("full_name") or actor.get("email") or "SA", detail[:2000], _now()),
            )
    except Exception as exc:  # pragma: no cover
        print(f"[SA INVESTIGATION EVENT WARNING] {exc}")
    _audit(actor, audit_action or f"SA_INVESTIGATION_{event_type}", f"{investigation_id}: {detail}"[:2000], document_id)


# ---------------------------------------------------------------------------
# documents
# ---------------------------------------------------------------------------

_DOC_COLUMNS = ("id", "filename", "doc_type", "mean_conf", "verdict", "status", "pages", "fields", "validation",
                "ocr_text", "cleaned_ocr_text", "uploaded_by", "created_at", "updated_at", "metadata")


def load_document(document_id: str) -> Optional[Dict[str, Any]]:
    with _server().get_db() as db:
        row = db.execute(f"SELECT {', '.join(_DOC_COLUMNS)} FROM documents WHERE id=?", (_text(document_id),)).fetchone()
    if not row:
        return None
    doc = dict(row)
    raw_fields = doc.get("fields")
    fields = _parse_json(raw_fields, None)
    doc["fields"] = fields if isinstance(fields, dict) else {}
    # malformed = a non-empty payload that is not a JSON object (unparseable or wrong shape)
    doc["fields_malformed"] = raw_fields not in (None, "") and not isinstance(fields, dict)
    validation = _parse_json(doc.get("validation"), {})
    doc["validation"] = validation if isinstance(validation, dict) else {}
    metadata = _parse_json(doc.get("metadata"), {})
    doc["metadata"] = metadata if isinstance(metadata, dict) else {}
    return doc


def document_visible(document: Dict[str, Any], user: Dict[str, Any]) -> bool:
    """Same visibility rules as ``GET /api/documents/{id}`` (mapping applies them too)."""
    return _mapping()._map_document_visible(document, user)


def document_fingerprint(document: Dict[str, Any]) -> Tuple[str, str]:
    """Content fingerprint used for 'already investigated' deduplication."""
    s = _server()
    doc_id = _text(document.get("id"))
    for ext in (".png", ".pdf", ".jpg", ".jpeg"):
        path = os.path.join(s.UPLOADS_DIR, f"{doc_id}{ext}")
        if os.path.isfile(path):
            digest = hashlib.sha256()
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest(), "FILE_CONTENT"
    text = _text(document.get("cleaned_ocr_text")) or _text(document.get("ocr_text"))
    flat = _flat_fields(document.get("fields"))
    payload = {"text": text, "fields": {key: flat[key] for key in sorted(flat) if flat[key]}}
    if not text and not payload["fields"]:
        return hashlib.sha256(f"document:{doc_id}".encode("utf-8")).hexdigest(), "DOCUMENT_ID"
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest(), "OCR_CONTENT"


# ---------------------------------------------------------------------------
# deep links (existing views only)
# ---------------------------------------------------------------------------


def _links_for(kind: str, ref: Any, **extra: Any) -> List[Dict[str, str]]:
    ref = _text(ref)
    if kind == "document":
        return [{"label": "Open Source Document", "href": f"/?open_document={ref}", "api": f"/api/documents/{ref}"},
                {"label": "Open Scan", "href": f"/api/documents/{ref}/file", "api": f"/api/documents/{ref}/file"}]
    if kind == "land":
        links = [{"label": "Open Parcel", "href": f"/?land_id={ref}", "api": f"/api/land-records/{ref}"}]
        record = _text(extra.get("record_id"))
        if record:
            links.append({"label": "Open Map", "href": f"/map?open_record={record}", "api": f"/api/map/records/{record}"})
        return links
    if kind == "property":
        return [{"label": "Open Parcel", "href": f"/map?property={ref}", "api": f"/api/map/properties/{ref}"}]
    if kind == "mutation":
        land = _text(extra.get("land_id"))
        return [{"label": "Open Mutation", "href": f"/?land_id={land}&mutation={ref}" if land else f"/?mutation={ref}",
                 "api": f"/api/mutations/{ref}"}]
    if kind == "encumbrance":
        land = _text(extra.get("land_id"))
        return [{"label": "Open Encumbrance", "href": f"/?land_id={land}&encumbrance={ref}" if land else f"/?encumbrance={ref}",
                 "api": f"/api/encumbrances/{ref}"}]
    if kind == "court_case":
        survey = _text(extra.get("survey"))
        village = _text(extra.get("village"))
        query = "&".join(part for part in (f"survey={survey}" if survey else "", f"village={village}" if village else "",
                                            f"case={ref}") if part)
        return [{"label": "Open Court Case", "href": f"/litigation?{query}", "api": f"/api/court-cases/{ref}"}]
    if kind == "jamabandi":
        return [{"label": "Open Jamabandi", "href": f"/?open_document={ref}", "api": f"/api/documents/{ref}"}]
    if kind == "registry":
        return [{"label": "Open Registry", "href": f"/?open_document={ref}", "api": f"/api/documents/{ref}"}]
    if kind == "investigation":
        return [{"label": "Open Investigation", "href": f"/?investigation={ref}", "api": f"/api/sa/investigations/{ref}"}]
    return []


# ---------------------------------------------------------------------------
# evidence registry
# ---------------------------------------------------------------------------


class _Evidence:
    """Collects evidence items exactly once and hands out stable ids."""

    def __init__(self) -> None:
        self.items: List[Dict[str, Any]] = []
        self._index: Dict[Tuple[str, str], str] = {}

    def add(self, kind: str, ref: Any, label: str, *, status: str = STATUS_FOUND, source: str = "",
            snapshot: Optional[Dict[str, Any]] = None, pages: Optional[List[int]] = None, **link_extra: Any) -> str:
        key = (kind, _text(ref) or label)
        if key in self._index:
            return self._index[key]
        evidence_id = f"E{len(self.items) + 1:02d}"
        self.items.append({
            "evidence_id": evidence_id,
            "kind": kind,
            "ref": _text(ref),
            "label": label,
            "status": status,
            "source": source,
            "snapshot": snapshot or {},
            "pages": pages or [],
            "links": _links_for(kind, ref, **link_extra),
        })
        self._index[key] = evidence_id
        return evidence_id

    def get(self, evidence_id: str) -> Optional[Dict[str, Any]]:
        for item in self.items:
            if item["evidence_id"] == evidence_id:
                return item
        return None


# ---------------------------------------------------------------------------
# 1) entity extraction (document fields + conservative OCR-text patterns)
# ---------------------------------------------------------------------------

_ENTITY_SPECS: Tuple[Tuple[str, Tuple[str, ...], str], ...] = (
    ("parcel_id", ("parcel_id",), "STRONG"),
    ("khasra_number", ("khasra_number",), "STRONG"),
    ("survey_number", ("survey_number", "gat_number"), "STRONG"),
    ("registration_no", ("registration_no", "registration_number"), "STRONG"),
    ("mutation_no", ("mutation_no", "mutation_number"), "STRONG"),
    ("case_number", ("case_number", "court_case_no"), "STRONG"),
    ("plot_number", ("plot_number",), "SUPPORTING"),
    ("khata_number", ("khata_number",), "SUPPORTING"),
    ("khewat_number", ("khewat_number",), "SUPPORTING"),
    ("owner_name", ("owner_name",), "SUPPORTING"),
    ("father_name", ("father_name",), "SUPPORTING"),
    ("seller_name", ("seller_name", "vendor_name", "transferor"), "SUPPORTING"),
    ("buyer_name", ("buyer_name", "purchaser_name", "transferee"), "SUPPORTING"),
    ("village", ("village",), "SUPPORTING"),
    ("tehsil", ("tehsil", "taluka"), "SUPPORTING"),
    ("district", ("district",), "SUPPORTING"),
    ("state", ("state",), "SUPPORTING"),
    ("area", ("area",), "SUPPORTING"),
    ("document_date", ("document_date",), "SUPPORTING"),
    ("registration_date", ("registration_date",), "SUPPORTING"),
    ("mutation_date", ("mutation_date",), "SUPPORTING"),
    ("khatauni_year", ("khatauni_year",), "SUPPORTING"),
    ("court_name", ("court_name",), "SUPPORTING"),
    ("land_class", ("land_class",), "SUPPORTING"),
    ("ownership_type", ("ownership_type",), "SUPPORTING"),
)

_TEXT_PATTERNS: Dict[str, "re.Pattern[str]"] = {
    "case_number": re.compile(
        r"(?:case|suit|petition|appeal|o\.?s\.?|c\.?s\.?|w\.?p\.?|r\.?f\.?a\.?|c\.?r\.?p\.?)\s*(?:no\.?|number|#)?\s*[:\-]?\s*"
        r"((?:[A-Z]{1,5}[\s./-]*)?\d{1,6}\s*(?:/|of)\s*\d{2,4})", re.I),
    "court_name": re.compile(
        r"((?:hon'?ble\s+)?(?:supreme court|high court|district (?:and sessions )?court|civil (?:judge|court)|"
        r"court of [^\n,.]{3,40}|[A-Za-z]{3,20} court)(?:[ ,]+(?:at|of)\s+[A-Za-z .]{3,30})?)", re.I),
    "seller_name": re.compile(r"(?:seller|vendor|transferor|executant)(?:'s)?\s*(?:name)?\s*[:\-]\s*([^\n,;]{3,60})", re.I),
    "buyer_name": re.compile(r"(?:buyer|purchaser|vendee|transferee)(?:'s)?\s*(?:name)?\s*[:\-]\s*([^\n,;]{3,60})", re.I),
    "registration_no": re.compile(r"(?:registration|regd\.?|registry|deed)\s*(?:no\.?|number|#)\s*[:\-]?\s*([A-Z0-9][A-Z0-9/-]{1,30})", re.I),
    "registration_date": re.compile(
        r"(?:registration|regd\.?|registered)\s*(?:date|on)\s*[:\-]?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2})", re.I),
    "mutation_no": re.compile(r"(?:mutation|namantaran|ferfar|intkal)\s*(?:no\.?|number|#)\s*[:\-]?\s*([A-Z0-9][A-Z0-9/-]{1,30})", re.I),
    "mutation_date": re.compile(
        r"(?:mutation|namantaran)\s*(?:date|dated|on)\s*[:\-]?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2})", re.I),
    "parcel_id": re.compile(r"(?:parcel|ulpin|bhu-?naksha)\s*(?:id|no\.?|number|#)\s*[:\-]?\s*([A-Z0-9][A-Z0-9/-]{2,40})", re.I),
    "khewat_number": re.compile(r"khewat\s*(?:no\.?|number|#)?\s*[:\-]?\s*(\d[\d/-]{0,15})", re.I),
}
_TEXT_PATTERN_CONFIDENCE = 0.55


def _clean_capture(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" .,:;-")


def extract_entities(document: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Identifiers with value / source / page / confidence.

    Values come from the document's already-extracted fields first (the
    canonical pipeline output); conservative OCR-text patterns only ADD
    identifiers the pipeline does not model (case numbers, seller/buyer,
    registration dates ...) and are labelled *Possible* until confirmed.
    """
    fields = document.get("fields") if isinstance(document.get("fields"), dict) else {}
    flat = _flat_fields(fields)
    pages = document.get("pages")
    try:
        page_count = int(pages or 0)
    except (TypeError, ValueError):
        page_count = 0
    page = 1 if page_count <= 1 else None
    page_note = "" if page_count <= 1 else "OCR pipeline does not track the page per field"
    entities: List[Dict[str, Any]] = []
    seen: set = set()

    def push(entity_type: str, value: str, source: str, confidence: float, strength: str, field_key: str = "") -> None:
        value = _text(value)
        if not value or (entity_type, _norm_text(value)) in seen:
            return
        seen.add((entity_type, _norm_text(value)))
        certainty = "Confirmed" if source == "DOCUMENT_FIELDS" and confidence >= 0.8 else "Possible"
        entities.append({
            "entity_id": f"N{len(entities) + 1:02d}",
            "type": entity_type,
            "value": value,
            "source": source,
            "field_key": field_key,
            "page": page,
            "page_note": page_note,
            "confidence": round(max(0.0, min(1.0, confidence)), 3),
            "strength": strength,
            "certainty": certainty,
        })

    for entity_type, keys, strength in _ENTITY_SPECS:
        for key in keys:
            if flat.get(key):
                push(entity_type, flat[key], "DOCUMENT_FIELDS", _field_confidence(fields, key), strength, key)
                break

    text = _text(document.get("cleaned_ocr_text")) or _text(document.get("ocr_text"))
    if text:
        strength_by_type = {name: strength for name, _keys, strength in _ENTITY_SPECS}
        for entity_type, pattern in _TEXT_PATTERNS.items():
            if any(item["type"] == entity_type for item in entities):
                continue
            match = pattern.search(text)
            if match:
                push(entity_type, _clean_capture(match.group(1)), "OCR_TEXT_PATTERN", _TEXT_PATTERN_CONFIDENCE,
                     strength_by_type.get(entity_type, "SUPPORTING"))
    return entities


def _entity_map(entities: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {item["type"]: item for item in entities}


def _value(ids: Dict[str, Dict[str, Any]], key: str) -> str:
    entity = ids.get(key)
    return _text(entity.get("value")) if entity else ""


# ---------------------------------------------------------------------------
# 2) bounded, partial-result source lookups (existing services only)
# ---------------------------------------------------------------------------


def _gather(tasks: Dict[str, Callable[[], Any]], timeout: float) -> Dict[str, Dict[str, Any]]:
    """Run source lookups in parallel with bounded concurrency and a deadline.

    A lookup that fails or overruns is reported as SOURCE UNAVAILABLE for that
    source only — the investigation continues with partial results instead of
    inventing anything.
    """
    results: Dict[str, Dict[str, Any]] = {}
    if not tasks:
        return results
    pool = ThreadPoolExecutor(max_workers=min(4, len(tasks)), thread_name_prefix="sa-inv-source")
    started = _now()
    futures = {name: pool.submit(_timed, fn) for name, fn in tasks.items()}
    deadline = started + max(0.5, float(timeout))
    try:
        for name, future in futures.items():
            remaining = max(0.05, deadline - _now())
            try:
                value, elapsed = future.result(timeout=remaining)
                results[name] = {"ok": True, "value": value, "error": None, "elapsed_ms": elapsed}
            except FutureTimeout:
                results[name] = {"ok": False, "value": None, "error": "TIMEOUT", "elapsed_ms": int((_now() - started) * 1000)}
            except Exception as exc:  # noqa: BLE001 - reported per source
                results[name] = {"ok": False, "value": None, "error": f"{type(exc).__name__}: {exc}"[:300],
                                 "elapsed_ms": int((_now() - started) * 1000)}
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return results


def _timed(fn: Callable[[], Any]) -> Tuple[Any, int]:
    started = _now()
    value = fn()
    return value, int((_now() - started) * 1000)


def _lookup_land(land_id: str, actor: Dict[str, Any], document_id: str) -> Optional[Dict[str, Any]]:
    """The authoritative parcel aggregate: ``land_intel.land_record_detail``."""
    if not land_id:
        return None
    li = _land_intel()
    try:
        detail = li.land_record_detail(land_id, actor)
    except HTTPException as exc:
        if exc.status_code == 404:
            return None
        raise
    detail = dict(detail)
    doc_ids = [_text(item.get("id")) for item in (detail.get("documents") or []) if _text(item.get("id"))]
    detail["document_rows"] = _documents_with_fields(doc_ids)
    detail["ownership_analysis"] = _mapping().analyze_ownership_history(detail["document_rows"]) if len(doc_ids) >= 1 else {}
    detail["independent_document_ids"] = [doc_id for doc_id in doc_ids if doc_id != document_id]
    return detail


def _documents_with_fields(doc_ids: Sequence[str]) -> List[Dict[str, Any]]:
    ids = [_text(item) for item in doc_ids if _text(item)][:200]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    with _server().get_db() as db:
        rows = db.execute(
            f"SELECT id, filename, doc_type, status, fields, uploaded_by, created_at FROM documents WHERE id IN ({placeholders})",
            tuple(ids),
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        fields = _parse_json(item.get("fields"), {})
        item["fields"] = fields if isinstance(fields, dict) else {}
        out.append(item)
    return out


def _lookup_court(survey: str, case_number: str) -> Dict[str, Any]:
    court = _court_cases()
    result = {"survey_cases": [], "by_number": [], "searched": []}
    if survey:
        # All same-survey cases regardless of village; village agreement is
        # classified later (different village => POSSIBLE, never applied).
        result["survey_cases"] = court.list_cases(survey, "")
        result["searched"].append(f"litigation register by survey/khasra {survey}")
    if case_number:
        wanted = _norm_code(case_number)
        result["by_number"] = [case for case in court._all_cases() if wanted and _norm_code(case.get("case_number")) == wanted]
        result["searched"].append(f"litigation register by case number {case_number}")
    return result


def _lookup_registers(mutation_no: str, registration_no: str) -> Dict[str, Any]:
    """Register-wide lookups by identifier (mutation number, deed/registration number)."""
    encumbrances, mutations = _land_intel()._register_index()
    result = {"by_mutation_no": [], "by_deed_no": [], "searched": ["mutation register"]}
    wanted_mutation = _norm_code(mutation_no)
    wanted_deed = _norm_code(registration_no)
    for mutation in mutations:
        if wanted_mutation and _norm_code(mutation.get("mutation_no")) == wanted_mutation:
            result["by_mutation_no"].append(mutation)
        if wanted_deed and _norm_code(mutation.get("deed_no")) == wanted_deed:
            result["by_deed_no"].append(mutation)
    result["encumbrance_count"] = len(encumbrances)
    return result


def _lookup_registry(registration_no: str, fingerprint: str, document_id: str, actor: Dict[str, Any]) -> Dict[str, Any]:
    """Registry = registered deed documents already screened by this system."""
    result: Dict[str, Any] = {"same_registration": [], "same_content": [], "searched": ["screened documents (registry copies)"]}
    if registration_no:
        wanted = _norm_code(registration_no)
        with _server().get_db() as db:
            rows = db.execute(
                "SELECT id, filename, doc_type, status, fields, uploaded_by, created_at FROM documents "
                "WHERE id != ? AND fields LIKE ? ORDER BY created_at DESC LIMIT 200",
                (document_id, f"%{registration_no[:40]}%"),
            ).fetchall()
        for row in rows:
            item = dict(row)
            if not document_visible(item, actor):
                continue
            fields = _parse_json(item.get("fields"), {})
            fields = fields if isinstance(fields, dict) else {}
            flat = _flat_fields(fields)
            if wanted and _norm_code(flat.get("registration_no")) == wanted:
                item["fields"] = fields
                item["flat"] = flat
                result["same_registration"].append(item)
    if fingerprint:
        with _server().get_db() as db:
            rows = db.execute(
                "SELECT investigation_id, document_id, state, created_at FROM land_investigations "
                "WHERE fingerprint=? AND document_id != ? ORDER BY created_at DESC LIMIT 20",
                (fingerprint, document_id),
            ).fetchall()
        seen: set = set()
        for row in rows:
            other_id = _text(row["document_id"])
            if other_id in seen:
                continue
            seen.add(other_id)
            other = load_document(other_id)
            if other and document_visible(other, actor):
                result["same_content"].append({"id": other_id, "status": other.get("status"), "doc_type": other.get("doc_type"),
                                               "filename": other.get("filename"), "investigation_id": row["investigation_id"]})
    return result


def _lookup_parcel(fields: Dict[str, Any], document_id: str) -> Dict[str, Any]:
    mapping = _mapping()
    resolution = mapping._resolve(fields)
    linked = None
    with _server().get_db() as db:
        row = db.execute(
            "SELECT property_id, source_type, linked_at FROM property_documents WHERE document_id=? ORDER BY linked_at DESC LIMIT 1",
            (document_id,),
        ).fetchone()
    if row:
        linked = dict(row)
    return {"resolution": resolution, "linked": linked}


# ---------------------------------------------------------------------------
# 3) matching (EXACT / STRONG / POSSIBLE / CONFLICTING / NO_MATCH)
# ---------------------------------------------------------------------------


def classify_match(*, strong_hits: Sequence[str], hard_conflicts: Sequence[str], supporting_hits: Sequence[str],
                   minor_differences: Sequence[str]) -> str:
    """Deterministic match classification shared by every source.

    * strong identifier + a supporting identifier agree, nothing conflicts -> EXACT
    * strong identifier agrees, only minor differences / no supporting data -> STRONG
    * strong identifier agrees but a supporting identifier CONFLICTS       -> CONFLICTING
    * only supporting identifiers agree                                    -> POSSIBLE
    * nothing agrees                                                       -> NO_MATCH
    """
    if strong_hits and hard_conflicts:
        return MATCH_CONFLICTING
    if strong_hits and supporting_hits and not minor_differences:
        return MATCH_EXACT
    if strong_hits:
        return MATCH_STRONG
    if len(supporting_hits) >= 2:
        return MATCH_POSSIBLE
    return MATCH_NONE


def _match_record(source: str, record_id: Any, label: str, *, strong_hits: List[str], hard_conflicts: List[str],
                  supporting_hits: List[str], minor_differences: List[str], reasons: List[str], evidence_id: str,
                  applies: bool = True) -> Dict[str, Any]:
    match_class = classify_match(strong_hits=strong_hits, hard_conflicts=hard_conflicts,
                                 supporting_hits=supporting_hits, minor_differences=minor_differences)
    if not applies and match_class in {MATCH_EXACT, MATCH_STRONG}:
        match_class = MATCH_POSSIBLE
    score = {MATCH_EXACT: 0.95, MATCH_STRONG: 0.8, MATCH_POSSIBLE: 0.45, MATCH_CONFLICTING: 0.3, MATCH_NONE: 0.0}[match_class]
    return {
        "source": source,
        "record_id": _text(record_id),
        "label": label,
        "match_class": match_class,
        "certainty": CERTAINTY_LABEL[match_class],
        "score": score,
        "matched_identifiers": strong_hits + supporting_hits,
        "strong_hits": list(strong_hits),
        "supporting_hits": list(supporting_hits),
        "conflicting_identifiers": hard_conflicts,
        "minor_differences": minor_differences,
        "reasons": reasons,
        "evidence_id": evidence_id,
    }


def _best_class(matches: Sequence[Dict[str, Any]]) -> str:
    order = [MATCH_EXACT, MATCH_STRONG, MATCH_CONFLICTING, MATCH_POSSIBLE, MATCH_NONE]
    classes = {item["match_class"] for item in matches}
    for name in order:
        if name in classes:
            return name
    return MATCH_NONE


# ---------------------------------------------------------------------------
# investigation context (everything the rule engine works on)
# ---------------------------------------------------------------------------


class _Context:
    def __init__(self, investigation_id: str, document: Dict[str, Any], actor: Dict[str, Any]) -> None:
        self.investigation_id = investigation_id
        self.document = document
        self.document_id = _text(document.get("id"))
        self.actor = actor
        self.doc_type = _text(document.get("doc_type")) or "Land Record"
        self.is_transfer = _is_transfer(self.doc_type)
        self.fields = document.get("fields") if isinstance(document.get("fields"), dict) else {}
        self.flat = _flat_fields(self.fields)
        self.entities: List[Dict[str, Any]] = []
        self.ids: Dict[str, Dict[str, Any]] = {}
        self.evidence = _Evidence()
        self.matches: List[Dict[str, Any]] = []
        self.findings: List[Dict[str, Any]] = []
        self.sources: Dict[str, Dict[str, Any]] = {}
        self.land_id = ""
        self.land_key = ""
        self.survey = ""
        self.village = ""
        self.land: Optional[Dict[str, Any]] = None
        self.parcel: Dict[str, Any] = {}
        self.court: Dict[str, Any] = {}
        self.registers: Dict[str, Any] = {}
        self.registry: Dict[str, Any] = {}
        self.timeline: List[Dict[str, Any]] = []
        self.scenarios: List[Dict[str, Any]] = []
        self.recommendation: Dict[str, Any] = {}
        self.confidence: Dict[str, Any] = {}
        self.snapshot_at = _now()
        self.fingerprint = ""
        self.fingerprint_basis = ""
        self.doc_evidence = ""
        self.page = 1 if int(document.get("pages") or 1) <= 1 else None

    # -- helpers ----------------------------------------------------------
    def value(self, key: str) -> str:
        return _value(self.ids, key)

    def pages(self) -> List[int]:
        return [self.page] if self.page else []

    def finding(self, finding_type: str, severity: str, title: str, description: str, *, confidence: float,
                status_label: str, evidence: Sequence[str] = (), why: str = "", resolution: Sequence[str] = (),
                origin: str = "SA_RULE", affects: Sequence[str] = ("APPROVE",), code: str = "") -> Dict[str, Any]:
        severity = severity if severity in SEVERITY_RANK else "MEDIUM"
        evidence_ids = [item for item in dict.fromkeys(evidence) if item]
        records = []
        for evidence_id in evidence_ids:
            item = self.evidence.get(evidence_id)
            if item:
                records.append({"kind": item["kind"], "id": item["ref"], "label": item["label"], "links": item["links"]})
        finding = {
            "finding_id": f"{self.investigation_id}-F{len(self.findings) + 1:02d}",
            "type": finding_type,
            "code": code or finding_type,
            "severity": severity,
            "confidence": round(max(0.0, min(1.0, confidence)), 2),
            "status_label": status_label,
            "title": title,
            "description": description,
            "evidence": evidence_ids,
            "source_records": records,
            "pages": self.pages(),
            "why": why or f"SA raised this because the rule {code or finding_type} (rules {RULES_VERSION}) matched the records listed as evidence.",
            "resolution": list(resolution),
            "is_alert": severity in ALERT_SEVERITIES,
            "origin": origin,
            "affects": list(affects),
        }
        self.findings.append(finding)
        return finding

    def source_status(self, name: str, status: str, searched: str, **extra: Any) -> None:
        payload = {"status": status, "searched": searched}
        payload.update(extra)
        self.sources[name] = payload


# ---------------------------------------------------------------------------
# 4) the investigation itself (pure functions over the context)
# ---------------------------------------------------------------------------


def _stage_extract(ctx: _Context) -> None:
    ctx.entities = extract_entities(ctx.document)
    ctx.ids = _entity_map(ctx.entities)
    ctx.doc_evidence = ctx.evidence.add(
        "document", ctx.document_id, f"{ctx.document.get('filename') or ctx.document_id} ({ctx.doc_type})",
        source="documents", pages=ctx.pages(),
        snapshot={"status": ctx.document.get("status"), "doc_type": ctx.doc_type, "uploaded_by": ctx.document.get("uploaded_by"),
                  "owner_name": ctx.flat.get("owner_name", ""), "survey_number": ctx.flat.get("survey_number", ""),
                  "khasra_number": ctx.flat.get("khasra_number", ""), "village": ctx.flat.get("village", "")})
    strong = [item for item in ctx.entities if item["strength"] == "STRONG"]
    if ctx.document.get("fields_malformed"):
        ctx.finding("EXTRACTION_QUALITY", "MEDIUM", "Stored field payload is malformed",
                    "SA identified that the document's stored field payload could not be parsed; identifiers were "
                    "recovered from OCR text patterns only.", confidence=0.9, status_label=STATUS_UNKNOWN,
                    evidence=[ctx.doc_evidence], resolution=["Re-run OCR / field extraction for this document."])
    parcel_ids = [item for item in strong if item["type"] in {"survey_number", "khasra_number", "parcel_id"}]
    if not parcel_ids:
        ctx.finding("IDENTIFIER_MISSING", "HIGH", "No parcel identifier extracted",
                    "SA could not find a survey, khasra or parcel identifier in the document. Without one, the parcel "
                    "cannot be matched against the land records; nothing below should be read as a parcel match.",
                    confidence=0.95, status_label=STATUS_UNKNOWN, evidence=[ctx.doc_evidence],
                    why="Parcel matching requires at least one strong identifier (survey/khasra/parcel id).",
                    resolution=["Correct the survey/khasra number in the field editor and run a new investigation."])
    elif not ctx.flat.get("village"):
        ctx.finding("IDENTIFIER_MISSING", "MEDIUM", "Village not extracted",
                    "Records indicate the parcel identity is only partially known: a survey/khasra number was read but "
                    "no village. Same-number parcels in other villages cannot be told apart.",
                    confidence=0.9, status_label=STATUS_UNKNOWN, evidence=[ctx.doc_evidence],
                    resolution=["Confirm the village from the scan and update the field."])
    mean_conf = ctx.document.get("mean_conf")
    try:
        mean_conf = float(mean_conf) if mean_conf is not None else None
    except (TypeError, ValueError):
        mean_conf = None
    if mean_conf is not None and mean_conf < 55:
        ctx.finding("EXTRACTION_QUALITY", "LOW", "Low OCR confidence",
                    f"Records indicate a mean OCR confidence of {mean_conf:.0f}%; extracted identifiers should be treated as "
                    "Possible until a reviewer confirms them.", confidence=0.9, status_label=STATUS_UNKNOWN,
                    evidence=[ctx.doc_evidence], resolution=["Verify the extracted identifiers against the scan."])


def _stage_identity(ctx: _Context) -> None:
    li = _land_intel()
    land_id, key, survey = li.land_id_for_fields(ctx.flat)
    ctx.survey = survey
    ctx.village = li._land_village(ctx.flat) if hasattr(li, "_land_village") else ctx.flat.get("village", "")
    if survey or ctx.village:
        ctx.land_id, ctx.land_key = land_id, key


def _stage_sources(ctx: _Context, run: "_Run") -> None:
    fingerprint = ctx.fingerprint
    tasks: Dict[str, Callable[[], Any]] = {
        "land": lambda: _lookup_land(ctx.land_id, ctx.actor, ctx.document_id),
        "parcel": lambda: _lookup_parcel(ctx.fields, ctx.document_id),
        "court": lambda: _lookup_court(ctx.survey, ctx.value("case_number")),
        "registers": lambda: _lookup_registers(ctx.value("mutation_no"), ctx.value("registration_no")),
        "registry": lambda: _lookup_registry(ctx.value("registration_no"), fingerprint, ctx.document_id, ctx.actor),
    }
    results = _gather(tasks, source_timeout_seconds())
    run.stage("parcel", "DONE")
    run.stage("mutations", "RUNNING", state=STATE_INVESTIGATING)
    for name in ("land", "parcel", "court", "registers", "registry"):
        outcome = results.get(name) or {"ok": False, "error": "NOT RUN", "elapsed_ms": 0, "value": None}
        if not outcome["ok"]:
            ctx.source_status(name, STATUS_UNAVAILABLE, _SOURCE_DESCRIPTION[name], error=outcome["error"], elapsed_ms=outcome["elapsed_ms"])
        else:
            ctx.source_status(name, STATUS_FOUND, _SOURCE_DESCRIPTION[name], elapsed_ms=outcome["elapsed_ms"])
    ctx.land = results["land"]["value"] if results["land"]["ok"] else None
    ctx.parcel = results["parcel"]["value"] if results["parcel"]["ok"] else {}
    ctx.court = results["court"]["value"] if results["court"]["ok"] else {}
    ctx.registers = results["registers"]["value"] if results["registers"]["ok"] else {}
    ctx.registry = results["registry"]["value"] if results["registry"]["ok"] else {}
    ctx.snapshot_at = _now()
    for name, outcome in results.items():
        if not outcome["ok"]:
            ctx.finding("SOURCE_UNAVAILABLE", "MEDIUM", f"{_SOURCE_DESCRIPTION[name]} unavailable",
                        f"SA could not read {_SOURCE_DESCRIPTION[name]} ({outcome['error']}). Statements about this source are "
                        "UNKNOWN, not negative. The investigation is partially incomplete.", confidence=1.0,
                        status_label=STATUS_UNAVAILABLE, evidence=[ctx.doc_evidence],
                        why="A source that could not be searched must never be reported as 'no record found'.",
                        resolution=["Retry the investigation once the source is reachable."], affects=("APPROVE", "REJECT"))


_SOURCE_DESCRIPTION = {
    "land": "land record aggregate (documents, ownership, mutations, encumbrances, litigation, risk)",
    "parcel": "controlled parcel register",
    "court": "litigation register",
    "registers": "mutation register",
    "registry": "screened registry documents",
}


def _stage_parcel_match(ctx: _Context) -> None:
    """Parcel / land matching from the land aggregate and the controlled parcel register."""
    land = ctx.land
    parcel = ctx.parcel or {}
    resolution = parcel.get("resolution") or {}
    if ctx.sources.get("land", {}).get("status") == STATUS_UNAVAILABLE:
        ctx.sources["land"]["match_class"] = MATCH_NONE
    elif not ctx.land_id:
        ctx.source_status("land", STATUS_UNKNOWN, _SOURCE_DESCRIPTION["land"], note="no parcel identifier to search with")
    elif land is None:
        ctx.source_status("land", STATUS_NOT_FOUND, _SOURCE_DESCRIPTION["land"], land_id=ctx.land_id,
                          note=f"no land record for survey {ctx.survey or '—'} / village {ctx.village or '—'}")
        ctx.finding("IDENTIFIER_MISMATCH", "MEDIUM", "Parcel not found in land records",
                    f"Records indicate no land record for survey/khasra {ctx.survey or '—'} in village {ctx.village or '—'} "
                    "among the documents visible to this investigation. NOT FOUND IN SEARCHED SOURCES is not proof that the "
                    "parcel does not exist.", confidence=0.8, status_label=STATUS_NOT_FOUND, evidence=[ctx.doc_evidence],
                    resolution=["Check the identifiers against the scan.", "Search the Jamabandi / RoR for the parcel."],
                    code="PARCEL_NOT_FOUND")
    else:
        independent = land.get("independent_document_ids") or []
        register_rows = len(land.get("mutations") or []) + len(land.get("encumbrances") or []) + \
            len((land.get("litigation") or {}).get("cases") or [])
        prop = land.get("property") or {}
        record_id = independent[0] if independent else ""
        land_evidence = ctx.evidence.add("land", land.get("land_id"), f"Land record {prop.get('survey') or ''} · {prop.get('village') or ''}".strip(" ·"),
                                         source="land_intel.land_record_detail", record_id=record_id or ctx.document_id,
                                         snapshot={"current_owner": (land.get("current_owner") or {}).get("owner"),
                                                   "area": prop.get("area"), "documents": len(land.get("documents") or []),
                                                   "independent_documents": len(independent), "register_rows": register_rows})
        if not independent and register_rows == 0:
            ctx.source_status("land", STATUS_NOT_FOUND, _SOURCE_DESCRIPTION["land"], land_id=ctx.land_id,
                              note="the only record for this parcel identity is the investigated document itself")
            ctx.finding("IDENTIFIER_MISMATCH", "MEDIUM", "No independent record for this parcel",
                        f"Records indicate that survey/khasra {ctx.survey or '—'} · {ctx.village or '—'} appears in no other document, "
                        "mutation, encumbrance or court case. The parcel identity comes from the uploaded document alone.",
                        confidence=0.85, status_label=STATUS_NOT_FOUND, evidence=[ctx.doc_evidence, land_evidence],
                        resolution=["Obtain the Jamabandi / RoR extract for the parcel and upload it as a record."],
                        code="PARCEL_NOT_FOUND")
            ctx.matches.append(_match_record("LAND_RECORD", land.get("land_id"), "Parcel identity (document only)",
                                             strong_hits=[], hard_conflicts=[], supporting_hits=[], minor_differences=[],
                                             reasons=["No independent record shares this parcel identity."],
                                             evidence_id=land_evidence))
        else:
            strong_hits = ["survey_number" if ctx.flat.get("survey_number") else "khasra_number"]
            supporting: List[str] = []
            minor: List[str] = []
            conflicts: List[str] = []
            reasons: List[str] = [f"Survey/khasra {ctx.survey} matched the land record identity."]
            # Compare against INDEPENDENT evidence: the aggregate's property view
            # may be built from the investigated document itself when it is the
            # newest record, which would make every comparison trivially agree.
            prop = _independent_reference(land, ctx.document_id, prop)
            if ctx.village and _norm_text(prop.get("village")) == _norm_text(ctx.village):
                supporting.append("village")
                reasons.append(f"Village {ctx.village} matched.")
            for key in ("tehsil", "district"):
                doc_v, rec_v = ctx.flat.get(key, ""), _text(prop.get(key))
                if doc_v and rec_v:
                    if _norm_text(doc_v) == _norm_text(rec_v):
                        supporting.append(key)
                        reasons.append(f"{key.title()} {doc_v} matched.")
                    else:
                        conflicts.append(key)
                        reasons.append(f"{key.title()} differs: document {doc_v} vs record {rec_v}.")
            record_area = _text(prop.get("area"))
            delta = _area_delta(ctx.flat.get("area"), record_area)
            if delta is not None:
                if delta <= 0.02:
                    supporting.append("area")
                    reasons.append(f"Area {ctx.flat.get('area')} matched the record.")
                elif delta <= AREA_TOLERANCE:
                    minor.append("area")
                    reasons.append(f"Area differs slightly ({delta:.0%}).")
                else:
                    conflicts.append("area")
                    reasons.append(f"Area differs by {delta:.0%}: document {ctx.flat.get('area')} vs record {record_area}.")
            owner_names = [ctx.flat.get("owner_name", ""), ctx.value("seller_name"), ctx.value("buyer_name")]
            history_owners = [_text(event.get("owner")) for event in (land.get("ownership_history") or [])
                              if _text(event.get("document_id")) != ctx.document_id]
            history_owners.append(_text((land.get("current_owner") or {}).get("owner")))
            named = [name for name in owner_names if name]
            if named and any(history_owners):
                if any(_same_owner(name, known) for name in named for known in history_owners if known):
                    supporting.append("owner_name")
                    reasons.append("A party named in the document appears in the parcel's ownership history.")
                else:
                    minor.append("owner_name")
                    reasons.append("No party named in the document appears in the parcel's ownership history (assessed under Ownership).")
            ctx.matches.append(_match_record("LAND_RECORD", land.get("land_id"), f"Land record {prop.get('survey')} · {prop.get('village')}",
                                             strong_hits=strong_hits, hard_conflicts=conflicts, supporting_hits=supporting,
                                             minor_differences=minor, reasons=reasons, evidence_id=land_evidence))
            match_class = ctx.matches[-1]["match_class"]
            ctx.source_status("land", STATUS_CONFLICT if match_class == MATCH_CONFLICTING else STATUS_FOUND,
                              _SOURCE_DESCRIPTION["land"], land_id=ctx.land_id, match_class=match_class,
                              independent_documents=len(independent), register_rows=register_rows)
            if conflicts:
                for key in conflicts:
                    if key == "area":
                        severity = "HIGH" if (delta or 0) > AREA_SEVERE else "MEDIUM"
                        ctx.finding("AREA_MISMATCH", severity, "Area differs from the land record",
                                    f"Records indicate an area of {record_area} for this parcel; the document states {ctx.flat.get('area')} "
                                    f"({delta:.0%} difference).", confidence=0.85, status_label=STATUS_CONFLICT,
                                    evidence=[ctx.doc_evidence, land_evidence],
                                    resolution=["Check whether a partition or re-survey explains the difference.",
                                                "Compare the area unit on the scan with the record's unit."])
                    else:
                        ctx.finding("IDENTIFIER_MISMATCH", "HIGH", f"{key.title()} conflicts with the land record",
                                    f"The survey/khasra number matched, but the document's {key} ({ctx.flat.get(key)}) conflicts with the "
                                    f"record's {key} ({prop.get(key)}). A same-number parcel in another {key} is possible.",
                                    confidence=0.85, status_label=STATUS_CONFLICT, evidence=[ctx.doc_evidence, land_evidence],
                                    resolution=[f"Confirm the {key} on the scan and against the Jamabandi."])
            else:
                ctx.finding("RECORD_MATCH", "INFO", f"Parcel {CERTAINTY_LABEL[match_class].lower()} in land records",
                            f"Records indicate {len(independent)} independent document(s) and {register_rows} register row(s) for "
                            f"survey/khasra {ctx.survey} · {prop.get('village') or '—'}.", confidence=ctx.matches[-1]["score"],
                            status_label=STATUS_FOUND, evidence=[land_evidence], affects=("REJECT",), code="PARCEL_MATCH")

    # controlled parcel register (mapping._resolve) — an independent parcel source
    if ctx.sources.get("parcel", {}).get("status") == STATUS_UNAVAILABLE:
        return
    status = _text(resolution.get("status")).upper()
    best = (resolution.get("matches") or [None])[0]
    if status == "INSUFFICIENT DATA" or not resolution:
        ctx.source_status("parcel", STATUS_UNKNOWN, _SOURCE_DESCRIPTION["parcel"], note="insufficient identity fields")
        return
    if not best:
        ctx.source_status("parcel", STATUS_NOT_FOUND, _SOURCE_DESCRIPTION["parcel"])
        return
    prop = best.get("property") or {}
    property_id = _text(prop.get("property_id"))
    evidence_id = ctx.evidence.add("property", property_id, f"Controlled parcel {prop.get('parcel_id') or property_id}",
                                   source="mapping.properties",
                                   snapshot={"survey_number": prop.get("survey_number"), "village": prop.get("village"),
                                             "district": prop.get("district"), "area": prop.get("area"),
                                             "data_source": prop.get("data_source")})
    matched = list(best.get("matched_fields") or [])
    conflicting = list(best.get("conflicting_fields") or [])
    strong = [field for field in matched if field in _mapping().LAND_IDENTIFIER_FIELDS]
    supporting = [field for field in matched if field not in strong]
    ctx.matches.append(_match_record("PARCEL", property_id, f"Controlled parcel {prop.get('parcel_id') or property_id}",
                                     strong_hits=strong, hard_conflicts=conflicting, supporting_hits=supporting,
                                     minor_differences=[], reasons=list(best.get("reasons") or []), evidence_id=evidence_id))
    match_class = ctx.matches[-1]["match_class"]
    ctx.source_status("parcel", STATUS_CONFLICT if match_class == MATCH_CONFLICTING else
                      (STATUS_POSSIBLE if match_class == MATCH_POSSIBLE else STATUS_FOUND),
                      _SOURCE_DESCRIPTION["parcel"], property_id=property_id, match_class=match_class,
                      resolution_status=status)
    if match_class == MATCH_CONFLICTING:
        ctx.finding("IDENTIFIER_MISMATCH", "HIGH", "Controlled parcel conflicts on supporting identifiers",
                    f"The parcel register entry {prop.get('parcel_id') or property_id} matches on {', '.join(strong)} but conflicts on "
                    f"{', '.join(conflicting)}. SA does not treat this as the same parcel.", confidence=0.8,
                    status_label=STATUS_CONFLICT, evidence=[ctx.doc_evidence, evidence_id],
                    resolution=["Confirm village/taluka/district on the scan.", "Open the parcel on the map and compare."])
    if parcel.get("linked") and _text(parcel["linked"].get("property_id")) and _text(parcel["linked"].get("property_id")) != property_id:
        ctx.finding("IDENTIFIER_MISMATCH", "MEDIUM", "Linked property differs from the best parcel candidate",
                    f"Records indicate the document was linked to property {parcel['linked'].get('property_id')} at upload, but the "
                    f"best controlled-parcel candidate now is {property_id}.", confidence=0.7, status_label=STATUS_CONFLICT,
                    evidence=[ctx.doc_evidence, evidence_id], resolution=["Review the property link in the map workspace."])


def _independent_reference(land: Dict[str, Any], document_id: str, prop: Dict[str, Any]) -> Dict[str, Any]:
    """Village / tehsil / district / area taken from the newest independent
    document on the parcel. A value no independent record carries is left
    blank (not compared) rather than echoed back from the investigated
    document through the aggregate's property view."""
    rows = [row for row in (land.get("document_rows") or []) if _text(row.get("id")) != document_id
            and _text(row.get("status")).upper() != "REJECTED"]
    rows.sort(key=lambda row: float(row.get("created_at") or 0), reverse=True)
    reference = dict(prop or {})
    for key in ("village", "tehsil", "district", "area"):
        for row in rows:
            value = _flat_fields(row.get("fields")).get(key, "")
            if value:
                reference[key] = value
                break
        else:
            reference[key] = ""
    return reference


def _stage_mutations(ctx: _Context) -> None:
    land = ctx.land or {}
    registers = ctx.registers or {}
    land_mutations = list(land.get("mutations") or [])
    doc_mutation_no = ctx.value("mutation_no")
    doc_registration_no = ctx.value("registration_no")
    doc_owner = ctx.flat.get("owner_name", "")
    seller = ctx.value("seller_name") or (doc_owner if ctx.is_transfer else "")
    buyer = ctx.value("buyer_name")
    unavailable = ctx.sources.get("registers", {}).get("status") == STATUS_UNAVAILABLE
    if unavailable and not land_mutations:
        return
    found_any = False
    number_hit = None
    for mutation in registers.get("by_mutation_no") or []:
        found_any = True
        same_land = _norm_text(mutation.get("survey_number") or mutation.get("khasra_number")) == _norm_text(ctx.survey) and \
            (not _norm_text(mutation.get("village")) or not ctx.village or _norm_text(mutation.get("village")) == _norm_text(ctx.village))
        evidence_id = ctx.evidence.add("mutation", mutation.get("id"), f"Mutation {mutation.get('mutation_no')} ({_text(mutation.get('status')).upper()})",
                                       source="land_mutations", land_id=ctx.land_id,
                                       snapshot={"previous_owner": mutation.get("previous_owner"), "new_owner": mutation.get("new_owner"),
                                                 "status": mutation.get("status"), "deed_no": mutation.get("deed_no"),
                                                 "survey_number": mutation.get("survey_number"), "village": mutation.get("village")})
        supporting, conflicts, minor = [], [], []
        reasons = [f"Mutation number {doc_mutation_no} matched the register."]
        if same_land:
            supporting.append("survey_number")
            reasons.append("The mutation is registered on the same parcel.")
        else:
            conflicts.append("survey_number")
            reasons.append(f"The mutation is registered on survey {mutation.get('survey_number')} · {mutation.get('village')}, not this parcel.")
        parties = [name for name in (doc_owner, seller, buyer) if name]
        if parties:
            if any(_same_owner(name, mutation.get("new_owner")) or _same_owner(name, mutation.get("previous_owner")) for name in parties):
                supporting.append("owner_name")
                reasons.append("A party in the document appears on the mutation.")
            else:
                conflicts.append("owner_name")
                reasons.append("None of the document's parties appear on the mutation.")
        ctx.matches.append(_match_record("MUTATION", mutation.get("id"), f"Mutation {mutation.get('mutation_no')}",
                                         strong_hits=["mutation_no"], hard_conflicts=conflicts, supporting_hits=supporting,
                                         minor_differences=minor, reasons=reasons, evidence_id=evidence_id))
        number_hit = ctx.matches[-1]
        if conflicts:
            ctx.finding("MUTATION_MISMATCH", "HIGH", f"Mutation {mutation.get('mutation_no')} does not agree with the document",
                        "Records indicate mutation " + _text(mutation.get("mutation_no")) + " exists but conflicts on " +
                        ", ".join(conflicts) + f" (register: {mutation.get('previous_owner') or '—'} → {mutation.get('new_owner') or '—'}, "
                        f"survey {mutation.get('survey_number')}).", confidence=0.85, status_label=STATUS_CONFLICT,
                        evidence=[ctx.doc_evidence, evidence_id],
                        resolution=["Open the mutation and compare parties and parcel with the scan."])
        elif _text(mutation.get("status")).upper() != "COMPLETED":
            ctx.finding("MUTATION_MISMATCH", "MEDIUM", f"Mutation {mutation.get('mutation_no')} is {_text(mutation.get('status')).upper()}",
                        f"Records indicate the mutation referenced by the document is not completed (status {_text(mutation.get('status')).upper()}). "
                        "The document may describe a transfer the register has not recorded yet.", confidence=0.9,
                        status_label=STATUS_FOUND, evidence=[evidence_id],
                        resolution=["Complete or decide the mutation through the Mutations workflow before relying on the document."])
        else:
            ctx.finding("RECORD_MATCH", "INFO", f"Mutation {mutation.get('mutation_no')} confirmed",
                        f"Records indicate a COMPLETED mutation matching the document ({mutation.get('previous_owner')} → {mutation.get('new_owner')}).",
                        confidence=0.95, status_label=STATUS_FOUND, evidence=[evidence_id], affects=("REJECT",), code="MUTATION_MATCH")
    if doc_mutation_no and not found_any and not unavailable:
        ctx.finding("MUTATION_MISMATCH", "MEDIUM", f"Mutation {doc_mutation_no} not found",
                    f"The document cites mutation {doc_mutation_no}; records indicate no such entry in the mutation register. "
                    "NOT FOUND IN SEARCHED SOURCES — the register may be incomplete.", confidence=0.75,
                    status_label=STATUS_NOT_FOUND, evidence=[ctx.doc_evidence],
                    resolution=["Ask the applicant for the mutation order and register it via Mutations."])
    # deed number (registration number) recorded on a mutation
    for mutation in registers.get("by_deed_no") or []:
        evidence_id = ctx.evidence.add("mutation", mutation.get("id"), f"Mutation {mutation.get('mutation_no')} ({_text(mutation.get('status')).upper()})",
                                       source="land_mutations", land_id=ctx.land_id,
                                       snapshot={"deed_no": mutation.get("deed_no"), "new_owner": mutation.get("new_owner"),
                                                 "previous_owner": mutation.get("previous_owner")})
        parties = [name for name in (buyer, doc_owner) if name]
        if parties and not any(_same_owner(name, mutation.get("new_owner")) for name in parties):
            ctx.finding("MUTATION_MISMATCH", "HIGH", f"Deed {doc_registration_no} mutated to a different owner",
                        f"Records indicate mutation {mutation.get('mutation_no')} was recorded for deed {doc_registration_no} in favour of "
                        f"{mutation.get('new_owner') or '—'}, which does not match the document's transferee/owner.", confidence=0.85,
                        status_label=STATUS_CONFLICT, evidence=[ctx.doc_evidence, evidence_id],
                        resolution=["Compare the deed copy with the mutation order."])
        else:
            ctx.matches.append(_match_record("MUTATION", mutation.get("id"), f"Mutation {mutation.get('mutation_no')} (deed number)",
                                             strong_hits=["registration_no"], hard_conflicts=[], supporting_hits=["owner_name"] if parties else [],
                                             minor_differences=[], reasons=[f"Deed number {doc_registration_no} recorded on the mutation."],
                                             evidence_id=evidence_id))
    # parcel-level mutations (from the authoritative land aggregate)
    seen_ids = {item["record_id"] for item in ctx.matches if item["source"] == "MUTATION"}
    for mutation in land_mutations:
        if _text(mutation.get("id")) in seen_ids:
            continue
        evidence_id = ctx.evidence.add("mutation", mutation.get("id"), f"Mutation {mutation.get('mutation_no')} ({_text(mutation.get('status')).upper()})",
                                       source="land_mutations", land_id=ctx.land_id,
                                       snapshot={"previous_owner": mutation.get("previous_owner"), "new_owner": mutation.get("new_owner"),
                                                 "status": mutation.get("status"), "deed_date": mutation.get("deed_date")})
        supporting = ["survey_number"]
        parties = [name for name in (doc_owner, seller, buyer) if name]
        if parties and any(_same_owner(name, mutation.get("new_owner")) or _same_owner(name, mutation.get("previous_owner")) for name in parties):
            supporting.append("owner_name")
        ctx.matches.append(_match_record("MUTATION", mutation.get("id"), f"Mutation {mutation.get('mutation_no')}",
                                         strong_hits=[], hard_conflicts=[], supporting_hits=supporting + (["village"] if ctx.village else []),
                                         minor_differences=[], reasons=["Registered on the matched parcel (no mutation number in the document)."],
                                         evidence_id=evidence_id))
    mutation_matches = [item for item in ctx.matches if item["source"] == "MUTATION"]
    if not land_mutations and not (registers.get("by_mutation_no") or registers.get("by_deed_no")):
        ctx.source_status("registers", STATUS_UNAVAILABLE if unavailable else STATUS_NOT_FOUND, _SOURCE_DESCRIPTION["registers"],
                          searched_for=[value for value in (doc_mutation_no, doc_registration_no, ctx.land_id) if value])
    else:
        ctx.source_status("registers", STATUS_FOUND, _SOURCE_DESCRIPTION["registers"], count=len(mutation_matches),
                          match_class=_best_class(mutation_matches) if mutation_matches else MATCH_NONE)


def _stage_court(ctx: _Context) -> None:
    court = ctx.court or {}
    unavailable = ctx.sources.get("court", {}).get("status") == STATUS_UNAVAILABLE
    if unavailable:
        return
    doc_case = ctx.value("case_number")
    wanted = _norm_code(doc_case)
    parties_in_doc = [name for name in (ctx.flat.get("owner_name", ""), ctx.value("seller_name"), ctx.value("buyer_name")) if name]
    seen: set = set()
    applied: List[Dict[str, Any]] = []
    for case in list(court.get("survey_cases") or []) + list(court.get("by_number") or []):
        case_id = _text(case.get("id"))
        if case_id in seen:
            continue
        seen.add(case_id)
        case_village = _norm_text(case.get("village"))
        same_survey = _norm_text(case.get("survey_number") or case.get("khasra_number")) == _norm_text(ctx.survey) and bool(ctx.survey)
        village_agrees = not case_village or not ctx.village or case_village == _norm_text(ctx.village)
        number_agrees = bool(wanted) and _norm_code(case.get("case_number")) == wanted
        evidence_id = ctx.evidence.add("court_case", case_id, f"Court case {case.get('case_number')} ({_text(case.get('status')).upper()})",
                                       source="land_court_cases", survey=case.get("survey_number"), village=case.get("village"),
                                       snapshot={"court_name": case.get("court_name"), "parties": case.get("parties"),
                                                 "status": case.get("status"), "filed_date": case.get("filed_date"),
                                                 "survey_number": case.get("survey_number"), "village": case.get("village")})
        strong, supporting, conflicts, minor, reasons = [], [], [], [], []
        if number_agrees:
            strong.append("case_number")
            reasons.append(f"Case number {doc_case} matched the litigation register.")
        if same_survey:
            (strong if not number_agrees else supporting).append("survey_number")
            reasons.append("Same survey/khasra number as the document.")
            if case_village and ctx.village:
                if village_agrees:
                    supporting.append("village")
                    reasons.append("Same village.")
                else:
                    (conflicts if number_agrees else minor).append("village")
                    reasons.append(f"Different village on the case ({case.get('village')}) — possible match only.")
            elif not case_village:
                reasons.append("The case did not record a village; register rule applies it to the survey.")
        elif number_agrees:
            conflicts.append("survey_number")
            reasons.append(f"The case is registered on survey {case.get('survey_number')} · {case.get('village')}, not this parcel.")
        party_hit = any(_norm_text(name) and _norm_text(name) in _norm_text(case.get("parties")) for name in parties_in_doc)
        if party_hit:
            supporting.append("parties")
            reasons.append("A party named in the document appears in the case parties.")
        applies = same_survey and village_agrees
        match = _match_record("COURT_CASE", case_id, f"Court case {case.get('case_number')}", strong_hits=strong, hard_conflicts=conflicts,
                              supporting_hits=supporting, minor_differences=minor, reasons=reasons, evidence_id=evidence_id, applies=applies)
        if not applies and match["match_class"] in {MATCH_EXACT, MATCH_STRONG}:
            match["match_class"] = MATCH_POSSIBLE
            match["certainty"] = CERTAINTY_LABEL[MATCH_POSSIBLE]
        ctx.matches.append(match)
        active = _text(case.get("status")).upper() == "ACTIVE"
        if match["match_class"] == MATCH_CONFLICTING:
            ctx.finding("IDENTIFIER_MISMATCH", "HIGH", f"Case {case.get('case_number')} is registered on a different parcel",
                        f"The document cites case {doc_case}; records indicate that case concerns survey {case.get('survey_number')} · "
                        f"{case.get('village') or '—'}, not this parcel.", confidence=0.8, status_label=STATUS_CONFLICT,
                        evidence=[ctx.doc_evidence, evidence_id], resolution=["Open the court case and confirm the parcel it concerns."])
            continue
        if not applies:
            ctx.finding("COURT_CASE_OVERLAP", "LOW", f"Possible match: case {case.get('case_number')} on the same survey number in another village",
                        f"Possible match only — records indicate case {case.get('case_number')} ({_text(case.get('status')).upper()}) on survey "
                        f"{case.get('survey_number')} in village {case.get('village') or '—'}. The village differs, so SA does not apply it to this parcel.",
                        confidence=0.4, status_label=STATUS_POSSIBLE, evidence=[evidence_id],
                        why="Same-number parcels exist in different villages; a village mismatch keeps this a POSSIBLE match.",
                        resolution=["Confirm the village on the case record; if it is this parcel, update the case."], affects=())
            continue
        applied.append(case)
        if active:
            severity = "HIGH"
            description = (f"Records indicate an ACTIVE court case {case.get('case_number')} ({case.get('court_name') or 'court not recorded'}) "
                           f"on this parcel; parties: {case.get('parties') or '—'}.")
            if ctx.is_transfer:
                description += " The document is a transfer instrument; a transfer during pending litigation requires the case status and any stay order to be verified first."
            ctx.finding("COURT_CASE_OVERLAP", severity, f"Active litigation on the parcel — case {case.get('case_number')}", description,
                        confidence=match["score"], status_label=STATUS_FOUND, evidence=[evidence_id],
                        why="The litigation register lists an ACTIVE case whose survey/khasra (and village, where recorded) matches the document.",
                        resolution=["Verify the case status with the court record.", "Check for a stay/injunction before any transfer decision.",
                                    "If the case is decided, close it in the register with the decision summary."])
            if party_hit:
                ctx.finding("PARTY_RELATIONSHIP", "HIGH", "A document party is a litigant in the active case",
                            f"Records indicate that a party named in the document appears among the parties of active case {case.get('case_number')}.",
                            confidence=0.7, status_label=STATUS_FOUND, evidence=[ctx.doc_evidence, evidence_id],
                            resolution=["Confirm whether the party's claim in the case affects this transaction."])
        else:
            ctx.finding("COURT_CASE_OVERLAP", "INFO", f"Closed case {case.get('case_number')} on the parcel",
                        f"Records indicate case {case.get('case_number')} was {_text(case.get('status')).lower() or 'closed'}"
                        + (f": {case.get('decision_summary')}" if _text(case.get("decision_summary")) else ".") ,
                        confidence=match["score"], status_label=STATUS_FOUND, evidence=[evidence_id], affects=())
    if doc_case and not (court.get("by_number")):
        ctx.finding("COURT_CASE_OVERLAP", "INFO", f"Case {doc_case} cited by the document was not found",
                    f"The document mentions case {doc_case}; records indicate no such case in the litigation register — NOT FOUND IN SEARCHED SOURCES.",
                    confidence=0.7, status_label=STATUS_NOT_FOUND, evidence=[ctx.doc_evidence],
                    resolution=["Register the case with its court record if it concerns this parcel."], affects=())
    case_matches = [item for item in ctx.matches if item["source"] == "COURT_CASE"]
    if not case_matches:
        ctx.source_status("court", STATUS_NOT_FOUND if ctx.survey else STATUS_UNKNOWN, _SOURCE_DESCRIPTION["court"],
                          searched_for=[value for value in (ctx.survey, doc_case) if value])
    else:
        ctx.source_status("court", STATUS_FOUND, _SOURCE_DESCRIPTION["court"], count=len(case_matches), applied=len(applied),
                          active=sum(1 for case in applied if _text(case.get("status")).upper() == "ACTIVE"),
                          match_class=_best_class(case_matches))


def _stage_registry(ctx: _Context) -> None:
    registry = ctx.registry or {}
    if ctx.sources.get("registry", {}).get("status") == STATUS_UNAVAILABLE:
        return
    doc_registration = ctx.value("registration_no")
    doc_parties = [name for name in (ctx.flat.get("owner_name", ""), ctx.value("seller_name"), ctx.value("buyer_name")) if name]
    hits = 0
    for other in registry.get("same_registration") or []:
        hits += 1
        flat = other.get("flat") or {}
        same_parcel = _norm_text(flat.get("survey_number") or flat.get("khasra_number")) == _norm_text(ctx.survey) and \
            (_norm_text(flat.get("village")) == _norm_text(ctx.village) or not flat.get("village") or not ctx.village)
        other_parties = [flat.get("owner_name", ""), flat.get("seller_name", ""), flat.get("buyer_name", "")]
        parties_agree = bool(doc_parties) and any(_same_owner(a, b) for a in doc_parties for b in other_parties if b)
        approved = _text(other.get("status")).upper() in {"APPROVED", "VERIFIED", "AUTO_APPROVED"}
        evidence_id = ctx.evidence.add("registry", other.get("id"), f"Registry copy {other.get('filename') or other.get('id')} ({_text(other.get('status')).upper()})",
                                       source="documents", snapshot={"registration_no": flat.get("registration_no"), "owner_name": flat.get("owner_name"),
                                                                     "survey_number": flat.get("survey_number"), "village": flat.get("village"),
                                                                     "status": other.get("status"), "doc_type": other.get("doc_type")})
        conflicts = [] if same_parcel else ["survey_number"]
        supporting = ["owner_name"] if parties_agree else []
        if same_parcel:
            supporting.append("survey_number")
        ctx.matches.append(_match_record("REGISTRY", other.get("id"), f"Registry copy {other.get('id')}", strong_hits=["registration_no"],
                                         hard_conflicts=conflicts, supporting_hits=supporting, minor_differences=[] if parties_agree or not doc_parties else ["owner_name"],
                                         reasons=[f"Registration number {doc_registration} also appears on document {other.get('id')}."],
                                         evidence_id=evidence_id))
        if not same_parcel:
            ctx.finding("IDENTIFIER_MISMATCH", "HIGH", f"Registration {doc_registration} belongs to a different parcel",
                        f"Records indicate registration number {doc_registration} on document {other.get('id')} for survey "
                        f"{flat.get('survey_number') or flat.get('khasra_number') or '—'} · {flat.get('village') or '—'}, not this parcel.",
                        confidence=0.8, status_label=STATUS_CONFLICT, evidence=[ctx.doc_evidence, evidence_id],
                        resolution=["Compare both registry copies; one registration number cannot cover two parcels."])
        elif doc_parties and not parties_agree:
            ctx.finding("DUPLICATE_TRANSACTION", "CRITICAL" if approved else "HIGH",
                        f"Registration {doc_registration} already recorded with different parties",
                        f"Records indicate an {'approved ' if approved else ''}registry copy ({other.get('id')}) with the same registration number but "
                        f"different parties ({flat.get('owner_name') or '—'}). Two transactions under one registration number is a contradiction.",
                        confidence=0.85, status_label=STATUS_CONFLICT, evidence=[ctx.doc_evidence, evidence_id],
                        why="One registration number must identify one registered instrument; a second copy with other parties is a duplicate-transaction signal.",
                        resolution=["Obtain the certified copy from the Sub-Registrar and compare parties.", "Escalate if the earlier record is approved."])
        else:
            ctx.finding("DUPLICATE_TRANSACTION", "MEDIUM" if not approved else "LOW", f"Registration {doc_registration} already screened",
                        f"Records indicate document {other.get('id')} ({_text(other.get('status')).upper()}) carries the same registration number and parties. "
                        "This upload may be a repeated copy of the same instrument.", confidence=0.8, status_label=STATUS_FOUND,
                        evidence=[evidence_id], resolution=["Decide whether the earlier record already covers this instrument."], affects=())
    for other in registry.get("same_content") or []:
        hits += 1
        approved = _text(other.get("status")).upper() in {"APPROVED", "VERIFIED", "AUTO_APPROVED"}
        evidence_id = ctx.evidence.add("document", other.get("id"), f"Identical content: {other.get('filename') or other.get('id')} ({_text(other.get('status')).upper()})",
                                       source="documents", snapshot={"status": other.get("status"), "investigation_id": other.get("investigation_id")})
        ctx.finding("DUPLICATE_TRANSACTION", "HIGH" if approved else "MEDIUM", "Identical document content already exists",
                    f"Records indicate document {other.get('id')} has byte-identical content (same fingerprint) and was already investigated as "
                    f"{other.get('investigation_id')}.", confidence=0.95, status_label=STATUS_FOUND, evidence=[ctx.doc_evidence, evidence_id],
                    why="The content fingerprint of this upload equals another screened document's fingerprint.",
                    resolution=["Open the earlier record and decide which copy is authoritative."])
    if not hits:
        ctx.source_status("registry", STATUS_NOT_FOUND if doc_registration else STATUS_UNKNOWN, _SOURCE_DESCRIPTION["registry"],
                          note="" if doc_registration else "no registration number to search with")
    else:
        ctx.source_status("registry", STATUS_FOUND, _SOURCE_DESCRIPTION["registry"], count=hits)


def _stage_ownership(ctx: _Context) -> None:
    land = ctx.land
    doc_owner = ctx.flat.get("owner_name", "")
    seller = ctx.value("seller_name")
    buyer = ctx.value("buyer_name")
    # party relationship inside the document
    if seller and buyer and _same_owner(seller, buyer):
        ctx.finding("PARTY_RELATIONSHIP", "CRITICAL", "Seller and buyer appear to be the same person",
                    f"SA identified that the transferor ({seller}) and the transferee ({buyer}) resolve to the same name.",
                    confidence=_owner_similarity(seller, buyer).get("confidence", 0.8), status_label=STATUS_CONFLICT,
                    evidence=[ctx.doc_evidence], resolution=["Verify both parties' identity documents."])
    if not land:
        return
    history = [event for event in (land.get("ownership_history") or []) if _text(event.get("document_id")) != ctx.document_id]
    current = _text((land.get("current_owner") or {}).get("owner"))
    latest_independent = None
    for event in reversed(history):
        if _text(event.get("owner")):
            latest_independent = event
            break
    if latest_independent:
        current = _text(latest_independent.get("owner"))
    if not current:
        ctx.source_status("ownership", STATUS_NOT_FOUND, "ownership history (documents + completed mutations)")
        return
    from_mutation = latest_independent is not None and latest_independent.get("kind") == "MUTATION"
    from_approved = latest_independent is not None and _text(latest_independent.get("status")).upper() in {"APPROVED", "VERIFIED", "AUTO_APPROVED", "COMPLETED"}
    ref_kind = "mutation" if from_mutation else "document"
    ref_id = latest_independent.get("mutation_id") if from_mutation else (latest_independent or {}).get("document_id")
    ref_label = (f"Mutation {latest_independent.get('mutation_no')}" if from_mutation else
                 f"{(latest_independent or {}).get('doc_type') or 'Record'} {(latest_independent or {}).get('document_id')}")
    ref_evidence = ctx.evidence.add(ref_kind, ref_id, f"{ref_label} — owner {current}", source="ownership_history", land_id=ctx.land_id,
                                    snapshot={"owner": current, "year": (latest_independent or {}).get("year"),
                                              "status": (latest_independent or {}).get("status")})
    claimant = seller if (ctx.is_transfer and seller) else doc_owner
    role = "transferor (seller)" if (ctx.is_transfer and seller) else "recorded owner"
    similarity = _owner_similarity(claimant, current) if claimant else {"match": False, "confidence": 0.0}
    ctx.source_status("ownership", STATUS_FOUND, "ownership history (documents + completed mutations)", current_owner=current,
                      basis=ref_label, events=len(history))
    if not claimant:
        ctx.finding("OWNERSHIP_MISMATCH", "MEDIUM", "Document names no owner to compare",
                    f"Records indicate the current recorded owner is {current} ({ref_label}); the document does not name an owner/transferor.",
                    confidence=0.8, status_label=STATUS_UNKNOWN, evidence=[ctx.doc_evidence, ref_evidence],
                    resolution=["Extract or enter the owner/transferor name from the scan."], code="OWNER_NOT_EXTRACTED")
        return
    if similarity.get("match"):
        ctx.finding("OWNERSHIP_CONSISTENT", "INFO", f"Document {role} matches the recorded owner",
                    f"Records indicate the current recorded owner is {current} ({ref_label}); the document's {role} {claimant} matches "
                    f"({similarity.get('method')}, {similarity.get('confidence', 0):.0%}).", confidence=similarity.get("confidence", 0.8),
                    status_label=STATUS_FOUND, evidence=[ctx.doc_evidence, ref_evidence], affects=("REJECT",))
    else:
        if ctx.is_transfer and buyer and _same_owner(buyer, current):
            ctx.finding("OWNERSHIP_CONSISTENT", "INFO", "Transferee already recorded as owner",
                        f"Records indicate {current} is already the recorded owner ({ref_label}); the transfer described by the document may already be mutated.",
                        confidence=0.7, status_label=STATUS_FOUND, evidence=[ctx.doc_evidence, ref_evidence], affects=("REJECT",))
            return
        # A mutation still in progress that transfers the parcel TO the
        # claimant explains the difference: report it as pending, not as a
        # proven contradiction (POSSIBLE, never upgraded to fact).
        pending = [mutation for mutation in (land.get("mutations") or [])
                   if _text(mutation.get("status")).upper() not in {"COMPLETED", "REJECTED"}
                   and _same_owner(claimant, mutation.get("new_owner")) and _same_owner(current, mutation.get("previous_owner"))]
        if pending and not ctx.is_transfer:
            mutation = pending[0]
            mutation_evidence = ctx.evidence.add("mutation", mutation.get("id"),
                                                 f"Mutation {mutation.get('mutation_no')} ({_text(mutation.get('status')).upper()})",
                                                 source="land_mutations", land_id=ctx.land_id,
                                                 snapshot={"previous_owner": mutation.get("previous_owner"), "new_owner": mutation.get("new_owner"),
                                                           "status": mutation.get("status")})
            ctx.finding("OWNERSHIP_MISMATCH", "MEDIUM", "Document owner depends on a mutation that is not completed",
                        f"Records indicate the current recorded owner is {current} ({ref_label}); the document names {claimant}. "
                        f"Mutation {mutation.get('mutation_no')} ({_text(mutation.get('status')).upper()}) would transfer the parcel to "
                        f"{claimant}, but it is not completed, so the document's ownership claim is POSSIBLE, not confirmed.",
                        confidence=0.8, status_label=STATUS_POSSIBLE, evidence=[ctx.doc_evidence, ref_evidence, mutation_evidence],
                        why="An in-progress mutation explains the owner difference; SA does not treat a pending register entry as fact.",
                        resolution=["Decide the pending mutation through the Mutations workflow, then run a new investigation."],
                        code="OWNERSHIP_PENDING_MUTATION")
        else:
            severity = "CRITICAL" if (from_mutation or from_approved) and similarity.get("confidence", 0) < 0.5 else "HIGH"
            ctx.finding("OWNERSHIP_MISMATCH", severity, f"Document {role} differs from the recorded owner",
                        f"Records indicate the current recorded owner is {current} ({ref_label}); the document names {claimant} as {role}. "
                        f"Name similarity {similarity.get('confidence', 0):.0%} — treated as a CONFLICT, not a spelling variant.",
                        confidence=round(1 - float(similarity.get("confidence", 0) or 0), 2), status_label=STATUS_CONFLICT,
                        evidence=[ctx.doc_evidence, ref_evidence],
                        why=("The latest independent ownership evidence is a " + ("completed mutation" if from_mutation else
                             ("verified record" if from_approved else "screened record")) + " that names a different owner."),
                        resolution=["Check for an intermediate transfer (sale, inheritance, court decree) not yet in the register.",
                                    "If a mutation exists on paper, register it via Mutations and re-run the investigation."])
    # existing ownership-history analysis (mapping) folded in as findings
    analysis = land.get("ownership_analysis") or {}
    for item in analysis.get("findings") or []:
        code = _text(item.get("type") or item.get("finding_type") or item.get("code")).upper()
        mapped = _OWNERSHIP_FINDING_MAP.get(code)
        if not mapped or code == "TRANSFER_SUPPORTED":
            continue
        finding_type, severity = mapped
        refs = [_text(ref) for ref in (item.get("evidence") or item.get("document_ids") or []) if _text(ref)]
        evidence_ids = [ctx.evidence.add("document", ref, f"Record {ref}", source="documents") for ref in refs[:6]]
        ctx.finding(finding_type, severity, _text(item.get("title") or code.replace("_", " ").title()),
                    "Ownership-history analysis: " + _text(item.get("reason") or item.get("detail") or code),
                    confidence=0.7, status_label=STATUS_CONFLICT if severity in {"HIGH", "MEDIUM"} else STATUS_FOUND,
                    evidence=evidence_ids or [ctx.doc_evidence], origin="OWNERSHIP_ANALYSIS", code=code,
                    resolution=["Review the referenced records side by side in the Comparison workspace."])


def _stage_risk_and_timeline(ctx: _Context) -> None:
    land = ctx.land
    risk = (land or {}).get("risk") or {}
    for flag in risk.get("flags") or []:
        code = _text(flag.get("code")).upper()
        mapped = _RISK_FLAG_MAP.get(code, ("RISK_SIGNAL", None))
        finding_type, severity = mapped
        severity = severity or _RISK_SEVERITY.get(_text(flag.get("severity")).upper(), "MEDIUM")
        evidence_ids = []
        for item in flag.get("evidence") or []:
            kind = _text(item.get("type"))
            if kind in {"document", "mutation", "encumbrance", "court_case"}:
                evidence_ids.append(ctx.evidence.add(kind, item.get("ref"), _text(item.get("label")) or f"{kind} {item.get('ref')}",
                                                     source="land_intel.compute_land_risk", land_id=ctx.land_id))
        # The same signal SA already raised from its own rules is reported once:
        # the engine's flag is kept for transparency but counted as corroboration.
        already = [item for item in ctx.findings if item["type"] == finding_type and item["origin"] == "SA_RULE"
                   and SEVERITY_RANK.get(item["severity"], 0) >= SEVERITY_RANK.get(severity, 0)]
        if already:
            ctx.finding(finding_type, "INFO", "Land-risk engine corroborates: " + (_text(flag.get("title")) or code.replace("_", " ").title()),
                        "Land-risk engine: " + _text(flag.get("detail")), confidence=0.85, status_label=STATUS_FOUND,
                        evidence=evidence_ids or [ctx.doc_evidence], origin="LAND_RISK_ENGINE", code=code,
                        why=(f"The existing land-risk engine raised {code} ({_text(flag.get('severity')).upper()}); SA already reported the same "
                             f"condition as {already[0]['finding_id']}, so it is counted once and shown here as corroboration."),
                        resolution=["See " + already[0]["finding_id"] + "."], affects=())
            continue
        ctx.finding(finding_type, severity, _text(flag.get("title")) or code.replace("_", " ").title(),
                    "Land-risk engine: " + _text(flag.get("detail")), confidence=0.85 if severity in ALERT_SEVERITIES else 0.7,
                    status_label=STATUS_FOUND, evidence=evidence_ids or [ctx.doc_evidence], origin="LAND_RISK_ENGINE", code=code,
                    why=f"The existing deterministic land-risk engine raised {code} for this parcel; SA reports it unchanged.",
                    resolution=["Open the parcel's Risk Review for the engine's own evidence list."],
                    affects=("APPROVE",) if severity != "INFO" else ())
    # document-level timeline checks
    doc_date = _parse_date(ctx.flat.get("document_date")) or _parse_date(ctx.value("registration_date"))
    today = time.strftime("%Y-%m-%d")
    if doc_date and doc_date > today:
        ctx.finding("TIMELINE_ANOMALY", "HIGH", "Document is dated in the future",
                    f"The document date {doc_date} is later than today ({today}).", confidence=0.9, status_label=STATUS_CONFLICT,
                    evidence=[ctx.doc_evidence], resolution=["Check the date on the scan; correct the field if OCR misread it."])
    reg_date = _parse_date(ctx.value("registration_date"))
    if doc_date and reg_date and reg_date < doc_date:
        ctx.finding("TIMELINE_ANOMALY", "MEDIUM", "Registration date precedes the document date",
                    f"The document states registration on {reg_date} but is dated {doc_date}.", confidence=0.8,
                    status_label=STATUS_CONFLICT, evidence=[ctx.doc_evidence], resolution=["Confirm both dates on the scan."])
    if land and ctx.is_transfer and doc_date:
        for mutation in land.get("mutations") or []:
            if _text(mutation.get("status")).upper() != "COMPLETED":
                continue
            deed_date = _parse_date(mutation.get("deed_date"))
            seller = ctx.value("seller_name") or ctx.flat.get("owner_name", "")
            if deed_date and seller and _same_owner(seller, mutation.get("new_owner")) and doc_date < deed_date:
                evidence_id = ctx.evidence.add("mutation", mutation.get("id"), f"Mutation {mutation.get('mutation_no')} (COMPLETED)",
                                               source="land_mutations", land_id=ctx.land_id)
                ctx.finding("TIMELINE_ANOMALY", "HIGH", "Transfer dated before the transferor acquired the parcel",
                            f"Records indicate {mutation.get('new_owner')} became owner by mutation {mutation.get('mutation_no')} (deed {deed_date}); "
                            f"the document transfers from that person on {doc_date}, which is earlier.", confidence=0.8,
                            status_label=STATUS_CONFLICT, evidence=[ctx.doc_evidence, evidence_id],
                            resolution=["Compare the deed date with the mutation's deed date."])
    # unified chronological timeline: existing register timeline + this document + investigation
    events: List[Dict[str, Any]] = []
    for item in (land or {}).get("timeline") or []:
        if item.get("kind") == "CURRENT":
            continue
        refs = item.get("refs") or {}
        kind_map = {"DOCUMENT": ("document", refs.get("document_id")), "MUTATION": ("mutation", refs.get("mutation_id")),
                    "ENCUMBRANCE": ("encumbrance", refs.get("encumbrance_id")), "COURT_CASE": ("court_case", refs.get("case_id"))}
        kind, ref = kind_map.get(_text(item.get("kind")), (None, None))
        is_self = kind == "document" and _text(ref) == ctx.document_id
        events.append({
            "kind": item.get("kind"), "date": item.get("date") or "", "year": item.get("year"), "title": item.get("title"),
            "detail": item.get("detail"), "status": item.get("status"), "source": "land_intel.build_timeline",
            "refs": refs, "links": _links_for(kind, ref, land_id=ctx.land_id, survey=ctx.survey, village=ctx.village) if kind and ref else [],
            "is_investigated_document": is_self, "certainty": "Confirmed" if not is_self else "Under investigation",
        })
    if not any(event.get("is_investigated_document") for event in events):
        events.append({"kind": "DOCUMENT", "date": doc_date or "", "year": _year_of(doc_date or ctx.flat.get("document_date") or ctx.flat.get("khatauni_year")),
                       "title": f"{ctx.doc_type} under investigation", "detail": f"Document {ctx.document_id} ({ctx.document.get('filename')})",
                       "status": _text(ctx.document.get("status")).upper(), "source": "documents", "refs": {"document_id": ctx.document_id},
                       "links": _links_for("document", ctx.document_id), "is_investigated_document": True, "certainty": "Under investigation"})
    events.append({"kind": "INVESTIGATION", "date": time.strftime("%Y-%m-%d", time.gmtime(ctx.snapshot_at)), "year": int(time.strftime("%Y", time.gmtime(ctx.snapshot_at))),
                   "title": f"SA investigation {ctx.investigation_id}", "detail": "Sources read at this time (data snapshot).", "status": "SNAPSHOT",
                   "source": "sa_investigation", "refs": {"investigation_id": ctx.investigation_id}, "links": _links_for("investigation", ctx.investigation_id),
                   "is_investigated_document": False, "certainty": "Confirmed"})
    events.sort(key=lambda event: ((event.get("year") is None), event.get("year") or 0, event.get("date") or "", event.get("title") or ""))
    ctx.timeline = events


# ---------------------------------------------------------------------------
# 5) scenarios, confidences, recommendation (explainable arithmetic)
# ---------------------------------------------------------------------------


def _count(findings: Sequence[Dict[str, Any]], severity: str) -> int:
    return sum(1 for item in findings if item["severity"] == severity)


def _parcel_class(ctx: _Context) -> str:
    land_matches = [item for item in ctx.matches if item["source"] in {"LAND_RECORD", "PARCEL"}]
    return _best_class(land_matches) if land_matches else MATCH_NONE


def _confidences(ctx: _Context) -> Dict[str, Any]:
    def source_conf(source: str, label: str) -> Dict[str, Any]:
        matches = [item for item in ctx.matches if item["source"] == source]
        status = ctx.sources.get({"COURT_CASE": "court", "MUTATION": "registers"}.get(source, source.lower()), {}).get("status")
        if status == STATUS_UNAVAILABLE:
            return {"value": None, "label": STATUS_UNAVAILABLE, "explanation": f"{label} could not be searched."}
        if not matches:
            return {"value": None, "label": STATUS_NOT_FOUND if status != STATUS_UNKNOWN else STATUS_UNKNOWN,
                    "explanation": f"No {label.lower()} matched the document's identifiers in the searched sources."}
        best = _best_class(matches)
        score = max(item["score"] for item in matches)
        return {"value": round(score, 2), "label": CERTAINTY_LABEL[best],
                "explanation": f"Best {label.lower()} match class is {best}: " + "; ".join(matches[0]["reasons"][:3])}

    parcel_matches = [item for item in ctx.matches if item["source"] in {"LAND_RECORD", "PARCEL"}]
    parcel_class = _parcel_class(ctx)
    land_status = ctx.sources.get("land", {}).get("status")
    if land_status == STATUS_UNKNOWN:
        parcel = {"value": None, "label": "Unknown", "explanation": "The document carries no parcel identifier, so no parcel search was possible."}
    elif land_status == STATUS_UNAVAILABLE and not parcel_matches:
        parcel = {"value": None, "label": STATUS_UNAVAILABLE, "explanation": "The land record aggregate could not be read."}
    else:
        parcel = {"value": round(max((item["score"] for item in parcel_matches), default=0.0), 2), "label": CERTAINTY_LABEL[parcel_class],
                  "explanation": ("Parcel match class " + parcel_class + ": " + "; ".join(parcel_matches[0]["reasons"][:3])) if parcel_matches
                  else "No parcel could be matched (see findings)."}
    ownership_findings = [item for item in ctx.findings if item["type"] in {"OWNERSHIP_CONSISTENT", "OWNERSHIP_MISMATCH"} and item["origin"] == "SA_RULE"]
    if ownership_findings:
        consistent = [item for item in ownership_findings if item["type"] == "OWNERSHIP_CONSISTENT"]
        ownership = {"value": round(consistent[0]["confidence"], 2) if consistent else round(1 - ownership_findings[0]["confidence"], 2),
                     "label": "Consistent" if consistent else "Conflict", "explanation": ownership_findings[0]["description"]}
    else:
        ownership = {"value": None, "label": STATUS_UNKNOWN, "explanation": "No independent ownership evidence was found to compare with."}
    court = source_conf("COURT_CASE", "Court case")
    mutation = source_conf("MUTATION", "Mutation")
    unavailable = [name for name, item in ctx.sources.items() if item.get("status") == STATUS_UNAVAILABLE]
    components = [("parcel", 0.45, parcel["value"] if parcel["value"] is not None else 0.0), ("ownership", 0.25, ownership["value"]),
                  ("mutation", 0.15, mutation["value"] if mutation["value"] is not None else (0.7 if not unavailable else None)),
                  ("court", 0.15, court["value"] if court["value"] is not None else (0.7 if not unavailable else None))]
    weight_sum = sum(weight for _name, weight, value in components if value is not None)
    overall = round(sum(weight * value for _name, weight, value in components if value is not None) / weight_sum, 2) if weight_sum else 0.0
    overall = round(max(0.0, overall - 0.1 * len(unavailable)), 2)
    return {
        "parcel_match": parcel,
        "ownership": ownership,
        "court_case_match": court,
        "mutation_match": mutation,
        "overall_evidence": {"value": overall, "label": "Overall evidence confidence",
                             "explanation": "Weighted mean of the available components (parcel 0.45, ownership 0.25, mutation 0.15, court 0.15); "
                                            "a source that returned nothing counts 0.70 when it was searched, and each unavailable source subtracts 0.10. "
                                            "This is evidence quality, not a probability that the document is genuine."},
        "unavailable_sources": unavailable,
    }


def _build_scenarios(ctx: _Context) -> List[Dict[str, Any]]:
    findings = ctx.findings
    critical = [item for item in findings if item["severity"] == "CRITICAL"]
    high = [item for item in findings if item["severity"] == "HIGH"]
    medium = [item for item in findings if item["severity"] == "MEDIUM"]
    unavailable = [name for name, item in ctx.sources.items() if item.get("status") == STATUS_UNAVAILABLE]
    possible = [item for item in ctx.matches if item["match_class"] == MATCH_POSSIBLE]
    parcel_class = _parcel_class(ctx)
    affected = sorted({item["record_id"] for item in ctx.matches if item["record_id"]} | ({ctx.land_id} if ctx.land_id else set()))
    supports_approve = [item["finding_id"] for item in findings if "REJECT" in item["affects"] and item["severity"] == "INFO"]
    contradicts_approve = [item["finding_id"] for item in findings if "APPROVE" in item["affects"] and item["severity"] != "INFO"]
    uncertainties = [f"{name}: {STATUS_UNAVAILABLE}" for name in unavailable]
    uncertainties += [f"{item['label']}: POSSIBLE match only" for item in possible]
    uncertainties += [f"{item['title']}: {item['status_label']}" for item in findings if item["status_label"] in {STATUS_NOT_FOUND, STATUS_UNKNOWN}]

    base = {MATCH_EXACT: 0.9, MATCH_STRONG: 0.75, MATCH_POSSIBLE: 0.4, MATCH_CONFLICTING: 0.2, MATCH_NONE: 0.25}[parcel_class]
    approve_score = base - 0.4 * len(critical) - 0.2 * len(high) - 0.08 * len(medium) - 0.1 * len(unavailable)
    reject_score = 0.05 + 0.35 * len(critical) + 0.12 * len(high) + (0.2 if parcel_class == MATCH_CONFLICTING else 0.0)
    verify_score = 0.3 + 0.1 * (len(medium) + len(high)) + 0.15 * len(unavailable) + (0.2 if parcel_class in {MATCH_POSSIBLE, MATCH_NONE} else 0.0)
    clamp = lambda value: round(max(0.02, min(0.98, value)), 2)  # noqa: E731

    def scenario(key: str, title: str, assumptions: List[str], supporting: List[str], contradicting: List[str], result: str,
                 score: float, formula: str) -> Dict[str, Any]:
        return {"key": key, "title": title, "assumptions": assumptions, "supporting_evidence": supporting,
                "contradicting_evidence": contradicting, "affected_records": affected, "expected_result": result,
                "uncertainties": uncertainties, "plausibility": clamp(score), "plausibility_explained": formula,
                "computed_from": "structured findings and matches only (no free-text generation)"}

    scenarios = [
        scenario("A", "Scenario A — Approve", [
            "The extracted identifiers are read correctly from the scan.",
            "The searched registers are complete for this parcel.",
            "POSSIBLE matches are NOT treated as facts.",
        ], supports_approve, contradicts_approve,
            "The document is treated as consistent with the records. Any register change (mutation, status) still goes through its own existing, human-approved workflow.",
            approve_score,
            f"base({parcel_class})={base} − 0.40×{len(critical)} critical − 0.20×{len(high)} high − 0.08×{len(medium)} medium − 0.10×{len(unavailable)} unavailable"),
        scenario("B", "Scenario B — Reject", [
            "At least one contradiction is real and not an OCR/spelling artefact.",
            "The contradicting record is the authoritative one.",
        ], [item["finding_id"] for item in critical + high], supports_approve,
            "The document is returned/rejected in the verification workflow with the contradictions as reasons; no register is changed.",
            reject_score,
            f"0.05 + 0.35×{len(critical)} critical + 0.12×{len(high)} high" + (" + 0.20 conflicting parcel" if parcel_class == MATCH_CONFLICTING else "")),
        scenario("C", "Scenario C — Further verification", [
            "Missing or ambiguous evidence can be obtained (certified copies, court status, mutation orders).",
        ], [item["finding_id"] for item in medium + high] + [item["finding_id"] for item in findings if item["status_label"] in {STATUS_NOT_FOUND, STATUS_UNKNOWN, STATUS_UNAVAILABLE, STATUS_POSSIBLE}],
            [],
            "The document stays pending while the listed items are verified; the investigation is re-run afterwards.",
            verify_score,
            f"0.30 + 0.10×({len(medium)} medium + {len(high)} high) + 0.15×{len(unavailable)} unavailable" + (" + 0.20 weak parcel match" if parcel_class in {MATCH_POSSIBLE, MATCH_NONE} else "")),
    ]
    for item in scenarios:
        item["supporting_evidence"] = list(dict.fromkeys(item["supporting_evidence"]))
        item["contradicting_evidence"] = list(dict.fromkeys(item["contradicting_evidence"]))
    return scenarios


def _recommend(ctx: _Context) -> Dict[str, Any]:
    findings = ctx.findings
    critical = [item for item in findings if item["severity"] == "CRITICAL"]
    high = [item for item in findings if item["severity"] == "HIGH"]
    medium = [item for item in findings if item["severity"] == "MEDIUM"]
    unavailable = ctx.confidence.get("unavailable_sources") or []
    parcel_class = _parcel_class(ctx)
    risk = ((ctx.land or {}).get("risk") or {})
    if critical:
        decision, rule = "REJECT", "R1: one or more CRITICAL contradictions with authoritative records"
        reason = f"SA identified {len(critical)} critical contradiction(s): " + "; ".join(item["title"] for item in critical[:3]) + "."
    elif high or unavailable or parcel_class in {MATCH_CONFLICTING, MATCH_POSSIBLE, MATCH_NONE}:
        decision, rule = "FURTHER_VERIFICATION", "R2: HIGH findings, unavailable sources, or a parcel match weaker than STRONG"
        bits = []
        if high:
            bits.append(f"{len(high)} high-severity finding(s): " + "; ".join(item["title"] for item in high[:3]))
        if parcel_class in {MATCH_CONFLICTING, MATCH_POSSIBLE, MATCH_NONE}:
            bits.append(f"parcel match is {parcel_class}")
        if unavailable:
            bits.append("unavailable source(s): " + ", ".join(unavailable))
        reason = "SA recommends further verification because " + "; ".join(bits) + "."
    elif medium:
        decision, rule = "FURTHER_VERIFICATION", "R3: MEDIUM findings remain on a STRONG/EXACT parcel match"
        reason = f"SA recommends further verification: the parcel matched ({parcel_class}) but {len(medium)} medium-severity item(s) remain: " + \
                 "; ".join(item["title"] for item in medium[:3]) + "."
    else:
        decision, rule = "APPROVE", "R4: EXACT/STRONG parcel match, all sources searched, nothing above LOW"
        reason = (f"Records indicate a {CERTAINTY_LABEL[parcel_class].lower()} parcel match with no contradiction above LOW severity across the "
                  f"searched sources (risk engine verdict {risk.get('verdict') or 'n/a'}).")
    overall = (ctx.confidence.get("overall_evidence") or {}).get("value") or 0.0
    confidence = round(max(0.05, min(0.98, overall * (1 - 0.1 * len(unavailable)))), 2)
    what_would_change: List[str] = []
    for item in critical + high + medium:
        for step in item.get("resolution") or []:
            if step not in what_would_change:
                what_would_change.append(step)
    if unavailable:
        what_would_change.append("Re-run once the unavailable source(s) respond: " + ", ".join(unavailable) + ".")
    if parcel_class in {MATCH_POSSIBLE, MATCH_NONE}:
        what_would_change.append("An independent record (Jamabandi/RoR, mutation or registry copy) for the same survey/khasra and village would upgrade the parcel match.")
    scenario_key = {"APPROVE": "A", "REJECT": "B", "FURTHER_VERIFICATION": "C"}[decision]
    return {
        "recommendation": decision,
        "reason": reason,
        "rule": rule,
        "scenario": scenario_key,
        "scenario_plausibility": next((item["plausibility"] for item in ctx.scenarios if item["key"] == scenario_key), None),
        "confidence": confidence,
        "confidence_explained": f"overall evidence confidence {overall:.2f} × (1 − 0.10 × {len(unavailable)} unavailable sources)",
        "critical_findings": [item["finding_id"] for item in critical],
        "high_findings": [item["finding_id"] for item in high],
        "supporting_evidence": sorted({evidence_id for item in findings if item["severity"] == "INFO" for evidence_id in item["evidence"]}),
        "contradicting_evidence": sorted({evidence_id for item in critical + high for evidence_id in item["evidence"]}),
        "uncertainties": [item["title"] for item in findings if item["status_label"] in {STATUS_NOT_FOUND, STATUS_UNKNOWN, STATUS_UNAVAILABLE, STATUS_POSSIBLE}],
        "what_would_change": what_would_change[:12],
        "risk_engine": {"verdict": risk.get("verdict"), "highest_severity": risk.get("highest_severity"), "why": risk.get("why"),
                        "note": "Existing land-risk engine verdict, reported unchanged; SA does not compute a competing score."},
        "parcel_match_class": parcel_class,
        "language": "SA recommendations are workflow signals derived from local records; they are not legal determinations.",
    }


def _evidence_graph(ctx: _Context) -> Dict[str, Any]:
    nodes: List[Dict[str, Any]] = [{"id": f"doc:{ctx.document_id}", "kind": "document", "label": f"Document {ctx.document_id}", "certainty": "Under investigation"}]
    edges: List[Dict[str, Any]] = []
    for entity in ctx.entities:
        if entity["type"] in {"khasra_number", "survey_number", "parcel_id", "registration_no", "mutation_no", "case_number"}:
            node_id = f"id:{entity['type']}"
            nodes.append({"id": node_id, "kind": "identifier", "label": f"{entity['type']} {entity['value']}", "certainty": entity["certainty"]})
            edges.append({"from": f"doc:{ctx.document_id}", "to": node_id, "relation": "extracted", "confidence": entity["confidence"]})
    if ctx.land_id and ctx.land:
        nodes.append({"id": f"land:{ctx.land_id}", "kind": "parcel", "label": f"Parcel {ctx.survey} · {ctx.village}", "certainty": CERTAINTY_LABEL[_parcel_class(ctx)],
                      "links": _links_for("land", ctx.land_id)})
        for key in ("id:khasra_number", "id:survey_number"):
            if any(node["id"] == key for node in nodes):
                edges.append({"from": key, "to": f"land:{ctx.land_id}", "relation": "identifies", "confidence": (ctx.confidence.get("parcel_match") or {}).get("value")})
        owner = _text((ctx.land.get("current_owner") or {}).get("owner"))
        if owner:
            nodes.append({"id": "owner:current", "kind": "owner", "label": f"Recorded owner {owner}", "certainty": "Confirmed"})
            edges.append({"from": f"land:{ctx.land_id}", "to": "owner:current", "relation": "current_owner", "confidence": 1.0})
    for match in ctx.matches:
        if match["source"] in {"LAND_RECORD", "PARCEL"} or not match["record_id"]:
            continue
        kind = {"MUTATION": "mutation", "COURT_CASE": "court_case", "REGISTRY": "registry", "ENCUMBRANCE": "encumbrance"}.get(match["source"], "record")
        node_id = f"{kind}:{match['record_id']}"
        evidence = ctx.evidence.get(match["evidence_id"]) or {}
        nodes.append({"id": node_id, "kind": kind, "label": match["label"], "certainty": match["certainty"], "links": evidence.get("links", [])})
        anchor = f"land:{ctx.land_id}" if (ctx.land_id and ctx.land) else f"doc:{ctx.document_id}"
        edges.append({"from": anchor, "to": node_id, "relation": match["match_class"].lower(), "confidence": match["score"]})
    for finding in ctx.findings:
        if finding["severity"] in ALERT_SEVERITIES:
            node_id = f"finding:{finding['finding_id']}"
            nodes.append({"id": node_id, "kind": "finding", "label": finding["title"], "severity": finding["severity"], "certainty": finding["status_label"]})
            for record in finding["source_records"]:
                target = f"{record['kind'] if record['kind'] != 'land' else 'land'}:{record['id']}"
                if any(node["id"] == target for node in nodes):
                    edges.append({"from": node_id, "to": target, "relation": "derived_from", "confidence": finding["confidence"]})
    unique_nodes: Dict[str, Dict[str, Any]] = {}
    for node in nodes:
        unique_nodes.setdefault(node["id"], node)
    return {"nodes": list(unique_nodes.values()), "edges": edges, "legend": "Document → identifiers → parcel → owner / mutation / court case / registry; alerts point at the records they derive from."}


# ---------------------------------------------------------------------------
# ownership-guarded execution wrapper (assistant_tasks runner)
# ---------------------------------------------------------------------------


class _OwnershipLost(RuntimeError):
    """This runner no longer owns the investigation row (lease reclaimed)."""


class _Cancelled(RuntimeError):
    """The administrator cancelled the request between stages."""


class _Run:
    def __init__(self, investigation_id: str, request_id: Optional[str]) -> None:
        self.investigation_id = investigation_id
        self.request_id = request_id or ""
        self.token = ""
        self.progress: Dict[str, Any] = {"stages": [{"key": key, "label": label, "status": "PENDING", "started_at": None, "finished_at": None, "note": ""}
                                                    for key, label, _state in STAGES], "current": None, "current_label": None}

    def claim(self) -> None:
        run_id = ""
        if self.request_id:
            with _server().get_db() as db:
                row = db.execute("SELECT run_id FROM assistant_requests WHERE request_id=?", (self.request_id,)).fetchone()
            run_id = _text(row["run_id"]) if row else ""
        self.token = run_id or uuid.uuid4().hex
        now = _now()
        with _server().get_db() as db:
            cur = db.execute(
                "UPDATE land_investigations SET run_token=?, state=?, attempts=attempts+1, started_at=COALESCE(started_at, ?), "
                "updated_at=?, failure='', completed_at=NULL, progress=? WHERE investigation_id=? AND state NOT IN (?, ?)",
                (self.token, STATE_EXTRACTING, now, now, _json(self.progress), self.investigation_id, STATE_APPROVED, STATE_REJECTED),
            )
            if (getattr(cur, "rowcount", 0) or 0) != 1:
                raise assistant_tasks.AssistantPermanentError(
                    "The investigation is missing or already decided; it cannot be re-run.", code=assistant_tasks.ERROR_VALIDATION)

    def _write(self, assignments: str, params: Tuple[Any, ...]) -> None:
        with _server().get_db() as db:
            cur = db.execute(
                f"UPDATE land_investigations SET {assignments}, updated_at=? WHERE investigation_id=? AND run_token=?",
                (*params, _now(), self.investigation_id, self.token),
            )
        if (getattr(cur, "rowcount", 0) or 0) != 1:
            # FAIL-CLOSED: a zombie runner can never write over the current owner.
            raise _OwnershipLost(f"Investigation {self.investigation_id} is owned by another execution.")

    def stage(self, key: str, status: str, note: str = "", state: Optional[str] = None) -> None:
        now = _now()
        for item in self.progress["stages"]:
            if item["key"] == key:
                item["status"] = status
                if status == "RUNNING":
                    item["started_at"] = item["started_at"] or now
                    self.progress["current"] = key
                    self.progress["current_label"] = item["label"]
                elif status in {"DONE", "FAILED", "SKIPPED"}:
                    item["finished_at"] = now
                if note:
                    item["note"] = note
        if state:
            self._write("progress=?, state=?", (_json(self.progress), state))
        else:
            self._write("progress=?", (_json(self.progress),))

    def check_cancel(self) -> None:
        if not self.request_id:
            return
        with _server().get_db() as db:
            row = db.execute("SELECT cancel_requested, run_id FROM assistant_requests WHERE request_id=?", (self.request_id,)).fetchone()
        if row and _text(row["run_id"]) and _text(row["run_id"]) != self.token:
            raise _OwnershipLost("The request lease moved to another execution.")
        if row and int(row["cancel_requested"] or 0):
            raise _Cancelled("Cancelled by the administrator.")

    def fail(self, code: str, message: str) -> None:
        for item in self.progress["stages"]:
            if item["status"] == "RUNNING":
                item["status"] = "FAILED"
                item["finished_at"] = _now()
        try:
            self._write("progress=?, state=?, failure=?", (_json(self.progress), STATE_FAILED, _json({"code": code, "message": message[:500]})))
        except _OwnershipLost:
            pass


def runner_for(actor: Dict[str, Any]) -> Callable[[Dict[str, Any], str], Dict[str, Any]]:
    """Bind the server-resolved actor to the assistant_tasks runner (worker
    threads do not inherit request context)."""
    snapshot = {"id": actor.get("id"), "email": actor.get("email"), "full_name": actor.get("full_name"), "role": actor.get("role")}

    def _run(payload: Dict[str, Any], request_id: str) -> Dict[str, Any]:
        return run_investigation(_text(payload.get("investigation_id")), snapshot, request_id)

    return _run


def run_investigation(investigation_id: str, actor: Dict[str, Any], request_id: Optional[str] = None) -> Dict[str, Any]:
    """Execute the pipeline for one investigation. Idempotent per run token;
    a reclaimed (zombie) execution can never complete the row."""
    ensure_investigation_tables()
    run = _Run(investigation_id, request_id)
    run.claim()
    row = load_investigation(investigation_id)
    if not row:
        raise assistant_tasks.AssistantPermanentError("Investigation not found.", code=assistant_tasks.ERROR_VALIDATION)
    document = load_document(row["document_id"])
    try:
        if not document:
            raise assistant_tasks.AssistantPermanentError("The investigated document no longer exists.", code=assistant_tasks.ERROR_VALIDATION)
        if not document_visible(document, actor):
            raise assistant_tasks.AssistantPermanentError("The document is not visible to the investigating administrator.", code=assistant_tasks.ERROR_AUTH)
        ctx = _Context(investigation_id, document, actor)
        ctx.fingerprint, ctx.fingerprint_basis = row.get("fingerprint") or "", row.get("fingerprint_basis") or ""
        if not ctx.fingerprint:
            ctx.fingerprint, ctx.fingerprint_basis = document_fingerprint(document)
        run.stage("extract", "RUNNING", state=STATE_EXTRACTING)
        _stage_extract(ctx)
        _stage_identity(ctx)
        run.stage("extract", "DONE", note=f"{len(ctx.entities)} identifier(s) extracted")
        _event(investigation_id, actor, "EXTRACTED", f"{len(ctx.entities)} identifiers; parcel identity {ctx.land_id or 'unknown'}", ctx.document_id)
        run.check_cancel()
        run.stage("parcel", "RUNNING", state=STATE_MATCHING)
        _stage_sources(ctx, run)
        _stage_parcel_match(ctx)
        run.check_cancel()
        _stage_mutations(ctx)
        run.stage("mutations", "DONE")
        run.stage("court_cases", "RUNNING")
        _stage_court(ctx)
        _stage_registry(ctx)
        run.stage("court_cases", "DONE")
        run.stage("ownership", "RUNNING")
        _stage_ownership(ctx)
        _stage_risk_and_timeline(ctx)
        run.stage("ownership", "DONE")
        _event(investigation_id, actor, "MATCHED", f"parcel {_parcel_class(ctx)}; {len(ctx.matches)} record match(es); {len(ctx.findings)} finding(s)", ctx.document_id)
        run.check_cancel()
        run.stage("scenarios", "RUNNING", state=STATE_SCENARIO)
        ctx.findings.sort(key=lambda item: (-SEVERITY_RANK[item["severity"]], item["finding_id"]))
        ctx.confidence = _confidences(ctx)
        ctx.scenarios = _build_scenarios(ctx)
        ctx.recommendation = _recommend(ctx)
        run.stage("scenarios", "DONE")
        run.stage("finalize", "RUNNING")
        _persist_result(ctx, run, row)
        run.stage("finalize", "DONE")
        for finding in ctx.findings:
            if finding["is_alert"]:
                _event(investigation_id, actor, "FINDING", f"{finding['severity']} {finding['type']} {finding['finding_id']}: {finding['title']}", ctx.document_id,
                       audit_action="SA_INVESTIGATION_FINDING")
        _event(investigation_id, actor, "RECOMMENDED", f"{ctx.recommendation['recommendation']} (confidence {ctx.recommendation['confidence']}) — {ctx.recommendation['rule']}",
               ctx.document_id, audit_action="SA_INVESTIGATION_RECOMMENDED")
        return {"investigation_id": investigation_id, "state": STATE_READY, "recommendation": ctx.recommendation["recommendation"],
                "confidence": ctx.recommendation["confidence"], "findings": len(ctx.findings),
                "alerts": sum(1 for item in ctx.findings if item["is_alert"]), "proposal_id": ctx.recommendation.get("proposal_id")}
    except _OwnershipLost as exc:
        raise assistant_tasks.AssistantPermanentError(str(exc), code=assistant_tasks.ERROR_VALIDATION) from exc
    except _Cancelled as exc:
        run.fail("CANCELLED", str(exc))
        _event(investigation_id, actor, "CANCELLED", "Investigation cancelled before completion", row.get("document_id"))
        raise assistant_tasks.AssistantPermanentError("The investigation was cancelled.", code=assistant_tasks.ERROR_VALIDATION) from exc
    except assistant_tasks.AssistantPermanentError as exc:
        run.fail(exc.code, str(exc))
        _event(investigation_id, actor, "FAILED", f"{exc.code}: {exc}", row.get("document_id"))
        raise
    except HTTPException as exc:
        run.fail("HTTP_%s" % exc.status_code, str(exc.detail))
        _event(investigation_id, actor, "FAILED", f"HTTP {exc.status_code}: {exc.detail}", row.get("document_id"))
        raise assistant_tasks.AssistantPermanentError(str(exc.detail), code=assistant_tasks.ERROR_VALIDATION) from exc
    except Exception as exc:  # noqa: BLE001 - classified as retryable provider-style failure
        run.fail(type(exc).__name__, str(exc))
        _event(investigation_id, actor, "FAILED", f"{type(exc).__name__}: {str(exc)[:200]}", row.get("document_id"))
        if isinstance(exc, (sqlite3.OperationalError, TimeoutError, ConnectionError)):
            raise assistant_tasks.AssistantProviderError(f"Investigation source failure: {type(exc).__name__}") from exc
        raise assistant_tasks.AssistantProviderError(f"Investigation failed: {type(exc).__name__}: {str(exc)[:200]}") from exc


def _persist_result(ctx: _Context, run: _Run, row: Dict[str, Any]) -> None:
    now = _now()
    summary = {
        "finding_counts": {severity: _count(ctx.findings, severity) for severity in SEVERITIES},
        "alert_count": sum(1 for item in ctx.findings if item["is_alert"]),
        "match_counts": {name: sum(1 for item in ctx.matches if item["source"] == name) for name in ("LAND_RECORD", "PARCEL", "MUTATION", "COURT_CASE", "REGISTRY")},
        "sources": {name: item.get("status") for name, item in ctx.sources.items()},
        "partial": any(item.get("status") == STATUS_UNAVAILABLE for item in ctx.sources.values()),
        "contradictions_first": [item["finding_id"] for item in ctx.findings if item["severity"] in ALERT_SEVERITIES],
    }
    reproducibility = {
        "investigation_version": row.get("investigation_version"),
        "engine_version": ENGINE_VERSION,
        "rules_version": RULES_VERSION,
        "model": MODEL_INFO,
        "data_snapshot_at": ctx.snapshot_at,
        "data_snapshot_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ctx.snapshot_at)),
        "actor_role": ctx.actor.get("role"),
        "source_timeout_seconds": source_timeout_seconds(),
        "fingerprint": ctx.fingerprint,
        "fingerprint_basis": ctx.fingerprint_basis,
        "request_id": run.request_id,
        "run_token": run.token,
    }
    land = ctx.land or {}
    snapshot = {
        "document": {"id": ctx.document_id, "status": ctx.document.get("status"), "doc_type": ctx.doc_type, "fields": ctx.flat},
        "land": {"land_id": land.get("land_id"), "property": land.get("property"), "current_owner": land.get("current_owner"),
                 "documents": land.get("documents"), "ownership_history": land.get("ownership_history"),
                 "mutations": [{k: m.get(k) for k in ("id", "mutation_no", "status", "previous_owner", "new_owner", "deed_no", "deed_date")} for m in land.get("mutations") or []],
                 "encumbrances": [{k: e.get(k) for k in ("id", "lender", "reference_no", "status", "amount")} for e in land.get("encumbrances") or []],
                 "litigation": land.get("litigation"), "risk": {k: (land.get("risk") or {}).get(k) for k in ("verdict", "highest_severity", "counts", "why")}} if land else None,
        "parcel_resolution": {k: (ctx.parcel.get("resolution") or {}).get(k) for k in ("status", "confidence", "conflicting_fields", "reasons")} if ctx.parcel else None,
    }
    proposal_id = _text(row.get("proposal_id"))
    with _server().get_db() as db:
        db.execute("DELETE FROM investigation_findings WHERE investigation_id=?", (ctx.investigation_id,))
        for finding in ctx.findings:
            db.execute(
                "INSERT INTO investigation_findings(finding_id, investigation_id, finding_type, severity, confidence, status_label, title, description, "
                "evidence, source_records, pages, why, resolution, is_alert, origin, rule_code, affects, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (finding["finding_id"], ctx.investigation_id, finding["type"], finding["severity"], finding["confidence"], finding["status_label"],
                 finding["title"], finding["description"], _json(finding["evidence"]), _json(finding["source_records"]), _json(finding["pages"]),
                 finding["why"], _json(finding["resolution"]), 1 if finding["is_alert"] else 0, finding["origin"], finding.get("code") or finding["type"],
                 _json(list(finding.get("affects") or [])), now),
            )
    run._write(
        "state=?, completed_at=?, land_id=?, property_id=?, match_class=?, recommendation=?, recommendation_payload=?, confidence=?, risk_verdict=?, "
        "summary=?, entities=?, matches=?, sources=?, timeline=?, scenarios=?, evidence=?, evidence_graph=?, snapshot=?, reproducibility=?, "
        "fingerprint=?, fingerprint_basis=?, failure=''",
        (STATE_READY, now, ctx.land_id if ctx.land else "", _text(ctx.sources.get("parcel", {}).get("property_id")), _parcel_class(ctx),
         ctx.recommendation["recommendation"], _json(ctx.recommendation), _json(ctx.confidence), _text(((ctx.land or {}).get("risk") or {}).get("verdict")),
         _json(summary), _json(ctx.entities), _json(ctx.matches), _json(ctx.sources), _json(ctx.timeline), _json(ctx.scenarios),
         _json(ctx.evidence.items), _json(_evidence_graph(ctx)), _json(snapshot), _json(reproducibility), ctx.fingerprint, ctx.fingerprint_basis),
    )
    # Governance proposal: SA PROPOSES its recommendation; only an administrator
    # (password + CAS) can turn it into a recorded decision. Idempotent per
    # investigation version so re-runs of the same logical request never
    # create a second proposal.
    try:
        proposal = _ensure_recommendation_proposal(ctx, row)
        proposal_id = _text(proposal.get("proposal_id")) if proposal else proposal_id
    except Exception as exc:  # noqa: BLE001 - the investigation stays usable; the decision endpoint creates the proposal lazily
        print(f"[SA INVESTIGATION PROPOSAL WARNING] {exc}")
    if proposal_id:
        ctx.recommendation["proposal_id"] = proposal_id
        run._write("proposal_id=?, recommendation_payload=?", (proposal_id, _json(ctx.recommendation)))


def _proposal_evidence(ctx_or_row: Any) -> List[Dict[str, Any]]:
    if isinstance(ctx_or_row, _Context):
        findings, evidence = ctx_or_row.findings, ctx_or_row.evidence.items
    else:
        findings, evidence = ctx_or_row.get("findings") or [], ctx_or_row.get("evidence") or []
    by_id = {item["evidence_id"]: item for item in evidence}
    out: List[Dict[str, Any]] = []
    for finding in findings:
        if finding["severity"] not in ALERT_SEVERITIES:
            continue
        out.append({"type": "finding", "finding_id": finding["finding_id"], "severity": finding["severity"], "title": finding["title"],
                    "status": finding["status_label"], "records": [
                        {"kind": by_id[eid]["kind"], "ref": by_id[eid]["ref"], "label": by_id[eid]["label"], "links": by_id[eid]["links"]}
                        for eid in finding["evidence"] if eid in by_id]})
    return out[:20]


def _nonce(row: Dict[str, Any]) -> str:
    """Per-row nonce for governance idempotency keys (request id when the
    investigation runs on the task lifecycle, else its creation timestamp)."""
    return _text(row.get("request_id")) or f"{float(row.get('created_at') or 0):.6f}"


def _ensure_recommendation_proposal(ctx: _Context, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    gov = _governance()
    recommendation = ctx.recommendation["recommendation"]
    decision = DECISION_FOR_RECOMMENDATION[recommendation]
    # The key is unique per investigation ROW (created_at nonce), not just per
    # id, so a re-run of the same investigation version reuses its own
    # PROPOSED proposal but can never attach to a foreign or already-executed one.
    key = f"sa-inv:{ctx.investigation_id}:{_nonce(row)}:v{row.get('investigation_version')}:{decision}:recommended"
    data = {
        "action_type": ACTION_TYPE,
        "target_type": "INVESTIGATION",
        "target_ids": [ctx.investigation_id],
        "before": {"investigation": {"state": STATE_READY, "investigation_version": row.get("investigation_version"), "recommendation": recommendation}},
        "after": {"decision": decision, "recommendation": recommendation, "override": False, "note": "", "document_id": ctx.document_id,
                  "investigation_id": ctx.investigation_id},
        "reason": f"SA recommendation for {ctx.investigation_id}: {recommendation}. {ctx.recommendation['reason']}"[:1000],
        "evidence": [{"type": "investigation", "investigation_id": ctx.investigation_id, "links": _links_for("investigation", ctx.investigation_id),
                      "scenarios": [{"key": s["key"], "title": s["title"], "plausibility": s["plausibility"]} for s in ctx.scenarios]}] + _proposal_evidence(ctx),
        "confidence": ctx.recommendation["confidence"],
        "risk": "HIGH" if recommendation == "REJECT" else ("MEDIUM" if recommendation == "FURTHER_VERIFICATION" else "LOW"),
    }
    return gov.create_proposal(data, created_by="SA_INVESTIGATION", idempotency_key=key)


# ---------------------------------------------------------------------------
# persistence / projections
# ---------------------------------------------------------------------------

_JSON_COLUMNS = ("progress", "recommendation_payload", "confidence", "summary", "entities", "matches", "sources", "timeline",
                 "scenarios", "evidence", "evidence_graph", "snapshot", "reproducibility", "failure")
_JSON_DEFAULTS = {"entities": [], "matches": [], "timeline": [], "scenarios": [], "evidence": []}


def load_investigation(investigation_id: str) -> Optional[Dict[str, Any]]:
    ensure_investigation_tables()
    with _server().get_db() as db:
        row = db.execute("SELECT * FROM land_investigations WHERE investigation_id=?", (_text(investigation_id),)).fetchone()
        if not row:
            return None
        findings = db.execute("SELECT * FROM investigation_findings WHERE investigation_id=? ORDER BY finding_id ASC", (_text(investigation_id),)).fetchall()
    item = dict(row)
    for column in _JSON_COLUMNS:
        item[column] = _parse_json(item.get(column), _JSON_DEFAULTS.get(column, {}))
    item["findings"] = [_finding_row(finding) for finding in findings]
    item["findings"].sort(key=lambda finding: (-SEVERITY_RANK.get(finding["severity"], 0), finding["finding_id"]))
    return item


def _finding_row(row: Any) -> Dict[str, Any]:
    item = dict(row)
    return {
        "finding_id": item["finding_id"], "investigation_id": item["investigation_id"], "type": item["finding_type"], "severity": item["severity"],
        "confidence": item["confidence"], "status_label": item["status_label"], "title": item["title"], "description": item["description"],
        "evidence": _parse_json(item.get("evidence"), []), "source_records": _parse_json(item.get("source_records"), []),
        "pages": _parse_json(item.get("pages"), []), "why": item["why"], "resolution": _parse_json(item.get("resolution"), []),
        "is_alert": bool(item["is_alert"]), "origin": item["origin"], "code": item.get("rule_code") or item["finding_type"],
        "affects": _parse_json(item.get("affects"), []), "created_at": item["created_at"],
    }


def _next_investigation_id(db: Any) -> str:
    year = time.strftime("%Y")
    prefix = f"INV-{year}-"
    rows = db.execute("SELECT investigation_id FROM land_investigations WHERE investigation_id LIKE ? ORDER BY investigation_id DESC LIMIT 1",
                      (prefix + "%",)).fetchall()
    highest = 0
    for row in rows:
        match = re.search(r"(\d+)$", _text(row["investigation_id"]))
        if match:
            highest = max(highest, int(match.group(1)))
    return f"{prefix}{highest + 1:06d}"


def _stage_view(item: Dict[str, Any]) -> Dict[str, Any]:
    progress = item.get("progress") or {}
    stages = progress.get("stages") or []
    state = item.get("state")
    label = progress.get("current_label")
    if state in COMPLETE_STATES:
        label = "Investigation complete"
    elif state == STATE_FAILED:
        label = "Investigation failed"
    elif state == STATE_CREATED:
        label = "Queued"
    return {"stages": stages, "current": progress.get("current"), "current_label": label,
            "complete": state in COMPLETE_STATES, "running": state in RUNNING_STATES, "failed": state == STATE_FAILED}


def summarize(item: Dict[str, Any]) -> Dict[str, Any]:
    recommendation = item.get("recommendation_payload") or {}
    summary = item.get("summary") or {}
    return {
        "investigation_id": item["investigation_id"],
        "document_id": item["document_id"],
        "investigation_version": item.get("investigation_version"),
        "state": item["state"],
        "complete": item["state"] in COMPLETE_STATES,
        "decidable": item["state"] in DECIDABLE_STATES,
        "progress": _stage_view(item),
        "recommendation": item.get("recommendation") or None,
        "recommendation_reason": recommendation.get("reason"),
        "confidence": recommendation.get("confidence"),
        "confidences": item.get("confidence") if item["state"] in COMPLETE_STATES else {},
        "match_class": item.get("match_class") or None,
        "land_id": item.get("land_id") or None,
        "risk_verdict": item.get("risk_verdict") or None,
        "finding_counts": summary.get("finding_counts") or {},
        "alert_count": summary.get("alert_count") or 0,
        "partial": bool(summary.get("partial")),
        "failure": item.get("failure") or None,
        "proposal_id": item.get("proposal_id") or None,
        "decision": item.get("decision") or None,
        "decision_override": bool(item.get("decision_override")),
        "decided_by": item.get("decided_by") or None,
        "decided_at": item.get("decided_at"),
        "created_by": item.get("created_by"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "completed_at": item.get("completed_at"),
        "trigger": item.get("trigger_kind"),
        "attempts": int(item.get("attempts") or 0),
        "links": _links_for("investigation", item["investigation_id"]) + _links_for("document", item["document_id"])[:1],
    }


def detail(item: Dict[str, Any], user: Dict[str, Any]) -> Dict[str, Any]:
    payload = summarize(item)
    complete = item["state"] in COMPLETE_STATES
    payload.update({
        "document": item.get("_document") or _document_header(load_document(item["document_id"])),
        "entities": item.get("entities") or [],
        "matches": (item.get("matches") or []) if complete else [],
        "findings": (item.get("findings") or []) if complete else [],
        "alerts": [finding for finding in (item.get("findings") or []) if finding.get("is_alert")] if complete else [],
        "sources": item.get("sources") or {},
        "timeline": (item.get("timeline") or []) if complete else [],
        "scenarios": (item.get("scenarios") or []) if complete else [],
        "recommendation_detail": (item.get("recommendation_payload") or {}) if complete else {},
        "evidence": item.get("evidence") or [],
        "evidence_graph": (item.get("evidence_graph") or {}) if complete else {},
        "snapshot": (item.get("snapshot") or {}) if complete else {},
        "reproducibility": item.get("reproducibility") or {},
        "decision_note": item.get("decision_note") or "",
        "flow": ["Document", "Extracted Info", "Matched Parcel", "Ownership", "Registry", "Mutation", "Court Cases", "Timeline",
                 "Conflicts", "Scenarios", "SA Recommendation", "Administrator Decision"],
        "incomplete_notice": None if complete else ("This investigation has not completed; nothing here is a result." if item["state"] != STATE_FAILED
                                                   else "This investigation FAILED; no findings were produced."),
        "can_decide": user.get("role") == _server().ROLE_ADMIN and item["state"] in DECIDABLE_STATES,
        "expected_decision": DECISION_FOR_RECOMMENDATION.get(item.get("recommendation") or "", None),
    })
    if item.get("request_id") and _text(item.get("created_by_id")) == _text(user.get("id")):
        # Only the actor who owns the background task sees its request id / envelope.
        payload["request_id"] = item["request_id"]
        payload["task"] = assistant_tasks.get_request(item["request_id"], _text(user.get("id")))
    return payload


# ---------------------------------------------------------------------------
# creation / start / decision (service functions used by routes and hooks)
# ---------------------------------------------------------------------------


def find_existing(fingerprint: str, document_id: str) -> List[Dict[str, Any]]:
    ensure_investigation_tables()
    with _server().get_db() as db:
        # Only investigations whose source document still exists can be
        # "opened"; orphans (document removed later) are not offered as duplicates.
        rows = db.execute(
            """SELECT li.* FROM land_investigations li
               WHERE (li.fingerprint=? OR li.document_id=?) AND li.state != ?
                 AND EXISTS (SELECT 1 FROM documents d WHERE d.id = li.document_id)
               ORDER BY li.created_at DESC LIMIT 10""",
            (fingerprint, document_id, STATE_FAILED),
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        for column in _JSON_COLUMNS:
            item[column] = _parse_json(item.get(column), _JSON_DEFAULTS.get(column, {}))
        items.append(item)
    return items


def create_investigation(document: Dict[str, Any], actor: Dict[str, Any], *, force_new: bool = False, trigger: str = "MANUAL",
                         request_id: Optional[str] = None) -> Dict[str, Any]:
    """Create the persistent investigation (state CREATED). Does not run it."""
    ensure_investigation_tables()
    document_id = _text(document.get("id"))
    fingerprint, basis = document_fingerprint(document)
    rid = assistant_tasks.normalize_request_id(request_id)
    if rid:
        with _server().get_db() as db:
            row = db.execute("SELECT investigation_id, created_by_id FROM land_investigations WHERE request_id=?", (rid,)).fetchone()
        if row:
            if _text(row["created_by_id"]) != _text(actor.get("id")):
                raise HTTPException(status_code=404, detail="Investigation not found.")
            return {"investigation": load_investigation(row["investigation_id"]), "already_investigated": False, "existing": [], "replayed": True}
    if not force_new:
        existing = find_existing(fingerprint, document_id)
        if existing:
            _audit(actor, "SA_INVESTIGATION_DUPLICATE", f"Document {document_id} content already investigated as {existing[0]['investigation_id']}", document_id)
            return {"investigation": existing[0], "already_investigated": True,
                    "existing": [summarize(item) for item in existing], "replayed": False,
                    "message": "This document content was already investigated. Open the existing investigation or run a new one."}
    now = _now()
    rid = rid or assistant_tasks.new_request_id()
    investigation_id = ""
    for _attempt in range(5):
        with _server().get_db() as db:
            version_row = db.execute("SELECT COALESCE(MAX(investigation_version), 0) AS v FROM land_investigations WHERE document_id=?", (document_id,)).fetchone()
            version = int(version_row["v"] or 0) + 1
            candidate = _next_investigation_id(db)
            try:
                db.execute(
                    "INSERT INTO land_investigations(investigation_id, document_id, investigation_version, fingerprint, fingerprint_basis, state, progress, "
                    "request_id, trigger_kind, created_by, created_by_id, created_by_role, created_at, updated_at, reproducibility) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (candidate, document_id, version, fingerprint, basis, STATE_CREATED, _json({"stages": [], "current": None, "current_label": "Queued"}),
                     rid, trigger, actor.get("full_name") or actor.get("email") or "", _text(actor.get("id")), _text(actor.get("role")), now, now,
                     _json({"engine_version": ENGINE_VERSION, "rules_version": RULES_VERSION, "model": MODEL_INFO, "fingerprint": fingerprint, "fingerprint_basis": basis})),
                )
                investigation_id = candidate
            except Exception as exc:  # primary-key race on the sequential id: pick the next one
                if "unique" not in str(exc).lower() and "primary" not in str(exc).lower():
                    raise
                # A concurrent request may have won the same request_id: attach to it.
                existing = db.execute("SELECT investigation_id, created_by_id FROM land_investigations WHERE request_id=?", (rid,)).fetchone()
                if existing:
                    if _text(existing["created_by_id"]) != _text(actor.get("id")):
                        raise HTTPException(status_code=404, detail="Investigation not found.")
                    investigation_id = existing["investigation_id"]
        if investigation_id:
            break
    if not investigation_id:
        raise HTTPException(status_code=503, detail="Could not allocate an investigation id; please retry.")
    _event(investigation_id, actor, "CREATED", f"document {document_id} v{version} ({trigger.lower()} trigger, fingerprint {basis})", document_id,
           audit_action="SA_INVESTIGATION_CREATED")
    _event(investigation_id, actor, "DOCUMENT_ATTACHED", f"document {document_id} attached ({document.get('filename')})", document_id,
           audit_action="SA_INVESTIGATION_DOCUMENT_UPLOADED")
    return {"investigation": load_investigation(investigation_id), "already_investigated": False, "existing": [], "replayed": False}


def start_investigation(item: Dict[str, Any], actor: Dict[str, Any], *, wait: Optional[float] = None) -> Dict[str, Any]:
    """Submit the investigation to the shared assistant task lifecycle."""
    envelope = assistant_tasks.submit_or_replay(
        request_id=item["request_id"], surface=SURFACE, user_id=_text(actor.get("id")), kind=TASK_KIND,
        payload={"investigation_id": item["investigation_id"], "document_id": item["document_id"]},
        runner=runner_for(actor), wait_seconds=wait if wait is not None else wait_seconds(),
    )
    return envelope


def auto_start_for_upload(document_id: str, user: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Upload hook: SA investigates administrator uploads automatically.

    Returns a small reference for the upload response or None. Never raises
    to the caller (the upload itself must stay unaffected).
    """
    try:
        if not auto_investigate_enabled() or user.get("role") != _server().ROLE_ADMIN:
            return None
        document = load_document(document_id)
        if not document:
            return None
        created = create_investigation(document, user, force_new=False, trigger="UPLOAD")
        item = created["investigation"]
        if created.get("already_investigated"):
            return {"investigation_id": item["investigation_id"], "state": item["state"], "already_investigated": True,
                    "existing": created.get("existing") or [], "auto_started": False}
        envelope = start_investigation(item, user, wait=upload_wait_seconds())
        fresh = load_investigation(item["investigation_id"]) or item
        return {"investigation_id": fresh["investigation_id"], "state": fresh["state"], "already_investigated": False, "auto_started": True,
                "task_state": (envelope or {}).get("state"), "request_id": item["request_id"]}
    except Exception as exc:  # noqa: BLE001 - never break the upload
        print(f"[SA INVESTIGATION AUTO-START WARNING] {exc}")
        return None


def validate_decision_proposal(proposal: Dict[str, Any]) -> None:
    """Governance hook (``ai_governance._validate_target``): a decision proposal
    must target an existing, decidable investigation with a registered
    decision; an override needs a note."""
    ensure_investigation_tables()
    ids = proposal.get("target_ids") or []
    if len(ids) != 1:
        raise HTTPException(status_code=400, detail="An investigation decision targets exactly one investigation.")
    after = proposal.get("proposed_state") or proposal.get("after") or {}
    decision = _text(after.get("decision")).upper()
    if decision not in DECISIONS:
        raise HTTPException(status_code=400, detail="Investigation decision is not registered.")
    with _server().get_db() as db:
        row = db.execute("SELECT state, recommendation FROM land_investigations WHERE investigation_id=?", (_text(ids[0]),)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Investigation '{ids[0]}' not found.")
    if row["state"] not in DECIDABLE_STATES:
        raise HTTPException(status_code=409, detail=f"Investigation is {row['state']} and cannot be decided.")
    override = decision != DECISION_FOR_RECOMMENDATION.get(_text(row["recommendation"]), "")
    if override and not _text(after.get("note")):
        raise HTTPException(status_code=400, detail="An override note is required when the decision differs from SA's recommendation.")


def execute_decision(proposal: Dict[str, Any], admin: Dict[str, Any]) -> Dict[str, Any]:
    """Governance executor: records the ADMINISTRATOR's decision on the
    investigation. Touches no document, register or parcel. Compare-and-set on
    the investigation state so two concurrent approvals record exactly one.
    Called only from ``ai_governance._execute`` after password verification and
    the proposal's own CAS claim."""
    ensure_investigation_tables()
    after = proposal.get("proposed_state") or {}
    investigation_id = _text((proposal.get("target_ids") or [""])[0])
    decision = _text(after.get("decision")).upper()
    if decision not in DECISIONS:
        raise HTTPException(status_code=400, detail="Investigation decision is not registered.")
    note = _text(after.get("note"))
    item = load_investigation(investigation_id)
    if not item:
        raise HTTPException(status_code=404, detail="Investigation not found.")
    expected = DECISION_FOR_RECOMMENDATION.get(_text(item.get("recommendation")), "")
    override = decision != expected
    if override and not note:
        raise HTTPException(status_code=400, detail="An override note is required when the decision differs from SA's recommendation.")
    now = _now()
    with _server().get_db() as db:
        cur = db.execute(
            "UPDATE land_investigations SET state=?, decision=?, decision_note=?, decision_override=?, decided_by=?, decided_at=?, updated_at=?, proposal_id=? "
            "WHERE investigation_id=? AND state IN (?, ?)",
            (DECISION_STATE[decision], decision, note, 1 if override else 0, admin.get("full_name") or admin.get("email") or "Administrator", now, now,
             _text(proposal.get("proposal_id")), investigation_id, STATE_READY, STATE_NEEDS_REVIEW),
        )
        recorded = (getattr(cur, "rowcount", 0) or 0) == 1
    if not recorded:
        raise HTTPException(status_code=409, detail="The investigation was already decided by another approval; no second decision was recorded.")
    _event(investigation_id, admin, f"DECISION_{decision}", f"administrator decision {decision} (SA recommended {item.get('recommendation')}); proposal {proposal.get('proposal_id')}",
           item.get("document_id"), audit_action="SA_INVESTIGATION_DECIDED")
    if override:
        _event(investigation_id, admin, "OVERRIDE", f"decision {decision} overrides SA recommendation {item.get('recommendation')}: {note[:300]}",
               item.get("document_id"), audit_action="SA_INVESTIGATION_OVERRIDE")
    return {"investigation_id": investigation_id, "state": DECISION_STATE[decision], "decision": decision, "override": override,
            "recommendation": item.get("recommendation"), "decided_by": admin.get("full_name"), "decided_at": now,
            "note": "Recorded the administrator's decision only; no land record, document status or register was modified."}


def _decision_proposal(item: Dict[str, Any], decision: str, note: str, override: bool, admin: Dict[str, Any]) -> Dict[str, Any]:
    gov = _governance()
    linked = gov.get_proposal(item["proposal_id"]) if item.get("proposal_id") else None
    if (linked and not override and linked["status"] == gov.PROPOSED and _now() <= float(linked["expires_at"] or 0)
            and _text((linked.get("proposed_state") or {}).get("decision")).upper() == decision):
        return linked
    key = (f"sa-inv:{item['investigation_id']}:{_nonce(item)}:v{item.get('investigation_version')}:{decision}:"
           f"{'override' if override else 'admin'}:{_text(admin.get('id'))}")
    payload = item.get("recommendation_payload") or {}
    data = {
        "action_type": ACTION_TYPE,
        "target_type": "INVESTIGATION",
        "target_ids": [item["investigation_id"]],
        "before": {"investigation": {"state": item["state"], "investigation_version": item.get("investigation_version"), "recommendation": item.get("recommendation")}},
        "after": {"decision": decision, "recommendation": item.get("recommendation"), "override": override, "note": note,
                  "document_id": item["document_id"], "investigation_id": item["investigation_id"]},
        "reason": (f"Administrator decision {decision} on {item['investigation_id']} (SA recommended {item.get('recommendation')})."
                   + (" OVERRIDE: " + note[:300] if override else "")),
        "evidence": [{"type": "investigation", "investigation_id": item["investigation_id"], "links": _links_for("investigation", item["investigation_id"])}]
                    + _proposal_evidence(item),
        "confidence": float(payload.get("confidence") or 0.5),
        "risk": "HIGH" if override else "MEDIUM",
    }
    return gov.create_proposal(data, created_by=admin.get("full_name") or "ADMIN_PREPARED", idempotency_key=key)


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/sa/investigations", tags=["SA Investigation"])


def _admin():
    s = _server()
    return s.require_roles(s.ROLE_ADMIN)


def _reviewer():
    s = _server()
    return s.require_roles(s.ROLE_ADMIN, s.ROLE_VERIFICATION_OFFICER)


class InvestigationCreate(BaseModel):
    document_id: str
    force_new: bool = False
    request_id: Optional[str] = Field(default=None, max_length=128)


class DecisionRequest(BaseModel):
    decision: str
    note: str = Field(default="", max_length=2000)
    password: str = Field(default="", max_length=1024)


class InvestigationEvent(BaseModel):
    event_type: str
    ref: str = Field(default="", max_length=200)


def _load_or_404(investigation_id: str, user: Dict[str, Any]) -> Dict[str, Any]:
    item = load_investigation(investigation_id)
    if not item:
        raise HTTPException(status_code=404, detail="Investigation not found.")
    document = load_document(item["document_id"])
    if document is None or not document_visible(document, user):
        # Existing document visibility rules decide who may see an investigation.
        raise HTTPException(status_code=404, detail="Investigation not found.")
    item["_document"] = _document_header(document)
    return item


def _document_header(document: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Small document header for the detail view (no OCR text, no field payload)."""
    if not document:
        return None
    return {"id": document.get("id"), "filename": document.get("filename"), "doc_type": document.get("doc_type"),
            "status": document.get("status"), "uploaded_by": document.get("uploaded_by"), "created_at": document.get("created_at")}


def _task_error(exc: Exception) -> HTTPException:
    if isinstance(exc, assistant_tasks.RequestConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, assistant_tasks.AssistantPermanentError):
        return HTTPException(status_code=404 if exc.code == assistant_tasks.ERROR_VALIDATION else 400, detail=str(exc))
    return HTTPException(status_code=500, detail="Investigation request failed.")


@router.get("/alerts")
def list_alerts(limit: int = Query(50, ge=1, le=200), user: dict = Depends(_reviewer())):
    """HIGH/CRITICAL findings across investigations, each referencing its
    investigation and source records with deep links."""
    ensure_investigation_tables()
    with _server().get_db() as db:
        rows = db.execute(
            "SELECT f.*, i.document_id, i.state AS investigation_state, i.recommendation FROM investigation_findings f "
            "JOIN land_investigations i ON i.investigation_id = f.investigation_id WHERE f.is_alert=1 AND i.state IN (?,?,?,?) "
            "ORDER BY f.created_at DESC LIMIT ?",
            (STATE_READY, STATE_NEEDS_REVIEW, STATE_APPROVED, STATE_REJECTED, limit * 3),
        ).fetchall()
    doc_ids = list(dict.fromkeys(_text(row["document_id"]) for row in rows))
    visible_docs: Dict[str, Dict[str, Any]] = {}
    if doc_ids:
        placeholders = ",".join("?" for _ in doc_ids)
        with _server().get_db() as db:
            docs = db.execute(f"SELECT id, status, uploaded_by, filename FROM documents WHERE id IN ({placeholders})", tuple(doc_ids)).fetchall()
        visible_docs = {row["id"]: dict(row) for row in docs if document_visible(row, user)}
    # Source-record deep links come from each investigation's evidence list
    # (one grouped query, not one per alert).
    inv_ids = list(dict.fromkeys(_text(row["investigation_id"]) for row in rows))
    evidence_by_inv: Dict[str, Dict[str, Dict[str, Any]]] = {}
    if inv_ids:
        placeholders = ",".join("?" for _ in inv_ids)
        with _server().get_db() as db:
            evidence_rows = db.execute(f"SELECT investigation_id, evidence FROM land_investigations WHERE investigation_id IN ({placeholders})",
                                       tuple(inv_ids)).fetchall()
        for evidence_row in evidence_rows:
            items = _parse_json(evidence_row["evidence"], [])
            evidence_by_inv[evidence_row["investigation_id"]] = {item.get("evidence_id"): item for item in items if isinstance(item, dict)}
    alerts = []
    for row in rows:
        document = visible_docs.get(_text(row["document_id"]))
        if not document:
            continue
        finding = _finding_row(row)
        links = _links_for("investigation", row["investigation_id"]) + _links_for("document", row["document_id"])[:1]
        evidence_map = evidence_by_inv.get(_text(row["investigation_id"])) or {}
        source_records = []
        for evidence_id in finding.get("evidence") or []:
            item = evidence_map.get(evidence_id)
            if not item:
                continue
            source_records.append({"evidence_id": evidence_id, "kind": item.get("kind"), "record_id": item.get("record_id"),
                                   "label": item.get("label"), "source": item.get("source"), "links": item.get("links") or []})
            for link in item.get("links") or []:
                if link not in links:
                    links.append(link)
        finding.update({
            "document_id": row["document_id"], "document_filename": document.get("filename"), "investigation_state": row["investigation_state"],
            "recommendation": row["recommendation"], "links": links, "source_evidence": source_records,
            "why_am_i_seeing_this": finding["why"], "what_would_resolve_this": finding["resolution"],
        })
        alerts.append(finding)
        if len(alerts) >= limit:
            break
    return {"alerts": alerts, "count": len(alerts)}


@router.get("")
def list_investigations(document_id: str = Query(""), state: str = Query(""), limit: int = Query(50, ge=1, le=200),
                        user: dict = Depends(_reviewer())):
    ensure_investigation_tables()
    where, params = [], []
    if _text(document_id):
        where.append("document_id=?")
        params.append(_text(document_id))
    if _text(state):
        where.append("state=?")
        params.append(_text(state).upper())
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with _server().get_db() as db:
        rows = db.execute(f"SELECT * FROM land_investigations{clause} ORDER BY created_at DESC LIMIT ?", (*params, limit * 2)).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        for column in _JSON_COLUMNS:
            item[column] = _parse_json(item.get(column), _JSON_DEFAULTS.get(column, {}))
        items.append(item)
    doc_ids = list(dict.fromkeys(_text(item["document_id"]) for item in items))
    visible: Dict[str, Dict[str, Any]] = {}
    if doc_ids:
        placeholders = ",".join("?" for _ in doc_ids)
        with _server().get_db() as db:
            docs = db.execute(f"SELECT id, status, uploaded_by, filename, doc_type FROM documents WHERE id IN ({placeholders})", tuple(doc_ids)).fetchall()
        visible = {row["id"]: dict(row) for row in docs if document_visible(row, user)}
    out = []
    for item in items:
        document = visible.get(_text(item["document_id"]))
        if not document:
            continue
        summary = summarize(item)
        summary["document"] = {"id": document["id"], "filename": document.get("filename"), "doc_type": document.get("doc_type"), "status": document.get("status")}
        out.append(summary)
        if len(out) >= limit:
            break
    return {"investigations": out, "count": len(out), "states": sorted(RUNNING_STATES | COMPLETE_STATES | {STATE_FAILED})}


@router.post("")
def create_and_start(req: InvestigationCreate, user: dict = Depends(_admin())):
    """Create (or attach to) an investigation and start it in the background."""
    document = load_document(req.document_id)
    if not document or not document_visible(document, user):
        raise HTTPException(status_code=404, detail="Document not found.")
    created = create_investigation(document, user, force_new=req.force_new, trigger="MANUAL", request_id=req.request_id)
    item = created["investigation"]
    if created.get("already_investigated"):
        return {"already_investigated": True, "existing": created["existing"], "investigation": detail(item, user),
                "message": created.get("message"), "actions": ["open_existing", "run_new"]}
    if _text(item.get("created_by_id")) != _text(user.get("id")):
        raise HTTPException(status_code=404, detail="Investigation not found.")
    try:
        envelope = start_investigation(item, user)
    except Exception as exc:  # noqa: BLE001
        raise _task_error(exc) from exc
    fresh = load_investigation(item["investigation_id"]) or item
    return {"already_investigated": False, "investigation": detail(fresh, user), "task": envelope}


@router.get("/{investigation_id}")
def get_investigation(investigation_id: str, user: dict = Depends(_reviewer())):
    item = _load_or_404(investigation_id, user)
    _audit(user, "SA_INVESTIGATION_VIEWED", f"{item['investigation_id']} viewed (state {item['state']})", item["document_id"])
    return {"investigation": detail(item, user)}


@router.get("/{investigation_id}/explain")
def explain(investigation_id: str, user: dict = Depends(_reviewer())):
    """'Why did SA recommend X?' — answered from the stored structured findings."""
    item = _load_or_404(investigation_id, user)
    if item["state"] not in COMPLETE_STATES:
        return {"question": "Why did SA recommend this?", "answer": ["The investigation has not completed, so there is no recommendation to explain."],
                "state": item["state"]}
    payload = item.get("recommendation_payload") or {}
    findings = item.get("findings") or []
    by_id = {finding["finding_id"]: finding for finding in findings}
    decisive_ids = list(payload.get("critical_findings") or []) + list(payload.get("high_findings") or [])
    decisive = [by_id[fid] for fid in decisive_ids if fid in by_id]
    answer = [f"SA recommended {payload.get('recommendation')} under rule {payload.get('rule')} (rules {(item.get('reproducibility') or {}).get('rules_version')})."]
    answer.append(payload.get("reason") or "")
    for finding in decisive[:8]:
        answer.append(f"{finding['severity']} · {finding['title']}: {finding['description']} [{finding['status_label']}]")
    conf = item.get("confidence") or {}
    answer.append("Separate confidences — parcel match: " + _fmt_conf(conf.get("parcel_match")) + "; ownership: " + _fmt_conf(conf.get("ownership"))
                  + "; mutation match: " + _fmt_conf(conf.get("mutation_match")) + "; court-case match: " + _fmt_conf(conf.get("court_case_match"))
                  + "; overall evidence: " + _fmt_conf(conf.get("overall_evidence")) + ".")
    if payload.get("uncertainties"):
        answer.append("Uncertainties SA did not resolve: " + "; ".join(payload["uncertainties"][:6]) + ".")
    if payload.get("what_would_change"):
        answer.append("What would change the recommendation: " + " ".join(payload["what_would_change"][:5]))
    return {"question": f"Why did SA recommend {payload.get('recommendation')}?", "answer": [line for line in answer if line],
            "recommendation": payload.get("recommendation"), "rule": payload.get("rule"), "decisive_findings": decisive,
            "critical_findings": [by_id[fid] for fid in (payload.get("critical_findings") or []) if fid in by_id],
            "what_would_change": payload.get("what_would_change") or [],
            "confidences": conf, "scenarios": [{"key": s["key"], "title": s["title"], "plausibility": s["plausibility"], "plausibility_explained": s["plausibility_explained"]}
                                              for s in item.get("scenarios") or []],
            "risk_engine": payload.get("risk_engine"), "reproducibility": item.get("reproducibility"),
            "disclaimer": payload.get("language")}


def _fmt_conf(entry: Optional[Dict[str, Any]]) -> str:
    if not entry:
        return "n/a"
    value = entry.get("value")
    return (f"{float(value):.0%} ({entry.get('label')})" if value is not None else str(entry.get("label")))


@router.post("/{investigation_id}/events")
def record_event(investigation_id: str, req: InvestigationEvent, user: dict = Depends(_reviewer())):
    """UI-reported audit events (evidence opened / scenario viewed). Only
    references that exist in the investigation are accepted."""
    item = _load_or_404(investigation_id, user)
    event_type = _text(req.event_type).upper()
    ref = _text(req.ref)
    if event_type == "EVIDENCE_OPENED":
        if not any(_text(e.get("evidence_id")) == ref for e in item.get("evidence") or []):
            raise HTTPException(status_code=400, detail="Unknown evidence reference.")
        action = "SA_INVESTIGATION_EVIDENCE_OPENED"
    elif event_type == "SCENARIO_VIEWED":
        if not any(_text(s.get("key")) == ref for s in item.get("scenarios") or []):
            raise HTTPException(status_code=400, detail="Unknown scenario reference.")
        action = "SA_INVESTIGATION_SCENARIO_VIEWED"
    else:
        raise HTTPException(status_code=400, detail="Event type is not registered.")
    _event(item["investigation_id"], user, event_type, ref, item["document_id"], audit_action=action)
    return {"status": "ok"}


@router.post("/{investigation_id}/retry")
def retry(investigation_id: str, user: dict = Depends(_admin())):
    item = _load_or_404(investigation_id, user)
    if item["state"] in FINAL_STATES:
        raise HTTPException(status_code=409, detail=f"Investigation is {item['state']}; run a new investigation instead.")
    if _text(item.get("created_by_id")) != _text(user.get("id")):
        raise HTTPException(status_code=404, detail="Investigation not found.")
    try:
        envelope = assistant_tasks.retry_request(item["request_id"], _text(user.get("id")), runner_for(user), wait_seconds())
    except Exception as exc:  # noqa: BLE001
        raise _task_error(exc) from exc
    _audit(user, "SA_INVESTIGATION_RETRY", f"{item['investigation_id']} retry requested", item["document_id"])
    fresh = load_investigation(item["investigation_id"]) or item
    return {"investigation": detail(fresh, user), "task": envelope}


@router.post("/{investigation_id}/cancel")
def cancel(investigation_id: str, user: dict = Depends(_admin())):
    item = _load_or_404(investigation_id, user)
    if _text(item.get("created_by_id")) != _text(user.get("id")):
        raise HTTPException(status_code=404, detail="Investigation not found.")
    try:
        envelope = assistant_tasks.cancel_request(item["request_id"], _text(user.get("id")))
    except Exception as exc:  # noqa: BLE001
        raise _task_error(exc) from exc
    if item["state"] in RUNNING_STATES and envelope.get("state") in {"cancelled"}:
        with _server().get_db() as db:
            db.execute("UPDATE land_investigations SET state=?, failure=?, updated_at=? WHERE investigation_id=? AND state=?",
                       (STATE_FAILED, _json({"code": "CANCELLED", "message": "Cancelled before it started."}), _now(), item["investigation_id"], STATE_CREATED))
    _audit(user, "SA_INVESTIGATION_CANCELLED", f"{item['investigation_id']} cancel requested", item["document_id"])
    fresh = load_investigation(item["investigation_id"]) or item
    return {"investigation": detail(fresh, user), "task": envelope}


@router.post("/{investigation_id}/decision")
def decide(investigation_id: str, req: DecisionRequest, user: dict = Depends(_admin())):
    """Administrator decision through the existing governance flow.

    Order of operations (fail closed): RBAC -> investigation decidable ->
    decision registered -> override note rule -> administrator password
    verified server-side -> proposal (reused or created) -> unchanged
    ``approve_proposal`` CAS -> executor records the decision once.
    The request body (password) is never logged or persisted.
    """
    item = _load_or_404(investigation_id, user)
    decision = _text(req.decision).upper()
    if decision not in DECISIONS:
        raise HTTPException(status_code=400, detail="Decision must be APPROVE, REJECT or NEEDS_REVIEW.")
    if item["state"] not in DECIDABLE_STATES:
        raise HTTPException(status_code=409, detail=f"Investigation is {item['state']} and cannot be decided.")
    note = _text(req.note)
    override = decision != DECISION_FOR_RECOMMENDATION.get(_text(item.get("recommendation")), "")
    if override and not note:
        raise HTTPException(status_code=400, detail="An override note is required when the decision differs from SA's recommendation.")
    gov = _governance()
    gov.verify_administrator_password(user, req.password)
    proposal = _decision_proposal(item, decision, note, override, user)
    executed = gov.approve_proposal(proposal["proposal_id"], user, note)
    if note and not override:
        # The SA-prepared proposal carries no administrator note; keep the
        # note the administrator typed alongside the recorded decision (it is
        # also part of the proposal's approval event).
        with _server().get_db() as db:
            db.execute(
                "UPDATE land_investigations SET decision_note=? WHERE investigation_id=? AND proposal_id=? "
                "AND (decision_note IS NULL OR decision_note='')",
                (note, item["investigation_id"], _text(proposal.get("proposal_id"))),
            )
    fresh = load_investigation(item["investigation_id"]) or item
    return {"investigation": detail(fresh, user), "proposal": executed, "override": override}
