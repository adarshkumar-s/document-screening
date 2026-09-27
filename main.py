"""Production ASGI entrypoint for the Document Screening application."""

import os
from fastapi.responses import FileResponse

from server import BASE_DIR, app

# Install narrow production hotfixes before any request can reach the upload
# pipeline. This explicitly wires the high-recall OCR implementation and the
# document bridge before FastAPI serves requests.
import runtime_patch
runtime_patch.apply()

import ocr_runtime_fix
ocr_runtime_fix.install()

# Last resort: only after all structured OCR variants fail, use the slower
# text-rendering path. This deliberately trades latency for extraction recall.
import ocr_last_resort
ocr_last_resort.install()

# Final production rescue: retry orientation and, when OCR coverage is weak,
# inspect the actual document pixels with the configured multimodal model.
# This is intentionally slower only for difficult scans.
import ocr_pro_rescue
ocr_pro_rescue.install()

import ocr_land_bridge
ocr_land_bridge.install()

# QR-certified copies and public verification are installed after the DB has
# been initialized by server.py. The migration is additive and safe for old
# records; only verified/approved records can receive a certified copy.
import certification
certification.install()
app.include_router(certification.router)

from ai_governance import router as ai_approval_router
from mapping import document_history_router, map_router
from land_intel import encumbrance_router, land_router, mutation_router, report_router
from court_cases import router as court_cases_router
from demo_scenarios import demo_router, seed_all
from backup_restore import backup_router
from land_intelligence import router as land_intelligence_router
from parcel_locator import map_router as parcel_map_router, router as parcel_locator_router

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
    if os.getenv("SEED_DEMO_LI_ON_STARTUP", "").strip().lower() != "true":
        return
    result = seed_all(dry_run=False)
    created = result.get("created", {})
    print(f"[DEMO SEED] startup seed complete: {created}")


@app.get("/land-intelligence", include_in_schema=False)
def land_intelligence_ui():
    return FileResponse(os.path.join(BASE_DIR, "land-intelligence.html"), media_type="text/html")
