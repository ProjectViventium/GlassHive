from __future__ import annotations

import asyncio
import base64
import json
import os
from urllib.parse import urlsplit
from fastmcp import Client
from fastmcp.exceptions import ToolError
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from workers_projects_runtime import mcp_server, runtime_env
from workers_projects_runtime.bootstrap import sign_bootstrap_source_path
from workers_projects_runtime.mcp_server import create_mcp_server
from workers_projects_runtime.signed_links import verify_signed_link_token


class FakeApiClient:
    def list_projects(self, owner_id: str | None = None):
        items = [
            {"project_id": "prj_123", "owner_id": "demo-owner", "title": "Inbox Zero", "goal": "Triage open loops"},
            {"project_id": "prj_999", "owner_id": "other", "title": "Other", "goal": "Ignore me"},
        ]
        if owner_id:
            return [item for item in items if item["owner_id"] == owner_id]
        return items

    def create_project(self, *, owner_id: str | None, title: str, goal: str, default_worker_profile: str = "openclaw-general"):
        return {
            "project_id": "prj_new",
            "owner_id": owner_id,
            "title": title,
            "goal": goal,
            "default_worker_profile": default_worker_profile,
        }

    def get_project(self, project_id: str):
        return {"project_id": project_id, "owner_id": "demo-owner", "title": "Inbox Zero", "goal": "Triage open loops"}

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
        return {
            "tenant_id": "local",
            "owner_id": "demo-owner",
            "default_worker_profile": payload.get("default_worker_profile", ""),
            "codex_reasoning_effort": payload.get("codex_reasoning_effort", ""),
            "claude_effort": payload.get("claude_effort", ""),
            "openclaw_effort": payload.get("openclaw_effort", ""),
            "updated_at": "2026-05-24T00:00:00+00:00",
        }

    def list_project_runs(self, project_id: str):
        return [{"run_id": "run_123", "project_id": project_id, "state": "completed"}]

    def list_project_events(self, project_id: str):
        return [{"event_id": "evt_123", "project_id": project_id, "event_type": "run.completed"}]

    def list_workers(self, project_id: str):
        return [{"worker_id": "wrk_123", "project_id": project_id, "profile": "openclaw-general", "state": "ready"}]

    def find_worker_by_alias_across_projects(
        self,
        *,
        owner_id: str | None,
        alias: str,
        execution_mode: str | None = None,
    ):
        scoped = mcp_server._request_scoped_alias(alias)
        for project in self.list_projects(owner_id):
            for worker in self.list_workers(project["project_id"]):
                if worker.get("state") == "terminated":
                    continue
                if execution_mode and worker.get("execution_mode") and worker.get("execution_mode") != execution_mode:
                    continue
                if worker.get("alias") == scoped:
                    return {"project": project, "worker": worker}
        return None

    def create_worker(
        self,
        *,
        project_id: str,
        owner_id: str | None,
        name: str,
        role: str,
        profile: str = "openclaw-general",
        backend: str = "openclaw",
        execution_mode: str = "docker",
        alias: str | None = None,
        workspace_root: str | None = None,
        bootstrap_profile: str | None = None,
        bootstrap_bundle: dict | None = None,
        start_synchronously: bool = True,
    ):
        return {
            "worker_id": "wrk_new",
            "project_id": project_id,
            "owner_id": owner_id,
            "name": name,
            "role": role,
            "profile": profile,
            "backend": backend,
            "execution_mode": execution_mode,
            "alias": alias,
            "workspace_root": workspace_root,
            "state": "ready",
            "bootstrap_bundle": bootstrap_bundle,
            "start_synchronously": start_synchronously,
        }

    def find_or_resume_worker(self, **kwargs):
        payload = self.create_worker(**kwargs)
        payload["worker_id"] = "wrk_resumed"
        return payload

    def get_worker(self, worker_id: str):
        return {
            "worker_id": worker_id,
            "project_id": "prj_123",
            "tenant_id": "tenant-alpha",
            "owner_id": "demo-owner",
            "profile": "openclaw-general",
            "state": "ready",
        }

    def worker_live(self, worker_id: str):
        return {
            "worker": {
                "worker_id": worker_id,
                "project_id": "prj_123",
                "tenant_id": "tenant-alpha",
                "owner_id": "demo-owner",
                "state": "ready",
            },
            "runtime_details": {"view_url": "http://127.0.0.1:62310/?autoconnect=1"},
            "project_runs": [{"run_id": "run_123"}],
        }

    def list_artifacts(self, worker_id: str):
        return {
            "items": [
                {
                    "path": "index.html",
                    "name": "index.html",
                    "size": 128,
                    "download_url": f"/v1/workers/{worker_id}/artifacts/download?path=index.html",
                },
                {
                    "path": ".codex/config.toml",
                    "name": "config.toml",
                    "size": 64,
                    "download_url": f"/v1/workers/{worker_id}/artifacts/download?path=.codex/config.toml",
                },
                {
                    "path": "tmp/chrome-user-data/Default/Default/Extensions/fdpohaocaechififmbbbbbknoalclacl/8.6_0/capture/index.html",
                    "name": "index.html",
                    "size": 197,
                    "download_url": f"/v1/workers/{worker_id}/artifacts/download?path=tmp/chrome-user-data/Default/Default/Extensions/fdpohaocaechififmbbbbbknoalclacl/8.6_0/capture/index.html",
                },
                {
                    "path": "uploads/source.txt.metadata.json",
                    "name": "source.txt.metadata.json",
                    "size": 32,
                    "download_url": f"/v1/workers/{worker_id}/artifacts/download?path=uploads/source.txt.metadata.json",
                },
            ]
        }

    def worker_runs(self, worker_id: str):
        return [{"run_id": "run_123", "worker_id": worker_id, "state": "completed"}]

    def worker_events(self, worker_id: str):
        return [{"event_id": "evt_123", "worker_id": worker_id, "event_type": "worker.ready"}]

    def assign_run(self, worker_id: str, instruction: str, *, effort: str | None = None, bootstrap_bundle: dict | None = None):
        return {"run_id": "run_assign", "worker_id": worker_id, "instruction": instruction, "effort": effort or "", "state": "queued", "bootstrap_bundle": bootstrap_bundle}

    def send_message(self, worker_id: str, message: str):
        return {"run_id": "run_msg", "worker_id": worker_id, "instruction": message, "state": "queued"}

    def schedule_run(self, worker_id: str, instruction: str, *, run_at: str | None = None, schedule_text: str | None = None, delay_seconds: int | None = None, bootstrap_bundle: dict | None = None):
        return {
            "schedule_id": "sch_123",
            "worker_id": worker_id,
            "project_id": "prj_123",
            "instruction": instruction,
            "schedule_text": schedule_text or "",
            "run_at": run_at or "2026-05-23T19:00:00+00:00",
            "state": "pending",
            "delay_seconds": delay_seconds,
        }

    def worker_schedules(self, worker_id: str, include_done: bool = False):
        return [{"schedule_id": "sch_123", "worker_id": worker_id, "state": "pending", "include_done": include_done}]

    def get_schedule(self, schedule_id: str):
        return {"schedule_id": schedule_id, "worker_id": "wrk_123", "state": "pending"}

    def lifecycle(self, worker_id: str, action: str):
        return {"worker_id": worker_id, "state": "ready", "action": action}

    def desktop_action(self, worker_id: str, action: str, url: str | None = None):
        return {"worker_id": worker_id, "action": action, "url": url, "view_url": "http://127.0.0.1:62310/?autoconnect=1"}

    def takeover(self, worker_id: str):
        return {"supported": True, "url": f"http://127.0.0.1:8766/ui/workers/{worker_id}/view", "mode": "workstation-desktop"}

    def get_run(self, run_id: str):
        return {"run_id": run_id, "worker_id": "wrk_123", "project_id": "prj_123", "state": "completed", "output_text": "OK"}

    def metrics(self):
        return {"projects": 1, "workers": 1, "runs": 2, "queued_runs": 0, "active_runs": 0, "events": 3}


class TrackingApiClient(FakeApiClient):
    def __init__(self):
        self.calls: list[str] = []
        self.create_project_payloads: list[dict] = []
        self.create_worker_payloads: list[dict] = []
        self.find_or_resume_payloads: list[dict] = []
        self.assign_run_payloads: list[dict] = []
        self.schedule_run_payloads: list[dict] = []

    def list_projects(self, owner_id: str | None = None):
        self.calls.append("list_projects")
        return super().list_projects(owner_id)

    def list_workers(self, project_id: str):
        self.calls.append("list_workers")
        return super().list_workers(project_id)

    def create_project(self, **kwargs):
        self.calls.append("create_project")
        self.create_project_payloads.append(kwargs)
        return super().create_project(**kwargs)

    def find_or_resume_worker(self, **kwargs):
        self.calls.append("find_or_resume_worker")
        self.find_or_resume_payloads.append(kwargs)
        return super().find_or_resume_worker(**kwargs)

    def create_worker(self, **kwargs):
        self.create_worker_payloads.append(kwargs)
        return super().create_worker(**kwargs)

    def assign_run(self, worker_id: str, instruction: str, *, effort: str | None = None, bootstrap_bundle: dict | None = None):
        self.calls.append("assign_run")
        self.assign_run_payloads.append({"worker_id": worker_id, "instruction": instruction, "effort": effort or "", "bootstrap_bundle": bootstrap_bundle})
        return super().assign_run(worker_id, instruction, effort=effort, bootstrap_bundle=bootstrap_bundle)

    def schedule_run(self, worker_id: str, instruction: str, *, run_at: str | None = None, schedule_text: str | None = None, delay_seconds: int | None = None, bootstrap_bundle: dict | None = None):
        self.schedule_run_payloads.append(
            {
                "worker_id": worker_id,
                "instruction": instruction,
                "run_at": run_at,
                "schedule_text": schedule_text,
                "delay_seconds": delay_seconds,
                "bootstrap_bundle": bootstrap_bundle,
            }
        )
        return super().schedule_run(
            worker_id,
            instruction,
            run_at=run_at,
            schedule_text=schedule_text,
            delay_seconds=delay_seconds,
            bootstrap_bundle=bootstrap_bundle,
        )


class PreferenceApiClient(TrackingApiClient):
    def __init__(self):
        super().__init__()
        self.preference_payloads: list[dict] = []
        self.preferences: dict = {
            "tenant_id": "local",
            "owner_id": "demo-owner",
            "default_worker_profile": "codex-cli",
            "codex_reasoning_effort": "xhigh",
            "claude_effort": "",
            "openclaw_effort": "",
            "updated_at": "2026-05-24T00:00:00+00:00",
        }

    def get_preferences(self):
        return dict(self.preferences)

    def update_preferences(self, payload: dict):
        self.preference_payloads.append(payload)
        self.preferences = {**self.preferences, **payload}
        return dict(self.preferences)


class PollingApiClient(FakeApiClient):
    def __init__(self, states: list[str]):
        self.states = list(states)
        self.get_run_calls = 0

    def get_run(self, run_id: str):
        self.get_run_calls += 1
        state = self.states[min(self.get_run_calls - 1, len(self.states) - 1)]
        return {
            "run_id": run_id,
            "worker_id": "wrk_poll",
            "project_id": "prj_poll",
            "state": state,
            "output_text": "Poll completed" if state == "completed" else "",
            "error_text": "",
        }

    def worker_live(self, worker_id: str):
        return {
            "worker": {
                "worker_id": worker_id,
                "project_id": "prj_poll",
                "state": "ready",
                "owner_id": "demo-owner",
            },
            "runtime_details": {"view_url": "http://127.0.0.1:62310/?autoconnect=1"},
            "project_runs": [],
        }


class RememberedDispatchApiClient(TrackingApiClient):
    def __init__(self, *, tenant_id: str = "local"):
        super().__init__()
        self.tenant_id = tenant_id
        self.workers: dict[str, dict] = {}
        self.runs: dict[str, dict] = {}

    def find_or_resume_worker(self, **kwargs):
        payload = super().find_or_resume_worker(**kwargs)
        payload.update(
            {
                "tenant_id": self.tenant_id,
                "owner_id": kwargs.get("owner_id"),
                "project_id": kwargs.get("project_id"),
                "last_run_id": "",
            }
        )
        self.workers[payload["worker_id"]] = payload
        return payload

    def assign_run(
        self,
        worker_id: str,
        instruction: str,
        *,
        effort: str | None = None,
        bootstrap_bundle: dict | None = None,
    ):
        payload = super().assign_run(
            worker_id,
            instruction,
            effort=effort,
            bootstrap_bundle=bootstrap_bundle,
        )
        payload.update(
            {
                "tenant_id": self.tenant_id,
                "project_id": self.workers.get(worker_id, {}).get("project_id") or "prj_new",
                "state": "completed",
                "output_text": "remembered dispatch completed",
                "error_text": "",
            }
        )
        self.runs[payload["run_id"]] = payload
        if worker_id in self.workers:
            self.workers[worker_id]["last_run_id"] = payload["run_id"]
        return payload

    def get_run(self, run_id: str):
        return self.runs[run_id]

    def get_worker(self, worker_id: str):
        return self.workers[worker_id]

    def worker_live(self, worker_id: str):
        worker = self.get_worker(worker_id)
        run_id = str(worker.get("last_run_id") or "")
        run = self.runs.get(run_id)
        runs = [run] if run else []
        return {
            "worker": worker,
            "runtime_details": {"view_url": "http://127.0.0.1:62310/?autoconnect=1"},
            "runs": runs,
            "project_runs": runs,
        }


class RetryableFailureApiClient(FakeApiClient):
    def __init__(self):
        self.assigned: list[dict[str, str]] = []

    def get_run(self, run_id: str):
        if run_id == "run_retryable_failed":
            return {
                "run_id": "run_retryable_failed",
                "worker_id": "wrk_retry",
                "project_id": "prj_retry",
                "tenant_id": "local",
                "state": "failed",
                "queued_at": "2026-05-25T10:00:00+00:00",
                "instruction": "Build the requested research workbook and report.",
                "output_text": "",
                "error_text": "codex-cli exited with code 1: provider failed",
                "failure_class": "provider_rate_limited",
                "failure_retryable": True,
                "failure_user_message": "The provider rate-limited the worker before it finished.",
                "failure_recommended_recovery": "Use workspace_continue to resume the same workspace.",
                "failure_diagnostic_summary": "response.failed: Too Many Requests",
            }
        return {
            "run_id": run_id,
            "worker_id": "wrk_retry",
            "project_id": "prj_retry",
            "tenant_id": "local",
            "state": "queued",
            "queued_at": "2026-05-25T10:05:00+00:00",
            "instruction": self.assigned[-1]["instruction"] if self.assigned else "",
            "output_text": "",
            "error_text": "",
        }

    def get_worker(self, worker_id: str):
        payload = super().get_worker(worker_id)
        payload.update({"worker_id": worker_id, "project_id": "prj_retry", "tenant_id": "local", "profile": "codex-cli", "state": "ready"})
        return payload

    def worker_live(self, worker_id: str):
        return {
            "worker": {
                "worker_id": worker_id,
                "project_id": "prj_retry",
                "state": "ready",
                "owner_id": "demo-owner",
                "last_run_id": "run_retryable_failed",
            },
            "runtime_details": {},
            "project_runs": [],
        }

    def assign_run(
        self,
        worker_id: str,
        instruction: str,
        *,
        effort: str | None = None,
        bootstrap_bundle: dict | None = None,
    ):
        self.assigned.append(
            {
                "worker_id": worker_id,
                "instruction": instruction,
                "effort": effort or "",
                "bootstrap_bundle": bootstrap_bundle,
            }
        )
        return {
            "run_id": "run_continued",
            "worker_id": worker_id,
            "project_id": "prj_retry",
            "tenant_id": "local",
            "state": "queued",
            "instruction": instruction,
            "effort": effort or "",
            "bootstrap_bundle": bootstrap_bundle,
        }


class EnterpriseRetryableFailureApiClient(RetryableFailureApiClient):
    def __init__(
        self,
        *,
        run_tenant_id: str = "tenant-alpha",
        worker_tenant_id: str = "tenant-alpha",
        worker_owner_id: str = "user-a",
        new_run_tenant_id: str = "tenant-alpha",
        previous_state: str = "failed",
    ):
        super().__init__()
        self.run_tenant_id = run_tenant_id
        self.worker_tenant_id = worker_tenant_id
        self.worker_owner_id = worker_owner_id
        self.new_run_tenant_id = new_run_tenant_id
        self.previous_state = previous_state

    def get_run(self, run_id: str):
        payload = super().get_run(run_id)
        if run_id == "run_retryable_failed":
            payload["tenant_id"] = self.run_tenant_id
            payload["state"] = self.previous_state
        return payload

    def get_worker(self, worker_id: str):
        payload = super().get_worker(worker_id)
        payload.update(
            {
                "tenant_id": self.worker_tenant_id,
                "owner_id": self.worker_owner_id,
            }
        )
        return payload

    def assign_run(
        self,
        worker_id: str,
        instruction: str,
        *,
        effort: str | None = None,
        bootstrap_bundle: dict | None = None,
    ):
        payload = super().assign_run(
            worker_id,
            instruction,
            effort=effort,
            bootstrap_bundle=bootstrap_bundle,
        )
        payload["tenant_id"] = self.new_run_tenant_id
        return payload


