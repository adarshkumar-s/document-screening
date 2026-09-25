"""Production ASGI entrypoint for the Document Screening application."""

from server import BASE_DIR, app
from ai_governance import router as ai_approval_router
from mapping import document_history_router, map_router
from land_intel import (
    court_router,
    encumbrance_router,
    land_router,
    mutation_router,
    report_router,
)
from demo_scenarios import demo_router
from backup_restore import backup_router

# The canonical server owns authentication, OCR, AI, audit, document, task,
# and admin behavior. This entrypoint adds only the Portfolio-derived map
# surface, its document-history support endpoints, the AI-approval router,
# and the Land Intelligence extension (encumbrances, mutations, land risk,
# verification reports, demo scenarios, and data management).
app.include_router(map_router)
app.include_router(document_history_router)
app.include_router(ai_approval_router)
app.include_router(encumbrance_router)
app.include_router(mutation_router)
app.include_router(land_router)
app.include_router(court_router)
app.include_router(report_router)
app.include_router(demo_router)
app.include_router(backup_router)
