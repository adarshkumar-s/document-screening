"""Production ASGI entrypoint for the existing Document Screening application."""
from fastapi import Request
from fastapi.responses import Response
from server import app
from document_mapping import router as document_mapping_router
from demo_land import router as demo_land_router
from ai_governance import router as ai_approval_router

app.include_router(demo_land_router)
app.include_router(ai_approval_router)
app.include_router(document_mapping_router)

@app.middleware('http')
async def document_mapping_cache(request: Request, call_next):
    response=await call_next(request)
    if request.url.path.startswith('/api/land/document-map'):
        response.headers['Cache-Control']='no-store, private'
    return response
