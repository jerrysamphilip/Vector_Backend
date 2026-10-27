"""
Quote repository (ADR 0009, chunk A3): products, quote lines on proposals and the discount-approval
queries (BR-SF-15, BR-SF-17). Tenant-scoped; never commits.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, Optional, Protocol

from app.models.sales import Opportunity, Proposal
from app.models.sales_extra import Product, ProposalLine
from app.models.tenant import Tenant
from app.models.user import User
from app.repositories.base import TenantId
from app.repositories.sales_common import SessionRepository

LineTuple = tuple[Any, Any, Any]
"""(quantity, unit_price, discount_pct) of a quote line."""


class QuoteRepository(Protocol):
    def products(self, tenant_id: TenantId, include_inactive: bool) -> list[Product]: ...
    def product(self, tenant_id: TenantId, product_id: str) -> Optional[Product]: ...
    def product_map(self, tenant_id: TenantId) -> dict[str, Product]: ...
    def lines(self, proposal_id: str) -> list[ProposalLine]: ...
    def line_tuples(self, proposal_id: str) -> list[LineTuple]: ...
    def line_tuples_for(self, proposal_ids: Sequence[str]) -> dict[str, list[LineTuple]]: ...
    def delete_lines(self, proposal_id: str) -> None: ...
    def deal_owner_id(self, opportunity_id: str) -> Optional[str]: ...
    def managers(self, tenant_id: TenantId) -> dict[str, Optional[str]]: ...
    def active_user_ids(self, tenant_id: TenantId) -> set[str]: ...
    def active_users_with_roles(self, tenant_id: TenantId, roles: Sequence[str]) -> list[str]: ...
    def approvals(self, tenant_id: TenantId, statuses: Sequence[str], *, involving: Optional[str] = None,
                  owner_ids: Optional[Iterable[str]] = None, limit: int = 200) -> list[tuple[Proposal, Opportunity]]: ...
    def tenant(self, tenant_id: TenantId) -> Optional[Tenant]: ...
    def user(self, user_id: str) -> Optional[User]: ...
    def add(self, entity: Any) -> Any: ...
    def flush(self) -> None: ...


class SqlAlchemyQuoteRepository(SessionRepository):
    def products(self, tenant_id: TenantId, include_inactive: bool) -> list[Product]:
        q = self._session.query(Product).filter(Product.tenant_id == tenant_id)
        if not include_inactive:
            q = q.filter(Product.active.is_(True))
        return q.order_by(Product.name).all()

    def product(self, tenant_id: TenantId, product_id: str) -> Optional[Product]:
        return self._session.query(Product).filter(Product.product_id == product_id,
                                                   Product.tenant_id == tenant_id).first()

    def product_map(self, tenant_id: TenantId) -> dict[str, Product]:
        return {x.product_id: x for x in self._session.query(Product).filter(Product.tenant_id == tenant_id)}

    def lines(self, proposal_id: str) -> list[ProposalLine]:
        return self._session.query(ProposalLine).filter(ProposalLine.proposal_id == proposal_id) \
            .order_by(ProposalLine.sort_order).all()

    def line_tuples(self, proposal_id: str) -> list[LineTuple]:
        rows = self._session.query(ProposalLine.quantity, ProposalLine.unit_price, ProposalLine.discount_pct) \
            .filter(ProposalLine.proposal_id == proposal_id).all()
        return [(q, p, d) for q, p, d in rows]

    def line_tuples_for(self, proposal_ids: Sequence[str]) -> dict[str, list[LineTuple]]:
        out: dict[str, list[LineTuple]] = {}
        if not proposal_ids:
            return out
        for line in self._session.query(ProposalLine).filter(ProposalLine.proposal_id.in_(list(proposal_ids))):
            out.setdefault(line.proposal_id, []).append((line.quantity, line.unit_price, line.discount_pct))
        return out

    def delete_lines(self, proposal_id: str) -> None:
        self._session.query(ProposalLine).filter(ProposalLine.proposal_id == proposal_id) \
            .delete(synchronize_session=False)

    def deal_owner_id(self, opportunity_id: str) -> Optional[str]:
        return self._session.query(Opportunity.owner_id).filter(Opportunity.opportunity_id == opportunity_id).scalar()

    def managers(self, tenant_id: TenantId) -> dict[str, Optional[str]]:
        return {u: m for u, m in self._session.query(User.user_id, User.manager_id)
                .filter(User.tenant_id == tenant_id).all()}

    def active_user_ids(self, tenant_id: TenantId) -> set[str]:
        return {u for (u,) in self._session.query(User.user_id).filter(User.tenant_id == tenant_id,
                                                                       User.status == "ACTIVE")}

    def active_users_with_roles(self, tenant_id: TenantId, roles: Sequence[str]) -> list[str]:
        return [u for (u,) in self._session.query(User.user_id).filter(
            User.tenant_id == tenant_id, User.role.in_(list(roles)), User.status == "ACTIVE")]

    def approvals(self, tenant_id: TenantId, statuses: Sequence[str], *, involving: Optional[str] = None,
                  owner_ids: Optional[Iterable[str]] = None, limit: int = 200) -> list[tuple[Proposal, Opportunity]]:
        """Quotes in the given approval states with their deals, newest request first.

        ``involving``: only the user's own quotes (deal owner or requester). ``owner_ids``: only deals of
        these owners (an empty set matches nothing)."""
        q = self._session.query(Proposal, Opportunity).join(
            Opportunity, Opportunity.opportunity_id == Proposal.opportunity_id).filter(Proposal.tenant_id == tenant_id)
        q = q.filter(Proposal.approval_status == statuses[0]) if len(statuses) == 1 else \
            q.filter(Proposal.approval_status.in_(list(statuses)))
        if involving is not None:
            q = q.filter((Opportunity.owner_id == involving) | (Proposal.approval_requested_by == involving))
        elif owner_ids is not None:
            owners = set(owner_ids)
            q = q.filter(Opportunity.owner_id.in_(owners)) if owners else q.filter(Proposal.proposal_id.is_(None))
        return [(p, o) for p, o in q.order_by(Proposal.approval_requested_at.desc()).limit(limit).all()]

    def tenant(self, tenant_id: TenantId) -> Optional[Tenant]:
        return self._session.query(Tenant).filter(Tenant.tenant_id == tenant_id).first()

    def user(self, user_id: str) -> Optional[User]:
        return self._session.query(User).filter(User.user_id == user_id).first()
