# app/routers/template_router.py
"""
Email Template API endpoints.
"""

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime
from pathlib import Path
import uuid
import hashlib
import os

from app.core.auth import require_role, require_permission
from app.core.config import settings
from app.core.database import get_db
from app.models.email_template import EmailTemplate, EmailTemplateVersion
from app.models.email_attachment import EmailAttachment
from app.models.user import User
from app.models.email_sequence import EmailSequence
from app.models.campaign import Campaign
from app.schemas.template_schema import (
    EmailTemplateCreate,
    EmailTemplateUpdate,
    EmailTemplateApprove,
    EmailTemplateResponse,
    EmailTemplateListItem,
    TemplateVersionResponse,
    EmailAttachmentResponse,
)
from app.services.audit_service import AuditService
from app.services.campaign_email_service import CampaignEmailService
from app.utils.email_utils import finalize_email_body


router = APIRouter(prefix="/templates", tags=["Email Templates"])



def get_audit_service(db: Session = Depends(get_db)) -> AuditService:
    return AuditService(db)


def _content_hash(subject: str, body: str) -> str:
    """Generate content fingerprint for duplicate detection."""
    content = f"{subject}|{body}"
    return hashlib.sha256(content.encode()).hexdigest()


def _to_response(template: EmailTemplate, db: Session) -> EmailTemplateResponse:
    """Convert EmailTemplate to response."""
    version_count = db.query(EmailTemplateVersion).filter(
        EmailTemplateVersion.template_id == template.template_id
    ).count()

    attachments = db.query(EmailAttachment).filter(
        EmailAttachment.template_id == template.template_id
    ).order_by(EmailAttachment.created_at.asc()).all()

    return EmailTemplateResponse(
        template_id=template.template_id,
        campaign_id=template.campaign_id,
        sequence_id=template.sequence_id,
        subject=template.subject,
        body=template.body,
        designation=template.designation,
        tone=template.tone,
        is_ai_generated=template.is_ai_generated,
        ai_model=template.ai_model,
        ai_prompt_id=template.ai_prompt_id,
        content_fingerprint=template.content_fingerprint,
        approved_by=template.approved_by,
        approved_at=template.approved_at,
        is_approved=template.approved_by is not None,
        created_at=template.created_at,
        version_count=version_count,
        attachments=[EmailAttachmentResponse.model_validate(a) for a in attachments],
    )


# =============================
# TEMPLATE ENDPOINTS
# =============================

