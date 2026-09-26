"""Fast deterministic document extraction bridge.

Text PDFs use their embedded text layer; scanned PDFs and images use the
canonical fast OCR pipeline. No Gemini/guided OCR call is allowed on the
critical upload path.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Tuple


def extract_pdf_text(content: bytes) -> Tuple[str, int]:
    """Return ``(text, pages)`` for a PDF.

    Uses the embedded text layer of EVERY page (deterministic, fast, no OCR).
    When the file is a scan with no usable text layer, falls back to the
    canonical fast OCR of the first page so callers still get text."""
    import server

    if not getattr(server, "HAS_PDFIUM", False):
        raise ValueError("PDF processing is unavailable because the PDF engine is not installed")
    pdf = server.pdfium.PdfDocument(content)
    pages = len(pdf)
    if pages == 0:
        raise ValueError("PDF contains no pages")
    collected = []
    for index in range(pages):
        try:
            textpage = pdf[index].get_textpage()
            page_text = textpage.get_text_range() or ""
            textpage.close()
        except Exception:
            page_text = ""
        collected.append(page_text)
    text = "\n".join(collected).replace("\r\n", "\n").replace("\r", "\n").strip()

    words = [w for w in text.split() if any(ch.isalnum() for ch in w)]
    if len(text) < 40 or len(words) < 8:
        # Scanned PDF: rasterize once and use the canonical fast OCR path.
        import ocr_pipeline

        image, _page_count = ocr_pipeline._render_pdf_first_page(content)
        ocr_result = ocr_pipeline.run_fast_ocr(image)
        text = (ocr_result.get("text") or "").strip()
    return text, pages


def install() -> None:
    import server
    import ocr_pipeline

    original = server.run_ocr_pipeline
    if getattr(original, "_land_bridge", False):
        return

    async def run_ocr_pipeline_fast(content: bytes, filename: str, lang: str = "auto") -> Dict[str, Any]:
        # The canonical fast pipeline already does:
        #   PDF text layer -> single rasterization -> single Tesseract pass
        #   -> deterministic field extraction -> cache.
        # Do not call server.run_guided_ocr or Gemini here.
        parsed = await ocr_pipeline.run_fast_ocr_pipeline(
            content,
            filename,
            lang=lang,
            doc_type_hint="Land Record",
            user={},
            doc_id=None,
        )
        parsed.setdefault("pipeline_meta", {})
        parsed["pipeline_meta"].update({
            "production_fast_path": True,
            "guided_ocr_skipped": True,
            "gemini_critical_path": False,
        })
        return parsed

    run_ocr_pipeline_fast._land_bridge = True
    server.run_ocr_pipeline = run_ocr_pipeline_fast
