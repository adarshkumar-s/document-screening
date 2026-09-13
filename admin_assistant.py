import os
import json
import time
import hmac
import hashlib
import uuid
import secrets
from datetime import datetime, date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Depends, status, Request
from pydantic import BaseModel, Field

# -------------------------------------------------------------------
# Safe dynamic access to the main application
# -------------------------------------------------------------------
def get_server():
    import server
    return server


def get_db_instance():
    return get_server().get_db()


def get_admin_dependency():
    server = get_server()
    return server.require_roles(server.ROLE_ADMIN)


def get_staff_dependency():
    server = get_server()
    return server.require_roles(
        server.ROLE_ADMIN,
        server.ROLE_VERIFICATION_OFFICER,
        server.ROLE_DATA_OFFICER,
    )


def log_system_audit(username: str, action: str, detail: str, doc_id: Optional[str] = None):
    get_server().log_audit(username, action, detail, doc_id)


def get_ai_client():
    return getattr(get_server(), "ai_client", None)


router = APIRouter(prefix="/api/admin/assistant", tags=["AI Admin Assistant"])

ACTION_SECRET = os.getenv("ADMIN_ACTION_SECRET", "").strip()
if not ACTION_SECRET:
    if os.getenv("APP_ENV", "development").strip().lower() in {"production", "prod"}:
        raise RuntimeError("ADMIN_ACTION_SECRET must be configured in production.")
    ACTION_SECRET = secrets.token_urlsafe(32)
TOKEN_TTL_SECONDS = 300
USED_ACTION_TOKENS = set()
TASK_TABLE_READY = False


