"""DEMO SCENARIO tests: deterministic seeding (S1-S16), expected risk outcomes
per scenario, the preview surface, idempotency, real-data protection,
production refusal, and a clean demo-only wipe."""
import pytest


@pytest.fixture
def demo(make_user_client):
    """Admin client + guaranteed cleanup of demo data."""
    client, headers, _ = make_user_client("ADMIN", prefix="demo")
    yield client, headers
    client.delete("/api/admin/demo/data", headers=headers)


def _find_land(client, headers, survey, village=""):
    """Locate one demo parcel by exact survey (and village) match.

    The shared test database accumulates parcels from many suites, so a bare
    ``q=`` search can return unrelated records whose ids merely contain the
    digits — match the identity fields, never just take the first row.
    """
    items = client.get("/api/land-records", headers=headers, params={"q": survey, "limit": 100}).json()["land_records"]
    matches = [item for item in items if item["survey"] == survey
               and (not village or item["village"] == village)]
    assert matches, f"parcel {survey}/{village} not found"
    return matches[0]


def test_demo_scenarios_are_admin_only(make_user_client):
    viewer, viewer_headers, _ = make_user_client("VIEWER", prefix="demov")
    officer, officer_headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="demoo")
    assert viewer.get("/api/admin/demo/scenarios", headers=viewer_headers).status_code == 403
    assert viewer.get("/api/admin/demo/preview", headers=viewer_headers).status_code == 403
    assert officer.post("/api/admin/demo/seed", headers=officer_headers, json={"scenario": "all"}).status_code == 403
    assert officer.delete("/api/admin/demo/data", headers=officer_headers).status_code == 403


def test_scenario_catalog_lists_sixteen_scenarios(demo):
    client, headers = demo
    response = client.get("/api/admin/demo/scenarios", headers=headers)
    assert response.status_code == 200
    scenarios = response.json()["scenarios"]
    assert len(scenarios) == 16
    ids = sorted((item["id"] for item in scenarios), key=lambda value: int(value[1:]))
    assert ids == [f"S{i}" for i in range(1, 17)]


def test_preview_reports_expected_dataset_before_anything_is_loaded(demo):
    client, headers = demo
    preview = client.get("/api/admin/demo/preview", headers=headers).json()
    assert preview["scenario_count"] == 16
    assert preview["dataset"] == {"documents": 29, "mutations": 7, "encumbrances": 4,
                                  "court_cases": 6, "land_records": 16, "total": 46}
    assert preview["present"]["documents"] == 0
    assert preview["would_create"] == 46
    assert preview["production_blocked"] is False
    assert preview["complete"] is False

    client.post("/api/admin/demo/seed", headers=headers, json={"scenario": "all"})
    after = client.get("/api/admin/demo/preview", headers=headers).json()
    assert after["would_create"] == 0
    assert after["already_present"] == 46
    assert after["complete"] is True


def test_seeding_is_deterministic_and_idempotent(demo):
    client, headers = demo
    first = client.post("/api/admin/demo/seed", headers=headers, json={"scenario": "all"})
    assert first.status_code == 200
    assert first.json()["created"]["total"] == 46
    first_lands = client.get("/api/land-records", headers=headers, params={"q": "Ambedarpur"}).json()
    second = client.post("/api/admin/demo/seed", headers=headers, json={"scenario": "all"})
    assert second.status_code == 200
    body = second.json()
    assert body["created"]["total"] == 0, "second seed must create nothing"
    assert body["dataset"]["total"] == 46
    assert sum(len(ids) for ids in body["skipped_artifacts"].values()) == 46
    second_lands = client.get("/api/land-records", headers=headers, params={"q": "Ambedarpur"}).json()
    assert first_lands["total"] == second_lands["total"]


def test_real_records_survive_seeding(demo, insert_land_document):
    """Seeding must not overwrite, duplicate or displace production rows."""
    client, headers = demo
    production_doc = insert_land_document(survey="888", village="Realville", owner="Real Owner")
    seeded = client.post("/api/admin/demo/seed", headers=headers, json={"scenario": "all"})
    assert seeded.status_code == 200

    kept = client.get(f"/api/documents/{production_doc}", headers=headers)
    assert kept.status_code == 200
    assert kept.json()["fields"]["owner_name"]["value"] == "Real Owner"
    land = _find_land(client, headers, "888", "Realville")
    assert land["record_count"] == 1


