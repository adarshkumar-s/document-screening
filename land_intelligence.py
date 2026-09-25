"""Resilient OCR + parcel intelligence extensions.

This module builds on the existing deterministic land-intelligence layer instead
of replacing it. It adds:
- bounded OCR rescue using image variants and corroboration;
- field-level confidence/corroboration diagnostics;
- script-aware owner comparison without silently merging identities;
- parcel-level evidence aggregation across documents, encumbrances, mutations,
  and litigation;
- evidence-linked review signals suitable for the operator UI.

All findings are review signals, never legal conclusions. Existing RBAC and the
AI approval/CAS path remain authoritative.
"""
from __future__ import annotations

import difflib
import io
import os
import re
import time
import unicodedata
from collections import Counter
from typing import Any, Dict, List, Optional, Sequence, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import server
from server import ROLE_ADMIN, ROLE_DATA_OFFICER, ROLE_VERIFICATION_OFFICER, get_db, require_roles

router = APIRouter(prefix="/api/land-intelligence", tags=["Land Intelligence"])
REVIEW_ROLES = (ROLE_VERIFICATION_OFFICER, ROLE_ADMIN)
KEY_FIELDS = ("owner_name", "survey_number", "khasra_number", "village", "area")
MAX_RESCUE_PASSES = 3


def _value(field: Any) -> str:
    if isinstance(field, dict):
        return str(field.get("value") or "").strip()
    return str(field or "").strip()


