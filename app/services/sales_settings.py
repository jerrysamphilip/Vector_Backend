"""
Per-workspace sales settings (BRD v2.0 BR-SF-02, 03, 05, 12).

Stored as one JSON document per workspace; missing keys fall back to DEFAULTS,
so new settings never need a migration. Reads and writes go through
``SalesSettingsRepository`` (app/repositories/sales_admin.py); the merge rules and the
amount masking below are pure.

The ``db``-taking functions keep the signature other modules call them with.
"""
import copy
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional

from app.repositories.sales import SqlSession
from app.repositories.sales_admin import SalesSettingsRepository, SqlAlchemySalesSettingsRepository

DEFAULTS = {
    # BR-SF-03: how leads without an owner are assigned
    "lead_assignment": {
        "mode": "off",              # off / round_robin / region
        "users": [],                # round-robin pool (user ids), also the region fallback
        "regions": [],              # [{"field": "country" | "state", "value": "India", "user_id": "..."}]
        "next_index": 0,
    },
    # BR-SF-02: disqualification reasons offered in the UI (free text still allowed)
    "disqualify_reasons": ["No budget", "Not the decision maker", "No need", "Went with a competitor",
                           "Unresponsive", "Bad timing"],
    "default_recycle_days": 90,
    # BR-SF-05: reasons offered when a deal is won or lost; a reason is required to close
    "win_reasons": ["Best product fit", "Price", "Relationship", "Speed of delivery", "Strong champion"],
    "loss_reasons": ["Price", "Lost to competitor", "No decision", "Timing", "Missing features", "Budget cut"],
    "require_close_reason": True,
    "stale_deal_days": 14,
    # BR-SF-12: who must not see amounts and revenue
    "amount_hidden_roles": [],      # e.g. ["AGENT"]
    "amount_hidden_levels": [],     # e.g. [4]
    # BR-SF-17: quotes with a discount above this percent need approval; None = off
    "discount_approval_threshold": None,
}


# ── Pure rules ───────────────────────────────────────────────

def merge_settings(stored: Optional[dict]) -> dict:
    """DEFAULTS overlaid with what the workspace stored (nested dicts merged one level deep)."""
    merged: Dict[str, Any] = copy.deepcopy(DEFAULTS)
    for key, value in (stored or {}).items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


def apply_changes(current: dict, changes: dict) -> dict:
    """``current`` with the known keys of ``changes`` applied (unknown keys are ignored)."""
    for key, value in changes.items():
        if key not in DEFAULTS:
            continue
        if isinstance(DEFAULTS[key], dict) and isinstance(value, dict):
            current[key].update(value)
        else:
            current[key] = value
    return current


def amounts_visible(user: Any, settings: dict) -> bool:
    """Amount and revenue fields are hidden from the roles / levels the workspace lists (BR-SF-12)."""
    if user.role in ("SUPER_ADMIN", "ADMIN"):
        return True
    if user.role in (settings.get("amount_hidden_roles") or []):
        return False
    if user.sales_level and user.sales_level in (settings.get("amount_hidden_levels") or []):
        return False
    return True


# ── Repository-backed ────────────────────────────────────────

def load_settings(repo: SalesSettingsRepository, tenant_id: str) -> dict:
    return merge_settings(repo.stored(tenant_id))


def store_settings(repo: SalesSettingsRepository, tenant_id: str, changes: dict) -> dict:
    """Merge ``changes`` into the stored document and stage it (caller commits)."""
    current = apply_changes(load_settings(repo, tenant_id), changes)
    repo.save(tenant_id, current)
    return current


def amounts_allowed(repo: SalesSettingsRepository, user: Any, settings: Optional[dict] = None) -> bool:
    """can_see_amounts over a repository; the workspace settings are cached on the user object."""
    if user.role in ("SUPER_ADMIN", "ADMIN"):
        return True
    s = settings or getattr(user, "_sales_settings", None) or load_settings(repo, user.tenant_id)
    user._sales_settings = s
    return amounts_visible(user, s)


# ── Session API (signatures other modules use) ───────────────

def get_settings(db: SqlSession, tenant_id: str) -> dict:
    return load_settings(SqlAlchemySalesSettingsRepository(db), tenant_id)


def save_settings(db: SqlSession, tenant_id: str, changes: dict) -> dict:
    return store_settings(SqlAlchemySalesSettingsRepository(db), tenant_id, changes)


def can_see_amounts(db: SqlSession, user: Any, settings: Optional[dict] = None) -> bool:
    """Amount and revenue fields are hidden from the roles / levels the workspace lists (BR-SF-12)."""
    return amounts_allowed(SqlAlchemySalesSettingsRepository(db), user, settings)


MONEY_KEYS = {
    "amount", "weighted_amount", "weighted", "total_amount", "open_amount", "open_pipeline", "weighted_pipeline",
    "won_amount", "lost_amount", "won", "lost", "pipeline", "forecast", "best_case", "average_deal",
    "new_amount", "existing_amount", "annual_revenue", "unit_price", "line_total", "subtotal", "discount_total",
    "target", "gap", "commit", "closed", "revenue", "revenue_per_contact",
    # Derived from amounts, so they reveal them too (forecast categories, % of target)
    "best_case_category", "attainment", "categories",
}


def _is_number(v) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float, Decimal)):
        return True
    if isinstance(v, str):  # amounts serialised as text ("12500.00")
        try:
            Decimal(v.replace(",", "").strip())
            return True
        except (InvalidOperation, ValueError):
            return False
    return False


def _blank_all(data) -> Any:
    """Every number below a money key (e.g. {"categories": {"COMMIT": 120.0}}) is an amount."""
    if isinstance(data, dict):
        return {k: _blank_all(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_blank_all(v) for v in data]
    return None if _is_number(data) else data


def mask_money(data, keys=MONEY_KEYS) -> Any:
    """Blank every money field in an API response, recursively."""
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            if k in keys and isinstance(v, dict):
                out[k] = _blank_all(v)
            elif k in keys and _is_number(v):
                out[k] = None
            else:
                out[k] = mask_money(v, keys)
        return out
    if isinstance(data, list):
        return [mask_money(v, keys) for v in data]
    return data


def masked_with(repo: SalesSettingsRepository, user: Any, data, keys=MONEY_KEYS) -> Any:
    return data if amounts_allowed(repo, user) else mask_money(data, keys)


def masked_for(db: SqlSession, user: Any, data, keys=MONEY_KEYS) -> Any:
    return masked_with(SqlAlchemySalesSettingsRepository(db), user, data, keys)
