import asyncio
import base64
import hmac
import importlib.util
import json
import logging
import os
import time
from hashlib import sha256
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import glass_drive_ui.server as server_module
from glass_drive_ui.server import create_app
from glass_drive_ui.signed_links import (
    SensitiveUrlLogFilter,
    create_signed_link_ref,
    install_sensitive_url_log_filter,
    redact_sensitive_url_text,
    resolve_signed_link_ref,
)


def worker_cookie_name(worker_id: str) -> str:
    digest = sha256(str(worker_id).encode("utf-8")).hexdigest()[:24]
    return f"glasshive_gh_token_{digest}"


@pytest.fixture(autouse=True)
def clear_glasshive_ui_env(monkeypatch, tmp_path):
    for name in (
        "WPR_API_TOKEN",
        "GLASSHIVE_DEFAULT_OWNER_ID",
        "GLASSHIVE_ENTERPRISE_MODE",
        "WPR_ENTERPRISE_MODE",
        "GLASSHIVE_AUTH_MODE",
        "GLASSHIVE_ENTERPRISE_TENANT_ID",
        "WPR_ENTERPRISE_TENANT_ID",
        "GLASSHIVE_TRUST_INBOUND_IDENTITY",
        "GLASSHIVE_OWNER_IDENTITY_CLAIMS",
        "GLASSHIVE_OWNER_IDENTITY_ALIASES_JSON",
        "GLASSHIVE_OWNER_IDENTITY_ALIASES_FILE",
        "GLASSHIVE_ALLOW_LOCAL_DEMO_OWNER",
        "GLASSHIVE_COOKIE_SECURE",
        "GLASSHIVE_SIGNED_LINK_SECRET",
        "GLASSHIVE_LINK_REF_STATE_PATH",
        "GLASSHIVE_LINK_REF_TTL_SECONDS",
        "GLASSHIVE_WORKSPACE_LINK_AUTO_RESUME",
        "GLASSHIVE_HOST_WORKERS_ENABLED",
        "GLASSHIVE_DEFAULT_WORKER_PROFILE",
        "GLASSHIVE_ALLOWED_WORKER_PROFILES",
        "GLASSHIVE_WATCH_SESSION_STATE_PATH",
        "GLASSHIVE_MAX_WATCH_SESSION_DURATION_S",
        "WPR_DEFAULT_EXECUTION_MODE",
        "WPR_ALLOWED_WORKER_PROFILES",
        "WPR_LINK_REF_TTL_SECONDS",
        "VIVENTIUM_ENV_FILE",
        "VIVENTIUM_DISABLE_DEFAULT_RUNTIME_ENV",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GLASSHIVE_LINK_REF_STATE_PATH", str(tmp_path / "link_refs.sqlite3"))
    server_module._NOVNC_VIEW_URL_CACHE.clear()
    server_module._NOVNC_ASSET_CACHE.clear()
    server_module._NOVNC_HTTP_CLIENT = None


class FakeRuntimeClient:
    def __init__(self):
        self.base_url = "http://runtime.test"
        self.header_contexts = []
        self.desktop_actions = []
        self.launch_failures = []
        self.fail_assign = False
        self.duplicate_requests = []
        self.create_project_requests = []
        self.create_worker_requests = []
        self.assign_requests = []
        self.schedule_requests = []
        self.preference_requests = []
        self.metadata_requests = []
        self.get_worker_requests = []
        self.message_requests = []
        self.steer_requests = []
        self.lifecycle_requests = []
        self.worker_live_requests = []
        self.worker_view_open_requests = []

    def health(self):
        return {"status": "ok"}

    def with_headers(self, headers: dict[str, str]):
        self.header_contexts.append(headers)
        return self

    def list_projects(self):
        return [{"project_id": "prj_1", "title": "Alpha"}]

    def list_workers(self, project_id: str):
        return [
            {"worker_id": "wrk_1", "name": "Main Worker", "profile": "codex-cli", "state": "ready"},
            {"worker_id": "wrk_dead", "name": "Old Worker", "profile": "codex-cli", "state": "terminated"},
        ]

    def get_worker(self, worker_id: str):
        self.get_worker_requests.append(worker_id)
        return {"worker_id": worker_id, "project_id": "prj_1", "profile": "codex-cli"}

    def get_project(self, project_id: str):
        return {"project_id": project_id, "title": "Alpha"}

    def get_preferences(self):
        return {
            "tenant_id": "local",
            "owner_id": "demo-owner",
            "default_worker_profile": "",
            "codex_reasoning_effort": "",
            "claude_effort": "",
            "openclaw_effort": "",
            "updated_at": "",
        }

    def update_preferences(self, payload: dict):
        self.preference_requests.append(payload)
        return {
            "tenant_id": "local",
            "owner_id": "demo-owner",
            "default_worker_profile": payload.get("default_worker_profile", ""),
            "codex_reasoning_effort": payload.get("codex_reasoning_effort", ""),
            "claude_effort": payload.get("claude_effort", ""),
            "openclaw_effort": payload.get("openclaw_effort", ""),
            "updated_at": "2026-05-24T00:00:00+00:00",
        }

    def worker_live(self, worker_id: str):
        self.worker_live_requests.append(worker_id)
        return {
            "worker": {"worker_id": worker_id, "name": "Main Worker", "project_id": "prj_1", "profile": "codex-cli", "state": "ready"},
            "runtime_details": {"view_url": "http://127.0.0.1:60812/?autoconnect=1"},
            "latest_output": "OK",
            "deliverable": {
                "kind": "webpage",
                "browser_url": "file:///workspace/project/index.html",
                "label": "index.html",
                "preferred_surface": "desktop",
            },
        }

    def create_project(self, owner_id: str, title: str, goal: str, default_worker_profile: str):
        self.create_project_requests.append(
            {
                "owner_id": owner_id,
                "title": title,
                "goal": goal,
                "default_worker_profile": default_worker_profile,
            }
        )
        return {"project_id": "prj_new"}

    def create_worker(self, project_id: str, owner_id: str, profile: str, **kwargs):
        self.create_worker_requests.append({"project_id": project_id, "owner_id": owner_id, "profile": profile, **kwargs})
        return {"worker_id": "wrk_new"}

    def duplicate_worker(self, project_id: str, source_worker_id: str, owner_id: str):
        self.duplicate_requests.append(
            {"project_id": project_id, "source_worker_id": source_worker_id, "owner_id": owner_id}
        )
        return {"worker_id": "wrk_dup"}

    def assign_run(self, worker_id: str, instruction: str):
        self.assign_requests.append({"worker_id": worker_id, "instruction": instruction})
        if self.fail_assign:
            raise RuntimeError("assign failed")
        return {"run_id": "run_1"}

    def schedule_run(self, worker_id: str, instruction: str, **kwargs):
        self.schedule_requests.append({"worker_id": worker_id, "instruction": instruction, **kwargs})
        return {"schedule_id": "sch_1", "worker_id": worker_id, "run_at": "2026-05-23T19:00:00+00:00", "state": "pending"}

    def update_worker_metadata(self, worker_id: str, payload: dict):
        self.metadata_requests.append({"worker_id": worker_id, "payload": payload})
        return {"worker_id": worker_id, "favorite": payload.get("favorite", False)}

    def launch_failed(self, worker_id: str, reason: str):
        self.launch_failures.append({"worker_id": worker_id, "reason": reason})
        return {"worker_id": worker_id, "state": "failed", "last_error": reason}

    def desktop_action(self, worker_id: str, action: str, url: str | None = None, run_id: str | None = None):
        self.desktop_actions.append({"worker_id": worker_id, "action": action, "url": url, "run_id": run_id})
        return {"status": "launched", "action": action}

    def message(self, worker_id: str, message: str):
        self.message_requests.append({"worker_id": worker_id, "message": message})
        return {"status": "queued"}

    def steer(self, worker_id: str, message: str):
        self.steer_requests.append({"worker_id": worker_id, "message": message})
        return {"run_id": "run_steer", "worker_id": worker_id, "project_id": "prj_1", "instruction": message, "state": "queued", "queued_at": "2026-04-17T00:00:00+00:00", "started_at": None, "ended_at": None, "output_text": "", "error_text": ""}

    def lifecycle(self, worker_id: str, action: str):
        self.lifecycle_requests.append({"worker_id": worker_id, "action": action})
        return {"status": action}

    def record_worker_view_open(self, worker_id: str):
        self.worker_view_open_requests.append(worker_id)
        return {}


def signed_worker_token(secret: str, *, worker_id: str = "wrk_1", tenant_id: str = "tenant-alpha", owner_id: str = "user-a") -> str:
    payload = {
        "v": 1,
        "kind": "worker_view",
        "worker_id": worker_id,
        "tenant_id": tenant_id,
        "owner_id": owner_id,
        "path": "",
        "exp": int(time.time()) + 900,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    signature = hmac.new(secret.encode("utf-8"), encoded.encode("utf-8"), sha256).hexdigest()
    return f"{encoded}.{signature}"


def worker_ref_record(url: str) -> dict[str, object]:
    assert url.startswith("/r/")
    assert "gh_token=" not in url
    record = resolve_signed_link_ref(url.rsplit("/", 1)[1])
    assert record is not None
    assert record["kind"] == "worker_view"
    assert "gh_token=" not in str(record.get("target_url") or "")
    return record


def load_runtime_signed_links_module():
    module_path = Path(__file__).resolve().parents[3] / "runtime_phase1" / "src" / "workers_projects_runtime" / "signed_links.py"
    spec = importlib.util.spec_from_file_location("glasshive_runtime_signed_links_for_ui_test", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def signed_artifact_token(
    secret: str,
    *,
    kind: str = "artifact_download",
    worker_id: str = "wrk_1",
    tenant_id: str = "tenant-alpha",
    owner_id: str = "user-a",
    path: str = "workspace/report.txt",
) -> str:
    payload = {
        "v": 1,
        "kind": kind,
        "worker_id": worker_id,
        "tenant_id": tenant_id,
        "owner_id": owner_id,
        "path": path,
        "exp": int(time.time()) + 900,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    signature = hmac.new(secret.encode("utf-8"), encoded.encode("utf-8"), sha256).hexdigest()
    return f"{encoded}.{signature}"


def set_enterprise_ui_env(
    monkeypatch,
    *,
    service_secret: str = "ui-service-secret",
    signed_secret: str = "ui-signed-link-secret",
) -> None:
    monkeypatch.setenv("WPR_API_TOKEN", service_secret)
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", signed_secret)
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")


def test_ui_loads_enterprise_service_auth_from_runtime_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / "runtime.env"
    link_ref_state = tmp_path / "shared-link-refs.sqlite3"
    env_file.write_text(
        "\n".join(
            [
                "GLASSHIVE_ENTERPRISE_MODE=true",
                "GLASSHIVE_AUTH_MODE=first_party_assertion",
                "GLASSHIVE_ENTERPRISE_TENANT_ID=tenant_public_safe",
                "GLASSHIVE_TRUST_INBOUND_IDENTITY=true",
                "WPR_API_TOKEN=service-secret",
                "GLASSHIVE_SIGNED_LINK_SECRET=signed-link-secret",
                f"GLASSHIVE_LINK_REF_STATE_PATH={link_ref_state}",
            ]
        )
    )
    monkeypatch.setenv("VIVENTIUM_ENV_FILE", str(env_file))
    monkeypatch.setenv("VIVENTIUM_DISABLE_DEFAULT_RUNTIME_ENV", "1")
    monkeypatch.delenv("GLASSHIVE_LINK_REF_STATE_PATH", raising=False)
    fake = FakeRuntimeClient()

    client = TestClient(create_app(runtime_client=fake))
    response = client.get(
        "/api/bootstrap",
        headers={
            "X-Viventium-Tenant-Id": "tenant_public_safe",
            "X-Viventium-User-Id": "qa-user",
            "X-Viventium-User-Email": "qa@example.invalid",
            "X-Viventium-User-Role": "member",
        },
    )

    assert response.status_code == 200, response.text
    assert os.environ["GLASSHIVE_LINK_REF_STATE_PATH"] == str(link_ref_state)
    assert fake.header_contexts[0]["X-WPR-Token"] == "service-secret"
    assert fake.header_contexts[0]["X-Viventium-User-Id"] == "qa-user"


def test_bootstrap_and_launch_flow():
    fake = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=fake))
    boot = client.get('/api/bootstrap')
    assert boot.status_code == 200
    assert boot.json()['new_workspace_options'][0]['value'] == 'new:codex-cli'
    assert boot.json()['new_workspace_options'][0]['label'] == 'Codex worker'
    assert boot.json()['default_launch_surface'] == 'desktop'
    assert boot.json()['default_workspace_type'] == 'sandboxed'
    assert boot.json()['workspace_type_options'][0]['label'] == 'Sandboxed Workspace'
    assert len(boot.json()['existing_workspaces']) == 1
    assert boot.json()['existing_workspaces'][0]['is_active'] is False
    assert boot.json()['existing_workspaces'][0]['is_resumable'] is True
    assert boot.json()['existing_workspaces'][0]['state_label'] == 'retained'
    assert boot.json()['existing_workspaces'][0]['watch_url'] == '/watch/wrk_1?project_id=prj_1&surface=desktop'
    assert boot.json()['existing_workspaces'][0]['project_url'] == '/ui/projects/prj_1?worker_id=wrk_1'
    assert boot.json()['existing_workspaces'][0]['desktop_url'] == '/desktop/wrk_1'
    assert boot.json()['existing_workspaces'][0]['api_url'] == '/api/worker/wrk_1'

    launch = client.post('/api/launch', json={
        'description': 'Research a self-hosted worker runtime',
        'success_criteria': 'Return three viable options',
        'context': 'Focus on resumable sandboxes',
        'workspace_option': 'new:codex-cli',
    })
    assert launch.status_code == 200
    assert launch.json()['watch_url'].startswith('/watch/wrk_new')
    assert 'surface=desktop' in launch.json()['watch_url']
    assert fake.create_worker_requests[-1]['start_synchronously'] is True


def test_launch_applies_codex_effort_to_new_workspace_bootstrap():
    fake = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=fake))

    launch = client.post('/api/launch', json={
        'description': 'Create a report',
        'success_criteria': 'Report exists',
        'workspace_option': 'new:codex-cli',
        'effort': 'xhigh',
    })

    assert launch.status_code == 200
    bundle = fake.create_worker_requests[-1]['bootstrap_bundle']
    assert bundle["env"]["WPR_CODEX_CLI_REASONING_EFFORT"] == "xhigh"


