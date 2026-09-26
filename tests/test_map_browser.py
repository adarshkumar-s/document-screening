"""Real Leaflet browser regressions with deterministic API/provider responses.

Run against `uvicorn main:app` with MAP_BROWSER_URL=http://127.0.0.1:8000.
Install playwright + Chromium; optionally set MAP_BROWSER_EXECUTABLE.
No production data or external geocoder/tile service is required.
"""
import json
import os
from urllib.parse import urlparse

import pytest

pytestmark = pytest.mark.skipif(not os.getenv("MAP_BROWSER_URL"), reason="Opt-in browser suite")

GEOMETRY = {"type": "Polygon", "coordinates": [[[77.10, 28.60], [77.11, 28.60],
            [77.11, 28.61], [77.10, 28.61], [77.10, 28.60]]]}
BASE = dict(status="APPROVED", doc_type="Land Record", village="Sundarpur", district="Ghaziabad",
            tehsil="Sadar", state="Uttar Pradesh", lat=None, lon=None, location_status="VILLAGE_LEVEL")
RECORDS = [dict(BASE, id="exact", owner="Exact Owner", survey="1", plot="1", lat=28.62, lon=77.12,
                location_status="EXACT_PIN"),
           dict(BASE, id="shape", owner="Shape Owner", survey="2", plot="2", reference_geometry=GEOMETRY,
                reference_property={"property_id": "parcel-2", "geometry_source": "Test reference"}),
           dict(BASE, id="village", owner="Village Owner", survey="3", plot="3"),
           dict(BASE, id="missing", owner="Missing Owner", survey="4", plot="4", village="", district="", location_status="UNRESOLVED")]


@pytest.fixture
def browser_page():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.getenv("MAP_BROWSER_EXECUTABLE") or None,
                                    args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        errors, requests = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script("""
            localStorage.setItem('documentScreeningMapTileSource', 'schematic');
            // Instrument the real Leaflet instance without exposing app internals in production.
            document.addEventListener('DOMContentLoaded', () => {
              const original = L.map;
              window.mapInstances = [];
              L.map = (...args) => { const map = original(...args); mapInstances.push(map); return map; };
            });
        """)

        def api(route):
            path = urlparse(route.request.url).path
            requests.append((path, route.request.method))
            status = 200
            if path == "/api/auth/me":
                data = {"user": {"role": "VERIFICATION_OFFICER", "full_name": "Map Tester"}}
            elif path == "/api/map/records":
                data = {"records": RECORDS, "metadata": {"summary": {"records": len(RECORDS)}}}
            elif path == "/api/map/properties":
                data = {"properties": [{"property_id": "parcel-2", "survey_number": "2", "geometry": GEOMETRY}]}
            elif path == "/api/map/geocode":
                data = {"lat": 28.65, "lon": 77.15, "source": "Test village geocode"}
            elif path.endswith("/history"):
                data = {"items": [], "summary": {}}
            elif path == "/api/land-records/test-land":
                data = {"map": {"focus_record_id": "shape"}}
            elif path == "/api/parcels/resolve":
                data = {"status": "RESOLVED", "parcel": {"property_id": "parcel-2", "survey_number": "2", "geometry": GEOMETRY}}
            else:
                status, data = 404, {"detail": "Unexpected test API request: " + path}
            route.fulfill(status=status, content_type="application/json", body=json.dumps(data))

        page.route("**/api/**", api)
        yield page, requests, errors
        browser.close()
        assert not errors, errors


def navigate(page, query=""):
    page.goto(os.environ["MAP_BROWSER_URL"] + "/map" + query)
    page.wait_for_function("document.querySelector('#recordCount').textContent.includes('4 document')")


def paths(page):
    return page.evaluate("mapInstances[0] ? Object.values(mapInstances[0]._layers).filter(l => l instanceof L.Path).length : 0")


def test_sheet_to_map_initializes_once_and_focuses_exact_pin(browser_page):
    page, requests, _ = browser_page
    navigate(page)
    assert not any(p == "/api/map/geocode" for p, _ in requests)
    page.locator('[data-plot-key="1"]').click()
    page.locator('[data-map-id="exact"]').click()
    page.wait_for_function("window.mapInstances?.length === 1 && mapInstances[0].getZoom() >= 14")
    assert paths(page) == 1
    assert page.locator("#mapShowMode").input_value() == "selected"
    assert page.locator("#mapLoadHint").is_hidden()
    assert abs(page.evaluate("mapInstances[0].getCenter().lat") - 28.62) < .01


def test_document_deep_link_draws_reference_and_filters_all_mode(browser_page):
    page, requests, _ = browser_page
    navigate(page, "?document_id=shape&locate=1")
    page.wait_for_function("window.mapInstances?.[0]?.getZoom() > 10")
    assert paths(page) == 1
    assert "REFERENCE GEOMETRY" in page.locator("#selectedRecordPanel").inner_text()
    assert not any(p == "/api/map/geocode" for p, _ in requests)
    page.locator("#mapShowMode").select_option("all")
    assert paths(page) == 2
    page.locator("#mapSearch").fill("Exact Owner")
    assert paths(page) == 1
    page.locator("#refreshBtn").click()
    page.wait_for_function("document.querySelector('#mapNotice').textContent.includes('refreshed')")
    assert page.evaluate("mapInstances.length") == 1
    assert paths(page) == 1


