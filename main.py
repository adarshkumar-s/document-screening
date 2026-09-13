"""Production ASGI entrypoint for the existing application plus Land Intelligence."""
from fastapi import Request
from fastapi.responses import Response
from server import app, BASE_DIR
import land_intelligence
from demo_land import router as demo_land_router
from ai_governance import router as ai_approval_router
from land_intelligence_bridge import router as land_bridge_router

app.include_router(demo_land_router)
app.include_router(ai_approval_router)
app.include_router(land_bridge_router)


@app.middleware("http")
async def add_land_intelligence_link(request: Request, call_next):
    """Add Land Intelligence navigation and the saved-document bridge to HTML pages."""
    response = await call_next(request)
    if "text/html" not in response.headers.get("content-type", ""):
        return response
    body = b"".join([chunk async for chunk in response.body_iterator])
    script = b'<script src="/land-intelligence-bridge.js"></script>'
    if b"land-intelligence-bridge.js" not in body and b"</body>" in body:
        body = body.replace(b"</body>", script + b"</body>", 1)
    if request.url.path == "/":
        link = ('<a href="/land-intelligence" style="position:fixed;right:18px;bottom:18px;z-index:99999;'
                'background:#1f4f8a;color:#fff;padding:11px 15px;border-radius:9px;text-decoration:none;'
                'font:700 13px system-ui;box-shadow:0 5px 18px rgba(0,0,0,.18)">Land Intelligence &rarr;</a>').encode("ascii")
        if b"/land-intelligence" not in body and b"</body>" in body:
            body = body.replace(b"</body>", link + b"</body>", 1)
    headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
    return Response(content=body, status_code=response.status_code, headers=headers, media_type="text/html")
