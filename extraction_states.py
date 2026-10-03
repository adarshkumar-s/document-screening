"""Explicit, evidence-first extraction states for the OCR/extraction pipeline.

Before this module the pipeline reported several overlapping signals
(``ocr_quality``, ``ai_decision_support.recommendation``, per-page
``engine_error``) but never a single, named outcome. A reviewer could not tell
"the engine is not installed" apart from "the scan was blank" apart from "we
read the page but found no survey number".

This module owns one pure function that maps evidence the pipeline has already
computed onto the six states the workflow needs:

    EXTRACTION_SUCCESS          required fields extracted with adequate confidence
    NEEDS_HUMAN_REVIEW          fields extracted but low confidence / validation issues
    TEXT_FOUND_FIELDS_MISSING   text was read but no required identity field came out
    NO_TEXT_DETECTED            the engine ran and produced no usable text
    OCR_ENGINE_UNAVAILABLE      no OCR engine could run at all
    AI_FALLBACK_FAILED          deterministic extraction was thin and the AI/vision
                                rescue was attempted and failed

Design rules enforced here:

* The function is pure — no database, no network, no OCR call — so every state
  transition is unit-testable.
* It never upgrades a state on its own. Absence of evidence yields a
  review/unknown state, never a success.
* Every result carries the evidence that decided it, so the UI can show *why*
  rather than just a badge.

Thresholds are configuration, not literals buried in branches.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Optional, Sequence

# ---------------------------------------------------------------------------
# Configurable thresholds.  Defaults deliberately match the conventions already
# used elsewhere in the application (validate_single_field treats confidence
# below 0.65 as a warning; the pipeline's own quality bands are 0.75 / 0.45).
# ---------------------------------------------------------------------------


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


# Mean field confidence at or above which extraction counts as successful.
MIN_SUCCESS_CONFIDENCE = _env_float("EXTRACTION_MIN_SUCCESS_CONFIDENCE", 0.65)
# Below this, extraction always requires a human even if fields were found.
MIN_REVIEW_CONFIDENCE = _env_float("EXTRACTION_MIN_REVIEW_CONFIDENCE", 0.40)
# Minimum number of required identity fields that must be non-empty for the
# transcript to count as having yielded fields.
MIN_REQUIRED_FIELDS = _env_int("EXTRACTION_MIN_REQUIRED_FIELDS", 2)
# A transcript shorter than this is treated as blank rather than sparse.
MIN_USABLE_TRANSCRIPT_CHARS = _env_int("EXTRACTION_MIN_TRANSCRIPT_CHARS", 20)

# The identity fields a land record must yield. Mirrors
# server.APPROVAL_REQUIRED_FIELDS so the extraction state and the approval gate
# can never disagree about what "required" means.
REQUIRED_IDENTITY_FIELDS: Sequence[str] = (
    "owner_name", "survey_number", "khasra_number", "village", "district",
)

# Canonical state names. Exposed so the UI, tests and any future persistence
# share one vocabulary instead of ad-hoc strings.
EXTRACTION_SUCCESS = "EXTRACTION_SUCCESS"
NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"
TEXT_FOUND_FIELDS_MISSING = "TEXT_FOUND_FIELDS_MISSING"
NO_TEXT_DETECTED = "NO_TEXT_DETECTED"
OCR_ENGINE_UNAVAILABLE = "OCR_ENGINE_UNAVAILABLE"
AI_FALLBACK_FAILED = "AI_FALLBACK_FAILED"

EXTRACTION_STATES: Sequence[str] = (
    EXTRACTION_SUCCESS,
    NEEDS_HUMAN_REVIEW,
    TEXT_FOUND_FIELDS_MISSING,
    NO_TEXT_DETECTED,
    OCR_ENGINE_UNAVAILABLE,
    AI_FALLBACK_FAILED,
)

# States a reviewer must act on before the record can progress.
REVIEW_REQUIRED_STATES = frozenset({
    NEEDS_HUMAN_REVIEW,
    TEXT_FOUND_FIELDS_MISSING,
    NO_TEXT_DETECTED,
    OCR_ENGINE_UNAVAILABLE,
    AI_FALLBACK_FAILED,
})


def _field_values(fields: Optional[Dict[str, Any]], keys: Iterable[str]) -> Dict[str, str]:
    """Non-empty values for the given field keys, tolerating both shapes."""
    out: Dict[str, str] = {}
    if not isinstance(fields, dict):
        return out
    for key in keys:
        entry = fields.get(key)
        if isinstance(entry, dict):
            value = str(entry.get("value") or "").strip()
        elif entry is None:
            value = ""
        else:
            value = str(entry).strip()
        if value:
            out[key] = value
    return out


def _field_confidences(fields: Optional[Dict[str, Any]]) -> List[float]:
    if not isinstance(fields, dict):
        return []
    confidences: List[float] = []
    for entry in fields.values():
        if not isinstance(entry, dict):
            continue
        if not str(entry.get("value") or "").strip():
            continue
        try:
            confidences.append(float(entry.get("confidence") or 0.0))
        except (TypeError, ValueError):
            continue
    return confidences


def _ai_failed(*metas: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return the first AI/vision stage that actually ran and errored.

    ``ai_not_configured`` is *not* a failure: no rescue was possible, so the
    correct state is driven by the deterministic result instead.
    """
    for meta in metas:
        if not isinstance(meta, dict):
            continue
        status = str(meta.get("status") or "").lower()
        if status in ("error", "ai_error", "failed"):
            return meta
    return None


