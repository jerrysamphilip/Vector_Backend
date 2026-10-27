"""
Products, quote lines on proposals, discount approval and the proposal PDF (BRD v2.0 BR-SF-15/17).

A proposal with lines takes its amount from them: quantity x unit price, less each line's discount.
``QuoteService`` is what app/routers/quotes_router.py calls: one method per route, one unit of work
per call. Approval rules live in app/services/quote_approval.py, the PDF layout in
app/services/quote_pdf.py, SQL in app/repositories/sales_quotes.py.
"""
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, List, Optional
from uuid import UUID

from app.core.concurrency import resolve_expected
from app.core.errors import BadRequestError, NotFoundError, PermissionDenied
from app.domain.sales import ADMIN_ROLES, ApprovalQuery
from app.models.sales_extra import Product, ProposalLine
from app.services import quote_approval as qa
from app.services.contact_service import clean_str
from app.services.quote_pdf import QuotePdfData, render_quote_pdf
from app.services.sales import SalesRules, SalesServiceBase, as_float, entity_id, money
from app.services.sales_settings import amounts_allowed, masked_with, store_settings

CENT = Decimal("0.01")
NO_AMOUNTS = "Your role cannot see or change amounts"


# ── Pure helpers ─────────────────────────────────────────────

def product_dict(p: Any) -> dict:
    return {"product_id": p.product_id, "name": p.name, "sku": p.sku, "description": p.description,
            "unit_price": as_float(p.unit_price), "unit": p.unit, "active": p.active}


def line_total(line: Any) -> Decimal:
    gross = Decimal(line.quantity) * Decimal(line.unit_price)
    return (gross * (Decimal(100) - Decimal(line.discount_pct)) / Decimal(100)).quantize(CENT, ROUND_HALF_UP)


def line_dict(line: Any) -> dict:
    return {"line_id": line.line_id, "product_id": line.product_id, "description": line.description,
            "quantity": float(line.quantity), "unit_price": as_float(line.unit_price),
            "discount_pct": float(line.discount_pct), "line_total": float(line_total(line))}


def quote_dict(p: Any, lines: List[Any]) -> dict:
    gross = sum((Decimal(line.quantity) * Decimal(line.unit_price) for line in lines), Decimal(0)).quantize(CENT)
    total = sum((line_total(line) for line in lines), Decimal(0))
    return {"proposal_id": p.proposal_id, "row_version": p.row_version, "title": p.title, "status": p.status,
            "lines": [line_dict(line) for line in lines],
            "subtotal": float(gross), "discount_total": float(gross - total),
            "amount": float(total) if lines else as_float(p.amount),
            "valid_until": p.valid_until, "notes": p.notes}


def parse_line(i: int, line: dict, product: Any) -> tuple:
    """(description, quantity, unit_price, discount) of quote line ``i`` (0-based), or BadRequestError."""
    description = clean_str(line.get("description")) or (product.name if product else None)
    if not description:
        raise BadRequestError(f"Line {i + 1} needs a description or a product")
    try:
        qty = Decimal(str(line.get("quantity")))
        disc = Decimal(str(line.get("discount_pct") or 0))
    except Exception:
        raise BadRequestError(f"Line {i + 1}: quantity and discount must be numbers")
    if qty <= 0 or not (0 <= disc <= 100):
        raise BadRequestError(f"Line {i + 1}: quantity must be positive and discount 0-100%")
    raw_price = line.get("unit_price")
    price = money(raw_price) if raw_price not in (None, "") else (product.unit_price if product else None)
    if price is None:
        raise BadRequestError(f"Line {i + 1} needs a unit price")
    return description, qty, price, disc


def approval_result(approval: qa.QuoteApproval, user: Any, p: Any) -> dict:
    return {"proposal_id": p.proposal_id, "row_version": p.row_version, "opportunity_id": p.opportunity_id,
            "title": p.title, "status": p.status, **approval.state(p, user)}


