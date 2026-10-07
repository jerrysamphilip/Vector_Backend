# app/schemas/conversation_schema.py
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime

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

    class Config:
        from_attributes = True

class ConversationListResponse(BaseModel):
    items: List[ConversationListItem]
    total: int
    page: int
    page_size: int

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
