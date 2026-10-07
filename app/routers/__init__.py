# app/routers/__init__.py
"""
API routers for Outreach AI.
"""

from app.routers.campaign_router import router as campaign_router
from app.routers.template_router import router as template_router

__all__ = [
    "campaign_router",
    "template_router",
]