def test_launch_accepts_codex_none_effort_to_match_mcp_policy():
    fake = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=fake))

    launch = client.post('/api/launch', json={
        'description': 'Create a report',
        'success_criteria': 'Report exists',
        'workspace_option': 'new:codex-cli',
        'effort': 'none',
    })

    assert launch.status_code == 200
    bundle = fake.create_worker_requests[-1]['bootstrap_bundle']
    assert bundle["env"]["WPR_CODEX_CLI_REASONING_EFFORT"] == "none"


def test_launch_applies_claude_max_effort_to_runtime_env():
    fake = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=fake))

    launch = client.post('/api/launch', json={
        'description': 'Create a report',
        'success_criteria': 'Report exists',
        'workspace_option': 'new:claude-code',
        'effort': 'max',
    })

    assert launch.status_code == 200
    bundle = fake.create_worker_requests[-1]['bootstrap_bundle']
    assert bundle["env"]["WPR_CLAUDE_CODE_EFFORT"] == "max"
    assert "Worker effort preference" not in bundle.get("system_instructions", "")


def test_launch_watch_url_uses_short_ref_when_signed_links_enabled(monkeypatch):
    monkeypatch.setenv("WPR_API_TOKEN", "ui-service-token")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "ui-signed-link-secret")
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    launch = client.post('/api/launch', json={
        'description': 'Verify signed launch URL shape',
        'success_criteria': 'The returned watch URL is a short ref with no raw token',
        'context': '',
        'workspace_option': 'new:codex-cli',
    })

    assert launch.status_code == 200
    watch_url = launch.json()["watch_url"]
    record = worker_ref_record(watch_url)
    assert str(record["target_url"]).startswith("/watch/wrk_new?")
    assert "surface=desktop" in str(record["target_url"])
    redirect = client.get(watch_url, follow_redirects=False)
    assert redirect.status_code == 307
    assert redirect.headers["location"].startswith("/watch/wrk_new?")
    assert "gh_token=" not in redirect.headers["location"]


def test_launch_honors_available_host_workspace_type(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "true")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    fake = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=fake))

    boot = client.get("/api/bootstrap")
    assert boot.status_code == 200
    assert boot.json()["default_workspace_type"] == "host"
    assert [item["label"] for item in boot.json()["workspace_type_options"]] == [
        "Sandboxed Workspace",
        "Your Computer",
    ]

    launch = client.post(
        "/api/launch",
        json={
            "description": "Create a local marker file",
            "success_criteria": "Marker file exists",
            "workspace_option": "new:codex-cli",
            "workspace_type": "host",
        },
    )

    assert launch.status_code == 200
    assert fake.create_worker_requests[-1]["execution_mode"] == "host"


def test_bootstrap_prefers_glasshive_execution_mode_alias(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "true")
    monkeypatch.setenv("GLASSHIVE_DEFAULT_EXECUTION_MODE", "host")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    fake = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=fake))

    boot = client.get("/api/bootstrap")

    assert boot.status_code == 200
    assert boot.json()["default_workspace_type"] == "host"


def test_host_workspace_type_available_even_when_docker_is_default(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "true")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    fake = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=fake))

    boot = client.get("/api/bootstrap")
    assert boot.status_code == 200
    assert boot.json()["default_workspace_type"] == "sandboxed"
    assert [item["label"] for item in boot.json()["workspace_type_options"]] == [
        "Sandboxed Workspace",
        "Your Computer",
    ]

    launch = client.post(
        "/api/launch",
        json={
            "description": "Create a local marker file",
            "success_criteria": "Marker file exists",
            "workspace_option": "new:codex-cli",
            "workspace_type": "host",
        },
    )

    assert launch.status_code == 200
    assert fake.create_worker_requests[-1]["execution_mode"] == "host"


def test_launch_rejects_unavailable_host_workspace_type(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "false")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    fake = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=fake))

    boot = client.get("/api/bootstrap")
    assert boot.status_code == 200
    assert boot.json()["default_workspace_type"] == "sandboxed"
    assert [item["label"] for item in boot.json()["workspace_type_options"]] == ["Sandboxed Workspace"]

    launch = client.post(
        "/api/launch",
        json={
            "description": "Create a local marker file",
            "success_criteria": "Marker file exists",
            "workspace_option": "new:codex-cli",
            "workspace_type": "host",
        },
    )

    assert launch.status_code == 400
    assert "Your Computer workspaces are not available" in launch.text
    assert fake.create_worker_requests == []


def test_bootstrap_filters_worker_profiles_from_guardrail_env(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "codex-cli")
    fake = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=fake))

    boot = client.get("/api/bootstrap")

    assert boot.status_code == 200
    assert boot.json()["new_workspace_options"] == [
        {"value": "new:codex-cli", "label": "Codex worker", "profile": "codex-cli"}
    ]


def test_bootstrap_uses_configured_default_worker_profile(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "codex-cli,claude-code")
    monkeypatch.setenv("GLASSHIVE_DEFAULT_WORKER_PROFILE", "claude-code")
    fake = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=fake))

    boot = client.get("/api/bootstrap")

    assert boot.status_code == 200
    assert boot.json()["default_workspace_option"] == "new:claude-code"
    assert boot.json()["deployment_default_workspace_option"] == "new:claude-code"


def test_bootstrap_uses_saved_user_default_worker_profile():
    class PreferenceRuntime(FakeRuntimeClient):
        def get_preferences(self):
            return {
                "tenant_id": "local",
                "owner_id": "demo-owner",
                "default_worker_profile": "openclaw-general",
                "codex_reasoning_effort": "high",
                "claude_effort": "max",
                "openclaw_effort": "",
                "updated_at": "2026-05-24T00:00:00+00:00",
            }

    client = TestClient(create_app(runtime_client=PreferenceRuntime()))

    boot = client.get("/api/bootstrap")

    assert boot.status_code == 200
    assert boot.json()["default_workspace_option"] == "new:openclaw-general"
    assert boot.json()["user_preferences"]["codex_reasoning_effort"] == "high"


def test_preference_endpoint_proxies_saved_defaults():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.patch(
        "/api/preferences",
        json={"default_worker_profile": "codex-cli", "codex_reasoning_effort": "xhigh"},
    )

    assert response.status_code == 200, response.text
    assert runtime.preference_requests == [
        {"default_worker_profile": "codex-cli", "codex_reasoning_effort": "xhigh"}
    ]
    assert response.json()["default_worker_profile"] == "codex-cli"


def test_bootstrap_fails_loud_for_default_worker_profile_outside_allowlist(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "openclaw-general")
    monkeypatch.setenv("GLASSHIVE_DEFAULT_WORKER_PROFILE", "claude-code")
    fake = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=fake))

    with pytest.raises(RuntimeError, match="GLASSHIVE_DEFAULT_WORKER_PROFILE"):
        client.get("/api/bootstrap")


def test_bootstrap_fails_loud_for_allowed_profile_list_with_no_supported_profiles(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "not-a-real-worker")
    fake = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=fake))

    with pytest.raises(RuntimeError, match="GLASSHIVE_ALLOWED_WORKER_PROFILES"):
        client.get("/api/bootstrap")


def test_bootstrap_dedupes_workspace_rows_by_worker_id():
    class DuplicateRuntime(FakeRuntimeClient):
        def list_projects(self):
            return [
                {"project_id": "prj_1", "title": "Alpha"},
                {"project_id": "prj_2", "title": "Duplicate Reference"},
            ]

        def list_workers(self, project_id: str):
            return [{"worker_id": "wrk_1", "name": "Main Worker", "profile": "codex-cli", "state": "ready"}]

    client = TestClient(create_app(runtime_client=DuplicateRuntime()))

    boot = client.get("/api/bootstrap")

    assert boot.status_code == 200
    assert [item["worker_id"] for item in boot.json()["existing_workspaces"]] == ["wrk_1"]


