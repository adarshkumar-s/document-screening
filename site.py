"""Production ASGI entrypoint.
Imports the existing application unchanged, then registers the Land Intelligence layer
and its standalone UI. The original / routes and APIs remain available.
"""
import os
from fastapi.responses import FileResponse
from server import app, BASE_DIR
import land_intelligence
from demo_land import router as demo_land_router

app.include_router(demo_land_router)

@app.get("/land-intelligence", include_in_schema=False)
def land_intelligence_ui():
    return FileResponse(os.path.join(BASE_DIR, "land-intelligence.html"))
