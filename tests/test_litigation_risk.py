"""Litigation ↔ risk-engine integration: explainable signals, verdict effect,
evidence, the shared query path (no per-parcel lookups), the land drawer
aggregate, the timeline and the due-diligence brief."""
import uuid

import land_intel
from land_intel import build_timeline, compute_land_risk, land_identity


def _land(survey, village="Litgville", records=None):
    key, land_id = land_identity(survey, village)
    return {"land_id": land_id, "land_key": key, "survey": survey, "khasra": survey, "village": village,
            "tehsil": "Sadar", "district": "Ghaziabad", "records": records or []}


def _codes(risk):
    return {flag["code"]: flag for flag in risk["flags"]}


def _with_registers(monkeypatch, encumbrances=(), mutations=()):
    monkeypatch.setattr(land_intel, "_land_register_rows", lambda land: (list(encumbrances), list(mutations)))


# ------------------------------------------------------- risk-rule behaviour ----

def test_active_case_escalates_the_verdict_and_is_explained(monkeypatch, insert_court_case):
    _with_registers(monkeypatch)
    survey = f"L{uuid.uuid4().hex[:6]}"
    insert_court_case(survey, "Litgville", case_type="TITLE", filed_date="2023-02-02", status="ACTIVE")

    risk = compute_land_risk(_land(survey, "Litgville"))
    flags = _codes(risk)
    assert "ACTIVE_LITIGATION" in flags
    assert flags["ACTIVE_LITIGATION"]["severity"] == "HIGH"
    assert risk["verdict"] == "HIGH_RISK"
    assert risk["why"] == [flags["ACTIVE_LITIGATION"]["title"]]
    assert risk["counts"]["high"] == 1
    # explainable evidence, not just a red badge
    evidence = flags["ACTIVE_LITIGATION"]["evidence"]
    assert evidence and evidence[0]["type"] == "court_case" and evidence[0]["label"]
    # litigation is reported alongside the score so reports/UI read one source
    assert risk["litigation"]["active_count"] == 1 and risk["litigation"]["verdict"] == "ACTIVE_LITIGATION"
    assert risk["inputs"]["court_cases"] == 1 and risk["inputs"]["active_court_cases"] == 1


def test_closed_case_stays_visible_as_information_only(monkeypatch, insert_court_case):
    _with_registers(monkeypatch)
    survey = f"L{uuid.uuid4().hex[:6]}"
    insert_court_case(survey, "Litgville", status="DECIDED", filed_date="2019-01-01",
                      closed_date="2021-06-30", decision_summary="Decreed.")

    risk = compute_land_risk(_land(survey, "Litgville"))
    flags = _codes(risk)
    assert set(flags) == {"CLOSED_LITIGATION_ON_RECORD"}
    assert flags["CLOSED_LITIGATION_ON_RECORD"]["severity"] == "INFO"
    assert risk["verdict"] == "CLEAR"
    assert risk["litigation"]["verdict"] == "PRIOR_LITIGATION"


def test_transfer_recorded_while_a_suit_was_pending(monkeypatch, insert_court_case):
    _with_registers(monkeypatch, mutations=[
        {"id": "M1", "mutation_no": "M-2024-1", "status": "COMPLETED", "deed_date": "2024-05-12",
         "previous_owner": "A", "new_owner": "B", "reason_type": "SALE", "decided_at": 1700000000,
         "created_at": 1700000000, "updated_at": 1700000000},
    ])
    survey = f"L{uuid.uuid4().hex[:6]}"
    insert_court_case(survey, "Litgville", case_type="TITLE", filed_date="2023-01-10", status="ACTIVE")

    flags = _codes(compute_land_risk(_land(survey, "Litgville")))
    assert {"ACTIVE_LITIGATION", "TRANSFER_DURING_LITIGATION"} <= set(flags)
    assert "M-2024-1" in flags["TRANSFER_DURING_LITIGATION"]["detail"]
    assert {item["type"] for item in flags["TRANSFER_DURING_LITIGATION"]["evidence"]} == {"court_case", "mutation"}


def test_deed_after_the_case_closed_is_not_flagged(monkeypatch, insert_court_case):
    _with_registers(monkeypatch, mutations=[
        {"id": "M2", "mutation_no": "M-2022-9", "status": "COMPLETED", "deed_date": "2022-03-09",
         "previous_owner": "A", "new_owner": "B", "reason_type": "COURT_DECREE", "decided_at": 1700000000,
         "created_at": 1700000000, "updated_at": 1700000000},
    ])
    survey = f"L{uuid.uuid4().hex[:6]}"
    insert_court_case(survey, "Litgville", status="DECIDED", filed_date="2019-01-01", closed_date="2021-12-10")

    flags = _codes(compute_land_risk(_land(survey, "Litgville")))
    assert "TRANSFER_DURING_LITIGATION" not in flags
    assert set(flags) == {"CLOSED_LITIGATION_ON_RECORD"}