def test_bootstrap_exposes_paused_workers_as_resumable_workspaces():
    class PausedRuntime(FakeRuntimeClient):
        def list_workers(self, project_id: str):
            return [
                {"worker_id": "wrk_idle", "name": "Idle Worker", "profile": "codex-cli", "state": "paused"},
                {"worker_id": "wrk_dead", "name": "Old Worker", "profile": "codex-cli", "state": "terminated"},
            ]

    client = TestClient(create_app(runtime_client=PausedRuntime()))

    boot = client.get("/api/bootstrap")

    assert boot.status_code == 200
    assert boot.json()["existing_workspaces"] == [
        {
            "project_id": "prj_1",
            "project_title": "Alpha",
            "worker_id": "wrk_idle",
            "name": "Idle Worker",
            "workspace_label": "Alpha",
            "profile": "codex-cli",
            "state": "paused",
            "favorite": False,
            "is_active": False,
            "is_resumable": True,
            "state_label": "paused",
            "watch_url": "/watch/wrk_idle?project_id=prj_1&surface=desktop",
            "project_url": "/ui/projects/prj_1?worker_id=wrk_idle",
            "desktop_url": "/desktop/wrk_idle",
            "api_url": "/api/worker/wrk_idle",
        }
    ]


def test_watch_assets_render():
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))
    home = client.get('/')
    assert home.status_code == 200
    assert 'GlassHive' in home.text
    assert 'Workspace' in home.text
    assert 'Define the project once. Watch the worker deliver.' in home.text
    assert 'Workspace Type' in home.text
    assert 'Run Project' in home.text
    assert 'role="tablist"' in home.text
    assert 'data-view-tab="workspaces"' in home.text
    assert 'workspace-view' in home.text
    assert 'Inactive Workspaces' in home.text
    assert 'Status Report' in home.text
    assert 'Glass Drive' not in home.text
    watch = client.get('/watch/wrk_1')
    assert watch.status_code == 200
    assert 'GlassHive' in watch.text
    assert 'Workspace live view' in watch.text
    assert 'Open project workspace' in watch.text
    assert 'Open worker details' in watch.text
    assert 'Send redirects now' in watch.text
    assert 'Hold Send or Cmd/Ctrl+Enter to queue instead' in watch.text
    assert 'Glass Drive' not in watch.text
    desktop = client.get('/desktop/wrk_1')
    assert desktop.status_code == 200
    assert 'GlassHive Desktop' in desktop.text
    live = client.get('/api/worker/wrk_1/live')
    assert live.status_code == 200
    assert live.json()['runtime_details']['view_available'] is True
    assert 'view_url' not in live.json()['runtime_details']
    assert live.json()['project_title'] == 'Alpha'


def test_worker_view_signed_token_respects_watch_session_cap(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_TTL_S", "3600")
    monkeypatch.setenv("GLASSHIVE_MAX_WATCH_SESSION_DURATION_S", "120")

    token = server_module.sign_link_token(
        kind="worker_view",
        worker_id="wrk_1",
        tenant_id="tenant-alpha",
        owner_id="user-a",
    )
    payload = server_module.verify_signed_link_token(token)

    assert payload is not None
    assert 1 <= int(payload["exp"]) - int(time.time()) <= 120


def test_signed_workspace_links_reuse_persisted_watch_session_deadline(tmp_path, monkeypatch):
    signed_secret = "ui-signed-link-secret"
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", signed_secret)
    monkeypatch.setenv("GLASSHIVE_MAX_WATCH_SESSION_DURATION_S", "60")
    monkeypatch.setenv("GLASSHIVE_WATCH_SESSION_STATE_PATH", str(tmp_path / "watch-sessions.sqlite3"))
    identity = {"tenant_id": "tenant-alpha", "user_id": "user-a"}
    now = {"value": 1_000}

    monkeypatch.setattr(server_module.time, "time", lambda: now["value"])
    monkeypatch.setattr(server_module.sign_link_token.__globals__["time"], "time", lambda: now["value"])

    first_url = server_module._append_signed_worker_token("/watch/wrk_1", "wrk_1", identity)
    first_payload = server_module.verify_signed_link_token(str(worker_ref_record(first_url)["token"]))
    assert first_payload is not None
    assert first_payload["exp"] == 1_060

    now["value"] = 1_020
    second_url = server_module._append_signed_worker_token("/watch/wrk_1", "wrk_1", identity)
    second_payload = server_module.verify_signed_link_token(str(worker_ref_record(second_url)["token"]))
    assert second_payload is not None
    assert second_payload["exp"] == 1_060

    now["value"] = 1_061
    third_url = server_module._append_signed_worker_token("/watch/wrk_1", "wrk_1", identity)
    third_payload = server_module.verify_signed_link_token(str(worker_ref_record(third_url)["token"]))
    assert third_payload is not None
    assert third_payload["exp"] == 1_121


def test_runtime_minted_watch_tokens_can_reopen_after_expired_session_deadline(tmp_path, monkeypatch):
    secret = "ui-signed-link-secret"
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", secret)
    monkeypatch.setenv("WPR_API_TOKEN", "service-token")
    monkeypatch.setenv("GLASSHIVE_MAX_WATCH_SESSION_DURATION_S", "60")
    monkeypatch.setenv("GLASSHIVE_WATCH_SESSION_STATE_PATH", str(tmp_path / "watch-sessions.sqlite3"))
    now = {"value": 1_000}

    monkeypatch.setattr(server_module.time, "time", lambda: now["value"])
    monkeypatch.setattr(server_module.sign_link_token.__globals__["time"], "time", lambda: now["value"])
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))

    first_token = server_module.sign_link_token(
        kind="worker_view",
        worker_id="wrk_1",
        tenant_id="tenant-alpha",
        owner_id="user-a",
    )
    assert client.get(f"/watch/wrk_1?gh_token={first_token}").status_code == 200

    now["value"] = 1_020
    fresh_callback_token = server_module.sign_link_token(
        kind="worker_view",
        worker_id="wrk_1",
        tenant_id="tenant-alpha",
        owner_id="user-a",
    )
    assert client.get(f"/watch/wrk_1?gh_token={fresh_callback_token}").status_code == 200

    now["value"] = 1_061
    expired_original_response = client.get(f"/watch/wrk_1?gh_token={first_token}")
    assert expired_original_response.status_code == 401

    expired_session_callback_token = server_module.sign_link_token(
        kind="worker_view",
        worker_id="wrk_1",
        tenant_id="tenant-alpha",
        owner_id="user-a",
    )
    reopened_response = client.get(f"/watch/wrk_1?gh_token={expired_session_callback_token}")
    assert reopened_response.status_code == 200

    main_ui_url = server_module._append_signed_worker_token(
        "/watch/wrk_1",
        "wrk_1",
        {"tenant_id": "tenant-alpha", "user_id": "user-a"},
    )
    assert client.get(main_ui_url).status_code == 200


def test_active_novnc_websocket_closes_at_watch_session_cap(tmp_path, monkeypatch):
    secret = "signed-link-secret"
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", secret)
    monkeypatch.setenv("GLASSHIVE_MAX_WATCH_SESSION_DURATION_S", "1")
    monkeypatch.setenv("GLASSHIVE_WATCH_SESSION_STATE_PATH", str(tmp_path / "watch-sessions.sqlite3"))
    token = signed_worker_token(secret)
    upstreams = []

    class FakeUpstream:
        def __init__(self):
            self.closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            self.closed = True
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(30)
            raise StopAsyncIteration

        async def send(self, message):
            _ = message

        async def close(self):
            self.closed = True

    def fake_connect(*args, **kwargs):
        _ = args, kwargs
        upstream = FakeUpstream()
        upstreams.append(upstream)
        return upstream

    monkeypatch.setattr(server_module.websockets, "connect", fake_connect)
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))

    with client.websocket_connect(f"/novnc/wrk_1/websockify?gh_token={token}") as websocket:
        with pytest.raises(WebSocketDisconnect) as exc:
            websocket.receive_text()

    assert exc.value.code == 1008
    assert upstreams and upstreams[0].closed is True


def test_launcher_workspace_hive_static_controls():
    static_dir = Path(server_module.__file__).parent / "static"
    app_js = (static_dir / "app.js").read_text(encoding="utf-8")
    desktop_js = (static_dir / "desktop.js").read_text(encoding="utf-8")
    index_html = (static_dir / "index.html").read_text(encoding="utf-8")
    styles_css = (static_dir / "styles.css").read_text(encoding="utf-8")
    watch_html = (static_dir / "watch.html").read_text(encoding="utf-8")
    watch_js = (static_dir / "watch.js").read_text(encoding="utf-8")
    desktop_html = (static_dir / "desktop.html").read_text(encoding="utf-8")
    assert "workspace-live-frame" in app_js
    assert "MAX_LIVE_TILE_IFRAMES" in app_js
    assert "RETAINED_TILE_REFRESH_MS" in app_js
    assert "dataset.nextLiveRefreshAt" in app_js
    assert "document.hidden" in app_js
    assert "workspaceRefreshInFlight" in app_js
    assert "withAuth('/api/bootstrap')" in app_js
    assert "show-workspace-status" in index_html
    assert "show-workspace-watch" in index_html
    assert "workerApiUrl(workerId, '/steer')" in app_js
    assert "workerApiUrl(workerId, `/action/${encodeURIComponent(action)}`)" in app_js
    assert "workerApiUrl(workerId, '/metadata')" in app_js
    assert "appendUrlPath" in app_js
    assert "deployment_default_workspace_option" in app_js
    assert "dataset.watchVisible !== 'false'" in app_js
    assert "dataset.watchVisible === 'true' || tile.dataset.statusVisible === 'true'" in app_js
    assert "Full watch" in app_js
    assert "workspace-status-button" in app_js
    assert "Open latest workspace output" in app_js
    assert "Open status" in app_js
    assert "Status Report" in index_html
    assert "Inactive Workspaces" in index_html
    assert "glasshive:duplicate-workspace" in app_js
    assert "Duplicate selected workspace" in app_js
    assert "/novnc/${workerId}/websockify" in desktop_js
    assert "runtime.view_available" in desktop_js
    assert "desktopRefreshInFlight" in desktop_js
    assert "scheduleDesktopRefresh" in desktop_js
    assert "isSettledWorkspaceState" in desktop_js
    assert "viewHealthHealthy" in desktop_js
    assert "ACTIVE_WORKER_STATES" in desktop_js
    assert "showSettledWorkspaceStatus" in desktop_js
    assert "settledDesktopSuppressed" in desktop_js
    assert "Workspace complete" in desktop_js
    assert "The latest output and workspace files are available from the status panel" in desktop_js
    assert "Clipboard sync: inactive until workspace resumes" in desktop_js
    assert "desktop.js?v=20260625a" in desktop_html
    assert "}, 5000);" not in desktop_js
    assert 'id="project-files"' in index_html
    assert 'id="schedule-text"' in index_html
    assert 'id="schedule-button"' in index_html
    assert 'id="workspace-type"' in index_html
    assert 'Initial watch surface' not in index_html
    assert "renderWorkspaceTypeOptions" in app_js
    assert "idle_terminated" in app_js
    assert "stopped" in app_js
    assert "resumable" in app_js
    assert "Worker completed" in watch_js
    assert "Workspace paused" in watch_js
    assert "Use Resume to continue from the same state" in watch_js
    assert "IDLE_REFRESH_MS" in watch_js
    assert "refreshInFlight" in watch_js
    assert "const GLASSHIVE_UI_REV = '20260626a'" in watch_js
    assert "const workspaceApiBase = `/api/workspace/${workerId}`" in watch_js
    assert "/api/worker/${workerId}/live" not in watch_js
    assert '@app.get("/api/workspace/{worker_id}/live")' in (Path(server_module.__file__).read_text(encoding="utf-8"))
    assert "const connecting = !completed && attachStartedAt" in watch_js
    assert "function filePreviewUrl()" in watch_js
    assert "function fileDeliverableKey(deliverable, runId)" in watch_js
    assert "function isFilePreviewUrl(url)" in watch_js
    assert "lastAttachedFilePreviewKey === filePreviewKey" in watch_js
    assert "if (!isFilePreviewUrl(url))" in watch_js
    assert "currentDeliverable?.kind === 'file'" in watch_js
    assert "syncResultActions(currentDeliverable)" in watch_js
    assert 'id="artifact-list"' in watch_html
    assert "function syncArtifactList(items)" in watch_js
    assert "function liveProgressText(data)" in watch_js
    assert "consolePayload.stdout || consolePayload.stderr" in watch_js
    assert "Live progress:" in watch_js
    assert "gh_token|gh_sig|token|signature|sig" in watch_js
    assert "data.artifacts?.items || []" in watch_js
    assert "Workspace files" in watch_js
    assert "watch.js?v=20260626a" in watch_html
    assert ".artifact-row" in styles_css
    assert "artifact-list-more" in watch_js
    assert ".artifact-list-more" in styles_css
    assert 'aria-controls="result-panel"' in watch_html
    assert "result-toggle-action" in watch_html
    assert "Close latest workspace output status" in watch_js
    assert ".result-toggle-action" in styles_css
    assert ".workspace-status-button" in styles_css
    assert "Open current desktop in new tab" in watch_js
    assert "if (previewUrl) return previewUrl" not in watch_js
    assert "setInterval(refresh" not in watch_js
    assert "Workspace resuming" not in watch_js
    assert 'grid-template-areas:' in styles_css
    assert '"brand controls"' in styles_css
    assert '.watch-meta-line p' in styles_css
    assert 'white-space: normal' in styles_css


