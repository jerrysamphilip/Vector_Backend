"""
Quote discount approval (BRD v2.0 BR-SF-17).

A workspace may set a discount threshold (percent, sales settings key
"discount_approval_threshold"; None = off). A quote (proposal with lines) whose
effective discount is above it cannot be sent, put under review or accepted
until it is approved:

  effective discount = the larger of the biggest line discount and the overall
                       discount of the quote ((subtotal - total) / subtotal)

The quote owner (the deal owner, or whoever created the proposal) submits it;
anyone in the owner's manager chain, or an ADMIN / SUPER_ADMIN, approves or
rejects it with a reason. Nobody decides on their own quote, except the
SUPER_ADMIN (nobody is above them). Raising the discount above what was
approved puts the quote back to PENDING; bringing it within the threshold
clears a PENDING / REJECTED request.

The pure helpers at the top have no database access (unit-tested in
tests/unit/test_discount_approval_rules.py). ``QuoteApproval`` applies them over the quote
repository (app/repositories/sales_quotes.py); it never commits.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.core.clock import utcnow
from app.core.errors import BadRequestError, ConflictError, PermissionDenied
from app.services.sales_collaborators import SalesCollaborators
from app.services.sales_settings import load_settings

APPROVAL_STATUSES = ("NOT_REQUIRED", "PENDING", "APPROVED", "REJECTED")
GATED_STATUSES = ("SENT", "UNDER_REVIEW", "ACCEPTED")   # proposal statuses that need a cleared discount
DECIDER_ROLES = ("SUPER_ADMIN", "ADMIN")
SETTING_KEY = "discount_approval_threshold"
PCT = Decimal("0.01")


# ── Pure rules ───────────────────────────────────────────────

def parse_threshold(value) -> Optional[float]:
    """Validate a threshold from the API: None / "" turns approval off, else a percent 0-100."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise BadRequestError("discount_approval_threshold must be a percent between 0 and 100")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise BadRequestError("discount_approval_threshold must be a percent between 0 and 100")
    if not number.is_finite() or number < 0 or number > 100:
        raise BadRequestError("discount_approval_threshold must be a percent between 0 and 100")
    return float(number.quantize(PCT, ROUND_HALF_UP))


def effective_discount(lines: Iterable[Tuple[object, object, object]]) -> Decimal:
    """Lines are (quantity, unit_price, discount_pct). The larger of the top line discount and the overall one."""
    top, gross, net = Decimal(0), Decimal(0), Decimal(0)
    for qty, price, disc in lines:
        qty, price, disc = Decimal(str(qty or 0)), Decimal(str(price or 0)), Decimal(str(disc or 0))
        top = max(top, disc)
        gross += qty * price
        net += qty * price * (Decimal(100) - disc) / Decimal(100)
    overall = (gross - net) * Decimal(100) / gross if gross > 0 else Decimal(0)
    return max(top, overall).quantize(PCT, ROUND_HALF_UP)


def approval_required(effective, threshold) -> bool:
    return threshold is not None and Decimal(str(effective)) > Decimal(str(threshold))


def is_cleared(effective, threshold, approval_status: str, approved_pct) -> bool:
    """May the quote go out? Yes when no approval is needed, or an approval covers this discount."""
    if not approval_required(effective, threshold):
        return True
    return (approval_status == "APPROVED" and approved_pct is not None
            and Decimal(str(effective)) <= Decimal(str(approved_pct)))


def status_after_edit(approval_status: str, effective, threshold, approved_pct) -> str:
    """Approval status once the quote lines changed."""
    if not approval_required(effective, threshold):
        return "NOT_REQUIRED" if approval_status in ("PENDING", "REJECTED") else approval_status
    if approval_status == "APPROVED" and (approved_pct is None or Decimal(str(effective)) > Decimal(str(approved_pct))):
        return "PENDING"
    return approval_status


def manager_chain(managers: Dict[str, Optional[str]], user_id: Optional[str]) -> List[str]:
    """user's manager, their manager, ... (cycle-safe). managers maps user_id -> manager_id."""
    chain, seen, current = [], {user_id}, managers.get(user_id) if user_id else None
    while current and current not in seen:
        chain.append(current)
        seen.add(current)
        current = managers.get(current)
    return chain


def may_decide(role: str, user_id: str, owner_id: Optional[str], requested_by: Optional[str],
               chain: List[str]) -> bool:
    if user_id in (owner_id, requested_by) and role != "SUPER_ADMIN":
        return False
    return role in DECIDER_ROLES or user_id in chain


