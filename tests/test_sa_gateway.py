"""SA gateway security: the Admin AI entry point is SA behind a verified
secondary password (security lock).

Covers the hard requirements:
* password verified server-side and resolved to an administrator identity;
* the browser cannot supply the administrator name/role/unlock flag;
* SA sessions are short-lived, server-side, bound to the logged-in admin,
  revoked on logout / expiry / version bump;
* passwords are never logged, returned, or stored in plaintext;
* SA endpoints are unreachable without a live SA session.
"""
import re
import time
import uuid
from pathlib import Path

import pytest

import sa_gateway

ROOT = Path(__file__).resolve().parents[1]

SA_PASSWORD = "Correct Horse Battery 42!"


def _rid(prefix: str) -> str:
    """Unique logical request id per test run (rows persist in the test DB)."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _make_sa_credential(admin_email: str, password: str = SA_PASSWORD):
    import server

    with server.get_db() as db:
        user = db.execute(
            "SELECT id, full_name FROM users WHERE LOWER(email)=?", (admin_email.lower(),)
        ).fetchone()
    assert user, "admin user must exist"
    return sa_gateway.create_sa_credential(user["id"], password, label="test"), dict(user)


def _unlock(client, password=SA_PASSWORD, extra=None, headers=None):
    body = {"password": password}
    if extra:
        body.update(extra)
    return client.post("/api/sa/unlock", json=body, headers=headers or {})


# ------------------------------------------------------------------
# UI contract (Admin AI 🔒 gateway; no separate visible SA button)
# ------------------------------------------------------------------
def test_ui_shows_admin_ai_locked_and_password_prompt():
    html = (ROOT / "index.html").read_text()
    js = (ROOT / "js" / "admin-assistant.js").read_text()

    # The panel shows "Admin AI 🔒" as the entry point.
    assert "Admin AI \U0001F512" in html
    # SA is NOT directly accessible before authentication: the prompt is
    # "Enter AI access password" and the workspace starts hidden.
    assert "Enter AI access password" in html
    assert 'id="saWorkspace" hidden' in html
    assert "Welcome, " in js  # rendered with the verified administrator name
    # There is NO separate visible "SA" button.
    assert not re.search(r">\s*SA\s*</button>", html)
    # The client never stores or trusts an unlock flag; the server decides.
    assert "localStorage.setItem" not in js or "unlocked" not in js.split("localStorage.setItem")[0][-80:]
    assert 'unlocked=true' not in js


# ------------------------------------------------------------------
# Credential -> administrator identity -> SA session
# ------------------------------------------------------------------
def test_sa_requires_admin_login_and_password_cannot_be_skipped(make_user_client):
    from fastapi.testclient import TestClient

    import main

    admin, admin_headers, admin_email = make_user_client("ADMIN", prefix="sag")
    officer, officer_headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="sago")
    anon = TestClient(main.app)

    # Anonymous: no access.
    assert anon.post("/api/sa/unlock", json={"password": SA_PASSWORD}).status_code == 401
    assert anon.get("/api/sa/session").status_code == 401

    # A Verification Officer cannot open SA even with the correct password.
    _make_sa_credential(admin_email)
    denied = officer.post("/api/sa/unlock", headers=officer_headers, json={"password": SA_PASSWORD})
    assert denied.status_code == 403

    # Missing password is a validation error (hidden form fields cannot help).
    missing = admin.post("/api/sa/unlock", headers=admin_headers, json={"name": "Adarsh"})
    assert missing.status_code == 422

    # Browser-supplied identity fields are IGNORED; the verified administrator
    # name comes from the server-side credential mapping only.
    ok = _unlock(admin, extra={"name": "Adarsh", "username": "Adarsh", "isAdmin": True, "unlocked": True}, headers=admin_headers)
    assert ok.status_code == 200, ok.text
    import server

    with server.get_db() as db:
        real = db.execute("SELECT full_name FROM users WHERE LOWER(email)=?", (admin_email.lower(),)).fetchone()
    assert ok.json()["admin"]["full_name"] == real["full_name"]
    assert ok.json()["welcome"] == f"Welcome, {real['full_name']}"


def test_credential_cannot_be_used_or_created_across_administrators(make_user_client):
    """SECURITY: administrator A can neither USE nor CREATE an SA credential
    belonging to administrator B.

    ``valid credential -> administrator identity -> SA session`` still holds,
    but the credential must belong to the logged-in administrator - and the
    verified name is never accepted from the browser.
    """
    admin_a, admin_a_headers, admin_a_email = make_user_client("ADMIN", prefix="saga")
    admin_b, admin_b_headers, admin_b_email = make_user_client("ADMIN", prefix="sagb")

    import server

    with server.get_db() as db:
        db.execute("UPDATE users SET full_name=? WHERE LOWER(email)=?", ("Adarsh", admin_b_email.lower()))
    # The credential is configured for Adarsh's identity only.
    _make_sa_credential(admin_b_email, "Adarsh Access Pass 1!")

    # A cannot USE B's credential: ownership is checked server-side.
    stolen = _unlock(admin_a, password="Adarsh Access Pass 1!",
                     extra={"name": "Adarsh", "isAdmin": True, "unlocked": True},
                     headers=admin_a_headers)
    assert stolen.status_code == 401
    status = admin_a.get("/api/sa/session", headers=admin_a_headers)
    assert status.json()["unlocked"] is False

    # A cannot CREATE a credential belonging to B: the endpoint always binds
    # to the CALLING administrator. A spoofed admin_email field is ignored.
    forged = admin_a.post("/api/sa/credentials", headers=admin_a_headers,
                          json={"password": "Forged For Adarsh 1!", "admin_email": admin_b_email, "label": "x"})
    assert forged.status_code == 200, forged.text
    assert forged.json()["admin"]["full_name"] != "Adarsh"

    # The forged credential belongs to A: B cannot use it ...
    b_with_forged = _unlock(admin_b, password="Forged For Adarsh 1!", headers=admin_b_headers)
    assert b_with_forged.status_code == 401
    # ... and B's own password still resolves to B ("Welcome, Adarsh").
    ok = _unlock(admin_b, password="Adarsh Access Pass 1!", headers=admin_b_headers)
    assert ok.status_code == 200, ok.text
    assert ok.json()["admin"]["full_name"] == "Adarsh"
    assert ok.json()["welcome"] == "Welcome, Adarsh"

    # A's own credential resolves to A's own verified identity.
    ok_a = _unlock(admin_a, password="Forged For Adarsh 1!", headers=admin_a_headers)
    assert ok_a.status_code == 200, ok_a.text
    with server.get_db() as db:
        real_a = db.execute("SELECT full_name FROM users WHERE LOWER(email)=?", (admin_a_email.lower(),)).fetchone()
    assert ok_a.json()["admin"]["full_name"] == real_a["full_name"]


def test_credential_rotation_invalidates_old_password_and_existing_sessions(make_user_client):
    """Rotation must actually invalidate the old credential and revoke every
    existing SA session (re-authentication with the new password required)."""
    import server

    admin, admin_headers, admin_email = make_user_client("ADMIN", prefix="sagr")
    old_password = "Original SA Password 1!"
    new_password = "Rotated SA Password 2!"
    _make_sa_credential(admin_email, old_password)

    assert _unlock(admin, password=old_password, headers=admin_headers).status_code == 200
    captured_cookie = admin.cookies.get("sa_session")
    assert captured_cookie
    assert admin.get("/api/sa/session", headers=admin_headers).json()["unlocked"] is True

    # Rotate via the administrator endpoint (self only).
    rotated = admin.post("/api/sa/credentials", headers=admin_headers,
                         json={"password": new_password, "label": "rotate"})
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["rotated"] is True

    # 1) The OLD password stops working immediately.
    old_try = _unlock(admin, password=old_password, headers=admin_headers)
    assert old_try.status_code == 401
    # 2) Every EXISTING SA session is revoked server-side (captured cookie
    #    replay included) - re-authentication with the new password is needed.
    assert admin.get("/api/sa/session", headers=admin_headers).json()["unlocked"] is False
    replay = admin.post("/api/admin/assistant/query", headers=admin_headers,
                        cookies={"sa_session": captured_cookie},
                        json={"query": "Show recent activity", "request_id": _rid("req-rotated")})
    assert replay.status_code == 401
    # 3) The old credential is scrubbed from storage (no usable hash left).
    with server.get_db() as db:
        rows = db.execute(
            "SELECT c.secret_hash, c.is_active FROM sa_credentials c JOIN users u ON u.id=c.admin_user_id WHERE LOWER(u.email)=?",
            (admin_email.lower(),),
        ).fetchall()
    for row in rows:
        if not row["is_active"]:
            assert old_password not in (row["secret_hash"] or "")
            assert not (row["secret_hash"] or "").startswith("$argon2"), "retired hashes must be scrubbed"
    # 4) The NEW password opens a fresh session.
    assert _unlock(admin, password=new_password, headers=admin_headers).status_code == 200
    assert admin.get("/api/sa/session", headers=admin_headers).json()["unlocked"] is True


def test_wrong_password_opens_no_session_and_password_never_leaks(make_user_client):
    admin, admin_headers, admin_email = make_user_client("ADMIN", prefix="sagw")
    secret = "Super Secret SA Password 987!"
    _make_sa_credential(admin_email, secret)

    bad = _unlock(admin, password="not-the-password", headers=admin_headers)
    assert bad.status_code == 401
    assert "not-the-password" not in bad.text
    assert secret not in bad.text
    status = admin.get("/api/sa/session", headers=admin_headers)
    assert status.json()["unlocked"] is False

    # The secret is stored hashed (argon2id), never plaintext. Retired
    # (rotated) credentials are scrubbed to a non-verifying placeholder.
    import server

    with server.get_db() as db:
        rows = db.execute("SELECT secret_hash, is_active FROM sa_credentials").fetchall()
    assert rows
    joined = " ".join(r["secret_hash"] for r in rows)
    assert secret not in joined
    assert all(r["secret_hash"].startswith("$argon2") for r in rows if r["is_active"])
    assert all(secret not in (r["secret_hash"] or "") for r in rows)

    # Nothing about the secret is written to the audit trail.
    audit = admin.get("/api/audit", headers=admin_headers)
    assert secret not in audit.text
    assert "SA_UNLOCK_FAILED" in audit.text  # failure IS audited (without secret)

    ok = _unlock(admin, password=secret, headers=admin_headers)
    assert ok.status_code == 200
    assert secret not in ok.text


def test_sa_session_is_required_for_every_sa_call(make_user_client):
    """Client-side `unlocked=true` / `isAdmin` / hidden fields are worthless."""
    admin, admin_headers, admin_email = make_user_client("ADMIN", prefix="sags")
    _make_sa_credential(admin_email)

    probe_id = _rid("req-probe")
    probe = {"query": "What needs my attention?", "request_id": probe_id,
             "unlocked": True, "isAdmin": True, "name": "Adarsh"}
    denied = admin.post("/api/admin/assistant/query", headers=admin_headers, json=probe)
    assert denied.status_code in (401, 403)
    denied2 = admin.post("/api/admin/assistant/briefing", headers=admin_headers,
                         json={"request_id": _rid("req-brief"), "unlocked": True})
    assert denied2.status_code in (401, 403)
    denied3 = admin.get("/api/admin/assistant/requests/" + probe_id, headers=admin_headers)
    assert denied3.status_code in (401, 403)

    assert _unlock(admin, headers=admin_headers).status_code == 200
    allowed = admin.post("/api/admin/assistant/query", headers=admin_headers, json=probe)
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["state"] == "succeeded"


def test_logout_revokes_sa_session_server_side(make_user_client):
    admin, admin_headers, admin_email = make_user_client("ADMIN", prefix="sagl")
    _make_sa_credential(admin_email)
    assert _unlock(admin, headers=admin_headers).status_code == 200

    captured_sa_cookie = admin.cookies.get("sa_session")
    assert captured_sa_cookie
    assert admin.post("/api/admin/assistant/query", headers=admin_headers,
                      json={"query": "Show recent activity", "request_id": _rid("req-logout")}).status_code == 200

    out = admin.post("/api/auth/logout", headers=admin_headers)
    assert out.status_code == 200

    # Even replaying the captured (still unexpired) cookie cannot get back in:
    # revocation happened server-side on logout, not just client-side.
    forged = admin.post(
        "/api/admin/assistant/query",
        headers=admin_headers,
        cookies={"sa_session": captured_sa_cookie},
        json={"query": "Show recent activity", "request_id": _rid("req-logout")},
    )
    assert forged.status_code in (401, 403)


def test_sa_session_expires_and_requires_reauthentication(make_user_client):
    admin, admin_headers, admin_email = make_user_client("ADMIN", prefix="sage")
    _make_sa_credential(admin_email)
    assert _unlock(admin, headers=admin_headers).status_code == 200

    import server

    with server.get_db() as db:
        db.execute("UPDATE sa_sessions SET expires_at=? WHERE revoked_at IS NULL", (time.time() - 5,))

    denied = admin.post("/api/admin/assistant/query", headers=admin_headers,
                        json={"query": "What needs my attention?", "request_id": _rid("req-expire")})
    assert denied.status_code == 401
    status = admin.get("/api/sa/session", headers=admin_headers)
    assert status.json()["unlocked"] is False

    # Re-authentication opens a fresh session.
    assert _unlock(admin, headers=admin_headers).status_code == 200
    allowed = admin.post("/api/admin/assistant/query", headers=admin_headers,
                         json={"query": "What needs my attention?", "request_id": _rid("req-expire")})
    assert allowed.status_code == 200


def test_password_change_bumps_version_and_kills_sa_session(make_user_client):
    admin, admin_headers, admin_email = make_user_client("ADMIN", prefix="sagp")
    _make_sa_credential(admin_email)
    assert _unlock(admin, headers=admin_headers).status_code == 200

    changed = admin.post("/api/auth/change-password", headers=admin_headers,
                         json={"current_password": "Strong Land Password 123!", "new_password": "A Whole New Pass 456!"})
    assert changed.status_code == 200

    denied = admin.post("/api/admin/assistant/query", headers=admin_headers,
                        json={"query": "What needs my attention?", "request_id": _rid("req-pwchg")})
    assert denied.status_code == 401


def test_lock_endpoint_closes_sa(make_user_client):
    admin, admin_headers, admin_email = make_user_client("ADMIN", prefix="sagk")
    _make_sa_credential(admin_email)
    assert _unlock(admin, headers=admin_headers).status_code == 200
    assert admin.get("/api/sa/session", headers=admin_headers).json()["unlocked"] is True

    assert admin.post("/api/sa/lock", headers=admin_headers).status_code == 200
    assert admin.get("/api/sa/session", headers=admin_headers).json()["unlocked"] is False
    denied = admin.post("/api/admin/assistant/query", headers=admin_headers,
                        json={"query": "What needs my attention?", "request_id": _rid("req-lock")})
    assert denied.status_code == 401


def test_unlock_throttles_repeated_failures(make_user_client):
    admin, admin_headers, admin_email = make_user_client("ADMIN", prefix="sagt")
    _make_sa_credential(admin_email)
    codes = []
    for _ in range(6):
        codes.append(_unlock(admin, password="wrong-attempt", headers=admin_headers).status_code)
    assert codes[:5] == [401] * 5
    assert 429 in codes


def test_sa_request_actor_isolation_between_administrators(make_user_client):
    """One administrator can never read or retry another's SA requests."""
    import server

    admin_a, a_headers, a_email = make_user_client("ADMIN", prefix="sagaa")
    admin_b, b_headers, b_email = make_user_client("ADMIN", prefix="sagbb")
    # Each administrator gets their own DISTINCT credential.
    _make_sa_credential(a_email, "Admin A Access Pass 1!")
    _make_sa_credential(b_email, "Admin B Access Pass 2!")

    assert _unlock(admin_a, password="Admin A Access Pass 1!", headers=a_headers).status_code == 200
    iso_id = _rid("req-isolation")
    made = admin_a.post("/api/admin/assistant/query", headers=a_headers,
                        json={"query": "Show recent activity", "request_id": iso_id})
    assert made.status_code == 200

    assert _unlock(admin_b, password="Admin B Access Pass 2!", headers=b_headers).status_code == 200
    stolen = admin_b.get(f"/api/admin/assistant/requests/{iso_id}", headers=b_headers)
    assert stolen.status_code == 404
    stolen_retry = admin_b.post(f"/api/admin/assistant/requests/{iso_id}/retry", headers=b_headers)
    assert stolen_retry.status_code == 404


