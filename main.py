"""Production ASGI entrypoint for the Document Screening application."""

from fastapi import Request

from server import BASE_DIR, app
from ai_governance import router as ai_approval_router
from mapping import document_history_router, map_router
from land_intel import (
    encumbrance_router,
    land_router,
    mutation_router,
    report_router,
)
from court_cases import router as court_cases_router
from demo_scenarios import demo_router
from backup_restore import backup_router
from performance import performance_middleware

# Keep the performance fast-path narrowly scoped to heavy list endpoints.
# All other routes continue through the canonical application unchanged.
@app.middleware("http")
async def _performance_fast_path(request: Request, call_next):
    return await performance_middleware(request, call_next)

# The canonical server owns authentication, OCR, AI, audit, document, task,
# and admin behavior. This entrypoint adds only the map/history surface,
# AI approval, Land Intelligence, and the additive litigation register.
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