def test_unknown_scenario_rejected(demo):
    client, headers = demo
    response = client.post("/api/admin/demo/seed", headers=headers, json={"scenario": "S999"})
    assert response.status_code == 422


def test_scenario_risk_outcomes(demo):
    client, headers = demo
    client.delete("/api/admin/demo/data", headers=headers)  # defensive re-baseline
    seeded = client.post("/api/admin/demo/seed", headers=headers, json={"scenario": "all"})
    assert seeded.status_code == 200

    def risk_for(survey):
        # The shared test database holds many parcels; narrow each lookup so the
        # fixed response limit can never hide a scenario.
        rows = client.get("/api/land-records/risk-review", headers=headers,
                          params={"q": survey, "limit": 100}).json()["land_records"]
        matches = [item for item in rows if item["survey"] == survey]
        assert matches, f"scenario land {survey} missing from risk review"
        return matches[0]

    expectations = {
        "201": ("CLEAR", set()),                                           # S1 clean
        "45/2": ("CLEAR", set()),                                          # S2 valid mutation
        "103": ("HIGH_RISK", {"ACTIVE_ENCUMBRANCE"}),                      # S3 active encumbrance
        "204": ("REVIEW", {"OWNER_CHANGE_NO_MUTATION"}),                   # S4 no mutation
        "105": ("HIGH_RISK", {"OWNER_CONFLICT_YEAR"}),                     # S5 conflicting history
        "106": ("REVIEW", {"AREA_JUMP"}),                                  # S6 area change
        "207": ("CLEAR", {"PENDING_MUTATION"}),                            # S7 pending mutation
        "208": ("REVIEW", {"OWNER_CHANGE_NO_MUTATION", "REJECTED_MUTATION"}),  # S8 rejected mutation
        "209": ("CLEAR", {"LOW_QUALITY_EXTRACTION"}),                      # S9 blurry OCR
        "210": ("REVIEW", {"DUPLICATE_CONFLICT"}),                         # S10 duplicate
        "311": ("HIGH_RISK", {"ACTIVE_LITIGATION", "TRANSFER_DURING_LITIGATION", "PENDING_MUTATION"}),  # S11
        "312": ("HIGH_RISK", {"ACTIVE_LITIGATION", "ACTIVE_ENCUMBRANCE"}),  # S12
        "313": ("HIGH_RISK", {"ACTIVE_LITIGATION"}),                        # S13 revenue dispute
        "314": ("CLEAR", {"CLOSED_LITIGATION_ON_RECORD"}),                  # S14 decided
        "315": ("CLEAR", {"CLOSED_LITIGATION_ON_RECORD"}),                  # S15 settled + released
        "316": ("HIGH_RISK", {"ACTIVE_ENCUMBRANCE", "SALE_DURING_ENCUMBRANCE", "OWNER_CHANGE_NO_MUTATION",
                              "REJECTED_MUTATION", "CLOSED_LITIGATION_ON_RECORD"}),  # S16
    }
    for survey, (verdict, allowed_flags) in expectations.items():
        land = risk_for(survey)
        detail = client.get(f"/api/land-records/{land['land_id']}", headers=headers).json()
        # Only judge the scenario when the land is purely demo data (an
        # environment with leftovers of the same survey is not a failure).
        doc_ids = [doc["id"] for doc in detail["documents"]]
        if doc_ids and not all(doc_id.startswith("DEMO-") for doc_id in doc_ids):
            continue
        risk = land["risk"]
        codes = {flag["code"] for flag in risk["flags"]}
        assert risk["verdict"] == verdict, f"{survey}: {risk['verdict']} != {verdict} ({codes})"
        assert codes == allowed_flags, f"{survey}: unexpected flags {codes ^ allowed_flags}"


def test_litigation_scenarios_return_the_expected_case_status(demo):
    """S11-S13 ACTIVE, S14 DECIDED, S15 SETTLED, S16 WITHDRAWN — read back
    through the litigation endpoints, not just the risk flags."""
    client, headers = demo
    client.post("/api/admin/demo/seed", headers=headers, json={"scenario": "all"})
    expected_status = {"311": "ACTIVE", "312": "ACTIVE", "313": "ACTIVE",
                       "314": "DECIDED", "315": "SETTLED", "316": "WITHDRAWN"}
    expected_verdict = {k: ("ACTIVE_LITIGATION" if v == "ACTIVE" else "PRIOR_LITIGATION")
                        for k, v in expected_status.items()}
    for survey, status in expected_status.items():
        land_id = _find_land(client, headers, survey)["land_id"]
        litigation = client.get(f"/api/land-records/{land_id}/litigation", headers=headers).json()
        assert litigation["court_cases"], f"no case registered for {survey}"
        assert litigation["court_cases"][0]["status"] == status, survey
        assert litigation["verdict"] == expected_verdict[survey], survey
        # the detail drawer carries the same case for the reviewer
        detail = client.get(f"/api/land-records/{land_id}", headers=headers).json()
        assert detail["litigation"]["cases"][0]["case_number"] == litigation["court_cases"][0]["case_number"]


