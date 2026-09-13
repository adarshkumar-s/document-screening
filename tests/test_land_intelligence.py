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
