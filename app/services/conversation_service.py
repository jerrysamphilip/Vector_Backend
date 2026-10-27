# app/services/conversation_service.py
"""
Unified inbox (S-10 / F-13): server-side search and paging, campaign name per conversation, newest
activity first, reply labels (S-09) and a per-email activity timeline. SQL lives in
app/repositories/inbox.py; this module holds the rules (visibility, mark-as-read, reply attribution)
and owns the unit of work. Conversations are limited to the workspace and to the contacts the user
may see (owner / team scope, BR-SH-02, same rule as the contacts list).
"""
import uuid
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.core.clock import utcnow
from app.domain.inbox import ConversationQuery, OwnerScope
from app.models.conversation import Conversation
from app.models.email_message import EmailEvent, EmailMessage
from app.models.user import User
from app.repositories.inbox import ConversationRow, InboxUnitOfWork, LastMessage
from app.schemas.conversation_schema import (ActivityItem, ConversationDetailResponse, ConversationListItem,
                                             ConversationListResponse, MessageResponse)
from app.services.contact_service import can_manage_contacts, visible_user_ids
from app.utils.email_utils import conversation_snippet

# email_events.event_type -> timeline step
_EVENT_STEPS = {
    EmailEvent.EVENT_SENT: "SENT",
    EmailEvent.EVENT_DELIVERED: "DELIVERED",
    EmailEvent.EVENT_OPEN: "OPENED",
    EmailEvent.EVENT_CLICK: "CLICKED",
    EmailEvent.EVENT_BOUNCE: "BOUNCED",
    EmailEvent.EVENT_SENDER_BOUNCE: "BOUNCED",
    EmailEvent.EVENT_UNSUBSCRIBE: "UNSUBSCRIBED",
    EmailEvent.EVENT_REPLY: "REPLIED",
}
_FINAL_STEPS = {"BOUNCED": "BOUNCED", "COMPLAINED": "COMPLAINED", "REJECTED": "BOUNCED"}


def prospect_display_name(first_name: Optional[str], last_name: Optional[str]) -> str:
    return f"{first_name or ''} {last_name or ''}".strip() or "Unknown"


def build_activity(message: EmailMessage, events: Iterable[Tuple[int, Optional[datetime], Any]],
                   replies_by_time: Dict[Optional[datetime], EmailMessage]) -> List[ActivityItem]:
    """Timeline of one sent email: sent, delivered, opened, clicked, bounced, replied (with the
    reply's label), oldest first. Steps come from the email's events, with sent_at / delivered_at /
    final_status filling in what has no event. Pure (unit-tested)."""
    steps: List[ActivityItem] = []
    seen = set()
    for event_type, at, metadata in events:
        step = _EVENT_STEPS.get(event_type)
        if step is None:
            continue
        item = ActivityItem(type=step, at=at)
        if step == "REPLIED":
            reply_id = metadata.get("reply_message_id") if isinstance(metadata, dict) else None
            reply = replies_by_time.get(at)
            item.reply_message_id = reply_id or (reply.message_id if reply is not None else None)
            item.reply_label = reply.reply_label if reply is not None else None
        steps.append(item)
        seen.add(step)
    if message.sent_at and "SENT" not in seen:
        steps.append(ActivityItem(type="SENT", at=message.sent_at))
    if message.delivered_at and "DELIVERED" not in seen:
        steps.append(ActivityItem(type="DELIVERED", at=message.delivered_at))
    final_step = _FINAL_STEPS.get((message.final_status or "").upper())
    if final_step and final_step not in seen:
        steps.append(ActivityItem(type=final_step, at=message.final_status_at))
    steps.sort(key=lambda s: (s.at is None, s.at or datetime.min))
    return steps


def _list_item(row: ConversationRow, last: Optional[LastMessage], campaign: Optional[Tuple[str, str]],
               label: Optional[Tuple[Optional[str], bool]]) -> ConversationListItem:
    return ConversationListItem(
        id=row.id,
        prospect_id=row.prospect_id,
        prospect_name=prospect_display_name(row.first_name, row.last_name),
        prospect_email=row.prospect_email,
        inbox_email=row.inbox_email or "",
        subject=row.subject or "No Subject",
        last_message_snippet=conversation_snippet(last.body_text if last else None),
        last_message_at=row.latest_at or utcnow(),
        is_unread=row.is_unread,
        status=row.status or "OPEN",
        last_message_direction=(last.direction if last else None) or "OUTBOUND",
        last_message_status=last.status if last else None,
        last_message_final_status=last.final_status if last else None,
        campaign_id=campaign[0] if campaign else None,
        campaign_name=campaign[1] if campaign else None,
        last_reply_label=label[0] if label else None,
        needs_review=bool(label and label[1]),
    )


