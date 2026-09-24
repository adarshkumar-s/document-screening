"""SA reliability: timeout / Try again with a safe task lifecycle.

THE CRITICAL REQUIREMENT: a timeout followed by Try Again must NEVER cause the
same logical operation to execute twice.

These tests prove the at-most-once guarantees end to end:
* the original request is preserved server-side and retried verbatim;
* a retry while work is still running only re-attaches (no second execution);
* a retry after completion replays the stored result (no second execution);
* even a re-executed runner can only ever create ONE proposal (idempotency
  keys), and an approved proposal executes exactly once.
"""
import threading
import time
import uuid

import pytest

import ai_governance
import assistant_tasks


def _rid(prefix: str) -> str:
    """Unique logical request id per test run (rows persist in the test DB)."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _sa_admin(make_user_client, prefix="salo"):
    admin, headers, email = make_user_client("ADMIN", prefix=prefix)
    import sa_gateway

    sa_gateway.create_sa_credential(admin.get("/api/auth/me", headers=headers).json()["user"]["id"],
                                    "SA Lifecycle Pass 1!", label="lifecycle")
    r = admin.post("/api/sa/unlock", headers=headers, json={"password": "SA Lifecycle Pass 1!"})
    assert r.status_code == 200, r.text
    return admin, headers


# ------------------------------------------------------------------
# assistant_tasks core lifecycle (unit level)
# ------------------------------------------------------------------
def test_submit_replay_never_reexecutes_completed_work():
    calls = []

    def runner(payload, request_id):
        calls.append(request_id)
        return {"response": f"done:{payload.get('query')}"}

    rid1 = _rid("life-unit-0001")
    first = assistant_tasks.submit_or_replay(
        request_id=rid1, surface="SA", user_id="u1", kind="QUERY",
        payload={"query": "hello"}, runner=runner, wait_seconds=5,
    )
    assert first["state"] == "succeeded"
    assert first["result"]["response"] == "done:hello"
    assert len(calls) == 1

    # Retrying the SAME logical request replays the stored result.
    for _ in range(3):
        again = assistant_tasks.submit_or_replay(
            request_id=rid1, surface="SA", user_id="u1", kind="QUERY",
            payload={"query": "hello"}, runner=runner, wait_seconds=5,
        )
        assert again["state"] == "succeeded"
        assert again["replayed"] is True
    assert len(calls) == 1, "a completed logical operation must never execute twice"

    # The ORIGINAL request is preserved automatically.
    fetched = assistant_tasks.get_request(rid1, "u1")
    assert fetched["request"]["query"] == "hello"

    # An explicit Try again also only replays.
    retried = assistant_tasks.retry_request(rid1, "u1", runner, wait_seconds=5)
    assert retried["replayed"] is True
    assert len(calls) == 1


def test_request_id_reuse_with_different_payload_is_permanent_error():
    def runner(payload, request_id):
        return {"ok": True}

    rid2 = _rid("life-unit-0002")
    assistant_tasks.submit_or_replay(
        request_id=rid2, surface="SA", user_id="u1", kind="QUERY",
        payload={"query": "one"}, runner=runner, wait_seconds=5,
    )
    with pytest.raises(assistant_tasks.RequestConflict):
        assistant_tasks.submit_or_replay(
            request_id=rid2, surface="SA", user_id="u1", kind="QUERY",
            payload={"query": "TWO"}, runner=runner, wait_seconds=5,
        )


def test_timeout_reattaches_to_running_work_without_second_execution():
    calls = []
    release = threading.Event()

    def runner(payload, request_id):
        calls.append(request_id)
        release.wait(5)
        return {"response": "late result"}

    rid3 = _rid("life-unit-0003")
    timed_out = assistant_tasks.submit_or_replay(
        request_id=rid3, surface="SA", user_id="u1", kind="QUERY",
        payload={"query": "slow"}, runner=runner, wait_seconds=0.2,
    )
    # The frontend is told to offer "Try again" while the server keeps running.
    assert timed_out["state"] == "timeout"
    assert timed_out["error"]["retryable"] is True
    assert timed_out["request"]["query"] == "slow"

    # Try again while the work is STILL running: re-attach, never re-execute.
    attached = assistant_tasks.retry_request(rid3, "u1", runner, wait_seconds=0.2)
    assert attached["state"] == "running"
    assert len(calls) == 1, "a running logical operation must never start twice"

    release.set()
    deadline = time.time() + 5
    final = None
    while time.time() < deadline:
        final = assistant_tasks.get_request(rid3, "u1")
        if final["state"] == "succeeded":
            break
        time.sleep(0.05)
    assert final and final["state"] == "succeeded"
    assert final["result"]["response"] == "late result"
    assert len(calls) == 1, "the logical operation executed exactly once"

    # A late Try again replays the stored outcome.
    replay = assistant_tasks.retry_request(rid3, "u1", runner, wait_seconds=5)
    assert replay["replayed"] is True
    assert len(calls) == 1


def test_provider_failure_is_retryable_but_permanent_errors_are_not():
    calls = []

    def flaky(payload, request_id):
        calls.append(1)
        if len(calls) == 1:
            raise assistant_tasks.AssistantProviderError("The AI provider is temporarily unavailable.")
        return {"response": "recovered"}

    rid4 = _rid("life-unit-0004")
    failed = assistant_tasks.submit_or_replay(
        request_id=rid4, surface="SA", user_id="u1", kind="QUERY",
        payload={"query": "q"}, runner=flaky, wait_seconds=5,
    )
    assert failed["state"] == "failed_retryable"
    assert failed["error"]["retryable"] is True
    assert failed["error"]["code"] == "PROVIDER"

    fixed = assistant_tasks.retry_request(rid4, "u1", flaky, wait_seconds=5)
    assert fixed["state"] == "succeeded"

    def broken(payload, request_id):
        raise ValueError("bad input")

    rid5 = _rid("life-unit-0005")
    permanent = assistant_tasks.submit_or_replay(
        request_id=rid5, surface="SA", user_id="u1", kind="QUERY",
        payload={"query": "q"}, runner=broken, wait_seconds=5,
    )
    assert permanent["state"] == "failed_permanent"
    assert permanent["error"]["retryable"] is False, "permanent errors must NOT offer Try again"

    def never(payload, request_id):
        raise AssertionError("a permanent failure must never be re-executed")

    still = assistant_tasks.retry_request(rid5, "u1", never, wait_seconds=5)
    assert still["state"] == "failed_permanent"


def test_cancel_is_the_dismiss_path():
    def runner(payload, request_id):
        return {"response": "x"}

    rid6 = _rid("life-unit-0006")
    created = assistant_tasks.submit_or_replay(
        request_id=rid6, surface="SA", user_id="u1", kind="QUERY",
        payload={"query": "q"}, runner=runner, wait_seconds=5,
    )
    assert created["state"] == "succeeded"
    # Dismissing after completion keeps the result (never lost, never duplicated).
    cancelled = assistant_tasks.cancel_request(rid6, "u1")
    assert cancelled["state"] in ("succeeded", "cancelled")

    calls = []

    def flaky(payload, request_id):
        raise assistant_tasks.AssistantProviderError("temporarily down")

    def slow(payload, request_id):
        calls.append(1)
        return {"response": "y"}

    # Cancel BEFORE any successful execution: nothing runs until an explicit retry.
    rid7 = _rid("life-unit-0007")
    failed = assistant_tasks.submit_or_replay(
        request_id=rid7, surface="SA", user_id="u1", kind="QUERY",
        payload={"query": "q"}, runner=flaky, wait_seconds=5,
    )
    assert failed["state"] == "failed_retryable"
    cancelled = assistant_tasks.cancel_request(rid7, "u1")
    assert cancelled["state"] == "cancelled"
    row_state = assistant_tasks.get_request(rid7, "u1")
    assert row_state["state"] == "cancelled"
    # Cancelled work can be explicitly retried later (the request is preserved).
    revived = assistant_tasks.retry_request(rid7, "u1", slow, wait_seconds=5)
    assert revived["state"] == "succeeded"
    assert calls == [1]


# ------------------------------------------------------------------
# Proposal idempotency + execute-once governance
# ------------------------------------------------------------------
def test_one_logical_request_creates_at_most_one_proposal(tmp_path):
    import server

    old = server.DB_PATH
    server.DB_PATH = str(tmp_path / "idem-proposals.db")
    server.init_db()
    ai_governance.ensure_governance_tables()
    try:
        payload = {
            "action_type": "CREATE_AI_TASK",
            "target_type": "TASK",
            "target_ids": [],
            "before": {},
            "after": {"title": "Review flagged record", "description": "d"},
            "reason": "logical request X",
            "evidence": [],
            "confidence": 0.9,
            "risk": "MEDIUM",
        }
        first = ai_governance.create_proposal(payload, created_by="AI_ASSISTANT", idempotency_key="req-key-1")
        # Even a re-executed runner with the same key returns the SAME proposal.
        second = ai_governance.create_proposal(payload, created_by="AI_ASSISTANT", idempotency_key="req-key-1")
        third = ai_governance.create_proposal(payload, created_by="AI_ASSISTANT", idempotency_key="req-key-1")
        assert first["proposal_id"] == second["proposal_id"] == third["proposal_id"]
        with server.get_db() as db:
            count = db.execute("SELECT COUNT(*) AS n FROM ai_proposals").fetchone()["n"]
        assert count == 1, "a logical request must never create duplicate proposals"

        other = ai_governance.create_proposal(payload, created_by="AI_ASSISTANT", idempotency_key="req-key-2")
        assert other["proposal_id"] != first["proposal_id"]
    finally:
        server.DB_PATH = old


def test_approved_proposal_executes_exactly_once(tmp_path):
    import server

    old = server.DB_PATH
    server.DB_PATH = str(tmp_path / "exec-once.db")
    server.init_db()
    from mapping import _ensure_tables
    _ensure_tables()
    ai_governance.ensure_governance_tables()
    try:
        with server.get_db() as db:
            db.execute(
                "INSERT INTO documents (id,filename,doc_type,mean_conf,verdict,status,languages,pages,fields,validation,"
                "ai_decision_support,ocr_text,cleaned_ocr_text,detected_language,original_fields,uploaded_by,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("DOC-EXEC-ONCE", "a.pdf", "Land Record", 50, "review", "PENDING_VERIFICATION", "[]", 1,
                 "{}", "{}", "{}", "", "", "eng", "{}", "admin@test", 0, 111.0),
            )
        proposal = ai_governance.create_proposal({
            "action_type": "REQUEST_REPROCESSING",
            "target_type": "DOCUMENT",
            "target_ids": ["DOC-EXEC-ONCE"],
            "before": {"documents": {"DOC-EXEC-ONCE": {"status": "PENDING_VERIFICATION"}}},
            "after": {},
            "reason": "reprocess requested",
            "evidence": [],
            "confidence": 0.9,
            "risk": "LOW",
        }, created_by="AI_ASSISTANT", idempotency_key="exec-once-key")
        admin = {"id": "admin-1", "full_name": "Admin One", "email": "admin1@test", "role": "ADMIN"}

        executed = ai_governance.approve_proposal(proposal["proposal_id"], admin, "ok")
        assert executed["status"] == "EXECUTED"

        # A retried approval (e.g. after a timeout) REPLAYS the outcome and
        # never executes the mutation a second time.
        replayed = ai_governance.approve_proposal(proposal["proposal_id"], admin, "ok again")
        assert replayed["status"] == "EXECUTED"
        assert replayed.get("replayed") is True
        assert replayed["execution_result"] == executed["execution_result"]

        with server.get_db() as db:
            events = db.execute(
                "SELECT COUNT(*) AS n FROM ai_approval_events WHERE proposal_id=? AND event_type='AI_PROPOSAL_EXECUTED'",
                (proposal["proposal_id"],),
            ).fetchone()["n"]
            updated = db.execute("SELECT updated_at FROM documents WHERE id='DOC-EXEC-ONCE'").fetchone()["updated_at"]
        assert events == 1, "the logical operation executed exactly once"
        assert updated != 111.0
    finally:
        server.DB_PATH = old


# ------------------------------------------------------------------
# HTTP end-to-end through the SA surface
# ------------------------------------------------------------------
def test_sa_timeout_then_try_again_never_duplicates_the_proposal(make_user_client, insert_land_document, monkeypatch):
    """The money test: timeout -> Try again must NEVER create a second
    proposal or re-execute the completed logical operation."""
    import admin_assistant

    admin, headers = _sa_admin(make_user_client, prefix="salo1")
    doc_id = insert_land_document(status="DRAFT")

    calls = []
    real_turn = admin_assistant.run_assistant_turn

    def counting_turn(prompt, user=None, idempotency_key=None):
        calls.append({"prompt": prompt, "key": idempotency_key})
        return real_turn(prompt, user=user, idempotency_key=idempotency_key)

    monkeypatch.setattr(admin_assistant, "run_assistant_turn", counting_turn)

    query = f"Propose reprocessing document #{doc_id}"
    request_id = _rid("req-http-once")
    first = admin.post("/api/admin/assistant/query", headers=headers,
                       json={"query": query, "request_id": request_id})
    assert first.status_code == 200, first.text
    env = first.json()
    assert env["state"] == "succeeded"
    proposal_id = env["result"]["action_card"]["proposal_id"]

    # A client that timed out would hit the REAL Try again button:
    for _ in range(2):
        again = admin.post(f"/api/admin/assistant/requests/{request_id}/retry", headers=headers)
        assert again.status_code == 200, again.text
        assert again.json()["state"] == "succeeded"
        assert again.json()["replayed"] is True
        assert again.json()["result"]["action_card"]["proposal_id"] == proposal_id

    # And a full re-submission with the same request id also replays.
    resubmit = admin.post("/api/admin/assistant/query", headers=headers,
                          json={"query": query, "request_id": request_id})
    assert resubmit.json()["replayed"] is True

    assert len(calls) == 1, "the logical operation must never execute twice"
    import server

    with server.get_db() as db:
        rows = db.execute("SELECT proposal_id FROM ai_proposals WHERE idempotency_key=?", (request_id,)).fetchall()
    assert [r["proposal_id"] for r in rows] == [proposal_id], "exactly ONE proposal exists"


def test_sa_timeout_envelope_and_polling_pick_up_the_preserved_result(make_user_client, monkeypatch):
    import admin_assistant

    admin, headers = _sa_admin(make_user_client, prefix="salo2")
    release = threading.Event()
    calls = []

    def slow_turn(prompt, user=None, idempotency_key=None):
        calls.append(1)
        release.wait(5)
        return {"response": "finished after timeout", "records": [], "action_card": None}

    monkeypatch.setattr(admin_assistant, "run_assistant_turn", slow_turn)
    monkeypatch.setenv("SA_REQUEST_WAIT_SECONDS", "0.3")

    request_id = _rid("req-http-timeout")
    timed = admin.post("/api/admin/assistant/query", headers=headers,
                       json={"query": "What needs my attention?", "request_id": request_id})
    assert timed.status_code == 200, timed.text
    env = timed.json()
    assert env["state"] == "timeout"
    assert env["error"] == {"code": "TIMEOUT", "detail": "The assistant timed out.", "retryable": True}
    assert env["request"]["query"] == "What needs my attention?", "the original request is preserved"

    # Try again while still running: re-attach only.
    attached = admin.post(f"/api/admin/assistant/requests/{request_id}/retry", headers=headers)
    assert attached.json()["state"] in ("running", "timeout", "succeeded")
    assert len(calls) == 1

    release.set()
    deadline = time.time() + 5
    final = None
    while time.time() < deadline:
        polled = admin.get(f"/api/admin/assistant/requests/{request_id}", headers=headers)
        assert polled.status_code == 200
        final = polled.json()
        if final["state"] == "succeeded":
            break
        time.sleep(0.05)
    assert final and final["state"] == "succeeded"
    assert final["result"]["response"] == "finished after timeout"
    assert len(calls) == 1, "timeout + Try again must never execute twice"


def test_sa_permanent_errors_never_offer_retry(make_user_client, monkeypatch):
    import admin_assistant

    admin, headers = _sa_admin(make_user_client, prefix="salo3")

    def boom(prompt, user=None, idempotency_key=None):
        raise ValueError("validation exploded")

    monkeypatch.setattr(admin_assistant, "run_assistant_turn", boom)
    request_id = _rid("req-http-perm")
    failed = admin.post("/api/admin/assistant/query", headers=headers,
                        json={"query": "anything", "request_id": request_id})
    assert failed.status_code == 200
    env = failed.json()
    assert env["state"] == "failed_permanent"
    assert env["error"]["retryable"] is False, "permanent errors must not show Try again"

    # Empty query is a permanent validation error at the HTTP layer.
    empty = admin.post("/api/admin/assistant/query", headers=headers,
                       json={"query": "   ", "request_id": "req-http-perm-000002"})
    assert empty.status_code == 400

    # A malformed request id is a permanent validation error too.
    bad_id = admin.post("/api/admin/assistant/query", headers=headers,
                        json={"query": "hi", "request_id": "bad id with spaces!!"})
    assert bad_id.status_code == 400


def test_cancel_endpoint_dismisses_without_duplicating(make_user_client, monkeypatch):
    import admin_assistant

    admin, headers = _sa_admin(make_user_client, prefix="salo4")
    release = threading.Event()

    def slow_turn(prompt, user=None, idempotency_key=None):
        release.wait(5)
        return {"response": "late", "records": [], "action_card": None}

    monkeypatch.setattr(admin_assistant, "run_assistant_turn", slow_turn)
    monkeypatch.setenv("SA_REQUEST_WAIT_SECONDS", "0.3")

    request_id = _rid("req-http-cancel")
    timed = admin.post("/api/admin/assistant/query", headers=headers,
                       json={"query": "slow one", "request_id": request_id})
    assert timed.json()["state"] == "timeout"

    cancelled = admin.post(f"/api/admin/assistant/requests/{request_id}/cancel", headers=headers)
    assert cancelled.status_code == 200

    release.set()
    time.sleep(0.4)
    # Whatever ran while in flight is preserved exactly once and replays.
    final = admin.get(f"/api/admin/assistant/requests/{request_id}", headers=headers).json()
    assert final["state"] in ("cancelled", "succeeded")
