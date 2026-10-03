"""Tests for the explicit extraction-state classifier.

These pin the property the workflow depends on: a state is derived from
evidence, and absence of evidence never becomes a success.
"""
import pytest

from extraction_states import (
    AI_FALLBACK_FAILED,
    EXTRACTION_STATES,
    EXTRACTION_SUCCESS,
    NEEDS_HUMAN_REVIEW,
    NO_TEXT_DETECTED,
    OCR_ENGINE_UNAVAILABLE,
    REQUIRED_IDENTITY_FIELDS,
    REVIEW_REQUIRED_STATES,
    TEXT_FOUND_FIELDS_MISSING,
    classify_extraction_state,
)


def _fields(**overrides):
    """A complete, high-confidence required-field set with overrides applied."""
    base = {
        "owner_name": ("Ram Singh", 0.95),
        "survey_number": ("452", 0.95),
        "khasra_number": ("452", 0.95),
        "village": ("Sundarpur", 0.95),
        "district": ("Ghaziabad", 0.95),
    }
    base.update(overrides)
    out = {}
    for key, value in base.items():
        if value is None:
            out[key] = {"value": "", "confidence": 0.0}
        else:
            out[key] = {"value": value[0], "confidence": value[1]}
    return out


TEXT = ("Khatauni for village Sundarpur, tehsil Sadar, district Ghaziabad. "
        "Owner Ram Singh, son of Test Father. Survey No 452. Area 2.5 hectares.")


def test_clean_extraction_is_success():
    out = classify_extraction_state(
        ocr_text=TEXT, fields=_fields(), validation={"verdict": "valid", "summary": {"invalid": 0}},
    )
    assert out["state"] == EXTRACTION_SUCCESS
    assert out["requires_human_review"] is False
    assert out["evidence"]["required_field_count"] == 5
    assert out["evidence"]["required_fields_missing"] == []


def _low_conf_fields():
    """Every required field present but at low confidence."""
    return {key: {"value": "present", "confidence": 0.10} for key in REQUIRED_IDENTITY_FIELDS}


def test_every_documented_state_is_reachable_and_named():
    """Each documented state must be producible from real evidence shapes."""
    reached = {
        classify_extraction_state(ocr_text=TEXT, fields=_fields(),
                                  validation={"verdict": "valid"})["state"],
        classify_extraction_state(ocr_text=TEXT, fields=_low_conf_fields(),
                                  validation={"verdict": "review"})["state"],
        classify_extraction_state(ocr_text=TEXT, fields=_fields(owner_name=None, khasra_number=None,
                                                                village=None, district=None),
                                  validation={"verdict": "review"})["state"],
        classify_extraction_state(ocr_text="", fields=_fields(),
                                  validation={"verdict": "review"})["state"],
        classify_extraction_state(ocr_text="", engine_error="Tesseract executable is unavailable",
                                  engine_available=False, fields=_fields(),
                                  validation={"verdict": "review"})["state"],
        classify_extraction_state(ocr_text=TEXT, fields=_fields(owner_name=None, khasra_number=None,
                                                                village=None, district=None),
                                  validation={"verdict": "review"},
                                  ai_meta={"status": "ai_error", "error": "Gemini"})["state"],
    }
    assert reached == set(EXTRACTION_STATES), (
        f"documented states not all reachable: missing {set(EXTRACTION_STATES) - reached}"
    )


def test_low_confidence_forces_human_review_not_success():
    fields = _fields()
    for key in REQUIRED_IDENTITY_FIELDS:
        fields[key] = {"value": "present", "confidence": 0.10}
    out = classify_extraction_state(ocr_text=TEXT, fields=fields,
                                    validation={"verdict": "review", "summary": {"invalid": 0}})
    assert out["state"] == NEEDS_HUMAN_REVIEW
    assert out["requires_human_review"] is True
    assert "confidence" in out["reason"]


def test_invalid_value_forces_human_review_even_at_high_confidence():
    out = classify_extraction_state(
        ocr_text=TEXT, fields=_fields(),
        validation={"verdict": "rejected", "summary": {"invalid": 2}},
    )
    assert out["state"] == NEEDS_HUMAN_REVIEW
    assert out["evidence"]["invalid_field_count"] == 2


def test_missing_required_fields_is_never_a_success():
    fields = _fields(owner_name=None, khasra_number=None, village=None, district=None)
    out = classify_extraction_state(ocr_text=TEXT, fields=fields,
                                    validation={"verdict": "review", "summary": {"invalid": 0}})
    assert out["state"] == TEXT_FOUND_FIELDS_MISSING
    assert set(out["evidence"]["required_fields_missing"]) == {"owner_name", "khasra_number", "village", "district"}
    assert out["requires_human_review"] is True


def test_blank_transcript_is_no_text_detected_not_success():
    out = classify_extraction_state(ocr_text="   \n ", fields=_fields(),
                                    validation={"verdict": "valid"})
    assert out["state"] == NO_TEXT_DETECTED
    assert out["evidence"]["transcript_chars"] == 0


