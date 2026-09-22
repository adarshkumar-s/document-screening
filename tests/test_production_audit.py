"""FULL PRODUCTION AUDIT regression tests.

Covers the defects found during the audit of the deployed application:
- the corrupted admin_assistant source that stopped the app from booting;
- the missing favicon route;
- duplicate-email signup / user creation returning 500 instead of 409;
- demo seeding colliding with the mutation register's UNIQUE mutation_no
  (used to fail with 500 and leave a partially seeded dataset);
- request paths executing DDL (request-time CREATE TABLE / CREATE INDEX);
- N+1 register queries on the land endpoints;
- unbounded audit / document list responses and OCR payloads in lists.
"""
import io
import time
import uuid

import pytest


def _survey(prefix="8"):
    return f"{prefix}{uuid.uuid4().hex[:5]}"


def _reset(state):
    state["queries"] = 0
    state["ddl"] = 0
    state["sql"] = []


def _count_containing(state, fragment):
    return sum(1 for statement in state["sql"] if fragment in statement)


def _land_id(client, headers, survey):
    rows = client.get("/api/land-records", headers=headers, params={"q": survey}).json()["land_records"]
    matches = [row for row in rows if row["survey"] == survey]
    assert matches, f"parcel {survey} missing"
    return matches[0]["land_id"]


# ------------------------------------------------------------ boot / assets --

def test_application_modules_import_cleanly():
    """The deployed app once shipped a corrupted admin_assistant.py (literal
    \\n inside a list literal) that made every import fail."""
    import admin_assistant  # noqa: F401
    import main  # noqa: F401
    import sa_agent  # noqa: F401


def test_favicon_route_serves_the_tracked_asset(make_user_client):
    client, headers, _ = make_user_client("VIEWER", prefix="fav")
    response = client.get("/favicon.svg")
    assert response.status_code == 200
    assert "image/svg" in response.headers["content-type"]


# ------------------------------------------------------------------- auth ----

def test_duplicate_email_signup_is_409_not_500(make_user_client):
    from fastapi.testclient import TestClient
    from main import app

    client = TestClient(app)
    email = f"dup-{uuid.uuid4().hex[:8]}@example.test"
    first = client.post("/api/auth/signup",
                        json={"full_name": "First", "email": email, "password": "Strong Pass 123!"})
    assert first.status_code == 200
    second = client.post("/api/auth/signup",
                         json={"full_name": "Second", "email": email.upper(), "password": "Another Pass 123!"})
    assert second.status_code == 409, second.text
    assert "already exists" in second.json()["detail"]


def test_duplicate_email_add_user_is_409_not_500(make_user_client):
    admin, headers, _ = make_user_client("ADMIN", prefix="addu")
    email = f"created-{uuid.uuid4().hex[:8]}@example.test"
    first = admin.post("/api/users", headers=headers,
                       json={"full_name": "Created", "email": email, "password": "Strong Pass 123!",
                             "role": "VIEWER"})
    assert first.status_code == 200
    second = admin.post("/api/users", headers=headers,
                        json={"full_name": "Again", "email": email, "password": "Strong Pass 123!",
                              "role": "VIEWER"})
    assert second.status_code == 409, second.text


# ------------------------------------------------------------ demo safety ----

def test_demo_seed_survives_a_real_mutation_number_collision(make_user_client):
    """A real mutation holding a demo's mutation_no must not break seeding
    (500 + partially seeded dataset) and must never be modified."""
    import server

    client, headers, _ = make_user_client("ADMIN", prefix="democ")
    client.delete("/api/admin/demo/data", headers=headers)
    try:
        with server.get_db() as db:
            db.execute(
                """INSERT OR REPLACE INTO land_mutations
                   (id, mutation_no, survey_number, village, previous_owner, new_owner, status,
                    documents, document_checklist, risk_status, risk_payload, encumbrance_status,
                    created_at, updated_at)
                   VALUES ('REAL-SEQ-A', 'M-2026-0012', '999777', 'Collisionville', 'Real Owner',
                           'Other Owner', 'RECEIVED', '[]', '[]', 'UNKNOWN', '{}', 'UNKNOWN', 0, 0)""")

        seeded = client.post("/api/admin/demo/seed", headers=headers, json={"scenario": "all"})
        assert seeded.status_code == 200, seeded.text
        body = seeded.json()
        assert body["created"]["total"] > 0
        # the colliding demo mutation is skipped, the real row is untouched
        with server.get_db() as db:
            real = db.execute("SELECT previous_owner FROM land_mutations WHERE id='REAL-SEQ-A'").fetchone()
            demo_clone = db.execute("SELECT id FROM land_mutations WHERE mutation_no='M-2026-0012' AND id != 'REAL-SEQ-A'").fetchone()
        assert real["previous_owner"] == "Real Owner"
        assert demo_clone is None

        again = client.post("/api/admin/demo/seed", headers=headers, json={"scenario": "all"})
        assert again.status_code == 200
        assert again.json()["created"]["total"] == 0
    finally:
        client.delete("/api/admin/demo/data", headers=headers)
        with server.get_db() as db:
            db.execute("DELETE FROM land_mutations WHERE id='REAL-SEQ-A'")


