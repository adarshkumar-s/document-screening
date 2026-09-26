"""SA Investigation — deterministic regression tests.

SA orchestrates the EXISTING Land Intelligence services (land aggregate,
mutation/encumbrance/litigation registers, ownership analysis, risk engine)
for one uploaded document and proposes a recommendation through the existing
AI-governance approval flow. These tests exercise:

* extraction (value / source page / confidence, missing fields, malformed
  JSON, duplicate content);
* matching classes (EXACT / STRONG / POSSIBLE / CONFLICTING / NO_MATCH);
* court, mutation and ownership investigation rules; timeline; scenarios;
* security (RBAC, actor isolation, SA never executes land changes);
* approval (stays PROPOSED, wrong password, CAS, concurrency, replay, zombie);
* audit (lifecycle, password never persisted, override audited).
"""
import json
import re
import threading
import time
import uuid

import pytest

DEFAULT_TEST_PASSWORD = "Strong Land Password 123!"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _unique_parcel():
    token = uuid.uuid4().hex[:5]
    return f"9{token}", f"Invpur{token}"


_TEST_STARTED = {"ts": 0.0}


@pytest.fixture(autouse=True)
def _sa_test_env(monkeypatch):
    """Synchronous-enough waits so tests observe terminal states, and a small
    source deadline so unavailable-source tests stay fast."""
    _TEST_STARTED["ts"] = time.time()
    monkeypatch.setenv("SA_INVESTIGATION_WAIT_SECONDS", "30")
    monkeypatch.setenv("SA_INVESTIGATION_UPLOAD_WAIT_SECONDS", "30")
    monkeypatch.setenv("SA_INVESTIGATION_SOURCE_TIMEOUT_SECONDS", "10")
    monkeypatch.setenv("SA_AUTO_INVESTIGATE", "0")
    yield


