"""Phase 14 — MUTATION tests: create, queue, review transitions, human-only
completion, the active-encumbrance safety gate, rejection, and events."""
import uuid

from land_intel import land_identity


def _survey():
    return f"7{uuid.uuid4().hex[:5]}"


def _mk(make_user_client, role, prefix):
    return make_user_client(role, prefix=prefix)


def _create_mutation(client, headers, survey, village="Mutationpur", **overrides):
    payload = {
        "survey_number": survey, "khasra_number": survey, "village": village,
        "tehsil": "Sadar", "district": "Ghaziabad",
        "previous_owner": "Ram Swaroop Sharma", "new_owner": "Amit Sharma",
        "reason_type": "SALE", "deed_no": "REG-2026-99", "deed_date": "2026-02-10",
        "documents": [],
    }
    payload.update(overrides)
    return client.post("/api/mutations", headers=headers, json=payload)


def test_mutation_requires_authentication():
    from fastapi.testclient import TestClient

    from main import app

    client = TestClient(app)
    assert client.get("/api/mutations").status_code == 401
    assert client.post("/api/mutations", json={}).status_code == 401


def test_mutation_create_defaults_and_unique_number(make_user_client):
    officer, headers, _ = _mk(make_user_client, "VERIFICATION_OFFICER", "muto")
    first = _create_mutation(officer, headers, _survey())
    second = _create_mutation(officer, headers, _survey())
    assert first.status_code == 200 and second.status_code == 200
    m1, m2 = first.json()["mutation"], second.json()["mutation"]
    assert m1["status"] == "RECEIVED"
    assert m1["mutation_no"].startswith("M-")
    assert m2["mutation_no"] != m1["mutation_no"]
    assert m1["risk_status"] in {"CLEAR", "REVIEW", "HIGH_RISK", "UNKNOWN"}


def test_mutation_invalid_type_and_missing_owner_rejected(make_user_client):
    officer, headers, _ = _mk(make_user_client, "VERIFICATION_OFFICER", "mutv")
    bad_type = _create_mutation(officer, headers, _survey(), reason_type="FORGERY")
    assert bad_type.status_code == 422
    no_owner = _create_mutation(officer, headers, _survey(), new_owner="")
    assert no_owner.status_code == 422
    no_identity = officer.post("/api/mutations", headers=headers, json={
        "previous_owner": "A", "new_owner": "B"})
    assert no_identity.status_code == 422


def test_mutation_review_transitions_and_reject_requires_notes(make_user_client):
    officer, officer_headers, _ = _mk(make_user_client, "VERIFICATION_OFFICER", "mutr")
    mutation = _create_mutation(officer, officer_headers, _survey()).json()["mutation"]
    mid = mutation["id"]

    no_notes = officer.post(f"/api/mutations/{mid}/review", headers=officer_headers,
                            json={"action": "REJECT", "notes": ""})
    assert no_notes.status_code == 400

    invalid = officer.post(f"/api/mutations/{mid}/review", headers=officer_headers,
                           json={"action": "SELF_APPROVE"})
    assert invalid.status_code == 400

    started = officer.post(f"/api/mutations/{mid}/review", headers=officer_headers,
                           json={"action": "START_REVIEW", "notes": "Documents received"})
    assert started.status_code == 200
    assert started.json()["mutation"]["status"] == "UNDER_REVIEW"

    verified = officer.post(f"/api/mutations/{mid}/review", headers=officer_headers,
                            json={"action": "MARK_VERIFIED", "notes": "Deed matches"})
    assert verified.status_code == 200
    assert verified.json()["mutation"]["status"] == "VERIFIED"

    rejected = officer.post(f"/api/mutations/{mid}/review", headers=officer_headers,
                            json={"action": "REJECT", "notes": "Signature mismatch found on re-check"})
    assert rejected.status_code == 200
    assert rejected.json()["mutation"]["status"] == "REJECTED"

    # a rejected mutation is frozen
    frozen = officer.post(f"/api/mutations/{mid}/review", headers=officer_headers,
                          json={"action": "START_REVIEW"})
    assert frozen.status_code == 400

    events = officer.get(f"/api/mutations/{mid}/events", headers=officer_headers).json()["events"]
    statuses = [event["status"] for event in events]
    assert statuses == ["RECEIVED", "UNDER_REVIEW", "VERIFIED", "REJECTED"]


