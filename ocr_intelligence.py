"""High-recall document intelligence for Indian land records."""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Tuple, Optional

FIELD_KEYS = [
    "owner_name", "father_name", "survey_number", "khasra_number", "khata_number",
    "plot_number", "area", "village", "tehsil", "district", "state", "document_date",
    "land_class", "ownership_type", "mutation_no", "document_type"
]


def _empty_field() -> Dict[str, Any]:
    return {"value": "", "confidence": 0, "source": "not_found"}


def _label_fallback(evidence: str) -> Dict[str, Dict[str, Any]]:
    """Extract explicit label:value pairs before asking an AI model.

    Land-record scans often have good text but OCR label spelling differs from
    the canonical parser. This conservative pass only captures values that are
    explicitly adjacent to a known label; it never infers a value from context.
    """
    out = {k: _empty_field() for k in FIELD_KEYS}
    aliases = {
        "owner_name": ["owner name", "name of owner", "land owner", "भूस्वामी", "खातेदार", "नाम"],
        "father_name": ["father name", "father's name", "guardian name", "पिता का नाम", "पिता"],
        "survey_number": ["survey number", "survey no", "survey", "सर्वे नंबर", "सर्वे नं"],
        "khasra_number": ["khasra number", "khasra no", "khasra", "खसरा नंबर", "खसरा नं"],
        "khata_number": ["khata number", "khata no", "khata", "खाता नंबर", "खाता नं"],
        "plot_number": ["plot number", "plot no", "plot", "प्लॉट नंबर"],
        "area": ["area", "extent", "land area", "क्षेत्रफल", "रकबा"],
        "village": ["village", "village name", "गांव", "ग्राम", "मौजा"],
        "tehsil": ["tehsil", "taluka", "mandal", "तहसील", "तालुका", "मंडल"],
        "district": ["district", "जिला"],
        "state": ["state", "राज्य"],
        "document_date": ["document date", "date of document", "date", "दिनांक"],
        "land_class": ["land class", "land type", "भूमि वर्ग", "भूमि प्रकार"],
        "ownership_type": ["ownership type", "ownership", "स्वामित्व"],
        "mutation_no": ["mutation no", "mutation number", "नामांतरण", "म्यूटेशन"],
        "document_type": ["document type", "record type", "दस्तावेज प्रकार"],
    }
    # Process line-by-line first; then a same-line fallback over the full text.
    lines = [re.sub(r"\s+", " ", x).strip() for x in (evidence or "").splitlines() if x.strip()]
    for key, labels in aliases.items():
        label_re = "|".join(re.escape(x) for x in sorted(labels, key=len, reverse=True))
        pattern = re.compile(rf"(?:^|[|;])\s*(?:{label_re})\s*(?:[:#\-–]|\s{{1,4}})\s*(.+?)\s*$", re.I)
        candidates = []
        for line in lines:
            m = pattern.search(line)
            if m:
                candidates.append(m.group(1).strip(" .;,-–"))
        if not candidates:
            pattern2 = re.compile(rf"(?:{label_re})\s*[:#\-–]\s*([^\n|;]+)", re.I)
            candidates = [m.group(1).strip(" .;,-–") for m in pattern2.finditer(evidence or "")]
        value = next((v for v in candidates if v and len(v) <= 180), "")
        if value:
            out[key] = {"value": value, "confidence": 0.82, "source": "ocr-label"}
    return out


def _normalise_fields(raw: Any, evidence: str, *, source: str = "ocr+ai") -> Dict[str, Dict[str, Any]]:
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
            val, conf = str(value or "").strip(), 0
        if evidence and val and val.casefold() not in evidence.casefold():
            val, conf = "", 0
        out[key] = {"value": val, "confidence": conf, "source": source if val else "not_found"}
    return out