@pytest.fixture
def insert_doc():
    """Insert a screened document with full control over its fields."""
    import server

    inserted = []

    def _insert(*, survey, village, owner="Ram Singh", year="2021", area="2.5 ha", status="APPROVED",
                doc_type="Land Record", uploader="sa-inv@example.test", extra=None, drop=(), raw_fields=None,
                ocr_text="", confidence=0.92, tehsil="Sadar", district="Ghaziabad"):
        doc_id = uuid.uuid4().hex[:12]
        fields = {
            "owner_name": owner, "father_name": "Test Father", "survey_number": survey, "khasra_number": survey,
            "area": area, "village": village, "tehsil": tehsil, "district": district, "state": "Uttar Pradesh",
            "document_date": f"{year}-06-15", "khatauni_year": year,
        }
        fields.update(extra or {})
        for key in drop:
            fields.pop(key, None)
        payload = raw_fields if raw_fields is not None else json.dumps(
            {key: {"value": value, "confidence": confidence, "page": 1} for key, value in fields.items()})
        with server.get_db() as db:
            db.execute(
                """INSERT OR REPLACE INTO documents
                (id,filename,doc_type,mean_conf,verdict,status,languages,pages,fields,validation,
                 ai_decision_support,ocr_text,cleaned_ocr_text,detected_language,original_fields,
                 uploaded_by,reviewer_comments,created_at,updated_at,lat,lon)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (doc_id, f"{doc_id}.pdf", doc_type, 90, "review", status, "[]", 1,
                 payload, "{}", "{}", ocr_text, "", "eng", payload if raw_fields is None else "{}",
                 uploader, "", float(year), float(year), None, None),
            )
        inserted.append(doc_id)
        return doc_id

    yield _insert

    import server as _server
    with _server.get_db() as db:
        for doc_id in inserted:
            db.execute("DELETE FROM documents WHERE id=?", (doc_id,))
            db.execute("DELETE FROM property_documents WHERE document_id=?", (doc_id,))


@pytest.fixture
def insert_mutation():
    import server
    from land_intel import ensure_land_tables

    ensure_land_tables()
    inserted = []

    def _insert(*, survey, village, previous_owner, new_owner, status="COMPLETED", deed_no="", mutation_no=None,
                deed_date="2020-03-01", tehsil="Sadar", district="Ghaziabad"):
        mid = uuid.uuid4().hex[:12]
        number = mutation_no or f"MUT-T-{uuid.uuid4().hex[:8]}"
        now = time.time()
        with server.get_db() as db:
            db.execute(
                """INSERT INTO land_mutations (id, mutation_no, survey_number, khasra_number, village, tehsil, district,
                   previous_owner, new_owner, reason_type, deed_no, deed_date, documents, document_checklist, status,
                   created_by, created_at, updated_at, decided_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (mid, number, survey, survey, village, tehsil, district, previous_owner, new_owner, "SALE", deed_no,
                 deed_date, "[]", "[]", status, "tester", now, now, now if status == "COMPLETED" else None),
            )
        inserted.append(mid)
        return mid, number

    yield _insert

    import server as _server
    with _server.get_db() as db:
        for mid in inserted:
            db.execute("DELETE FROM land_mutations WHERE id=?", (mid,))


@pytest.fixture
def cleanup_investigations():
    """Remove investigations/proposals created by a test (canonical DB is shared)."""
    import server
    import sa_investigation

    sa_investigation.ensure_investigation_tables()
    started = time.time() - 1
    yield
    with server.get_db() as db:
        rows = db.execute("SELECT investigation_id, request_id FROM land_investigations WHERE created_at>=?", (started,)).fetchall()
        for row in rows:
            db.execute("DELETE FROM investigation_findings WHERE investigation_id=?", (row["investigation_id"],))
            db.execute("DELETE FROM investigation_events WHERE investigation_id=?", (row["investigation_id"],))
            db.execute("DELETE FROM land_investigations WHERE investigation_id=?", (row["investigation_id"],))
            if row["request_id"]:
                db.execute("DELETE FROM assistant_requests WHERE request_id=?", (row["request_id"],))
            proposals = db.execute("SELECT proposal_id FROM ai_proposals WHERE target_type='INVESTIGATION' AND target_ids LIKE ?",
                                   (f'%"{row["investigation_id"]}"%',)).fetchall()
            for proposal in proposals:
                db.execute("DELETE FROM ai_approval_events WHERE proposal_id=?", (proposal["proposal_id"],))
                db.execute("DELETE FROM ai_proposals WHERE proposal_id=?", (proposal["proposal_id"],))


def _investigate(client, headers, document_id, *, force_new=False, expect=200):
    response = client.post("/api/sa/investigations", json={"document_id": document_id, "force_new": force_new}, headers=headers)
    assert response.status_code == expect, response.text
    return response.json()


def _detail(client, headers, investigation_id):
    response = client.get(f"/api/sa/investigations/{investigation_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["investigation"]


def _run(client, headers, document_id):
    body = _investigate(client, headers, document_id)
    assert body["already_investigated"] is False
    inv = body["investigation"]
    assert inv["state"] == "READY_FOR_REVIEW", (inv["state"], inv.get("failure"))
    return _detail(client, headers, inv["investigation_id"])


def _types(inv, severity=None):
    return {f["type"] for f in inv["findings"] if severity is None or f["severity"] == severity}


def _find(inv, finding_type, severity=None):
    return [f for f in inv["findings"] if f["type"] == finding_type and (severity is None or f["severity"] == severity)]


def _audit_rows(action_like, ref):
    """Audit rows written during THIS test (the audit log is append-only and
    investigation ids restart after the cleanup fixture removes rows)."""
    import server
    with server.get_db() as db:
        return [dict(r) for r in db.execute(
            "SELECT action, detail, username FROM audit WHERE action LIKE ? AND detail LIKE ? AND ts>=? ORDER BY id",
            (action_like, f"%{ref}%", _TEST_STARTED["ts"])).fetchall()]


def _proposal_status(pid):
    import server
    with server.get_db() as db:
        row = db.execute("SELECT status FROM ai_proposals WHERE proposal_id=?", (pid,)).fetchone()
    return row["status"] if row else None


# --------------------------------------------------------------------------- #
# extraction
# --------------------------------------------------------------------------- #

def test_extraction_reports_value_source_and_confidence_per_identifier(make_user_client, insert_doc, cleanup_investigations):
    admin, headers, _ = make_user_client("ADMIN", prefix="saext")
    survey, village = _unique_parcel()
    insert_doc(survey=survey, village=village, owner="Asha Verma", year="2019")
    doc = insert_doc(survey=survey, village=village, owner="Asha Verma", year="2023",
                     ocr_text="Registration No: RG-77/2023\nMutation No: MN-5")
    inv = _run(admin, headers, doc)
    by_key = {entity["type"]: entity for entity in inv["entities"]}
    assert by_key["survey_number"]["value"] == survey
    assert by_key["survey_number"]["strength"] == "STRONG"
    assert by_key["survey_number"]["source"] == "DOCUMENT_FIELDS"
    assert by_key["survey_number"]["page"] == 1
    assert 0 < by_key["survey_number"]["confidence"] <= 1
    assert by_key["survey_number"]["certainty"] == "Confirmed"
    # identifiers found only in OCR text are reported with their text source, lower confidence and "Possible"
    assert by_key["registration_no"]["value"] == "RG-77/2023" and by_key["registration_no"]["source"] == "OCR_TEXT_PATTERN"
    assert by_key["registration_no"]["certainty"] == "Possible"
    assert by_key["registration_no"]["confidence"] < by_key["survey_number"]["confidence"]
    assert by_key["mutation_no"]["value"] == "MN-5"
    assert inv["reproducibility"]["investigation_version"] == 1
    assert inv["reproducibility"]["rules_version"] and inv["reproducibility"]["engine_version"]
    assert inv["reproducibility"]["data_snapshot_at"]


def test_missing_parcel_identifier_is_a_high_finding_and_never_approved(make_user_client, insert_doc, cleanup_investigations):
    admin, headers, _ = make_user_client("ADMIN", prefix="samiss")
    survey, village = _unique_parcel()
    doc = insert_doc(survey=survey, village=village, drop=("survey_number", "khasra_number", "plot_number", "village"))
    inv = _run(admin, headers, doc)
    missing = _find(inv, "IDENTIFIER_MISSING", "HIGH")
    assert missing and missing[0]["status_label"] == "UNKNOWN"
    assert inv["sources"]["land"]["status"] == "UNKNOWN"
    assert inv["recommendation"] == "FURTHER_VERIFICATION"
    assert inv["confidences"]["parcel_match"]["label"] == "Unknown"


def test_malformed_fields_json_is_reported_not_fatal(make_user_client, insert_doc, cleanup_investigations):
    admin, headers, _ = make_user_client("ADMIN", prefix="samal")
    survey, village = _unique_parcel()
    doc = insert_doc(survey=survey, village=village, raw_fields="{not json")
    inv = _run(admin, headers, doc)
    assert inv["state"] == "READY_FOR_REVIEW"
    assert _find(inv, "EXTRACTION_QUALITY")
    assert inv["recommendation"] == "FURTHER_VERIFICATION"
    assert not inv["failure"]


def test_duplicate_content_offers_open_existing_or_run_new(make_user_client, insert_doc, cleanup_investigations):
    admin, headers, _ = make_user_client("ADMIN", prefix="sadup")
    survey, village = _unique_parcel()
    insert_doc(survey=survey, village=village, year="2018")
    first_doc = insert_doc(survey=survey, village=village, year="2022")
    first = _run(admin, headers, first_doc)
    # same document again
    again = _investigate(admin, headers, first_doc)
    assert again["already_investigated"] is True
    assert again["existing"][0]["investigation_id"] == first["investigation_id"]
    assert set(again["actions"]) == {"open_existing", "run_new"}
    # identical content under a new document id (same fingerprint)
    twin = insert_doc(survey=survey, village=village, year="2022")
    dup = _investigate(admin, headers, twin)
    assert dup["already_investigated"] is True
    assert first["investigation_id"] in [item["investigation_id"] for item in dup["existing"]]
    assert _audit_rows("SA_INVESTIGATION_DUPLICATE", first["investigation_id"])
    # explicit "Run New" creates a fresh investigation
    fresh = _investigate(admin, headers, twin, force_new=True)
    assert fresh["already_investigated"] is False
    assert fresh["investigation"]["investigation_id"] != first["investigation_id"]
    assert fresh["investigation"]["state"] == "READY_FOR_REVIEW"


# --------------------------------------------------------------------------- #
# matching
# --------------------------------------------------------------------------- #

def test_exact_match_when_strong_and_supporting_identifiers_agree(make_user_client, insert_doc, cleanup_investigations):
    admin, headers, _ = make_user_client("ADMIN", prefix="saex")
    survey, village = _unique_parcel()
    insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2017")
    insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2020")
    doc = insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2023")
    inv = _run(admin, headers, doc)
    land = [m for m in inv["matches"] if m["source"] == "LAND_RECORD"][0]
    assert land["match_class"] == "EXACT"
    assert "survey_number" in land["strong_hits"] and "village" in land["supporting_hits"]
    assert inv["sources"]["land"]["status"] == "FOUND"
    assert "OWNERSHIP_CONSISTENT" in _types(inv)
    assert inv["recommendation"] == "APPROVE"
    assert inv["confidences"]["parcel_match"]["label"] == "Confirmed"
    assert inv["confidences"]["parcel_match"]["value"] >= 0.9


def test_strong_match_when_only_minor_differences(make_user_client, insert_doc, cleanup_investigations):
    admin, headers, _ = make_user_client("ADMIN", prefix="sast")
    survey, village = _unique_parcel()
    insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2017", area="2.5 ha")
    doc = insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2023", area="2.6 ha")
    inv = _run(admin, headers, doc)
    land = [m for m in inv["matches"] if m["source"] == "LAND_RECORD"][0]
    assert land["match_class"] == "STRONG"
    assert "area" in land["minor_differences"]
    assert not _find(inv, "AREA_MISMATCH")
    assert inv["confidences"]["parcel_match"]["label"] == "Strong match"


def test_conflicting_match_when_supporting_identifier_contradicts(make_user_client, insert_doc, cleanup_investigations):
    admin, headers, _ = make_user_client("ADMIN", prefix="sacf")
    survey, village = _unique_parcel()
    insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2017", tehsil="Sadar")
    doc = insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2023", tehsil="Dadri")
    inv = _run(admin, headers, doc)
    land = [m for m in inv["matches"] if m["source"] == "LAND_RECORD"][0]
    assert land["match_class"] == "CONFLICTING"
    assert "tehsil" in land["conflicting_identifiers"]
    mismatch = _find(inv, "IDENTIFIER_MISMATCH", "HIGH")
    assert mismatch and mismatch[0]["status_label"] == "CONFLICT"
    assert inv["sources"]["land"]["status"] == "CONFLICT"
    assert inv["recommendation"] == "FURTHER_VERIFICATION"


def test_area_mismatch_beyond_tolerance_is_flagged_with_evidence(make_user_client, insert_doc, cleanup_investigations):
    admin, headers, _ = make_user_client("ADMIN", prefix="saar")
    survey, village = _unique_parcel()
    insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2017", area="2.5 ha")
    doc = insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2023", area="6.0 ha")
    inv = _run(admin, headers, doc)
    area = _find(inv, "AREA_MISMATCH")
    assert area and area[0]["severity"] == "HIGH"
    assert len(area[0]["evidence"]) >= 2  # document + land record
    evidence_ids = {e["evidence_id"] for e in inv["evidence"]}
    assert set(area[0]["evidence"]) <= evidence_ids


def test_no_match_when_no_independent_record_exists(make_user_client, insert_doc, cleanup_investigations):
    admin, headers, _ = make_user_client("ADMIN", prefix="sano")
    survey, village = _unique_parcel()
    doc = insert_doc(survey=survey, village=village, owner="Solo Owner", year="2023")
    inv = _run(admin, headers, doc)
    assert inv["sources"]["land"]["status"] == "NOT FOUND IN SEARCHED SOURCES"
    parcel = [f for f in inv["findings"] if f.get("code") == "PARCEL_NOT_FOUND"]
    assert parcel and parcel[0]["status_label"] == "NOT FOUND IN SEARCHED SOURCES"
    assert inv["confidences"]["parcel_match"]["label"] == "Not found"
    assert inv["recommendation"] == "FURTHER_VERIFICATION"
    # "no evidence = no claim": nothing says the parcel does not exist
    assert "not proof" in parcel[0]["description"] or "alone" in parcel[0]["description"]


# --------------------------------------------------------------------------- #
# court cases
# --------------------------------------------------------------------------- #

def test_active_court_case_on_parcel_is_high_with_deep_link(make_user_client, insert_doc, insert_court_case, cleanup_investigations):
    admin, headers, _ = make_user_client("ADMIN", prefix="sact")
    survey, village = _unique_parcel()
    insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2017")
    case_id, number = insert_court_case(survey, village, case_number=f"CS-{uuid.uuid4().hex[:4]}/2022",
                                        parties="Kamla Devi v. State")
    doc = insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2023")
    inv = _run(admin, headers, doc)
    overlap = _find(inv, "COURT_CASE_OVERLAP", "HIGH")
    assert overlap and overlap[0]["is_alert"]
    assert overlap[0]["why"] and overlap[0]["resolution"]
    court_matches = [m for m in inv["matches"] if m["source"] == "COURT_CASE"]
    assert court_matches and court_matches[0]["match_class"] in {"EXACT", "STRONG"}
    evidence = {e["evidence_id"]: e for e in inv["evidence"]}
    links = [link for eid in overlap[0]["evidence"] for link in evidence[eid]["links"]]
    assert any(link["label"] == "Open Court Case" and "/litigation" in link["href"] for link in links)
    assert inv["confidences"]["court_case_match"]["value"] is not None
    assert inv["recommendation"] in {"FURTHER_VERIFICATION", "REJECT"}
    # party relationship: the document owner is a litigant
    assert _find(inv, "PARTY_RELATIONSHIP", "HIGH")


def test_unrelated_court_case_is_ignored(make_user_client, insert_doc, insert_court_case, cleanup_investigations):
    admin, headers, _ = make_user_client("ADMIN", prefix="saun")
    survey, village = _unique_parcel()
    other_survey, other_village = _unique_parcel()
    insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2017")
    insert_court_case(other_survey, other_village, parties="X v. Y")
    doc = insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2023")
    inv = _run(admin, headers, doc)
    assert not _find(inv, "COURT_CASE_OVERLAP", "HIGH")
    assert inv["sources"]["court"]["status"] == "NOT FOUND IN SEARCHED SOURCES"
    assert inv["confidences"]["court_case_match"]["label"] == "NOT FOUND IN SEARCHED SOURCES"


def test_same_survey_other_village_is_only_a_possible_match(make_user_client, insert_doc, insert_court_case, cleanup_investigations):
    admin, headers, _ = make_user_client("ADMIN", prefix="saamb")
    survey, village = _unique_parcel()
    _, other_village = _unique_parcel()
    insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2017")
    insert_court_case(survey, other_village, parties="P v. Q")
    doc = insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2023")
    inv = _run(admin, headers, doc)
    court_matches = [m for m in inv["matches"] if m["source"] == "COURT_CASE"]
    assert court_matches and court_matches[0]["match_class"] == "POSSIBLE"
    assert not _find(inv, "COURT_CASE_OVERLAP", "HIGH")
    possible = _find(inv, "COURT_CASE_OVERLAP", "LOW")
    assert possible and possible[0]["status_label"] == "POSSIBLE MATCH"
    assert inv["confidences"]["court_case_match"]["label"] == "Possible"


# --------------------------------------------------------------------------- #
# mutations
# --------------------------------------------------------------------------- #

def test_completed_mutation_referenced_by_document_is_confirmed(make_user_client, insert_doc, insert_mutation, cleanup_investigations):
    admin, headers, _ = make_user_client("ADMIN", prefix="samu")
    survey, village = _unique_parcel()
    insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2017")
    mid, number = insert_mutation(survey=survey, village=village, previous_owner="Kamla Devi", new_owner="Ravi Kumar")
    doc = insert_doc(survey=survey, village=village, owner="Ravi Kumar", year="2023", extra={"mutation_no": number})
    inv = _run(admin, headers, doc)
    mutation_matches = [m for m in inv["matches"] if m["source"] == "MUTATION" and m["record_id"] == mid]
    assert mutation_matches and mutation_matches[0]["match_class"] == "EXACT"
    assert [f for f in inv["findings"] if f.get("code") == "MUTATION_MATCH"]
    assert not _find(inv, "MUTATION_MISMATCH")
    assert inv["confidences"]["mutation_match"]["label"] == "Confirmed"
    links = [link for e in inv["evidence"] if e["kind"] == "mutation" for link in e["links"]]
    assert any(link["label"] == "Open Mutation" for link in links)


def test_pending_mutation_is_medium_and_needs_verification(make_user_client, insert_doc, insert_mutation, cleanup_investigations):
    admin, headers, _ = make_user_client("ADMIN", prefix="sapm")
    survey, village = _unique_parcel()
    insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2017")
    _, number = insert_mutation(survey=survey, village=village, previous_owner="Kamla Devi", new_owner="Ravi Kumar", status="UNDER_REVIEW")
    doc = insert_doc(survey=survey, village=village, owner="Ravi Kumar", year="2023", extra={"mutation_no": number})
    inv = _run(admin, headers, doc)
    pending = _find(inv, "MUTATION_MISMATCH", "MEDIUM")
    assert pending and "UNDER_REVIEW" in pending[0]["title"]
    assert inv["recommendation"] == "FURTHER_VERIFICATION"


def test_mutation_conflicting_with_registry_deed_is_high(make_user_client, insert_doc, insert_mutation, cleanup_investigations):
    admin, headers, _ = make_user_client("ADMIN", prefix="samc")
    survey, village = _unique_parcel()
    deed = f"DEED-{uuid.uuid4().hex[:6]}"
    insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2017")
    insert_mutation(survey=survey, village=village, previous_owner="Kamla Devi", new_owner="Someone Else", deed_no=deed)
    doc = insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2023", doc_type="Sale Deed",
                     extra={"registration_number": deed, "seller_name": "Kamla Devi", "buyer_name": "Ravi Kumar"})
    inv = _run(admin, headers, doc)
    conflict = [f for f in _find(inv, "MUTATION_MISMATCH", "HIGH") if deed in f["title"]]
    assert conflict and conflict[0]["status_label"] == "CONFLICT"
    assert inv["recommendation"] in {"FURTHER_VERIFICATION", "REJECT"}


# --------------------------------------------------------------------------- #
# ownership
# --------------------------------------------------------------------------- #

def test_ownership_mismatch_on_transfer_is_critical_and_rejects(make_user_client, insert_doc, cleanup_investigations):
    admin, headers, _ = make_user_client("ADMIN", prefix="saom")
    survey, village = _unique_parcel()
    insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2017")
    insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2020")
    doc = insert_doc(survey=survey, village=village, owner="Fake Seller", year="2023", doc_type="Sale Deed", status="PENDING",
                     extra={"seller_name": "Fake Seller", "buyer_name": "Buyer Person"})
    inv = _run(admin, headers, doc)
    mismatch = _find(inv, "OWNERSHIP_MISMATCH", "CRITICAL")
    assert mismatch and mismatch[0]["status_label"] == "CONFLICT"
    assert "Records indicate" in mismatch[0]["description"]
    assert inv["recommendation"] == "REJECT"
    assert inv["recommendation_detail"]["rule"].startswith("R1")
    assert mismatch[0]["finding_id"] in inv["recommendation_detail"]["critical_findings"]
    assert inv["confidences"]["ownership"]["value"] < 0.5


def test_ownership_gap_without_mutation_is_reported_from_existing_engines(make_user_client, insert_doc, cleanup_investigations):
    admin, headers, _ = make_user_client("ADMIN", prefix="saog")
    survey, village = _unique_parcel()
    insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2015")
    insert_doc(survey=survey, village=village, owner="Ravi Kumar", year="2019")
    doc = insert_doc(survey=survey, village=village, owner="Ravi Kumar", year="2023")
    inv = _run(admin, headers, doc)
    gap = _find(inv, "OWNERSHIP_GAP")
    assert gap
    assert {f["origin"] for f in gap} <= {"LAND_RISK_ENGINE", "OWNERSHIP_ANALYSIS"}
    # the existing risk engine's verdict is integrated (and snapshotted), not replaced by a competing score
    assert inv["risk_verdict"] in {"REVIEW", "HIGH_RISK"}
    assert inv["snapshot"]["land"]["risk"]["verdict"] == inv["risk_verdict"]
    assert inv["recommendation"] == "FURTHER_VERIFICATION"


# --------------------------------------------------------------------------- #
# timeline
# --------------------------------------------------------------------------- #

def test_timeline_is_chronological_and_marks_the_investigated_document(make_user_client, insert_doc, insert_court_case, cleanup_investigations):
    admin, headers, _ = make_user_client("ADMIN", prefix="satl")
    survey, village = _unique_parcel()
    insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2016")
    insert_court_case(survey, village, filed_date="2019-02-01", closed_date="2020-05-01", status="DECIDED", parties="A v. B")
    insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2021")
    doc = insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2024")
    inv = _run(admin, headers, doc)
    years = [event["year"] for event in inv["timeline"] if event.get("year")]
    assert years == sorted(years)
    kinds = [event["kind"] for event in inv["timeline"]]
    assert "COURT_CASE" in kinds and "DOCUMENT" in kinds and kinds[-1] == "INVESTIGATION"
    assert sum(1 for event in inv["timeline"] if event.get("is_investigated_document")) == 1
    assert all(event["links"] for event in inv["timeline"] if event["kind"] != "INVESTIGATION")


def test_conflicting_dates_are_timeline_anomalies(make_user_client, insert_doc, cleanup_investigations):
    admin, headers, _ = make_user_client("ADMIN", prefix="satd")
    survey, village = _unique_parcel()
    insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2016")
    doc = insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2023",
                     extra={"registration_date": "2021-01-05", "document_date": "2099-06-15"})
    inv = _run(admin, headers, doc)
    anomalies = _find(inv, "TIMELINE_ANOMALY")
    titles = " | ".join(a["title"] for a in anomalies)
    assert "future" in titles.lower()
    assert "precedes" in titles.lower()
    assert any(a["severity"] == "HIGH" for a in anomalies)


