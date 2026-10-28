"""
SQL for the unified inbox (S-10 / F-13, ADR 0009): one page of conversations with search, campaign
and label filters, ordered by their latest message; the latest message, campaign and reply label of
each conversation on the page (one query each, never one per conversation); a thread with the
activity of its emails; and the writes of a manual reply. Every call is tenant-scoped and honours the
owner/team visibility scope (``OwnerScope``). Never commits: the service owns the unit of work.

Ordering uses the newest message time, computed from email_messages: conversations.last_message_at is
not maintained when the scheduler sends into an existing conversation, and its ``onupdate`` bumps it
whenever the row changes (marking it read), so it is only the fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import ColumnElement, and_, desc, exists, func, or_, select
from sqlalchemy.orm import Session

from app.domain.inbox import ConversationQuery, OwnerScope, is_system_notice
from app.models.campaign import Campaign, CampaignProspect
from app.models.conversation import Conversation
from app.models.email_message import EmailEvent, EmailMessage
from app.models.prospect import Prospect
from app.models.sending_inbox import SendingInbox
from app.repositories.base import SqlAlchemyUnitOfWork
from app.repositories.system_notices import reply_message
from app.utils import sql_like


@dataclass(frozen=True)
class ConversationRow:
    id: str
    prospect_id: str
    first_name: Optional[str]
    last_name: Optional[str]
    prospect_email: str
    inbox_email: Optional[str]
    subject: Optional[str]
    status: Optional[str]
    is_unread: bool
    latest_at: Optional[datetime]


@dataclass(frozen=True)
class LastMessage:
    conversation_id: str
    message_id: str
    direction: Optional[str]
    body_text: Optional[str]
    status: Optional[str]
    final_status: Optional[str]
    reply_label: Optional[str]
    is_system: bool = False  # a system notice (app.domain.inbox.is_system_notice), not a reply


def _message_time() -> ColumnElement[Any]:
    return func.coalesce(EmailMessage.sent_at, EmailMessage.scheduled_at)


def _scope_clause(scope: OwnerScope) -> Optional[ColumnElement[bool]]:
    if scope.owner_ids is None:
        return None
    clause: ColumnElement[bool] = Prospect.owner_id.in_(sorted(scope.owner_ids))
    if scope.include_unowned:
        clause = or_(clause, Prospect.owner_id.is_(None))
    return clause


def _search_clause(term: str) -> ColumnElement[bool]:
    full_name = func.concat(func.coalesce(Prospect.first_name, ""), " ", func.coalesce(Prospect.last_name, ""))
    return or_(
        sql_like.ilike(Prospect.first_name, term),
        sql_like.ilike(Prospect.last_name, term),
        sql_like.ilike(full_name, term),
        sql_like.ilike(Prospect.email, term),
        sql_like.ilike(Conversation.subject, term),
    )


class ConversationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    # ── Listing ────────────────────────────────────────────────────
    def page(self, tenant_id: str, query: ConversationQuery, scope: OwnerScope) -> Tuple[List[ConversationRow], int]:
        latest = (
            select(EmailMessage.conversation_id.label("cid"), func.max(_message_time()).label("latest_at"))
            .join(Conversation, Conversation.id == EmailMessage.conversation_id)
            .where(Conversation.tenant_id == tenant_id)
            .group_by(EmailMessage.conversation_id)
            .subquery()
        )
        q = (
            self._session.query(
                Conversation.id, Conversation.prospect_id, Prospect.first_name, Prospect.last_name, Prospect.email,
                SendingInbox.email_address, Conversation.subject, Conversation.status, Conversation.is_unread,
                func.coalesce(latest.c.latest_at, Conversation.last_message_at).label("latest_at"),
            )
            .join(latest, latest.c.cid == Conversation.id)  # inner join: only conversations with messages
            .join(Prospect, Prospect.prospect_id == Conversation.prospect_id)
            .outerjoin(SendingInbox, SendingInbox.inbox_id == Conversation.inbox_id)
            .filter(Conversation.tenant_id == tenant_id, Prospect.tenant_id == tenant_id)
        )
        scope_clause = _scope_clause(scope)
        if scope_clause is not None:
            q = q.filter(scope_clause)
        if query.search:
            q = q.filter(_search_clause(query.search))
        if query.campaign_id:
            q = q.filter(exists().where(EmailMessage.conversation_id == Conversation.id,
                                        EmailMessage.campaign_id == query.campaign_id))
        if query.label is not None:
            q = q.filter(exists().where(EmailMessage.conversation_id == Conversation.id,
                                        EmailMessage.direction == "INBOUND",
                                        EmailMessage.reply_label == query.label.value))
        total = q.order_by(None).count()
        rows = (
            q.order_by(desc(func.coalesce(latest.c.latest_at, Conversation.last_message_at)), desc(Conversation.id))
            .offset(query.paging.offset).limit(query.paging.limit).all()
        )
        return [ConversationRow(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], bool(r[8]), r[9]) for r in rows], total

    def _ranked(self, conversation_ids: Sequence[str], *criteria: ColumnElement[bool]) -> Any:
        """Messages of these conversations ranked newest first per conversation (rn = 1 is the newest).
        On a time tie (second precision) the reply wins: it necessarily came after the send."""
        rn = func.row_number().over(
            partition_by=EmailMessage.conversation_id,
            order_by=(desc(_message_time()), func.coalesce(EmailMessage.direction, "OUTBOUND").asc(),
                      desc(EmailMessage.message_id)),
        ).label("rn")
        return (
            select(EmailMessage.message_id, EmailMessage.conversation_id, EmailMessage.direction,
                   EmailMessage.body_text, EmailMessage.status, EmailMessage.final_status,
                   EmailMessage.reply_label, EmailMessage.campaign_id, EmailMessage.from_email,
                   EmailMessage.subject, EmailMessage.reply_label_source, rn)
            .where(EmailMessage.conversation_id.in_(list(conversation_ids)), *criteria)
            .subquery()
        )

    def last_messages(self, conversation_ids: Sequence[str]) -> Dict[str, LastMessage]:
        """The newest message of each conversation (snippet, direction, delivery state): one query."""
        if not conversation_ids:
            return {}
        ranked = self._ranked(conversation_ids)
        rows = self._session.execute(select(ranked).where(ranked.c.rn == 1)).all()
        return {r.conversation_id: LastMessage(r.conversation_id, r.message_id, r.direction, r.body_text,
                                               r.status, r.final_status, r.reply_label, is_system_notice(r))
                for r in rows}

    def latest_campaigns(self, conversation_ids: Sequence[str]) -> Dict[str, Tuple[str, str]]:
        """conversation id -> (campaign id, name) of its newest message that carries a campaign: one query."""
        if not conversation_ids:
            return {}
        ranked = self._ranked(conversation_ids, EmailMessage.campaign_id.isnot(None))
        rows = self._session.execute(
            select(ranked.c.conversation_id, Campaign.campaign_id, Campaign.campaign_name)
            .join(Campaign, Campaign.campaign_id == ranked.c.campaign_id)
            .where(ranked.c.rn == 1)
        ).all()
        return {r[0]: (r[1], r[2]) for r in rows}

    def latest_reply_labels(self, conversation_ids: Sequence[str]) -> Dict[str, Tuple[Optional[str], bool]]:
        """conversation id -> (label, needs review) of its newest labelled reply: one query."""
        if not conversation_ids:
            return {}
        ranked = self._ranked(conversation_ids, reply_message(), EmailMessage.reply_label.isnot(None))
        review = (
            select(EmailMessage.conversation_id)
            .where(EmailMessage.conversation_id.in_(list(conversation_ids)), EmailMessage.reply_needs_review.is_(True))
            .distinct()
        )
        flagged = {cid for (cid,) in self._session.execute(review).all()}
        rows = self._session.execute(select(ranked.c.conversation_id, ranked.c.reply_label).where(ranked.c.rn == 1)).all()
        labels: Dict[str, Tuple[Optional[str], bool]] = {r[0]: (r[1], r[0] in flagged) for r in rows}
        for cid in flagged - labels.keys():
            labels[cid] = (None, True)
        return labels

    # ── Thread ─────────────────────────────────────────────────────
    def get(self, tenant_id: str, conversation_id: str, *, lock: bool = False) -> Optional[Conversation]:
        q = self._session.query(Conversation).filter(Conversation.id == conversation_id,
                                                     Conversation.tenant_id == tenant_id)
        if lock:
            q = q.with_for_update()
        return q.first()

    def thread_messages(self, conversation_id: str) -> List[EmailMessage]:
        return (
            self._session.query(EmailMessage)
            .filter(EmailMessage.conversation_id == conversation_id)
            .order_by(_message_time(), desc(func.coalesce(EmailMessage.direction, "OUTBOUND")), EmailMessage.message_id)
            .all()
        )

    def events_for(self, message_ids: Iterable[str]) -> List[Tuple[str, int, Optional[datetime], Any]]:
        ids = list(message_ids)
        if not ids:
            return []
        rows = (
            self._session.query(EmailEvent.message_id, EmailEvent.event_type, EmailEvent.event_time,
                                EmailEvent.event_metadata)
            .filter(EmailEvent.message_id.in_(ids))
            .order_by(EmailEvent.event_time)
            .all()
        )
        return [(r[0], int(r[1]), r[2], r[3]) for r in rows]

    def campaign_names(self, tenant_id: str, campaign_ids: Iterable[str]) -> Dict[str, str]:
        ids = sorted({c for c in campaign_ids if c})
        if not ids:
            return {}
        rows = self._session.query(Campaign.campaign_id, Campaign.campaign_name).filter(
            Campaign.campaign_id.in_(ids), Campaign.tenant_id == tenant_id).all()
        return {cid: name for cid, name in rows}

    # ── Manual reply ───────────────────────────────────────────────
    def reply_campaign_id(self, conversation: Conversation) -> Optional[str]:
        """Campaign of the thread's newest sent campaign email, else the contact's newest enrolment."""
        last_outbound = (
            self._session.query(EmailMessage.campaign_id)
            .filter(EmailMessage.conversation_id == conversation.id, EmailMessage.direction == "OUTBOUND",
                    EmailMessage.campaign_id.isnot(None))
            .order_by(desc(EmailMessage.sent_at)).first()
        )
        if last_outbound:
            return last_outbound[0]
        enrollment = (
            self._session.query(CampaignProspect.campaign_id)
            .join(Campaign, and_(Campaign.campaign_id == CampaignProspect.campaign_id,
                                 Campaign.tenant_id == conversation.tenant_id))
            .filter(CampaignProspect.prospect_id == conversation.prospect_id)
            .order_by(desc(CampaignProspect.enrolled_at)).first()
        )
        return enrollment[0] if enrollment else None

    def add_message(self, message: EmailMessage) -> EmailMessage:
        self._session.add(message)
        return message

    def refresh(self, entity: Any) -> None:
        self._session.flush()
        self._session.refresh(entity)


class InboxUnitOfWork(SqlAlchemyUnitOfWork):
    def __enter__(self) -> "InboxUnitOfWork":
        super().__enter__()
        return self

    @property
    def conversations(self) -> ConversationRepository:
        return ConversationRepository(self.session)

    @classmethod
    def for_session(cls, session: Session) -> "InboxUnitOfWork":
        uow = cls(lambda: session)
        uow._owns_session = False
        return uow
