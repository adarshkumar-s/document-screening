import json

from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def test_demo_health_is_credential_free():
    r = client.get('/api/demo-land/health')
    assert r.status_code == 200
    assert r.json()['credentials_required'] is False


def test_demo_geojson_is_synthetic():
    r = client.get('/api/demo-land/geojson')
    assert r.status_code == 200
    data = r.json()
    assert data['type'] == 'FeatureCollection'
    assert data['metadata']['authoritative'] is False
    assert len(data['features']) == 4
    assert all(f['properties']['parcel_id'].startswith('DEMO-') for f in data['features'])


def test_property_search_supports_survey_and_village():
    assert client.get('/api/demo-land/properties?q=DEMO-103').json()['total'] == 2
    assert client.get('/api/demo-land/properties?q=Demo%20Village').json()['total'] == 4


def test_reverse_map_document_link_is_demo_only():
    data = client.get('/api/demo-land/properties/DEMO-PROP-103-A').json()
    assert {d['id'] for d in data['documents']} == {'DEMO-DOC-7-12', 'DEMO-DOC-SALE'}
    assert all(d['property_id'] == data['property_id'] for d in data['documents'])


def test_area_difference_is_review_required():
    r = client.get('/api/demo-land/compare/DEMO-DOC-7-12/DEMO-PROP-103-A')
    assert r.status_code == 200
    data = r.json()
    assert data['overall_status'] == 'REVIEW REQUIRED'
    area = next(x for x in data['checks'] if x['field'] == 'area')
    assert area['difference'] == 0.09


def test_missing_demo_property_returns_error_payload():
    r = client.get('/api/demo-land/properties/DOES-NOT-EXIST')
    assert r.status_code == 200
    assert r.json()['error'] == 'Property not found'


def test_demo_scenarios_cover_core_investigation_paths():
    r = client.get('/api/demo-land/scenarios')
    assert r.status_code == 200
    ids = {x['id'] for x in r.json()['scenarios']}
    assert {'consistent', 'area-review', 'no-match', 'low-confidence', 'subdivision'} <= ids


def test_demo_no_match_does_not_fabricate_coordinates():
    r = client.get('/api/demo-land/scenario/no-match')
    assert r.status_code == 200
    data = r.json()
    assert data['property'] is None
    assert data['document']['fields']['survey_number'] == 'DEMO-999'


    
def test_authenticated_land_investigation_workflow():
    import server
    from land_intelligence import _ensure_tables
    _ensure_tables()
    auth = client.post("/api/auth/signup", json={
        "full_name":"Investigator","email":"investigator@example.test",
        "password":"Strong Test Password 123!"
    })
    assert auth.status_code == 200
    headers = {"Authorization": f"Bearer {auth.json()['token']}"}

    search = client.get("/api/land/search?q=DEMO-103", headers=headers)
    assert search.status_code == 200
    assert search.json()["results"]

    detail = client.get("/api/land/investigate/DEMO-PROP-103-A", headers=headers)
    assert detail.status_code == 200
    data = detail.json()
    assert data["property_id"] == "DEMO-PROP-103-A"
    assert data["provenance"]
    assert data["timeline"]
    assert "neighbors" in data

    resolution = client.get("/api/land/resolve/document/DEMO-DOC-7-12", headers=headers)
    assert resolution.status_code == 404


def test_land_resolution_explains_conflicts_and_missing_fields(tmp_path):
    import server
    from land_intelligence import _ensure_tables
    _ensure_tables()
    with server.get_db() as db:
        db.execute("INSERT OR REPLACE INTO properties (property_id,parcel_id,district,taluka,village,survey_number,gat_number,khasra_number,sub_division,parent_property_id,area,area_unit,geometry,centroid,latitude,longitude,crs,georeferenced,geometry_source,geometry_confidence,data_source,source_confidence,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("TEST-PROP","TEST-1","Demo District","Demo Taluka","Demo Village","TEST-1",None,None,None,None,1.0,"ha",None,None,None,None,"EPSG:4326",0,"Synthetic test",0.9,"Synthetic test",0.9,0,0))
    from land_intelligence import _resolve
    result = _resolve({"survey_number":{"value":"TEST-1","confidence":0.8},"district":{"value":"Other District","confidence":0.8}})
    assert result["status"] in {"NO MATCH","POSSIBLE MATCH"}
    assert "district" in result.get("conflicting_fields", []) or result["status"] == "NO MATCH"


def test_document_property_comparison_exposes_sources_and_confidence():
    import server
    from land_intelligence import _ensure_tables
    _ensure_tables()
    with server.get_db() as db:
        db.execute("INSERT OR IGNORE INTO documents (id,filename,doc_type,mean_conf,verdict,status,languages,pages,fields,validation,ai_decision_support,ocr_text,cleaned_ocr_text,detected_language,original_fields,uploaded_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("TEST-DOC","test.pdf","Land Record",90,"CONSISTENT","DRAFT","[]",1,
                    '{"survey_number":{"value":"DEMO-103","confidence":0.9},"village":{"value":"Demo Village","confidence":0.95},"area":{"value":"2.40 ha","confidence":0.8}}',
                    '{}','{}','','','eng','{}','investigator@example.test',0,0))
    auth = client.post("/api/auth/login", json={"email":"investigator@example.test","password":"Strong Test Password 123!"})
    assert auth.status_code == 200
    headers = {"Authorization": f"Bearer {auth.json()['token']}"}
    r = client.get("/api/land/compare/TEST-DOC/DEMO-PROP-103-A", headers=headers)
    assert r.status_code == 200
    checks = r.json()["checks"]
    assert {x["field"] for x in checks} >= {"district","taluka","village","survey_number","gat_number","khasra_number","sub_division","area"}
    assert all("source" in x and "confidence" in x for x in checks)


