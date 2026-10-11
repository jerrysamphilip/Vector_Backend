# app/main.py
"""
Outreach AI - FastAPI Application
Campaign Manager Backend
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

# Logging: emit logger.info/warning calls (level from LOG_LEVEL, default INFO)
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s",
)
# Request IDs in every log line (LOG_FORMAT=json for JSON logs) and optional Sentry (SENTRY_DSN)
from app.core.observability import configure_logging, init_sentry, install_observability  # noqa: E402
configure_logging()
init_sentry()
logger = logging.getLogger("app.main")
 
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
 
from app.core.config import settings
 
# Import all models FIRST to resolve SQLAlchemy relationships
import app.models  # noqa: F401
# Trigger reload
 
from app.routers import campaign_router, template_router
from app.routers.prospect_list_router import router as prospect_list_router
from app.routers.prospect_list_router import prospect_upload_router
from app.routers.ai_email_router import router as ai_email_router
from app.routers.campaign_wizard_router import router as campaign_wizard_router
from app.routers.tracking_router import router as tracking_router
from app.routers.ses_webhook_router import router as ses_webhook_router
from app.routers.email_scheduler_router import router as email_scheduler_router
from app.routers.company_profile_router import router as company_profile_router
from app.routers.inbox_router import router as inbox_router
from app.routers.automation_router import router as automation_router
from app.routers.auth_router import router as auth_router
from app.routers.user_router import router as user_router

# =============================
# LIFESPAN - MIGRATIONS (opt-in) AND BACKGROUND LOOPS
# =============================
# Schema changes run via `python -m app.db.migrate` (compose `migrate` service, Kubernetes
# initContainer); AUTO_MIGRATE=true also runs them here for local convenience. The background
# loops live in app/jobs.py and normally run in the worker (`python -m app.worker`); with
# RUN_BACKGROUND_JOBS=true (the default) they also run here. Each iteration takes a MySQL named
# lock, so the API and the worker never run the same iteration twice.
@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.AUTO_MIGRATE:
        from app.db.migrate import run_migrations
        await asyncio.get_running_loop().run_in_executor(None, run_migrations)

    tasks = []
    if settings.RUN_BACKGROUND_JOBS:
        from app import jobs
        tasks = jobs.start_jobs()
        logger.info("Started %d background loops in the API process (RUN_BACKGROUND_JOBS=true)", len(tasks))
    try:
        yield
    except (asyncio.CancelledError, KeyboardInterrupt):
        # Normal during reload/CTRL+C shutdown.
        pass
    finally:
        if tasks:
            from app import jobs
            await jobs.stop_jobs(tasks)


# Create FastAPI app with lifespan
# API docs are off in production unless ENABLE_DOCS=true
_docs_enabled = settings.ENABLE_DOCS or not settings.is_production
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="AI-powered email outreach platform",
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
    lifespan=lifespan,
)

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi import Request
import json

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    # Never log the request body (it can carry passwords/tokens); path + error locations only
    logger.info(
        "422 validation error on %s %s: %s",
        request.method,
        request.url.path,
        [(".".join(str(p) for p in err.get("loc", ())), err.get("type")) for err in exc.errors()],
    )
    return JSONResponse(
        status_code=422,
        content={"success": False, "error": "Validation Error", "detail": exc.errors(), "code": "VALIDATION_ERROR"},
    )
 
# Kartik has changed this: Added global rate limiter and standard exception handlers
try:
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.util import get_remote_address
    from slowapi.errors import RateLimitExceeded
    from slowapi.middleware import SlowAPIMiddleware
    _SLOWAPI_AVAILABLE = True
except ImportError:
    _SLOWAPI_AVAILABLE = False

from app.core.exceptions import global_exception_handler, BusinessLogicError, ResourceNotFoundError, ExternalServiceError

if _SLOWAPI_AVAILABLE:
    limiter = Limiter(key_func=get_remote_address, default_limits=["100/minute"])
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)
else:
    print("WARNING slowapi not installed. Rate limiting middleware is disabled.")

app.add_exception_handler(BusinessLogicError, global_exception_handler)
app.add_exception_handler(ResourceNotFoundError, global_exception_handler)
app.add_exception_handler(ExternalServiceError, global_exception_handler)
app.add_exception_handler(Exception, global_exception_handler)

from app.core.database import engine
from sqlalchemy import text
 
# CORS middleware for frontend
allowed_origins = [origin.strip() for origin in (settings.FRONTEND_URL or "").split(",") if origin.strip()]
if settings.DEBUG:
    for dev_origin in ("http://localhost:5173", "http://127.0.0.1:5173"):
        if dev_origin not in allowed_origins:
            allowed_origins.append(dev_origin)

# Refuse to start (when deployed) without the key that encrypts mailbox passwords
from app.core.encrypted_type import check_credentials_key
check_credentials_key()

# Per-IP request ceiling; sign-in/reset endpoints have stricter limits (app/core/rate_limit.py)
from app.core.rate_limit import RateLimitMiddleware
app.add_middleware(RateLimitMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins or ["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Outermost: X-Request-ID + Prometheus HTTP metrics, and GET /metrics (app/core/observability.py)
install_observability(app)
 
 
# =============================
# HEALTH CHECK
# =============================
 
@app.get("/health", tags=["Health"])
async def health_check():
    """Liveness check: the process is up and serving requests (no dependencies checked)."""
    return {
        "status": "healthy",
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
    }


@app.get("/health/ready", tags=["Health"])
def readiness_check():
    """Readiness check: the database answers SELECT 1, else 503."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as e:
        logger.warning("Readiness check failed: database unreachable (%s)", type(e).__name__)
        return JSONResponse(status_code=503, content={"status": "unavailable", "database": "unreachable"})
    return {"status": "ready", "database": "ok"}
 
 
