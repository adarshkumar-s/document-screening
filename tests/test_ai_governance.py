import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import main
import server
import ai_governance


client = TestClient(main.app)


def test_admin_assistant_accepts_current_non_numeric_document_ids():
    import admin_assistant

    assert admin_assistant._record_id_from_prompt("Show document #DOC-2026-ABC") == "DOC-2026-ABC"
    assert admin_assistant._record_id_from_prompt("Propose reprocessing record 123") == "123"


def test_ai_action_registry_is_closed():
    assert "REQUEST_REPROCESSING" in ai_governance.ACTION_REGISTRY
    assert "DROP_DATABASE" not in ai_governance.ACTION_REGISTRY


def test_ai_proposal_requires_registered_action():
    with pytest.raises(Exception):
        ai_governance.create_proposal({
            "action_type": "DROP_DATABASE",
            "target_type": "DOCUMENT",
            "target_ids": ["missing"],
            "before": {},
            "after": {},
            "reason": "test",
            "evidence": [],
            "confidence": 0.5,
            "risk": "HIGH",
        }, created_by="AI_ASSISTANT")


def test_legacy_ai_execution_endpoint_is_disabled():
    response = client.post("/api/admin/assistant/execute-action", json={"token":"anything"})
    assert response.status_code in (401, 403, 410)
    if response.status_code == 410:
        assert "approval" in response.json()["detail"].lower()


def test_create_verification_case_requires_admin_approval_and_executes_server_side(tmp_path):
    old_db_path = server.DB_PATH
    server.DB_PATH = str(tmp_path / "governed-case.db")
    server.init_db()
    from mapping import _ensure_tables
    _ensure_tables()
    with server.get_db() as db:
        db.execute(
            "INSERT INTO documents (id,filename,doc_type,mean_conf,verdict,status,languages,pages,fields,validation,ai_decision_support,ocr_text,cleaned_ocr_text,detected_language,original_fields,uploaded_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("CASE-DOC","case.pdf","Land Record",60,"review","PENDING_VERIFICATION","[]",1,
             '{"owner_name":{"value":"A","confidence":0.6},"survey_number":{"value":"452","confidence":0.9},"village":{"value":"Sundarpur","confidence":0.9}}',
             '{}','{}','','','eng','{}','admin@landrec.gov.in',0,0)
        )
        db.execute("INSERT INTO property_documents(property_id,document_id,source_type,linked_at) VALUES (?,?,?,?)",
                   ("DEMO-PROP-103-A","CASE-DOC","SUPPORTING_DOCUMENT",0))
    admin = client.post("/api/auth/login", json={"email":"admin@landrec.gov.in","password":"Admin@123"})
    assert admin.status_code == 200
    headers = {"Authorization": f"Bearer {admin.json()['token']}"}
    proposal = client.post("/api/admin/ai-approval/proposals", headers=headers, json={"proposal":{
        "action_type":"CREATE_VERIFICATION_CASE",
        "target_type":"DOCUMENT",
        "target_ids":["CASE-DOC"],
        "before":{},
        "after":{"title":"Review ownership evidence"},
        "reason":"Ownership evidence requires human review.",
        "evidence":[{"kind":"FACT","field":"survey_number","value":"452"}],
        "confidence":0.9,
        "risk":"MEDIUM"
    }})
    assert proposal.status_code == 200
    pid=proposal.json()["proposal"]["proposal_id"]
    # Final approval re-verifies the authenticated administrator's password.
    approved = client.post(f"/api/admin/ai-approval/proposals/{pid}/approve", headers=headers,
                           json={"note":"Approved for verification", "password":"Admin@123"})
    assert approved.status_code == 200
    assert approved.json()["proposal"]["status"] == "EXECUTED"
    assert approved.json()["proposal"]["execution_result"]["cases"]
    with server.get_db() as db:
        assert db.execute("SELECT 1 FROM verification_cases").fetchone() is not None
    server.DB_PATH = old_db_path


def test_location_actions_are_registered_and_admin_governed():
    assert "SET_PROPERTY_LOCATION" in ai_governance.ACTION_REGISTRY
    assert "CLEAR_PROPERTY_LOCATION" in ai_governance.ACTION_REGISTRY
    with pytest.raises(Exception):
        ai_governance.create_proposal({
            "action_type":"SET_PROPERTY_LOCATION","target_type":"PROPERTY",
            "target_ids":["DOES-NOT-EXIST"],"before":{},
            "after":{"latitude":28.62,"longitude":77.10},"reason":"test",
            "evidence":[],"confidence":0.9,"risk":"HIGH"
        }, created_by="AI_ASSISTANT")


