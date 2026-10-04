"""Regression tests for audited mapping defects (see MAPPING_AUDIT.md).

Each test reproduces a defect that was verified against the pre-fix code:
D4 geometry import validation, D5 CSV formula injection, D7 parcel-candidate
overstatement, D8 geometry history, D9/D10 caps and ordering, D13 stale writes.
"""
import json
import uuid

from fastapi.testclient import TestClient

import mapping
import server
from main import app

client = TestClient(app, raise_server_exceptions=False)


def _make_user(role="VERIFICATION_OFFICER"):
    suffix = uuid.uuid4().hex[:10]
    email = f"maphard-{suffix}@example.test"
    password = "Strong Map Password 123!"
    signup = client.post("/api/auth/signup", json={
        "full_name": "Map Hardening", "email": email, "password": password,
    })
    assert signup.status_code == 200
    with server.get_db() as db:
        db.execute("UPDATE users SET role=? WHERE email=?", (role, email))
    login = client.post("/api/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200
    return {"Authorization": "Bearer " + login.json()["token"]}


def _insert_document(doc_id, *, owner="Ram Singh", survey="452", village="Sundarpur",
                     lat=None, lon=None, status="APPROVED", created_at=1700000000.0,
                     area="2.5 ha"):
    fields = {
        "owner_name": {"value": owner, "confidence": 0.95},
        "survey_number": {"value": survey, "confidence": 0.95},
        "area": {"value": area, "confidence": 0.9},
        "village": {"value": village, "confidence": 0.95},
        "tehsil": {"value": "Sadar", "confidence": 0.9},
        "district": {"value": "Ghaziabad", "confidence": 0.9},
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
             "map-hardening@example.test", created_at, created_at, lat, lon),
        )


# ---------------------------------------------------------------- D4: import


def test_import_rejects_out_of_range_coordinates():
    doc_id = "HARD-OOR-" + uuid.uuid4().hex[:8]
    _insert_document(doc_id)
    headers = _make_user()
    geo = {"type": "Polygon", "coordinates": [[[500.0, 999.0], [80.2, 23.1], [80.2, 23.2], [500.0, 999.0]]]}
    response = client.post(f"/api/map/records/{doc_id}/boundary/import", headers=headers,
                           json={"geojson": geo, "reason": "bad file"})
    assert response.status_code == 400
    assert "geometry.coordinates[0][0]" in response.json()["detail"]
    with server.get_db() as db:
        row = db.execute("SELECT map_geometry FROM documents WHERE id=?", (doc_id,)).fetchone()
    assert row["map_geometry"] in (None, "")


def test_import_rejects_non_finite_coordinates_without_crashing():
    doc_id = "HARD-NAN-" + uuid.uuid4().hex[:8]
    _insert_document(doc_id)
    headers = _make_user()
    headers["Content-Type"] = "application/json"
    # Raw JSON with NaN literals: stdlib json accepts them, so the endpoint
    # itself must reject them (pre-fix this crashed the worker with a 500).
    body = ('{"geojson": {"type": "Polygon", "coordinates": '
            '[[[NaN, 23.1], [80.2, 23.1], [80.2, 23.2], [NaN, 23.1]]]}}')
    response = client.post(f"/api/map/records/{doc_id}/boundary/import", headers=headers, content=body)
    assert response.status_code == 400
    assert "finite" in response.json()["detail"]
    with server.get_db() as db:
        row = db.execute("SELECT map_geometry FROM documents WHERE id=?", (doc_id,)).fetchone()
    assert row["map_geometry"] in (None, "")


