"""Superior AI operations agent for the administrator portal.

SA is deliberately separate from the normal Admin Assistant. It can inspect and
reason across registered website capabilities, prepare consequential operations,
and execute them only after the existing AI Approval Center approves a proposal.
"""
import json, os, time, uuid, re, asyncio
from typing import Any, Dict, List, Optional
from fastapi import HTTPException

SA_CODE_ENV = "SA_ACTIVATION_CODE"
SA_TTL = int(os.getenv("SA_SESSION_TTL_SECONDS", "3600"))

def _server():
    import server
    return server

def _db():
    return _server().get_db()

def _ai():
    return getattr(_server(), "ai_client", None)

def _assistant():
    import admin_assistant
    return admin_assistant


def _model_candidates():
    return list(dict.fromkeys(x for x in [
        os.getenv("SA_MODEL", "").strip(),
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
    ] if x))


# An error that says "this model cannot serve this request here": wrong or
# retired model name, unsupported method, revoked key. Retrying the same model
# cannot help, so SA moves straight to the next candidate.
_MODEL_LEVEL_MARKERS = (
    "404","not_found","not found","not supported","unsupported",
    "invalid_argument","permission_denied","403","401","unauthenticated",
    "api key not valid","api_key_invalid","is not supported for",
)

# An error that may clear on its own or on a different model: overload, quota,
# timeouts, and SQLite contention on this deployment's single database file.
_TRANSIENT_MARKERS = (
    "503","unavailable","429","resource_exhausted","deadline_exceeded",
    "deadline exceeded","timeout","timed out","500","502","504","internal",
    "temporarily","try again","database is locked","connection","reset by peer",
)


def classify_error(exc: Exception) -> str:
    """Classify a model error as MODEL, TRANSIENT or UNKNOWN."""
    message = str(exc or "").lower()
    if any(x in message for x in _MODEL_LEVEL_MARKERS):
        return "MODEL"
    if any(x in message for x in _TRANSIENT_MARKERS):
        return "TRANSIENT"
    return "UNKNOWN"


# The model that answered most recently. Model names change over time, so SA
# prefers the candidate that is known to work right now instead of repeating a
# call that already failed this session.
_last_working_model = None


def last_working_model() -> Optional[str]:
    return _last_working_model


def _ordered_models() -> List[str]:
    candidates = _model_candidates()
    if _last_working_model and _last_working_model in candidates:
        candidates.remove(_last_working_model)
        candidates.insert(0, _last_working_model)
    return candidates


def _generate_content(client, prompt, config):
    """Call the AI model with self-healing candidate selection.

    One stale model name used to fail the entire SA turn: the old code tried
    only two candidates and retried only on 429/503, so a 404 (the usual
    symptom of a renamed or retired model) surfaced to the administrator as a
    dead assistant. SA now walks its candidate list, skipping models that
    cannot serve the request, pausing briefly on transient failures, and
    remembering which model answered so the next turn starts there.

    The walk is bounded by SA_MODEL_ATTEMPTS and a wall-clock budget so a
    failing provider degrades SA to its deterministic behaviour instead of
    hanging the request.
    """
    global _last_working_model

    max_attempts = max(1, int(os.getenv("SA_MODEL_ATTEMPTS", "4")))
    budget = float(os.getenv("SA_MODEL_BUDGET_SECONDS", "12"))
    started = time.monotonic()
    attempts = 0
    last_exc = None

    for model in _ordered_models():
        if attempts >= max_attempts or (time.monotonic() - started) >= budget:
            break
        attempts += 1
        try:
            response = client.models.generate_content(model=model, contents=prompt, config=config)
            _last_working_model = model
            return response
        except Exception as exc:
            last_exc = exc
            kind = classify_error(exc)
            if kind == "MODEL":
                continue
            if kind == "UNKNOWN":
                # Not a provider fault: fail fast rather than burning the
                # remaining candidates on a programming error.
                break
            if (time.monotonic() - started) < budget:
                time.sleep(0.2 * attempts)

    raise last_exc or RuntimeError("No AI model is configured.")

def _json(v):
    return json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)

_tables_ready = False