def _pct(value) -> str:
    return f"{float(value):g}"


def can_submit(user, p, owner_id: Optional[str]) -> bool:
    return user.user_id in (owner_id, p.created_by) or user.role in DECIDER_ROLES


# ── Over the repositories ────────────────────────────────────

_LOOK_UP: Any = ...   # "look it up" marker for QuoteApproval.state


def _waiting_hint(approval_status: Optional[str]) -> str:
    if approval_status == "PENDING":
        return " It is waiting for approval."
    if approval_status == "REJECTED":
        return " It was rejected; change the discount or submit it again."
    return " Submit it for approval first."


def _full_name(user) -> str:
    return f"{user.first_name} {user.last_name}".strip()


class QuoteApproval:
    """Discount approval of one workspace's quotes (BR-SF-17) over the sales repositories."""

    def __init__(self, repos: Any, collab: SalesCollaborators) -> None:
        self.repos = repos
        self.collab = collab

    @property
    def _quotes(self) -> Any:
        return self.repos.quotes

    def threshold_for(self, tenant_id: str) -> Optional[float]:
        value = load_settings(self.repos.settings, tenant_id).get(SETTING_KEY)
        try:
            return parse_threshold(value)
        except BadRequestError:
            return None

    def quote_discount(self, proposal_id: str) -> Decimal:
        return effective_discount(self._quotes.line_tuples(proposal_id))

    def owner_id(self, p) -> Optional[str]:
        return self._quotes.deal_owner_id(p.opportunity_id)

    def can_decide(self, user, p, owner_id: Optional[str] = None) -> bool:
        owner_id = owner_id if owner_id is not None else self.owner_id(p)
        chain = manager_chain(self._quotes.managers(user.tenant_id), owner_id) if user.role not in DECIDER_ROLES else []
        return may_decide(user.role, user.user_id, owner_id, p.approval_requested_by, chain)

    def approver_ids(self, tenant_id: str, owner_id: Optional[str], exclude: Iterable[Optional[str]] = ()) -> List[str]:
        """Who is told about a request: the owner's manager chain, or the workspace admins when there is none."""
        active = self._quotes.active_user_ids(tenant_id)
        skip = {x for x in exclude if x}
        ids = [u for u in manager_chain(self._quotes.managers(tenant_id), owner_id) if u in active and u not in skip]
        if not ids:
            ids = [u for u in self._quotes.active_users_with_roles(tenant_id, DECIDER_ROLES) if u not in skip]
        return ids

    def state(self, p, user=None, threshold: Any = _LOOK_UP, effective: Optional[Decimal] = None,
              owner_id: Any = _LOOK_UP) -> dict:
        """Approval block returned with a quote."""
        threshold = self.threshold_for(p.tenant_id) if threshold is _LOOK_UP else threshold
        effective = self.quote_discount(p.proposal_id) if effective is None else effective
        owner_id = self.owner_id(p) if owner_id is _LOOK_UP else owner_id
        names = self.repos.lookups.user_names([p.approval_requested_by, p.approval_decided_by])
        required = approval_required(effective, threshold)
        result = {
            "approval_status": p.approval_status or "NOT_REQUIRED",
            "approval_required": required,
            "approval_threshold": threshold,
            "effective_discount_pct": float(effective),
            "approved_discount_pct": float(p.approval_discount_pct) if p.approval_discount_pct is not None else None,
            "can_send": is_cleared(effective, threshold, p.approval_status, p.approval_discount_pct),
            "approval_requested_by": p.approval_requested_by,
            "approval_requested_by_name": names.get(p.approval_requested_by),
            "approval_requested_at": p.approval_requested_at,
            "approval_decided_by": p.approval_decided_by,
            "approval_decided_by_name": names.get(p.approval_decided_by),
            "approval_decided_at": p.approval_decided_at,
            "approval_reason": p.approval_reason,
        }
        if user is not None:
            result["can_submit"] = (required and not result["can_send"] and p.approval_status != "PENDING"
                                    and can_submit(user, p, owner_id))
            result["can_decide"] = p.approval_status == "PENDING" and self.can_decide(user, p, owner_id)
        return result

    def ensure_can_send(self, p, new_status: Optional[str]) -> None:
        """Refuse moving a quote to SENT / UNDER_REVIEW / ACCEPTED while its discount is not approved."""
        if not new_status or new_status == p.status or new_status not in GATED_STATUSES:
            return
        threshold = self.threshold_for(p.tenant_id)
        if threshold is None:
            return
        effective = self.quote_discount(p.proposal_id)
        if is_cleared(effective, threshold, p.approval_status, p.approval_discount_pct):
            return
        raise ConflictError((
            f"This quote has a {_pct(effective)}% discount, above the {_pct(threshold)}% approval limit, "
            f"so it cannot be sent or accepted until a manager approves it.{_waiting_hint(p.approval_status)}"))

    def _history(self, user, p, old: str, new: str) -> None:
        self.collab.record_changes(p.tenant_id, "DEAL", p.opportunity_id, {"quote_approval": f"{p.title}: {old}"},
                                   {"quote_approval": f"{p.title}: {new}"}, user.user_id, "UI")

    def _request(self, user, p, effective: Decimal, owner_id: Optional[str], note: Optional[str], why: str) -> None:
        old = p.approval_status.lower()
        p.approval_status = "PENDING"
        p.approval_discount_pct = effective
        p.approval_requested_by = user.user_id
        p.approval_requested_at = utcnow()
        p.approval_decided_by = None
        p.approval_decided_at = None
        p.approval_reason = None
        self._history(user, p, old, f"pending approval ({_pct(effective)}% discount){' - ' + note if note else ''}")
        body = _full_name(user) + f" {why} \"{p.title}\" with a {_pct(effective)}% discount."
        if note:
            body += f"\n\nNote: {note}"
        self.collab.notify(p.tenant_id, self.approver_ids(p.tenant_id, owner_id, exclude=[user.user_id]),
                           "QUOTE_APPROVAL", f"Discount approval needed: {p.title}", body, "/app/pipeline?tab=approvals")

    def submit(self, user, p, note: Optional[str] = None) -> None:
        owner_id = self.owner_id(p)
        if not can_submit(user, p, owner_id):
            raise PermissionDenied("Only the quote owner can submit it for approval")
        threshold = self.threshold_for(p.tenant_id)
        effective = self.quote_discount(p.proposal_id)
        if not approval_required(effective, threshold):
            raise BadRequestError("This quote's discount is within the limit; it needs no approval")
        if p.approval_status == "PENDING":
            raise ConflictError("This quote is already waiting for approval")
        if is_cleared(effective, threshold, p.approval_status, p.approval_discount_pct):
            raise ConflictError("This quote's discount is already approved")
        self._request(user, p, effective, owner_id, note, "asks you to approve")

    def decide(self, user, p, approve: bool, reason: Optional[str]) -> None:
        owner_id = self.owner_id(p)
        if p.approval_status != "PENDING":
            raise ConflictError("This quote is not waiting for approval")
        if not self.can_decide(user, p, owner_id):
            raise PermissionDenied("Only the owner's managers or an admin can decide on this discount")
        if not approve and not reason:
            raise BadRequestError("Give a reason for rejecting the discount")
        effective = self.quote_discount(p.proposal_id)
        p.approval_status = "APPROVED" if approve else "REJECTED"
        if approve:
            p.approval_discount_pct = effective
        p.approval_decided_by = user.user_id
        p.approval_decided_at = utcnow()
        p.approval_reason = reason
        verb = "approved" if approve else "rejected"
        self._history(user, p, "pending approval",
                      f"{verb} ({_pct(effective)}% discount){' - ' + reason if reason else ''}")
        self.collab.notify(p.tenant_id, [u for u in (owner_id, p.approval_requested_by) if u != user.user_id],
                           "QUOTE_DECISION", f"Discount {verb}: {p.title}",
                           f"{_full_name(user)} {verb} the {_pct(effective)}% discount on \"{p.title}\"."
                           + (f"\n\nReason: {reason}" if reason else ""),
                           f"/app/deals/{p.opportunity_id}")

    def after_lines_changed(self, user, p) -> None:
        """Re-check the approval once the quote lines were replaced (call after flush, before commit)."""
        threshold = self.threshold_for(p.tenant_id)
        effective = self.quote_discount(p.proposal_id)
        new = status_after_edit(p.approval_status or "NOT_REQUIRED", effective, threshold, p.approval_discount_pct)
        if new == p.approval_status:
            if new == "PENDING":
                p.approval_discount_pct = effective  # approvers see the discount being asked for now
            return
        if new == "PENDING":  # raised above what was approved: back to the approvers
            self._request(user, p, effective, self.owner_id(p), None, "raised the discount on the approved quote")
            return
        self._history(user, p, p.approval_status.lower(), "approval not required (discount within the limit)")
        p.approval_status = new
        p.approval_discount_pct = None
        p.approval_reason = None
