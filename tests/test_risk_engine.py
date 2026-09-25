"""Phase 14 — LAND RISK ENGINE tests: every deterministic rule, severity,
verdict mapping, evidence, and the non-authoritative disclaimer."""
import time
import uuid

import land_intel
from land_intel import compute_land_risk, land_identity


def _land(survey, village="Riskville", records=None):
    key, land_id = land_identity(survey, village)
    return {
        "land_id": land_id,
        "land_key": key,
        "survey": survey,
        "khasra": survey,
        "village": village,
        "tehsil": "Sadar",
        "district": "Ghaziabad",
        "records": records or [],
    }


def _record(doc_id, owner, year, area="2.0 ha", status="APPROVED", **extra):
    base = {
        "id": doc_id, "filename": f"{doc_id}.pdf", "status": status,
        "doc_type": "Land Record", "year": year, "owner": owner, "area": area,
        "fields": {}, "created_at": float(year),
    }
    base.update(extra)
    return base


def _codes(risk):
    return {flag["code"]: flag for flag in risk["flags"]}


def _with_registers(monkeypatch, encumbrances=(), mutations=()):
    monkeypatch.setattr(land_intel, "_land_register_rows",
                        lambda land, registers=None: (list(encumbrances), list(mutations)))


# -- verdicts -----------------------------------------------------------------

def test_clear_verdict_for_single_consistent_record(monkeypatch):
    _with_registers(monkeypatch)
    land = _land("R1", records=[_record("a", "Ram Singh", "2022")])
    risk = compute_land_risk(land)
    assert risk["verdict"] == "CLEAR"
    assert risk["flags"] == []
    assert risk["legal_authority"] is False
    assert "not a legally authoritative" in risk["disclaimer"]


def test_verdict_order_review_with_info_flags(monkeypatch):
    _with_registers(monkeypatch)
    land = _land("R2", records=[
        _record("a", "Old Owner", "2020"),
        _record("b", "New Owner", "2024"),
        _record("c", "Disputed Owner", "2024", status="REJECTED"),
    ])
    risk = compute_land_risk(land)
    codes = _codes(risk)
    assert "OWNER_CHANGE_NO_MUTATION" in codes  # REVIEW severity
    assert "REJECTED_CONFLICT_COPY" in codes    # INFO severity
    assert risk["verdict"] == "REVIEW"
    assert risk["counts"]["review"] >= 1
    assert risk["counts"]["info"] >= 1


# -- rule 1: active encumbrance ------------------------------------------------

def test_active_encumbrance_is_high_risk_with_evidence(monkeypatch):
    enc = {"id": "enc1", "status": "ACTIVE", "lender": "Example Bank", "reference_no": "LN-1",
           "amount": 850000, "start_date": "2024-11-05", "release_date": None, "evidence_doc_id": "doc9"}
    _with_registers(monkeypatch, encumbrances=[enc])
    land = _land("R3", records=[_record("a", "Mahesh Verma", "2021")])
    risk = compute_land_risk(land)
    flag = _codes(risk)["ACTIVE_ENCUMBRANCE"]
    assert risk["verdict"] == "HIGH_RISK"
    assert flag["severity"] == "HIGH"
    assert "Example Bank" in flag["detail"]
    assert {"type": "encumbrance", "ref": "enc1"} == {k: flag["evidence"][0][k] for k in ("type", "ref")}
    assert "doc9" in risk["evidence_documents"]


def test_released_encumbrance_does_not_flag(monkeypatch):
    enc = {"id": "enc1", "status": "RELEASED", "lender": "Example Bank", "reference_no": "LN-1",
           "amount": 100, "start_date": "2020-01-01", "release_date": "2024-01-01"}
    _with_registers(monkeypatch, encumbrances=[enc])
    land = _land("R4", records=[_record("a", "Ram Singh", "2022")])
    risk = compute_land_risk(land)
    assert "ACTIVE_ENCUMBRANCE" not in _codes(risk)


# -- rule 2: transfer while encumbrance live -----------------------------------

def test_sale_during_encumbrance(monkeypatch):
    enc = {"id": "enc1", "status": "RELEASED", "lender": "Bank", "reference_no": "LN-2",
           "amount": 100, "start_date": "2020-01-01", "release_date": "2024-01-01"}
    mut = {"id": "m1", "mutation_no": "M-2023-0001", "status": "COMPLETED",
           "deed_date": "2022-06-15", "previous_owner": "A", "new_owner": "B"}
    _with_registers(monkeypatch, encumbrances=[enc], mutations=[mut])
    land = _land("R5", records=[])
    risk = compute_land_risk(land)
    flag = _codes(risk)["SALE_DURING_ENCUMBRANCE"]
    assert flag["severity"] == "HIGH"
    refs = {item["ref"] for item in flag["evidence"]}
    assert refs == {"m1", "enc1"}


