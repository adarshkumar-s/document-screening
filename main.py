"""Production ASGI entrypoint for the existing Document Screening application."""
from fastapi import Request
from fastapi.responses import FileResponse
from server import app, BASE_DIR
from document_mapping import router as document_mapping_router
from document_insights import router as document_insights_router
from demo_land import router as demo_land_router
from ai_governance import router as ai_approval_router

app.include_router(demo_land_router)
app.include_router(ai_approval_router)
app.include_router(document_mapping_router)
app.include_router(document_insights_router)

@app.get('/document-map', include_in_schema=False)
def document_map_ui(): return FileResponse(f'{BASE_DIR}/document-map.html')
@app.get('/document-map.css', include_in_schema=False)
def document_map_css(): return FileResponse(f'{BASE_DIR}/document-map.css', media_type='text/css')
@app.get('/document-map.js', include_in_schema=False)
def document_map_js(): return FileResponse(f'{BASE_DIR}/document-map.js', media_type='application/javascript')

@app.middleware('http')
async def document_mapping_cache(request: Request, call_next):
    response=await call_next(request)
    if request.url.path.startswith('/api/land/document-map') or request.url.path.startswith('/api/land/document-insights'):
        response.headers['Cache-Control']='no-store, private'
    return response
