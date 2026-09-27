"""Final high-recall OCR rescue layer.

The application already has Tesseract, deterministic extraction, and a Gemini
provider. This module connects those pieces on the actual production path:

* retry badly oriented scans at 0/90/180/270 degrees;
* preserve the strongest Tesseract result instead of accepting the first result;
* when OCR coverage is poor, inspect the actual document pixels with the
  configured multimodal model and merge only explicit values it can read;
* for scanned PDFs, rescue multiple pages rather than trusting page one;
* never invent a value when both OCR and vision are uncertain.

This is intentionally a slower fallback. Normal documents still use the fast
path and do not pay the vision cost when OCR coverage is healthy.
"""
from __future__ import annotations

import asyncio
import io
import os
from typing import Any, Dict, List


def install() -> None:
    import ocr_pipeline as pipeline
    from PIL import Image, ImageOps

    if getattr(pipeline, "_OCR_PRO_RESCUE_INSTALLED", False):
        return

    server = pipeline.get_server()
    original_run_fast = pipeline.run_fast_ocr
    original_pipeline = pipeline.run_fast_ocr_pipeline

    def _quality(result: Dict[str, Any]) -> float:
        if not result:
            return 0.0
        words = float(result.get("word_count", 0) or 0)
        conf = float(result.get("confidence", 0) or 0)
        text = str(result.get("text") or "")
        return conf * 0.7 + min(words / 100.0, 1.0) * 0.3 + min(len(text) / 2000.0, 0.15)

    def _rotate_rescue(image: Image.Image, requested_lang: str) -> Dict[str, Any]:
        base = original_run_fast(image, requested_lang)
        best = base
        if _quality(base) >= 0.42 and int(base.get("word_count", 0) or 0) >= 8:
            return base
        source = ImageOps.exif_transpose(image).convert("L")
        for angle in (90, 180, 270):
            rotated = source.rotate(angle, expand=True, fillcolor=255)
            try:
                candidate = original_run_fast(rotated, requested_lang)
            except Exception:
                continue
            if _quality(candidate) > _quality(best):
                best = dict(candidate)
                best["orientation_rescue"] = angle
        return best

    pipeline.run_fast_ocr = _rotate_rescue

    def _field_count(fields: Any) -> int:
        if not isinstance(fields, dict):
            return 0
        return sum(1 for value in fields.values() if isinstance(value, dict) and str(value.get("value") or "").strip())

    def _needs_vision(result: Dict[str, Any]) -> bool:
        fields = result.get("fields") or {}
        count = _field_count(fields)
        words = int(result.get("word_count", 0) or 0)
        confidence = float(result.get("confidence", 0) or 0)
        if os.getenv("OCR_VISION_ALWAYS", "").strip().lower() == "true":
            return True
        return count < 5 or words < 18 or confidence < 0.48 or not str(result.get("ocr_text") or "").strip()

    async def _vision_page(image: Image.Image, doc_type: str) -> tuple[Dict[str, Any], Dict[str, Any]]:
        try:
            import ocr_intelligence
            return await asyncio.to_thread(
                ocr_intelligence.extract_fields_from_image_with_vision,
                image,
                doc_type or "Land Record",
            )
        except Exception as exc:
            return {}, {"provider": "gemini_vision", "status": "error", "error": type(exc).__name__}

    def _merge_fields(primary: Dict[str, Any], vision: Dict[str, Any]) -> Dict[str, Any]:
        try:
            import ocr_intelligence
            return ocr_intelligence._merge(primary or {}, vision or {})
        except Exception:
            merged = dict(primary or {})
            for key, value in (vision or {}).items():
                if key not in merged or not str((merged.get(key) or {}).get("value") or "").strip():
                    merged[key] = value
            return merged

    async def _rescue_vision(result: Dict[str, Any], content: bytes, filename: str, lang: str, doc_type: str) -> Dict[str, Any]:
        if not _needs_vision(result):
            return result
        if not getattr(server, "ai_client", None):
            meta = dict(result.get("pipeline_meta") or {})
            meta["vision_rescue"] = {"status": "ai_not_configured"}
            result["pipeline_meta"] = meta
            return result

        pages: List[Image.Image] = []
        name = (filename or "").lower()
        if name.endswith(".pdf") and getattr(server, "HAS_PDFIUM", False):
            try:
                pdf = server.pdfium.PdfDocument(content)
                max_pages = min(len(pdf), int(os.getenv("OCR_VISION_MAX_PAGES", "20")))
                for index in range(max_pages):
                    pages.append(pdf[index].render(scale=2.4).to_pil())
            except Exception:
                pages = []
        else:
            try:
                image = Image.open(io.BytesIO(content))
                image.load()
                pages = [image]
            except Exception:
                pages = []

        if not pages:
            return result

        vision_fields: Dict[str, Any] = {}
        statuses: List[Dict[str, Any]] = []
        for image in pages:
            fields, meta = await _vision_page(image, doc_type)
            statuses.append(meta)
            if fields:
                vision_fields = _merge_fields(vision_fields, fields)

        merged = _merge_fields(result.get("fields") or {}, vision_fields)
        result["fields"] = merged
        result["original_fields"] = merged

        # Keep the application's existing validation object authoritative. The
        # legacy validator used by process_upload returns the verdict/status
        # shape expected by the database layer. A newer helper may return only
        # field-level validation details, so never replace a valid verdict with
        # an incompatible object.
        try:
            import ocr_intelligence
            candidate_validation = ocr_intelligence.validate_fields(merged)
            if isinstance(candidate_validation, dict) and candidate_validation.get("verdict"):
                result["validation"] = candidate_validation
            else:
                existing = result.get("validation")
                if not isinstance(existing, dict):
                    result["validation"] = {"verdict": "REVIEW", "issues": [], "warnings": []}
        except Exception:
            if not isinstance(result.get("validation"), dict):
                result["validation"] = {"verdict": "REVIEW", "issues": [], "warnings": []}

        found_conf = [float(v.get("confidence", 0) or 0) for v in merged.values() if isinstance(v, dict) and str(v.get("value") or "").strip()]
        if found_conf:
            result["mean_conf"] = round(sum(found_conf) / len(found_conf), 1)
            result["confidence"] = result["mean_conf"] / 100.0 if result["mean_conf"] > 1 else result["mean_conf"]

        meta = dict(result.get("pipeline_meta") or {})
        meta["vision_rescue"] = {
            "status": "completed",
            "pages_inspected": len(pages),
            "page_results": statuses,
            "trigger": "low_ocr_coverage",
        }
        result["pipeline_meta"] = meta
        result["ocr_method"] = str(result.get("ocr_method") or "") + "+vision_rescue"
        return result

    async def robust_pipeline(content: bytes, filename: str, lang: str = "auto", doc_type_hint: str = "Land Record", user=None, doc_id=None):
        result = await original_pipeline(
            content, filename, lang,
            doc_type_hint=doc_type_hint,
            user=user,
            doc_id=doc_id,
        )
        return await _rescue_vision(result, content, filename, lang, doc_type_hint)

    pipeline.run_fast_ocr_pipeline = robust_pipeline
    pipeline._OCR_PRO_RESCUE_INSTALLED = True
    print("[OCR] orientation + multimodal vision rescue installed")
