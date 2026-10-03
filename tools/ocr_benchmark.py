#!/usr/bin/env python3
"""Evaluate land-record OCR/extraction output against a reviewed golden dataset.

Input is JSONL, one object per document:
{"document_id":"sample-001","expected":{"survey_number":"12/3","village":"Rampur"},
 "predicted":{"survey_number":"12/3","village":"Rampur"}}

Keep real personal data out of benchmark fixtures. This tool reports extraction
coverage separately from correctness so missing fields cannot look like success.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any


def normalize(value: Any) -> str:
    """Normalize text conservatively; retain letters/digits and Indic scripts."""
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).casefold().strip()
    text = re.sub(r"\s+", " ", text)
    return text


def levenshtein(a: str, b: str) -> int:
    """Memory-efficient Levenshtein distance."""
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(current[-1] + 1, previous[j] + 1,
                               previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def similarity(expected: str, predicted: str) -> float:
    if not expected and not predicted:
        return 1.0
    if not expected or not predicted:
        return 0.0
    return max(0.0, 1.0 - levenshtein(expected, predicted) / max(len(expected), len(predicted)))


def evaluate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = present = exact = 0
    per_field: dict[str, dict[str, int]] = defaultdict(lambda: {
        "expected": 0, "predicted": 0, "exact": 0
    })
    similarity_sum = 0.0
    compared = 0
    errors: list[dict[str, str]] = []

    for row_number, row in enumerate(rows, 1):
        expected = row.get("expected")
        predicted = row.get("predicted")
        if not isinstance(expected, dict) or not isinstance(predicted, dict):
            raise ValueError(f"line {row_number}: expected and predicted must be objects")
        for field, raw_expected in expected.items():
            exp = normalize(raw_expected)
            pred = normalize(predicted.get(field))
            if not exp:
                continue
            total += 1
            per_field[field]["expected"] += 1
            if pred:
                present += 1
                per_field[field]["predicted"] += 1
            if exp == pred:
                exact += 1
                per_field[field]["exact"] += 1
            similarity_sum += similarity(exp, pred)
            compared += 1
            if exp != pred:
                errors.append({
                    "document_id": str(row.get("document_id", row_number)),
                    "field": str(field),
                    "expected": str(raw_expected),
                    "predicted": str(predicted.get(field, "")),
                })

    return {
        "documents": len(rows),
        "expected_field_values": total,
        "field_coverage": present / total if total else 0.0,
        "exact_match_accuracy": exact / total if total else 0.0,
        "mean_character_similarity": similarity_sum / compared if compared else 0.0,
        "per_field": {
            field: {
                **counts,
                "coverage": counts["predicted"] / counts["expected"] if counts["expected"] else 0.0,
                "exact_match_accuracy": counts["exact"] / counts["expected"] if counts["expected"] else 0.0,
            }
            for field, counts in sorted(per_field.items())
        },
        "mismatches": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jsonl", type=Path, help="Reviewed JSONL golden set and predictions")
    parser.add_argument("--no-mismatches", action="store_true",
                        help="omit raw field values from output (safer for shared reports)")
    args = parser.parse_args()
    try:
        rows = []
        with args.jsonl.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"line {line_number}: invalid JSON: {exc}") from exc
        report = evaluate(rows)
        if args.no_mismatches:
            report.pop("mismatches", None)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(f"ocr_benchmark: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