def test_location_proposal_requires_approval_before_execution(tmp_path):
    import mapping
    import server
    old_db_path = server.DB_PATH
    server.DB_PATH = str(tmp_path / "ai-location-test.db")
    server.init_db()
    mapping._ensure_tables()
    with server.get_db() as db:
        admin = db.execute("SELECT id,full_name,email FROM users WHERE role='ADMIN' AND is_active=1 ORDER BY id LIMIT 1").fetchone()
    if not admin:
        pytest.skip("No test administrator is available")
    proposal = ai_governance.create_proposal({
        "action_type":"SET_PROPERTY_LOCATION","target_type":"PROPERTY",
        "target_ids":["DEMO-PROP-103-A"],
        "before":{"properties":{"DEMO-PROP-103-A":{"location_status":"PARCEL_GEOMETRY"}}},
        "after":{"latitude":28.6227,"longitude":77.1057,"reason":"Governed test pin"},
        "reason":"AI proposes a verified pin for administrator review.",
        "evidence":[{"type":"test"}],"confidence":0.95,"risk":"HIGH"
    }, created_by="AI_ASSISTANT")
    assert proposal["status"] == "PROPOSED"
    with server.get_db() as db:
        row=db.execute("SELECT latitude,longitude,location_status FROM properties WHERE property_id='DEMO-PROP-103-A'").fetchone()
    assert row["location_status"] == "PARCEL_GEOMETRY"
    assert row["latitude"] != 28.6227
    executed=ai_governance.approve_proposal(proposal["proposal_id"],dict(admin),"test approval")
    assert executed["status"] == "EXECUTED"
    assert executed["execution_result"]["location_status"] == "EXACT_PIN"
    server.DB_PATH = old_db_path


def test_ai_location_pin_command_creates_proposal_without_mutating_property(tmp_path):
    import mapping
    import server
    old_db_path = server.DB_PATH
    server.DB_PATH = str(tmp_path / "assistant-location.db")
    server.init_db()
    mapping._ensure_tables()
    before = server.get_db()
    with before as db:
        row = db.execute("SELECT location_status,latitude,longitude FROM properties WHERE property_id='DEMO-PROP-103-A'").fetchone()
    result = __import__("admin_assistant").run_assistant_turn(
        "Set exact pin for DEMO-PROP-103-A at 28.6227, 77.1057"
    )
    assert result["action_card"]["confirmation_required"] is True
    assert result["action_card"]["action_type"] == "SET_PROPERTY_LOCATION"
    with server.get_db() as db:
        after = db.execute("SELECT location_status,latitude,longitude FROM properties WHERE property_id='DEMO-PROP-103-A'").fetchone()
    assert after["location_status"] == row["location_status"]
    assert after["latitude"] == row["latitude"]
    assert after["longitude"] == row["longitude"]
    server.DB_PATH = old_db_path


def _concurrency_probe_proposal(title):
    return ai_governance.create_proposal({
        "action_type": "CREATE_AI_TASK",
        "target_type": "DOCUMENT",
        "target_ids": [],
        "before": {},
        "after": {"title": title, "description": "race regression probe", "task_type": "ADMIN_REVIEW"},
        "reason": "concurrency regression probe",
        "evidence": [],
        "confidence": 0.9,
        "risk": "LOW",
    }, created_by="AI_ASSISTANT")