class StaleRequestedRunApiClient(FakeApiClient):
    def get_run(self, run_id: str):
        if run_id == "run_old_failed":
            return {
                "run_id": "run_old_failed",
                "worker_id": "wrk_stale",
                "project_id": "prj_stale",
                "state": "failed",
                "queued_at": "2026-05-24T10:00:00+00:00",
                "output_text": "",
                "error_text": "Older failed run",
            }
        return {
            "run_id": "run_new_completed",
            "worker_id": "wrk_stale",
            "project_id": "prj_stale",
            "state": "completed",
            "queued_at": "2026-05-24T10:05:00+00:00",
            "output_text": "Latest artifact is ready",
            "error_text": "",
        }

    def worker_live(self, worker_id: str):
        return {
            "worker": {
                "worker_id": worker_id,
                "project_id": "prj_stale",
                "tenant_id": "tenant-alpha",
                "owner_id": "demo-owner",
                "state": "ready",
                "last_run_id": "run_new_completed",
            },
            "runs": [
                {
                    "run_id": "run_new_completed",
                    "worker_id": worker_id,
                    "project_id": "prj_stale",
                    "state": "completed",
                    "queued_at": "2026-05-24T10:05:00+00:00",
                },
                {
                    "run_id": "run_old_failed",
                    "worker_id": worker_id,
                    "project_id": "prj_stale",
                    "state": "failed",
                    "queued_at": "2026-05-24T10:00:00+00:00",
                },
            ],
            "runtime_details": {},
        }


class OlderLastRunApiClient(StaleRequestedRunApiClient):
    def get_run(self, run_id: str):
        if run_id == "run_new_completed":
            return {
                "run_id": "run_new_completed",
                "worker_id": "wrk_stale",
                "project_id": "prj_stale",
                "state": "completed",
                "queued_at": "2026-05-24T10:05:00+00:00",
                "output_text": "Requested run is the newest result",
                "error_text": "",
            }
        return {
            "run_id": "run_old_failed",
            "worker_id": "wrk_stale",
            "project_id": "prj_stale",
            "state": "failed",
            "queued_at": "2026-05-24T10:00:00+00:00",
            "output_text": "",
            "error_text": "Older failed run",
        }

    def worker_live(self, worker_id: str):
        return {
            "worker": {
                "worker_id": worker_id,
                "project_id": "prj_stale",
                "tenant_id": "tenant-alpha",
                "owner_id": "demo-owner",
                "state": "ready",
                "last_run_id": "run_old_failed",
            },
            "runs": [
                {
                    "run_id": "run_new_completed",
                    "worker_id": worker_id,
                    "project_id": "prj_stale",
                    "state": "completed",
                    "queued_at": "2026-05-24T10:05:00+00:00",
                },
                {
                    "run_id": "run_old_failed",
                    "worker_id": worker_id,
                    "project_id": "prj_stale",
                    "state": "failed",
                    "queued_at": "2026-05-24T10:00:00+00:00",
                },
            ],
            "runtime_details": {},
        }


class TerminalRequestedNewerRunningApiClient(StaleRequestedRunApiClient):
    def get_run(self, run_id: str):
        if run_id == "run_old_failed":
            return {
                "run_id": "run_old_failed",
                "worker_id": "wrk_stale",
                "project_id": "prj_stale",
                "state": "failed",
                "queued_at": "2026-05-24T10:00:00+00:00",
                "output_text": "",
                "error_text": "Requested run failed",
            }
        return {
            "run_id": "run_new_running",
            "worker_id": "wrk_stale",
            "project_id": "prj_stale",
            "state": "running",
            "queued_at": "2026-05-24T10:05:00+00:00",
            "output_text": "",
            "error_text": "",
        }

    def worker_live(self, worker_id: str):
        return {
            "worker": {
                "worker_id": worker_id,
                "project_id": "prj_stale",
                "tenant_id": "tenant-alpha",
                "owner_id": "demo-owner",
                "state": "running",
                "last_run_id": "run_new_running",
            },
            "runs": [
                {
                    "run_id": "run_new_running",
                    "worker_id": worker_id,
                    "project_id": "prj_stale",
                    "state": "running",
                    "queued_at": "2026-05-24T10:05:00+00:00",
                },
                {
                    "run_id": "run_old_failed",
                    "worker_id": worker_id,
                    "project_id": "prj_stale",
                    "state": "failed",
                    "queued_at": "2026-05-24T10:00:00+00:00",
                },
            ],
            "runtime_details": {},
        }


class MixedTimezoneRunOrderApiClient(StaleRequestedRunApiClient):
    def get_run(self, run_id: str):
        if run_id == "run_old_failed":
            return {
                "run_id": "run_old_failed",
                "worker_id": "wrk_stale",
                "project_id": "prj_stale",
                "state": "failed",
                "queued_at": "2026-05-24T09:30:00+02:00",
                "output_text": "",
                "error_text": "Older failed run",
            }
        return {
            "run_id": "run_new_completed",
            "worker_id": "wrk_stale",
            "project_id": "prj_stale",
            "state": "completed",
            "queued_at": "2026-05-24T08:00:00+00:00",
            "output_text": "Newer mixed-timezone artifact is ready",
            "error_text": "",
        }

    def worker_live(self, worker_id: str):
        payload = super().worker_live(worker_id)
        for item in payload["runs"]:
            if item["run_id"] == "run_old_failed":
                item["queued_at"] = "2026-05-24T09:30:00+02:00"
            else:
                item["queued_at"] = "2026-05-24T08:00:00+00:00"
        return payload


def test_configured_default_worker_profile_fails_loud_when_not_allowed(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_DEFAULT_WORKER_PROFILE", "claude-code")
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "openclaw-general")

    with pytest.raises(RuntimeError, match="GLASSHIVE_DEFAULT_WORKER_PROFILE"):
        mcp_server._configured_default_worker_profile()


def test_configured_default_worker_profile_prefers_codex_when_allowed(monkeypatch):
    monkeypatch.delenv("GLASSHIVE_DEFAULT_WORKER_PROFILE", raising=False)
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "openclaw-general,codex-cli")

    assert mcp_server._configured_default_worker_profile() == "codex-cli"


def test_project_create_reads_default_worker_profile_at_call_time(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_DEFAULT_WORKER_PROFILE", "claude-code")
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "codex-cli,claude-code")
    api = TrackingApiClient()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            await client.call_tool(
                "project_create",
                {
                    "title": "Use deployment default",
                    "goal": "Project should use the current default worker.",
                },
            )

    asyncio.run(scenario())
    assert api.create_project_payloads[-1]["default_worker_profile"] == "claude-code"


def _tool_json(result) -> object:
    if result.structured_content is not None:
        return result.structured_content
    assert result.content, "Expected text content from MCP tool call"
    return json.loads(result.content[0].text)


def _callback_headers() -> dict[str, str]:
    return {
        "X-Viventium-User-Id": "user_public_safe",
        "X-Viventium-Conversation-Id": "conv_public_safe",
        "X-Viventium-Parent-Message-Id": "user_msg_public_safe",
        "X-Viventium-Message-Id": "assistant_msg_public_safe",
    }


