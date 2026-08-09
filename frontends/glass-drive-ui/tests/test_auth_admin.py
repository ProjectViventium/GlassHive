from __future__ import annotations

import io
import json

import glass_drive_ui.auth_admin as auth_admin
from glass_drive_ui.auth_gateway import HumanAuthGateway


def test_preapprove_oidc_cli_reads_private_identity_from_stdin_and_is_idempotent(
    tmp_path,
    monkeypatch,
    capsys,
):
    state_path = tmp_path / "auth.sqlite3"
    monkeypatch.setenv("GLASSHIVE_HUMAN_AUTH_MODE", "oidc")
    monkeypatch.setenv("GLASSHIVE_AUTH_STATE_PATH", str(state_path))
    monkeypatch.setenv("GLASSHIVE_OIDC_ISSUER", "https://identity.example.invalid")
    monkeypatch.setenv("GLASSHIVE_OIDC_CLIENT_ID", "public-safe-client")
    monkeypatch.setenv(
        "GLASSHIVE_OIDC_REDIRECT_URI",
        "https://glasshive.example.invalid/auth/oidc/callback",
    )
    monkeypatch.setenv("GLASSHIVE_ALLOW_PRINCIPAL_ENROLLMENT", "false")
    payload = {
        "subject": "stable-object-id",
        "email": "member@example.invalid",
        "display_name": "Example Member",
        "role": "member",
    }

    for _ in range(2):
        monkeypatch.setattr(auth_admin.sys, "stdin", io.StringIO(json.dumps(payload)))
        assert auth_admin.main(["preapprove-oidc", "--stdin-json"]) == 0

    output = capsys.readouterr().out
    assert "member@example.invalid" not in output
    assert "stable-object-id" not in output
    gateway = HumanAuthGateway.from_env()
    principals = gateway.list_principals()
    assert len(principals) == 1
    assert principals[0]["email"] == "member@example.invalid"


def test_preapprove_oidc_cli_rejects_malformed_input_without_echoing_it(
    monkeypatch,
    capsys,
):
    private_input = '{"subject":"private-subject"'
    monkeypatch.setattr(auth_admin.sys, "stdin", io.StringIO(private_input))

    assert auth_admin.main(["preapprove-oidc", "--stdin-json"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "private-subject" not in captured.err
    assert "one JSON object" in captured.err


def test_preapprove_oidc_cli_does_not_silently_reenable_disabled_account(
    tmp_path,
    monkeypatch,
    capsys,
):
    state_path = tmp_path / "auth.sqlite3"
    monkeypatch.setenv("GLASSHIVE_HUMAN_AUTH_MODE", "oidc")
    monkeypatch.setenv("GLASSHIVE_AUTH_STATE_PATH", str(state_path))
    monkeypatch.setenv("GLASSHIVE_OIDC_ISSUER", "https://identity.example.invalid")
    monkeypatch.setenv("GLASSHIVE_OIDC_CLIENT_ID", "public-safe-client")
    monkeypatch.setenv(
        "GLASSHIVE_OIDC_REDIRECT_URI",
        "https://glasshive.example.invalid/auth/oidc/callback",
    )
    gateway = HumanAuthGateway.from_env()
    principal = gateway.preapprove_oidc_principal(subject="stable-object-id", role="member")
    gateway.set_principal_disabled(principal_id=principal["user_id"], disabled=True)
    monkeypatch.setattr(
        auth_admin.sys,
        "stdin",
        io.StringIO(json.dumps({"subject": "stable-object-id", "role": "member"})),
    )

    assert auth_admin.main(["preapprove-oidc", "--stdin-json"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Account is disabled" in captured.err
    assert gateway.list_principals()[0]["disabled"] is True


def test_preapprove_oidc_cli_reports_corrupt_state_without_a_traceback(
    tmp_path,
    monkeypatch,
    capsys,
):
    state_path = tmp_path / "auth.sqlite3"
    state_path.write_text("not sqlite", encoding="utf-8")
    monkeypatch.setenv("GLASSHIVE_HUMAN_AUTH_MODE", "oidc")
    monkeypatch.setenv("GLASSHIVE_AUTH_STATE_PATH", str(state_path))
    monkeypatch.setenv("GLASSHIVE_OIDC_ISSUER", "https://identity.example.invalid")
    monkeypatch.setenv("GLASSHIVE_OIDC_CLIENT_ID", "public-safe-client")
    monkeypatch.setenv(
        "GLASSHIVE_OIDC_REDIRECT_URI",
        "https://glasshive.example.invalid/auth/oidc/callback",
    )
    monkeypatch.setattr(
        auth_admin.sys,
        "stdin",
        io.StringIO(json.dumps({"subject": "stable-object-id", "role": "member"})),
    )

    assert auth_admin.main(["preapprove-oidc", "--stdin-json"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Provisioning failed" in captured.err
    assert "Traceback" not in captured.err
    assert "stable-object-id" not in captured.err


def test_preapprove_oidc_cli_rejects_non_string_identity_fields_before_storage(
    tmp_path,
    monkeypatch,
    capsys,
):
    state_path = tmp_path / "auth.sqlite3"
    monkeypatch.setenv("GLASSHIVE_HUMAN_AUTH_MODE", "oidc")
    monkeypatch.setenv("GLASSHIVE_AUTH_STATE_PATH", str(state_path))
    monkeypatch.setenv("GLASSHIVE_OIDC_ISSUER", "https://identity.example.invalid")
    monkeypatch.setenv("GLASSHIVE_OIDC_CLIENT_ID", "public-safe-client")
    monkeypatch.setenv(
        "GLASSHIVE_OIDC_REDIRECT_URI",
        "https://glasshive.example.invalid/auth/oidc/callback",
    )
    monkeypatch.setattr(
        auth_admin.sys,
        "stdin",
        io.StringIO(json.dumps({"subject": ["not", "a", "subject"]})),
    )

    assert auth_admin.main(["preapprove-oidc", "--stdin-json"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "field subject must be a string" in captured.err
    assert not state_path.exists()


def test_preapprove_oidc_cli_bounds_state_permission_errors(
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(
        auth_admin.sys,
        "stdin",
        io.StringIO(json.dumps({"subject": "stable-object-id", "role": "member"})),
    )
    monkeypatch.setattr(
        auth_admin.HumanAuthGateway,
        "from_env",
        classmethod(lambda cls: (_ for _ in ()).throw(PermissionError("state unavailable"))),
    )

    assert auth_admin.main(["preapprove-oidc", "--stdin-json"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Provisioning failed" in captured.err
    assert "Traceback" not in captured.err
    assert "stable-object-id" not in captured.err
