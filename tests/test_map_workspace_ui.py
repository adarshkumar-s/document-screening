"""Frontend mapping workspace tests (textual regressions + JS syntax).

Complements the opt-in Playwright suite (tests/test_map_browser.py): these run
in every environment and pin the audit-driven changes — stale-request
protection, concurrency guards, geometry provenance display, review-queue
status views, truncation disclosure, and legend additions.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAP_JS = (ROOT / "map.js").read_text(encoding="utf-8")
MAP_HTML = (ROOT / "map.html").read_text(encoding="utf-8")
MAP_CSS = (ROOT / "map.css").read_text(encoding="utf-8")


def test_map_js_has_stale_request_protection():
    """A slow older /api/map/records response must not overwrite newer state."""
    assert "state.recordsRequestId" in MAP_JS
    assert "requestId !== state.recordsRequestId" in MAP_JS
    # Loading state is distinguishable from an empty result.
    assert "Loading records…" in MAP_JS


def test_map_js_sends_expected_updated_at_for_writes():
    """Stale-edit protection is exercised by every write path in the UI."""
    assert MAP_JS.count("expected_updated_at: record.updated_at ?? null") >= 4
    assert "record.updated_at = result.updated_at ?? record.updated_at" in MAP_JS \
        or "record.updated_at = cleared.updated_at" in MAP_JS
    # Structured 409 detail objects are surfaced as readable messages.
    assert "detail.code" in MAP_JS or "error.code" in MAP_JS
    assert "typeof detail === 'object'" in MAP_JS


def test_boundary_provenance_and_history_are_visible():
    assert "Boundary source" in MAP_JS
    assert "openBoundaryHistory" in MAP_JS
    assert "data-boundary-history" in MAP_JS
    assert "/boundary/history" in MAP_JS
    assert "Screening geometry only" in MAP_JS
    # Import now captures a change reason.
    assert "Reason for this boundary import" in MAP_JS
    # History strings are escaped before rendering.
    history_block = MAP_JS.split("async function openBoundaryHistory")[1].split("async function loadParcelCandidates")[0]
    assert history_block.count("esc(") >= 5


def test_review_queue_exposes_status_views_and_counts():
    assert "data-queue-status" in MAP_JS
    assert "counts" in MAP_JS
    assert "openReviewQueue('open')" in MAP_JS
    assert "?status=" in MAP_JS
    # Non-open findings do not show resolve/dismiss buttons.
    assert "finding.status === 'OPEN'" in MAP_JS or "!finding.status || finding.status === 'OPEN'" in MAP_JS


def test_truncation_and_legend_indicators_exist():
    assert 'id="mapTruncatedHint"' in MAP_HTML
    assert "mapTruncatedHint" in MAP_JS
    assert "record_cap" in MAP_JS
    assert "Traced / imported / estimated boundary" in MAP_HTML
    assert ".legend-line" in MAP_CSS
    assert ".queue-status-tabs" in MAP_CSS
    # Existing legend and disclaimer strings must remain.
    assert "Exact reviewer pin" in MAP_HTML
    assert "Village-level approximate" in MAP_HTML
    assert "not a legal boundary or title determination" in MAP_HTML


def test_map_js_passes_syntax_check():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available in this environment")
    subprocess.run([node, "--check", str(ROOT / "map.js")], check=True, capture_output=True)
