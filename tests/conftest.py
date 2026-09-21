import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _restore_server_db_path():
    """Some existing tests reassign server.DB_PATH directly; restore it after
    every test so later tests keep using the canonical database."""
    import server

    saved = (server.DB_PATH, server.SQLITE_PATH)
    yield
    server.DB_PATH, server.SQLITE_PATH = saved



def _bump_role(email: str, role: str) -> None:
    import server

    with server.get_db() as db:
        db.execute("UPDATE users SET role=? WHERE email=?", (role, email))


@pytest.fixture
def make_user_client():
    """Factory creating an authenticated TestClient per role.

    A fresh TestClient per user is required because the canonical session
    cookie takes precedence over the Authorization header."""
    from fastapi.testclient import TestClient

    import server
    from main import app  # canonical entrypoint with every router mounted

    created = []

    def _make(role="VERIFICATION_OFFICER", prefix="li"):
        import uuid

        client = TestClient(app)
        suffix = uuid.uuid4().hex[:10]
        email = f"{prefix}-{suffix}@example.test"
        password = "Strong Land Password 123!"
        response = client.post("/api/auth/signup", json={
            "full_name": f"{prefix.title()} Tester", "email": email, "password": password,
        })
        assert response.status_code == 200, response.text
        _bump_role(email, role)
        login = client.post("/api/auth/login", json={"email": email, "password": password})
        assert login.status_code == 200, login.text
        created.append(email)
        return client, {"Authorization": "Bearer " + login.json()["token"]}, email

    yield _make

    import server as _server

    for email in created:
        try:
            with _server.get_db() as db:
                db.execute("DELETE FROM users WHERE email=?", (email,))
        except Exception:
            pass


@pytest.fixture
def insert_land_document():
    """Insert a synthetic screened document with land fields (auto-cleaned)."""
    import json
    import uuid

    import server

    inserted = []

    def _insert(doc_id=None, *, owner="Ram Singh", survey="452", village="Sundarpur",
                year="2023", area="2.5 ha", status="APPROVED", doc_type="Land Record",
                confidence=0.95, mean_conf=90, uploader="landintel@example.test", lat=None, lon=None):
        doc_id = doc_id or uuid.uuid4().hex[:12]
        fields = {
            "owner_name": {"value": owner, "confidence": confidence},
            "father_name": {"value": "Test Father", "confidence": confidence},
            "survey_number": {"value": survey, "confidence": confidence},
            "khasra_number": {"value": survey, "confidence": confidence},
            "plot_number": {"value": survey, "confidence": confidence},
            "area": {"value": area, "confidence": confidence},
            "village": {"value": village, "confidence": confidence},
            "tehsil": {"value": "Sadar", "confidence": confidence},
            "district": {"value": "Ghaziabad", "confidence": confidence},
            "state": {"value": "Uttar Pradesh", "confidence": confidence},
            "document_date": {"value": f"{year}-06-15", "confidence": confidence},
            "khatauni_year": {"value": year, "confidence": confidence},
        }
        with server.get_db() as db:
            db.execute(
                """INSERT OR REPLACE INTO documents
                (id,filename,doc_type,mean_conf,verdict,status,languages,pages,fields,validation,
                 ai_decision_support,ocr_text,cleaned_ocr_text,detected_language,original_fields,
                 uploaded_by,reviewer_comments,created_at,updated_at,lat,lon)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (doc_id, f"{doc_id}.pdf", doc_type, mean_conf, "review", status, "[]", 1,
                 json.dumps(fields), "{}", "{}", "", "", "eng", json.dumps(fields),
                 uploader, "", float(year), float(year), lat, lon),
            )
        inserted.append(doc_id)
        return doc_id

    yield _insert

    import server as _server

    for doc_id in inserted:
        try:
            with _server.get_db() as db:
                db.execute("DELETE FROM documents WHERE id=?", (doc_id,))
        except Exception:
            pass

