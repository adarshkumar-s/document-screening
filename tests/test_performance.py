"""PERFORMANCE REGRESSION tests.

Proves the optimisation pass changed cost, not behaviour:
- risk / litigation / mutation / encumbrance / report / due-diligence results
  are byte-identical whether registers are loaded per parcel or batched;
- query counts for the land endpoints are constant (no N+1) as the portfolio
  grows, and each register is read at most once per request;
- no request path executes DDL (the request-time CREATE INDEX bottleneck);
- the audit endpoint is SQL-paginated and filterable with the same row shape;
- the document list no longer ships OCR payloads while the detail endpoint
  still does; RBAC, search and filters are unchanged;
- the lazy audit include parameter is additive and backward compatible.
"""
import time
import uuid

import pytest


# ---------------------------------------------------------------- helpers ----

def _survey(prefix="8"):
    return f"{prefix}{uuid.uuid4().hex[:5]}"


def _reset(state):
    state["queries"] = 0
    state["ddl"] = 0
    state["sql"] = []


def _count_containing(state, fragment):
    return sum(1 for statement in state["sql"] if fragment in statement)


def _admin(make_user_client, prefix):
    client, headers, email = make_user_client("ADMIN", prefix=prefix)
    return client, headers


# ------------------------------------------------- behaviour equivalence ----

def _land_id(client, headers, survey):
    rows = client.get("/api/land-records", headers=headers, params={"q": survey}).json()["land_records"]
    matches = [row for row in rows if row["survey"] == survey]
    assert matches, f"parcel {survey} missing"
    return matches[0]["land_id"]



def test_preloaded_registers_produce_identical_risk_and_states(make_user_client, insert_land_document):
    """Batching must be invisible: identical risk payloads either way."""
    from land_intel import _court_case_index, _get_land, _register_index, _register_states, compute_land_risk

    admin, headers = _admin(make_user_client, "perfa")
    survey = _survey()
    insert_land_document(survey=survey, village="Equivalence", owner="Old Owner", year="2019")
    insert_land_document(survey=survey, village="Equivalence", owner="New Owner", year="2023")
    land = _get_land(admin, _land_id(admin, headers, survey))

    risk_inline = compute_land_risk(land)
    states_inline = _register_states(land)
    case_index = _court_case_index()
    registers = _register_index()
    risk_batched = compute_land_risk(land, case_index=case_index, registers=registers)
    states_batched = _register_states(land, case_index=case_index, registers=registers)

    assert risk_inline == risk_batched
    assert {key: value for key, value in states_inline.items()} == \
           {key: value for key, value in states_batched.items()}


def test_risk_review_payload_unchanged_for_a_known_mixture(make_user_client, insert_land_document,
                                                            insert_court_case):
    """A parcel with a known shape still scores exactly as the rule set says."""
    admin, headers = _admin(make_user_client, "perfb")
    survey = _survey()
    insert_land_document(survey=survey, village="Mixville", owner="A", year="2020")
    insert_court_case(survey, "Mixville", case_type="TITLE", filed_date="2023-01-01", status="ACTIVE")

    rows = admin.get("/api/land-records/risk-review", headers=headers, params={"q": survey}).json()["land_records"]
    row = next(item for item in rows if item["survey"] == survey)
    assert row["risk"]["verdict"] == "HIGH_RISK"
    codes = {flag["code"] for flag in row["risk"]["flags"]}
    assert "ACTIVE_LITIGATION" in codes
    assert row["litigation_status"] == "ACTIVE" and row["active_litigation_count"] == 1
    assert row["encumbrance_status"] == "NONE"


# ------------------------------------------------------------- N+1 guards ----

def test_no_request_path_executes_ddl(query_counter, make_user_client, insert_land_document):
    """The request-time CREATE INDEX / CREATE TABLE bottleneck stays dead."""
    admin, headers = _admin(make_user_client, "perfddl")
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

    assert query_counter["ddl"] == 0, [sql[:60] for sql in query_counter["sql"] if sql.startswith(("CREATE", "ALTER"))]


def test_land_records_and_risk_review_queries_do_not_grow_with_parcels(
        query_counter, make_user_client, insert_land_document, insert_court_case):
    """Register/litigation reads are batched: doubling the portfolio must not
    add a single query to a list or risk-review request."""
    admin, headers = _admin(make_user_client, "perfn1")

    for index in range(6):
        survey = _survey("5")
        insert_land_document(survey=survey, village=f"Growth{index}", owner=f"Owner {index}")
    insert_court_case(_survey("5"), "Growth0", case_type="CIVIL")  # register content exists

    _reset(query_counter)
    admin.get("/api/land-records", headers=headers, params={"limit": 100})
    small_list = query_counter["queries"]
    _reset(query_counter)
    admin.get("/api/land-records/risk-review", headers=headers, params={"limit": 100})
    small_risk = query_counter["queries"]

    for index in range(6, 12):
        survey = _survey("5")
        insert_land_document(survey=survey, village=f"Growth{index}", owner=f"Owner {index}")

    _reset(query_counter)
    admin.get("/api/land-records", headers=headers, params={"limit": 100})
    big_list = query_counter["queries"]
    _reset(query_counter)
    admin.get("/api/land-records/risk-review", headers=headers, params={"limit": 100})
    big_risk = query_counter["queries"]

    assert big_list == small_list, f"list queries grew with parcels: {small_list} -> {big_list}"
    assert big_risk == small_risk, f"risk queries grew with parcels: {small_risk} -> {big_risk}"
    # and the absolute numbers stay tiny (documents + registers + litigation + audit-context)
    assert big_risk <= 12


