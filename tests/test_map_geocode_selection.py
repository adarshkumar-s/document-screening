"""Structured record-click geocoding with bounded, labelled fallbacks."""
import json
import uuid
from urllib.parse import parse_qs, urlparse

import pytest
import mapping


@pytest.fixture
def geocoder(monkeypatch):
    calls = []
    replies = []
    class Response:
        def __init__(self, data): self.data = data
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return json.dumps(self.data).encode()
    def urlopen(request, timeout=10):
        params = parse_qs(urlparse(request.full_url).query)
        calls.append(params)
        value = replies.pop(0) if replies else []
        if isinstance(value, Exception):
            raise value
        return Response(value)
    monkeypatch.setattr(mapping.urllib.request, 'urlopen', urlopen)
    monkeypatch.setattr(mapping.time, 'sleep', lambda _: None)
    mapping._map_geocode_cache.clear()
    yield calls, replies
    mapping._map_geocode_cache.clear()


def candidate(state='Uttar Pradesh', district='Ghaziabad', lat='28.65', lon='77.15'):
    return dict(lat=lat, lon=lon, display_name=f'Test Village, {district}, {state}, India',
                address={'state': state, 'county': district})


def test_record_click_retries_without_tehsil_then_caches(make_user_client, geocoder):
    client, headers, _ = make_user_client()
    calls, replies = geocoder
    replies.extend([[], [candidate()]])
    payload = dict(village='Village '+uuid.uuid4().hex, tehsil='Sadar', district='Ghaziabad', state='Uttar Pradesh')
    response = client.post('/api/map/geocode', headers=headers, json=payload)
    assert response.status_code == 200
    assert response.json()['match_level'] == 'village'
    assert response.json()['lat'] == 28.65
    assert 'Sadar' in calls[0]['q'][0] and 'Sadar' not in calls[1]['q'][0]
    assert calls[0]['addressdetails'] == ['1']
    assert client.post('/api/map/geocode', headers=headers, json=payload).json()['cached'] is True
    assert len(calls) == 2


def test_district_fallback_is_not_labelled_as_village(make_user_client, geocoder):
    client, headers, _ = make_user_client()
    calls, replies = geocoder
    district = 'District '+uuid.uuid4().hex
    replies.extend([[], [], [], [candidate(district=district)]])
    response = client.post('/api/map/geocode', headers=headers, json={
        'village': 'Unfindable village', 'district': district, 'state': 'Uttar Pradesh'})
    data = response.json()
    assert data['match_level'] == 'district'
    assert data['approximate'] is True
    assert len(calls) == 4
    assert calls[-1]['q'] == [f'{district}, Uttar Pradesh, India']


def test_rejects_wrong_state_and_invalid_coordinates(make_user_client, geocoder):
    client, headers, _ = make_user_client()
    _, replies = geocoder
    district = 'District '+uuid.uuid4().hex
    replies.append([candidate(state='Haryana', district=district),
                    candidate(district=district, lat='999'), candidate(district=district, lat='NaN')])
    data = client.post('/api/map/geocode', headers=headers, json={
        'district': district, 'state': 'Uttar Pradesh'}).json()
    assert data['lat'] is None and data['lon'] is None
    assert data['match_level'] == 'unresolved'


def test_offline_click_stops_and_can_retry(make_user_client, geocoder):
    client, headers, _ = make_user_client()
    calls, replies = geocoder
    payload = dict(village='Village '+uuid.uuid4().hex, district='Ghaziabad', state='Uttar Pradesh')
    replies.append(OSError('offline'))
    result = client.post('/api/map/geocode', headers=headers, json=payload).json()
    assert result['unavailable'] is True
    assert len(calls) == 1
    replies.append([candidate()])
    assert client.post('/api/map/geocode', headers=headers, json=payload).json()['lat'] == 28.65
    assert len(calls) == 2


def test_structured_geocode_requires_a_place(make_user_client):
    client, headers, _ = make_user_client()
    for payload in ({}, {'state': 'Haryana'}, {'village': 'x' * 201}):
        assert client.post('/api/map/geocode', headers=headers, json=payload).status_code == 400