def test_import_rejects_self_intersection_open_ring_and_vertex_flood():
    doc_id = "HARD-MAL-" + uuid.uuid4().hex[:8]
    _insert_document(doc_id)
    headers = _make_user()
    # Self-intersecting bow-tie (rings are auto-closed).
    bowtie = {"type": "Polygon", "coordinates": [[[80.1, 23.1], [80.2, 23.2], [80.1, 23.2], [80.2, 23.1]]]}
    assert client.post(f"/api/map/records/{doc_id}/boundary/import", headers=headers,
                       json={"geojson": bowtie}).status_code == 400
    # Degenerate zero-area ring.
    flat = {"type": "Polygon", "coordinates": [[[80.1, 23.1], [80.2, 23.1], [80.3, 23.1], [80.1, 23.1]]]}
    assert client.post(f"/api/map/records/{doc_id}/boundary/import", headers=headers,
                       json={"geojson": flat}).status_code == 400
    # Vertex flood beyond MAP_MAX_IMPORT_VERTICES.
    ring = [[80.0 + i * 1e-5, 23.0] for i in range(mapping.MAP_MAX_IMPORT_VERTICES + 10)]
    ring.append(ring[0])
    flood = {"type": "Polygon", "coordinates": [ring]}
    response = client.post(f"/api/map/records/{doc_id}/boundary/import", headers=headers,
                           json={"geojson": flood})
    assert response.status_code == 400
    assert "too many vertices" in response.json()["detail"]
    # Unsupported geometry types are refused, not coerced.
    for geom in ({"type": "Point", "coordinates": [80.1, 23.1]},
                 {"type": "MultiPolygon", "coordinates": []},
                 {"type": "GeometryCollection", "geometries": []}):
        assert client.post(f"/api/map/records/{doc_id}/boundary/import", headers=headers,
                           json={"geojson": geom}).status_code == 400


def test_import_accepts_valid_polygon_and_records_reason():
    doc_id = "HARD-OK-" + uuid.uuid4().hex[:8]
    _insert_document(doc_id)
    headers = _make_user()
    geo = {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [
        [[80.1, 23.1], [80.2, 23.1], [80.2, 23.2], [80.1, 23.2], [80.1, 23.1]]]}}
    response = client.post(f"/api/map/records/{doc_id}/boundary/import", headers=headers,
                           json={"geojson": geo, "reason": "Checked against survey sheet"})
    assert response.status_code == 200
    assert response.json()["geometry"]["coordinates"][0][0] == [80.1, 23.1]
    with server.get_db() as db:
        row = db.execute("SELECT map_geometry_reason FROM documents WHERE id=?", (doc_id,)).fetchone()
    assert row["map_geometry_reason"] == "Checked against survey sheet"


# --------------------------------------------------- D8: geometry history


def test_boundary_changes_preserve_previous_geometry_and_reason():
    doc_id = "HARD-HIST-" + uuid.uuid4().hex[:8]
    _insert_document(doc_id)
    headers = _make_user()
    first = client.put(f"/api/map/records/{doc_id}/boundary", headers=headers,
                       json={"points": [[23.1, 80.1], [23.1, 80.2], [23.2, 80.2]], "reason": "first trace"})
    assert first.status_code == 200
    second = client.put(f"/api/map/records/{doc_id}/boundary", headers=headers,
                        json={"points": [[23.5, 80.5], [23.5, 80.6], [23.6, 80.6]], "reason": "corrected trace"})
    assert second.status_code == 200

    history = client.get(f"/api/map/records/{doc_id}/boundary/history", headers=headers)
    assert history.status_code == 200
    data = history.json()
    assert data["current"]["reason"] == "corrected trace"
    actions = [item["action"] for item in data["items"]]
    assert actions == ["boundary_set", "boundary_set"]
    # The FIRST geometry must be recoverable from history (before/after chain).
    oldest = data["items"][-1]
    assert oldest["previous_geometry"] is None
    assert oldest["new_geometry"]["coordinates"][0][0] == [80.1, 23.1]
    # Newest entry: previous = first trace, new = corrected trace.
    assert data["items"][0]["previous_geometry"]["coordinates"][0][0] == [80.1, 23.1]
    assert data["items"][0]["new_geometry"]["coordinates"][0][0] == [80.5, 23.5]
    assert data["items"][0]["reason"] == "corrected trace"
    assert data["items"][0]["actor"]