def test_geojson_import_rejects_unsupported_crs():
    import server
    from fastapi.testclient import TestClient
    from main import app
    auth = client.post("/api/auth/signup", json={
        "full_name":"GIS Officer","email":"gis@example.test",
        "password":"Strong Test Password 123!"
    })
    assert auth.status_code == 200
    headers = {"Authorization": f"Bearer {auth.json()['token']}"}
    payload = '{"type":"FeatureCollection","crs":{"type":"name","properties":{"name":"EPSG:3857"}},"features":[]}'
    r = client.post("/api/land/import-geojson", headers=headers, files={"file":("bad.geojson",payload,"application/geo+json")})
    assert r.status_code == 422


def test_land_intelligence_ui_route_serves_html():
    r = client.get("/land-intelligence")
    assert r.status_code == 200
    assert "LAND INTELLIGENCE" in r.text
    assert 'href="/land-intelligence.css"' in r.text
    assert 'src="/land-intelligence.js"' in r.text
    assert "leaflet@1.9.4/dist/leaflet.css" in r.text
    assert "leaflet@1.9.4/dist/leaflet.js" in r.text


def test_land_intelligence_css_route_serves_css_not_html():
    r = client.get("/land-intelligence.css")
    assert r.status_code == 200
    assert r.headers["content-type"].split(";", 1)[0] == "text/css"
    assert ":root{" in r.text
    assert ".workspace{" in r.text
    assert "<html" not in r.text.lower()
    assert "LAND INTELLIGENCE" not in r.text


def test_land_intelligence_js_route_serves_javascript_not_html():
    r = client.get("/land-intelligence.js")
    assert r.status_code == 200
    assert r.headers["content-type"].split(";", 1)[0] == "application/javascript"
    assert '"use strict"' in r.text
    assert "function initMap()" in r.text
    assert "<html" not in r.text.lower()
    assert "LAND INTELLIGENCE" not in r.text


def test_land_intelligence_assets_are_available_from_canonical_server_app():
    import server
    from fastapi.testclient import TestClient

    server_client = TestClient(server.app)
    html = server_client.get("/land-intelligence")
    css = server_client.get("/land-intelligence.css")
    js = server_client.get("/land-intelligence.js")

    assert html.status_code == 200
    assert css.status_code == 200
    assert js.status_code == 200
    assert css.headers["content-type"].split(";", 1)[0] == "text/css"
    assert js.headers["content-type"].split(";", 1)[0] == "application/javascript"
    assert ":root{" in css.text
    assert '"use strict"' in js.text


def test_land_intelligence_css_matches_repository_file():
    import server
    from pathlib import Path
    from main import BASE_DIR

    r = client.get("/land-intelligence.css")
    expected = Path(BASE_DIR, "land-intelligence.css").read_text(encoding="utf-8")
    assert r.text == expected


def test_land_intelligence_js_matches_repository_file():
    import server
    from pathlib import Path
    from main import BASE_DIR

    r = client.get("/land-intelligence.js")
    expected = Path(BASE_DIR, "land-intelligence.js").read_text(encoding="utf-8")
    assert r.text == expected


def test_land_intelligence_route_table_has_one_ui_asset_route_each():
    import server

    paths = [route.path for route in server.app.routes]
    assert paths.count("/land-intelligence") == 1
    assert paths.count("/land-intelligence.css") == 1
    assert paths.count("/land-intelligence.js") == 1


