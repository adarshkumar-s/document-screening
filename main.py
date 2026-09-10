"""Production ASGI entrypoint for the existing application plus Land Intelligence."""
import os
from fastapi import Request
from fastapi.responses import FileResponse, Response
from server import app, BASE_DIR
import land_intelligence
from demo_land import router as demo_land_router

app.include_router(demo_land_router)

@app.get("/land-intelligence", include_in_schema=False)
def land_intelligence_ui():
    return FileResponse(os.path.join(BASE_DIR, "land-intelligence.html"))

@app.middleware("http")
async def add_land_intelligence_link(request: Request, call_next):
    """Add one small navigation action without replacing the existing application."""
    response = await call_next(request)
    if request.url.path != "/" or "text/html" not in response.headers.get("content-type", ""):
        return response
    body = b"".join([chunk async for chunk in response.body_iterator])
    link = ('<a href="/land-intelligence" style="position:fixed;right:18px;bottom:18px;z-index:99999;'
            'background:#1f4f8a;color:#fff;padding:11px 15px;border-radius:9px;text-decoration:none;'
            'font:700 13px system-ui;box-shadow:0 5px 18px rgba(0,0,0,.18)">Land Intelligence &rarr;</a>').encode("ascii")
    if b"/land-intelligence" not in body and b"</body>" in body:
        body = body.replace(b"</body>", link + b"</body>", 1)
    headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
    return Response(content=body, status_code=response.status_code, headers=headers, media_type="text/html")