def test_concurrent_approval_executes_the_mutation_exactly_once(tmp_path, monkeypatch):
    """Two simultaneous approval requests must never execute the mutation twice.

    The atomic PROPOSED -> EXECUTING claim (compare-and-set in SQL) means only
    ONE request acquires the execution claim and runs _execute(); the losing
    request gets a safe conflict / re-attach response and cannot execute.
    """
    import threading
    import time

    old_db_path = server.DB_PATH
    server.DB_PATH = str(tmp_path / "ai-concurrency-test.db")
    server.init_db()
    ai_governance.ensure_governance_tables()
    try:
        proposal = _concurrency_probe_proposal("Concurrency probe")
        pid = proposal["proposal_id"]
        admin = {"id": "admin-1", "full_name": "Admin One", "email": "admin1@test", "role": "ADMIN"}

        real_execute = ai_governance._execute
        calls, claims = [], []

        def slow_execute(p, adm):
            # Record the execution claim this request owns, then hold the race
            # window open so the concurrent request is exercised mid-execution.
            calls.append(p["proposal_id"])
            claims.append(ai_governance.get_proposal(pid).get("execution_claim"))
            time.sleep(0.3)
            return real_execute(p, adm)

        monkeypatch.setattr(ai_governance, "_execute", slow_execute)

        results, errors = [], []
        barrier = threading.Barrier(2)

        def run():
            barrier.wait()
            try:
                results.append(ai_governance.approve_proposal(pid, admin, "concurrent"))
            except Exception as exc:  # noqa: BLE001 - recorded and asserted below
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
            assert not t.is_alive(), "approval thread hung"

        # Exactly ONE request owned the execution claim and ran _execute ...
        assert calls == [pid], "only one request may acquire the claim and run _execute"
        assert len(claims) == 1 and claims[0], "exactly one request owns the execution"

        with server.get_db() as db:
            row = db.execute(
                "SELECT status, execution_claim FROM ai_proposals WHERE proposal_id=?", (pid,)
            ).fetchone()
            tasks = db.execute(
                "SELECT COUNT(*) AS n FROM ai_tasks WHERE title=?", ("Concurrency probe",)
            ).fetchone()["n"]
        # ... the claim in the database belongs to that single execution ...
        assert row["execution_claim"] == claims[0]
        # ... the mutation happened exactly once and the proposal finished.
        assert tasks == 1, "the underlying mutation must execute exactly once"
        assert row["status"] == "EXECUTED"
        with server.get_db() as db:
            n_exec_events = db.execute(
                "SELECT COUNT(*) AS n FROM ai_approval_events WHERE proposal_id=? AND event_type='AI_PROPOSAL_EXECUTED'",
                (pid,),
            ).fetchone()["n"]
        assert n_exec_events == 1, "exactly one owner may emit the EXECUTED audit event"

        # The losing request got a safe conflict / re-attach outcome and can
        # never execute the mutation.
        assert len(results) + len(errors) == 2
        assert len(results) >= 1, "the winning request must complete"
        for exc in errors:
            assert isinstance(exc, HTTPException)
            assert exc.status_code == 409
        for r in results:
            assert r["status"] == "EXECUTED"
    finally:
        server.DB_PATH = old_db_path


def test_claimed_or_decided_states_never_execute_again(tmp_path, monkeypatch):
    """EXECUTING, FAILED, REJECTED and EXPIRED can never run the mutation again."""
    old_db_path = server.DB_PATH
    server.DB_PATH = str(tmp_path / "ai-state-guard-test.db")
    server.init_db()
    ai_governance.ensure_governance_tables()
    try:
        admin = {"id": "admin-1", "full_name": "Admin One", "email": "admin1@test", "role": "ADMIN"}
        calls = []
        monkeypatch.setattr(ai_governance, "_execute", lambda p, a: calls.append(1))
        for state in ("EXECUTING", "FAILED", "REJECTED", "EXPIRED"):
            proposal = _concurrency_probe_proposal(f"State guard {state}")
            with server.get_db() as db:
                db.execute(
                    "UPDATE ai_proposals SET status=?, execution_claim=? WHERE proposal_id=?",
                    (state, "another-owners-claim" if state == "EXECUTING" else None, proposal["proposal_id"]),
                )
            with pytest.raises(HTTPException) as exc:
                ai_governance.approve_proposal(proposal["proposal_id"], admin, "try again")
            assert exc.value.status_code == 409
            # An execution claimed by someone else can never be taken over.
            if state == "EXECUTING":
                with pytest.raises(HTTPException) as exc2:
                    ai_governance.approve_proposal(proposal["proposal_id"], admin, "take over")
                assert exc2.value.status_code == 409
        assert calls == [], "no claimed/decided state may ever execute again"

        # And completion is ownership-safe AND fail-closed: only the executor
        # that owns the execution claim can transition EXECUTING -> EXECUTED.
        # If the claim is lost mid-execution, the zombie's completion is
        # refused - no false EXECUTED event, no result clobbering - and the
        # caller gets a safe conflict/re-attach response.
        proposal = _concurrency_probe_proposal("Ownership guard")
        pid2 = proposal["proposal_id"]

        def reassign_then_succeed(p, adm):
            # Simulate another run taking over the execution claim.
            with server.get_db() as db:
                db.execute(
                    "UPDATE ai_proposals SET execution_claim=? WHERE proposal_id=?",
                    ("reclaimed-by-other-run", pid2),
                )
            return {"tasks": ["zombie-result"]}

        monkeypatch.setattr(ai_governance, "_execute", reassign_then_succeed)
        with pytest.raises(HTTPException) as exc:
            ai_governance.approve_proposal(pid2, admin, "zombie")
        assert exc.value.status_code == 409  # safe conflict / re-attach

        fresh = ai_governance.get_proposal(pid2)
        # The zombie's stale completion was rejected: state and result stay
        # with the reclaiming owner (still executing under its claim).
        assert fresh["status"] == "EXECUTING"
        assert fresh["execution_claim"] == "reclaimed-by-other-run"
        assert "zombie-result" not in json.dumps(fresh.get("execution_result") or {})
        with server.get_db() as db:
            n_exec_events = db.execute(
                "SELECT COUNT(*) AS n FROM ai_approval_events WHERE proposal_id=? AND event_type='AI_PROPOSAL_EXECUTED'",
                (pid2,),
            ).fetchone()["n"]
            n_fail_events = db.execute(
                "SELECT COUNT(*) AS n FROM ai_approval_events WHERE proposal_id=? AND event_type='AI_PROPOSAL_FAILED'",
                (pid2,),
            ).fetchone()["n"]
        # Fail-closed audit: the stale executor emits neither EXECUTED nor a
        # false FAILED for the claim it no longer owns.
        assert n_exec_events == 0
        assert n_fail_events == 0
    finally:
        server.DB_PATH = old_db_path


