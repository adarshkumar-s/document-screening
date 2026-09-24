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


def test_credential_resolves_to_configured_administrator_identity(make_user_client):
    """valid credential -> administrator identity -> SA session (the name is
    NEVER accepted from the browser)."""
    admin_a, admin_a_headers, _ = make_user_client("ADMIN", prefix="saga")
    _admin_b, _b_headers, admin_b_email = make_user_client("ADMIN", prefix="sagb")

    import server

    with server.get_db() as db:
        db.execute("UPDATE users SET full_name=? WHERE LOWER(email)=?", ("Adarsh", admin_b_email.lower()))
    # The credential is configured for Adarsh's identity only.
    _make_sa_credential(admin_b_email)

    # A different signed-in administrator presenting the credential becomes the
    # VERIFIED identity (Adarsh) - the submitted "name" cannot fake this.
    ok = _unlock(admin_a, extra={"name": "Not Adarsh"}, headers=admin_a_headers)
    assert ok.status_code == 200, ok.text
    assert ok.json()["admin"]["full_name"] == "Adarsh"
    assert ok.json()["welcome"] == "Welcome, Adarsh"

    status = admin_a.get("/api/sa/session", headers=admin_a_headers)
    assert status.json()["unlocked"] is True
    assert status.json()["admin"]["full_name"] == "Adarsh"


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

    # The secret is stored hashed (argon2id), never plaintext.
    import server

    with server.get_db() as db:
        rows = db.execute("SELECT secret_hash FROM sa_credentials").fetchall()
    assert rows
    joined = " ".join(r["secret_hash"] for r in rows)
    assert secret not in joined
    assert all(r["secret_hash"].startswith("$argon2") for r in rows)

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