def test_existing_risk_rules_are_unchanged_by_the_new_rule(monkeypatch, insert_court_case):
    """A parcel with no registered case scores exactly as before: the
    litigation rule is purely additive."""
    _with_registers(monkeypatch)
    survey = f"L{uuid.uuid4().hex[:6]}"
    risk = compute_land_risk(_land(survey, "Nowherevillage"))
    assert risk["flags"] == []
    assert risk["why"] == ["No review signals recorded"]
    assert risk["litigation"]["case_count"] == 0 and risk["litigation"]["verdict"] == "CLEAR"
    assert risk["legal_authority"] is False


# ------------------------------------------------------------ query shape ----

def test_grouped_index_matches_per_parcel_lookup(insert_court_case):
    """The shared grouping used by list/risk endpoints must agree with the
    single-parcel lookup — that is what keeps batching correct."""
    import court_cases

    survey = f"L{uuid.uuid4().hex[:6]}"
    insert_court_case(survey, "Groupvillage", case_type="REVENUE")
    insert_court_case(survey, "", case_type="CIVIL")  # village-free register entry
    grouped = court_cases.cases_by_land()
    assert len(court_cases.cases_for_land({"survey": survey, "village": "Groupvillage"}, grouped)) == 2
    assert len(court_cases.cases_for_land({"survey": survey, "village": "Groupvillage"})) == 2
    # batch and single lookups agree for a parcel in another village too
    other = {"survey": survey, "village": "Somewhereelse"}
    assert court_cases.cases_for_land(other, grouped) == court_cases.cases_for_land(other)
    assert court_cases.cases_for_land({"survey": f"ZZZ{survey}", "village": "Groupvillage"}, grouped) == []


def test_land_detail_carries_litigation_and_timeline(make_user_client, insert_land_document, insert_court_case):
    admin, headers, _ = make_user_client("ADMIN", prefix="ld1")
    survey = f"L{uuid.uuid4().hex[:6]}"
    insert_land_document(survey=survey, village="Timelinepur", owner="Sita Devi", year="2018", status="APPROVED")
    insert_land_document(survey=survey, village="Timelinepur", owner="Amit Sharma", year="2023",
                         status="APPROVED", doc_type="Sale Deed")
    case_id, case_number = insert_court_case(survey, "Timelinepur", case_type="TITLE", filed_date="2024-02-01")

    land_id = admin.get("/api/land-records", headers=headers, params={"q": survey}).json()["land_records"][0]["land_id"]
    detail = admin.get(f"/api/land-records/{land_id}", headers=headers).json()

    assert detail["litigation"]["case_count"] == 1
    assert detail["litigation"]["active_count"] == 1
    assert detail["litigation"]["cases"][0]["case_number"] == case_number
    assert detail["risk"]["verdict"] == "HIGH_RISK"
    assert any("litigation" in reason.lower() or "court case" in reason.lower() for reason in detail["risk"]["why"])

    kinds = [event["kind"] for event in detail["timeline"]]
    assert "COURT_CASE" in kinds and "DOCUMENT" in kinds and kinds[-1] == "CURRENT"
    court_event = next(event for event in detail["timeline"] if event["kind"] == "COURT_CASE")
    assert court_event["year"] == 2024 and case_number in court_event["title"]
    assert court_event["refs"]["case_id"] == case_id

    # the records index exposes the same state without extra round-trips
    listed = admin.get("/api/land-records", headers=headers, params={"q": survey}).json()["land_records"][0]
    assert listed["litigation_status"] == "ACTIVE" and listed["active_litigation_count"] == 1


def test_timeline_is_ordered_and_built_from_rows(make_user_client, insert_land_document, insert_court_case):
    admin, headers, _ = make_user_client("ADMIN", prefix="ld2")
    survey = f"L{uuid.uuid4().hex[:6]}"
    insert_land_document(survey=survey, village="Orderpur", owner="A", year="2020", status="APPROVED")
    insert_court_case(survey, "Orderpur", status="DECIDED", filed_date="2016-03-03", closed_date="2018-04-04")
    land_id = admin.get("/api/land-records", headers=headers, params={"q": survey}).json()["land_records"][0]["land_id"]
    timeline = admin.get(f"/api/land-records/{land_id}", headers=headers).json()["timeline"]

    dated = [event for event in timeline if event["kind"] != "CURRENT"]
    assert [event["date"] for event in dated] == sorted(event["date"] for event in dated)
    assert any(event["kind"] == "COURT_CASE" and event["status"] == "DECIDED" for event in dated)