def test_clear_and_estimate_record_history_and_clear_keeps_previous():
    doc_id = "HARD-CLR-" + uuid.uuid4().hex[:8]
    _insert_document(doc_id, lat=28.62, lon=77.10, area="2.5 ha")
    headers = _make_user()
    assert client.put(f"/api/map/records/{doc_id}/boundary", headers=headers,
                      json={"points": [[23.1, 80.1], [23.1, 80.2], [23.2, 80.2]]}).status_code == 200
    assert client.post(f"/api/map/records/{doc_id}/boundary/estimate", headers=headers,
                       json={"reason": "screening estimate"}).status_code == 200
    assert client.post(f"/api/map/records/{doc_id}/boundary/clear", headers=headers,
                       json={"reason": "invalid trace"}).status_code == 200
    history = client.get(f"/api/map/records/{doc_id}/boundary/history", headers=headers).json()
    actions = [item["action"] for item in history["items"]]
    assert actions == ["boundary_cleared", "boundary_estimated", "boundary_set"]
    # Cleared geometry still recoverable from the clear entry.
    assert history["items"][0]["previous_geometry"] is not None
    assert history["current"]["geometry"] is None


def test_boundary_history_is_role_and_visibility_scoped():
    doc_id = "HARD-HRBAC-" + uuid.uuid4().hex[:8]
    _insert_document(doc_id, status="DRAFT")
    reviewer = _make_user()
    assert client.get(f"/api/map/records/{doc_id}/boundary/history", headers=reviewer).status_code == 200
    # A data officer who did not upload the record cannot read its geometry history.
    outsider = _make_user(role="DATA_OFFICER")
    assert client.get(f"/api/map/records/{doc_id}/boundary/history", headers=outsider).status_code == 404
    # Unauthenticated access is refused (cookie-free client).
    fresh = TestClient(app, raise_server_exceptions=False)
    assert fresh.get(f"/api/map/records/{doc_id}/boundary/history").status_code == 401
    # Writes stay RBAC-protected.
    assert client.post(f"/api/map/records/{doc_id}/boundary/import", headers=outsider,
                       json={"geojson": {"type": "Polygon", "coordinates": [
                           [[80.1, 23.1], [80.2, 23.1], [80.2, 23.2], [80.1, 23.1]]]}}).status_code == 403


# ------------------------------------------------------ D13: stale writes


def test_stale_write_is_rejected_when_record_changed_elsewhere():
    doc_id = "HARD-STALE-" + uuid.uuid4().hex[:8]
    _insert_document(doc_id, lat=28.62, lon=77.10)
    headers = _make_user()
    with server.get_db() as db:
        stale_version = db.execute("SELECT updated_at FROM documents WHERE id=?", (doc_id,)).fetchone()[0]
    # Another reviewer changes the record...
    moved = client.put(f"/api/map/records/{doc_id}/location", headers=headers,
                       json={"lat": 28.63, "lon": 77.11})
    assert moved.status_code == 200
    # ...and a stale editor must get a conflict, not a silent overwrite.
    conflict = client.put(f"/api/map/records/{doc_id}/location", headers=headers,
                          json={"lat": 28.64, "lon": 77.12, "expected_updated_at": stale_version})
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "STALE_RECORD"
    with server.get_db() as db:
        row = db.execute("SELECT lat FROM documents WHERE id=?", (doc_id,)).fetchone()
    assert row["lat"] == 28.63  # unchanged by the stale editor
    # Without expected_updated_at the legacy behaviour remains (backward compat).
    legacy = client.put(f"/api/map/records/{doc_id}/location", headers=headers,
                        json={"lat": 28.65, "lon": 77.13})
    assert legacy.status_code == 200


