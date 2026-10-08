# app/db/seed_guard.py
"""
Seed and demo scripts create users with known passwords and fake records. They must never
run against a real workspace, so each one calls require_dev_environment() first.
"""
import os

DEV_ENVIRONMENTS = {"development", "dev", "local"}
DEV_PROFILES = {"dev", "development", "local"}


def is_dev_environment() -> bool:
    from app.core.config import settings
    environment = (settings.ENVIRONMENT or "").strip().lower()
    profile = (os.getenv("APP_PROFILE", "dev") or "").strip().lower()
    return environment in DEV_ENVIRONMENTS and profile in DEV_PROFILES and not settings.is_production


def require_dev_environment(script: str) -> None:
    """Refuse to run outside development / local (ENVIRONMENT and APP_PROFILE)."""
    if not is_dev_environment():
        from app.core.config import settings
        raise SystemExit(
            f"{script} only runs in development or local environments "
            f"(ENVIRONMENT={settings.ENVIRONMENT!r}, APP_PROFILE={os.getenv('APP_PROFILE', 'dev')!r}). Refusing to seed.")


def demo_password() -> str:
    """Password for seeded demo users: DEMO_PASSWORD from the environment; a fixed default only in dev."""
    value = (os.getenv("DEMO_PASSWORD") or "").strip()
    if value:
        return value
    if not is_dev_environment():
        raise SystemExit("Set DEMO_PASSWORD to seed demo users")
    return "demo-pass-123"