def test_engine_unavailable_takes_precedence_over_blank_text():
    out = classify_extraction_state(
        ocr_text="", engine_error="Tesseract executable is unavailable",
        engine_available=False, fields=_fields(), validation={"verdict": "review"},
    )
    assert out["state"] == OCR_ENGINE_UNAVAILABLE
    assert "Tesseract executable is unavailable" in out["reason"]
    assert "TESSERACT_CMD" in out["recommended_action"]


def test_ai_failure_is_reported_as_ai_fallback_failed_not_success():
    fields = _fields(owner_name=None, khasra_number=None, village=None, district=None)
    out = classify_extraction_state(
        ocr_text=TEXT, fields=fields, validation={"verdict": "review"},
        ai_meta={"status": "ai_error", "error": "Gemini"},
    )
    assert out["state"] == AI_FALLBACK_FAILED
    assert out["requires_human_review"] is True


def test_ai_not_configured_is_not_treated_as_a_failure():
    """No provider configured means no rescue was possible, not that one failed."""
    fields = _fields(owner_name=None, khasra_number=None, village=None, district=None)
    out = classify_extraction_state(
        ocr_text=TEXT, fields=fields, validation={"verdict": "review"},
        ai_meta={"status": "ai_not_configured"},
    )
    assert out["state"] == TEXT_FOUND_FIELDS_MISSING


def test_vision_failure_counts_as_ai_fallback_failure():
    out = classify_extraction_state(
        ocr_text="", fields=_fields(), validation={"verdict": "review"},
        vision_meta={"status": "error", "error": "Vision"},
    )
    assert out["state"] == AI_FALLBACK_FAILED


def test_absence_of_evidence_never_yields_success():
    """An empty call — no text, no fields, no errors — must demand review."""
    out = classify_extraction_state()
    assert out["state"] != EXTRACTION_SUCCESS
    assert out["requires_human_review"] is True
    assert out["state"] in REVIEW_REQUIRED_STATES


def test_result_shape_is_stable_for_ui_consumers():
    out = classify_extraction_state(ocr_text=TEXT, fields=_fields(),
                                    validation={"verdict": "valid"})
    for key in ("state", "requires_human_review", "reason", "recommended_action",
                "evidence", "thresholds"):
        assert key in out, f"missing {key}"
    for key in ("transcript_chars", "required_fields_found", "required_fields_missing",
                "mean_field_confidence", "validation_verdict", "engine_error"):
        assert key in out["evidence"], f"missing evidence.{key}"


def test_fields_in_plain_string_form_are_accepted():
    """Some callers pass {'owner_name': 'Ram Singh'} rather than value dicts."""
    out = classify_extraction_state(
        ocr_text=TEXT,
        fields={k: "present" for k in REQUIRED_IDENTITY_FIELDS},
        validation={"verdict": "valid"},
    )
    # Confidence is unknown for plain strings, so it must not be claimed as a
    # confident success.
    assert out["evidence"]["required_field_count"] == 5
    assert out["evidence"]["mean_field_confidence"] is None


def test_malformed_inputs_do_not_raise():
    for bad in (None, {}, {"owner_name": "not-a-dict"}, {"owner_name": {"value": None, "confidence": "abc"}}):
        out = classify_extraction_state(ocr_text=TEXT, fields=bad, validation={"verdict": "valid"})
        assert out["state"] in EXTRACTION_STATES


def test_unicode_indic_text_is_counted_as_text():
    hindi = "खतौनी ग्राम सुंदरपुर तहसील सदर जिला गाजियाबाद। स्वामी राम सिंह। सर्वे नंबर 452। क्षेत्रफल 2.5 हेक्टेयर।"
    out = classify_extraction_state(ocr_text=hindi, fields=_fields(),
                                    validation={"verdict": "valid"})
    assert out["evidence"]["transcript_chars"] > 20
    assert out["state"] == EXTRACTION_SUCCESS


def test_review_required_states_cover_every_non_success_state():
    assert EXTRACTION_SUCCESS not in REVIEW_REQUIRED_STATES
    assert set(EXTRACTION_STATES) - {EXTRACTION_SUCCESS} == set(REVIEW_REQUIRED_STATES)


def test_state_is_derived_without_side_effects():
    """The classifier must not touch the database or the network."""
    before = classify_extraction_state(ocr_text=TEXT, fields=_fields(),
                                       validation={"verdict": "valid"})
    after = classify_extraction_state(ocr_text=TEXT, fields=_fields(),
                                      validation={"verdict": "valid"})
    assert before == after


# ---------------------------------------------------------------------------
# Integration: the state must actually be produced by the real pipeline, not
# only by the classifier in isolation.
# ---------------------------------------------------------------------------
import asyncio
import io
import zlib


