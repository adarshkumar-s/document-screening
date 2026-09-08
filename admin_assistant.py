import os
import json
import time
import hmac
import hashlib
from datetime import datetime, date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel
import google.generativeai as genai

# Re-use DB helpers and RBAC checks from server.py
from server import get_db, require_roles, ROLE_ADMIN, log_audit

router = APIRouter(prefix="/api/admin/assistant", tags=["AI Admin Assistant"])

ACTION_SECRET = os.getenv("JWT_SECRET", "dilrmp-hackathon-secure-secret-2026")
TOKEN_TTL_SECONDS = 300
USED_ACTION_TOKENS = set()

# -------------------------------------------------------------------
# Read-Only Database Tools for Gemini
# -------------------------------------------------------------------
def search_records(query: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Search records by ID, filename, owner, survey/khasra number, or village."""
    q = f"%{query.strip().lower()}%"
    with get_db() as db:
        cur = db.execute("SELECT id, filename, doc_type, status, mean_conf, fields, created_at FROM documents ORDER BY created_at DESC")
        rows = cur.fetchall()

    results = []
    for r in rows:
        item = dict(r)
        f = json.loads(item.get("fields") or "{}")
        owner = (f.get("owner_name", {}).get("value") or "").lower()
        survey = (f.get("survey_number", {}).get("value") or f.get("khasra_number", {}).get("value") or "").lower()
        village = (f.get("village", {}).get("value") or "").lower()
        doc_id = str(item["id"]).lower()

        if q[1:-1] in owner or q[1:-1] in survey or q[1:-1] in village or q[1:-1] in doc_id:
            results.append({
                "id": item["id"],
                "filename": item["filename"],
                "doc_type": item["doc_type"],
                "status": item["status"],
                "confidence": item["mean_conf"],
                "owner_name": f.get("owner_name", {}).get("value", "—"),
                "survey_number": f.get("survey_number", {}).get("value") or f.get("khasra_number", {}).get("value", "—"),
                "village": f.get("village", {}).get("value", "—")
            })
            if len(results) >= limit:
                break
    return results

def get_record_details(record_id: str) -> Dict[str, Any]:
    """Retrieve full validation, consistency, and OCR details of a record."""
    with get_db() as db:
        row = db.execute("SELECT * FROM documents WHERE id=?", (record_id,)).fetchone()
        if not row:
            return {"error": f"Record '{record_id}' not found."}
        d = dict(row)
        d["fields"] = json.loads(d.get("fields") or "{}")
        d["validation"] = json.loads(d.get("validation") or "{}")
        d["ai_decision_support"] = json.loads(d.get("ai_decision_support") or "{}")
        return d

def get_system_statistics() -> Dict[str, Any]:
    """Fetch live counts of documents and status breakdowns."""
    with get_db() as db:
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
        "processed_today": today_proc
    }

def get_pending_records(limit: int = 10) -> List[Dict[str, Any]]:
    """Fetch pending records awaiting verification."""
    with get_db() as db:
        cur = db.execute("SELECT id, filename, doc_type, status, mean_conf, fields FROM documents WHERE status='PENDING_VERIFICATION' LIMIT ?", (limit,))
        results = []
        for r in cur.fetchall():
            item = dict(r)
            f = json.loads(item.get("fields") or "{}")
            results.append({
                "id": item["id"],
                "filename": item["filename"],
                "owner_name": f.get("owner_name", {}).get("value", "—"),
                "status": item["status"],
                "confidence": item["mean_conf"]
            })
        return results

def get_low_confidence_records(threshold: int = 75, limit: int = 10) -> List[Dict[str, Any]]:
    """Fetch records with low confidence scores."""
    with get_db() as db:
        cur = db.execute("SELECT id, filename, status, mean_conf, fields FROM documents WHERE mean_conf < ? LIMIT ?", (threshold, limit))
        results = []
        for r in cur.fetchall():
            item = dict(r)
            f = json.loads(item.get("fields") or "{}")
            results.append({
                "id": item["id"],
                "filename": item["filename"],
                "owner_name": f.get("owner_name", {}).get("value", "—"),
                "status": item["status"],
                "confidence": item["mean_conf"]
            })
        return results

def get_recent_activity(limit: int = 15) -> List[Dict[str, Any]]:
    """Fetch recent entries from the audit log."""
    with get_db() as db:
        cur = db.execute("SELECT ts, username, action, detail, doc_id FROM audit ORDER BY id DESC LIMIT ?", (limit,))
        return [
            {
                "time": datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M:%S"),
                "user": r["username"],
                "action": r["action"],
                "detail": r["detail"],
                "doc_id": r["doc_id"]
            }
            for r in cur.fetchall()
        ]

# -------------------------------------------------------------------
# Action Token Security (Human Confirmation)
# -------------------------------------------------------------------
def generate_action_token(payload: dict) -> str:
    payload["exp"] = time.time() + TOKEN_TTL_SECONDS
    payload_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
    sig = hmac.new(ACTION_SECRET.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    return f"{payload_bytes.hex()}.{sig}"

def verify_and_consume_token(token: str) -> Optional[dict]:
    if token in USED_ACTION_TOKENS:
        return None
    try:
        hex_data, sig = token.split(".")
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

def prepare_admin_action(action_type: str, target_identifier: str, new_role: Optional[str] = None) -> Dict[str, Any]:
    with get_db() as db:
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
        "run_consistency_check": f"Run consistency verification on {target_name}"
    }

    return {
        "confirmation_required": True,
        "action_type": action_type,
        "action_description": descriptions.get(action_type, action_type),
        "target_id": target_id,
        "target_display": target_name,
        "token": token
    }

def execute_action_in_db(payload: dict, admin_user: dict) -> Dict[str, Any]:
    action_type = payload["action_type"]
    target_id = payload["target_id"]
    params = payload.get("parameters", {})

    with get_db() as db:
        if action_type == "disable_user":
            db.execute("UPDATE users SET is_active=0 WHERE id=?", (target_id,))
            log_audit(admin_user["full_name"], "AI_DEACTIVATE_USER", f"Disabled user #{target_id}", target_id)
            return {"success": True, "message": f"User #{target_id} has been deactivated."}

        elif action_type == "change_user_role":
            new_r = params.get("new_role", "VIEWER").upper()
            db.execute("UPDATE users SET role=?, version=version+1 WHERE id=?", (new_r, target_id))
            log_audit(admin_user["full_name"], "AI_UPDATE_ROLE", f"Changed role of #{target_id} to {new_r}", target_id)
            return {"success": True, "message": f"User #{target_id} role updated to {new_r}."}

        elif action_type == "reprocess_document":
            db.execute("UPDATE documents SET status='DRAFT' WHERE id=?", (target_id,))
            log_audit(admin_user["full_name"], "AI_REPROCESS", f"Reset status of doc #{target_id}", target_id)
            return {"success": True, "message": f"Document #{target_id} set to DRAFT for reprocessing."}

    return {"success": False, "message": "Action could not be executed."}

# -------------------------------------------------------------------
# Gemini Assistant Loop
# -------------------------------------------------------------------
TOOL_DEFINITIONS = [
    search_records,
    get_record_details,
    get_system_statistics,
    get_pending_records,
    get_low_confidence_records,
    get_recent_activity,
    prepare_admin_action
]

SYSTEM_INSTRUCTION = """
You are the AI Admin Assistant for the Digital India Land Records Modernization Programme (DILRMP).
You assist Administrators by reviewing documents, explaining discrepancies, querying metrics, and setting up administrative actions.

Rules:
1. You DO NOT have direct database write access.
2. If an administrative change is requested (e.g., disable a user, change a role, reprocess a document), ALWAYS invoke `prepare_admin_action`.
3. Never use defamatory terms or call documents fraudulent or legally invalid. Use neutral phrasing like 'possible mismatch', 'flagged for review', 'low OCR confidence', or 'requires verification'.
4. Rely solely on tool data. Do not hallucinate records.
"""

def run_assistant_turn(prompt: str) -> Dict[str, Any]:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return {"response": "GEMINI_API_KEY is not configured in .env.", "records": [], "action_card": None}

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(model_name="gemini-1.5-flash", system_instruction=SYSTEM_INSTRUCTION, tools=TOOL_DEFINITIONS)
        chat = model.start_chat(enable_automatic_function_calling=False)
        response = chat.send_message(prompt)

        records_found = []
        action_card = None

        while response.candidates[0].content.parts and any(p.function_call for p in response.candidates[0].content.parts):
            part = next(p for p in response.candidates[0].content.parts if p.function_call)
            fn_call = part.function_call
            fn_name = fn_call.name
            fn_args = dict(fn_call.args)

            tool_fn = globals().get(fn_name)
            tool_output = tool_fn(**fn_args) if tool_fn else {"error": "Tool not found"}

            if isinstance(tool_output, dict) and tool_output.get("confirmation_required"):
                action_card = tool_output
            elif isinstance(tool_output, list) and tool_output and "id" in tool_output[0]:
                records_found.extend(tool_output)

            response = chat.send_message(
                genai.protos.Content(parts=[
                    genai.protos.Part(function_response=genai.protos.FunctionResponse(name=fn_name, response={"result": tool_output}))
                ])
            )

        return {"response": response.text, "records": records_found, "action_card": action_card}
    except Exception as e:
        return {"response": f"Assistant Error: {str(e)}", "records": [], "action_card": None}

def build_system_briefing() -> str:
    stats = get_system_statistics()
    low_ocr = get_low_confidence_records(limit=5)
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    
    if not api_key:
        return f"""# SYSTEM BRIEFING
## Activity
- Processed today: {stats.get('processed_today', 0)}
- Total records: {stats.get('total_documents', 0)}

## Attention Required
- Low OCR confidence records (< 75%): {len(low_ocr)}

## Record Processing
- System queue running.

## Verification
- Pending: {stats.get('pending_verification', 0)} | Approved: {stats.get('approved', 0)} | Returned: {stats.get('returned', 0)}

## System Status
- Operational. Telemetry fallback active.
"""
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(
            model_name="gemini-1.5-flash",
            system_instruction="Create a formal 5-section SYSTEM BRIEFING using headers: # SYSTEM BRIEFING, ## Activity, ## Attention Required, ## Record Processing, ## Verification, ## System Status. Use professional language without declaring documents fraudulent."
        )
        res = model.generate_content(f"Generate briefing based on this telemetry: Stats={stats}, LowConfidenceSamples={low_ocr}")
        return res.text
    except Exception as e:
        return f"# SYSTEM BRIEFING\nTelemetry summary: Total={stats.get('total_documents')}, Pending={stats.get('pending_verification')}. (AI synthesis failed: {str(e)})"

# -------------------------------------------------------------------
# Router Endpoints (Strictly Protected by require_roles(ROLE_ADMIN))
# -------------------------------------------------------------------
class QueryReq(BaseModel):
    query: str

class ActionReq(BaseModel):
    token: str

@router.post("/query")
def api_query(req: QueryReq, current_user: dict = Depends(require_roles(ROLE_ADMIN))):
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty.")
    return run_assistant_turn(req.query.strip())

@router.post("/execute-action")
def api_execute_action(req: ActionReq, current_user: dict = Depends(require_roles(ROLE_ADMIN))):
    payload = verify_and_consume_token(req.token)
    if not payload:
        raise HTTPException(status_code=400, detail="Token expired, invalid, or already executed.")
    return execute_action_in_db(payload, current_user)

@router.post("/briefing")
def api_briefing(current_user: dict = Depends(require_roles(ROLE_ADMIN))):
    return {"briefing": build_system_briefing()}