def test_server_instructions_advertise_mcp_owned_usage_contract(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    monkeypatch.setenv("WPR_HOST_MENTION_CODEX", "@codex")
    monkeypatch.setenv("WPR_HOST_MENTION_CLAUDE", "@claude")
    monkeypatch.setenv("WPR_HOST_MENTION_OPENCLAW", "@openclaw")
    server = create_mcp_server(api_client=FakeApiClient())
    instructions = server.instructions.lower()

    for phrase in [
        "persistent projects",
        "resumable workers",
        "workstation sandboxes",
        "host-native workers",
        "less is more",
        "must not invent tool results",
        "data in and data out must be exact",
        "markdown-sensitive characters",
        "real browser",
        "desktop",
        "local files/projects",
        "installed clis",
        "workspace_launch",
        "description, optional success_criteria, and optional context",
        "worker_delegate_once",
        "callbacks are an optional host-app delivery enhancement",
        "neutral glasshive/librechat headers",
        "do not refuse solely because your own model context lacks file contents",
        "workspace_status",
        "workspace_wait",
        "omits ids",
        "workspace_continue",
        "sandboxed workspace",
        "codex workspace",
        "execution_mode='docker'",
        "runtime_dependency_missing",
        "high/xhigh",
        "deep research",
        "do not shorten",
        "full picture",
        "pass mcp/tool availability as context",
        "view / steer",
        "follow_up_context",
        "own voice",
        "should not expose raw worker/run/provider/queue plumbing",
        "@codex",
        "@claude",
        "@openclaw",
    ]:
        assert phrase in instructions


def test_server_instructions_reflect_disabled_host_workers(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "false")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    server = create_mcp_server(api_client=FakeApiClient())
    instructions = server.instructions.lower()

    assert "host-native workers are disabled" in instructions
    assert "configured default 'docker'" in instructions
    assert "do not request execution_mode='host'" in instructions


def test_enterprise_mcp_http_auth_middleware_gates_transport_requests(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-token")

    async def ok(request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/mcp", ok, methods=["POST"])])
    app.add_middleware(mcp_server.EnterpriseMcpHttpAuthMiddleware)
    client = TestClient(app)
    good_headers = {
        "X-GlassHive-Service-Token": "service-token",
        "X-GlassHive-Tenant-Id": "tenant-alpha",
        "X-GlassHive-User-Id": "user-a",
    }

    assert client.post("/mcp").status_code == 401
    assert client.post("/mcp", headers={"X-WPR-Token": "wrong"}).status_code == 401
    assert client.post("/mcp", headers={"X-WPR-Token": "service-token"}).status_code == 401
    assert client.post(
        "/mcp",
        headers={
            "X-GlassHive-Service-Token": "service-token",
            "X-GlassHive-Tenant-Id": "tenant-alpha",
        },
    ).status_code == 401
    assert client.post("/mcp", headers={**good_headers, "X-GlassHive-Tenant-Id": "tenant-beta"}).status_code == 401
    assert client.post("/mcp", headers=good_headers).status_code == 200
    assert client.post("/mcp", headers={**good_headers, "Authorization": "Bearer service-token"}).status_code == 200


def test_enterprise_mcp_requires_service_authentication(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setattr(mcp_server, "DEFAULT_API_TOKEN", "service-token")

    with pytest.raises(PermissionError):
        mcp_server._require_enterprise_mcp_service_auth(
            {
                "x-viventium-tenant-id": "tenant",
                "x-viventium-user-id": "forged-user",
            }
        )

    mcp_server._require_enterprise_mcp_service_auth(
        {
            "x-wpr-token": "service-token",
            "x-viventium-tenant-id": "tenant-alpha",
            "x-viventium-user-id": "user-a",
        }
    )
    mcp_server._require_enterprise_mcp_service_auth(
        {
            "x-glasshive-service-token": "service-token",
            "x-glasshive-tenant-id": "tenant-alpha",
            "x-glasshive-user-id": "user-a",
        }
    )
    mcp_server._require_enterprise_mcp_service_auth({"authorization": "Bearer service-token"})

    with pytest.raises(PermissionError, match="tenant assertion"):
        mcp_server._require_enterprise_mcp_identity_assertion(
            {
                "x-wpr-token": "service-token",
                "x-viventium-tenant-id": "tenant-beta",
                "x-viventium-user-id": "user-a",
            }
        )
    with pytest.raises(PermissionError, match="user assertion"):
        mcp_server._require_enterprise_mcp_identity_assertion(
            {
                "x-wpr-token": "service-token",
                "x-viventium-tenant-id": "tenant-alpha",
            }
        )
    mcp_server._require_enterprise_mcp_identity_assertion(
        {
            "x-wpr-token": "service-token",
            "x-viventium-tenant-id": "tenant-alpha",
            "x-viventium-user-id": "user-a",
        }
    )


def test_enterprise_owner_and_alias_accept_generic_glasshive_identity_headers(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setattr(mcp_server, "DEFAULT_API_TOKEN", "service-token")
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-GlassHive-Service-Token": "service-token",
            "X-GlassHive-Tenant-Id": "tenant-alpha",
            "X-GlassHive-User-Id": "user-a",
        },
    )

    assert mcp_server._request_owner_id("spoofed-owner") == "user-a"
    assert mcp_server._request_scoped_alias("Shared Workspace") == "tenant-alpha--user-a--shared-workspace"


def test_enterprise_worker_delegate_once_rejects_before_preflight_without_service_auth(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setattr(mcp_server, "DEFAULT_API_TOKEN", "service-token")
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            with pytest.raises(ToolError):
                await client.call_tool(
                    "worker_delegate_once",
                    {
                        "title": "No auth",
                        "instruction": "This should not reach callback preflight or create work.",
                        "profile": "codex-cli",
                    },
                )

    asyncio.run(scenario())


def test_mcp_transport_security_allows_configured_public_host(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_MCP_URL", "http://glasshive.localtest.me:8877/mcp")
    server = create_mcp_server(host="127.0.0.1", port=8877, api_client=FakeApiClient())

    settings = server.settings.transport_security
    assert settings is not None
    assert settings.enable_dns_rebinding_protection is True
    assert "127.0.0.1:*" in settings.allowed_hosts
    assert "glasshive.localtest.me:8877" in settings.allowed_hosts
    assert "glasshive.localtest.me:*" in settings.allowed_hosts


def test_mcp_transport_security_keeps_unknown_hosts_closed(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_MCP_URL", "http://glasshive.localtest.me:8877/mcp")
    server = create_mcp_server(host="127.0.0.1", port=8877, api_client=FakeApiClient())

    settings = server.settings.transport_security
    assert settings is not None
    assert "evil.example.com:8877" not in settings.allowed_hosts


def test_mcp_server_exposes_tools_and_resources(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "http://127.0.0.1:8780")
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            tools = await client.list_tools()
            tool_names = {tool.name for tool in tools}
            assert "project_create" in tool_names
            assert "workspace_launch" in tool_names
            assert "workspace_schedule" in tool_names
            assert "worker_delegate_once" in tool_names
            assert "worker_schedule" in tool_names
            assert "worker_schedules" in tool_names
            assert "worker_takeover" in tool_names
            assert "workspace_artifacts" in tool_names
            assert "workspace_artifact_download" in tool_names
            assert "worker_find_or_resume" in tool_names
            assert "workspace_preferences_get" in tool_names
            assert "workspace_preferences_set" in tool_names

            created = await client.call_tool(
                "project_create",
                {"owner_id": "demo-owner", "title": "Inbox Zero", "goal": "Triage open loops"},
            )
            created_payload = _tool_json(created)
            assert created_payload["project_id"] == "prj_new"

            worker = await client.call_tool(
                "worker_find_or_resume",
                {
                    "project_id": "prj_new",
                    "owner_id": "demo-owner",
                    "name": "Codex Host",
                    "role": "coding",
                    "alias": "codex-main",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                },
            )
            worker_payload = _tool_json(worker)
            assert worker_payload["worker_id"] == "wrk_resumed"
            assert worker_payload["execution_mode"] == "host"

            takeover = await client.call_tool("worker_takeover", {"worker_id": "wrk_123"})
            takeover_payload = _tool_json(takeover)
            assert takeover_payload["takeover"]["supported"] is True
            assert takeover_payload["operator_url"] == (
                "http://127.0.0.1:8780/watch/wrk_123?surface=desktop&project_id=prj_123"
            )
            assert takeover_payload["watch_url"] == takeover_payload["operator_url"]
            assert takeover_payload["view_url"] == takeover_payload["operator_url"]
            assert takeover_payload["direct_desktop_url"].startswith("http://127.0.0.1:")
            assert takeover_payload["runtime_takeover_url"].endswith("/ui/workers/wrk_123/view")

            live_resource = await client.read_resource("wpr://workers/wrk_123/live")
            assert live_resource[0].text is not None
            live_payload = json.loads(live_resource[0].text)
            assert live_payload["worker"]["worker_id"] == "wrk_123"

    asyncio.run(scenario())

def test_workspace_artifacts_returns_signed_download_links(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ARTIFACT_BASE_URL", "https://glasshive.example.test")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            listed = await client.call_tool("workspace_artifacts", {"worker_id": "wrk_123"})
            payload = _tool_json(listed)
            assert payload["status"] == "ok"
            assert payload["items"][0]["path"] == "index.html"
            assert all(item["path"] != ".codex/config.toml" for item in payload["items"])
            assert all(not item["path"].startswith("tmp/chrome-user-data/") for item in payload["items"])
            assert all(not item["path"].startswith("uploads/") for item in payload["items"])
            assert payload["items"][0]["signed_open_url"].startswith(
                "https://glasshive.example.test/v1/signed-links/"
            )
            assert payload["items"][0]["signed_download_url"].startswith(
                "https://glasshive.example.test/v1/signed-links/"
            )
            assert "127.0.0.1" not in payload["items"][0]["signed_open_url"]
            assert "127.0.0.1" not in payload["items"][0]["signed_download_url"]
            assert "Use relevant signed_open_url values" in payload["next_action_guidance"]
            open_token = urlsplit(payload["items"][0]["signed_open_url"]).path.rsplit("/", 1)[-1]
            download_token = urlsplit(payload["items"][0]["signed_download_url"]).path.rsplit("/", 1)[-1]
            assert verify_signed_link_token(open_token)["kind"] == "artifact_open"
            assert verify_signed_link_token(download_token)["kind"] == "artifact_download"

            download = await client.call_tool(
                "workspace_artifact_download",
                {"worker_id": "wrk_123", "path": "index.html"},
            )
            download_payload = _tool_json(download)
            assert download_payload["status"] == "ok"
            assert download_payload["signed_open_url"].startswith(
                "https://glasshive.example.test/v1/signed-links/"
            )
            assert download_payload["signed_download_url"].startswith(
                "https://glasshive.example.test/v1/signed-links/"
            )
            assert download_payload["path"] == "index.html"
            assert "Use signed_open_url as the user-facing file link" in download_payload["next_action_guidance"]
            open_token = urlsplit(download_payload["signed_open_url"]).path.rsplit("/", 1)[-1]
            download_token = urlsplit(download_payload["signed_download_url"]).path.rsplit("/", 1)[-1]
            assert verify_signed_link_token(open_token)["kind"] == "artifact_open"
            assert verify_signed_link_token(download_token)["kind"] == "artifact_download"

    asyncio.run(scenario())


def test_workspace_launch_uses_saved_profile_and_effort_preferences(monkeypatch):
    api = PreferenceApiClient()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            saved = await client.call_tool(
                "workspace_preferences_set",
                {"default_worker_profile": "codex-cli", "codex_reasoning_effort": "xhigh"},
            )
            assert _tool_json(saved)["default_worker_profile"] == "codex-cli"

            launched = await client.call_tool(
                "workspace_launch",
                {
                    "description": "Create a synthetic QA marker",
                    "success_criteria": "Marker exists",
                    "expose_diagnostics": True,
                },
            )
            payload = _tool_json(launched)
            assert payload["profile"] == "codex-cli"
            assert payload["effort"] == "xhigh"
            bundle = api.find_or_resume_payloads[-1]["bootstrap_bundle"]
            assert bundle["env"]["WPR_CODEX_CLI_REASONING_EFFORT"] == "xhigh"
            assigned = api.assign_run_payloads[-1]
            assert assigned["effort"] == "xhigh"
            assert assigned["bootstrap_bundle"]["env"]["WPR_CODEX_CLI_REASONING_EFFORT"] == "xhigh"

    asyncio.run(scenario())


def test_workspace_launch_projects_saved_claude_max_effort(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "codex-cli,claude-code")
    api = PreferenceApiClient()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            saved = await client.call_tool(
                "workspace_preferences_set",
                {"default_worker_profile": "claude-code", "claude_effort": "max"},
            )
            assert _tool_json(saved)["default_worker_profile"] == "claude-code"

            launched = await client.call_tool(
                "workspace_launch",
                {
                    "description": "Create a synthetic Claude QA marker",
                    "success_criteria": "Marker exists",
                    "expose_diagnostics": True,
                },
            )
            payload = _tool_json(launched)
            assert payload["profile"] == "claude-code"
            assert payload["effort"] == "max"
            bundle = api.find_or_resume_payloads[-1]["bootstrap_bundle"]
            assert bundle["env"]["WPR_CLAUDE_CODE_EFFORT"] == "max"
            assigned = api.assign_run_payloads[-1]
            assert assigned["effort"] == "max"
            assert assigned["bootstrap_bundle"]["env"]["WPR_CLAUDE_CODE_EFFORT"] == "max"

    asyncio.run(scenario())


def test_workspace_artifact_download_rejects_traversal_before_signing(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ARTIFACT_BASE_URL", "https://glasshive.example.test")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            for path in (
                "../runtime_phase1.db",
                "/etc/passwd",
                "outputs/../secret.txt",
                "outputs\\.git/config",
                "tmp/chrome-user-data/Default/Default/Extensions/fdpohaocaechififmbbbbbknoalclacl/8.6_0/capture/index.html",
                "uploads/source.txt.metadata.json",
            ):
                with pytest.raises(ToolError):
                    await client.call_tool(
                        "workspace_artifact_download",
                        {"worker_id": "wrk_123", "path": path},
                    )

    asyncio.run(scenario())


@pytest.mark.parametrize("surface", ["librechat", "glasshive"])
def test_workspace_status_returns_view_steer_link_for_web_mcp_surfaces(monkeypatch, surface):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive.example.test")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {"X-GlassHive-Surface": surface})
    api_client = PollingApiClient(["completed"])
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            status = await client.call_tool(
                "workspace_status",
                {"run_id": "run_poll", "worker_id": "wrk_poll"},
            )
            payload = _tool_json(status)
            assert payload["view_steer_url"].startswith(
                "https://glasshive.example.test/watch/wrk_poll?surface=desktop&project_id=prj_poll"
            )
            assert "gh_token=" in payload["view_steer_url"]
            assert payload["view_steer"]["include_in_response"] is True

    asyncio.run(scenario())


def test_workspace_wait_prefers_newer_worker_run_over_stale_failed_run(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive.example.test")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")
    server = create_mcp_server(api_client=StaleRequestedRunApiClient())

    async def scenario():
        async with Client(server) as client:
            waited = await client.call_tool(
                "workspace_wait",
                {
                    "run_id": "run_old_failed",
                    "worker_id": "wrk_stale",
                    "timeout_seconds": 0,
                },
            )
            payload = _tool_json(waited)
            assert payload["status"] == "completed"
            assert payload["requested_run_stale"] is True
            assert payload["requested_run_id"] == "run_old_failed"
            assert payload["requested_run_state"] == "failed"
            assert payload["run_id"] == "run_new_completed"
            assert payload["latest_run_id"] == "run_new_completed"
            assert payload["run_state"] == "completed"
            assert payload["output_text"] == "Latest artifact is ready"
            assert payload["artifact_links"]["items"][0]["signed_open_url"].startswith(
                "https://glasshive.example.test/v1/signed-links/"
            )
            assert "signed_download_url" not in payload["artifact_links"]["items"][0]
            assert "acknowledge the requested run outcome first" in payload["next_action_guidance"]

    asyncio.run(scenario())


def test_workspace_status_failed_run_surfaces_partial_artifact_links(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive.example.test")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")
    server = create_mcp_server(api_client=RetryableFailureApiClient())

    async def scenario():
        async with Client(server) as client:
            checked = await client.call_tool(
                "workspace_status",
                {
                    "run_id": "run_retryable_failed",
                    "worker_id": "wrk_retry",
                    "include_live": False,
                },
            )
            payload = _tool_json(checked)
            assert payload["terminal"] is True
            assert payload["run_state"] == "failed"
            assert payload["failure_class"] == "provider_rate_limited"
            assert payload["artifact_links"]["items"][0]["signed_open_url"].startswith(
                "https://glasshive.example.test/v1/signed-links/"
            )
            assert "artifact_links are optional delivery aids" in payload["next_action_guidance"]
            assert "Markdown-sensitive characters" in payload["next_action_guidance"]
            assert "workspace_continue" in payload["next_action_guidance"]

    asyncio.run(scenario())


def test_workspace_wait_compares_mixed_timezone_run_timestamps(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive.example.test")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")
    server = create_mcp_server(api_client=MixedTimezoneRunOrderApiClient())

    async def scenario():
        async with Client(server) as client:
            waited = await client.call_tool(
                "workspace_wait",
                {
                    "run_id": "run_old_failed",
                    "worker_id": "wrk_stale",
                    "timeout_seconds": 0,
                },
            )
            payload = _tool_json(waited)
            assert payload["requested_run_stale"] is True
            assert payload["run_id"] == "run_new_completed"
            assert payload["output_text"] == "Newer mixed-timezone artifact is ready"

    asyncio.run(scenario())


def test_workspace_status_does_not_mark_requested_run_stale_when_last_run_id_is_older(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive.example.test")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")
    server = create_mcp_server(api_client=OlderLastRunApiClient())

    async def scenario():
        async with Client(server) as client:
            status = await client.call_tool(
                "workspace_status",
                {"run_id": "run_new_completed", "worker_id": "wrk_stale"},
            )
            payload = _tool_json(status)
            assert payload["requested_run_stale"] is False
            assert payload["run_id"] == "run_new_completed"
            assert payload["latest_run_id"] == "run_new_completed"
            assert payload["run_state"] == "completed"
            assert payload["output_text"] == "Requested run is the newest result"

    asyncio.run(scenario())


def test_workspace_wait_returns_terminal_requested_run_when_newer_run_is_still_running(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive.example.test")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")
    server = create_mcp_server(api_client=TerminalRequestedNewerRunningApiClient())

    async def scenario():
        async with Client(server) as client:
            waited = await client.call_tool(
                "workspace_wait",
                {
                    "run_id": "run_old_failed",
                    "worker_id": "wrk_stale",
                    "timeout_seconds": 30,
                    "poll_interval_seconds": 1,
                },
            )
            payload = _tool_json(waited)
            assert payload["status"] == "terminal"
            assert payload["timed_out"] is False
            assert payload["attempts"] == 1
            assert payload["requested_run_stale"] is True
            assert payload["run_id"] == "run_old_failed"
            assert payload["run_state"] == "failed"
            assert payload["error_text"] == "Requested run failed"
            assert payload["latest_run_id"] == "run_new_running"
            assert payload["latest_run_state"] == "running"
            assert "acknowledge the requested run outcome first" in payload["next_action_guidance"]

    asyncio.run(scenario())


def test_enterprise_view_steer_url_does_not_fall_back_to_unsigned(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive.example.test")
    monkeypatch.delenv("GLASSHIVE_SIGNED_LINK_SECRET", raising=False)
    monkeypatch.delenv("WPR_API_TOKEN", raising=False)

    url = mcp_server._signed_view_steer_url(
        {
            "worker_id": "wrk_local",
            "project_id": "prj_local",
            "tenant_id": "local",
            "owner_id": "owner-a",
        },
        "prj_local",
        "librechat",
    )

    assert url is None


def test_enterprise_mcp_refuses_non_http_transport(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")

    with pytest.raises(RuntimeError, match="streamable-http"):
        mcp_server._require_enterprise_mcp_transport("stdio")
    with pytest.raises(RuntimeError, match="streamable-http"):
        mcp_server._require_enterprise_mcp_transport("sse")
    mcp_server._require_enterprise_mcp_transport("streamable-http")


@pytest.mark.parametrize("surface", ["telegram", "voice", "unknown-surface"])
def test_worker_takeover_omits_operator_url_for_non_web_surface(monkeypatch, surface):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "http://127.0.0.1:8780")
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {"X-Viventium-Surface": surface})
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            takeover = await client.call_tool("worker_takeover", {"worker_id": "wrk_123"})
            payload = _tool_json(takeover)
            assert payload["operator_url"] is None
            assert payload["watch_url"] is None
            assert payload["operator_url_available"] is False
            assert payload["operator_url_surface"] == surface
            assert payload["view_url"] is None
            assert payload["runtime_takeover_url"] is None
            assert payload["direct_desktop_url"] is None
            assert payload["terminal_url"] is None
            assert payload["worker_url"] is None
            assert payload["takeover"]["url_available"] is False
            assert "url" not in payload["takeover"]
            serialized = json.dumps(payload)
            assert "127.0.0.1" not in serialized
            assert "localhost" not in serialized
            assert "noVNC" not in serialized

    asyncio.run(scenario())


def test_worker_delegate_once_creates_resumes_and_runs_without_listing(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    monkeypatch.setenv("WPR_CODEX_BIN", "/bin/sh")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_URL", "http://127.0.0.1:3180/api/viventium/glasshive/callback")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "public-safe-test-secret")
    monkeypatch.setattr(mcp_server, "get_http_headers", _callback_headers)
    api_client = TrackingApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            delegated = await client.call_tool(
                "worker_delegate_once",
                {
                    "owner_id": "demo-owner",
                    "title": "Host Page Title QA",
                    "goal": "Open a local page and report the title.",
                    "instruction": "Open the local QA page and reply with the page title.",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                    "bootstrap_bundle_json": {
                        "files": {
                            "project-definition.md": "# Host Page Title QA\n\nReport the page title.\n",
                        }
                    },
                },
            )
            payload = _tool_json(delegated)
            assert payload["status"] == "dispatched"
            assert payload["callback_ready"] is True
            assert payload["view_steer_url"].startswith(
                "http://127.0.0.1:8780/watch/wrk_resumed?surface=desktop&project_id=prj_new"
            )
            assert payload["view_steer"]["include_in_acknowledgement"] is True
            assert payload["pre_wait_user_update"]["use_before_blocking_wait_when_possible"] is True
            assert payload["pre_wait_user_update"]["view_steer_url"] == payload["view_steer_url"]
            assert payload["follow_up_context"]["worker_id"] == "wrk_resumed"
            assert payload["follow_up_context"]["run_id"] == "run_assign"
            assert payload["follow_up_context"]["project_id"] == "prj_new"
            assert payload["follow_up_context"]["status_tool"] == "workspace_status"
            assert payload["follow_up_context"]["blocking_wait_tool"] == "workspace_wait"
            assert "user_status" not in payload
            assert "own voice" in payload["acknowledgement_guidance"]
            assert "labeled [View / Steer](view_steer_url) link" in payload["acknowledgement_guidance"]
            assert "do not paste the bare URL" in payload["acknowledgement_guidance"]
            assert "canned template" in payload["acknowledgement_guidance"]
            assert "Do not call workspace_status" in payload["main_agent_next_action"]
            assert "labeled [View / Steer](view_steer_url) link" in payload["main_agent_next_action"]
            assert "follow_up_context.run_id" in payload["main_agent_next_action"]
            assert payload["delegation_audit"]["title"] == "Host Page Title QA"
            assert "Open the local QA page" in payload["delegation_audit"]["instruction_preview"]
            assert "project_id" not in payload
            assert "worker_id" not in payload
            assert "run_id" not in payload
            assert "alias" not in payload

    asyncio.run(scenario())

    assigned_instruction = api_client.assign_run_payloads[-1]["instruction"]
    assert "Open the local QA page and reply with the page title." in assigned_instruction
    assert "host-side responsibilities" in assigned_instruction
    assert "do not mark the workspace blocked" in assigned_instruction
    assert "report only blockers observable from inside this worker workspace" in assigned_instruction
    assert assigned_instruction.count("Host-side GlassHive orchestration checks") == 1


def test_worker_delegate_once_blocks_missing_host_cli_before_api_calls(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "true")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    monkeypatch.setenv("WPR_CODEX_BIN", "/tmp/glasshive-missing-codex")
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})
    api_client = TrackingApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            delegated = await client.call_tool(
                "worker_delegate_once",
                {
                    "owner_id": "demo-owner",
                    "title": "Unavailable host Codex",
                    "instruction": "Run a host Codex task.",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                },
            )
            payload = _tool_json(delegated)
            assert payload["status"] == "blocked"
            assert payload["failure_class"] == "runtime_dependency_missing"
            assert payload["failure_retryable"] is False
            assert "did not start" in payload["acknowledgement_guidance"]
            assert payload["view_steer_url"] is None
            assert payload["view_steer"]["include_in_acknowledgement"] is False

    asyncio.run(scenario())
    assert api_client.calls == []


def test_worker_delegate_once_recovers_default_host_dependency_to_docker(monkeypatch, tmp_path):
    fake_node = tmp_path / "node"
    fake_node.write_text("#!/usr/bin/env bash\necho 'v20.20.2'\n")
    fake_node.chmod(0o755)
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "true")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    monkeypatch.setenv("WPR_CODEX_BIN", "/bin/echo")
    monkeypatch.setenv(
        "GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON",
        json.dumps({"codex-cli": [{"binary": str(fake_node), "label": "Node.js", "min_version": "22.19.0"}]}),
    )
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})
    api_client = TrackingApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            delegated = await client.call_tool(
                "worker_delegate_once",
                {
                    "owner_id": "demo-owner",
                    "title": "Recover to sandbox",
                    "instruction": "Use GlassHive to complete a simple public-safe task.",
                    "profile": "codex-cli",
                },
            )
            payload = _tool_json(delegated)
            assert payload["status"] == "dispatched"
            assert payload["runtime_recovery"]["from_execution_mode"] == "host"
            assert payload["runtime_recovery"]["to_execution_mode"] == "docker"
            assert payload["follow_up_context"]["run_id"] == "run_assign"
            assert "install global software" in payload["acknowledgement_guidance"]

    asyncio.run(scenario())
    assert api_client.find_or_resume_payloads[-1]["execution_mode"] == "docker"
    assert api_client.assign_run_payloads[-1]["worker_id"] == "wrk_resumed"


def test_worker_delegate_once_blocks_explicit_host_dependency_mismatch_before_api_calls(monkeypatch, tmp_path):
    fake_node = tmp_path / "node"
    fake_node.write_text("#!/usr/bin/env bash\necho 'v20.20.2'\n")
    fake_node.chmod(0o755)
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "true")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    monkeypatch.setenv("WPR_CODEX_BIN", "/bin/echo")
    monkeypatch.setenv(
        "GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON",
        json.dumps({"codex-cli": [{"binary": str(fake_node), "label": "Node.js", "min_version": "22.19.0"}]}),
    )
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})
    api_client = TrackingApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            delegated = await client.call_tool(
                "worker_delegate_once",
                {
                    "owner_id": "demo-owner",
                    "title": "Explicit host stays blocked",
                    "instruction": "Run a host Codex task.",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                },
            )
            payload = _tool_json(delegated)
            assert payload["status"] == "blocked"
            assert payload["failure_class"] == "runtime_dependency_missing"
            assert "Node.js" in payload["failure_user_message"]
            assert "22.19.0" in payload["failure_user_message"]

    asyncio.run(scenario())
    assert api_client.calls == []


