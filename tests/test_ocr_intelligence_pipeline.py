from ocr_intelligence import extract_fields_with_ai, validate_fields, _merge


def test_ai_failure_preserves_deterministic_fields():
    base = {
        "owner_name": {"value": "Ramesh Kumar", "confidence": 0.8},
        "survey_number": {"value": "131", "confidence": 0.9},
    }
    fields, meta = extract_fields_with_ai("Owner Name: Ramesh Kumar\nSurvey Number: 131", deterministic=base)
    assert fields["owner_name"]["value"] == "Ramesh Kumar"
    assert fields["survey_number"]["value"] == "131"
    assert meta["status"] in {"ok", "ai_not_configured", "ai_error"}


def test_merge_fills_missing_values_without_erasing_evidence():
    a = {"owner_name": {"value": "", "confidence": 0}}
    b = {"owner_name": {"value": "Sita Devi", "confidence": 85, "source": "vision+ai"}}
    merged = _merge(a, b)
    assert merged["owner_name"]["value"] == "Sita Devi"
    assert merged["owner_name"]["confidence"] == 85


def test_validation_reports_required_fields():
    fields = {
        "owner_name": {"value": "Ramesh", "confidence": 80},
        "survey_number": {"value": "131", "confidence": 90},
        "khasra_number": {"value": "22", "confidence": 90},
        "village": {"value": "Shantiban", "confidence": 90},
        "district": {"value": "Pune", "confidence": 90},
    }
    result = validate_fields(fields)
    assert result["required_missing"] == []
    assert result["valid"] is True