def test_rotation_is_atomic_single_transaction(make_user_client, monkeypatch):
    """SECURITY: rotation must be ONE database transaction.

    Deactivating+scrubbing old credentials, revoking every existing SA session
    and registering the new credential must commit together: there is no
    committed state where the new credential is active while an old captured
    SA session (or the old password) still works. A failure mid-rotation rolls
    the WHOLE rotation back.
    """
    import contextlib

    import server

    admin, admin_headers, admin_email = make_user_client("ADMIN", prefix="sagatom")
    old_password = "Atomic Rotate Old 1!"
    new_password = "Atomic Rotate New 2!"
    _created, admin_user = _make_sa_credential(admin_email, old_password)
    admin_id = admin_user["id"]

    assert _unlock(admin, password=old_password, headers=admin_headers).status_code == 200
    captured_cookie = admin.cookies.get("sa_session")
    assert captured_cookie

    # --- failure mid-rotation: EVERYTHING rolls back --------------------------
    real_revoke = sa_gateway._revoke_identity_sessions_in

    def boom(db, admin_user_id):
        raise RuntimeError("injected revocation failure")

    monkeypatch.setattr(sa_gateway, "_revoke_identity_sessions_in", boom)
    with pytest.raises(RuntimeError):
        sa_gateway.create_sa_credential(admin_id, new_password, "atomic", rotate=True)

    # The old world survives entirely: old password valid, new not registered,
    # captured SA session still live - nothing was half-committed.
    assert _unlock(admin, password=old_password, headers=admin_headers).status_code == 200
    replay = admin.get("/api/sa/session", headers=admin_headers,
                       cookies={"sa_session": captured_cookie})
    assert replay.json()["unlocked"] is True
    with server.get_db() as db:
        rows = db.execute(
            "SELECT is_active, secret_hash FROM sa_credentials WHERE admin_user_id=?", (admin_id,)
        ).fetchall()
        n_revoked = db.execute(
            "SELECT COUNT(*) AS n FROM sa_sessions WHERE (admin_user_id=? OR bound_user_id=?) "
            "AND revoked_at IS NOT NULL",
            (admin_id, admin_id),
        ).fetchone()["n"]
    assert len(rows) == 1 and rows[0]["is_active"] == 1
    assert rows[0]["secret_hash"].startswith("$argon2")
    assert n_revoked == 0

    # --- successful rotation: ALL effects share ONE transaction ---------------
    monkeypatch.setattr(sa_gateway, "_revoke_identity_sessions_in", real_revoke)
    sa_gateway.ensure_sa_tables()
    events, tx = [], {"n": 0}
    real_get_db = sa_gateway._get_db

    @contextlib.contextmanager
    def recording_get_db():
        tx["n"] += 1
        mine = tx["n"]
        with real_get_db() as db:
            orig_execute = db.execute

            def execute_recording(query, params=()):
                events.append((mine, " ".join(query.split())))
                return orig_execute(query, params)

            db.execute = execute_recording
            yield db

    monkeypatch.setattr(sa_gateway, "_get_db", recording_get_db)
    result = sa_gateway.create_sa_credential(admin_id, new_password, "atomic", rotate=True)
    assert result["revoked_sessions"] >= 1
    assert tx["n"] == 1, "rotation must be exactly one database transaction"
    assert all(t == 1 for (t, _sql) in events), "every rotation statement must share that transaction"
    tx_sqls = [sql for (_t, sql) in events]
    assert any(q.startswith("UPDATE sa_credentials") for q in tx_sqls), "old credentials scrubbed in-tx"
    assert any(q.startswith("UPDATE sa_sessions") for q in tx_sqls), "old sessions revoked in-tx"
    assert any(q.startswith("INSERT INTO sa_credentials") for q in tx_sqls), "new credential in-tx"

    # Post-conditions (deep coverage in the rotation test above): old password
    # is dead, captured session is dead, the new password authenticates.
    assert _unlock(admin, password=old_password, headers=admin_headers).status_code == 401
    dead = admin.get("/api/sa/session", headers=admin_headers,
                     cookies={"sa_session": captured_cookie})
    assert dead.json()["unlocked"] is False
    assert _unlock(admin, password=new_password, headers=admin_headers).status_code == 200