def test_workspace_schedule_blocks_missing_host_dependency_before_api_calls(monkeypatch, tmp_path):
    fake_node = tmp_path / "node"
    fake_node.write_text("#!/usr/bin/env bash\necho 'v20.20.2'\n")
    fake_node.chmod(0o755)
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "true")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    monkeypatch.setenv("WPR_CODEX_BIN", "/bin/echo")
    monkeypatch.setenv(
        "GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON",
        json.dumps({"codex-cli": [{"binary": str(fake_node), "label": "Node.js", "min_version": "22.19.0"}]}),
    )
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})
    api_client = TrackingApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            scheduled = await client.call_tool(
                "workspace_schedule",
                {
                    "description": "Run later on host",
                    "success_criteria": "The scheduled host run is accepted only if the runtime is available.",
                    "schedule_text": "in 20 minutes",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                },
            )
            payload = _tool_json(scheduled)
            assert payload["status"] == "blocked"
            assert payload["failure_class"] == "runtime_dependency_missing"
            assert "Node.js" in payload["failure_user_message"]
            assert "22.19.0" in payload["failure_user_message"]

    asyncio.run(scenario())
    assert api_client.calls == []
    assert api_client.schedule_run_payloads == []


def test_workspace_schedule_recovers_default_host_dependency_to_docker(monkeypatch, tmp_path):
    fake_node = tmp_path / "node"
    fake_node.write_text("#!/usr/bin/env bash\necho 'v20.20.2'\n")
    fake_node.chmod(0o755)
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "true")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    monkeypatch.setenv("WPR_CODEX_BIN", "/bin/echo")
    monkeypatch.setenv(
        "GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON",
        json.dumps({"codex-cli": [{"binary": str(fake_node), "label": "Node.js", "min_version": "22.19.0"}]}),
    )
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})
    api_client = TrackingApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            scheduled = await client.call_tool(
                "workspace_schedule",
                {
                    "description": "Run later using the available runtime",
                    "success_criteria": "The scheduled run is accepted using safe recovery.",
                    "schedule_text": "in 20 minutes",
                    "profile": "codex-cli",
                    "expose_diagnostics": True,
                },
            )
            payload = _tool_json(scheduled)
            assert payload["status"] == "scheduled"
            assert payload["execution_mode"] == "docker"
            assert payload["runtime_recovery"]["to_execution_mode"] == "docker"

    asyncio.run(scenario())
    assert api_client.find_or_resume_payloads[-1]["execution_mode"] == "docker"
    assert api_client.schedule_run_payloads[-1]["schedule_text"] == "in 20 minutes"


def test_worker_schedule_blocks_missing_host_dependency_before_schedule(monkeypatch, tmp_path):
    fake_node = tmp_path / "node"
    fake_node.write_text("#!/usr/bin/env bash\necho 'v20.20.2'\n")
    fake_node.chmod(0o755)
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "true")
    monkeypatch.setenv("WPR_CODEX_BIN", "/bin/echo")
    monkeypatch.setenv(
        "GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON",
        json.dumps({"codex-cli": [{"binary": str(fake_node), "label": "Node.js", "min_version": "22.19.0"}]}),
    )

    class HostWorkerApiClient(TrackingApiClient):
        def get_worker(self, worker_id: str):
            payload = super().get_worker(worker_id)
            payload.update({"profile": "codex-cli", "execution_mode": "host"})
            return payload

    api_client = HostWorkerApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            scheduled = await client.call_tool(
                "worker_schedule",
                {
                    "worker_id": "wrk_host",
                    "instruction": "Run later on host.",
                    "schedule_text": "in 20 minutes",
                },
            )
            payload = _tool_json(scheduled)
            assert payload["status"] == "blocked"
            assert payload["failure_class"] == "runtime_dependency_missing"
            assert "Node.js" in payload["failure_user_message"]

    asyncio.run(scenario())
    assert api_client.schedule_run_payloads == []


def test_worker_schedule_and_workspace_schedule_are_glasshive_native(monkeypatch):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_URL", "http://127.0.0.1:3180/api/viventium/glasshive/callback")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "public-safe-test-secret")
    monkeypatch.setattr(mcp_server, "get_http_headers", _callback_headers)
    api_client = TrackingApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            scheduled = await client.call_tool(
                "worker_schedule",
                {
                    "worker_id": "wrk_123",
                    "instruction": "Create scheduled-proof.txt",
                    "schedule_text": "in 20 minutes",
                },
            )
            scheduled_payload = _tool_json(scheduled)
            assert scheduled_payload["schedule_id"] == "sch_123"
            assert scheduled_payload["schedule_text"] == "in 20 minutes"

            workspace = await client.call_tool(
                "workspace_schedule",
                {
                    "description": "Check the workspace later",
                    "success_criteria": "A scheduled run is accepted",
                    "schedule_text": "in 20 minutes",
                    "profile": "codex-cli",
                    "execution_mode": "docker",
                    "expose_diagnostics": True,
                },
            )
            workspace_payload = _tool_json(workspace)
            assert workspace_payload["status"] == "scheduled"
            assert workspace_payload["schedule_id"] == "sch_123"
            assert workspace_payload["callback_ready"] is True

    asyncio.run(scenario())
    assert api_client.calls == ["create_project", "find_or_resume_worker"]
    assert api_client.find_or_resume_payloads[0]["start_synchronously"] is False


def test_worker_delegate_once_exposes_diagnostics_only_when_requested(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    monkeypatch.setenv("WPR_CODEX_BIN", "/bin/sh")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_URL", "http://127.0.0.1:3180/api/viventium/glasshive/callback")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "public-safe-test-secret")
    monkeypatch.setattr(mcp_server, "get_http_headers", _callback_headers)
    server = create_mcp_server(api_client=TrackingApiClient())

    async def scenario():
        async with Client(server) as client:
            delegated = await client.call_tool(
                "worker_delegate_once",
                {
                    "owner_id": "demo-owner",
                    "title": "Diagnostic host task",
                    "instruction": "Run a diagnostic host task.",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                    "expose_diagnostics": True,
                },
            )
            payload = _tool_json(delegated)
            assert payload["project_id"] == "prj_new"
            assert payload["worker_id"] == "wrk_resumed"
            assert payload["run_id"] == "run_assign"
            assert payload["execution_mode"] == "host"
            assert payload["alias"] == "codex-cli-diagnostic-host-task"
            assert payload["submitted_instruction"].startswith("Run a diagnostic host task.")
            assert "host-side responsibilities" in payload["submitted_instruction"]
            assert "report only blockers observable from inside this worker workspace" in payload["submitted_instruction"]

    asyncio.run(scenario())


def test_worker_delegate_once_dispatches_without_callback_by_default(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    monkeypatch.setenv("WPR_CODEX_BIN", "/bin/sh")
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_CALLBACK_URL", raising=False)
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", raising=False)
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})
    api_client = TrackingApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            delegated = await client.call_tool(
                "worker_delegate_once",
                {
                    "owner_id": "demo-owner",
                    "title": "Host Page Title QA",
                    "instruction": "Open the local QA page and reply with the page title.",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                },
            )
            payload = _tool_json(delegated)
            assert payload["status"] == "dispatched"
            assert payload["callback_ready"] is False
            assert payload["callback_delivery"] == "not_configured_standalone_polling_available"
            assert payload["follow_up_context"]["status_tool"] == "workspace_status"
            assert payload["follow_up_context"]["blocking_wait_tool"] == "workspace_wait"
            assert "user_status" not in payload
            assert "ask for status" in payload["acknowledgement_guidance"]
            assert "workspace_status" in payload["main_agent_next_action"]
            assert payload["missing_callback_fields"] == []

    asyncio.run(scenario())
    assert api_client.calls == ["create_project", "find_or_resume_worker", "assign_run"]


def test_worker_delegate_once_can_require_callback_for_host_apps(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    monkeypatch.setenv("WPR_CODEX_BIN", "/bin/sh")
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_CALLBACK_URL", raising=False)
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", raising=False)
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})
    api_client = TrackingApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            delegated = await client.call_tool(
                "worker_delegate_once",
                {
                    "owner_id": "demo-owner",
                    "title": "Host callback-required QA",
                    "instruction": "Only dispatch when host callback context is present.",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                    "require_callback": True,
                },
            )
            payload = _tool_json(delegated)
            assert payload["status"] == "blocked"
            assert payload["callback_ready"] is False
            assert "conversation_id" in payload["missing_callback_fields"]

    asyncio.run(scenario())
    assert api_client.calls == []


def test_workspace_status_and_wait_are_standalone_mcp_followup_tools(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "http://127.0.0.1:8780")
    monkeypatch.setenv("WPR_MCP_BLOCKING_WAIT_POLL_INTERVAL_SEC", "1")
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {"X-Viventium-Surface": "web"})
    api_client = PollingApiClient(["queued", "running", "completed"])
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            status = await client.call_tool(
                "workspace_status",
                {"run_id": "run_poll", "worker_id": "wrk_poll"},
            )
            status_payload = _tool_json(status)
            assert status_payload["mode"] == "non_blocking"
            assert status_payload["terminal"] is False
            assert status_payload["run_state"] == "queued"
            assert status_payload["worker_state"] == "ready"
            assert status_payload["view_steer_url"].startswith(
                "http://127.0.0.1:8780/watch/wrk_poll?surface=desktop&project_id=prj_poll"
            )

            waited = await client.call_tool(
                "workspace_wait",
                {
                    "run_id": "run_poll",
                    "worker_id": "wrk_poll",
                    "timeout_seconds": 1,
                    "poll_interval_seconds": 0.01,
                },
            )
            waited_payload = _tool_json(waited)
            assert waited_payload["mode"] == "blocking_wait"
            assert waited_payload["status"] == "completed"
            assert waited_payload["terminal"] is True
            assert waited_payload["output_text"] == "Poll completed"
            assert waited_payload["attempts"] == 2

    asyncio.run(scenario())
    assert api_client.get_run_calls == 3


def test_workspace_wait_enforces_configured_poll_interval_floor(monkeypatch):
    monkeypatch.setenv("WPR_MCP_BLOCKING_WAIT_POLL_INTERVAL_SEC", "7")
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {"X-Viventium-Surface": "web"})
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(mcp_server.asyncio, "sleep", fake_sleep)
    api_client = PollingApiClient(["running", "completed"])
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            waited = await client.call_tool(
                "workspace_wait",
                {
                    "run_id": "run_poll",
                    "worker_id": "wrk_poll",
                    "timeout_seconds": 30,
                    "poll_interval_seconds": 0.01,
                    "include_live": False,
                },
            )
            waited_payload = _tool_json(waited)
            assert waited_payload["status"] == "completed"
            assert waited_payload["attempts"] == 2

    asyncio.run(scenario())
    assert sleep_calls == [7.0]


def test_workspace_wait_default_polling_backs_off_without_host_parameters():
    assert mcp_server._blocking_wait_sleep_interval_seconds(attempts=1, base_interval=5.0, adaptive=True) == 5.0
    assert mcp_server._blocking_wait_sleep_interval_seconds(attempts=7, base_interval=5.0, adaptive=True) == 10.0
    assert mcp_server._blocking_wait_sleep_interval_seconds(attempts=13, base_interval=5.0, adaptive=True) == 20.0
    assert mcp_server._blocking_wait_sleep_interval_seconds(attempts=19, base_interval=5.0, adaptive=True) == 30.0
    assert mcp_server._blocking_wait_sleep_interval_seconds(attempts=19, base_interval=5.0, adaptive=False) == 5.0


def test_workspace_wait_rejects_non_finite_timing_inputs(monkeypatch):
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {"X-Viventium-Surface": "web"})
    api_client = PollingApiClient(["running"])
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            with pytest.raises(ToolError, match="poll_interval_seconds must be a finite number"):
                await client.call_tool(
                    "workspace_wait",
                    {
                        "run_id": "run_poll",
                        "worker_id": "wrk_poll",
                        "timeout_seconds": 0,
                        "poll_interval_seconds": "NaN",
                    },
                )

            with pytest.raises(ToolError, match="timeout_seconds must be a finite number"):
                await client.call_tool(
                    "workspace_wait",
                    {
                        "run_id": "run_poll",
                        "worker_id": "wrk_poll",
                        "timeout_seconds": "Infinity",
                    },
                )

            with pytest.raises(ToolError, match="poll_interval_seconds must be greater than 0"):
                await client.call_tool(
                    "workspace_wait",
                    {
                        "run_id": "run_poll",
                        "worker_id": "wrk_poll",
                        "timeout_seconds": 0,
                        "poll_interval_seconds": 0,
                    },
                )

    asyncio.run(scenario())


def test_workspace_wait_resolves_same_conversation_recent_launch_when_ids_are_omitted(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "http://127.0.0.1:8780")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-Surface": "web",
            "X-Viventium-User-Id": "qa-user",
            "X-Viventium-Conversation-Id": "conv-recent-dispatch",
        },
    )
    api_client = RememberedDispatchApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            launched = await client.call_tool(
                "workspace_launch",
                {
                    "description": "Create a public-safe marker file.",
                    "success_criteria": "The marker file exists.",
                    "profile": "codex-cli",
                    "execution_mode": "docker",
                },
            )
            launch_payload = _tool_json(launched)
            assert launch_payload["status"] == "dispatched"
            assert launch_payload["follow_up_context"]["run_id"] == "run_assign"

            waited = await client.call_tool(
                "workspace_wait",
                {
                    "timeout_seconds": 0,
                    "poll_interval_seconds": 0.01,
                },
            )
            waited_payload = _tool_json(waited)
            assert waited_payload["status"] == "completed"
            assert waited_payload["run_id"] == "run_assign"
            assert waited_payload["worker_id"] == "wrk_resumed"
            assert waited_payload["resolved_from_recent_dispatch"] is True
            assert waited_payload["output_text"] == "remembered dispatch completed"

            status = await client.call_tool("workspace_status", {})
            status_payload = _tool_json(status)
            assert status_payload["run_id"] == "run_assign"
            assert status_payload["resolved_from_recent_dispatch"] is True

    asyncio.run(scenario())


def test_enterprise_launch_without_conversation_id_is_not_remembered(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-token")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    monkeypatch.setattr(mcp_server, "DEFAULT_API_TOKEN", "service-token")
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-GlassHive-Service-Token": "service-token",
            "X-GlassHive-Tenant-Id": "tenant-alpha",
            "X-GlassHive-User-Id": "user-a",
            "X-GlassHive-Surface": "web",
        },
    )
    api_client = RememberedDispatchApiClient(tenant_id="tenant-alpha")
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            launched = await client.call_tool(
                "workspace_launch",
                {
                    "description": "Enterprise scoped marker.",
                    "success_criteria": "The marker exists.",
                    "profile": "codex-cli",
                    "execution_mode": "docker",
                },
            )
            launch_payload = _tool_json(launched)
            assert launch_payload["status"] == "dispatched"

            with pytest.raises(ToolError, match="recent GlassHive launch"):
                await client.call_tool("workspace_wait", {"timeout_seconds": 0})

            explicit_status = await client.call_tool(
                "workspace_status",
                {
                    "run_id": launch_payload["follow_up_context"]["run_id"],
                    "worker_id": launch_payload["follow_up_context"]["worker_id"],
                },
            )
            assert _tool_json(explicit_status)["run_id"] == "run_assign"

    asyncio.run(scenario())


def test_enterprise_recent_launch_fallback_is_scoped_by_user_and_conversation(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-token")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    monkeypatch.setattr(mcp_server, "DEFAULT_API_TOKEN", "service-token")
    current_headers = {
        "X-GlassHive-Service-Token": "service-token",
        "X-GlassHive-Tenant-Id": "tenant-alpha",
        "X-GlassHive-User-Id": "user-a",
        "X-GlassHive-Conversation-Id": "conv-a",
        "X-GlassHive-Surface": "web",
    }
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: current_headers)
    api_client = RememberedDispatchApiClient(tenant_id="tenant-alpha")
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        nonlocal current_headers
        async with Client(server) as client:
            launched = await client.call_tool(
                "workspace_launch",
                {
                    "description": "Enterprise scoped marker.",
                    "success_criteria": "The marker exists.",
                    "profile": "codex-cli",
                    "execution_mode": "docker",
                },
            )
            assert _tool_json(launched)["status"] == "dispatched"

            same_user = await client.call_tool(
                "workspace_wait",
                {"timeout_seconds": 0, "poll_interval_seconds": 0.01},
            )
            assert _tool_json(same_user)["resolved_from_recent_dispatch"] is True

            current_headers = {
                **current_headers,
                "X-GlassHive-User-Id": "user-b",
            }
            with pytest.raises(ToolError, match="recent GlassHive launch"):
                await client.call_tool("workspace_wait", {"timeout_seconds": 0})

            current_headers = {
                **current_headers,
                "X-GlassHive-User-Id": "user-a",
                "X-GlassHive-Conversation-Id": "conv-b",
            }
            with pytest.raises(ToolError, match="recent GlassHive launch"):
                await client.call_tool("workspace_status", {})

            current_headers = {
                key: value
                for key, value in current_headers.items()
                if key != "X-GlassHive-Conversation-Id"
            }
            with pytest.raises(ToolError, match="recent GlassHive launch"):
                await client.call_tool("workspace_wait", {"timeout_seconds": 0})

    asyncio.run(scenario())


def test_workspace_status_surfaces_retryable_failure_metadata(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "http://127.0.0.1:8780")
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {"X-Viventium-Surface": "web"})
    server = create_mcp_server(api_client=RetryableFailureApiClient())

    async def scenario():
        async with Client(server) as client:
            status = await client.call_tool(
                "workspace_status",
                {"run_id": "run_retryable_failed", "worker_id": "wrk_retry"},
            )
            payload = _tool_json(status)
            assert payload["terminal"] is True
            assert payload["run_state"] == "failed"
            assert payload["failure_class"] == "provider_rate_limited"
            assert payload["failure_retryable"] is True
            assert "rate-limited" in payload["failure_user_message"]
            assert "workspace_continue" in payload["failure_recommended_recovery"]
            assert "workspace_continue" in payload["next_action_guidance"]

    asyncio.run(scenario())


