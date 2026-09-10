"""Production ASGI entrypoint.
Imports the existing application unchanged, then registers the Land Intelligence layer
and its standalone UI. The original / routes and APIs remain available.
"""
from fastapi.responses import FileResponse
from server import app, BASE_DIR
import land_intelligence  # registers /api/land routes and initializes synthetic dataset

@app.get("/land-intelligence", include_in_schema=False)
def land_intelligence_ui():
    return FileResponse(os.path.join(BASE_DIR, "land-intelligence.html"))
