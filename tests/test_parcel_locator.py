"""Exercise mounted routes, not just unmounted locator helpers."""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

import mapping
import parcel_locator
import server
from main import app


@pytest.fixture
def parcels():
    mapping.ensure_schema()
    ids = []

    def create(survey="12", village=None, tehsil="Sadar", doc_id=None):
        pid = "TEST-PARCEL-" + uuid.uuid4().hex
        ids.append(pid)
        geometry = {"type": "Polygon", "coordinates": [[[77, 28], [77.01, 28],
                    [77.01, 28.01], [77, 28.01], [77, 28]]]}
        with server.get_db() as db:
            db.execute("""INSERT INTO properties
                (property_id, parcel_id, survey_number, village, taluka, district, geometry)
                VALUES (?,?,?,?,?,?,?)""",
                       (pid, pid, survey, village or pid, tehsil, "Test District", json.dumps(geometry)))
            if doc_id:
                db.execute("INSERT INTO property_documents(property_id,document_id,linked_at) VALUES (?,?,?)",
                           (pid, doc_id, 1))
        return pid

    yield create
    with server.get_db() as db:
        for pid in ids:
            db.execute("DELETE FROM property_documents WHERE property_id=?", (pid,))
            db.execute("DELETE FROM properties WHERE property_id=?", (pid,))


def test_secure_map_router_is_mounted_first(make_user_client, monkeypatch):
    client, headers, _ = make_user_client()
    monkeypatch.setattr(parcel_locator, "_visible_properties", lambda user: [])
    data = client.get("/api/map/properties", headers=headers).json()
    assert data["metadata"]["authorized_only"] is True
    assert data["properties"] == []


def test_map_properties_respect_document_visibility(make_user_client, insert_land_document, parcels):
    officer, headers, email = make_user_client("DATA_OFFICER")
    village = "Map visibility " + uuid.uuid4().hex
    own_doc = insert_land_document(status="DRAFT", uploader=email)
    hidden_doc = insert_land_document(status="DRAFT", uploader="someone-else@example.test")
    public_doc = insert_land_document(status="APPROVED", uploader="someone-else@example.test")
    own = parcels(village=village, doc_id=own_doc)
    hidden = parcels(village=village, doc_id=hidden_doc)
    public = parcels(village=village, doc_id=public_doc)
    unlinked = parcels(village=village)
    result = officer.get("/api/map/properties", params={"village": village}, headers=headers).json()
    assert result["metadata"]["authorized_only"] is True
    assert {p["property_id"] for p in result["properties"]} == {own}
    for pid in (hidden, public, unlinked):
        assert officer.post("/api/parcels/resolve", headers=headers, json={"parcel_id": pid}).status_code == 404
    viewer, vh, _ = make_user_client("VIEWER")
    visible = viewer.get("/api/map/properties", params={"village": village}, headers=vh).json()
    assert {p["property_id"] for p in visible["properties"]} == {public}


def test_map_properties_filter_tehsil(make_user_client, parcels):
    reviewer, headers, _ = make_user_client()
    village = "Same village " + uuid.uuid4().hex
    expected = parcels(village=village, tehsil="North")
    parcels(village=village, tehsil="South")
    data = reviewer.get("/api/map/properties", headers=headers,
                        params={"village": village, "tehsil": "north"}).json()
    assert [p["property_id"] for p in data["properties"]] == [expected]


def test_resolver_requires_exact_identity_and_reports_ambiguity(make_user_client, parcels):
    reviewer, headers, _ = make_user_client()
    village = "Resolve village " + uuid.uuid4().hex
    pid = parcels(survey="123", village=village)
    endpoint = "/api/parcels/resolve"
    assert reviewer.post(endpoint, headers=headers, json={}).status_code == 400
    assert reviewer.post(endpoint, headers=headers, json={"village": village, "survey": "12"}).status_code == 404
    data = reviewer.post(endpoint, headers=headers, json={"village": village, "survey": "१२३"}).json()
    assert data["parcel"]["property_id"] == pid
    assert data["persisted"] is False
    parcels(survey="123", village=village)
    data = reviewer.post(endpoint, headers=headers, json={"village": village, "survey": "123"}).json()
    assert data["status"] == "AMBIGUOUS"


def test_records_keep_reference_geometry_separate_from_exact_pins(make_user_client, insert_land_document, parcels):
    reviewer, headers, _ = make_user_client()
    doc_id = insert_land_document()
    pid = parcels(doc_id=doc_id)
    response = reviewer.get("/api/map/records", params={"q": doc_id}, headers=headers)
    record = next(r for r in response.json()["records"] if r["id"] == doc_id)
    assert record["reference_geometry"]["type"] == "Polygon"
    assert record["reference_property"]["property_id"] == pid
    assert record["lat"] is None and record["lon"] is None
    assert record["location_status"] != "EXACT_PIN"


def test_every_map_script_is_served():
    import re
    client = TestClient(app)
    html = client.get("/map").text
    for src in re.findall(r'<script src="([^"]+)"', html):
        response = client.get(src)
        assert response.status_code == 200, src
        assert "javascript" in response.headers["content-type"], src
