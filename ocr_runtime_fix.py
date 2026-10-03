"""High-recall runtime OCR engine.

The previous runtime patch tried a giant multilingual Tesseract language pack
in one pass. That is slow and, for many Indian scans, materially worse than
staged language selection. This implementation deliberately spends more CPU
when the document is ambiguous: probe likely scripts cheaply, then run the
best candidates at full resolution with several page-segmentation and image
variants. A result is returned only after the attempts are scored.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable
import os
import re
import sys


def install() -> None:
    import ocr_pipeline as pipeline
    import pytesseract
    from PIL import ImageEnhance, ImageFilter, ImageOps

    if getattr(pipeline, "_OCR_RUNTIME_RELIABILITY_INSTALLED", False):
        return

    server = pipeline.get_server()
    try:
        cmd = server.locate_tesseract()
        if cmd:
            pytesseract.pytesseract.tesseract_cmd = cmd
            server._TESSERACT_OK = None
    except Exception:
        pass

    def installed() -> set[str]:
        try:
            if not server.tesseract_available():
                return set()
            return set(pytesseract.get_languages(config=""))
        except Exception:
            return set()

    def requested_candidates(requested: str) -> list[str]:
        have = installed()
        requested = (requested or "auto").strip().lower()
        if requested != "auto":
            if requested in have:
                return [requested] + (["eng"] if requested != "eng" and "eng" in have else [])
            return ["eng"] if "eng" in have else []
        # This is intentionally small for the probe. The full pass can add
        # candidates once a script is detected from the probe output.
        preferred = ["eng", "hin", "ben", "tel", "tam", "mar", "guj", "pan", "kan", "ori", "urd"]
        return [x for x in preferred if x in have]

    def build(data: Dict[str, Any]):
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
        return "\n".join(" ".join(grouped[k]) for k in order), words, (sum(scores) / len(scores) if scores else 0.0)

    def recognize(image, language: str, psm: int, timeout: int = 45):
        try:
            return pytesseract.image_to_data(
                image,
                lang=language,
                config=f"--oem 3 --psm {psm} -c preserve_interword_spaces=1",
                output_type=pytesseract.Output.DICT,
                timeout=timeout,
            ), None
        except Exception as exc:
            return None, f"{type(exc).__name__}: {exc}"

    def normalise_script(text: str) -> str:
        # Prefer the application's script detector, but never let detection
        # failure prevent OCR from completing.
        try:
            return server.detect_primary_script(text) or "eng"
        except Exception:
            return "eng"

    def score(text: str, words: list[str], confidence: float, language: str, requested: str) -> float:
        if not words:
            return 0.0
        # Confidence matters, but raw word count alone should not win because
        # a wrong language can hallucinate many tiny tokens.
        useful = sum(1 for w in words if len(re.sub(r"\W", "", w, flags=re.UNICODE)) >= 2)
        script = normalise_script(text)
        script_bonus = 0.12 if requested == "auto" and script == language else 0.0
        if language == "eng" and script == "eng":
            script_bonus += 0.05
        return confidence * 0.62 + min(useful / 80.0, 1.0) * 0.38 + script_bonus

    def normalise_source(image, warnings: list[str]):
        """Greyscale + bound the source image, degrading instead of aborting.

        A preprocessing failure on one odd input (unsupported mode, truncated
        object, exotic subclass) must not zero out the whole OCR attempt: we
        hand Tesseract the original object and record why the ladder was
        skipped.  Real PIL images take the full path.
        """
        try:
            src = ImageOps.exif_transpose(image).convert("L")
        except Exception as exc:
            note = f"grayscale_skipped: {type(exc).__name__}: {exc}"
            warnings.append(note)
            print(f"[OCR] preprocessing skipped ({note})", file=sys.stderr)
            return image
        try:
            longest = max(src.size)
            if longest > 5000:
                scale = 5000.0 / longest
                src = src.resize((max(1, int(src.width * scale)), max(1, int(src.height * scale))))
            elif longest < 1600:
                scale = 1600.0 / max(1, longest)
                src = src.resize((max(1, int(src.width * scale)), max(1, int(src.height * scale))))
        except Exception as exc:
            warnings.append(f"resize_skipped: {type(exc).__name__}: {exc}")
        return src

    def variants(src, warnings: list[str]):
        """Build the cheap preprocessing ladder, dropping steps that fail.

        Each rung is independent: if median filtering or thresholding is not
        possible for this image, the remaining rungs still run rather than the
        whole page producing no OCR attempts.
        """
        out = []
        try:
            normal = ImageOps.autocontrast(src, cutoff=0.5)
            normal = ImageEnhance.Contrast(normal).enhance(1.18)
            normal = ImageEnhance.Sharpness(normal).enhance(1.4)
            out.append((normal, "enhanced"))
        except Exception as exc:
            warnings.append(f"enhanced_skipped: {type(exc).__name__}: {exc}")
            normal = None
        if normal is not None:
            # Keep variants cheap. The source project uses the same principle:
            # preprocessing is a ladder, not one destructive transformation.
            try:
                out.append((normal.point(lambda p: 255 if p > 175 else 0), "threshold"))
            except Exception as exc:
                warnings.append(f"threshold_skipped: {type(exc).__name__}: {exc}")
            try:
                out.append((normal.filter(ImageFilter.MedianFilter(size=3)), "denoised"))
            except Exception as exc:
                warnings.append(f"denoised_skipped: {type(exc).__name__}: {exc}")
        if not out:
            # Nothing in the ladder was applicable: OCR the source as given.
            out.append((src, "raw"))
        return out

    def robust_ocr(image, requested_lang: str = "auto") -> Dict[str, Any]:
        have = installed()
        if not server.tesseract_available():
            return {
                "text": "", "confidence": 0.0, "word_count": 0,
                "detected_language": "eng", "method": "tesseract_unavailable",
                "engine_error": "Tesseract executable is unavailable. Set TESSERACT_CMD or deploy the repository Dockerfile.",
            }
        candidates = requested_candidates(requested_lang)
        if not candidates:
            return {
                "text": "", "confidence": 0.0, "word_count": 0,
                "detected_language": "eng", "method": "tesseract_no_languages",
                "engine_error": "Tesseract is installed but no usable language packs were found.",
            }

        preprocessing_warnings: list[str] = []
        src = normalise_source(image, preprocessing_warnings)

        requested = (requested_lang or "auto").strip().lower()
        errors: list[str] = []
        probe_scores = []
        try:
            base_probe = ImageOps.autocontrast(src, cutoff=0.5)
        except Exception as exc:
            preprocessing_warnings.append(f"probe_contrast_skipped: {type(exc).__name__}: {exc}")
            base_probe = src

        # Explicit language: do not waste time guessing. Auto language uses a
        # small one-pass-per-language probe; this is slower than a huge combined
        # Tesseract language call but considerably more reliable on mixed scripts.
        if requested == "auto":
            probe_image = base_probe
            try:
                if max(probe_image.size) > 1200:
                    s = 1200.0 / max(probe_image.size)
                    probe_image = probe_image.resize((max(1, int(probe_image.width * s)), max(1, int(probe_image.height * s))))
            except Exception as exc:
                preprocessing_warnings.append(f"probe_resize_skipped: {type(exc).__name__}: {exc}")
                probe_image = base_probe
            for lang in candidates:
                data, err = recognize(probe_image, lang, 6, timeout=18)
                if err:
                    errors.append(f"{lang}: {err}")
                    continue
                text, words, conf = build(data)
                probe_scores.append((score(text, words, conf, lang, requested), lang, text, len(words), conf))
            probe_scores.sort(reverse=True)
            # Always keep English as a fallback, then the two strongest script
            # candidates. This avoids the old 10-language mega-pass.
            selected = []
            if "eng" in have:
                selected.append("eng")
            for _, lang, _, _, _ in probe_scores:
                if lang not in selected:
                    selected.append(lang)
                if len(selected) >= 3:
                    break
            candidates = selected or candidates[:3]

        best = None
        for variant, variant_name in variants(src, preprocessing_warnings):
            for language in candidates:
                # For auto, run a few layout modes. For an explicit language,
                # psm 6 + 11 is usually enough; psm 3 is the final sparse-page
                # fallback.
                psms = (6, 11, 3)
                for psm in psms:
                    data, err = recognize(variant, language, psm)
                    if err:
                        errors.append(f"{language}/psm{psm}: {err}")
                        continue
                    text, words, conf = build(data)
                    if not words:
                        continue
                    quality = score(text, words, conf, language, requested)
                    candidate = (quality, len(words), conf, text, language, psm, variant_name)
                    if best is None or candidate[:3] > best[:3]:
                        best = candidate

        if best is None:
            return {
                "text": "", "confidence": 0.0, "word_count": 0,
                "detected_language": "eng", "method": "tesseract_blank",
                "engine_error": errors[-1] if errors else "Tesseract returned no recognized words",
                "strategy": {"languages": candidates, "variants": ["enhanced", "denoised", "threshold"], "psms": [6, 11, 3],
                             "preprocessing_warnings": preprocessing_warnings, "engine_errors": errors[:20]},
            }
        _, count, conf, text, language, psm, variant_name = best
        return {
            "text": text, "confidence": round(conf, 3), "word_count": count,
            "detected_language": normalise_script(text),
            "tesseract_language": language,
            "method": f"tesseract_{variant_name}_psm{psm}",
            "strategy": {"lang": language, "psm": psm, "variant": variant_name, "probe": probe_scores[:5],
                         "preprocessing_warnings": preprocessing_warnings, "engine_errors": errors[:20]},
        }

    pipeline.run_fast_ocr = robust_ocr
    pipeline._OCR_RUNTIME_RELIABILITY_INSTALLED = True
    print("[OCR] high-recall staged runtime installed; Tesseract:", getattr(pytesseract.pytesseract, "tesseract_cmd", "tesseract"))
