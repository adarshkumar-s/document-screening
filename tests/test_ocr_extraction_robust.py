"""Regression tests for robust OCR field extraction, engine-failure surfacing
and mapping resilience.

These cover the production failure modes reported against the fast pipeline:
  * OCR text without "Label: Value" colons extracted NOTHING;
  * one-line OCR text made values swallow the rest of the page;
  * a missing/broken Tesseract engine silently returned empty results;
  * parcel resolution crashed when the mapping schema was not yet created on
    the active database.
"""
import asyncio
import io
import json
import uuid

import pytest
from PIL import Image

import mapping
import server


# ---------------------------------------------------------------------------
# field extraction: separator-optional labels
# ---------------------------------------------------------------------------

def test_flat_ocr_text_without_colons_extracts_fields():
    text = ("Owner Name Ram Sharma Father Name Mohan Lal Village Shantiban "
            "District Rampur Survey No 131 Khasra Number 45/2 Area 2.5 Acre")
    fields = server.extract_fields_from_ocr(text, "scan.png")["fields"]
    assert fields["owner_name"]["value"] == "Ram Sharma"
    assert fields["father_name"]["value"] == "Mohan Lal"
    assert fields["village"]["value"] == "Shantiban"
    assert fields["district"]["value"] == "Rampur"
    assert fields["survey_number"]["value"] == "131"
    assert fields["khasra_number"]["value"] == "45/2"
    assert fields["area"]["value"] == "2.5 Acre"


def test_label_and_value_on_consecutive_lines():
    text = "Owner Name\nRam Sharma\nVillage\nShantiban\nSurvey No\n131\n"
    fields = server.extract_fields_from_ocr(text, "scan.png")["fields"]
    assert fields["owner_name"]["value"] == "Ram Sharma"
    assert fields["village"]["value"] == "Shantiban"
    assert fields["survey_number"]["value"] == "131"


def test_dense_single_line_values_are_cut_at_the_next_label():
    text = "Survey No: 131 · Village: Shantiban · Owner Name: Adarsh / Shivangi (disputed)"
    fields = server.extract_fields_from_ocr(text, "scan.png")["fields"]
    assert fields["survey_number"]["value"] == "131"
    assert fields["village"]["value"] == "Shantiban"
    assert fields["owner_name"]["value"] == "Adarsh / Shivangi (disputed)"


def test_value_that_looks_like_a_label_is_not_stolen():
    # 'State Test' is the owner's NAME; the state field must stay empty and
    # consumed values must never be re-scanned as labels.
    text = "Owner Name: State Test\nVillage: Testville"
    fields = server.extract_fields_from_ocr(text, "x.png")["fields"]
    assert fields["owner_name"]["value"] == "State Test"
    assert fields["state"]["value"] == ""
    assert fields["village"]["value"] == "Testville"


def test_prose_cannot_fill_numeric_identifier_fields():
    # "APPLICATION FOR MUTATION OF NAMES" is not a mutation number.
    text = "APPLICATION FOR MUTATION OF NAMES\nStatus: UNDER REVIEW"
    fields = server.extract_fields_from_ocr(text, "app.png")["fields"]
    assert fields["mutation_no"]["value"] == ""


def test_fuzzy_label_rescue_is_numeric_only_and_bounded():
    # OCR typos on numeric identifier labels are rescued ("Matation" ->
    # "mutation"); prose words ("report") must never become values.
    text = "Matation No: 45/2\nSurney: 99\nthe field report is awaited"
    fields = server.extract_fields_from_ocr(text, "scan.png")["fields"]
    assert fields["mutation_no"]["value"] == "45/2"
    assert fields["survey_number"]["value"] == "99"
    assert fields["khatauni_year"]["value"] == ""


def test_year_fallback_ignores_plain_dates():
    text = "Village: Testville\nOrder Date: 2025-07-15"
    fields = server.extract_fields_from_ocr(text, "scan.png")["fields"]
    assert fields["document_date"]["value"] == "2025-07-15"
    assert fields["khatauni_year"]["value"] == ""


def test_fiscal_year_fallback_survives_without_year_label():
    text = "Village: Testville\nRecord period 2024-25"
    fields = server.extract_fields_from_ocr(text, "scan.png")["fields"]
    assert fields["khatauni_year"]["value"] == "2024-25"


def test_filename_is_never_identity_and_missing_owner_is_flagged():
    result = server.extract_fields_from_ocr("Village: Greenfield\nDistrict: Pune",
                                            "Alice_Smith_land_record.png")
    assert result["fields"]["owner_name"]["value"] == ""
    assert any("Record-holder" in issue["msg"] for issue in result["validation"]["issues"])


