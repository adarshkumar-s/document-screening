"""Production ASGI entrypoint for the Document Screening application."""

from server import BASE_DIR, app
from ai_governance import router as ai_approval_router
from demo_land import router as demo_land_router
from land_intelligence import router as land_intelligence_router

# Keep the document-screening application as the source of truth and register
# only the replacement Land Intelligence workflow here.  OCR, AI governance,
# authentication, and audit routes remain owned by server.app.
app.include_router(demo_land_router)
app.include_router(land_intelligence_router)
app.include_router(ai_approval_router)