# =============================
# REGISTER ROUTERS
# =============================
 
# Campaign Management
app.include_router(auth_router)
app.include_router(user_router)
# Before campaign_router: GET /campaigns/lists would otherwise match GET /campaigns/{campaign_id}
app.include_router(campaign_wizard_router)
app.include_router(campaign_router)
app.include_router(template_router)
app.include_router(prospect_list_router)
app.include_router(prospect_upload_router)
app.include_router(ai_email_router)
app.include_router(tracking_router)
app.include_router(ses_webhook_router)
app.include_router(email_scheduler_router)
app.include_router(company_profile_router)
app.include_router(inbox_router)
app.include_router(automation_router)
from app.routers.conversation_router import router as conversation_router
from app.routers.deliverability_router import router as deliverability_router
from app.routers.campaign_draft_router import router as campaign_draft_router
from app.routers.reports_router import router as reports_router
app.include_router(conversation_router)
app.include_router(deliverability_router)
app.include_router(campaign_draft_router)
app.include_router(reports_router)


from app.routers.platform_admin_router import router as platform_admin_router
app.include_router(platform_admin_router)

# Contact management (contacts across all lists, accounts, activities)
from app.routers.contacts_router import router as contacts_router
from app.routers.accounts_router import router as accounts_router
app.include_router(contacts_router)
app.include_router(accounts_router)
from app.routers.tasks_router import router as tasks_router
from app.routers.lists_router import router as lists_router
from app.routers.crm_router import router as crm_router
from app.routers.import_router import router as import_router
app.include_router(tasks_router)
app.include_router(lists_router)
app.include_router(crm_router)
app.include_router(import_router)

# Phase 2: sales hierarchy, leads, pipeline, reports (BRD v2.0 5.4 - 5.10)
from app.routers.sales_router import router as sales_router
from app.routers.sales_reports_router import router as sales_reports_router
app.include_router(sales_router)
app.include_router(sales_reports_router)
from app.routers.sales_admin_router import router as sales_admin_router
app.include_router(sales_admin_router)
from app.routers.quotes_router import router as quotes_router
from app.routers.custom_reports_router import router as custom_reports_router
from app.routers.connections_router import router as connections_router
app.include_router(quotes_router)
app.include_router(custom_reports_router)
app.include_router(connections_router)
 
 
# =============================
# ROOT
# =============================
 
@app.get("/", tags=["Root"])
async def root():
    """API root - returns available endpoints."""
    return {
        "message": f"Welcome to {settings.APP_NAME} API",
        "version": settings.APP_VERSION,
        "docs": "/docs",
        "endpoints": {
            "campaigns": "/campaigns",
            "sequences": "/campaigns/{id}/sequences",
            "templates": "/templates",
            "health": "/health",
        }
    }
 