# --------------------------------------------------------- DDL-free requests --

def test_no_request_path_executes_ddl(query_counter, make_user_client, insert_land_document):
    admin, headers, _ = make_user_client("ADMIN", prefix="ddlchk")
    survey = _survey()
    insert_land_document(survey=survey, village="DDLville")

    _reset(query_counter)
    admin.get("/api/land-records", headers=headers, params={"limit": 10})
    admin.get("/api/land-records/risk-review", headers=headers, params={"limit": 10})
    land_id = _land_id(admin, headers, survey)
    admin.get(f"/api/land-records/{land_id}", headers=headers)
    admin.post(f"/api/land-records/{land_id}/due-diligence", headers=headers)
    admin.get(f"/api/land-records/{land_id}/litigation", headers=headers)
    admin.get("/api/audit", headers=headers)
    admin.get("/api/documents", headers=headers)
    admin.post("/api/admin/demo/preview", headers=headers)

    assert query_counter["ddl"] == 0, [sql[:60] for sql in query_counter["sql"] if sql.startswith(("CREATE", "ALTER"))]


# ------------------------------------------------------------------- N+1 ----

def test_land_endpoints_do_not_grow_queries_with_parcels(query_counter, make_user_client,
                                                          insert_land_document, insert_court_case):
    admin, headers, _ = make_user_client("ADMIN", prefix="n1chk")

    for index in range(6):
        insert_land_document(survey=_survey("5"), village=f"Growth{index}", owner=f"Owner {index}")
    insert_court_case(_survey("5"), "Growth0", case_type="CIVIL")

    _reset(query_counter)
    admin.get("/api/land-records", headers=headers, params={"limit": 100})
    small_list = query_counter["queries"]
    _reset(query_counter)
    admin.get("/api/land-records/risk-review", headers=headers, params={"limit": 100})
    small_risk = query_counter["queries"]

    for index in range(6, 12):
        insert_land_document(survey=_survey("5"), village=f"Growth{index}", owner=f"Owner {index}")

    _reset(query_counter)
    admin.get("/api/land-records", headers=headers, params={"limit": 100})
    big_list = query_counter["queries"]
    _reset(query_counter)
    admin.get("/api/land-records/risk-review", headers=headers, params={"limit": 100})
    big_risk = query_counter["queries"]

    assert big_list == small_list, f"list queries grew with parcels: {small_list} -> {big_list}"
    assert big_risk == small_risk, f"risk queries grew with parcels: {small_risk} -> {big_risk}"
    assert big_risk <= 12


def test_detail_reads_each_register_once(query_counter, make_user_client, insert_land_document,
                                          insert_court_case):
    admin, headers, _ = make_user_client("ADMIN", prefix="detchk")
    survey = _survey()
    insert_land_document(survey=survey, village="Onceville", owner="A", year="2020")
    insert_land_document(survey=survey, village="Onceville", owner="B", year="2023")
    insert_court_case(survey, "Onceville", case_type="CIVIL")
    land_id = _land_id(admin, headers, survey)

    _reset(query_counter)
    detail = admin.get(f"/api/land-records/{land_id}", headers=headers)
    assert detail.status_code == 200

    assert _count_containing(query_counter, "SELECT * FROM LAND_ENCUMBRANCES") == 1
    assert _count_containing(query_counter, "SELECT * FROM LAND_MUTATIONS") == 1
    assert _count_containing(query_counter, "FROM LAND_COURT_CASES") == 1
    body = detail.json()
    assert body["litigation"]["case_count"] == 1
    assert [event["kind"] for event in body["timeline"]].count("COURT_CASE") == 1


# ------------------------------------------------- audit pagination + shape --

def test_audit_is_paginated_in_sql_with_filters(query_counter, make_user_client):
    import server

    admin, headers, _ = make_user_client("ADMIN", prefix="audchk")
    marker = f"auditpage-{uuid.uuid4().hex[:8]}"
    for index in range(12):
        server.log_audit(f"pageuser{index % 2}@example.test", f"PAGE_TEST_{index % 3}",
                         f"{marker} entry {index}", None)
    time.sleep(0.01)

    _reset(query_counter)
    default_page = admin.get("/api/audit", headers=headers).json()
    assert default_page["limit"] == 50 and default_page["offset"] == 0
    assert len(default_page["audit"]) <= 50
    assert default_page["total"] >= 12
    assert {"id", "ts", "username", "action", "detail", "doc_id"} <= set(default_page["audit"][0])

    action_page = admin.get("/api/audit", headers=headers, params={"action": "PAGE_TEST_1", "limit": 5}).json()
    assert action_page["total"] >= 1
    assert all(row["action"] == "PAGE_TEST_1" for row in action_page["audit"])

    user_page = admin.get("/api/audit", headers=headers,
                          params={"username": "pageuser0@example.test", "q": marker}).json()
    assert user_page["total"] >= 1

    paged = admin.get("/api/audit", headers=headers, params={"limit": 5, "offset": 5}).json()
    assert len(paged["audit"]) == 5 and paged["offset"] == 5

    today = time.strftime("%Y-%m-%d", time.gmtime())
    ranged = admin.get("/api/audit", headers=headers,
                       params={"date_from": today, "date_to": today, "q": marker}).json()
    assert ranged["total"] >= 1

    bad = admin.get("/api/audit", headers=headers, params={"date_from": "not-a-date"})
    assert bad.status_code == 422


