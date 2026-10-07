# app/routers/quotes_router.py
"""
Products, quote lines on proposals, and the proposal PDF (BRD v2.0 BR-SF-15).

A proposal with lines takes its amount from them: quantity x unit price, less
each line's discount. The PDF is generated in-process (app/utils/pdf.py).
"""
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import require_role
from app.core.database import get_db
from app.models.account import Account
from app.models.prospect import Prospect
from app.models.sales import Opportunity, Proposal
from app.models.sales_extra import Product, ProposalLine
from app.models.user import User
from app.services import sales as svc
from app.services.contact_service import clean_str
from app.services.sales_settings import can_see_amounts, masked_for

router = APIRouter(tags=["Quotes"])
tenant_user = require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")
admin_user = require_role("SUPER_ADMIN", "ADMIN")
CENT = Decimal("0.01")


# ── Products ─────────────────────────────────────────────────

class ProductWrite(BaseModel):
    name: Optional[str] = None
    sku: Optional[str] = None
    description: Optional[str] = None
    unit_price: Optional[Any] = None
    unit: Optional[str] = None
    active: Optional[bool] = None


def _product_dict(p: Product) -> dict:
    return {"product_id": p.product_id, "name": p.name, "sku": p.sku, "description": p.description,
            "unit_price": svc.as_float(p.unit_price), "unit": p.unit, "active": p.active}


