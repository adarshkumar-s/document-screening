"""Phase 14 — ENCUMBRANCE tests: create/list/update/release, RBAC, statuses,
release semantics, and land-record scoping."""
import uuid

import pytest


def _unique_survey(prefix="9"):
    return f"{prefix}{uuid.uuid4().hex[:5]}"


def test_encumbrance_create_requires_reviewer_role(make_user_client):
    viewer, viewer_headers, _ = make_user_client("VIEWER", prefix="encv")
    officer, officer_headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="enco")
    survey = _unique_survey()

    forbidden = viewer.post("/api/encumbrances", headers=viewer_headers, json={
        "survey_number": survey, "village": "Sundarpur", "lender": "Example Bank",
    })
    assert forbidden.status_code == 403

    created = officer.post("/api/encumbrances", headers=officer_headers, json={
        "survey_number": survey, "village": "Sundarpur", "lender": "Example Bank",
        "reference_no": "LN-2026-00123", "amount": 850000, "start_date": "2026-01-10",
        "owner_name": "Ram Singh",
    })
    assert created.status_code == 200, created.text
    encumbrance = created.json()["encumbrance"]
    assert encumbrance["status"] == "ACTIVE"
    assert encumbrance["lender"] == "Example Bank"
    assert encumbrance["reference_no"] == "LN-2026-00123"
    assert float(encumbrance["amount"]) == 850000.0
    assert encumbrance["is_active"] is True


def test_encumbrance_requires_authentication():
    from fastapi.testclient import TestClient

    from main import app

    client = TestClient(app)
    assert client.get("/api/encumbrances").status_code == 401
    assert client.post("/api/encumbrances", json={"lender": "X", "survey_number": "1"}).status_code == 401


def test_encumbrance_requires_land_identity(make_user_client):
    officer, officer_headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="encn")
    response = officer.post("/api/encumbrances", headers=officer_headers, json={"lender": "No Land Bank"})
    assert response.status_code == 422


def test_encumbrance_rejects_invalid_status(make_user_client):
    officer, officer_headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="encs")
    bad = officer.post("/api/encumbrances", headers=officer_headers, json={
        "survey_number": _unique_survey(), "village": "Sundarpur", "lender": "Bank",
        "status": "SETTLED",
    })
    assert bad.status_code == 422


def test_encumbrance_release_flow_and_auditing(make_user_client, insert_land_document):
    officer, officer_headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="encr")
    admin, admin_headers, _ = make_user_client("ADMIN", prefix="encra")
    survey = _unique_survey("87")
    doc_id = insert_land_document(survey=survey, village="Releasepur")

    created = officer.post("/api/encumbrances", headers=officer_headers, json={
        "survey_number": survey, "village": "Releasepur", "lender": "Gramin Bank",
        "reference_no": "LN-2026-777", "amount": 250000, "start_date": "2025-02-01",
        "evidence_doc_id": doc_id,
    })
    assert created.status_code == 200
    encumbrance_id = created.json()["encumbrance"]["id"]

    # data officer may not release (RBAC)
    data_officer, do_headers, _ = make_user_client("DATA_OFFICER", prefix="encd")
    denied = data_officer.post(f"/api/encumbrances/{encumbrance_id}/release", headers=do_headers,
                               json={"release_date": "2026-02-01"})
    assert denied.status_code == 403

    released = admin.post(f"/api/encumbrances/{encumbrance_id}/release", headers=admin_headers,
                          json={"release_date": "2026-02-01", "notes": "NOC received"})
    assert released.status_code == 200
    released_row = released.json()["encumbrance"]
    assert released_row["status"] == "RELEASED"
    assert released_row["release_date"] == "2026-02-01"
    assert released_row["is_active"] is False

    # releasing again must fail
    again = admin.post(f"/api/encumbrances/{encumbrance_id}/release", headers=admin_headers, json={})
    assert again.status_code == 400

    # release is audited
    audit = admin.get("/api/audit", headers=admin_headers)
    assert audit.status_code == 200
    actions = [row["action"] for row in audit.json().get("audit", audit.json().get("logs", []))]
    assert "ENCUMBRANCE_CREATED" in actions
    assert "ENCUMBRANCE_RELEASED" in actions


def test_encumbrance_update_audited_and_restricted(make_user_client):
    officer, officer_headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="encu")
    survey = _unique_survey("55")
    created = officer.post("/api/encumbrances", headers=officer_headers, json={
        "survey_number": survey, "village": "Updatepur", "lender": "State Bank", "amount": 100000,
    })
    encumbrance_id = created.json()["encumbrance"]["id"]

    data_officer, do_headers, _ = make_user_client("DATA_OFFICER", prefix="encud")
    denied = data_officer.put(f"/api/encumbrances/{encumbrance_id}", headers=do_headers,
                              json={"lender": "New Bank"})
    assert denied.status_code == 403

    updated = officer.put(f"/api/encumbrances/{encumbrance_id}", headers=officer_headers,
                          json={"lender": "Cooperative Bank", "amount": 125000})
    assert updated.status_code == 200
    assert updated.json()["encumbrance"]["lender"] == "Cooperative Bank"

    # status transition to RELEASED must go through the release action
    blocked = officer.put(f"/api/encumbrances/{encumbrance_id}", headers=officer_headers,
                          json={"status": "RELEASED"})
    assert blocked.status_code == 400


def test_encumbrance_evidence_document_must_exist(make_user_client):
    officer, officer_headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="ence")
    response = officer.post("/api/encumbrances", headers=officer_headers, json={
        "survey_number": _unique_survey(), "village": "Evidencepur", "lender": "Bank",
        "evidence_doc_id": "does-not-exist",
    })
    assert response.status_code == 404


def test_encumbrance_list_filters_by_status(make_user_client):
    officer, officer_headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="encl")
    survey = _unique_survey("33")
    officer.post("/api/encumbrances", headers=officer_headers, json={
        "survey_number": survey, "village": "Listpur", "lender": "Filter Bank", "reference_no": survey,
    })
    listed = officer.get("/api/encumbrances", headers=officer_headers, params={"q": survey, "status": "ACTIVE"})
    assert listed.status_code == 200
    body = listed.json()
    assert body["total"] >= 1
    assert all(item["status"] == "ACTIVE" for item in body["encumbrances"])
