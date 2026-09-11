"""Server-side governance for AI-generated consequential actions.

AI can inspect and propose. Only an authenticated administrator can approve.
The executor accepts only registered action types and re-validates state.
"""
import json, time, uuid
from typing import Any, Dict, Optional, List
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

def _server():
    import server
    return server

router = APIRouter(prefix="/api/admin/ai-approval", tags=["AI Approval Center"])

PROPOSED="PROPOSED"; APPROVED="APPROVED"; REJECTED="REJECTED"; EXPIRED="EXPIRED"; EXECUTED="EXECUTED"; FAILED="FAILED"
TTL_SECONDS=30*60

ACTION_REGISTRY = {
    "CREATE_AI_TASK",
    "ASSIGN_AI_TASK",
    "REASSIGN_AI_TASK",
    "REQUEST_REVIEW",
    "REQUEST_REPROCESSING",
    "PROPOSE_FIELD_CORRECTION",
    "PROPOSE_STATUS_CHANGE",
    "CREATE_VERIFICATION_CASE",
    "ESCALATE_RECORD",
}

class ProposalCreate(BaseModel):
    action_type: str
    target_type: str
    target_ids: List[str] = Field(default_factory=list)
    before: Dict[str, Any] = Field(default_factory=dict)
    after: Dict[str, Any] = Field(default_factory=dict)
    reason: str
    evidence: List[Dict[str, Any]] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    risk: str = "MEDIUM"

class DecisionReq(BaseModel):
    note: str = ""

class AIProposalRequest(BaseModel):
    proposal: ProposalCreate

def ensure_governance_tables():
    s=_server()
    with s.get_db() as db:
        db.execute("""
        CREATE TABLE IF NOT EXISTS ai_proposals (
          proposal_id TEXT PRIMARY KEY,
          action_type TEXT NOT NULL,
          target_type TEXT NOT NULL,
          target_ids TEXT NOT NULL,
          before_state TEXT NOT NULL,
          proposed_state TEXT NOT NULL,
          reason TEXT NOT NULL,
          evidence TEXT NOT NULL,
          confidence REAL NOT NULL,
          risk TEXT NOT NULL,
          status TEXT NOT NULL,
          created_by TEXT NOT NULL,
          created_at REAL NOT NULL,
          expires_at REAL NOT NULL,
          approved_by TEXT,
          approved_at REAL,
          execution_at REAL,
          execution_result TEXT NOT NULL DEFAULT '{}'
        )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_ai_proposals_status ON ai_proposals(status)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_ai_proposals_created ON ai_proposals(created_at)")
        db.execute("""
        CREATE TABLE IF NOT EXISTS ai_approval_events (
          event_id TEXT PRIMARY KEY,
          proposal_id TEXT NOT NULL,
          actor TEXT NOT NULL,
          event_type TEXT NOT NULL,
          detail TEXT NOT NULL,
          created_at REAL NOT NULL
        )
        """)

def _json(v): return json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)

def _event(proposal_id, actor, event_type, detail):
    s=_server()
    with s.get_db() as db:
        db.execute("INSERT INTO ai_approval_events(event_id,proposal_id,actor,event_type,detail,created_at) VALUES(?,?,?,?,?,?)",
                   (uuid.uuid4().hex,proposal_id,actor,event_type,detail,time.time()))
    s.log_audit(actor,event_type,detail,proposal_id)

def _validate_target(p):
    s=_server()
    action=p["action_type"]; ids=p["target_ids"]
    if action not in ACTION_REGISTRY: raise HTTPException(400,"Action type is not registered.")
    if not ids and action not in {"CREATE_AI_TASK"}: raise HTTPException(400,"At least one target is required.")
    if action in {"REQUEST_REPROCESSING","PROPOSE_FIELD_CORRECTION","PROPOSE_STATUS_CHANGE","ASSIGN_AI_TASK","REQUEST_REVIEW","ESCALATE_RECORD"}:
        with s.get_db() as db:
            for rid in ids:
                if not db.execute("SELECT id FROM documents WHERE id=?",(str(rid),)).fetchone():
                    raise HTTPException(404,f"Document '{rid}' not found.")
    if action in {"ASSIGN_AI_TASK","REASSIGN_AI_TASK"}:
        oid=str(p["proposed_state"].get("assigned_to") or "")
        if not oid: raise HTTPException(400,"assigned_to is required.")
        with s.get_db() as db:
            u=db.execute("SELECT id,role,is_active FROM users WHERE id=? OR LOWER(email)=?",(oid,oid.lower())).fetchone()
        if not u or u["role"]!="VERIFICATION_OFFICER" or not u["is_active"]:
            raise HTTPException(400,"Target must be an active Verification Officer.")

def create_proposal(data: Dict[str,Any], created_by="AI_ASSISTANT"):
    ensure_governance_tables()
    p={**data}
    p["action_type"]=str(p.get("action_type","")).upper()
    p["target_type"]=str(p.get("target_type","")).upper()
    p["target_ids"]=[str(x) for x in (p.get("target_ids") or [])]
    p["confidence"]=float(p.get("confidence",0))
    p["risk"]=str(p.get("risk","MEDIUM")).upper()
    if p["confidence"]<0 or p["confidence"]>1: raise ValueError("confidence must be 0..1")
    _validate_target(p)
    pid="AI-"+uuid.uuid4().hex[:10].upper()
    now=time.time(); exp=now+TTL_SECONDS
    with _server().get_db() as db:
        db.execute("""INSERT INTO ai_proposals(proposal_id,action_type,target_type,target_ids,before_state,proposed_state,reason,evidence,confidence,risk,status,created_by,created_at,expires_at)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (pid,p["action_type"],p["target_type"],_json(p["target_ids"]),_json(p.get("before",{})),_json(p.get("after",{})),str(p.get("reason","")),
                    _json(p.get("evidence",[])),p["confidence"],p["risk"],PROPOSED,created_by,now,exp))
    _event(pid,created_by,"AI_PROPOSAL_CREATED",f"{p['action_type']} proposal created for {p['target_ids']}")
    return get_proposal(pid)

