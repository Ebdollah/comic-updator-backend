from fastapi import APIRouter
from app.api.v1 import health, sources, vortexscans

api_router = APIRouter()

# Include health check routes
api_router.include_router(health.router, prefix="/health", tags=["health"])

# Generic multi-source routes: /sources, /sources/{source}/series, ...
api_router.include_router(sources.router, prefix="/sources", tags=["sources"])

# Original vortexscans-only routes, kept so existing links keep working
api_router.include_router(vortexscans.router, prefix="/vortexscans", tags=["vortexscans"])