def test_seeded_court_cases_are_wired_into_the_risk_engine(demo):
    client, headers = demo
    client.post("/api/admin/demo/seed", headers=headers, json={"scenario": "all"})
    high_surveys = set()
    s11 = None
    for survey in ("103", "105", "311", "312", "313", "316"):
        rows = client.get("/api/land-records/risk-review", headers=headers,
                          params={"q": survey, "limit": 100}).json()["land_records"]
        matches = [item for item in rows if item["survey"] == survey]
        assert matches, f"{survey} missing from risk review"
        assert matches[0]["risk"]["verdict"] == "HIGH_RISK", survey
        high_surveys.add(survey)
        if survey == "311":
            s11 = matches[0]
    assert high_surveys == {"103", "105", "311", "312", "313", "316"}
    flags = {flag["code"] for flag in s11["risk"]["flags"]}
    assert "ACTIVE_LITIGATION" in flags and "TRANSFER_DURING_LITIGATION" in flags
    assert s11["risk"]["why"], "the verdict must come with human-readable reasons"


def test_low_quality_scenario_doc_enters_normal_workflow(demo):
    """S9's blurry document is a real DRAFT in the portal workflow."""
    client, headers = demo
    client.post("/api/admin/demo/seed", headers=headers, json={"scenario": "S9"})
    documents = client.get("/api/documents", headers=headers).json()["documents"]
    demo_docs = [doc for doc in documents if doc["id"].startswith("DEMO-S9")]
    assert len(demo_docs) == 1
    assert demo_docs[0]["status"] == "DRAFT"
    assert demo_docs[0]["mean_conf"] < 60


def test_wipe_removes_only_demo_artifacts(demo, insert_land_document, insert_court_case):
    client, headers = demo
    client.post("/api/admin/demo/seed", headers=headers, json={"scenario": "all"})
    production_doc = insert_land_document(survey="999", village="Productionville")
    production_case, production_case_number = insert_court_case(
        "999", "Productionville", case_number=f"REAL-CR-{production_doc[:6]}", status="ACTIVE")

    wiped = client.delete("/api/admin/demo/data", headers=headers)
    assert wiped.status_code == 200
    removed = wiped.json()["removed"]
    assert removed["documents"] >= 29
    assert removed["encumbrances"] >= 4
    assert removed["mutations"] >= 7
    assert removed["court_cases"] >= 6

    listing = client.get("/api/land-records", headers=headers, params={"q": "Ambedarpur"})
    assert listing.json()["total"] == 0
    kept = client.get("/api/land-records", headers=headers, params={"q": "Productionville"})
    assert kept.json()["total"] == 1
    # the operator-registered court case (non-DEMO id) must survive too
    fetched = client.get(f"/api/court-cases/{production_case}", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json()["court_case"]["case_number"] == production_case_number

    audit = client.get("/api/audit", headers=headers)
    actions = [row["action"] for row in audit.json().get("audit", audit.json().get("logs", []))]
    assert "DEMO_DATA_SEEDED" in actions
    assert "DEMO_DATA_CLEARED" in actions


def test_production_environment_refuses_demo_writes(demo, monkeypatch):
    """The guard reads the live environment; there is no override flag."""
    import server

    client, headers = demo
    monkeypatch.setattr(server, "IS_PRODUCTION", True)

    seeded = client.post("/api/admin/demo/seed", headers=headers, json={"scenario": "all"})
    assert seeded.status_code == 403
    assert "production" in seeded.json()["detail"].lower()

    wiped = client.delete("/api/admin/demo/data", headers=headers)
    assert wiped.status_code == 403

    # nothing was written and read-only surfaces still work
    preview = client.get("/api/admin/demo/preview", headers=headers)
    assert preview.status_code == 200
    assert preview.json()["production_blocked"] is True
    assert preview.json()["present"]["documents"] == 0
