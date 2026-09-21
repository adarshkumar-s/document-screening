"""Phase 14 — LAND RECORD VERIFICATION REPORT tests: generation, required
fields, QR, RBAC, and the non-authoritative report claim."""
import uuid


def _survey():
    return f"4{uuid.uuid4().hex[:5]}"


def test_report_generation_contains_all_required_fields(make_user_client, insert_land_document):
    admin, admin_headers, _ = make_user_client("ADMIN", prefix="rpta")
    survey = _survey()
    doc_a = insert_land_document(survey=survey, village="Reportpur", owner="Sita Devi", year="2021")
    doc_b = insert_land_document(survey=survey, village="Reportpur", owner="Sita Devi", year="2023")

    created = admin.post("/api/reports/land-verification", headers=admin_headers,
                         json={"land_id": "", "document_id": doc_b})
    # empty land_id must not 500 — expect 404 (land unknown) or 422
    assert created.status_code in {404, 422}

    listed = admin.get("/api/land-records", headers=admin_headers, params={"q": survey})
    land_id = listed.json()["land_records"][0]["land_id"]

    generated = admin.post("/api/reports/land-verification", headers=admin_headers,
                           json={"land_id": land_id, "document_id": doc_b})
    assert generated.status_code == 200, generated.text
    report = generated.json()["report"]
    reference = generated.json()["reference_no"]
    assert reference.startswith("LVR-")

    for key in ("document_id", "land_record_id", "owner", "survey", "area", "village", "tehsil",
                "district", "verification_status", "risk_status", "encumbrance_status",
                "mutation_status", "supporting_documents", "generated_at", "reviewer", "reference_no"):
        assert key in report, f"missing report field: {key}"
    assert report["document_id"] == doc_b
    assert report["owner"] == "Sita Devi"
    assert {doc["id"] for doc in report["supporting_documents"]} >= {doc_a, doc_b}


def test_report_html_contains_qr_and_disclaimer(make_user_client, insert_land_document):
    admin, admin_headers, _ = make_user_client("ADMIN", prefix="rptb")
    survey = _survey()
    insert_land_document(survey=survey, village="Qrville")
    listed = admin.get("/api/land-records", headers=admin_headers, params={"q": survey})
    land_id = listed.json()["land_records"][0]["land_id"]
    reference = admin.post("/api/reports/land-verification", headers=admin_headers,
                           json={"land_id": land_id}).json()["reference_no"]

    html = admin.get(f"/api/reports/land-verification/{reference}", headers=admin_headers)
    assert html.status_code == 200
    body = html.text
    assert "LAND RECORD VERIFICATION REPORT" in body
    assert "data:image/png;base64," in body  # machine-readable verification QR
    assert "NOT an official government land title certificate" in body
    assert reference in body

    json_view = admin.get(f"/api/reports/land-verification/{reference}",
                          headers=admin_headers, params={"format": "json"})
    assert json_view.status_code == 200
    assert json_view.json()["report"]["reference_no"] == reference


def test_report_qr_endpoint_serves_png(make_user_client, insert_land_document):
    admin, admin_headers, _ = make_user_client("ADMIN", prefix="rptc")
    survey = _survey()
    insert_land_document(survey=survey, village="Pngville")
    land_id = admin.get("/api/land-records", headers=admin_headers, params={"q": survey}).json()["land_records"][0]["land_id"]
    reference = admin.post("/api/reports/land-verification", headers=admin_headers,
                           json={"land_id": land_id}).json()["reference_no"]
    qr = admin.get(f"/api/reports/land-verification/{reference}/qr.png", headers=admin_headers)
    assert qr.status_code == 200
    assert qr.headers["content-type"] == "image/png"
    assert qr.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_report_generation_is_reviewer_only_and_viewing_is_staff(make_user_client, insert_land_document):
    viewer, viewer_headers, _ = make_user_client("VIEWER", prefix="rptd")
    data_officer, do_headers, _ = make_user_client("DATA_OFFICER", prefix="rpte")
    admin, admin_headers, _ = make_user_client("ADMIN", prefix="rptf")
    survey = _survey()
    insert_land_document(survey=survey, village="Rbacville", status="APPROVED")
    land_id = admin.get("/api/land-records", headers=admin_headers, params={"q": survey}).json()["land_records"][0]["land_id"]

    denied_viewer = viewer.post("/api/reports/land-verification", headers=viewer_headers,
                                json={"land_id": land_id})
    assert denied_viewer.status_code == 403
    denied_officer = data_officer.post("/api/reports/land-verification", headers=do_headers,
                                       json={"land_id": land_id})
    assert denied_officer.status_code == 403

    reference = admin.post("/api/reports/land-verification", headers=admin_headers,
                           json={"land_id": land_id}).json()["reference_no"]
    # only reviewers (verifier/admin) may view reports — they carry owner data
    viewed = data_officer.get(f"/api/reports/land-verification/{reference}", headers=do_headers)
    assert viewed.status_code == 403
    from fastapi.testclient import TestClient

    from main import app

    anonymous = TestClient(app)
    assert anonymous.get(f"/api/reports/land-verification/{reference}").status_code == 401


def test_report_references_are_audit_logged(make_user_client, insert_land_document):
    admin, admin_headers, _ = make_user_client("ADMIN", prefix="rptg")
    survey = _survey()
    insert_land_document(survey=survey, village="Auditville")
    land_id = admin.get("/api/land-records", headers=admin_headers, params={"q": survey}).json()["land_records"][0]["land_id"]
    reference = admin.post("/api/reports/land-verification", headers=admin_headers,
                           json={"land_id": land_id}).json()["reference_no"]
    admin.get(f"/api/reports/land-verification/{reference}", headers=admin_headers)
    audit = admin.get("/api/audit", headers=admin_headers)
    entries = audit.json().get("audit", audit.json().get("logs", []))
    actions = [row["action"] for row in entries]
    assert "REPORT_GENERATED" in actions
    assert "REPORT_VIEWED" in actions
    assert any(reference in row["detail"] for row in entries)


def test_unknown_report_reference_is_404(make_user_client):
    staff, staff_headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="rpth")
    assert staff.get("/api/reports/land-verification/LVR-2026-999999", headers=staff_headers).status_code == 404
