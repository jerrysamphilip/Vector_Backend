# app/routers/quotes_router.py
"""
Products, quote lines on proposals, and the proposal PDF (BRD v2.0 BR-SF-15).

A proposal with lines takes its amount from them: quantity x unit price, less
each line's discount. The PDF is generated in-process (app/utils/pdf.py).

Discounts above the workspace threshold need approval before the quote goes
out (BR-SF-17): /proposals/{id}/approval*, /quotes/approvals and
/quotes/approval-settings; the rules live in app/services/quote_approval.py.

Thin routes (ADR 0009): each calls one QuoteService method (app/services/quotes.py).
"""
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from app.core.auth import require_role
from app.core.concurrency import if_match_version
from app.core.database import get_db
from app.domain.sales import ApprovalQuery
from app.schemas.sales_schema import ApprovalSettingsIn, DecisionIn, LinesIn, ProductWrite, SubmitIn
from app.services.quotes import QuoteService
from app.services.sales import entity_id
from app.utils.http_files import content_disposition

router = APIRouter(tags=["Quotes"])
tenant_user = require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")
admin_user = require_role("SUPER_ADMIN", "ADMIN")
PROPOSAL_MISSING = "Proposal not found"


def quote_service(db=Depends(get_db)) -> QuoteService:
    return QuoteService.for_session(db)


# ── Products ─────────────────────────────────────────────────

@router.get("/products")
def list_products(include_inactive: bool = False, service: QuoteService = Depends(quote_service),
                  current_user: Any = Depends(tenant_user)):
    return service.products(current_user, include_inactive)


@router.post("/products", status_code=201)
def create_product(payload: ProductWrite, service: QuoteService = Depends(quote_service),
                   current_user: Any = Depends(admin_user)):
    return service.create_product(current_user, payload.model_dump())


@router.patch("/products/{product_id}")
def update_product(product_id: str, payload: ProductWrite, service: QuoteService = Depends(quote_service),
                   current_user: Any = Depends(admin_user)):
    return service.update_product(current_user, entity_id(product_id, "Product not found"),
                                  payload.model_dump(exclude_unset=True))


# ── Quote lines ──────────────────────────────────────────────

@router.get("/proposals/{proposal_id}/lines")
def get_lines(proposal_id: str, service: QuoteService = Depends(quote_service),
              current_user: Any = Depends(tenant_user)):
    return service.lines(current_user, entity_id(proposal_id, PROPOSAL_MISSING))


@router.put("/proposals/{proposal_id}/lines")
def set_lines(proposal_id: str, payload: LinesIn, service: QuoteService = Depends(quote_service),
              current_user: Any = Depends(tenant_user), if_match: Optional[int] = Depends(if_match_version)):
    """Replace the quote lines; the proposal amount becomes their total."""
    return service.set_lines(current_user, proposal_id,
                             [line.model_dump() for line in payload.lines], payload.expected_version, if_match)


# ── Discount approval (BR-SF-17) ─────────────────────────────

@router.get("/quotes/approval-settings")
def get_approval_settings(service: QuoteService = Depends(quote_service), current_user: Any = Depends(tenant_user)):
    return service.approval_settings(current_user)


@router.put("/quotes/approval-settings")
def put_approval_settings(payload: ApprovalSettingsIn, service: QuoteService = Depends(quote_service),
                          current_user: Any = Depends(admin_user)):
    return service.set_approval_settings(current_user, payload.discount_approval_threshold)


@router.get("/proposals/{proposal_id}/approval")
def get_approval(proposal_id: str, service: QuoteService = Depends(quote_service),
                 current_user: Any = Depends(tenant_user)):
    return service.approval(current_user, entity_id(proposal_id, PROPOSAL_MISSING))


@router.post("/proposals/{proposal_id}/approval/submit")
def submit_approval(proposal_id: str, payload: Optional[SubmitIn] = None, service: QuoteService = Depends(quote_service),
                    current_user: Any = Depends(tenant_user), if_match: Optional[int] = Depends(if_match_version)):
    return service.submit(current_user, entity_id(proposal_id, PROPOSAL_MISSING), payload.note if payload else None,
                          payload.expected_version if payload else None, if_match)


@router.post("/proposals/{proposal_id}/approval/approve")
def approve_discount(proposal_id: str, payload: Optional[DecisionIn] = None,
                     service: QuoteService = Depends(quote_service), current_user: Any = Depends(tenant_user),
                     if_match: Optional[int] = Depends(if_match_version)):
    return service.decide(current_user, entity_id(proposal_id, PROPOSAL_MISSING), True,
                          payload.reason if payload else None, payload.expected_version if payload else None, if_match)


@router.post("/proposals/{proposal_id}/approval/reject")
def reject_discount(proposal_id: str, payload: DecisionIn, service: QuoteService = Depends(quote_service),
                    current_user: Any = Depends(tenant_user), if_match: Optional[int] = Depends(if_match_version)):
    return service.decide(current_user, entity_id(proposal_id, PROPOSAL_MISSING), False, payload.reason,
                          payload.expected_version, if_match)


@router.get("/quotes/approvals")
def list_approvals(scope: Literal["mine", "requested"] = "mine",
                   status: Literal["PENDING", "APPROVED", "REJECTED", "ALL"] = "PENDING",
                   limit: int = Query(200, ge=1, le=500),
                   service: QuoteService = Depends(quote_service), current_user: Any = Depends(tenant_user)):
    """mine: quotes awaiting (or decided by) my approval; requested: my own quotes that needed approval."""
    return service.approvals(current_user, ApprovalQuery(scope, status, limit))


# ── PDF ──────────────────────────────────────────────────────

@router.get("/proposals/{proposal_id}/pdf")
def proposal_pdf(proposal_id: str, service: QuoteService = Depends(quote_service),
                 current_user: Any = Depends(tenant_user)):
    filename, pdf = service.pdf(current_user, proposal_id)
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": content_disposition(filename)})