def test_detail_reads_each_register_once(query_counter, make_user_client, insert_land_document,
                                          insert_court_case):
    admin, headers = _admin(make_user_client, "perfdet")
    survey = _survey()
    insert_land_document(survey=survey, village="Onceville", owner="A", year="2020")
    insert_land_document(survey=survey, village="Onceville", owner="B", year="2023")
    insert_court_case(survey, "Onceville", case_type="CIVIL")
    land_id = _land_id(admin, headers, survey)

    _reset(query_counter)
    detail = admin.get(f"/api/land-records/{land_id}", headers=headers)
    assert detail.status_code == 200

    # the full-register reads happen exactly once; the only other register
    # touch is register_lands()'s narrow identity projection (also constant)
    assert _count_containing(query_counter, "SELECT * FROM LAND_ENCUMBRANCES") == 1
    assert _count_containing(query_counter, "SELECT * FROM LAND_MUTATIONS") == 1
    assert _count_containing(query_counter, "FROM LAND_COURT_CASES") == 1
    assert _count_containing(query_counter, "FROM LAND_ENCUMBRANCES") <= 2
    assert _count_containing(query_counter, "FROM LAND_MUTATIONS") <= 2
    # litigation + timeline present without further register reads
    body = detail.json()
    assert body["litigation"]["case_count"] == 1
    assert [event["kind"] for event in body["timeline"]].count("COURT_CASE") == 1


def test_litigation_parcels_endpoint_is_constant_too(query_counter, make_user_client,
                                                      insert_land_document, insert_court_case):
    admin, headers = _admin(make_user_client, "perflit")
    survey = _survey()
    insert_land_document(survey=survey, village="Litconst", owner="A")
    insert_court_case(survey, "Litconst", case_type="REVENUE")
    land_id = _land_id(admin, headers, survey)

    _reset(query_counter)
    first = admin.get(f"/api/land-records/{land_id}/litigation", headers=headers)
    assert first.status_code == 200 and first.json()["active_count"] == 1
    single = query_counter["queries"]

    for _ in range(5):  # more cases and parcels must not change the query count
        insert_court_case(survey, "Litconst", case_type="TITLE")
        insert_land_document(survey=_survey("6"), village="Elsewhere", owner="B")

    _reset(query_counter)
    admin.get(f"/api/land-records/{land_id}/litigation", headers=headers)
    assert query_counter["queries"] == single


# ------------------------------------------------------- audit pagination ----

def test_audit_is_paginated_in_sql_with_filters(query_counter, make_user_client):
    import server

    admin, headers = _admin(make_user_client, "perfaud")
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
    row = default_page["audit"][0]
    assert {"id", "ts", "username", "action", "detail", "doc_id"} <= set(row)

    action_page = admin.get("/api/audit", headers=headers,
                            params={"action": f"PAGE_TEST_1", "limit": 5}).json()
    assert action_page["total"] >= 1
    assert all(row["action"] == "PAGE_TEST_1" for row in action_page["audit"])
    assert len(action_page["audit"]) <= 5

    user_page = admin.get("/api/audit", headers=headers,
                          params={"username": "pageuser0@example.test", "q": marker}).json()
    assert user_page["total"] >= 1
    assert all(row["username"] == "pageuser0@example.test" for row in user_page["audit"])

    paged = admin.get("/api/audit", headers=headers, params={"limit": 5, "offset": 5}).json()
    assert len(paged["audit"]) == 5 and paged["offset"] == 5

    today = time.strftime("%Y-%m-%d", time.gmtime())
    ranged = admin.get("/api/audit", headers=headers,
                       params={"date_from": today, "date_to": today, "q": marker}).json()
    assert ranged["total"] >= 1

    bad = admin.get("/api/audit", headers=headers, params={"date_from": "not-a-date"})
    assert bad.status_code == 422


def test_audit_authorization_unchanged(make_user_client):
    officer, officer_headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="perfaudr")
    viewer, viewer_headers, _ = make_user_client("VIEWER", prefix="perfaudv")
    assert officer.get("/api/audit", headers=officer_headers).status_code == 403
    assert viewer.get("/api/audit", headers=viewer_headers).status_code == 403


# ----------------------------------------------------- document endpoints ----

