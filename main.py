"""Production ASGI entrypoint for the Document Screening application."""
from server import app
from ai_governance import router as ai_approval_router
from demo_land import router as demo_land_router
from land_intelligence import router as land_intelligence_router
from land_intelligence_bridge import router as land_intelligence_bridge_router

# Keep the existing document-screening app as the source of truth and add the
# controlled parcel/property investigation workflow as explicit routers.
app.include_router(demo_land_router)
app.include_router(land_intelligence_router)
app.include_router(land_intelligence_bridge_router)
app.include_router(ai_approval_router)
