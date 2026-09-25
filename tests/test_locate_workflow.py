"""Locate workflow regression tests (A-J).

Covers the complete chain the acceptance criterion depends on:
All Records -> record -> canonical parcel -> map layer/centre/highlight.

The tests exercise the real application: the demo dataset, the canonical land
records API, the canonical parcel resolver (``POST /api/parcels/resolve`` and
``GET /api/land-records/{land_id}/parcel``), the deep-link URL contract the map
page consumes, and the static front-end wiring that renders the Locate action.

No new synthetic dataset is created: everything is built on the existing demo
scenarios plus the existing demo parcel geometry.
"""
import json
import re
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def demo(make_user_client):
    """Admin client with the existing demo dataset seeded, cleaned afterwards."""
    client, headers, _ = make_user_client("ADMIN", prefix="locate")
    seed = client.post("/api/admin/demo/seed", headers=headers, json={"scenario": "all"})
    assert seed.status_code == 200, seed.text
    yield client, headers
    client.delete("/api/admin/demo/data", headers=headers)


def _land(client, headers, *, query="", survey=None, village=None):
    response = client.get("/api/land-records", headers=headers, params={"q": query, "limit": 100})
    assert response.status_code == 200, response.text
    records = response.json()["land_records"]
    for item in records:
        if survey is not None and str(item.get("survey")) != str(survey):
            continue
        if village is not None and str(item.get("village")) != village:
            continue
        return item
    return None


