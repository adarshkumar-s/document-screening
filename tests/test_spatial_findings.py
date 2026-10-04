"""Spatial review-queue lifecycle tests (audit defects D1, D2, D3, D6).

Verified pre-fix behaviours these tests pin down:
* the queue 500'd whenever a finding's document had no linked property,
* resolved findings resurrected as new OPEN rows on the next fetch,
* foreign (ai_governance) findings were clobbered by a queue GET,
* the recorded-vs-boundary area mismatch rule could never fire.
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
    email = f"queue-{suffix}@example.test"
    password = "Strong Map Password 123!"
    assert client.post("/api/auth/signup", json={
        "full_name": "Queue Tester", "email": email, "password": password}).status_code == 200
    with server.get_db() as db:
        db.execute("UPDATE users SET role=? WHERE email=?", (role, email))
    login = client.post("/api/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200
    return {"Authorization": "Bearer " + login.json()["token"]}


def _insert_document(doc_id, *, survey="452", village="Sundarpur", lat=None, lon=None,
                     status="APPROVED", area="2.5 ha", owner="Ram Singh"):
    fields = {
        "owner_name": {"value": owner, "confidence": 0.95},
        "survey_number": {"value": survey, "confidence": 0.95},
        "area": {"value": area, "confidence": 0.9},
        "village": {"value": village, "confidence": 0.95},
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
             "queue@example.test", 1700000000.0, 1700000000.0, lat, lon),
        )


def _duplicate_pair(prefix):
    """Two records sharing ONE location unique to this test.

    Unique coordinates matter: findings group every record at the same pinned
    coordinates, so tests that share a coordinate would merge their evidence."""
    a, b = f"{prefix}-A-{uuid.uuid4().hex[:6]}", f"{prefix}-B-{uuid.uuid4().hex[:6]}"
    seed = int(uuid.uuid4().hex[:8], 16)
    lat = round(21.0 + (seed % 8000) / 1000.0, 6)      # 21.000 - 28.999
    lon = round(70.0 + ((seed // 8000) % 8000) / 1000.0, 6)  # 70.000 - 77.999
    _insert_document(a, lat=lat, lon=lon)
    _insert_document(b, lat=lat, lon=lon)
    return a, b


def test_review_queue_survives_findings_without_linked_property():
    """D1: pre-fix this exact scenario crashed with an unhandled IntegrityError
    because verification_findings.property_id is NOT NULL and the document was
    not linked to any reference parcel."""
    a, b = _duplicate_pair("Q-UNLINKED")
    headers = _make_user()
    response = client.get("/api/map/review-queue", headers=headers)
    assert response.status_code == 200
    findings = response.json()["findings"]
    duplicate = next(f for f in findings
                     if f["finding_type"] == "DUPLICATE_EXACT_LOCATION"
                     and set(f["evidence"]) == {a, b})
    assert duplicate["status"] == "OPEN"
    # Resolution works for unlinked findings too.
    resolved = client.post(f"/api/map/review-queue/{duplicate['id']}/resolve",
                           headers=headers, json={"status": "RESOLVED", "note": "intentional"})
    assert resolved.status_code == 200


def test_resolved_finding_does_not_resurrect_on_next_fetch():
    """D2: resolving used to be undone by the next queue GET."""
    a, b = _duplicate_pair("Q-PERSIST")
    headers = _make_user()
    first = client.get("/api/map/review-queue", headers=headers).json()
    duplicate = next(f for f in first["findings"]
                     if f["finding_type"] == "DUPLICATE_EXACT_LOCATION"
                     and set(f["evidence"]) == {a, b})
    assert client.post(f"/api/map/review-queue/{duplicate['id']}/resolve",
                       headers=headers, json={"status": "RESOLVED", "note": "checked"}).status_code == 200
    second = client.get("/api/map/review-queue", headers=headers).json()
    open_same = [f for f in second["findings"]
                 if f["finding_type"] == "DUPLICATE_EXACT_LOCATION"
                 and set(f["evidence"]) == {a, b}]
    assert open_same == [], "resolved finding must not return as a new open row"
    # The decision is still visible under the resolved filter, same id, with a note.
    resolved_view = client.get("/api/map/review-queue?status=resolved", headers=headers).json()
    kept = next(f for f in resolved_view["findings"] if f["id"] == duplicate["id"])
    assert kept["status"] == "RESOLVED"
    with server.get_db() as db:
        row = db.execute("SELECT status, resolution_note, resolved_by FROM verification_findings WHERE finding_id=?",
                         (duplicate["id"],)).fetchone()
    assert row["status"] == "RESOLVED"
    assert row["resolution_note"] == "checked"
    assert row["resolved_by"]


def test_queue_get_never_touches_foreign_findings():
    """D3: ai_governance verification-case findings must survive a queue GET."""
    _duplicate_pair("Q-FOREIGN")
    headers = _make_user()
    foreign_id = "AI-FGN-" + uuid.uuid4().hex[:8]
    with server.get_db() as db:
        db.execute(
            """INSERT INTO verification_findings
               (finding_id,property_id,case_id,finding_type,severity,status,title,evidence,created_by,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (foreign_id, "DEMO-PROP-103-A", "CASE-FGN", "AI_PROPOSAL", "REVIEW", "OPEN",
             "AI case finding", "{}", "admin", 1.0, 1.0),
        )
    assert client.get("/api/map/review-queue", headers=headers).status_code == 200
    with server.get_db() as db:
        row = db.execute("SELECT status FROM verification_findings WHERE finding_id=?", (foreign_id,)).fetchone()
    assert row["status"] == "OPEN", "map queue must not supersede findings it does not manage"
    # And foreign findings are not resolvable through the map endpoint.
    assert client.post(f"/api/map/review-queue/{foreign_id}/resolve", headers=headers,
                       json={"status": "RESOLVED"}).status_code == 404
    # Clean up so later tests in this module don't see it.
    with server.get_db() as db:
        db.execute("DELETE FROM verification_findings WHERE finding_id=?", (foreign_id,))


