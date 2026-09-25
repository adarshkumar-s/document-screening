"""Compatibility ASGI entrypoint for Document Screening."""

from ai_governance import router as ai_approval_router
from mapping import document_history_router, map_router
from land_intel import (
    encumbrance_router,
    land_router,
    mutation_router,
    parcel_router,
    report_router,
)
from demo_scenarios import demo_router
from backup_restore import backup_router
from server import app

app.include_router(map_router)
app.include_router(document_history_router)
app.include_router(ai_approval_router)
app.include_router(encumbrance_router)
app.include_router(mutation_router)
app.include_router(parcel_router)
app.include_router(land_router)
app.include_router(report_router)
app.include_router(demo_router)
app.include_router(backup_router)