# --------------------------------------------------------------------------- #
# scenarios / recommendation / explainability
# --------------------------------------------------------------------------- #

def test_all_three_scenarios_are_computed_with_evidence(make_user_client, insert_doc, insert_court_case, cleanup_investigations):
    admin, headers, _ = make_user_client("ADMIN", prefix="sasc")
    survey, village = _unique_parcel()
    insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2017")
    insert_court_case(survey, village, parties="Kamla Devi v. Z")
    doc = insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2023")
    inv = _run(admin, headers, doc)
    scenarios = {s["key"]: s for s in inv["scenarios"]}
    assert set(scenarios) == {"A", "B", "C"}
    for scenario in scenarios.values():
        for key in ("assumptions", "supporting_evidence", "contradicting_evidence", "affected_records", "expected_result",
                    "uncertainties", "plausibility", "plausibility_explained", "computed_from"):
            assert key in scenario, key
        assert 0 < scenario["plausibility"] < 1
        assert scenario["affected_records"]
    # scenario evidence = finding ids, each of which carries record evidence with deep links
    findings = {f["finding_id"]: f for f in inv["findings"]}
    evidence_ids = {e["evidence_id"] for e in inv["evidence"]}
    assert scenarios["A"]["contradicting_evidence"], "the active case must contradict approval"
    for finding_id in scenarios["A"]["contradicting_evidence"] + scenarios["B"]["supporting_evidence"]:
        assert finding_id in findings
        assert findings[finding_id]["evidence"] and set(findings[finding_id]["evidence"]) <= evidence_ids
    assert scenarios["C"]["plausibility"] >= scenarios["A"]["plausibility"]
    assert inv["recommendation_detail"]["scenario"] in {"B", "C"}
    graph = inv["evidence_graph"]
    assert {n["kind"] for n in graph["nodes"]} >= {"document", "parcel", "court_case"}
    assert graph["edges"]