# -------------------------------------------------------------------
# AI TASK / ROLE COMMUNICATION LAYER
# -------------------------------------------------------------------
def ensure_task_table():
    """Create the task queue without changing the existing documents/users schema."""
    global TASK_TABLE_READY
    if TASK_TABLE_READY:
        return
    with get_db_instance() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS ai_tasks (
                id TEXT PRIMARY KEY,
                record_id TEXT,
                task_type TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                priority TEXT NOT NULL DEFAULT 'MEDIUM',
                assigned_to TEXT,
                assigned_by TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                parent_task_id TEXT,
                metadata TEXT NOT NULL DEFAULT '{}',
                result TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
    TASK_TABLE_READY = True


class TaskCreateReq(BaseModel):
    record_id: Optional[str] = None
    task_type: str = "REVIEW_RECORD"
    title: str
    description: str
    priority: str = "MEDIUM"
    assigned_to: Optional[str] = None
    parent_task_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class TaskResponseReq(BaseModel):
    status: str
    message: str = ""
    result: Dict[str, Any] = Field(default_factory=dict)


class QueryReq(BaseModel):
    query: str


class ActionReq(BaseModel):
    token: str


class TaskAssignReq(BaseModel):
    task_id: str
    officer_id: str


def _now() -> float:
    return time.time()


def _task_row(row) -> Dict[str, Any]:
    if not row:
        return {}
    item = dict(row)
    for key in ("metadata", "result"):
        try:
            item[key] = json.loads(item.get(key) or "{}")
        except Exception:
            item[key] = {}
    return item


def _user_by_id_or_email(identifier: str):
    with get_db_instance() as db:
        return db.execute(
            "SELECT id, full_name, email, role, is_active FROM users WHERE id=? OR LOWER(email)=?",
            (identifier, identifier.lower()),
        ).fetchone()


def list_verification_officers() -> List[Dict[str, Any]]:
    ensure_task_table()
    with get_db_instance() as db:
        rows = db.execute(
            "SELECT id, full_name, email, role, is_active FROM users "
            "WHERE role='VERIFICATION_OFFICER' AND is_active=1 ORDER BY full_name"
        ).fetchall()
        result = []
        for row in rows:
            user = dict(row)
            active = db.execute(
                "SELECT COUNT(*) AS c FROM ai_tasks WHERE assigned_to=? "
                "AND status IN ('PENDING','ACCEPTED','IN_PROGRESS')",
                (user["id"],),
            ).fetchone()["c"]
            result.append({
                "id": user["id"],
                "name": user["full_name"],
                "email": user["email"],
                "role": user["role"],
                "active_tasks": active,
                "availability": "HIGH" if active <= 3 else ("MEDIUM" if active <= 8 else "LOW"),
            })
        return result


def get_officer_workload(officer_id: str) -> Dict[str, Any]:
    ensure_task_table()
    user = _user_by_id_or_email(officer_id)
    if not user or user["role"] != "VERIFICATION_OFFICER":
        return {"error": f"Verification Officer '{officer_id}' not found."}
    with get_db_instance() as db:
        rows = db.execute(
            "SELECT status, COUNT(*) AS c FROM ai_tasks WHERE assigned_to=? GROUP BY status",
            (user["id"],),
        ).fetchall()
    counts = {r["status"]: r["c"] for r in rows}
    active = sum(counts.get(s, 0) for s in ("PENDING", "ACCEPTED", "IN_PROGRESS"))
    return {
        "officer_id": user["id"],
        "name": user["full_name"],
        "email": user["email"],
        "active_tasks": active,
        "pending": counts.get("PENDING", 0),
        "accepted": counts.get("ACCEPTED", 0),
        "in_progress": counts.get("IN_PROGRESS", 0),
        "completed": counts.get("COMPLETED", 0),
        "returned": counts.get("RETURNED", 0),
        "escalated": counts.get("ESCALATED", 0),
        "availability": "HIGH" if active <= 3 else ("MEDIUM" if active <= 8 else "LOW"),
    }


def find_available_officer() -> Dict[str, Any]:
    officers = list_verification_officers()
    if not officers:
        return {"error": "No active Verification Officer is available."}
    return min(officers, key=lambda x: x.get("active_tasks", 999))


def get_faulty_records(limit: int = 30) -> List[Dict[str, Any]]:
    """Find records requiring attention using deterministic evidence from the DB."""
    with get_db_instance() as db:
        rows = db.execute(
            "SELECT id, filename, doc_type, status, mean_conf, fields, validation, ai_decision_support, uploaded_by, updated_at "
            "FROM documents ORDER BY updated_at DESC"
        ).fetchall()

    faulty = []
    for row in rows:
        d = dict(row)
        try:
            fields = json.loads(d.get("fields") or "{}")
        except Exception:
            fields = {}
        try:
            validation = json.loads(d.get("validation") or "{}")
        except Exception:
            validation = {}
        try:
            ai = json.loads(d.get("ai_decision_support") or "{}")
        except Exception:
            ai = {}

        reasons: List[str] = []
        severity = "MEDIUM"
        conf = float(d.get("mean_conf") or 0.0)
        if conf < 45:
            reasons.append("Very low OCR confidence")
            severity = "CRITICAL"
        elif conf < 75:
            reasons.append("Low OCR confidence")
            severity = max_severity(severity, "HIGH")

        missing = []
        invalid = []
        for name, obj in fields.items():
            if not isinstance(obj, dict):
                continue
            value = str(obj.get("value") or "").strip()
            st = str(obj.get("validation_status") or "").upper()
            if st == "MISSING" or not value:
                missing.append(name)
            elif st == "INVALID":
                invalid.append(name)
        if missing:
            reasons.append("Missing fields: " + ", ".join(missing[:4]))
            severity = max_severity(severity, "HIGH")
        if invalid:
            reasons.append("Validation issues: " + ", ".join(invalid[:4]))
            severity = max_severity(severity, "HIGH")

        corrections = ai.get("ai_corrections") or ai.get("corrections") or []
        if corrections:
            reasons.append(f"AI corrected {len(corrections)} field(s)")
            severity = max_severity(severity, "MEDIUM")

        escalation = ai.get("escalation_report")
        if escalation:
            reasons.append("OCR/AI extraction escalation")
            severity = "CRITICAL"

        consistency = ai.get("consistency") or ai.get("consistency_report")
        if consistency and isinstance(consistency, dict):
            mismatches = consistency.get("mismatched", 0) or consistency.get("mismatches", 0)
            if mismatches:
                reasons.append(f"Cross-document mismatch ({mismatches})")
                severity = max_severity(severity, "CRITICAL")

        if d.get("status") == "RETURNED_TO_DATA_OFFICER":
            reasons.append("Returned for correction")
            severity = max_severity(severity, "HIGH")

        if reasons:
            faulty.append({
                "id": d["id"],
                "filename": d["filename"],
                "doc_type": d["doc_type"],
                "status": d["status"],
                "confidence": conf,
                "owner_name": (fields.get("owner_name") or {}).get("value", "—"),
                "survey_number": (fields.get("survey_number") or {}).get("value") or (fields.get("khasra_number") or {}).get("value", "—"),
                "severity": severity,
                "reasons": reasons,
                "uploaded_by": d.get("uploaded_by"),
            })
        if len(faulty) >= limit:
            break
    return faulty


def max_severity(a: str, b: str) -> str:
    order = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
    return b if order.get(b, 0) > order.get(a, 0) else a


def create_task(
    record_id: Optional[str],
    task_type: str,
    title: str,
    description: str,
    priority: str,
    assigned_to: Optional[str],
    assigned_by: str,
    metadata: Optional[Dict[str, Any]] = None,
    parent_task_id: Optional[str] = None,
) -> Dict[str, Any]:
    ensure_task_table()
    task_id = "TASK-" + uuid.uuid4().hex[:8].upper()
    now = _now()
    with get_db_instance() as db:
        db.execute(
            "INSERT INTO ai_tasks (id, record_id, task_type, title, description, priority, assigned_to, assigned_by, status, parent_task_id, metadata, result, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, '{}', ?, ?)",
            (
                task_id, record_id, task_type, title, description,
                priority.upper(), assigned_to, assigned_by, parent_task_id,
                json.dumps(metadata or {}, ensure_ascii=False), now, now,
            ),
        )
    log_system_audit(assigned_by, "AI_TASK_CREATED", f"Created {task_type} {task_id} for record #{record_id or 'N/A'}", record_id)
    return {"id": task_id, "status": "PENDING", "record_id": record_id, "assigned_to": assigned_to, "priority": priority.upper()}


def get_tasks_for_user(user: Dict[str, Any], status_filter: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    ensure_task_table()
    with get_db_instance() as db:
        if user.get("role") == "ADMIN":
            if status_filter:
                rows = db.execute(
                    "SELECT * FROM ai_tasks WHERE status=? ORDER BY created_at DESC LIMIT ?",
                    (status_filter.upper(), limit),
                ).fetchall()
            else:
                rows = db.execute("SELECT * FROM ai_tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        else:
            if status_filter:
                rows = db.execute(
                    "SELECT * FROM ai_tasks WHERE assigned_to=? AND status=? ORDER BY created_at DESC LIMIT ?",
                    (user["id"], status_filter.upper(), limit),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM ai_tasks WHERE assigned_to=? ORDER BY created_at DESC LIMIT ?",
                    (user["id"], limit),
                ).fetchall()

        result = []
        for row in rows:
            item = _task_row(row)
            assigned = _user_by_id_or_email(item.get("assigned_to")) if item.get("assigned_to") else None
            item["assigned_name"] = assigned["full_name"] if assigned else "Unassigned"
            item["created_time"] = datetime.fromtimestamp(item["created_at"]).strftime("%d %b %Y %H:%M")
            item["updated_time"] = datetime.fromtimestamp(item["updated_at"]).strftime("%d %b %Y %H:%M")
            result.append(item)
        return result


def get_system_statistics() -> Dict[str, Any]:
    with get_db_instance() as db:
        total = db.execute("SELECT COUNT(*) as c FROM documents").fetchone()["c"]
        pending = db.execute("SELECT COUNT(*) as c FROM documents WHERE status='PENDING_VERIFICATION'").fetchone()["c"]
        approved = db.execute("SELECT COUNT(*) as c FROM documents WHERE status='APPROVED'").fetchone()["c"]
        returned = db.execute("SELECT COUNT(*) as c FROM documents WHERE status='RETURNED_TO_DATA_OFFICER'").fetchone()["c"]
        rejected = db.execute("SELECT COUNT(*) as c FROM documents WHERE status='REJECTED'").fetchone()["c"]
        today_ts = datetime.combine(date.today(), datetime.min.time()).timestamp()
        today_proc = db.execute("SELECT COUNT(*) as c FROM documents WHERE updated_at >= ?", (today_ts,)).fetchone()["c"]
    return {
        "total_documents": total,
        "pending_verification": pending,
        "approved": approved,
        "returned": returned,
        "rejected": rejected,
        "processed_today": today_proc,
    }


def get_pending_records(limit: int = 10) -> List[Dict[str, Any]]:
    with get_db_instance() as db:
        cur = db.execute("SELECT id, filename, doc_type, status, mean_conf, fields FROM documents WHERE status='PENDING_VERIFICATION' LIMIT ?", (limit,))
        results = []
        for r in cur.fetchall():
            item = dict(r)
            f = json.loads(item.get("fields") or "{}")
            results.append({
                "id": item["id"], "filename": item["filename"],
                "owner_name": f.get("owner_name", {}).get("value", "—"),
                "status": item["status"], "confidence": item["mean_conf"],
            })
        return results


def get_low_confidence_records(threshold: int = 75, limit: int = 10) -> List[Dict[str, Any]]:
    with get_db_instance() as db:
        cur = db.execute("SELECT id, filename, status, mean_conf, fields FROM documents WHERE mean_conf < ? LIMIT ?", (threshold, limit))
        results = []
        for r in cur.fetchall():
            item = dict(r)
            f = json.loads(item.get("fields") or "{}")
            results.append({
                "id": item["id"], "filename": item["filename"],
                "owner_name": f.get("owner_name", {}).get("value", "—"),
                "status": item["status"], "confidence": item["mean_conf"],
            })
        return results


def search_records(query: str, limit: int = 10) -> List[Dict[str, Any]]:
    q = query.strip().lower()
    with get_db_instance() as db:
        rows = db.execute("SELECT id, filename, doc_type, status, mean_conf, fields, created_at FROM documents ORDER BY created_at DESC").fetchall()
    results = []
    for r in rows:
        item = dict(r)
        f = json.loads(item.get("fields") or "{}")
        owner = (f.get("owner_name", {}).get("value") or "").lower()
        survey = (f.get("survey_number", {}).get("value") or f.get("khasra_number", {}).get("value") or "").lower()
        village = (f.get("village", {}).get("value") or "").lower()
        doc_id = str(item["id"]).lower()
        if q in owner or q in survey or q in village or q in doc_id:
            results.append({
                "id": item["id"], "filename": item["filename"], "doc_type": item["doc_type"],
                "status": item["status"], "confidence": item["mean_conf"],
                "owner_name": f.get("owner_name", {}).get("value", "—"),
                "survey_number": f.get("survey_number", {}).get("value") or f.get("khasra_number", {}).get("value", "—"),
                "village": f.get("village", {}).get("value", "—"),
            })
            if len(results) >= limit:
                break
    return results


def get_record_details(record_id: str) -> Dict[str, Any]:
    with get_db_instance() as db:
        row = db.execute("SELECT * FROM documents WHERE id=?", (record_id,)).fetchone()
        if not row:
            return {"error": f"Record '{record_id}' not found."}
        d = dict(row)
        for key in ("fields", "validation", "ai_decision_support", "original_fields"):
            if key in d:
                try:
                    d[key] = json.loads(d.get(key) or "{}")
                except Exception:
                    d[key] = {}
        return d


def get_recent_activity(limit: int = 15) -> List[Dict[str, Any]]:
    with get_db_instance() as db:
        cur = db.execute("SELECT ts, username, action, detail, doc_id FROM audit ORDER BY id DESC LIMIT ?", (limit,))
        return [{
            "time": datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M:%S"),
            "user": r["username"], "action": r["action"], "detail": r["detail"], "doc_id": r["doc_id"]
        } for r in cur.fetchall()]


def generate_action_token(payload: dict) -> str:
    payload = dict(payload)
    payload["exp"] = time.time() + TOKEN_TTL_SECONDS
    payload_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
    sig = hmac.new(ACTION_SECRET.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    return f"{payload_bytes.hex()}.{sig}"


def verify_and_consume_token(token: str) -> Optional[dict]:
    if token in USED_ACTION_TOKENS:
        return None
    try:
        hex_data, sig = token.split(".", 1)
        payload_bytes = bytes.fromhex(hex_data)
        expected_sig = hmac.new(ACTION_SECRET.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        payload = json.loads(payload_bytes.decode("utf-8"))
        if time.time() > payload.get("exp", 0):
            return None
        USED_ACTION_TOKENS.add(token)
        return payload
    except Exception:
        return None


def recommend_assignment(record_id: str) -> Dict[str, Any]:
    record = get_record_details(record_id)
    if record.get("error"):
        return record
    officer = find_available_officer()
    if officer.get("error"):
        return officer
    confidence = float(record.get("mean_conf") or 0.0)
    priority = "CRITICAL" if confidence < 45 else ("HIGH" if confidence < 75 else "MEDIUM")
    return {
        "record_id": record_id,
        "officer": officer,
        "priority": priority,
        "reason": f"{officer['name']} has the lowest active AI task load ({officer['active_tasks']}) among active Verification Officers.",
    }



def _create_ai_proposal(action_type: str, target_type: str, target_ids: List[str], before: Dict[str, Any], after: Dict[str, Any], reason: str, evidence: List[Dict[str, Any]], confidence: float, risk: str = "MEDIUM"):
    from ai_governance import create_proposal
    return create_proposal({"action_type": action_type, "target_type": target_type, "target_ids": [str(x) for x in target_ids], "before": before, "after": after, "reason": reason, "evidence": evidence, "confidence": max(0.0, min(1.0, float(confidence))), "risk": risk}, created_by="AI_ASSISTANT")

def prepare_assignment_action(record_id: str, officer_id: str) -> Dict[str, Any]:
    rec = get_record_details(record_id)
    if rec.get("error"):
        return rec
    officer = _user_by_id_or_email(officer_id)
    if not officer or officer["role"] != "VERIFICATION_OFFICER" or not officer["is_active"]:
        return {"error": "Selected Verification Officer is not active or does not exist."}
    proposal = _create_ai_proposal("ASSIGN_AI_TASK", "DOCUMENT", [str(record_id)],
        {"documents": {str(record_id): {"status": rec.get("status"), "mean_conf": rec.get("mean_conf")}}},
        {"assigned_to": str(officer["id"]), "task_type": "VERIFY_RECORD", "title": "Verify land record #" + str(record_id),
         "description": "Review OCR output, validation findings and unresolved discrepancies.",
         "priority": "CRITICAL" if float(rec.get("mean_conf") or 0) < 45 else ("HIGH" if float(rec.get("mean_conf") or 0) < 75 else "MEDIUM")},
        "Assign to the selected active Verification Officer based on current workload.",
        [{"type":"record","record_id":str(record_id),"status":rec.get("status"),"ocr_confidence":rec.get("mean_conf")}, {"type":"officer","officer_id":str(officer["id"]),"name":officer["full_name"]}], 0.9, "MEDIUM")
    return {"confirmation_required": True, "proposal": proposal, "proposal_id": proposal["proposal_id"], "action_type": proposal["action_type"], "action_description": "Assign record #" + str(record_id) + " to " + officer["full_name"], "target_id": record_id, "target_display": "Record #" + str(record_id) + " → " + officer["full_name"] + " (Verification Officer)"}


def prepare_distribution_action(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    officers = list_verification_officers()
    if not officers:
        return {"error": "No active Verification Officers are available."}
    assignments = []
    sorted_officers = sorted(officers, key=lambda x: x.get("active_tasks", 0))
    loads = {o["id"]: o.get("active_tasks", 0) for o in sorted_officers}
    for rec in records:
        oid = min(loads, key=loads.get)
        assignments.append({"record_id": str(rec["id"]), "officer_id": str(oid), "priority": rec.get("severity", "MEDIUM")})
        loads[oid] += 1
    proposal = _create_ai_proposal("ASSIGN_AI_TASK", "DOCUMENT", [str(r["id"]) for r in records],
        {"records": {str(r["id"]): {"status": r.get("status"), "mean_conf": r.get("confidence")} for r in records}},
        {"assignments": assignments, "task_type": "VERIFY_FAULTY_RECORD", "title": "Review flagged land record",
         "description": "Review OCR, validation and consistency evidence requiring attention."},
        "Balance flagged verification work using current deterministic officer workload.",
        [{"type":"workload","officers":[{"id":o["id"],"active_tasks":o["active_tasks"]} for o in officers]},
         {"type":"records","count":len(records)}], 0.88, "MEDIUM")
    preview = []
    by_id = {str(o["id"]): o["name"] for o in officers}
    for a in assignments:
        preview.append(f"#{a['record_id']} → {by_id.get(a['officer_id'], a['officer_id'])}")
    return {
        "confirmation_required": True,
        "action_type": "distribute_faulty_records",
        "action_description": f"Distribute {len(assignments)} faulty records across available Verification Officers",
        "target_id": "BULK",
        "target_display": "; ".join(preview[:8]) + ("; …" if len(preview) > 8 else ""),
        "proposal": proposal,
        "proposal_id": proposal["proposal_id"],
        "assignment_count": len(assignments),
    }


def prepare_admin_action(action_type: str, target_identifier: str, new_role: Optional[str] = None) -> Dict[str, Any]:
    with get_db_instance() as db:
        if action_type in ["disable_user", "change_user_role"]:
            user = db.execute("SELECT id, full_name, email, role, is_active FROM users WHERE id=? OR LOWER(email)=?", (target_identifier, target_identifier.lower())).fetchone()
            if not user:
                return {"error": f"User '{target_identifier}' not found."}
            target_id = user["id"]
            target_name = f"{user['full_name']} ({user['email']}, Role: {user['role']})"
        elif action_type in ["reprocess_document", "run_consistency_check"]:
            doc = db.execute("SELECT id, filename FROM documents WHERE id=?", (target_identifier,)).fetchone()
            if not doc:
                return {"error": f"Document '{target_identifier}' not found."}
            target_id = doc["id"]
            target_name = f"Document #{doc['id']} ({doc['filename']})"
        else:
            return {"error": f"Unsupported action '{action_type}'."}
    payload = {"action_type": action_type, "target_id": target_id, "parameters": {"new_role": new_role} if new_role else {}}
    token = generate_action_token(payload)
    descriptions = {
        "disable_user": f"Deactivate user account for {target_name}",
        "change_user_role": f"Update role of {target_name} to '{new_role}'",
        "reprocess_document": f"Queue {target_name} for reprocessing",
        "run_consistency_check": f"Run consistency verification on {target_name}",
    }
    return {"confirmation_required": True, "action_type": action_type, "action_description": descriptions[action_type], "target_id": target_id, "target_display": target_name, "token": token}


def execute_action_in_db(payload: dict, admin_user: dict) -> Dict[str, Any]:
    action_type = payload["action_type"]
    target_id = payload["target_id"]
    params = payload.get("parameters", {})
    if action_type == "assign_record":
        officer = _user_by_id_or_email(str(params.get("officer_id", "")))
        if not officer or officer["role"] != "VERIFICATION_OFFICER" or not officer["is_active"]:
            return {"success": False, "message": "The selected Verification Officer is no longer active."}
        rec = get_record_details(str(target_id))
        if rec.get("error"):
            return {"success": False, "message": rec["error"]}
        priority = "CRITICAL" if float(rec.get("mean_conf") or 0) < 45 else ("HIGH" if float(rec.get("mean_conf") or 0) < 75 else "MEDIUM")
        task = create_task(
            record_id=str(target_id), task_type="VERIFY_RECORD",
            title=f"Verify land record #{target_id}",
            description="Review this record, its OCR result, validation findings and any unresolved discrepancies.",
            priority=priority, assigned_to=str(officer["id"]), assigned_by=admin_user.get("full_name", "Administrator"),
            metadata={"source": "ADMIN_AI", "record_status": rec.get("status"), "ocr_confidence": rec.get("mean_conf")},
        )
        log_system_audit(admin_user.get("full_name", "Administrator"), "AI_ASSIGN_RECORD", f"Assigned #{target_id} to {officer['full_name']} via AI task {task['id']}", target_id)
        return {"success": True, "message": f"Record #{target_id} was assigned to {officer['full_name']}. Task {task['id']} is now in their task inbox.", "task": task}

    if action_type == "distribute_faulty_records":
        created = []
        officers = {str(o["id"]): o["name"] for o in list_verification_officers()}
        for a in params.get("assignments", []):
            oid = str(a.get("officer_id"))
            rid = str(a.get("record_id"))
            if oid not in officers:
                continue
            created.append(create_task(
                record_id=rid, task_type="VERIFY_FAULTY_RECORD",
                title=f"Review flagged record #{rid}",
                description="Review the record because the system detected OCR, validation, or consistency evidence requiring attention.",
                priority=str(a.get("priority", "MEDIUM")), assigned_to=oid,
                assigned_by=admin_user.get("full_name", "Administrator"), metadata={"source": "ADMIN_AI_BULK_DISTRIBUTION"},
            ))
        log_system_audit(admin_user.get("full_name", "Administrator"), "AI_DISTRIBUTE_RECORDS", f"Distributed {len(created)} records through AI task dispatcher")
        return {"success": True, "message": f"Created {len(created)} verification tasks and distributed them across active Verification Officers.", "tasks": created}

    with get_db_instance() as db:
        if action_type == "disable_user":
            db.execute("UPDATE users SET is_active=0 WHERE id=?", (target_id,))
            log_system_audit(admin_user.get("full_name", "Admin"), "AI_DEACTIVATE_USER", f"Disabled user #{target_id}", target_id)
            return {"success": True, "message": f"User #{target_id} has been deactivated."}
        if action_type == "change_user_role":
            new_r = params.get("new_role", "VIEWER").upper()
            if new_r not in get_server().VALID_ROLES:
                return {"success": False, "message": "Invalid role."}
            db.execute("UPDATE users SET role=?, version=version+1 WHERE id=?", (new_r, target_id))
            log_system_audit(admin_user.get("full_name", "Admin"), "AI_UPDATE_ROLE", f"Changed role of #{target_id} to {new_r}", target_id)
            return {"success": True, "message": f"User #{target_id} role updated to {new_r}."}
        if action_type == "reprocess_document":
            db.execute("UPDATE documents SET status='DRAFT' WHERE id=?", (target_id,))
            log_system_audit(admin_user.get("full_name", "Admin"), "AI_REPROCESS", f"Reset status of doc #{target_id}", target_id)
            return {"success": True, "message": f"Document #{target_id} set to DRAFT for reprocessing."}
    return {"success": False, "message": "Action could not be executed."}


# -------------------------------------------------------------------
# Assistant intelligence
# -------------------------------------------------------------------
SYSTEM_INSTRUCTION = """
You are the AI Admin Assistant for the Digital India Land Records Modernization Programme (DILRMP).
You are an operations assistant, not the legal authority.

You can:
- monitor records and OCR/AI quality
- report Verification Officer workload and performance
- find records requiring attention
- recommend who should receive a record
- prepare assignment/distribution actions
- explain record history and Audit Trail activity
- coordinate work through the AI task inbox

Rules:
1. Never invent records, users, workload or results.
2. Never use direct database write access. Consequential operations may only become registered proposals; execution requires explicit Administrator approval through the server-side Approval Center.
3. Never call a document fraudulent or legally invalid. Use neutral wording such as possible mismatch, flagged for review, low OCR confidence, or requires verification.
4. AI may recommend or prepare a task, but Administrator approval is required before any consequential mutation.
5. The backend/RBAC system is authoritative.
6. Keep explanations concise, factual and useful to an Administrator.
"""


def _record_id_from_prompt(prompt: str) -> Optional[str]:
    import re
    m = re.search(r"(?:record|document|lr)\s*#?\s*(\d+)", prompt.lower())
    return m.group(1) if m else None


def _report_officers() -> str:
    officers = list_verification_officers()
    if not officers:
        return "There are no active Verification Officers currently available."
    lines = ["Verification Officer report:"]
    for o in officers:
        lines.append(f"- {o['name']}: {o['active_tasks']} active task(s), availability {o['availability']}")
    return "\n".join(lines)


def _attention_report() -> str:
    faulty = get_faulty_records(20)
    critical = sum(1 for x in faulty if x["severity"] == "CRITICAL")
    high = sum(1 for x in faulty if x["severity"] == "HIGH")
    medium = len(faulty) - critical - high
    if not faulty:
        return "No records currently meet the deterministic attention rules."
    lines = [f"I found {len(faulty)} records requiring attention: {critical} critical, {high} high, {medium} medium."]
    for r in faulty[:8]:
        lines.append(f"- #{r['id']} [{r['severity']}]: {'; '.join(r['reasons'][:2])}")
    return "\n".join(lines)


def get_operational_intelligence() -> Dict[str, Any]:
    stats = get_system_statistics()
    low = get_low_confidence_records(75, 20)
    faulty = get_faulty_records(30)
    officers = list_verification_officers()
    tasks = get_tasks_for_user({"id": None, "role": "ADMIN"}, limit=100)
    active = [t for t in tasks if str(t.get("status","")).upper() in ("PENDING","ACCEPTED","IN_PROGRESS")]
    overdue = []
    now = time.time()
    for t in active:
        if now - float(t.get("updated_at") or t.get("created_at") or now) > 7*86400:
            overdue.append(t)
    return {
        "system": stats,
        "low_confidence": low[:20],
        "attention_records": faulty[:30],
        "officer_workload": officers,
        "active_ai_tasks": active[:50],
        "stale_ai_tasks": overdue[:30],
        "recent_activity": get_recent_activity(15),
    }


def propose_for_record(action_type: str, record_id: str, reason: str, confidence: float, risk: str = "MEDIUM", after: Optional[Dict[str, Any]] = None):
    rec = get_record_details(record_id)
    if rec.get("error"):
        return rec
    before = {"documents": {str(record_id): {"status": rec.get("status"), "mean_conf": rec.get("mean_conf")}}}
    evidence = [{"type":"document","record_id":str(record_id),"status":rec.get("status"),"ocr_confidence":rec.get("mean_conf")},
                {"type":"validation","value":rec.get("validation",{})}]
    return _create_ai_proposal(action_type, "DOCUMENT", [str(record_id)], before, after or {}, reason, evidence, confidence, risk)


def run_assistant_turn(prompt: str) -> Dict[str, Any]:
    ensure_task_table()
    lower = prompt.lower().strip()

    # Deterministic property-history analysis. This is read-only and never mutates records.
    if any(k in lower for k in ["property history", "ownership history", "ownership transfer", "chain of title"]):
        import re
        match = re.search(r"(?:property|parcel|survey|record|document)\s*(?:id|number|no\.?)?\s*[:#-]?\s*([A-Za-z0-9][A-Za-z0-9_-]{2,})", prompt, re.I)
        if not match:
            return {"response":"Please include a property or parcel identifier, for example: 'Show property history for DEMO-PROP-103-A'.","records":[],"action_card":None}
        identifier=match.group(1)
        try:
            from land_intelligence import _history_for_property
            with get_db_instance() as db:
                row=db.execute("SELECT property_id FROM properties WHERE property_id=? OR parcel_id=? OR survey_number=?", (identifier,identifier,identifier)).fetchone()
            if not row:
                return {"response":f"No property matching '{identifier}' was found.","records":[],"action_card":None}
            history=_history_for_property(row["property_id"])
            lines=["PROPERTY HISTORY"]
            for event in history.get("events",[]):
                lines.append(f"- {event.get('year') or 'Year unavailable'} | {event.get('document_type')} | Owner: {event.get('owner') or 'Unknown'} | Survey: {event.get('survey_number') or 'Unknown'} | Khasra: {event.get('khasra_number') or 'Unknown'}")
            for finding in history.get("findings",[]):
                lines += ["", "ASSESSMENT", f"- {finding['title']}", f"- {finding['reason']}", f"- Human action: {finding['human_action']}"]
            if not history.get("findings"):
                lines += ["", "ASSESSMENT", "- No ownership change was detected from the linked records."]
            lines += ["", "This is evidence-based decision support, not a legal determination. No authoritative record was changed."]
            return {"response":"\n".join(lines),"records":[],"action_card":None}
        except Exception:
            return {"response":"Property history is temporarily unavailable; existing document and parcel workflows remain usable.","records":[],"action_card":None}

    # Read-only location intelligence and governed pin proposals.
    if any(k in lower for k in ["where is", "location", "show on map", "map location"]) and any(k in lower for k in ["property", "parcel", "survey", "record"]):
        import re
        match = re.search(r"(?:property|parcel|survey|record|document)\s*(?:id|number|no\.?)?\s*[:#-]?\s*([A-Za-z0-9][A-Za-z0-9_-]{2,})", prompt, re.I)
        if not match:
            return {"response":"Please include a property or parcel identifier, for example: 'Where is property DEMO-PROP-103-A?'","records":[],"action_card":None}
        identifier=match.group(1)
        try:
            with get_db_instance() as db:
                row=db.execute("""SELECT property_id,parcel_id,district,taluka,village,survey_number,khasra_number,
                                         latitude,longitude,location_status,location_source,location_updated_at
                                  FROM properties WHERE property_id=? OR parcel_id=? OR survey_number=? LIMIT 1""",
                               (identifier,identifier,identifier)).fetchone()
            if not row:
                return {"response":f"No property matching '{identifier}' was found.","records":[],"action_card":None}
            p=dict(row)
            status=p.get("location_status") or "UNRESOLVED"
            if status=="EXACT_PIN":
                meaning="EXACT human-set location"
            elif status=="VILLAGE_LEVEL":
                meaning="VILLAGE-LEVEL approximate location; this does not represent the exact parcel."
            elif status=="PARCEL_GEOMETRY":
                meaning="PARCEL GEOMETRY location from the project-owned dataset."
            else:
                meaning="UNRESOLVED location; no coordinate was fabricated."
            return {"response":f"Location for {p['property_id']}: {meaning}\\nCoordinates: {p.get('latitude') or '—'}, {p.get('longitude') or '—'}\\nVillage: {p.get('village') or '—'} | Taluka: {p.get('taluka') or '—'} | District: {p.get('district') or '—'}\\nNo authoritative legal conclusion is implied.",
                    "records":[p],"action_card":None,
                    "map_context":{"property_id":p["property_id"],"location_status":status,"latitude":p.get("latitude"),"longitude":p.get("longitude")}}
        except Exception:
            return {"response":"Property location is temporarily unavailable; no record was changed.","records":[],"action_card":None}

    if "set exact pin" in lower or "pin this property" in lower:
        import re
        match = re.search(r"(?:property|parcel|record)\s*(?:id|number|no\.?)?\s*[:#-]?\s*([A-Za-z0-9][A-Za-z0-9_-]{2,})", prompt, re.I)
        coords = re.search(r"(-?\d{1,3}(?:\.\d+)?)\s*[, ]\s*(-?\d{1,3}(?:\.\d+)?)", prompt)
        if not match or not coords:
            return {"response":"To prepare an exact-pin proposal, provide the property/parcel ID and coordinates, for example: 'Set exact pin for DEMO-PROP-103-A at 28.6221, 77.1050'.","records":[],"action_card":None}
        pid, lat, lon = match.group(1), float(coords.group(1)), float(coords.group(2))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return {"response":"The requested coordinates are outside WGS84 bounds.","records":[],"action_card":None}
        with get_db_instance() as db:
            row=db.execute("SELECT property_id,location_status,latitude,longitude,location_updated_at FROM properties WHERE property_id=? OR parcel_id=?",(pid,pid)).fetchone()
        if not row:
            return {"response":f"Property '{pid}' was not found.","records":[],"action_card":None}
        before={"properties":{row["property_id"]:{k:row[k] for k in ("location_status","latitude","longitude","location_updated_at")}}}
        proposal=_create_ai_proposal("SET_PROPERTY_LOCATION","PROPERTY",[row["property_id"]],before,
            {"latitude":lat,"longitude":lon,"reason":"Exact pin proposed by the administrator through the AI assistant."},
            "AI prepared an exact location pin for administrator review.",[{"type":"property","property_id":row["property_id"]},{"type":"coordinates","latitude":lat,"longitude":lon}],0.95,"HIGH")
        return {"response":"I prepared an exact-pin proposal. It has NOT changed the property. An administrator must approve it in the AI Approval Center.",
                "records":[dict(row)],"action_card":{"confirmation_required":True,"proposal":proposal,"proposal_id":proposal["proposal_id"],
                "action_type":proposal["action_type"],"action_description":"Set exact property pin","target_display":row["property_id"]}}

    if "clear exact pin" in lower or "remove exact pin" in lower:
        import re
        match = re.search(r"(?:property|parcel|record)\s*(?:id|number|no\.?)?\s*[:#-]?\s*([A-Za-z0-9][A-Za-z0-9_-]{2,})", prompt, re.I)
        if not match:
            return {"response":"Please include the property or parcel ID whose exact pin should be cleared.","records":[],"action_card":None}
        pid=match.group(1)
        with get_db_instance() as db:
            row=db.execute("SELECT property_id,location_status,latitude,longitude,location_updated_at FROM properties WHERE property_id=? OR parcel_id=?",(pid,pid)).fetchone()
        if not row:
            return {"response":f"Property '{pid}' was not found.","records":[],"action_card":None}
        before={"properties":{row["property_id"]:{k:row[k] for k in ("location_status","latitude","longitude","location_updated_at")}}}
        proposal=_create_ai_proposal("CLEAR_PROPERTY_LOCATION","PROPERTY",[row["property_id"]],before,{"reason":"Clear exact pin proposed by the administrator through the AI assistant."},
            "AI prepared an exact-pin removal proposal for administrator review.",[{"type":"property","property_id":row["property_id"]},{"type":"current_location","status":row["location_status"],"latitude":row["latitude"],"longitude":row["longitude"]}],0.95,"HIGH")
        return {"response":"I prepared a clear-pin proposal. It has NOT changed the property. An administrator must approve it in the AI Approval Center.",
                "records":[dict(row)],"action_card":{"confirmation_required":True,"proposal":proposal,"proposal_id":proposal["proposal_id"],
                "action_type":proposal["action_type"],"action_description":"Clear exact property pin","target_display":row["property_id"]}}

    # Existing administrative actions
    if any(k in lower for k in ["disable", "deactivate", "change role", "promote", "demote"]):
        return {"response": "For security, the AI assistant cannot prepare or execute account deactivation or role changes. Perform those administrator controls through the existing Users interface; the AI may only report user/workload information.", "records": [], "action_card": None}

    # Officer reporting / availability
    if ("verification officer" in lower or "verifier" in lower) and any(k in lower for k in ["report", "workload", "performance", "busy", "free", "available", "who"]):
        officers = list_verification_officers()
        if "free" in lower or "available" in lower:
            available = [o for o in officers if o["availability"] == "HIGH"]
            text = "Available Verification Officers:\n" + "\n".join(f"- {o['name']} ({o['active_tasks']} active tasks)" for o in available) if available else "No Verification Officer is currently in the high-availability range."
        else:
            text = _report_officers()
        return {"response": text, "records": [], "action_card": None}

    # Urgency and operational intelligence
    if any(k in lower for k in ["urgent", "most urgent", "priority", "critical now", "what should i do"]):
        intel = get_operational_intelligence()
        critical = [r for r in intel["attention_records"] if r.get("severity") == "CRITICAL"]
        overdue = intel["stale_ai_tasks"]
        lines = ["CRITICAL", f"- {len(critical)} record(s) meet critical deterministic attention rules.", f"- {len(overdue)} AI task(s) are stale by the 7-day operational threshold.", "", "HIGH", f"- {len(intel['low_confidence'])} record(s) have OCR confidence below 75.", "", "RECOMMENDED NEXT ACTIONS", "1. Review critical records first.", "2. Review stale or unassigned verification work.", "3. Use the Approval Center for any consequential change.", "", "These are recommendations only. No authoritative record was changed."]
        return {"response":"\n".join(lines), "records": critical[:8], "action_card": None}

    # Prepare a governed reprocessing proposal
    if ("reprocess" in lower or "re-processing" in lower) and ("record" in lower or "document" in lower):
        record_id = _record_id_from_prompt(prompt)
        if not record_id:
            return {"response":"Please include a record number, for example: 'Propose reprocessing document #123'.", "records":[], "action_card":None}
        proposal = propose_for_record("REQUEST_REPROCESSING", record_id, "Administrator requested reprocessing; AI prepared the change for review.", 0.94, "LOW")
        if proposal.get("error"): return {"response":proposal["error"],"records":[],"action_card":None}
        return {"response":"I prepared a reprocessing proposal. It is not executed. An administrator must review and approve it in the AI Approval Center.", "records":[{"id":record_id}], "action_card":{"confirmation_required":True,"proposal":proposal,"proposal_id":proposal["proposal_id"],"action_type":proposal["action_type"],"action_description":"Request document reprocessing","target_display":"Document #"+str(record_id)}}

    # Prepare a governed escalation proposal
    if "escalate" in lower and ("record" in lower or "document" in lower):
        record_id = _record_id_from_prompt(prompt)
        if not record_id:
            return {"response":"Please include a record number, for example: 'Escalate record #123'.", "records":[], "action_card":None}
        proposal = propose_for_record("ESCALATE_RECORD", record_id, "Escalation recommended for administrator review based on the selected record.", 0.9, "HIGH", {"assigned_to":None,"task_type":"ADMIN_REVIEW","title":"Administrator review for record #"+str(record_id),"priority":"CRITICAL"})
        if proposal.get("error"): return {"response":proposal["error"],"records":[],"action_card":None}
        return {"response":"I prepared an escalation proposal. No record status was changed.", "records":[{"id":record_id}], "action_card":{"confirmation_required":True,"proposal":proposal,"proposal_id":proposal["proposal_id"],"action_type":proposal["action_type"],"action_description":"Escalate record for administrator review","target_display":"Record #"+str(record_id)}}
    # Faulty records / attention
    if any(k in lower for k in ["faulty", "problematic", "problem records", "records with issues", "needs my attention"]):
        faulty = get_faulty_records(20)
        return {"response": _attention_report(), "records": faulty, "action_card": None}

    # Assign one record
    if "assign" in lower and ("record" in lower or "document" in lower):
        record_id = _record_id_from_prompt(prompt)
        if not record_id:
            return {"response": "Please include the record number, for example: 'Assign record #1042 to a free Verification Officer'.", "records": [], "action_card": None}
        recs = get_record_details(record_id)
        if recs.get("error"):
            return {"response": recs["error"], "records": [], "action_card": None}
        officer_id = None
        import re
        # Explicit officer by name/email after 'to'
        m = re.search(r"\bto\s+(.+)$", prompt, re.I)
        if m:
            target_text = m.group(1).strip()
            for o in list_verification_officers():
                if o["name"].lower() in target_text.lower() or o["email"].lower() in target_text.lower():
                    officer_id = str(o["id"])
                    break
        recommendation = recommend_assignment(record_id) if officer_id is None else None
        chosen = recommendation["officer"] if recommendation else _user_by_id_or_email(officer_id)
        if recommendation and recommendation.get("error"):
            return {"response": recommendation["error"], "records": [], "action_card": None}
        if chosen is None:
            return {"response": "I could not identify an active Verification Officer for this assignment.", "records": [], "action_card": None}
        oid = str(chosen["id"])
        card = prepare_assignment_action(record_id, oid)
        if card.get("error"):
            return {"response": card["error"], "records": [], "action_card": None}
        reason = recommendation["reason"] if recommendation else f"You selected {chosen['full_name']}."
        return {
            "response": f"Record #{record_id} is ready for assignment. {reason}\nThe task will appear in the Verification Officer's AI Task Inbox after confirmation.",
            "records": [{"id": record_id, "status": recs.get("status"), "owner_name": (recs.get("fields", {}).get("owner_name") or {}).get("value", "—"), "filename": recs.get("filename")}],
            "action_card": card,
        }

    # Bulk distribution
    if "distribute" in lower and any(k in lower for k in ["faulty", "problem", "flagged", "records"]):
        faulty = get_faulty_records(30)
        if not faulty:
            return {"response": "There are no records currently requiring attention under the deterministic checks.", "records": [], "action_card": None}
        card = prepare_distribution_action(faulty)
        return {"response": f"I prepared a balanced distribution for {len(faulty)} records across the active Verification Officers. Please confirm to create their tasks.", "records": faulty, "action_card": card}

    # Task/status questions
    if "task" in lower and any(k in lower for k in ["pending", "assigned", "inbox", "status", "today"]):
        tasks = get_tasks_for_user({"id": None, "role": "ADMIN"}, limit=30)
        pending = [t for t in tasks if t["status"] in ("PENDING", "ACCEPTED", "IN_PROGRESS")]
        lines = [f"There are {len(pending)} active AI tasks."]
        for t in pending[:10]:
            lines.append(f"- {t['id']} | #{t.get('record_id') or '—'} | {t['priority']} | {t['assigned_name']} | {t['status']}")
        return {"response": "\n".join(lines), "records": [], "action_card": None}

    stats = get_system_statistics()
    pending = get_pending_records(limit=5)
    low_ocr = get_low_confidence_records(limit=5)
    faulty = get_faulty_records(8)
    officers = list_verification_officers()
    recent = get_recent_activity(limit=5)
    intelligence = get_operational_intelligence()
    context = {
        "statistics": stats,
        "pending_records": pending,
        "low_confidence_records": low_ocr,
        "faulty_records": faulty,
        "verification_officers": officers,
        "recent_activity": recent,
        "operational_intelligence": intelligence,
        "governance": {"consequential_actions_require_admin_approval": True},
    }

    records_found = []
    if "pending" in lower:
        records_found = pending
    elif "low" in lower or "confidence" in lower:
        records_found = low_ocr
    elif any(k in lower for k in ["search", "find", "show"]):
        cleaned = lower
        for word in ("search", "find", "show", "records"):
            cleaned = cleaned.replace(word, "")
        cleaned = cleaned.strip()
        if cleaned:
            records_found = search_records(cleaned, limit=8)

    client = get_ai_client()
    if not client:
        return {"response": f"System Metrics: Total={stats['total_documents']}, Pending={stats['pending_verification']}, Approved={stats['approved']}. {len(faulty)} records currently require attention.", "records": records_found, "action_card": None}

    try:
        from google.genai import types
        model_prompt = f"{SYSTEM_INSTRUCTION}\n\nLive system data (authoritative):\n{json.dumps(context, ensure_ascii=False, indent=2)}\n\nAdministrator request:\n{prompt}"
        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=model_prompt,
            config=types.GenerateContentConfig(temperature=0.15),
        )
        return {"response": (response.text or "").strip(), "records": records_found, "action_card": None}
    except Exception as exc:
        return {"response": f"Database information is available, but AI explanation is temporarily unavailable: {str(exc)}", "records": records_found, "action_card": None}


def build_system_briefing() -> str:
    stats = get_system_statistics()
    low_ocr = get_low_confidence_records(limit=5)
    faulty = get_faulty_records(20)
    officers = list_verification_officers()
    tasks = get_tasks_for_user({"id": None, "role": "ADMIN"}, limit=100)
    active_tasks = [t for t in tasks if t["status"] in ("PENDING", "ACCEPTED", "IN_PROGRESS")]
    critical = sum(1 for r in faulty if r["severity"] == "CRITICAL")
    fallback = f"""# SYSTEM BRIEFING
## Activity
- Processed today: {stats.get('processed_today', 0)}
- Total records: {stats.get('total_documents', 0)}

## Attention Required
- Records requiring attention: {len(faulty)}
- Critical: {critical}
- Low OCR confidence sample: {len(low_ocr)}

## Verification
- Pending records: {stats.get('pending_verification', 0)}
- Active AI tasks: {len(active_tasks)}
- Active Verification Officers: {len(officers)}

## Workload
""" + "\n".join(f"- {o['name']}: {o['active_tasks']} active task(s), {o['availability']} availability" for o in officers[:8]) + "\n\n## System Status\n- Operational.\n"

    client = get_ai_client()
    if not client:
        return fallback
    try:
        from google.genai import types
        prompt = f"""{SYSTEM_INSTRUCTION}\nGenerate a concise administrator briefing in Markdown using only this data:\n{fallback}\nDo not invent numbers."""
        res = client.models.generate_content(model="gemini-3.6-flash", contents=prompt, config=types.GenerateContentConfig(temperature=0.1))
        return (res.text or fallback).strip()
    except Exception:
        return fallback


# -------------------------------------------------------------------
# HTTP routes
# -------------------------------------------------------------------
@router.post("/query")
def assistant_query(req: QueryReq, user: dict = Depends(get_admin_dependency())):
    return run_assistant_turn(req.query)


@router.post("/briefing")
def assistant_briefing(user: dict = Depends(get_admin_dependency())):
    return {"briefing": build_system_briefing()}


@router.post("/execute-action")
def assistant_execute_action(req: ActionReq, user: dict = Depends(get_admin_dependency())):
    raise HTTPException(status_code=410, detail="Legacy AI action execution is disabled. Review and approve the proposal in the AI Approval Center.")


@router.get("/tasks")
def list_tasks(status_filter: Optional[str] = None, user: dict = Depends(get_staff_dependency())):
    return {"tasks": get_tasks_for_user(user, status_filter=status_filter)}


@router.get("/tasks/{task_id}")
def get_task(task_id: str, user: dict = Depends(get_staff_dependency())):
    ensure_task_table()
    with get_db_instance() as db:
        row = db.execute("SELECT * FROM ai_tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Task not found.")
    task = _task_row(row)
    if user.get("role") != "ADMIN" and task.get("assigned_to") != user.get("id"):
        raise HTTPException(status_code=403, detail="This task is not assigned to you.")
    return {"task": task}


@router.post("/tasks")
def create_task_endpoint(req: TaskCreateReq, user: dict = Depends(get_admin_dependency())):
    if req.assigned_to:
        officer = _user_by_id_or_email(req.assigned_to)
        if not officer or officer["role"] not in ("DATA_OFFICER", "VERIFICATION_OFFICER") or not officer["is_active"]:
            raise HTTPException(status_code=400, detail="Assigned user is not an active Data Officer or Verification Officer.")
    return {"task": create_task(req.record_id, req.task_type, req.title, req.description, req.priority, req.assigned_to, user["full_name"], req.metadata, req.parent_task_id)}


@router.post("/tasks/{task_id}/respond")
def respond_to_task(task_id: str, req: TaskResponseReq, user: dict = Depends(get_staff_dependency())):
    ensure_task_table()
    allowed = {"ACCEPTED", "IN_PROGRESS", "COMPLETED", "RETURNED", "ESCALATED", "BLOCKED", "CANCELLED"}
    new_status = req.status.upper()
    if new_status not in allowed:
        raise HTTPException(status_code=400, detail="Invalid task status.")
    with get_db_instance() as db:
        row = db.execute("SELECT * FROM ai_tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Task not found.")
        task = dict(row)
        if user.get("role") != "ADMIN" and task.get("assigned_to") != user.get("id"):
            raise HTTPException(status_code=403, detail="This task is not assigned to you.")
        result = dict(req.result or {})
        if req.message:
            result["message"] = req.message
        db.execute(
            "UPDATE ai_tasks SET status=?, result=?, updated_at=? WHERE id=?",
            (new_status, json.dumps(result, ensure_ascii=False), _now(), task_id),
        )
    log_system_audit(user["full_name"], "AI_TASK_RESPONSE", f"Task {task_id} changed to {new_status}: {req.message[:240]}", task.get("record_id"))

    # AI workflow hand-off: when an officer cannot finish a task, create the next role's task.
    follow_up = None
    message_lower = (req.message or "").lower()
    if new_status in ("BLOCKED", "RETURNED") and user.get("role") == "VERIFICATION_OFFICER":
        if any(k in message_lower for k in ("missing document", "missing file", "re-upload", "unreadable", "poor scan", "upload")):
            with get_db_instance() as db2:
                data_officer = db2.execute(
                    "SELECT id, full_name FROM users WHERE role='DATA_OFFICER' AND is_active=1 ORDER BY id LIMIT 1"
                ).fetchone()
            if data_officer:
                follow_up = create_task(
                    record_id=task.get("record_id"), task_type="FIX_RECORD_DOCUMENT",
                    title=f"Fix supporting document for record #{task.get('record_id')}",
                    description=f"Verification Officer reported: {req.message}. Re-upload or correct the supporting document, then resubmit the record.",
                    priority=task.get("priority", "HIGH"), assigned_to=str(data_officer["id"]),
                    assigned_by="AI Task Dispatcher", metadata={"parent_task_id": task_id, "source": "OFFICER_BLOCKED"},
                    parent_task_id=task_id,
                )
                log_system_audit("AI Task Dispatcher", "AI_TASK_HANDOFF", f"Created {follow_up['id']} for Data Officer {data_officer['full_name']} from {task_id}", task.get("record_id"))
    elif new_status == "ESCALATED":
        follow_up = create_task(
            record_id=task.get("record_id"), task_type="ADMIN_REVIEW",
            title=f"Administrator attention required for record #{task.get('record_id')}",
            description=f"Verification Officer escalated task {task_id}: {req.message or 'No additional note provided.'}",
            priority="CRITICAL", assigned_to=None, assigned_by="AI Task Dispatcher",
            metadata={"parent_task_id": task_id, "source": "OFFICER_ESCALATION"}, parent_task_id=task_id,
        )

    return {"success": True, "message": f"Task {task_id} updated to {new_status}.", "task_id": task_id, "status": new_status, "follow_up_task": follow_up}


@router.post("/tasks/{task_id}/reassign")
def reassign_task(task_id: str, req: TaskAssignReq, user: dict = Depends(get_admin_dependency())):
    ensure_task_table()
    officer = _user_by_id_or_email(req.officer_id)
    if not officer or officer["role"] != "VERIFICATION_OFFICER" or not officer["is_active"]:
        raise HTTPException(status_code=400, detail="Target Verification Officer is not active.")
    with get_db_instance() as db:
        row = db.execute("SELECT * FROM ai_tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Task not found.")
        db.execute("UPDATE ai_tasks SET assigned_to=?, status='PENDING', updated_at=? WHERE id=?", (officer["id"], _now(), task_id))
    log_system_audit(user["full_name"], "AI_TASK_REASSIGNED", f"Reassigned task {task_id} to {officer['full_name']}", dict(row).get("record_id"))
    return {"success": True, "message": f"Task {task_id} reassigned to {officer['full_name']}."}


@router.get("/officers")
def officers_endpoint(user: dict = Depends(get_staff_dependency())):
    return {"officers": list_verification_officers()}


@router.get("/officers/{officer_id}/workload")
def officer_workload_endpoint(officer_id: str, user: dict = Depends(get_admin_dependency())):
    return get_officer_workload(officer_id)


@router.get("/faulty-records")
def faulty_records_endpoint(limit: int = 30, user: dict = Depends(get_staff_dependency())):
    return {"records": get_faulty_records(max(1, min(limit, 100)))}


# Initialize the table when the router is imported, after server's DB helpers exist.
try:
    ensure_task_table()
except Exception as exc:
    print(f"[AI TASK TABLE WARNING] {exc}")


from ai_governance import router as ai_approval_router
router.include_router(ai_approval_router)
