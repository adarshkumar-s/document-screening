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
    assert "tile.openstreetmap.fr/hot" in js.text
    assert "coordinatePair" in js.text
    assert "Number(null)" in js.text
    assert "mapRegionStatus" in html.text
    assert "fitRecordsBtn" in js.text
    assert "World_Imagery" in js.text
    assert "World_Street_Map" not in js.text
    assert "basemaps.cartocdn.com" not in js.text
    assert "portfolioMapVillageCache" in js.text
    assert "set exact pin" in js.text.lower()
    assert css.headers["content-type"].split(";", 1)[0] == "text/css"
    assert js.headers["content-type"].split(";", 1)[0] == "application/javascript"
    assert client.get("/land-intelligence").status_code == 200
    assert client.get("/api/demo-land/health").status_code == 404


def test_map_uses_labelled_ocr_corner_fields_for_document_geometry():
    doc_id = "MAP-OCR-GEOMETRY-" + uuid.uuid4().hex[:8]
    _insert_document(doc_id)
    coordinates = [
        "23.17642 N, 80.01231 E", "23.17642 N, 80.01331 E",
        "23.17742 N, 80.01331 E", "23.17742 N, 80.01231 E",
    ]
    with server.get_db() as db:
        row = db.execute("SELECT fields FROM documents WHERE id=?", (doc_id,)).fetchone()
        fields = json.loads(row["fields"])
        fields.update({f"coordinate_{index + 1}": {"value": value, "confidence": 0.8}
                       for index, value in enumerate(coordinates)})
        db.execute("UPDATE documents SET fields=? WHERE id=?", (json.dumps(fields), doc_id))
    headers = _make_user("VERIFICATION_OFFICER")
    record = next(item for item in client.get("/api/map/records", headers=headers).json()["records"] if item["id"] == doc_id)
    assert record["geometry"]["type"] == "Polygon"
    assert record["geometry_source"] == "OCR-extracted printed coordinates"
    assert record["geometry"]["coordinates"][0][0] == [80.01231, 23.17642]


def test_reviewer_can_save_mapping_only_digitized_boundary():
    doc_id = "MAP-BOUNDARY-" + uuid.uuid4().hex[:8]
    _insert_document(doc_id)
    headers = _make_user("VERIFICATION_OFFICER")
    response = client.put(
        f"/api/map/records/{doc_id}/boundary",
        headers=headers,
        json={"points": [[23.1, 80.1], [23.1, 80.2], [23.2, 80.2]], "reason": "Checked against source sheet"},
    )
    assert response.status_code == 200
    assert response.json()["geometry"]["type"] == "Polygon"
    with server.get_db() as db:
        row = db.execute("SELECT map_geometry, map_geometry_source FROM documents WHERE id=?", (doc_id,)).fetchone()
    assert json.loads(row["map_geometry"])["coordinates"][0][0] == [80.1, 23.1]
    assert row["map_geometry_source"] == "Officer-digitized boundary"
    record = next(item for item in client.get("/api/map/records", headers=headers).json()["records"] if item["id"] == doc_id)
    assert record["geometry_source"] == "Officer-digitized boundary"


def test_reviewer_boundary_requires_three_distinct_corners():
    doc_id = "MAP-BOUNDARY-BAD-" + uuid.uuid4().hex[:8]
    _insert_document(doc_id)
    headers = _make_user("VERIFICATION_OFFICER")
    response = client.put(
        f"/api/map/records/{doc_id}/boundary", headers=headers,
        json={"points": [[23.1, 80.1], [23.1, 80.1], [23.1, 80.1]]},
    )
    assert response.status_code == 400


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