def test_failed_execution_completion_is_fail_closed_and_audited(tmp_path, monkeypatch):
    """The EXECUTING -> FAILED path applies the identical ownership check."""
    old_db_path = server.DB_PATH
    server.DB_PATH = str(tmp_path / "ai-fail-ownership-test.db")
    server.init_db()
    ai_governance.ensure_governance_tables()
    try:
        admin = {"id": "admin-1", "full_name": "Admin One", "email": "admin1@test", "role": "ADMIN"}

        # (a) Legitimate failure: FAILED is recorded and audited exactly once.
        proposal = _concurrency_probe_proposal("Legit failure")
        pid_a = proposal["proposal_id"]

        def fail_execute(p, adm):
            raise RuntimeError("executor blew up")

        monkeypatch.setattr(ai_governance, "_execute", fail_execute)
        with pytest.raises(RuntimeError):
            ai_governance.approve_proposal(pid_a, admin, "will fail")
        fresh = ai_governance.get_proposal(pid_a)
        assert fresh["status"] == "FAILED"
        assert "executor blew up" in json.dumps(fresh.get("execution_result") or {})
        with server.get_db() as db:
            n_fail = db.execute(
                "SELECT COUNT(*) AS n FROM ai_approval_events WHERE proposal_id=? AND event_type='AI_PROPOSAL_FAILED'",
                (pid_a,),
            ).fetchone()["n"]
        assert n_fail == 1, "a legitimate failure keeps its audit event"

        # (b) Ownership lost before the failure: no overwrite, no false event.
        proposal = _concurrency_probe_proposal("Stale failure")
        pid_b = proposal["proposal_id"]

        def reassign_then_fail(p, adm):
            with server.get_db() as db:
                db.execute(
                    "UPDATE ai_proposals SET execution_claim=? WHERE proposal_id=?",
                    ("reclaimed-by-other-run", pid_b),
                )
            raise RuntimeError("stale executor blew up")

        monkeypatch.setattr(ai_governance, "_execute", reassign_then_fail)
        with pytest.raises(RuntimeError):
            ai_governance.approve_proposal(pid_b, admin, "will fail stale")
        fresh = ai_governance.get_proposal(pid_b)
        # The current owner's claim/state/result are untouched.
        assert fresh["status"] == "EXECUTING"
        assert fresh["execution_claim"] == "reclaimed-by-other-run"
        assert "stale executor blew up" not in json.dumps(fresh.get("execution_result") or {})
        with server.get_db() as db:
            n_fail = db.execute(
                "SELECT COUNT(*) AS n FROM ai_approval_events WHERE proposal_id=? AND event_type='AI_PROPOSAL_FAILED'",
                (pid_b,),
            ).fetchone()["n"]
        assert n_fail == 0, "a stale executor must never emit a false FAILED event"
    finally:
        server.DB_PATH = old_db_path
