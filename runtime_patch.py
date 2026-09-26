"""Explicit production runtime hotfixes loaded before FastAPI starts."""
import io
import json
import os
import re
import time


def apply():
    import ocr_pipeline as mod
    import pytesseract
    from PIL import ImageOps

    def cache_store(chash, user, source_doc_id, filename, lang, ocr_result, fields, validation, pages, metadata):
        mod.ensure_ocr_cache_table()
        scope = mod._visibility_scope_for(user)
        dbmod = mod.get_server()

        def dump(value):
            try:
                return json.dumps(value or {}, ensure_ascii=False)
            except Exception:
                return "{}"

        with dbmod.get_db() as db:
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
                    str((user or {}).get("email") or "").lower(),
                    str((user or {}).get("role") or "").upper(),
                    scope,
                    source_doc_id,
                    os.path.basename(filename or "upload"),
                    lang or "auto",
                    ocr_result.get("text", "") or "",
                    ocr_result.get("cleaned_text", ocr_result.get("text", "")) or "",
                    ocr_result.get("detected_language", "eng") or "eng",
                    float(ocr_result.get("confidence", 0) or 0),
                    dump(fields),
                    dump(validation),
                    ocr_result.get("method", "tesseract_fast") or "tesseract_fast",
                    int(pages or 1),
                    int(ocr_result.get("word_count", 0) or 0),
                    dump(metadata),
                    time.time(),
                ),
            )

    def fast_ocr(image, requested_lang="auto"):
        requested = (requested_lang or "auto").strip().lower()
        if requested in ("", "auto"):
            # English is the cheapest first pass. Hindi is attempted only when
            # the first pass produces almost no text.
            primary = "eng"
            fallback = "eng+hin"
        else:
            try:
                candidates = mod.get_server()._ocr_languages(requested)
            except Exception:
                candidates = [requested]
            primary = candidates[0] if candidates else "eng"
            fallback = "+".join(candidates[:2]) if candidates else "eng"

        gray = ImageOps.grayscale(image)
        max_side = 2600
        if max(gray.size) > max_side:
            scale = max_side / max(gray.size)
            gray = gray.resize((max(1, int(gray.width * scale)), max(1, int(gray.height * scale))))

        def recognize(language):
            text = pytesseract.image_to_string(
                gray,
                lang=language,
                config="--oem 1 --psm 6",
            ) or ""
            return re.sub(r"\n{3,}", "\n\n", text).strip()

        text = recognize(primary)
        if len(re.findall(r"\S+", text)) < 4 and fallback != primary:
            alt = recognize(fallback)
            if len(re.findall(r"\S+", alt)) > len(re.findall(r"\S+", text)):
                text, primary = alt, fallback

        words = re.findall(r"\S+", text)
        detected = mod.get_server().detect_primary_script(text) or "eng"
        return {
            "text": text,
            "confidence": 0.80 if words else 0.0,
            "word_count": len(words),
            "detected_language": detected,
            "method": "tesseract_fast",
            "strategy": {"lang": primary, "psm": 6},
        }

    mod.cache_store = cache_store
    mod.run_fast_ocr = fast_ocr

    # Remove only the logged-in utility-bar logo. Keep the authentication logo
    # and the main portal header logo.
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
    try:
        html = open(path, encoding="utf-8").read()
        app_start = html.find('<div id="appShell" class="hidden">')
        if app_start >= 0:
            utility_start = html.find('<div class="gov-utility-bar">', app_start)
            header_start = html.find('<div class="gov-header">', utility_start)
            if utility_start >= 0 and header_start > utility_start:
                html = html[:utility_start] + html[header_start:]
                open(path, "w", encoding="utf-8").write(html)
    except Exception as exc:
        print("[RUNTIME PATCH] logo cleanup skipped:", type(exc).__name__)
