"""Production ASGI entrypoint."""
from server import app
import land_intelligence
from demo_land import router as demo_land_router
from ai_governance import router as ai_approval_router
from land_intelligence_bridge import router as land_bridge_router

app.include_router(demo_land_router)
app.include_router(ai_approval_router)
app.include_router(land_bridge_router)