def test_worker_lifecycle_endpoint_supports_workspace_hive_controls():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.post("/api/worker/wrk_1/action/resume")

    assert response.status_code == 200
    assert response.json()["status"] == "resume"
    assert runtime.lifecycle_requests == [{"worker_id": "wrk_1", "action": "resume"}]


def test_launch_uses_desktop_surface_for_browser_projects():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    launch = client.post('/api/launch', json={
        'description': 'Create a hello world landing page and verify it renders in the browser',
        'success_criteria': 'The page is visible and renders HELLO WORLD',
        'context': '',
        'workspace_option': 'new:codex-cli',
    })
    assert launch.status_code == 200
    assert 'surface=desktop' in launch.json()['watch_url']
    assert runtime.desktop_actions == [
        {'worker_id': 'wrk_new', 'action': 'terminal', 'url': None, 'run_id': 'run_1'},
    ]


def test_launch_preopens_browser_for_explicit_external_navigation():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    launch = client.post('/api/launch', json={
        'description': 'Open the browser to https://example.com and inspect the page',
        'success_criteria': 'The page is visible and the title is captured',
        'context': '',
        'workspace_option': 'new:codex-cli',
    })
    assert launch.status_code == 200
    assert 'surface=desktop' in launch.json()['watch_url']
    assert runtime.desktop_actions == [
        {'worker_id': 'wrk_new', 'action': 'browser', 'url': 'https://example.com', 'run_id': None},
        {'worker_id': 'wrk_new', 'action': 'terminal', 'url': None, 'run_id': 'run_1'},
    ]


def test_browser_action_accepts_explicit_url():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    response = client.post('/api/worker/wrk_1/action/browser', json={'url': 'file:///workspace/project/index.html'})
    assert response.status_code == 200
    assert runtime.desktop_actions[-1] == {
        'worker_id': 'wrk_1',
        'action': 'browser',
        'url': 'file:///workspace/project/index.html',
        'run_id': None,
    }


def test_worker_action_surfaces_runtime_conflict_cleanly():
    class ConflictRuntime(FakeRuntimeClient):
        def desktop_action(self, worker_id: str, action: str, url: str | None = None, run_id: str | None = None):
            response = httpx.Response(409, json={"detail": "Workspace is not ready for browser action"})
            raise httpx.HTTPStatusError("conflict", request=httpx.Request("POST", "http://runtime.test"), response=response)

    client = TestClient(create_app(runtime_client=ConflictRuntime()))

    response = client.post('/api/worker/wrk_1/action/browser', json={'url': 'file:///workspace/project/index.html'})

    assert response.status_code == 409
    assert response.json()["detail"] == "Workspace is not ready for browser action"


def test_launch_respects_explicit_terminal_surface_override():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    launch = client.post('/api/launch', json={
        'description': 'Research a self-hosted worker runtime',
        'success_criteria': 'Return three viable options',
        'context': '',
        'workspace_option': 'new:codex-cli',
        'launch_surface': 'terminal',
    })
    assert launch.status_code == 200
    assert 'surface=terminal' in launch.json()['watch_url']
    assert runtime.desktop_actions == []


def test_launch_failure_marks_new_worker_failed():
    runtime = FakeRuntimeClient()
    runtime.fail_assign = True
    client = TestClient(create_app(runtime_client=runtime))
    launch = client.post('/api/launch', json={
        'description': 'Research a self-hosted worker runtime',
        'success_criteria': 'Return three viable options',
        'context': '',
        'workspace_option': 'new:codex-cli',
    })
    assert launch.status_code == 502
    assert runtime.launch_failures == [{'worker_id': 'wrk_new', 'reason': 'assign failed'}]


def test_launch_duplicate_workspace_uses_runtime_duplicate_endpoint():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    launch = client.post('/api/launch', json={
        'description': 'Branch the existing workspace for a parallel experiment',
        'success_criteria': 'The experiment starts in a duplicated workspace',
        'context': '',
        'workspace_option': 'duplicate:wrk_1',
    })
    assert launch.status_code == 200
    assert launch.json()['watch_url'].startswith('/watch/wrk_dup')
    assert runtime.duplicate_requests == [
        {'project_id': 'prj_new', 'source_worker_id': 'wrk_1', 'owner_id': 'demo-owner'},
    ]


def test_launch_open_workspace_reuses_existing_worker():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    launch = client.post('/api/launch', json={
        'description': 'Resume the existing workspace for another task',
        'success_criteria': 'The same workspace starts a new run',
        'context': '',
        'workspace_option': 'open:wrk_1',
        'launch_surface': 'terminal',
    })
    assert launch.status_code == 200
    assert launch.json()['watch_url'].startswith('/watch/wrk_1')
    assert runtime.get_worker_requests == ['wrk_1']
    assert runtime.create_project_requests == []
    assert runtime.create_worker_requests == []
    assert runtime.duplicate_requests == []
    assert runtime.assign_requests[0]['worker_id'] == 'wrk_1'


def test_launch_accepts_legacy_worker_option_fallback():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    launch = client.post('/api/launch', json={
        'description': 'Resume through the legacy worker option fallback',
        'success_criteria': 'The same workspace starts a new run',
        'context': '',
        'worker_option': 'open:wrk_1',
        'launch_surface': 'terminal',
    })
    assert launch.status_code == 200
    assert launch.json()['watch_url'].startswith('/watch/wrk_1')
    assert runtime.create_project_requests == []
    assert runtime.assign_requests[0]['worker_id'] == 'wrk_1'


def test_novnc_proxy_uses_worker_view_origin(monkeypatch):
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    class FakeUpstreamResponse:
        status_code = 200
        content = b'export default "ok";'
        headers = {'content-type': 'text/javascript'}

    class FakeHttpxClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url: str):
            assert url == 'http://127.0.0.1:60812/core/rfb.js'
            return FakeUpstreamResponse()

    monkeypatch.setattr(server_module.httpx, 'Client', FakeHttpxClient)
    response = client.get('/novnc/wrk_1/core/rfb.js')
    assert response.status_code == 200
    assert response.text == 'export default "ok";'


def test_novnc_proxy_caches_authorized_view_origin_and_static_assets(monkeypatch):
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    requested_urls = []

    class FakeUpstreamResponse:
        status_code = 200
        content = b'export default "cached";'
        headers = {'content-type': 'text/javascript'}

    class FakeHttpxClient:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, url: str):
            requested_urls.append(url)
            return FakeUpstreamResponse()

    monkeypatch.setattr(server_module.httpx, 'Client', FakeHttpxClient)

    first = client.get('/novnc/wrk_1/core/rfb.js')
    second = client.get('/novnc/wrk_1/core/rfb.js')

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.text == second.text == 'export default "cached";'
    assert runtime.worker_live_requests == ['wrk_1']
    assert requested_urls == ['http://127.0.0.1:60812/core/rfb.js']


def test_signed_watch_token_authenticates_runtime_calls(monkeypatch):
    secret = "ui-signed-link-secret"
    monkeypatch.setenv("WPR_API_TOKEN", secret)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    token = signed_worker_token(secret)
    response = client.get(f'/api/worker/wrk_1/live?gh_token={token}')

    assert response.status_code == 200
    assert runtime.header_contexts[-1]["X-WPR-Token"] == secret
    assert runtime.header_contexts[-1]["X-Viventium-Tenant-Id"] == "tenant-alpha"
    assert runtime.header_contexts[-1]["X-Viventium-User-Id"] == "user-a"
    assert runtime.header_contexts[-1]["X-Viventium-User-Role"] == "operator"


def test_bootstrap_signs_workspace_links_in_enterprise_mode(monkeypatch):
    signed_secret = "ui-signed-link-secret"
    set_enterprise_ui_env(monkeypatch, signed_secret=signed_secret)
    monkeypatch.setenv("GLASSHIVE_TRUST_INBOUND_IDENTITY", "true")
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    headers = {
        "X-Viventium-Tenant-Id": "tenant-alpha",
        "X-Viventium-User-Id": "user-a",
        "X-Viventium-User-Role": "member",
    }

    response = client.get("/api/bootstrap", headers=headers)

    assert response.status_code == 200
    workspace = response.json()["existing_workspaces"][0]
    watch_ref = worker_ref_record(workspace["watch_url"])
    project_ref = worker_ref_record(workspace["project_url"])
    desktop_ref = worker_ref_record(workspace["desktop_url"])
    api_ref = worker_ref_record(workspace["api_url"])
    assert str(watch_ref["target_url"]).startswith("/watch/wrk_1?")
    assert str(project_ref["target_url"]).startswith("/ui/projects/prj_1?")
    assert str(desktop_ref["target_url"]) == "/desktop/wrk_1"
    assert str(api_ref["target_url"]) == "/api/worker/wrk_1"
    assert client.get(workspace["watch_url"], headers=headers).status_code == 200
    assert client.get(workspace["desktop_url"], headers=headers).status_code == 200


