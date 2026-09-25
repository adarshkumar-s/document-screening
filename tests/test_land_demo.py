"""DEMO-LI — end-to-end tests for the synthetic Land Intelligence demo dataset.

Covers: admin-governed seeding, idempotence, relationship validity, mutation /
encumbrance / court-case register behaviour, expected risk verdicts per
scenario, document-spec extraction through the REAL deterministic extractor,
the upload -> extraction -> parcel match -> litigation alert flow, and the
no-false-alert checks on clean/disposed parcels.

The dataset itself is fictional (namespace ``DEMO-LI-``); these tests never
weaken production behaviour — they exercise the existing APIs only.
"""
import os
import uuid

import pytest

os.environ.setdefault("ADMIN_INITIAL_PASSWORD", "Admin@123")
os.environ.setdefault("APP_ENV", "test")

import server  # noqa: E402
import land_demo_data  # noqa: E402
from land_demo_docs import DOCUMENT_SPECS, SCENARIO_INDEX, document_plain_text  # noqa: E402
from land_intel import land_identity  # noqa: E402


# fixed shape of the dataset — update consciously if the dataset grows
EXPECTED = {
    "parcels": 19,
    "scenarios": 19,
    "documents": 32,
    "mutations": 9,
    "encumbrances": 9,
    "court_cases": 5,
    "case_orders": 9,
    "sample_documents": 25,
}

DEVNAPUR = land_demo_data.VILLAGE_DEVNAPUR
SHANTIBAN = land_demo_data.VILLAGE_SHANTIBAN


def _li(land_id):
    return land_identity(land_id, SHANTIBAN)[1]


def _lid(survey, village):
    return land_identity(survey, village)[1]


def _count(db, table, prefix="DEMO-LI-%"):
    return db.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE id LIKE ?", (prefix,)).fetchone()["n"]


@pytest.fixture
def li_demo(make_user_client):
    """Admin client with the DEMO-LI dataset seeded; wiped afterwards."""
    client, headers, _ = make_user_client("ADMIN", prefix="lidemo")
    response = client.post("/api/admin/demo/seed", json={"scenario": "LI"}, headers=headers)
    assert response.status_code == 200, response.text
    yield client, headers
    client.delete("/api/admin/demo/data", headers=headers)


def spec_by_name(filename):
    return next(spec for spec in DOCUMENT_SPECS if spec["filename"] == filename)


# ---------------------------------------------------------------------------
# governance: admin-only seeding
# ---------------------------------------------------------------------------

def test_li_demo_endpoints_are_admin_only(make_user_client):
    viewer, viewer_headers, _ = make_user_client("VIEWER", prefix="liv")
    officer, officer_headers, _ = make_user_client("DATA_OFFICER", prefix="lio")
    assert viewer.get("/api/admin/demo/land-intel/index", headers=viewer_headers).status_code == 403
    assert officer.post("/api/admin/demo/seed", headers=officer_headers, json={"scenario": "LI"}).status_code == 403
    assert officer.delete("/api/admin/demo/data", headers=officer_headers).status_code == 403


def test_li_unknown_seed_scenario_rejected(li_demo):
    client, headers = li_demo
    assert client.post("/api/admin/demo/seed", headers=headers, json={"scenario": "LI-NOPE"}).status_code == 422


# ---------------------------------------------------------------------------
# seeding: success, shape, idempotence
# ---------------------------------------------------------------------------

def test_li_seed_succeeds_with_expected_shape(li_demo):
    client, headers = li_demo
    with server.get_db() as db:
        assert _count(db, "documents") == EXPECTED["documents"]
        assert _count(db, "land_mutations") == EXPECTED["mutations"]
        assert _count(db, "land_mutation_events") == EXPECTED["mutations"]
        assert _count(db, "land_encumbrances") == EXPECTED["encumbrances"]
        assert _count(db, "land_court_cases") == EXPECTED["court_cases"]
        assert _count(db, "land_case_orders") == EXPECTED["case_orders"]
        assert db.execute("SELECT COUNT(*) AS n FROM properties WHERE property_id LIKE 'DEMO-LI-%'").fetchone()["n"] == EXPECTED["parcels"]


