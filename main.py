"""Production ASGI entrypoint."""
from fastapi import Request
from fastapi.responses import Response
from server import app
import land_intelligence
from demo_land import router as demo_land_router
from ai_governance import router as ai_approval_router
from land_intelligence_bridge import router as land_bridge_router

app.include_router(demo_land_router)
app.include_router(ai_approval_router)
app.include_router(land_bridge_router)

@app.middleware("http")
async def add_navigation(request: Request, call_next):
    response = await call_next(request)
    if "text/html" not in response.headers.get("content-type", ""):
        return response
    body = b"".join([chunk async for chunk in response.body_iterator])
    if b"/administration-nav.js" not in body and b"</body>" in body:
        body = body.replace(b"</body>", b'<script src="/administration-nav.js"></script></body>', 1)
    headers = {k:v for k,v in response.headers.items() if k.lower() != "content-length"}
    return Response(content=body, status_code=response.status_code, headers=headers, media_type="text/html")