def test_indic_digits_survive_extraction():
    text = "मालिक का नाम: राम बहादुर सिंह\nसर्वे नं.: ४५२\nवर्ष: २०१९-२०"
    fields = server.extract_fields_from_ocr(text, "record.png")["fields"]
    assert fields["owner_name"]["value"] == "राम बहादुर सिंह"
    assert fields["survey_number"]["value"] == "४५२"
    assert fields["khatauni_year"]["value"] == "२०१९-२०"


# ---------------------------------------------------------------------------
# run_guided_ocr: line-structured text from one single Tesseract pass
# ---------------------------------------------------------------------------

def _fake_image_to_data(*_args, **_kwargs):
    words = ["Survey", "No:", "131", "Village:", "Shantiban"]
    return {
        "text": words + [""] * 0,
        "conf": ["95"] * len(words),
        "left": [0] * len(words),
        "top": [0] * len(words),
        "width": [10] * len(words),
        "height": [10] * len(words),
        "block_num": [0, 0, 0, 1, 1],
        "par_num": [0, 0, 0, 0, 0],
        "line_num": [0, 0, 0, 1, 1],
    }


def test_run_guided_ocr_reconstructs_line_structure(monkeypatch):
    monkeypatch.setattr(server, "HAS_TESSERACT", True)
    monkeypatch.setattr(server.pytesseract, "image_to_data", _fake_image_to_data)
    image = Image.new("L", (100, 100), 255)
    result = server.run_guided_ocr(image, {"lang": "eng", "lang_candidates": ["eng"], "psm": 3})
    assert result["text"] == "Survey No: 131\nVillage: Shantiban"
    assert result["word_count"] == 5
    assert "engine_error" not in result


# ---------------------------------------------------------------------------
# engine failures are surfaced, never silent
# ---------------------------------------------------------------------------

def _broken_tesseract(*_args, **_kwargs):
    raise RuntimeError("tesseract is not installed or it's not in your PATH")


def test_engine_failure_surfaces_instead_of_empty_success(monkeypatch, tmp_path):
    server.DB_PATH = str(tmp_path / "engine-fail.db")
    server.init_db()
    monkeypatch.setattr(server, "HAS_TESSERACT", True)
    monkeypatch.setattr(server.pytesseract, "image_to_data", _broken_tesseract)

    import ocr_pipeline
    ocr_pipeline.CACHE_TABLE_READY = False

    image = Image.new("RGB", (60, 60), "white")
    buf = io.BytesIO()
    image.save(buf, "PNG")
    result = asyncio.run(ocr_pipeline.run_fast_ocr_pipeline(
        buf.getvalue(), "scan.png", "auto", user={"email": "a@b.test", "role": "ADMIN"}, doc_id="DOC-1",
    ))
    assert result["ocr_text"] == ""
    assert result["ai_decision_support"]["ocr_quality"] == "ENGINE_UNAVAILABLE"
    assert result["ai_decision_support"]["recommendation"] == "INSTALL_OCR_ENGINE"
    assert result["pipeline_meta"]["ocr_engine"] == "unavailable"
    assert any(issue["severity"] == "error" for issue in result["validation"]["issues"])
    assert result["escalated"] is True


def test_engine_failure_cache_is_invalidated_once_engine_works(monkeypatch, tmp_path):
    server.DB_PATH = str(tmp_path / "engine-cache.db")
    server.init_db()
    import ocr_pipeline
    ocr_pipeline.CACHE_TABLE_READY = False

    monkeypatch.setattr(server, "HAS_TESSERACT", True)
    monkeypatch.setattr(server.pytesseract, "image_to_data", _broken_tesseract)

    image = Image.new("RGB", (60, 60), "white")
    buf = io.BytesIO()
    image.save(buf, "PNG")
    content = buf.getvalue()
    user = {"email": "a@b.test", "role": "ADMIN"}

    asyncio.run(ocr_pipeline.run_fast_ocr_pipeline(
        content, "scan.png", "auto", user=user, doc_id="DOC-1"))

    # Engine still broken: the cached (empty) result is served — no re-OCR.
    monkeypatch.setattr(server, "tesseract_available", lambda: False)
    hit = asyncio.run(ocr_pipeline.run_fast_ocr_pipeline(
        content, "scan-copy.png", "auto", user=user, doc_id="DOC-2"))
    assert hit["pipeline_meta"]["cache_hit"] is True

    # Engine repaired: the stale engine-failure entry must NOT be served.
    monkeypatch.setattr(server, "tesseract_available", lambda: True)
    monkeypatch.setattr(server.pytesseract, "image_to_data", _fake_image_to_data)
    refreshed = asyncio.run(ocr_pipeline.run_fast_ocr_pipeline(
        content, "scan-copy2.png", "auto", user=user, doc_id="DOC-3"))
    assert refreshed["pipeline_meta"]["cache_hit"] is False
    assert "131" in refreshed["ocr_text"]