def test_li_seed_is_idempotent(li_demo):
    client, headers = li_demo
    first = client.get("/api/land-records", headers=headers, params={"q": SHANTIBAN, "limit": 100}).json()
    again = client.post("/api/admin/demo/seed", headers=headers, json={"scenario": "LI"})
    assert again.status_code == 200
    second = client.get("/api/land-records", headers=headers, params={"q": SHANTIBAN, "limit": 100}).json()
    assert first["total"] == second["total"]
    with server.get_db() as db:
        assert _count(db, "documents") == EXPECTED["documents"]
        assert _count(db, "land_mutations") == EXPECTED["mutations"]
        assert _count(db, "land_court_cases") == EXPECTED["court_cases"]


# ---------------------------------------------------------------------------
# relationships
# ---------------------------------------------------------------------------

def test_li_parcel_relationships_are_valid(li_demo):
    client, headers = li_demo
    # every scenario parcel resolves through the land-record API with its documents
    for row in SCENARIO_INDEX:
        land_id = _lid(row["parcel"]["survey"], row["parcel"]["village"])
        detail = client.get(f"/api/land-records/{land_id}", headers=headers)
        assert detail.status_code == 200, f"{row['scenario_id']}: {detail.text}"
        payload = detail.json()
        assert payload["property"]["survey"] == row["parcel"]["survey"]
        assert payload["property"]["village"] == row["parcel"]["village"]
        assert payload["documents"], f"{row['scenario_id']} parcel has no documents"
    # register children always reference an existing parent
    with server.get_db() as db:
        mutation_ids = {row["id"] for row in db.execute("SELECT id FROM land_mutations").fetchall()}
        for event in db.execute("SELECT mutation_id FROM land_mutation_events WHERE id LIKE 'DEMO-LI-%'").fetchall():
            assert event["mutation_id"] in mutation_ids
        case_ids = {row["id"] for row in db.execute("SELECT id FROM land_court_cases").fetchall()}
        for order in db.execute("SELECT case_id FROM land_case_orders WHERE id LIKE 'DEMO-LI-%'").fetchall():
            assert order["case_id"] in case_ids
        # controlled parcels exist for every demo identity
        for row in SCENARIO_INDEX:
            found = db.execute(
                "SELECT property_id FROM properties WHERE survey_number=? AND village=? AND property_id LIKE 'DEMO-LI-%'",
                (row["parcel"]["survey"], row["parcel"]["village"])).fetchone()
            assert found, f"no controlled parcel for {row['scenario_id']}"


def test_li_mutation_relationships_work(li_demo):
    client, headers = li_demo
    detail = client.get(f"/api/land-records/{_lid('71/2', DEVNAPUR)}", headers=headers).json()
    completed = [m for m in detail["mutations"] if m["mutation_no"] == "M/DEMO/2024/0041"]
    assert completed and completed[0]["status"] == "COMPLETED"
    assert completed[0]["previous_owner"] == "Gautam" and completed[0]["new_owner"] == "Saurav"
    history_owners = [event["owner"] for event in detail["ownership_history"] if event["kind"] == "MUTATION"]
    assert "Saurav" in history_owners

    chain = client.get(f"/api/land-records/{_lid('72/14', DEVNAPUR)}", headers=headers).json()
    chain_completed = [m for m in chain["mutations"] if m["status"] == "COMPLETED"]
    assert {m["mutation_no"] for m in chain_completed} == {"M/DEMO/2019/0044", "M/DEMO/2024/0063"}
    mutation_hops = [(event["previous_owner"], event["owner"]) for event in chain["ownership_history"] if event["kind"] == "MUTATION"]
    assert ("Lakhan", "Kiran") in mutation_hops and ("Kiran", "Yashoda") in mutation_hops

    events = client.get("/api/mutations/DEMO-LI-MUT-001-M1/events", headers=headers)
    assert events.status_code == 200 and events.json()["events"]