def test_sale_after_release_does_not_flag(monkeypatch):
    enc = {"id": "enc1", "status": "RELEASED", "lender": "Bank", "reference_no": "LN-2",
           "amount": 100, "start_date": "2020-01-01", "release_date": "2021-01-01"}
    mut = {"id": "m1", "mutation_no": "M-2022-0001", "status": "COMPLETED",
           "deed_date": "2022-06-15", "previous_owner": "A", "new_owner": "B"}
    _with_registers(monkeypatch, encumbrances=[enc], mutations=[mut])
    risk = compute_land_risk(_land("R6"))
    assert "SALE_DURING_ENCUMBRANCE" not in _codes(risk)


# -- rule 3: owner conflict in the same year ------------------------------------

def test_owner_conflict_same_year_is_high(monkeypatch):
    _with_registers(monkeypatch)
    land = _land("R7", records=[
        _record("a", "Jagdish Yadav", "2021"),
        _record("b", "Kallu Yadav", "2021"),
    ])
    risk = compute_land_risk(land)
    flag = _codes(risk)["OWNER_CONFLICT_YEAR"]
    assert flag["severity"] == "HIGH"
    assert risk["verdict"] == "HIGH_RISK"
    assert {item["ref"] for item in flag["evidence"]} == {"a", "b"}


def test_same_owner_spelling_variation_is_not_conflict(monkeypatch):
    _with_registers(monkeypatch)
    land = _land("R8", records=[
        _record("a", "Ram Swaroop Sharma", "2021"),
        _record("b", "Ramswaroop Sharma", "2021"),
    ])
    risk = compute_land_risk(land)
    assert "OWNER_CONFLICT_YEAR" not in _codes(risk)


# -- rule 4: owner change without mutation ---------------------------------------

def test_owner_change_without_mutation_is_review(monkeypatch):
    _with_registers(monkeypatch)
    land = _land("R9", records=[
        _record("a", "Harish Chandra", "2018"),
        _record("b", "Premwati Devi", "2022"),
    ])
    risk = compute_land_risk(land)
    flag = _codes(risk)["OWNER_CHANGE_NO_MUTATION"]
    assert flag["severity"] == "REVIEW"
    assert "Harish Chandra" in flag["detail"] and "Premwati Devi" in flag["detail"]
    assert risk["verdict"] == "REVIEW"


def test_owner_change_bridged_by_completed_mutation_is_clear(monkeypatch):
    mut = {"id": "m1", "mutation_no": "M-2023-0001", "status": "COMPLETED",
           "deed_date": "2023-02-10", "previous_owner": "Ram Swaroop Sharma", "new_owner": "Amit Sharma"}
    _with_registers(monkeypatch, mutations=[mut])
    land = _land("R10", records=[
        _record("a", "Ram Swaroop Sharma", "2019"),
        _record("b", "Amit Sharma", "2023"),
    ])
    risk = compute_land_risk(land)
    assert risk["verdict"] == "CLEAR"
    assert "OWNER_CHANGE_NO_MUTATION" not in _codes(risk)


def test_pending_mutation_adds_info_flag(monkeypatch):
    mut = {"id": "m1", "mutation_no": "M-2026-0012", "status": "UNDER_REVIEW",
           "deed_date": "2024-08-19", "previous_owner": "Old Owner", "new_owner": "New Owner"}
    _with_registers(monkeypatch, mutations=[mut])
    land = _land("R11", records=[
        _record("a", "Old Owner", "2020"),
        _record("b", "New Owner", "2024"),
    ])
    risk = compute_land_risk(land)
    assert "PENDING_MUTATION" in _codes(risk)
    assert "OWNER_CHANGE_NO_MUTATION" not in _codes(risk)
    assert risk["verdict"] == "CLEAR"


# -- rule 5: area jump -------------------------------------------------------------

def test_area_jump_without_partition_is_review(monkeypatch):
    _with_registers(monkeypatch)
    land = _land("R12", records=[
        _record("a", "Shyam Lal", "2019", area="2.50 ha"),
        _record("b", "Shyam Lal", "2023", area="4.30 ha"),
    ])
    risk = compute_land_risk(land)
    flag = _codes(risk)["AREA_JUMP"]
    assert flag["severity"] == "REVIEW"
    assert "72%" in flag["detail"]


def test_area_change_bridged_by_partition_mutation(monkeypatch):
    mut = {"id": "m1", "mutation_no": "M-2022-0009", "status": "COMPLETED", "reason_type": "PARTITION",
           "deed_date": "2022-05-01", "previous_owner": "Shyam Lal", "new_owner": "Shyam Lal"}
    _with_registers(monkeypatch, mutations=[mut])
    land = _land("R13", records=[
        _record("a", "Shyam Lal", "2019", area="2.50 ha"),
        _record("b", "Shyam Lal", "2023", area="4.30 ha"),
    ])
    risk = compute_land_risk(land)
    assert "AREA_JUMP" not in _codes(risk)


def test_small_area_change_is_not_flagged(monkeypatch):
    _with_registers(monkeypatch)
    land = _land("R14", records=[
        _record("a", "Ram Singh", "2019", area="2.50 ha"),
        _record("b", "Ram Singh", "2023", area="2.60 ha"),
    ])
    risk = compute_land_risk(land)
    assert "AREA_JUMP" not in _codes(risk)