def test_completion_is_admin_only_and_audited(make_user_client):
    officer, officer_headers, _ = _mk(make_user_client, "VERIFICATION_OFFICER", "mutc")
    admin, admin_headers, _ = _mk(make_user_client, "ADMIN", "mutca")
    mutation = _create_mutation(officer, officer_headers, _survey()).json()["mutation"]
    officer.post(f"/api/mutations/{mutation['id']}/review", headers=officer_headers,
                 json={"action": "MARK_VERIFIED"})

    forbidden = officer.post(f"/api/mutations/{mutation['id']}/complete",
                             headers=officer_headers, json={"notes": "attempt"})
    assert forbidden.status_code == 403

    completed = admin.post(f"/api/mutations/{mutation['id']}/complete", headers=admin_headers,
                           json={"notes": "Approved after human review"})
    assert completed.status_code == 200
    assert completed.json()["mutation"]["status"] == "COMPLETED"

    # already completed cannot be completed again
    repeat = admin.post(f"/api/mutations/{mutation['id']}/complete", headers=admin_headers, json={})
    assert repeat.status_code == 400

    audit = admin.get("/api/audit", headers=admin_headers)
    actions = [row["action"] for row in audit.json().get("audit", audit.json().get("logs", []))]
    assert "MUTATION_CREATED" in actions
    assert "MUTATION_STATUS_CHANGED" in actions
    assert "MUTATION_COMPLETED" in actions


def test_safety_gate_blocks_completion_with_active_encumbrance(make_user_client, insert_land_document):
    officer, officer_headers, _ = _mk(make_user_client, "VERIFICATION_OFFICER", "mutg")
    admin, admin_headers, _ = _mk(make_user_client, "ADMIN", "mutga")
    survey = _survey()
    doc_id = insert_land_document(survey=survey, village="Gatepur", owner="Mahesh Verma")
    officer.post("/api/encumbrances", headers=officer_headers, json={
        "survey_number": survey, "village": "Gatepur", "lender": "Example Bank",
        "reference_no": "LN-2026-004", "amount": 850000, "start_date": "2025-01-10",
        "evidence_doc_id": doc_id,
    })
    mutation = _create_mutation(officer, officer_headers, survey, village="Gatepur",
                                documents=[doc_id]).json()["mutation"]
    assert mutation["encumbrance_status"] == "ACTIVE"
    assert mutation["risk_status"] == "HIGH_RISK"

    officer.post(f"/api/mutations/{mutation['id']}/review", headers=officer_headers,
                 json={"action": "MARK_VERIFIED"})

    # 1) without confirmation the gate blocks completion with evidence payload
    blocked = admin.post(f"/api/mutations/{mutation['id']}/complete", headers=admin_headers,
                         json={"notes": ""})
    assert blocked.status_code == 409
    gate = blocked.json()["detail"]
    assert gate["code"] == "ACTIVE_ENCUMBRANCE"
    assert "active encumbrance" in gate["message"]
    assert gate["encumbrances"][0]["lender"] == "Example Bank"
    assert gate["encumbrances"][0]["reference_no"] == "LN-2026-004"

    # the blocked attempt is itself audited
    audit = admin.get("/api/audit", headers=admin_headers)
    actions = [row["action"] for row in audit.json().get("audit", audit.json().get("logs", []))]
    assert "MUTATION_SAFETY_GATE_BLOCKED" in actions

    # 2) explicit human confirmation passes the gate (still admin-governed)
    completed = admin.post(f"/api/mutations/{mutation['id']}/complete", headers=admin_headers,
                           json={"notes": "Bank NOC verified in person", "confirm_active_encumbrance": True})
    assert completed.status_code == 200
    assert completed.json()["mutation"]["status"] == "COMPLETED"