def test_workspace_continue_queues_same_workspace_recovery(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "http://127.0.0.1:8780")
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {"X-Viventium-Surface": "web"})
    api_client = RetryableFailureApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            continued = await client.call_tool(
                "workspace_continue",
                {
                    "run_id": "run_retryable_failed",
                    "continuation_goal": "Continue and finish the workbook from current partial files.",
                    "effort": "medium",
                },
            )
            payload = _tool_json(continued)
            assert payload["status"] == "queued"
            assert payload["previous_run_id"] == "run_retryable_failed"
            assert payload["previous_failure_class"] == "provider_rate_limited"
            assert payload["effort"] == "medium"
            assert payload["follow_up_context"]["run_id"] == "run_continued"
            assert payload["view_steer_url"].startswith("http://127.0.0.1:8780/watch/wrk_retry")

    asyncio.run(scenario())
    assert len(api_client.assigned) == 1
    instruction = api_client.assigned[0]["instruction"]
    assert "Original task:" in instruction
    assert "Build the requested research workbook and report." in instruction
    assert "Previous failure classification:" in instruction
    assert "provider_rate_limited" in instruction
    assert "current files" in instruction
    assert api_client.assigned[0]["effort"] == "medium"


def test_workspace_continue_does_not_nest_previous_continue_wrappers(monkeypatch):
    class NestedContinuationApiClient(RetryableFailureApiClient):
        def get_run(self, run_id: str):
            payload = super().get_run(run_id)
            if run_id == "run_retryable_failed":
                payload["instruction"] = (
                    "Continue this GlassHive workspace from its current files, browser state, notes, and partial outputs.\n\n"
                    "Preserve the original user request, success criteria, response format, and any files already available in the workspace.\n\n"
                    "Original task:\nBuild the requested research workbook and report.\n\n"
                    "Previous failure classification:\n- class: provider_response_failed\n"
                    "- retryable: True\n\n"
                    "Continuation request:\nResume the original task from current files."
                )
            return payload

    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "http://127.0.0.1:8780")
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {"X-Viventium-Surface": "web"})
    api_client = NestedContinuationApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            await client.call_tool(
                "workspace_continue",
                {
                    "run_id": "run_retryable_failed",
                    "continuation_goal": "Finish from the existing files.",
                },
            )

    asyncio.run(scenario())
    instruction = api_client.assigned[0]["instruction"]
    assert instruction.count("Original task:") == 1
    assert "Build the requested research workbook and report." in instruction
    assert "Resume the original task from current files." not in instruction
    assert "Finish from the existing files." in instruction


def test_workspace_continue_rejects_mismatched_worker_id(monkeypatch):
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})
    api_client = RetryableFailureApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            with pytest.raises(ToolError, match="worker_id must match"):
                await client.call_tool(
                    "workspace_continue",
                    {"run_id": "run_retryable_failed", "worker_id": "wrk_other"},
                )

    asyncio.run(scenario())
    assert api_client.assigned == []


def test_workspace_continue_rejects_active_previous_run(monkeypatch):
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})
    api_client = EnterpriseRetryableFailureApiClient(previous_state="running")
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            with pytest.raises(ToolError, match="only for terminal"):
                await client.call_tool(
                    "workspace_continue",
                    {"run_id": "run_retryable_failed"},
                )

    asyncio.run(scenario())
    assert api_client.assigned == []


def test_enterprise_workspace_continue_rechecks_tenant_and_owner_scope(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setattr(mcp_server, "DEFAULT_API_TOKEN", "service-token")
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-GlassHive-Service-Token": "service-token",
            "X-GlassHive-Tenant-Id": "tenant-alpha",
            "X-GlassHive-User-Id": "user-a",
            "X-GlassHive-Surface": "web",
        },
    )
    api_client = EnterpriseRetryableFailureApiClient(worker_owner_id="user-b")
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            with pytest.raises(ToolError, match="authenticated user"):
                await client.call_tool(
                    "workspace_continue",
                    {"run_id": "run_retryable_failed"},
                )

    asyncio.run(scenario())
    assert api_client.assigned == []


def test_enterprise_workspace_continue_rejects_cross_tenant_previous_run(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setattr(mcp_server, "DEFAULT_API_TOKEN", "service-token")
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-GlassHive-Service-Token": "service-token",
            "X-GlassHive-Tenant-Id": "tenant-alpha",
            "X-GlassHive-User-Id": "user-a",
            "X-GlassHive-Surface": "web",
        },
    )
    api_client = EnterpriseRetryableFailureApiClient(run_tenant_id="tenant-beta")
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            with pytest.raises(ToolError, match="authenticated tenant"):
                await client.call_tool(
                    "workspace_continue",
                    {"run_id": "run_retryable_failed"},
                )

    asyncio.run(scenario())
    assert api_client.assigned == []


def test_enterprise_workspace_status_rechecks_tenant_and_owner_scope(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setattr(mcp_server, "DEFAULT_API_TOKEN", "service-token")
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-GlassHive-Service-Token": "service-token",
            "X-GlassHive-Tenant-Id": "tenant-alpha",
            "X-GlassHive-User-Id": "user-a",
            "X-GlassHive-Surface": "web",
        },
    )
    api_client = EnterpriseRetryableFailureApiClient(worker_owner_id="user-b")
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            with pytest.raises(ToolError, match="authenticated user"):
                await client.call_tool(
                    "workspace_status",
                    {"run_id": "run_retryable_failed", "worker_id": "wrk_retry"},
                )

    asyncio.run(scenario())


def test_enterprise_workspace_artifacts_rechecks_owner_scope(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")
    monkeypatch.setattr(mcp_server, "DEFAULT_API_TOKEN", "service-token")
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-GlassHive-Service-Token": "service-token",
            "X-GlassHive-Tenant-Id": "tenant-alpha",
            "X-GlassHive-User-Id": "user-a",
        },
    )
    api_client = EnterpriseRetryableFailureApiClient(worker_owner_id="user-b")
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            with pytest.raises(ToolError, match="authenticated user"):
                await client.call_tool("workspace_artifacts", {"worker_id": "wrk_retry"})
            with pytest.raises(ToolError, match="authenticated user"):
                await client.call_tool(
                    "workspace_artifact_download",
                    {"worker_id": "wrk_retry", "path": "index.html"},
                )

    asyncio.run(scenario())


def test_workspace_continue_rejects_previous_run_without_worker_scope(monkeypatch):
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})
    api_client = RetryableFailureApiClient()

    def get_run_without_worker(run_id: str):
        payload = RetryableFailureApiClient.get_run(api_client, run_id)
        if run_id == "run_retryable_failed":
            payload["worker_id"] = ""
        return payload

    api_client.get_run = get_run_without_worker  # type: ignore[method-assign]
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            with pytest.raises(ToolError, match="previous run to include a worker_id"):
                await client.call_tool(
                    "workspace_continue",
                    {"run_id": "run_retryable_failed", "worker_id": "wrk_retry"},
                )

    asyncio.run(scenario())
    assert api_client.assigned == []


def test_workspace_wait_returns_timeout_without_callback(monkeypatch):
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})
    api_client = PollingApiClient(["running"])
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            waited = await client.call_tool(
                "workspace_wait",
                {
                    "run_id": "run_poll",
                    "timeout_seconds": 0,
                    "poll_interval_seconds": 0.01,
                    "include_live": False,
                },
            )
            payload = _tool_json(waited)
            assert payload["status"] == "timeout"
            assert payload["timed_out"] is True
            assert payload["terminal"] is False
            assert "workspace_status" in payload["next_action_guidance"]

    asyncio.run(scenario())
    assert api_client.get_run_calls == 1


def test_workspace_wait_uses_configured_default_timeout_when_omitted(monkeypatch):
    monkeypatch.setenv("WPR_MCP_BLOCKING_WAIT_DEFAULT_SEC", "0")
    monkeypatch.setenv("WPR_MCP_BLOCKING_WAIT_MAX_SEC", "900")
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})
    api_client = PollingApiClient(["running"])
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            waited = await client.call_tool(
                "workspace_wait",
                {
                    "run_id": "run_poll",
                    "worker_id": "wrk_poll",
                    "poll_interval_seconds": 0.01,
                    "include_live": False,
                },
            )
            payload = _tool_json(waited)
            assert payload["status"] == "timeout"
            assert payload["timed_out"] is True
            assert payload["terminal"] is False

    asyncio.run(scenario())
    assert api_client.get_run_calls == 1


def test_workspace_wait_long_task_timeout_env_is_capped(monkeypatch):
    monkeypatch.setenv("WPR_MCP_BLOCKING_WAIT_DEFAULT_SEC", "2700")
    monkeypatch.setenv("WPR_MCP_BLOCKING_WAIT_MAX_SEC", "3600")
    assert mcp_server._blocking_wait_default_seconds() == 2700
    assert mcp_server._blocking_wait_max_seconds() == 3600

    monkeypatch.setenv("WPR_MCP_BLOCKING_WAIT_DEFAULT_SEC", "5000")
    assert mcp_server._blocking_wait_default_seconds() == 3600


def test_worker_delegate_once_merges_upload_headers(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "true")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    monkeypatch.setenv("WPR_CODEX_BIN", "/bin/echo")
    monkeypatch.setenv("GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON", "{}")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_URL", "http://127.0.0.1:3180/api/viventium/glasshive/callback")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "public-safe-test-secret")
    files = [
        {
            "file_id": "file-999",
            "filename": "brief.txt",
            "text": "Synthetic upload brief.",
            "source": "local",
            "context": "message_attachment",
        }
    ]
    encoded_files = "b64:" + base64.b64encode(json.dumps(files).encode()).decode()
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            **_callback_headers(),
            "X-Viventium-Request-Files": encoded_files,
        },
    )
    api_client = TrackingApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            delegated = await client.call_tool(
                "worker_delegate_once",
                {
                    "owner_id": "demo-owner",
                    "title": "Host File QA",
                    "instruction": "Read the attached brief and summarize it.",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                },
            )
            payload = _tool_json(delegated)
            assert payload["status"] == "dispatched"

    asyncio.run(scenario())
    worker_payload = api_client.find_or_resume_payloads[0]
    bundle = worker_payload["bootstrap_bundle"]
    assert bundle["project_definition"].startswith("# Host File QA")
    assert bundle["files"][0]["path"] == "uploads/brief.txt"
    assert bundle["files"][0]["content"] == "Synthetic upload brief."


def test_worker_delegate_once_materializes_explicit_uploaded_files(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-GlassHive-Tenant-Id": "tenant-alpha",
            "X-GlassHive-User-Id": "user-123",
            "X-WPR-Token": "service-secret",
        },
    )
    api_client = TrackingApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            delegated = await client.call_tool(
                "worker_delegate_once",
                {
                    "owner_id": "ignored-by-test-client",
                    "title": "Upload Roundtrip QA",
                    "instruction": "Read uploads/client_upload.txt and write upload_roundtrip_result.txt.",
                    "profile": "codex-cli",
                    "execution_mode": "docker",
                    "uploaded_files": [
                        {
                            "file_id": "file-explicit-1",
                            "filename": "client_upload.txt",
                            "text": "CLIENT_UPLOAD_SMOKE_20260524\n",
                            "source": "librechat_model_context",
                            "context": "message_attachment",
                        }
                    ],
                },
            )
            payload = _tool_json(delegated)
            assert payload["status"] == "dispatched"

    asyncio.run(scenario())
    bundle = api_client.find_or_resume_payloads[0]["bootstrap_bundle"]
    assert bundle["glasshive_upload_context"]["tool_uploaded_files"][0]["file_id"] == "file-explicit-1"
    assert bundle["viventium_upload_context"]["tool_uploaded_files"][0]["filename"] == "client_upload.txt"
    assert bundle["files"][0]["path"] == "uploads/client_upload.txt"
    assert bundle["files"][0]["content"] == "CLIENT_UPLOAD_SMOKE_20260524\n"
    assert "## Attached workspace files" in bundle["project_definition"]
    assert "`uploads/client_upload.txt`" in bundle["project_definition"]
    assert "Do not ask the user to re-attach" in bundle["project_definition"]


def test_uploaded_file_text_prefers_owner_scoped_binary_when_available(monkeypatch, tmp_path):
    uploads_root = tmp_path / "uploads"
    upload_path = (
        uploads_root
        / "user-123"
        / "f3e753c4-44d9-48b5-8e0b-934b7e5f2c4a__Synthetic_Client_Brief_Source.pdf"
    )
    upload_path.parent.mkdir(parents=True)
    upload_path.write_bytes(b"%PDF-1.7\nsynthetic pdf bytes\n")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(uploads_root))
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(uploads_root))
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-GlassHive-Tenant-Id": "tenant-alpha",
            "X-GlassHive-User-Id": "user-123",
            "X-WPR-Token": "service-secret",
        },
    )
    api_client = TrackingApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            delegated = await client.call_tool(
                "worker_delegate_once",
                {
                    "title": "PDF redaction QA",
                    "instruction": "Redact the attached PDF and return a PDF.",
                    "profile": "codex-cli",
                    "execution_mode": "docker",
                    "uploaded_files": [
                        {
                            "filename": "Synthetic Client Brief Source.pdf",
                            "text": "extracted text is not a substitute for the original PDF",
                        }
                    ],
                },
            )
            assert _tool_json(delegated)["status"] == "dispatched"

    asyncio.run(scenario())
    bundle = api_client.find_or_resume_payloads[0]["bootstrap_bundle"]
    projected = bundle["files"][0]
    assert projected["path"] == "uploads/Synthetic-Client-Brief-Source.pdf"
    assert projected["source_path"] == str(upload_path)
    assert "content" not in projected
    assert projected["source_path_token"] == sign_bootstrap_source_path(
        upload_path,
        tenant_id="tenant-alpha",
        owner_id="user-123",
    )


def test_uploaded_file_text_does_not_cross_owner_boundary(monkeypatch, tmp_path):
    uploads_root = tmp_path / "uploads"
    other_upload = uploads_root / "other-user" / "uuid__same-name.pdf"
    other_upload.parent.mkdir(parents=True)
    other_upload.write_bytes(b"%PDF-1.7\nother user's file\n")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(uploads_root))
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(uploads_root))
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-GlassHive-Tenant-Id": "tenant-alpha",
            "X-GlassHive-User-Id": "user-123",
            "X-WPR-Token": "service-secret",
        },
    )
    api_client = TrackingApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            delegated = await client.call_tool(
                "worker_delegate_once",
                {
                    "title": "Cross-owner upload QA",
                    "instruction": "Use the attached file.",
                    "profile": "codex-cli",
                    "execution_mode": "docker",
                    "uploaded_files": [{"filename": "same name.pdf", "text": "visible model text only"}],
                },
            )
            assert _tool_json(delegated)["status"] == "dispatched"

    asyncio.run(scenario())
    bundle = api_client.find_or_resume_payloads[0]["bootstrap_bundle"]
    projected = bundle["files"][0]
    assert projected["path"] == "uploads/same-name.pdf.metadata.json"
    manifest = json.loads(projected["content"])
    assert manifest["source_status"] == "original_bytes_unavailable"
    assert manifest["extracted_text_available"] is True
    assert "visible model text only" not in projected["content"]
    assert "source_path" not in projected
    assert "substituting extracted text" in bundle["project_definition"]


def test_binary_upload_text_without_source_reports_blocker(monkeypatch, tmp_path):
    uploads_root = tmp_path / "uploads"
    (uploads_root / "user-123").mkdir(parents=True)
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(uploads_root))
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(uploads_root))
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-GlassHive-Tenant-Id": "tenant-alpha",
            "X-GlassHive-User-Id": "user-123",
            "X-WPR-Token": "service-secret",
        },
    )
    api_client = TrackingApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            delegated = await client.call_tool(
                "worker_delegate_once",
                {
                    "title": "Missing PDF bytes QA",
                    "instruction": "Redact the uploaded PDF and return a PDF.",
                    "profile": "codex-cli",
                    "execution_mode": "docker",
                    "uploaded_files": [{"filename": "missing source.pdf", "text": "extracted text only"}],
                },
            )
            assert _tool_json(delegated)["status"] == "dispatched"

    asyncio.run(scenario())
    bundle = api_client.find_or_resume_payloads[0]["bootstrap_bundle"]
    projected = bundle["files"][0]
    assert projected["path"] == "uploads/missing-source.pdf.metadata.json"
    assert "source_path" not in projected
    assert "missing source.pdf.txt" not in json.dumps(bundle)
    manifest = json.loads(projected["content"])
    assert manifest["source_status"] == "original_bytes_unavailable"
    assert manifest["extracted_text_available"] is True
    assert "extracted text only" not in projected["content"]
    assert "metadata/blocker manifests" in bundle["project_definition"]


def test_upload_owner_id_with_path_separators_is_rejected(monkeypatch, tmp_path):
    uploads_root = tmp_path / "uploads"
    escaped_file = uploads_root / "other-user" / "brief.pdf"
    escaped_file.parent.mkdir(parents=True)
    escaped_file.write_bytes(b"%PDF-1.7\nother user's file\n")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(uploads_root))
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(uploads_root))

    entry = mcp_server._project_upload_file_entry(
        {"filename": "brief.pdf", "text": "visible model text only"},
        1,
        tenant_id="tenant-alpha",
        owner_id="../other-user",
    )

    assert entry is not None
    assert entry["path"] == "uploads/brief.pdf.metadata.json"
    assert "source_path" not in entry


