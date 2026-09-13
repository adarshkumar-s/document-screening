"""Compatibility ASGI entrypoint for Document Screening."""

from ai_governance import router as ai_approval_router
from mapping import document_history_router, map_router
from server import app

app.include_router(map_router)
app.include_router(document_history_router)
app.include_router(ai_approval_router)