def _ownership_doc(doc_id, owner, year, survey="452", khasra="77", village="Sundarpur", area="2.5", doc_type="Land Record", status="PENDING_VERIFICATION", ocr_text=""):
    return {
        "id": doc_id, "filename": doc_id + ".txt", "doc_type": doc_type, "status": status,
        "ocr_text": ocr_text,
        "fields": {
            "owner_name": {"value": owner, "confidence": 0.95},
            "survey_number": {"value": survey, "confidence": 0.95},
            "khasra_number": {"value": khasra, "confidence": 0.95},
            "village": {"value": village, "confidence": 0.95},
            "area": {"value": area, "confidence": 0.95},
            "khatauni_year": {"value": str(year), "confidence": 0.95},
        },
    }


def test_ownership_transfer_without_mutation_is_review_not_conflict():
    from land_intelligence import analyze_ownership_history
    result = analyze_ownership_history([
        _ownership_doc("A-2019", "Ram Bahadur Singh", 2019),
        _ownership_doc("B-2023", "Kamla Devi Singh", 2023),
    ])
    assert result["findings"][0]["type"] == "TRANSFER_CANDIDATE"
    assert result["findings"][0]["severity"] == "WARNING"
    assert result["findings"][0]["human_action"] == "Verification required"


def test_ownership_transfer_with_mutation_is_supported_but_not_approved():
    from land_intelligence import analyze_ownership_history
    result = analyze_ownership_history([
        _ownership_doc("A-2019", "Ram Bahadur Singh", 2019),
        _ownership_doc("M-2021", "Ram Bahadur Singh", 2021, doc_type="Mutation Record", ocr_text="Mutation transferred from Ram Bahadur Singh to Kamla Devi Singh"),
        _ownership_doc("B-2023", "Kamla Devi Singh", 2023),
    ])
    assert any(f["type"] == "TRANSFER_SUPPORTED" for f in result["findings"])
    assert result["legal_authority"] is False
    assert all(f["human_action"] == "Verification required" for f in result["findings"])


def test_same_period_different_owner_is_conflict():
    from land_intelligence import analyze_ownership_history
    result = analyze_ownership_history([
        _ownership_doc("A-2019", "Ram Bahadur Singh", 2019),
        _ownership_doc("C-2019", "Mahesh Verma", 2019),
    ])
    assert result["findings"][0]["type"] == "OWNERSHIP_CONFLICT"
    assert result["findings"][0]["severity"] == "ERROR"


def test_same_survey_different_village_is_not_ownership_conflict():
    from land_intelligence import analyze_ownership_history
    result = analyze_ownership_history([
        _ownership_doc("A", "Ram Bahadur Singh", 2019, village="Sundarpur"),
        _ownership_doc("B", "Kamla Devi Singh", 2023, village="Pipariya"),
    ])
    assert result["findings"] == []
    assert result["relationships"] == []


def test_ocr_owner_variation_is_conservative_duplicate_signal():
    from land_intelligence import analyze_ownership_history
    result = analyze_ownership_history([
        _ownership_doc("A", "Ram Bahadur Singh", 2019),
        _ownership_doc("B", "Ram Bahadur Sing", 2020),
    ])
    assert result["findings"][0]["type"] == "POSSIBLE_DUPLICATE_OCR_VARIATION"
    assert "OCR variation" in result["findings"][0]["reason"]


def test_partition_candidate_is_not_treated_as_duplicate():
    from land_intelligence import analyze_ownership_history
    result = analyze_ownership_history([
        _ownership_doc("B-2021", "Kamla Devi Singh", 2021, khasra="77", area="2.5"),
        _ownership_doc("P-2022", "Kamla Devi Singh", 2022, khasra="77/1", area="1.25"),
    ])
    assert result["findings"][0]["type"] == "PARTITION_CANDIDATE"
    assert result["relationships"][0]["relationship_type"] == "POSSIBLE_PREDECESSOR"


def test_indic_digits_and_cadastral_separators_are_safe():
    from land_intelligence import _land_number
    assert _land_number("४५२") == "452"
    assert _land_number("45/2") == "45/2"
    assert _land_number("452") != _land_number("45/2")


def test_unverified_reference_is_explicit_in_history_event():
    from land_intelligence import analyze_ownership_history
    result = analyze_ownership_history([
        _ownership_doc("A", "Ram Bahadur Singh", 2019, status="PENDING_VERIFICATION"),
        _ownership_doc("B", "Kamla Devi Singh", 2023, status="PENDING_VERIFICATION"),
    ])
    assert result["events"][0]["verification_status"] == "PENDING_VERIFICATION"
    assert result["findings"][0]["human_action"] == "Verification required"
