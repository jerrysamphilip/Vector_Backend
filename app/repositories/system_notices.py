"""System notices in SQL: the query form of ``app.domain.inbox.is_system_notice``.

The "Delivery Failed" note the SES webhook writes into a thread is an INBOUND email_messages row but not a
reply from the contact. Every count or list of replies done in SQL excludes it with ``reply_message()``;
``not_system_notice()`` keeps every other row (outbound included). The marks match the domain function:
reply_label_source SYSTEM, the MAILER-DAEMON sender, or (notes written before the marker existed) the
note's fixed subject and body wording. Columns are COALESCEd so a NULL never turns the test into NULL.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import ColumnElement, and_, func, not_, or_

from app.domain.inbox import (
    BOUNCE_NOTICE_BODY_PREFIX,
    BOUNCE_NOTICE_SUBJECT_PREFIX,
    SYSTEM_NOTICE_SENDER,
    LabelSource,
)
from app.models.email_message import EmailMessage


def _inbound() -> ColumnElement[bool]:
    return func.coalesce(EmailMessage.direction, "OUTBOUND") == "INBOUND"


def _notice_marks() -> ColumnElement[bool]:
    def text(column: Any) -> Any:
        return func.coalesce(column, "")

    return or_(
        text(EmailMessage.reply_label_source) == LabelSource.SYSTEM.value,
        func.lower(text(EmailMessage.from_email)) == SYSTEM_NOTICE_SENDER.lower(),
        and_(text(EmailMessage.subject).startswith(BOUNCE_NOTICE_SUBJECT_PREFIX, autoescape=True),
             text(EmailMessage.body_text).startswith(BOUNCE_NOTICE_BODY_PREFIX, autoescape=True)),
    )


def system_notice() -> ColumnElement[bool]:
    """True for a system notice row (never NULL)."""
    return and_(_inbound(), _notice_marks())


def not_system_notice() -> ColumnElement[bool]:
    """Every row except system notices (outbound rows included)."""
    return not_(system_notice())


def reply_message() -> ColumnElement[bool]:
    """An inbound reply from the contact: INBOUND and not a system notice."""
    return and_(_inbound(), not_(_notice_marks()))
