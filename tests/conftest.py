import os

# The initial-administrator bootstrap only runs when this is configured; set
# it before any test module imports so login tests do not depend on import
# order (test_ocr_workflow.py used to be the only place this was set).
os.environ.setdefault("ADMIN_INITIAL_PASSWORD", "Admin@123")
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
                confidence=0.95, mean_conf=90, uploader="landintel@example.test", lat=None, lon=None,
                ocr_text=""):
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
                 json.dumps(fields), "{}", "{}", ocr_text, "", "eng", json.dumps(fields),
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


@pytest.fixture
def insert_court_case():
    """Insert a court case straight into the litigation register (auto-cleaned).

    Uses the register's own schema so tests exercise the same rows the API
    produces. Ids default to a random hex so demo-wipe tests can prove that
    non-DEMO rows survive.
    """
    import json
    import time
    import uuid

    from court_cases import ensure_schema

    ensure_schema()
    inserted = []

    def _insert(survey, village="Litigationpur", *, case_number=None, case_type="CIVIL",
                 court_name="Court of the District Judge", filed_date="2022-01-10",
                 closed_date="", status="ACTIVE", parties="A v. B", relief_sought="",
                 decision_summary="", evidence_doc_ids=(), case_id=None, notes="", created_by="tester@example.test"):
        cid = case_id or uuid.uuid4().hex[:12]
        number = case_number or f"DEMO-TEST-{uuid.uuid4().hex[:8]}"
        now = time.time()
        import server
        with server.get_db() as db:
            db.execute(
                """INSERT OR REPLACE INTO land_court_cases
                   (id, survey_number, khasra_number, village, tehsil, district, case_number, case_type,
                    court_name, filed_date, closed_date, status, parties, relief_sought, decision_summary,
                    evidence_doc_ids, notes, created_by, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (cid, survey, survey, village, "Sadar", "Ghaziabad", number, case_type, court_name,
                 filed_date, closed_date, status, parties, relief_sought, decision_summary,
                 json.dumps(list(evidence_doc_ids)), notes, created_by, now, now),
            )
        inserted.append(cid)
        return cid, number

    yield _insert

    import server
    with server.get_db() as db:
        for cid in inserted:
            db.execute("DELETE FROM land_court_cases WHERE id=?", (cid,))


@pytest.fixture
def query_counter(monkeypatch):
    """Count + classify database queries issued through server.DBConnection.

    Tests seed first, then reset the counters before the request under
    measurement, so setup traffic never pollutes the numbers.
    """
    import server

    state = {"queries": 0, "ddl": 0, "sql": []}
    original = server.DBConnection.execute

    def counting_execute(self, query, params=()):
        statement = " ".join(str(query).split())
        upper = statement.upper()
        if upper.startswith(("CREATE", "ALTER", "DROP")):
            state["ddl"] += 1
        state["queries"] += 1
        state["sql"].append(upper)
        return original(self, query, params)

    monkeypatch.setattr(server.DBConnection, "execute", counting_execute)
    return state
