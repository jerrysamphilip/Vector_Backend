# app/main.py
"""
Outreach AI - FastAPI Application
Campaign Manager Backend
"""

import asyncio
from contextlib import asynccontextmanager
 
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

# Import scheduler and services for auto-start
from app.services.email_scheduler_service import email_scheduler
from app.services.imap_sync_service import imap_sync_service
from app.services.deliverability_service import deliverability_service
from app.services.warmup_service import warmup_service
from app.services.alert_center_service import alert_center_service


# =============================
# LIFESPAN - AUTO-START SCHEDULER
# =============================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start scheduler on startup, stop on shutdown."""
    # Startup: Launch scheduler in background
    print("Starting email scheduler (30s interval)...")
    scheduler_task = asyncio.create_task(
        email_scheduler.run_continuous(interval_seconds=30)
    )
    
    # Start IMAP Sync in background (every 5 mins)
    # Run in a thread-pool executor so blocking imaplib calls don't freeze the event loop.
    async def run_imap_sync():
        loop = asyncio.get_event_loop()
        print("Starting IMAP sync service (120s interval)...")
        while True:
            try:
                with SessionLocal() as db:
                    await loop.run_in_executor(
                        None, imap_sync_service.sync_all_active_inboxes, db
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"IMAP Sync Error: {e}")
            # Each inbox syncs about every 4 minutes (replies visible within 10, BR-DF-05)
            await asyncio.sleep(120)

    imap_task = asyncio.create_task(run_imap_sync())

    # Start Deliverability Sync in background (every 10 mins)
    # Run in a thread-pool executor so blocking DNS/SES calls don't freeze the event loop.
    async def run_deliverability_sync():
        loop = asyncio.get_event_loop()
        print("Starting Deliverability sync service (600s interval)...")

        def _do_deliverability_sync():
            with SessionLocal() as db:
                deliverability_service.sync_ses_metrics(db)
                settings_domains = db.execute(text("SELECT domain_name FROM sending_domains")).fetchall()
                for row in settings_domains:
                    deliverability_service.perform_dns_scan(row[0], db)

        while True:
            try:
                await loop.run_in_executor(None, _do_deliverability_sync)
                with SessionLocal() as db:
                    await alert_center_service.evaluate_and_dispatch_all(db)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Deliverability Sync Error: {e}")
            await asyncio.sleep(600)

    deliverability_task = asyncio.create_task(run_deliverability_sync())

    async def run_warmup_sync():
        interval = getattr(settings, "WARMUP_INTERVAL_SECONDS", 900) or 900
        print(f"Starting warmup service ({interval}s interval)...")
        while True:
            try:
                with SessionLocal() as db:
                    await warmup_service.run_cycle(db)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Warmup Service Error: {e}")
            await asyncio.sleep(interval)

    warmup_task = asyncio.create_task(run_warmup_sync())

    # Contacts and companies deleted more than 90 days ago are purged (BR-CM-35)
    async def run_crm_purge():
        from app.services.crm import purge_expired
        loop = asyncio.get_event_loop()
        while True:
            try:
                def _purge():
                    with SessionLocal() as db:
                        return purge_expired(db)
                purged = await loop.run_in_executor(None, _purge)
                if purged:
                    print(f"CRM purge: permanently deleted {purged} record(s) past the 90-day restore window")
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"CRM purge error: {e}")
            await asyncio.sleep(6 * 3600)

    purge_task = asyncio.create_task(run_crm_purge())

    # Every sent email gets a final status within 15 minutes (BR-DF-04), and domain /
    # campaign health is recomputed hourly with auto-pause on risk (BR-DF-07).
    async def run_send_safety():
        from app.services.send_safety import reconcile_delivery_status, run_health_cycle
        loop = asyncio.get_event_loop()
        last_health = 0.0
        while True:
            try:
                def _reconcile():
                    with SessionLocal() as db:
                        return reconcile_delivery_status(db)
                await loop.run_in_executor(None, _reconcile)
                if loop.time() - last_health >= 3600:
                    def _health():
                        with SessionLocal() as db:
                            return run_health_cycle(db)
                    result = await loop.run_in_executor(None, _health)
                    last_health = loop.time()
                    if result.get("paused"):
                        print(f"Health check paused {result['paused']} campaign(s)")
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Send safety loop error: {e}")
            await asyncio.sleep(60)

    send_safety_task = asyncio.create_task(run_send_safety())

    # Phase 2 "Should" jobs: notification emails every minute; lead recycling and
    # stale-deal alerts hourly (BR-SF-02, 05, 07); calendar / email sync (BR-SF-16)
    async def run_sales_jobs():
        from app.services import sales_jobs, account_sync
        from app.services.notifications import email_pending
        loop = asyncio.get_event_loop()
        last_hourly = 0.0
        last_sync = 0.0
        while True:
            try:
                with SessionLocal() as db:
                    await email_pending(db)
                if loop.time() - last_hourly >= 3600:
                    def _hourly():
                        with SessionLocal() as db:
                            return sales_jobs.recycle_leads(db), sales_jobs.stale_deals(db)
                    await loop.run_in_executor(None, _hourly)
                    last_hourly = loop.time()
                if loop.time() - last_sync >= 600:
                    def _sync():
                        with SessionLocal() as db:
                            return account_sync.sync_all(db)
                    await loop.run_in_executor(None, _sync)
                    last_sync = loop.time()
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Sales jobs error: {e}")
            await asyncio.sleep(60)

    sales_jobs_task = asyncio.create_task(run_sales_jobs())
    try:
        yield
    except (asyncio.CancelledError, KeyboardInterrupt):
        # Normal during reload/CTRL+C shutdown.
        pass
    finally:
        # Shutdown: Stop scheduler and background tasks gracefully.
        print("Stopping email scheduler...")
        email_scheduler.stop()

        scheduler_task.cancel()
        imap_task.cancel()
        deliverability_task.cancel()
        warmup_task.cancel()
        purge_task.cancel()
        send_safety_task.cancel()
        sales_jobs_task.cancel()

        await asyncio.gather(
            scheduler_task,
            imap_task,
            deliverability_task,
            warmup_task,
            purge_task,
            send_safety_task,
            sales_jobs_task,
            return_exceptions=True,
        )

