"""Production document-intelligence bridge.

OCR is evidence acquisition; deterministic parsing and multimodal AI are
separate extraction layers. The review UI always receives the canonical field
schema, even when one provider fails.
"""
from __future__ import annotations

from typing import Any, Dict, Tuple, List, Optional

MAX_PDF_PAGES = 20


def _usable(text: str) -> bool:
    words = [w for w in (text or "").split() if any(ch.isalnum() for ch in w)]
    return len(text or "") >= 40 and len(words) >= 8


def _render_page(pdf, index: int):
    return pdf[index].render(scale=2.2).to_pil()


def extract_pdf_evidence(content: bytes, lang: str = "auto") -> Tuple[str, int, Dict[str, Any], Optional[Any]]:
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
    rescue_image = None
    for index in range(min(pages, MAX_PDF_PAGES)):
        text = ""
        try:
            tp = pdf[index].get_textpage()
            text = (tp.get_text_range() or "").strip()
            tp.close()
        except Exception:
            pass
        if _usable(text):
            page_methods.append("embedded_text")
            evidence.append(f"[PAGE {index + 1}]\n{text}")
            continue
        try:
            image = _render_page(pdf, index)
            if rescue_image is None:
                rescue_image = image
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
    return "\n\n".join(evidence).strip(), pages, {"pages_processed": min(pages, MAX_PDF_PAGES), "ocr_pages": ocr_pages, "page_methods": page_methods}, rescue_image


def extract_image_evidence(content: bytes, filename: str, lang: str = "auto") -> Tuple[str, int, Dict[str, Any], Any]:
    import io
    from PIL import Image
    import ocr_pipeline
    image = Image.open(io.BytesIO(content))
    image.load()
    result = ocr_pipeline.run_fast_ocr(image, lang)
    return (result.get("text") or "").strip(), 1, {
        "ocr_pages": 1, "page_methods": [result.get("method", "tesseract")],
        "confidence": result.get("confidence", 0), "engine_error": result.get("engine_error", "")
    }, image


def install() -> None:
    import server
    original = server.run_ocr_pipeline
    if getattr(original, "_land_bridge", False):
        return

    async def run_ocr_pipeline_intelligent(content: bytes, filename: str, lang: str = "auto") -> Dict[str, Any]:
        import ocr_intelligence
        name = (filename or "").lower()
        if name.endswith(".pdf"):
            transcript, pages, evidence_meta, rescue_image = extract_pdf_evidence(content, lang)
        else:
            transcript, pages, evidence_meta, rescue_image = extract_image_evidence(content, filename, lang)

        # First use the application's canonical extractor. This is the zero-AI
        # fallback and is intentionally never discarded when Gemini fails.
        deterministic_fields = {k: {"value": "", "confidence": 0} for k in ocr_intelligence.FIELD_KEYS}
        deterministic_meta = {"status": "not_run"}
        if transcript:
            try:
                parsed = server.extract_fields_from_ocr(transcript, filename or "upload")
                deterministic_fields = parsed.get("fields") or deterministic_fields
                deterministic_meta = {"status": "ok", "field_count": sum(1 for v in deterministic_fields.values() if v.get("value"))}
            except Exception as exc:
                deterministic_meta = {"status": "error", "error": type(exc).__name__}

        fields, ai_meta = ocr_intelligence.extract_fields_with_ai(transcript, "Land Record", deterministic_fields)

        # If OCR itself produced no usable evidence, ask the multimodal model to
        # inspect the original pixels. This is a true rescue path rather than
        # fabricating data from filenames or form labels.
        vision_meta = {"status": "not_needed"}
        if rescue_image is not None and not transcript:
            vision_fields, vision_meta = ocr_intelligence.extract_fields_from_image_with_vision(rescue_image, "Land Record")
            fields = ocr_intelligence._merge(fields, vision_fields)

        validation = ocr_intelligence.validate_fields(fields)
        confidence_values = [int(v.get("confidence", 0)) for v in fields.values() if v.get("value")]
        overall = round(sum(confidence_values) / len(confidence_values), 1) if confidence_values else 0
        result: Dict[str, Any] = {
            "ocr_text": transcript, "cleaned_ocr_text": transcript, "text": transcript,
            "fields": fields, "original_fields": fields, "validation": validation,
            "confidence": overall / 100.0, "mean_conf": overall, "pages": pages,
            "word_count": len(transcript.split()),
            "detected_language": lang if lang != "auto" else "eng",
            "ocr_method": "multi_page_tesseract+deterministic+ai",
            "pipeline_meta": {
                "production_fast_path": True,
                "guided_ocr_skipped": False,
                "gemini_critical_path": True,
                "evidence": evidence_meta,
                "deterministic_extraction": deterministic_meta,
                "ai_extraction": ai_meta,
                "vision_rescue": vision_meta,
            },
        }
        if not transcript:
            result["engine_error"] = "OCR produced no text; multimodal vision rescue was attempted."
        return result

    run_ocr_pipeline_intelligent._land_bridge = True
    server.run_ocr_pipeline = run_ocr_pipeline_intelligent
