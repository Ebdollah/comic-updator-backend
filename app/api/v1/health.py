from fastapi import APIRouter, Depends, HTTPException
from typing import Dict, Any
from app.core.logging import get_logger
from app.services.firebase import firebase_service

logger = get_logger(__name__)
router = APIRouter()


@router.get("")
async def health_check() -> Dict[str, Any]:
    """Health check endpoint."""
    return {
        "status": "healthy",
        "message": "Comic Updator Backend is running",
        "version": "1.0.0"
    }


@router.get("/detailed")
async def detailed_health_check() -> Dict[str, Any]:
    """Detailed health check including database connectivity."""
    health_status = {
        "status": "healthy",
        "message": "Comic Updator Backend is running",
        "version": "1.0.0",
        "services": {}
    }
    
    # Check Firebase connectivity
    try:
        if firebase_service.db is not None:
            # Try to access a collection to test connectivity
            test_collection = firebase_service.get_collection("health_check")
            health_status["services"]["firebase"] = {
                "status": "connected",
                "message": "Firebase connection successful"
            }
        else:
            health_status["services"]["firebase"] = {
                "status": "not_configured",
                "message": "Firebase not configured"
            }
    except Exception as e:
        logger.error(f"Firebase health check failed: {e}")
        health_status["services"]["firebase"] = {
            "status": "error",
            "message": f"Firebase connection failed: {str(e)}"
        }
        health_status["status"] = "degraded"
    
    return health_status
