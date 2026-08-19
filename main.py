from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from app.core.config import settings
from app.core.logging import setup_logging, get_logger
from app.core.scheduler import start_scheduler, shutdown_scheduler
from app.api.api import router as api_router
from app.services.firebase import firebase_service

# Setup logging
setup_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    # Startup
    logger.info("Starting Comic Updator Backend")
    
    # Initialize Firebase
    try:
        firebase_service.initialize()
        logger.info("Firebase service initialized")
    except Exception as e:
        logger.error(f"Failed to initialize Firebase: {e}")

    # Start the recurring scrape job
    try:
        start_scheduler()
    except Exception as e:
        logger.error(f"Failed to start scheduler: {e}")

    yield

    # Shutdown
    shutdown_scheduler()
    logger.info("Shutting down Comic Updator Backend")


# Create FastAPI application
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Backend API for Comic Updator application",
    debug=settings.debug,
    lifespan=lifespan
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_origin_regex=settings.cors_origin_regex,
    allow_credentials=True,
    allow_methods=settings.cors_methods,
    allow_headers=settings.cors_headers,
)

# Include API routes
app.include_router(api_router, prefix="/api")


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "message": "Welcome to Comic Updator Backend API",
        "version": settings.app_version,
        "docs": "/docs",
        "health": "/api/v1/health"
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_level="info" if not settings.debug else "debug"
    )
