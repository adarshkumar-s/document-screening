import io
import json
import os

from fastapi.testclient import TestClient
from PIL import Image

os.environ.setdefault("ADMIN_INITIAL_PASSWORD", "Admin@123")
import server


def login(client):
    response = client.post("/api/auth/login", json={"email": "admin@landrec.gov.in", "password": "Admin@123"})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}


def test_filename_is_never_an_owner_name():
    result = server.extract_fields_from_ocr("Village: Greenfield\nDistrict: Pune", "Alice_Smith_land_record.png")
    assert result["fields"]["owner_name"]["value"] == ""
    assert any("Record-holder" in issue["msg"] for issue in result["validation"]["issues"])


def test_english_fields_are_extracted_from_ocr_text():
    text = "Owner Name: Alice Sharma\nFather's Name: Mohan Sharma\nVillage: Greenfield\nDistrict: Pune\nKhasra No: 45/2\nArea: 1.25 Acres\nDate: 01/09/2026"
    fields = server.extract_fields_from_ocr(text, "unrelated-name.jpeg")["fields"]
    assert fields["owner_name"]["value"] == "Alice Sharma"
    assert fields["father_name"]["value"] == "Mohan Sharma"
    assert fields["khasra_number"]["value"] == "45/2"
    assert fields["village"]["value"] == "Greenfield"
    assert fields["document_date"]["value"] == "01/09/2026"


def test_api_persists_and_retrieves_each_scan_without_stale_owner(monkeypatch, tmp_path):
    server.DB_PATH = str(tmp_path / "records.db")
    server.init_db()
    client = TestClient(server.app)
    headers = login(client)
    owners = iter(["Asha Verma", "Bharat Singh"])

    def fake_pipeline(_content, filename, *args):
        owner = next(owners)
        return server.extract_fields_from_ocr(f"Owner Name: {owner}\nVillage: Testville", filename)

    monkeypatch.setattr(server, "run_ocr_pipeline", fake_pipeline)
    image = io.BytesIO(); Image.new("RGB", (20, 20), "white").save(image, "PNG")
    first = client.post("/api/process", headers=headers, files={"file": ("first.png", image.getvalue(), "image/png")})
    second = client.post("/api/process", headers=headers, files={"file": ("second.png", image.getvalue(), "image/png")})
    assert first.status_code == second.status_code == 200
    assert first.json()["fields"]["owner_name"]["value"] == "Asha Verma"
    assert second.json()["fields"]["owner_name"]["value"] == "Bharat Singh"
    retrieved = client.get(f"/api/documents/{second.json()['id']}", headers=headers)
    assert retrieved.status_code == 200
    assert retrieved.json()["fields"]["owner_name"]["value"] == "Bharat Singh"


def test_upload_rejects_empty_and_unsupported_files(tmp_path):
    server.DB_PATH = str(tmp_path / "validation.db")
    server.init_db()
    client = TestClient(server.app)
    headers = login(client)
    assert client.post("/api/process", headers=headers, files={"file": ("bad.exe", b"x")}).status_code == 415
    assert client.post("/api/process", headers=headers, files={"file": ("empty.png", b"")}).status_code == 422
