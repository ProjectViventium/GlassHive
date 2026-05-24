from __future__ import annotations

import base64
import hmac
import json
import os
import time
from hashlib import sha256

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


def _signature(secret: str, message: str) -> str:
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), sha256).hexdigest()


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
