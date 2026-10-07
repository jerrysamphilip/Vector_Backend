"""
Per-workspace sales settings (BRD v2.0 BR-SF-02, 03, 05, 12).

Stored as one JSON document per workspace; missing keys fall back to DEFAULTS,
so new settings never need a migration.
"""
import copy
from typing import Optional

from sqlalchemy.orm import Session

from app.models.sales_extra import SalesSetting
from app.models.user import User

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
}


def get_settings(db: Session, tenant_id: str) -> dict:
    row = db.query(SalesSetting).filter(SalesSetting.tenant_id == tenant_id).first()
    merged = copy.deepcopy(DEFAULTS)
    if row and row.settings:
        for key, value in row.settings.items():
            if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
                merged[key].update(value)
            else:
                merged[key] = value
    return merged


def save_settings(db: Session, tenant_id: str, changes: dict) -> dict:
    current = get_settings(db, tenant_id)
    for key, value in changes.items():
        if key not in DEFAULTS:
            continue
        if isinstance(DEFAULTS[key], dict) and isinstance(value, dict):
            current[key].update(value)
        else:
            current[key] = value
    row = db.query(SalesSetting).filter(SalesSetting.tenant_id == tenant_id).first()
    if row:
        row.settings = current
    else:
        db.add(SalesSetting(tenant_id=tenant_id, settings=current))
    db.flush()
    return current


def can_see_amounts(db: Session, user: User, settings: Optional[dict] = None) -> bool:
    """Amount and revenue fields are hidden from the roles / levels the workspace lists (BR-SF-12)."""
    if user.role in ("SUPER_ADMIN", "ADMIN"):
        return True
    s = settings or getattr(user, "_sales_settings", None) or get_settings(db, user.tenant_id)
    user._sales_settings = s
    if user.role in (s.get("amount_hidden_roles") or []):
        return False
    if user.sales_level and user.sales_level in (s.get("amount_hidden_levels") or []):
        return False
    return True


MONEY_KEYS = {
    "amount", "weighted_amount", "weighted", "total_amount", "open_amount", "open_pipeline", "weighted_pipeline",
    "won_amount", "lost_amount", "won", "lost", "pipeline", "forecast", "best_case", "average_deal",
    "new_amount", "existing_amount", "annual_revenue", "unit_price", "line_total", "subtotal", "discount_total",
    "target", "gap", "commit", "closed", "revenue", "revenue_per_contact",
}


def mask_money(data, keys=MONEY_KEYS):
    """Blank every money field in an API response, recursively."""
    if isinstance(data, dict):
        return {k: (None if k in keys and isinstance(v, (int, float)) and not isinstance(v, bool)
                    else mask_money(v, keys)) for k, v in data.items()}
    if isinstance(data, list):
        return [mask_money(v, keys) for v in data]
    return data


def masked_for(db: Session, user: User, data, keys=MONEY_KEYS):
    return data if can_see_amounts(db, user) else mask_money(data, keys)