def test_unavailable_source_is_partial_and_never_claims_absence(make_user_client, insert_doc, cleanup_investigations, monkeypatch):
    import sa_investigation

    def boom(*_args, **_kwargs):
        raise RuntimeError("litigation register offline")

    monkeypatch.setattr(sa_investigation, "_lookup_court", boom)
    admin, headers, _ = make_user_client("ADMIN", prefix="sasu")
    survey, village = _unique_parcel()
    insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2017")
    doc = insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2023")
    inv = _run(admin, headers, doc)
    assert inv["sources"]["court"]["status"] == "SOURCE UNAVAILABLE"
    unavailable = _find(inv, "SOURCE_UNAVAILABLE", "MEDIUM")
    assert unavailable and "UNKNOWN" in unavailable[0]["description"]
    assert inv["partial"] is True
    assert inv["confidences"]["court_case_match"]["label"] == "SOURCE UNAVAILABLE"
    assert inv["recommendation"] == "FURTHER_VERIFICATION"
    assert any("unavailable" in item.lower() for item in inv["recommendation_detail"]["uncertainties"])
    scenarios = {s["key"]: s for s in inv["scenarios"]}
    assert any("unavailable" in item.lower() for item in scenarios["A"]["uncertainties"])


def test_explain_endpoint_answers_from_structured_findings(make_user_client, insert_doc, cleanup_investigations):
    admin, headers, _ = make_user_client("ADMIN", prefix="saexp")
    survey, village = _unique_parcel()
    insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2017")
    doc = insert_doc(survey=survey, village=village, owner="Fake Seller", year="2023", doc_type="Sale Deed",
                     extra={"seller_name": "Fake Seller", "buyer_name": "Buyer Person"})
    inv = _run(admin, headers, doc)
    response = admin.get(f"/api/sa/investigations/{inv['investigation_id']}/explain", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["question"] == "Why did SA recommend REJECT?"
    assert any("R1" in line for line in body["answer"])
    assert body["confidences"]["overall_evidence"]["explanation"]
    assert body["what_would_change"]
    assert [f for f in body["critical_findings"] if f["type"] == "OWNERSHIP_MISMATCH"]


# --------------------------------------------------------------------------- #
# security
# --------------------------------------------------------------------------- #

def test_rbac_officers_and_viewers_cannot_run_or_decide(make_user_client, insert_doc, cleanup_investigations):
    admin, headers, _ = make_user_client("ADMIN", prefix="sarb")
    survey, village = _unique_parcel()
    insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2017")
    doc = insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2023")
    inv = _run(admin, headers, doc)
    officer, o_headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="sarbvo")
    data_officer, d_headers, _ = make_user_client("DATA_OFFICER", prefix="sarbdo")
    viewer, v_headers, _ = make_user_client("VIEWER", prefix="sarbv")
    # verification officer: read-only
    assert officer.get(f"/api/sa/investigations/{inv['investigation_id']}", headers=o_headers).status_code == 200
    assert officer.post("/api/sa/investigations", json={"document_id": doc}, headers=o_headers).status_code == 403
    assert officer.post(f"/api/sa/investigations/{inv['investigation_id']}/decision",
                        json={"decision": "APPROVE", "password": DEFAULT_TEST_PASSWORD}, headers=o_headers).status_code == 403
    # data officer / viewer: nothing
    for client, h in ((data_officer, d_headers), (viewer, v_headers)):
        assert client.get("/api/sa/investigations", headers=h).status_code == 403
        assert client.get(f"/api/sa/investigations/{inv['investigation_id']}", headers=h).status_code == 403
        assert client.post("/api/sa/investigations", json={"document_id": doc}, headers=h).status_code == 403
    # anonymous (fresh client: the admin client carries a session cookie)
    from fastapi.testclient import TestClient
    from main import app
    assert TestClient(app).get(f"/api/sa/investigations/{inv['investigation_id']}").status_code in {401, 403}