def test_signed_watch_token_is_worker_scoped(monkeypatch):
    secret = "ui-signed-link-secret"
    monkeypatch.setenv("WPR_API_TOKEN", secret)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    token = signed_worker_token(secret, worker_id="wrk_other")
    response = client.get(f'/api/worker/wrk_1/live?gh_token={token}')

    assert response.status_code == 403
    assert runtime.header_contexts == []


def test_signed_watch_token_is_worker_scoped_for_control_and_desktop_routes(monkeypatch):
    secret = "ui-signed-link-secret"
    monkeypatch.setenv("WPR_API_TOKEN", secret)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    token = signed_worker_token(secret, worker_id="wrk_other")
    probes = [
        ("post", "/api/worker/wrk_1/steer", {"message": "do not cross workers"}),
        ("post", "/api/worker/wrk_1/message", {"message": "do not cross workers"}),
        ("post", "/api/worker/wrk_1/action/pause", None),
        ("post", "/api/worker/wrk_1/action/resume", None),
        ("post", "/api/worker/wrk_1/action/interrupt", None),
        ("post", "/api/worker/wrk_1/action/terminate", None),
        ("get", "/desktop/wrk_1", None),
        ("get", "/novnc/wrk_1/core/rfb.js", None),
    ]

    for method, path, body in probes:
        request = getattr(client, method)
        response = request(f"{path}?gh_token={token}", json=body) if body is not None else request(f"{path}?gh_token={token}")
        assert response.status_code == 403, path

    assert runtime.header_contexts == []


def test_runtime_proxy_strips_signed_query_params_before_upstream(monkeypatch):
    service_secret = "ui-service-secret"
    signed_secret = "ui-signed-link-secret"
    set_enterprise_ui_env(monkeypatch, service_secret=service_secret, signed_secret=signed_secret)
    token = signed_worker_token(signed_secret)
    captured = {}

    class FakeUpstreamResponse:
        status_code = 200
        content = b"{}"
        headers = {"content-type": "application/json"}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, headers=None, content=None):
            captured.update({"method": method, "url": url, "headers": headers or {}, "content": content})
            return FakeUpstreamResponse()

    monkeypatch.setattr(server_module.httpx, "AsyncClient", FakeAsyncClient)
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))

    response = client.get(
        f"/v1/workers/wrk_1/live?worker_id=wrk_1&gh_token={token}&gh_kind=worker_view&gh_exp=123&gh_sig=abc&path=outputs%2Freport.txt"
    )

    assert response.status_code == 200
    assert "gh_token" not in captured["url"]
    assert "gh_kind" not in captured["url"]
    assert "gh_exp" not in captured["url"]
    assert "gh_sig" not in captured["url"]
    assert captured["url"] == "http://runtime.test/v1/workers/wrk_1/live?worker_id=wrk_1&path=outputs%2Freport.txt"
    assert captured["headers"]["X-WPR-Token"] == service_secret
    assert captured["headers"]["X-Viventium-User-Id"] == "user-a"


def test_novnc_submodule_imports_can_inherit_signed_token_from_referer(monkeypatch):
    secret = "ui-signed-link-secret"
    monkeypatch.setenv("WPR_API_TOKEN", secret)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    class FakeUpstreamResponse:
        status_code = 200
        content = b'export default "ok";'
        headers = {'content-type': 'text/javascript'}

    class FakeHttpxClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url: str):
            assert url == 'http://127.0.0.1:60812/core/util/int.js'
            return FakeUpstreamResponse()

    monkeypatch.setattr(server_module.httpx, 'Client', FakeHttpxClient)
    token = signed_worker_token(secret)
    response = client.get(
        '/novnc/wrk_1/core/util/int.js',
        headers={'referer': f'http://glasshive.example.test/novnc/wrk_1/core/rfb.js?gh_token={token}'},
    )

    assert response.status_code == 200
    assert runtime.header_contexts[-1]["X-Viventium-User-Id"] == "user-a"


def test_signed_watch_sets_worker_scoped_cookie(monkeypatch):
    secret = "ui-signed-link-secret"
    monkeypatch.setenv("WPR_API_TOKEN", secret)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    token = signed_worker_token(secret)
    response = client.get(f'/watch/wrk_1?gh_token={token}')

    assert response.status_code == 200
    set_cookie = response.headers["set-cookie"]
    assert f"{worker_cookie_name('wrk_1')}=" in set_cookie
    assert "glasshive_gh_token_wrk_1=" not in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert runtime.lifecycle_requests == []


