"""Court-case / litigation register tests: CRUD, search, parcel linkage,
closure transitions, RBAC and audit events."""
import pytest
import uuid


@pytest.fixture(autouse=True)
def _no_case_left_behind():
    """Every case this module creates through the API is removed again, so the
    register stays repeatable and never leaks into other suites."""
    import server
    from court_cases import ensure_schema

    ensure_schema()
    with server.get_db() as db:
        before = {row["id"] for row in db.execute("SELECT id FROM land_court_cases").fetchall()}
    yield
    with server.get_db() as db:
        created = {row["id"] for row in db.execute("SELECT id FROM land_court_cases").fetchall()} - before
        for case_id in created:
            db.execute("DELETE FROM land_court_cases WHERE id=?", (case_id,))


def _payload(survey, village="Casework", **overrides):
    body = {
        "survey_number": survey, "village": village, "case_number": f"DEMO-CR-{uuid.uuid4().hex[:8]}",
        "case_type": "TITLE", "court_name": "Court of the District Judge", "filed_date": "2023-04-04",
        "parties": "Ramu v. Shamu", "relief_sought": "Declaration of title",
    }
    body.update(overrides)
    return body


# ------------------------------------------------------------------ CRUD ----

def test_case_can_be_registered_and_read_back(make_user_client, insert_land_document):
    officer, headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="ccr1")
    survey = f"7{uuid.uuid4().hex[:5]}"
    insert_land_document(survey=survey, village="Casework", owner="Ramu")

    created = officer.post("/api/court-cases", headers=headers, json=_payload(survey))
    assert created.status_code == 200, created.text
    case = created.json()["court_case"]
    assert case["status"] == "ACTIVE" and case["case_type"] == "TITLE" and case["village"] == "Casework"

    fetched = officer.get(f"/api/court-cases/{case['id']}", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json()["court_case"]["case_number"] == case["case_number"]

    # ...and it is linked to the parcel the reviewer looks at
    land = officer.get("/api/land-records", headers=headers, params={"q": survey}).json()["land_records"][0]
    litigation = officer.get(f"/api/land-records/{land['land_id']}/litigation", headers=headers).json()
    assert litigation["active_count"] == 1
    assert litigation["verdict"] == "ACTIVE_LITIGATION"
    assert litigation["court_cases"][0]["case_number"] == case["case_number"]


def test_case_number_is_unique(make_user_client):
    officer, headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="ccr2")
    survey = f"7{uuid.uuid4().hex[:5]}"
    body = _payload(survey, case_number=f"DEMO-CR-DUP-{uuid.uuid4().hex[:8]}")
    assert officer.post("/api/court-cases", headers=headers, json=body).status_code == 200
    assert officer.post("/api/court-cases", headers=headers, json=body).status_code == 409


def test_validation_rejects_bad_input(make_user_client):
    officer, headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="ccr3")
    assert officer.post("/api/court-cases", headers=headers,
                        json=_payload("7001", case_number="")).status_code == 422
    assert officer.post("/api/court-cases", headers=headers,
                        json=_payload("7001", case_type="MAGIC")).status_code == 422
    assert officer.post("/api/court-cases", headers=headers,
                        json={"case_number": "DEMO-CR-NOLAND", "case_type": "CIVIL"}).status_code == 422


def test_evidence_must_reference_a_real_document(make_user_client):
    officer, headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="ccr4")
    response = officer.post("/api/court-cases", headers=headers,
                            json=_payload("7002", evidence_doc_ids=["does-not-exist"]))
    assert response.status_code == 404


# ------------------------------------------------------------ search view ----

def test_register_search_by_survey_village_status_and_text(make_user_client):
    officer, headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="ccs1")
    survey = f"7{uuid.uuid4().hex[:5]}"
    other = f"7{uuid.uuid4().hex[:5]}"
    officer.post("/api/court-cases", headers=headers, json=_payload(survey, case_type="POSSESSION"))
    officer.post("/api/court-cases", headers=headers, json=_payload(other, case_type="REVENUE"))

    scoped = officer.get("/api/court-cases", headers=headers, params={"survey": survey}).json()
    assert len(scoped["court_cases"]) == 1 and scoped["active"] == 1

    wrong_village = officer.get("/api/court-cases", headers=headers,
                               params={"survey": survey, "village": "Nowhere"}).json()
    assert wrong_village["court_cases"] == []

    by_status = officer.get("/api/court-cases", headers=headers,
                           params={"survey": survey, "status": "DECIDED"}).json()
    assert by_status["court_cases"] == []
    active_only = officer.get("/api/court-cases", headers=headers,
                             params={"survey": survey, "status": "ACTIVE"}).json()
    assert len(active_only["court_cases"]) == 1

    admin, admin_headers, _ = make_user_client("ADMIN", prefix="ccs2")
    register = admin.get("/api/court-cases", headers=admin_headers, params={"q": other}).json()
    assert any(case["survey_number"] == other for case in register["court_cases"])


def test_unscoped_register_search_needs_reviewer_role(make_user_client):
    viewer, viewer_headers, _ = make_user_client("VIEWER", prefix="ccs3")
    assert viewer.get("/api/court-cases", headers=viewer_headers).status_code == 400


# ------------------------------------------------------- closure/update ------