def _ensure_tables():
    """Create the SA tables once per process. DDL must never run inside a
    user request — _log and _active_session call this on every request."""
    global _tables_ready
    if _tables_ready:
        return
    with _db() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS sa_sessions(
            session_id TEXT PRIMARY KEY, admin_id TEXT NOT NULL, admin_name TEXT NOT NULL,
            admin_email TEXT, activated_at REAL NOT NULL, expires_at REAL NOT NULL,
            active INTEGER NOT NULL DEFAULT 1, last_used_at REAL NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS sa_activity(
            event_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, admin_id TEXT NOT NULL,
            admin_name TEXT NOT NULL, event_type TEXT NOT NULL, task TEXT NOT NULL,
            detail TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_sa_activity_admin_time ON sa_activity(admin_id,created_at)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_sa_activity_session ON sa_activity(session_id,created_at)")
    _tables_ready = True

def _log(session: Dict[str,Any], event_type: str, task: str, detail: str, data=None):
    _ensure_tables()
    with _db() as db:
        db.execute("""INSERT INTO sa_activity(event_id,session_id,admin_id,admin_name,event_type,task,detail,data,created_at)
                      VALUES(?,?,?,?,?,?,?,?,?)""",
                   (uuid.uuid4().hex,session["session_id"],session["admin_id"],session["admin_name"],
                    event_type,task,detail,_json(data or {}),time.time()))
    try:
        _server().log_audit(session["admin_name"], "SA_"+event_type, detail[:500], None)
    except Exception:
        pass

FEATURES = {
    "documents": "Search documents, inspect complete document records, OCR confidence, extracted fields, validation, AI decision support and status.",
    "document_workflow": "Pending verification, faulty/attention records, officer workloads, AI task inbox, assignment and escalation preparation.",
    "land_intelligence": "Properties, parcel/location information, ownership history, mutations, encumbrances, deterministic land risk and land-document context.",
    "litigation": "Court/litigation register and case information linked to land/property records.",
    "mapping": "Property coordinates, map/location status and governed exact-pin proposals.",
    "reporting": "System statistics, operational intelligence, activity and administrative briefings.",
    "governance": "AI proposals, evidence, approval/rejection state and execution events.",
    "administration": "Authenticated administrator context, users and audit trail. Account/role changes remain controlled by existing administration workflows.",
    "backup_restore": "Backup/restore feature is available through the website; SA must never bypass its own server-side safety gates.",
}

def feature_catalog():
    return [{"name":k,"description":v} for k,v in FEATURES.items()]

def _active_session(session_id: str, admin: Dict[str,Any]):
    _ensure_tables()
    with _db() as db:
        row=db.execute("SELECT * FROM sa_sessions WHERE session_id=? AND active=1",(session_id,)).fetchone()
    if not row or str(row["admin_id"]) != str(admin["id"]) or time.time() > float(row["expires_at"]):
        if row:
            with _db() as db: db.execute("UPDATE sa_sessions SET active=0 WHERE session_id=?",(session_id,))
        raise HTTPException(401,"SA session expired or is not valid for this administrator.")
    session=dict(row)
    with _db() as db: db.execute("UPDATE sa_sessions SET last_used_at=? WHERE session_id=?",(time.time(),session_id))
    return session

# Each SA identity has a separate server-side credential. Only the Argon2
# hashes are stored in the deployment environment; plaintext passwords never
# live in the repository or browser code.
IDENTITIES = {
    "gautam": "Gautam",
    "adarsh": "Adarsh",
    "devi_cr": "Devi Cr",
}
SA_PASSWORD_ENV = {
    "gautam": "SA_PASSWORD_HASH_GAUTAM",
    "adarsh": "SA_PASSWORD_HASH_ADARSH",
    "devi_cr": "SA_PASSWORD_HASH_DEVI_CR",
}
SA_MAX_PASSWORD_ATTEMPTS = 5
SA_ATTEMPT_WINDOW_SECONDS = 15 * 60
_sa_password_attempts: Dict[str, List[float]] = {}

def _identity_key(name: str) -> str:
    return str(name or "").strip().lower().replace(" ", "_")

def _password_hash_for(identity_key: str) -> str:
    return os.getenv(SA_PASSWORD_ENV.get(identity_key, ""), "").strip()

def _check_password(identity_key: str, password: str) -> bool:
    stored_hash = _password_hash_for(identity_key)
    if not stored_hash or not password:
        return False
    try:
        return bool(_server().verify_password(stored_hash, password)[0])
    except Exception:
        return False

def _password_attempt_allowed(identity_key: str) -> bool:
    now = time.time()
    recent = [ts for ts in _sa_password_attempts.get(identity_key, []) if now - ts < SA_ATTEMPT_WINDOW_SECONDS]
    _sa_password_attempts[identity_key] = recent
    return len(recent) < SA_MAX_PASSWORD_ATTEMPTS

def _record_password_failure(identity_key: str):
    _sa_password_attempts.setdefault(identity_key, []).append(time.time())

def activation_options(admin: Dict[str,Any], code: str):
    expected=os.getenv(SA_CODE_ENV,"").strip()
    if not expected:
        raise HTTPException(503,"SA activation is not configured. Set SA_ACTIVATION_CODE on the server.")
    if not code or not __import__("hmac").compare_digest(code,expected):
        _ensure_tables()
        try: _server().log_audit(admin["full_name"],"SA_ACTIVATION_FAILED","Invalid SA activation attempt.",None)
        except Exception: pass
        raise HTTPException(403,"Invalid SA activation code.")
    return {"options":[{"key":key,"name":name} for key,name in IDENTITIES.items()],
            "authenticated_admin":{"id":admin["id"],"name":admin["full_name"],"email":admin.get("email")}}

def activate(admin: Dict[str,Any], code: str, selected_name: str, password: str):
    activation_options(admin,code)
    identity_key = _identity_key(selected_name)
    canonical = IDENTITIES.get(identity_key)
    if not canonical:
        raise HTTPException(400,"Select one of the registered administrator identities.")
    if not _password_attempt_allowed(identity_key):
        try: _server().log_audit(admin["full_name"],"SA_ACTIVATION_LOCKED",
                                 f"SA password temporarily locked for identity {identity_key}.",None)
        except Exception: pass
        raise HTTPException(429,"Too many failed password attempts for this administrator identity. Try again later.")
    if not _check_password(identity_key, password):
        _record_password_failure(identity_key)
        try: _server().log_audit(admin["full_name"],"SA_ACTIVATION_FAILED",
                                 f"Invalid SA identity password for {identity_key}.",None)
        except Exception: pass
        raise HTTPException(403,"The password for the selected administrator identity is incorrect.")
    _sa_password_attempts.pop(identity_key, None)

    # The authenticated account must already be an administrator. The second
    # credential proves which SA identity is being activated; it is never
    # accepted as a way to impersonate a non-authenticated website account.
    _ensure_tables()
    sid="SA-"+uuid.uuid4().hex[:12].upper()
    now=time.time()
    with _db() as db:
        db.execute("""INSERT INTO sa_sessions(session_id,admin_id,admin_name,admin_email,activated_at,expires_at,active,last_used_at)
                      VALUES(?,?,?,?,?,?,1,?)""",(sid,admin["id"],canonical,admin.get("email"),now,now+SA_TTL,now))
    session={"session_id":sid,"admin_id":admin["id"],"admin_name":canonical,"admin_email":admin.get("email")}
    _log(session,"SESSION_STARTED","SA activation",f"SA activated as admin.{identity_key}",
         {"authenticated_admin":admin["full_name"],"selected_identity":canonical})
    return {"session_id":sid,"admin":f"admin.{identity_key}","expires_at":now+SA_TTL,
            "features":feature_catalog()}

def deactivate(admin, session_id):
    s=_active_session(session_id,admin)
    with _db() as db: db.execute("UPDATE sa_sessions SET active=0 WHERE session_id=?",(session_id,))
    _log(s,"SESSION_ENDED","SA session ended","Administrator ended the SA session.")
    return {"ok":True}

def _read_tools():
    a=_assistant()
    return {
        "system_statistics": lambda args: a.get_system_statistics(),
        "operational_intelligence": lambda args: a.get_operational_intelligence(),
        "pending_records": lambda args: a.get_pending_records(int(args.get("limit",20))),
        "low_confidence_records": lambda args: a.get_low_confidence_records(float(args.get("threshold",75)),int(args.get("limit",20))),
        "attention_records": lambda args: a.get_faulty_records(int(args.get("limit",30))),
        "search_documents": lambda args: a.search_records(str(args.get("query","")),int(args.get("limit",20))),
        "document": lambda args: a.get_record_details(str(args["record_id"])),
        "record_operations": lambda args: a.get_record_operations(str(args["record_id"])),
        "officers": lambda args: a.list_verification_officers(),
        "officer_workload": lambda args: a.get_officer_workload(str(args["officer_id"])),
        "tasks": lambda args: a.get_tasks_for_user({"id":None,"role":"ADMIN"},limit=int(args.get("limit",100))),
        "recent_activity": lambda args: a.get_recent_activity(int(args.get("limit",30))),
        "property": lambda args: _property(args),
        "property_history": lambda args: _property_history(args),
        "litigation": lambda args: _litigation(args),
        "governance_proposals": lambda args: _governance(args),
    }

def _property(args):
    identifier=str(args.get("property_id") or args.get("query") or "")
    with _db() as db:
        r=db.execute("""SELECT property_id,parcel_id,district,taluka,village,survey_number,khasra_number,
                               latitude,longitude,location_status,location_source,location_updated_at
                        FROM properties WHERE property_id=? OR parcel_id=? OR survey_number=? LIMIT 1""",
                     (identifier,identifier,identifier)).fetchone()
    return dict(r) if r else {"error":f"Property '{identifier}' not found."}

def _property_history(args):
    identifier=str(args.get("property_id") or args.get("query") or "")
    with _db() as db:
        r=db.execute("SELECT property_id FROM properties WHERE property_id=? OR parcel_id=? OR survey_number=? LIMIT 1",(identifier,identifier,identifier)).fetchone()
    if not r: return {"error":f"Property '{identifier}' not found."}
    from mapping import _history_for_property
    return _history_for_property(r["property_id"])

def _litigation(args):
    identifier=str(args.get("query") or args.get("property_id") or "")
    try:
        from court_cases import list_cases_for_property
        return {"cases":list_cases_for_property(identifier)}
    except Exception:
        # Fall back to the actual litigation tables if the helper name changes.
        with _db() as db:
            rows=db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%case%'").fetchall()
        return {"available_tables":[dict(x) for x in rows],"query":identifier}

def _governance(args):
    from ai_governance import list_proposals
    return {"proposals":list_proposals(args.get("status"),int(args.get("limit",50)))}

READ_TOOL_DESCRIPTIONS = {
    "system_statistics":"Get current portal document statistics.",
    "operational_intelligence":"Get current attention records, workload, stale tasks and activity.",
    "pending_records":"List pending verification records.",
    "low_confidence_records":"Find records below an OCR confidence threshold.",
    "attention_records":"Find records requiring attention using deterministic OCR/validation/consistency signals.",
    "search_documents":"Search documents by ID, owner, survey/khasra or village.",
    "document":"Retrieve the complete current document record by ID.",
    "record_operations":"Determine which implemented read-only and approval-gated operations are available for a specific record.",
    "officers":"List active Verification Officers and workloads.",
    "officer_workload":"Get one officer's workload.",
    "tasks":"List AI workflow tasks.",
    "recent_activity":"Read recent audit activity.",
    "property":"Get property/location data.",
    "property_history":"Get linked ownership/property history.",
    "litigation":"Search litigation/court records associated with a property/query.",
    "governance_proposals":"Inspect AI governance proposals and their status.",
}

WRITE_INTENTS = {
    "create_task":("CREATE_AI_TASK","DOCUMENT"),
    "assign_task":("ASSIGN_AI_TASK","DOCUMENT"),
    "reassign_task":("REASSIGN_AI_TASK","TASK"),
    "reprocess":("REQUEST_REPROCESSING","DOCUMENT"),
    "escalate":("ESCALATE_RECORD","DOCUMENT"),
    "verification_case":("CREATE_VERIFICATION_CASE","DOCUMENT"),
    "set_property_location":("SET_PROPERTY_LOCATION","PROPERTY"),
    "clear_property_location":("CLEAR_PROPERTY_LOCATION","PROPERTY"),
    "mutation_application":("CREATE_MUTATION_APPLICATION","PROPERTY"),
}

def _extract_id(text):
    """Resolve an actual-looking record/property reference from natural language.

    SA and the Admin Assistant must agree on what counts as an identifier, so
    both now share one parser. Keeping two copies of this logic is how the two
    assistants drifted apart and started reading "record 123" differently.
    """
    try:
        return _assistant()._record_id_from_prompt(text or "")
    except Exception:
        # A parser failure must never break the SA turn; SA simply asks for the
        # identifier again.
        return None

def _coerce_json(raw: Any) -> Optional[Dict[str, Any]]:
    """Recover a JSON object from model output that is not strictly JSON.

    Models routinely wrap JSON in markdown fences or add a sentence around it.
    The old code did a bare json.loads and threw the whole plan away on the
    first fence, silently downgrading SA to the heuristic planner. Recover the
    object when one is present and return None only when there is none.
    """
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if not text:
        return None
    text = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text, flags=re.I | re.M).strip()
    for candidate in (text,):
        try:
            value = json.loads(candidate)
        except Exception:
            value = None
        if isinstance(value, dict):
            return value
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            value = json.loads(text[start:end + 1])
        except Exception:
            return None
        if isinstance(value, dict):
            return value
    return None


def _sanitize_plan(plan: Any) -> Optional[Dict[str, Any]]:
    """Keep only the steps SA can actually execute.

    Discards hallucinated tool names and malformed steps instead of letting
    them reach the executor, and returns None when nothing usable is left so
    the deterministic planner takes over.
    """
    if not isinstance(plan, dict):
        return None
    steps = plan.get("steps")
    if not isinstance(steps, list):
        return None
    known = set(READ_TOOL_DESCRIPTIONS)
    cleaned = []
    for step in steps[:12]:
        if not isinstance(step, dict):
            continue
        tool = step.get("tool")
        if tool not in known:
            continue
        args = step.get("args") if isinstance(step.get("args"), dict) else {}
        depends_on = step.get("depends_on")
        cleaned.append({
            "id": str(step.get("id") or f"step_{len(cleaned) + 1}"),
            "tool": tool,
            "args": args,
            "purpose": str(step.get("purpose") or ""),
            "depends_on": depends_on if isinstance(depends_on, list) else [],
        })
    if not cleaned:
        return None
    plan["steps"] = cleaned
    plan.setdefault("goal", "")
    plan.setdefault("response_style", "concise evidence-based administrator briefing")
    return plan


def _plan_with_ai(task: str):
    client=_ai()
    if not client:
        return None
    from google.genai import types
    tools=", ".join(READ_TOOL_DESCRIPTIONS.keys())
    prompt=f"""You are the planning brain of a website-owning administrator AI.
Return ONLY valid JSON with keys goal, steps, response_style.
steps is an array of objects: id, tool, args, purpose, depends_on.
Only use these READ tools: {tools}.
Never invent tool names. Keep to at most 12 steps. Prefer independent steps.
Website capabilities: {json.dumps(FEATURES,ensure_ascii=False)}
Administrator task: {task}"""
    try:
        r=_generate_content(client,prompt,types.GenerateContentConfig(temperature=0.05,response_mime_type="application/json"))
        return _sanitize_plan(_coerce_json(getattr(r,"text","")))
    except Exception:
        return None

def _fallback_plan(task):
    t=task.lower()
    rid=_extract_id(task)
    steps=[]
    wants_operations=any(x in t for x in (
        "what operation","which operation","what can be done","available operation",
        "available operations","what actions","which actions","operations can be done",
        "operations are available","what are the operations","list operations",
        "operation can be done","can i do with"
    ))
    if rid:
        steps.append({"id":"document","tool":"document","args":{"record_id":rid},"purpose":"Inspect the referenced document","depends_on":[]})
        if wants_operations:
            steps.append({"id":"operations","tool":"record_operations","args":{"record_id":rid},"purpose":"List the implemented operations available for this record","depends_on":["document"]})
    if any(x in t for x in ["land","property","parcel","mutation","encumbrance","ownership","location"]):
        if rid: steps += [{"id":"property","tool":"property","args":{"property_id":rid},"purpose":"Inspect land/property context","depends_on":[]},
                          {"id":"history","tool":"property_history","args":{"property_id":rid},"purpose":"Inspect ownership history","depends_on":[]}]
    if any(x in t for x in ["litigation","court","case","legal"]):
        steps.append({"id":"litigation","tool":"litigation","args":{"query":rid or task},"purpose":"Inspect litigation context","depends_on":[]})
    if any(x in t for x in ["pending","verification"]): steps.append({"id":"pending","tool":"pending_records","args":{"limit":30},"purpose":"Inspect verification queue","depends_on":[]})
    if any(x in t for x in ["officer","workload","assign","task"]): steps.append({"id":"officers","tool":"officers","args":{},"purpose":"Inspect workflow capacity","depends_on":[]})
    if any(x in t for x in ["urgent","attention","risk","problem","issue"]): steps.append({"id":"intel","tool":"operational_intelligence","args":{},"purpose":"Inspect operational risk signals","depends_on":[]})
    if not steps: steps=[{"id":"intel","tool":"operational_intelligence","args":{},"purpose":"Establish current system context","depends_on":[]}]
    return {"goal":task,"steps":steps,"response_style":"concise evidence-based administrator briefing"}

# SQLite contention on a single database file is transient: the same read
# succeeds a moment later. Retrying these keeps one locked write from being
# reported to the administrator as a broken capability.
_DB_TRANSIENT_MARKERS = (
    "database is locked",
    "database table is locked",
    "cannot start a transaction within a transaction",
    "disk i/o error",
)


def is_transient_db_error(message: str) -> bool:
    lowered = str(message or "").lower()
    return any(x in lowered for x in _DB_TRANSIENT_MARKERS)


async def _execute_reads(plan):
    tools=_read_tools()
    valid=[]
    for s in plan.get("steps",[])[:12]:
        if s.get("tool") in tools: valid.append(s)
    async def one(s):
        last=None
        for attempt in range(3):
            try:
                result=await asyncio.to_thread(tools[s["tool"]],s.get("args") or {})
                return {"id":s.get("id"),"tool":s["tool"],"purpose":s.get("purpose"),"result":result,"ok":True}
            except Exception as e:
                last=e
                if attempt < 2 and is_transient_db_error(str(e)):
                    await asyncio.sleep(0.15 * (attempt + 1))
                    continue
                break
        return {"id":s.get("id"),"tool":s.get("tool"),"purpose":s.get("purpose"),"error":str(last),"ok":False}
    return await asyncio.gather(*(one(s) for s in valid))

def _synthesize(task, evidence, session=None):
    client=_ai()
    if not client:
        return "I checked the available site data. Here is what I found:\n\n"+_json(evidence)[:9000]
    from google.genai import types
    recent_context = ""
    if session:
        try:
            with _db() as db:
                rows=db.execute(
                    "SELECT task,detail FROM sa_activity WHERE session_id=? AND event_type='TASK_COMPLETED' ORDER BY created_at DESC LIMIT 4",
                    (session["session_id"],)
                ).fetchall()
            if rows:
                recent_context = "\nRecent SA context (use only to understand follow-ups):\n" + "\n".join(
                    f"- {r['task']}: {r['detail']}" for r in rows
                )
        except Exception:
            recent_context = ""
    prompt=f"""You are SA, a friendly, capable operations partner for the administrator of a land-records website.
Speak naturally, like a helpful teammate, not like a rigid chatbot.
Start with a direct answer, then give the important evidence and the next useful step.
If the administrator's request is a follow-up, use the recent SA context when relevant.
Be concise by default. Do not dump raw JSON unless asked.
Identify uncertainty clearly. Never invent facts.
Never claim a consequential action was executed unless execution evidence says so.
If the task asks what can be done with a record, explicitly list the operations found in the record_operations evidence and distinguish READ_ONLY from APPROVAL_REQUIRED.
Consequential actions must remain proposals for Administrator Approval.
Task: {task}
Evidence: {json.dumps(evidence,ensure_ascii=False,default=str)[:40000]}
{recent_context}"""
    answer=""
    try:
        r=_generate_content(client,prompt,types.GenerateContentConfig(temperature=0.25,max_output_tokens=700))
        answer=(getattr(r,"text","") or "").strip()
    except Exception:
        answer=""
    if not answer:
        # Models occasionally answer with an empty body: a safety block, a
        # truncated completion, or a quota-shaped no-op. Retry once with a
        # plainer prompt before falling back to the verified raw evidence.
        try:
            r=_generate_content(
                client,
                "Summarise this administrator evidence in three short sentences.\nTask: "
                + str(task)
                + "\nEvidence: "
                + json.dumps(evidence, ensure_ascii=False, default=str)[:20000],
                types.GenerateContentConfig(temperature=0.1),
            )
            answer=(getattr(r,"text","") or "").strip()
        except Exception:
            answer=""
    return answer or _deterministic_answer(task,evidence)


def _deterministic_answer(task: str, evidence: Any) -> str:
    """Readable summary built directly from the collected evidence.

    Used when no model could write the answer. It never invents anything: it
    reports what the site actually returned, including which reads failed.
    """
    lines=["I could not reach the language model, so here is the verified site data I collected for \""+str(task)+"\":",""]
    for item in (evidence or [])[:12]:
        if not isinstance(item, dict):
            continue
        label=str(item.get("tool") or "step")
        if item.get("ok") is False:
            lines.append(f"- {label}: unavailable ({item.get('error') or 'unknown error'})")
            continue
        lines.append(f"- {label}: "+_json(item.get("result"))[:800])
    lines += ["", "No record was changed. Consequential actions still require Administrator Approval."]
    return "\n".join(lines)

def _write_action(task: str) -> Optional[str]:
    """Recognise the governed operation an administrator is asking SA to prepare.

    SA only ever prepares a proposal for these intents; execution still happens
    in the AI Approval Center. Recognising the phrasing variants matters here
    because a missed intent silently degrades to a plain answer instead of
    telling the administrator that the operation needs approval.
    """
    t=(task or "").lower()
    if any(x in t for x in ("reprocess","re-process","re-run ocr","rerun ocr","re-extract","back through ocr")): return "reprocess"
    if "escalat" in t or ("review" in t and any(x in t for x in ("flag","raise","send","needs","need","require"))): return "escalate"
    if "assign" in t or "hand over" in t or "allocate an officer" in t or "give to a free officer" in t: return "assign_task"
    if any(x in t for x in ("verification case","open a verification","start verification")): return "verification_case"
    if "clear exact pin" in t or "remove exact pin" in t: return "clear_property_location"
    if "set exact pin" in t or "set location" in t: return "set_property_location"
    if "mutation application" in t or "create mutation" in t: return "mutation_application"
    if any(x in t for x in ("create task","verification task","open a task","raise a task")): return "create_task"
    return None


def _proposal_for_write(task, session):
    t=task.lower()
    rid=_extract_id(task)
    action=_write_action(task)
    if not action: return None
    if not rid and action not in {"create_task","mutation_application"}: return {"error":"I need a target record/property identifier before preparing that action."}
    a,b=WRITE_INTENTS[action]
    ass=_assistant()
    if action=="assign_task":
        rec=ass.get_record_details(rid)
        officer=ass.find_available_officer()
        if rec.get("error"): return rec
        if officer.get("error"): return officer
        proposal=ass._create_ai_proposal(a,b,[rid],
            {"documents":{rid:{"status":rec.get("status"),"mean_conf":rec.get("mean_conf")}}},
            {"assigned_to":str(officer["id"]),"task_type":"VERIFY_RECORD","title":f"Verify land record #{rid}",
             "description":"Review OCR output, validation findings and unresolved discrepancies.",
             "priority":"HIGH" if float(rec.get("mean_conf") or 0)<75 else "MEDIUM"},
            f"SA prepared assignment after reviewing current record and officer capacity.",
            [{"type":"record","record_id":rid},{"type":"officer","id":officer["id"],"name":officer["name"],"active_tasks":officer["active_tasks"]}],0.9,"MEDIUM")
    elif action in {"reprocess","escalate","verification_case"}:
        rec=ass.get_record_details(rid)
        if rec.get("error"): return rec
        after={"task_type":"ADMIN_REVIEW","title":f"Administrator review for record #{rid}","priority":"HIGH"} if action=="escalate" else {}
        proposal=ass._create_ai_proposal(a,b,[rid],
            {"documents":{rid:{"status":rec.get("status"),"mean_conf":rec.get("mean_conf")}}},
            after,f"SA prepared {action} for administrator approval.",[{"type":"record","record_id":rid,"status":rec.get("status"),"mean_conf":rec.get("mean_conf")}],0.9,"MEDIUM")
    elif action=="clear_property_location":
        p=_property({"property_id":rid})
        if p.get("error"): return p
        proposal=ass._create_ai_proposal(a,b,[p["property_id"]],
            {"properties":{p["property_id"]:{k:p.get(k) for k in ("location_status","latitude","longitude","location_updated_at")}}},
            {"reason":"SA prepared location change for administrator approval."},
            "SA prepared exact-pin removal; no property was changed.",[{"type":"property","property_id":p["property_id"]}],0.95,"HIGH")
    else:
        return {"error":f"SA recognized the requested operation '{action}', but that operation needs specific parameters and cannot be safely prepared from this request alone."}
    return {"confirmation_required":True,"proposal":proposal,"proposal_id":proposal["proposal_id"],
            "action_type":proposal["action_type"],"action_description":f"SA prepared {action.replace('_',' ')}","target_display":rid or "multi-record operation"}

# Phrases that unambiguously ask for repository work. A single one is enough
# to hand the task to Arena.
_ARENA_STRONG_PHRASES = (
    # implementation / coding
    "implement", "implementation", "refactor", "rewrite", "rebuild", "redesign",
    "write code", "write the code", "coding", "code change", "change the code",
    "codebase", "source code", "add a feature", "add feature", "add functionality",
    "add an endpoint", "create an endpoint", "new endpoint", "new route", "new api",
    # repository / deployment work
    "database schema", "schema change", "migration", "pull request", "open a pr",
    "merge request", "in the repository", "in the repo", "in the codebase",
    "in the code", "across the repo", "deploy", "deployment", "docker",
    "render.yaml", "github actions", "ci pipeline", "unit test", "write tests",
    "add tests", "test suite", "pytest", "architecture",
    # breakage that needs a code fix
    "is broken", "is not working", "isn't working", "doesn't work",
    "does not work", "not working properly", "throwing an error",
    "throws an error", "stack trace", "traceback", "crash", "crashes",
    "failing to load", "500 error", "fix the bug", "fix this bug", "fix a bug",
    "fix the error", "fix that error", "fix the issue",
    # explicitly heavy work
    "heavy work", "large change", "major change", "complex change",
    "deep implementation", "full implementation", "complete implementation",
    "end to end implementation", "full rebuild", "complete rebuild",
)

# Implementation verbs. A verb on its own is not a handoff signal: "add a
# verification officer" and "create a task" are operational requests that SA
# prepares for Administrator Approval.
_ARENA_VERBS = (
    "implement", "build", "create", "add", "write", "refactor", "rewrite",
    "redesign", "rebuild", "modify", "change", "update", "fix", "patch", "debug",
    "deploy", "migrate", "remove", "delete", "rename", "move", "optimize",
    "disable", "enable", "replace", "extend",
)

# Code artefacts. A noun on its own is not a handoff signal either: "list the
# uploaded files", "is the production API healthy?" and "show me the audit
# log" are questions about the running site, which SA can answer itself.
_ARENA_NOUNS = (
    "repository", "repo", "github", "codebase", "code", "source code",
    "endpoint", "endpoints", "route", "routes", "frontend", "backend",
    "database schema", "schema", "api", "apis", "docker", "deployment",
    "file", "files", "component", "components", "template", "templates",
    "stylesheet", "css", "javascript", "pipeline", "migration", "database table",
    "table", "column", "columns", "query", "script", "module", "package",
    "branch", "page", "pages", "form", "forms", "button", "dashboard",
    "layout", "html", "python", "sql", "config", "configuration",
    "environment variable", "yaml", "workflow",
)


def _is_arena_task(task: str) -> bool:
    """Decide whether the request should be handed to Arena AI.

    Arena is the coding/implementation agent for this repository. SA should
    remain the fast conversational and operational interface; it should not
    attempt large implementation jobs itself. The handoff is a prompt for
    Arena, not an action performed by SA.
    """
    t=(task or "").lower().strip()
    if not t:
        return False

    # Phrases that unambiguously ask for repository work. One is enough.
    if any(phrase in t for phrase in _ARENA_STRONG_PHRASES):
        return True

    # Otherwise require an implementation verb acting on a code artefact.
    # Matching bare nouns sent ordinary operational questions to Arena: "list
    # the uploaded files", "is the production API healthy?" and "show me the
    # audit log" are all questions SA can answer from the running site.
    if any(verb in t for verb in _ARENA_VERBS) and any(noun in t for noun in _ARENA_NOUNS):
        return True

    # Very long implementation requests are also better delegated, even if
    # they do not contain one of the exact terms above.
    return len(t) > 700


def _arena_prompt(task: str) -> str:
    """Create a production-grade implementation brief for Arena AI.

    Arena is the agent that can actually inspect and modify this repository.
    SA's job is to understand the administrator's request and hand Arena a
    complete, unambiguous implementation brief rather than pretending to do
    the coding itself.
    """
    return f"""You are Arena AI, the implementation/coding agent working directly
on the repository:

https://github.com/adarshkumar-s/document-screening

The administrator sent this request through the site's SA assistant:

--- ADMINISTRATOR REQUEST ---
{task}
--- END REQUEST ---

Your job is to inspect the EXISTING repository first and then implement the
requested work directly in the repository.

IMPORTANT WORKING RULES
1. Do NOT guess the current architecture. Inspect the relevant existing files,
   routes, templates, frontend JavaScript/CSS, backend code, database models,
   authentication/authorization, APIs, tests, configuration, and deployment
   setup before editing.
2. Do NOT throw away existing working functionality unless the administrator
   explicitly requested a replacement.
3. Preserve existing security, authentication, authorization, audit logging,
   approval gates, and administrator controls.
4. Reuse the application's existing patterns and components where practical.
5. Make the change complete across the full stack when required; do not stop
   after changing only the visible UI.
6. Handle edge cases, errors, loading states, empty states, permissions, and
   responsive behavior where relevant.
7. Keep consequential actions behind the existing administrator approval
   workflow. Never weaken or bypass an approval gate just to make a feature
   convenient.
8. Do not invent database fields, routes, APIs, or existing functionality.
   Verify them from the repository before using them.
9. After implementation, run the relevant tests/checks and inspect the final
   diff for regressions.
10. If deployment configuration is affected, verify it against the repository's
    actual Render/Docker/startup configuration rather than assuming it.
11. If the request is ambiguous, inspect the repository for context first.
    Only ask for clarification when a safe implementation genuinely cannot be
    determined.
12. Finish the requested implementation rather than merely describing code
    that the administrator could write.

QUALITY BAR
- Production-quality implementation.
- Consistent with the existing application's architecture and UI.
- No placeholder code.
- No fake success messages.
- No dead buttons or UI that is not connected to its backend behavior.
- No accidental data mutation during read-only operations.
- No regression to existing routes or workflows.
- Verify that all changed imports, references, routes, selectors, and assets
  actually exist.

DELIVERABLE
When finished, report:
1. What you changed.
2. Which files changed.
3. Important architectural decisions.
4. Tests/validation performed and their results.
5. Any limitations or remaining risks.
6. The commit(s) created, if applicable.

Do the repository inspection and implementation yourself. This prompt is the
handoff from SA; the administrator expects Arena to perform the actual coding
work, not merely return suggestions.
"""


def run(task: str, admin: Dict[str,Any], session_id: str):
    session=_active_session(session_id,admin)
    _log(session,"TASK_STARTED",task,"SA started an administrator task.")
    t=task.lower().strip()
    if t in {"hi","hello","hey","good morning","good afternoon","good evening"}:
        answer="Hi! I am SA. Tell me what you want to inspect or get done, and I will work through the site with you. If the task is consequential, I will prepare it for Administrator Approval rather than changing anything directly."
        plan={"goal":"conversation","steps":[],"response_style":"friendly"}
        evidence=[]
        card=None
    elif any(x in t for x in ("what can you do","what can you help","capabilities","features can you access")):
        answer="I can work across the site documents, OCR and validation, Land Intelligence, ownership history, mapping, litigation, verification workflow, officer workload, AI tasks, governance and audit information. I can prepare consequential changes for approval, but I will not bypass the Approval Center."
        plan={"goal":"capability overview","steps":[],"response_style":"friendly"}
        evidence=[]
        card=None
    elif _is_arena_task(task):
        prompt=_arena_prompt(task)
        answer="This request needs repository-level implementation work. I will not pretend to do that inside the quick SA turn. I prepared a complete implementation brief for Arena AI, which is the coding agent that can inspect and modify this repository:\n\n"+prompt
        plan={"goal":"deep-work handoff","steps":[],"response_style":"friendly"}
        evidence=[]
        card=None
    else:
        # Keep ordinary turns fast and deterministic. The fallback planner now
        # understands explicit #IDs and operation-discovery requests, so simple
        # questions do not need a second "planning" model round.
        plan=_fallback_plan(task)
        evidence=asyncio.run(_execute_reads(plan))
        answer=_synthesize(task,evidence,session)
        card=_proposal_for_write(task,session)
        if card and card.get("error"): answer += "\n\n"+card["error"]
        elif card: answer += "\n\nI prepared the consequential action, but nothing has changed. Please review and approve it in the AI Approval Center."
    _log(session,"TASK_COMPLETED",task,"SA completed the request.",{"plan":plan,"evidence":evidence,"proposal_id":card.get("proposal_id") if card else None})
    return {"response":answer,"mode":"SA","admin":"admin."+session["admin_name"].lower().replace(" ","_"),"plan":plan,"evidence":evidence,"action_card":card,"session_id":session_id}
def report(admin:Dict[str,Any], session_id:Optional[str]=None, limit:int=200):
    _ensure_tables()
    with _db() as db:
        if session_id:
            rows=db.execute("SELECT * FROM sa_activity WHERE session_id=? ORDER BY created_at DESC LIMIT ?",(session_id,min(limit,500))).fetchall()
        else:
            rows=db.execute("SELECT * FROM sa_activity WHERE admin_id=? ORDER BY created_at DESC LIMIT ?",(admin["id"],min(limit,500))).fetchall()
    out=[]
    for r in rows:
        d=dict(r)
        try:d["data"]=json.loads(d.get("data") or "{}")
        except Exception:d["data"]={}
        out.append(d)
    return {"admin":f"admin.{admin['full_name'].lower().replace(' ','_')}","events":out}
