import pytest

from land_intelligence import assess_ocr, corroborate_field_passes, owner_match


def test_ocr_diagnosis_explains_missing_key_fields():
    result = assess_ocr({"text": "owner: Ram", "confidence": 0.32, "word_count": 2}, {"owner_name": {"value": "Ram", "confidence": 0.32}})
    assert result["status"] == "UNREADABLE"
    assert "survey_number" in result["missing_key_fields"]
    assert result["diagnosis"]


def test_multi_pass_corroboration_raises_confidence():
    result = corroborate_field_passes([
        {"survey_number": {"value": "142/3", "confidence": 0.70}},
        {"survey_number": {"value": "142/3", "confidence": 0.80}},
        {"survey_number": {"value": "142/8", "confidence": 0.90}},
    ])
    assert result["survey_number"]["value"] == "142/3"
    assert result["survey_number"]["corroboration"] == 2
    assert result["survey_number"]["confidence"] == 0.88


def test_owner_match_does_not_silently_merge_fuzzy_identities():
    result = owner_match("Ramesh Kumar", "Ramesh Kumr")
    assert result["match"] is True
    assert result["requires_review"] is True
    assert "do not merge" in result["warning"]
