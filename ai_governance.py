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

def _admin_user():
    s=_server()
    return s.require_roles(s.ROLE_ADMIN)

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
    "SET_PROPERTY_LOCATION",
    "CLEAR_PROPERTY_LOCATION",
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
    if action in {"REQUEST_REPROCESSING","PROPOSE_FIELD_CORRECTION","PROPOSE_STATUS_CHANGE","ASSIGN_AI_TASK","REQUEST_REVIEW","ESCALATE_RECORD","CREATE_VERIFICATION_CASE"}:
        with s.get_db() as db:
            for rid in ids:
                if not db.execute("SELECT id FROM documents WHERE id=?",(str(rid),)).fetchone():
                    raise HTTPException(404,f"Document '{rid}' not found.")
    if action in {"SET_PROPERTY_LOCATION","CLEAR_PROPERTY_LOCATION"}:
        with s.get_db() as db:
            for rid in ids:
                if not db.execute("SELECT property_id FROM properties WHERE property_id=? OR parcel_id=?",(str(rid),str(rid))).fetchone():
                    raise HTTPException(404,f"Property '{rid}' not found.")
        if action == "SET_PROPERTY_LOCATION":
            proposed = p.get("proposed_state") or p.get("after") or {}
            lat, lon = proposed.get("latitude"), proposed.get("longitude")
            if lat is None or lon is None:
                raise HTTPException(400,"SET_PROPERTY_LOCATION requires latitude and longitude.")
            if not (-90 <= float(lat) <= 90 and -180 <= float(lon) <= 180):
                raise HTTPException(400,"Property coordinates are outside WGS84 bounds.")
    if action in {"ASSIGN_AI_TASK","REASSIGN_AI_TASK"}:
        assigned_values=[]
        proposed = p.get("proposed_state") or p.get("after") or {}
        if action=="ASSIGN_AI_TASK" and proposed.get("assignments"):
            assigned_values=[str(x.get("officer_id")) for x in proposed.get("assignments",[]) if x.get("officer_id")]
        else:
            oid=str(proposed.get("assigned_to") or "")
            if oid: assigned_values=[oid]
        if not assigned_values: raise HTTPException(400,"A registered active Verification Officer is required.")
        with s.get_db() as db:
            for oid in assigned_values:
                u=db.execute("SELECT id,role,is_active FROM users WHERE id=? OR LOWER(email)=?",(oid,oid.lower())).fetchone()
                if not u or u["role"]!="VERIFICATION_OFFICER" or not u["is_active"]:
                    raise HTTPException(400,"Every proposed assignee must be an active Verification Officer.")

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
    if not p.get("before") and p["target_ids"]:
        p["before"] = _current_state(p["action_type"], p["target_ids"])
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
        properties={}
        for rid in ids:
            row=db.execute("SELECT id,status,mean_conf,fields,uploaded_by FROM documents WHERE id=?",(rid,)).fetchone()
            if row:
                d=dict(row)
                try:d["fields"]=json.loads(d.get("fields") or "{}")
                except Exception:d["fields"]={}
                docs[rid]=d
            prow=db.execute("""SELECT property_id,location_status,latitude,longitude,location_updated_at
                               FROM properties WHERE property_id=? OR parcel_id=?""",(rid,rid)).fetchone()
            if prow:
                properties[prow["property_id"]]=dict(prow)
        return {"documents":docs,"properties":properties}

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
    expected_props=before.get("properties") or {}
    actual_props=current.get("properties") or {}
    for rid, exp in expected_props.items():
        got=actual_props.get(rid)
        if not got: raise HTTPException(409,f"Property {rid} no longer exists.")
        for key in ("location_status","latitude","longitude","location_updated_at"):
            if key in exp and str(got.get(key)) != str(exp.get(key)):
                raise HTTPException(409,f"Property {rid} location changed since this proposal was created.")

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
            created=[]
            assignments=after.get("assignments") if action=="ASSIGN_AI_TASK" else None
            if assignments:
                for item in assignments:
                    rid=str(item.get("record_id"))
                    assigned=str(item.get("officer_id"))
                    u=db.execute("SELECT id,role,is_active FROM users WHERE id=? OR LOWER(email)=?",(assigned,assigned.lower())).fetchone()
                    if not u or u["role"]!="VERIFICATION_OFFICER" or not u["is_active"]: raise HTTPException(400,"Assigned officer is no longer active.")
                    created.append(_create_task(db,rid,str(u["id"]),after.get("task_type","VERIFY_FAULTY_RECORD"),after.get("title") or "Review flagged land record",after.get("description") or "Review the evidence and verification findings.",str(item.get("priority") or "MEDIUM"),admin["full_name"],after.get("metadata")))
                return {"tasks":created}
            assigned=after.get("assigned_to")
            if assigned:
                u=db.execute("SELECT id,role,is_active FROM users WHERE id=? OR LOWER(email)=?",(str(assigned),str(assigned).lower())).fetchone()
                if not u or u["role"]!="VERIFICATION_OFFICER" or not u["is_active"]: raise HTTPException(400,"Assigned officer is no longer active.")
                assigned=str(u["id"])
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
        if action in {"SET_PROPERTY_LOCATION","CLEAR_PROPERTY_LOCATION"}:
            from land_intelligence import LocationUpdate, update_property_location
            for rid in ids:
                req = LocationUpdate(
                    latitude=after.get("latitude") if action=="SET_PROPERTY_LOCATION" else None,
                    longitude=after.get("longitude") if action=="SET_PROPERTY_LOCATION" else None,
                    reason=str(after.get("reason") or "AI proposal approved by administrator."),
                )
                update = update_property_location(rid, req, admin)
            return {"properties": ids, "location_status": "EXACT_PIN" if action=="SET_PROPERTY_LOCATION" else "RESTORED"}

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
            from land_intelligence import _ensure_tables
            _ensure_tables()
            cases = []
            grouped = {}
            for rid in ids:
                row = db.execute(
                    "SELECT pd.property_id FROM property_documents pd WHERE pd.document_id=? ORDER BY pd.linked_at DESC LIMIT 1",
                    (rid,),
                ).fetchone()
                property_id = row["property_id"] if row else None
                grouped.setdefault(property_id, []).append(rid)
            for property_id, record_ids in grouped.items():
                if not property_id:
                    raise HTTPException(400, "Verification cases require a document linked to a property.")
                case_id = "CASE-" + uuid.uuid4().hex[:10].upper()
                now = time.time()
                findings = [{
                    "finding_type": "AI_PROPOSAL",
                    "severity": "REVIEW",
                    "title": after.get("title") or "AI-proposed verification case",
                    "evidence": {
                        "proposal_id": proposal["proposal_id"],
                        "record_ids": record_ids,
                        "reason": proposal.get("reason", ""),
                        "evidence": proposal.get("evidence", []),
                    },
                }]
                db.execute(
                    """INSERT INTO verification_cases
                       (case_id,property_id,status,assigned_officer,findings,warnings,comparison_results,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (case_id, property_id, "OPEN", None, _json(findings), _json([]), _json([]), now, now),
                )
                for finding in findings:
                    db.execute(
                        """INSERT INTO verification_findings
                           (finding_id,property_id,case_id,finding_type,severity,status,title,evidence,created_by,created_at,updated_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        ("FND-" + uuid.uuid4().hex[:10].upper(), property_id, case_id,
                         finding["finding_type"], finding["severity"], "OPEN", finding["title"],
                         _json(finding["evidence"]), admin["full_name"], now, now),
                    )
                if property_id:
                    db.execute(
                        "INSERT INTO property_timeline(id,property_id,event_type,description,source,created_at) VALUES (?,?,?,?,?,?)",
                        (uuid.uuid4().hex, property_id, "AI_VERIFICATION_CASE_CREATED",
                         "Verification case " + case_id + " created after administrator approval of AI proposal " + proposal["proposal_id"] + ".",
                         "AI Approval Center", now),
                    )
                cases.append({"case_id": case_id, "property_id": property_id, "record_ids": record_ids})
            return {"records": ids, "cases": cases}
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
        cur=db.execute("UPDATE ai_proposals SET status=?,approved_by=?,approved_at=? WHERE proposal_id=? AND status=?",(APPROVED,admin["full_name"],time.time(),pid,PROPOSED))
        if getattr(cur,"rowcount",1)!=1: raise HTTPException(409,"Proposal was already decided.")
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
def proposals(status_filter: Optional[str]=None, limit:int=100, user:dict=Depends(_admin_user())):
    return {"proposals":list_proposals(status_filter,limit)}

@router.get("/proposals/{proposal_id}")
def proposal_detail(proposal_id:str,user:dict=Depends(_admin_user())):
    p=get_proposal(proposal_id)
    if not p: raise HTTPException(404,"Proposal not found.")
    return {"proposal":p}

@router.post("/proposals")
def proposal_create(req:AIProposalRequest,user:dict=Depends(_admin_user())):
    # This endpoint is for an administrator to explicitly create a proposal; AI itself uses create_proposal internally.
    return {"proposal":create_proposal(req.proposal.model_dump(),created_by="ADMIN_PREPARED")}

@router.post("/proposals/{proposal_id}/approve")
def proposal_approve(proposal_id:str,req:DecisionReq,user:dict=Depends(_admin_user())):
    return {"proposal":approve_proposal(proposal_id,user,req.note)}

@router.post("/proposals/{proposal_id}/reject")
def proposal_reject(proposal_id:str,req:DecisionReq,user:dict=Depends(_admin_user())):
    return {"proposal":reject_proposal(proposal_id,user,req.note)}

@router.get("/proposals/{proposal_id}/events")
def proposal_events(proposal_id:str,user:dict=Depends(_admin_user())):
    ensure_governance_tables()
    with _server().get_db() as db:
        rows=db.execute("SELECT * FROM ai_approval_events WHERE proposal_id=? ORDER BY created_at ASC",(proposal_id,)).fetchall()
    return {"events":[dict(r) for r in rows]}