def test_build_timeline_handles_registers_without_documents():
    """A parcel known only to the registers still gets a usable chronology."""
    land = {"land_id": "LR-X", "survey": "9001", "village": "Registerpur", "records": [], "current_owner": ""}
    timeline = build_timeline(
        land,
        [{"id": "E1", "lender": "Example Bank", "reference_no": "R1", "amount": 10.0,
          "start_date": "2020-05-05", "release_date": "2022-01-01", "status": "RELEASED"}],
        [{"id": "M1", "mutation_no": "M-1", "status": "COMPLETED", "deed_date": "2021-06-06",
          "previous_owner": "A", "new_owner": "B", "reason_type": "SALE", "deed_no": "D1"}],
        [{"id": "C1", "case_number": "CASE-1", "case_type": "CIVIL", "court_name": "Court",
          "filed_date": "2019-01-01", "closed_date": "", "status": "ACTIVE", "parties": "A v. B",
          "decision_summary": ""}],
    )
    kinds = [event["kind"] for event in timeline]
    assert kinds[0] == "COURT_CASE"          # 2019 filing precedes everything else
    assert kinds[-1] == "CURRENT"
    assert "DOCUMENT" not in kinds          # a register-only parcel has no documents
    assert timeline[0]["year"] == 2019
    assert sum(1 for event in timeline if event["kind"] == "ENCUMBRANCE") == 2  # created + released
    assert timeline[-1]["kind"] == "CURRENT" and "1 active court case(s)" in timeline[-1]["detail"]


# ------------------------------------------------------------ due diligence ----

def test_due_diligence_brief_aggregates_every_register(make_user_client, insert_land_document, insert_court_case):
    admin, headers, _ = make_user_client("ADMIN", prefix="dd1")
    survey = f"L{uuid.uuid4().hex[:6]}"
    doc_a = insert_land_document(survey=survey, village="Diligepur", owner="Old Owner", year="2019", status="APPROVED")
    doc_b = insert_land_document(survey=survey, village="Diligepur", owner="New Owner", year="2023", status="APPROVED")
    insert_court_case(survey, "Diligepur", case_type="TITLE", filed_date="2022-07-07", status="ACTIVE")
    land_id = admin.get("/api/land-records", headers=headers, params={"q": survey}).json()["land_records"][0]["land_id"]

    response = admin.post(f"/api/land-records/{land_id}/due-diligence", headers=headers)
    assert response.status_code == 200, response.text
    brief = response.json()

    assert brief["verdict"] == "HIGH_RISK"
    assert brief["checks"]["litigation"]["case_count"] == 1
    assert brief["checks"]["litigation"]["active_count"] == 1
    assert brief["checks"]["documents"]["count"] == 2
    assert {doc["document_id"] for doc in brief["checks"]["documents"]["field_matrix"]} == {doc_a, doc_b}
    # ownership mismatch is reported as a document-level difference, not invented
    differences = {item["field"]: item for item in brief["checks"]["documents"]["differences"]}
    assert "owner" in differences
    assert differences["owner"]["values"] == ["Old Owner", "New Owner"]
    assert set(differences["owner"]["documents"]) == {doc_a, doc_b}
    assert brief["checks"]["documents"]["comparison_pair"]["endpoint"] == "/api/documents/compare"
    assert any("court" in action for action in brief["next_actions"])
    assert [flag["code"] for flag in brief["checks"]["risk"]["flags"]] == [f["code"] for f in
                                                                            admin.get(f"/api/land-records/{land_id}/risk",
                                                                                        headers=headers).json()["risk"]["flags"]]
    assert brief["legal_authority"] is False
    assert brief["timeline"][-1]["kind"] == "CURRENT"
    assert "ACTIVE_LITIGATION" in {item["code"] for item in brief["inconsistencies"]}

    audit = admin.get("/api/audit", headers=headers).json()["audit"]
    assert "DUE_DILIGENCE_RUN" in [row["action"] for row in audit]


def test_due_diligence_requires_a_session():
    from fastapi.testclient import TestClient
    from main import app

    anonymous = TestClient(app)
    assert anonymous.post("/api/land-records/LR-0/due-diligence").status_code == 401


def test_viewer_cannot_run_due_diligence(make_user_client):
    viewer, headers, _ = make_user_client("VIEWER", prefix="dd2")
    assert viewer.post("/api/land-records/LR-1/due-diligence", headers=headers).status_code == 403
