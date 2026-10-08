# app/routers/conversation_router.py
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List

from app.core.database import get_db
from app.core.auth import require_role
from app.models.user import User
from app.services.conversation_service import ConversationService
from app.schemas.conversation_schema import ConversationListResponse, ConversationDetailResponse, ReplyRequest, ReplyResponse

router = APIRouter(prefix="/conversations", tags=["Conversations"])

@router.get("", response_model=ConversationListResponse)
def get_conversations(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    campaign_id: str = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    service = ConversationService(db)
    return service.list_conversations(current_user.tenant_id, page, page_size, campaign_id)

@router.get("/{conversation_id}", response_model=ConversationDetailResponse)
def get_conversation_thread(
    conversation_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    service = ConversationService(db)
    thread = service.get_conversation_thread(conversation_id, current_user.tenant_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return thread

@router.post("/{conversation_id}/reply", response_model=ReplyResponse)
def reply_to_conversation(
    conversation_id: str,
    request: ReplyRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    service = ConversationService(db)
    message = service.create_reply(conversation_id, request.body_text, current_user.user_id, current_user.tenant_id)
    if not message:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"message": message, "status": "sent"}