def get_proposal(pid):
    ensure_governance_tables()
    with _server().get_db() as db:
        r=db.execute("SELECT * FROM ai_proposals WHERE proposal_id=?",(pid,)).fetchone()
    if not r: return None
    d=dict(r)
    for k in ("target_ids","before_state","proposed_state","evidence","execution_result"):
        d[k]=json.loads(d.get(k) or ("[]" if k=="target_ids" or k=="evidence" else "{}"))
    if d["status"]==PROPOSED and time.time()>d["expires_at"]:
        with _server().get_db() as db:
            db.execute("UPDATE ai_proposals SET status=? WHERE proposal_id=? AND status=?",(EXPIRED,pid,PROPOSED))
        d["status"]=EXPIRED
    return d

def list_proposals(status_filter=None, limit=100):
    ensure_governance_tables()
    if status_filter: status_filter=status_filter.upper()
    with _server().get_db() as db:
        if status_filter:
            rows=db.execute("SELECT * FROM ai_proposals WHERE status=? ORDER BY created_at DESC LIMIT ?",(status_filter,min(max(int(limit),1),200))).fetchall()
        else:
            rows=db.execute("SELECT * FROM ai_proposals ORDER BY created_at DESC LIMIT ?",(min(max(int(limit),1),200),)).fetchall()
    return [get_proposal(r["proposal_id"]) for r in rows]

def _current_state(action, ids):
    s=_server()
    with s.get_db() as db:
        docs={}
        for rid in ids:
            row=db.execute("SELECT id,status,mean_conf,fields,uploaded_by FROM documents WHERE id=?",(rid,)).fetchone()
            if row:
                d=dict(row)
                try:d["fields"]=json.loads(d.get("fields") or "{}")
                except Exception:d["fields"]={}
                docs[rid]=d
        return {"documents":docs}

def _assert_before(proposal,current):
    before=proposal.get("before_state") or {}
    if not before: return
    expected=before.get("documents") or {}
    actual=current.get("documents") or {}
    for rid, exp in expected.items():
        got=actual.get(rid)
        if not got: raise HTTPException(409,f"Target {rid} no longer exists.")
        for key in ("status","mean_conf"):
            if key in exp and str(got.get(key))!=str(exp.get(key)):
                raise HTTPException(409,f"Target {rid} changed since this proposal was created.")

