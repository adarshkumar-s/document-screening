#!/usr/bin/env python3
"""Deterministic demo-data loader for the Land Records / Document Screening app.

This is the command-line front for the SAME seeding service the administration
panel uses (``demo_scenarios``), so the portal and the CLI can never disagree
about what the demo dataset contains.

Usage
-----
    python scripts/seed_demo_data.py --check-only     # read-only: report the plan
    python scripts/seed_demo_data.py --yes            # write the demo dataset
    python scripts/seed_demo_data.py --clear --yes    # remove demo rows only
    python scripts/seed_demo_data.py --json           # machine-readable output

Safety
------
* Writes are refused unless ``--yes`` is given.
* Writes are refused outright when ``APP_ENV`` marks the instance as production.
  There is deliberately no override flag for that.
* Only demo-tagged rows are ever touched (``metadata.demo`` on documents, the
  ``DEMO-`` id prefix on the mutation / encumbrance / litigation registers), and
  seeding is insert-if-absent, so real records are neither read-modified nor
  overwritten and existing demo rows are never clobbered.
* Every write is written to the application's own audit trail.

Exit codes
----------
0  success (or nothing to do)
1  --check-only found the dataset incomplete
2  refused: production environment
3  refused: confirmation (--yes) missing
4  error (database unavailable, bad arguments, ...)
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

KINDS = ("land_records", "documents", "mutations", "encumbrances", "court_cases")
LABELS = {
    "land_records": "land records",
    "documents": "synthetic documents",
    "mutations": "mutations",
    "encumbrances": "encumbrances",
    "court_cases": "court cases",
}


def _dataset_lines(counts: dict) -> str:
    return "\n".join(f"    {counts.get(kind, 0):>4}  {LABELS[kind]}" for kind in KINDS)


def _removal_lines(removed: dict) -> str:
    labels = {"documents": "demo documents", "mutations": "demo mutations",
              "encumbrances": "demo encumbrances", "court_cases": "demo court cases",
              "events": "mutation audit events"}
    return "\n".join(f"    {removed.get(kind, 0):>4}  {label}" for kind, label in labels.items())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seed_demo_data.py",
        description="Seed or clear the deterministic demo dataset (S1-S16).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--check-only", action="store_true",
                        help="Report what the dataset contains and what is missing. Never writes.")
    parser.add_argument("--yes", action="store_true", help="Required for any write (seed or clear).")
    parser.add_argument("--clear", action="store_true", help="Remove demo-tagged rows only (requires --yes).")
    parser.add_argument("--scenario", default="all",
                        help="Report on one scenario id (e.g. S11). Seeding is always the whole dataset.")
    parser.add_argument("--json", dest="as_json", action="store_true", help="Emit a JSON summary instead of text.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    import server  # noqa: F401  (import first: owns DB path, audit trail and environment)
    import demo_scenarios as demo
    from land_intel import _audit

    db_path = getattr(server, "SQLITE_PATH", "configured database")
    app_env = os.getenv("APP_ENV", "development")
    blocked = demo.production_block_reason()

    def emit(payload: dict, text: str, code: int) -> int:
        print(json.dumps(payload, indent=2, sort_keys=True) if args.as_json else text)
        return code

    try:
        preview = demo.preview()
    except Exception as exc:  # pragma: no cover - environment problem
        print(f"error: cannot read the database at {db_path}: {exc}", file=sys.stderr)
        return 4

    base = {
        "database": str(db_path),
        "app_env": app_env,
        "production_blocked": blocked is not None,
        "dataset": preview["dataset"],
        "present": preview["present"],
        "would_create": preview["would_create"],
        "already_present": preview["already_present"],
        "complete": preview["complete"],
    }

    # ---- production guard applies to every write, and cannot be overridden --
    if blocked and (args.yes or args.clear):
        return emit({**base, "status": "refused", "reason": blocked},
                    f"REFUSED — {blocked}\nNothing was written.", 2)

    # ---- read-only report ---------------------------------------------------
    if args.check_only or not args.yes:
        header = "DEMO DATA CHECK" if args.check_only else "DEMO DATA — DRY RUN (no --yes, nothing written)"
        lines = [
            f"{header}",
            f"  database : {db_path}",
            f"  APP_ENV  : {app_env}",
            f"  scenarios: {preview['scenario_count']} (S1-S{preview['scenario_count']})",
            "",
            "  Dataset the loader guarantees:",
            _dataset_lines(preview["dataset"]),
            "",
            "  Present in this database now:",
            _dataset_lines(preview["present"]),
            "",
            f"  Would create : {preview['would_create']} row(s)",
            f"  Already there : {preview['already_present']} row(s)",
        ]
        if args.check_only:
            lines.append(f"\n  Verdict: {'COMPLETE — no seeding needed' if preview['complete'] else 'INCOMPLETE — run: python scripts/seed_demo_data.py --yes'}")
            return emit({**base, "status": "complete" if preview["complete"] else "incomplete"},
                        "\n".join(lines), 0 if preview["complete"] else 1)
        return emit({**base, "status": "dry-run"}, "\n".join(lines), 0 if preview["would_create"] == 0 else 3)

    # ---- clear: demo rows only ---------------------------------------------
    if args.clear:
        removable = demo.current_demo_counts()
        if args.check_only:  # pragma: no cover - guarded above, kept for clarity
            return emit({**base, "status": "dry-run", "would_remove": removable},
                        f"Would remove demo rows only:\n{_dataset_lines(removable)}", 0)
        result = demo.clear_demo_dataset(actor=f"cli:{os.getenv('USER', 'seed_demo_data')}")
        removed = result["removed"]
        remaining = demo.current_demo_counts()
        payload = {**base, "status": "cleared", "removed": removed, "remaining_demo": remaining,
                   "real_data_untouched": True}
        text = ("DEMO DATA REMOVED\n"
                + _removal_lines(removed)
                + f"\n    demo rows remaining: {sum(remaining[kind] for kind in KINDS if kind in remaining)}"
                + "\n    Real records were not touched."
                + "\n    Audit event: DEMO_DATA_CLEARED")
        return emit(payload, text, 0)

    # ---- seed ---------------------------------------------------------------
    if args.scenario and args.scenario.strip().upper() not in {"ALL", ""}:
        wanted = args.scenario.strip().upper()
        known = {item["id"] for item in demo.SCENARIOS}
        if wanted not in known:
            print(f"error: unknown scenario '{args.scenario}'. Known: {', '.join(sorted(known))}", file=sys.stderr)
            return 4
        print(f"note: the demo dataset is all-or-nothing; '{wanted}' is seeded as part of S1-S16.")

    result = demo.seed_all()
    created, skipped = result["created"], result["skipped_artifacts"]
    skipped_total = sum(len(ids) for ids in skipped.values())
    _audit({"email": f"cli:{os.getenv('USER', 'seed_demo_data')}", "full_name": "seed_demo_data.py"},
           "DEMO_DATA_SEEDED",
           f"CLI demo seed: created {created['total']}, skipped {skipped_total} "
           f"(dataset: {result['dataset']['total']} demo rows across {result['dataset']['land_records']} parcels)")
    second_look = demo.current_demo_counts()
    payload = {**base, "status": "seeded", "created": created, "skipped": skipped_total,
               "present_after": second_look, "scenario": args.scenario}
    text = ("DEMO DATA LOADED\n"
            + _dataset_lines(created)
            + f"\n    skipped (already present): {skipped_total}"
            + f"\n    demo rows now in database : {sum(second_look.get(kind, 0) for kind in KINDS if kind != 'land_records')}"
            + "\n    Real records were not touched."
            + "\n    Audit event: DEMO_DATA_SEEDED")
    if created["total"] == 0:
        text = "DEMO DATA ALREADY LOADED — nothing created (idempotent).\n" + _dataset_lines(second_look)
    return emit(payload, text, 0)


if __name__ == "__main__":
    raise SystemExit(main())