def test_auto_language_candidates_are_filtered_to_installed_packs(monkeypatch):
    monkeypatch.setattr(server, "_AVAILABLE_OCR_LANGS", {"eng"})
    assert server._ocr_languages("auto") == ["eng", "eng"]
    monkeypatch.setattr(server, "_AVAILABLE_OCR_LANGS", {"eng", "hin", "tel", "tam"})
    assert server._ocr_languages("auto") == ["hin+eng+tel+tam", "eng"]
    # explicit codes are honoured exactly regardless of installed packs
    assert server._ocr_languages("mal") == ["mal"]


# ---------------------------------------------------------------------------
# mapping resilience
# ---------------------------------------------------------------------------

def test_resolve_survives_database_swaps(tmp_path, monkeypatch):
    """mapping schema must materialise lazily on the ACTIVE database — a
    switch of server.DB_PATH used to crash parcel resolution with
    'no such table: properties'."""
    server.DB_PATH = str(tmp_path / "swap.db")
    server.init_db()
    # deliberately do NOT call mapping._ensure_tables here
    with server.get_db() as db:
        db.execute(
            """INSERT INTO documents (id,filename,doc_type,mean_conf,verdict,status,languages,pages,
               fields,validation,ai_decision_support,ocr_text,cleaned_ocr_text,detected_language,
               original_fields,uploaded_by,created_at,updated_at)
               VALUES (?,?,?,90,'review','APPROVED','[]',1,'{}','{}','{}','','','eng','{}','x@y.z',1,1)""",
            (uuid.uuid4().hex, "swap.png", "Land Record"),
        )
    resolution = mapping._resolve({
        "survey_number": {"value": "452", "confidence": 0.95},
        "village": {"value": "Sundarpur", "confidence": 0.95},
    })
    assert resolution["status"] in {"MATCH", "POSSIBLE MATCH", "NO MATCH", "INSUFFICIENT DATA"}


def test_resolve_cross_column_gat_number_matching(tmp_path):
    server.DB_PATH = str(tmp_path / "gat.db")
    server.init_db()
    mapping.ensure_schema()
    property_id = "PROP-GAT-1"
    with server.get_db() as db:
        db.execute("DELETE FROM properties WHERE property_id=?", (property_id,))
        db.execute(
            """INSERT INTO properties (property_id,parcel_id,district,taluka,village,survey_number,
               gat_number,khasra_number,area,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (property_id, "PARCEL-GAT-1", "Ghaziabad", "Sadar", "Sundarpur",
             "", "452", "", 1.0, 1, 1),
        )
    try:
        # The document calls it a survey number; the parcel row stores it as a
        # gat number. Resolution must still link them.
        resolution = mapping._resolve({
            "survey_number": {"value": "452", "confidence": 0.95},
            "village": {"value": "Sundarpur", "confidence": 0.95},
        })
        assert resolution["status"] in {"MATCH", "POSSIBLE MATCH"}
        assert resolution["matches"][0]["property"]["property_id"] == property_id
        assert "gat_number" in resolution["matches"][0]["matched_fields"]
    finally:
        with server.get_db() as db:
            db.execute("DELETE FROM properties WHERE property_id=?", (property_id,))


def test_resolve_does_not_mask_genuine_conflicts(tmp_path):
    server.DB_PATH = str(tmp_path / "conflict.db")
    server.init_db()
    mapping.ensure_schema()
    property_id = "PROP-CONF-1"
    with server.get_db() as db:
        db.execute("DELETE FROM properties WHERE property_id=?", (property_id,))
        db.execute(
            """INSERT INTO properties (property_id,parcel_id,district,taluka,village,survey_number,
               gat_number,khasra_number,area,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (property_id, "PARCEL-CONF-1", "Ghaziabad", "Sadar", "Sundarpur",
             "999", "452", "", 1.0, 1, 1),
        )
    try:
        resolution = mapping._resolve({
            "survey_number": {"value": "452", "confidence": 0.95},
            "village": {"value": "Sundarpur", "confidence": 0.95},
        })
        # primary column holds a DIFFERENT number -> the sibling hit must not
        # turn a conflict into a clean match
        best = resolution["matches"][0] if resolution.get("matches") else {}
        assert best.get("status") != "MATCH"
    finally:
        with server.get_db() as db:
            db.execute("DELETE FROM properties WHERE property_id=?", (property_id,))
