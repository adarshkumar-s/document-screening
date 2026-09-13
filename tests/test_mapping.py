import json
import uuid
from fastapi.testclient import TestClient

import mapping
import server
from main import app

client = TestClient(app)


def _make_user(role="VERIFICATION_OFFICER"):
    suffix = uuid.uuid4().hex[:10]
    email = f"map-{suffix}@example.test"
    password = "Strong Map Password 123!"
    signup = client.post("/api/auth/signup", json={
        "full_name": "Map Reviewer", "email": email, "password": password,
    })
    assert signup.status_code == 200
    with server.get_db() as db:
        db.execute("UPDATE users SET role=? WHERE email=?", (role, email))
    login = client.post("/api/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200
    return {"Authorization": "Bearer " + login.json()["token"]}


def _insert_document(doc_id, *, owner="Ram Singh", survey="452", village="Sundarpur", year="2023", lat=None, lon=None, status="PENDING_VERIFICATION"):
    fields = {
        "owner_name": {"value": owner, "confidence": 0.95},
        "survey_number": {"value": survey, "confidence": 0.95},
        "khasra_number": {"value": "77", "confidence": 0.9},
        "plot_number": {"value": survey, "confidence": 0.9},
        "area": {"value": "2.5 ha", "confidence": 0.9},
        "village": {"value": village, "confidence": 0.95},
        "tehsil": {"value": "Sadar", "confidence": 0.9},
        "district": {"value": "Ghaziabad", "confidence": 0.9},
        "state": {"value": "Uttar Pradesh", "confidence": 0.9},
        "document_date": {"value": year, "confidence": 0.9},
    }
    with server.get_db() as db:
        db.execute(
            """INSERT OR REPLACE INTO documents
            (id,filename,doc_type,mean_conf,verdict,status,languages,pages,fields,validation,
             ai_decision_support,ocr_text,cleaned_ocr_text,detected_language,original_fields,
             uploaded_by,created_at,updated_at,lat,lon)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (doc_id, f"{doc_id}.pdf", "Land Record", 90, "review", status, "[]", 1,
             json.dumps(fields), "{}", "{}", "", "", "eng", json.dumps(fields),
             "map-reviewer@example.test", float(year), float(year), lat, lon),
        )


def test_map_assets_are_explicit_and_portal_navigation_is_visible():
    portal = client.get("/")
    assert portal.status_code == 200
    assert 'href="/map"' in portal.text
    assert "Land Records Map" in portal.text

    html = client.get("/map")
    css = client.get("/map.css")
    js = client.get("/map.js")
    assert html.status_code == css.status_code == js.status_code == 200
    assert "Land Records Map" in html.text
    assert "/static/vendor/leaflet/leaflet.css" in html.text
    assert "Offline schematic" in html.text
    assert "portfolioMapVillageCache" in js.text
    assert "set exact pin" in js.text.lower()
    assert css.headers["content-type"].split(";", 1)[0] == "text/css"
    assert js.headers["content-type"].split(";", 1)[0] == "application/javascript"
    assert client.get("/land-intelligence").status_code == 404
    assert client.get("/api/demo-land/health").status_code == 404


def test_map_records_are_document_grounded_and_role_filtered():
    _insert_document("MAP-RECORD-1", lat=28.62, lon=77.10)
    headers = _make_user("VERIFICATION_OFFICER")
    response = client.get("/api/map/records", headers=headers)
    assert response.status_code == 200
    record = next(item for item in response.json()["records"] if item["id"] == "MAP-RECORD-1")
    assert record["lat"] == 28.62
    assert record["lon"] == 77.10
    assert record["survey"] == "452"
    assert record["village"] == "Sundarpur"
    assert record["location_status"] == "EXACT_PIN"
    assert response.json()["metadata"]["summary"]["exact_pins"] >= 1


def test_exact_document_pin_is_rbac_protected_and_audited():
    _insert_document("MAP-PIN-1")
    officer = _make_user("DATA_OFFICER")
    forbidden = client.put("/api/map/records/MAP-PIN-1/location", headers=officer, json={"lat": 28.621, "lon": 77.101})
    assert forbidden.status_code == 403

    verifier = _make_user("VERIFICATION_OFFICER")
    updated = client.put("/api/map/records/MAP-PIN-1/location", headers=verifier, json={"lat": 28.621, "lon": 77.101})
    assert updated.status_code == 200
    assert updated.json()["lat"] == 28.621
    cleared = client.put("/api/map/records/MAP-PIN-1/location", headers=verifier, json={"lat": None, "lon": None})
    assert cleared.status_code == 200
    assert cleared.json()["lat"] is None and cleared.json()["lon"] is None
    with server.get_db() as db:
        actions = [row["action"] for row in db.execute("SELECT action FROM audit WHERE doc_id=? ORDER BY id", ("MAP-PIN-1",)).fetchall()]
    assert "location_set" in actions and "location_cleared" in actions


def test_history_includes_current_record_and_transfer_aware_reasoning():
    _insert_document("MAP-HISTORY-2019", owner="Ram Singh", year="2019")
    _insert_document("MAP-HISTORY-2023", owner="Kamla Devi", year="2023")
    headers = _make_user("VERIFICATION_OFFICER")
    response = client.get("/api/documents/MAP-HISTORY-2023/history", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["including_current"] is True
    assert {item["id"] for item in data["items"]} >= {"MAP-HISTORY-2019", "MAP-HISTORY-2023"}
    assert "ownership_history" in data
    assert "findings" in data["ownership_history"]
    assert data["history_summary"]["owners"] == ["Ram Singh", "Kamla Devi"]


def test_map_summary_and_export_are_authenticated_and_role_scoped():
    _insert_document("MAP-EXPORT-1", lat=28.63, lon=77.11, status="APPROVED")
    headers = _make_user("VERIFICATION_OFFICER")
    summary = client.get("/api/map/summary", headers=headers)
    assert summary.status_code == 200
    assert summary.json()["summary"]["records"] >= 1
    export = client.get("/api/map/export.csv", headers=headers)
    assert export.status_code == 200
    assert "land-map-register.csv" in export.headers.get("content-disposition", "")
    assert "MAP-EXPORT-1" in export.text
    assert "location_status" in export.text.splitlines()[0]


def test_geocode_is_cached_without_fabricating_unresolved_coordinates(monkeypatch):
    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b'[{"lat":"28.6205","lon":"77.1065","display_name":"Sundarpur, India"}]'

    calls = []
    monkeypatch.setattr(mapping.urllib.request, "urlopen", lambda request, timeout=10: (calls.append(request.full_url) or FakeResponse()))
    monkeypatch.setattr(mapping.time, "sleep", lambda seconds: None)
    mapping._map_last_geocode_request = 0
    headers = _make_user("VERIFICATION_OFFICER")
    query = f"Sundarpur Cache Test {uuid.uuid4().hex}, Sadar, Ghaziabad"
    first = client.post("/api/map/geocode", headers=headers, json={"query": query})
    assert first.status_code == 200
    assert first.json()["lat"] == 28.6205
    second = client.post("/api/map/geocode", headers=headers, json={"query": query})
    assert second.status_code == 200
    assert second.json()["cached"] is True
    assert len(calls) == 1


def test_mapping_compatibility_helpers_remain_available_to_ai_governance():
    mapping._ensure_tables()
    with server.get_db() as db:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(properties)").fetchall()}
    assert {"location_status", "location_base_latitude", "location_base_longitude"} <= columns
    assert callable(mapping.analyze_ownership_history)
    assert callable(mapping._resolve)
