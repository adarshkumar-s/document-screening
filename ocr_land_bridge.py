"""Fast, deterministic document extraction bridge.

Keeps the existing OCR/AI pipeline intact for scanned images while fixing a
critical path for text PDFs: generated and real PDFs that already contain a
usable text layer should not be rasterized and sent through the expensive AI +
Tesseract pipeline. Deterministic field extraction is also merged back into
AI/OCR results so land identifiers are not lost when AI extraction omits them.
"""
from __future__ import annotations

import io
import os
from typing import Any, Dict, Optional

from PIL import Image


def _usable_text(text: str) -> bool:
    normalized = " ".join((text or "").split())
    # A short title alone is not enough to establish a document's land identity.
    # Require enough text to contain meaningful record content.
    return len(normalized) >= 80


def extract_pdf_text(content: bytes) -> tuple[str, int]:
    """Return embedded PDF text and page count without rasterizing the PDF."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(content)
    pages = len(pdf)
    chunks = []
    for index in range(pages):
        page = pdf[index]
        text_page = page.get_textpage()
        try:
            chunks.append(text_page.get_text_range() or "")
        finally:
            try:
                text_page.close()
            except Exception:
                pass
            try:
                page.close()
            except Exception:
                pass
    try:
        pdf.close()
    except Exception:
        pass
    return "\n".join(chunks), pages


def _merge_fields(primary: Dict[str, Any], fallback: Dict[str, Any]) -> Dict[str, Any]:
    """Keep AI/OCR values but fill missing fields from deterministic extraction."""
    merged = dict(primary or {})
    for key, fallback_obj in (fallback or {}).items():
        if key == "document_type":
            if not (merged.get(key) or {}).get("value"):
                merged[key] = fallback_obj
            continue
        current = merged.get(key)
        current_value = current.get("value") if isinstance(current, dict) else current
        fallback_value = fallback_obj.get("value") if isinstance(fallback_obj, dict) else fallback_obj
        if not str(current_value or "").strip() and str(fallback_value or "").strip():
            merged[key] = fallback_obj
    return merged


def install() -> None:
    """Install the bridge into server.run_ocr_pipeline before requests arrive."""
    import server

    original = server.run_ocr_pipeline
    if getattr(original, "_land_bridge", False):
        return

    async def run_ocr_pipeline_fast(content: bytes, filename: str, lang: str = "auto") -> Dict[str, Any]:
        ext = os.path.splitext(filename or "")[1].lower()

        if ext == ".pdf":
            if not getattr(server, "HAS_PDFIUM", False):
                raise ValueError("PDF processing is unavailable because the PDF engine is not installed")
            text, pages = extract_pdf_text(content)
            if _usable_text(text):
                # Text PDFs need no rasterization, Tesseract, Gemini prescan, or
                # repeated AI extraction. The existing deterministic extractor
                # already understands survey/village/khasra labels used by the
                # Land Intelligence demo documents.
                parsed = server.extract_fields_from_ocr(text, filename)
                parsed["pages"] = pages
                parsed["languages"] = ["English"]
                parsed["detected_language"] = "eng"
                parsed["pipeline_meta"] = {
                    **(parsed.get("pipeline_meta") or {}),
                    "mode": "EMBEDDED_PDF_TEXT",
                    "pdf_text_layer": True,
                    "ocr_skipped": True,
                    "pages": pages,
                }
                parsed["ai_decision_support"] = {
                    **(parsed.get("ai_decision_support") or {}),
                    "pipeline_mode": "EMBEDDED_PDF_TEXT",
                    "explanation": "Usable embedded PDF text was extracted directly; raster OCR was not required.",
                }
                return parsed

        # Scanned PDFs and images retain the existing production AI/OCR path.
        parsed = await original(content, filename, lang)

        # OCR output can contain the exact labelled fields even when AI field
        # extraction omitted them. Fill only missing values; never overwrite an
        # AI value with deterministic text.
        ocr_text = parsed.get("ocr_text") or ""
        if ocr_text:
            deterministic = server.extract_fields_from_ocr(ocr_text, filename)
            parsed["fields"] = _merge_fields(parsed.get("fields") or {}, deterministic.get("fields") or {})
            parsed["original_fields"] = _merge_fields(parsed.get("original_fields") or {}, deterministic.get("original_fields") or {})
            if parsed.get("pipeline_meta") is None:
                parsed["pipeline_meta"] = {}
            parsed["pipeline_meta"] = {
                **parsed["pipeline_meta"],
                "deterministic_field_backfill": True,
            }
        return parsed

    run_ocr_pipeline_fast._land_bridge = True
    server.run_ocr_pipeline = run_ocr_pipeline_fast