def test_short_worker_view_ref_can_auto_resume_when_configured(monkeypatch):
    secret = "ui-signed-link-secret"
    monkeypatch.setenv("WPR_API_TOKEN", secret)
    monkeypatch.setenv("GLASSHIVE_WORKSPACE_LINK_AUTO_RESUME", "true")
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    token = signed_worker_token(secret)
    target_url = f"http://testserver/watch/wrk_1?surface=desktop&gh_token={token}"
    ref_id = create_signed_link_ref(token=token, target_url=target_url)

    response = client.get(f"/r/{ref_id}", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "http://testserver/watch/wrk_1?surface=desktop"
    assert "gh_token=" not in response.headers["location"]
    assert runtime.worker_view_open_requests == ["wrk_1"]
    assert runtime.lifecycle_requests == [{"worker_id": "wrk_1", "action": "resume"}]


def test_short_worker_view_ref_redirects_and_sets_worker_cookie(monkeypatch):
    secret = "ui-signed-link-secret"
    monkeypatch.setenv("WPR_API_TOKEN", secret)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    token = signed_worker_token(secret)
    target_url = f"http://testserver/watch/wrk_1?surface=desktop&gh_token={token}"
    ref_id = create_signed_link_ref(token=token, target_url=target_url)

    response = client.get(f"/r/{ref_id}", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "http://testserver/watch/wrk_1?surface=desktop"
    assert "gh_token=" not in response.headers["location"]
    assert f"/r/{ref_id}" not in target_url
    set_cookie = response.headers["set-cookie"]
    assert f"{worker_cookie_name('wrk_1')}=" in set_cookie
    assert "glasshive_gh_token_wrk_1=" not in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie


def test_short_worker_view_ref_rejects_unconfigured_absolute_redirect_target(monkeypatch):
    secret = "ui-signed-link-secret"
    monkeypatch.setenv("WPR_API_TOKEN", secret)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    token = signed_worker_token(secret)
    ref_id = create_signed_link_ref(
        token=token,
        target_url=f"https://unexpected.example.test/watch/wrk_1?gh_token={token}",
    )

    response = client.get(f"/r/{ref_id}", follow_redirects=False)

    assert response.status_code == 403
    assert "target is not allowed" in response.text


@pytest.mark.parametrize(
    "target_url",
    [
        "//unexpected.example.test/watch/wrk_1",
        "////unexpected.example.test/watch/wrk_1",
        r"/\unexpected.example.test/watch/wrk_1",
    ],
)
def test_short_worker_view_ref_rejects_relative_redirect_bypass_targets(monkeypatch, target_url):
    secret = "ui-signed-link-secret"
    monkeypatch.setenv("WPR_API_TOKEN", secret)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    token = signed_worker_token(secret)
    ref_id = create_signed_link_ref(token=token, target_url=target_url)

    response = client.get(f"/r/{ref_id}", follow_redirects=False)

    assert response.status_code == 400
    assert "target path is not allowed" in response.text


def test_short_worker_view_ref_allows_explicit_redirect_host_allowlist(monkeypatch):
    secret = "ui-signed-link-secret"
    monkeypatch.setenv("WPR_API_TOKEN", secret)
    monkeypatch.setenv("GLASSHIVE_ALLOWED_REDIRECT_HOSTS", "allowed.example.test, https://other.example.test")
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    token = signed_worker_token(secret)
    ref_id = create_signed_link_ref(
        token=token,
        target_url=f"https://allowed.example.test/watch/wrk_1?surface=desktop&gh_token={token}",
    )

    response = client.get(f"/r/{ref_id}", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "https://allowed.example.test/watch/wrk_1?surface=desktop"
    assert "gh_token=" not in response.headers["location"]


def test_enterprise_short_worker_view_ref_bootstraps_direct_link_and_rechecks_asserted_owner(monkeypatch):
    secret = "ui-signed-link-secret"
    set_enterprise_ui_env(monkeypatch, signed_secret=secret)
    monkeypatch.setenv("GLASSHIVE_TRUST_INBOUND_IDENTITY", "true")
    monkeypatch.setenv("GLASSHIVE_MAX_WATCH_SESSION_DURATION_S", "300")
    now = {"value": 1_000}
    monkeypatch.setattr(server_module.time, "time", lambda: now["value"])
    monkeypatch.setattr(server_module.sign_link_token.__globals__["time"], "time", lambda: now["value"])
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    token = server_module.sign_link_token(
        kind="worker_view",
        worker_id="wrk_1",
        tenant_id="tenant-alpha",
        owner_id="user-a",
        ttl_seconds=60,
    )
    ref_id = create_signed_link_ref(
        token=token,
        target_url=f"http://testserver/watch/wrk_1?surface=desktop&gh_token={token}",
    )
    assert ref_id

    now["value"] = 2_000
    direct_response = client.get(f"/r/{ref_id}", follow_redirects=False)
    assert direct_response.status_code == 401
    assert "authenticated user assertion" in direct_response.text
    assert runtime.worker_view_open_requests == []
    wrong_user = {
        "X-GlassHive-Tenant-Id": "tenant-alpha",
        "X-GlassHive-User-Id": "user-b",
        "X-GlassHive-User-Email": "user-a",
        "X-GlassHive-User-Role": "member",
    }
    assert client.get(f"/r/{ref_id}", headers=wrong_user, follow_redirects=False).status_code == 404
    monkeypatch.setenv("GLASSHIVE_OWNER_IDENTITY_CLAIMS", "user_id,email")
    email_claim_response = client.get(f"/r/{ref_id}", headers=wrong_user, follow_redirects=False)
    assert email_claim_response.status_code == 307
    assert email_claim_response.headers["location"] == "http://testserver/watch/wrk_1?surface=desktop"
    monkeypatch.setenv("GLASSHIVE_OWNER_IDENTITY_CLAIMS", "user_id")
    monkeypatch.setenv("GLASSHIVE_OWNER_IDENTITY_ALIASES_JSON", json.dumps({"user-a": ["user-b"]}))
    alias_response = client.get(f"/r/{ref_id}", headers=wrong_user, follow_redirects=False)
    assert alias_response.status_code == 307
    assert alias_response.headers["location"] == "http://testserver/watch/wrk_1?surface=desktop"
    wrong_tenant = {**wrong_user, "X-GlassHive-Tenant-Id": "tenant-beta"}
    assert client.get(f"/r/{ref_id}", headers=wrong_tenant, follow_redirects=False).status_code in {401, 404}
    monkeypatch.setenv("GLASSHIVE_OWNER_IDENTITY_ALIASES_JSON", json.dumps({"*": ["user-b"]}))
    assert client.get(f"/r/{ref_id}", headers=wrong_user, follow_redirects=False).status_code == 404
    monkeypatch.delenv("GLASSHIVE_OWNER_IDENTITY_ALIASES_JSON", raising=False)
    response = client.get(
        f"/r/{ref_id}",
        headers={
            "X-GlassHive-Tenant-Id": "tenant-alpha",
            "X-GlassHive-User-Id": "user-a",
            "X-GlassHive-User-Role": "member",
        },
        follow_redirects=False,
    )
    assert response.status_code == 307
    assert response.headers["location"] == "http://testserver/watch/wrk_1?surface=desktop"
    set_cookie = response.headers["set-cookie"]
    cookie_value = set_cookie.split(f"{worker_cookie_name('wrk_1')}=", 1)[1].split(";", 1)[0]
    assert cookie_value != token
    refreshed_payload = server_module.verify_signed_link_token(cookie_value)
    assert refreshed_payload is not None
    assert refreshed_payload["worker_id"] == "wrk_1"
    assert refreshed_payload["owner_id"] == "user-a"
    assert runtime.worker_view_open_requests == ["wrk_1", "wrk_1", "wrk_1"]
    assert runtime.header_contexts[-1]["X-Viventium-Tenant-Id"] == "tenant-alpha"
    assert runtime.header_contexts[-1]["X-Viventium-User-Id"] == "user-a"


def test_ui_resolves_runtime_created_short_ref_when_state_path_is_shared(monkeypatch):
    secret = "ui-signed-link-secret"
    set_enterprise_ui_env(monkeypatch, signed_secret=secret)
    monkeypatch.setenv("GLASSHIVE_TRUST_INBOUND_IDENTITY", "true")
    runtime_signed_links = load_runtime_signed_links_module()
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    token = runtime_signed_links.sign_link_token(
        kind="worker_view",
        worker_id="wrk_1",
        tenant_id="tenant-alpha",
        owner_id="user-a",
    )
    ref_id = runtime_signed_links.create_signed_link_ref(
        token=token,
        target_url=f"http://testserver/watch/wrk_1?surface=desktop&gh_token={token}",
    )
    assert ref_id

    response = client.get(
        f"/r/{ref_id}",
        headers={
            "X-GlassHive-Tenant-Id": "tenant-alpha",
            "X-GlassHive-User-Id": "user-a",
            "X-GlassHive-User-Role": "member",
        },
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert response.headers["location"] == "http://testserver/watch/wrk_1?surface=desktop"
    assert "gh_token=" not in response.headers["location"]


def test_short_worker_view_ref_ttl_config_can_expire_refs(monkeypatch):
    secret = "ui-signed-link-secret"
    monkeypatch.setenv("WPR_API_TOKEN", secret)
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", secret)
    monkeypatch.setenv("GLASSHIVE_LINK_REF_TTL_SECONDS", "30")
    now = {"value": 1_000}
    monkeypatch.setattr(server_module.time, "time", lambda: now["value"])
    monkeypatch.setattr(server_module.sign_link_token.__globals__["time"], "time", lambda: now["value"])
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))
    token = server_module.sign_link_token(
        kind="worker_view",
        worker_id="wrk_1",
        tenant_id="tenant-alpha",
        owner_id="user-a",
    )
    ref_id = create_signed_link_ref(token=token, target_url=f"http://testserver/watch/wrk_1?gh_token={token}")
    record = resolve_signed_link_ref(ref_id)
    assert record is not None
    assert record["expires_at"] == 1_030

    now["value"] = 1_031
    response = client.get(f"/r/{ref_id}", follow_redirects=False)
    assert response.status_code == 401


def test_ui_sensitive_url_log_filter_redacts_signed_tokens():
    raw = (
        'GET /novnc/wrk_1/websockify?gh_token=secret-token&gh_sig=signature&gh_exp=123 '
        'GET /v1/signed-links/opaque-token?download=1'
    )

    cookie = f"Set-Cookie: {worker_cookie_name('wrk_1')}=worker-secret; HttpOnly; SameSite=lax"

    assert redact_sensitive_url_text(f"{raw} {cookie}") == (
        'GET /novnc/wrk_1/websockify?gh_token=[redacted]&gh_sig=[redacted]&gh_exp=[redacted] '
        'GET /v1/signed-links/[redacted]?download=1 '
        f"Set-Cookie: {worker_cookie_name('wrk_1')}=[redacted]; HttpOnly; SameSite=lax"
    )
    assert redact_sensitive_url_text("gh_sig=signature&gh_token=secret-token") == (
        "gh_sig=[redacted]&gh_token=[redacted]"
    )
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg="%s",
        args=(f"{raw} {cookie}",),
        exc_info=None,
    )
    assert SensitiveUrlLogFilter().filter(record) is True
    assert "secret-token" not in record.args[0]
    assert "opaque-token" not in record.args[0]
    assert "worker-secret" not in record.args[0]
    assert "gh_token=[redacted]" in record.args[0]
    assert f"{worker_cookie_name('wrk_1')}=[redacted]" in record.args[0]


def test_ui_sensitive_url_log_filter_installs_for_child_loggers(caplog):
    install_sensitive_url_log_filter()
    raw = "https://glasshive.example.test/watch/wrk_1?gh_token=secret-token&gh_sig=signature"
    logger = logging.getLogger("glass_drive_ui.server")

    with caplog.at_level(logging.INFO, logger="glass_drive_ui.server"):
        logger.info("opening %s", raw, extra={"target_url": raw})

    assert "secret-token" not in caplog.text
    assert "gh_token=[redacted]" in caplog.text
    assert caplog.records
    assert getattr(caplog.records[-1], "target_url") == (
        "https://glasshive.example.test/watch/wrk_1?gh_token=[redacted]&gh_sig=[redacted]"
    )


def test_signed_watch_does_not_set_cookie_for_different_worker(monkeypatch):
    secret = "ui-signed-link-secret"
    monkeypatch.setenv("WPR_API_TOKEN", secret)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    token = signed_worker_token(secret, worker_id="wrk_other")
    response = client.get(f'/watch/wrk_1?gh_token={token}')

    assert response.status_code == 403
    assert "set-cookie" not in response.headers


def test_novnc_submodule_imports_can_inherit_signed_token_from_cookie(monkeypatch):
    secret = "ui-signed-link-secret"
    monkeypatch.setenv("WPR_API_TOKEN", secret)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    class FakeUpstreamResponse:
        status_code = 200
        content = b'export default "ok";'
        headers = {'content-type': 'text/javascript'}

    class FakeHttpxClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url: str):
            assert url == 'http://127.0.0.1:60812/core/input/util.js'
            return FakeUpstreamResponse()

    monkeypatch.setattr(server_module.httpx, 'Client', FakeHttpxClient)
    token = signed_worker_token(secret)
    client.cookies.set(worker_cookie_name("wrk_1"), token)
    response = client.get('/novnc/wrk_1/core/input/util.js')

    assert response.status_code == 200
    assert runtime.header_contexts[-1]["X-Viventium-Tenant-Id"] == "tenant-alpha"
    assert runtime.header_contexts[-1]["X-Viventium-User-Id"] == "user-a"


def test_novnc_rejects_invalid_asset_path():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.get('/novnc/wrk_1/core/%5Cbad.js')

    assert response.status_code == 400


def test_novnc_proxy_handles_upstream_transport_error(monkeypatch):
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    class FakeHttpxClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url: str):
            raise httpx.ConnectError("upstream unavailable")

    monkeypatch.setattr(server_module.httpx, 'Client', FakeHttpxClient)
    response = client.get('/novnc/wrk_1/core/rfb.js')

    assert response.status_code == 502


def test_unsigned_inbound_identity_headers_are_ignored_by_default(monkeypatch):
    secret = "ui-signed-link-secret"
    monkeypatch.setenv("WPR_API_TOKEN", secret)
    monkeypatch.setenv("GLASSHIVE_DEFAULT_OWNER_ID", "default-owner")
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.get(
        '/api/bootstrap',
        headers={
            "X-Viventium-User-Id": "forged-user",
            "X-Viventium-User-Role": "admin",
        },
    )

    assert response.status_code == 200
    assert response.json()["owner_id"] == "default-owner"
    assert runtime.header_contexts[-1]["X-Viventium-User-Id"] == "default-owner"
    assert "X-Viventium-User-Role" not in runtime.header_contexts[-1]


def test_enterprise_ui_requires_service_token_at_startup(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "ui-signed-link-secret")

    with pytest.raises(RuntimeError, match="requires WPR_API_TOKEN"):
        create_app(runtime_client=FakeRuntimeClient())


def test_enterprise_ui_requires_signed_link_secret_at_startup(monkeypatch):
    monkeypatch.setenv("WPR_API_TOKEN", "ui-service-secret")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")

    with pytest.raises(RuntimeError, match="requires GLASSHIVE_SIGNED_LINK_SECRET"):
        create_app(runtime_client=FakeRuntimeClient())


def test_enterprise_ui_requires_signed_link_secret_distinct_from_service_token(monkeypatch):
    monkeypatch.setenv("WPR_API_TOKEN", "same-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "same-secret")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")

    with pytest.raises(RuntimeError, match="differ from WPR_API_TOKEN"):
        create_app(runtime_client=FakeRuntimeClient())


def test_enterprise_ui_rejects_invalid_owner_identity_config_at_startup(monkeypatch):
    monkeypatch.setenv("WPR_API_TOKEN", "ui-service-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "ui-signed-link-secret")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_OWNER_IDENTITY_CLAIMS", "user_id,role")

    with pytest.raises(RuntimeError, match="only supports"):
        create_app(runtime_client=FakeRuntimeClient())

    monkeypatch.setenv("GLASSHIVE_OWNER_IDENTITY_CLAIMS", "user_id")
    monkeypatch.setenv("GLASSHIVE_OWNER_IDENTITY_ALIASES_JSON", "[]")

    with pytest.raises(RuntimeError, match="must be a JSON object"):
        create_app(runtime_client=FakeRuntimeClient())

    monkeypatch.delenv("GLASSHIVE_OWNER_IDENTITY_ALIASES_JSON", raising=False)
    monkeypatch.setenv("GLASSHIVE_OWNER_IDENTITY_ALIASES_FILE", "/tmp/glasshive-missing-aliases.json")

    with pytest.raises(RuntimeError, match="could not be read"):
        create_app(runtime_client=FakeRuntimeClient())


def test_enterprise_bootstrap_requires_authenticated_user_assertion(monkeypatch):
    set_enterprise_ui_env(monkeypatch)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.get('/api/bootstrap')

    assert response.status_code == 401
    assert "authenticated user assertion" in response.json()["detail"]
    assert runtime.header_contexts == []


def test_enterprise_ui_disables_builtin_openapi_docs(monkeypatch):
    set_enterprise_ui_env(monkeypatch)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/").status_code == 401
    assert client.get("/watch/wrk_1").status_code == 401
    assert client.get("/desktop/wrk_1").status_code == 401