def test_stale_boundary_write_is_rejected():
    doc_id = "HARD-STALE-B-" + uuid.uuid4().hex[:8]
    _insert_document(doc_id)
    headers = _make_user()
    with server.get_db() as db:
        stale_version = db.execute("SELECT updated_at FROM documents WHERE id=?", (doc_id,)).fetchone()[0]
    assert client.put(f"/api/map/records/{doc_id}/boundary", headers=headers,
                      json={"points": [[23.1, 80.1], [23.1, 80.2], [23.2, 80.2]]}).status_code == 200
    conflict = client.post(f"/api/map/records/{doc_id}/boundary/import", headers=headers,
                           json={"geojson": {"type": "Polygon", "coordinates": [
                               [[80.5, 23.5], [80.6, 23.5], [80.6, 23.6], [80.5, 23.5]]]},
                                 "expected_updated_at": stale_version})
    assert conflict.status_code == 409


# -------------------------------------------------- D5: CSV formula safety


def test_csv_export_neutralises_formula_injection():
    doc_id = "HARD-CSV-" + uuid.uuid4().hex[:8]
    _insert_document(doc_id, owner="=cmd|' /C calc'!A0")
    headers = _make_user()
    response = client.get("/api/map/export.csv", headers=headers)
    assert response.status_code == 200
    row = next(line for line in response.text.splitlines() if doc_id in line)
    assert "='+cmd" in row or ",'=cmd" in row  # value re-prefixed, not executed
    assert ",=cmd|" not in row
    # Export population metadata is disclosed.
    assert response.headers.get("X-Map-Record-Cap") == str(mapping.MAP_RECORD_CAP)
    assert response.headers.get("X-Map-Truncated") in {"true", "false"}


def test_csv_export_bom_and_unicode_preserved():
    _insert_document(doc_id := "HARD-UNI-" + uuid.uuid4().hex[:8], owner="श्री राम सिंह")
    headers = _make_user()
    response = client.get("/api/map/export.csv", headers=headers)
    assert response.text.startswith("\ufeff")
    assert "श्री राम सिंह" in response.text


# ------------------------------------------- D9/D10: caps and ordering


def test_summary_and_records_share_the_same_record_cap():
    headers = _make_user()
    summary = client.get("/api/map/summary", headers=headers).json()
    records = client.get("/api/map/records?limit=10000", headers=headers).json()
    assert summary["metadata"]["record_cap"] == records["metadata"]["record_cap"] == mapping.MAP_RECORD_CAP
    # Same dataset => same count for an unfiltered view.
    assert summary["summary"]["records"] == records["total"]


def test_records_pagination_is_deterministic_with_equal_timestamps():
    ids = []
    for index in range(5):
        doc_id = f"HARD-PAGE-{index}-" + uuid.uuid4().hex[:6]
        ids.append(doc_id)
        _insert_document(doc_id, created_at=1700000000.0)  # identical timestamps
    headers = _make_user()
    seen = []
    offset = 0
    while True:
        page = client.get(f"/api/map/records?limit=2&offset={offset}&q=HARD-PAGE-", headers=headers).json()
        seen.extend(item["id"] for item in page["records"])
        if not page["has_more"]:
            break
        offset += 2
        assert offset < 50
    # No duplicates and no drops across page boundaries.
    assert sorted(seen) == sorted(set(seen))
    assert set(ids) <= set(seen)


def test_records_location_filter_still_works_after_sql_fix():
    exact_id = "HARD-LOC-E-" + uuid.uuid4().hex[:8]
    approx_id = "HARD-LOC-V-" + uuid.uuid4().hex[:8]
    unresolved_id = "HARD-LOC-U-" + uuid.uuid4().hex[:8]
    _insert_document(exact_id, lat=28.62, lon=77.10)
    _insert_document(approx_id, village="Sundarpur")
    _insert_document(unresolved_id, village="")
    headers = _make_user()
    exact = client.get("/api/map/records?location=EXACT_PIN&q=HARD-LOC-", headers=headers).json()
    assert {r["id"] for r in exact["records"]} == {exact_id}
    village = client.get("/api/map/records?location=VILLAGE_LEVEL&q=HARD-LOC-", headers=headers).json()
    assert {r["id"] for r in village["records"]} == {approx_id}
    unresolved = client.get("/api/map/records?location=UNRESOLVED&q=HARD-LOC-", headers=headers).json()
    assert {r["id"] for r in unresolved["records"]} == {unresolved_id}