def test_worker_tools_use_configured_default_execution_mode(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            worker = await client.call_tool(
                "worker_find_or_resume",
                {
                    "project_id": "prj_new",
                    "owner_id": "demo-owner",
                    "name": "Codex Host",
                    "role": "coding",
                    "alias": "codex-main",
                    "profile": "codex-cli",
                },
            )
            worker_payload = _tool_json(worker)
            assert worker_payload["execution_mode"] == "host"

    asyncio.run(scenario())


def test_worker_create_accepts_structured_bootstrap_bundle_files_mapping(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            worker = await client.call_tool(
                "worker_create",
                {
                    "project_id": "prj_new",
                    "owner_id": "demo-owner",
                    "name": "Host Browser QA",
                    "role": "Open a host browser and report the page title.",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                    "bootstrap_bundle_json": {
                        "files": {
                            "project-definition.md": "# Host Browser QA\n\nOpen a local page and report the title.\n",
                            "notes/context.md": "Synthetic QA context.\n",
                        }
                    },
                },
            )
            worker_payload = _tool_json(worker)
            bundle = worker_payload["bootstrap_bundle"]
            assert bundle["project_definition"] == "# Host Browser QA\n\nOpen a local page and report the title.\n"
            assert bundle["files"] == [
                {
                    "scope": "workspace",
                    "path": "project-definition.md",
                    "content": "# Host Browser QA\n\nOpen a local page and report the title.\n",
                },
                {
                    "scope": "workspace",
                    "path": "notes/context.md",
                    "content": "Synthetic QA context.\n",
                },
            ]

    asyncio.run(scenario())


def test_worker_create_merges_structured_bootstrap_bundle_with_upload_headers(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    files = [
        {
            "file_id": "file-789",
            "filename": "brief.txt",
            "text": "Synthetic upload brief.",
            "source": "local",
            "context": "message_attachment",
        }
    ]
    encoded_files = "b64:" + base64.b64encode(json.dumps(files).encode()).decode()
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-Request-Files": encoded_files,
        },
    )
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            worker = await client.call_tool(
                "worker_create",
                {
                    "project_id": "prj_new",
                    "owner_id": "demo-owner",
                    "name": "Host File QA",
                    "role": "Read the uploaded brief on the host worker.",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                    "bootstrap_bundle_json": {
                        "files": {
                            "project-definition.md": "# Host File QA\n\nRead the attached brief.\n",
                        }
                    },
                },
            )
            worker_payload = _tool_json(worker)
            bundle = worker_payload["bootstrap_bundle"]
            assert bundle["project_definition"].startswith("# Host File QA\n\nRead the attached brief.\n")
            assert "## Attached workspace files" in bundle["project_definition"]
            assert "`uploads/brief.txt`" in bundle["project_definition"]
            paths = [item["path"] for item in bundle["files"]]
            assert paths == ["project-definition.md", "uploads/brief.txt"]
            assert bundle["files"][1]["content"] == "Synthetic upload brief."

    asyncio.run(scenario())


def test_worker_find_or_resume_keeps_json_string_bootstrap_bundle(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            worker = await client.call_tool(
                "worker_find_or_resume",
                {
                    "project_id": "prj_new",
                    "owner_id": "demo-owner",
                    "name": "Host Browser QA",
                    "role": "Open a host browser and report the page title.",
                    "alias": "host-browser-qa",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                    "bootstrap_bundle_json": json.dumps(
                        {
                            "project_definition": "Open a local page and report the title.",
                            "files": [{"scope": "workspace", "path": "notes/context.md", "content": "QA context.\n"}],
                        }
                    ),
                },
            )
            worker_payload = _tool_json(worker)
            bundle = worker_payload["bootstrap_bundle"]
            assert bundle["project_definition"] == "Open a local page and report the title."
            assert bundle["files"][0]["path"] == "notes/context.md"

    asyncio.run(scenario())


def test_worker_find_or_resume_accepts_structured_bootstrap_bundle(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            worker = await client.call_tool(
                "worker_find_or_resume",
                {
                    "project_id": "prj_new",
                    "owner_id": "demo-owner",
                    "name": "Host Browser QA",
                    "role": "Open a host browser and report the page title.",
                    "alias": "host-browser-qa",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                    "bootstrap_bundle_json": {
                        "files": {
                            "project-definition.md": "# Host Browser QA\n\nReport the page title.\n",
                        }
                    },
                },
            )
            worker_payload = _tool_json(worker)
            bundle = worker_payload["bootstrap_bundle"]
            assert bundle["project_definition"] == "# Host Browser QA\n\nReport the page title.\n"
            assert bundle["files"][0]["path"] == "project-definition.md"

    asyncio.run(scenario())


def test_normalize_bootstrap_bundle_ignores_malformed_files_value():
    bundle = mcp_server._normalize_bootstrap_bundle({"project_definition": "Do the work.", "files": "oops"})

    assert bundle == {"project_definition": "Do the work.", "files": []}


def test_worker_tool_schemas_advertise_host_native_execution(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            tools = {tool.name: tool.model_dump() for tool in await client.list_tools()}
            worker_create = tools["worker_create"]
            worker_resume = tools["worker_find_or_resume"]
            desktop_action = tools["worker_desktop_action"]
            worker_delegate = tools["worker_delegate_once"]
            workspace_launch = tools["workspace_launch"]

            for tool in (worker_create, worker_resume):
                description = tool["description"]
                assert "execution_mode='host'" in description
                schema = tool["inputSchema"]["properties"]
                execution_schema = schema["execution_mode"]
                assert execution_schema["anyOf"][0]["enum"] == ["docker", "host"]
                assert "real computer/session" in execution_schema["description"]
                assert "codex-cli" in schema["profile"]["description"]
                bootstrap_schema = schema["bootstrap_bundle_json"]
                bootstrap_types = {
                    variant.get("type")
                    for variant in bootstrap_schema.get("anyOf", [])
                    if isinstance(variant, dict)
                }
                assert {"string", "object", "null"}.issubset(bootstrap_types)

            action_schema = desktop_action["inputSchema"]["properties"]["action"]
            assert action_schema["enum"] == [
                "terminal",
                "files",
                "browser",
                "focus_browser",
                "codex",
                "claude",
                "openclaw",
            ]
            for tool in (worker_delegate, workspace_launch):
                upload_schema = tool["inputSchema"]["properties"]["uploaded_files"]
                upload_description = upload_schema["description"].lower()
                assert "attached/uploaded files" in upload_description
                assert "chat host does not project upload metadata" in upload_description

    asyncio.run(scenario())


def test_workspace_launch_uses_documented_ui_fields_without_low_level_chain(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    api = TrackingApiClient()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "workspace_launch",
                {
                    "description": "Build a small evidence file from the uploaded report.",
                    "success_criteria": (
                        "The workspace creates summary.txt and reports where it is. "
                        "The host chat verifies View / Steer link visibility and wait/status polling cadence."
                    ),
                    "context": "Use the attached file and keep the workspace resumable.",
                    "profile": "codex-cli",
                    "require_callback": False,
                },
            )
            payload = _tool_json(result)
            assert payload["status"] == "dispatched"
            minimal = await client.call_tool(
                "workspace_launch",
                {
                    "description": "Summarize the provided public-safe note.",
                    "context": "No distinct acceptance criteria were supplied.",
                    "require_callback": False,
                },
            )
            assert _tool_json(minimal)["status"] == "dispatched"

    asyncio.run(scenario())

    assert api.calls == [
        "create_project",
        "find_or_resume_worker",
        "assign_run",
        "create_project",
        "find_or_resume_worker",
        "assign_run",
    ]
    assert api.find_or_resume_payloads[0]["profile"] == "codex-cli"
    explicit_instruction = api.assign_run_payloads[0]["instruction"]
    assert "Explicit success criteria:" in explicit_instruction
    assert "Treat explicit success criteria as hard acceptance gates." in explicit_instruction
    assert "The host chat verifies View / Steer link visibility" in explicit_instruction
    assert "host-side responsibilities" in explicit_instruction
    assert "do not mark the workspace blocked" in explicit_instruction
    assert "report only blockers observable from inside this worker workspace" in explicit_instruction
    minimal_instruction = api.assign_run_payloads[1]["instruction"]
    assert "Default completion check:" in minimal_instruction
    assert "Satisfy the user's request as stated, preserving explicit constraints." in minimal_instruction
    assert "Treat explicit success criteria as hard acceptance gates." not in minimal_instruction
    assert "do not invent extra gates" in minimal_instruction


def test_workspace_launch_returns_structured_quota_block_with_reuse_options(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")

    class QuotaBlockedApi(TrackingApiClient):
        def find_or_resume_worker(self, **kwargs):
            self.calls.append("find_or_resume_worker")
            self.find_or_resume_payloads.append(kwargs)
            raise mcp_server.GlassHiveBlockedError(
                {
                    "status": "blocked",
                    "failure_class": "glasshive_worker_quota_exceeded",
                    "failure_retryable": 1,
                    "failure_user_message": "GlassHive did not start a new workspace because capacity is full.",
                    "failure_recommended_recovery": "Use one of `available_workspace_options` if it fits, or wait.",
                    "failure_diagnostic_summary": "GLASSHIVE_MAX_ACTIVE_WORKERS_PER_USER=3",
                    "available_workspace_options": [
                        {
                            "project_id": "prj_existing",
                            "worker_id": "wrk_existing",
                            "project_title": "Existing research workspace",
                            "workspace_name": "Research",
                            "alias": "research",
                            "state": "ready",
                            "profile": "codex-cli",
                            "execution_mode": "docker",
                        }
                    ],
                    "acknowledgement_guidance": (
                        "Explain that GlassHive capacity is full. Do not claim a workspace is running."
                    ),
                    "main_agent_next_action": (
                        "Review `available_workspace_options` and pick/reuse one that matches the user's task. "
                        "Do not suggest switching profile or sandbox mode as the fix for this quota."
                    ),
                }
            )

    api = QuotaBlockedApi()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "workspace_launch",
                {
                    "description": "Do a research pass.",
                    "profile": "codex-cli",
                    "require_callback": False,
                },
            )
            payload = _tool_json(result)
            assert payload["status"] == "blocked"
            assert payload["failure_class"] == "glasshive_worker_quota_exceeded"
            assert payload["available_workspace_options"][0]["worker_id"] == "wrk_existing"
            assert "pick/reuse" in payload["main_agent_next_action"]
            assert "Do not suggest switching profile or sandbox mode" in payload["main_agent_next_action"]
            assert payload["view_steer_url"] is None

    asyncio.run(scenario())
    assert api.calls == ["create_project", "find_or_resume_worker"]
    assert api.assign_run_payloads == []


def test_workspace_launch_connected_account_intent_warns_without_host_broker(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    api = TrackingApiClient()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "workspace_launch",
                {
                    "description": "Check connected-account content for a public-safe marker.",
                    "success_criteria": "Report only what the available tools can prove.",
                    "context": "Use connected-account MCP/tools if the host provided them.",
                    "profile": "codex-cli",
                    "connected_account_content_intent": True,
                },
            )
            payload = _tool_json(result)
            assert payload["status"] == "dispatched"

    asyncio.run(scenario())

    assigned_instruction = api.assign_run_payloads[-1]["instruction"]
    bootstrap_bundle = api.find_or_resume_payloads[-1]["bootstrap_bundle"]
    assert "did not receive a complete host-signed `glasshive-user-capabilities` broker grant/config" in assigned_instruction
    assert "Do not claim brokered MCP access" in assigned_instruction
    assert "system_instructions" in bootstrap_bundle
    assert "Do not claim brokered MCP access" in bootstrap_bundle["system_instructions"]


def test_worker_delegate_once_connected_account_intent_accepts_complete_broker_bundle(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    api = TrackingApiClient()
    server = create_mcp_server(api_client=api)
    broker_bundle = {
        "glasshive_capability_broker": {
            "version": 1,
            "name": "glasshive-user-capabilities",
            "url": "http://127.0.0.1:3180/api/viventium/glasshive/capabilities/mcp",
            "grant_expires_at": 9_999_999_999,
            "allowed_servers": ["google_workspace", "ms-365"],
            "scopes": {"content_read": True},
        },
        "glasshive_capability_intent": {"content_read": True},
        "codex_config_append": (
            "[mcp_servers.glasshive-user-capabilities]\n"
            'url = "http://127.0.0.1:3180/api/viventium/glasshive/capabilities/mcp"\n'
            'bearer_token_env_var = "GLASSHIVE_CAPABILITY_BROKER_TOKEN"'
        ),
        "env": {"GLASSHIVE_CAPABILITY_BROKER_TOKEN": "public-safe-test-grant"},
    }

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "worker_delegate_once",
                {
                    "title": "Brokered connected-account check",
                    "instruction": "Use the brokered connected-account tools to check the public-safe marker.",
                    "goal": "Report proven results only.",
                    "profile": "codex-cli",
                    "connected_account_content_intent": True,
                    "bootstrap_bundle_json": broker_bundle,
                },
            )
            payload = _tool_json(result)
            assert payload["status"] == "dispatched"

    asyncio.run(scenario())

    assigned_instruction = api.assign_run_payloads[-1]["instruction"]
    bootstrap_bundle = api.find_or_resume_payloads[-1]["bootstrap_bundle"]
    assert "Do not claim brokered MCP access" not in assigned_instruction
    assert "system_instructions" not in bootstrap_bundle
    assert bootstrap_bundle["glasshive_capability_broker"]["allowed_servers"] == ["google_workspace", "ms-365"]
    assert bootstrap_bundle["env"]["GLASSHIVE_CAPABILITY_BROKER_TOKEN"] == "public-safe-test-grant"


def test_complete_broker_bundle_requires_supported_version_and_unexpired_grant():
    broker_bundle = {
        "glasshive_capability_broker": {
            "version": 1,
            "name": "glasshive-user-capabilities",
            "url": "http://127.0.0.1:3180/api/viventium/glasshive/capabilities/mcp",
            "grant_expires_at": 9_999_999_999,
            "scopes": {"content_read": True},
        },
        "codex_config_append": (
            "[mcp_servers.glasshive-user-capabilities]\n"
            'url = "http://127.0.0.1:3180/api/viventium/glasshive/capabilities/mcp"\n'
            'bearer_token_env_var = "GLASSHIVE_CAPABILITY_BROKER_TOKEN"'
        ),
        "env": {"GLASSHIVE_CAPABILITY_BROKER_TOKEN": "public-safe-test-grant"},
    }

    assert mcp_server._has_complete_capability_broker_bundle(broker_bundle) is True
    assert mcp_server._has_complete_capability_broker_bundle(
        {**broker_bundle, "glasshive_capability_broker": {**broker_bundle["glasshive_capability_broker"], "version": 2}}
    ) is False
    assert mcp_server._has_complete_capability_broker_bundle(
        {
            **broker_bundle,
            "glasshive_capability_broker": {
                **broker_bundle["glasshive_capability_broker"],
                "grant_expires_at": 1,
            },
        }
    ) is False


def test_worker_delegate_once_preserves_explicit_goal_in_run_instruction(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    api = TrackingApiClient()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "worker_delegate_once",
                {
                    "title": "Exact data in out QA",
                    "instruction": "Create a file named goal_preservation_qa.txt.",
                    "goal": "The file must state whether the explicit goal was visible in the active run instruction.",
                    "profile": "codex-cli",
                },
            )
            payload = _tool_json(result)
            assert payload["status"] == "dispatched"

    asyncio.run(scenario())

    assigned_instruction = api.assign_run_payloads[-1]["instruction"]
    assert "User-visible success condition:" in assigned_instruction
    assert "explicit goal was visible" in assigned_instruction


def test_worker_delegate_once_connected_account_intent_warns_without_content_read_scope(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    api = TrackingApiClient()
    server = create_mcp_server(api_client=api)
    broker_bundle = {
        "glasshive_capability_broker": {
            "version": 1,
            "name": "glasshive-user-capabilities",
            "url": "http://127.0.0.1:3180/api/viventium/glasshive/capabilities/mcp",
            "grant_expires_at": 9_999_999_999,
            "allowed_servers": ["google_workspace"],
            "scopes": {"content_read": False},
        },
        "glasshive_capability_intent": {"content_read": False},
        "codex_config_append": (
            "[mcp_servers.glasshive-user-capabilities]\n"
            'url = "http://127.0.0.1:3180/api/viventium/glasshive/capabilities/mcp"\n'
            'bearer_token_env_var = "GLASSHIVE_CAPABILITY_BROKER_TOKEN"'
        ),
        "env": {"GLASSHIVE_CAPABILITY_BROKER_TOKEN": "public-safe-test-grant"},
    }

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "worker_delegate_once",
                {
                    "title": "Brokered connected-account check",
                    "instruction": "Use brokered connected-account tools only if content-read scope is real.",
                    "goal": "Report proven results only.",
                    "profile": "codex-cli",
                    "connected_account_content_intent": True,
                    "bootstrap_bundle_json": broker_bundle,
                },
            )
            payload = _tool_json(result)
            assert payload["status"] == "dispatched"

    asyncio.run(scenario())

    assigned_instruction = api.assign_run_payloads[-1]["instruction"]
    bootstrap_bundle = api.find_or_resume_payloads[-1]["bootstrap_bundle"]
    assert "Do not claim brokered MCP access" in assigned_instruction
    assert "Do not claim brokered MCP access" in bootstrap_bundle["system_instructions"]


def test_worker_run_connected_account_intent_warns_without_host_broker(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    api = TrackingApiClient()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "worker_run",
                {
                    "worker_id": "wrk_123",
                    "instruction": "Check connected-account content for a public-safe marker.",
                    "connected_account_content_intent": True,
                },
            )
            payload = _tool_json(result)
            assert payload["state"] == "queued"

    asyncio.run(scenario())

    assigned = api.assign_run_payloads[-1]
    assert "Do not claim brokered MCP access" in assigned["instruction"]
    assert "Do not claim brokered MCP access" in assigned["bootstrap_bundle"]["system_instructions"]


def test_workspace_schedule_connected_account_intent_warns_without_host_broker(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    api = TrackingApiClient()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "workspace_schedule",
                {
                    "description": "Check connected-account content later.",
                    "success_criteria": "Report proven results only.",
                    "schedule_text": "in 5 minutes",
                    "connected_account_content_intent": True,
                },
            )
            payload = _tool_json(result)
            assert payload["status"] == "scheduled"

    asyncio.run(scenario())

    assert "Do not claim brokered MCP access" in api.find_or_resume_payloads[-1]["bootstrap_bundle"]["system_instructions"]
    assert "Do not claim brokered MCP access" in api.schedule_run_payloads[-1]["instruction"]


def test_worker_create_connected_account_intent_warns_without_host_broker(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    api = TrackingApiClient()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "worker_create",
                {
                    "project_id": "prj_123",
                    "name": "Connected account worker",
                    "role": "Report proven connected-account results only.",
                    "connected_account_content_intent": True,
                },
            )
            payload = _tool_json(result)
            assert payload["worker_id"] == "wrk_new"

    asyncio.run(scenario())

    bundle = api.create_worker_payloads[-1]["bootstrap_bundle"]
    assert "Do not claim brokered MCP access" in bundle["system_instructions"]


def test_worker_find_or_resume_connected_account_intent_warns_without_host_broker(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    api = TrackingApiClient()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "worker_find_or_resume",
                {
                    "project_id": "prj_123",
                    "name": "Connected account worker",
                    "role": "Report proven connected-account results only.",
                    "alias": "connected-account-worker",
                    "connected_account_content_intent": True,
                },
            )
            payload = _tool_json(result)
            assert payload["worker_id"] == "wrk_resumed"

    asyncio.run(scenario())

    bundle = api.find_or_resume_payloads[-1]["bootstrap_bundle"]
    assert "Do not claim brokered MCP access" in bundle["system_instructions"]


def test_worker_schedule_connected_account_intent_warns_without_host_broker(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    api = TrackingApiClient()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "worker_schedule",
                {
                    "worker_id": "wrk_123",
                    "instruction": "Check connected-account content later.",
                    "schedule_text": "in 5 minutes",
                    "connected_account_content_intent": True,
                },
            )
            payload = _tool_json(result)
            assert payload["state"] == "pending"

    asyncio.run(scenario())

    scheduled = api.schedule_run_payloads[-1]
    assert "Do not claim brokered MCP access" in scheduled["instruction"]
    assert "Do not claim brokered MCP access" in scheduled["bootstrap_bundle"]["system_instructions"]


def test_audit_preview_redacts_json_quoted_tokens():
    long_base64 = "A" * 520
    preview = mcp_server._audit_preview(
        json.dumps(
            {
                "GLASSHIVE_CAPABILITY_BROKER_TOKEN": "public-safe-token-value-123456",
                "auth": "abcd",
                "credential": "creds",
                "session_token": "short",
                "signature": "very-private-signature-value",
                "blob": long_base64,
                "pair": "credentialid:abcdefghijklmnopqrstuvwxyz123456",
            }
        )
    )

    assert "public-safe-token-value" not in preview
    assert "abcd" not in preview
    assert "creds" not in preview
    assert "short" not in preview
    assert "very-private-signature-value" not in preview
    assert long_base64 not in preview
    assert "abcdefghijklmnopqrstuvwxyz123456" not in preview
    assert "[REDACTED]" in preview


def test_workspace_continue_rechecks_stale_connected_account_guard_with_fresh_broker(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")

    class PreviousGuardApi(TrackingApiClient):
        def get_run(self, run_id: str):
            return {
                "run_id": run_id,
                "worker_id": "wrk_123",
                "project_id": "prj_123",
                "state": "failed",
                "instruction": "Check connected-account content.\n\n" + mcp_server.CONNECTED_ACCOUNT_NO_BROKER_NOTE,
            }

    api = PreviousGuardApi()
    server = create_mcp_server(api_client=api)
    broker_bundle = {
        "glasshive_capability_broker": {
            "version": 1,
            "name": "glasshive-user-capabilities",
            "url": "http://127.0.0.1:3180/api/viventium/glasshive/capabilities/mcp",
            "grant_expires_at": 9_999_999_999,
            "allowed_servers": ["google_workspace"],
            "scopes": {"content_read": True},
        },
        "glasshive_capability_intent": {"content_read": True},
        "codex_config_append": (
            "[mcp_servers.glasshive-user-capabilities]\n"
            'url = "http://127.0.0.1:3180/api/viventium/glasshive/capabilities/mcp"\n'
            'bearer_token_env_var = "GLASSHIVE_CAPABILITY_BROKER_TOKEN"'
        ),
        "env": {"GLASSHIVE_CAPABILITY_BROKER_TOKEN": "public-safe-test-grant"},
    }

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "workspace_continue",
                {
                    "run_id": "run_failed",
                    "connected_account_content_intent": True,
                    "bootstrap_bundle_json": broker_bundle,
                },
            )
            payload = _tool_json(result)
            assert payload["status"] == "queued"

    asyncio.run(scenario())

    assigned = api.assign_run_payloads[-1]
    assert "Do not claim brokered MCP access" not in assigned["instruction"]
    assert assigned["bootstrap_bundle"]["glasshive_capability_broker"]["scopes"]["content_read"] is True


def test_workspace_launch_reuses_existing_workspace_alias_across_projects(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")

    class ExistingAliasApi(TrackingApiClient):
        def list_projects(self, owner_id: str | None = None):
            self.calls.append("list_projects")
            return [{"project_id": "prj_existing", "owner_id": "demo-owner", "title": "Marketing sandbox"}]

        def list_workers(self, project_id: str):
            self.calls.append("list_workers")
            return [
                {
                    "worker_id": "wrk_existing",
                    "project_id": project_id,
                    "owner_id": "demo-owner",
                    "name": "JohnDoe",
                    "role": "Marketing",
                    "profile": "codex-cli",
                    "backend": "openclaw",
                    "execution_mode": "docker",
                    "alias": "marketing-sandbox",
                    "state": "paused",
                }
            ]

    api = ExistingAliasApi()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "workspace_launch",
                {
                    "description": "Use my Marketing sandbox to update the campaign note.",
                    "success_criteria": "The existing named workspace is reused.",
                    "workspace_alias": "marketing-sandbox",
                    "profile": "codex-cli",
                    "require_callback": False,
                },
            )
            payload = _tool_json(result)
            assert payload["status"] == "dispatched"

    asyncio.run(scenario())

    assert "create_project" not in api.calls
    assert api.find_or_resume_payloads[-1]["project_id"] == "prj_existing"
    assert api.find_or_resume_payloads[-1]["alias"] == "marketing-sandbox"


def test_workspace_launch_reuses_enterprise_scoped_workspace_alias(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-token")
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-WPR-Token": "service-token",
            "X-Viventium-Tenant-Id": "tenant-alpha",
            "X-Viventium-User-Id": "user-a",
            "X-Viventium-User-Role": "member",
        },
    )

    class ExistingEnterpriseAliasApi(TrackingApiClient):
        def list_projects(self, owner_id: str | None = None):
            self.calls.append("list_projects")
            return [{"project_id": "prj_existing", "owner_id": "user-a", "title": "Marketing sandbox"}]

        def list_workers(self, project_id: str):
            self.calls.append("list_workers")
            return [
                {
                    "worker_id": "wrk_existing",
                    "project_id": project_id,
                    "owner_id": "user-a",
                    "name": "JohnDoe",
                    "role": "Marketing",
                    "profile": "codex-cli",
                    "backend": "openclaw",
                    "execution_mode": "docker",
                    "alias": "tenant-alpha--user-a--marketing-sandbox",
                    "state": "paused",
                }
            ]

    api = ExistingEnterpriseAliasApi()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "workspace_launch",
                {
                    "description": "Tell JohnDoe to use my Marketing sandbox for the next task.",
                    "success_criteria": "The enterprise-scoped workspace alias is reused.",
                    "workspace_alias": "marketing-sandbox",
                    "profile": "codex-cli",
                    "require_callback": False,
                },
            )
            payload = _tool_json(result)
            assert payload["status"] == "dispatched"

    asyncio.run(scenario())

    assert "create_project" not in api.calls
    assert api.find_or_resume_payloads[-1]["project_id"] == "prj_existing"
    assert api.find_or_resume_payloads[-1]["alias"] == "marketing-sandbox"


def test_tool_descriptions_advertise_mcp_owned_usage_contract(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    server = create_mcp_server(api_client=FakeApiClient())
    instructions = mcp_server.glasshive_workers_server_instructions()
    assert "exact GlassHive tool id exposed by the host application" in instructions
    assert "workspace_launch_mcp_glasshive-workers-projects" in instructions
    assert "not in the available tool list" in instructions
    assert "Preserve host-side GlassHive orchestration requirements as context" in instructions
    assert "not by the worker running inside the workspace" in instructions
    assert "MCP/tools are preferred when they can satisfy the task" in instructions
    assert "Do not make tool choice a workspace success criterion" in instructions
    assert "Do not invent project goals, success criteria" in instructions
    assert "memory-derived priorities" in instructions
    assert "For vague user adjectives like urgent or important, pass the adjective through" in instructions
    assert "trust the GlassHive worker to find the best path" in instructions
    assert "Satisfy the user's request as stated, preserving explicit constraints" in instructions
    assert "success_criteria as broker/tool evidence gates" not in instructions
    assert "preferred scoped option" in instructions
    assert "non-broker host connectors are fallback after" in instructions
    assert "Connected-account read authorization comes from the host-signed broker grant" in instructions
    assert "compatibility hint for hosts that want an extra missing-broker warning" in instructions
    assert "not a required authorization switch" in instructions

    public_action_tools = {
        "projects_list",
        "project_create",
        "project_get",
        "workspace_launch",
        "worker_delegate_once",
        "project_runs",
        "project_events",
        "workers_list",
        "worker_create",
        "worker_find_or_resume",
        "worker_get",
        "worker_live",
        "worker_run",
        "worker_message",
        "worker_pause",
        "worker_resume",
        "worker_interrupt",
        "worker_terminate",
        "worker_desktop_action",
        "worker_takeover",
        "run_get",
        "workspace_status",
        "workspace_wait",
        "workspace_continue",
        "workspace_artifacts",
        "workspace_artifact_download",
        "metrics_summary",
    }

    async def scenario():
        async with Client(server) as client:
            tools = {tool.name: tool.model_dump() for tool in await client.list_tools()}
            assert public_action_tools.issubset(set(tools))

            for tool_name in public_action_tools:
                description = tools[tool_name]["description"]
                normalized = description.lower()
                assert "use" in normalized, tool_name
                assert "return" in normalized, tool_name
                assert any(marker in normalized for marker in ("do not", "prefer", "only when", "instead")), tool_name
                assert len(description.split()) >= 20, tool_name

            delegate_description = tools["worker_delegate_once"]["description"]
            workspace_description = tools["workspace_launch"]["description"]
            assert "description" in workspace_description
            assert "success_criteria" in workspace_description
            assert "optional context" in workspace_description
            assert "optional success_criteria" in workspace_description
            assert "Do not chain project_create" in workspace_description
            assert "uploaded-file requests" in workspace_description
            assert "Do not shorten" in workspace_description
            assert "full available background" in workspace_description
            assert "View / Steer link" in workspace_description
            assert "workspace-internal deliverable blockers" in workspace_description
            assert "do not turn tool choice into a success criterion" in workspace_description
            assert "Satisfy the user's request as stated, preserving explicit constraints" in workspace_description
            assert "Do not invent provider lists" in workspace_description
            assert "memory-derived priorities" in workspace_description
            assert "For vague user adjectives like urgent or important" in workspace_description
            assert "must not fabricate MCP/tool results" in workspace_description
            assert "force a downloadable artifact" in workspace_description
            assert "deep research" in workspace_description
            assert "critical analysis" in workspace_description
            assert "high/xhigh" in workspace_description
            assert "Claude max" in workspace_description
            assert "Use medium only for ordinary bounded tasks" in workspace_description
            assert "first show the View / Steer link" in workspace_description

            assert "callbacks are optional" in delegate_description.lower()
            assert "uploaded-file tasks" in delegate_description
            assert "workspace_status" in delegate_description
            assert "workspace_wait" in delegate_description
            assert "write your own short acknowledgement" in delegate_description.lower()
            assert "sandbox" in delegate_description.lower()
            assert "blocked" in delegate_description.lower()
            assert "delegation_audit" in delegate_description
            assert "View / Steer link" in delegate_description
            assert "follow_up_context" in delegate_description
            assert "worker_id/run_id" in delegate_description
            assert "Do not shorten" in delegate_description
            assert "full available brief" in delegate_description
            assert "workspace-internal deliverable blockers" in delegate_description
            assert "Pass MCP/tool availability as context" in delegate_description
            assert "critical analysis" in delegate_description
            assert "high/xhigh" in delegate_description

            for tool_name in (
                "workspace_launch",
                "worker_delegate_once",
                "workspace_schedule",
                "worker_create",
                "worker_find_or_resume",
                "worker_run",
                "worker_schedule",
            ):
                schema = tools[tool_name]["inputSchema"]["properties"]
                assert "connected_account_content_intent" in schema
                intent_description = schema["connected_account_content_intent"]["description"]
                assert "connected-account content" in intent_description
                assert "host-signed broker grant" in intent_description
                assert "does not unlock reads or writes" in intent_description

            desktop_description = tools["worker_desktop_action"]["description"]
            assert "raw desktop URLs are diagnostic" in desktop_description

            wait_description = tools["workspace_wait"]["description"]
            assert "first surface the View / Steer link" in wait_description
            assert "Omit poll_interval_seconds for normal work" in wait_description
            assert "backs off toward the configured cap" in wait_description

            launch_success_description = tools["workspace_launch"]["inputSchema"]["properties"][
                "success_criteria"
            ]["description"]
            schedule_description = tools["workspace_schedule"]["description"]
            schedule_success_description = tools["workspace_schedule"]["inputSchema"]["properties"][
                "success_criteria"
            ]["description"]
            launch_context_description = tools["workspace_launch"]["inputSchema"]["properties"][
                "context"
            ]["description"]
            launch_effort_description = tools["workspace_launch"]["inputSchema"]["properties"]["effort"][
                "description"
            ]
            assert "Use explicit user requirements only" in launch_success_description
            assert "Optional workspace-internal acceptance criteria" in launch_success_description
            assert "broker/tool availability" in launch_success_description
            assert "memory-derived priorities" in launch_context_description
            assert "For vague user adjectives like urgent or important" in launch_context_description
            assert "deep research" in launch_effort_description
            assert "critical analysis" in launch_effort_description
            assert "Do not invent schedule success criteria" in schedule_description
            assert "Trust the scheduled GlassHive worker" in schedule_description
            assert "For vague user adjectives like urgent or important" in schedule_description
            assert "Use explicit user requirements only" in schedule_success_description
            assert "Optional acceptance criteria" in schedule_success_description
            assert "Satisfy the user's request as stated, preserving explicit constraints" in (
                launch_success_description + schedule_success_description
            )
            assert "success_criteria" not in tools["workspace_launch"]["inputSchema"].get("required", [])
            assert "success_criteria" not in tools["workspace_schedule"]["inputSchema"].get("required", [])

    asyncio.run(scenario())


def test_host_worker_disabled_forces_docker_default_and_rejects_host(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "false")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            worker = await client.call_tool(
                "worker_find_or_resume",
                {
                    "project_id": "prj_new",
                    "owner_id": "demo-owner",
                    "name": "Docker Worker",
                    "role": "coding",
                    "alias": "docker-main",
                    "profile": "codex-cli",
                },
            )
            worker_payload = _tool_json(worker)
            assert worker_payload["execution_mode"] == "docker"

            with pytest.raises(ToolError, match="host-native GlassHive workers are disabled"):
                await client.call_tool(
                    "worker_find_or_resume",
                    {
                        "project_id": "prj_new",
                        "owner_id": "demo-owner",
                        "name": "Codex Host",
                        "role": "coding",
                        "alias": "codex-main",
                        "profile": "codex-cli",
                        "execution_mode": "host",
                    },
                )

    asyncio.run(scenario())


def test_worker_tools_default_owner_from_request_headers(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-User-Id": "user-from-request",
        },
    )
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            project = await client.call_tool(
                "project_create",
                {
                    "title": "Host Browser QA",
                    "goal": "Open a host browser.",
                    "default_worker_profile": "codex-cli",
                },
            )
            project_payload = _tool_json(project)
            assert project_payload["owner_id"] == "user-from-request"

            worker = await client.call_tool(
                "worker_find_or_resume",
                {
                    "project_id": "prj_new",
                    "name": "Codex Host",
                    "role": "host browser control",
                    "alias": "codex-main",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                },
            )
            worker_payload = _tool_json(worker)
            assert worker_payload["owner_id"] == "user-from-request"
            assert worker_payload["execution_mode"] == "host"

    asyncio.run(scenario())


def test_merge_request_context_adds_callback_metadata(monkeypatch):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_URL", "http://localhost:3080/api/viventium/glasshive/callback")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "callback-secret")
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-User-Id": "user-123",
            "X-Viventium-Agent-Id": "agent-main",
            "X-Viventium-Conversation-Id": "conv-123",
            "X-Viventium-Parent-Message-Id": "msg-parent",
            "X-Viventium-Message-Id": "msg-current",
            "X-Viventium-Surface": "telegram",
            "X-Viventium-Input-Mode": "voice_note",
            "X-Viventium-Stream-Id": "stream-123",
            "X-Viventium-Voice-Call-Session-Id": "call-123",
            "X-Viventium-Voice-Request-Id": "voice-req-123",
            "X-Viventium-Telegram-Chat-Id": "chat-123",
            "X-Viventium-Telegram-User-Id": "tg-user-123",
            "X-Viventium-Telegram-Message-Id": "tg-msg-123",
        },
    )

    bundle = mcp_server._merge_request_context({"project_definition": "Do the work"})

    assert bundle is not None
    callbacks = bundle["callbacks"]
    assert callbacks["events_webhook_url"].endswith("/api/viventium/glasshive/callback")
    assert callbacks["hmac_secret"] == "callback-secret"
    assert callbacks["conversation_id"] == "conv-123"
    assert callbacks["parent_message_id"] == "msg-parent"
    assert callbacks["surface"] == "telegram"
    assert callbacks["stream_id"] == "stream-123"
    assert callbacks["voice_call_session_id"] == "call-123"
    assert callbacks["telegram_chat_id"] == "chat-123"
    assert bundle["viventium_context"]["user_id"] == "user-123"