@router.post("", response_model=EmailTemplateResponse, status_code=201, dependencies=[Depends(require_permission("manage_templates"))])
async def create_template(
    data: EmailTemplateCreate,
    db: Session = Depends(get_db),
    audit: AuditService = Depends(get_audit_service),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """
    Create an email template.
    
    Supports personalization tokens like {{first_name}}, {{company_name}}.
    """
    owns_campaign = db.query(Campaign.campaign_id).filter(
        Campaign.campaign_id == data.campaign_id,
        Campaign.tenant_id == current_user.tenant_id,
    ).first()
    if not owns_campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    template = EmailTemplate(
        template_id=str(uuid.uuid4()),
        campaign_id=data.campaign_id,
        sequence_id=data.sequence_id,
        subject=data.subject,
        # body=normalize_paragraph_spacing(data.body) if data.body else data.body,
        body=finalize_email_body(data.body) if data.body else data.body,
        designation=data.designation,
        tone=data.tone.value if data.tone else None,
        is_ai_generated=data.is_ai_generated,
        ai_prompt_id=data.ai_prompt_id,
        content_fingerprint=_content_hash(data.subject, data.body),
    )
    
    db.add(template)
    
    # Create initial version
    version = EmailTemplateVersion(
        version_id=str(uuid.uuid4()),
        template_id=template.template_id,
        subject=data.subject,
        # body=normalize_paragraph_spacing(data.body) if data.body else data.body,
        body=finalize_email_body(data.body) if data.body else data.body,
        version_number=1,
        created_by=current_user.user_id,
        is_active=True,
    )
    db.add(version)

    # Audit log
    audit.log_action(
        tenant_id=current_user.tenant_id,
        user_id=current_user.user_id,
        action=AuditService.ACTION_CREATE,
        entity_type=AuditService.ENTITY_TEMPLATE,
        entity_id=template.template_id,
    )
    
    db.commit()
    return _to_response(template, db)


@router.get("/{template_id}", response_model=EmailTemplateResponse)
async def get_template(
    template_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """Get template details."""
    template = _get_template_or_404(template_id, db, current_user.tenant_id)
    
    return _to_response(template, db)


@router.put("/{template_id}", response_model=EmailTemplateResponse, dependencies=[Depends(require_permission("manage_templates"))])
async def update_template(
    template_id: str,
    data: EmailTemplateUpdate,
    db: Session = Depends(get_db),
    audit: AuditService = Depends(get_audit_service),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """
    Update a template.
    
    Creates a new version in history. Clears approval status.
    """
    template = _get_template_or_404(template_id, db, current_user.tenant_id)
    
    # Get current max version
    max_version = db.query(EmailTemplateVersion).filter(
        EmailTemplateVersion.template_id == template_id
    ).count()
    
    # Update fields
    if data.subject is not None:
        template.subject = data.subject
    if data.body is not None:
        # template.body = normalize_paragraph_spacing(data.body)
        template.body = finalize_email_body(data.body)
    if data.designation is not None:
        template.designation = data.designation
    if data.tone is not None:
        template.tone = data.tone.value
    
    # Update fingerprint
    template.content_fingerprint = _content_hash(template.subject, template.body)
    
    # Clear approval (content changed)
    template.approved_by = None
    template.approved_at = None
    
    # Create new version
    version = EmailTemplateVersion(
        version_id=str(uuid.uuid4()),
        template_id=template_id,
        subject=template.subject,
        body=template.body,
        version_number=max_version + 1,
        created_by=current_user.user_id,
        is_active=True,
    )
    db.add(version)

    # Mark old versions as inactive
    db.query(EmailTemplateVersion).filter(
        EmailTemplateVersion.template_id == template_id,
        EmailTemplateVersion.version_id != version.version_id,
    ).update({"is_active": False})
    
    # Propagate to any not-yet-sent EmailMessage snapshots for this template —
    # otherwise the edit above has no effect on a campaign that already launched
    # (see CampaignEmailService.propagate_template_edit for why).
    CampaignEmailService(db).propagate_template_edit(template_id)

    # Audit log
    audit.log_action(
        tenant_id=current_user.tenant_id,
        user_id=current_user.user_id,
        action=AuditService.ACTION_UPDATE,
        entity_type=AuditService.ENTITY_TEMPLATE,
        entity_id=template_id,
    )

    db.commit()
    return _to_response(template, db)


@router.post("/{template_id}/approve", response_model=EmailTemplateResponse)
async def approve_template(
    template_id: str,
    data: Optional[EmailTemplateApprove] = None,
    db: Session = Depends(get_db),
    audit: AuditService = Depends(get_audit_service),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER")),
):
    """
    Approve a template for sending.
    
    Only approved templates can be used in campaigns.
    """
    template = _get_template_or_404(template_id, db, current_user.tenant_id)
    
    # The approver is always the authenticated user; any approved_by in the
    # request body is ignored so it can't be spoofed.
    template.approved_by = current_user.user_id
    template.approved_at = datetime.utcnow()
    
    # Audit log
    audit.log_action(
        tenant_id=current_user.tenant_id,
        user_id=current_user.user_id,
        action=AuditService.ACTION_APPROVE,
        entity_type=AuditService.ENTITY_TEMPLATE,
        entity_id=template_id,
    )
    
    db.commit()
    return _to_response(template, db)


@router.get("/{template_id}/versions", response_model=List[TemplateVersionResponse])
async def get_template_versions(
    template_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """Get version history for a template."""
    template = _get_template_or_404(template_id, db, current_user.tenant_id)
    
    versions = db.query(EmailTemplateVersion).filter(
        EmailTemplateVersion.template_id == template_id
    ).order_by(EmailTemplateVersion.version_number.desc()).all()
    
    return [
        TemplateVersionResponse(
            version_id=v.version_id,
            template_id=v.template_id,
            subject=v.subject,
            body=v.body,
            version_number=v.version_number,
            created_by=v.created_by,
            created_at=v.created_at,
            is_active=v.is_active,
        )
        for v in versions
    ]


@router.delete("/{template_id}", status_code=204, dependencies=[Depends(require_permission("manage_templates"))])
async def delete_template(
    template_id: str,
    db: Session = Depends(get_db),
    audit: AuditService = Depends(get_audit_service),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """Delete a template and its versions."""
    template = _get_template_or_404(template_id, db, current_user.tenant_id)
    
    # Audit log
    audit.log_action(
        tenant_id=current_user.tenant_id,
        user_id=current_user.user_id,
        action=AuditService.ACTION_DELETE,
        entity_type=AuditService.ENTITY_TEMPLATE,
        entity_id=template_id,
    )
    
    # Delete versions first
    db.query(EmailTemplateVersion).filter(
        EmailTemplateVersion.template_id == template_id
    ).delete()

    db.delete(template)
    db.commit()


# =============================
# ATTACHMENT ENDPOINTS
# =============================

def _attachments_dir() -> Path:
    directory = Path(settings.ATTACHMENTS_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _get_template_or_404(template_id: str, db: Session, tenant_id: str) -> EmailTemplate:
    # Templates have no tenant_id of their own; ownership is via their campaign.
    # Another tenant's template is reported as not found.
    template = db.query(EmailTemplate).join(
        Campaign, Campaign.campaign_id == EmailTemplate.campaign_id
    ).filter(
        EmailTemplate.template_id == template_id,
        Campaign.tenant_id == tenant_id,
    ).first()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    return template


@router.post(
    "/{template_id}/attachments",
    response_model=EmailAttachmentResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("manage_templates"))],
)
async def upload_attachment(
    template_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """Upload a file attachment for an email template (Gmail-style compose attachment)."""
    template = _get_template_or_404(template_id, db, current_user.tenant_id)

    existing_count = db.query(EmailAttachment).filter(
        EmailAttachment.template_id == template_id
    ).count()
    if existing_count >= settings.MAX_ATTACHMENTS_PER_TEMPLATE:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum of {settings.MAX_ATTACHMENTS_PER_TEMPLATE} attachments per email reached",
        )

    contents = await file.read()
    max_bytes = settings.MAX_ATTACHMENT_SIZE_MB * 1024 * 1024
    if len(contents) > max_bytes:
        raise HTTPException(
            status_code=400,
            detail=f"File exceeds the {settings.MAX_ATTACHMENT_SIZE_MB}MB attachment size limit",
        )
    if not contents:
        raise HTTPException(status_code=400, detail="File is empty")

    original_name = os.path.basename(file.filename or "attachment")
    stored_name = f"{uuid.uuid4()}_{original_name}"

    with open(_attachments_dir() / stored_name, "wb") as out_file:
        out_file.write(contents)

    attachment = EmailAttachment(
        attachment_id=str(uuid.uuid4()),
        template_id=template_id,
        filename=original_name,
        content_type=file.content_type,
        size_bytes=len(contents),
        storage_path=stored_name,
    )
    db.add(attachment)
    db.commit()
    db.refresh(attachment)

    return EmailAttachmentResponse.model_validate(attachment)


@router.get("/{template_id}/attachments", response_model=List[EmailAttachmentResponse])
async def list_attachments(
    template_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """List attachments for a template."""
    _get_template_or_404(template_id, db, current_user.tenant_id)
    attachments = db.query(EmailAttachment).filter(
        EmailAttachment.template_id == template_id
    ).order_by(EmailAttachment.created_at.asc()).all()
    return [EmailAttachmentResponse.model_validate(a) for a in attachments]


@router.get("/{template_id}/attachments/{attachment_id}/download")
async def download_attachment(
    template_id: str,
    attachment_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """Download/preview an attachment's original file."""
    _get_template_or_404(template_id, db, current_user.tenant_id)
    attachment = db.query(EmailAttachment).filter(
        EmailAttachment.attachment_id == attachment_id,
        EmailAttachment.template_id == template_id,
    ).first()
    if not attachment:
        raise HTTPException(status_code=404, detail="Attachment not found")

    file_path = _attachments_dir() / attachment.storage_path
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Attachment file missing from storage")

    return FileResponse(
        path=str(file_path),
        filename=attachment.filename,
        media_type=attachment.content_type or "application/octet-stream",
    )


@router.delete(
    "/{template_id}/attachments/{attachment_id}",
    status_code=204,
    dependencies=[Depends(require_permission("manage_templates"))],
)
async def delete_attachment(
    template_id: str,
    attachment_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """Remove an attachment from a template."""
    _get_template_or_404(template_id, db, current_user.tenant_id)
    attachment = db.query(EmailAttachment).filter(
        EmailAttachment.attachment_id == attachment_id,
        EmailAttachment.template_id == template_id,
    ).first()
    if not attachment:
        raise HTTPException(status_code=404, detail="Attachment not found")

    file_path = _attachments_dir() / attachment.storage_path
    if file_path.exists():
        try:
            file_path.unlink()
        except OSError:
            pass

    db.delete(attachment)
    db.commit()
