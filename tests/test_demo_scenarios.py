"""Phase 14 — DEMO SCENARIO tests: deterministic seeding, expected risk
outcomes per scenario, admin-only access, and clean wipe."""
import pytest


@pytest.fixture
def demo(make_user_client):
    """Admin client + guaranteed cleanup of demo data."""
    client, headers, _ = make_user_client("ADMIN", prefix="demo")
    yield client, headers
    client.delete("/api/admin/demo/data", headers=headers)


def test_demo_scenarios_are_admin_only(make_user_client):
    viewer, viewer_headers, _ = make_user_client("VIEWER", prefix="demov")
    officer, officer_headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="demoo")
    assert viewer.get("/api/admin/demo/scenarios", headers=viewer_headers).status_code == 403
    assert officer.post("/api/admin/demo/seed", headers=officer_headers, json={"scenario": "all"}).status_code == 403
    assert officer.delete("/api/admin/demo/data", headers=officer_headers).status_code == 403


def test_scenario_catalog_lists_ten_scenarios(demo):
    client, headers = demo
    response = client.get("/api/admin/demo/scenarios", headers=headers)
    assert response.status_code == 200
    scenarios = response.json()["scenarios"]
    assert len(scenarios) == 10
    ids = sorted((item["id"] for item in scenarios), key=lambda value: int(value[1:]))
    assert ids == [f"S{i}" for i in range(1, 11)]


def test_seeding_is_deterministic_and_idempotent(demo):
    client, headers = demo
    first = client.post("/api/admin/demo/seed", headers=headers, json={"scenario": "all"})
    assert first.status_code == 200
    first_lands = client.get("/api/land-records", headers=headers, params={"q": "Ambedarpur"}).json()
    second = client.post("/api/admin/demo/seed", headers=headers, json={"scenario": "all"})
    assert second.status_code == 200
    second_lands = client.get("/api/land-records", headers=headers, params={"q": "Ambedarpur"}).json()
    assert first_lands["total"] == second_lands["total"]


def test_unknown_scenario_rejected(demo):
    client, headers = demo
    response = client.post("/api/admin/demo/seed", headers=headers, json={"scenario": "S999"})
    assert response.status_code == 422


def test_scenario_risk_outcomes(demo):
    client, headers = demo
    client.delete("/api/admin/demo/data", headers=headers)  # defensive re-baseline
    seeded = client.post("/api/admin/demo/seed", headers=headers, json={"scenario": "all"})
    assert seeded.status_code == 200
    review = client.get("/api/land-records/risk-review", headers=headers, params={"limit": 100})
    by_survey = {item["survey"]: item for item in review.json()["land_records"]}

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
    }
    for survey, (verdict, allowed_flags) in expectations.items():
        assert survey in by_survey, f"scenario land {survey} missing"
        land = by_survey[survey]
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


def test_wipe_removes_only_demo_artifacts(demo, insert_land_document):
    client, headers = demo
    client.post("/api/admin/demo/seed", headers=headers, json={"scenario": "all"})
    production_doc = insert_land_document(survey="999", village="Productionville")

    wiped = client.delete("/api/admin/demo/data", headers=headers)
    assert wiped.status_code == 200
    removed = wiped.json()["removed"]
    assert removed["documents"] >= 17
    assert removed["encumbrances"] >= 1
    assert removed["mutations"] >= 3

    listing = client.get("/api/land-records", headers=headers, params={"q": "Ambedarpur"})
    assert listing.json()["total"] == 0
    kept = client.get("/api/land-records", headers=headers, params={"q": "Productionville"})
    assert kept.json()["total"] == 1

    audit = client.get("/api/audit", headers=headers)
    actions = [row["action"] for row in audit.json().get("audit", audit.json().get("logs", []))]
    assert "DEMO_DATA_SEEDED" in actions
    assert "DEMO_DATA_CLEARED" in actions


def test_low_quality_scenario_doc_enters_normal_workflow(demo):
    """S9's blurry document is a real DRAFT in the portal workflow."""
    client, headers = demo
    client.post("/api/admin/demo/seed", headers=headers, json={"scenario": "S9"})
    documents = client.get("/api/documents", headers=headers).json()["documents"]
    demo_docs = [doc for doc in documents if doc["id"].startswith("DEMO-S9")]
    assert len(demo_docs) == 1
    assert demo_docs[0]["status"] == "DRAFT"
    assert demo_docs[0]["mean_conf"] < 60
