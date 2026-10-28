"""Request and response bodies of the authentication API (app/routers/auth_router.py)."""

from pydantic import BaseModel, EmailStr


class RegisterRequest(BaseModel):
    first_name: str
    last_name: str
    email: EmailStr
    password: str
    tenant_name: str = "My Workspace"


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    # Optional: browser clients send it in the httpOnly refresh_token cookie instead
    refresh_token: str | None = None


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class SetPasswordRequest(BaseModel):
    new_password: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


class MagicLoginRequest(BaseModel):
    token: str


class UpdateProfileRequest(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    avatar_url: str | None = None


class GoogleCallbackRequest(BaseModel):
    credential: str  # Google ID token from frontend


class TokenResponse(BaseModel):
    # Tokens are omitted in cookie mode (X-Auth-Mode: cookie) and when a second factor is required
    access_token: str | None = None
    refresh_token: str | None = None
    token_type: str | None = None
    user: dict | None = None
    first_login: bool = False
    mfa_required: bool = False
    mfa_token: str | None = None


class MfaVerifyRequest(BaseModel):
    mfa_token: str
    code: str | None = None
    recovery_code: str | None = None


class MfaCodeRequest(BaseModel):
    code: str


class MfaDisableRequest(BaseModel):
    password: str | None = None
    code: str | None = None
    recovery_code: str | None = None


class MfaTenantPolicyRequest(BaseModel):
    require_mfa: bool