def test_throttle_state_is_shared_across_processes():
    """The failed-unlock throttle lives in the database: failures recorded by
    ONE application process are enforced by every other process/worker - the
    limit cannot be bypassed by restarting or running more workers."""
    import os
    import subprocess
    import sys

    import server

    key = "proc-throttle-" + uuid.uuid4().hex[:12]
    env = os.environ.copy()
    env["DB_PATH"] = str(server.DB_PATH)  # child process: separate memory, same database
    script = (
        "import sa_gateway\n"
        "sa_gateway.ensure_sa_tables()\n"
        f"key = {key!r}\n"
        "outs = [sa_gateway._admit_attempt(key) for _ in range(5)]\n"
        "assert outs == [True] * 5, outs\n"
        "assert sa_gateway._admit_attempt(key) is False, '6th attempt must be throttled'\n"
        "print('SUBPROCESS_OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], cwd=str(ROOT), env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert "SUBPROCESS_OK" in proc.stdout, proc.stderr or proc.stdout

    # THIS interpreter is a completely separate application instance with its
    # own memory - and it still sees the child process's failures.
    assert sa_gateway._admit_attempt(key) is False


def test_throttle_state_survives_process_state_reset(make_user_client):
    """A restarted worker (fresh process state, empty memory) does NOT reset
    the throttle: only the shared database state counts."""
    import importlib

    admin, admin_headers, admin_email = make_user_client("ADMIN", prefix="sagrs")
    _make_sa_credential(admin_email)
    codes = [_unlock(admin, password="wrong-attempt", headers=admin_headers).status_code for _ in range(3)]
    assert codes == [401] * 3

    importlib.reload(sa_gateway)  # simulate a restarted worker: all in-memory state gone

    more = [_unlock(admin, password="wrong-attempt", headers=admin_headers).status_code for _ in range(3)]
    # 3 recorded failures survived the reset: attempts 4 and 5 are still
    # admitted (401) and attempt 6 is throttled (429).
    assert more == [401, 401, 429]


def test_throttle_success_clears_and_window_expires(make_user_client, monkeypatch):
    """Policy preservation: max failed attempts per window, a successful
    authentication clears the failure state, expired windows reset, and old
    failure records are cleaned - without storing any secret."""
    import time as _time

    import server

    admin, admin_headers, admin_email = make_user_client("ADMIN", prefix="sagpol")
    secret = "Throttle Policy Pass 1!"
    _created, admin_user = _make_sa_credential(admin_email, secret)
    key = admin_user["id"]

    for _ in range(3):
        assert _unlock(admin, password="wrong", headers=admin_headers).status_code == 401
    # Successful authentication clears the failure state ...
    assert _unlock(admin, password=secret, headers=admin_headers).status_code == 200
    codes = [_unlock(admin, password="wrong", headers=admin_headers).status_code for _ in range(5)]
    assert codes == [401] * 5  # full fresh budget after the clear
    assert _unlock(admin, password="wrong", headers=admin_headers).status_code == 429

    # ... and an expired window grants a fresh budget (old records expired).
    monkeypatch.setattr(sa_gateway, "_FAILED_WINDOW_SECONDS", 0.3)
    _time.sleep(0.35)
    assert _unlock(admin, password="wrong", headers=admin_headers).status_code == 401
    with server.get_db() as db:
        row = db.execute(
            "SELECT failures FROM sa_auth_throttle WHERE key=?", (key,)
        ).fetchone()
        leaked = db.execute(
            "SELECT COUNT(*) AS n FROM sa_auth_throttle WHERE last_admit_token LIKE '%Password%' "
            "OR key LIKE '%Password%'"
        ).fetchone()["n"]
    assert row and row["failures"] == 1, "the expired window reset the counter"
    assert leaked == 0, "no secret may ever be stored in the throttle state"
