"""Saved record → existing parcel geometry → map.

Locate is read-only. It matches parcel ID, land ID, or survey/khasra plus
village and district/tehsil. It never matches owner name, never geocodes a
village into a cadastral parcel, and never writes a centroid or pin.
"""
import json
import uuid
from pathlib import Path

import pytest

import mapping
import server
from land_intel import land_identity

ROOT = Path(__file__).resolve().parents[1]


def _snapshot(property_id, document_id=None):
    with server.get_db() as db:
        row = db.execute(
            "SELECT geometry, centroid, latitude, longitude, location_status FROM properties WHERE property_id=?",
            (property_id,),
        ).fetchone()
        audit = 0
        if document_id:
            audit = db.execute("SELECT COUNT(*) AS n FROM audit WHERE doc_id=?", (document_id,)).fetchone()["n"]
        timeline = db.execute(
            "SELECT COUNT(*) AS n FROM property_timeline WHERE property_id=?",
            (property_id,),
        ).fetchone()["n"]
    return {
        "geometry": row["geometry"] if row else None,
        "centroid": row["centroid"] if row else None,
        "latitude": row["latitude"] if row else None,
        "longitude": row["longitude"] if row else None,
        "location_status": row["location_status"] if row else None,
        "audit": audit,
        "timeline": timeline,
    }


@pytest.fixture
def locate_world(make_user_client):
    """Named DEMO-LI-S* fixtures. Cleaned up even if the assertion fails."""
    admin, headers, _ = make_user_client("ADMIN", prefix="locate")
    created = {"properties": [], "documents": []}

    def add_property(parcel_id, *, survey, village, district="Locate District", tehsil="Locate Tehsil",
                     geometry=None, centroid=None, latitude=None, longitude=None, location_status="UNRESOLVED",
                     sub_division=""):
        property_id = "LOCATE-PROP-" + parcel_id
        created["properties"].append(property_id)
        with server.get_db() as db:
            db.execute(
                """INSERT INTO properties (
                    property_id, parcel_id, district, taluka, village, survey_number, gat_number, khasra_number,
                    sub_division, area, area_unit, geometry, centroid, latitude, longitude, georeferenced,
                    geometry_source, data_source, created_at, updated_at, location_status
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (property_id, parcel_id, district, tehsil, village, survey, "", survey, sub_division,
                 1.0, "hectare",
                 json.dumps(geometry) if geometry else None,
                 json.dumps(centroid) if centroid else None,
                 latitude, longitude, 0,
                 "Copied from existing synthetic reference geometry" if geometry else "Intentionally unresolved",
                 "Locate regression fixture", 1, 1, location_status),
            )
        return property_id

    def add_document(doc_id, *, owner, survey, village, district="Locate District", tehsil="Locate Tehsil",
                     status="APPROVED", uploader="locate@example.test", property_id=None):
        created["documents"].append(doc_id)
        fields = {
            "owner_name": {"value": owner},
            "survey_number": {"value": survey},
            "khasra_number": {"value": survey},
            "village": {"value": village},
            "tehsil": {"value": tehsil},
            "district": {"value": district},
        }
        with server.get_db() as db:
            db.execute(
                """INSERT INTO documents
                   (id, filename, doc_type, mean_conf, verdict, status, languages, pages, fields, validation,
                    ai_decision_support, ocr_text, cleaned_ocr_text, detected_language, original_fields,
                    uploaded_by, reviewer_comments, created_at, updated_at, lat, lon)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (doc_id, doc_id + ".pdf", "Land Record", 90, "review", status, "[]", 1,
                 json.dumps(fields), "{}", "{}", "hidden-ocr-must-not-leak", "", "eng", json.dumps(fields),
                 uploader, "", 1, 1, None, None),
            )
            if property_id:
                db.execute(
                    "INSERT OR IGNORE INTO property_documents(property_id, document_id, source_type, linked_at) VALUES (?,?,?,?)",
                    (property_id, doc_id, "locate_fixture", 1),
                )
        return doc_id

    mapping._ensure_tables()
    yield {
        "admin": admin,
        "headers": headers,
        "add_property": add_property,
        "add_document": add_document,
    }
    with server.get_db() as db:
        for doc_id in created["documents"]:
            db.execute("DELETE FROM property_documents WHERE document_id=?", (doc_id,))
            db.execute("DELETE FROM documents WHERE id=?", (doc_id,))
            db.execute("DELETE FROM audit WHERE doc_id=?", (doc_id,))
        for property_id in created["properties"]:
            db.execute("DELETE FROM property_documents WHERE property_id=?", (property_id,))
            db.execute("DELETE FROM properties WHERE property_id=?", (property_id,))


