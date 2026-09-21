"""Phase 14 — LAND RECORD / INTEGRATION tests: land-record listing and
search, detail sections, role scoping, document-level land context in the
existing verification APIs, and the ownership-history integration."""
import json
import uuid

import pytest


def _survey(prefix="6"):
    return f"{prefix}{uuid.uuid4().hex[:5]}"


def test_land_records_require_authentication():
    from fastapi.testclient import TestClient

    from main import app

    client = TestClient(app)
    assert client.get("/api/land-records").status_code == 401


def test_land_listing_groups_documents_by_survey_and_village(make_user_client, insert_land_document):
    admin, headers, _ = make_user_client("ADMIN", prefix="lila")
    survey = _survey()
    insert_land_document(survey=survey, village="Groupville", owner="First Owner", year="2020")
    insert_land_document(survey=survey, village="Groupville", owner="Second Owner", year="2023")

    listed = admin.get("/api/land-records", headers=headers, params={"q": survey})
    assert listed.status_code == 200
    body = listed.json()
    assert body["total"] == 1
    land = body["land_records"][0]
    assert land["survey"] == survey
    assert land["village"] == "Groupville"
    assert land["record_count"] == 2
    assert land["current_owner"] == "Second Owner"  # latest year wins
    assert land["encumbrance_status"] == "NONE"


def test_land_detail_contains_all_required_sections(make_user_client, insert_land_document):
    admin, headers, _ = make_user_client("ADMIN", prefix="lidt")
    survey = _survey()
    insert_land_document(survey=survey, village="Detailville", owner="Sita Devi", year="2022")
    land_id = admin.get("/api/land-records", headers=headers, params={"q": survey}).json()["land_records"][0]["land_id"]

    detail = admin.get(f"/api/land-records/{land_id}", headers=headers)
    assert detail.status_code == 200
    body = detail.json()
    for section in ("property", "current_owner", "ownership_history", "mutations",
                    "encumbrances", "encumbrance_banner", "risk", "documents", "map", "audit"):
        assert section in body, f"missing section {section}"
    assert body["property"]["survey"] == survey
    assert body["current_owner"]["owner"] == "Sita Devi"
    assert body["map"]["url"].startswith("/map")
    assert body["risk"]["verdict"] == "CLEAR"
    # admin sees the real audit trail; ownership history carries the document
    assert isinstance(body["audit"], list)
    assert body["ownership_history"][0]["owner"] == "Sita Devi"


def test_land_detail_audit_is_restricted_for_non_admins(make_user_client, insert_land_document):
    officer, officer_headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="liau")
    survey = _survey()
    insert_land_document(survey=survey, village="Audit Restrict")
    land_id = officer.get("/api/land-records", headers=officer_headers, params={"q": survey}).json()["land_records"][0]["land_id"]
    detail = officer.get(f"/api/land-records/{land_id}", headers=officer_headers)
    assert detail.json()["audit"].get("restricted") is True


def test_viewer_cannot_open_unapproved_land(make_user_client, insert_land_document):
    viewer, viewer_headers, _ = make_user_client("VIEWER", prefix="livw")
    admin, admin_headers, _ = make_user_client("ADMIN", prefix="livwa")
    survey = _survey()
    insert_land_document(survey=survey, village="Approvedville", status="APPROVED")
    draft_survey = _survey()
    insert_land_document(survey=draft_survey, village="Draftville", status="DRAFT")

    # approved land is visible to viewers
    ok = viewer.get("/api/land-records", headers=viewer_headers, params={"q": survey})
    assert ok.status_code == 200 and ok.json()["total"] == 1
    # draft-only land is invisible to viewers (document visibility rules)
    denied = viewer.get("/api/land-records", headers=viewer_headers, params={"q": draft_survey})
    assert denied.json()["total"] == 0


def test_unknown_land_is_404(make_user_client):
    staff, headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="li404")
    assert staff.get("/api/land-records/LR-000000000000", headers=headers).status_code == 404


def test_document_detail_carries_land_context_for_reviewers(make_user_client, insert_land_document):
    officer, officer_headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="licx")
    survey = _survey()
    doc_id = insert_land_document(survey=survey, village="Contextville", owner="Context Owner")

    detail = officer.get(f"/api/documents/{doc_id}", headers=officer_headers)
    assert detail.status_code == 200
    context = detail.json().get("land_context")
    assert context and context["matched"] is True
    assert context["land_id"]
    assert context["risk_verdict"] == "CLEAR"
    assert context["encumbrance_banner"]["text"]


