"""Compatibility ASGI entrypoint for the existing application."""
import os
from fastapi import Request
from fastapi.responses import FileResponse, Response
from server import app, BASE_DIR
from document_mapping import router as document_mapping_router
from demo_land import router as demo_land_router
from ai_governance import router as ai_approval_router

app.include_router(demo_land_router)
app.include_router(ai_approval_router)
app.include_router(document_mapping_router)

@app.get('/document-map', include_in_schema=False)
def document_map_ui():
    return FileResponse(os.path.join(BASE_DIR,'document-map.html'))

@app.get('/document-map.css', include_in_schema=False)
def document_map_css():
    return FileResponse(os.path.join(BASE_DIR,'document-map.css'),media_type='text/css')

@app.get('/document-map.js', include_in_schema=False)
def document_map_js():
    return FileResponse(os.path.join(BASE_DIR,'document-map.js'),media_type='application/javascript')

@app.middleware('http')
async def add_document_mapping_link(request: Request, call_next):
    response=await call_next(request)
    if request.url.path!='/' or 'text/html' not in response.headers.get('content-type',''):
        return response
    body=b''.join([chunk async for chunk in response.body_iterator])
    link=(' <a href="/document-map" style="position:fixed;right:18px;bottom:18px;z-index:99999;'
          'background:#1f4f8a;color:#fff;padding:11px 15px;border-radius:9px;text-decoration:none;'
          'font:700 13px system-ui;box-shadow:0 5px 18px rgba(0,0,0,.18)">Document Mapping &rarr;</a>').encode('ascii')
    if b'/document-map' not in body and b'</body>' in body:
        body=body.replace(b'</body>',link+b'</body>',1)
    headers={k:v for k,v in response.headers.items() if k.lower()!='content-length'}
    return Response(content=body,status_code=response.status_code,headers=headers,media_type='text/html')