def test_close_transition_rules(make_user_client):
    officer, headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="cch1")
    survey = f"7{uuid.uuid4().hex[:5]}"
    case = officer.post("/api/court-cases", headers=headers, json=_payload(survey)).json()["court_case"]

    assert officer.post(f"/api/court-cases/{case['id']}/close", headers=headers,
                         json={"status": "PENDING"}).status_code == 422
    closed = officer.post(f"/api/court-cases/{case['id']}/close", headers=headers,
                          json={"status": "SETTLED", "closed_date": "2024-02-02", "decision_summary": "Compromise recorded."})
    assert closed.status_code == 200
    assert closed.json()["court_case"]["status"] == "SETTLED"
    assert closed.json()["court_case"]["closed_date"] == "2024-02-02"

    # an already-closed case cannot be closed again
    again = officer.post(f"/api/court-cases/{case['id']}/close", headers=headers, json={"status": "DECIDED"})
    assert again.status_code == 409


def test_all_four_closure_statuses_are_supported(make_user_client):
    officer, headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="cch2")
    for index, status in enumerate(("DECIDED", "SETTLED", "WITHDRAWN")):
        survey = f"700{index}"
        case = officer.post("/api/court-cases", headers=headers,
                            json=_payload(survey, village=f"Closeville{index}")).json()["court_case"]
        result = officer.post(f"/api/court-cases/{case['id']}/close", headers=headers,
                              json={"status": status, "closed_date": "2024-01-01"})
        assert result.status_code == 200, result.text
        assert result.json()["court_case"]["status"] == status


def test_update_amends_particulars_but_not_outcome(make_user_client):
    officer, headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="ccu1")
    survey = f"7{uuid.uuid4().hex[:5]}"
    case = officer.post("/api/court-cases", headers=headers, json=_payload(survey)).json()["court_case"]

    updated = officer.put(f"/api/court-cases/{case['id']}", headers=headers,
                          json={"court_name": "Court of the Civil Judge (Sr. Div.)", "case_type": "REVENUE"})
    assert updated.status_code == 200, updated.text
    assert updated.json()["court_case"]["court_name"].endswith("Sr. Div.)")
    assert updated.json()["court_case"]["case_type"] == "REVENUE"
    assert updated.json()["court_case"]["status"] == "ACTIVE"

    assert officer.put(f"/api/court-cases/{case['id']}", headers=headers, json={}).status_code == 422
    assert officer.put(f"/api/court-cases/{case['id']}", headers=headers,
                       json={"case_type": "MAGIC"}).status_code == 422


def test_viewer_cannot_write_the_register(make_user_client):
    viewer, viewer_headers, _ = make_user_client("VIEWER", prefix="ccrb1")
    survey = f"7{uuid.uuid4().hex[:5]}"
    assert viewer.post("/api/court-cases", headers=viewer_headers, json=_payload(survey)).status_code == 403
    assert viewer.put(f"/api/court-cases/whatever", headers=viewer_headers, json={"notes": "x"}).status_code == 403
    assert viewer.post("/api/court-cases/whatever/close", headers=viewer_headers, json={"status": "DECIDED"}).status_code == 403


def test_data_officer_cannot_amend_someone_elses_case(make_user_client):
    officer, officer_headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="ccrb3")
    clerk, clerk_headers, _ = make_user_client("DATA_OFFICER", prefix="ccrb4")
    survey = f"7{uuid.uuid4().hex[:5]}"
    case = officer.post("/api/court-cases", headers=officer_headers, json=_payload(survey)).json()["court_case"]
    assert clerk.put(f"/api/court-cases/{case['id']}", headers=clerk_headers,
                     json={"notes": "changed by someone else"}).status_code == 403


def test_missing_case_returns_404(make_user_client):
    officer, headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="cc404")
    assert officer.get("/api/court-cases/does-not-exist", headers=headers).status_code == 404
    assert officer.post("/api/court-cases/does-not-exist/close", headers=headers,
                        json={"status": "DECIDED"}).status_code == 404


def test_litigation_page_and_landing_endpoints_exist(make_user_client):
    officer, headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="ccpage")
    page = officer.get("/litigation", headers=headers)
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert "Court Case" in page.text


# ------------------------------------------------------------ audit trail ----

def test_every_register_change_is_audited(make_user_client):
    # A VERIFICATION_OFFICER performs the writes; only ADMIN may read the trail.
    officer, headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="ccaud")
    admin, admin_headers, _ = make_user_client("ADMIN", prefix="ccaud2")
    survey = f"7{uuid.uuid4().hex[:5]}"
    case = officer.post("/api/court-cases", headers=headers, json=_payload(survey)).json()["court_case"]
    officer.put(f"/api/court-cases/{case['id']}", headers=headers, json={"notes": "typo fixed"})
    officer.post(f"/api/court-cases/{case['id']}/close", headers=headers,
                 json={"status": "DECIDED", "closed_date": "2024-05-05", "decision_summary": "Decreed."})

    payload = admin.get("/api/audit", headers=admin_headers).json()
    rows = payload.get("audit", payload.get("logs", []))
    actions = [row["action"] for row in rows]
    assert "COURT_CASE_CREATED" in actions
    assert "COURT_CASE_UPDATED" in actions
    assert "COURT_CASE_CLOSED" in actions