# Create FastAPI app with lifespan
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="AI-powered email outreach platform",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi import Request
import json

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    print("="*50)
    print("422 VALIDATION ERROR:")
    print("ERRORS:", exc.errors())
    print("BODY:", exc.body)
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

# =============================
# AUTO-CREATE DATABASE TABLES
# =============================
# This ensures tables are created on startup for new developers
# without needing to run Alembic migrations manually.
from app.core.database import engine, SessionLocal
from app.models import Base  # Base is defined in app/models/__init__.py
from app.models.persona_blueprint import PersonaBlueprint
from app.db.seed_blueprints import seed_blueprints
from sqlalchemy import text
 
try:
    Base.metadata.create_all(bind=engine)
    print("Database tables verified/created successfully")
    
    # AUTO-MIGRATE: Add new columns if they don't exist (for existing databases)
    with engine.connect() as conn:
        # Check existing columns in campaigns table
        result = conn.execute(text("""
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
            AND TABLE_NAME = 'campaigns'
            AND COLUMN_NAME IN ('sender_name', 'cta_link', 'campaign_description', 'daily_batch_size')
        """))
        existing_cols = [row[0] for row in result.fetchall()]

        # Add sender_name if not exists
        if 'sender_name' not in existing_cols:
            try:
                conn.execute(text("ALTER TABLE campaigns ADD COLUMN sender_name VARCHAR(100) NULL"))
                conn.commit()
                print("Added sender_name column to campaigns table")
            except Exception:
                pass  # Column may already exist

        # Add campaign_description if not exists
        if 'campaign_description' not in existing_cols:
            try:
                conn.execute(text("ALTER TABLE campaigns ADD COLUMN campaign_description TEXT NULL"))
                conn.commit()
                print("Added campaign_description column to campaigns table")
            except Exception:
                pass  # Column may already exist

        # Add cta_link if not exists
        if 'cta_link' not in existing_cols:
            try:
                conn.execute(text("ALTER TABLE campaigns ADD COLUMN cta_link VARCHAR(500) NULL"))
                conn.commit()
                print("Added cta_link column to campaigns table")
            except Exception:
                pass  # Column may already exist

        # Add daily_batch_size if not exists
        if 'daily_batch_size' not in existing_cols:
            try:
                conn.execute(text("ALTER TABLE campaigns ADD COLUMN daily_batch_size INT NULL"))
                conn.commit()
                print("Added daily_batch_size column to campaigns table")
            except Exception:
                pass  # Column may already exist

        # ----------------------------------------
        # AUTO-MIGRATE: email_templates table
        # ----------------------------------------
        result = conn.execute(text("""
            SELECT COLUMN_NAME 
            FROM INFORMATION_SCHEMA.COLUMNS 
            WHERE TABLE_NAME = 'email_templates' 
            AND COLUMN_NAME IN ('cta_link', 'personalization_tokens')
        """))
        template_cols = [row[0] for row in result.fetchall()]
        
        # Add cta_link if not exists
        if 'cta_link' not in template_cols:
            try:
                conn.execute(text("ALTER TABLE email_templates ADD COLUMN cta_link TEXT NULL"))
                conn.commit()
                print("Added cta_link column to email_templates table")
            except Exception as e:
                print(f"WARNING Failed to add cta_link to email_templates: {e}")

        # Add personalization_tokens if not exists
        if 'personalization_tokens' not in template_cols:
            try:
                conn.execute(text("ALTER TABLE email_templates ADD COLUMN personalization_tokens JSON NULL"))
                conn.commit()
                print("Added personalization_tokens column to email_templates table")
            except Exception as e:
                print(f"WARNING Failed to add personalization_tokens to email_templates: {e}")
        
        # ----------------------------------------
        # AUTO-MIGRATE: email_messages table
        # ----------------------------------------
        result = conn.execute(text("""
            SELECT COLUMN_NAME 
            FROM INFORMATION_SCHEMA.COLUMNS 
            WHERE TABLE_NAME = 'email_messages' 
            AND COLUMN_NAME IN ('conversation_id', 'direction', 'campaign_id')
        """))
        msg_cols = [row[0] for row in result.fetchall()]
        
        # Add conversation_id if not exists
        if 'conversation_id' not in msg_cols:
            try:
                conn.execute(text("ALTER TABLE email_messages ADD COLUMN conversation_id VARCHAR(36) NULL"))
                conn.execute(text("ALTER TABLE email_messages ADD CONSTRAINT fk_em_conversation FOREIGN KEY (conversation_id) REFERENCES conversations(id)"))
                conn.commit()
                print("Added conversation_id column to email_messages table")
            except Exception as e:
                print(f"WARNING Failed to add conversation_id: {e}")

        # Add direction if not exists
        if 'direction' not in msg_cols:
            try:
                conn.execute(text("ALTER TABLE email_messages ADD COLUMN direction VARCHAR(20) DEFAULT 'OUTBOUND'"))
                conn.commit()
                print("Added direction column to email_messages table")
            except Exception as e:
                print(f"WARNING Failed to add direction: {e}")

        # Make campaign_id nullable (if it exists)
        if 'campaign_id' in msg_cols:
            try:
                # MySQL Syntax
                conn.execute(text("ALTER TABLE email_messages MODIFY COLUMN campaign_id VARCHAR(36) NULL"))
                conn.commit()
                print("Modified campaign_id to be NULLABLE in email_messages table")
            except Exception as e:
                print(f"WARNING Failed to modify campaign_id: {e}")

        # ----------------------------------------
        # AUTO-MIGRATE: sending_inboxes table
        # ----------------------------------------
        result = conn.execute(text("""
            SELECT COLUMN_NAME 
            FROM INFORMATION_SCHEMA.COLUMNS 
            WHERE TABLE_NAME = 'sending_inboxes' 
            AND COLUMN_NAME IN (
                'imap_host',
                'imap_username',
                'imap_password',
                'last_sync_at',
                'warmup_status',
                'warmup_pool',
                'warmup_reputation',
                'warmup_auto_adjust',
                'warmup_randomize',
                'warmup_reply_rate_target',
                'warmup_max_target',
                'warmup_daily_target',
                'warmup_identifier',
                'warmup_issue_code',
                'warmup_issue_message',
                'warmup_last_activity_at',
                'warmup_today_sent',
                'warmup_today_opened',
                'warmup_today_replied',
                'warmup_today_saved'
            )
        """))
        inbox_cols = [row[0] for row in result.fetchall()]
        
        # Add imap_host if not exists
        if 'imap_host' not in inbox_cols:
            try:
                conn.execute(text("ALTER TABLE sending_inboxes ADD COLUMN imap_host VARCHAR(255) NULL"))
                conn.execute(text("ALTER TABLE sending_inboxes ADD COLUMN imap_port INT DEFAULT 993"))
                conn.execute(text("ALTER TABLE sending_inboxes ADD COLUMN imap_username VARCHAR(255) NULL"))
                conn.execute(text("ALTER TABLE sending_inboxes ADD COLUMN imap_password VARCHAR(255) NULL"))
                conn.execute(text("ALTER TABLE sending_inboxes ADD COLUMN last_sync_at TIMESTAMP NULL"))
                conn.commit()
                print("Added IMAP columns to sending_inboxes table")
            except Exception as e:
                print(f"WARNING Failed to add IMAP columns: {e}")

        warmup_column_sql = {
            "warmup_status": "ALTER TABLE sending_inboxes ADD COLUMN warmup_status VARCHAR(50) DEFAULT 'ACTIVE'",
            "warmup_pool": "ALTER TABLE sending_inboxes ADD COLUMN warmup_pool VARCHAR(50) DEFAULT 'FOUNDATION'",
            "warmup_reputation": "ALTER TABLE sending_inboxes ADD COLUMN warmup_reputation FLOAT DEFAULT 65",
            "warmup_auto_adjust": "ALTER TABLE sending_inboxes ADD COLUMN warmup_auto_adjust BOOLEAN DEFAULT TRUE",
            "warmup_randomize": "ALTER TABLE sending_inboxes ADD COLUMN warmup_randomize BOOLEAN DEFAULT TRUE",
            "warmup_reply_rate_target": "ALTER TABLE sending_inboxes ADD COLUMN warmup_reply_rate_target INT DEFAULT 35",
            "warmup_max_target": "ALTER TABLE sending_inboxes ADD COLUMN warmup_max_target INT NULL",
            "warmup_daily_target": "ALTER TABLE sending_inboxes ADD COLUMN warmup_daily_target INT DEFAULT 0",
            "warmup_identifier": "ALTER TABLE sending_inboxes ADD COLUMN warmup_identifier VARCHAR(120) NULL",
            "warmup_issue_code": "ALTER TABLE sending_inboxes ADD COLUMN warmup_issue_code VARCHAR(100) NULL",
            "warmup_issue_message": "ALTER TABLE sending_inboxes ADD COLUMN warmup_issue_message TEXT NULL",
            "warmup_last_activity_at": "ALTER TABLE sending_inboxes ADD COLUMN warmup_last_activity_at TIMESTAMP NULL",
            "warmup_today_sent": "ALTER TABLE sending_inboxes ADD COLUMN warmup_today_sent INT DEFAULT 0",
            "warmup_today_opened": "ALTER TABLE sending_inboxes ADD COLUMN warmup_today_opened INT DEFAULT 0",
            "warmup_today_replied": "ALTER TABLE sending_inboxes ADD COLUMN warmup_today_replied INT DEFAULT 0",
            "warmup_today_saved": "ALTER TABLE sending_inboxes ADD COLUMN warmup_today_saved INT DEFAULT 0",
        }
        for col_name, sql in warmup_column_sql.items():
            if col_name not in inbox_cols:
                try:
                    conn.execute(text(sql))
                    conn.commit()
                    print(f"Added {col_name} column to sending_inboxes table")
                except Exception as e:
                    print(f"WARNING Failed to add {col_name} to sending_inboxes: {e}")

        # ----------------------------------------
        # AUTO-MIGRATE: global_unsubscribes table
        # ----------------------------------------
        result = conn.execute(text("""
            SELECT COLUMN_NAME 
            FROM INFORMATION_SCHEMA.COLUMNS 
            WHERE TABLE_NAME = 'global_unsubscribes' 
            AND COLUMN_NAME = 'suppression_expires_at'
        """))
        gu_cols = [row[0] for row in result.fetchall()]
        
        if 'suppression_expires_at' not in gu_cols:
            try:
                conn.execute(text("ALTER TABLE global_unsubscribes ADD COLUMN suppression_expires_at TIMESTAMP NULL"))
                conn.commit()
                print("Added suppression_expires_at column to global_unsubscribes table")
            except Exception as e:
                print(f"WARNING Failed to add suppression_expires_at column: {e}")
    
        # ----------------------------------------
        # AUTO-MIGRATE: users table (auth fields)
        # ----------------------------------------
        result = conn.execute(text("""
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = 'users'
              AND COLUMN_NAME IN (
                  'password_hash',
                  'auth_provider',
                  'google_id',
                  'email_verified',
                  'avatar_url',
                  'invited_by'
              )
        """))
        user_cols = [row[0] for row in result.fetchall()]

        if 'password_hash' not in user_cols:
            try:
                conn.execute(text("ALTER TABLE users ADD COLUMN password_hash VARCHAR(255) NULL"))
                conn.commit()
                print("Auth migrate: added users.password_hash")
            except Exception as e:
                print(f"Auth migrate warning (password_hash): {e}")

        if 'auth_provider' not in user_cols:
            try:
                conn.execute(text("ALTER TABLE users ADD COLUMN auth_provider VARCHAR(50) NULL DEFAULT 'local'"))
                conn.commit()
                print("Auth migrate: added users.auth_provider")
            except Exception as e:
                print(f"Auth migrate warning (auth_provider): {e}")

        if 'google_id' not in user_cols:
            try:
                conn.execute(text("ALTER TABLE users ADD COLUMN google_id VARCHAR(255) NULL"))
                conn.commit()
                print("Auth migrate: added users.google_id")
            except Exception as e:
                print(f"Auth migrate warning (google_id): {e}")

        if 'email_verified' not in user_cols:
            try:
                conn.execute(text("ALTER TABLE users ADD COLUMN email_verified BOOLEAN NOT NULL DEFAULT FALSE"))
                conn.commit()
                print("Auth migrate: added users.email_verified")
            except Exception as e:
                print(f"Auth migrate warning (email_verified): {e}")

        if 'avatar_url' not in user_cols:
            try:
                conn.execute(text("ALTER TABLE users ADD COLUMN avatar_url VARCHAR(500) NULL"))
                conn.commit()
                print("Auth migrate: added users.avatar_url")
            except Exception as e:
                print(f"Auth migrate warning (avatar_url): {e}")

        if 'invited_by' not in user_cols:
            try:
                conn.execute(text("ALTER TABLE users ADD COLUMN invited_by VARCHAR(36) NULL"))
                conn.commit()
                user_cols.append("invited_by")
                print("Auth migrate: added users.invited_by")
            except Exception as e:
                print(f"Auth migrate warning (invited_by): {e}")

        try:
            uq_tenant_email = conn.execute(text("""
                SELECT INDEX_NAME
                FROM INFORMATION_SCHEMA.STATISTICS
                WHERE TABLE_NAME = 'users'
                  AND INDEX_NAME = 'uq_user_tenant_email'
                LIMIT 1
            """)).first()
            if not uq_tenant_email:
                conn.execute(text("ALTER TABLE users ADD CONSTRAINT uq_user_tenant_email UNIQUE (tenant_id, email)"))
                conn.commit()
                print("Auth migrate: added uq_user_tenant_email")
        except Exception as e:
            print(f"Auth migrate warning (uq_user_tenant_email): {e}")

        try:
            uq_google = conn.execute(text("""
                SELECT INDEX_NAME
                FROM INFORMATION_SCHEMA.STATISTICS
                WHERE TABLE_NAME = 'users'
                  AND INDEX_NAME = 'uq_users_google_id'
                LIMIT 1
            """)).first()
            if not uq_google:
                conn.execute(text("ALTER TABLE users ADD CONSTRAINT uq_users_google_id UNIQUE (google_id)"))
                conn.commit()
                print("Auth migrate: added uq_users_google_id")
        except Exception as e:
            print(f"Auth migrate warning (uq_users_google_id): {e}")

        try:
            fk_invited = conn.execute(text("""
                SELECT CONSTRAINT_NAME
                FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
                WHERE TABLE_NAME = 'users'
                  AND CONSTRAINT_TYPE = 'FOREIGN KEY'
                  AND CONSTRAINT_NAME = 'fk_users_invited_by'
                LIMIT 1
            """)).first()
            if not fk_invited and 'invited_by' in user_cols:
                conn.execute(text(
                    "ALTER TABLE users ADD CONSTRAINT fk_users_invited_by "
                    "FOREIGN KEY (invited_by) REFERENCES users(user_id)"
                ))
                conn.commit()
                print("Auth migrate: added fk_users_invited_by")
        except Exception as e:
            print(f"Auth migrate warning (fk_users_invited_by): {e}")

        try:
            user_cols_refresh = conn.execute(text(
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_NAME = 'users' AND TABLE_SCHEMA = DATABASE()"
            )).fetchall()
            user_col_names = {r[0] for r in user_cols_refresh}
            if 'custom_permissions' not in user_col_names:
                conn.execute(text("ALTER TABLE users ADD COLUMN custom_permissions JSON NULL"))
                conn.commit()
                print("Auth migrate: added custom_permissions column to users")
        except Exception as e:
            print(f"Auth migrate warning (custom_permissions): {e}")

    # Seed/update persona blueprints on every startup
    # This ensures all developers get the latest blueprint definitions
    from app.db.contact_schema import ensure_contact_schema
    ensure_contact_schema(engine)

    from app.db.sending_schema import ensure_sending_schema
    ensure_sending_schema(engine)

    from app.db.phase2_schema import ensure_phase2_schema
    ensure_phase2_schema(engine)

    from app.db.security_schema import encrypt_mailbox_passwords
    encrypt_mailbox_passwords(engine)

    with SessionLocal() as db:
        total = seed_blueprints(db)
        print(f"Persona blueprints synchronized ({total} total)")
except Exception as e:
    print(f"WARNING Database setup warning: {e}")
 
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
 
 
# =============================
# HEALTH CHECK
# =============================
 
@app.get("/health", tags=["Health"])
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
    }
 
 
# =============================
# REGISTER ROUTERS
# =============================
 
# Campaign Management
app.include_router(auth_router)
app.include_router(user_router)
app.include_router(campaign_router)
app.include_router(template_router)
app.include_router(prospect_list_router)
app.include_router(prospect_upload_router)
app.include_router(ai_email_router)
app.include_router(campaign_wizard_router)
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
 
