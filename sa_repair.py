"""Bounded self-healing for the SA assistant.

SA can see the whole portal, so it is often the first thing that notices a
system problem: a table that was never created on this deployment, a missing
index, a document whose OCR text never produced extracted fields. What SA may
*do* about those problems is deliberately narrow.

The rules implemented here:

1. **Detect, don't guess.** ``diagnose`` runs explicit probes. A problem exists
   only when a probe says so.
2. **Whitelisted safe repairs only.** ``SAFE_REPAIRS`` is a fixed registry of
   idempotent, non-destructive repairs: create a table that is missing, create
   an index that is missing. Nothing in it can delete, update or overwrite
   administrator data. A problem that is not in the registry is never repaired
   automatically, whatever the administrator asked for.
3. **Verify, do not assume.** After a repair the original probe is re-run.
   "The call did not raise" is not evidence of success, and neither is an HTTP
   status.
4. **One attempt, then stop.** A repair is attempted once. If verification
   fails, SA reports the exact failure and stops. There is no retry loop and
   no second, stronger attempt.
5. **Data repairs stay approval-gated.** Anything that would change a record
   becomes a normal proposal in the existing AI Approval Center, using only
   action types registered in ``ai_governance.ACTION_REGISTRY``.
6. **Every step is evidence.** Detection, repair, verification and failure are
   recorded as timestamped events so the chain can be audited afterwards.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

MAX_PROBLEMS = 12
STALE_TASK_SECONDS = 14 * 24 * 3600


# ---------------------------------------------------------------------------
# Problem / result types
# ---------------------------------------------------------------------------

@dataclass
class Problem:
    """Something a probe found wrong."""

    id: str
    title: str
    detail: str
    safe: bool
    evidence: Dict[str, Any] = field(default_factory=dict)
    proposal_action: Optional[str] = None
    proposal_targets: List[str] = field(default_factory=list)
    proposal_after: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "detail": self.detail,
            "safe": self.safe, "evidence": self.evidence,
            "proposal_action": self.proposal_action,
            "proposal_targets": list(self.proposal_targets),
        }


@dataclass
class RepairResult:
    """What SA did about one problem, with its evidence chain."""

    problem_id: str
    title: str
    detected: bool = False
    safe: bool = False
    attempted: bool = False
    applied: bool = False
    verified: bool = False
    attempts: int = 0
    error: str = ""
    detail: str = ""
    proposal: Optional[Dict[str, Any]] = None
    events: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.applied and self.verified

    def record(self, event_type: str, detail: str, **data: Any) -> None:
        self.events.append({
            "event": event_type, "detail": detail, "at": time.time(), **data,
        })

    def as_dict(self) -> Dict[str, Any]:
        return {
            "problem_id": self.problem_id, "title": self.title,
            "detected": self.detected, "safe": self.safe,
            "attempted": self.attempted, "applied": self.applied,
            "verified": self.verified, "attempts": self.attempts,
            "error": self.error, "detail": self.detail,
            "proposal_id": (self.proposal or {}).get("proposal_id"),
            "events": self.events,
        }


# ---------------------------------------------------------------------------
# Database probes
# ---------------------------------------------------------------------------

def _db():
    import server
    return server.get_db()


def table_exists(name: str) -> bool:
    with _db() as db:
        row = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    return bool(row)


def index_exists(name: str) -> bool:
    with _db() as db:
        row = db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name=?", (name,)
        ).fetchone()
    return bool(row)


def _table_names() -> List[str]:
    with _db() as db:
        rows = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return [str(row["name"]) for row in rows]


# ---------------------------------------------------------------------------
# Whitelisted safe repairs
# ---------------------------------------------------------------------------
#
# Every entry is idempotent (CREATE ... IF NOT EXISTS) and non-destructive.
# Adding an entry here is the only way to widen what SA may repair on its own.

def _repair_sa_tables() -> Dict[str, Any]:
    import sa_agent
    sa_agent.repair_schema()
    return {"repaired": ["sa_sessions", "sa_activity", "sa_conversation_state"]}


def _repair_governance_tables() -> Dict[str, Any]:
    import ai_governance
    ai_governance.ensure_governance_tables()
    return {"repaired": ["ai_proposals", "ai_approval_events"]}


def _repair_land_tables() -> Dict[str, Any]:
    import land_intel
    land_intel.ensure_land_tables()
    return {"repaired": ["land_encumbrances", "land_mutations"]}


def _repair_court_case_schema() -> Dict[str, Any]:
    import court_cases
    court_cases.ensure_schema()
    return {"repaired": ["land_court_cases"]}


def _repair_task_table() -> Dict[str, Any]:
    import admin_assistant
    admin_assistant.ensure_task_table()
    return {"repaired": ["ai_tasks"]}


def _repair_mapping_tables() -> Dict[str, Any]:
    import mapping
    mapping._ensure_tables()
    return {"repaired": ["properties", "property_documents", "verification_cases"]}


def _repair_activity_index() -> Dict[str, Any]:
    with _db() as db:
        db.execute("CREATE INDEX IF NOT EXISTS idx_sa_activity_admin_time ON sa_activity(admin_id,created_at)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_sa_activity_session ON sa_activity(session_id,created_at)")
    return {"repaired": ["idx_sa_activity_admin_time", "idx_sa_activity_session"]}


@dataclass(frozen=True)
class SafeRepair:
    """One entry of the safe-repair whitelist."""

    problem_id: str
    title: str
    detail: str
    detect: Callable[[], Optional[Dict[str, Any]]]
    repair: Callable[[], Dict[str, Any]]
    verify: Callable[[], bool]


def _missing_table_problem(problem_id: str, table: str, title: str, repair: Callable[[], Dict[str, Any]]) -> SafeRepair:
    def detect() -> Optional[Dict[str, Any]]:
        if table_exists(table):
            return None
        return {"missing_table": table, "present_tables": _table_names()[:40]}

    def verify() -> bool:
        return table_exists(table)

    return SafeRepair(
        problem_id=problem_id,
        title=title,
        detail=f"The table '{table}' does not exist in this deployment's database.",
        detect=detect,
        repair=repair,
        verify=verify,
    )


SAFE_REPAIRS: Dict[str, SafeRepair] = {
    repair.problem_id: repair
    for repair in (
        _missing_table_problem("missing_sa_tables", "sa_sessions", "SA session tables are missing", _repair_sa_tables),
        _missing_table_problem("missing_governance_tables", "ai_proposals", "AI governance tables are missing", _repair_governance_tables),
        _missing_table_problem("missing_land_tables", "land_encumbrances", "Land Intelligence tables are missing", _repair_land_tables),
        _missing_table_problem("missing_court_case_table", "land_court_cases", "Litigation register table is missing", _repair_court_case_schema),
        _missing_table_problem("missing_task_table", "ai_tasks", "AI task table is missing", _repair_task_table),
        _missing_table_problem("missing_mapping_tables", "properties", "Mapping tables are missing", _repair_mapping_tables),
        SafeRepair(
            problem_id="missing_sa_activity_index",
            title="SA activity indexes are missing",
            detail="The sa_activity table has no supporting indexes, so SA history queries scan the whole table.",
            detect=lambda: None if index_exists("idx_sa_activity_session") else {"missing_index": "idx_sa_activity_session"},
            repair=_repair_activity_index,
            verify=lambda: index_exists("idx_sa_activity_session"),
        ),
    )
}


# ---------------------------------------------------------------------------
# Approval-gated data problems
# ---------------------------------------------------------------------------

def _documents_without_extraction() -> List[Dict[str, Any]]:
    """Documents that have OCR text but no extracted fields.

    Repairing this changes a record, so it is only ever proposed.
    """
    import json as _json
    import server
    with server.get_db() as db:
        rows = db.execute(
            "SELECT id, filename, status, mean_conf, fields, ocr_text FROM documents ORDER BY created_at DESC LIMIT 500"
        ).fetchall()
    broken: List[Dict[str, Any]] = []
    for row in rows:
        raw_fields = row["fields"] if "fields" in row.keys() else ""
        try:
            fields = _json.loads(raw_fields or "{}")
        except Exception:
            fields = {}
        if not isinstance(fields, dict):
            fields = {}
        populated = {k: v for k, v in fields.items() if isinstance(v, dict) and str(v.get("value") or "").strip()}
        if (row["ocr_text"] or "").strip() and not populated:
            broken.append({
                "id": str(row["id"]), "filename": row["filename"],
                "status": row["status"], "mean_conf": row["mean_conf"],
            })
    return broken[:10]


def _stale_pending_tasks() -> List[Dict[str, Any]]:
    import server
    cutoff = time.time() - STALE_TASK_SECONDS
    try:
        with server.get_db() as db:
            rows = db.execute(
                "SELECT id, record_id, title, status, assigned_to, created_at FROM ai_tasks "
                "WHERE status='PENDING' AND created_at < ? ORDER BY created_at ASC LIMIT 20",
                (cutoff,),
            ).fetchall()
    except Exception:
        return []
    return [
        {"id": str(row["id"]), "record_id": row["record_id"], "title": row["title"],
         "assigned_to": row["assigned_to"], "age_days": round((time.time() - float(row["created_at"] or 0)) / 86400, 1)}
        for row in rows
    ]


def _detect_data_problems() -> List[Problem]:
    problems: List[Problem] = []
    try:
        broken = _documents_without_extraction()
    except Exception:
        broken = []
    if broken:
        problems.append(Problem(
            id="documents_without_extraction",
            title=f"{len(broken)} document(s) have OCR text but no extracted fields",
            detail="These documents were screened but produced no field values. Re-running the extraction "
                   "workflow would change the record, so SA will only prepare a proposal.",
            safe=False,
            evidence={"documents": broken, "count": len(broken)},
            proposal_action="REQUEST_REPROCESSING",
            proposal_targets=[str(item["id"]) for item in broken],
        ))
    try:
        stale = _stale_pending_tasks()
    except Exception:
        stale = []
    if stale:
        problems.append(Problem(
            id="stale_pending_tasks",
            title=f"{len(stale)} verification task(s) have been pending for more than 14 days",
            detail="Reassigning a task changes who is responsible for it, so SA will only prepare a proposal.",
            safe=False,
            evidence={"tasks": stale, "count": len(stale)},
            proposal_action="REASSIGN_AI_TASK",
            proposal_targets=[str(item["id"]) for item in stale],
        ))
    return problems


# ---------------------------------------------------------------------------
# Diagnosis and repair
# ---------------------------------------------------------------------------

def diagnose(problem_id: Optional[str] = None) -> List[Problem]:
    """Run every probe and return the problems that are actually present."""
    problems: List[Problem] = []
    for repair in SAFE_REPAIRS.values():
        if problem_id and repair.problem_id != problem_id:
            continue
        try:
            evidence = repair.detect()
        except Exception as exc:
            problems.append(Problem(
                id=repair.problem_id, title=repair.title,
                detail=f"The health probe itself failed: {exc}", safe=False,
                evidence={"probe_error": str(exc)},
            ))
            continue
        if evidence:
            problems.append(Problem(
                id=repair.problem_id, title=repair.title, detail=repair.detail,
                safe=True, evidence=evidence,
            ))
    if not problem_id:
        problems.extend(_detect_data_problems())
    return problems[:MAX_PROBLEMS]


def heal(problem: Problem, *, actor: Optional[Dict[str, Any]] = None,
         log: Optional[Callable[..., None]] = None) -> RepairResult:
    """Diagnose-then-repair one problem, once, and prove the result.

    ``log`` is a ``(event_type, detail, data)`` callback supplied by SA so the
    evidence chain lands in the same audit trail as everything else SA does.
    """
    result = RepairResult(problem_id=problem.id, title=problem.title, safe=problem.safe)
    result.detected = True
    result.record("PROBLEM_DETECTED", problem.detail, evidence=problem.evidence)

    def emit(event_type: str, detail: str, data: Optional[Dict[str, Any]] = None) -> None:
        result.record(event_type, detail, **(data or {}))
        if log:
            try:
                log(event_type, detail, data or {})
            except Exception:
                pass

    # Unsafe problems are never executed here: they become proposals.
    if not problem.safe:
        emit("REPAIR_WITHHELD", "This repair changes data, so it requires Administrator Approval.")
        result.detail = "Prepared for approval instead of executing."
        result.error = ""
        result.proposal = _prepare_proposal(problem, actor=actor, log=emit)
        return result

    repair = SAFE_REPAIRS.get(problem.id)
    if repair is None:
        # Anything outside the whitelist is refused rather than attempted.
        emit("REPAIR_REFUSED", "No whitelisted safe repair is registered for this problem.")
        result.error = "refused: not in the safe-repair whitelist"
        result.detail = result.error
        return result

    # Exactly one attempt. A failed repair is reported, never retried.
    result.attempted = True
    result.attempts = 1
    try:
        outcome = repair.repair()
        result.applied = True
        emit("REPAIR_EXECUTED", f"Applied the whitelisted safe repair for {problem.id}.", {"outcome": outcome})
        result.detail = str(outcome)
    except Exception as exc:
        result.applied = False
        result.error = f"repair failed: {exc}"
        result.detail = result.error
        emit("REPAIR_FAILED", result.error)
        return result

    # Verification re-runs the probe. Success is proven, not assumed.
    try:
        verified = bool(repair.verify())
    except Exception as exc:
        verified = False
        result.error = f"verification failed: {exc}"

    result.verified = verified
    if verified:
        emit("REPAIR_VERIFIED", "The original probe now reports the problem is resolved.")
    else:
        result.error = result.error or "verification failed: the probe still reports the problem"
        result.detail = result.error
        emit("REPAIR_UNVERIFIED", result.error)
    return result


def _prepare_proposal(problem: Problem, *, actor: Optional[Dict[str, Any]],
                      log: Callable[..., None]) -> Optional[Dict[str, Any]]:
    """Create an approval-gated proposal for a data repair.

    Only action types registered in ``ai_governance.ACTION_REGISTRY`` are used,
    and the proposal is created through the same helper the rest of the portal
    uses, so the Approval Center remains the only thing that can execute it.
    """
    if not problem.proposal_action or not problem.proposal_targets:
        log("REPAIR_PROPOSAL_SKIPPED", "No registered action covers this problem.")
        return None
    try:
        import ai_governance
        import admin_assistant
    except Exception as exc:
        log("REPAIR_PROPOSAL_FAILED", f"Could not load the approval system: {exc}")
        return None

    if problem.proposal_action not in ai_governance.ACTION_REGISTRY:
        log("REPAIR_PROPOSAL_REFUSED", f"Action {problem.proposal_action} is not registered.")
        return None

    evidence_items = [{"type": "diagnostic", "problem": problem.id, "detail": problem.detail}]
    reason = f"SA detected '{problem.title}' during a health check. Human approval is required before anything changes."

    try:
        if problem.proposal_action == "REASSIGN_AI_TASK":
            officer = admin_assistant.find_available_officer()
            if officer.get("error"):
                log("REPAIR_PROPOSAL_FAILED", f"No officer is available: {officer.get('error')}")
                return None
            for task in problem.evidence.get("tasks", []):
                proposal = admin_assistant._create_ai_proposal(
                    "REASSIGN_AI_TASK", "TASK", [str(task["id"])],
                    {"tasks": {str(task["id"]): {"status": "PENDING", "assigned_to": task.get("assigned_to")}}},
                    {"task_id": str(task["id"]), "assigned_to": str(officer["id"])},
                    reason, evidence_items, 0.7, "MEDIUM",
                )
                log("REPAIR_PROPOSAL_CREATED", f"Prepared reassignment for task {task['id']}.",
                    {"proposal_id": proposal.get("proposal_id")})
                return proposal
            return None
        proposal = admin_assistant._create_ai_proposal(
            problem.proposal_action,
            "DOCUMENT" if problem.proposal_action == "REQUEST_REPROCESSING" else "TASK",
            list(problem.proposal_targets),
            {"documents": {target: {} for target in problem.proposal_targets}},
            problem.proposal_after or {},
            reason, evidence_items, 0.7, "MEDIUM",
        )
    except Exception as exc:
        log("REPAIR_PROPOSAL_FAILED", f"Could not create the proposal: {exc}")
        return None

    log("REPAIR_PROPOSAL_CREATED", f"Prepared {problem.proposal_action} for {len(problem.proposal_targets)} target(s).",
        {"proposal_id": proposal.get("proposal_id")})
    return proposal


def self_heal(*, actor: Optional[Dict[str, Any]] = None,
              log: Optional[Callable[..., None]] = None,
              only: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Run the full detect -> repair -> verify -> report cycle.

    Returns a report describing every problem found and exactly what happened
    for each one. Nothing in this function can mutate a business record.
    """
    problems = diagnose()
    if only:
        wanted = set(only)
        problems = [p for p in problems if p.id in wanted]
    results = [heal(problem, actor=actor, log=log) for problem in problems]
    return {
        "checked_at": time.time(),
        "problems_found": len(problems),
        "safe_repairs_applied": sum(1 for r in results if r.ok),
        "safe_repairs_failed": sum(1 for r in results if r.attempted and not r.ok),
        "proposals_created": sum(1 for r in results if r.proposal),
        "results": [r.as_dict() for r in results],
    }