def _ai_ran(*metas: Optional[Dict[str, Any]]) -> bool:
    for meta in metas:
        if isinstance(meta, dict) and str(meta.get("status") or "").lower() not in ("", "not_run", "not_needed"):
            return True
    return False


def classify_extraction_state(
    *,
    ocr_text: str = "",
    engine_error: Optional[str] = None,
    engine_available: Optional[bool] = None,
    fields: Optional[Dict[str, Any]] = None,
    validation: Optional[Dict[str, Any]] = None,
    ai_meta: Optional[Dict[str, Any]] = None,
    vision_meta: Optional[Dict[str, Any]] = None,
    page_methods: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Derive one explicit extraction state from evidence already collected.

    Pure and side-effect free. Returns::

        {
          "state": str,                    # one of EXTRACTION_STATES
          "requires_human_review": bool,
          "reason": str,                   # human explanation
          "recommended_action": str,       # what the reviewer should do next
          "evidence": {                    # the inputs that decided it
              "transcript_chars": int,
              "engine_error": str | None,
              "required_fields_found": [str],
              "required_fields_missing": [str],
              "mean_field_confidence": float | None,
              "validation_verdict": str | None,
              "ai_status": str | None,
              "vision_status": str | None,
          },
          "thresholds": {...},             # the configuration in force
        }
    """
    transcript = str(ocr_text or "")
    transcript_chars = len(transcript.strip())
    has_text = transcript_chars >= MIN_USABLE_TRANSCRIPT_CHARS

    found = _field_values(fields, REQUIRED_IDENTITY_FIELDS)
    missing = [k for k in REQUIRED_IDENTITY_FIELDS if k not in found]
    confidences = _field_confidences(fields)
    mean_confidence = round(sum(confidences) / len(confidences), 4) if confidences else None

    validation = validation if isinstance(validation, dict) else {}
    verdict = str(validation.get("verdict") or "").lower() or None
    invalid_count = 0
    try:
        invalid_count = int((validation.get("summary") or {}).get("invalid") or 0)
    except (TypeError, ValueError):
        invalid_count = 0

    failed_ai = _ai_failed(ai_meta, vision_meta)

    evidence = {
        "transcript_chars": transcript_chars,
        "engine_error": str(engine_error) if engine_error else None,
        "engine_available": engine_available,
        "required_fields_found": sorted(found),
        "required_fields_missing": missing,
        "required_field_count": len(found),
        "mean_field_confidence": mean_confidence,
        "validation_verdict": verdict,
        "invalid_field_count": invalid_count,
        "ai_status": (ai_meta or {}).get("status") if isinstance(ai_meta, dict) else None,
        "vision_status": (vision_meta or {}).get("status") if isinstance(vision_meta, dict) else None,
        "page_methods": list(page_methods or []),
    }
    thresholds = {
        "min_success_confidence": MIN_SUCCESS_CONFIDENCE,
        "min_review_confidence": MIN_REVIEW_CONFIDENCE,
        "min_required_fields": MIN_REQUIRED_FIELDS,
        "min_transcript_chars": MIN_USABLE_TRANSCRIPT_CHARS,
    }

    def result(state: str, reason: str, action: str) -> Dict[str, Any]:
        return {
            "state": state,
            "requires_human_review": state in REVIEW_REQUIRED_STATES,
            "reason": reason,
            "recommended_action": action,
            "evidence": evidence,
            "thresholds": thresholds,
        }

    # 1. No engine could run. Highest precedence: nothing else was observed.
    if engine_error and not has_text:
        if engine_available is False or not has_text:
            return result(
                OCR_ENGINE_UNAVAILABLE,
                "No OCR engine could process this file, so no text was read. "
                f"Engine error: {engine_error}",
                "Install the OCR engine (Tesseract plus language packs) or set "
                "TESSERACT_CMD, then re-process this document.",
            )

    # 2. The engine ran but produced no usable text.
    if not has_text:
        if failed_ai is not None:
            return result(
                AI_FALLBACK_FAILED,
                "OCR produced no usable text and the AI/vision rescue also "
                f"failed ({failed_ai.get('error') or failed_ai.get('status')}).",
                "Open the scan manually: rotate or re-scan the page, or type "
                "the fields from the original document.",
            )
        return result(
            NO_TEXT_DETECTED,
            "The OCR engine ran but produced no usable text on any page.",
            "Check the scan quality and orientation, re-scan if needed, or "
            "confirm the document is genuinely blank.",
        )

    # 3. Text exists but no required identity field came out of it.
    if len(found) < MIN_REQUIRED_FIELDS:
        if failed_ai is not None:
            return result(
                AI_FALLBACK_FAILED,
                "Text was read but the required identity fields were not "
                "extracted, and the AI/vision fallback failed "
                f"({failed_ai.get('error') or failed_ai.get('status')}).",
                "Review the raw OCR text and enter the missing identifiers "
                "manually.",
            )
        return result(
            TEXT_FOUND_FIELDS_MISSING,
            "Text was read from the document but fewer than "
            f"{MIN_REQUIRED_FIELDS} required identity fields were extracted "
            f"(missing: {', '.join(missing) or 'none'}).",
            "Open the raw OCR text beside the fields and supply the missing "
            "survey/khasra, village, district or owner values.",
        )

    # 4. Fields exist. Decide between success and human review.
    low_confidence = mean_confidence is not None and mean_confidence < MIN_SUCCESS_CONFIDENCE
    has_invalid = verdict == "rejected" or invalid_count > 0

    if has_invalid:
        return result(
            NEEDS_HUMAN_REVIEW,
            "Required fields were extracted but at least one value failed its "
            "validation rule.",
            "Correct the invalid values before approving, or reject the record.",
        )

    if low_confidence or verdict == "review":
        detail = []
        if low_confidence:
            detail.append(f"mean field confidence {mean_confidence} is below {MIN_SUCCESS_CONFIDENCE}")
        if verdict == "review":
            detail.append("validation reports unresolved warnings")
        return result(
            NEEDS_HUMAN_REVIEW,
            "Required fields were extracted but need confirmation: "
            + "; ".join(detail) + ".",
            "Verify the extracted values against the scan before approving.",
        )

    return result(
        EXTRACTION_SUCCESS,
        f"{len(found)} required identity fields extracted with mean confidence "
        f"{mean_confidence} and no validation errors.",
        "Proceed with parcel matching and verification.",
    )


__all__ = [
    "classify_extraction_state",
    "EXTRACTION_STATES",
    "EXTRACTION_SUCCESS",
    "NEEDS_HUMAN_REVIEW",
    "TEXT_FOUND_FIELDS_MISSING",
    "NO_TEXT_DETECTED",
    "OCR_ENGINE_UNAVAILABLE",
    "AI_FALLBACK_FAILED",
    "REVIEW_REQUIRED_STATES",
    "REQUIRED_IDENTITY_FIELDS",
]