def test_recorded_area_mismatch_rule_actually_fires():
    """D6: the pre-fix code read a comparison key that never existed."""
    doc_id = "Q-AREA-" + uuid.uuid4().hex[:8]
    # Recorded 2.5 ha but a ~few-hundred-m² traced boundary: far beyond tolerance.
    _insert_document(doc_id, lat=28.62, lon=77.10, area="2.5 ha")
    headers = _make_user()
    assert client.put(f"/api/map/records/{doc_id}/boundary", headers=headers,
                      json={"points": [[28.6200, 77.1000], [28.6201, 77.1000], [28.6201, 77.1001]],
                            "reason": "traced"}).status_code == 200
    findings = client.get("/api/map/review-queue", headers=headers).json()["findings"]
    mismatch = next((f for f in findings
                     if f["finding_type"] == "RECORDED_AREA_MISMATCH" and doc_id in f["evidence"]), None)
    assert mismatch is not None, "recorded-vs-boundary mismatch must be detected"
    assert mismatch["severity"] == "WARNING"
    assert str(mapping.MAP_AREA_MISMATCH_PERCENT) in mismatch["reason"] or "25" in mismatch["reason"]


def test_area_mismatch_not_raised_for_aligned_geometry():
    doc_id = "Q-ALIGNED-" + uuid.uuid4().hex[:8]
    _insert_document(doc_id, lat=28.62, lon=77.10, area="1 ha")
    headers = _make_user()
    # ~0.0009° side at 28.6°N ≈ 100 m × 88 m ≈ 8800 m² within 25% of 10000 m²? No:
    # choose closer: 0.001° lat ≈ 111 m, 0.00108° lon ≈ 105 m => ~11660 m² (16.6%).
    assert client.put(f"/api/map/records/{doc_id}/boundary", headers=headers,
                      json={"points": [[28.6200, 77.1000], [28.6200, 77.10108], [28.6210, 77.10108], [28.6210, 77.1000]],
                            "reason": "aligned"}).status_code == 200
    findings = client.get("/api/map/review-queue", headers=headers).json()["findings"]
    assert not any(f["finding_type"] == "RECORDED_AREA_MISMATCH" and doc_id in f["evidence"]
                   for f in findings)


def test_finding_superseded_when_condition_disappears_and_history_kept():
    """Recompute closes findings whose data changed, without deleting history."""
    a, b = _duplicate_pair("Q-CLOSE")
    headers = _make_user()
    first = client.get("/api/map/review-queue", headers=headers).json()
    duplicate = next(f for f in first["findings"]
                     if f["finding_type"] == "DUPLICATE_EXACT_LOCATION" and set(f["evidence"]) == {a, b})
    # Break the condition: move one pin elsewhere.
    assert client.put(f"/api/map/records/{b}/location", headers=headers,
                      json={"lat": 29.5, "lon": 78.5}).status_code == 200
    second = client.get("/api/map/review-queue", headers=headers).json()
    assert not any(f["finding_type"] == "DUPLICATE_EXACT_LOCATION" and set(f["evidence"]) == {a, b}
                   for f in second["findings"])
    # The row still exists, closed with an explanatory note (auditable history).
    with server.get_db() as db:
        row = db.execute("SELECT status, resolution_note FROM verification_findings WHERE finding_id=?",
                         (duplicate["id"],)).fetchone()
    assert row["status"] == "SUPERSEDED"
    assert "no longer present" in row["resolution_note"]


def test_queue_filters_and_role_restrictions():
    a, b = _duplicate_pair("Q-FILTER")
    headers = _make_user()
    data = client.get("/api/map/review-queue", headers=headers).json()
    assert data["counts"]["open"] >= 1
    assert data["record_cap"] == mapping.MAP_RECORD_CAP
    only_dup = client.get("/api/map/review-queue?type=DUPLICATE_EXACT_LOCATION", headers=headers).json()
    assert {f["finding_type"] for f in only_dup["findings"]} == {"DUPLICATE_EXACT_LOCATION"}
    by_severity = client.get("/api/map/review-queue?severity=WARNING", headers=headers).json()
    assert all(f["severity"] == "WARNING" for f in by_severity["findings"])
    assert client.get("/api/map/review-queue?status=bogus", headers=headers).status_code == 400
    # Only verification officers and admins.
    officer = _make_user(role="DATA_OFFICER")
    assert client.get("/api/map/review-queue", headers=officer).status_code == 403
    viewer = _make_user(role="VIEWER")
    assert client.get("/api/map/review-queue", headers=viewer).status_code == 403
    # A cookie-free client must get 401 (the shared client carries sessions).
    fresh = TestClient(app, raise_server_exceptions=False)
    assert fresh.get("/api/map/review-queue").status_code == 401


def test_conflicts_endpoint_reports_duplicate_locations():
    a, b = _duplicate_pair("Q-CONF")
    headers = _make_user()
    response = client.get("/api/map/conflicts", headers=headers)
    assert response.status_code == 200
    assert any(set(item["record_ids"]) == {a, b} for item in response.json()["conflicts"])
    # Screening disclaimer preserved.
    assert "do not establish" in response.json()["disclaimer"]
