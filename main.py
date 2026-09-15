"""Production ASGI entrypoint for the Document Screening application."""

from server import BASE_DIR, app
from ai_governance import router as ai_approval_router
from mapping import document_history_router, map_router

# The canonical server owns authentication, OCR, AI, audit, document, task,
# and admin behavior. This entrypoint adds only the Portfolio-derived map
# surface and its document-history support endpoints.
app.include_router(map_router)
app.include_router(document_history_router)
app.include_router(ai_approval_router)