@router.get("/products")
def list_products(include_inactive: bool = False, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    q = db.query(Product).filter(Product.tenant_id == current_user.tenant_id)
    if not include_inactive:
        q = q.filter(Product.active.is_(True))
    return masked_for(db, current_user, [_product_dict(p) for p in q.order_by(Product.name)])


@router.post("/products", status_code=201)
def create_product(payload: ProductWrite, db: Session = Depends(get_db), current_user: User = Depends(admin_user)):
    if not clean_str(payload.name):
        raise HTTPException(status_code=400, detail="Give the product a name")
    p = Product(tenant_id=current_user.tenant_id, name=clean_str(payload.name), sku=clean_str(payload.sku),
                description=payload.description, unit_price=svc.money(payload.unit_price) or Decimal("0"),
                unit=clean_str(payload.unit))
    db.add(p)
    db.commit()
    return _product_dict(p)


@router.patch("/products/{product_id}")
def update_product(product_id: str, payload: ProductWrite, db: Session = Depends(get_db),
                   current_user: User = Depends(admin_user)):
    p = db.query(Product).filter(Product.product_id == product_id, Product.tenant_id == current_user.tenant_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Product not found")
    data = payload.model_dump(exclude_unset=True)
    if "name" in data:
        p.name = clean_str(data["name"]) or p.name
    for f in ("sku", "unit"):
        if f in data:
            setattr(p, f, clean_str(data[f]))
    if "description" in data:
        p.description = data["description"]
    if "unit_price" in data:
        p.unit_price = svc.money(data["unit_price"]) or Decimal("0")
    if "active" in data:
        p.active = bool(data["active"])
    db.commit()
    return _product_dict(p)


# ── Quote lines ──────────────────────────────────────────────

class LineIn(BaseModel):
    product_id: Optional[str] = None
    description: Optional[str] = None
    quantity: Any = 1
    unit_price: Optional[Any] = None
    discount_pct: Any = 0


class LinesIn(BaseModel):
    lines: List[LineIn]


def _line_total(line: ProposalLine) -> Decimal:
    gross = Decimal(line.quantity) * Decimal(line.unit_price)
    return (gross * (Decimal(100) - Decimal(line.discount_pct)) / Decimal(100)).quantize(CENT, ROUND_HALF_UP)


def _line_dict(line: ProposalLine) -> dict:
    return {"line_id": line.line_id, "product_id": line.product_id, "description": line.description,
            "quantity": float(line.quantity), "unit_price": svc.as_float(line.unit_price),
            "discount_pct": float(line.discount_pct), "line_total": float(_line_total(line))}


def _proposal(db: Session, user: User, proposal_id: str) -> Proposal:
    p = db.query(Proposal).filter(Proposal.proposal_id == proposal_id, Proposal.tenant_id == user.tenant_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Proposal not found")
    svc.get_opp(db, user, p.opportunity_id)  # visibility through the deal
    return p


def _quote(db: Session, p: Proposal) -> dict:
    lines = db.query(ProposalLine).filter(ProposalLine.proposal_id == p.proposal_id).order_by(ProposalLine.sort_order).all()
    gross = sum((Decimal(l.quantity) * Decimal(l.unit_price) for l in lines), Decimal(0)).quantize(CENT)
    total = sum((_line_total(l) for l in lines), Decimal(0))
    return {"proposal_id": p.proposal_id, "title": p.title, "status": p.status, "lines": [_line_dict(l) for l in lines],
            "subtotal": float(gross), "discount_total": float(gross - total), "amount": float(total) if lines else svc.as_float(p.amount),
            "valid_until": p.valid_until, "notes": p.notes}


@router.get("/proposals/{proposal_id}/lines")
def get_lines(proposal_id: str, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    return masked_for(db, current_user, _quote(db, _proposal(db, current_user, proposal_id)))


@router.put("/proposals/{proposal_id}/lines")
def set_lines(proposal_id: str, payload: LinesIn, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    """Replace the quote lines; the proposal amount becomes their total."""
    if not can_see_amounts(db, current_user):
        raise HTTPException(status_code=403, detail="Your role cannot see or change amounts")
    p = _proposal(db, current_user, proposal_id)
    products = {x.product_id: x for x in db.query(Product).filter(Product.tenant_id == current_user.tenant_id)}
    db.query(ProposalLine).filter(ProposalLine.proposal_id == p.proposal_id).delete(synchronize_session=False)
    for i, line in enumerate(payload.lines):
        product = products.get(line.product_id) if line.product_id else None
        if line.product_id and not product:
            raise HTTPException(status_code=400, detail="Unknown product")
        description = clean_str(line.description) or (product.name if product else None)
        if not description:
            raise HTTPException(status_code=400, detail=f"Line {i + 1} needs a description or a product")
        try:
            qty = Decimal(str(line.quantity))
            disc = Decimal(str(line.discount_pct or 0))
        except Exception:
            raise HTTPException(status_code=400, detail=f"Line {i + 1}: quantity and discount must be numbers")
        if qty <= 0 or not (0 <= disc <= 100):
            raise HTTPException(status_code=400, detail=f"Line {i + 1}: quantity must be positive and discount 0-100%")
        price = svc.money(line.unit_price) if line.unit_price not in (None, "") else (product.unit_price if product else None)
        if price is None:
            raise HTTPException(status_code=400, detail=f"Line {i + 1} needs a unit price")
        db.add(ProposalLine(proposal_id=p.proposal_id, product_id=product.product_id if product else None,
                            description=description[:500], quantity=qty, unit_price=price, discount_pct=disc, sort_order=i))
    db.flush()
    quote = _quote(db, p)
    if payload.lines:
        p.amount = Decimal(str(quote["amount"]))
    db.commit()
    return quote


# ── PDF ──────────────────────────────────────────────────────

@router.get("/proposals/{proposal_id}/pdf")
def proposal_pdf(proposal_id: str, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    from app.models.tenant import Tenant
    from app.utils.pdf import Document
    if not can_see_amounts(db, current_user):
        raise HTTPException(status_code=403, detail="Your role cannot see amounts")
    p = _proposal(db, current_user, proposal_id)
    opp = db.query(Opportunity).filter(Opportunity.opportunity_id == p.opportunity_id).first()
    company = db.query(Account).filter(Account.account_id == opp.account_id).first() if opp.account_id else None
    contact = db.query(Prospect).filter(Prospect.prospect_id == opp.prospect_id).first() if opp.prospect_id else None
    tenant = db.query(Tenant).filter(Tenant.tenant_id == current_user.tenant_id).first()
    owner = db.query(User).filter(User.user_id == opp.owner_id).first()
    quote = _quote(db, p)
    money = lambda v: f"{'-' if v < 0 else ''}{svc.settings.QUOTE_CURRENCY} {abs(v):,.2f}"

    doc = Document(title=p.title)
    page = doc.add_page()
    W, H = 595.28, 841.89
    left, right = 50, W - 50
    page.rect(0, H - 8, W, 8, fill=(0.18, 0.42, 0.75))
    page.text(left, H - 60, getattr(tenant, "tenant_name", None) or "Proposal", 18, bold=True)
    page.text(right, H - 60, "QUOTE", 18, bold=True, color=(0.18, 0.42, 0.75), align="right")
    page.text(right, H - 78, f"Ref {p.proposal_id[:8].upper()}", 9, color=(0.45, 0.48, 0.55), align="right")
    page.text(right, H - 91, f"Date {date.today():%d %b %Y}", 9, color=(0.45, 0.48, 0.55), align="right")
    if p.valid_until:
        page.text(right, H - 104, f"Valid until {p.valid_until:%d %b %Y}", 9, color=(0.45, 0.48, 0.55), align="right")

    y = H - 130
    page.text(left, y, "Prepared for", 9, bold=True, color=(0.45, 0.48, 0.55))
    y -= 15
    for line in filter(None, [contact.full_name if contact else None, company.name if company else (contact.company_name if contact else None),
                              contact.email if contact else None]):
        page.text(left, y, line, 10)
        y -= 14
    py = H - 130
    page.text(320, py, "Prepared by", 9, bold=True, color=(0.45, 0.48, 0.55))
    if owner:
        page.text(320, py - 15, f"{owner.first_name} {owner.last_name}".strip(), 10)
        page.text(320, py - 29, owner.email, 10)

    y = min(y, py - 45) - 20
    page.text(left, y, p.title, 13, bold=True)
    y -= 26

    cols = [(left, "Item", "left"), (340, "Qty", "right"), (420, "Unit price", "right"), (470, "Disc.", "right"), (right, "Total", "right")]
    page.rect(left - 6, y - 6, right - left + 12, 20)
    for x, label, align in cols:
        page.text(x if align == "left" else x, y, label, 9, bold=True, color=(0.35, 0.38, 0.45), align=align)
    y -= 22
    lines = quote["lines"] or [{"description": p.title, "quantity": 1, "unit_price": quote["amount"] or 0,
                                "discount_pct": 0, "line_total": quote["amount"] or 0}]
    for line in lines:
        if y < 140:
            page = doc.add_page()
            y = H - 60
        y_after = page.wrap(left, y, line["description"], 270, 10, 13, color=(0.1, 0.12, 0.16))
        page.text(340, y, f"{line['quantity']:g}", 10, align="right")
        page.text(420, y, f"{line['unit_price']:,.2f}", 10, align="right")
        page.text(470, y, f"{line['discount_pct']:g}%" if line["discount_pct"] else "-", 10, align="right")
        page.text(right, y, f"{line['line_total']:,.2f}", 10, align="right")
        y = min(y_after + 13, y) - 8
        page.line(left, y, right, y)
        y -= 16
    y -= 8
    for label, value, bold in (("Subtotal", quote["subtotal"] or quote["amount"] or 0, False),
                               ("Discount", -(quote["discount_total"] or 0), False),
                               ("Total", quote["amount"] or 0, True)):
        if label == "Discount" and not quote["discount_total"]:
            continue
        page.text(420, y, label, 11 if bold else 10, bold=bold, align="right")
        page.text(right, y, money(value), 11 if bold else 10, bold=bold, align="right")
        y -= 17
    if p.notes:
        y -= 14
        page.text(left, y, "Notes", 9, bold=True, color=(0.45, 0.48, 0.55))
        page.wrap(left, y - 15, p.notes, right - left, 10, 14)
    page.text(left, 40, f"{getattr(tenant, 'tenant_name', '') or ''} - {p.title}", 8, color=(0.6, 0.62, 0.66))

    filename = "".join(c if c.isalnum() or c in " -_" else "_" for c in p.title)[:80].strip() or "proposal"
    return Response(content=doc.render(), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{filename}.pdf"'})