def test_li_encumbrance_relationships_work(li_demo):
    client, headers = li_demo
    detail = client.get(f"/api/land-records/{_lid('124', SHANTIBAN)}", headers=headers).json()
    by_ref = {item["reference_no"]: item for item in detail["encumbrances"]}
    assert set(by_ref) == {"DEMO-LN-2024-102", "DEMO-CH-2025-033", "DEMO-LN-2019-054"}
    assert by_ref["DEMO-LN-2024-102"]["status"] == "ACTIVE"
    assert by_ref["DEMO-CH-2025-033"]["status"] == "ACTIVE"
    assert by_ref["DEMO-LN-2019-054"]["status"] == "RELEASED" and by_ref["DEMO-LN-2019-054"]["release_date"]
    register = client.get(f"/api/land-records/{_lid('124', SHANTIBAN)}/encumbrances", headers=headers).json()
    assert register["encumbrance_status"] == "ACTIVE" and len(register["active_encumbrances"]) == 2


def test_li_court_case_relationships_work(li_demo):
    client, headers = li_demo
    listing = client.get("/api/court-cases", headers=headers).json()
    by_no = {case["case_no"]: case for case in listing["court_cases"]}
    assert "DEMO-CS-2025-0142" in by_no
    case = by_no["DEMO-CS-2025-0142"]
    assert case["petitioner"] == "Adarsh" and case["respondent"] == "Shivangi"
    assert case["status"] == "PENDING" and "(fictional demo court)" in case["court_name"]
    assert case["next_hearing_date"] == "2026-10-19"

    fetched = client.get("/api/court-cases/DEMO-CS-2025-0142", headers=headers).json()["court_case"]
    assert len(fetched["orders"]) == 3
    assert any(order["order_type"] == "COMMISSION" for order in fetched["orders"])

    sub = client.get(f"/api/land-records/{_lid('131', SHANTIBAN)}/court-cases", headers=headers).json()
    assert sub["litigation_status"] == "ACTIVE"
    assert {c["case_no"] for c in sub["court_cases"]} == {"DEMO-CS-2025-0142"}
    # the related-mutation link resolves
    linked = client.get("/api/court-cases/DEMO-CIVIL-2024-0087", headers=headers).json()["court_case"]
    assert linked["related_mutation_id"] == "DEMO-LI-COURT-003-M1"
    assert any(m["mutation_no"] == "M/DEMO/2024/0201" for m in client.get(
        f"/api/land-records/{_lid('133', SHANTIBAN)}/mutations", headers=headers).json()["mutations"])


def test_li_court_case_api_permissions(make_user_client):
    verifier, verifier_headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="licv")
    viewer, viewer_headers, _ = make_user_client("VIEWER", prefix="licvw")
    officer, officer_headers, _ = make_user_client("DATA_OFFICER", prefix="licdo")
    # everyone authenticated may read
    assert viewer.get("/api/court-cases", headers=viewer_headers).status_code == 200
    # only reviewer/admin may write
    assert officer.post("/api/court-cases", headers=officer_headers, json={
        "survey_number": "199", "village": SHANTIBAN, "case_no": "DEMO-TEST-CASE-RBAC",
        "court_name": "Demo", "petitioner": "A", "respondent": "B"}).status_code == 403
    created = verifier.post("/api/court-cases", headers=verifier_headers, json={
        "survey_number": "199", "village": SHANTIBAN, "case_no": "DEMO-TEST-CASE-RBAC",
        "court_name": "Demo court (fictional)", "petitioner": "Alpha", "respondent": "Beta",
        "filing_date": "2026-01-04", "status": "PENDING", "issue_summary": "RBAC probe case."})
    assert created.status_code == 200, created.text
    order = verifier.post("/api/court-cases/DEMO-TEST-CASE-RBAC/orders", headers=verifier_headers,
                          json={"order_date": "2026-02-01", "order_type": "HEARING", "summary": "First RBAC hearing."})
    assert order.status_code == 200
    with server.get_db() as db:
        db.execute("DELETE FROM land_case_orders WHERE case_id IN (SELECT id FROM land_court_cases WHERE case_no=?)", ("DEMO-TEST-CASE-RBAC",))
        db.execute("DELETE FROM land_court_cases WHERE case_no=?", ("DEMO-TEST-CASE-RBAC",))