def test_actor_isolation_other_admin_cannot_cancel_or_replay_request(make_user_client, insert_doc, cleanup_investigations):
    admin_a, headers_a, _ = make_user_client("ADMIN", prefix="saiso")
    admin_b, headers_b, _ = make_user_client("ADMIN", prefix="saisob")
    survey, village = _unique_parcel()
    insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2017")
    doc = insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2023")
    inv = _run(admin_a, headers_a, doc)
    # the background task belongs to admin A
    assert admin_b.post(f"/api/sa/investigations/{inv['investigation_id']}/cancel", headers=headers_b).status_code == 404
    replay = admin_b.post("/api/sa/investigations", json={"document_id": doc, "request_id": inv["request_id"]}, headers=headers_b)
    assert replay.status_code == 404
    # document visibility rules are the existing ones (admins see documents); no browser identity override
    response = admin_b.get(f"/api/sa/investigations/{inv['investigation_id']}", headers={**headers_b, "X-User-Role": "ADMIN", "X-User-Id": "1"})
    assert response.status_code == 200
    assert response.json()["investigation"]["created_by"] != ""


def test_investigation_of_hidden_document_is_not_visible(make_user_client, insert_doc, cleanup_investigations):
    admin, headers, _ = make_user_client("ADMIN", prefix="sahid")
    survey, village = _unique_parcel()
    insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2017")
    doc = insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2023", status="PENDING")
    inv = _run(admin, headers, doc)
    officer, o_headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="sahidvo")
    assert officer.get(f"/api/sa/investigations/{inv['investigation_id']}", headers=o_headers).status_code == 200
    # a document the officer cannot see under the existing rules -> investigation not found
    import server
    with server.get_db() as db:
        db.execute("UPDATE documents SET status='PENDING' WHERE id=?", (doc,))
    import mapping
    original = mapping._map_document_visible

    def deny_officer(row, user):
        if user.get("role") == "VERIFICATION_OFFICER":
            return False
        return original(row, user)

    mapping._map_document_visible = deny_officer
    try:
        assert officer.get(f"/api/sa/investigations/{inv['investigation_id']}", headers=o_headers).status_code == 404
        listed = officer.get("/api/sa/investigations", headers=o_headers).json()["investigations"]
        assert inv["investigation_id"] not in [item["investigation_id"] for item in listed]
    finally:
        mapping._map_document_visible = original


