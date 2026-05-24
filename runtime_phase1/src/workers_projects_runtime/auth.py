from __future__ import annotations

import os
import re
from dataclasses import dataclass


DEFAULT_TENANT_ID = "local"
DEFAULT_AUTH_MODE = "local"
ENTERPRISE_AUTH_MODES = {
    "first_party_assertion",
    "external_oidc",
    "oauth_oidc",
    "oauth_entra",
    "oauth_direct_registration",
}
IDENTITY_HEADER_ALIASES = {
    "x-viventium-tenant-id": ("x-glasshive-tenant-id", "x-librechat-tenant-id"),
    "x-viventium-user-id": ("x-glasshive-user-id", "x-librechat-user-id"),
    "x-viventium-user-email": ("x-glasshive-user-email", "x-librechat-user-email"),
    "x-viventium-user-role": ("x-glasshive-user-role", "x-librechat-user-role"),
}


def _env_bool(name: str, default: bool = False) -> bool:
    value = str(os.environ.get(name, "")).strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on", "enabled"}


def sanitize_identity_value(value: object) -> str:
    text = str(value or "").strip()
    if text.startswith("{{") and text.endswith("}}"):
        return ""
    if text.startswith("${") and text.endswith("}"):
        return ""
    return text[:512]


def header_identity_value(headers: dict[str, str], primary: str) -> str:
    candidates = (primary, *IDENTITY_HEADER_ALIASES.get(primary, ()))
    for name in candidates:
        value = sanitize_identity_value(headers.get(name))
        if value:
            return value
    return ""


def normalize_identity_segment(value: object, fallback: str) -> str:
    text = sanitize_identity_value(value).lower()
    if not text:
        text = fallback
    text = re.sub(r"[^a-z0-9_.@-]+", "-", text).strip(".-")
    return text[:160] or fallback


@dataclass(frozen=True)
class AuthContext:
    tenant_id: str = DEFAULT_TENANT_ID
    user_id: str = ""
    email: str = ""
    role: str = ""
    auth_mode: str = DEFAULT_AUTH_MODE
    enterprise: bool = False

    @property
    def owner_id(self) -> str:
        return self.user_id

    @property
    def is_user_scoped(self) -> bool:
        return bool(self.enterprise and self.user_id)


class GlassHiveAuthError(RuntimeError):
    pass


class EnterpriseAuthSettings:
    def __init__(self) -> None:
        self.enterprise = _env_bool("GLASSHIVE_ENTERPRISE_MODE") or _env_bool("WPR_ENTERPRISE_MODE")
        self.auth_mode = os.environ.get("GLASSHIVE_AUTH_MODE", DEFAULT_AUTH_MODE).strip().lower() or DEFAULT_AUTH_MODE
        self.tenant_id = sanitize_identity_value(
            os.environ.get("GLASSHIVE_ENTERPRISE_TENANT_ID")
            or os.environ.get("WPR_ENTERPRISE_TENANT_ID")
            or DEFAULT_TENANT_ID
        )
        self.user_header = os.environ.get("GLASSHIVE_AUTH_USER_HEADER", "x-viventium-user-id").strip().lower()
        self.email_header = os.environ.get("GLASSHIVE_AUTH_EMAIL_HEADER", "x-viventium-user-email").strip().lower()
        self.role_header = os.environ.get("GLASSHIVE_AUTH_ROLE_HEADER", "x-viventium-user-role").strip().lower()
        self.tenant_header = os.environ.get("GLASSHIVE_AUTH_TENANT_HEADER", "x-viventium-tenant-id").strip().lower()
        self.external_validation_required = _env_bool(
            "GLASSHIVE_AUTH_EXTERNAL_VALIDATION_REQUIRED",
            default=True,
        )

    def validate_startup(self, *, api_token: str) -> None:
        if not self.enterprise:
            return
        if not api_token:
            raise RuntimeError("GLASSHIVE_ENTERPRISE_MODE requires WPR_API_TOKEN for fail-closed service auth")
        signed_link_secret = os.environ.get("GLASSHIVE_SIGNED_LINK_SECRET", "").strip()
        if not signed_link_secret:
            raise RuntimeError(
                "GLASSHIVE_ENTERPRISE_MODE requires GLASSHIVE_SIGNED_LINK_SECRET for scoped takeover and artifact links"
            )
        if signed_link_secret == api_token:
            raise RuntimeError("GLASSHIVE_SIGNED_LINK_SECRET must be distinct from WPR_API_TOKEN")
        if not self.tenant_id or self.tenant_id == DEFAULT_TENANT_ID:
            raise RuntimeError("GLASSHIVE_ENTERPRISE_MODE requires GLASSHIVE_ENTERPRISE_TENANT_ID")
        if self.auth_mode not in ENTERPRISE_AUTH_MODES:
            raise RuntimeError(
                "GLASSHIVE_ENTERPRISE_MODE requires GLASSHIVE_AUTH_MODE to be one of "
                + ", ".join(sorted(ENTERPRISE_AUTH_MODES))
            )
        if self.auth_mode != "first_party_assertion" and self.external_validation_required:
            raise RuntimeError(
                "GLASSHIVE_AUTH_MODE values other than first_party_assertion require an external "
                "token validator before the runtime can trust OAuth/OIDC identity assertions"
            )

    def context_from_headers(self, headers: dict[str, str]) -> AuthContext:
        if not self.enterprise:
            return AuthContext()

        asserted_tenant_id = header_identity_value(headers, self.tenant_header)
        tenant_id = self.tenant_id
        if asserted_tenant_id and asserted_tenant_id != tenant_id:
            raise GlassHiveAuthError("Tenant assertion does not match this GlassHive deployment")
        user_id = header_identity_value(headers, self.user_header)
        email = header_identity_value(headers, self.email_header)
        role = header_identity_value(headers, self.role_header)
        if not tenant_id:
            raise GlassHiveAuthError("Missing enterprise tenant assertion")
        if not user_id:
            raise GlassHiveAuthError("Missing authenticated user assertion")
        return AuthContext(
            tenant_id=tenant_id,
            user_id=user_id,
            email=email,
            role=role,
            auth_mode=self.auth_mode,
            enterprise=True,
        )


def scoped_alias(ctx: AuthContext, alias: str) -> str:
    clean_alias = normalize_identity_segment(alias, "workspace")
    if not ctx.enterprise:
        return clean_alias
    tenant = normalize_identity_segment(ctx.tenant_id, "tenant")
    user = normalize_identity_segment(ctx.user_id, "user")
    return f"{tenant}--{user}--{clean_alias}"[:240]