# ---------------------------------------------------------------------------
# risk calculation sees the underlying conditions
# ---------------------------------------------------------------------------

def test_li_risk_verdicts_match_expected(li_demo):
    client, headers = li_demo
    review = client.get("/api/land-records/risk-review", headers=headers, params={"limit": 100}).json()
    by_land = {item["land_id"]: item for item in review["land_records"]}
    for row in SCENARIO_INDEX:
        land_id = _lid(row["parcel"]["survey"], row["parcel"]["village"])
        assert land_id in by_land, f"{row['scenario_id']} missing from risk review"
        assert by_land[land_id]["risk"]["verdict"] == row["expected_verdict"], (
            f"{row['scenario_id']}: expected {row['expected_verdict']}, "
            f"got {by_land[land_id]['risk']['verdict']} with flags "
            f"{[f['code'] for f in by_land[land_id]['risk']['flags']]}")


def test_li_risk_flags_carry_explanatory_evidence(li_demo):
    client, headers = li_demo
    risk = client.get(f"/api/land-records/{_lid('143', SHANTIBAN)}/risk", headers=headers).json()["risk"]
    codes = {flag["code"]: flag for flag in risk["flags"]}
    assert codes["SALE_DURING_ENCUMBRANCE"]["severity"] == "HIGH"
    evidence_types = {item["type"] for flag in risk["flags"] for item in flag["evidence"]}
    assert {"mutation", "encumbrance", "document"} <= evidence_types
    court_risk = client.get(f"/api/land-records/{_lid('131', SHANTIBAN)}/risk", headers=headers).json()["risk"]
    litigation_flag = next(flag for flag in court_risk["flags"] if flag["code"] == "ACTIVE_LITIGATION")
    assert litigation_flag["evidence"] and litigation_flag["evidence"][0]["ref"] == "DEMO-LI-COURT-001-C1"
    stayed = client.get(f"/api/land-records/{_lid('133', SHANTIBAN)}/risk", headers=headers).json()["risk"]
    assert any(flag["code"] == "TRANSFER_STAYED" for flag in stayed["flags"])
    assert stayed["inputs"]["active_court_cases"] == 1


# ---------------------------------------------------------------------------
# document specs: extraction + database consistency
# ---------------------------------------------------------------------------

def test_li_document_specs_extract_through_real_extractor():
    for spec in DOCUMENT_SPECS:
        parsed = server.extract_fields_from_ocr(document_plain_text(spec), spec["filename"])
        fields = parsed["fields"]
        text = parsed["ocr_text"]
        assert "SYNTHETIC DEMO DOCUMENT" in text, spec["filename"]
        assert fields["survey_number"]["value"] == spec["fields"]["Survey No"], spec["filename"]
        assert fields["village"]["value"] == spec["fields"]["Village"], spec["filename"]
        if "Owner Name" in spec["fields"]:
            assert fields["owner_name"]["value"] == spec["fields"]["Owner Name"], spec["filename"]
        if "Father's Name" in spec["fields"]:
            assert fields["father_name"]["value"] == spec["fields"]["Father's Name"], spec["filename"]
        if "Mutation No" in spec["fields"]:
            assert fields["mutation_no"]["value"] == spec["fields"]["Mutation No"], spec["filename"]
        if "Case No" in spec["fields"]:
            assert spec["fields"]["Case No"] in text, spec["filename"]
        if "Area" in spec["fields"]:
            assert fields["area"]["value"] == spec["fields"]["Area"], spec["filename"]