def _existing_polygon(index):
    seed = mapping._SYNTHETIC_PROPERTIES[index]
    return json.loads(json.dumps(seed["geometry"])), json.loads(json.dumps(seed["centroid"]))


def test_named_cases_cover_polygon_point_missing_centroid_and_unresolved(locate_world):
    admin, headers = locate_world["admin"], locate_world["headers"]
    add_property, add_document = locate_world["add_property"], locate_world["add_document"]
    polygon, centroid = _existing_polygon(0)
    other_polygon, other_centroid = _existing_polygon(1)
    point_at = mapping._SYNTHETIC_PROPERTIES[2]["centroid"]
    missing_polygon, _missing_centroid = _existing_polygon(3)
    fifth_polygon, fifth_centroid = _existing_polygon(2)

    cases = {
        "DEMO-LI-S1": {"geometry": polygon, "centroid": centroid, "kind": "Polygon", "status": "REFERENCE_GEOMETRY"},
        "DEMO-LI-S2": {"geometry": other_polygon, "centroid": other_centroid, "kind": "Polygon", "status": "REFERENCE_GEOMETRY"},
        "DEMO-LI-S3": {"geometry": {"type": "Point", "coordinates": point_at}, "centroid": point_at, "kind": "Point", "status": "REFERENCE_GEOMETRY"},
        "DEMO-LI-S4": {"geometry": missing_polygon, "centroid": None, "kind": "Polygon", "status": "REFERENCE_GEOMETRY"},
        "DEMO-LI-S5": {"geometry": fifth_polygon, "centroid": fifth_centroid, "kind": "Polygon", "status": "REFERENCE_GEOMETRY"},
        "DEMO-LI-S9": {"geometry": None, "centroid": None, "kind": None, "status": "UNRESOLVED"},
    }
    for parcel_id, spec in cases.items():
        survey = parcel_id.replace("DEMO-LI-", "LS-")
        village = "Locate " + parcel_id
        property_id = add_property(
            parcel_id, survey=survey, village=village, geometry=spec["geometry"], centroid=spec["centroid"],
            location_status=spec["status"],
        )
        doc_id = add_document(
            "LOCATE-DOC-" + parcel_id, owner="Not A Spatial Key", survey=survey, village=village, property_id=property_id,
        )
        before = _snapshot(property_id, doc_id)
        resolved = admin.post("/api/parcels/resolve", headers=headers, json={"document_id": doc_id})
        assert resolved.status_code == 200, resolved.text
        body = resolved.json()
        assert body["persisted"] is False
        assert body["read_only"] is True
        parcel = body["parcel"]
        assert parcel["parcel_id"] == parcel_id
        assert parcel["survey_number"] == survey
        assert parcel["village"] == village
        assert parcel["land_id"] == land_identity(survey, village)[1]
        assert parcel["location"]["geocoded_address"] is None
        assert parcel["location"]["authoritative"] is False
        if spec["kind"]:
            assert body["status"] == "RESOLVED"
            assert parcel["geometry"]["type"] == spec["kind"]
            assert parcel["location"]["label"] == "Reference geometry"
            assert parcel["location"]["kind"] == "reference_geometry"
            if spec["centroid"] is None:
                assert parcel["centroid"] is None
                assert parcel["surface_point"]
                assert parcel["location"]["surface_point"] == "DERIVED_NOT_STORED"
            else:
                assert parcel["centroid"] == spec["centroid"]
        else:
            assert parcel["geometry"] is None
            assert parcel["location"]["label"] == "Location not available"
            assert parcel["location"]["kind"] == "unresolved"
        after = _snapshot(property_id, doc_id)
        assert after == before

        by_land = admin.post("/api/parcels/resolve", headers=headers, json={"land_id": parcel["land_id"]})
        assert by_land.status_code == 200
        assert by_land.json()["parcel"]["parcel_id"] == parcel_id
        assert _snapshot(property_id, doc_id) == before

        by_parcel = admin.post("/api/parcels/resolve", headers=headers, json={"parcel_id": parcel_id})
        assert by_parcel.status_code == 200
        assert by_parcel.json()["parcel"]["survey_number"] == survey
        assert _snapshot(property_id, doc_id) == before