class ConversationService:
    def __init__(self, db: Any) -> None:
        self._db = db
        self._uow = InboxUnitOfWork.for_session(db)

    def owner_scope(self, user: User) -> OwnerScope:
        ids = visible_user_ids(self._db, user)
        if ids is None:
            return OwnerScope()
        return OwnerScope(frozenset(ids), include_unowned=can_manage_contacts(user))

    def list_conversations(self, user: User, query: ConversationQuery) -> ConversationListResponse:
        scope = self.owner_scope(user)
        with self._uow as uow:
            repo = uow.conversations
            rows, total = repo.page(user.tenant_id, query, scope)
            ids = [r.id for r in rows]
            last = repo.last_messages(ids)
            campaigns = repo.latest_campaigns(ids)
            labels = repo.latest_reply_labels(ids)
        items = [_list_item(r, last.get(r.id), campaigns.get(r.id), labels.get(r.id)) for r in rows]
        return ConversationListResponse(
            items=items, total=total, page=query.paging.page, page_size=query.paging.page_size,
            has_more=query.paging.offset + len(items) < total,
        )

    def _visible(self, user: User, conversation: Conversation) -> bool:
        prospect = conversation.prospect
        return prospect is not None and self.owner_scope(user).allows(prospect.owner_id)

    def get_conversation_thread(self, user: User, conversation_id: str) -> Optional[ConversationDetailResponse]:
        """The thread with each email's activity; marks it read. None when missing or not visible."""
        with self._uow as uow:
            repo = uow.conversations
            conversation = repo.get(user.tenant_id, conversation_id)
            if conversation is None or not self._visible(user, conversation):
                return None
            messages = repo.thread_messages(conversation.id)
            outbound_ids = [m.message_id for m in messages if (m.direction or "OUTBOUND") == "OUTBOUND"]
            events: Dict[str, List[Tuple[int, Optional[datetime], Any]]] = {}
            for message_id, event_type, at, metadata in repo.events_for(outbound_ids):
                events.setdefault(message_id, []).append((event_type, at, metadata))
            names = repo.campaign_names(user.tenant_id, (m.campaign_id for m in messages))
            replies_by_time = {m.sent_at: m for m in messages if m.direction == "INBOUND"}
            detail = ConversationDetailResponse(
                id=conversation.id, prospect_id=conversation.prospect_id, inbox_id=conversation.inbox_id,
                subject=conversation.subject,
                messages=[self._message(m, names, events.get(m.message_id, []), replies_by_time) for m in messages],
            )
            if conversation.is_unread:
                conversation.is_unread = False
                uow.commit()
            return detail

    @staticmethod
    def _message(m: EmailMessage, names: Dict[str, str], events: List[Tuple[int, Optional[datetime], Any]],
                 replies_by_time: Dict[Optional[datetime], EmailMessage]) -> MessageResponse:
        response = MessageResponse.model_validate(m)
        response.campaign_name = names.get(m.campaign_id or "")
        response.reply_needs_review = bool(m.reply_needs_review)
        if (m.direction or "OUTBOUND") == "OUTBOUND":
            response.activity = build_activity(m, events, replies_by_time)
        return response

    def create_reply(self, user: User, conversation_id: str, body_text: str) -> Optional[EmailMessage]:
        """Queue a manual reply in the thread (sent by the scheduler). None when missing or not visible."""
        with self._uow as uow:
            repo = uow.conversations
            conv = repo.get(user.tenant_id, conversation_id)
            if conv is None or not self._visible(user, conv):
                return None
            now = utcnow()
            new_msg = repo.add_message(EmailMessage(
                message_id=str(uuid.uuid4()),
                campaign_id=repo.reply_campaign_id(conv),  # keeps the thread's campaign attribution
                prospect_id=conv.prospect_id,
                sequence_id=None,
                inbox_id=conv.inbox_id,
                conversation_id=conv.id,
                subject=f"Re: {conv.subject}" if conv.subject else "Reply",
                body_text=body_text,
                to_email=conv.prospect.email,
                from_email=conv.inbox.email_address,
                direction="OUTBOUND",
                status="QUEUED",
                scheduled_at=now,
                sent_at=None,
            ))
            conv.last_message_at = now
            conv.is_unread = False
            uow.commit()
            repo.refresh(new_msg)
            return new_msg
