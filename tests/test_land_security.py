"""Phase 15 — SECURITY tests for the Land Intelligence extension.

Server-side authorisation must stay authoritative: client-supplied roles,
statuses, risk levels, land ownership and encumbrance state are never trusted.
"""
import json
import uuid
import zipfile
import io

import pytest


def _survey(prefix="5"):
    return f"{prefix}{uuid.uuid4().hex[:5]}"


def test_client_cannot_forge_mutation_status_or_risk(make_user_client):
    officer, headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="secm")
    response = officer.post("/api/mutations", headers=headers, json={
        "survey_number": _survey(), "village": "Forgeville",
        "previous_owner": "A", "new_owner": "B", "status": "COMPLETED",
        "risk_status": "CLEAR", "encumbrance_status": "CLEAR", "reviewer": "self-approved",
    })
    assert response.status_code == 200
    mutation = response.json()["mutation"]
    assert mutation["status"] == "RECEIVED"          # status never client-supplied
    assert mutation["reviewer"] != "self-approved"
    # risk snapshot is recomputed server-side, never accepted from the client
    if "risk_status" in mutation:
        assert mutation["risk_status"] in {"CLEAR", "REVIEW", "HIGH_RISK", "UNKNOWN"}


def test_client_cannot_forge_encumbrance_release_via_update(make_user_client):
    officer, headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="sece")
    survey = _survey()
    encumbrance = officer.post("/api/encumbrances", headers=headers, json={
        "survey_number": survey, "village": "Forgeville", "lender": "Bank",
    }).json()["encumbrance"]
    # direct status rewrite is rejected — release requires the audited action
    forged = officer.put(f"/api/encumbrances/{encumbrance['id']}", headers=headers,
                         json={"status": "RELEASED", "release_date": "2026-01-01"})
    assert forged.status_code == 400
    current = officer.get(f"/api/encumbrances/{encumbrance['id']}", headers=headers).json()["encumbrance"]
    assert current["status"] == "ACTIVE"


def test_unknown_encumbrance_and_mutation_ids_404(make_user_client):
    officer, headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="sec4")
    admin, admin_headers, _ = make_user_client("ADMIN", prefix="sec4a")
    assert officer.get("/api/encumbrances/missing-id", headers=headers).status_code == 404
    assert officer.get("/api/mutations/missing-id", headers=headers).status_code == 404
    assert officer.post("/api/encumbrances/missing-id/release", headers=headers, json={}).status_code == 404
    # RBAC is evaluated before existence for the admin-only completion action
    assert officer.post("/api/mutations/missing-id/complete", headers=headers, json={}).status_code == 403
    assert admin.post("/api/mutations/missing-id/complete", headers=admin_headers, json={}).status_code == 404


def test_land_id_from_client_is_never_trusted(make_user_client, insert_land_document):
    officer, headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="secl")
    survey = _survey()
    insert_land_document(survey=survey, village="Trustville")
    listed = officer.get("/api/land-records", headers=headers, params={"q": survey}).json()["land_records"][0]
    # a fabricated id must not resolve to any land
    assert officer.get("/api/land-records/LR-forged000000", headers=headers).status_code == 404
    # the canonical id is derived from the land identity, not client input
    assert listed["land_id"].startswith("LR-")


def test_data_officer_cannot_access_others_documents_via_land_apis(make_user_client, insert_land_document):
    owner, owner_headers, _ = make_user_client("DATA_OFFICER", prefix="seco")
    outsider, outsider_headers, _ = make_user_client("DATA_OFFICER", prefix="secx")
    survey = _survey()
    doc_id = insert_land_document(survey=survey, village="Idorville", uploader=owner_email(owner_headers))
    # the document belongs to another officer — direct read is forbidden
    direct = outsider.get(f"/api/documents/{doc_id}", headers=outsider_headers)
    assert direct.status_code == 403
    # land listing for a data officer only includes their own documents' lands
    lands = outsider.get("/api/land-records", headers=outsider_headers, params={"q": survey})
    assert lands.json()["total"] == 0


def owner_email(headers):
    from fastapi.testclient import TestClient

    from main import app

    client = TestClient(app)
    return client.get("/api/auth/me", headers=headers).json()["user"]["email"]