def test_owner_name_is_not_spatial_identity(locate_world):
    admin, headers = locate_world["admin"], locate_world["headers"]
    own = locate_world["add_property"]("DEMO-LI-S1-OWNER", survey="LS-OWN", village="Owner Village")
    decoy = locate_world["add_property"]("DEMO-LI-S2-OWNER", survey="Unique Owner", village="Owner Village")
    doc_id = locate_world["add_document"](
        "LOCATE-DOC-OWNER", owner="Unique Owner", survey="LS-OWN", village="Owner Village", property_id=own,
    )
    resolved = admin.post(
        "/api/parcels/resolve", headers=headers,
        json={"document_id": doc_id, "owner_name": "Unique Owner", "owner": "Unique Owner"},
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["parcel"]["property_id"] == own
    assert resolved.json()["parcel"]["property_id"] != decoy


def test_district_disambiguates_and_conflict_does_not_draw_the_other_parcel(locate_world):
    admin, headers = locate_world["admin"], locate_world["headers"]
    north = locate_world["add_property"](
        "DEMO-LI-S5-NORTH", survey="LS-DIST", village="Same Village", district="North District",
        geometry={"type": "Point", "coordinates": [4.5, 6.5]}, centroid=[4.5, 6.5], location_status="REFERENCE_GEOMETRY",
    )
    locate_world["add_property"](
        "DEMO-LI-S5-SOUTH", survey="LS-DIST", village="Same Village", district="South District",
        geometry={"type": "Point", "coordinates": [8.5, 9.5]}, centroid=[8.5, 9.5], location_status="REFERENCE_GEOMETRY",
    )
    doc_id = locate_world["add_document"](
        "LOCATE-DOC-DIST", owner="Shared Name", survey="LS-DIST", village="Same Village",
        district="North District", property_id=north,
    )
    resolved = admin.post("/api/parcels/resolve", headers=headers, json={"document_id": doc_id})
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["parcel"]["property_id"] == north
    assert resolved.json()["parcel"]["district"] == "North District"
    conflict = admin.post("/api/parcels/resolve", headers=headers, json={
        "document_id": doc_id, "parcel_id": "DEMO-LI-S5-SOUTH",
    })
    assert conflict.status_code == 200
    assert conflict.json()["status"] == "CONFLICT"
    assert conflict.json()["parcel"] is None
    assert "8.5" not in conflict.text


def test_ambiguous_identity_does_not_choose_or_return_rings(locate_world):
    admin, headers = locate_world["admin"], locate_world["headers"]
    locate_world["add_property"](
        "DEMO-LI-S2-A", survey="LS-AMB", village="Ambigville", sub_division="A",
        geometry={"type": "Point", "coordinates": [3.14159, 2.71828]}, location_status="REFERENCE_GEOMETRY",
    )
    locate_world["add_property"](
        "DEMO-LI-S2-B", survey="LS-AMB", village="Ambigville", sub_division="B",
        geometry={"type": "Point", "coordinates": [1.41421, 1.73205]}, location_status="REFERENCE_GEOMETRY",
    )
    doc_id = locate_world["add_document"]("LOCATE-DOC-AMB", owner="Either", survey="LS-AMB", village="Ambigville")
    resolved = admin.post("/api/parcels/resolve", headers=headers, json={"document_id": doc_id})
    assert resolved.status_code == 200, resolved.text
    body = resolved.json()
    assert body["status"] == "AMBIGUOUS"
    assert body["parcel"] is None
    assert body["persisted"] is False
    assert "3.14159" not in resolved.text
    assert "1.41421" not in resolved.text
    assert all("geometry" not in match for match in body["matches"])


def test_hidden_record_does_not_reveal_geometry(locate_world, make_user_client):
    viewer, viewer_headers, _ = make_user_client("VIEWER", prefix="locatev")
    officer, officer_headers, officer_email = make_user_client("DATA_OFFICER", prefix="locateo")
    property_id = locate_world["add_property"](
        "DEMO-LI-S9-HIDDEN", survey="LS-HID", village="Hidden Village",
        geometry={"type": "Point", "coordinates": [1.25, 2.5]}, centroid=[1.25, 2.5],
        location_status="REFERENCE_GEOMETRY",
    )
    doc_id = locate_world["add_document"](
        "LOCATE-DOC-HIDDEN", owner="Secret Holder", survey="LS-HID", village="Hidden Village",
        status="DRAFT", uploader="someone-else@example.test", property_id=property_id,
    )
    for client, headers in ((viewer, viewer_headers), (officer, officer_headers)):
        denied = client.post("/api/parcels/resolve", headers=headers, json={"document_id": doc_id})
        assert denied.status_code == 404
        assert "1.25" not in denied.text
        assert "Secret Holder" not in denied.text
        by_parcel = client.post("/api/parcels/resolve", headers=headers, json={"parcel_id": "DEMO-LI-S9-HIDDEN"})
        assert by_parcel.status_code == 404
        assert "1.25" not in by_parcel.text
    listing = viewer.get("/api/documents", headers=viewer_headers)
    assert doc_id not in listing.text
    assert "1.25" not in listing.text
    # the officer still cannot read another officer's draft, and Locate stays read-only
    assert officer_email not in doc_id
    assert _snapshot(property_id, doc_id)["latitude"] is None


def test_viewer_locate_of_an_approved_record_is_read_only(locate_world, make_user_client):
    viewer, viewer_headers, _ = make_user_client("VIEWER", prefix="locateva")
    polygon, centroid = _existing_polygon(0)
    property_id = locate_world["add_property"](
        "DEMO-LI-S1-VIEW", survey="LS-VIEW", village="Viewer Village",
        geometry=polygon, centroid=centroid, location_status="REFERENCE_GEOMETRY",
    )
    doc_id = locate_world["add_document"](
        "LOCATE-DOC-VIEW", owner="Approved Owner", survey="LS-VIEW", village="Viewer Village",
        status="APPROVED", property_id=property_id,
    )
    before = _snapshot(property_id, doc_id)
    resolved = viewer.post("/api/parcels/resolve", headers=viewer_headers, json={"document_id": doc_id})
    assert resolved.status_code == 200, resolved.text
    body = resolved.json()
    assert body["read_only"] is True
    assert body["persisted"] is False
    assert body["parcel"]["location"]["label"] == "Reference geometry"
    assert body["parcel"]["geometry"]["type"] == "Polygon"
    assert _snapshot(property_id, doc_id) == before
    listing = viewer.get("/api/documents", headers=viewer_headers).json()
    row = next(item for item in listing["documents"] if item["id"] == doc_id)
    assert row["spatial"]["location_label"] == "Reference geometry"
    assert row["spatial"]["has_geometry"] is True
    assert row["spatial"]["parcel_id"] == "DEMO-LI-S1-VIEW"
    assert "coordinates" not in json.dumps(row["spatial"])
    assert "ocr_text" not in row


def test_seeded_demo_records_locate_existing_reference_geometry(make_user_client):
    import land_demo_data

    admin, headers, _ = make_user_client("ADMIN", prefix="locateseed")
    land_demo_data.seed_all()
    try:
        with server.get_db() as db:
            assert db.execute("SELECT COUNT(*) AS n FROM properties WHERE property_id LIKE 'DEMO-LI-%'").fetchone()["n"] == 19
        listing = admin.get("/api/documents", headers=headers, params={"limit": 1000})
        assert listing.status_code == 200
        documents = {item["id"]: item for item in listing.json()["documents"]}
        saved = documents["DEMO-LI-MUT-001-DOC1"]
        assert saved["spatial"]["location_label"] == "Reference geometry"
        assert saved["spatial"]["has_geometry"] is True
        assert saved["spatial"]["land_id"] == land_identity("71/2", "Devnapur")[1]
        assert "coordinates" not in json.dumps(saved["spatial"])

        before = _snapshot("DEMO-LI-PROP-001", "DEMO-LI-MUT-001-DOC1")
        resolved = admin.post("/api/parcels/resolve", headers=headers, json={"document_id": "DEMO-LI-MUT-001-DOC1"})
        assert resolved.status_code == 200, resolved.text
        parcel = resolved.json()["parcel"]
        assert parcel["survey_number"] == "71/2"
        assert parcel["village"] == "Devnapur"
        assert parcel["parcel_id"] == "DEMO-LI-PARCEL-001"
        assert parcel["land_id"] == saved["spatial"]["land_id"]
        assert parcel["geometry"]["type"] == "Polygon"
        assert parcel["location"]["label"] == "Reference geometry"
        assert parcel["location"]["authoritative"] is False
        assert parcel["latitude"] is None or parcel.get("location", {}).get("stored_latitude") is None
        assert before["latitude"] is None and before["longitude"] is None
        assert _snapshot("DEMO-LI-PROP-001", "DEMO-LI-MUT-001-DOC1") == before

        point = admin.post("/api/parcels/resolve", headers=headers, json={"document_id": "DEMO-LI-MUT-003-DOC1"}).json()
        assert point["parcel"]["geometry"]["type"] == "Point"
        assert point["parcel"]["location"]["label"] == "Reference geometry"
        assert _snapshot("DEMO-LI-PROP-003", "DEMO-LI-MUT-003-DOC1")["latitude"] is None

        missing = admin.post("/api/parcels/resolve", headers=headers, json={"document_id": "DEMO-LI-MUT-004-DOC1"}).json()
        assert missing["parcel"]["geometry"]["type"] == "Polygon"
        assert missing["parcel"]["centroid"] is None
        assert missing["parcel"]["surface_point"]
        assert _snapshot("DEMO-LI-PROP-004")["centroid"] is None

        unresolved = admin.post("/api/parcels/resolve", headers=headers, json={"document_id": "DEMO-LI-ENC-004-DOC1"}).json()
        assert unresolved["parcel"]["geometry"] is None
        assert unresolved["parcel"]["location"]["label"] == "Location not available"
        assert documents["DEMO-LI-ENC-004-DOC1"]["spatial"]["location_label"] == "Location not available"
        assert _snapshot("DEMO-LI-PROP-009")["geometry"] in (None, "")

        detail = admin.get(f"/api/land-records/{saved['spatial']['land_id']}", headers=headers)
        assert detail.status_code == 200
        assert "land_id=" in detail.json()["map"]["url"]
        assert "locate=1" in detail.json()["map"]["url"]
        assert detail.json()["map"]["location_label"] == "Reference geometry"
    finally:
        land_demo_data.clear_all()


def test_locate_ui_uses_the_existing_map_and_does_not_geocode():
    app_js = (ROOT / "js" / "app.js").read_text(encoding="utf-8")
    map_js = (ROOT / "map.js").read_text(encoding="utf-8")
    index = (ROOT / "index.html").read_text(encoding="utf-8")
    html = (ROOT / "map.html").read_text(encoding="utf-8")
    assert "data-record-locate" in app_js
    assert "function locateRecord" in app_js
    assert "params.set('land_id', landId)" in app_js
    assert "params.set('parcel_id', parcelId)" in app_js
    assert "params.set('locate', '1')" in app_js
    assert "<th>Location</th>" in index
    assert "function locateSavedRecord" in map_js
    assert "/api/parcels/resolve" in map_js
    assert "L.geoJSON" in map_js
    assert "Reference geometry" in map_js
    assert "Land Intelligence" in map_js
    assert "Open document" in map_js
    assert "showMode: 'selected'" in map_js
    assert "APPROXIMATE — VILLAGE LOCATION" in map_js
    assert "No exact parcel coordinate is being created" in map_js
    start = map_js.find("async function locateSavedRecord")
    end = map_js.find("async function boot", start)
    body = map_js[start:end]
    assert "/api/map/geocode" not in body
    assert "/location" not in body
    assert "REFERENCE_GEOMETRY" in html
    assert "Reference geometry" in html