# -- rule 6: conflicting duplicates --------------------------------------------------

def test_duplicate_conflict_same_owner_different_area(monkeypatch):
    _with_registers(monkeypatch)
    land = _land("R15", records=[
        _record("a", "Dinesh Chand", "2022", area="1.80 ha"),
        _record("b", "Dinesh Chand", "2022", area="3.20 ha"),
    ])
    risk = compute_land_risk(land)
    flag = _codes(risk)["DUPLICATE_CONFLICT"]
    assert flag["severity"] == "REVIEW"
    assert {item["ref"] for item in flag["evidence"]} == {"a", "b"}


# -- rule 7: rejected conflicting copy -------------------------------------------------

def test_rejected_conflict_copy_is_info(monkeypatch):
    _with_registers(monkeypatch)
    land = _land("R16", records=[
        _record("a", "Live Owner", "2021"),
        _record("b", "Disputed Owner", "2021", status="REJECTED"),
    ])
    risk = compute_land_risk(land)
    flag = _codes(risk)["REJECTED_CONFLICT_COPY"]
    assert flag["severity"] == "INFO"
    # owner conflict only counts live records — a rejected copy stays info-only
    assert "OWNER_CONFLICT_YEAR" not in _codes(risk)
    assert risk["verdict"] == "CLEAR"


# -- rule 8: chain gap ------------------------------------------------------------------

def test_chain_gap_is_informational(monkeypatch):
    _with_registers(monkeypatch)
    land = _land("R17", records=[
        _record("a", "Ram Singh", "1990"),
        _record("b", "Ram Singh", "2023"),
    ])
    risk = compute_land_risk(land)
    flag = _codes(risk)["CHAIN_GAP"]
    assert flag["severity"] == "INFO"
    assert "33 years" in flag["title"]
    assert risk["verdict"] == "CLEAR"


# -- rule 9: suspicious mutation sequence --------------------------------------------------

def test_owner_flip_flop_between_same_parties_is_high(monkeypatch):
    mutations = [
        {"id": "m1", "mutation_no": "M-2024-0001", "status": "COMPLETED", "deed_date": "2024-01-05",
         "previous_owner": "A", "new_owner": "B", "created_at": time.time() - 86400 * 10,
         "decided_at": time.time() - 86400 * 10},
        {"id": "m2", "mutation_no": "M-2024-0002", "status": "COMPLETED", "deed_date": "2024-06-05",
         "previous_owner": "B", "new_owner": "A", "created_at": time.time() - 86400 * 5,
         "decided_at": time.time() - 86400 * 5},
    ]
    _with_registers(monkeypatch, mutations=mutations)
    risk = compute_land_risk(_land("R18"))
    flag = _codes(risk)["SUSPICIOUS_MUTATION_SEQUENCE"]
    assert flag["severity"] == "HIGH"
    assert risk["verdict"] == "HIGH_RISK"


def test_rapid_mutation_burst_is_review(monkeypatch):
    now = time.time()
    mutations = [
        {"id": f"m{i}", "mutation_no": f"M-2026-{i:04d}", "status": "COMPLETED", "deed_date": "2026-01-05",
         "previous_owner": f"Party {i}", "new_owner": f"Party {i + 1}",
         "created_at": now - 86400 * 2, "decided_at": now - 86400 * 2}
        for i in range(3)
    ]
    _with_registers(monkeypatch, mutations=mutations)
    risk = compute_land_risk(_land("R19"))
    codes = _codes(risk)
    assert codes["SUSPICIOUS_MUTATION_SEQUENCE"]["severity"] == "REVIEW"
    assert "3 mutations" in codes["SUSPICIOUS_MUTATION_SEQUENCE"]["title"]


# -- rule 10: low-quality extraction ---------------------------------------------------------

def test_low_quality_extraction_is_info(monkeypatch):
    _with_registers(monkeypatch)
    land = _land("R20", records=[_record("a", "Blurred Name", "2023", mean_conf=44)])
    risk = compute_land_risk(land)
    assert _codes(risk)["LOW_QUALITY_EXTRACTION"]["severity"] == "INFO"


def test_good_quality_does_not_flag(monkeypatch):
    _with_registers(monkeypatch)
    land = _land("R21", records=[_record("a", "Clear Name", "2023", mean_conf=92)])
    risk = compute_land_risk(land)
    assert "LOW_QUALITY_EXTRACTION" not in _codes(risk)


# -- evidence & inputs ----------------------------------------------------------------------

def test_risk_collects_related_document_evidence(monkeypatch):
    enc = {"id": "enc1", "status": "ACTIVE", "lender": "Bank", "reference_no": "LN",
           "amount": 10, "start_date": "2024-01-01", "release_date": None, "evidence_doc_id": "deed"}
    _with_registers(monkeypatch, encumbrances=[enc])
    land = _land("R22", records=[_record("a", "Owner", "2023")])
    risk = compute_land_risk(land)
    assert risk["inputs"]["records"] == 1
    assert risk["inputs"]["encumbrances"] == 1
    assert risk["inputs"]["active_encumbrances"] == 1
    assert "deed" in risk["evidence_documents"]