def test_merge_request_context_loads_callback_metadata_from_runtime_env(tmp_path, monkeypatch):
    runtime_env = tmp_path / "runtime.env"
    runtime_env.write_text(
        "\n".join(
            [
                "VIVENTIUM_GLASSHIVE_CALLBACK_URL=http://localhost:3080/api/viventium/glasshive/callback",
                "VIVENTIUM_GLASSHIVE_CALLBACK_SECRET=runtime-secret",
            ]
        )
        + "\n"
    )
    monkeypatch.setenv("VIVENTIUM_ENV_FILE", str(runtime_env))
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_CALLBACK_URL", raising=False)
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", raising=False)
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-User-Id": "user-123",
            "X-Viventium-Conversation-Id": "conv-123",
            "X-Viventium-Parent-Message-Id": "msg-parent",
            "X-Viventium-Message-Id": "msg-current",
        },
    )

    bundle = mcp_server._merge_request_context({"project_definition": "Do the work"})

    assert bundle is not None
    callbacks = bundle["callbacks"]
    assert callbacks["events_webhook_url"].endswith("/api/viventium/glasshive/callback")
    assert callbacks["hmac_secret"] == "runtime-secret"


def test_merge_request_context_does_not_auto_attach_incomplete_parent_callback(monkeypatch):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_URL", "http://localhost:3080/api/viventium/glasshive/callback")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "callback-secret")
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-User-Id": "user-123",
            "X-Viventium-Conversation-Id": "conv-123",
        },
    )

    bundle = mcp_server._merge_request_context({"project_definition": "Do the work"})

    assert bundle is not None
    assert "callbacks" not in bundle
    assert bundle["viventium_context"]["user_id"] == "user-123"
    assert bundle["viventium_context"]["conversation_id"] == "conv-123"


