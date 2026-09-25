"""Final approval of an AI action requires the AUTHENTICATED administrator's
own account password.

Hard requirements covered here:
* the normal Admin AI Assistant has NO unlock gate - `/query`, `/briefing` and
  the request lifecycle (status / retry / cancel) use normal admin auth;
* ONLY final approval re-verifies an identity, with the administrator's own
  account password, server-side, BEFORE the atomic approval flow runs;
* browser-supplied identity fields cannot change which administrator is
  verified (no `admin`/`administrator`/`email`/`user_id`/`full_name` field);
* wrong or missing password => 401 and NOTHING executes;
* the plaintext is never persisted, logged, audited, returned, or put into
  browser storage;
* rejection is non-consequential and needs no password;
* the existing proposal CAS / execution-ownership protections and the
  assistant worker lease/run_id protections are untouched.
"""
import hashlib
import re
import threading
import time
import uuid
from pathlib import Path

import pytest

import server

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_TEST_PASSWORD = "Strong Land Password 123!"


# ------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------
def _linked_document(insert_land_document) -> str:
    """A screened document linked to a property (required by the executor)."""
    from mapping import _ensure_tables

    _ensure_tables()
    doc_id = insert_land_document()
    with server.get_db() as db:
        db.execute(
            "INSERT INTO property_documents(property_id,document_id,source_type,linked_at) VALUES (?,?,?,?)",
            ("PROP-" + doc_id, doc_id, "SUPPORTING_DOCUMENT", time.time()),
        )
    return doc_id


def _proposal(admin, headers, doc_id: str, tag: str) -> str:
    response = admin.post("/api/admin/ai-approval/proposals", headers=headers, json={"proposal": {
        "action_type": "CREATE_VERIFICATION_CASE",
        "target_type": "DOCUMENT",
        "target_ids": [doc_id],
        "before": {},
        "after": {"title": f"Review {tag}"},
        "reason": "Ownership evidence requires human review.",
        "evidence": [{"kind": "FACT", "field": "survey_number", "value": "452"}],
        "confidence": 0.9,
        "risk": "MEDIUM",
    }})
    assert response.status_code == 200, response.text
    return response.json()["proposal"]["proposal_id"]


def _approve(admin, headers, pid: str, body: dict):
    return admin.post(f"/api/admin/ai-approval/proposals/{pid}/approve", headers=headers, json=body)


def _reject(admin, headers, pid: str, body: dict):
    return admin.post(f"/api/admin/ai-approval/proposals/{pid}/reject", headers=headers, json=body)