def test_sa_never_executes_land_changes_directly(make_user_client, insert_doc, cleanup_investigations):
    """SA orchestrates; the administrator decision changes ONLY the
    investigation. Documents, registers and parcels are untouched."""
    import pathlib
    source = pathlib.Path("sa_investigation.py").read_text(encoding="utf-8")
    for forbidden in ("UPDATE documents", "INSERT INTO land_mutations", "UPDATE land_mutations", "complete_mutation(",
                      "review_mutation(", "create_mutation(", "_transition_mutation(", "UPDATE properties", "INSERT INTO properties"):
        assert forbidden not in source, forbidden
    admin, headers, _ = make_user_client("ADMIN", prefix="sanoex")
    survey, village = _unique_parcel()
    insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2017")
    doc = insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2023", status="PENDING")
    inv = _run(admin, headers, doc)
    import server
    with server.get_db() as db:
        mutations_before = db.execute("SELECT COUNT(*) FROM land_mutations").fetchone()[0]
        status_before = db.execute("SELECT status FROM documents WHERE id=?", (doc,)).fetchone()[0]
    decided = admin.post(f"/api/sa/investigations/{inv['investigation_id']}/decision",
                         json={"decision": "APPROVE", "password": DEFAULT_TEST_PASSWORD, "note": "fine"}, headers=headers)
    assert decided.status_code == 200, decided.text
    with server.get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM land_mutations").fetchone()[0] == mutations_before
        assert db.execute("SELECT status FROM documents WHERE id=?", (doc,)).fetchone()[0] == status_before
    assert decided.json()["investigation"]["state"] == "APPROVED"


# --------------------------------------------------------------------------- #
# approval flow
# --------------------------------------------------------------------------- #

def test_recommendation_stays_proposed_until_administrator_approves(make_user_client, insert_doc, cleanup_investigations):
    admin, headers, _ = make_user_client("ADMIN", prefix="sapr")
    survey, village = _unique_parcel()
    insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2017")
    doc = insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2023")
    inv = _run(admin, headers, doc)
    assert inv["state"] == "READY_FOR_REVIEW" and inv["decidable"] is True
    assert inv["proposal_id"]
    assert _proposal_status(inv["proposal_id"]) == "PROPOSED"
    proposal = admin.get(f"/api/admin/ai-approval/proposals/{inv['proposal_id']}", headers=headers).json()["proposal"]
    assert proposal["action_type"] == "SA_INVESTIGATION_DECISION"
    assert proposal["created_by"] == "SA_INVESTIGATION"
    assert proposal["status"] == "PROPOSED"
    assert proposal["target_ids"] == [inv["investigation_id"]]
    # visible in the standard approvals queue
    queue = admin.get("/api/admin/ai-approval/proposals", headers=headers, params={"status_filter": "PROPOSED"}).json()
    assert inv["proposal_id"] in [item["proposal_id"] for item in queue["proposals"]]
    # nothing was decided by SA itself
    assert inv["decision"] in (None, "")
    assert not _audit_rows("SA_INVESTIGATION_DECIDED", inv["investigation_id"])


def test_wrong_password_blocks_execution_and_missing_override_note(make_user_client, insert_doc, cleanup_investigations):
    admin, headers, _ = make_user_client("ADMIN", prefix="sawp")
    survey, village = _unique_parcel()
    insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2017")
    doc = insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2023")
    inv = _run(admin, headers, doc)
    url = f"/api/sa/investigations/{inv['investigation_id']}/decision"
    assert admin.post(url, json={"decision": "APPROVE", "password": "definitely wrong"}, headers=headers).status_code == 401
    assert admin.post(url, json={"decision": "APPROVE"}, headers=headers).status_code == 401
    assert admin.post(url, json={"decision": "NUKE", "password": DEFAULT_TEST_PASSWORD}, headers=headers).status_code == 400
    # override (REJECT against an APPROVE recommendation) needs a note - checked before the password is even looked at
    assert admin.post(url, json={"decision": "REJECT", "password": DEFAULT_TEST_PASSWORD}, headers=headers).status_code == 400
    after = _detail(admin, headers, inv["investigation_id"])
    assert after["state"] == "READY_FOR_REVIEW"
    assert _proposal_status(inv["proposal_id"]) == "PROPOSED"
    assert not _audit_rows("SA_INVESTIGATION_DECIDED", inv["investigation_id"])


def test_correct_password_executes_once_via_existing_cas_and_replays_safely(make_user_client, insert_doc, cleanup_investigations):
    admin, headers, _ = make_user_client("ADMIN", prefix="saok")
    survey, village = _unique_parcel()
    insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2017")
    doc = insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2023")
    inv = _run(admin, headers, doc)
    url = f"/api/sa/investigations/{inv['investigation_id']}/decision"
    first = admin.post(url, json={"decision": "APPROVE", "password": DEFAULT_TEST_PASSWORD, "note": "looks right"}, headers=headers)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["investigation"]["state"] == "APPROVED"
    assert body["investigation"]["decision"] == "APPROVE"
    assert body["investigation"]["decision_note"] == "looks right"
    assert body["override"] is False
    assert body["proposal"]["status"] == "EXECUTED"
    assert _proposal_status(inv["proposal_id"]) == "EXECUTED"
    decided_at = body["investigation"]["decided_at"]
    # a second decision is refused (state guard) and nothing re-executes
    second = admin.post(url, json={"decision": "APPROVE", "password": DEFAULT_TEST_PASSWORD}, headers=headers)
    assert second.status_code == 409
    # replaying the underlying proposal approval never re-executes either
    replay = admin.post(f"/api/admin/ai-approval/proposals/{inv['proposal_id']}/approve",
                        json={"note": "again", "password": DEFAULT_TEST_PASSWORD}, headers=headers)
    assert replay.status_code == 200, replay.text
    assert replay.json()["proposal"].get("replayed") is True
    assert _detail(admin, headers, inv["investigation_id"])["decided_at"] == decided_at
    assert len(_audit_rows("SA_INVESTIGATION_DECIDED", inv["investigation_id"])) == 1


def test_concurrent_decisions_execute_exactly_once(make_user_client, insert_doc, cleanup_investigations, monkeypatch):
    import ai_governance

    admin, headers, _ = make_user_client("ADMIN", prefix="sarace")
    survey, village = _unique_parcel()
    insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2017")
    doc = insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2023")
    inv = _run(admin, headers, doc)
    real_current_state = ai_governance._current_state

    def slow_current_state(action, ids):
        time.sleep(0.2)
        return real_current_state(action, ids)

    monkeypatch.setattr(ai_governance, "_current_state", slow_current_state)
    url = f"/api/sa/investigations/{inv['investigation_id']}/decision"
    results = []
    barrier = threading.Barrier(2)

    def run():
        barrier.wait()
        results.append(admin.post(url, json={"decision": "APPROVE", "password": DEFAULT_TEST_PASSWORD}, headers=headers))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
        assert not thread.is_alive()
    codes = sorted(r.status_code for r in results)
    assert set(codes) <= {200, 409}, [r.text for r in results]
    assert 200 in codes
    assert _detail(admin, headers, inv["investigation_id"])["state"] == "APPROVED"
    assert len(_audit_rows("SA_INVESTIGATION_DECIDED", inv["investigation_id"])) == 1
    assert _proposal_status(inv["proposal_id"]) == "EXECUTED"


