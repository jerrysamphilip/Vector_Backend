# app/routers/user_router.py
"""
Team management endpoints: list users, invite, change role, deactivate.

Request parsing and auth only; the rules live in app/services/user_admin_service.py
(roles, permissions, deactivation) and app/services/invitation_service.py (invitations).
Handlers carry return type hints with ``response_model=None`` where no response model is
declared, so the OpenAPI schema and serialisation stay as they were.
"""

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.orm import Session

from app.core.auth import require_permission, require_role
from app.core.database import get_db
from app.models.user import User
from app.schemas.user_admin_schema import (
    AcceptInviteRequest,
    ChangeRoleRequest,
    InviteRequest,
    UpdatePermissionsRequest,
    UserOut,
)
from app.services import invitation_service as invitations
from app.services import user_admin_service as team

router = APIRouter(prefix="/api/users", tags=["User Management"])

admins = require_role("SUPER_ADMIN", "ADMIN")
super_admin = require_role("SUPER_ADMIN")
manage_team = [Depends(require_permission("manage_team"))]


def _user_out(u: User) -> UserOut:
    return UserOut(
        user_id=u.user_id,
        tenant_id=u.tenant_id,
        first_name=u.first_name,
        last_name=u.last_name,
        email=u.email,
        role=u.role,
        status=u.status,
        auth_provider=u.auth_provider or "local",
        email_verified=u.email_verified or False,
        avatar_url=u.avatar_url,
        created_at=u.created_at,
        last_login_at=u.last_login_at,
        permissions=team.effective_permissions(u),
    )


@router.get("", response_model=list[UserOut])
def list_users(
    current_user: User = Depends(admins),
    db: Session = Depends(get_db),
) -> List[UserOut]:
    """List all team members in the current tenant."""
    return [_user_out(u) for u in team.list_members(db, current_user)]


@router.post("/invite", status_code=status.HTTP_201_CREATED, dependencies=manage_team, response_model=None)
def invite_user(
    body: InviteRequest,
    request: Request,
    current_user: User = Depends(admins),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Send an invitation to join the tenant workspace."""
    return invitations.invite_user(db, request, current_user, body.email, body.role)


@router.post("/{user_id}/resend-invite", dependencies=manage_team, response_model=None)
def resend_invite(
    user_id: str,
    request: Request,
    current_user: User = Depends(admins),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Send a pending invitation again: a new temporary password and a new sign-in link; the
    previous invitation and links stop working. Only for accounts still INVITED, at most
    once a minute per user. Same guards as invite (tenant, role, manage_team).
    """
    return invitations.resend_invite(db, request, current_user, user_id)


@router.post("/accept-invite", status_code=status.HTTP_201_CREATED, response_model=None)
def accept_invite(body: AcceptInviteRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Accept an invitation and create a user account."""
    return invitations.accept_invite(db, body)


@router.put("/{user_id}/role", dependencies=manage_team, response_model=None)
def change_role(
    user_id: str,
    body: ChangeRoleRequest,
    current_user: User = Depends(super_admin),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Change a team member's role (SUPER_ADMIN only)."""
    team.change_role(db, current_user, user_id, body.role)
    return {"message": f"Role updated to {body.role}", "user_id": user_id}


@router.delete("/{user_id}", status_code=status.HTTP_200_OK, dependencies=manage_team, response_model=None)
def deactivate_user(
    user_id: str,
    current_user: User = Depends(admins),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Deactivate a team member (SUPER_ADMIN or ADMIN). Sets status to INACTIVE."""
    team.deactivate(db, current_user, user_id)
    return {"message": "User deactivated", "user_id": user_id}


@router.put("/{user_id}/reactivate", status_code=status.HTTP_200_OK, dependencies=manage_team, response_model=None)
def reactivate_user(
    user_id: str,
    current_user: User = Depends(admins),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Reactivate a previously deactivated team member (SUPER_ADMIN or ADMIN)."""
    team.reactivate(db, current_user, user_id)
    return {"message": "User reactivated", "user_id": user_id}


@router.delete("/{user_id}/permanent", status_code=status.HTTP_200_OK, dependencies=manage_team,
               response_model=None)
def delete_user_permanent(
    user_id: str,
    current_user: User = Depends(super_admin),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Permanently delete a deactivated team member (SUPER_ADMIN only)."""
    team.delete_permanently(db, current_user, user_id)
    return {"message": "User permanently deleted", "user_id": user_id}


@router.get("/{user_id}/permissions", response_model=None)
def get_permissions(
    user_id: str,
    current_user: User = Depends(admins),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Get effective permissions for a team member."""
    return team.permissions_view(db, current_user, user_id)


@router.put("/{user_id}/permissions", status_code=status.HTTP_200_OK, response_model=None)
def update_permissions(
    user_id: str,
    body: UpdatePermissionsRequest,
    current_user: User = Depends(super_admin),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Set custom permissions for a team member (SUPER_ADMIN only)."""
    stored = team.update_permissions(db, current_user, user_id, body.permissions)
    return {
        "message": "Permissions updated",
        "user_id": user_id,
        "permissions": stored,
    }


@router.delete("/{user_id}/permissions", status_code=status.HTTP_200_OK, response_model=None)
def reset_permissions(
    user_id: str,
    current_user: User = Depends(super_admin),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Reset a user's permissions back to their role defaults (SUPER_ADMIN only)."""
    defaults = team.reset_permissions(db, current_user, user_id)
    return {
        "message": "Permissions reset to role defaults",
        "user_id": user_id,
        "permissions": defaults,
    }


@router.get("/invitations", response_model=None)
def list_invitations(
    current_user: User = Depends(admins),
    db: Session = Depends(get_db),
) -> List[Dict[str, Any]]:
    """List all invitations for the current tenant."""
    return invitations.list_invitations(db, current_user)


@router.delete("/invitations/{invitation_id}", dependencies=manage_team, response_model=None)
def revoke_invitation(
    invitation_id: str,
    current_user: User = Depends(admins),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Revoke a pending invitation."""
    invitations.revoke_invitation(db, current_user, invitation_id)
    return {"message": "Invitation revoked"}
