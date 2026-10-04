"""Parcel matching / resolution tests (spec §4).

Pins the evidence separation, ambiguity handling and normalization rules of
`mapping._resolve`: duplicate identifiers across villages, subdivision
mismatches, Unicode digit normalization, missing identifiers, contradictory
administrative data, close competing candidates, stable ranking, and the rule
that weak or ambiguous evidence can never produce an automatic MATCH.
"""
import uuid

import mapping
import server


def _seed_property(suffix, *, village="MatchVillage", district="Ghaziabad", taluka="Sadar",
                   survey="45", gat="", khasra="", sub_division=None):
    property_id = f"PROP-{suffix}"
    with server.get_db() as db:
        db.execute(
            """INSERT INTO properties (property_id,parcel_id,district,taluka,village,survey_number,
               gat_number,khasra_number,sub_division,area,area_unit,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (property_id, f"PARCEL-{suffix}", district, taluka, village, survey, gat, khasra,
             sub_division, 1.0, "ha", 1, 1),
        )
    return property_id


def _cleanup(*property_ids):
    with server.get_db() as db:
        for property_id in property_ids:
            db.execute("DELETE FROM properties WHERE property_id=?", (property_id,))


def test_duplicate_survey_number_in_different_villages_is_not_silently_matched():
    suffix = uuid.uuid4().hex[:8]
    a = _seed_property(f"A-{suffix}", village="Village Alpha", survey="45")
    b = _seed_property(f"B-{suffix}", village="Village Beta", survey="45")
    try:
        # Document from Village Alpha: the same survey number in Village Beta
        # must conflict, not match.
        result = mapping._resolve({
            "survey_number": {"value": "45"},
            "village": {"value": "Village Alpha"},
        })
        assert result["resolution_status"] in {"MATCH", "POSSIBLE MATCH", "NO MATCH", "AMBIGUOUS_MATCH"}
        best = result["matches"][0]["property"]["property_id"] if result["matches"] else None
        assert best == a
        beta = next(m for m in result["matches"] if m["property"]["property_id"] == b)
        assert "village" in beta["conflicting_fields"]
        assert beta["status"] == "NO MATCH"
        # And the top-level summary must reflect that conflicting candidates exist.
        assert result["conflicting_fields"] or any(
            m["conflicting_fields"] for m in result["matches"])
    finally:
        _cleanup(a, b)


def test_subdivision_mismatch_is_reported_as_conflicting_not_matched():
    suffix = uuid.uuid4().hex[:8]
    prop = _seed_property(f"SUB-{suffix}", survey="45", sub_division="1")
    try:
        result = mapping._resolve({
            "survey_number": {"value": "45"},
            "sub_division": {"value": "2"},
            "village": {"value": "MatchVillage"},
        })
        best = result["matches"][0]
        assert best["property"]["property_id"] == prop
        assert "sub_division" in best["conflicting_fields"]
        assert "sub_division" not in best["matched_fields"]
        # 45 must never be treated equal to 45/1: subdivision difference is evidence.
        assert "Conflicting sub_division." in " ".join(best["reasons"])
    finally:
        _cleanup(prop)


def test_survey_45_is_not_treated_as_equal_to_45_1():
    suffix = uuid.uuid4().hex[:8]
    prop = _seed_property(f"SDIV-{suffix}", survey="45/1")
    try:
        result = mapping._resolve({"survey_number": {"value": "45"}, "village": {"value": "MatchVillage"}})
        # Document survey 45 vs parcel 45/1: identifier conflict or nothing.
        if result["matches"]:
            best = result["matches"][0]
            if best["property"]["property_id"] == prop:
                assert best["status"] != "MATCH"
                assert "survey_number" in best["conflicting_fields"]
                assert "survey_number" not in best["matched_fields"]
                assert result["resolution_status"] != "MATCH"
    finally:
        _cleanup(prop)


def test_unicode_digits_and_formatting_normalize_without_loss():
    suffix = uuid.uuid4().hex[:8]
    # Parcel stores Devanagari digits; document uses ASCII (and vice versa).
    prop = _seed_property(f"UNI-{suffix}", survey="४५")  # Devanagari 45
    try:
        result = mapping._resolve({"survey_number": {"value": "45"}, "village": {"value": "MatchVillage"}})
        assert result["matches"], "Unicode digits in the register must match ASCII in the document"
        best = result["matches"][0]
        assert best["property"]["property_id"] == prop
        assert "survey_number" in best["matched_fields"]
    finally:
        _cleanup(prop)
    # Whitespace differences are normalized too.
    suffix2 = uuid.uuid4().hex[:8]
    prop2 = _seed_property(f"WS-{suffix2}", village="Match  Village")
    try:
        result = mapping._resolve({"village": {"value": "match village"}, "survey_number": {"value": "45"}})
        # Village with extra spaces normalizes to the same key.
        best = next((m for m in result["matches"] if m["property"]["property_id"] == prop2), None)
        if best is not None:
            assert "village" in best["matched_fields"]
    finally:
        _cleanup(prop2)


def test_missing_identifiers_yield_insufficient_evidence_not_match():
    result = mapping._resolve({})
    assert result["status"] == "INSUFFICIENT DATA"
    assert result["resolution_status"] == "INSUFFICIENT DATA"
    assert result["matches"] == []
    assert result["reasons"]


def test_geography_only_candidates_are_never_a_match():
    suffix = uuid.uuid4().hex[:8]
    prop = _seed_property(f"GEO-{suffix}", survey="12345")
    try:
        result = mapping._resolve({"village": {"value": "MatchVillage"}})
        assert result["matches"], "village-only lookup still returns candidates"
        assert result["resolution_status"] in {"INSUFFICIENT_EVIDENCE", "AMBIGUOUS_MATCH"}
        assert result["resolution_status"] != "MATCH"
        best = result["matches"][0]
        assert not set(best["matched_fields"]) & set(mapping.LAND_IDENTIFIER_FIELDS)
        assert best["evidence"]["positive"] == ["village"]
        assert best["evidence"]["source_reliability"]
    finally:
        _cleanup(prop)


def test_close_competing_candidates_are_ambiguous():
    suffix = uuid.uuid4().hex[:8]
    a = _seed_property(f"C1-{suffix}", survey="45", sub_division="1")
    b = _seed_property(f"C2-{suffix}", survey="45", sub_division="2")
    try:
        # Document identifies the village + survey but not the subdivision:
        # both subdivisions score identically -> ambiguity, never a pick.
        result = mapping._resolve({
            "survey_number": {"value": "45"},
            "village": {"value": "MatchVillage"},
        })
        assert result["resolution_status"] == "AMBIGUOUS_MATCH"
        assert result["status"] == "AMBIGUOUS_MATCH"
        assert len(result["matches"]) >= 2
        scores = [m["score"] for m in result["matches"][:2]]
        assert scores[0] == scores[1]
        assert any("human parcel selection is required" in r for r in result["reasons"])
        # Stable ordering by property id when scores tie.
        ids = [m["property"]["property_id"] for m in result["matches"][:2]]
        assert ids == sorted(ids)
    finally:
        _cleanup(a, b)


def test_ranking_is_stable_and_explainable_across_calls():
    suffix = uuid.uuid4().hex[:8]
    a = _seed_property(f"R1-{suffix}", survey="45")
    b = _seed_property(f"R2-{suffix}", survey="45")
    try:
        first = mapping._resolve({"survey_number": {"value": "45"}, "village": {"value": "MatchVillage"}})
        second = mapping._resolve({"survey_number": {"value": "45"}, "village": {"value": "MatchVillage"}})
        assert [m["property"]["property_id"] for m in first["matches"]] == \
               [m["property"]["property_id"] for m in second["matches"]]
        for candidate in first["matches"]:
            assert candidate["reasons"], "every candidate must explain why it matched"
            assert candidate["evidence"]["positive"]
            assert candidate["evidence"]["source_reliability"]
    finally:
        _cleanup(a, b)


def test_contradictory_administrative_data_is_surfaced_separately():
    suffix = uuid.uuid4().hex[:8]
    prop = _seed_property(f"CON-{suffix}", survey="45", district="Kanpur Nagar")
    try:
        result = mapping._resolve({
            "survey_number": {"value": "45"},
            "village": {"value": "MatchVillage"},
            "district": {"value": "Ghaziabad"},
        })
        best = result["matches"][0]
        evidence = best["evidence"]
        assert "survey_number" in evidence["positive"]
        assert "district" in evidence["contradictory"]
        assert evidence["contradictory"] != evidence["positive"]
        assert result["evidence_summary"]["contradictory"]
    finally:
        _cleanup(prop)


def test_properties_are_never_selected_automatically_by_ordering():
    """A candidate must not win merely because it sorts first."""
    suffix = uuid.uuid4().hex[:8]
    # Weak-only candidates: no strong identifier at all.
    a = _seed_property(f"W1-{suffix}", survey="11111")
    b = _seed_property(f"W2-{suffix}", survey="22222")
    try:
        result = mapping._resolve({"village": {"value": "MatchVillage"}})
        assert result["resolution_status"] != "MATCH"
        assert result["resolution_status"] in {"INSUFFICIENT_EVIDENCE", "AMBIGUOUS_MATCH"}
        # Evidence summary keeps positive/contradictory/missing separate.
        summary = result["evidence_summary"]
        assert set(summary) >= {"positive", "contradictory", "missing", "source_reliability"}
    finally:
        _cleanup(a, b)
