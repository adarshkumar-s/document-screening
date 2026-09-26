"""Small runtime compatibility fixes loaded automatically by Python's site module.

These fixes are intentionally narrow:
- PostgreSQL uses SQL-standard UPSERT for the OCR cache (the previous
  SQLite-only INSERT OR REPLACE caused every production OCR request to become
  a 422 after OCR completed).
- The fast OCR path uses a lightweight direct Tesseract invocation instead of
  the heavier guided OCR wrapper, with a conservative fallback for multilingual
  documents.
- The logged-in application shell hides the duplicate utility-bar logo while
  retaining the main VectorFlow header logo.

No database/schema replacement or alternate OCR queue is introduced.
"""
from __future__ import annotations

import builtins
import io
import json
import os
import re
import time
from typing import Any, Dict, Optional


_ORIGINAL_IMPORT = builtins.__import__
_PATCHED = set()


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return "{}"


def _patch_ocr_pipeline(mod):
    if getattr(mod, "_runtime_hotfix_applied", False):
        return

    # ------------------------------------------------------------------
    # PostgreSQL-safe OCR cache UPSERT.
    # ------------------------------------------------------------------
    def cache_store_pg_safe(chash: str, user: Dict[str, Any], source_doc_id: str,
                            filename: str, lang: str, ocr_result: Dict[str, Any],
                            fields: Dict[str, Any], validation: Dict[str, Any],
                            pages: int, metadata: Dict[str, Any]) -> None:
        mod.ensure_ocr_cache_table()
        scope = mod._visibility_scope_for(user)
        fields_j = _json(fields)
        validation_j = _json(validation)
        meta_j = _json(metadata or {})
        srv = mod.get_server()
        with srv.get_db() as db:
            # ON CONFLICT works on both PostgreSQL and modern SQLite, unlike
            # SQLite's INSERT OR REPLACE which is invalid PostgreSQL syntax.
            db.execute(
                """
                INSERT INTO ocr_cache (
                    content_hash, owner_email, owner_role, visibility_scope, source_doc_id,
                    filename, lang, ocr_text, cleaned_text, detected_language, confidence,
                    fields, validation, ocr_method, pages, word_count, metadata_json, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT (content_hash) DO UPDATE SET
                    owner_email=excluded.owner_email,
                    owner_role=excluded.owner_role,
                    visibility_scope=excluded.visibility_scope,
                    source_doc_id=excluded.source_doc_id,
                    filename=excluded.filename,
                    lang=excluded.lang,
                    ocr_text=excluded.ocr_text,
                    cleaned_text=excluded.cleaned_text,
                    detected_language=excluded.detected_language,
                    confidence=excluded.confidence,
                    fields=excluded.fields,
                    validation=excluded.validation,
                    ocr_method=excluded.ocr_method,
                    pages=excluded.pages,
                    word_count=excluded.word_count,
                    metadata_json=excluded.metadata_json
                """,
                (
                    chash,
                    str((user or {}).get("email") or ""),
                    str((user or {}).get("role") or ""),
                    scope,
                    source_doc_id,
                    os.path.basename(filename or "upload"),
                    lang or "auto",
                    ocr_result.get("text", "") or "",
                    ocr_result.get("cleaned_text", ocr_result.get("text", "")) or "",
                    ocr_result.get("detected_language", "eng") or "eng",
                    float(ocr_result.get("confidence", 0) or 0),
                    fields_j,
                    validation_j,
                    ocr_result.get("method", "tesseract") or "tesseract",
                    int(pages or 1),
                    int(ocr_result.get("word_count", 0) or 0),
                    meta_j,
                    time.time(),
                ),
            )

    mod.cache_store = cache_store_pg_safe

    # ------------------------------------------------------------------
    # Lightweight, predictable OCR. Avoid the old guided wrapper on the
    # critical path; it could invoke several preprocessing/recognition steps.
    # ------------------------------------------------------------------
    def direct_fast_ocr(image, requested_lang: str = "auto") -> Dict[str, Any]:
        srv = mod.get_server()
        if not getattr(srv, "HAS_TESSERACT", False):
            raise RuntimeError("Tesseract OCR engine is not installed")
        import pytesseract
        from PIL import ImageOps

        requested = (requested_lang or "auto").strip().lower()
        if requested in ("", "auto"):
            primary = "eng+hin"
            fallback = "eng+hin+tel+tam"
        else:
            candidates = srv._ocr_languages(requested)
            primary = candidates[0] if candidates else "eng"
            fallback = "+".join(candidates[:4]) if candidates else "eng"

        gray = ImageOps.grayscale(image)
        # Keep the first pass cheap. Do not resize already-large scans.
        if max(gray.size) > 4200:
            scale = 4200 / max(gray.size)
            gray = gray.resize((max(1, int(gray.width * scale)), max(1, int(gray.height * scale))))

        def recognize(lang):
            data = pytesseract.image_to_data(
                gray,
                lang=lang,
                config="--oem 1 --psm 6",
                output_type=pytesseract.Output.DICT,
            )
            words = []
            confs = []
            for txt, conf in zip(data.get("text", []), data.get("conf", [])):
                txt = (txt or "").strip()
                try:
                    c = float(conf)
                except Exception:
                    c = -1
                if txt and c >= 0:
                    words.append(txt)
                    confs.append(c)
            return " ".join(words), confs

        text, confs = recognize(primary)
        # Only pay for the multilingual fallback when the cheap pass did not
        # produce useful text. This keeps ordinary English scans fast.
        if len(text.split()) < 3 and fallback != primary:
            text2, confs2 = recognize(fallback)
            if len(text2.split()) > len(text.split()):
                text, confs = text2, confs2
                primary = fallback

        mean_conf = (sum(confs) / len(confs) / 100.0) if confs else 0.0
        detected = srv.detect_primary_script(text) or "eng"
        return {
            "text": text,
            "confidence": mean_conf,
            "word_count": len(text.split()),
            "detected_language": detected,
            "strategy": {"lang": primary, "psm": 6, "method": "tesseract_direct_fast"},
        }

    mod.run_fast_ocr = direct_fast_ocr
    mod._runtime_hotfix_applied = True


def _patch_index_html():
    """Remove only the logged-in app-shell utility logo; keep auth + main header."""
    base = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, "index.html")
    if not os.path.isfile(path):
        return
    try:
        text = open(path, "r", encoding="utf-8").read()
        marker = '<div id="appShell" class="hidden">'
        start = text.find(marker)
        if start < 0:
            return
        utility_start = text.find('  <div class="gov-utility-bar">', start)
        header_start = text.find('  <div class="gov-header">', utility_start)
        if utility_start < 0 or header_start < 0 or header_start < utility_start:
            return
        # Only remove the first utility bar inside appShell. The authentication
        # screen's utility bar is intentionally left untouched.
        updated = text[:utility_start] + text[header_start:]
        if updated != text:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(updated)
    except Exception:
        # Static branding must never prevent application startup.
        pass


def _import(name, globals=None, locals=None, fromlist=(), level=0):
    module = _ORIGINAL_IMPORT(name, globals, locals, fromlist, level)
    root = name.split(".", 1)[0]
    if root == "ocr_pipeline":
        try:
            _patch_ocr_pipeline(module)
        except Exception:
            pass
    return module


builtins.__import__ = _import
_patch_index_html()
