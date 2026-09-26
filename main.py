"""Production ASGI entrypoint for the Document Screening application."""

import os
from fastapi.responses import FileResponse

from server import BASE_DIR, app

# Install narrow production hotfixes before any request can reach the upload
# pipeline. This explicitly wires the fast OCR implementation and removes the
# duplicate logged-in utility logo; it does not replace the DB, routes, queue,
# mapping system, or Land Intelligence architecture.
import runtime_patch
runtime_patch.apply()

# Install the document-processing bridge before any upload request can resolve
# server.run_ocr_pipeline. This preserves the existing routes while allowing
# text PDFs to bypass expensive raster OCR and ensuring OCR text can backfill
# land identifiers needed by Land Intelligence matching.
import ocr_land_bridge
ocr_land_bridge.install()

from ai_governance import router as ai_approval_router
from mapping import document_history_router, map_router
from land_intel import encumbrance_router, land_router, mutation_router, report_router
from court_cases import router as court_cases_router
from demo_scenarios import demo_router, seed_all
from backup_restore import backup_router
from land_intelligence import router as land_intelligence_router
from parcel_locator import map_router as parcel_map_router, router as parcel_locator_router

# The parcel locator is mounted before the legacy mapping router so its
# canonical RBAC-aware parcel endpoints are available without replacing the
# existing document-map endpoints.
app.include_router(parcel_locator_router)
app.include_router(parcel_map_router)
app.include_router(map_router)
app.include_router(document_history_router)
app.include_router(ai_approval_router)
app.include_router(encumbrance_router)
app.include_router(mutation_router)
app.include_router(land_router)
app.include_router(report_router)
app.include_router(court_cases_router)
app.include_router(demo_router)
app.include_router(backup_router)
app.include_router(land_intelligence_router)


@app.on_event("startup")
def seed_demo_on_startup_when_explicitly_enabled():
    """One-shot, idempotent demo seeding for a controlled deployment.

    This is intentionally opt-in and does not expose a production API bypass.
    The Render environment flag is removed immediately after the seed deploy.
    The existing seeder is insert-if-absent and only creates DEMO-* artifacts.
    """
    if os.getenv("SEED_DEMO_LI_ON_STARTUP", "").strip().lower() != "true":
        return
    result = seed_all(dry_run=False)
    created = result.get("created", {})
    print(f"[DEMO SEED] startup seed complete: {created}")


@app.get("/land-intelligence", include_in_schema=False)
def land_intelligence_ui():
    return FileResponse(os.path.join(BASE_DIR, "land-intelligence.html"), media_type="text/html")