def test_zombie_runner_cannot_complete_a_reclaimed_investigation(make_user_client, insert_doc, cleanup_investigations):
    """Ownership guard: once another execution claims the row, every write of
    the stale runner fails closed and the row keeps the new owner's token."""
    import server
    import sa_investigation

    admin, headers, _ = make_user_client("ADMIN", prefix="sazom")
    survey, village = _unique_parcel()
    doc = insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2023")
    document = sa_investigation.load_document(doc)
    actor = {"id": 1, "email": "zombie@example.test", "full_name": "Zombie", "role": "ADMIN"}
    created = sa_investigation.create_investigation(document, actor, trigger="TEST")
    inv_id = created["investigation"]["investigation_id"]
    stale = sa_investigation._Run(inv_id, None)
    stale.claim()
    fresh = sa_investigation._Run(inv_id, None)
    fresh.claim()
    with pytest.raises(sa_investigation._OwnershipLost):
        stale.stage("extract", "DONE", state="READY_FOR_REVIEW")
    with server.get_db() as db:
        row = db.execute("SELECT run_token, state, attempts FROM land_investigations WHERE investigation_id=?", (inv_id,)).fetchone()
    assert row["run_token"] == fresh.token
    assert row["state"] == "EXTRACTING"
    assert row["attempts"] == 2
    # the stale runner's failure path is also silenced (no overwrite)
    stale.fail("ZOMBIE", "should not be written")
    with server.get_db() as db:
        assert db.execute("SELECT state FROM land_investigations WHERE investigation_id=?", (inv_id,)).fetchone()[0] == "EXTRACTING"
    # an unfinished investigation is never shown as complete
    item = sa_investigation.load_investigation(inv_id)
    view = sa_investigation.detail(item, actor)
    assert view["complete"] is False and view["findings"] == [] and view["scenarios"] == []


