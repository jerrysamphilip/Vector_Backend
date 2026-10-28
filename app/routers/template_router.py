# app/routers/template_router.py
"""
Email Template API endpoints.

Thin routes (ADR 0009, chunk A2): one call to app/services/campaign_template_service.EmailTemplateService
each; templates are found through a campaign of the caller's tenant (another tenant's is "not found").
The attachment store is the FileStore adapter (S2).
"""

from typing import Any, List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, UploadFile, status
from fastapi.responses import FileResponse, Response

from app.adapters import providers
from app.adapters.storage import FileStore
from app.core.auth import require_permission, require_role
from app.core.database import get_db
from app.core.errors import NotFoundError
from app.schemas.template_schema import (
    EmailAttachmentResponse,
    EmailTemplateApprove,
    EmailTemplateCreate,
    EmailTemplateResponse,
    EmailTemplateUpdate,
    TemplateVersionResponse,
)
from app.services.campaign_template_service import EmailTemplateService

router = APIRouter(prefix="/templates", tags=["Email Templates"])

ANY_ROLE = ("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")
MANAGE_TEMPLATES = [Depends(require_permission("manage_templates"))]


def template_service(db=Depends(get_db)) -> EmailTemplateService:
    return EmailTemplateService.for_session(db)


# =============================
# TEMPLATE ENDPOINTS
# =============================

@router.post("", response_model=EmailTemplateResponse, status_code=201, dependencies=MANAGE_TEMPLATES)
async def create_template(
    data: EmailTemplateCreate,
    templates: EmailTemplateService = Depends(template_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
):
    """
    Create an email template.

    Supports personalization tokens like {{first_name}}, {{company_name}}.
    """
    return templates.create(current_user, data)


@router.get("/{template_id}", response_model=EmailTemplateResponse)
async def get_template(
    template_id: str,
    templates: EmailTemplateService = Depends(template_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
):
    """Get template details."""
    return templates.get(current_user, template_id)


@router.put("/{template_id}", response_model=EmailTemplateResponse, dependencies=MANAGE_TEMPLATES)
async def update_template(
    template_id: str,
    data: EmailTemplateUpdate,
    templates: EmailTemplateService = Depends(template_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
):
    """
    Update a template.

    Creates a new version in history. Clears approval status.
    """
    return templates.update(current_user, template_id, data)


@router.post("/{template_id}/approve", response_model=EmailTemplateResponse)
async def approve_template(
    template_id: str,
    data: Optional[EmailTemplateApprove] = None,
    templates: EmailTemplateService = Depends(template_service),
    current_user: Any = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER")),
):
    """
    Approve a template for sending.

    Only approved templates can be used in campaigns.
    """
    return templates.approve(current_user, template_id)


@router.get("/{template_id}/versions", response_model=List[TemplateVersionResponse])
async def get_template_versions(
    template_id: str,
    templates: EmailTemplateService = Depends(template_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
):
    """Get version history for a template."""
    return templates.versions(current_user, template_id)


@router.delete("/{template_id}", status_code=204, dependencies=MANAGE_TEMPLATES)
async def delete_template(
    template_id: str,
    templates: EmailTemplateService = Depends(template_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
):
    """Delete a template and its versions."""
    templates.delete(current_user, template_id)


# =============================
# ATTACHMENT ENDPOINTS
# =============================

def _content_disposition(filename: str) -> str:
    """The same header FileResponse sends for a download (RFC 6266 / 5987 for non-ASCII names)."""
    quoted = quote(filename)
    if quoted != filename:
        return f"attachment; filename*=utf-8''{quoted}"
    return f'attachment; filename="{filename}"'


@router.post(
    "/{template_id}/attachments",
    response_model=EmailAttachmentResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=MANAGE_TEMPLATES,
)
async def upload_attachment(
    template_id: str,
    file: UploadFile = File(...),
    templates: EmailTemplateService = Depends(template_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
    store: FileStore = Depends(providers.attachment_store),
):
    """Upload a file attachment for an email template (Gmail-style compose attachment)."""
    templates.template_for(current_user, template_id)
    contents = await file.read()
    return templates.add_attachment(current_user, template_id, file.filename, file.content_type, contents, store)


@router.get("/{template_id}/attachments", response_model=List[EmailAttachmentResponse])
async def list_attachments(
    template_id: str,
    templates: EmailTemplateService = Depends(template_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
):
    """List attachments for a template."""
    return templates.attachments(current_user, template_id)


@router.get("/{template_id}/attachments/{attachment_id}/download")
async def download_attachment(
    template_id: str,
    attachment_id: str,
    templates: EmailTemplateService = Depends(template_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
    store: FileStore = Depends(providers.attachment_store),
):
    """Download/preview an attachment's original file."""
    attachment = templates.attachment_for(current_user, template_id, attachment_id)
    if not store.exists(attachment.storage_path):
        raise NotFoundError("Attachment file missing from storage")
    media_type = attachment.content_type or "application/octet-stream"
    file_path = store.local_path(attachment.storage_path)
    if file_path is None:  # a remote store (G4): send the bytes
        return Response(content=store.get(attachment.storage_path), media_type=media_type,
                        headers={"Content-Disposition": _content_disposition(attachment.filename)})
    return FileResponse(path=str(file_path), filename=attachment.filename, media_type=media_type)


@router.delete(
    "/{template_id}/attachments/{attachment_id}",
    status_code=204,
    dependencies=MANAGE_TEMPLATES,
)
async def delete_attachment(
    template_id: str,
    attachment_id: str,
    templates: EmailTemplateService = Depends(template_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
    store: FileStore = Depends(providers.attachment_store),
):
    """Remove an attachment from a template."""
    templates.delete_attachment(current_user, template_id, attachment_id, store)
