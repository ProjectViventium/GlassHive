from __future__ import annotations

import base64
import hmac
import json
import os
import time
from hashlib import sha256
from urllib.parse import urlencode


DEFAULT_TTL_SECONDS = 15 * 60


def signed_link_secret() -> str:
    return (
        os.environ.get("GLASSHIVE_SIGNED_LINK_SECRET", "").strip()
        or os.environ.get("WPR_API_TOKEN", "").strip()
    )


def signed_link_ttl_seconds() -> int:
    raw = os.environ.get("GLASSHIVE_SIGNED_LINK_TTL_S", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_TTL_SECONDS
    except ValueError:
        value = DEFAULT_TTL_SECONDS
    return max(60, min(value, 24 * 3600))


def _message(
    *,
    kind: str,
    worker_id: str,
    tenant_id: str,
    owner_id: str,
    path: str,
    expires_at: int,
) -> str:
    return "\0".join(
        [
            "v1",
            str(kind or ""),
            str(worker_id or ""),
            str(tenant_id or ""),
            str(owner_id or ""),
            str(path or ""),
            str(expires_at),
        ]
    )


def _signature(secret: str, message: str) -> str:
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), sha256).hexdigest()


def sign_link_params(
    *,
    kind: str,
    worker_id: str,
    tenant_id: str,
    owner_id: str,
    path: str = "",
    ttl_seconds: int | None = None,
    expires_at: int | None = None,
) -> dict[str, str]:
    secret = signed_link_secret()
    if not secret:
        return {}
    resolved_expires_at = int(expires_at) if expires_at else int(time.time()) + int(ttl_seconds or signed_link_ttl_seconds())
    message = _message(
        kind=kind,
        worker_id=worker_id,
        tenant_id=tenant_id,
        owner_id=owner_id,
        path=path,
        expires_at=resolved_expires_at,
    )
    signature = _signature(secret, message)
    return {
        "gh_kind": kind,
        "gh_exp": str(resolved_expires_at),
        "gh_sig": signature,
    }


def append_signed_query(url: str, params: dict[str, str]) -> str:
    if not params:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{urlencode(params)}"


def verify_signed_link(
    *,
    kind: str,
    worker_id: str,
    tenant_id: str,
    owner_id: str,
    path: str = "",
    expires_at: str,
    signature: str,
) -> bool:
    secret = signed_link_secret()
    if not secret or not signature:
        return False
    try:
        exp_int = int(str(expires_at or ""))
    except ValueError:
        return False
    if exp_int < int(time.time()):
        return False
    message = _message(
        kind=kind,
        worker_id=worker_id,
        tenant_id=tenant_id,
        owner_id=owner_id,
        path=path,
        expires_at=exp_int,
    )
    expected = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), sha256).hexdigest()
    return hmac.compare_digest(expected, str(signature or ""))


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _base64url_decode(value: str) -> bytes:
    padded = f"{value}{'=' * (-len(value) % 4)}"
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def sign_link_token(
    *,
    kind: str,
    worker_id: str,
    tenant_id: str,
    owner_id: str,
    path: str = "",
    ttl_seconds: int | None = None,
) -> str:
    secret = signed_link_secret()
    if not secret:
        return ""
    payload = {
        "v": 1,
        "kind": str(kind or ""),
        "worker_id": str(worker_id or ""),
        "tenant_id": str(tenant_id or ""),
        "owner_id": str(owner_id or ""),
        "path": str(path or ""),
        "exp": int(time.time()) + int(ttl_seconds or signed_link_ttl_seconds()),
    }
    encoded = _base64url_encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return f"{encoded}.{_signature(secret, encoded)}"


def verify_signed_link_token(token: str) -> dict[str, object] | None:
    secret = signed_link_secret()
    if not secret:
        return None
    parts = str(token or "").split(".", 1)
    if len(parts) != 2:
        return None
    encoded, signature = parts
    if not encoded or not hmac.compare_digest(_signature(secret, encoded), signature):
        return None
    try:
        payload = json.loads(_base64url_decode(encoded).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("v") != 1:
        return None
    try:
        expires_at = int(payload.get("exp") or 0)
    except (TypeError, ValueError):
        return None
    if expires_at < int(time.time()):
        return None
    return payload