def test_failed_investigation_is_never_complete_and_can_be_retried(make_user_client, insert_doc, cleanup_investigations, monkeypatch):
    import sa_investigation

    admin, headers, _ = make_user_client("ADMIN", prefix="safail")
    survey, village = _unique_parcel()
    insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2017")
    doc = insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2023")
    real = sa_investigation._stage_ownership
    calls = {"n": 0}

    def flaky(ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient failure")
        return real(ctx)

    monkeypatch.setattr(sa_investigation, "_stage_ownership", flaky)
    body = _investigate(admin, headers, doc)
    inv = body["investigation"]
    assert inv["state"] == "FAILED"
    assert inv["complete"] is False and inv["decidable"] is False
    assert inv["failure"]["message"]
    assert inv["findings"] == []
    assert admin.post(f"/api/sa/investigations/{inv['investigation_id']}/decision",
                      json={"decision": "APPROVE", "password": DEFAULT_TEST_PASSWORD}, headers=headers).status_code == 409
    retried = admin.post(f"/api/sa/investigations/{inv['investigation_id']}/retry", headers=headers)
    assert retried.status_code == 200, retried.text
    after = _detail(admin, headers, inv["investigation_id"])
    assert after["state"] == "READY_FOR_REVIEW"
    assert after["attempts"] == 2
    assert _audit_rows("SA_INVESTIGATION_FAILED", inv["investigation_id"])
    assert _audit_rows("SA_INVESTIGATION_RETRY", inv["investigation_id"])


# --------------------------------------------------------------------------- #
# audit
# --------------------------------------------------------------------------- #

def test_lifecycle_is_audited_and_password_is_never_stored(make_user_client, insert_doc, cleanup_investigations):
    admin, headers, email = make_user_client("ADMIN", prefix="saaud")
    survey, village = _unique_parcel()
    insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2017")
    doc = insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2023")
    inv = _run(admin, headers, doc)
    inv_id = inv["investigation_id"]
    evidence_id = inv["evidence"][0]["evidence_id"]
    assert admin.post(f"/api/sa/investigations/{inv_id}/events", json={"event_type": "EVIDENCE_OPENED", "ref": evidence_id}, headers=headers).status_code == 200
    assert admin.post(f"/api/sa/investigations/{inv_id}/events", json={"event_type": "SCENARIO_VIEWED", "ref": "B"}, headers=headers).status_code == 200
    assert admin.post(f"/api/sa/investigations/{inv_id}/events", json={"event_type": "SCENARIO_VIEWED", "ref": "Z"}, headers=headers).status_code == 400
    decided = admin.post(f"/api/sa/investigations/{inv_id}/decision",
                         json={"decision": "APPROVE", "password": DEFAULT_TEST_PASSWORD, "note": "ok"}, headers=headers)
    assert decided.status_code == 200
    actions = {row["action"] for row in _audit_rows("SA_INVESTIGATION_%", inv_id)}
    for expected in ("SA_INVESTIGATION_CREATED", "SA_INVESTIGATION_DOCUMENT_UPLOADED", "SA_INVESTIGATION_EXTRACTED",
                     "SA_INVESTIGATION_MATCHED", "SA_INVESTIGATION_RECOMMENDED", "SA_INVESTIGATION_VIEWED",
                     "SA_INVESTIGATION_EVIDENCE_OPENED", "SA_INVESTIGATION_SCENARIO_VIEWED", "SA_INVESTIGATION_DECIDED"):
        assert expected in actions, expected
    import server
    with server.get_db() as db:
        blobs = [json.dumps(dict(r), default=str) for r in db.execute("SELECT * FROM audit WHERE detail LIKE ? AND ts>=?", (f"%{inv_id}%", _TEST_STARTED["ts"])).fetchall()]
        blobs += [json.dumps(dict(r), default=str) for r in db.execute("SELECT * FROM investigation_events WHERE investigation_id=?", (inv_id,)).fetchall()]
        blobs += [json.dumps(dict(r), default=str) for r in db.execute("SELECT * FROM land_investigations WHERE investigation_id=?", (inv_id,)).fetchall()]
        blobs += [json.dumps(dict(r), default=str) for r in db.execute("SELECT * FROM ai_proposals WHERE proposal_id=?", (inv["proposal_id"],)).fetchall()]
        blobs += [json.dumps(dict(r), default=str) for r in db.execute("SELECT * FROM ai_approval_events WHERE proposal_id=?", (inv["proposal_id"],)).fetchall()]
    joined = "\n".join(blobs)
    assert DEFAULT_TEST_PASSWORD not in joined
    assert "password" not in joined.lower() or re.search(r"password[\"']?\s*[:=]\s*[\"']?strong", joined, re.I) is None


def test_override_requires_note_and_is_audited(make_user_client, insert_doc, cleanup_investigations):
    admin, headers, _ = make_user_client("ADMIN", prefix="saovr")
    survey, village = _unique_parcel()
    insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2017")
    doc = insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2023")
    inv = _run(admin, headers, doc)
    assert inv["recommendation"] == "APPROVE"
    url = f"/api/sa/investigations/{inv['investigation_id']}/decision"
    decided = admin.post(url, json={"decision": "REJECT", "password": DEFAULT_TEST_PASSWORD, "note": "Applicant withdrew"}, headers=headers)
    assert decided.status_code == 200, decided.text
    body = decided.json()
    assert body["override"] is True
    assert body["investigation"]["state"] == "REJECTED"
    assert body["investigation"]["decision_override"] is True
    assert body["investigation"]["decision_note"] == "Applicant withdrew"
    overrides = _audit_rows("SA_INVESTIGATION_OVERRIDE", inv["investigation_id"])
    assert overrides and "Applicant withdrew" in overrides[0]["detail"]
    # the override went through its own governance proposal with HIGH risk, not SA's prepared one
    assert body["proposal"]["proposal_id"] != inv["proposal_id"]
    proposal = admin.get(f"/api/admin/ai-approval/proposals/{body['proposal']['proposal_id']}", headers=headers).json()["proposal"]
    assert proposal["risk"] == "HIGH" and proposal["status"] == "EXECUTED"
    assert proposal["proposed_state"]["override"] is True


# --------------------------------------------------------------------------- #
# upload hook / alerts / list
# --------------------------------------------------------------------------- #

def test_admin_upload_auto_starts_investigation_when_enabled(make_user_client, insert_doc, cleanup_investigations, monkeypatch):
    import sa_investigation

    admin, headers, email = make_user_client("ADMIN", prefix="saup")
    officer, _, o_email = make_user_client("VERIFICATION_OFFICER", prefix="saupvo")
    survey, village = _unique_parcel()
    insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2017")
    doc = insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2023")
    import server
    with server.get_db() as db:
        admin_row = dict(db.execute("SELECT id, email, full_name, role FROM users WHERE email=?", (email,)).fetchone())
        officer_row = dict(db.execute("SELECT id, email, full_name, role FROM users WHERE email=?", (o_email,)).fetchone())
    monkeypatch.setenv("SA_AUTO_INVESTIGATE", "0")
    assert sa_investigation.auto_start_for_upload(doc, admin_row) is None
    monkeypatch.setenv("SA_AUTO_INVESTIGATE", "1")
    assert sa_investigation.auto_start_for_upload(doc, officer_row) is None  # only administrator uploads
    ref = sa_investigation.auto_start_for_upload(doc, admin_row)
    assert ref and ref["auto_started"] is True
    assert ref["state"] == "READY_FOR_REVIEW"
    inv = _detail(admin, headers, ref["investigation_id"])
    assert inv["trigger"] == "UPLOAD"
    # uploading the same content again does not start a second investigation
    again = sa_investigation.auto_start_for_upload(doc, admin_row)
    assert again["already_investigated"] is True


def test_process_endpoint_returns_investigation_reference(make_user_client, cleanup_investigations, monkeypatch, tmp_path):
    import io
    import server
    from PIL import Image

    monkeypatch.setenv("SA_AUTO_INVESTIGATE", "1")
    admin, headers, _ = make_user_client("ADMIN", prefix="saproc")
    survey, village = _unique_parcel()

    def fake_pipeline(_content, filename, *args):
        return server.extract_fields_from_ocr(f"Owner Name: Upload Owner\nVillage: {village}\nKhasra No: {survey}", filename)

    monkeypatch.setattr(server, "run_ocr_pipeline", fake_pipeline)
    image = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(image, "PNG")
    response = admin.post("/api/process", headers=headers, files={"file": (f"{survey}.png", image.getvalue(), "image/png")})
    assert response.status_code == 200, response.text
    body = response.json()
    try:
        assert body.get("sa_investigation"), body.keys()
        ref = body["sa_investigation"]
        assert ref["investigation_id"].startswith("INV-")
        inv = _detail(admin, headers, ref["investigation_id"])
        assert inv["document_id"] == body["id"]
        assert inv["reproducibility"]["fingerprint_basis"] == "FILE_CONTENT"
    finally:
        import os
        with server.get_db() as db:
            db.execute("DELETE FROM documents WHERE id=?", (body["id"],))
        for ext in (".png", ".pdf", ".jpg", ".jpeg"):
            path = os.path.join(server.UPLOADS_DIR, body["id"] + ext)
            if os.path.exists(path):
                os.remove(path)


def test_alerts_reference_investigation_and_deep_link(make_user_client, insert_doc, insert_court_case, cleanup_investigations):
    admin, headers, _ = make_user_client("ADMIN", prefix="saal")
    survey, village = _unique_parcel()
    insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2017")
    insert_court_case(survey, village, parties="Kamla Devi v. Z")
    doc = insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2023")
    inv = _run(admin, headers, doc)
    alerts = admin.get("/api/sa/investigations/alerts", headers=headers).json()["alerts"]
    mine = [a for a in alerts if a["investigation_id"] == inv["investigation_id"]]
    assert mine
    for alert in mine:
        assert alert["severity"] in {"HIGH", "CRITICAL"}
        assert alert["why"]
        assert alert["resolution"]
        assert any(link["label"] == "Open Investigation" for link in alert["links"])
    assert any(any(link["label"] == "Open Court Case" for link in alert["links"]) for alert in mine)
    listed = admin.get("/api/sa/investigations", headers=headers, params={"document_id": doc}).json()
    assert listed["count"] == 1
    card = listed["investigations"][0]
    assert card["finding_counts"]["HIGH"] >= 1
    assert card["recommendation"] in {"FURTHER_VERIFICATION", "REJECT"}
    assert card["links"] and card["document"]["id"] == doc


def test_list_and_detail_query_counts_stay_bounded(query_counter, make_user_client, insert_doc, cleanup_investigations):
    admin, headers, _ = make_user_client("ADMIN", prefix="saq")
    ids = []
    for _ in range(3):
        survey, village = _unique_parcel()
        insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2017")
        doc = insert_doc(survey=survey, village=village, owner="Kamla Devi", year="2023")
        ids.append(_run(admin, headers, doc)["investigation_id"])
    query_counter["queries"] = 0
    query_counter["ddl"] = 0
    query_counter["sql"] = []
    assert admin.get("/api/sa/investigations", headers=headers).status_code == 200
    assert query_counter["ddl"] == 0
    assert query_counter["queries"] <= 6
    query_counter["queries"] = 0
    query_counter["sql"] = []
    assert admin.get(f"/api/sa/investigations/{ids[0]}", headers=headers).status_code == 200
    assert query_counter["ddl"] == 0
    assert query_counter["queries"] <= 8