def test_li_document_specs_agree_with_seeded_database(li_demo):
    client, headers = li_demo
    with server.get_db() as db:
        mutation_nos = {row["mutation_no"] for row in db.execute("SELECT mutation_no FROM land_mutations").fetchall()}
        case_nos = {row["case_no"] for row in db.execute("SELECT case_no FROM land_court_cases").fetchall()}
        enc_refs = {row["reference_no"] for row in db.execute("SELECT reference_no FROM land_encumbrances").fetchall()}
    for spec in DOCUMENT_SPECS:
        # every document's parcel identity resolves to a seeded scenario
        scenario = next(row for row in SCENARIO_INDEX if row["scenario_id"] == "DEMO-" + spec["scenario"])
        assert (spec["fields"]["Survey No"], spec["fields"]["Village"]) == (
            scenario["parcel"]["survey"], scenario["parcel"]["village"]), spec["filename"]
        if "Mutation No" in spec["fields"]:
            assert spec["fields"]["Mutation No"] in mutation_nos, spec["filename"]
        if "Case No" in spec["fields"]:
            assert spec["fields"]["Case No"] in case_nos, spec["filename"]
        for value in spec["fields"].values():
            if str(value).startswith("DEMO-LN-") or str(value).startswith("DEMO-CH-"):
                assert str(value) in enc_refs, spec["filename"]


def test_li_sample_documents_are_present_and_generated():
    from tools.generate_demo_documents import DOCUMENT_SPECS as _, DEFAULT_OUT  # noqa: F401
    import tools.generate_demo_documents as gen
    missing = [spec["filename"] for spec in gen.DOCUMENT_SPECS
               if not os.path.isfile(os.path.join(gen.DEFAULT_OUT, spec["filename"]))]
    assert not missing, f"missing generated samples: {missing}"
    assert len(gen.DOCUMENT_SPECS) == EXPECTED["sample_documents"]


# ---------------------------------------------------------------------------
# upload -> extraction -> parcel match -> alert flows
# ---------------------------------------------------------------------------

def _fake_pipeline_for(spec):
    text = document_plain_text(spec)

    def fake_pipeline(_content, filename, *_args, **_kwargs):
        return server.extract_fields_from_ocr(text, filename)

    return fake_pipeline