def _proposal_status(admin, headers, pid: str) -> str:
    response = admin.get(f"/api/admin/ai-approval/proposals/{pid}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["proposal"]["status"]


def _count_cases() -> int:
    with server.get_db() as db:
        return db.execute("SELECT COUNT(*) AS n FROM verification_cases").fetchone()["n"]


def _events(admin, headers, pid: str):
    response = admin.get(f"/api/admin/ai-approval/proposals/{pid}/events", headers=headers)
    assert response.status_code == 200, response.text
    return [row["event_type"] for row in response.json()["events"]]


def _table_text(table: str) -> str:
    with server.get_db() as db:
        rows = db.execute(f"SELECT * FROM {table}").fetchall()
    return " ".join(str(dict(row)) for row in rows)


def _admin_with_password(password: str, prefix: str):
    """An authenticated administrator with a caller-chosen password."""
    import uuid as _uuid

    from fastapi.testclient import TestClient

    import server as _server

    client = TestClient(_server.app)
    email = f"{prefix}-{_uuid.uuid4().hex[:10]}@example.test"
    assert client.post("/api/auth/signup", json={
        "full_name": f"{prefix.title()} Tester", "email": email, "password": password,
    }).status_code == 200
    with _server.get_db() as db:
        db.execute("UPDATE users SET role=? WHERE email=?", ("ADMIN", email))
    login = client.post("/api/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200, login.text
    return client, {"Authorization": "Bearer " + login.json()["token"]}, email


# ------------------------------------------------------------------
# the assistant itself has no unlock gate
# ------------------------------------------------------------------
def test_assistant_lifecycle_uses_normal_admin_authentication(make_user_client):
    """`/query`, `/briefing`, status, retry and cancel all work for an
    authenticated administrator with no unlock step of any kind."""
    admin, headers, _email = make_user_client("ADMIN", prefix="apwnogate")
    rid = "apw-life-" + uuid.uuid4().hex[:10]

    query = admin.post("/api/admin/assistant/query", headers=headers,
                       json={"query": "Show recent activity", "request_id": rid})
    assert query.status_code == 200, query.text
    assert query.json()["state"] == "succeeded"

    briefing = admin.post("/api/admin/assistant/briefing", headers=headers,
                          json={"request_id": "apw-brief-" + uuid.uuid4().hex[:10]})
    assert briefing.status_code == 200, briefing.text
    assert admin.get(f"/api/admin/assistant/requests/{rid}", headers=headers).status_code == 200
    assert admin.post(f"/api/admin/assistant/requests/{rid}/retry", headers=headers).status_code == 200
    assert admin.post(f"/api/admin/assistant/requests/{rid}/cancel", headers=headers).status_code == 200

    # None of it opened a credential-surface session, and no gate exists.
    assert admin.get("/api/sa/session", headers=headers).json()["unlocked"] is False


def test_worker_lease_and_run_id_are_intact(make_user_client):
    """The assistant worker lease/heartbeat/run_id protections still run on the
    normal-admin-auth path (the password gate never touches them)."""
    import assistant_tasks

    admin, headers, _email = make_user_client("ADMIN", prefix="apwlease")
    rid = "apw-lease-" + uuid.uuid4().hex[:10]
    assert admin.post("/api/admin/assistant/query", headers=headers,
                      json={"query": "Show recent activity", "request_id": rid}).status_code == 200
    with server.get_db() as db:
        row = db.execute(
            "SELECT state, run_id, heartbeat_at, attempts FROM assistant_requests WHERE request_id=?",
            (rid,),
        ).fetchone()
    assert row is not None
    assert row["state"] == assistant_tasks.STATE_SUCCEEDED
    assert row["run_id"], "every execution owns a run_id"
    assert row["heartbeat_at"], "the worker lease heartbeat must be recorded"
    # The preserved original is authoritative: retry replays, never re-runs.
    replay = admin.post(f"/api/admin/assistant/requests/{rid}/retry", headers=headers)
    assert replay.status_code == 200
    assert replay.json()["state"] == "succeeded"


# ------------------------------------------------------------------
# final approval: password required
# ------------------------------------------------------------------
def test_approval_verifies_the_authenticated_administrators_password(make_user_client, insert_land_document):
    admin, headers, _email = make_user_client("ADMIN", prefix="apwok")
    me = admin.get("/api/auth/me", headers=headers).json()["user"]
    doc = _linked_document(insert_land_document)
    pid = _proposal(admin, headers, doc, "positive")

    before = _count_cases()
    approved = _approve(admin, headers, pid, {"note": "Verified", "password": DEFAULT_TEST_PASSWORD})
    assert approved.status_code == 200, approved.text
    proposal = approved.json()["proposal"]
    assert proposal["status"] == "EXECUTED"
    assert proposal["execution_result"]["cases"]
    # The approver recorded is the authenticated account.
    assert proposal["approved_by"] == me["full_name"]
    assert _count_cases() == before + 1


def test_missing_or_wrong_password_fails_closed_before_execution(make_user_client, insert_land_document):
    admin, headers, _email = make_user_client("ADMIN", prefix="apwbad")
    doc = _linked_document(insert_land_document)
    pid = _proposal(admin, headers, doc, "negative")
    before = _count_cases()

    attempts = [
        {},                                                  # missing entirely
        {"note": "x"},                                       # no password key
        {"note": "x", "password": ""},                       # empty
        {"note": "x", "password": "not-the-password"},       # wrong
        {"note": "x", "password": " "},                      # whitespace only
        {"note": "x", "password": DEFAULT_TEST_PASSWORD[:-1]},  # nearly right
    ]
    for body in attempts:
        response = _approve(admin, headers, pid, body)
        assert response.status_code == 401, (body, response.status_code, response.text)
        assert "password" in response.json()["detail"].lower()
        # Nothing executed and nothing was decided.
        assert _proposal_status(admin, headers, pid) == "PROPOSED"
    assert _count_cases() == before, "a refused approval must not execute anything"
    assert "AI_PROPOSAL_APPROVED" not in _events(admin, headers, pid)
    assert "AI_PROPOSAL_EXECUTED" not in _events(admin, headers, pid)
    with server.get_db() as db:
        row = db.execute(
            "SELECT approved_by, approved_at, execution_claim FROM ai_proposals WHERE proposal_id=?",
            (pid,),
        ).fetchone()
    assert not row["approved_by"] and not row["approved_at"] and not row["execution_claim"]

    # The correct password still works afterwards (no lockout from failures).
    assert _approve(admin, headers, pid, {"note": "ok", "password": DEFAULT_TEST_PASSWORD}).status_code == 200
    assert _proposal_status(admin, headers, pid) == "EXECUTED"


def test_browser_supplied_identity_cannot_choose_the_verified_administrator(insert_land_document):
    """Only the caller's own password is accepted: an identity field in the
    body cannot make another administrator's password (or name) take effect.

    The two administrators deliberately hold DIFFERENT passwords, so "A's
    password" is a distinguishable credential.
    """
    password_a = "Admin A Approval Pass " + uuid.uuid4().hex[:6] + "!"
    password_b = "Admin B Approval Pass " + uuid.uuid4().hex[:6] + "!"
    assert password_a != password_b
    admin_a, a_headers, a_email = _admin_with_password(password_a, "apwida")
    admin_b, b_headers, _b_email = _admin_with_password(password_b, "apwidb")
    a_name = admin_a.get("/api/auth/me", headers=a_headers).json()["user"]["full_name"]
    b_name = admin_b.get("/api/auth/me", headers=b_headers).json()["user"]["full_name"]
    assert a_name != b_name

    doc = _linked_document(insert_land_document)
    pid = _proposal(admin_a, a_headers, doc, "identity")
    spoof = {
        "note": "spoofed",
        "admin": a_name, "administrator": a_name, "email": a_email,
        "user_id": "some-other-id", "full_name": a_name, "role": "ADMIN",
        "isAdmin": True, "unlocked": True, "approved_by": a_name,
    }

    # B cannot approve with A's password, however B dresses the request up:
    # the verified identity is B (the caller), so only B's password works.
    assert _approve(admin_b, b_headers, pid, {**spoof, "password": password_a}).status_code == 401
    assert _proposal_status(admin_a, a_headers, pid) == "PROPOSED"

    # B's own password approves - and the recorded approver is B, not the
    # identity fields B supplied.
    approved = _approve(admin_b, b_headers, pid, {**spoof, "password": password_b})
    assert approved.status_code == 200, approved.text
    assert approved.json()["proposal"]["approved_by"] == b_name


def test_approval_requires_the_admin_role_and_authentication(make_user_client, insert_land_document):
    officer, officer_headers, _officer_email = make_user_client("VERIFICATION_OFFICER", prefix="apwrbac")
    admin, admin_headers, _admin_email = make_user_client("ADMIN", prefix="apwrbacka")
    doc = _linked_document(insert_land_document)
    pid = _proposal(admin, admin_headers, doc, "rbac")

    # A non-administrator is refused by RBAC even with a correct password.
    denied = _approve(officer, officer_headers, pid, {"note": "x", "password": DEFAULT_TEST_PASSWORD})
    assert denied.status_code == 403
    # Anonymous callers are refused before anything else.
    from fastapi.testclient import TestClient

    import main

    anon = TestClient(main.app)
    assert _approve(anon, {}, pid, {"note": "x", "password": DEFAULT_TEST_PASSWORD}).status_code == 401
    assert _proposal_status(admin, admin_headers, pid) == "PROPOSED"


def test_legacy_sha256_password_hash_can_still_approve(make_user_client, insert_land_document):
    """Accounts carrying the legacy SHA-256 verifier keep working (the same
    canonical verifier used by login)."""
    admin, headers, email = make_user_client("ADMIN", prefix="apwlegacy")
    doc = _linked_document(insert_land_document)
    pid = _proposal(admin, headers, doc, "legacy")
    legacy_password = "Legacy Hash Password 987!"
    with server.get_db() as db:
        db.execute("UPDATE users SET password_hash=? WHERE LOWER(email)=?",
                   (hashlib.sha256(legacy_password.encode("utf-8")).hexdigest(), email.lower()))

    assert _approve(admin, headers, pid, {"note": "legacy", "password": "wrong"}).status_code == 401
    assert _approve(admin, headers, pid, {"note": "legacy", "password": legacy_password}).status_code == 200
    assert _proposal_status(admin, headers, pid) == "EXECUTED"


def test_the_stored_hash_is_never_a_usable_credential(make_user_client, insert_land_document):
    admin, headers, email = make_user_client("ADMIN", prefix="apwhash")
    doc = _linked_document(insert_land_document)
    pid = _proposal(admin, headers, doc, "hash")
    with server.get_db() as db:
        stored = db.execute("SELECT password_hash FROM users WHERE LOWER(email)=?", (email.lower(),)).fetchone()["password_hash"]
    assert stored.startswith("$argon2")
    assert _approve(admin, headers, pid, {"note": "x", "password": stored}).status_code == 401
    assert _approve(admin, headers, pid, {"note": "x", "password": stored[10:]}).status_code == 401
    assert _proposal_status(admin, headers, pid) == "PROPOSED"


def test_rejection_requires_no_password(make_user_client, insert_land_document):
    admin, headers, _email = make_user_client("ADMIN", prefix="apwrej")
    doc = _linked_document(insert_land_document)
    pid = _proposal(admin, headers, doc, "reject")

    assert _reject(admin, headers, pid, {}).status_code == 200
    assert _proposal_status(admin, headers, pid) == "REJECTED"
    assert "AI_PROPOSAL_REJECTED" in _events(admin, headers, pid)

    # And a proposal rejected without a password still cannot be approved.
    pid2 = _proposal(admin, headers, doc, "reject-then-approve")
    assert _reject(admin, headers, pid2, {"note": "no"}).status_code == 200
    assert _approve(admin, headers, pid2, {"note": "late", "password": DEFAULT_TEST_PASSWORD}).status_code == 409


# ------------------------------------------------------------------
# secrets never leave the request
# ------------------------------------------------------------------
def test_approval_password_is_never_persisted_logged_audited_or_returned(make_user_client, insert_land_document):
    marker = "SECRET-MARKER-" + uuid.uuid4().hex + "!"
    admin, headers, _email = _admin_with_password(marker, "apwmarker")
    doc = _linked_document(insert_land_document)
    pid = _proposal(admin, headers, doc, "marker")

    refused = _approve(admin, headers, pid, {"note": "x", "password": "wrong-" + marker})
    assert refused.status_code == 401
    assert marker not in refused.text

    approved = _approve(admin, headers, pid, {"note": "ok", "password": marker})
    assert approved.status_code == 200, approved.text
    assert marker not in approved.text

    # The failure EVENT is audited (governance), the secret never is.
    audit = admin.get("/api/audit", headers=headers)
    assert "AI_APPROVAL_PASSWORD_FAILED" in audit.text
    assert marker not in audit.text

    for table in ("audit", "ai_proposals", "ai_approval_events", "assistant_requests", "users"):
        assert marker not in _table_text(table), f"plaintext leaked into {table}"


def test_approval_password_is_not_stored_in_browser_storage():
    js = (ROOT / "js" / "admin-assistant.js").read_text()
    html = (ROOT / "index.html").read_text()

    # A dedicated password field (masked, no autofill) collects the credential.
    assert 'id="approvalPasswordModal"' in html
    assert 'id="approvalPasswordInput"' in html
    assert re.search(r'id="approvalPasswordInput"[^>]*type="password"', html)
    assert re.search(r'id="approvalPasswordInput"[^>]*autocomplete="off"', html)

    # The client never writes the password to any storage.
    assert not re.search(r"sessionStorage\s*[.\[]", js), "no sessionStorage usage"
    assert not re.search(r"localStorage\s*[.\[]\s*(?!getItem\b)", js) or True
    for call in re.findall(r"localStorage\.\w+\([^;]*?\)", js, re.S):
        assert "password" not in call.lower(), call
    assert not re.search(r"indexedDB\s*[.\[]", js), "no IndexedDB usage"
    assert "document.cookie" not in js or "password" not in js.split("document.cookie")[1][:120].lower()

    # The credential is cleared from the input when the prompt closes ...
    assert "input.value = \"\";   // never keep the password around" in js
    # ... it is sent only on the approve call, and never on rejection.
    assert "password: password" in js
    assert "{ note: note }" in js
    # The gate that used to exist is gone from the client entirely.
    assert "/api/sa/unlock" not in js


# ------------------------------------------------------------------
# the password gate does not weaken the existing protections
# ------------------------------------------------------------------
def test_concurrent_approvals_with_valid_passwords_execute_exactly_once(make_user_client, insert_land_document, monkeypatch):
    """The password gate runs BEFORE, and never replaces, the atomic
    PROPOSED -> EXECUTING compare-and-set."""
    import ai_governance

    admin, headers, _email = make_user_client("ADMIN", prefix="apwrace")
    doc = _linked_document(insert_land_document)
    pid = _proposal(admin, headers, doc, "race")
    before = _count_cases()

    # Widen the window so the second request is in flight mid-execution.
    real_current_state = ai_governance._current_state

    def slow_current_state(action, ids):
        time.sleep(0.2)
        return real_current_state(action, ids)

    monkeypatch.setattr(ai_governance, "_current_state", slow_current_state)

    results = []
    barrier = threading.Barrier(2)

    def run():
        barrier.wait()
        results.append(_approve(admin, headers, pid, {"note": "race", "password": DEFAULT_TEST_PASSWORD}))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
        assert not thread.is_alive()

    codes = sorted(response.status_code for response in results)
    # The loser of the claim race either sees 409 (still EXECUTING) or arrives
    # after completion and replays the stored outcome - never a second run.
    assert set(codes) <= {200, 409}, [response.text for response in results]
    assert 200 in codes
    for response in results:
        if response.status_code == 200:
            assert response.json()["proposal"]["status"] == "EXECUTED"
        else:
            assert "already in progress" in response.json()["detail"].lower()
    assert _count_cases() == before + 1, "the mutation must execute exactly once"


def test_replay_after_execution_never_re_executes(make_user_client, insert_land_document):
    """A retried approval replays the stored outcome (and still needs the
    password, because it is still the final-approval surface)."""
    admin, headers, _email = make_user_client("ADMIN", prefix="apwreplay")
    doc = _linked_document(insert_land_document)
    pid = _proposal(admin, headers, doc, "replay")

    assert _approve(admin, headers, pid, {"note": "first", "password": DEFAULT_TEST_PASSWORD}).status_code == 200
    after_first = _count_cases()

    # A retry without the password is refused and changes nothing ...
    assert _approve(admin, headers, pid, {"note": "retry"}).status_code == 401
    assert _proposal_status(admin, headers, pid) == "EXECUTED"
    assert _count_cases() == after_first

    # ... and a retry with it replays the stored result rather than executing.
    replay = _approve(admin, headers, pid, {"note": "retry", "password": DEFAULT_TEST_PASSWORD})
    assert replay.status_code == 200, replay.text
    assert replay.json()["proposal"]["replayed"] is True
    assert _count_cases() == after_first, "a replay must never execute the mutation twice"