def test_enterprise_ui_static_shells_require_trusted_identity(monkeypatch):
    set_enterprise_ui_env(monkeypatch)
    monkeypatch.setenv("GLASSHIVE_TRUST_INBOUND_IDENTITY", "true")
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    headers = {
        "X-Viventium-Tenant-Id": "tenant-alpha",
        "X-Viventium-User-Id": "user-a",
        "X-Viventium-User-Role": "operator",
    }

    assert client.get("/", headers=headers).status_code == 200
    assert client.get("/watch/wrk_1", headers=headers).status_code == 200
    assert client.get("/desktop/wrk_1", headers=headers).status_code == 200


def test_enterprise_watch_shell_accepts_signed_worker_link(monkeypatch):
    signed_secret = "ui-signed-link-secret"
    set_enterprise_ui_env(monkeypatch, signed_secret=signed_secret)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    token = signed_worker_token(signed_secret)

    assert client.get(f"/watch/wrk_1?gh_token={token}").status_code == 200
    assert client.get(f"/desktop/wrk_1?gh_token={token}").status_code == 200


def test_enterprise_signed_worker_link_is_tenant_scoped(monkeypatch):
    signed_secret = "ui-signed-link-secret"
    set_enterprise_ui_env(monkeypatch, signed_secret=signed_secret)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    token = signed_worker_token(signed_secret, tenant_id="tenant-beta")

    assert client.get(f"/watch/wrk_1?gh_token={token}").status_code == 401
    assert client.get(f"/desktop/wrk_1?gh_token={token}").status_code == 401
    assert client.get(f"/api/worker/wrk_1/live?gh_token={token}").status_code == 401
    assert runtime.header_contexts == []


def test_enterprise_signed_artifact_link_proxies_without_user_assertion(monkeypatch):
    service_secret = "ui-service-secret"
    signed_secret = "ui-signed-link-secret"
    set_enterprise_ui_env(monkeypatch, service_secret=service_secret, signed_secret=signed_secret)
    monkeypatch.setenv("GLASSHIVE_TRUST_INBOUND_IDENTITY", "true")
    token = signed_artifact_token(signed_secret)
    captured = {}

    class FakeUpstreamResponse:
        status_code = 200
        content = b"artifact bytes"
        headers = {
            "content-type": "text/plain",
            "content-disposition": 'attachment; filename="report.txt"',
        }

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, headers=None, content=None):
            captured.update({"method": method, "url": url, "headers": headers or {}, "content": content})
            return FakeUpstreamResponse()

    monkeypatch.setattr(server_module.httpx, "AsyncClient", FakeAsyncClient)
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))

    response = client.get(f"/v1/signed-links/{token}")

    assert response.status_code == 200, response.text
    assert response.content == b"artifact bytes"
    assert response.headers["content-disposition"] == 'attachment; filename="report.txt"'
    assert captured["url"] == f"http://runtime.test/v1/signed-links/{token}"
    assert captured["headers"]["X-WPR-Token"] == service_secret
    assert captured["headers"]["X-Viventium-Tenant-Id"] == "tenant-alpha"
    assert captured["headers"]["X-Viventium-User-Id"] == "user-a"
    assert captured["headers"]["X-Viventium-User-Role"] == "member"


def test_enterprise_signed_artifact_open_link_proxies_without_user_assertion(monkeypatch):
    service_secret = "ui-service-secret"
    signed_secret = "ui-signed-link-secret"
    set_enterprise_ui_env(monkeypatch, service_secret=service_secret, signed_secret=signed_secret)
    monkeypatch.setenv("GLASSHIVE_TRUST_INBOUND_IDENTITY", "true")
    token = signed_artifact_token(signed_secret, kind="artifact_open")
    captured = {}

    class FakeUpstreamResponse:
        status_code = 200
        content = b"<html><body>artifact preview</body></html>"
        headers = {
            "content-type": "text/html; charset=utf-8",
            "cache-control": "no-store, no-cache, private, max-age=0",
        }

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, headers=None, content=None):
            captured.update({"method": method, "url": url, "headers": headers or {}, "content": content})
            return FakeUpstreamResponse()

    monkeypatch.setattr(server_module.httpx, "AsyncClient", FakeAsyncClient)
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))

    response = client.get(f"/v1/signed-links/{token}")

    assert response.status_code == 200
    assert b"artifact preview" in response.content
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["cache-control"] == "no-store, no-cache, private, max-age=0"
    assert captured["url"] == f"http://runtime.test/v1/signed-links/{token}"
    assert captured["headers"]["X-WPR-Token"] == service_secret
    assert captured["headers"]["X-Viventium-Tenant-Id"] == "tenant-alpha"
    assert captured["headers"]["X-Viventium-User-Id"] == "user-a"
    assert captured["headers"]["X-Viventium-User-Role"] == "member"


def test_enterprise_artifact_link_ref_uses_worker_cookie_identity(monkeypatch):
    service_secret = "ui-service-secret"
    signed_secret = "ui-signed-link-secret"
    set_enterprise_ui_env(monkeypatch, service_secret=service_secret, signed_secret=signed_secret)
    monkeypatch.setenv("GLASSHIVE_TRUST_INBOUND_IDENTITY", "true")
    captured = {}

    class FakeUpstreamResponse:
        status_code = 200
        content = b"artifact bytes"
        headers = {"content-type": "text/plain"}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, headers=None, content=None):
            captured.update({"method": method, "url": url, "headers": headers or {}, "content": content})
            return FakeUpstreamResponse()

    monkeypatch.setattr(server_module.httpx, "AsyncClient", FakeAsyncClient)
    app = create_app(runtime_client=FakeRuntimeClient())
    artifact_token = signed_artifact_token(signed_secret, kind="artifact_open")
    artifact_ref_id = create_signed_link_ref(
        token=artifact_token,
        target_url=f"http://testserver/v1/signed-links/{artifact_token}",
    )

    unauthenticated_client = TestClient(app)
    assert unauthenticated_client.get(f"/v1/link-refs/{artifact_ref_id}").status_code == 401
    assert captured == {}

    authenticated_client = TestClient(app)
    worker_token = signed_worker_token(signed_secret)
    worker_ref_id = create_signed_link_ref(
        token=worker_token,
        target_url=f"http://testserver/watch/wrk_1?surface=desktop&gh_token={worker_token}",
    )
    short_response = authenticated_client.get(
        f"/r/{worker_ref_id}",
        headers={
            "X-GlassHive-Tenant-Id": "tenant-alpha",
            "X-GlassHive-User-Id": "user-a",
            "X-GlassHive-User-Role": "member",
        },
        follow_redirects=False,
    )
    assert short_response.status_code == 307
    set_cookie = short_response.headers["set-cookie"]
    assert f"{worker_cookie_name('wrk_1')}=" in set_cookie
    assert "glasshive_gh_token_wrk_1=" not in set_cookie
    cookie_value = set_cookie.split(f"{worker_cookie_name('wrk_1')}=", 1)[1].split(";", 1)[0]
    authenticated_client.cookies.set(worker_cookie_name("wrk_1"), cookie_value)

    response = authenticated_client.get(f"/v1/link-refs/{artifact_ref_id}")

    assert response.status_code == 200, response.text
    assert response.content == b"artifact bytes"
    assert captured["url"] == f"http://runtime.test/v1/link-refs/{artifact_ref_id}"
    assert captured["headers"]["X-WPR-Token"] == service_secret
    assert captured["headers"]["X-Viventium-Tenant-Id"] == "tenant-alpha"
    assert captured["headers"]["X-Viventium-User-Id"] == "user-a"
    assert captured["headers"]["X-Viventium-User-Role"] == "member"


def test_signed_runtime_proxy_sets_worker_scoped_cookie(monkeypatch):
    service_secret = "ui-service-secret"
    signed_secret = "ui-signed-link-secret"
    set_enterprise_ui_env(monkeypatch, service_secret=service_secret, signed_secret=signed_secret)
    token = signed_worker_token(signed_secret)

    class FakeUpstreamResponse:
        status_code = 200
        content = b"<html>project</html>"
        headers = {"content-type": "text/html"}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, headers=None, content=None):
            return FakeUpstreamResponse()

    monkeypatch.setattr(server_module.httpx, "AsyncClient", FakeAsyncClient)
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))

    response = client.get(f"/ui/projects/prj_1?worker_id=wrk_1&gh_token={token}")

    assert response.status_code == 200
    assert f"{worker_cookie_name('wrk_1')}=" in response.headers["set-cookie"]
    assert "glasshive_gh_token_wrk_1=" not in response.headers["set-cookie"]


def test_signed_runtime_proxy_refreshes_worker_cookie_expiry(monkeypatch):
    service_secret = "ui-service-secret"
    signed_secret = "ui-signed-link-secret"
    set_enterprise_ui_env(monkeypatch, service_secret=service_secret, signed_secret=signed_secret)
    now = {"value": 1_000}
    monkeypatch.setattr(server_module.time, "time", lambda: now["value"])
    monkeypatch.setattr(server_module.sign_link_token.__globals__["time"], "time", lambda: now["value"])
    old_token = server_module.sign_link_token(
        kind="worker_view",
        worker_id="wrk_1",
        tenant_id="tenant-alpha",
        owner_id="user-a",
        ttl_seconds=60,
    )

    class FakeUpstreamResponse:
        status_code = 200
        content = b"<html>project</html>"
        headers = {"content-type": "text/html"}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, headers=None, content=None):
            return FakeUpstreamResponse()

    monkeypatch.setattr(server_module.httpx, "AsyncClient", FakeAsyncClient)
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))
    now["value"] = 1_040
    client.cookies.set(worker_cookie_name("wrk_1"), old_token)

    response = client.get("/ui/projects/prj_1?worker_id=wrk_1")

    assert response.status_code == 200
    set_cookie = response.headers["set-cookie"]
    refreshed = set_cookie.split(f"{worker_cookie_name('wrk_1')}=", 1)[1].split(";", 1)[0]
    assert refreshed != old_token
    refreshed_payload = server_module.verify_signed_link_token(refreshed)
    assert refreshed_payload is not None
    assert refreshed_payload["exp"] > 1_060


def test_worker_live_poll_refreshes_worker_cookie_expiry(monkeypatch):
    service_secret = "ui-service-secret"
    signed_secret = "ui-signed-link-secret"
    set_enterprise_ui_env(monkeypatch, service_secret=service_secret, signed_secret=signed_secret)
    now = {"value": 1_000}
    monkeypatch.setattr(server_module.time, "time", lambda: now["value"])
    monkeypatch.setattr(server_module.sign_link_token.__globals__["time"], "time", lambda: now["value"])
    old_token = server_module.sign_link_token(
        kind="worker_view",
        worker_id="wrk_1",
        tenant_id="tenant-alpha",
        owner_id="user-a",
        ttl_seconds=60,
    )
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    now["value"] = 1_040
    client.cookies.set(worker_cookie_name("wrk_1"), old_token)

    response = client.get("/api/worker/wrk_1/live")

    assert response.status_code == 200
    set_cookie = response.headers["set-cookie"]
    refreshed = set_cookie.split(f"{worker_cookie_name('wrk_1')}=", 1)[1].split(";", 1)[0]
    assert refreshed != old_token
    refreshed_payload = server_module.verify_signed_link_token(refreshed)
    assert refreshed_payload is not None
    assert refreshed_payload["exp"] > 1_060
    assert runtime.header_contexts[-1]["X-Viventium-User-Id"] == "user-a"