def test_merge_request_context_projects_uploaded_file_headers(monkeypatch):
    monkeypatch.setattr(mcp_server, "load_viventium_runtime_env", lambda: {})
    monkeypatch.delenv("WPR_LIBRECHAT_UPLOADS_ROOT", raising=False)
    files = [
        {
            "file_id": "file-123",
            "filename": "brief.txt",
            "filepath": "/uploads/user/brief.txt",
            "source": "local",
            "context": "message_attachment",
        }
    ]
    encoded_files = "b64:" + base64.b64encode(json.dumps(files).encode()).decode()
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-Request-Files": encoded_files,
        },
    )

    bundle = mcp_server._merge_request_context({"project_definition": "Read the attachment."})

    assert bundle is not None
    assert bundle["viventium_upload_context"]["request_files"][0]["file_id"] == "file-123"
    assert bundle["files"][0]["path"] == "uploads/brief.txt.metadata.json"
    assert "source_path" not in bundle["files"][0]
    manifest = json.loads(bundle["files"][0]["content"])
    assert manifest["file_id"] == "file-123"
    assert manifest["source_ref"] == "/uploads/user/brief.txt"


def test_merge_request_context_accepts_generic_glasshive_headers(monkeypatch):
    monkeypatch.setattr(mcp_server, "load_viventium_runtime_env", lambda: {})
    files = [
        {
            "file_id": "file-123",
            "filename": "brief.txt",
            "filepath": "/uploads/user/brief.txt",
            "source": "local",
            "context": "message_attachment",
        }
    ]
    encoded_files = "b64:" + base64.b64encode(json.dumps(files).encode()).decode()
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-GlassHive-Tenant-Id": "tenant-alpha",
            "X-GlassHive-User-Id": "user-123",
            "X-GlassHive-Conversation-Id": "conv-123",
            "X-GlassHive-Request-Files": encoded_files,
            "X-LibreChat-Tool-Resources": encoded_files,
        },
    )

    bundle = mcp_server._merge_request_context({"project_definition": "Read the attachment."})

    assert bundle is not None
    assert bundle["glasshive_context"]["tenant_id"] == "tenant-alpha"
    assert bundle["glasshive_context"]["user_id"] == "user-123"
    assert bundle["viventium_context"]["conversation_id"] == "conv-123"
    assert bundle["glasshive_upload_context"]["request_files"][0]["file_id"] == "file-123"
    assert bundle["viventium_upload_context"]["tool_resources"][0]["file_id"] == "file-123"
    assert bundle["files"][0]["path"] == "uploads/brief.txt.metadata.json"


def test_merge_request_context_maps_virtual_uploads_to_trusted_local_source(monkeypatch, tmp_path):
    uploads_root = tmp_path / "uploads"
    upload_path = uploads_root / "user-123" / "brief with spaces.txt"
    upload_path.parent.mkdir(parents=True)
    upload_path.write_text("Use this brief.")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(uploads_root))
    files = [
        {
            "file_id": "file-123",
            "filename": "brief with spaces.txt",
            "filepath": "/uploads/user-123/brief with spaces.txt",
            "source": "local",
            "context": "message_attachment",
        }
    ]
    encoded_files = "b64:" + base64.b64encode(json.dumps(files).encode()).decode()
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-Tenant-Id": "tenant-alpha",
            "X-Viventium-User-Id": "user-123",
            "X-Viventium-Request-Files": encoded_files,
        },
    )

    bundle = mcp_server._merge_request_context({"project_definition": "Read the attachment."})

    assert bundle is not None
    assert bundle["files"][0]["path"] == "uploads/brief-with-spaces.txt"
    assert bundle["files"][0]["source_path"] == str(upload_path)
    assert "## Attached workspace files" in bundle["project_definition"]
    assert "`uploads/brief-with-spaces.txt`" in bundle["project_definition"]
    assert "Do not ask the user to re-attach" in bundle["project_definition"]
    assert bundle["files"][0]["source_path_token"] == sign_bootstrap_source_path(
        upload_path,
        tenant_id="tenant-alpha",
        owner_id="user-123",
    )


def test_merge_request_context_uses_existing_source_root_when_upload_root_is_stale(monkeypatch, tmp_path):
    stale_root = tmp_path / "stale-uploads"
    fallback_root = tmp_path / "repo-uploads"
    upload_path = fallback_root / "user-123" / "brief.txt"
    upload_path.parent.mkdir(parents=True)
    upload_path.write_text("Use this brief.")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(stale_root))
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", os.pathsep.join([str(stale_root), str(fallback_root)]))
    files = [
        {
            "file_id": "file-123",
            "filename": "brief.txt",
            "filepath": "/uploads/user-123/brief.txt",
            "source": "local",
            "context": "message_attachment",
        }
    ]
    encoded_files = "b64:" + base64.b64encode(json.dumps(files).encode()).decode()
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-Tenant-Id": "tenant-alpha",
            "X-Viventium-User-Id": "user-123",
            "X-Viventium-Request-Files": encoded_files,
        },
    )

    bundle = mcp_server._merge_request_context({"project_definition": "Read the attachment."})

    assert bundle is not None
    assert bundle["files"][0]["source_path"] == str(upload_path)
    assert bundle["files"][0]["source_path_token"] == sign_bootstrap_source_path(
        upload_path,
        tenant_id="tenant-alpha",
        owner_id="user-123",
    )


def test_merge_request_context_uses_metadata_manifest_when_upload_file_is_missing(monkeypatch, tmp_path):
    missing_root = tmp_path / "missing-uploads"
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(missing_root))
    files = [
        {
            "file_id": "file-123",
            "filename": "brief.txt",
            "filepath": "/uploads/user-123/brief.txt",
            "source": "local",
            "context": "message_attachment",
        }
    ]
    encoded_files = "b64:" + base64.b64encode(json.dumps(files).encode()).decode()
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-Tenant-Id": "tenant-alpha",
            "X-Viventium-User-Id": "user-123",
            "X-Viventium-Request-Files": encoded_files,
        },
    )

    bundle = mcp_server._merge_request_context({"project_definition": "Read the attachment."})

    assert bundle is not None
    assert bundle["files"][0]["path"] == "uploads/brief.txt.metadata.json"
    assert "source_path" not in bundle["files"][0]
    assert "source_path_token" not in bundle["files"][0]
    manifest = json.loads(bundle["files"][0]["content"])
    assert manifest["source_ref"] == "/uploads/user-123/brief.txt"


def test_enterprise_request_context_does_not_copy_cross_user_virtual_upload(monkeypatch, tmp_path):
    uploads_root = tmp_path / "uploads"
    other_user_file = uploads_root / "other-user" / "brief.txt"
    other_user_file.parent.mkdir(parents=True)
    other_user_file.write_text("other user's data")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(uploads_root))
    files = [
        {
            "file_id": "file-other",
            "filename": "brief.txt",
            "filepath": "/uploads/other-user/brief.txt",
            "source": "local",
            "context": "message_attachment",
        }
    ]
    encoded_files = "b64:" + base64.b64encode(json.dumps(files).encode()).decode()
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-Tenant-Id": "tenant-alpha",
            "X-Viventium-User-Id": "user-123",
            "X-Viventium-Request-Files": encoded_files,
        },
    )

    bundle = mcp_server._merge_request_context({"project_definition": "Read the attachment."})

    assert bundle is not None
    assert bundle["files"][0]["path"] == "uploads/brief.txt.metadata.json"
    assert "source_path" not in bundle["files"][0]
    manifest = json.loads(bundle["files"][0]["content"])
    assert manifest["source_ref"] == "/uploads/other-user/brief.txt"
    assert "## Attached workspace files" in bundle["project_definition"]
    assert "`uploads/brief.txt.metadata.json`" in bundle["project_definition"]


def test_runtime_env_repairs_missing_upload_root_to_local_checkout(monkeypatch, tmp_path):
    fallback_root = tmp_path / "repo-uploads"
    fallback_root.mkdir()
    runtime_file = tmp_path / "runtime.env"
    runtime_file.write_text(
        "\n".join(
            [
                f"WPR_LIBRECHAT_UPLOADS_ROOT={tmp_path / 'missing-uploads'}",
                f"WPR_BOOTSTRAP_SOURCE_ROOTS={tmp_path / 'missing-uploads'}",
            ]
        )
        + "\n"
    )
    monkeypatch.setenv("VIVENTIUM_ENV_FILE", str(runtime_file))
    monkeypatch.delenv("WPR_LIBRECHAT_UPLOADS_ROOT", raising=False)
    monkeypatch.delenv("WPR_BOOTSTRAP_SOURCE_ROOTS", raising=False)
    monkeypatch.setattr(runtime_env, "_local_checkout_librechat_uploads_root", lambda: fallback_root)

    loaded = runtime_env.load_viventium_runtime_env({"WPR_LIBRECHAT_UPLOADS_ROOT", "WPR_BOOTSTRAP_SOURCE_ROOTS"})

    assert loaded["WPR_LIBRECHAT_UPLOADS_ROOT"] == str(fallback_root)
    assert os.environ["WPR_LIBRECHAT_UPLOADS_ROOT"] == str(fallback_root)
    assert str(fallback_root) in os.environ["WPR_BOOTSTRAP_SOURCE_ROOTS"].split(os.pathsep)


def test_runtime_env_loads_host_cli_binary_paths(monkeypatch, tmp_path):
    runtime_file = tmp_path / "runtime.env"
    runtime_file.write_text(
        "\n".join(
            [
                f"WPR_CODEX_BIN={tmp_path / 'codex'}",
                f"WPR_CLAUDE_CODE_BIN={tmp_path / 'claude'}",
                f"WPR_OPENCLAW_BIN={tmp_path / 'openclaw'}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VIVENTIUM_ENV_FILE", str(runtime_file))
    monkeypatch.delenv("WPR_CODEX_BIN", raising=False)
    monkeypatch.delenv("WPR_CLAUDE_CODE_BIN", raising=False)
    monkeypatch.delenv("WPR_OPENCLAW_BIN", raising=False)

    loaded = runtime_env.load_viventium_runtime_env(
        {"WPR_CODEX_BIN", "WPR_CLAUDE_CODE_BIN", "WPR_OPENCLAW_BIN"}
    )

    assert loaded["WPR_CODEX_BIN"] == str(tmp_path / "codex")
    assert os.environ["WPR_CLAUDE_CODE_BIN"] == str(tmp_path / "claude")
    assert os.environ["WPR_OPENCLAW_BIN"] == str(tmp_path / "openclaw")


def test_runtime_env_loads_host_worker_native_capability_knobs(monkeypatch, tmp_path):
    runtime_file = tmp_path / "runtime.env"
    runtime_file.write_text(
        "\n".join(
            [
                'GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON={"claude-code":{"required_help_flags":["--chrome"]}}',
                "WPR_HOST_RUNTIME_REQUIREMENTS_FILE=glasshive-requirements.json",
                "GLASSHIVE_HOST_CODEX_NATIVE_MCP_ALLOWLIST=computer-use,node_repl",
                "WPR_HOST_CODEX_NATIVE_MCP_ALLOWLIST=computer-use,node_repl",
                "GLASSHIVE_HOST_CODEX_PLUGIN_CACHE=codex-plugin-cache",
                "WPR_HOST_CODEX_PLUGIN_CACHE=codex-plugin-cache",
                "WPR_CODEX_CLI_IGNORE_USER_CONFIG=false",
                "WPR_CODEX_CLI_DISABLE_FEATURES=image_generation",
                "WPR_CODEX_CLI_PROVIDER_NAME=GlassHive Test Provider",
                "WPR_CODEX_CLI_DISABLE_CUSTOM_PROVIDER=false",
                "WPR_CLAUDE_CODE_ENABLE_CHROME=true",
                "WPR_CLAUDE_CODE_EFFORT=max",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    keys = {
        "GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON",
        "WPR_HOST_RUNTIME_REQUIREMENTS_FILE",
        "GLASSHIVE_HOST_CODEX_NATIVE_MCP_ALLOWLIST",
        "WPR_HOST_CODEX_NATIVE_MCP_ALLOWLIST",
        "GLASSHIVE_HOST_CODEX_PLUGIN_CACHE",
        "WPR_HOST_CODEX_PLUGIN_CACHE",
        "WPR_CODEX_CLI_IGNORE_USER_CONFIG",
        "WPR_CODEX_CLI_DISABLE_FEATURES",
        "WPR_CODEX_CLI_PROVIDER_NAME",
        "WPR_CODEX_CLI_DISABLE_CUSTOM_PROVIDER",
        "WPR_CLAUDE_CODE_ENABLE_CHROME",
        "WPR_CLAUDE_CODE_EFFORT",
    }
    monkeypatch.setenv("VIVENTIUM_ENV_FILE", str(runtime_file))
    for key in keys:
        monkeypatch.delenv(key, raising=False)

    loaded = runtime_env.load_viventium_runtime_env()

    for key in keys:
        assert key in loaded
        assert os.environ[key] == loaded[key]
    assert os.environ["WPR_CLAUDE_CODE_EFFORT"] == "max"
    assert "computer-use" in os.environ["GLASSHIVE_HOST_CODEX_NATIVE_MCP_ALLOWLIST"]


def test_merge_request_context_projects_extracted_upload_text(monkeypatch):
    files = [
        {
            "file_id": "file-456",
            "filename": "brief.txt",
            "text": "Use this brief.",
            "source": "local",
            "context": "message_attachment",
        }
    ]
    encoded_files = "b64:" + base64.b64encode(json.dumps(files).encode()).decode()
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-Request-Files": encoded_files,
        },
    )

    bundle = mcp_server._merge_request_context({"project_definition": "Read the attachment."})

    assert bundle is not None
    assert bundle["files"][0]["path"] == "uploads/brief.txt"
    assert bundle["files"][0]["content"] == "Use this brief."