class QuoteService(SalesServiceBase):

    # ── products ────────────────────────────────────────────
    def products(self, user: Any, include_inactive: bool) -> Any:
        with self.work() as rules:
            rows = rules.repos.quotes.products(user.tenant_id, include_inactive)
            return masked_with(rules.repos.settings, user, [product_dict(p) for p in rows])

    def create_product(self, user: Any, data: dict) -> dict:
        with self.work() as rules:
            if not clean_str(data.get("name")):
                raise BadRequestError("Give the product a name")
            p = Product(tenant_id=user.tenant_id, name=clean_str(data.get("name")), sku=clean_str(data.get("sku")),
                        description=data.get("description"), unit_price=money(data.get("unit_price")) or Decimal("0"),
                        unit=clean_str(data.get("unit")))
            rules.repos.quotes.add(p)
            self.commit()
            return product_dict(p)

    def update_product(self, user: Any, product_id: UUID | str, data: dict) -> dict:
        product_id = str(product_id)
        with self.work() as rules:
            p = rules.repos.quotes.product(user.tenant_id, product_id)
            if not p:
                raise NotFoundError("Product not found")
            if "name" in data:
                p.name = clean_str(data["name"]) or p.name
            for f in ("sku", "unit"):
                if f in data:
                    setattr(p, f, clean_str(data[f]))
            if "description" in data:
                p.description = data["description"]
            if "unit_price" in data:
                p.unit_price = money(data["unit_price"]) or Decimal("0")
            if "active" in data:
                p.active = bool(data["active"])
            self.commit()
            return product_dict(p)

    # ── quote lines ─────────────────────────────────────────
    @staticmethod
    def _proposal(rules: SalesRules, user: Any, proposal_id: str) -> Any:
        p = rules.repos.proposals.get(user.tenant_id, proposal_id)
        if not p:
            raise NotFoundError("Proposal not found")
        rules.get_opp(user, p.opportunity_id)  # visibility through the deal
        return p

    def lines(self, user: Any, proposal_id: UUID | str) -> dict:
        proposal_id = str(proposal_id)
        with self.work() as rules:
            p = self._proposal(rules, user, proposal_id)
            approval = qa.QuoteApproval(rules.repos, rules.collab)
            quote = quote_dict(p, rules.repos.quotes.lines(p.proposal_id))
            return masked_with(rules.repos.settings, user, {**quote, "approval": approval.state(p, user)})

    def set_lines(self, user: Any, proposal_id: str, lines: List[dict], expected: Optional[int],
                  if_match: Optional[int]) -> dict:
        """Replace the quote lines; the proposal amount becomes their total.
        ``proposal_id`` is the raw path value: the amount check answers before an unknown id does."""
        with self.work() as rules:
            if not amounts_allowed(rules.repos.settings, user):
                raise PermissionDenied(NO_AMOUNTS)
            proposal_id = str(entity_id(proposal_id, "Proposal not found"))
            p = self._proposal(rules, user, proposal_id)
            rules.repos.proposals.claim_version(p, resolve_expected(expected, if_match), "proposal")  # T-3
            quotes = rules.repos.quotes
            products = quotes.product_map(user.tenant_id)
            quotes.delete_lines(p.proposal_id)
            for i, line in enumerate(lines):
                product = products.get(line["product_id"]) if line.get("product_id") else None
                if line.get("product_id") and not product:
                    raise BadRequestError("Unknown product")
                description, qty, price, disc = parse_line(i, line, product)
                quotes.add(ProposalLine(proposal_id=p.proposal_id, product_id=product.product_id if product else None,
                                        description=description[:500], quantity=qty, unit_price=price,
                                        discount_pct=disc, sort_order=i))
            quotes.flush()
            approval = qa.QuoteApproval(rules.repos, rules.collab)
            approval.after_lines_changed(user, p)  # BR-SF-17: a raised discount goes back for approval
            quote = quote_dict(p, quotes.lines(p.proposal_id))
            if lines:
                p.amount = Decimal(str(quote["amount"]))
            quote["approval"] = approval.state(p, user)
            self.commit()
            return quote

    # ── discount approval (BR-SF-17) ────────────────────────
    def approval_settings(self, user: Any) -> dict:
        with self.work() as rules:
            return {"discount_approval_threshold": qa.QuoteApproval(rules.repos, rules.collab)
                    .threshold_for(user.tenant_id), "can_edit": user.role in ADMIN_ROLES}

    def set_approval_settings(self, user: Any, value: Optional[float]) -> dict:
        with self.work() as rules:
            threshold = qa.parse_threshold(value)
            store_settings(rules.repos.settings, user.tenant_id, {qa.SETTING_KEY: threshold})
            self.commit()
            return {"discount_approval_threshold": threshold, "can_edit": True}

    @staticmethod
    def _approval_proposal(rules: SalesRules, approval: qa.QuoteApproval, user: Any, proposal_id: str) -> Any:
        """A quote the user can see, or one they may decide on (approvers need not see every deal)."""
        p = rules.repos.proposals.get(user.tenant_id, proposal_id)
        if not p:
            raise NotFoundError("Proposal not found")
        if p.approval_status == "PENDING" and approval.can_decide(user, p):
            return p
        rules.get_opp(user, p.opportunity_id)
        return p

    def approval(self, user: Any, proposal_id: UUID | str) -> dict:
        proposal_id = str(proposal_id)
        with self.work() as rules:
            approval = qa.QuoteApproval(rules.repos, rules.collab)
            return approval_result(approval, user, self._approval_proposal(rules, approval, user, proposal_id))

    def submit(self, user: Any, proposal_id: UUID | str, note: Optional[str], expected: Optional[int],
               if_match: Optional[int]) -> dict:
        proposal_id = str(proposal_id)
        with self.work() as rules:
            approval = qa.QuoteApproval(rules.repos, rules.collab)
            p = self._proposal(rules, user, proposal_id)
            rules.repos.proposals.claim_version(p, resolve_expected(expected, if_match), "proposal")
            approval.submit(user, p, clean_str(note))
            self.commit()
            return approval_result(approval, user, p)

    def decide(self, user: Any, proposal_id: UUID | str, approve: bool, reason: Optional[str], expected: Optional[int],
               if_match: Optional[int]) -> dict:
        proposal_id = str(proposal_id)
        with self.work() as rules:
            approval = qa.QuoteApproval(rules.repos, rules.collab)
            p = self._approval_proposal(rules, approval, user, proposal_id)
            rules.repos.proposals.claim_version(p, resolve_expected(expected, if_match), "proposal")  # T-3
            approval.decide(user, p, approve, clean_str(reason))
            self.commit()
            return approval_result(approval, user, p)

    def approvals(self, user: Any, query: ApprovalQuery) -> dict:
        """mine: quotes awaiting (or decided by) my approval; requested: my own quotes that needed approval."""
        with self.work() as rules:
            approval = qa.QuoteApproval(rules.repos, rules.collab)
            rows = self._approval_rows(rules, user, query)
            threshold = approval.threshold_for(user.tenant_id)
            lines = rules.repos.quotes.line_tuples_for([p.proposal_id for p, _ in rows]) if rows else {}
            companies = rules.repos.lookups.account_names({o.account_id for _, o in rows if o.account_id}) \
                if rows else {}
            owners = rules.user_names([o.owner_id for _, o in rows])
            items = []
            for p, o in rows:
                effective = qa.effective_discount(lines.get(p.proposal_id, []))
                items.append({"proposal_id": p.proposal_id, "row_version": p.row_version, "title": p.title,
                              "status": p.status, "amount": as_float(p.amount),
                              "opportunity_id": o.opportunity_id, "opportunity_name": o.name,
                              "company_name": companies.get(o.account_id), "owner_id": o.owner_id,
                              "owner_name": owners.get(o.owner_id),
                              **approval.state(p, user, threshold=threshold, effective=effective, owner_id=o.owner_id)})
            return masked_with(rules.repos.settings, user, {"items": items, "total": len(items), "threshold": threshold})

    @staticmethod
    def _approval_rows(rules: SalesRules, user: Any, query: ApprovalQuery) -> list:
        quotes = rules.repos.quotes
        if query.scope == "requested":
            return quotes.approvals(user.tenant_id, query.statuses, involving=user.user_id, limit=query.limit)
        owner_ids = None
        if user.role not in qa.DECIDER_ROLES:
            owner_ids = rules.collab.team_user_ids(user) - {user.user_id}  # people whose manager chain includes me
        rows = quotes.approvals(user.tenant_id, query.statuses, owner_ids=owner_ids, limit=query.limit)
        return [(p, o) for p, o in rows if user.role == "SUPER_ADMIN"
                or user.user_id not in (o.owner_id, p.approval_requested_by)]

    # ── PDF ─────────────────────────────────────────────────
    def pdf(self, user: Any, proposal_id: str) -> tuple:
        """(filename, PDF bytes) of the quote."""
        with self.work() as rules:
            if not amounts_allowed(rules.repos.settings, user):
                raise PermissionDenied("Your role cannot see amounts")
            proposal_id = str(entity_id(proposal_id, "Proposal not found"))  # raw path value: 403 comes first
            p = self._proposal(rules, user, proposal_id)
            opp = rules.repos.opportunities.get(user.tenant_id, p.opportunity_id)
            lookups = rules.repos.lookups
            data = QuotePdfData(
                proposal=p, quote=quote_dict(p, rules.repos.quotes.lines(p.proposal_id)),
                company=lookups.account(opp.account_id) if opp.account_id else None,
                contact=lookups.prospect(opp.prospect_id) if opp.prospect_id else None,
                tenant=rules.repos.quotes.tenant(user.tenant_id), owner=rules.repos.quotes.user(opp.owner_id))
            return data.filename, render_quote_pdf(data)
