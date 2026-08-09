from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Sequence

from .auth_gateway import AuthGatewayError, HumanAuthGateway


MAX_STDIN_BYTES = 64 * 1024
_IDENTITY_FIELDS = ("subject", "email", "display_name", "role")


def _stdin_payload() -> dict[str, object]:
    raw = sys.stdin.read(MAX_STDIN_BYTES + 1)
    if len(raw) > MAX_STDIN_BYTES:
        raise AuthGatewayError("Provisioning input is too large")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise AuthGatewayError("Provisioning input must be one JSON object") from exc
    if not isinstance(payload, dict):
        raise AuthGatewayError("Provisioning input must be one JSON object")
    for name in _IDENTITY_FIELDS:
        if name in payload and not isinstance(payload[name], str):
            raise AuthGatewayError(f"Provisioning field {name} must be a string")
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m glass_drive_ui.auth_admin",
        description="Admin-only GlassHive identity provisioning",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    preapprove = commands.add_parser(
        "preapprove-oidc",
        help="Preapprove one exact OIDC subject while public enrollment remains closed",
    )
    preapprove.add_argument(
        "--stdin-json",
        action="store_true",
        required=True,
        help="Read subject, email, display_name, and role from standard input",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.command != "preapprove-oidc":
            raise AuthGatewayError("Unsupported admin command")
        payload = _stdin_payload()
        gateway = HumanAuthGateway.from_env()
        principal = gateway.preapprove_oidc_principal(
            subject=str(payload.get("subject") or ""),
            email=str(payload.get("email") or ""),
            display_name=str(payload.get("display_name") or ""),
            role=str(payload.get("role") or "member"),
        )
    except (AuthGatewayError, OSError, RuntimeError, sqlite3.Error) as exc:
        print(f"Provisioning failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"status": "preapproved", "user_id": principal["user_id"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
