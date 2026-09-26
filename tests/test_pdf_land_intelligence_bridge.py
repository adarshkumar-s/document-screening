from pathlib import Path

from fastapi.testclient import TestClient

import server
from main import app


client = TestClient(app)


def _make_admin():
    # Use a unique account so the test does not depend on deployment seed credentials.
    email = "pdf-li-test@example.test"
    password = "Strong PDF Land Password 123!"
    with server.get_db() as db:
        db.execute("DELETE FROM users WHERE email=?", (email,))
    signup = client.post("/api/auth/signup", json={
        "full_name": "PDF LI Test", "email": email, "password": password,
    })
    assert signup.status_code == 200
    with server.get_db() as db:
        db.execute("UPDATE users SET role=? WHERE email=?", (server.ROLE_ADMIN, email))
    login = client.post("/api/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200
    return {"Authorization": "Bearer " + login.json()["token"]}


def test_generated_court_pdf_uses_embedded_text_and_extracts_land_identity():
    sample = Path("samples/demo-land-intel/DEMO-LI-COURT-001-court-order.pdf")
    assert sample.is_file(), "The DEMO-LI court PDF must remain in the repository"

    from ocr_land_bridge import extract_pdf_text

    text, pages = extract_pdf_text(sample.read_bytes())
    assert pages >= 1
    assert "DEMO-CS-2025-0142" in text
    assert "Survey No: 131" in text
    assert "Village: Shantiban" in text

    parsed = server.extract_fields_from_ocr(text, sample.name)
    assert parsed["fields"]["survey_number"]["value"] == "131"
    assert parsed["fields"]["village"]["value"] == "Shantiban"


def test_production_entrypoint_installs_pdf_land_bridge():
    assert getattr(server.run_ocr_pipeline, "_land_bridge", False) is True


def test_uploaded_court_pdf_gets_land_context_when_demo_records_are_seeded():
    # This test exercises the actual upload endpoint. The demo seed is intentionally
    # explicit; production startup must never silently create demo data.
    headers = _make_admin()
    seeded = client.post("/api/admin/demo/seed", json={"scenario": "LI"}, headers=headers)
    assert seeded.status_code == 200, seeded.text

    sample = Path("samples/demo-land-intel/DEMO-LI-COURT-001-court-order.pdf")
    response = client.post(
        "/api/process",
        files={"file": (sample.name, sample.read_bytes(), "application/pdf")},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["fields"]["survey_number"]["value"] == "131"
    assert payload["fields"]["village"]["value"] == "Shantiban"

    detail = client.get(f"/api/documents/{payload['id']}", headers=headers)
    assert detail.status_code == 200, detail.text
    context = detail.json().get("land_context") or {}
    assert context.get("matched") is True
    banner = context.get("litigation_banner") or {}
    assert banner.get("text") == "Active litigation found for this property"
    assert banner.get("case_number") == "DEMO-CS-2025-0142"