def _resolve(client, headers, **payload):
    response = client.post("/api/parcels/resolve", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# A. Polygon parcels: geometry + centroid + bounds + highlight URL
# ---------------------------------------------------------------------------
def test_a_polygon_land_resolves_to_geometry_centroid_and_bounds(demo):
    client, headers = demo
    land = _land(client, headers, query="Ambedarpur", survey="103")  # encumbered scenario parcel
    assert land is not None, "demo encumbrance parcel 103/Ambedarpur must exist"

    parcel = _resolve(client, headers, land_id=land["land_id"])
    assert parcel["matched"] is True
    assert parcel["located"] is True
    assert parcel["geometry"]["type"] == "Polygon"
    assert parcel["centroid"]["latitude"] and parcel["centroid"]["longitude"]
    sw, ne = parcel["bounds"]
    assert sw[0] < ne[0] and sw[1] < ne[1]
    assert sw[0] <= parcel["centroid"]["latitude"] <= ne[0]
    assert sw[1] <= parcel["centroid"]["longitude"] <= ne[1]
    # The map deep link carries every identifier the map page needs to highlight it.
    assert parcel["urls"]["map"].startswith("/map?locate=1")
    assert land["land_id"] in parcel["urls"]["map"]
    assert parcel["property_id"]
    assert parcel["location"]["state"] == "REFERENCE_GEOMETRY"
    assert parcel["location"]["label"] == "Located on map"
    # Reference geometry is never presented as an authoritative boundary.
    assert parcel["geometry_authoritative"] is False
    assert parcel["reference_only"] is True
    # The documented stable shape is exactly what the frontends consume.
    import land_intel

    assert set(parcel) == set(land_intel.PARCEL_RESPONSE_FIELDS)


def test_a_land_detail_exposes_the_same_canonical_parcel(demo):
    client, headers = demo
    land = _land(client, headers, query="Ambedarpur", survey="103")
    detail = client.get(f"/api/land-records/{land['land_id']}", headers=headers)
    assert detail.status_code == 200
    body = detail.json()
    parcel = body["parcel"]
    assert parcel["parcel_id"] == _resolve(client, headers, land_id=land["land_id"])["parcel_id"]
    assert body["map"]["url"] == parcel["urls"]["map"]
    assert body["map"]["url"].startswith("/map")
    assert body["map"]["location_state"] == parcel["location"]["state"]
    # Detail -> Land Intelligence link for the selected parcel.
    assert parcel["urls"]["land_intelligence"] == f"/?land_id={land['land_id']}"


# ---------------------------------------------------------------------------
# B. Coordinates-only parcels become markers (no fabricated polygon)
# ---------------------------------------------------------------------------
def test_b_point_only_parcel_resolves_to_coordinates(demo):
    client, headers = demo
    land = _land(client, headers, query="Barkheda", survey="204")
    assert land is not None
    parcel = _resolve(client, headers, land_id=land["land_id"])
    assert parcel["geometry"] is None
    assert parcel["location"]["state"] == "STORED_POINT"
    assert parcel["location"]["label"] == "Located on map"
    assert parcel["location"]["latitude"] and parcel["location"]["longitude"]
    assert parcel["centroid"]["latitude"] == parcel["location"]["latitude"]


# ---------------------------------------------------------------------------
# C. Missing coordinates invoke the resolver's fallback chain
# ---------------------------------------------------------------------------
def test_c_missing_coordinates_use_persisted_address_geocode(make_user_client):
    """A record with no stored geometry/point resolves from the cached geocode."""
    import server
    import mapping

    client, headers, _ = make_user_client("ADMIN", prefix="locale")
    token = "LCGEO"
    fields = {
        "owner_name": {"value": "Geocode Owner"}, "survey_number": {"value": "9001"},
        "khasra_number": {"value": "9001"}, "village": {"value": f"Geo Village {token}"},
        "tehsil": {"value": "Sadar"}, "district": {"value": "Locate District"},
        "state": {"value": "Locate State"},
    }
    doc_id = f"LOCATE-{token}-DOC"
    cache_key = mapping._normalise(f"Geo Village {token}, Sadar, Locate District, Locate State")
    with server.get_db() as db:
        db.execute(
            """INSERT OR REPLACE INTO documents
               (id,filename,doc_type,mean_conf,verdict,status,languages,pages,fields,validation,
                ai_decision_support,ocr_text,cleaned_ocr_text,detected_language,original_fields,
                uploaded_by,created_at,updated_at,lat,lon)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (doc_id, f"{doc_id}.pdf", "Land Record", 90, "review", "APPROVED", "[]", 1,
             json.dumps(fields), "{}", "{}", "", "", "eng", json.dumps(fields),
             "locate@test", time.time(), time.time(), None, None),
        )
        db.execute(
            """INSERT OR REPLACE INTO geocode_cache
               (cache_key,village,taluka,district,state,latitude,longitude,display_name,status,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (cache_key, f"Geo Village {token}", "Sadar", "Locate District", "Locate State",
             28.5123, 77.4123, "Geo Village, Locate District, Locate State", "RESOLVED", time.time()),
        )
    try:
        parcel = _resolve(client, headers, document_id=doc_id, persist=True)
        assert parcel["located"] is True
        assert parcel["geometry"] is None
        assert parcel["location"]["state"] == "GEOCODED"
        assert parcel["location"]["label"] == "Location resolved from address — verify"
        assert parcel["location"]["resolved_from_address"] is True
        assert round(parcel["location"]["latitude"], 4) == 28.5123
        assert round(parcel["location"]["longitude"], 4) == 77.4123
        # Approximate resolution must never be labelled authoritative.
        assert parcel["location"]["authoritative"] is False
        assert parcel["reference_only"] is True
    finally:
        with server.get_db() as db:
            db.execute("DELETE FROM documents WHERE id=?", (doc_id,))
            db.execute("DELETE FROM geocode_cache WHERE cache_key=?", (cache_key,))


def test_c_missing_coordinates_without_fallback_stay_explicit(demo):
    """No geometry, no point, no cached geocode -> explicit unresolved state."""
    client, headers = demo
    land = _land(client, headers, query="Ambedarpur", survey="209")  # S9: no spatial data
    assert land is not None
    parcel = _resolve(client, headers, land_id=land["land_id"])
    assert parcel["located"] is False
    assert parcel["location"]["state"] == "UNRESOLVED"
    assert parcel["location"]["label"] == "Location unavailable — no verified coordinates"
    assert parcel["location"]["can_geocode"] is True
    assert parcel["location"]["geocode_query"]
    assert parcel["quality"]["issues"], "an unresolved location must explain itself"
    assert land["location_state"] == "UNRESOLVED"
    assert land["located"] is False


def test_c_geocoded_location_is_persisted_as_approximate_parcel_metadata(make_user_client):
    """persist=true stores an address geocode on the parcel, still labelled approximate."""
    import server
    import mapping

    client, headers, _ = make_user_client("ADMIN", prefix="locateg")
    token = "LCGEO2"
    village = f"Persist Village {token}"
    cache_key = mapping._normalise(f"{village}, Sadar, Locate District, Locate State")
    fields = {
        "owner_name": {"value": "Persist Owner"}, "survey_number": {"value": "9101"},
        "khasra_number": {"value": "9101"}, "village": {"value": village},
        "tehsil": {"value": "Sadar"}, "district": {"value": "Locate District"},
        "state": {"value": "Locate State"},
    }
    doc_id = f"LOCATE-{token}-DOC"
    with server.get_db() as db:
        db.execute(
            """INSERT OR REPLACE INTO documents
               (id,filename,doc_type,mean_conf,verdict,status,languages,pages,fields,validation,
                ai_decision_support,ocr_text,cleaned_ocr_text,detected_language,original_fields,
                uploaded_by,created_at,updated_at,lat,lon)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (doc_id, f"{doc_id}.pdf", "Land Record", 90, "review", "APPROVED", "[]", 1,
             json.dumps(fields), "{}", "{}", "", "", "eng", json.dumps(fields),
             "locate@test", time.time(), time.time(), None, None),
        )
        db.execute(
            """INSERT OR REPLACE INTO properties
               (property_id,parcel_id,district,taluka,village,survey_number,gat_number,khasra_number,
                sub_division,parent_property_id,area,area_unit,geometry,centroid,latitude,longitude,
                crs,georeferenced,geometry_source,geometry_confidence,data_source,source_confidence,
                created_at,updated_at,location_status,location_source,location_confidence,
                location_base_latitude,location_base_longitude,location_base_source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"LOCATE-{token}-PROP", f"LOCATE-{token}-PARCEL", "Locate District", "Sadar", village,
             "9101", None, "9101", None, None, None, "ha", None, None, None, None,
             "EPSG:4326", 0, None, None, "Canonical locate test", 0.5, time.time(), time.time(),
             "UNRESOLVED", None, None, None, None, None),
        )
        db.execute(
            """INSERT OR REPLACE INTO geocode_cache
               (cache_key,village,taluka,district,state,latitude,longitude,display_name,status,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (cache_key, village, "Sadar", "Locate District", "Locate State",
             28.6011, 77.5011, f"{village}, Locate District, Locate State", "RESOLVED", time.time()),
        )
        db.execute(
            "INSERT OR REPLACE INTO property_documents(property_id,document_id,source_type,linked_at) VALUES (?,?,?,?)",
            (f"LOCATE-{token}-PROP", doc_id, "uploaded_document", time.time()),
        )
    try:
        parcel = _resolve(client, headers, document_id=doc_id, persist=True)
        assert parcel["location"]["state"] == "GEOCODED"
        assert parcel["location"]["resolved_from_address"] is True
        assert parcel["geometry"] is None

        with server.get_db() as db:
            row = db.execute(
                "SELECT latitude, longitude, location_status, location_source FROM properties WHERE property_id=?",
                (f"LOCATE-{token}-PROP",),
            ).fetchone()
            events = [item["event_type"] for item in db.execute(
                "SELECT event_type FROM property_timeline WHERE property_id=?", (f"LOCATE-{token}-PROP",)).fetchall()]
        assert row["location_status"] == "GEOCODED_ADDRESS"
        assert round(row["latitude"], 4) == 28.6011 and round(row["longitude"], 4) == 77.5011
        assert "ADDRESS_GEOCODE_STORED" in events

        # The persisted geocode is still reported as approximate, never verified.
        again = _resolve(client, headers, document_id=doc_id)
        assert again["location"]["state"] == "GEOCODED"
        assert again["location"]["label"] == "Location resolved from address — verify"
        assert again["location"]["verified"] is False
        assert again["location"]["authoritative"] is False
    finally:
        with server.get_db() as db:
            db.execute("DELETE FROM documents WHERE id=?", (doc_id,))
            db.execute("DELETE FROM properties WHERE property_id=?", (f"LOCATE-{token}-PROP",))
            db.execute("DELETE FROM property_documents WHERE property_id=?", (f"LOCATE-{token}-PROP",))
            db.execute("DELETE FROM property_timeline WHERE property_id=?", (f"LOCATE-{token}-PROP",))
            db.execute("DELETE FROM geocode_cache WHERE cache_key=?", (cache_key,))


def test_c_polygon_without_centroid_derives_and_persists_it(demo):
    """S5 stores geometry but no centroid/coordinates: the resolver derives them."""
    import server

    client, headers = demo
    land = _land(client, headers, query="Ambedarpur", survey="105")
    assert land is not None
    parcel = _resolve(client, headers, land_id=land["land_id"], persist=True)
    assert parcel["geometry"]["type"] == "Polygon"
    assert parcel["centroid"]["latitude"] and parcel["centroid"]["longitude"]
    assert parcel["location"]["state"] == "REFERENCE_GEOMETRY"

    with server.get_db() as db:
        row = db.execute(
            "SELECT centroid, latitude, longitude, location_status FROM properties WHERE property_id=?",
            (parcel["property_id"],),
        ).fetchone()
        timeline = db.execute(
            "SELECT event_type FROM property_timeline WHERE property_id=?", (parcel["property_id"],)
        ).fetchall()
    assert row["latitude"] == parcel["centroid"]["latitude"]
    assert row["longitude"] == parcel["centroid"]["longitude"]
    assert row["location_status"] == "PARCEL_GEOMETRY"
    assert any(item["event_type"] == "PARCEL_CENTROID_COMPUTED" for item in timeline)


# ---------------------------------------------------------------------------
# D. Duplicate survey numbers are scoped by village - never guessed
# ---------------------------------------------------------------------------
def test_d_duplicate_survey_number_across_villages_picks_the_right_parcel(make_user_client):
    import server

    client, headers, _ = make_user_client("ADMIN", prefix="locdup")
    now = time.time()
    documents = {}
    for suffix, village, lon in (("A", "Locate Alpha", 77.20), ("B", "Locate Beta", 77.30)):
        doc_id = f"LOCATE-DUP-{suffix}"
        fields = {
            "owner_name": {"value": f"Owner {suffix}"}, "survey_number": {"value": "777"},
            "khasra_number": {"value": "777"}, "village": {"value": village},
            "tehsil": {"value": "Sadar"}, "district": {"value": "Locate District"},
        }
        polygon = {"type": "Polygon", "coordinates": [[
            [lon, 28.70], [lon + 0.01, 28.70], [lon + 0.01, 28.71], [lon, 28.71], [lon, 28.70],
        ]]}
        with server.get_db() as db:
            db.execute(
                """INSERT OR REPLACE INTO documents
                   (id,filename,doc_type,mean_conf,verdict,status,languages,pages,fields,validation,
                    ai_decision_support,ocr_text,cleaned_ocr_text,detected_language,original_fields,
                    uploaded_by,created_at,updated_at,lat,lon)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (doc_id, f"{doc_id}.pdf", "Land Record", 90, "review", "APPROVED", "[]", 1,
                 json.dumps(fields), "{}", "{}", "", "", "eng", json.dumps(fields),
                 "locate@test", now, now, None, None),
            )
            db.execute(
                """INSERT OR REPLACE INTO properties
                   (property_id,parcel_id,district,taluka,village,survey_number,gat_number,khasra_number,
                    sub_division,parent_property_id,area,area_unit,geometry,centroid,latitude,longitude,
                    crs,georeferenced,geometry_source,geometry_confidence,data_source,source_confidence,
                    created_at,updated_at,location_status,location_source,location_confidence,
                    location_base_latitude,location_base_longitude,location_base_source)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"LOCATE-DUP-PROP-{suffix}", f"LOCATE-DUP-PARCEL-{suffix}", "Locate District", "Sadar",
                 village, "777", None, "777", None, None, None, "ha", json.dumps(polygon), None,
                 None, None, "EPSG:4326", 1, "Duplicate-survey test geometry", 0.8,
                 "Duplicate-survey test", 0.8, now, now, "PARCEL_GEOMETRY",
                 "Duplicate-survey test geometry", 0.8, None, None, None),
            )
            db.execute(
                "INSERT OR REPLACE INTO property_documents(property_id,document_id,source_type,linked_at) VALUES (?,?,?,?)",
                (f"LOCATE-DUP-PROP-{suffix}", doc_id, "uploaded_document", now),
            )
        documents[suffix] = doc_id

    try:
        # Locating the Alpha record must never return Beta's parcel, and vice versa.
        alpha = _resolve(client, headers, document_id=documents["A"])
        beta = _resolve(client, headers, document_id=documents["B"])
        assert alpha["village"] == "Locate Alpha"
        assert round(alpha["centroid"]["longitude"], 3) == 77.205
        assert beta["village"] == "Locate Beta"
        assert round(beta["centroid"]["longitude"], 3) == 77.305
        assert alpha["parcel_id"] != beta["parcel_id"]
        assert alpha["quality"]["survey_scope"] == "village-scoped"

        # Survey number alone is ambiguous across villages: report candidates, never guess.
        ambiguous = _resolve(client, headers, survey_number="777")
        assert ambiguous["located"] is False
        assert ambiguous["match_method"] == "ambiguous_survey"
        assert len(ambiguous["candidates"]) >= 2
        assert {item["village"] for item in ambiguous["candidates"]} >= {"Locate Alpha", "Locate Beta"}

        # A wrong village must not silently fall back to another village's parcel.
        wrong = _resolve(client, headers, survey_number="777", village="Locate Gamma")
        assert wrong["located"] is False
        assert wrong["match_method"] == "unresolved"
    finally:
        with server.get_db() as db:
            for doc_id in documents.values():
                db.execute("DELETE FROM documents WHERE id=?", (doc_id,))
            db.execute("DELETE FROM properties WHERE property_id LIKE 'LOCATE-DUP-PROP-%'")
            db.execute("DELETE FROM property_documents WHERE property_id LIKE 'LOCATE-DUP-PROP-%'")


# ---------------------------------------------------------------------------
# E. Unresolved location is a clear, non-silent state
# ---------------------------------------------------------------------------
def test_e_unresolved_records_report_their_state(make_user_client):
    import server

    client, headers, _ = make_user_client("ADMIN", prefix="locnone")
    doc_id = "LOCATE-NOLOC-DOC"
    fields = {"owner_name": {"value": "No Location"}, "survey_number": {"value": "4242"},
              "village": {"value": "Locate Nowhere"}, "district": {"value": "Locate District"}}
    with server.get_db() as db:
        db.execute(
            """INSERT OR REPLACE INTO documents
               (id,filename,doc_type,mean_conf,verdict,status,languages,pages,fields,validation,
                ai_decision_support,ocr_text,cleaned_ocr_text,detected_language,original_fields,
                uploaded_by,created_at,updated_at,lat,lon)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (doc_id, f"{doc_id}.pdf", "Land Record", 90, "review", "APPROVED", "[]", 1,
             json.dumps(fields), "{}", "{}", "", "", "eng", json.dumps(fields),
             "locate@test", time.time(), time.time(), None, None),
        )
    try:
        parcel = _resolve(client, headers, document_id=doc_id)
        assert parcel["matched"] is True
        assert parcel["located"] is False
        assert parcel["location"]["state"] == "UNRESOLVED"
        assert parcel["location"]["label"] == "Location unavailable — no verified coordinates"
        assert parcel["location"]["note"]
        # The record is still reachable: the map can show the record and offer
        # the explicit address-resolution step instead of failing silently.
        assert parcel["urls"]["map"].startswith("/map?locate=1")
        assert parcel["documents"][0]["id"] == doc_id
        assert parcel["quality"]["issues"]
    finally:
        with server.get_db() as db:
            db.execute("DELETE FROM documents WHERE id=?", (doc_id,))


# ---------------------------------------------------------------------------
# F. Switching records updates the canonical parcel selection
# ---------------------------------------------------------------------------
def test_f_switching_records_returns_a_different_canonical_parcel(demo):
    client, headers = demo
    first = _land(client, headers, query="Ambedarpur", survey="103")
    second = _land(client, headers, query="Barkheda", survey="204")
    assert first and second

    parcel_one = _resolve(client, headers, land_id=first["land_id"])
    parcel_two = _resolve(client, headers, land_id=second["land_id"])
    assert parcel_one["land_id"] != parcel_two["land_id"]
    assert parcel_one["parcel_id"] != parcel_two["parcel_id"]
    assert parcel_one["centroid"] != parcel_two["centroid"]
    # Re-selecting the first record restores the first parcel (no stale state).
    again = _resolve(client, headers, land_id=first["land_id"])
    assert again["parcel_id"] == parcel_one["parcel_id"]
    assert again["centroid"] == parcel_one["centroid"]
    assert again["geometry"]["coordinates"] == parcel_one["geometry"]["coordinates"]


# ---------------------------------------------------------------------------
# G. All Records search -> Locate targets that exact result
# ---------------------------------------------------------------------------
def test_g_search_result_locate_targets_the_exact_record(demo):
    client, headers = demo
    listing = client.get("/api/land-records", headers=headers,
                         params={"q": "Ambedarpur", "limit": 100}).json()["land_records"]
    target = next(item for item in listing if str(item["survey"]) == "207")
    detail = client.get(f"/api/land-records/{target['land_id']}", headers=headers).json()
    assert detail["property"]["survey"] == target["survey"]
    assert detail["property"]["village"] == "Ambedarpur"
    assert detail["map"]["land_id"] == target["land_id"]

    # The document-level entry point used by the All Records Locate button
    # resolves to the same land record.
    document_id = detail["documents"][-1]["id"]
    via_document = _resolve(client, headers, document_id=document_id, persist=True)
    assert via_document["land_id"] == target["land_id"]
    assert via_document["urls"]["map"] == detail["map"]["url"]


def test_g_all_records_search_still_works(demo):
    client, headers = demo
    response = client.get("/api/documents", headers=headers)
    assert response.status_code == 200
    documents = response.json()["documents"]
    assert any(str(item["id"]).startswith("DEMO-") for item in documents)
    listing = client.get("/api/land-records", headers=headers, params={"q": "45/2"})
    assert listing.status_code == 200
    assert listing.json()["total"] >= 1
    # Each search hit keeps its own land_id (Locate can target it individually).
    for item in listing.json()["land_records"]:
        assert item["land_id"].startswith("LR-")


# ---------------------------------------------------------------------------
# H. Land Intelligence detail opens from the selected parcel
# ---------------------------------------------------------------------------
def test_h_land_intelligence_detail_is_reachable_from_the_parcel(demo):
    client, headers = demo
    land = _land(client, headers, query="Barkheda", survey="45/2")
    parcel = _resolve(client, headers, land_id=land["land_id"])
    assert parcel["urls"]["land_intelligence"] == f"/?land_id={land['land_id']}"
    assert parcel["urls"]["record"].startswith("/?open_document=")
    detail = client.get(f"/api/land-records/{land['land_id']}", headers=headers)
    assert detail.status_code == 200
    body = detail.json()
    for section in ("property", "current_owner", "ownership_history", "mutations",
                    "encumbrances", "encumbrance_banner", "risk", "documents", "map", "audit"):
        assert section in body
    assert body["parcel"]["location"]["state"] in {
        "VERIFIED", "STORED_POINT", "REFERENCE_GEOMETRY", "GEOCODED", "UNRESOLVED",
    }
    assert body["parcel"]["disclaimer"]


def test_h_parcel_endpoint_matches_the_resolver(demo):
    client, headers = demo
    land = _land(client, headers, query="Ambedarpur", survey="201")
    direct = client.get(f"/api/land-records/{land['land_id']}/parcel", headers=headers)
    assert direct.status_code == 200
    body = direct.json()
    assert body["land_id"] == land["land_id"]
    assert body["located"] is True
    assert body["geometry"]["type"] == "Polygon"
    assert body["urls"]["map"] == _resolve(client, headers, land_id=land["land_id"])["urls"]["map"]


# ---------------------------------------------------------------------------
# I. Existing Land Intelligence flows keep working next to the locator
# ---------------------------------------------------------------------------
def test_i_mutations_encumbrances_and_risk_flows_still_work(demo):
    client, headers = demo
    mutations = client.get("/api/mutations", headers=headers)
    assert mutations.status_code == 200
    assert mutations.json()["total"] >= 3

    encumbrances = client.get("/api/encumbrances", headers=headers)
    assert encumbrances.status_code == 200
    assert encumbrances.json()["total"] >= 1

    encumbered = _land(client, headers, query="Ambedarpur", survey="103")
    assert encumbered["encumbrance_status"] == "ACTIVE"
    encumbered_detail = client.get(f"/api/land-records/{encumbered['land_id']}", headers=headers).json()
    assert encumbered_detail["risk"]["verdict"] == "HIGH_RISK"
    # Risk review still lists the demo verdicts.
    review = client.get("/api/land-records/risk-review", headers=headers, params={"limit": 100})
    assert review.status_code == 200
    listed = {item["land_id"]: item["risk"]["verdict"] for item in review.json()["land_records"]}
    assert listed.get(encumbered["land_id"]) == "HIGH_RISK"
    clean = _land(client, headers, query="Ambedarpur", survey="201")
    clean_detail = client.get(f"/api/land-records/{clean['land_id']}", headers=headers).json()
    assert clean_detail["risk"]["verdict"] == "CLEAR"
    # The locator does not disturb the risk/encumbrance surfaces.
    assert _resolve(client, headers, land_id=encumbered["land_id"])["located"] is True


def test_i_viewer_cannot_reach_staff_land_review_surfaces(make_user_client, demo):
    viewer, viewer_headers, _ = make_user_client("VIEWER", prefix="locatev")
    assert viewer.get("/api/land-records/risk-review", headers=viewer_headers).status_code == 403
    # Viewers can still locate an approved record through the read-only resolver.
    demo_client, demo_headers = demo
    listing = demo_client.get("/api/land-records", headers=demo_headers,
                              params={"q": "Ambedarpur", "limit": 100}).json()["land_records"]
    assert listing
    parcel = _resolve(viewer, viewer_headers, land_id=listing[0]["land_id"])
    assert "location" in parcel and parcel["urls"]["map"].startswith("/map")


# ---------------------------------------------------------------------------
# J. Demo parcels are locatable (polygon, point and unresolved demo cases)
# ---------------------------------------------------------------------------
EXPECTED_DEMO_LOCATIONS = {
    "201": ("Ambedarpur", "REFERENCE_GEOMETRY", "Polygon"),
    "45/2": ("Barkheda", "REFERENCE_GEOMETRY", "Polygon"),
    "103": ("Ambedarpur", "REFERENCE_GEOMETRY", "Polygon"),   # encumbered / high risk
    "204": ("Barkheda", "STORED_POINT", None),                # point-only
    "105": ("Ambedarpur", "REFERENCE_GEOMETRY", "Polygon"),   # conflicting history / high risk
    "106": ("Barkheda", "REFERENCE_GEOMETRY", "Polygon"),
    "207": ("Ambedarpur", "STORED_POINT", None),              # pending mutation
    "208": ("Barkheda", "REFERENCE_GEOMETRY", "Polygon"),     # rejected mutation
    "210": ("Barkheda", "REFERENCE_GEOMETRY", "Polygon"),
}


@pytest.mark.parametrize("survey", sorted(EXPECTED_DEMO_LOCATIONS, key=str))
def test_j_demo_parcels_are_locatable(demo, survey):
    client, headers = demo
    village, state, geometry_type = EXPECTED_DEMO_LOCATIONS[survey]
    land = _land(client, headers, query=village, survey=survey)
    assert land is not None, f"demo land {survey}/{village} is missing from the listing"
    assert land["located"] is True
    assert land["location_state"] == state

    parcel = _resolve(client, headers, land_id=land["land_id"], persist=True)
    assert parcel["matched"] is True
    assert parcel["located"] is True
    assert parcel["location"]["state"] == state
    assert parcel["location"]["label"] == "Located on map"
    if geometry_type:
        assert parcel["geometry"]["type"] == geometry_type
        assert parcel["bounds"] and parcel["centroid"]
    else:
        assert parcel["geometry"] is None
        assert parcel["location"]["latitude"] is not None
    assert parcel["village"] == village
    assert parcel["owner"]
    assert parcel["property_id"].startswith("DEMO-LI-")


def test_j_demo_unresolved_land_is_reported_and_cleaned_up(demo):
    import server

    client, headers = demo
    land = _land(client, headers, query="Ambedarpur", survey="209")
    assert land is not None
    parcel = _resolve(client, headers, land_id=land["land_id"])
    assert parcel["located"] is False
    assert parcel["location"]["state"] == "UNRESOLVED"

    # Wiping the demo dataset removes only demo spatial records.
    with server.get_db() as db:
        before = db.execute("SELECT COUNT(*) AS c FROM properties").fetchone()["c"]
    assert client.delete("/api/admin/demo/data", headers=headers).status_code == 200
    with server.get_db() as db:
        after = db.execute("SELECT COUNT(*) AS c FROM properties").fetchone()["c"]
        demo_left = db.execute("SELECT COUNT(*) AS c FROM properties WHERE property_id LIKE 'DEMO-LI-%'").fetchone()["c"]
    assert demo_left == 0
    assert after < before


# ---------------------------------------------------------------------------
# Front-end contract: the Locate action and the map deep link exist and agree
# ---------------------------------------------------------------------------
def test_frontend_map_consumes_the_parcel_deep_link():
    source = (ROOT / "map.js").read_text(encoding="utf-8")
    for needle in ("/api/parcels/resolve", "locateParcel", "applyDeepLink", "deepLinkParams",
                   "syncParcelForRecord", "parcelLocatorPanel", "Open Land Intelligence",
                   "drawParcel", "clearParcelLayer"):
        assert needle in source, f"map.js must implement the locate workflow ({needle})"
    for message in ("Located on map", "Location unavailable — no verified coordinates",
                    "Location resolved from address — verify"):
        assert message in source, f"map.js must show the required location message: {message}"
    assert "land_id" in source and "document_id" in source
    # The existing static-test literals must survive.
    assert "World_Imagery" in source
    assert "coordinatePair" in source


def test_frontend_records_rows_expose_locate():
    source = (ROOT / "js" / "app.js").read_text(encoding="utf-8")
    assert "function locateRecordOnMap" in source
    assert "data-locate-record" in source
    assert "/api/parcels/resolve" in source
    assert "renderStaffRecords" in source
    # All Records and the viewer catalog both render the action.
    assert source.count("data-locate-record") >= 2
    assert "data-locate-status" in source


def test_frontend_spa_deep_links_forward_to_the_map():
    source = (ROOT / "js" / "app.js").read_text(encoding="utf-8")
    assert "function openLandDeepLink" in source
    assert "wantsMap" in source
    assert "`/map?${target.toString()}`" in source
    # Land Intelligence deep link (?land_id) is preserved for staff roles.
    assert "window.LandIntel.openLand(landId)" in source


def test_frontend_land_intelligence_rows_expose_locate():
    source = (ROOT / "js" / "land-intel.js").read_text(encoding="utf-8")
    assert "function landMapUrl" in source
    assert "data-land-map" in source
    assert "locate', '1'" in source or 'locate\', \'1\'' in source or '"locate"' in source


def test_frontend_map_page_has_the_parcel_panel():
    html = (ROOT / "map.html").read_text(encoding="utf-8")
    for element in ("parcelLocatorPanel", "parcelLocatorState", "parcelLocatorGrid",
                    "parcelOpenLand", "parcelOpenRecord", "parcelResolveAddress", "parcelClear"):
        assert f'id="{element}"' in html, f"map.html is missing #{element}"
    # The existing show-mode contract must be untouched.
    assert 'id="mapShowMode"' in html
    assert '<option value="selected" selected>' in html
    assert '<option value="all">' in html