def _upload(client, headers, spec, monkeypatch):
    import io
    from PIL import Image
    monkeypatch.setattr(server, "run_ocr_pipeline", _fake_pipeline_for(spec))
    buffer = io.BytesIO()
    Image.new("RGB", (1400, 1800), "white").save(buffer, "PNG")
    response = client.post(
        "/api/process?doc_type=" + spec["doc_type"].replace(" ", "%20"),
        headers=headers,
        files={"file": (spec["filename"], buffer.getvalue(), "image/png")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_li_upload_of_court_filing_resolves_parcel_and_raises_litigation_alert(li_demo, monkeypatch):
    client, headers = li_demo
    spec = spec_by_name("DEMO-LI-COURT-001-court-filing.png")
    uploaded = _upload(client, headers, spec, monkeypatch)
    detail = client.get(f"/api/documents/{uploaded['id']}", headers=headers).json()
    # the existing property-resolution path linked the upload to the controlled parcel
    with server.get_db() as db:
        link = db.execute(
            "SELECT property_id FROM property_documents WHERE document_id=? AND property_id LIKE 'DEMO-LI-%'",
            (uploaded["id"],)).fetchone()
        assert link, "upload was not linked to the DEMO-LI controlled parcel"
        parcel = db.execute("SELECT survey_number, village FROM properties WHERE property_id=?", (link["property_id"],)).fetchone()
        assert parcel["survey_number"] == "131" and parcel["village"] == SHANTIBAN
    context = detail["land_context"]
    assert context["matched"] is True
    assert context["land_id"] == _lid("131", SHANTIBAN)
    banner = context["litigation_banner"]
    assert banner["tone"] == "danger"
    assert banner["text"] == "Active litigation found for this property"
    assert banner["case_no"] == "DEMO-CS-2025-0142"
    assert banner["court"] and "(fictional demo court)" in banner["court"]
    assert banner["next_hearing_date"] == "2026-10-19"
    assert context["risk_verdict"] == "HIGH_RISK"
    assert any(flag["code"] == "ACTIVE_LITIGATION" for flag in context["risk_flags"])
    assert any(case["case_no"] == "DEMO-CS-2025-0142" for case in context["court_cases"])
    assert "Active litigation found" in context.get("recommendation", "")


def test_li_upload_matches_encumbered_and_conflict_parcels(li_demo, monkeypatch):
    client, headers = li_demo
    mortgage = _upload(client, headers, spec_by_name("DEMO-LI-ENC-002-mortgage-deed.png"), monkeypatch)
    context = client.get(f"/api/documents/{mortgage['id']}", headers=headers).json()["land_context"]
    assert context["matched"] is True
    assert context["land_id"] == _lid("122", SHANTIBAN)
    assert context["encumbrance_banner"]["tone"] == "danger"
    assert context["encumbrance_banner"]["reference"] == "DEMO-LN-2025-014"
    assert context["risk_verdict"] == "HIGH_RISK"
    assert any(flag["code"] == "ACTIVE_ENCUMBRANCE" for flag in context["risk_flags"])

    deed = _upload(client, headers, spec_by_name("DEMO-LI-RISK-002-deed-A.png"), monkeypatch)
    context = client.get(f"/api/documents/{deed['id']}", headers=headers).json()["land_context"]
    assert context["land_id"] == _lid("142", SHANTIBAN)
    assert any(flag["code"] == "OWNER_CONFLICT_YEAR" for flag in context["risk_flags"])


def test_li_pending_mutation_parcel_context(li_demo, monkeypatch):
    client, headers = li_demo
    application = _upload(client, headers, spec_by_name("DEMO-LI-MUT-002-mutation-application.png"), monkeypatch)
    context = client.get(f"/api/documents/{application['id']}", headers=headers).json()["land_context"]
    assert context["land_id"] == _lid("71/8", DEVNAPUR)
    assert context["pending_mutation_count"] >= 1
    assert context["risk_verdict"] == "CLEAR"
    assert context["litigation_status"] == "NONE"


# ---------------------------------------------------------------------------
# false-alert checks
# ---------------------------------------------------------------------------

def test_li_clean_parcel_has_no_false_litigation_warning(li_demo):
    client, headers = li_demo
    context = client.get("/api/documents/DEMO-LI-ENC-001-DOC1", headers=headers).json()["land_context"]
    assert context["matched"] is True
    assert context["litigation_status"] == "NONE"
    assert context["litigation_banner"]["tone"] == "ok"
    assert context["encumbrance_banner"]["tone"] == "ok"
    assert context["risk_verdict"] == "CLEAR"
    assert not any(flag["code"] in {"ACTIVE_LITIGATION", "TRANSFER_STAYED", "ACTIVE_ENCUMBRANCE"} for flag in context["risk_flags"])


def test_li_disposed_case_does_not_raise_active_litigation(li_demo):
    client, headers = li_demo
    context = client.get("/api/documents/DEMO-LI-COURT-004-DOC1", headers=headers).json()["land_context"]
    assert context["matched"] is True
    assert context["litigation_status"] == "DISPOSED"
    assert context["litigation_banner"]["tone"] == "ok"
    assert "disposed" in context["litigation_banner"]["text"].casefold()
    assert context["risk_verdict"] == "CLEAR"
    assert not any(flag["code"] == "ACTIVE_LITIGATION" for flag in context["risk_flags"])


def test_li_released_encumbrance_does_not_flag(li_demo):
    client, headers = li_demo
    risk = client.get(f"/api/land-records/{_lid('125', SHANTIBAN)}/risk", headers=headers).json()["risk"]
    assert risk["verdict"] == "CLEAR"
    assert not any(flag["code"] == "ACTIVE_ENCUMBRANCE" for flag in risk["flags"])
    detail = client.get(f"/api/land-records/{_lid('125', SHANTIBAN)}", headers=headers).json()
    assert detail["encumbrance_banner"]["tone"] == "ok"
    assert "released" in detail["encumbrance_banner"]["text"].casefold()


# ---------------------------------------------------------------------------
# demo-data index + reports + clearing
# ---------------------------------------------------------------------------

def test_li_demo_index_endpoint(li_demo):
    client, headers = li_demo
    index = client.get("/api/admin/demo/land-intel/index", headers=headers).json()
    assert index["dataset"] == "DEMO-LI"
    assert len(index["scenarios"]) == EXPECTED["scenarios"]
    assert len(index["documents"]) == EXPECTED["sample_documents"]
    assert all(entry["file_present"] for entry in index["documents"])
    flagship = next(row for row in index["scenarios"] if row["scenario_id"] == "DEMO-LI-COURT-001")
    assert flagship["expected_verdict"] == "HIGH_RISK"
    assert "DEMO-LI-COURT-001-court-filing.png" in flagship["documents_to_upload"]
    manifest_entry = next(entry for entry in index["documents"] if entry["filename"] == "DEMO-LI-COURT-001-court-filing.png")
    assert manifest_entry["parcel"]["survey"] == "131"


def test_li_report_includes_court_cases(li_demo):
    client, headers = li_demo
    land_id = _lid("131", SHANTIBAN)
    report = client.post("/api/reports/land-verification", headers=headers,
                         json={"land_id": land_id}).json()
    assert report["report"]["risk_status"] == "HIGH_RISK"
    assert report["report"]["litigation_status"] == "ACTIVE"
    assert any(case["case_no"] == "DEMO-CS-2025-0142" for case in report["report"]["court_cases"])
    html = client.get(report["html_url"], headers=headers)
    assert html.status_code == 200 and "DEMO-CS-2025-0142" in html.text


def test_li_clear_removes_only_demo_artifacts(li_demo):
    client, headers = li_demo
    marker = uuid.uuid4().hex[:12]
    with server.get_db() as db:
        db.execute(
            """INSERT INTO documents (id, filename, doc_type, mean_conf, verdict, status, languages, pages,
               fields, validation, ai_decision_support, ocr_text, cleaned_ocr_text, detected_language,
               original_fields, metadata, uploaded_by, reviewer_comments, created_at, updated_at)
               VALUES (?, 'production-marker.pdf', 'Land Record', 90, 'review', 'APPROVED', '[]', 1,
               '{}', '{}', '{}', '', '', 'eng', '{}', '{}', 'someone@example.test', '', 1, 1)""",
            (marker,),
        )
    cleared = client.delete("/api/admin/demo/data", headers=headers)
    assert cleared.status_code == 200
    removed = cleared.json()["removed"]
    assert removed["documents"] >= EXPECTED["documents"]
    assert removed["mutations"] >= EXPECTED["mutations"]
    assert removed["court_cases"] == EXPECTED["court_cases"]
    with server.get_db() as db:
        assert _count(db, "documents") == 0
        assert _count(db, "land_mutations") == 0
        assert _count(db, "land_encumbrances") == 0
        assert _count(db, "land_court_cases") == 0
        assert _count(db, "land_case_orders") == 0
        assert db.execute("SELECT COUNT(*) AS n FROM properties WHERE property_id LIKE 'DEMO-LI-%'").fetchone()["n"] == 0
        assert db.execute("SELECT id FROM documents WHERE id=?", (marker,)).fetchone()
        db.execute("DELETE FROM documents WHERE id=?", (marker,))
