"""Compatibility ASGI entrypoint for Document Screening."""

from ai_governance import router as ai_approval_router
from demo_land import router as demo_land_router
from land_intelligence import (
    document_history_router,
    map_router,
    router as land_intelligence_router,
)
from server import app

app.include_router(demo_land_router)
app.include_router(land_intelligence_router)
app.include_router(map_router)
app.include_router(document_history_router)
app.include_router(ai_approval_router)
