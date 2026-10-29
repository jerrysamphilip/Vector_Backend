# app/core/config.py
"""
Application configuration settings.
Loads environment variables for database and app settings.
"""

import configparser
import logging
import os
import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Union
from urllib.parse import quote_plus

from dotenv import dotenv_values
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


_BASE_DIR = Path(__file__).resolve().parents[2]
_ENV_FILE = str(_BASE_DIR / ".env")
# Test runs never read .env (it holds a developer's real OpenAI / AWS keys): the test harness sets this flag
# (tests/conftest.py, tests/unit/conftest.py, scripts/test.py, CI) before app.core.config is imported, and any
# in-process run under pytest is treated the same way. "0" opts a test run back in explicitly.
IGNORE_DOTENV_VAR = "OUTREACH_IGNORE_DOTENV"
LOCAL_ENVIRONMENTS = ("development", "dev", "local", "test")


def dotenv_disabled() -> bool:
    """True when .env must not be read: OUTREACH_IGNORE_DOTENV=1, or (flag unset) running under pytest."""
    flag = os.getenv(IGNORE_DOTENV_VAR, "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    return "pytest" in sys.modules


def settings_env_file() -> Optional[str]:
    """The .env file Settings reads, or None while testing (see dotenv_disabled)."""
    return None if dotenv_disabled() else _ENV_FILE


def _release_version() -> str:
    """Release identity: the VERSION file at the repository root (CI checks it matches CHANGELOG.md)."""
    try:
        return (_BASE_DIR / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0+unknown"
    except OSError:
        return "0.0.0+unknown"


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    # App Settings
    APP_NAME: str = "Outreach AI"
    APP_VERSION: str = _release_version()
    DEBUG: bool = False
    
    # MySQL Database Settings
    MYSQL_HOST: str = os.getenv("MYSQL_HOST", "host.docker.internal")
    MYSQL_PORT: int = int(os.getenv("MYSQL_PORT", "3306"))
    MYSQL_USER: str = os.getenv("MYSQL_USER", "root")
    MYSQL_PASSWORD: str = os.getenv("MYSQL_PASSWORD", "")
    MYSQL_DATABASE: str = os.getenv("MYSQL_DATABASE", "outreach_ai")
    # MySQL TLS (D8; off until DEC-01): disabled | required | verify_ca | verify_identity, and the CA bundle
    # the verify modes check the server certificate against (on RDS: the AWS global bundle, global-bundle.pem)
    MYSQL_SSL_MODE: str = "disabled"
    MYSQL_SSL_CA: str = ""
    
    # Connection Pool Settings
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_RECYCLE: int = 300
    
    # OpenAI Settings
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o-mini"
    # Hard usage caps (app/services/llm_budget.py): calls per UTC day across all processes, tokens per reply
    OPENAI_MAX_USD_PER_DAY: float = 0.20
    OPENAI_PRICE_INPUT_PER_M: float = 0.15    # USD per 1M prompt tokens (gpt-4o-mini)
    OPENAI_PRICE_OUTPUT_PER_M: float = 0.60   # USD per 1M completion tokens (gpt-4o-mini)
    OPENAI_MAX_CALLS_PER_DAY: int = 100
    OPENAI_MAX_OUTPUT_TOKENS: int = 1500
    
    # Default IDs for multi-tenant support
    DEFAULT_TENANT_ID: str = "00000000-0000-0000-0000-000000000001"
    DEFAULT_SYSTEM_USER_ID: str = "00000000-0000-0000-0000-000000000001"
    
    # AWS SES Configuration
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_REGION: str = "us-east-1"
    AWS_SES_CONFIGURATION_SET: str = ""
    AWS_SNS_TOPIC_ARN: str = ""
    # Optional comma-separated allow-list of SNS topics; empty = any topic (on top of AWS_SNS_TOPIC_ARN)
    SNS_ALLOWED_TOPIC_ARNS: str = ""
    AWS_SNS_ENABLED: bool = False
    
    # Email Settings
    SENDER_EMAIL: str = ""
    SENDER_NAME: str = "Outreach AI"
    SES_MAX_SEND_RATE: int = 12  # emails per second
    SES_SANDBOX_MODE: bool = False

    # CAN-SPAM / GDPR compliance
    # Physical mailing address appended to every outbound email footer (CAN-SPAM §5(a)(5))
    COMPANY_PHYSICAL_ADDRESS: str = ""  # unused: each tenant sets its address (Settings > sender identity)
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
    # Google reuses GOOGLE_CLIENT_ID/SECRET (below) with its own redirect, separate from Google sign-in's.
    GOOGLE_SYNC_REDIRECT_URI: str = ""    # https://<host>/vector/api/connections/google/callback
    GRAPH_REDIRECT_URI: str = ""          # https://<host>/vector/api/connections/microsoft/callback
    CONNECTIONS_POST_CONNECT_URL: str = "/vector/app/connections"
    
    # JWT Authentication (validated at start-up, see app/core/secrets_guard.py)
    JWT_SECRET_KEY: str = "change-this-in-production-use-a-long-random-string"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Session limits (ADR 0007, app/services/session_limits.py), enforced when the refresh token is
    # exchanged, so they take effect within one access-token lifetime (ACCESS_TOKEN_EXPIRE_MINUTES).
    # SESSION_ABSOLUTE_HOURS: a sign-in lasts at most this long, however active the user is.
    # SESSION_IDLE_MINUTES: refresh is refused when the user has not interacted for this long
    #   (activity is reported by the SPA in the X-User-Activity header; background polling is not activity).
    # REFRESH_REUSE_GRACE_SECONDS: a token rotated this recently may be presented again without being
    #   treated as theft (two tabs refreshing at once); later reuse revokes the whole session.
    SESSION_ABSOLUTE_HOURS: int = 12
    SESSION_IDLE_MINUTES: int = 30
    REFRESH_REUSE_GRACE_SECONDS: int = 30

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
    # Root log level (read from the environment in app/main.py before settings load)
    LOG_LEVEL: str = "INFO"
    # /docs, /redoc and /openapi.json are disabled in production unless this is true
    ENABLE_DOCS: bool = False
    # Public self-registration (creates a tenant + SUPER_ADMIN) and new-tenant Google sign-up. Off unless
    # explicitly true, in every environment (I-12); the local compose stack and the test runners set it.
    ALLOW_SELF_SIGNUP: Optional[bool] = None
    # Google sign-in may link itself to an EXISTING password account with the same verified email only
    # for these email domains (comma-separated, e.g. "example.com,example.org"). Empty = never auto-link.
    SSO_LINK_ALLOWED_DOMAINS: str = ""
    # Number of reverse proxies in front of the API that append to X-Forwarded-For (rate limiting)
    TRUSTED_PROXY_HOPS: int = 1
    # Test stacks only (app/core/rate_limit.py): multiplies the per-client-IP limits (login attempts,
    # sign-ups, reset/magic-link and two-factor requests, the global ceiling) so the live suites can share
    # one IP. Account lockouts (per email / per email+IP) are unchanged. Refused in production.
    RATE_LIMIT_TEST_MULTIPLIER: int = 1
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

    # Mailbox SMTP/IMAP TLS (app/core/tls.py). Certificates and host names are always verified;
    # false is a local dev/test escape hatch for self-signed fake servers and is refused in production.
    MAIL_TLS_VERIFY: bool = True

    # Warmup engine
    WARMUP_INTERVAL_SECONDS: int = 900
    WARMUP_MAX_SENDS_PER_CYCLE: int = 3
    WARMUP_REPLY_RATE_DEFAULT: int = 35
    WARMUP_RANDOMIZE_VARIANCE: int = 15

    # Schema & background jobs. Migrations run via `python -m app.db.migrate`; AUTO_MIGRATE runs
    # them at API start-up (local convenience). Deployments run the loops in `python -m app.worker`
    # and set RUN_BACKGROUND_JOBS=false on the API.
    AUTO_MIGRATE: bool = False
    RUN_BACKGROUND_JOBS: bool = True

    # Test/staging aid: let the scheduler send on weekends and holidays (the send window HOURS still apply).
    # Ignored when ENVIRONMENT=production.
    SEND_ON_ANY_DAY: bool = False
    # Scheduler retry backoff unit: a temporarily failed send is retried after unit * 2^attempt seconds
    # (default: 2, 4, 8 minutes). Test stacks shorten it; production refuses a value below the default.
    SEND_RETRY_BASE_SECONDS: int = 60
    WORKER_HEALTH_PORT: int = 8002

    # Observability (app/core/observability.py). LOG_FORMAT=json is read from the environment.
    METRICS_ENABLED: bool = True          # GET /metrics (Prometheus)
    METRICS_TOKEN: str = ""               # when set, /metrics requires "Authorization: Bearer <token>"
    SENTRY_DSN: str = ""                  # error reporting is off unless set
    SENTRY_TRACES_SAMPLE_RATE: float = 0.0

    # Browser sessions (app/core/auth_cookies.py). The SPA sends "X-Auth-Mode: cookie" and receives
    # httpOnly access/refresh cookies plus a readable csrf_token cookie instead of tokens in JSON.
    # COOKIE_SECURE unset = Secure cookies in production only. COOKIE_DOMAIN unset = host-only cookies.
    # REFRESH_COOKIE_PATH scopes the refresh cookie (the browser-visible path, e.g. "/vector/api/api/auth").
    COOKIE_SECURE: Optional[bool] = None
    COOKIE_DOMAIN: Optional[str] = None
    REFRESH_COOKIE_PATH: str = "/"

    @property
    def cookie_secure(self) -> bool:
        if self.COOKIE_SECURE is not None:
            return self.COOKIE_SECURE
        return self.is_production

    @property
    def is_production(self) -> bool:
        return (self.ENVIRONMENT or "").strip().lower() in ("production", "prod")

    @property
    def is_local_environment(self) -> bool:
        """A developer machine or a test run: ENVIRONMENT is development / dev / local / test and the
        process is not in Kubernetes. Anything else counts as deployed, where local-only conveniences
        (localhost links, private mail hosts) are refused."""
        if os.getenv("KUBERNETES_SERVICE_HOST"):
            return False
        return (self.ENVIRONMENT or "").strip().lower() in LOCAL_ENVIRONMENTS

    @property
    def self_signup_enabled(self) -> bool:
        return bool(self.ALLOW_SELF_SIGNUP)

    @property
    def sso_link_allowed_domains(self) -> frozenset:
        return frozenset(d.strip().lower().lstrip("@") for d in (self.SSO_LINK_ALLOWED_DOMAINS or "").split(",") if d.strip())

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
                        return f"{prefix}://{user}:{clean_password}@{host_part}"
            except ValueError:
                pass  # Fallback to returning original if parsing fails (never log the URL: it holds the password)

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
        env_file = settings_env_file()  # fixed at import; get_settings() re-checks per call
        case_sensitive = True


# Secrets are never taken from app-properties.ini (a shared, often committed file): they come from the
# environment or .env only. A secret key found in the ini is ignored with a warning (its value is not logged).
INI_SECRET_KEYS = frozenset({
    "JWT_SECRET_KEY", "CREDENTIALS_ENCRYPTION_KEY", "MYSQL_PASSWORD", "OPENAI_API_KEY",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "MS365_CLIENT_SECRET",
    "GOOGLE_CLIENT_SECRET", "SES_WEBHOOK_TOKEN", "METRICS_TOKEN", "SENTRY_DSN", "DATABASE_URL",
})
_SECRET_KEY_PATTERN = re.compile(r"(SECRET|PASSWORD|PASSWD|API_KEY|PRIVATE_KEY|ENCRYPTION_KEY|_TOKEN$|_DSN$)")


def is_secret_key(key: str) -> bool:
    """True for a setting that holds a credential (never read from app-properties.ini)."""
    upper = key.upper()
    return upper in INI_SECRET_KEYS or bool(_SECRET_KEY_PATTERN.search(upper))


def _ini_value(parser: configparser.ConfigParser, profile: str, key: str, value: str) -> Union[bool, int, str]:
    """Type conversions for known bools / ints (ports, sizes, ...); everything else stays a string."""
    if value.lower() in ("true", "false"):
        return parser.getboolean(profile, key)
    if value.isdigit() and any(word in key for word in ("port", "size", "recycle", "rate", "overflow")):
        return int(value)
    return value


def load_settings_from_ini() -> dict:
    """
    Load settings from app-properties.ini based on APP_PROFILE.
    Returns a dictionary of uppercase settings.
    """
    profile = os.getenv("APP_PROFILE", "dev")
    ini_path = _BASE_DIR / "app-properties.ini"
    config_values: Dict[str, Union[bool, int, str]] = {}
    if not ini_path.exists():
        logger.warning("app-properties.ini not found at %s", ini_path)
        return config_values
    logger.info("LOADING CONFIG: Profile '%s' from %s", profile, ini_path)
    parser = configparser.ConfigParser()
    parser.read(str(ini_path))
    if profile not in parser:
        logger.warning("Profile '%s' not found in app-properties.ini", profile)
        return config_values
    for key, value in parser[profile].items():
        config_values[key.upper()] = _ini_value(parser, profile, key, value)
    return config_values


def _dotenv_keys() -> Set[str]:
    """Names (never values) set in the .env file, which outranks app-properties.ini (none while testing)."""
    env_file = settings_env_file()
    if env_file is None:
        return set()
    try:
        return {k for k in dotenv_values(env_file) if k}
    except OSError:
        return set()


def select_ini_overrides(ini_settings: Dict[str, Any], known_fields: Iterable[str], environ_keys: Iterable[str],
                         dotenv_keys: Iterable[str]) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """The app-properties.ini values that apply: precedence is environment > .env > ini > code default.

    Returns (overrides, unknown keys, secret keys). Unknown keys belong to other services sharing the
    file; secret keys are refused (they must come from the environment or .env)."""
    known = set(known_fields)
    outranked = set(environ_keys) | set(dotenv_keys)
    overrides: Dict[str, Any] = {}
    unknown: List[str] = []
    secrets_found: List[str] = []
    for key, value in ini_settings.items():
        if is_secret_key(key):
            secrets_found.append(key)
        elif key in outranked:
            continue
        elif key not in known:
            unknown.append(key)
        else:
            overrides[key] = value
    return overrides, unknown, secrets_found


@lru_cache()
def get_settings() -> Settings:
    """Cached settings instance.

    Pydantic's own order is init kwargs > environment > .env > defaults, and the ini values are passed
    as init kwargs: so only ini keys that neither the environment nor .env sets are passed, which makes
    the effective order environment > .env > app-properties.ini > code default. app-properties.ini is
    shared with other services (e.g. its postgres_* keys), so only keys Settings declares are forwarded.
    """
    final_settings, unknown, secret_keys = select_ini_overrides(
        load_settings_from_ini(), Settings.model_fields.keys(), os.environ.keys(), _dotenv_keys())
    if unknown:
        logger.warning("IGNORING unrecognized app-properties.ini keys (not used by this service): %s", unknown)
    if secret_keys:
        logger.warning("IGNORING secret keys in app-properties.ini (set them in the environment or .env "
                       "instead; values not read): %s", sorted(secret_keys))
    # pydantic-settings' per-instance env file, decided per call (passed in the dict: the mypy plugin hides it)
    init_kwargs: Dict[str, Any] = {**final_settings, "_env_file": settings_env_file()}
    return Settings(**init_kwargs)


settings = get_settings()

# I-11 fail closed: a deployed process (Kubernetes, see secrets_guard.is_deployed) must say which
# environment it is. Without it the code default ("development") would quietly switch off every
# production-only safeguard (docs off, self sign-up off, Secure cookies, TLS verification guard, ...).
if os.getenv("KUBERNETES_SERVICE_HOST") and (
        "ENVIRONMENT" not in settings.model_fields_set or not (settings.ENVIRONMENT or "").strip()):
    raise RuntimeError(
        "ENVIRONMENT is not set in a deployed (Kubernetes) context. Set ENVIRONMENT explicitly "
        "(production, staging, ...) in the deployment secret; it no longer defaults to development there."
    )

# Refuse the public default JWT secret when deployed (BR-DF-09)
from app.core.secrets_guard import resolve_jwt_secret  # noqa: E402
settings.JWT_SECRET_KEY = resolve_jwt_secret(settings.JWT_SECRET_KEY)

# Mailbox passwords must never cross an unverified TLS connection in production
if not settings.MAIL_TLS_VERIFY and settings.is_production:
    raise RuntimeError("MAIL_TLS_VERIFY=false is for local development only and is refused in production.")


def refused_test_settings(s: "Settings") -> List[str]:
    """Test-stack switches that production refuses (start-up fails with these messages)."""
    if not s.is_production:
        return []
    errors = []
    if (s.RATE_LIMIT_TEST_MULTIPLIER or 1) != 1:
        errors.append("RATE_LIMIT_TEST_MULTIPLIER relaxes sign-in and sign-up rate limits; it is for test stacks "
                      "only and is refused in production (unset it or set it to 1).")
    if (s.SEND_RETRY_BASE_SECONDS or 0) < 60:
        errors.append("SEND_RETRY_BASE_SECONDS below 60 retries failed sends within seconds; it is for test stacks "
                      "only and is refused in production (unset it or use 60 or more).")
    return errors


_refused_test_settings = refused_test_settings(settings)
if _refused_test_settings:
    raise RuntimeError(" ".join(_refused_test_settings))



def database_tls_warning(s: "Settings") -> Optional[str]:
    """The start-up warning for unencrypted database traffic in production (D8), or None. A warning, not a
    refusal: TLS stays off until DEC-01 decides it."""
    if s.is_production and (s.MYSQL_SSL_MODE or "disabled").strip().lower() == "disabled":
        return ("MYSQL_SSL_MODE=disabled in production: database connections are not encrypted. Set "
                "MYSQL_SSL_MODE=verify_identity and MYSQL_SSL_CA to the RDS CA bundle once DEC-01 is decided.")
    return None


_db_tls_warning = database_tls_warning(settings)
if _db_tls_warning:
    logger.warning(_db_tls_warning)

# Credentialed CORS with a wildcard origin lets any site act as the signed-in user
if settings.is_production and any(o.strip() == "*" or "*" in o
                                  for o in (settings.FRONTEND_URL or "").split(",") if o.strip()):
    raise RuntimeError("FRONTEND_URL contains a wildcard origin; list the exact frontend origin(s) in production.")

# /metrics exposes tenant activity counters: production requires a scrape token (or METRICS_ENABLED=false)
if settings.is_production and settings.METRICS_ENABLED and not (settings.METRICS_TOKEN or "").strip():
    raise RuntimeError("METRICS_TOKEN is required in production while METRICS_ENABLED is true "
                       "(set a long random value in the deployment secret, or METRICS_ENABLED=false).")