def _merge(primary: Dict[str, Dict[str, Any]], secondary: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    result = {k: dict(primary.get(k) or _empty_field()) for k in FIELD_KEYS}
    for key in FIELD_KEYS:
        a, b = result[key], secondary.get(key) or _empty_field()
        if not a.get("value") and b.get("value"):
            result[key] = dict(b)
        elif b.get("value") and float(b.get("confidence", 0) or 0) > float(a.get("confidence", 0) or 0):
            result[key] = dict(b)
    return result


def _json_from_response(text: str) -> Dict[str, Any]:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip(), flags=re.I | re.S).strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        match = re.search(r"\{.*\}", text, flags=re.S)
        try:
            obj = json.loads(match.group(0)) if match else {}
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}


def _prompt(evidence: str, doc_type_hint: str) -> str:
    schema = ", ".join(f'"{k}": {{"value": "", "confidence": 0}}' for k in FIELD_KEYS)
    return f"""You are a production document-understanding engine for Indian land records.
Read the supplied evidence and extract fields exactly as printed. Never guess,
complete, translate, normalize, or invent a value. Preserve names, digits,
slashes, hyphens and local spelling. Handle Hindi/English mixed records and
common OCR label errors. If a field is absent, return an empty value and 0.
Return JSON only with exactly these keys: {{{schema}}}.
Document type: {doc_type_hint}
EVIDENCE:\n{evidence[:60000]}"""


def extract_fields_with_ai(ocr_text: str, doc_type_hint: str = "Land Record", deterministic: Optional[Dict[str, Dict[str, Any]]] = None) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    base = deterministic or {k: _empty_field() for k in FIELD_KEYS}
    label_fields = _label_fallback(ocr_text)
    base = _merge(base, label_fields)
    if not ocr_text.strip():
        return base, {"provider": "deterministic", "status": "no_ocr_text"}
    try:
        import server
        client = getattr(server, "ai_client", None)
        if client is None:
            return base, {"provider": "deterministic", "status": "ai_not_configured", "label_fallback": True}
        model = os.getenv("OCR_AI_MODEL", "gemini-2.5-flash")
        response = client.models.generate_content(model=model, contents=_prompt(ocr_text, doc_type_hint))
        ai_fields = _normalise_fields(_json_from_response(getattr(response, "text", "")), ocr_text)
        merged = _merge(base, ai_fields)
        found = sum(1 for v in merged.values() if v.get("value"))
        return merged, {"provider": "gemini", "model": model, "status": "ok", "fields_found": found, "deterministic_fallback": True, "label_fallback": True}
    except Exception as exc:
        return base, {"provider": "deterministic", "status": "ai_error", "error": type(exc).__name__, "ai_fallback": True, "label_fallback": True}


def extract_fields_from_image_with_vision(image, doc_type_hint: str = "Land Record") -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    empty = {k: _empty_field() for k in FIELD_KEYS}
    try:
        import server
        client = getattr(server, "ai_client", None)
        if client is None:
            return empty, {"provider": "vision", "status": "ai_not_configured"}
        model = os.getenv("OCR_VISION_MODEL", os.getenv("OCR_AI_MODEL", "gemini-2.5-flash"))
        response = client.models.generate_content(model=model, contents=[_prompt("IMAGE DOCUMENT — inspect the pixels directly", doc_type_hint), image])
        raw = _json_from_response(getattr(response, "text", ""))
        out = {}
        for key in FIELD_KEYS:
            val = raw.get(key)
            val = val.get("value") if isinstance(val, dict) else val
            try: conf = int(float(raw.get(key, {}).get("confidence", 0) if isinstance(raw.get(key), dict) else 0))
            except Exception: conf = 0
            out[key] = {"value": str(val or "").strip(), "confidence": max(0, min(100, conf)), "source": "vision+ai" if val else "not_found"}
        return out, {"provider": "gemini_vision", "model": model, "status": "ok"}
    except Exception as exc:
        return empty, {"provider": "vision", "status": "error", "error": type(exc).__name__}


def validate_fields(fields: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    required = ["owner_name", "survey_number", "khasra_number", "village", "district"]
    missing = [k for k in required if not (fields.get(k) or {}).get("value")]
    found = sum(1 for v in fields.values() if (v or {}).get("value"))
    return {"required_missing": missing, "field_count": len(fields), "found_count": found,
            "coverage_percent": round(found * 100 / max(1, len(fields)), 1), "valid": not missing}
