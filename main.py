"""Production ASGI entrypoint for the Document Screening application."""

import os
from fastapi.responses import FileResponse

from server import BASE_DIR, app

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
from demo_scenarios import demo_router
from backup_restore import backup_router
from land_intelligence import router as land_intelligence_router
from parcel_locator import router as parcel_locator_router

# The parcel locator is mounted before the legacy mapping router so its
# canonical RBAC-aware parcel endpoints are available without replacing the
# existing document-map endpoints.
app.include_router(parcel_locator_router)
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

@app.get("/land-intelligence", include_in_schema=False)
def land_intelligence_ui():
    return FileResponse(os.path.join(BASE_DIR, "land-intelligence.html"), media_type="text/html")