def test_audit_authorization_unchanged(make_user_client):
    officer, officer_headers, _ = make_user_client("DATA_OFFICER", prefix="audrb1")
    viewer, viewer_headers, _ = make_user_client("VIEWER", prefix="audrb2")
    assert officer.get("/api/audit", headers=officer_headers).status_code == 403
    assert viewer.get("/api/audit", headers=viewer_headers).status_code == 403


# ------------------------------------------------------- document payloads ----

def test_document_list_drops_ocr_but_detail_keeps_it(make_user_client, insert_land_document):
    admin, headers, _ = make_user_client("ADMIN", prefix="docchk")
    survey = _survey()
    doc_id = insert_land_document(survey=survey, village="Payload", owner="Sita Devi")

    listing = admin.get("/api/documents", headers=headers).json()
    item = next(row for row in listing["documents"] if row["id"] == doc_id)
    for heavy in ("ocr_text", "cleaned_ocr_text", "original_fields", "ai_decision_support"):
        assert heavy not in item, f"list must not ship {heavy}"
    assert item["fields"]["owner_name"]["value"] == "Sita Devi"
    assert "total" in listing and "limit" in listing and "offset" in listing

    bad = admin.get("/api/documents", headers=headers, params={"limit": "abc"})
    assert bad.status_code == 422

    detail = admin.get(f"/api/documents/{doc_id}", headers=headers).json()
    assert "ocr_text" in detail and "cleaned_ocr_text" in detail


def test_document_list_pagination(make_user_client, insert_land_document):
    admin, headers, _ = make_user_client("ADMIN", prefix="pgchk")
    for _ in range(4):
        insert_land_document(survey=_survey("9"), village="Pageville")

    page = admin.get("/api/documents", headers=headers, params={"limit": 3, "offset": 0}).json()
    assert len(page["documents"]) == 3 and page["total"] >= 4
    page2 = admin.get("/api/documents", headers=headers, params={"limit": 3, "offset": 3}).json()
    assert {row["id"] for row in page["documents"]}.isdisjoint({row["id"] for row in page2["documents"]})


def test_document_list_rbac_unchanged(make_user_client, insert_land_document):
    viewer, viewer_headers, _ = make_user_client("VIEWER", prefix="rbchk1")
    officer, officer_headers, email = make_user_client("DATA_OFFICER", prefix="rbchk2")
    admin, admin_headers, _ = make_user_client("ADMIN", prefix="rbchk3")

    mine = insert_land_document(survey=_survey("9"), village="Rbacville", uploader=email)
    other = insert_land_document(survey=_survey("9"), village="Rbacville",
                                 uploader="someone-else@example.test", status="APPROVED")
    draft = insert_land_document(survey=_survey("9"), village="Rbacville", uploader=email, status="DRAFT")

    viewer_ids = {row["id"] for row in viewer.get("/api/documents", headers=viewer_headers).json()["documents"]}
    assert {mine, other} <= viewer_ids and draft not in viewer_ids

    officer_ids = {row["id"] for row in officer.get("/api/documents", headers=officer_headers).json()["documents"]}
    assert officer_ids == {mine, draft}

    admin_ids = {row["id"] for row in admin.get("/api/documents", headers=admin_headers).json()["documents"]}
    assert {mine, other, draft} <= admin_ids


# -------------------------------------------------- behaviour equivalence ----

def test_preloaded_registers_produce_identical_risk_and_states(make_user_client, insert_land_document):
    from land_intel import _court_case_index, _get_land, _register_index, _register_states, compute_land_risk

    admin, headers, _ = make_user_client("ADMIN", prefix="equiv")
    survey = _survey()
    insert_land_document(survey=survey, village="Equivalence", owner="Old Owner", year="2019")
    insert_land_document(survey=survey, village="Equivalence", owner="New Owner", year="2023")
    land = _get_land(admin, _land_id(admin, headers, survey))

    assert compute_land_risk(land) == compute_land_risk(land, case_index=_court_case_index(),
                                                        registers=_register_index())
    assert _register_states(land) == _register_states(land, case_index=_court_case_index(),
                                                      registers=_register_index())
