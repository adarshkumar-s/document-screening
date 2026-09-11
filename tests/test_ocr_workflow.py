import io
import json
import os

from fastapi.testclient import TestClient
from PIL import Image

os.environ.setdefault("ADMIN_INITIAL_PASSWORD", "Admin@123")
os.environ.setdefault("APP_ENV", "test")
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

    
def test_password_hashing_uses_argon2id():
    import hashlib
    hashed = server.hash_password("Correct Horse Battery Staple")
    assert hashed.startswith("$argon2id$")
    assert server.verify_password(hashed, "Correct Horse Battery Staple")[0] is True
    assert server.verify_password(hashed, "wrong")[0] is False
    assert hashed != hashlib.sha256(b"Correct Horse Battery Staple").hexdigest()


def test_signup_cannot_self_assign_elevated_role(tmp_path):
    server.DB_PATH = str(tmp_path / "roles.db")
    server.init_db()
    client = TestClient(server.app)
    r = client.post("/api/auth/signup", json={
        "full_name":"Normal User","email":"normal@example.test",
        "password":"Strong Test Password 123!","role":"ADMIN"
    })
    assert r.status_code == 200
    assert r.json()["user"]["role"] == server.ROLE_DATA_OFFICER
    with server.get_db() as db:
        row = db.execute("SELECT role FROM users WHERE email=?", ("normal@example.test",)).fetchone()
        assert row["role"] == server.ROLE_DATA_OFFICER


def test_role_change_requires_admin_and_admin_cannot_change_self(tmp_path):
    server.DB_PATH = str(tmp_path / "role-change.db")
    server.init_db()
    client = TestClient(server.app)
    signup = client.post("/api/auth/signup", json={
        "full_name":"Normal User","email":"normal2@example.test",
        "password":"Strong Test Password 123!"
    })
    user_token = signup.json()["token"]
    assert client.put(
        "/api/users/whatever/role",
        headers={"Authorization":f"Bearer {user_token}"},
        json={"role":"ADMIN"}
    ).status_code == 403
    admin = login(client)
    with server.get_db() as db:
        admin_row = db.execute("SELECT id FROM users WHERE email=?", ("admin@landrec.gov.in",)).fetchone()
        target = db.execute("SELECT id FROM users WHERE email=?", ("normal2@example.test",)).fetchone()
    assert client.put(
        f"/api/users/{admin_row['id']}/role",
        headers=admin,
        json={"role":"ADMIN"}
    ).status_code == 400
    ok = client.put(
        f"/api/users/{target['id']}/role",
        headers=admin,
        json={"role":"VERIFICATION_OFFICER"}
    )
    assert ok.status_code == 200
    assert ok.json()["user"]["role"] == server.ROLE_VERIFICATION_OFFICER


def test_query_string_jwt_is_rejected(tmp_path):
    server.DB_PATH = str(tmp_path / "query-token.db")
    server.init_db()
    client = TestClient(server.app)
    admin = login(client)
    token = admin["Authorization"].split(" ", 1)[1]
    assert client.get("/api/auth/me", params={"token": token}).status_code == 401
    assert client.get("/api/auth/me", headers=admin).status_code == 200


def test_legacy_sha256_password_is_rehashed_on_success(tmp_path):
    import hashlib
    import uuid
    server.DB_PATH = str(tmp_path / "legacy.db")
    server.init_db()
    email = "legacy@example.test"
    password = "Legacy Password 123!"
    legacy_hash = hashlib.sha256(password.encode()).hexdigest()
    with server.get_db() as db:
        db.execute(
            "INSERT INTO users (id,full_name,email,password_hash,role,version,is_active) VALUES (?,?,?,?,?,?,?)",
            (uuid.uuid4().hex[:12],"Legacy",email,legacy_hash,server.ROLE_DATA_OFFICER,0,1)
        )
    client = TestClient(server.app)
    r = client.post("/api/auth/login", json={"email":email,"password":password})
    assert r.status_code == 200
    with server.get_db() as db:
        stored = db.execute("SELECT password_hash FROM users WHERE email=?", (email,)).fetchone()["password_hash"]
    assert stored.startswith("$argon2id$")
    assert stored != legacy_hash


def test_production_docs_are_disabled_when_configured():
    if server.IS_PRODUCTION:
        assert server.app.docs_url is None
        assert server.app.redoc_url is None
        assert server.app.openapi_url is None
    else:
        assert server.app.docs_url == "/docs"
