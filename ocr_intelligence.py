"""Document intelligence layer for high-recall land-record extraction.

OCR and field extraction are deliberately separate: Tesseract supplies the
verbatim evidence, then Gemini converts that evidence into the application's
canonical field schema. The AI step never invents values; every value must be
supported by OCR text and gets a confidence score.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Tuple

FIELD_KEYS = [
    "owner_name", "father_name", "survey_number", "khasra_number", "khata_number",
    "plot_number", "area", "village", "tehsil", "district", "state", "document_date",
    "land_class", "ownership_type", "mutation_no", "document_type"
]


def _empty_field() -> Dict[str, Any]:
    return {"value": "", "confidence": 0, "source": "not_found"}


def _normalise_fields(raw: Any, ocr_text: str) -> Dict[str, Dict[str, Any]]:
    data = raw if isinstance(raw, dict) else {}
    out: Dict[str, Dict[str, Any]] = {}
    for key in FIELD_KEYS:
        value = data.get(key)
        if isinstance(value, dict):
            val = str(value.get("value") or "").strip()
            try:
                conf = max(0, min(100, int(float(value.get("confidence", 0) or 0))))
            except Exception:
                conf = 0
        else:
            val = str(value or "").strip()
            conf = 0
        # Never accept an AI value that cannot be located in the OCR evidence.
        if val and val.casefold() not in ocr_text.casefold():
            val, conf = "", 0
        out[key] = {"value": val, "confidence": conf, "source": "ocr+ai" if val else "not_found"}
    return out


def _json_from_response(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            return {}
        try:
            obj = json.loads(match.group(0))
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}


def extract_fields_with_ai(ocr_text: str, doc_type_hint: str = "Land Record") -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """Extract all UI fields from OCR evidence using Gemini when configured.

    Falls back to an all-empty schema rather than fabricating data. This keeps
    the UI explicit about extraction failure and lets the operator retry.
    """
    empty = {k: _empty_field() for k in FIELD_KEYS}
    if not ocr_text.strip():
        return empty, {"provider": "none", "status": "no_ocr_text"}
    try:
        import server
        client = getattr(server, "ai_client", None)
        if client is None:
            return empty, {"provider": "gemini", "status": "not_configured"}
        model = os.getenv("OCR_AI_MODEL", "gemini-2.5-flash")
        schema = ", ".join(f'"{k}": {{"value": "", "confidence": 0}}' for k in FIELD_KEYS)
        prompt = f"""You are a document extraction engine for Indian land records.
Extract values ONLY from the OCR evidence below. Do not guess, infer, translate
names, or fill missing values. Preserve the document's spelling, digits,
slashes and punctuation. Handle Hindi/English and mixed-language records.
Return JSON only with exactly these keys: {{{schema}}}.
Confidence is 0-100 and must reflect how clearly the value is supported.
Document type hint: {doc_type_hint}
OCR EVIDENCE:\n{ocr_text[:50000]}"""
        response = client.models.generate_content(model=model, contents=prompt)
        fields = _normalise_fields(_json_from_response(getattr(response, "text", "")), ocr_text)
        found = sum(1 for v in fields.values() if v["value"])
        return fields, {"provider": "gemini", "model": model, "status": "ok", "fields_found": found}
    except Exception as exc:
        return empty, {"provider": "gemini", "status": "error", "error": type(exc).__name__}


def validate_fields(fields: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    required = ["owner_name", "survey_number", "khasra_number", "village", "district"]
    missing = [k for k in required if not (fields.get(k) or {}).get("value")]
    found = sum(1 for v in fields.values() if (v or {}).get("value"))
    return {"required_missing": missing, "field_count": len(fields), "found_count": found,
            "coverage_percent": round(found * 100 / max(1, len(fields)), 1), "valid": not missing}
