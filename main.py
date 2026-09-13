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

ADMIN_NAV = b'''<div id="administrationNav" class="administration-nav" style="display:inline-flex;position:relative;align-items:center;margin-right:10px;z-index:10000;">
<button id="administrationToggle" type="button" style="display:inline-flex;align-items:center;gap:7px;min-height:34px;padding:7px 13px;border:1px solid rgba(255,255,255,.55);border-radius:6px;background:#fff;color:#0b2d4d;font-weight:800;font-size:12px;line-height:1;cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.16);">Administration <span aria-hidden="true">&#9660;</span></button>
<div id="administrationMenu" style="display:none;position:absolute;top:calc(100% + 8px);right:0;width:230px;padding:7px;border:1px solid #cbd5e1;border-radius:9px;background:#fff;box-shadow:0 18px 45px rgba(15,23,42,.25);z-index:10001;">
<button type="button" data-admin-action="users" style="display:block;width:100%;padding:10px 11px;border:0;border-radius:6px;background:#fff;color:#16324f;text-align:left;font:600 13px/1.2 Arial,sans-serif;cursor:pointer;">Users</button>
<button type="button" data-admin-action="settings" style="display:block;width:100%;padding:10px 11px;border:0;border-radius:6px;background:#fff;color:#16324f;text-align:left;font:600 13px/1.2 Arial,sans-serif;cursor:pointer;">Settings</button>
<button type="button" data-admin-action="ai" style="display:block;width:100%;padding:10px 11px;border:0;border-radius:6px;background:#fff;color:#16324f;text-align:left;font:600 13px/1.2 Arial,sans-serif;cursor:pointer;">AI Corrections</button>
<div style="height:1px;background:#e2e8f0;margin:6px 2px;"></div>
<a href="/land-intelligence" style="display:block;width:100%;box-sizing:border-box;padding:10px 11px;border-radius:6px;color:#16324f;text-decoration:none;font:600 13px/1.2 Arial,sans-serif;">Land Intelligence</a>
<button type="button" data-admin-action="logout" style="display:block;width:100%;padding:10px 11px;border:0;border-radius:6px;background:#fff;color:#16324f;text-align:left;font:600 13px/1.2 Arial,sans-serif;cursor:pointer;">Log out</button>
</div></div>'''

@app.middleware("http")
async def add_navigation(request: Request, call_next):
    response = await call_next(request)
    if "text/html" not in response.headers.get("content-type", ""):
        return response
    body = b"".join([chunk async for chunk in response.body_iterator])

    # Put a real Administration entry point in the authenticated portal header.
    # The existing administration-nav.js enhances this markup with the app's
    # staff panels and modal behavior; the static markup guarantees the button
    # exists even if that enhancement script is delayed or unavailable.
    if request.url.path == "/" and b'id="administrationNav"' not in body:
        marker = b'<div class="gov-user-panel">'
        if marker in body:
            body = body.replace(marker, marker + ADMIN_NAV, 1)

    if b"/administration-nav.js" not in body and b"</body>" in body:
        body = body.replace(b"</body>", b'<script src="/administration-nav.js"></script></body>', 1)
    headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
    return Response(content=body, status_code=response.status_code, headers=headers, media_type="text/html")
