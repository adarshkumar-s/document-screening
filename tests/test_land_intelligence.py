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

