# app/core/config.py
"""
Application configuration settings.
Loads environment variables for database and app settings.
"""

import os
from pathlib import Path
from urllib.parse import quote_plus
from pydantic_settings import BaseSettings
from functools import lru_cache


_BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    # App Settings
    APP_NAME: str = "Outreach AI"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    
    # MySQL Database Settings
    MYSQL_HOST: str = os.getenv("MYSQL_HOST", "host.docker.internal")
    MYSQL_PORT: int = int(os.getenv("MYSQL_PORT", "3306"))
    MYSQL_USER: str = os.getenv("MYSQL_USER", "root")
    MYSQL_PASSWORD: str = os.getenv("MYSQL_PASSWORD", "")
    MYSQL_DATABASE: str = os.getenv("MYSQL_DATABASE", "outreach_ai")
    
    # Connection Pool Settings
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_RECYCLE: int = 300
    
    # OpenAI Settings
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o-mini"
    
    # Default IDs for multi-tenant support
    DEFAULT_TENANT_ID: str = "00000000-0000-0000-0000-000000000001"
    DEFAULT_SYSTEM_USER_ID: str = "00000000-0000-0000-0000-000000000001"
    
    # AWS SES Configuration
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_REGION: str = "us-east-1"
    AWS_SES_CONFIGURATION_SET: str = ""
    AWS_SNS_TOPIC_ARN: str = ""
    AWS_SNS_ENABLED: bool = False
    
    # Email Settings
    SENDER_EMAIL: str = ""
    SENDER_NAME: str = "Outreach AI"
    SES_MAX_SEND_RATE: int = 12  # emails per second
    SES_SANDBOX_MODE: bool = False

    # CAN-SPAM / GDPR compliance
    # Physical mailing address appended to every outbound email footer (CAN-SPAM §5(a)(5))
    COMPANY_PHYSICAL_ADDRESS: str = "Neutrino Tech Systems, 12301 West Parmer Lane, Unit 707, Cedar Park, TX 78613, USA"
    # Spam score threshold above which the email is BLOCKED from being sent (0–100).
    # Set to 0 to disable the gate entirely (not recommended for production).
    SPAM_SCORE_BLOCK_THRESHOLD: int = 25

    # Anti-spam sending jitter (random delay in seconds between sends from same inbox)
    SENDING_JITTER_MIN_SECONDS: int = 3
    SENDING_JITTER_MAX_SECONDS: int = 15

    # URL shorteners to flag as spam risk in link validation (comma-separated)
    BLOCKED_URL_SHORTENERS: str = "bit.ly,tinyurl.com,t.co,goo.gl,ow.ly,is.gd,buff.ly,rebrand.ly"

    # Custom tracking domain (links should match sender domain or this domain)
    CUSTOM_TRACKING_DOMAIN: str = ""

    # Maximum imperative verb density before flagging (percentage 0-100)
    MAX_IMPERATIVE_VERB_DENSITY: int = 8

    # Auto-suppress hard bounces (add to GlobalUnsubscribe on permanent failure)
    AUTO_SUPPRESS_HARD_BOUNCES: bool = True

    # Auto-pause guard (BR-DF-07). A campaign, or every campaign sending from a
    # domain, pauses within a minute when its hard-bounce or complaint rate
    # crosses these limits (SES itself reviews accounts at 5% / 0.1%).
    AUTO_PAUSE_BOUNCE_RATE: float = 0.05
    AUTO_PAUSE_COMPLAINT_RATE: float = 0.003
    AUTO_PAUSE_MIN_SENDS: int = 50          # rates apply from this many sends
    AUTO_PAUSE_MIN_BOUNCES: int = 5          # below MIN_SENDS: pause at this many hard bounces
    AUTO_PAUSE_MIN_COMPLAINTS: int = 2       # below MIN_SENDS: pause at this many complaints
    # BR-DF-04: a sent email with no delivery event after this long is marked UNCONFIRMED
    DELIVERY_CONFIRM_MINUTES: int = 15

    # Phase 2 (BRD v2.0). Financial year start month for forecasts (4 = April, Indian FY;
    # 1 = calendar year). New contacts emailed per user per day; follow-ups are not counted.
    FISCAL_YEAR_START_MONTH: int = 4
    DAILY_NEW_CONTACT_LIMIT: int = 500
    QUOTE_CURRENCY: str = "USD"          # printed on proposal PDFs (BR-SF-15)

    # Microsoft 365 mailboxes sign in with OAuth (BR-DF-05); see app/services/ms365_oauth.py
    MS365_CLIENT_ID: str = ""
    MS365_CLIENT_SECRET: str = ""
    MS365_TENANT_ID: str = "common"
    MS365_REDIRECT_URI: str = ""          # https://<host>/vector/api/inboxes/oauth/microsoft/callback
    MS365_POST_CONNECT_URL: str = "/vector/app/inboxes"  # where the browser lands afterwards

    # Personal calendar and email sync (BR-SF-16). Microsoft reuses MS365_CLIENT_ID/SECRET with its own redirect.
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = ""         # https://<host>/vector/api/connections/google/callback
    GRAPH_REDIRECT_URI: str = ""          # https://<host>/vector/api/connections/microsoft/callback
    CONNECTIONS_POST_CONNECT_URL: str = "/vector/app/connections"
    
    # JWT Authentication (validated at start-up, see app/core/secrets_guard.py)
    JWT_SECRET_KEY: str = "change-this-in-production-use-a-long-random-string"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Encrypts mailbox (SMTP/IMAP) passwords at rest. Required when deployed; keep it stable.
    CREDENTIALS_ENCRYPTION_KEY: str = ""

    # Shared secret for the SES/SNS webhook when SNS uses raw message delivery (raw messages carry
    # no AWS signature). Append ?token=<value> to the subscription URL.
    SES_WEBHOOK_TOKEN: str = ""

    # Google OAuth
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = ""

    # Frontend URL (for CORS and redirects)
    FRONTEND_URL: str = "http://localhost:5173"
    INVITE_ACCEPT_URL: str = ""
    RESET_PASSWORD_URL: str = ""
    PASSWORD_RESET_EXPIRE_MINUTES: int = 60
    MAGIC_LOGIN_URL: str = ""
    MAGIC_LOGIN_EXPIRE_HOURS: int = 168

    # Environment
    ENVIRONMENT: str = "development"
    BASE_URL: str = "https://outreach360.neutrinoaistudio.com"

    # Unsubscribe Suppression
    # After a prospect unsubscribes, they are blocked from ALL new campaigns for this duration.
    # Default: 180 days (6 months).
    # For testing: set UNSUBSCRIBE_SUPPRESSION_DAYS=1 for 1-day suppression.
    # For rapid testing: set UNSUBSCRIBE_SUPPRESSION_HOURS=2 for 2-hour suppression
    #   (HOURS takes priority over DAYS when > 0).
    UNSUBSCRIBE_SUPPRESSION_DAYS: int = 180
    UNSUBSCRIBE_SUPPRESSION_HOURS: int = 0  # 0 = disabled, use DAYS instead

    # External mailbox-provider ingestion (read-only, safe by default)
    ENABLE_POSTMASTER_INGEST: bool = False
    ENABLE_SNDS_INGEST: bool = False
    ENABLE_JMRP_INGEST: bool = False
    POSTMASTER_FEED_URL: str = ""  # Optional JSON feed URL
    SNDS_FEED_URL: str = ""        # Optional CSV feed URL
    JMRP_FEED_URL: str = ""        # Optional JSON feed URL

    # Email attachments (stored on local disk, see app/routers/template_router.py)
    ATTACHMENTS_DIR: str = str(_BASE_DIR / "uploads" / "attachments")
    MAX_ATTACHMENT_SIZE_MB: int = 10
    MAX_ATTACHMENTS_PER_TEMPLATE: int = 10

    # Warmup engine
    WARMUP_INTERVAL_SECONDS: int = 900
    WARMUP_MAX_SENDS_PER_CYCLE: int = 3
    WARMUP_REPLY_RATE_DEFAULT: int = 35
    WARMUP_RANDOMIZE_VARIANCE: int = 15
    
    @property
    def DATABASE_URL(self) -> str:
        """
        Return database URL.
        Priority:
        1. DATABASE_URL/Database_URL environment variable (with automatic "self-healing" for unencoded special chars)
        2. Construction from individual components (MYSQL_USER, MYSQL_PASSWORD, etc.)
        """
        # Try direct URL first
        database_url = os.getenv("Database_URL") or os.getenv("DATABASE_URL")
        if database_url:
            # DEVELOPER FIX: Self-heal the URL if it has unencoded special characters in the password
            try:
                from urllib.parse import quote_plus, unquote
                if "://" in database_url and "@" in database_url:
                    prefix, rest = database_url.split("://", 1)
                    auth_part, host_part = rest.rsplit("@", 1)
                    if ":" in auth_part:
                        user, password = auth_part.split(":", 1)
                        # Re-encode to ensure compatibility (unquote handles already encoded chars)
                        clean_password = quote_plus(unquote(password))
                        
                        # DEBUG: Log the intervention (Safe masking)
                        masked_pass = password[:1] + "****" + password[-1:] if len(password) > 2 else "****"
                        print(f"DEBUG: Self-healing DB URL. User: {user}, Pass: {masked_pass}, Host: {host_part}")
                        
                        return f"{prefix}://{user}:{clean_password}@{host_part}"
            except Exception as e:
                print(f"DEBUG: Self-healing failed: {e}")
                pass # Fallback to returning original if parsing fails
            
            print("DEBUG: Using provided DATABASE_URL without modification.")
            return database_url
            
        # Fallback to individual components with encoding
        try:
            from urllib.parse import quote_plus
            user = self.MYSQL_USER
            # Encode password to handle special chars like @
            password = quote_plus(self.MYSQL_PASSWORD)
            host = self.MYSQL_HOST
            port = self.MYSQL_PORT
            db = self.MYSQL_DATABASE
            
            return f"mysql+pymysql://{user}:{password}@{host}:{port}/{db}"
        except Exception as e:
            raise ValueError(f"Could not construct DATABASE_URL: {e}")
    
    class Config:
        env_file = str(_BASE_DIR / ".env")
        case_sensitive = True