def test_selection_resolves_only_one_village_and_reuses_cache(browser_page):
    page, requests, _ = browser_page
    navigate(page, "?map=1")
    page.locator('[data-record-id="village"] .record-main').click()
    page.wait_for_function("document.querySelector('#mapNotice').textContent.includes('Approximate village location available')")
    assert paths(page) == 1
    page.locator('[data-record-id="exact"] .record-main').click()
    page.locator('[data-record-id="village"] .record-main').click()
    assert [p for p, _ in requests].count("/api/map/geocode") == 1
    assert page.locator("#pinLatitude").input_value() == ""
    assert "APPROXIMATE" in page.locator("#selectedRecordPanel").inner_text()


def test_unresolved_record_never_gets_null_island_pin(browser_page):
    page, requests, _ = browser_page
    navigate(page, "?open_record=missing")
    page.wait_for_function("document.querySelector('#selectedRecordPanel').textContent.includes('Missing Owner')")
    assert paths(page) == 0
    assert not any(p == "/api/map/geocode" for p, _ in requests)
    assert "Location not available" in page.locator("#mapNotice").inner_text()


@pytest.mark.parametrize("query", ["?land_id=test-land&locate=1", "?parcel=parcel-2&locate=1"])
def test_land_and_parcel_links_open_real_map(browser_page, query):
    page, _, _ = browser_page
    navigate(page, query)
    page.wait_for_function("window.mapInstances?.[0]?.getZoom() > 10")
    assert paths(page) == 1
    assert page.locator("#mapMapView").is_visible()
    assert page.locator("#mapShowMode").input_value() == "selected"


def test_unknown_record_link_reports_unavailable(browser_page):
    page, _, _ = browser_page
    navigate(page, "?document_id=not-visible")
    page.wait_for_function("document.querySelector('#mapNotice').textContent.includes('Location unavailable')")
    assert paths(page) == 0


def test_district_fallback_is_labelled_and_zoomed_as_approximate(browser_page):
    page, _, _ = browser_page
    page.route("**/api/map/geocode", lambda route: route.fulfill(
        json={"lat": 28.66, "lon": 77.44, "match_level": "district", "query": "Ghaziabad, India"}))
    navigate(page, "?document_id=village")
    page.wait_for_function("document.querySelector('#mapNotice').textContent.includes('Approximate district location available')")
    assert "APPROXIMATE — DISTRICT LOCATION" in page.locator("#selectedRecordPanel").inner_text()
    assert page.evaluate("mapInstances[0].getZoom()") == 10
    assert page.locator("#pinLatitude").input_value() == ""


def test_geocode_failure_is_visible_not_a_silent_click(browser_page):
    page, _, _ = browser_page
    page.route("**/api/map/geocode", lambda route: route.fulfill(
        json={"lat": None, "lon": None, "error": "Provider unavailable"}))
    navigate(page, "?document_id=village")
    page.wait_for_function("document.querySelector('#mapNotice').textContent.includes('Provider unavailable')")
    assert paths(page) == 0


def test_real_api_record_click_focuses_pin(make_user_client, insert_land_document):
    """Exercise actual auth, records API and Leaflet together (no API mocks)."""
    playwright = pytest.importorskip("playwright.sync_api")
    client, headers, email = make_user_client("DATA_OFFICER", prefix="map-browser")
    doc_id = insert_land_document(uploader=email, lat=28.62, lon=77.12)
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.getenv("MAP_BROWSER_EXECUTABLE") or None,
                                    args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page()
        login = page.request.post(os.environ["MAP_BROWSER_URL"] + "/api/auth/login",
                                  data={"email": email, "password": "Strong Land Password 123!"})
        assert login.ok, login.text()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script("localStorage.setItem('documentScreeningMapTileSource', 'schematic')")
        page.goto(os.environ["MAP_BROWSER_URL"] + "/map?document_id=" + doc_id)
        page.wait_for_function("document.querySelector('#selectedRecordPanel').textContent.includes('VERIFIED_LOCATION')")
        page.locator("#map .leaflet-popup-content").wait_for()
        assert "VERIFIED LOCATION" in page.locator("#map .leaflet-popup-content").inner_text()
        assert "28.6200000, 77.1200000" in page.locator("#selectedRecordPanel").inner_text()
        assert page.locator("#mapMapView").is_visible()
        assert not errors
        browser.close()


def test_land_intelligence_map_has_one_instance_and_click_locates(browser_page):
    page, requests, _ = browser_page
    for endpoint in ('land-records', 'mutations', 'encumbrances', 'court-cases'):
        page.route(f'**/api/{endpoint}?*', lambda route: route.fulfill(json={}))
    # The overview does not need live tiles to exercise Leaflet/record clicks.
    page.route('https://*.tile.openstreetmap.org/**', lambda route: route.abort())
    page.goto(os.environ['MAP_BROWSER_URL'] + '/land-intelligence')
    page.locator('[data-tab="map"]').click()
    page.locator('[data-locate-record="village"]').click()
    page.wait_for_function("document.querySelector('#message').textContent.includes('Approximate village location')")
    assert page.evaluate('mapInstances.length') == 1
    assert abs(page.evaluate('mapInstances[0].getCenter().lat') - 28.65) < .01
    assert [path for path, _ in requests].count('/api/map/geocode') == 1
    page.locator('[data-locate-record="exact"]').click()
    page.wait_for_function('Math.abs(mapInstances[0].getCenter().lat - 28.62) < .01')
    assert not any(path == '/api/land-records/exact' for path, _ in requests)
