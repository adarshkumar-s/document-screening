"""Final OCR safety net.

If structured image_to_data extraction returns no words, perform a slower plain
Tesseract image_to_string pass. Some scans that defeat bounding-box extraction
still yield useful text through Tesseract's text renderer. This layer is only
used when the high-recall runtime has already failed, so normal uploads are
unchanged.
"""
from __future__ import annotations


def install() -> None:
    import io
    import ocr_pipeline as pipeline
    import pytesseract
    from PIL import Image, ImageEnhance, ImageOps

    if getattr(pipeline, "_OCR_LAST_RESORT_INSTALLED", False):
        return
    original = pipeline.run_fast_ocr
    server = pipeline.get_server()

    def fallback(image, requested_lang="auto"):
        result = original(image, requested_lang)
        if (result.get("text") or "").strip():
            return result
        if not server.tesseract_available():
            return result
        try:
            have = set(pytesseract.get_languages(config=""))
        except Exception:
            have = {"eng"}
        requested = (requested_lang or "auto").strip().lower()
        langs = []
        if requested != "auto" and requested in have:
            langs.append(requested)
        if "eng" in have:
            langs.append("eng")
        for code in ("hin", "tel", "tam", "ben", "mar", "guj", "pan", "kan", "ori", "urd"):
            if code in have and code not in langs:
                langs.append(code)
        src = ImageOps.exif_transpose(image).convert("L")
        if max(src.size) > 5000:
            scale = 5000.0 / max(src.size)
            src = src.resize((max(1, int(src.width * scale)), max(1, int(src.height * scale))))
        variants = [
            ImageOps.autocontrast(src, cutoff=0.5),
            ImageEnhance.Contrast(ImageOps.autocontrast(src, cutoff=0.5)).enhance(1.2),
        ]
        best = ""
        best_lang = "eng"
        for img in variants:
            for lang in langs[:4]:
                for psm in (6, 11, 3):
                    try:
                        text = pytesseract.image_to_string(img, lang=lang, config=f"--oem 3 --psm {psm}", timeout=45).strip()
                    except Exception:
                        continue
                    if len(text.split()) > len(best.split()):
                        best, best_lang = text, lang
        if not best:
            return result
        return {
            **result,
            "text": best,
            "cleaned_text": best,
            "word_count": len(best.split()),
            "confidence": max(float(result.get("confidence", 0) or 0), 0.35),
            "detected_language": server.detect_primary_script(best) or "eng",
            "tesseract_language": best_lang,
            "method": "tesseract_last_resort_text",
            "engine_error": "",
            "strategy": {"fallback": "image_to_string", "language": best_lang, "psm": [6, 11, 3]},
        }

    pipeline.run_fast_ocr = fallback
    pipeline._OCR_LAST_RESORT_INSTALLED = True