import configparser

# ... existing imports ...

def load_settings_from_ini() -> dict:
    """
    Load settings from app-properties.ini based on APP_PROFILE.
    Returns a dictionary of uppercase settings.
    """
    profile = os.getenv("APP_PROFILE", "dev")
    ini_path = _BASE_DIR / "app-properties.ini"
    
    config_values = {}
    
    if ini_path.exists():
        print(f"LOADING CONFIG: Profile '{profile}' from {ini_path}")
        parser = configparser.ConfigParser()
        parser.read(str(ini_path))
        
        if profile in parser:
            section = parser[profile]
            for key, value in section.items():
                # specific type conversions for known bools/ints
                if value.lower() in ['true', 'false']:
                    val = parser.getboolean(profile, key)
                elif value.isdigit():
                    # check if it should be an int (heuristic)
                    # ports and limits are ints, but some numeric strings might be strings
                    # For safety in Pydantic, passing string usually keeps it safe if model expects int (auto-cast)
                    # But explicit int is better for connection args.
                    if "port" in key or "size" in key or "recycle" in key or "rate" in key or "overflow" in key:
                         val = int(value)
                    else:
                         val = value
                else:
                    val = value
                
                config_values[key.upper()] = val
        else:
             print(f"WARNING: Profile '{profile}' not found in app-properties.ini")
    else:
        print(f"WARNING: app-properties.ini not found at {ini_path}")

    return config_values