def test_location_states_provenance_and_real_coverage_metrics():
    village_id = "MAP-STATE-VILLAGE-" + uuid.uuid4().hex[:8]
    unresolved_id = "MAP-STATE-NONE-" + uuid.uuid4().hex[:8]
    exact_id = "MAP-STATE-EXACT-" + uuid.uuid4().hex[:8]
    _insert_document(village_id, village="Sundarpur State Test", lat=None, lon=None)
    _insert_document(unresolved_id, village="", lat=None, lon=None)
    _insert_document(exact_id, village="Sundarpur State Test", lat=28.62, lon=77.10)
    headers = _make_user("VERIFICATION_OFFICER")
    payload = client.get("/api/map/records", headers=headers).json()
    records = {record["id"]: record for record in payload["records"]}
    assert records[village_id]["location_status"] == "VILLAGE_LEVEL"
    assert records[village_id]["location_label"] == "APPROXIMATE — VILLAGE LOCATION"
    assert records[village_id]["lat"] is None and records[village_id]["lon"] is None
    assert records[village_id]["location_confidence"] is None
    assert records[unresolved_id]["location_status"] == "UNRESOLVED"
    assert records[unresolved_id]["location_label"] == "LOCATION NOT AVAILABLE"
    assert records[exact_id]["location_label"] == "VERIFIED LOCATION"
    assert records[exact_id]["location_confidence"] is None
    summary = payload["metadata"]["summary"]
    assert summary["records"] >= 3
    assert summary["mapped_records"] >= 1
    assert 0 < summary["mapped_percent"] <= 100

    updated = client.put(
        f"/api/map/records/{village_id}/location", headers=headers,
        json={"lat": 28.6212345, "lon": 77.1012345, "reason": "Compared with signed location note"},
    )
    assert updated.status_code == 200
    refreshed = client.get("/api/map/records", headers=headers).json()
    record = next(item for item in refreshed["records"] if item["id"] == village_id)
    assert record["location_label"] == "VERIFIED LOCATION"
    assert record["location_verified_by"] == "Map Reviewer"
    assert record["location_verified_at"]
    assert record["location_audit_available"] is True
    assert record["location_confidence"] is None


