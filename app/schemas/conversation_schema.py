# app/schemas/conversation_schema.py
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime


class ActivityItem(BaseModel):
    """One step in a sent email's timeline (S-10): SENT / DELIVERED / OPENED / CLICKED / BOUNCED /
    COMPLAINED / UNSUBSCRIBED / REPLIED (with the reply's label)."""
    type: str
    at: Optional[datetime] = None
    reply_label: Optional[str] = None
    reply_message_id: Optional[str] = None


class MessageResponse(BaseModel):
    message_id: str
    direction: str  # INBOUND / OUTBOUND
    subject: Optional[str]
    body_text: Optional[str]
    from_email: Optional[str]
    to_email: str
    scheduled_at: Optional[datetime]
    sent_at: Optional[datetime]
    delivered_at: Optional[datetime] = None
    status: str
    # Delivery outcome of a sent email: DELIVERED / BOUNCED / COMPLAINED / REJECTED / FAILED / UNCONFIRMED
    final_status: Optional[str] = None
    campaign_id: Optional[str] = None
    campaign_name: Optional[str] = None
    # Inbound replies (S-09): POSITIVE / NEUTRAL / NEGATIVE / OUT_OF_OFFICE / UNSUBSCRIBE, None = not yet
    reply_label: Optional[str] = None
    reply_confidence: Optional[float] = None
    reply_needs_review: bool = False
    activity: List[ActivityItem] = Field(default_factory=list)

    class Config:
        from_attributes = True


class ConversationListItem(BaseModel):
    id: str
    prospect_id: str
    prospect_name: str
    prospect_email: str
    inbox_email: str
    subject: Optional[str]
    last_message_snippet: Optional[str]
    last_message_at: datetime
    is_unread: bool
    status: str
    last_message_direction: str = "OUTBOUND"  # INBOUND = reply/bounce received, OUTBOUND = last sent
    last_message_status: Optional[str] = None
    last_message_final_status: Optional[str] = None
    campaign_id: Optional[str] = None
    campaign_name: Optional[str] = None  # campaign of the thread's newest message that carries one
    last_reply_label: Optional[str] = None
    needs_review: bool = False

    class Config:
        from_attributes = True


class ConversationListResponse(BaseModel):
    items: List[ConversationListItem]
    total: int
    page: int
    page_size: int
    has_more: bool = False


class ConversationDetailResponse(BaseModel):
    id: str
    prospect_id: str
    inbox_id: str
    subject: Optional[str]
    messages: List[MessageResponse]

    class Config:
        from_attributes = True


class ReplyRequest(BaseModel):
    body_text: str


class ReplyResponse(BaseModel):
    message: MessageResponse
    status: str
