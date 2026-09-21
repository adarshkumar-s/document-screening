"""Phase 14 — MAP tests: show mode (Selected record / All records), the
resilient tile fallback sequence, and preservation of existing behaviour
(filters, neighbouring plots, exact/approximate locations, disclaimers)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAP_JS = (ROOT / "map.js").read_text(encoding="utf-8")
MAP_HTML = (ROOT / "map.html").read_text(encoding="utf-8")
MAP_CSS = (ROOT / "map.css").read_text(encoding="utf-8")


def test_show_mode_control_exists_with_selected_default():
    assert 'id="mapShowMode"' in MAP_HTML
    assert '<option value="selected" selected>● Selected record</option>' in MAP_HTML
    assert '<option value="all">○ All records</option>' in MAP_HTML
    assert "showMode: 'selected'" in MAP_JS  # default is Selected record
    assert "function setShowMode" in MAP_JS
    assert "updateShowModeStatus" in MAP_JS


def test_selected_mode_shows_only_selected_record():
    assert "function showModeRecords" in MAP_JS
    # In selected mode with no selection there are no markers at all.
    assert "state.showMode !== 'selected'" in MAP_JS
    assert "if (!state.selectedId) return { records: [], emphasizedId: null }" in MAP_JS
    # The selected marker is drawn emphasized.
    assert "emphasized" in MAP_JS
    assert "map-marker-selected" in MAP_JS


def test_selecting_a_record_renders_markers():
    # selectRecord() triggers renderMarkers() so the show-mode redraw happens.
    function_body = MAP_JS.split("function selectRecord(id)")[1].split("\n  }")[0]
    assert "renderMarkers()" in function_body


def test_all_records_mode_keeps_filters_and_neighbours():
    # The filtered list still feeds the markers in 'all' mode.
    assert "const filtered = filteredMapRecords();" in MAP_JS
    # Existing filters untouched.
    assert 'id="mapLocationFilter"' in MAP_HTML
    assert 'id="mapStatusFilter"' in MAP_HTML
    assert 'id="mapSearch"' in MAP_HTML
    # Neighbouring plot behaviour untouched.
    assert "data-nearby-plot" in MAP_JS
    # Exact / approximate / unresolved handling untouched.
    assert "APPROXIMATE — VILLAGE LOCATION" in MAP_JS
    assert "VERIFIED LOCATION" in MAP_JS
    assert "location_status !== location" in MAP_JS


def test_resilient_tile_fallback_sequence():
    assert "TILE_FALLBACK_ORDER = ['osmhot', 'osmde', 'esri', 'topo', 'osm', 'schematic']" in MAP_JS
    # The mirror is a real tile source definition (no SDK dependency added).
    assert "openstreetmap.de" in MAP_JS
    assert "osmde" in MAP_HTML
    # Offline schematic remains the terminal fallback.
    assert MAP_JS.index("'osmde'") < MAP_JS.index("'schematic']")
    # setTileSource validates against the full allowed list.
    assert "['osmhot', 'osmde', 'esri', 'topo', 'osm', 'schematic'].includes(source)" in MAP_JS


def test_non_authoritative_disclaimers_preserved():
    assert "not an authoritative cadastral record" in MAP_HTML or "Non-authoritative cadastral reference" in MAP_HTML
    assert "not a legal boundary or title determination" in MAP_HTML
    # Exact/approximate legend stays.
    assert "Exact reviewer pin" in MAP_HTML
    assert "Village-level approximate" in MAP_HTML


def test_show_mode_styles_added():
    assert ".show-mode-picker" in MAP_CSS
    assert "#mapShowModeStatus" in MAP_CSS


def test_map_assets_route_unchanged():
    import re

    server_py = (ROOT / "server.py").read_text(encoding="utf-8")
    assert '@app.get("/map", include_in_schema=False)' in server_py
    assert '@app.get("/map.js", include_in_schema=False)' in server_py