def test_review_approval_returns_land_risk_warning_for_encumbered_land(make_user_client, insert_land_document):
    officer, officer_headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="liwarn")
    survey = _survey()
    doc_id = insert_land_document(survey=survey, village="Warnville", owner="Mahesh Verma", status="PENDING_VERIFICATION")
    officer.post("/api/encumbrances", headers=officer_headers, json={
        "survey_number": survey, "village": "Warnville", "lender": "Example Bank",
        "reference_no": "LN-WARN", "amount": 500000,
    })
    result = officer.post(f"/api/documents/{doc_id}/review-action", headers=officer_headers,
                          json={"action": "approve", "comments": ""})
    assert result.status_code == 200
    warning = result.json().get("land_risk_warning")
    assert warning and warning["code"] == "LAND_RISK_PRESENT"
    assert warning["active_encumbrance_count"] == 1


def test_review_approval_has_no_warning_for_clean_land(make_user_client, insert_land_document):
    officer, officer_headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="liclean")
    survey = _survey()
    doc_id = insert_land_document(survey=survey, village="Cleanville", status="PENDING_VERIFICATION")
    result = officer.post(f"/api/documents/{doc_id}/review-action", headers=officer_headers,
                          json={"action": "approve", "comments": ""})
    assert result.status_code == 200
    assert result.json().get("land_risk_warning") is None


def test_active_encumbrance_reflects_on_land_listing(make_user_client, insert_land_document):
    officer, officer_headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="lienc")
    survey = _survey()
    insert_land_document(survey=survey, village="Banner Ville")
    officer.post("/api/encumbrances", headers=officer_headers, json={
        "survey_number": survey, "village": "Banner Ville", "lender": "Bank",
        "reference_no": "LN-BANNER", "amount": 850000,
    })
    land = officer.get("/api/land-records", headers=officer_headers, params={"q": survey}).json()["land_records"][0]
    assert land["encumbrance_status"] == "ACTIVE"
    assert land["active_encumbrance_count"] == 1
    banner = land["encumbrance_banner"]
    assert banner["tone"] == "danger"
    assert banner["lender"] == "Bank"
    assert banner["reference"] == "LN-BANNER"


def test_ownership_history_includes_completed_mutations(make_user_client, insert_land_document):
    admin, admin_headers, _ = make_user_client("ADMIN", prefix="lioh")
    officer, officer_headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="lioho")
    survey = _survey()
    insert_land_document(survey=survey, village="Historyville", owner="Old Owner", year="2020")
    insert_land_document(survey=survey, village="Historyville", owner="New Owner", year="2023")
    mutation = officer.post("/api/mutations", headers=officer_headers, json={
        "survey_number": survey, "village": "Historyville",
        "previous_owner": "Old Owner", "new_owner": "New Owner", "reason_type": "SALE",
    }).json()["mutation"]
    officer.post(f"/api/mutations/{mutation['id']}/review", headers=officer_headers,
                 json={"action": "MARK_VERIFIED"})
    admin.post(f"/api/mutations/{mutation['id']}/complete", headers=admin_headers,
               json={"notes": "verified"})

    land_id = admin.get("/api/land-records", headers=admin_headers, params={"q": survey}).json()["land_records"][0]["land_id"]
    detail = admin.get(f"/api/land-records/{land_id}", headers=admin_headers).json()
    kinds = [event["kind"] for event in detail["ownership_history"]]
    assert "DOCUMENT" in kinds and "MUTATION" in kinds
    mutation_event = next(event for event in detail["ownership_history"] if event["kind"] == "MUTATION")
    assert mutation_event["owner"] == "New Owner"
    assert mutation_event["previous_owner"] == "Old Owner"


def test_land_risk_endpoint_scopes_to_viewer_role(make_user_client, insert_land_document):
    viewer, viewer_headers, _ = make_user_client("VIEWER", prefix="lirv")
    officer, officer_headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="lirvo")
    survey = _survey()
    insert_land_document(survey=survey, village="Riskville2", status="APPROVED")
    land_id = officer.get("/api/land-records", headers=officer_headers, params={"q": survey}).json()["land_records"][0]["land_id"]
    risk = viewer.get(f"/api/land-records/{land_id}/risk", headers=viewer_headers)
    assert risk.status_code == 200
    assert risk.json()["risk"]["legal_authority"] is False
    # risk review workspace is reviewer-only
    denied = viewer.get("/api/land-records/risk-review", headers=viewer_headers)
    assert denied.status_code == 403


def test_map_show_mode_control_exists_and_defaults_to_selected():
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1] / "map.html").read_text(encoding="utf-8")
    assert 'id="mapShowMode"' in html
    assert '<option value="selected" selected>' in html
    assert '<option value="all">' in html
    js = (Path(__file__).resolve().parents[1] / "map.js").read_text(encoding="utf-8")
    assert "showMode: 'selected'" in js
    assert "'osmde'" in js  # resilient tile mirror fallback
    assert "openstreetmap.de" in js