def _conf(field: Any) -> float:
    if isinstance(field, dict):
        try:
            return max(0.0, min(1.0, float(field.get("confidence") or 0.0)))
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _norm(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    digit_map = str.maketrans("०१२३४५६७८९٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "012345678901234567890123456789")
    text = text.translate(digit_map).casefold()
    return re.sub(r"\s+", " ", text).strip()


def _owner_key(value: Any) -> str:
    """Conservative script-aware key; transliteration is intentionally limited."""
    text = _norm(value)
    if not text:
        return ""
    # Reuse the existing land_intel transliteration/matching when available.
    try:
        from land_intel import _owner_key as canonical_owner_key
        return canonical_owner_key(value)
    except Exception:
        return text


def owner_match(a: Any, b: Any) -> Dict[str, Any]:
    ka, kb = _owner_key(a), _owner_key(b)
    if not ka or not kb:
        return {"match": False, "confidence": 0.0, "method": "INSUFFICIENT_DATA", "requires_review": True}
    if ka == kb:
        return {"match": True, "confidence": 1.0, "method": "NORMALIZED_OR_TRANSLITERATED_EXACT", "requires_review": False}
    ratio = difflib.SequenceMatcher(None, ka, kb).ratio()
    return {
        "match": ratio >= 0.82,
        "confidence": round(ratio, 3),
        "method": "FUZZY_SCRIPT_AWARE",
        "requires_review": ratio >= 0.82,
        "warning": "Possible same person; do not merge identities automatically." if ratio >= 0.82 else None,
    }


def assess_ocr(ocr: Dict[str, Any], fields: Dict[str, Any]) -> Dict[str, Any]:
    text = str(ocr.get("text") or ocr.get("full_text") or "")
    words = int(ocr.get("word_count") or len(text.split()))
    mean_conf = float(ocr.get("confidence") or ocr.get("mean_conf") or 0.0)
    if mean_conf > 1.0:
        mean_conf /= 100.0
    filled = sum(1 for value in fields.values() if _value(value))
    key_filled = sum(1 for key in KEY_FIELDS if _value(fields.get(key)))
    missing = [key for key in KEY_FIELDS if not _value(fields.get(key))]
    diagnosis: List[str] = []
    if not words:
        diagnosis.append("No text detected; inspect page orientation, scan quality, or OCR language support.")
    elif words < 10:
        diagnosis.append(f"Only {words} words detected; the page may be cropped, blank, or low contrast.")
    if words >= 10 and key_filled == 0:
        diagnosis.append("Text exists but no key land-record fields were extracted; layout or script may need a rescue pass.")
    if mean_conf < 0.55:
        diagnosis.append(f"OCR confidence is low ({mean_conf:.0%}); extracted values require verification.")
    if missing:
        diagnosis.append("Missing key fields: " + ", ".join(missing) + ".")
    garbage = words >= 5 and 0 < mean_conf < 0.35
    status = "GOOD" if key_filled >= 3 and mean_conf >= 0.60 and not garbage else ("UNREADABLE" if garbage or key_filled == 0 or words < 5 else "UNCERTAIN")
    return {
        "status": status,
        "words": words,
        "mean_confidence": round(mean_conf, 3),
        "fields_filled": filled,
        "key_fields_filled": key_filled,
        "missing_key_fields": missing,
        "diagnosis": diagnosis or ["OCR quality is sufficient for structured review."],
    }


def corroborate_field_passes(pass_fields: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    keys = {key for item in pass_fields for key in item.keys()}
    for key in sorted(keys):
        candidates = []
        for item in pass_fields:
            value = _value(item.get(key))
            if value:
                candidates.append((_norm(value), value, _conf(item.get(key))))
        if not candidates:
            result[key] = {"value": "", "confidence": 0.0, "corroboration": 0, "passes": 0}
            continue
        counts = Counter(item[0] for item in candidates)
        winner_norm, count = counts.most_common(1)[0]
        winner = next(item for item in candidates if item[0] == winner_norm)
        confidence = max(item[2] for item in candidates if item[0] == winner_norm)
        if count >= 2:
            confidence = min(1.0, confidence + 0.08)
        result[key] = {
            "value": winner[1],
            "confidence": round(confidence, 3),
            "corroboration": count,
            "passes": len(candidates),
            "independent_agreement": count >= 2,
        }
    return result


def enhanced_image_variants(image: Any) -> List[Tuple[str, Any]]:
    try:
        from PIL import ImageEnhance, ImageOps
    except Exception:
        return []
    variants: List[Tuple[str, Any]] = []
    try:
        base = image.convert("RGB")
        variants.append(("grayscale_autocontrast", ImageOps.autocontrast(base.convert("L"), cutoff=1)))
        if max(base.size) >= 40:
            big = base.resize((base.width * 2, base.height * 2))
            sharp = ImageEnhance.Sharpness(ImageOps.autocontrast(big.convert("L"), cutoff=1)).enhance(2.0)
            variants.append(("upscale_2x_sharpen", sharp))
    except Exception:
        return []
    return variants


def _merge_result(original: Dict[str, Any], candidates: Sequence[Dict[str, Any]], labels: Sequence[str]) -> Dict[str, Any]:
    fields = [original.get("fields") or *candidates]
    merged = corroborate_field_passes(fields)
    base = dict(original)
    base["fields"] = merged
    quality = assess_ocr(original, merged)
    meta = dict(base.get("pipeline_meta") or {})
    meta["rescue"] = {
        "attempted": len(candidates),
        "passes": list(labels),
        "before": assess_ocr(original, original.get("fields") or {}),
        "after": quality,
        "corroboration": {k: v.get("corroboration", 0) for k, v in merged.items()},
    }
    base["pipeline_meta"] = meta
    support = dict(base.get("ai_decision_support") or {})
    support["ocr_rescue"] = meta["rescue"]
    base["ai_decision_support"] = support
    base["escalated"] = quality["status"] != "GOOD"
    return base


# Install a bounded wrapper around the existing pipeline. The wrapper is only
# used by main:app (Docker's production entrypoint); direct server imports keep
# the canonical function unchanged for tests that explicitly exercise it.
_original_pipeline = getattr(server, "run_ai_assisted_pipeline", None)


async def resilient_run_ai_assisted_pipeline(image: Any, requested_lang: str = "auto") -> Dict[str, Any]:
    if _original_pipeline is None:
        raise RuntimeError("Canonical OCR pipeline is unavailable")
    first = await _original_pipeline(image, requested_lang)
    before = assess_ocr(first, first.get("fields") or {})
    if before["status"] == "GOOD":
        return first
    candidates: List[Dict[str, Any]] = []
    labels: List[str] = []
    for label, variant in enhanced_image_variants(image)[:MAX_RESCUE_PASSES]:
        try:
            result = await _original_pipeline(variant, requested_lang)
        except Exception:
            continue
        candidates.append(result)
        labels.append(label)
    if not candidates:
        return first
    merged = _merge_result(first, candidates, labels)
    # Preserve the strongest full OCR payload instead of replacing it with a
    # field-only synthetic result. Pick the pass with the most key fields, then
    # confidence, while the merged fields remain the final structured values.
    ranked = [first, *candidates]
    best = max(ranked, key=lambda item: (
        sum(1 for key in KEY_FIELDS if _value((item.get("fields") or {}).get(key))),
        float(item.get("mean_conf") or 0),
    ))
    merged["ocr_text"] = best.get("ocr_text", merged.get("ocr_text", ""))
    merged["cleaned_ocr_text"] = best.get("cleaned_ocr_text", merged.get("cleaned_ocr_text", ""))
    merged["mean_conf"] = int(round(assess_ocr(best, merged["fields"])["mean_confidence"] * 100))
    merged["detected_language"] = best.get("detected_language", merged.get("detected_language", "eng"))
    return merged


if _original_pipeline is not None:
    server.run_ai_assisted_pipeline = resilient_run_ai_assisted_pipeline


class OCRDiagnosisReq(BaseModel):
    ocr: Dict[str, Any] = Field(default_factory=dict)
    fields: Dict[str, Any] = Field(default_factory=dict)


class OCRCorroborationReq(BaseModel):
    passes: List[Dict[str, Any]] = Field(default_factory=list, min_length=1, max_length=8)


class OwnerMatchReq(BaseModel):
    left: str
    right: str


def _document_rows_for_land(survey: str, village: str) -> List[Dict[str, Any]]:
    survey_n, village_n = _norm(survey), _norm(village)
    with get_db() as db:
        rows = db.execute("SELECT id, filename, status, fields, created_at FROM documents ORDER BY created_at ASC LIMIT 10000").fetchall()
    out = []
    for row in rows:
        import json
        fields = json.loads(row["fields"] or "{}")
        row_survey = _norm(_value(fields.get("survey_number")) or _value(fields.get("gat_number")) or _value(fields.get("khasra_number")))
        row_village = _norm(_value(fields.get("village")))
        if row_survey != survey_n or (village_n and row_village and row_village != village_n):
            continue
        out.append({"id": row["id"], "filename": row["filename"], "status": row["status"], "fields": fields, "created_at": row["created_at"]})
    return out


def parcel_intelligence(survey: str, village: str = "") -> Dict[str, Any]:
    if not survey.strip():
        raise HTTPException(400, "survey is required")
    documents = _document_rows_for_land(survey, village)
    evidence: List[Dict[str, Any]] = []
    flags: List[Dict[str, Any]] = []
    owners = []
    areas = []
    for doc in documents:
        fields = doc["fields"]
        owner = _value(fields.get("owner_name"))
        if owner:
            owners.append((owner, doc["id"]))
        area = _value(fields.get("area"))
        if area:
            try:
                numeric = float(re.search(r"\d+(?:\.\d+)?", area.replace(",", "")).group())
                areas.append((numeric, doc["id"]))
            except Exception:
                pass
        evidence.append({"type": "DOCUMENT", "id": doc["id"], "label": doc["filename"], "status": doc["status"]})

    try:
        from land_intel import list_encumbrances, calculate_land_risk
        encumbrances = list_encumbrances(survey, village)
        # calculate_land_risk has changed shape across historical versions;
        # use it when its public callable is available and fall back safely.
        try:
            risk = calculate_land_risk({"survey": survey, "village": village, "documents": documents, "encumbrances": encumbrances})
        except Exception:
            risk = None
    except Exception:
        encumbrances, risk = [], None

    try:
        from court_cases import active_cases, list_cases, litigation_verdict_for
        cases = list_cases(survey, village)
        litigation = litigation_verdict_for(cases)
    except Exception:
        cases, litigation = [], "UNKNOWN"
    try:
        from land_intel import list_mutations
        mutations = list_mutations(survey, village)
    except Exception:
        mutations = []

    for enc in encumbrances:
        if str(enc.get("status") or "").upper() == "ACTIVE":
            item = {"type": "ENCUMBRANCE", "id": enc.get("id"), "label": enc.get("lender") or "Active encumbrance", "severity": "HIGH"}
            flags.append({"code": "ACTIVE_ENCUMBRANCE", "severity": "HIGH", "title": "Active encumbrance", "reason": "An active encumbrance is registered for this parcel.", "evidence": [item]})
            evidence.append(item)
    for case in cases:
        item = {"type": "COURT_CASE", "id": case.get("id"), "label": case.get("case_number"), "severity": "HIGH" if str(case.get("status")).upper() == "ACTIVE" else "INFO"}
        evidence.append(item)
        if item["severity"] == "HIGH":
            flags.append({"code": "ACTIVE_LITIGATION", "severity": "HIGH", "title": "Active litigation", "reason": "An active registered case is linked to this survey/village.", "evidence": [item]})

    if len(owners) >= 2:
        latest_owner, latest_doc = owners[-1]
        for owner, doc_id in owners[:-1]:
            match = owner_match(owner, latest_owner)
            if not match["match"]:
                flags.append({"code": "OWNER_CHANGE", "severity": "MEDIUM", "title": "Recorded owner change", "reason": "Chronological records contain different owner names; verify the supporting mutation/transfer evidence.", "evidence": [{"type": "DOCUMENT", "id": doc_id}, {"type": "DOCUMENT", "id": latest_doc}]})
                break

    if len(areas) >= 2 and areas[0][0] > 0:
        ratio = abs(areas[-1][0] - areas[0][0]) / areas[0][0]
        if ratio >= 0.15:
            flags.append({"code": "AREA_CHANGE", "severity": "MEDIUM", "title": "Material area change", "reason": f"Recorded area changed by {ratio:.0%}; check partition, merger, correction, or measurement evidence.", "evidence": [{"type": "DOCUMENT", "id": areas[0][1]}, {"type": "DOCUMENT", "id": areas[-1][1]}]})

    if litigation == "ACTIVE_LITIGATION":
        verdict = "HIGH_RISK"
    elif any(flag["severity"] == "HIGH" for flag in flags):
        verdict = "HIGH_RISK"
    elif flags:
        verdict = "REVIEW"
    else:
        verdict = "CLEAR"

    return {
        "land": {"survey": survey, "village": village},
        "verdict": verdict,
        "documents": len(documents),
        "encumbrances": encumbrances,
        "mutations": mutations,
        "litigation": {"verdict": litigation, "cases": cases},
        "flags": flags,
        "evidence": evidence,
        "risk_engine": risk,
        "owners": [{"name": owner, "document_id": doc_id} for owner, doc_id in owners],
        "disclaimer": "Review signals are evidence summaries only and do not establish legal title, fraud, or final legal risk.",
    }


@router.post("/ocr/diagnose")
def diagnose_ocr(req: OCRDiagnosisReq, user: Dict[str, Any] = Depends(require_roles(*REVIEW_ROLES))):
    return assess_ocr(req.ocr, req.fields)


@router.post("/ocr/corroborate")
def corroborate_ocr(req: OCRCorroborationReq, user: Dict[str, Any] = Depends(require_roles(*REVIEW_ROLES))):
    return {"fields": corroborate_field_passes(req.passes), "passes": len(req.passes), "method": "MULTI_PASS_CORROBORATION"}


@router.post("/owner-match")
def compare_owners(req: OwnerMatchReq, user: Dict[str, Any] = Depends(require_roles(*REVIEW_ROLES))):
    return owner_match(req.left, req.right)


@router.get("/parcel")
def get_parcel_intelligence(survey: str = Query(..., max_length=120), village: str = Query("", max_length=200), user: Dict[str, Any] = Depends(require_roles(*REVIEW_ROLES))):
    return parcel_intelligence(survey, village)