@lru_cache()
def get_settings() -> Settings:
    """Cached settings instance."""
    # Load INI settings first
    ini_settings = load_settings_from_ini()
    
    # Pass INI settings as kwargs to override defaults/env files
    # Note: Constructor args in Pydantic V2 BaseSettings usually OVERRIDE env vars.
    # If we want Env Vars to win, we might need a different strategy, 
    # but usually "Profile" implies "Set these values".
    # Let's assume Profile > Defaults. 
    # Validating: If I pass kwargs, they override everything. 
    # If user sets MYSQL_HOST in docker run -e, they expect it to win.
    # Pydantic priority: Init settings > Env vars > Secrets > Config file val > Field default
    
    # To defer to explicit Env Vars, we should ONLY set keys that are NOT in os.environ
    #
    # app-properties.ini is shared with other, unrelated services (e.g. its
    # postgres_* keys belong to a different app entirely) — only forward keys
    # that Settings actually declares, so an unrelated section added to the
    # ini file doesn't crash config loading with "Extra inputs are not
    # permitted" for fields this app was never going to read.
    known_fields = set(Settings.model_fields.keys())
    final_settings = {}
    skipped = []
    for k, v in ini_settings.items():
        if k in os.environ:
            continue
        if k not in known_fields:
            skipped.append(k)
            continue
        final_settings[k] = v

    if skipped:
        print(f"IGNORING unrecognized app-properties.ini keys (not used by this service): {skipped}")

    return Settings(**final_settings)


settings = get_settings()

# Refuse the public default JWT secret when deployed (BR-DF-09)
from app.core.secrets_guard import resolve_jwt_secret  # noqa: E402
settings.JWT_SECRET_KEY = resolve_jwt_secret(settings.JWT_SECRET_KEY)
