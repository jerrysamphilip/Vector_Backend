# app/routers/conversation_router.py
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.auth import require_role
from app.domain.inbox import MAX_SEARCH_CHARS, ConversationQuery, ReplyLabel
from app.domain.paging import Paging
from app.models.user import User
from app.services.conversation_service import ConversationService
from app.schemas.conversation_schema import ConversationListResponse, ConversationDetailResponse, ReplyRequest, ReplyResponse

router = APIRouter(prefix="/conversations", tags=["Conversations"])

_READERS = require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")


@router.get("", response_model=ConversationListResponse)
def get_conversations(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    campaign_id: Optional[str] = Query(None, max_length=36),
    search: Optional[str] = Query(None, max_length=MAX_SEARCH_CHARS,
                                  description="Contact name, email or subject (matched literally)"),
    label: Optional[str] = Query(None, description="Reply label: POSITIVE / NEUTRAL / NEGATIVE / OUT_OF_OFFICE / UNSUBSCRIBE"),
    db: Session = Depends(get_db),
    current_user: User = Depends(_READERS),
):
    reply_label = ReplyLabel.parse(label) if label else None
    if label and reply_label is None:
        raise HTTPException(status_code=422, detail=f"Unknown reply label: {label}")
    query = ConversationQuery(paging=Paging(page, page_size), search=search, campaign_id=campaign_id,
                              label=reply_label)
    return ConversationService(db).list_conversations(current_user, query)


@router.get("/{conversation_id}", response_model=ConversationDetailResponse)
def get_conversation_thread(
    conversation_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(_READERS),
):
    thread = ConversationService(db).get_conversation_thread(current_user, conversation_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return thread


@router.post("/{conversation_id}/reply", response_model=ReplyResponse)
def reply_to_conversation(
    conversation_id: str,
    request: ReplyRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(_READERS),
):
    message = ConversationService(db).create_reply(current_user, conversation_id, request.body_text)
    if not message:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"message": message, "status": "sent"}
