"""Fast deterministic document extraction bridge.

Text PDFs use their embedded text layer; scanned PDFs and images use the
canonical fast OCR pipeline. No Gemini/guided OCR call is allowed on the
critical upload path.
"""
from __future__ import annotations

import os
from typing import Any, Dict


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
