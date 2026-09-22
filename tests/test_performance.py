"""Performance regression tests (restored without the reverted middleware).

These tests measure what the audit actually flagged:
- request paths must never issue DDL;
- land endpoints must not issue more queries as the portfolio grows;
- document lists must not ship OCR payloads;
- responses must stay small enough for the admin queues.

Pure query-count assertions via the ``query_counter`` fixture — no timing
flakiness, no external dependencies.
"""
import time
import uuid

import pytest


def _survey(prefix="4"):
    return f"{prefix}{uuid.uuid4().hex[:5]}"


def _reset(state):
    state["queries"] = 0
    state["ddl"] = 0
    state["sql"] = []


def _count_containing(state, fragment):
    return sum(1 for statement in state["sql"] if fragment in statement)


def _land_id(client, headers, survey):
    rows = client.get("/api/land-records", headers=headers, params={"q": survey}).json()["land_records"]
    assert rows, f"parcel {survey} missing"
    return rows[0]["land_id"]


def test_no_ddl_in_land_request_paths(query_counter, make_user_client, insert_land_document):
    admin, headers, _ = make_user_client("ADMIN", prefix="perf1")
    survey = _survey()
    insert_land_document(survey=survey, village="Perfville")

    _reset(query_counter)
    admin.get("/api/land-records", headers=headers, params={"limit": 5})
    admin.get("/api/land-records/risk-review", headers=headers, params={"limit": 5})
    land_id = _land_id(admin, headers, survey)
    admin.get(f"/api/land-records/{land_id}", headers=headers)
    admin.post(f"/api/land-records/{land_id}/due-diligence", headers=headers)
    admin.get("/api/documents", headers=headers)
    admin.get("/api/audit", headers=headers)

    assert query_counter["ddl"] == 0, [sql[:60] for sql in query_counter["sql"] if sql.startswith(("CREATE", "ALTER"))]


def test_land_records_index_query_count_is_constant(query_counter, make_user_client,
                                                    insert_land_document, insert_court_case):
    admin, headers, _ = make_user_client("ADMIN", prefix="perf2")

    for index in range(5):
        insert_land_document(survey=_survey(), village=f"Bench{index}", owner=f"Owner {index}")
    insert_court_case(_survey(), "Bench0", case_type="CIVIL")

    _reset(query_counter)
    admin.get("/api/land-records", headers=headers, params={"limit": 100})
    few = query_counter["queries"]
    _reset(query_counter)
    admin.get("/api/land-records/risk-review", headers=headers, params={"limit": 100})
    few_risk = query_counter["queries"]

    for index in range(5, 10):
        insert_land_document(survey=_survey(), village=f"Bench{index}", owner=f"Owner {index}")

    _reset(query_counter)
    admin.get("/api/land-records", headers=headers, params={"limit": 100})
    many = query_counter["queries"]
    _reset(query_counter)
    admin.get("/api/land-records/risk-review", headers=headers, params={"limit": 100})
    many_risk = query_counter["queries"]

    assert many == few
    assert many_risk == few_risk
    assert many_risk <= 12


def test_document_and_audit_lists_stay_light(query_counter, make_user_client, insert_land_document):
    import server

    admin, headers, _ = make_user_client("ADMIN", prefix="perf3")
    for index in range(8):
        insert_land_document(survey=_survey(), village=f"Light{index}", owner=f"Owner {index}",
                             ocr_text=("block " * 2000) + f" unique-{index}")

    marker = f"perfmarker-{uuid.uuid4().hex[:6]}"
    for index in range(8):
        server.log_audit("perf@example.test", f"PERF_TEST_{index}", f"{marker} row {index}", None)
    time.sleep(0.01)

    _reset(query_counter)
    listing = admin.get("/api/documents", headers=headers, params={"limit": 100})
    audit_page = admin.get("/api/audit", headers=headers, params={"limit": 10, "q": marker})

    assert listing.status_code == 200
    assert len(listing.content) < 200_000, f"documents payload too large: {len(listing.content)}B"
    body = listing.json()
    assert all("ocr_text" not in row for row in body["documents"])

    assert audit_page.status_code == 200
    assert len(audit_page.content) < 50_000, f"audit payload too large: {len(audit_page.content)}B"
    assert audit_page.json()["total"] >= 8
    assert _count_containing(query_counter, "SELECT * FROM AUDIT") == 0  # never selects *


def test_mapping_uses_light_projection(query_counter, make_user_client, insert_land_document):
    admin, headers, _ = make_user_client("ADMIN", prefix="perf4")
    survey = _survey()
    insert_land_document(survey=survey, village="Mapville",
                         ocr_text=("heavy ocr " * 1500) + f" marker-{survey}")

    _reset(query_counter)
    response = admin.get("/api/map/records", headers=headers)
    assert response.status_code == 200
    summary = admin.get("/api/map/summary", headers=headers)
    assert summary.status_code == 200

    # the map projection must not drag the full rows (OCR payloads) through
    assert _count_containing(query_counter, "SELECT * FROM DOCUMENTS") == 0
    assert len(response.content) < 300_000