def test_document_list_drops_ocr_but_detail_keeps_it(make_user_client, insert_land_document):
    admin, headers = _admin(make_user_client, "perfdoc")
    survey = _survey()
    doc_id = insert_land_document(survey=survey, village="Payload", owner="Sita Devi")

    listing = admin.get("/api/documents", headers=headers).json()
    item = next(row for row in listing["documents"] if row["id"] == doc_id)
    for heavy in ("ocr_text", "cleaned_ocr_text", "original_fields", "ai_decision_support"):
        assert heavy not in item, f"list must not ship {heavy}"
    assert item["fields"]["owner_name"]["value"] == "Sita Devi"
    assert listing["total"] >= 1

    detail = admin.get(f"/api/documents/{doc_id}", headers=headers).json()
    assert "ocr_text" in detail and "cleaned_ocr_text" in detail, "detail keeps the OCR payload columns"
    assert detail["fields"]["owner_name"]["value"] == "Sita Devi"


def test_document_list_pagination(make_user_client, insert_land_document):
    admin, headers = _admin(make_user_client, "perfpage")
    created = [insert_land_document(survey=_survey("9"), village="Pageville") for _ in range(4)]

    page = admin.get("/api/documents", headers=headers, params={"limit": 3, "offset": 0}).json()
    assert len(page["documents"]) == 3 and page["total"] >= 4 and page["limit"] == 3
    page2 = admin.get("/api/documents", headers=headers, params={"limit": 3, "offset": 3}).json()
    assert len(page2["documents"]) >= 1
    assert {row["id"] for row in page["documents"]}.isdisjoint({row["id"] for row in page2["documents"]})


def test_document_list_rbac_unchanged(make_user_client, insert_land_document):
    import server

    viewer, viewer_headers, _ = make_user_client("VIEWER", prefix="perfrbv")
    officer, officer_headers, email = make_user_client("DATA_OFFICER", prefix="perfrbo")
    admin, admin_headers, _ = make_user_client("ADMIN", prefix="perfrba")

    mine = insert_land_document(survey=_survey("9"), village="Rbacville", uploader=email)
    other = insert_land_document(survey=_survey("9"), village="Rbacville",
                                 uploader="someone-else@example.test", status="APPROVED")
    draft = insert_land_document(survey=_survey("9"), village="Rbacville",
                                 uploader=email, status="DRAFT")

    viewer_rows = viewer.get("/api/documents", headers=viewer_headers).json()["documents"]
    assert all(row["status"] == "APPROVED" for row in viewer_rows)
    viewer_ids = {row["id"] for row in viewer_rows}
    assert {mine, other} <= viewer_ids  # approved documents of any owner
    assert draft not in viewer_ids      # never unapproved work-in-progress

    officer_rows = officer.get("/api/documents", headers=officer_headers).json()["documents"]
    assert {row["id"] for row in officer_rows} == {mine, draft}  # own only, any status

    admin_rows = admin.get("/api/documents", headers=admin_headers).json()["documents"]
    assert {mine, other, draft} <= {row["id"] for row in admin_rows}


def test_document_filters_still_work_on_the_light_list(make_user_client, insert_land_document):
    admin, headers = _admin(make_user_client, "perffil")
    village = f"Filterville-{uuid.uuid4().hex[:6]}"
    kept = insert_land_document(survey=_survey("9"), village=village)
    other = insert_land_document(survey=_survey("9"), village="SomeOtherVillage")

    rows = admin.get("/api/documents", headers=headers, params={"village": village}).json()["documents"]
    assert kept in {row["id"] for row in rows}
    assert other not in {row["id"] for row in rows}


# ----------------------------------------------------- lazy detail include ----

def test_detail_include_is_additive_and_lazy_audit_works(make_user_client, insert_land_document,
                                                          insert_court_case):
    admin, headers = _admin(make_user_client, "perfinc")
    survey = _survey()
    insert_land_document(survey=survey, village="Includeville", owner="A")
    insert_court_case(survey, "Includeville", case_type="CIVIL")
    land_id = _land_id(admin, headers, survey)

    full = admin.get(f"/api/land-records/{land_id}", headers=headers).json()  # default: everything
    assert {"documents", "ownership_history", "mutations", "encumbrances",
            "litigation", "timeline", "audit", "risk"} <= set(full)

    without_audit = admin.get(f"/api/land-records/{land_id}", headers=headers,
                              params={"include": "documents,ownership,mutations,encumbrances,litigation,timeline"}).json()
    assert "audit" not in without_audit
    assert {"risk", "documents", "litigation", "timeline", "mutations", "encumbrances"} <= set(without_audit)
    assert without_audit["risk"] == full["risk"]
    assert without_audit["litigation"] == full["litigation"]

    audit_only = admin.get(f"/api/land-records/{land_id}", headers=headers,
                           params={"include": "audit"}).json()
    assert "audit" in audit_only and isinstance(audit_only["audit"], list)
    assert "documents" not in audit_only and "timeline" not in audit_only