def _text_pdf_bytes(text: str) -> bytes:
    """A real single-page PDF carrying an extractable text layer."""
    parts = ["BT", "3 Tr", "/F1 11 Tf", "14 TL", "40 780 Td"]
    for line in text.split("\n"):
        safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        parts.append(f"({safe}) Tj")
        parts.append("T*")
    parts.append("ET")
    content = zlib.compress("\n".join(parts).encode("cp1252", "replace"), 9)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        f"<< /Length {len(content)} /Filter /FlateDecode >>\nstream\n".encode() + content + b"\nendstream",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref_at = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode()
    )
    return out.getvalue()


LAND_PDF_TEXT = "\n".join([
    "Record of Rights - Khatauni",
    "Village: Sundarpur",
    "Tehsil: Sadar",
    "District: Ghaziabad",
    "Survey No: 452",
    "Khasra No: 452",
    "Owner Name: Ram Singh",
    "Father Name: Test Father",
    "Area: 2.5 hectares",
    "Date: 2023-06-15",
])


def test_pipeline_attaches_extraction_state_for_a_readable_pdf(monkeypatch, tmp_path):
    import ocr_pipeline
    import server

    server.DB_PATH = str(tmp_path / "state-pdf.db")
    server.init_db()
    ocr_pipeline.CACHE_TABLE_READY = False
    monkeypatch.setattr(ocr_pipeline, "cache_lookup", lambda *a, **k: None)

    result = asyncio.run(ocr_pipeline.run_fast_ocr_pipeline(
        _text_pdf_bytes(LAND_PDF_TEXT), "record.pdf", "auto",
        user={"email": "a@b.test", "role": "ADMIN"}, doc_id="DOC-STATE-1",
    ))

    state = result.get("extraction_state")
    assert state, "run_fast_ocr_pipeline must return an extraction_state"
    assert state["state"] in EXTRACTION_STATES
    assert result["pipeline_meta"]["extraction_state"] == state["state"]
    # The embedded text layer really contains these identifiers, so the
    # classifier must have found them.
    assert "452" in result["ocr_text"]
    assert result["fields"]["survey_number"]["value"] == "452"
    assert "survey_number" in state["evidence"]["required_fields_found"]


def test_pipeline_reports_engine_unavailable_state(monkeypatch, tmp_path):
    import ocr_pipeline
    import server

    server.DB_PATH = str(tmp_path / "state-engine.db")
    server.init_db()
    ocr_pipeline.CACHE_TABLE_READY = False
    monkeypatch.setattr(ocr_pipeline, "cache_lookup", lambda *a, **k: None)
    monkeypatch.setattr(server, "HAS_TESSERACT", True)
    monkeypatch.setattr(ocr_pipeline, "run_fast_ocr", lambda *_a, **_k: {
        "text": "", "confidence": 0.0, "word_count": 0,
        "detected_language": "eng", "method": "tesseract_unavailable",
        "engine_error": "Tesseract executable is unavailable",
    })

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (80, 80), "white").save(buf, "PNG")

    result = asyncio.run(ocr_pipeline.run_fast_ocr_pipeline(
        buf.getvalue(), "blank.png", "auto",
        user={"email": "a@b.test", "role": "ADMIN"}, doc_id="DOC-STATE-2",
    ))
    state = result["extraction_state"]
    assert state["state"] == OCR_ENGINE_UNAVAILABLE
    assert state["requires_human_review"] is True
    assert result["pipeline_meta"]["extraction_state"] == OCR_ENGINE_UNAVAILABLE


def test_document_detail_api_exposes_extraction_state(make_user_client, insert_land_document):
    """The state must reach the reviewer through the real detail endpoint."""
    officer, headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="est")
    doc_id = insert_land_document(
        survey="452", village="Sundarpur", owner="Ram Singh",
        ocr_text=TEXT, status="PENDING_VERIFICATION",
    )
    response = officer.get(f"/api/documents/{doc_id}", headers=headers)
    assert response.status_code == 200, response.text
    state = response.json().get("extraction_state")
    assert state, "document detail must carry an extraction_state"
    assert state["state"] in EXTRACTION_STATES
    assert "requires_human_review" in state
    assert "recommended_action" in state
    # The fixture supplies every required identity field, so the classifier
    # must have seen them.
    assert "survey_number" in state["evidence"]["required_fields_found"]


def test_document_detail_state_reflects_a_blank_scan(make_user_client, insert_land_document):
    """A document with no transcript must not be reported as extracted."""
    officer, headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="estb")
    doc_id = insert_land_document(ocr_text="", status="PENDING_VERIFICATION")
    response = officer.get(f"/api/documents/{doc_id}", headers=headers)
    assert response.status_code == 200, response.text
    state = response.json()["extraction_state"]
    assert state["state"] in {NO_TEXT_DETECTED, OCR_ENGINE_UNAVAILABLE, AI_FALLBACK_FAILED}
    assert state["requires_human_review"] is True