def test_enterprise_signed_artifact_link_cannot_open_workspace_shell(monkeypatch):
    signed_secret = "ui-signed-link-secret"
    set_enterprise_ui_env(monkeypatch, signed_secret=signed_secret)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    for token in (
        signed_artifact_token(signed_secret, kind="artifact_download"),
        signed_artifact_token(signed_secret, kind="artifact_open"),
    ):
        assert client.get(f"/watch/wrk_1?gh_token={token}").status_code == 403
        assert client.get(f"/desktop/wrk_1?gh_token={token}").status_code == 403
    assert runtime.header_contexts == []


def test_enterprise_signed_worker_link_cannot_proxy_signed_artifact_endpoint(monkeypatch):
    signed_secret = "ui-signed-link-secret"
    set_enterprise_ui_env(monkeypatch, signed_secret=signed_secret)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    token = signed_worker_token(signed_secret)

    assert client.get(f"/v1/signed-links/{token}").status_code == 403
    assert runtime.header_contexts == []


def test_enterprise_signed_artifact_link_cannot_proxy_raw_runtime_routes(monkeypatch):
    signed_secret = "ui-signed-link-secret"
    set_enterprise_ui_env(monkeypatch, signed_secret=signed_secret)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    token = signed_artifact_token(signed_secret, kind="artifact_open")

    assert client.get(f"/v1/workers/wrk_1/artifacts/open?gh_token={token}&path=workspace/report.txt").status_code == 403
    assert runtime.header_contexts == []


def test_signed_watch_rejects_unsafe_worker_cookie_name(monkeypatch):
    secret = "ui-signed-link-secret"
    monkeypatch.setenv("WPR_API_TOKEN", secret)
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.get("/watch/wrk_1%3Bbad")

    assert response.status_code == 400


def test_enterprise_trusted_identity_requires_user_assertion(monkeypatch):
    set_enterprise_ui_env(monkeypatch)
    monkeypatch.setenv("GLASSHIVE_TRUST_INBOUND_IDENTITY", "true")
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.get('/api/bootstrap', headers={"X-Viventium-Tenant-Id": "tenant-alpha"})

    assert response.status_code == 401
    assert "authenticated user assertion" in response.json()["detail"]
    assert runtime.header_contexts == []


def test_trusted_inbound_identity_headers_can_be_enabled(monkeypatch):
    secret = "ui-signed-link-secret"
    monkeypatch.setenv("WPR_API_TOKEN", secret)
    monkeypatch.setenv("GLASSHIVE_TRUST_INBOUND_IDENTITY", "true")
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.get(
        '/api/bootstrap',
        headers={
            "X-Viventium-Tenant-Id": "tenant-alpha",
            "X-Viventium-User-Id": "asserted-user",
            "X-Viventium-User-Role": "operator",
        },
    )

    assert response.status_code == 200
    assert response.json()["owner_id"] == "asserted-user"
    assert runtime.header_contexts[-1]["X-Viventium-Tenant-Id"] == "tenant-alpha"
    assert runtime.header_contexts[-1]["X-Viventium-User-Id"] == "asserted-user"
    assert runtime.header_contexts[-1]["X-Viventium-User-Role"] == "operator"


def test_enterprise_trusted_identity_uses_proxy_assertion(monkeypatch):
    service_secret = "ui-service-secret"
    set_enterprise_ui_env(monkeypatch, service_secret=service_secret)
    monkeypatch.setenv("GLASSHIVE_TRUST_INBOUND_IDENTITY", "true")
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.get(
        '/api/bootstrap',
        headers={
            "X-Viventium-Tenant-Id": "tenant-alpha",
            "X-Viventium-User-Id": "user-a",
            "X-Viventium-User-Email": "user-a@example.test",
            "X-Viventium-User-Role": "member",
        },
    )

    assert response.status_code == 200
    assert response.json()["owner_id"] == "user-a"
    assert runtime.header_contexts[-1]["X-WPR-Token"] == service_secret
    assert runtime.header_contexts[-1]["X-Viventium-Tenant-Id"] == "tenant-alpha"
    assert runtime.header_contexts[-1]["X-Viventium-User-Id"] == "user-a"
    assert runtime.header_contexts[-1]["X-Viventium-User-Email"] == "user-a@example.test"
    assert runtime.header_contexts[-1]["X-Viventium-User-Role"] == "member"


def test_enterprise_live_api_hides_raw_desktop_url_but_backend_requests_internal_details(monkeypatch):
    service_secret = "ui-service-secret"
    set_enterprise_ui_env(monkeypatch, service_secret=service_secret)
    monkeypatch.setenv("GLASSHIVE_TRUST_INBOUND_IDENTITY", "true")
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.get(
        "/api/worker/wrk_1/live",
        headers={
            "X-Viventium-Tenant-Id": "tenant-alpha",
            "X-Viventium-User-Id": "user-a",
            "X-Viventium-User-Role": "member",
        },
    )

    assert response.status_code == 200
    runtime_details = response.json()["runtime_details"]
    assert runtime_details["view_available"] is True
    assert "view_url" not in runtime_details
    assert runtime.header_contexts[-1]["X-WPR-Token"] == service_secret
    assert runtime.header_contexts[-1]["X-Viventium-Tenant-Id"] == "tenant-alpha"
    assert runtime.header_contexts[-1]["X-Viventium-User-Id"] == "user-a"
    assert runtime.header_contexts[-1]["X-Viventium-User-Role"] == "operator"


def test_enterprise_trusted_identity_rejects_tenant_mismatch(monkeypatch):
    set_enterprise_ui_env(monkeypatch)
    monkeypatch.setenv("GLASSHIVE_TRUST_INBOUND_IDENTITY", "true")
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.get(
        '/api/bootstrap',
        headers={
            "X-Viventium-Tenant-Id": "tenant-beta",
            "X-Viventium-User-Id": "user-a",
        },
    )

    assert response.status_code == 401
    assert "tenant assertion" in response.json()["detail"]
    assert runtime.header_contexts == []


def test_enterprise_local_demo_owner_requires_explicit_escape_hatch(monkeypatch):
    set_enterprise_ui_env(monkeypatch)
    monkeypatch.setenv("GLASSHIVE_ALLOW_LOCAL_DEMO_OWNER", "true")
    monkeypatch.setenv("GLASSHIVE_DEFAULT_OWNER_ID", "demo-owner")
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.get('/api/bootstrap')

    assert response.status_code == 200
    assert response.json()["owner_id"] == "demo-owner"
    assert runtime.header_contexts[-1]["X-Viventium-Tenant-Id"] == "tenant-alpha"
    assert runtime.header_contexts[-1]["X-Viventium-User-Id"] == "demo-owner"


def test_runtime_ui_proxy_injects_enterprise_identity(monkeypatch):
    service_secret = "ui-service-secret"
    set_enterprise_ui_env(monkeypatch, service_secret=service_secret)
    monkeypatch.setenv("GLASSHIVE_TRUST_INBOUND_IDENTITY", "true")
    captured = {}

    class FakeUpstreamResponse:
        status_code = 200
        content = b"<html>runtime ui</html>"
        headers = {"content-type": "text/html"}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, headers=None, content=None):
            captured.update({"method": method, "url": url, "headers": headers or {}, "content": content})
            return FakeUpstreamResponse()

    monkeypatch.setattr(server_module.httpx, "AsyncClient", FakeAsyncClient)
    client = TestClient(create_app(runtime_client=FakeRuntimeClient()))

    response = client.get(
        "/ui/projects/prj_1?worker_id=wrk_1",
        headers={
            "X-Viventium-Tenant-Id": "tenant-alpha",
            "X-Viventium-User-Id": "user-a",
            "X-Viventium-User-Role": "member",
        },
    )

    assert response.status_code == 200
    assert response.text == "<html>runtime ui</html>"
    assert captured["url"] == "http://runtime.test/ui/projects/prj_1?worker_id=wrk_1"
    assert captured["headers"]["X-WPR-Token"] == service_secret
    assert captured["headers"]["X-Viventium-Tenant-Id"] == "tenant-alpha"
    assert captured["headers"]["X-Viventium-User-Id"] == "user-a"
    assert captured["headers"]["X-Viventium-User-Role"] == "member"


def test_worker_steer_endpoint_uses_runtime_steer():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    response = client.post('/api/worker/wrk_1/steer', json={'message': 'Redirect to the new plan now.'})
    assert response.status_code == 200
    assert runtime.steer_requests == [{'worker_id': 'wrk_1', 'message': 'Redirect to the new plan now.'}]


def test_worker_message_endpoint_uses_runtime_queue_message():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))
    response = client.post('/api/worker/wrk_1/message', json={'message': 'Queue this after the current run finishes.'})
    assert response.status_code == 200
    assert runtime.message_requests == [{'worker_id': 'wrk_1', 'message': 'Queue this after the current run finishes.'}]


def test_launch_projects_uploaded_files_into_new_workspace_bootstrap():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.post('/api/launch', json={
        'description': 'Use the attached brief to create a polished summary',
        'success_criteria': 'A summary file is created',
        'context': '',
        'workspace_option': 'new:codex-cli',
        'files': [
            {
                'name': '../brief.txt',
                'mime_type': 'text/plain',
                'size': 12,
                'content_base64': base64.b64encode(b'hello upload').decode('ascii'),
            }
        ],
    })

    assert response.status_code == 200
    bundle = runtime.create_worker_requests[-1]['bootstrap_bundle']
    assert bundle['files'][0]['path'] == 'uploads/brief.txt'
    assert bundle['files'][0]['encoding'] == 'base64'
    assert 'uploads/brief.txt' in bundle['system_instructions']
    assert 'do not force a downloadable file' in bundle['system_instructions']


def test_schedule_project_creates_worker_without_starting_and_persists_schedule():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.post('/api/launch', json={
        'description': 'Check the workspace later',
        'success_criteria': 'The later check is queued',
        'context': '',
        'workspace_option': 'new:codex-cli',
        'schedule_text': 'in 20 minutes',
    })

    assert response.status_code == 200
    assert response.json()['status'] == 'scheduled'
    assert response.json()['schedule_id'] == 'sch_1'
    assert runtime.create_worker_requests[-1]['start_synchronously'] is False
    assert runtime.assign_requests == []
    assert runtime.schedule_requests[-1]['schedule_text'] == 'in 20 minutes'


def test_worker_metadata_endpoint_updates_favorite():
    runtime = FakeRuntimeClient()
    client = TestClient(create_app(runtime_client=runtime))

    response = client.post('/api/worker/wrk_1/metadata', json={'favorite': True})

    assert response.status_code == 200
    assert runtime.metadata_requests == [{'worker_id': 'wrk_1', 'payload': {'favorite': True}}]