def test_gate_auto_passes_after_encumbrance_release(make_user_client, insert_land_document):
    officer, officer_headers, _ = _mk(make_user_client, "VERIFICATION_OFFICER", "mutgr")
    admin, admin_headers, _ = _mk(make_user_client, "ADMIN", "mutgra")
    survey = _survey()
    officer.post("/api/encumbrances", headers=officer_headers, json={
        "survey_number": survey, "village": "Releasepur2", "lender": "Bank", "amount": 1,
    })
    mutation = _create_mutation(officer, officer_headers, survey, village="Releasepur2").json()["mutation"]
    listing = officer.get("/api/encumbrances", headers=officer_headers, params={"q": survey})
    enc_id = listing.json()["encumbrances"][0]["id"]
    officer.post(f"/api/encumbrances/{enc_id}/release", headers=officer_headers,
                 json={"release_date": "2026-03-01"})
    officer.post(f"/api/mutations/{mutation['id']}/review", headers=officer_headers,
                 json={"action": "MARK_VERIFIED"})
    admin_done = admin.post(f"/api/mutations/{mutation['id']}/complete", headers=admin_headers,
                            json={"notes": "Loan released before completion"})
    assert admin_done.status_code == 200


def test_completion_does_not_rewrite_document_fields(make_user_client, insert_land_document):
    """The register records the transfer; OCR document fields are never edited."""
    import json

    import server

    officer, officer_headers, _ = _mk(make_user_client, "VERIFICATION_OFFICER", "mutd")
    admin, admin_headers, _ = _mk(make_user_client, "ADMIN", "mutda")
    survey = _survey()
    doc_id = insert_land_document(survey=survey, village="Boundarypur", owner="Original Owner")
    mutation = _create_mutation(officer, officer_headers, survey, village="Boundarypur",
                                documents=[doc_id]).json()["mutation"]
    admin.post(f"/api/mutations/{mutation['id']}/complete", headers=admin_headers,
               json={"notes": "ok"})
    with server.get_db() as db:
        row = db.execute("SELECT fields FROM documents WHERE id=?", (doc_id,)).fetchone()
    fields = json.loads(row["fields"])
    assert fields["owner_name"]["value"] == "Original Owner"


def test_mutation_queue_filters(make_user_client):
    officer, officer_headers, _ = _mk(make_user_client, "VERIFICATION_OFFICER", "mutq")
    mutation = _create_mutation(officer, officer_headers, _survey(), village="Queuepur").json()["mutation"]
    listed = officer.get("/api/mutations", headers=officer_headers,
                         params={"q": "Queuepur", "status": "RECEIVED"})
    assert listed.status_code == 200
    ids = [item["id"] for item in listed.json()["mutations"]]
    assert mutation["id"] in ids
    counts = listed.json()["counts"]
    assert counts["RECEIVED"] >= 1

    viewer, viewer_headers, _ = _mk(make_user_client, "VIEWER", "mutqv")
    denied = viewer.get("/api/mutations", headers=viewer_headers)
    assert denied.status_code == 403


def test_mutation_attaches_documents_with_rbac(make_user_client, insert_land_document):
    officer, officer_headers, _ = _mk(make_user_client, "VERIFICATION_OFFICER", "mutdocs")
    missing = _create_mutation(officer, officer_headers, _survey(), documents=["nope-404"])
    assert missing.status_code == 404

    doc_id = insert_land_document(survey=_survey(), village="Docspur")
    ok = _create_mutation(officer, officer_headers, survey=_survey(), village="Docspur",
                          documents=[doc_id])
    assert ok.status_code == 200
    checklist = ok.json()["mutation"]["document_checklist"]
    assert checklist and checklist[0]["document_id"] == doc_id


def test_officer_without_elevated_role_cannot_review(make_user_client):
    data_officer, do_headers, _ = _mk(make_user_client, "DATA_OFFICER", "mutrbd")
    officer, officer_headers, _ = _mk(make_user_client, "VERIFICATION_OFFICER", "mutrbo")
    mutation = _create_mutation(officer, officer_headers, _survey()).json()["mutation"]
    denied = data_officer.post(f"/api/mutations/{mutation['id']}/review", headers=do_headers,
                               json={"action": "START_REVIEW"})
    assert denied.status_code == 403