def test_reference_geometry_is_explicitly_non_authoritative():
    headers = _make_user("VERIFICATION_OFFICER")
    response = client.get("/api/map/properties?village=Demo%20Village", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["metadata"]["authoritative"] is False
    assert "not an authoritative cadastral boundary" in data["metadata"]["disclaimer"]
    assert data["properties"]
    assert all(item["authoritative"] is False for item in data["properties"])
    assert all(item["synthetic"] is True for item in data["properties"])


def test_mapping_does_not_geocode_every_village_on_initial_load():
    js = client.get("/map.js").text
    assert "geocodeVillages();" not in js
    assert "No exact parcel coordinate is being created" in js
    assert "portfolioMapVillageCache" in js
    html = client.get("/map").text
    assert "Map tiles unavailable" in html
    assert "offline schematic" in html.lower()
    assert "Open document" in js
    assert "openHistory" in js


def test_history_exposes_location_state_without_replacing_existing_fields():
    doc_id = "MAP-HISTORY-LOCATION-" + uuid.uuid4().hex[:8]
    _insert_document(doc_id, village="History Location Village")
    headers = _make_user("VERIFICATION_OFFICER")
    client.put(f"/api/map/records/{doc_id}/location", headers=headers, json={"lat": 28.63, "lon": 77.11})
    response = client.get(f"/api/documents/{doc_id}/history", headers=headers)
    assert response.status_code == 200
    current = next(item for item in response.json()["items"] if item["id"] == doc_id)
    assert current["location_status"] == "EXACT_PIN"
    assert current["location_label"] == "VERIFIED LOCATION"
    assert current["location_verified_by"] == "Map Reviewer"


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


def test_boundary_rejects_self_intersection_and_tracks_accuracy():
    doc_id = "MAP-BOUNDARY-SELF-" + uuid.uuid4().hex[:8]
    _insert_document(doc_id)
    headers = _make_user("VERIFICATION_OFFICER")
    crossing = client.put(
        f"/api/map/records/{doc_id}/boundary", headers=headers,
        json={"points": [[23.1,80.1],[23.2,80.2],[23.1,80.2],[23.2,80.1]]},
    )
    assert crossing.status_code == 400
    saved = client.put(
        f"/api/map/records/{doc_id}/location", headers=headers,
        json={"lat": 28.621, "lon": 77.101, "accuracy_m": 8.5},
    )
    assert saved.status_code == 200
    assert saved.json()["accuracy_m"] == 8.5
    record = next(item for item in client.get("/api/map/records", headers=headers).json()["records"] if item["id"] == doc_id)
    assert record["location_accuracy_m"] == 8.5


def test_map_viewport_filters_exact_pins():
    doc_id = "MAP-BBOX-" + uuid.uuid4().hex[:8]
    _insert_document(doc_id, lat=28.62, lon=77.10)
    headers = _make_user("VERIFICATION_OFFICER")
    inside = client.get("/api/map/records?min_lat=28.6&min_lon=77.0&max_lat=28.7&max_lon=77.2", headers=headers)
    assert inside.status_code == 200
    assert any(item["id"] == doc_id for item in inside.json()["records"])
    outside = client.get("/api/map/records?min_lat=30&min_lon=80&max_lat=31&max_lon=81", headers=headers)
    assert outside.status_code == 200
    assert all(item["id"] != doc_id for item in outside.json()["records"])


def test_spatial_conflict_endpoint_flags_duplicate_exact_locations():
    _insert_document("MAP-CONFLICT-A", lat=28.64, lon=77.12)
    _insert_document("MAP-CONFLICT-B", lat=28.64, lon=77.12)
    headers = _make_user("VERIFICATION_OFFICER")
    response = client.get("/api/map/conflicts", headers=headers)
    assert response.status_code == 200
    assert any(set(item["record_ids"]) == {"MAP-CONFLICT-A", "MAP-CONFLICT-B"} for item in response.json()["conflicts"])



def test_polygon_hole_is_not_treated_as_parcel_interior():
    polygon = {
        "type": "Polygon",
        "coordinates": [
            [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
            [[3, 3], [7, 3], [7, 7], [3, 7], [3, 3]],
        ],
    }
    assert mapping._point_in_geometry(1, 1, polygon) is True
    assert mapping._point_in_geometry(5, 5, polygon) is False
    assert mapping._point_in_geometry(12, 5, polygon) is False


def test_multipolygon_hole_and_disjoint_component_handling():
    geometry = {
        "type": "MultiPolygon",
        "coordinates": [
            [
                [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]],
                [[1, 1], [3, 1], [3, 3], [1, 3], [1, 1]],
            ],
            [[[10, 10], [12, 10], [12, 12], [10, 12], [10, 10]]],
        ],
    }
    assert mapping._point_in_geometry(0.5, 0.5, geometry) is True
    assert mapping._point_in_geometry(2, 2, geometry) is False
    assert mapping._point_in_geometry(11, 11, geometry) is True
    assert mapping._point_in_geometry(6, 6, geometry) is False


def test_geometry_area_subtracts_holes_and_sums_multipolygon_parts():
    polygon = {
        "type": "Polygon",
        "coordinates": [
            [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
            [[3, 3], [7, 3], [7, 7], [3, 7], [3, 3]],
        ],
    }
    outer = mapping._boundary_area_m2(polygon["coordinates"][0])
    hole = mapping._boundary_area_m2(polygon["coordinates"][1])
    area = mapping._geometry_area_m2(polygon)
    assert area == round(outer - hole, 2)
    multi = {"type": "MultiPolygon", "coordinates": [
        [polygon["coordinates"][0], polygon["coordinates"][1]],
        [[[20, 20], [22, 20], [22, 22], [20, 22], [20, 20]]],
    ]}
    expected = round(
        outer - hole + mapping._boundary_area_m2(multi["coordinates"][1][0]), 2
    )
    assert mapping._geometry_area_m2(multi) == expected


def test_reference_comparison_labels_bbox_overlap_as_screening_only():
    result = mapping._reference_comparison({
        "geometry": None, "reference_geometry": None, "area": None,
        "north_boundary": None, "south_boundary": None,
        "east_boundary": None, "west_boundary": None,
        "boundary_completeness": 0,
    })
    assert result["bbox_overlap_method"] == "axis_aligned_bbox_screening_only"
    assert result["screening_only"] is True
