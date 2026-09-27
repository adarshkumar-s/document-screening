"""Safe guard for optional synthetic DEMO-LI parcel backfill.

Production should never fail startup because a demo-only geometry helper hit a
legacy schema/row-shape mismatch. The real land-record and OCR paths are not
changed by this guard.
"""
from __future__ import annotations

import os


def install(runtime_patch) -> None:
    if getattr(runtime_patch, "_safe_backfill_guard_installed", False):
        return

    original = getattr(runtime_patch, "_backfill_demo_parcels", None)

    def safe_backfill():
        # Synthetic parcel geometry is an optional demo feature. Keep it off in
        # production unless explicitly requested; this removes a source of
        # startup-only failures from normal deployments.
        if os.getenv("ENABLE_DEMO_PARCEL_BACKFILL", "").strip().lower() != "true":
            return
        if original is None:
            return
        try:
            original()
        except Exception as exc:
            # Demo geometry must never prevent the application from starting.
            print(f"[RUNTIME PATCH] optional DEMO-LI parcel backfill skipped: {type(exc).__name__}")

    runtime_patch._backfill_demo_parcels = safe_backfill
    runtime_patch._safe_backfill_guard_installed = True
