"""Runtime OCR reliability patch.

Keeps the canonical OCR pipeline but hardens the actual Tesseract invocation:
- resolves Tesseract on every process start instead of trusting PATH alone;
- discovers installed language packs and falls back to English;
- tries several page-segmentation/image variants when the first pass is blank;
- preserves line boundaries and word confidence;
- returns an explicit engine error instead of a silent empty success.
"""
from __future__ import annotations

import os
from typing import Any, Dict


def install() -> None:
    import ocr_pipeline as pipeline
    import pytesseract
    from PIL import ImageEnhance, ImageFilter, ImageOps

    if getattr(pipeline, "_OCR_RUNTIME_RELIABILITY_INSTALLED", False):
        return

    server = pipeline.get_server()

    # Re-resolve the executable at runtime. This matters on hosts where the
    # Python package is installed but the binary is injected by the image/host.
    try:
        cmd = server.locate_tesseract()
        if cmd:
            pytesseract.pytesseract.tesseract_cmd = cmd
            # Reset the server's cached availability after assigning the path.
            server._TESSERACT_OK = None
    except Exception:
        pass

    def _installed_languages() -> set[str]:
        try:
            if not server.tesseract_available():
                return set()
            return set(pytesseract.get_languages(config=""))
        except Exception:
            return set()

    def _candidates(requested: str) -> list[str]:
        requested = (requested or "auto").strip().lower()
        supported = {str(x.get("code")) for x in getattr(server, "SUPPORTED_LANGUAGES", [])}
        installed = _installed_languages()
        if requested != "auto" and requested in supported:
            return [requested] if not installed or requested in installed else ["eng"]
        if installed:
            combo = [c for c in ("hin", "eng", "tel", "tam", "ben", "mar", "guj", "pan", "kan", "ori", "urd") if c in installed]
            if "eng" not in combo and "eng" in installed:
                combo.insert(0, "eng")
            return ["+".join(combo)] if combo else (["eng"] if "eng" in installed else [])
        return ["eng"]

    def _build(data: Dict[str, Any]):
        texts = data.get("text", []) if data else []
        confs = data.get("conf", []) if data else []
        blocks = data.get("block_num") or [0] * len(texts)
        pars = data.get("par_num") or [0] * len(texts)
        lines = data.get("line_num") or [0] * len(texts)
        grouped: dict[tuple[int, int, int], list[str]] = {}
        order: list[tuple[int, int, int]] = []
        words: list[str] = []
        scores: list[float] = []
        for i, raw in enumerate(texts):
            word = str(raw or "").strip()
            try:
                conf = float(confs[i])
            except Exception:
                conf = -1.0
            if not word or conf < 0:
                continue
            words.append(word)
            scores.append(max(0.0, min(1.0, conf / 100.0)))
            try:
                key = (int(blocks[i]), int(pars[i]), int(lines[i]))
            except Exception:
                key = (0, 0, len(order))
            if key not in grouped:
                grouped[key] = []
                order.append(key)
            grouped[key].append(word)
        text = "\n".join(" ".join(grouped[k]) for k in order)
        return text, words, (sum(scores) / len(scores) if scores else 0.0)

    def _recognize(image, language: str, psm: int):
        try:
            return pytesseract.image_to_data(
                image,
                lang=language,
                config=f"--oem 3 --psm {psm} -c preserve_interword_spaces=1",
                output_type=pytesseract.Output.DICT,
                timeout=25,
            ), None
        except Exception as exc:
            return None, f"{type(exc).__name__}: {exc}"

    def robust_ocr(image, requested_lang: str = "auto") -> Dict[str, Any]:
        candidates = _candidates(requested_lang)
        if not server.tesseract_available():
            return {
                "text": "", "confidence": 0.0, "word_count": 0,
                "detected_language": "eng", "method": "tesseract_unavailable",
                "engine_error": "Tesseract executable is unavailable. Set TESSERACT_CMD or deploy the repository Dockerfile.",
            }
        if not candidates:
            return {"text": "", "confidence": 0.0, "word_count": 0,
                    "detected_language": "eng", "method": "tesseract_no_languages",
                    "engine_error": "Tesseract is installed but no usable language packs were found."}

        src = ImageOps.exif_transpose(image).convert("L")
        if max(src.size) > 3200:
            scale = 3200.0 / max(src.size)
            src = src.resize((max(1, int(src.width * scale)), max(1, int(src.height * scale))))
        elif max(src.size) < 1400:
            scale = 1400.0 / max(src.size)
            src = src.resize((max(1, int(src.width * scale)), max(1, int(src.height * scale))))

        normal = ImageOps.autocontrast(src, cutoff=0.5)
        normal = ImageEnhance.Contrast(normal).enhance(1.15)
        normal = ImageEnhance.Sharpness(normal).enhance(1.35)
        variants = [
            (normal, "enhanced"),
            (normal.filter(ImageFilter.MedianFilter(size=3)), "denoised"),
            (normal.point(lambda p: 255 if p > 180 else 0), "threshold"),
        ]
        attempts = []
        errors = []
        # Try the selected language first, then English if it is installed.
        langs = list(candidates)
        if "eng" in _installed_languages() and "eng" not in langs:
            langs.append("eng")

        for variant, variant_name in variants:
            for language in langs:
                for psm in (6, 11, 3):
                    data, error = _recognize(variant, language, psm)
                    if error:
                        errors.append(error)
                        continue
                    text, words, confidence = _build(data)
                    attempts.append((len(words), confidence, text, language, psm, variant_name))
                    if len(words) >= 4 and confidence >= 0.30:
                        best = attempts[-1]
                        return {
                            "text": best[2], "confidence": round(best[1], 3),
                            "word_count": best[0], "detected_language": server.detect_primary_script(best[2]) or "eng",
                            "tesseract_language": best[3], "method": f"tesseract_{best[5]}_psm{best[4]}",
                            "strategy": {"lang": best[3], "psm": best[4], "variant": best[5]},
                        }

        if attempts:
            best = max(attempts, key=lambda item: (item[0], item[1]))
            if best[0] > 0:
                return {
                    "text": best[2], "confidence": round(best[1], 3), "word_count": best[0],
                    "detected_language": server.detect_primary_script(best[2]) or "eng",
                    "tesseract_language": best[3], "method": f"tesseract_{best[5]}_psm{best[4]}",
                    "strategy": {"lang": best[3], "psm": best[4], "variant": best[5]},
                }
        return {
            "text": "", "confidence": 0.0, "word_count": 0, "detected_language": "eng",
            "method": "tesseract_blank", "engine_error": errors[-1] if errors else "Tesseract returned no recognized words",
            "strategy": {"languages": langs, "variants": [v[1] for v in variants], "psms": [6, 11, 3]},
        }

    pipeline.run_fast_ocr = robust_ocr
    pipeline._OCR_RUNTIME_RELIABILITY_INSTALLED = True

    print("[OCR] runtime reliability patch installed; Tesseract:",
          getattr(pytesseract.pytesseract, "tesseract_cmd", "tesseract"))