def test_invalid_bbox_and_unauthenticated_access_are_rejected():
    headers = _make_user()
    bad = client.get("/api/map/records?min_lat=30&min_lon=80&max_lat=28&max_lon=77", headers=headers)
    assert bad.status_code == 400
    fresh = TestClient(app, raise_server_exceptions=False)
    assert fresh.get("/api/map/records").status_code == 401
    huge = client.get("/api/map/records?limit=999999", headers=headers)
    assert huge.status_code == 422  # bounded page size


# ------------------------------------------- D7: parcel candidate honesty


def test_parcel_candidates_do_not_overstate_weak_matches():
    # One reference parcel shares only the VILLAGE with the document: no
    # strong cadastral identifier matches, so the endpoint must not say MATCH.
    headers = _make_user()
    suffix = uuid.uuid4().hex[:8]
    with server.get_db() as db:
        db.execute(
            """INSERT INTO properties (property_id,parcel_id,district,taluka,village,survey_number,
               sub_division,area,area_unit,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (f"PROP-WEAK-{suffix}", f"PARCEL-WEAK-{suffix}", "Ghaziabad", "Sadar",
             "WeakMatchVillage", "777", None, 1.0, "ha", 1, 1),
        )
    doc_id = "HARD-WEAK-" + suffix
    _insert_document(doc_id, survey="99999", village="WeakMatchVillage")
    try:
        response = client.get(f"/api/map/records/{doc_id}/parcel-candidates", headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert data["resolution_status"] != "MATCH"
        if data["candidates"]:
            # Evidence must be separated, with geography-only positives.
            assert data["resolution_status"] in {"INSUFFICIENT_EVIDENCE", "POSSIBLE MATCH", "AMBIGUOUS_MATCH"}
            evidence = data["candidates"][0]["evidence"]
            assert "village" in evidence["positive"]
            assert not set(evidence["positive"]) & set(mapping.LAND_IDENTIFIER_FIELDS)
            assert evidence["source_reliability"]
    finally:
        with server.get_db() as db:
            db.execute("DELETE FROM properties WHERE property_id=?", (f"PROP-WEAK-{suffix}",))


def test_parcel_candidates_no_match_when_nothing_shared():
    headers = _make_user()
    doc_id = "HARD-NOM-" + uuid.uuid4().hex[:8]
    _insert_document(doc_id, survey="88888", village="TotallyUnsharedVillage")
    data = client.get(f"/api/map/records/{doc_id}/parcel-candidates", headers=headers).json()
    assert data["resolution_status"] == "NO MATCH"
    assert data["candidates"] == []


# ------------------------------------------------- detail endpoint scaling


def test_single_record_detail_endpoints_do_not_require_full_dataset_scan():
    """spatial-checks/layers/parcel-candidates must answer for one record even
    when it is NOT the newest record, with correct 404s for invisible ids."""
    headers = _make_user()
    old_id = "HARD-OLD-" + uuid.uuid4().hex[:8]
    new_id = "HARD-NEW-" + uuid.uuid4().hex[:8]
    _insert_document(old_id, created_at=1600000000.0)
    _insert_document(new_id, created_at=1700000000.0)
    assert client.get(f"/api/map/records/{old_id}/spatial-checks", headers=headers).status_code == 200
    assert client.get(f"/api/map/records/{old_id}/layers", headers=headers).status_code == 200
    assert client.get(f"/api/map/records/{old_id}/parcel-candidates", headers=headers).status_code == 200
    assert client.get("/api/map/records/DOES-NOT-EXIST/spatial-checks", headers=headers).status_code == 404
    # Data officers only see their own uploads on detail endpoints too.
    outsider = _make_user(role="DATA_OFFICER")
    assert client.get(f"/api/map/records/{old_id}/spatial-checks", headers=outsider).status_code == 404