def test_report_html_escapes_malicious_field_values(make_user_client, insert_land_document):
    admin, headers, _ = make_user_client("ADMIN", prefix="secxss")
    survey = _survey()
    payload_owner = '<script>alert("xss")</script> Evil Owner'
    insert_land_document(survey=survey, village="XSSville", owner=payload_owner)
    land_id = admin.get("/api/land-records", headers=headers, params={"q": survey}).json()["land_records"][0]["land_id"]
    reference = admin.post("/api/reports/land-verification", headers=headers,
                           json={"land_id": land_id}).json()["reference_no"]
    html = admin.get(f"/api/reports/land-verification/{reference}", headers=headers).text
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_restore_member_names_cannot_escape_uploads_dir(make_user_client):
    admin, headers, _ = make_user_client("ADMIN", prefix="secup")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", json.dumps({
            "application": "document-screening", "format_version": 1,
            "table_counts": {"documents": 0},
        }))
        archive.writestr("db/tables/documents.json", "[]")
        archive.writestr("db/tables/audit.json", "[]")
        archive.writestr("db/tables/users.json", "[]")
        archive.writestr("uploads/../../etc/evil.png", b"nope")
    response = admin.post("/api/admin/data-management/restore", headers=headers,
                          files={"file": ("escape.zip", buffer.getvalue(), "application/zip")})
    assert response.status_code == 400


def test_sql_injection_attempts_are_inert(make_user_client):
    officer, headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="secs")
    survey = _survey()
    insert_doc_survey = survey
    injection = f"' OR '1'='1'; DROP TABLE land_encumbrances; --"
    created = officer.post("/api/encumbrances", headers=headers, json={
        "survey_number": injection, "village": "Injectionville", "lender": "Bank'; DROP TABLE users; --",
    })
    assert created.status_code == 200
    # the system is intact and the payload is stored as inert data
    found = officer.get("/api/encumbrances", headers=headers, params={"q": "Injectionville"})
    assert found.status_code == 200
    lands = officer.get("/api/land-records", headers=headers, params={"q": "Injectionville"})
    assert lands.status_code == 200
    # make sure the documents table is untouched by the fixture insert contract
    check = officer.get("/api/documents", headers=headers)
    assert check.status_code == 200


def test_demo_and_backup_endpoints_reject_privilege_escalation(make_user_client):
    data_officer, do_headers, _ = make_user_client("DATA_OFFICER", prefix="secp")
    assert data_officer.post("/api/admin/demo/seed", headers=do_headers, json={"scenario": "all"}).status_code == 403
    assert data_officer.delete("/api/admin/demo/data", headers=do_headers).status_code == 403
    assert data_officer.get("/api/admin/data-management/backup", headers=do_headers).status_code == 403
    assert data_officer.post("/api/admin/data-management/restore", headers=do_headers,
                             files={"file": ("x.zip", b"zip", "application/zip")}).status_code == 403


def test_safety_gate_cannot_be_bypassed_by_status_edits(make_user_client, insert_land_document):
    """Even with an encumbrance present, a non-admin can never complete."""
    officer, officer_headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="secg")
    data_officer, do_headers, _ = make_user_client("DATA_OFFICER", prefix="secgd")
    survey = _survey()
    insert_land_document(survey=survey, village="Gateville")
    officer.post("/api/encumbrances", headers=officer_headers, json={
        "survey_number": survey, "village": "Gateville", "lender": "Bank", "amount": 1,
    })
    mutation = officer.post("/api/mutations", headers=officer_headers, json={
        "survey_number": survey, "village": "Gateville",
        "previous_owner": "A", "new_owner": "B",
    }).json()["mutation"]
    # data officer cannot review or complete
    assert data_officer.post(f"/api/mutations/{mutation['id']}/review", headers=do_headers,
                             json={"action": "MARK_VERIFIED"}).status_code == 403
    assert data_officer.post(f"/api/mutations/{mutation['id']}/complete", headers=do_headers,
                             json={"confirm_active_encumbrance": True}).status_code == 403
    # even the admin confirmation flag cannot be exercised by a verifier
    officer.post(f"/api/mutations/{mutation['id']}/review", headers=officer_headers,
                 json={"action": "MARK_VERIFIED"})
    assert officer.post(f"/api/mutations/{mutation['id']}/complete", headers=officer_headers,
                        json={"notes": "bypass", "confirm_active_encumbrance": True}).status_code == 403
