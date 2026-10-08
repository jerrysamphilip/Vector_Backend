# app/services/conversation_service.py
from sqlalchemy.orm import Session
from sqlalchemy import desc, func
from typing import List, Optional
import uuid
from datetime import datetime

from app.models.conversation import Conversation
from app.models.email_message import EmailMessage
from app.schemas.conversation_schema import ConversationListItem, ConversationListResponse, ConversationDetailResponse
from app.utils.email_utils import extract_latest_message_text

class ConversationService:
    def __init__(self, db: Session):
        self.db = db

    def list_conversations(self, tenant_id: str, page: int = 1, page_size: int = 20, campaign_id: Optional[str] = None) -> ConversationListResponse:
        query = self.db.query(Conversation).filter(
            Conversation.tenant_id == tenant_id,
            Conversation.messages.any()
        )

        if campaign_id:
            # Filter by conversations that contain at least one message (sent, reply, or bounce)
            # attributed to this campaign. This is more reliable than joining via CampaignProspect
            # because INBOUND replies and bounce notifications carry campaign_id on the EmailMessage.
            query = query.filter(
                Conversation.messages.any(EmailMessage.campaign_id == campaign_id)
            )

        total = query.count()

        conversations = query.order_by(desc(Conversation.last_message_at))\
            .offset((page - 1) * page_size)\
            .limit(page_size).all()

        items = []
        for c in conversations:
            # Get last message for snippet and direction
            last_msg = self.db.query(EmailMessage).filter(EmailMessage.conversation_id == c.id)\
                .order_by(desc(EmailMessage.sent_at)).first()

            if last_msg and last_msg.body_text:
                cleaned = extract_latest_message_text(last_msg.body_text)
                snippet_src = cleaned or "No messages"
                snippet = snippet_src[:100] + ("..." if len(snippet_src) > 100 else "")
            else:
                snippet = "No messages"

            items.append(ConversationListItem(
                id=c.id,
                prospect_id=c.prospect_id,
                prospect_name=f"{c.prospect.first_name or ''} {c.prospect.last_name or ''}".strip() or "Unknown",
                prospect_email=c.prospect.email,
                inbox_email=c.inbox.email_address,
                subject=c.subject or "No Subject",
                last_message_snippet=snippet,
                last_message_at=c.last_message_at,
                is_unread=c.is_unread,
                status=c.status,
                last_message_direction=last_msg.direction if last_msg else "OUTBOUND",
            ))
            
        return ConversationListResponse(
            items=items,
            total=total,
            page=page,
            page_size=page_size
        )

    def get_conversation_thread(self, conversation_id: str, tenant_id: str) -> ConversationDetailResponse:
        conversation = self.db.query(Conversation).filter(
            Conversation.id == conversation_id,
            Conversation.tenant_id == tenant_id
        ).first()
        if not conversation:
            return None
        
        # Mark as read
        conversation.is_unread = False
        self.db.commit()
        
        return conversation

    def create_reply(self, conversation_id: str, body_text: str, user_id: str, tenant_id: str) -> EmailMessage:
        conv = self.db.query(Conversation).filter(
            Conversation.id == conversation_id,
            Conversation.tenant_id == tenant_id
        ).first()
        if not conv:
            return None
            
        # Get campaign_id from the latest outbound message to maintain thread continuity
        campaign_id = None
        last_outbound = self.db.query(EmailMessage).filter(
            EmailMessage.conversation_id == conv.id,
            EmailMessage.direction == "OUTBOUND",
            EmailMessage.campaign_id != None
        ).order_by(desc(EmailMessage.sent_at)).first()
        
        if last_outbound:
            campaign_id = last_outbound.campaign_id
        else:
            # Fallback to campaign enrollment if no outbound message yet
            from app.models.campaign import CampaignProspect
            enrollment = self.db.query(CampaignProspect).filter(
                CampaignProspect.prospect_id == conv.prospect_id
            ).first()
            if enrollment:
                campaign_id = enrollment.campaign_id

        # Create outbound message
        new_msg = EmailMessage(
            message_id=str(uuid.uuid4()),
            campaign_id=campaign_id,
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
            scheduled_at=datetime.utcnow(),
            sent_at=None
        )
        
        conv.last_message_at = datetime.utcnow()
        conv.is_unread = False
        
        self.db.add(new_msg)
        self.db.commit()
        self.db.refresh(new_msg)
        
        return new_msg
