"""
Proposal (quote) PDF layout (BRD v2.0 BR-SF-15), generated in-process with app/utils/pdf.py.

Pure presentation: ``render_quote_pdf`` turns the loaded quote (``QuotePdfData``) into PDF bytes.
"""
from dataclasses import dataclass
from datetime import date
from typing import Any, List, Optional

from app.core.config import settings
from app.utils.pdf import Document
from app.core.ids import short_id

W, H = 595.28, 841.89
LEFT, RIGHT = 50, W - 50
GREY = (0.45, 0.48, 0.55)
BLUE = (0.18, 0.42, 0.75)
COLUMNS = [(LEFT, "Item", "left"), (340, "Qty", "right"), (420, "Unit price", "right"), (470, "Disc.", "right"),
           (RIGHT, "Total", "right")]


@dataclass
class QuotePdfData:
    proposal: Any
    quote: dict
    company: Any = None
    contact: Any = None
    tenant: Any = None
    owner: Any = None

    @property
    def filename(self) -> str:
        name = "".join(c if c.isalnum() or c in " -_" else "_" for c in self.proposal.title)[:80].strip()
        return f"{name or 'proposal'}.pdf"


def format_money(v: float) -> str:
    return f"{'-' if v < 0 else ''}{settings.QUOTE_CURRENCY} {abs(v):,.2f}"


def _header(page: Any, data: QuotePdfData) -> None:
    p = data.proposal
    page.rect(0, H - 8, W, 8, fill=BLUE)
    page.text(LEFT, H - 60, getattr(data.tenant, "tenant_name", None) or "Proposal", 18, bold=True)
    page.text(RIGHT, H - 60, "QUOTE", 18, bold=True, color=BLUE, align="right")
    page.text(RIGHT, H - 78, f"Ref {short_id(p.proposal_id).upper()}", 9, color=GREY, align="right")
    page.text(RIGHT, H - 91, f"Date {date.today():%d %b %Y}", 9, color=GREY, align="right")
    if p.valid_until:
        page.text(RIGHT, H - 104, f"Valid until {p.valid_until:%d %b %Y}", 9, color=GREY, align="right")


def _recipient_lines(data: QuotePdfData) -> List[str]:
    contact, company = data.contact, data.company
    company_name = company.name if company else (contact.company_name if contact else None)
    return [line for line in (contact.full_name if contact else None, company_name,
                              contact.email if contact else None) if line]


def _parties(page: Any, data: QuotePdfData) -> float:
    """Prepared for / prepared by blocks; returns the y below them."""
    y = H - 130
    page.text(LEFT, y, "Prepared for", 9, bold=True, color=GREY)
    y -= 15
    for line in _recipient_lines(data):
        page.text(LEFT, y, line, 10)
        y -= 14
    py = H - 130
    page.text(320, py, "Prepared by", 9, bold=True, color=GREY)
    if data.owner:
        page.text(320, py - 15, f"{data.owner.first_name} {data.owner.last_name}".strip(), 10)
        page.text(320, py - 29, data.owner.email, 10)
    return min(y, py - 45) - 20


def _table_lines(data: QuotePdfData) -> List[dict]:
    quote = data.quote
    return quote["lines"] or [{"description": data.proposal.title, "quantity": 1, "unit_price": quote["amount"] or 0,
                               "discount_pct": 0, "line_total": quote["amount"] or 0}]


def _line_row(page: Any, y: float, line: dict) -> float:
    y_after = page.wrap(LEFT, y, line["description"], 270, 10, 13, color=(0.1, 0.12, 0.16))
    page.text(340, y, f"{line['quantity']:g}", 10, align="right")
    page.text(420, y, f"{line['unit_price']:,.2f}", 10, align="right")
    page.text(470, y, f"{line['discount_pct']:g}%" if line["discount_pct"] else "-", 10, align="right")
    page.text(RIGHT, y, f"{line['line_total']:,.2f}", 10, align="right")
    y = min(y_after + 13, y) - 8
    page.line(LEFT, y, RIGHT, y)
    return y - 16


def _table(doc: Any, page: Any, y: float, data: QuotePdfData) -> tuple:
    page.rect(LEFT - 6, y - 6, RIGHT - LEFT + 12, 20)
    for x, label, align in COLUMNS:
        page.text(x, y, label, 9, bold=True, color=(0.35, 0.38, 0.45), align=align)
    y -= 22
    for line in _table_lines(data):
        if y < 140:
            page = doc.add_page()
            y = H - 60
        y = _line_row(page, y, line)
    return page, y


def _totals(page: Any, y: float, quote: dict) -> float:
    for label, value, bold in (("Subtotal", quote["subtotal"] or quote["amount"] or 0, False),
                               ("Discount", -(quote["discount_total"] or 0), False),
                               ("Total", quote["amount"] or 0, True)):
        if label == "Discount" and not quote["discount_total"]:
            continue
        page.text(420, y, label, 11 if bold else 10, bold=bold, align="right")
        page.text(RIGHT, y, format_money(value), 11 if bold else 10, bold=bold, align="right")
        y -= 17
    return y


def render_quote_pdf(data: QuotePdfData) -> bytes:
    p = data.proposal
    doc = Document(title=p.title)
    page = doc.add_page()
    _header(page, data)
    y = _parties(page, data)
    page.text(LEFT, y, p.title, 13, bold=True)
    page, y = _table(doc, page, y - 26, data)
    y = _totals(page, y - 8, data.quote)
    if p.notes:
        y -= 14
        page.text(LEFT, y, "Notes", 9, bold=True, color=GREY)
        page.wrap(LEFT, y - 15, p.notes, RIGHT - LEFT, 10, 14)
    tenant_name: Optional[str] = getattr(data.tenant, "tenant_name", "") or ""
    page.text(LEFT, 40, f"{tenant_name} - {p.title}", 8, color=(0.6, 0.62, 0.66))
    return doc.render()