def _ensure_task_table():
    s=_server()
    with s.get_db() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS ai_tasks(
          id TEXT PRIMARY KEY, record_id TEXT, task_type TEXT NOT NULL, title TEXT NOT NULL,
          description TEXT NOT NULL, priority TEXT NOT NULL DEFAULT 'MEDIUM', assigned_to TEXT,
          assigned_by TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING', parent_task_id TEXT,
          metadata TEXT NOT NULL DEFAULT '{}', result TEXT NOT NULL DEFAULT '{}',
          created_at REAL NOT NULL, updated_at REAL NOT NULL)""")

def _create_task(db, rid, assigned_to, task_type, title, description, priority, actor, metadata=None):
    tid="TASK-"+uuid.uuid4().hex[:8].upper(); now=time.time()
    db.execute("""INSERT INTO ai_tasks(id,record_id,task_type,title,description,priority,assigned_to,assigned_by,status,parent_task_id,metadata,result,created_at,updated_at)
                  VALUES(?,?,?,?,?,?,?,?,'PENDING',NULL,?,'{}',?,?)""",
               (tid,rid,task_type,title,description,priority,assigned_to,actor,_json(metadata or {}),now,now))
    return tid

def _execute(proposal, admin):
    s=_server(); action=proposal["action_type"]; ids=proposal["target_ids"]; after=proposal["proposed_state"] or {}
    _ensure_task_table()
    with s.get_db() as db:
        if action in {"CREATE_AI_TASK","REQUEST_REVIEW","ASSIGN_AI_TASK","ESCALATE_RECORD"}:
            assigned=after.get("assigned_to")
            if assigned:
                u=db.execute("SELECT id,role,is_active FROM users WHERE id=? OR LOWER(email)=?",(str(assigned),str(assigned).lower())).fetchone()
                if not u or u["role"]!="VERIFICATION_OFFICER" or not u["is_active"]: raise HTTPException(400,"Assigned officer is no longer active.")
                assigned=str(u["id"])
            created=[]
            for rid in ids or [None]:
                title=after.get("title") or f"Review record #{rid}"
                task_type=after.get("task_type") or ("ADMIN_REVIEW" if action=="ESCALATE_RECORD" else "VERIFY_RECORD")
                created.append(_create_task(db,rid,assigned,task_type,title,after.get("description") or "Review the evidence and verification findings.",after.get("priority","MEDIUM"),admin["full_name"],after.get("metadata")))
            return {"tasks":created}
        if action=="REASSIGN_AI_TASK":
            tid=str(after.get("task_id") or (ids[0] if ids else ""))
            officer=str(after.get("assigned_to") or "")
            row=db.execute("SELECT id,assigned_to,status FROM ai_tasks WHERE id=?",(tid,)).fetchone()
            if not row: raise HTTPException(404,"AI task not found.")
            u=db.execute("SELECT id,role,is_active FROM users WHERE id=? OR LOWER(email)=?",(officer,officer.lower())).fetchone()
            if not u or u["role"]!="VERIFICATION_OFFICER" or not u["is_active"]: raise HTTPException(400,"Target officer is unavailable.")
            db.execute("UPDATE ai_tasks SET assigned_to=?,status='PENDING',updated_at=? WHERE id=?",(u["id"],time.time(),tid))
            return {"task_id":tid,"assigned_to":u["id"]}
        if action=="REQUEST_REPROCESSING":
            for rid in ids:
                db.execute("UPDATE documents SET status=?,updated_at=? WHERE id=?",(s.STATUS_DRAFT,time.time(),rid))
            return {"records":ids,"status":s.STATUS_DRAFT}
        if action=="PROPOSE_FIELD_CORRECTION":
            allowed=set(s.FIELD_KEYS)
            changes=after.get("fields") or {}
            for rid in ids:
                row=db.execute("SELECT fields,status FROM documents WHERE id=?",(rid,)).fetchone()
                if not row: raise HTTPException(404,f"Document {rid} not found.")
                if row["status"]==s.STATUS_APPROVED: raise HTTPException(400,"Approved records cannot be AI-corrected.")
                fields=json.loads(row["fields"] or "{}")
                for key,val in changes.items():
                    if key not in allowed: raise HTTPException(400,f"Field '{key}' is not registered.")
                    old=fields.get(key,{})
                    conf=old.get("confidence",0) if isinstance(old,dict) else 0
                    fields[key]={"value":str(val).strip(),"confidence":conf,"validation_status":"WARNING","validation_message":"AI-proposed correction; human verification required."}
                db.execute("UPDATE documents SET fields=?,updated_at=? WHERE id=?",(json.dumps(fields,ensure_ascii=False),time.time(),rid))
            return {"records":ids,"fields":changes}
        if action=="PROPOSE_STATUS_CHANGE":
            allowed={s.STATUS_DRAFT,s.STATUS_PROCESSING,s.STATUS_PENDING_VERIFICATION,s.STATUS_RETURNED,s.STATUS_REJECTED}
            new_status=str(after.get("status") or "")
            if new_status not in allowed: raise HTTPException(400,"Status is not registered.")
            for rid in ids:
                row=db.execute("SELECT status FROM documents WHERE id=?",(rid,)).fetchone()
                if row["status"]==s.STATUS_APPROVED and new_status!=s.STATUS_APPROVED: raise HTTPException(400,"Approved records cannot be reverted.")
                db.execute("UPDATE documents SET status=?,updated_at=? WHERE id=?",(new_status,time.time(),rid))
            return {"records":ids,"status":new_status}
        if action=="CREATE_VERIFICATION_CASE":
            # Delegate to the existing Land Intelligence schema only when it exists.
            for rid in ids:
                if not db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='verification_cases'").fetchone() and not s.DATABASE_URL:
                    raise HTTPException(503,"Verification case storage is not initialized.")
            return {"records":ids,"message":"Proposal validated; use the existing verification workflow to create the case."}
    raise HTTPException(400,"Registered action has no executor.")

def approve_proposal(pid, admin, note=""):
    p=get_proposal(pid)
    if not p: raise HTTPException(404,"Proposal not found.")
    if p["status"]!=PROPOSED: raise HTTPException(409,f"Proposal is {p['status']} and cannot be approved.")
    if time.time()>p["expires_at"]: raise HTTPException(409,"Proposal has expired.")
    _validate_target(p)
    current=_current_state(p["action_type"],p["target_ids"])
    _assert_before(p,current)
    with _server().get_db() as db:
        db.execute("UPDATE ai_proposals SET status=?,approved_by=?,approved_at=? WHERE proposal_id=? AND status=?",(APPROVED,admin["full_name"],time.time(),pid,PROPOSED))
        if db.execute("SELECT changes() AS c").fetchone()["c"]!=1: raise HTTPException(409,"Proposal was already decided.")
    _event(pid,admin["full_name"],"AI_PROPOSAL_APPROVED",f"Administrator approved proposal. {note[:240]}")
    try:
        result=_execute(p,admin)
        with _server().get_db() as db:
            db.execute("UPDATE ai_proposals SET status=?,execution_at=?,execution_result=? WHERE proposal_id=? AND status=?",(EXECUTED,time.time(),_json(result),pid,APPROVED))
        _event(pid,admin["full_name"],"AI_PROPOSAL_EXECUTED",f"Executed approved {p['action_type']} proposal.")
        return get_proposal(pid)
    except Exception as exc:
        with _server().get_db() as db:
            db.execute("UPDATE ai_proposals SET status=?,execution_at=?,execution_result=? WHERE proposal_id=? AND status=?",(FAILED,time.time(),_json({"error":str(exc)}),pid,APPROVED))
        _event(pid,admin["full_name"],"AI_PROPOSAL_FAILED",f"Approved proposal failed: {type(exc).__name__}")
        raise

def reject_proposal(pid, admin, note=""):
    p=get_proposal(pid)
    if not p: raise HTTPException(404,"Proposal not found.")
    if p["status"]!=PROPOSED: raise HTTPException(409,f"Proposal is {p['status']} and cannot be rejected.")
    with _server().get_db() as db:
        db.execute("UPDATE ai_proposals SET status=?,approved_by=?,approved_at=? WHERE proposal_id=? AND status=?",(REJECTED,admin["full_name"],time.time(),pid,PROPOSED))
    _event(pid,admin["full_name"],"AI_PROPOSAL_REJECTED",f"Administrator rejected proposal. {note[:240]}")
    return get_proposal(pid)

@router.get("/proposals")
def proposals(status_filter: Optional[str]=None, limit:int=100, user:dict=Depends(lambda: _server().require_roles(_server().ROLE_ADMIN))):
    return {"proposals":list_proposals(status_filter,limit)}

@router.get("/proposals/{proposal_id}")
def proposal_detail(proposal_id:str,user:dict=Depends(lambda: _server().require_roles(_server().ROLE_ADMIN))):
    p=get_proposal(proposal_id)
    if not p: raise HTTPException(404,"Proposal not found.")
    return {"proposal":p}

@router.post("/proposals")
def proposal_create(req:AIProposalRequest,user:dict=Depends(lambda: _server().require_roles(_server().ROLE_ADMIN))):
    # This endpoint is for an administrator to explicitly create a proposal; AI itself uses create_proposal internally.
    return {"proposal":create_proposal(req.proposal.model_dump(),created_by="ADMIN_PREPARED")}

@router.post("/proposals/{proposal_id}/approve")
def proposal_approve(proposal_id:str,req:DecisionReq,user:dict=Depends(lambda: _server().require_roles(_server().ROLE_ADMIN))):
    return {"proposal":approve_proposal(proposal_id,user,req.note)}

@router.post("/proposals/{proposal_id}/reject")
def proposal_reject(proposal_id:str,req:DecisionReq,user:dict=Depends(lambda: _server().require_roles(_server().ROLE_ADMIN))):
    return {"proposal":reject_proposal(proposal_id,user,req.note)}

@router.get("/proposals/{proposal_id}/events")
def proposal_events(proposal_id:str,user:dict=Depends(lambda: _server().require_roles(_server().ROLE_ADMIN))):
    ensure_governance_tables()
    with _server().get_db() as db:
        rows=db.execute("SELECT * FROM ai_approval_events WHERE proposal_id=? ORDER BY created_at ASC",(proposal_id,)).fetchall()
    return {"events":[dict(r) for r in rows]}
