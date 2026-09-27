"""Production document-intelligence bridge.

OCR is evidence acquisition; AI is field extraction. The bridge deliberately
keeps those stages separate so a good OCR transcript can be used to populate
the review form instead of relying on fragile regex-only mapping.
"""
from __future__ import annotations

from typing import Any, Dict, Tuple, List

MAX_PDF_PAGES = 20


def _usable(text: str) -> bool:
    words = [w for w in (text or "").split() if any(ch.isalnum() for ch in w)]
    return len(text or "") >= 40 and len(words) >= 8


def _render_page(pdf, index: int):
    page = pdf[index]
    bitmap = page.render(scale=2.2)
    return bitmap.to_pil()


def extract_pdf_evidence(content: bytes, lang: str = "auto") -> Tuple[str, int, Dict[str, Any]]:
    """Extract evidence from every page, using text layer then OCR per page."""
    import server
    import ocr_pipeline

    if not getattr(server, "HAS_PDFIUM", False):
        raise ValueError("PDF engine is not installed")
    pdf = server.pdfium.PdfDocument(content)
    pages = len(pdf)
    if pages == 0:
        raise ValueError("PDF contains no pages")

    evidence: List[str] = []
    ocr_pages = 0
    page_methods: List[str] = []
    for index in range(min(pages, MAX_PDF_PAGES)):
        text = ""
        try:
            tp = pdf[index].get_textpage()
            text = (tp.get_text_range() or "").strip()
            tp.close()
        except Exception:
            text = ""
        if _usable(text):
            page_methods.append("embedded_text")
            evidence.append(f"[PAGE {index + 1}]\n{text}")
            continue
        try:
            image = _render_page(pdf, index)
            result = ocr_pipeline.run_fast_ocr(image, lang)
            ocr_text = (result.get("text") or "").strip()
            if ocr_text:
                ocr_pages += 1
                page_methods.append(result.get("method", "tesseract"))
                evidence.append(f"[PAGE {index + 1}]\n{ocr_text}")
            elif text:
                page_methods.append("weak_embedded_text")
                evidence.append(f"[PAGE {index + 1}]\n{text}")
            else:
                page_methods.append("no_text")
        except Exception as exc:
            page_methods.append(f"ocr_error:{type(exc).__name__}")
            if text:
                evidence.append(f"[PAGE {index + 1}]\n{text}")

    transcript = "\n\n".join(evidence).strip()
    return transcript, pages, {"pages_processed": min(pages, MAX_PDF_PAGES), "ocr_pages": ocr_pages, "page_methods": page_methods}


def extract_image_evidence(content: bytes, filename: str, lang: str = "auto") -> Tuple[str, int, Dict[str, Any]]:
    import io
    from PIL import Image
    import ocr_pipeline
    image = Image.open(io.BytesIO(content))
    result = ocr_pipeline.run_fast_ocr(image, lang)
    return (result.get("text") or "").strip(), 1, {
        "ocr_pages": 1,
        "page_methods": [result.get("method", "tesseract")],
        "confidence": result.get("confidence", 0),
        "engine_error": result.get("engine_error", ""),
    }


def install() -> None:
    import server

    original = server.run_ocr_pipeline
    if getattr(original, "_land_bridge", False):
        return

    async def run_ocr_pipeline_intelligent(content: bytes, filename: str, lang: str = "auto") -> Dict[str, Any]:
        import ocr_intelligence

        name = (filename or "").lower()
        if name.endswith(".pdf"):
            transcript, pages, evidence_meta = extract_pdf_evidence(content, lang)
        else:
            transcript, pages, evidence_meta = extract_image_evidence(content, filename, lang)

        fields, ai_meta = ocr_intelligence.extract_fields_with_ai(transcript, "Land Record")
        validation = ocr_intelligence.validate_fields(fields)

        # Keep the response shape compatible with the existing review UI while
        # making the evidence and AI stages observable for debugging.
        confidence_values = [int(v.get("confidence", 0)) for v in fields.values() if v.get("value")]
        overall = round(sum(confidence_values) / len(confidence_values), 1) if confidence_values else 0
        result: Dict[str, Any] = {
            "ocr_text": transcript,
            "cleaned_ocr_text": transcript,
            "text": transcript,
            "fields": fields,
            "validation": validation,
            "confidence": overall / 100.0,
            "mean_conf": overall,
            "pages": pages,
            "word_count": len(transcript.split()),
            "detected_language": lang if lang != "auto" else "eng",
            "ocr_method": "multi_page_tesseract+ai",
            "pipeline_meta": {
                "production_fast_path": True,
                "guided_ocr_skipped": False,
                "gemini_critical_path": True,
                "evidence": evidence_meta,
                "ai_extraction": ai_meta,
            },
        }
        if not transcript:
            result["engine_error"] = "No OCR text was produced. Check Tesseract availability/language packs and the uploaded image quality."
        return result

    run_ocr_pipeline_intelligent._land_bridge = True
    server.run_ocr_pipeline = run_ocr_pipeline_intelligent
