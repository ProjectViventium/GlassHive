from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from workers_projects_runtime.api import create_app
from workers_projects_runtime.deliverables import deliverable_payload, is_deliverable_url
from workers_projects_runtime.openclaw_runtime import (
    RuntimeInfo,
    StubRuntime,
    WorkerInterruptedError,
    WorkerPausedError,
    WorkerTerminatedError,
)
from workers_projects_runtime.service import (
    WorkersProjectsService,
    terminal_callback_full_message,
    terminal_callback_message,
)
from workers_projects_runtime.signed_links import sign_link_params, sign_link_token
from workers_projects_runtime.store import Store


def wait_for_run(client: TestClient, run_id: str, timeout: float = 3.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/v1/runs/{run_id}")
        assert response.status_code == 200
        run = response.json()
        if run["state"] in {"completed", "failed", "cancelled", "interrupted"}:
            return run
        time.sleep(0.05)
    raise AssertionError(f"Run {run_id} did not settle within {timeout}s")


def wait_until(predicate, timeout: float = 2.0, interval: float = 0.01) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("Condition did not become true before timeout")


def test_terminal_callback_message_prefers_final_report():
    output = "\n".join(
        [
            "I am starting the browser.",
            "I am still scrolling through results.",
            "",
            "FINAL REPORT:",
            "Captured 42 rows.",
            "",
            "Created `results.md` and stopped on the target page.",
        ]
    )

    assert terminal_callback_message(output) == (
        "Captured 42 rows.\n\nCreated `results.md` and stopped on the target page."
    )


def test_terminal_callback_message_uses_line_anchored_final_report_marker():
    output = "\n".join(
        [
            "Progress: the harness says to include FINAL REPORT: at the end.",
            "",
            "FINAL REPORT:",
            "Captured 42 rows.",
        ]
    )

    assert terminal_callback_message(output) == "Captured 42 rows."


def test_terminal_callback_message_accepts_inline_final_report_marker():
    output = "Progress that should not surface.\nFINAL REPORT: Captured 42 rows."

    assert terminal_callback_message(output) == "Captured 42 rows."


def test_terminal_callback_message_uses_tail_without_mid_word_fragment():
    output = "\n\n".join(
        [
            "Opening the browser and trying the first path.",
            "Still gathering rows from the page.",
            "The useful result is ready.\nSaved the export and needs one approval.",
        ]
    )

    message = terminal_callback_message(output, fallback="Done")

    assert "The useful result is ready" in message
    assert not message.startswith("ows ")


def test_terminal_callback_message_prefers_concise_final_line_without_marker():
    output = "\n".join(
        [
            "Progress " + ("still working " * 120),
            "More progress " + ("checking browser state " * 80),
            "Example Domain",
        ]
    )

    assert terminal_callback_message(output, fallback="Done") == "Example Domain"


def test_terminal_callback_message_prefers_final_line_for_short_markerless_progress():
    output = "\n".join(
        [
            "Using the host browser and checking the page.",
            "Chrome is loaded; updating the work log.",
            "Viventium",
        ]
    )

    assert terminal_callback_message(output, fallback="Done") == "Viventium"


def test_terminal_callback_message_respects_visible_budget_with_prefix():
    output = "\n\n".join(
        [
            "Opening the browser and scrolling.",
            "FINAL REPORT:",
            "A" * 1500,
            "B" * 1500,
            "C" * 1500,
        ]
    )

    message = terminal_callback_message(output)

    assert len(message) <= 2400
    assert message.startswith("A")
    assert message.endswith("...")


def test_terminal_callback_full_message_preserves_long_final_report():
    final_report = "\n\n".join(["A" * 1500, "B" * 1500, "C" * 1500])
    output = f"Progress that should not surface.\nFINAL REPORT:\n{final_report}"

    assert terminal_callback_full_message(output) == final_report


def test_completed_callback_uses_final_report_message(tmp_path, monkeypatch):
    class FinalReportRuntime(StubRuntime):
        def run_task(
            self,
            worker: dict,
            instruction: str,
            timeout_sec: float | None = None,
            run_id: str | None = None,
        ) -> str:
            _ = worker, instruction, timeout_sec, run_id
            return "\n".join(
                [
                    "Opening the browser and scrolling.",
                    "Still collecting rows from the page.",
                    "",
                    "FINAL REPORT:",
                    "Captured 42 rows.",
                    "",
                    "Created `recent-connections.md` and stopped on the target page.",
                ]
            )

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

    payloads: list[dict] = []

    def capture_post(url, *, content, headers, timeout):
        _ = url, headers, timeout
        payloads.append(json.loads(content.decode("utf-8")))
        return Response()

    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_ATTEMPTS", "1")
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", capture_post)

    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, FinalReportRuntime(), max_workers=2)
    try:
        project = store.create_project("owner", "Callbacks", "Verify final report callbacks", "codex-cli")
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Browser Worker",
            role="browser worker",
            profile="codex-cli",
            backend="openclaw",
            runtime="codex-cli",
            model="gpt-5.4",
            bootstrap_bundle={
                "callbacks": {
                    "events_webhook_url": "http://callback.local/glasshive",
                    "hmac_secret": "callback-secret",
                    "conversation_id": "conv-1",
                    "parent_message_id": "msg-user",
                    "message_id": "msg-assistant",
                }
            },
        )

        run = service.assign_run(worker["worker_id"], "Open the browser and extract the result")
        wait_until(
            lambda: any(
                payload.get("event") == "run.completed" and payload.get("run_id") == run["run_id"]
                for payload in payloads
            )
        )

        completed = next(
            payload
            for payload in payloads
            if payload.get("event") == "run.completed" and payload.get("run_id") == run["run_id"]
        )
        assert completed["message"] == (
            "Captured 42 rows.\n\nCreated `recent-connections.md` and stopped on the target page."
        )
        assert completed["full_message"] == ""
        assert "Opening the browser" not in completed["message"]
        assert completed["message_id"] == "msg-assistant"
    finally:
        service.shutdown()


def test_completed_file_callback_adds_signed_download_and_watch_links(tmp_path, monkeypatch):
    class FileRuntime(StubRuntime):
        def run_task(
            self,
            worker: dict,
            instruction: str,
            timeout_sec: float | None = None,
            run_id: str | None = None,
        ) -> str:
            _ = instruction, timeout_sec, run_id
            info = self.ensure_worker_ready(worker)
            workspace = Path(info.workspace_dir)
            workspace.mkdir(parents=True, exist_ok=True)
            (workspace / "answer.txt").write_text("signed artifact", encoding="utf-8")
            return "FINAL REPORT:\nArtifact available at: /workspace/project/answer.txt"

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

    payloads: list[dict] = []

    def capture_post(url, *, content, headers, timeout):
        _ = url, headers, timeout
        payloads.append(json.loads(content.decode("utf-8")))
        return Response()

    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive-ui.example.test")
    monkeypatch.setenv("GLASSHIVE_ARTIFACT_BASE_URL", "https://glasshive-api.example.test")
    monkeypatch.setenv("WPR_API_TOKEN", "signed-link-secret")
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", capture_post)

    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, FileRuntime(), max_workers=2)
    try:
        project = store.create_project("owner", "Callbacks", "Signed artifact links", "codex-cli")
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="File Worker",
            role="file worker",
            profile="codex-cli",
            backend="openclaw",
            runtime="codex-cli",
            model="stub/codex-cli",
            bootstrap_bundle={
                "callbacks": {
                    "events_webhook_url": "http://callback.local/glasshive",
                    "hmac_secret": "callback-secret",
                    "conversation_id": "conv-1",
                    "parent_message_id": "msg-user",
                    "message_id": "msg-assistant",
                    "surface": "web",
                }
            },
        )

        service.assign_run(worker["worker_id"], "Create answer.txt")
        wait_until(lambda: any(payload.get("event") == "run.completed" for payload in payloads))

        completed = next(payload for payload in payloads if payload.get("event") == "run.completed")
        assert completed["deliverable"]["kind"] == "file"
        assert completed["deliverable"]["workspace_path"] == "answer.txt"
        assert "Download: [Download artifact](https://glasshive-api.example.test/v1/signed-links/" in completed["message"]
        assert (
            f"View / Steer: [Open GlassHive workspace](https://glasshive-ui.example.test/watch/{worker['worker_id']}?"
            in completed["message"]
        )
        assert "surface=desktop" in completed["message"]
        assert f"project_id={project['project_id']}" in completed["message"]
        assert "gh_token=" in completed["message"]
    finally:
        service.shutdown()


def test_failed_callback_reports_terminal_state_and_view_steer_link_without_local_path(tmp_path, monkeypatch):
    class FailingRuntime(StubRuntime):
        def run_task(
            self,
            worker: dict,
            instruction: str,
            timeout_sec: float | None = None,
            run_id: str | None = None,
        ) -> str:
            _ = worker, instruction, timeout_sec, run_id
            raise FileNotFoundError("Bootstrap source file not found: /Users/example/private-upload.pdf")

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

    payloads: list[dict] = []

    def capture_post(url, *, content, headers, timeout):
        _ = url, headers, timeout
        payloads.append(json.loads(content.decode("utf-8")))
        return Response()

    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive-ui.example.test")
    monkeypatch.setenv("WPR_API_TOKEN", "signed-link-secret")
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", capture_post)

    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, FailingRuntime(), max_workers=2)
    try:
        project = store.create_project("owner", "Callbacks", "Failure callback links", "codex-cli")
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="File Worker",
            role="file worker",
            profile="codex-cli",
            backend="openclaw",
            runtime="codex-cli",
            model="stub/codex-cli",
            bootstrap_bundle={
                "callbacks": {
                    "events_webhook_url": "http://callback.local/glasshive",
                    "hmac_secret": "callback-secret",
                    "conversation_id": "conv-1",
                    "parent_message_id": "msg-user",
                    "message_id": "msg-assistant",
                    "surface": "web",
                }
            },
        )

        run = service.assign_run(worker["worker_id"], "Read the uploaded file")
        wait_until(
            lambda: any(
                payload.get("event") == "run.failed" and payload.get("run_id") == run["run_id"]
                for payload in payloads
            )
        )

        failed = next(payload for payload in payloads if payload.get("event") == "run.failed")
        assert failed["run_state"] == "failed"
        assert "Bootstrap source file not found: [local path]" in failed["message"]
        assert "/Users/example" not in failed["message"]
        assert (
            f"View / Steer: [Open GlassHive workspace](https://glasshive-ui.example.test/watch/{worker['worker_id']}?"
            in failed["message"]
        )
        assert "gh_token=" in failed["message"]
        assert failed["watch_url"].startswith(f"https://glasshive-ui.example.test/watch/{worker['worker_id']}?")
    finally:
        service.shutdown()


def test_project_worker_lifecycle_with_stub_runtime(tmp_path):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["runtime_backend"] == "stub"

    project_resp = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Project Alpha",
            "goal": "Validate the standalone OpenClaw worker control plane.",
            "default_worker_profile": "openclaw-general",
        },
    )
    assert project_resp.status_code == 201
    project = project_resp.json()
    project_id = project["project_id"]

    worker_resp = client.post(
        f"/v1/projects/{project_id}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Research Worker",
            "role": "research",
            "profile": "openclaw-general",
            "backend": "openclaw",
            "bootstrap_profile": "host-login",
            "bootstrap_bundle": {
                "system_instructions": "Follow the project goal and keep operator checkpoints explicit.",
            },
        },
    )
    assert worker_resp.status_code == 201
    worker = worker_resp.json()
    worker_id = worker["worker_id"]
    assert worker["state"] == "ready"
    assert worker["runtime"] == "openclaw-stub"
    assert worker["execution_mode"] == "docker"
    assert worker["session_key"].endswith(worker_id)
    assert worker["bootstrap_profile"] == "host-login"

    assign_resp = client.post(
        f"/v1/workers/{worker_id}/assign",
        json={"instruction": "Research the best path for workers and projects."},
    )
    assert assign_resp.status_code == 202
    run = assign_resp.json()
    settled = wait_for_run(client, run["run_id"])
    assert settled["state"] == "completed"
    assert "STUB_OK" in settled["output_text"]

    pause_resp = client.post(f"/v1/workers/{worker_id}/pause")
    assert pause_resp.status_code == 202
    assert pause_resp.json()["state"] == "paused"

    resume_resp = client.post(f"/v1/workers/{worker_id}/resume")
    assert resume_resp.status_code == 202
    assert resume_resp.json()["state"] == "ready"

    message_resp = client.post(
        f"/v1/workers/{worker_id}/message",
        json={"message": "Shift focus to Codex and Claude worker design details."},
    )
    assert message_resp.status_code == 202
    message_run = wait_for_run(client, message_resp.json()["run_id"])
    assert message_run["state"] == "completed"
    assert "Operator message" in message_run["instruction"]

    events_resp = client.get(f"/v1/workers/{worker_id}/events")
    assert events_resp.status_code == 200
    assert len(events_resp.json()["items"]) >= 6

    terminate_resp = client.post(f"/v1/workers/{worker_id}/terminate")
    assert terminate_resp.status_code == 202
    assert terminate_resp.json()["state"] == "terminated"

    metrics_resp = client.get("/v1/metrics/summary")
    assert metrics_resp.status_code == 200
    metrics = metrics_resp.json()
    assert metrics["projects"] == 1
    assert metrics["workers"] == 1
    assert metrics["runs"] == 2
    assert metrics["queued_runs"] == 0
    assert metrics["events"] >= 7


def test_enterprise_mode_scopes_projects_workers_and_artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")

    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))
    headers_a = {
        "X-WPR-Token": "service-secret",
        "X-Viventium-Tenant-Id": "tenant-alpha",
        "X-Viventium-User-Id": "user-a",
        "X-Viventium-User-Email": "a@example.com",
        "X-Viventium-User-Role": "member",
    }
    headers_b = {
        "X-WPR-Token": "service-secret",
        "X-Viventium-Tenant-Id": "tenant-alpha",
        "X-Viventium-User-Id": "user-b",
        "X-Viventium-User-Email": "b@example.com",
        "X-Viventium-User-Role": "member",
    }
    generic_headers = {
        "X-GlassHive-Service-Token": "service-secret",
        "X-GlassHive-Tenant-Id": "tenant-alpha",
        "X-GlassHive-User-Id": "generic-user",
        "X-GlassHive-User-Email": "generic@example.com",
        "X-GlassHive-User-Role": "member",
    }

    assert client.get("/docs").status_code == 401
    assert client.get("/v1/projects", headers={"X-WPR-Token": "service-secret"}).status_code == 401
    assert client.get("/v1/projects", headers=generic_headers).status_code == 200
    mismatched_tenant = {**headers_a, "X-Viventium-Tenant-Id": "tenant-beta"}
    assert client.get("/v1/projects", headers=mismatched_tenant).status_code == 401

    project = client.post(
        "/v1/projects",
        headers=headers_a,
        json={
            "owner_id": "spoofed-owner",
            "title": "Enterprise Project",
            "goal": "Keep user work isolated.",
            "default_worker_profile": "openclaw-general",
        },
    )
    assert project.status_code == 201
    project_payload = project.json()
    assert project_payload["tenant_id"] == "tenant-alpha"
    assert project_payload["owner_id"] == "user-a"

    worker_payload = {
        "owner_id": "spoofed-owner",
        "name": "Shared Alias",
        "role": "research",
        "profile": "openclaw-general",
        "backend": "openclaw",
        "alias": "daily-browser",
    }
    worker_a = client.post(
        f"/v1/projects/{project_payload['project_id']}/workers/find-or-resume",
        headers=headers_a,
        json=worker_payload,
    )
    assert worker_a.status_code == 200
    worker_a_payload = worker_a.json()
    assert worker_a_payload["owner_id"] == "user-a"
    assert worker_a_payload["tenant_id"] == "tenant-alpha"
    assert worker_a_payload["alias"].startswith("tenant-alpha--user-a--")

    workspace_file = Path(worker_a_payload["workspace_dir"]) / "result.txt"
    workspace_file.parent.mkdir(parents=True, exist_ok=True)
    workspace_file.write_text("user-a result", encoding="utf-8")
    listed = client.get(f"/v1/workers/{worker_a_payload['worker_id']}/artifacts", headers=headers_a)
    assert listed.status_code == 200
    assert listed.json()["items"][0]["path"] == "result.txt"
    downloaded = client.get(
        f"/v1/workers/{worker_a_payload['worker_id']}/artifacts/download",
        headers=headers_a,
        params={"path": "result.txt"},
    )
    assert downloaded.status_code == 200
    assert downloaded.text == "user-a result"
    bare_download = client.get(
        f"/v1/workers/{worker_a_payload['worker_id']}/artifacts/download",
        params={"path": "result.txt"},
    )
    assert bare_download.status_code == 401
    signed = sign_link_params(
        kind="artifact_download",
        worker_id=worker_a_payload["worker_id"],
        tenant_id="tenant-alpha",
        owner_id="user-a",
        path="result.txt",
    )
    signed_download = client.get(
        f"/v1/workers/{worker_a_payload['worker_id']}/artifacts/download",
        params={"path": "result.txt", **signed},
    )
    assert signed_download.status_code == 200
    assert signed_download.text == "user-a result"
    signed_token = sign_link_token(
        kind="artifact_download",
        worker_id=worker_a_payload["worker_id"],
        tenant_id="tenant-alpha",
        owner_id="user-a",
        path="result.txt",
    )
    opaque_download = client.get(f"/v1/signed-links/{signed_token}")
    assert opaque_download.status_code == 200
    assert opaque_download.text == "user-a result"
    watch_token = sign_link_token(
        kind="worker_view",
        worker_id=worker_a_payload["worker_id"],
        tenant_id="tenant-alpha",
        owner_id="user-a",
    )
    opaque_watch = client.get(f"/v1/signed-links/{watch_token}", follow_redirects=False)
    assert opaque_watch.status_code == 302
    assert f"/ui/workers/{worker_a_payload['worker_id']}" in opaque_watch.headers["location"]
    assert "gh_sig=" in opaque_watch.headers["location"]
    signed_watch_query = urlsplit(opaque_watch.headers["location"]).query
    signed_live = client.get(f"/v1/workers/{worker_a_payload['worker_id']}/live?{signed_watch_query}")
    assert signed_live.status_code == 200
    assert signed_live.json()["worker"]["owner_id"] == "user-a"
    signed_pause = client.post(f"/v1/workers/{worker_a_payload['worker_id']}/pause?{signed_watch_query}")
    assert signed_pause.status_code == 202
    forged_signed_download = client.get(
        f"/v1/workers/{worker_a_payload['worker_id']}/artifacts/download",
        params={"path": "../runtime.db", **signed},
    )
    assert forged_signed_download.status_code == 401
    traversal = client.get(
        f"/v1/workers/{worker_a_payload['worker_id']}/artifacts/download",
        headers=headers_a,
        params={"path": "../runtime.db"},
    )
    assert traversal.status_code == 400
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("outside", encoding="utf-8")
    symlink_path = Path(worker_a_payload["workspace_dir"]) / "outside-link.txt"
    symlink_path.symlink_to(outside_file)
    symlink_escape = client.get(
        f"/v1/workers/{worker_a_payload['worker_id']}/artifacts/download",
        headers=headers_a,
        params={"path": "outside-link.txt"},
    )
    assert symlink_escape.status_code == 400

    assert client.get("/v1/projects", headers=headers_b).json()["items"] == []
    assert client.get(f"/v1/projects/{project_payload['project_id']}", headers=headers_b).status_code == 404
    assert client.get(f"/v1/workers/{worker_a_payload['worker_id']}", headers=headers_b).status_code == 404
    assert client.get(f"/v1/workers/{worker_a_payload['worker_id']}/artifacts", headers=headers_b).status_code == 404
    assert client.get("/v1/metrics/summary", headers=headers_a).json()["workers"] == 1
    assert client.get("/v1/metrics/summary", headers=headers_b).json()["workers"] == 0
    assert "metrics" not in client.get("/health").json()
    with pytest.raises(WebSocketDisconnect) as missing_token:
        with client.websocket_connect(f"/ws/workers/{worker_a_payload['worker_id']}/terminal"):
            pass
    assert missing_token.value.code == 4401
    with pytest.raises(WebSocketDisconnect) as wrong_user:
        with client.websocket_connect(f"/ws/workers/{worker_a_payload['worker_id']}/terminal", headers=headers_b):
            pass
    assert wrong_user.value.code == 4404


def test_enterprise_opaque_signed_links_reject_tamper_expiry_and_mismatch(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")

    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime()))
    headers = {
        "X-WPR-Token": "service-secret",
        "X-Viventium-Tenant-Id": "tenant-alpha",
        "X-Viventium-User-Id": "user-a",
        "X-Viventium-User-Role": "member",
    }
    project = client.post(
        "/v1/projects",
        headers=headers,
        json={"owner_id": "ignored", "title": "Signed Links", "goal": "Protect links."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        headers=headers,
        json={
            "owner_id": "ignored",
            "name": "Signed Link Worker",
            "role": "research",
            "profile": "codex-cli",
            "execution_mode": "docker",
        },
    ).json()
    workspace_file = Path(worker["workspace_dir"]) / "result.txt"
    workspace_file.parent.mkdir(parents=True, exist_ok=True)
    workspace_file.write_text("signed result", encoding="utf-8")

    valid = sign_link_token(
        kind="artifact_download",
        worker_id=worker["worker_id"],
        tenant_id="tenant-alpha",
        owner_id="user-a",
        path="result.txt",
    )
    assert client.get(f"/v1/signed-links/{valid}").status_code == 200

    tampered = f"{valid[:-1]}{'0' if valid[-1] != '0' else '1'}"
    assert client.get(f"/v1/signed-links/{tampered}").status_code == 401

    expired = sign_link_token(
        kind="artifact_download",
        worker_id=worker["worker_id"],
        tenant_id="tenant-alpha",
        owner_id="user-a",
        path="result.txt",
        ttl_seconds=-10,
    )
    assert client.get(f"/v1/signed-links/{expired}").status_code == 401

    mismatched_owner = sign_link_token(
        kind="artifact_download",
        worker_id=worker["worker_id"],
        tenant_id="tenant-alpha",
        owner_id="user-b",
        path="result.txt",
    )
    assert client.get(f"/v1/signed-links/{mismatched_owner}").status_code == 401


def test_enterprise_member_ui_redacts_runtime_internals(tmp_path, monkeypatch):
    class DesktopStubRuntime(StubRuntime):
        def describe_worker(self, worker: dict) -> dict[str, object]:
            return {
                "mode": "stub-desktop",
                "runtime": "openclaw-stub",
                "gateway_url": "http://127.0.0.1/stub-gateway",
                "workspace_dir": f"/tmp/{worker['worker_id']}/workspace",
                "view_url": "http://127.0.0.1:5901/?autoconnect=1&password=secret",
            }

    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")

    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=DesktopStubRuntime()))
    headers = {
        "X-WPR-Token": "service-secret",
        "X-Viventium-Tenant-Id": "tenant-alpha",
        "X-Viventium-User-Id": "user-a",
        "X-Viventium-User-Role": "member",
    }
    project = client.post(
        "/v1/projects",
        headers=headers,
        json={"owner_id": "ignored", "title": "Member UI", "goal": "Hide internals."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        headers=headers,
        json={
            "owner_id": "ignored",
            "name": "Redacted Worker",
            "role": "research",
            "profile": "codex-cli",
            "execution_mode": "docker",
        },
    ).json()

    live = client.get(f"/v1/workers/{worker['worker_id']}/live", headers=headers).json()

    assert "session_key" not in live["worker"]
    assert "workspace_dir" not in live["worker"]
    assert "pid" not in live["worker"]
    assert live["workspace"]["root"] == ""
    assert live["console"]["stdout"] == ""
    assert live["console"]["stderr"] == ""
    assert "workspace_dir" not in live["runtime_details"]
    assert "state_dir" not in live["runtime_details"]
    assert "gateway_url" not in live["runtime_details"]

    page = client.get(f"/ui/workers/{worker['worker_id']}", headers=headers)
    assert page.status_code == 200
    body = page.text
    assert "Session Key:" not in body
    assert "Worker ID:" not in body
    assert "agent:main:wpr:worker" not in body
    assert "/tmp/" not in body
    assert "Managed by GlassHive" in body

    project_page = client.get(f"/ui/projects/{project['project_id']}?worker_id={worker['worker_id']}", headers=headers)
    assert project_page.status_code == 200
    project_body = project_page.text
    assert "API docs" not in project_body
    assert "Gateway:" not in project_body
    assert "Open worker console" not in project_body
    assert "Take over terminal" not in project_body
    assert "Send message" not in project_body
    assert "Create worker only" not in project_body
    assert "workerAction('resume')" not in project_body
    assert "workerAction('pause')" not in project_body
    assert "Open full workspace" in project_body
    assert "Open desktop directly" not in project_body
    assert "http://127.0.0.1:5901" not in project_body
    assert f"/watch/{worker['worker_id']}?project_id={project['project_id']}&surface=desktop" in project_body
    assert f"/desktop/{worker['worker_id']}" in project_body


def test_live_payload_survives_unavailable_idle_compute(tmp_path):
    class UnavailableRuntime(StubRuntime):
        def describe_worker(self, worker: dict) -> dict[str, object]:
            raise RuntimeError("container was idle-reaped")

    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=UnavailableRuntime()))
    project = client.post(
        "/v1/projects",
        json={"owner_id": "owner", "title": "Idle Live", "goal": "Keep completed output visible."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "owner",
            "name": "Idle Worker",
            "role": "main",
            "profile": "codex-cli",
            "execution_mode": "docker",
        },
    ).json()
    run = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        json={"instruction": "create result"},
    ).json()
    completed = wait_for_run(client, run["run_id"])
    assert completed["state"] == "completed"

    live = client.get(f"/v1/workers/{worker['worker_id']}/live")

    assert live.status_code == 200
    payload = live.json()
    assert payload["worker"]["worker_id"] == worker["worker_id"]
    assert payload["latest_run"]["state"] == "completed"
    assert payload["runtime_details"]["mode"] == "unavailable"
    assert payload["runtime_details"]["sandbox_state"] == "compute_unavailable"


def test_enterprise_worker_lookup_authorizes_before_heal_side_effects(tmp_path, monkeypatch):
    class HealingRuntime(StubRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.collect_calls: list[str] = []

        def collect_completed_run(self, worker: dict, run_id: str | None = None) -> dict[str, str] | None:
            self.collect_calls.append(str(worker["worker_id"]))
            return {"state": "completed", "output_text": "done", "error_text": ""}

    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")
    db_path = tmp_path / "runtime.db"
    store = Store(str(db_path))
    project = store.create_project("user-a", "Heal Guard", "Do not heal cross-user.", "codex-cli", tenant_id="tenant-alpha")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="user-a",
        name="Running Worker",
        role="research",
        profile="codex-cli",
        backend="openclaw",
        runtime="codex-cli",
        model="gpt-test",
        tenant_id="tenant-alpha",
    )
    run = store.create_run(worker["worker_id"], project["project_id"], "finish", state="running")
    store.update_worker(worker["worker_id"], state="running", last_run_id=run["run_id"])
    runtime = HealingRuntime()
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=runtime))
    headers_b = {
        "X-WPR-Token": "service-secret",
        "X-Viventium-Tenant-Id": "tenant-alpha",
        "X-Viventium-User-Id": "user-b",
        "X-Viventium-User-Role": "member",
    }
    headers_a = {
        **headers_b,
        "X-Viventium-User-Id": "user-a",
    }

    assert client.get(f"/v1/workers/{worker['worker_id']}", headers=headers_b).status_code == 404
    assert runtime.collect_calls == []
    assert store.get_run(run["run_id"])["state"] == "running"

    assert client.get(f"/v1/workers/{worker['worker_id']}", headers=headers_a).status_code == 200
    assert runtime.collect_calls == [worker["worker_id"]]
    assert store.get_run(run["run_id"])["state"] == "completed"


def test_enterprise_mode_requires_service_token_at_startup(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.delenv("WPR_API_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="requires WPR_API_TOKEN"):
        create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime())


def test_enterprise_mode_requires_deployment_tenant_at_startup(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")
    monkeypatch.delenv("GLASSHIVE_ENTERPRISE_TENANT_ID", raising=False)
    monkeypatch.delenv("WPR_ENTERPRISE_TENANT_ID", raising=False)

    with pytest.raises(RuntimeError, match="requires GLASSHIVE_ENTERPRISE_TENANT_ID"):
        create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime())


def test_enterprise_mode_requires_signed_link_secret_at_startup(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.delenv("GLASSHIVE_SIGNED_LINK_SECRET", raising=False)

    with pytest.raises(RuntimeError, match="requires GLASSHIVE_SIGNED_LINK_SECRET"):
        create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime())


def test_enterprise_mode_requires_signed_link_secret_distinct_from_service_token(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "same-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "same-secret")

    with pytest.raises(RuntimeError, match="must be distinct"):
        create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime())


def test_enterprise_mode_loads_direct_runtime_env_file(tmp_path, monkeypatch):
    runtime_env = tmp_path / "runtime.env"
    runtime_env.write_text(
        "\n".join(
            [
                "GLASSHIVE_ENTERPRISE_MODE=true",
                "GLASSHIVE_AUTH_MODE=first_party_assertion",
                "GLASSHIVE_ENTERPRISE_TENANT_ID=tenant-alpha",
                "WPR_API_TOKEN=service-secret",
                "GLASSHIVE_SIGNED_LINK_SECRET=signed-link-secret",
                "OPENAI_API_KEY=runtime-openai-key",
                "ANTHROPIC_API_KEY=runtime-anthropic-key",
                "WPR_OPENCLAW_USE_CUSTOM_PROVIDER=1",
                "WPR_OPENCLAW_WIRE_API=openai-completions",
                "GLASSHIVE_MAX_WORKSPACES_PER_USER=4",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VIVENTIUM_ENV_FILE", str(runtime_env))
    for key in (
        "GLASSHIVE_ENTERPRISE_MODE",
        "GLASSHIVE_AUTH_MODE",
        "GLASSHIVE_ENTERPRISE_TENANT_ID",
        "WPR_API_TOKEN",
        "GLASSHIVE_SIGNED_LINK_SECRET",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "WPR_OPENCLAW_USE_CUSTOM_PROVIDER",
        "WPR_OPENCLAW_WIRE_API",
        "GLASSHIVE_MAX_WORKSPACES_PER_USER",
    ):
        monkeypatch.delenv(key, raising=False)

    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime()))

    assert client.get("/health").status_code == 200
    assert client.get("/docs").status_code == 401
    assert os.environ["OPENAI_API_KEY"] == "runtime-openai-key"
    assert os.environ["ANTHROPIC_API_KEY"] == "runtime-anthropic-key"
    assert os.environ["WPR_OPENCLAW_USE_CUSTOM_PROVIDER"] == "1"
    assert os.environ["WPR_OPENCLAW_WIRE_API"] == "openai-completions"
    assert os.environ["GLASSHIVE_MAX_WORKSPACES_PER_USER"] == "4"


def test_enterprise_oauth_modes_require_external_validator(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "oauth_oidc")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")

    with pytest.raises(RuntimeError, match="external token validator"):
        create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime())


def test_enterprise_admin_api_is_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")

    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime()))
    headers = {
        "X-WPR-Token": "service-secret",
        "X-Viventium-Tenant-Id": "tenant-alpha",
        "X-Viventium-User-Id": "admin-user",
        "X-Viventium-User-Role": "admin",
    }

    assert client.post("/v1/admin/reconcile", headers=headers).status_code == 404
    assert client.post("/v1/admin/schedules/run-due", headers=headers).status_code == 404


def test_enterprise_admin_api_requires_enable_flag_and_admin_role(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")
    monkeypatch.setenv("GLASSHIVE_ENABLE_ADMIN_API", "true")

    client = TestClient(create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=StubRuntime()))
    member_headers = {
        "X-WPR-Token": "service-secret",
        "X-Viventium-Tenant-Id": "tenant-alpha",
        "X-Viventium-User-Id": "member-user",
        "X-Viventium-User-Role": "member",
    }
    admin_headers = {
        **member_headers,
        "X-Viventium-User-Id": "admin-user",
        "X-Viventium-User-Role": "admin",
    }

    assert client.post("/v1/admin/reconcile", headers=member_headers).status_code == 403
    assert client.post("/v1/admin/schedules/run-due", headers=member_headers).status_code == 403
    assert client.post("/v1/admin/reconcile", headers=admin_headers).status_code == 200
    processed = client.post("/v1/admin/schedules/run-due", headers=admin_headers)
    assert processed.status_code == 200
    assert processed.json()["processed"] == []


def test_idle_reaper_stops_compute_but_preserves_worker(tmp_path, monkeypatch):
    class ReaperRuntime(StubRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.terminated: list[str] = []

        def terminate_worker(self, worker: dict) -> RuntimeInfo:
            self.terminated.append(worker["worker_id"])
            return RuntimeInfo(
                runtime=str(worker.get("runtime") or "openclaw-stub"),
                model=str(worker.get("model") or "stub-model"),
                gateway_url="",
                gateway_port=None,
                gateway_token=None,
                session_key=str(worker.get("session_key") or ""),
                state_dir=str(worker.get("state_dir") or ""),
                workspace_dir=str(worker.get("workspace_dir") or ""),
                pid=None,
            )

    monkeypatch.setenv("GLASSHIVE_IDLE_TERMINATE_AFTER_S", "1")
    monkeypatch.setenv("GLASSHIVE_IDLE_REAPER_INTERVAL_S", "3600")
    store = Store(str(tmp_path / "runtime.db"))
    runtime = ReaperRuntime()
    service = WorkersProjectsService(store, runtime)
    try:
        project = service.create_project("owner", "Idle", "Stop idle compute", "openclaw-general")
        worker = service.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Idle Worker",
            role="research",
            profile="openclaw-general",
            backend="openclaw",
        )
        with store._connect() as conn:
            conn.execute(
                "UPDATE workers SET updated_at = ? WHERE worker_id = ?",
                ((datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(), worker["worker_id"]),
            )

        reaped = service.reap_idle_workers_once()

        assert reaped and reaped[0]["worker_id"] == worker["worker_id"]
        assert runtime.terminated == [worker["worker_id"]]
        refreshed = store.get_worker(worker["worker_id"])
        assert refreshed is not None
        assert refreshed["state"] == "paused"
        assert store.list_events(worker["worker_id"])[-1]["event_type"] == "worker.idle_terminated"
        with store._connect() as conn:
            conn.execute(
                "UPDATE workers SET updated_at = ? WHERE worker_id = ?",
                ((datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(), worker["worker_id"]),
            )
        assert service.reap_idle_workers_once() == []
        assert runtime.terminated == [worker["worker_id"]]
    finally:
        service.shutdown()

def test_callbacks_sign_utf8_canonical_json_for_unicode_messages(tmp_path, monkeypatch):
    captured: dict[str, object] = {}

    class Response:
        def raise_for_status(self):
            return None

    def fake_post(url, *, content, headers, timeout):
        captured["url"] = url
        captured["content"] = content
        captured["headers"] = headers
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", fake_post)
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime())
    try:
        project = store.create_project("owner", "Callbacks", "Verify callback signatures", "codex-cli")
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Codex Host",
            role="host worker",
            profile="codex-cli",
            backend="openclaw",
            runtime="codex-cli",
            model="gpt-5.4",
            bootstrap_bundle={
                "callbacks": {
                    "events_webhook_url": "http://callback.local/glasshive",
                    "hmac_secret": "callback-secret",
                    "surface": "telegram",
                    "stream_id": "stream-123",
                    "voice_call_session_id": "call-123",
                    "telegram_chat_id": "chat-123",
                }
            },
        )
        run = store.create_run(worker["worker_id"], project["project_id"], "Open user's Chrome - no sandbox", state="running")

        service._emit_callback(worker, "run.started", run=run, message="Open user's Chrome - no sandbox — verified")
        wait_until(lambda: "content" in captured)
    finally:
        service.shutdown()

    content = captured["content"]
    assert isinstance(content, bytes)
    payload_text = content.decode("utf-8")
    assert "—" in payload_text
    assert "\\u2014" not in payload_text

    payload = json.loads(payload_text)
    assert payload["surface"] == "telegram"
    assert payload["stream_id"] == "stream-123"
    assert payload["voice_call_session_id"] == "call-123"
    assert payload["telegram_chat_id"] == "chat-123"
    binding = f"{payload['worker_id']}:{payload['run_id']}".encode("utf-8")
    derived_secret = hmac.new(b"callback-secret", binding, hashlib.sha256).hexdigest().encode("utf-8")
    expected = "sha256=" + hmac.new(derived_secret, content, hashlib.sha256).hexdigest()
    assert captured["headers"]["X-GlassHive-Signature"] == expected


def test_incomplete_viventium_parent_callback_is_not_enqueued(tmp_path, monkeypatch):
    posted = Event()

    def fake_post(*args, **kwargs):
        posted.set()
        raise AssertionError("Incomplete Viventium callbacks must not be sent")

    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", fake_post)
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime())
    try:
        project = store.create_project("owner", "Callbacks", "Skip incomplete parent callbacks", "codex-cli")
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Codex Host",
            role="host worker",
            profile="codex-cli",
            backend="openclaw",
            runtime="codex-cli",
            model="gpt-5.4",
            bootstrap_bundle={
                "callbacks": {
                    "events_webhook_url": "http://localhost:3080/api/viventium/glasshive/callback",
                    "hmac_secret": "callback-secret",
                    "user_id": "user-1",
                }
            },
        )
        run = store.create_run(worker["worker_id"], project["project_id"], "Finish", state="running")

        service._emit_callback(worker, "run.completed", run=run, message="Done")
        time.sleep(0.05)
        with store._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM callback_outbox").fetchone()[0]
    finally:
        service.shutdown()

    assert not posted.is_set()
    assert count == 0


def test_callbacks_retry_transient_delivery_failures(tmp_path, monkeypatch):
    attempts: list[int] = []

    class Response:
        def __init__(self, status_code: int):
            self.status_code = status_code

        def raise_for_status(self):
            if self.status_code >= 400:
                request = httpx.Request("POST", "http://callback.local/glasshive")
                response = httpx.Response(self.status_code, request=request)
                raise httpx.HTTPStatusError("server error", request=request, response=response)
            return None

    def fake_post(url, *, content, headers, timeout):
        _ = url, content, headers, timeout
        attempts.append(1)
        return Response(500 if len(attempts) == 1 else 200)

    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", fake_post)
    monkeypatch.setattr("workers_projects_runtime.service.time.sleep", lambda _seconds: None)
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime())
    try:
        project = store.create_project("owner", "Callbacks", "Verify callback retry", "codex-cli")
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Codex Host",
            role="host worker",
            profile="codex-cli",
            backend="openclaw",
            runtime="codex-cli",
            model="gpt-5.4",
            bootstrap_bundle={
                "callbacks": {
                    "events_webhook_url": "http://callback.local/glasshive",
                    "hmac_secret": "callback-secret",
                }
            },
        )
        run = store.create_run(worker["worker_id"], project["project_id"], "Open Chrome", state="running")

        service._emit_callback(worker, "run.completed", run=run, message="Done")
        wait_until(lambda: len(attempts) == 2)
    finally:
        service.shutdown()

    assert len(attempts) == 2
    assert not [event for event in store.list_events(worker["worker_id"]) if event["event_type"] == "callback.failed"]


def test_duplicate_callback_is_treated_as_delivered(tmp_path, monkeypatch):
    attempts: list[int] = []

    class Response:
        status_code = 409

        def raise_for_status(self):
            request = httpx.Request("POST", "http://callback.local/glasshive")
            response = httpx.Response(409, request=request)
            raise httpx.HTTPStatusError("duplicate callback", request=request, response=response)

    def fake_post(url, *, content, headers, timeout):
        _ = url, content, headers, timeout
        attempts.append(1)
        return Response()

    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", fake_post)
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime())
    try:
        project = store.create_project("owner", "Callbacks", "Verify callback dedupe", "codex-cli")
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Codex Host",
            role="host worker",
            profile="codex-cli",
            backend="openclaw",
            runtime="codex-cli",
            model="gpt-5.4",
            bootstrap_bundle={
                "callbacks": {
                    "events_webhook_url": "http://callback.local/glasshive",
                    "hmac_secret": "callback-secret",
                }
            },
        )
        run = store.create_run(worker["worker_id"], project["project_id"], "Open Chrome", state="running")

        service._emit_callback(worker, "run.completed", run=run, message="Done")
        wait_until(lambda: len(attempts) == 1)
    finally:
        service.shutdown()

    assert len(attempts) == 1
    assert not [event for event in store.list_events(worker["worker_id"]) if event["event_type"] == "callback.failed"]


def test_failed_callback_stays_pending_and_replays_on_restart(tmp_path, monkeypatch):
    attempts: list[int] = []

    class Response:
        def __init__(self, status_code: int):
            self.status_code = status_code

        def raise_for_status(self):
            if self.status_code >= 400:
                request = httpx.Request("POST", "http://callback.local/glasshive")
                response = httpx.Response(self.status_code, request=request)
                raise httpx.HTTPStatusError("server error", request=request, response=response)
            return None

    def failing_post(url, *, content, headers, timeout):
        _ = url, content, headers, timeout
        attempts.append(500)
        return Response(500)

    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_BASE_DELAY_S", "0")
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", failing_post)
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime())
    try:
        project = store.create_project("owner", "Callbacks", "Verify callback outbox replay", "codex-cli")
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Codex Host",
            role="host worker",
            profile="codex-cli",
            backend="openclaw",
            runtime="codex-cli",
            model="gpt-5.4",
            bootstrap_bundle={
                "callbacks": {
                    "events_webhook_url": "http://callback.local/glasshive",
                    "hmac_secret": "callback-secret",
                }
            },
        )
        run = store.create_run(worker["worker_id"], project["project_id"], "Open Chrome", state="running")

        service._emit_callback(worker, "run.completed", run=run, message="Done")
        wait_until(
            lambda: bool(store.list_pending_callbacks())
            and bool(
                [
                    event
                    for event in store.list_events(worker["worker_id"])
                    if event["event_type"] == "callback.failed"
                ]
            )
        )
    finally:
        service.shutdown()

    pending = store.list_pending_callbacks()
    assert len(pending) == 1
    callback_id = pending[0]["callback_id"]
    assert [event for event in store.list_events(worker["worker_id"]) if event["event_type"] == "callback.failed"]

    def succeeding_post(url, *, content, headers, timeout):
        _ = url, headers, timeout
        attempts.append(200)
        payload = json.loads(content.decode("utf-8"))
        assert payload["callback_id"] == callback_id
        return Response(200)

    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", succeeding_post)
    replay_service = WorkersProjectsService(store, StubRuntime())
    try:
        deadline = time.time() + 2
        while time.time() < deadline and store.list_pending_callbacks():
            time.sleep(0.05)
    finally:
        replay_service.shutdown()

    assert not store.list_pending_callbacks()
    with store._connect() as conn:
        row = conn.execute("SELECT status, attempts FROM callback_outbox WHERE callback_id = ?", (callback_id,)).fetchone()
    assert row["status"] == "delivered"
    assert row["attempts"] == 2
    assert attempts == [500, 200]


def test_pending_callback_replay_does_not_block_service_startup(tmp_path, monkeypatch):
    class Response:
        status_code = 500

        def raise_for_status(self):
            request = httpx.Request("POST", "http://callback.local/glasshive")
            response = httpx.Response(500, request=request)
            raise httpx.HTTPStatusError("server error", request=request, response=response)

    entered_post = Event()
    release_post = Event()

    def blocking_post(url, *, content, headers, timeout):
        _ = url, content, headers, timeout
        entered_post.set()
        release_post.wait(timeout=1)
        return Response()

    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_BASE_DELAY_S", "0")
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", blocking_post)

    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project("owner", "Callbacks", "Verify non-blocking callback replay", "codex-cli")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Codex Host",
        role="host worker",
        profile="codex-cli",
        backend="openclaw",
        runtime="codex-cli",
        model="gpt-5.4",
        bootstrap_bundle={
            "callbacks": {
                "events_webhook_url": "http://callback.local/glasshive",
                "hmac_secret": "callback-secret",
            }
        },
    )
    run = store.create_run(worker["worker_id"], project["project_id"], "Open Chrome", state="completed")
    payload = {
        "callback_id": "cb_pending_startup",
        "callback_ts": int(time.time()),
        "event": "run.completed",
        "project_id": project["project_id"],
        "worker_id": worker["worker_id"],
        "run_id": run["run_id"],
        "run_state": "completed",
        "message": "Done",
    }
    store.upsert_callback_outbox(
        callback_id="cb_pending_startup",
        project_id=project["project_id"],
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        event_type="run.completed",
        url="http://callback.local/glasshive",
        payload_json=json.dumps(payload, ensure_ascii=False),
    )

    started_at = time.perf_counter()
    service = WorkersProjectsService(store, StubRuntime())
    elapsed = time.perf_counter() - started_at
    try:
        assert elapsed < 0.5
        assert entered_post.wait(timeout=1)
    finally:
        release_post.set()
        service.shutdown()


def test_callbacks_retry_budget_is_configurable(tmp_path, monkeypatch):
    attempts: list[int] = []

    class Response:
        def __init__(self, status_code: int):
            self.status_code = status_code

        def raise_for_status(self):
            if self.status_code >= 400:
                request = httpx.Request("POST", "http://callback.local/glasshive")
                response = httpx.Response(self.status_code, request=request)
                raise httpx.HTTPStatusError("server error", request=request, response=response)
            return None

    def fake_post(url, *, content, headers, timeout):
        _ = url, content, headers, timeout
        attempts.append(1)
        return Response(500 if len(attempts) < 4 else 200)

    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_ATTEMPTS", "4")
    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_BASE_DELAY_S", "0")
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", fake_post)
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime())
    try:
        project = store.create_project("owner", "Callbacks", "Verify configurable callback retry", "codex-cli")
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Codex Host",
            role="host worker",
            profile="codex-cli",
            backend="openclaw",
            runtime="codex-cli",
            model="gpt-5.4",
            bootstrap_bundle={
                "callbacks": {
                    "events_webhook_url": "http://callback.local/glasshive",
                    "hmac_secret": "callback-secret",
                }
            },
        )
        run = store.create_run(worker["worker_id"], project["project_id"], "Open Chrome", state="running")

        service._emit_callback(worker, "run.completed", run=run, message="Done")
        wait_until(lambda: len(attempts) == 4)
    finally:
        service.shutdown()

    assert len(attempts) == 4
    assert not [event for event in store.list_events(worker["worker_id"]) if event["event_type"] == "callback.failed"]


def test_assign_run_does_not_block_on_callback_delivery(tmp_path, monkeypatch):
    entered_post = Event()
    release_post = Event()

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

    def blocking_post(url, *, content, headers, timeout):
        _ = url, content, headers, timeout
        entered_post.set()
        release_post.wait(timeout=1)
        return Response()

    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_BASE_DELAY_S", "0")
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", blocking_post)

    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime(), max_workers=2)
    try:
        project = store.create_project("owner", "Callbacks", "Verify non-blocking run callback", "codex-cli")
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Codex Host",
            role="host worker",
            profile="codex-cli",
            backend="openclaw",
            runtime="codex-cli",
            model="gpt-5.4",
            bootstrap_bundle={
                "callbacks": {
                    "events_webhook_url": "http://callback.local/glasshive",
                    "hmac_secret": "callback-secret",
                }
            },
        )

        started_at = time.perf_counter()
        run = service.assign_run(worker["worker_id"], "Open Chrome")
        elapsed = time.perf_counter() - started_at
        assert run["state"] == "queued"
        assert elapsed < 0.25
        assert entered_post.wait(timeout=1)
    finally:
        release_post.set()
        service.shutdown()


def test_callback_config_recovers_runtime_env_url_and_secret(tmp_path, monkeypatch, caplog):
    runtime_env = tmp_path / "runtime.env"
    runtime_env.write_text(
        "\n".join(
            [
                "VIVENTIUM_GLASSHIVE_CALLBACK_URL=http://callback.local/glasshive",
                "VIVENTIUM_GLASSHIVE_CALLBACK_SECRET=runtime-secret",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VIVENTIUM_ENV_FILE", str(runtime_env))
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_CALLBACK_URL", raising=False)
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", raising=False)

    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime())
    try:
        project = store.create_project("owner", "Callbacks", "Recover callback env", "codex-cli")
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Codex Host",
            role="host worker",
            profile="codex-cli",
            backend="openclaw",
            runtime="codex-cli",
            model="gpt-5.4",
            bootstrap_bundle={
                "callbacks": {
                    "conversation_id": "conv-1",
                    "message_id": "assistant-1",
                }
            },
        )

        with caplog.at_level("WARNING"):
            callbacks = service._callback_config_for(worker)
    finally:
        service.shutdown()

    assert callbacks["events_webhook_url"] == "http://callback.local/glasshive"
    assert callbacks["hmac_secret"] == "runtime-secret"
    assert callbacks["conversation_id"] == "conv-1"
    assert "Recovered GlassHive callback endpoint, secret" in caplog.text
    assert "runtime-secret" not in caplog.text


def test_reconcile_interrupts_active_run_when_worker_process_is_missing(tmp_path):
    class MissingProcessRuntime(StubRuntime):
        def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
            info = super().ensure_worker_ready(worker)
            info.pid = None
            return info

    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, MissingProcessRuntime())
    try:
        project = store.create_project("owner", "Orphan Cleanup", "Clean up stale active runs", "codex-cli")
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Codex Host",
            role="host worker",
            profile="codex-cli",
            backend="openclaw",
            runtime="codex-cli",
            model="gpt-5.4",
        )
        store.update_worker_state(worker["worker_id"], "running")
        run = store.create_run(worker["worker_id"], project["project_id"], "Long host task", state="running")

        service.reconcile_all_workers()
    finally:
        service.shutdown()

    reconciled_run = store.get_run(run["run_id"])
    assert reconciled_run["state"] == "interrupted"
    assert reconciled_run["ended_at"]
    assert "process was not running" in reconciled_run["error_text"]
    assert store.get_worker(worker["worker_id"])["state"] == "paused"
    assert store.metrics()["active_runs"] == 0
    assert any(event["event_type"] == "run.orphaned" for event in store.list_events(worker["worker_id"]))


def test_reconcile_skips_idle_paused_workers_and_cleans_inconsistent_runs(tmp_path):
    class CountingRuntime(StubRuntime):
        def __init__(self) -> None:
            self.reconciled_worker_ids: list[str] = []

        def reconcile_worker(self, worker: dict) -> RuntimeInfo:
            self.reconciled_worker_ids.append(worker["worker_id"])
            return super().reconcile_worker(worker)

    store = Store(str(tmp_path / "runtime.db"))
    project = store.create_project("owner", "Startup Efficiency", "Avoid Docker scans for paused workspaces", "codex-cli")
    paused_idle = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Paused Idle",
        role="saved worker",
        profile="codex-cli",
        backend="openclaw",
        runtime="codex-cli",
        model="gpt-5.4",
    )
    paused_with_run = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Paused With Run",
        role="inconsistent worker",
        profile="codex-cli",
        backend="openclaw",
        runtime="codex-cli",
        model="gpt-5.4",
    )
    ready_worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner",
        name="Ready Worker",
        role="active worker",
        profile="codex-cli",
        backend="openclaw",
        runtime="codex-cli",
        model="gpt-5.4",
    )
    store.update_worker_state(paused_idle["worker_id"], "paused")
    store.update_worker_state(paused_with_run["worker_id"], "paused")
    store.update_worker_state(ready_worker["worker_id"], "ready")
    active_run = store.create_run(paused_with_run["worker_id"], project["project_id"], "stale run", state="running")

    runtime = CountingRuntime()
    service = WorkersProjectsService(store, runtime)
    try:
        pass
    finally:
        service.shutdown()

    assert runtime.reconciled_worker_ids == [ready_worker["worker_id"]]
    assert store.get_run(active_run["run_id"])["state"] == "interrupted"
    assert any(event["event_type"] == "run.orphaned" for event in store.list_events(paused_with_run["worker_id"]))


def test_reconcile_does_not_regress_completed_run_when_process_is_missing(tmp_path):
    class MissingProcessRuntime(StubRuntime):
        def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
            info = super().ensure_worker_ready(worker)
            info.pid = None
            return info

    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, MissingProcessRuntime())
    try:
        project = store.create_project("owner", "Orphan Race", "Avoid regressing completed runs", "codex-cli")
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Codex Host",
            role="host worker",
            profile="codex-cli",
            backend="openclaw",
            runtime="codex-cli",
            model="gpt-5.4",
        )
        store.update_worker_state(worker["worker_id"], "running")
        run = store.create_run(worker["worker_id"], project["project_id"], "Finishing host task", state="running")
        store.finalize_run(run["run_id"], "completed", output_text="done")

        service.reconcile_all_workers()
    finally:
        service.shutdown()

    reconciled_run = store.get_run(run["run_id"])
    assert reconciled_run["state"] == "completed"
    assert reconciled_run["output_text"] == "done"
    assert not any(event["event_type"] == "run.orphaned" for event in store.list_events(worker["worker_id"]))


def test_worker_find_or_resume_reuses_alias_and_preserves_host_fields(tmp_path):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))
    project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Host Workers",
            "goal": "Reuse named host workers.",
            "default_worker_profile": "codex-cli",
        },
    ).json()
    payload = {
        "owner_id": "demo-owner",
        "name": "Codex Host",
        "role": "coding",
        "profile": "codex-cli",
        "backend": "openclaw",
        "execution_mode": "host",
        "alias": "codex-main",
        "workspace_root": str(tmp_path / "workspaces"),
    }

    first = client.post(f"/v1/projects/{project['project_id']}/workers/find-or-resume", json=payload)
    second = client.post(f"/v1/projects/{project['project_id']}/workers/find-or-resume", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["worker_id"] == first.json()["worker_id"]
    assert second.json()["execution_mode"] == "host"
    assert second.json()["alias"] == "codex-main"
    assert second.json()["workspace_root"] == str(tmp_path / "workspaces")


def test_worker_find_or_resume_refreshes_callback_bundle_on_alias_reuse(tmp_path, monkeypatch):
    monkeypatch.delenv("VIVENTIUM_ENV_FILE", raising=False)
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_CALLBACK_URL", raising=False)
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", raising=False)
    payloads: list[dict] = []

    class Response:
        def raise_for_status(self):
            return None

    def fake_post(url, *, content, headers, timeout):
        _ = url, headers, timeout
        payloads.append(json.loads(content.decode("utf-8")))
        return Response()

    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_ATTEMPTS", "1")
    monkeypatch.setattr("workers_projects_runtime.service.httpx.post", fake_post)

    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime(), max_workers=2)
    try:
        project = store.create_project("owner", "Host Workers", "Reuse named host workers.", "codex-cli")
        original = service.find_or_create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Codex Host",
            role="browser worker",
            profile="codex-cli",
            backend="openclaw",
            alias="codex-main",
            execution_mode="host",
            workspace_root=str(tmp_path / "workspaces"),
            bootstrap_bundle={
                "mcp_config": {"servers": {"safe-tool": {"url": "http://safe-tool.local/mcp"}}},
                "instructions_md": "Keep operator-seeded instructions.",
                "env_overrides": {"SAFE_FLAG": "1"},
                "files": [{"path": "seed.md", "content": "seed"}],
                "callbacks": {
                    "conversation_id": "old-conversation",
                    "parent_message_id": "old-parent",
                    "message_id": "old-assistant",
                }
            },
        )

        refreshed = service.find_or_create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Codex Host",
            role="browser worker",
            profile="codex-cli",
            backend="openclaw",
            alias="codex-main",
            execution_mode="host",
            workspace_root=str(tmp_path / "workspaces"),
            bootstrap_bundle={
                "files": [{"path": "current.md", "content": "current"}],
                "callbacks": {
                    "events_webhook_url": "http://callback.local/glasshive",
                    "hmac_secret": "callback-secret",
                    "conversation_id": "new-conversation",
                    "parent_message_id": "new-parent",
                    "message_id": "new-assistant",
                }
            },
        )

        assert refreshed["worker_id"] == original["worker_id"]
        stored_worker = store.get_worker(refreshed["worker_id"])
        stored_bundle = json.loads(stored_worker["bootstrap_bundle_json"])
        assert stored_bundle["callbacks"]["conversation_id"] == "new-conversation"
        assert stored_bundle["callbacks"]["events_webhook_url"] == "http://callback.local/glasshive"
        assert stored_bundle["mcp_config"]["servers"]["safe-tool"]["url"] == "http://safe-tool.local/mcp"
        assert stored_bundle["instructions_md"] == "Keep operator-seeded instructions."
        assert stored_bundle["env_overrides"]["SAFE_FLAG"] == "1"
        assert {item["path"] for item in stored_bundle["files"]} == {"seed.md", "current.md"}

        run = service.assign_run(refreshed["worker_id"], "Return the final result to the current chat")
        wait_until(lambda: (store.get_run(run["run_id"]) or {}).get("state") == "completed")
        wait_until(lambda: any(payload.get("event") == "run.completed" for payload in payloads))

        completed = next(payload for payload in payloads if payload.get("event") == "run.completed")
        assert completed["conversation_id"] == "new-conversation"
        assert completed["parent_message_id"] == "new-parent"
        assert completed["message_id"] == "new-assistant"
    finally:
        service.shutdown()


def test_worker_find_or_resume_preserves_bundle_when_no_new_bundle_is_provided(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime(), max_workers=2)
    try:
        project = store.create_project("owner", "Host Workers", "Reuse named host workers.", "codex-cli")
        original = service.find_or_create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Codex Host",
            role="browser worker",
            profile="codex-cli",
            backend="openclaw",
            alias="codex-main",
            execution_mode="host",
            workspace_root=str(tmp_path / "workspaces"),
            bootstrap_bundle={
                "mcp_config": {"servers": {"safe-tool": {"url": "http://safe-tool.local/mcp"}}},
                "callbacks": {"conversation_id": "existing-conversation"},
            },
        )

        resumed = service.find_or_create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Codex Host",
            role="browser worker",
            profile="codex-cli",
            backend="openclaw",
            alias="codex-main",
            execution_mode="host",
            workspace_root=str(tmp_path / "workspaces"),
            bootstrap_bundle=None,
        )

        assert resumed["worker_id"] == original["worker_id"]
        stored_bundle = json.loads(store.get_worker(resumed["worker_id"])["bootstrap_bundle_json"])
        assert stored_bundle["mcp_config"]["servers"]["safe-tool"]["url"] == "http://safe-tool.local/mcp"
        assert stored_bundle["callbacks"]["conversation_id"] == "existing-conversation"
    finally:
        service.shutdown()


def test_host_worker_disabled_blocks_host_creation_and_run(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "false")
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))
    project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Host Workers",
            "goal": "Verify disabled host execution is enforced.",
            "default_worker_profile": "codex-cli",
        },
    ).json()

    host_payload = {
        "owner_id": "demo-owner",
        "name": "Codex Host",
        "role": "coding",
        "profile": "codex-cli",
        "backend": "openclaw",
        "execution_mode": "host",
    }
    blocked = client.post(f"/v1/projects/{project['project_id']}/workers", json=host_payload)
    assert blocked.status_code == 403
    assert "host-native workers are disabled" in blocked.json()["detail"]

    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "true")
    created = client.post(f"/v1/projects/{project['project_id']}/workers", json=host_payload)
    assert created.status_code == 201
    worker_id = created.json()["worker_id"]

    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "false")
    run = client.post(f"/v1/workers/{worker_id}/assign", json={"instruction": "Open Chrome"})
    assert run.status_code == 403
    assert "host-native workers are disabled" in run.json()["detail"]


def test_duplicate_worker_copies_workspace_into_new_worker(tmp_path):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))

    source_project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Source Workspace",
            "goal": "Provide a reusable workspace to duplicate.",
            "default_worker_profile": "codex-cli",
        },
    ).json()
    source_worker = client.post(
        f"/v1/projects/{source_project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Main Workspace",
            "role": "main",
            "profile": "codex-cli",
            "backend": "openclaw",
            "bootstrap_profile": "host-login",
            "bootstrap_bundle": {
                "files": [
                    {
                        "scope": "workspace",
                        "path": "notes/from-bootstrap.txt",
                        "content": "seeded",
                    }
                ]
            },
        },
    ).json()

    source_workspace = Path(source_worker["workspace_dir"])
    source_workspace.mkdir(parents=True, exist_ok=True)
    (source_workspace / "app.txt").write_text("copied from source")
    (source_workspace / ".mcp.json").write_text('{"seed":"source"}')

    target_project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Duplicate Workspace",
            "goal": "Create a duplicated workspace",
            "default_worker_profile": "codex-cli",
        },
    ).json()

    duplicate = client.post(
        f"/v1/projects/{target_project['project_id']}/workers/duplicate",
        json={
            "owner_id": "demo-owner",
            "source_worker_id": source_worker["worker_id"],
            "name": "Main Workspace",
            "role": "main",
        },
    )
    assert duplicate.status_code == 201
    duplicated_worker = duplicate.json()
    duplicated_workspace = Path(duplicated_worker["workspace_dir"])

    assert (duplicated_workspace / "app.txt").read_text() == "copied from source"
    assert (duplicated_workspace / ".mcp.json").read_text() == '{"seed":"source"}'

    events = client.get(f"/v1/workers/{duplicated_worker['worker_id']}/events")
    assert events.status_code == 200
    assert any(item["event_type"] == "worker.duplicated" for item in events.json()["items"])


def test_duplicate_worker_does_not_copy_home_directory(tmp_path):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))

    source_project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Source Workspace",
            "goal": "Provide a reusable workspace to duplicate.",
            "default_worker_profile": "codex-cli",
        },
    ).json()
    source_worker = client.post(
        f"/v1/projects/{source_project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Main Workspace",
            "role": "main",
            "profile": "codex-cli",
            "backend": "openclaw",
        },
    ).json()

    source_workspace = Path(source_worker["workspace_dir"])
    source_home = source_workspace.parent / "home"
    source_home.mkdir(parents=True, exist_ok=True)
    source_workspace.mkdir(parents=True, exist_ok=True)
    (source_home / ".qa-home-marker").write_text("home-only")
    (source_workspace / "qa_workspace_marker.txt").write_text("workspace-only")

    target_project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Duplicate Workspace",
            "goal": "Create a duplicated workspace",
            "default_worker_profile": "codex-cli",
        },
    ).json()

    duplicate = client.post(
        f"/v1/projects/{target_project['project_id']}/workers/duplicate",
        json={
            "owner_id": "demo-owner",
            "source_worker_id": source_worker["worker_id"],
            "name": "Main Workspace",
            "role": "main",
        },
    )
    assert duplicate.status_code == 201
    duplicated_worker = duplicate.json()
    duplicated_workspace = Path(duplicated_worker["workspace_dir"])
    duplicated_home = duplicated_workspace.parent / "home"

    assert (duplicated_workspace / "qa_workspace_marker.txt").read_text() == "workspace-only"
    assert not (duplicated_home / ".qa-home-marker").exists()


def test_assign_run_on_paused_worker_resumes_before_queueing(tmp_path):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))

    project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Resume Workspace",
            "goal": "Resume the paused workspace before queueing a new run.",
            "default_worker_profile": "codex-cli",
        },
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Main Workspace",
            "role": "main",
            "profile": "codex-cli",
            "backend": "openclaw",
        },
    ).json()

    pause_resp = client.post(f"/v1/workers/{worker['worker_id']}/pause")
    assert pause_resp.status_code == 202
    assert pause_resp.json()["state"] == "paused"

    assign_resp = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        json={"instruction": "Resume this paused workspace and start a fresh run."},
    )
    assert assign_resp.status_code == 202

    events = client.get(f"/v1/workers/{worker['worker_id']}/events")
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "worker.paused" in event_types
    assert "worker.resumed" in event_types
    assert "run.queued" in event_types


def test_nonblocking_worker_create_defers_runtime_start_to_run_queue(tmp_path):
    class SlowReadyRuntime(StubRuntime):
        def __init__(self) -> None:
            self.ready_started = Event()
            self.ready_calls = 0

        def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
            self.ready_started.set()
            self.ready_calls += 1
            time.sleep(0.75)
            return super().ensure_worker_ready(worker)

        def run_task(
            self,
            worker: dict,
            instruction: str,
            timeout_sec: float | None = None,
            run_id: str | None = None,
        ) -> str:
            _ = instruction, timeout_sec, run_id
            self.ensure_worker_ready(worker)
            return "BACKGROUND_START_OK"

    db_path = tmp_path / "runtime.db"
    runtime = SlowReadyRuntime()
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=runtime))

    project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Background Workspace",
            "goal": "Queue work without blocking on runtime preparation.",
            "default_worker_profile": "codex-cli",
        },
    ).json()

    start = time.monotonic()
    worker_resp = client.post(
        f"/v1/projects/{project['project_id']}/workers/find-or-resume",
        json={
            "owner_id": "demo-owner",
            "name": "Main Workspace",
            "role": "main",
            "alias": "main",
            "profile": "codex-cli",
            "backend": "openclaw",
            "start_synchronously": False,
        },
    )
    elapsed = time.monotonic() - start

    assert worker_resp.status_code == 200
    worker = worker_resp.json()
    assert elapsed < 0.25
    assert worker["state"] == "paused"
    assert runtime.ready_calls == 0

    start = time.monotonic()
    assign_resp = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        json={"instruction": "Run after background preparation."},
    )
    assert assign_resp.status_code == 202
    assert time.monotonic() - start < 0.25

    assert runtime.ready_started.wait(1.0)
    settled = wait_for_run(client, assign_resp.json()["run_id"], timeout=3.0)
    assert settled["state"] == "completed"
    assert settled["output_text"] == "BACKGROUND_START_OK"


def test_openclaw_worker_exposes_operator_control_surface(tmp_path):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))

    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Control Surface", "goal": "View and control worker progress."},
    ).json()

    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Claude Worker",
            "role": "coder",
            "profile": "openclaw-claude",
            "backend": "openclaw",
        },
    ).json()

    takeover = client.get(f"/v1/workers/{worker['worker_id']}/takeover")
    assert takeover.status_code == 200
    data = takeover.json()
    assert data["supported"] is True
    assert data["mode"] == "web-terminal"
    assert data["url"].endswith(f"/ui/workers/{worker['worker_id']}/terminal")

    worker_ui = client.get(f"/ui/workers/{worker['worker_id']}")
    assert worker_ui.status_code == 200
    assert "Claude Worker" in worker_ui.text
    assert worker["session_key"] in worker_ui.text
    assert "Queue task" in worker_ui.text
    assert "Take over terminal" in worker_ui.text

    terminal_ui = client.get(f"/ui/workers/{worker['worker_id']}/terminal")
    assert terminal_ui.status_code == 200
    assert "Connecting to worker terminal" in terminal_ui.text
    assert f"const workerId = '{worker['worker_id']}'" in terminal_ui.text
    assert 'target="_top">Back to project workspace</a>' in terminal_ui.text
    assert 'target="_top">Worker console</a>' in terminal_ui.text


def test_project_workspace_ui_supports_simple_run_flow(tmp_path):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))

    project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Simple Flow",
            "goal": "Keep the operator path easy: prompt, worker, run, control.",
            "default_worker_profile": "openclaw-general",
        },
    ).json()

    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Primary Worker",
            "role": "research",
            "profile": "openclaw-general",
            "backend": "openclaw",
        },
    ).json()

    home = client.get("/ui")
    assert home.status_code == 200
    assert "Open project workspace" in home.text

    project_ui = client.get(f"/ui/projects/{project['project_id']}")
    assert project_ui.status_code == 200
    assert "Run Project" in project_ui.text
    assert "Create worker only" in project_ui.text
    assert "Selected Worker" in project_ui.text
    assert worker["worker_id"] in project_ui.text
    assert "Take over terminal" in project_ui.text


def test_live_payload_promotes_workspace_html_as_deliverable(tmp_path):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))

    project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Deliverable Detection",
            "goal": "Expose a presentable page result to the operator UI.",
            "default_worker_profile": "codex-cli",
        },
    ).json()

    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Page Worker",
            "role": "builder",
            "profile": "codex-cli",
            "backend": "openclaw",
        },
    ).json()

    workspace = Path(worker["workspace_dir"])
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "index.html").write_text("<!doctype html><h1>HELLO WORLD</h1>")

    live = client.get(f"/v1/workers/{worker['worker_id']}/live")
    assert live.status_code == 200
    payload = live.json()
    assert payload["deliverable"]["kind"] == "webpage"
    assert payload["deliverable"]["source"] == "workspace_html"
    assert payload["deliverable"]["browser_url"] == "file:///workspace/project/index.html"
    assert payload["deliverable"]["preferred_surface"] == "desktop"


def test_deliverable_detection_ignores_provider_api_endpoints(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "qa_enterprise_launcher_ui_pass.txt").write_text("QA_ENTERPRISE_LAUNCHER_UI_PASS")
    worker = {
        "worker_id": "wrk_1",
        "workspace_dir": str(workspace),
        "execution_mode": "docker",
    }
    output = (
        "Using https://pai-aaf.cognitiveservices.azure.com/openai/v1 for the model.\n"
        "FINAL REPORT: wrote the marker file."
    )

    assert not is_deliverable_url("https://pai-aaf.cognitiveservices.azure.com/openai/v1")
    assert not is_deliverable_url("https://[REDACTED_CREDENTIAL]/openai/v1")
    payload = deliverable_payload(worker, {"state": "completed"}, output)

    assert payload is not None
    assert payload["kind"] == "file"
    assert payload["source"] == "workspace_file"
    assert payload["workspace_path"] == "qa_enterprise_launcher_ui_pass.txt"
    assert "cognitiveservices" not in json.dumps(payload)


def test_deliverable_detection_ignores_glasshive_scaffold_files(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "glasshive_post_restart_smoke_20260523.txt").write_text("GH_POST_RESTART_OK")
    for name in (
        "AGENTS.md",
        "CLAUDE.md",
        "CODEX.md",
        "project-definition.md",
        "work-log.md",
    ):
        scaffold = workspace / name
        scaffold.write_text("# scaffold")
        os.utime(scaffold, (9999999999, 9999999999))
    helper_dir = workspace / "glasshive-host-tools"
    helper_dir.mkdir()
    helper = helper_dir / "capture-front-window.sh"
    helper.write_text("#!/usr/bin/env bash\n")
    os.utime(helper, (9999999999, 9999999999))
    mixed_case_helper_dir = workspace / "Node_Modules"
    mixed_case_helper_dir.mkdir()
    mixed_case_helper = mixed_case_helper_dir / "latest-helper-output.txt"
    mixed_case_helper.write_text("not a user artifact")
    os.utime(mixed_case_helper, (9999999999, 9999999999))
    worker = {
        "worker_id": "wrk_1",
        "workspace_dir": str(workspace),
        "execution_mode": "host",
    }

    payload = deliverable_payload(worker, {"state": "completed"}, "FINAL REPORT: wrote the smoke marker file.")

    assert payload is not None
    assert payload["kind"] == "file"
    assert payload["source"] == "workspace_file"
    assert payload["workspace_path"] == "glasshive_post_restart_smoke_20260523.txt"


class ControllableRuntime:
    def __init__(self) -> None:
        self.running = Event()
        self.release = Event()
        self.interrupted = Event()
        self.paused = Event()
        self.interrupt_run_ids: list[str | None] = []

    def resolve_model(self, profile: str) -> str:
        return "controllable/test"

    def _info(self, worker: dict, pid: int | None = 1234) -> RuntimeInfo:
        return RuntimeInfo(
            runtime="controllable",
            model=worker.get("model") or self.resolve_model(worker.get("profile", "")),
            gateway_url="",
            gateway_port=None,
            gateway_token=None,
            session_key=worker.get("session_key") or f"controllable:{worker['worker_id']}",
            state_dir="/tmp/controllable/state",
            workspace_dir="/tmp/controllable/workspace",
            pid=pid,
        )

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        self.paused.clear()
        return self._info(worker)

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        self.paused.set()
        return self._info(worker, pid=None)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        self.interrupt_run_ids.append(run_id)
        self.interrupted.set()
        return self._info(worker)

    def terminate_worker(self, worker: dict) -> RuntimeInfo:
        self.interrupted.set()
        return self._info(worker, pid=None)

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker, pid=None if self.paused.is_set() else 1234)

    def run_task(self, worker: dict, instruction: str, timeout_sec: float | None = None) -> str:
        self.running.set()
        deadline = time.time() + 5
        while time.time() < deadline:
            if self.interrupted.is_set():
                raise WorkerInterruptedError("Worker run was interrupted by the operator")
            if self.paused.is_set():
                time.sleep(0.05)
                continue
            if self.release.is_set():
                return "CONTROLLABLE_OK"
            time.sleep(0.05)
        raise AssertionError("ControllableRuntime timed out in test")


class SteerableControllableRuntime(ControllableRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.instructions: list[str] = []

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        info = super().ensure_worker_ready(worker)
        if self.instructions:
            self.interrupted.clear()
        return info

    def run_task(self, worker: dict, instruction: str, timeout_sec: float | None = None) -> str:
        self.instructions.append(instruction)
        if instruction.startswith("Operator steer instruction"):
            return "STEER_REDIRECT_OK"
        return super().run_task(worker, instruction, timeout_sec=timeout_sec)


class RefreshingModelRuntime(ControllableRuntime):
    def __init__(self, model: str) -> None:
        super().__init__()
        self.model = model
        self.ensure_models: list[str] = []

    def resolve_model(self, profile: str) -> str:
        return self.model

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        self.ensure_models.append(str(worker.get("model") or ""))
        return self._info(worker)


def test_assign_run_refreshes_stale_worker_model_before_queue(tmp_path):
    db_path = tmp_path / "runtime.db"
    store = Store(str(db_path))
    runtime = RefreshingModelRuntime("gpt-5.2-chat")
    service = WorkersProjectsService(store, runtime)
    service._ensure_worker_processor = lambda worker_id: None  # type: ignore[method-assign]

    project = service.create_project("demo-owner", "Refresh Model", "Refresh stale worker models.", "codex-cli")
    worker = service.create_worker(
        project_id=project["project_id"],
        owner_id="demo-owner",
        name="Model Worker",
        role="coder",
        profile="codex-cli",
        backend="openclaw",
        start_synchronously=False,
    )
    assert worker["model"] == "gpt-5.2-chat"

    runtime.model = "gpt-5.4"
    run = service.assign_run(worker["worker_id"], "Use the current configured model.")
    refreshed = store.get_worker(worker["worker_id"])

    assert run["state"] == "queued"
    assert refreshed["model"] == "gpt-5.4"
    assert refreshed["state"] == "starting"
    assert any(event["event_type"] == "worker.model_refreshed" for event in store.list_events(worker["worker_id"]))


def test_resume_worker_refreshes_stale_worker_model_before_runtime_start(tmp_path):
    db_path = tmp_path / "runtime.db"
    store = Store(str(db_path))
    runtime = RefreshingModelRuntime("gpt-5.2-chat")
    service = WorkersProjectsService(store, runtime)
    service._ensure_worker_processor = lambda worker_id: None  # type: ignore[method-assign]

    project = service.create_project("demo-owner", "Resume Model", "Resume with current model.", "codex-cli")
    worker = service.create_worker(
        project_id=project["project_id"],
        owner_id="demo-owner",
        name="Resume Worker",
        role="coder",
        profile="codex-cli",
        backend="openclaw",
        start_synchronously=False,
    )

    runtime.model = "gpt-5.4"
    resumed = service.resume_worker(worker["worker_id"])

    assert resumed["model"] == "gpt-5.4"
    assert runtime.ensure_models[-1] == "gpt-5.4"


def test_pause_resume_freezes_active_run_without_losing_it(tmp_path):
    db_path = tmp_path / "runtime.db"
    runtime = ControllableRuntime()
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=runtime))

    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Pause Resume", "goal": "Freeze and resume an active worker run."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Controllable Worker", "role": "coder"},
    ).json()

    run = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        json={"instruction": "Do a long running task."},
    ).json()
    assert runtime.running.wait(timeout=2), "worker run never started"

    paused = client.post(f"/v1/workers/{worker['worker_id']}/pause")
    assert paused.status_code == 202
    assert paused.json()["state"] == "paused"

    run_during_pause = client.get(f"/v1/runs/{run['run_id']}").json()
    assert run_during_pause["state"] == "running"

    resumed = client.post(f"/v1/workers/{worker['worker_id']}/resume")
    assert resumed.status_code == 202
    assert resumed.json()["state"] == "running"

    runtime.release.set()
    settled = wait_for_run(client, run["run_id"], timeout=3.0)
    assert settled["state"] == "completed"
    assert settled["output_text"] == "CONTROLLABLE_OK"


class RaisingPauseRuntime:
    def __init__(self) -> None:
        self.running = Event()
        self.paused = Event()

    def resolve_model(self, profile: str) -> str:
        return "pause-raising/test"

    def _info(self, worker: dict, pid: int | None = 2222) -> RuntimeInfo:
        return RuntimeInfo(
            runtime="pause-raising",
            model=worker.get("model") or self.resolve_model(worker.get("profile", "")),
            gateway_url="",
            gateway_port=None,
            gateway_token=None,
            session_key=worker.get("session_key") or f"pause-raising:{worker['worker_id']}",
            state_dir="/tmp/pause-raising/state",
            workspace_dir="/tmp/pause-raising/workspace",
            pid=pid,
        )

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        self.paused.clear()
        return self._info(worker)

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        self.paused.set()
        return self._info(worker, pid=None)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        return self._info(worker)

    def terminate_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker, pid=None)

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker, pid=None if self.paused.is_set() else 2222)

    def run_task(self, worker: dict, instruction: str, timeout_sec: float | None = None) -> str:
        self.running.set()
        deadline = time.time() + 5
        while time.time() < deadline:
            if self.paused.is_set():
                raise WorkerPausedError("Worker was paused while a run was active")
            time.sleep(0.05)
        raise AssertionError("RaisingPauseRuntime timed out in test")


def test_worker_paused_error_finalizes_run_as_paused(tmp_path):
    db_path = tmp_path / "runtime.db"
    runtime = RaisingPauseRuntime()
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=runtime))

    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Pause Finalize", "goal": "Finalize paused runs cleanly."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Pause Finalize Worker", "role": "coder"},
    ).json()

    run = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        json={"instruction": "Begin a run that will raise WorkerPausedError when paused."},
    ).json()
    assert runtime.running.wait(timeout=2), "worker run never started"

    paused = client.post(f"/v1/workers/{worker['worker_id']}/pause")
    assert paused.status_code == 202
    assert paused.json()["state"] == "paused"

    deadline = time.time() + 3.0
    settled = None
    while time.time() < deadline:
        response = client.get(f"/v1/runs/{run['run_id']}")
        assert response.status_code == 200
        candidate = response.json()
        if candidate["state"] == "paused":
            settled = candidate
            break
        time.sleep(0.05)

    assert settled is not None, "run did not settle into paused state"
    assert settled["state"] == "paused"
    assert "paused while a run was active" in settled["error_text"]


def test_interrupt_stops_active_run_and_keeps_worker_ready(tmp_path):
    db_path = tmp_path / "runtime.db"
    runtime = ControllableRuntime()
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=runtime))

    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Interrupt", "goal": "Stop the current worker task cleanly."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Interrupt Worker", "role": "coder"},
    ).json()

    run = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        json={"instruction": "Long task to be interrupted."},
    ).json()
    assert runtime.running.wait(timeout=2), "worker run never started"

    interrupted = client.post(f"/v1/workers/{worker['worker_id']}/interrupt")
    assert interrupted.status_code == 202
    assert interrupted.json()["state"] == "ready"
    assert runtime.interrupt_run_ids == [run["run_id"]]

    settled = wait_for_run(client, run["run_id"], timeout=3.0)
    assert settled["state"] == "interrupted"

    worker_after = client.get(f"/v1/workers/{worker['worker_id']}").json()
    assert worker_after["state"] == "ready"


def test_steer_interrupts_active_run_and_redirects_to_new_instruction(tmp_path):
    db_path = tmp_path / "runtime.db"
    runtime = SteerableControllableRuntime()
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=runtime))

    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Steer Redirect", "goal": "Redirect an active run immediately."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Steer Worker", "role": "coder"},
    ).json()

    first_run = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        json={"instruction": "Do the original long-running task."},
    ).json()
    assert runtime.running.wait(timeout=2), "worker run never started"

    steer_resp = client.post(
        f"/v1/workers/{worker['worker_id']}/steer",
        json={"message": "Switch immediately to the new operator direction."},
    )
    assert steer_resp.status_code == 202
    steer_run = steer_resp.json()
    assert steer_run["state"] == "queued"
    assert runtime.interrupt_run_ids == [first_run["run_id"]]

    interrupted = wait_for_run(client, first_run["run_id"], timeout=3.0)
    assert interrupted["state"] == "interrupted"
    redirected = wait_for_run(client, steer_run["run_id"], timeout=3.0)
    assert redirected["state"] == "completed"
    assert redirected["output_text"] == "STEER_REDIRECT_OK"

    events = client.get(f"/v1/workers/{worker['worker_id']}/events").json()["items"]
    event_types = [event["event_type"] for event in events]
    assert "worker.interrupted" in event_types
    assert "worker.steer" in event_types
    assert runtime.instructions[1].startswith("Operator steer instruction")
    assert "Do not stop at an acknowledgement" in runtime.instructions[1]


class HealRecoveryRuntime:
    def __init__(self) -> None:
        self.collect_run_ids: list[str | None] = []

    def resolve_model(self, profile: str) -> str:
        return "heal-recovery/test"

    def _info(self, worker: dict, pid: int | None = 4242) -> RuntimeInfo:
        return RuntimeInfo(
            runtime="heal-recovery",
            model=worker.get("model") or self.resolve_model(worker.get("profile", "")),
            gateway_url="",
            gateway_port=None,
            gateway_token=None,
            session_key=worker.get("session_key") or f"heal-recovery:{worker['worker_id']}",
            state_dir="/tmp/heal-recovery/state",
            workspace_dir="/tmp/heal-recovery/workspace",
            pid=pid,
        )

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        return self._info(worker)

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker, pid=None)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        return self._info(worker)

    def terminate_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker, pid=None)

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker)

    def collect_completed_run(self, worker: dict, run_id: str | None = None) -> dict[str, str] | None:
        self.collect_run_ids.append(run_id)
        return {
            "state": "completed",
            "output_text": "HEAL_COMPLETED_OK",
            "error_text": "",
        }


def test_heal_worker_restarts_processor_when_queued_runs_remain(tmp_path):
    db_path = tmp_path / "runtime.db"
    store = Store(str(db_path))
    runtime = HealRecoveryRuntime()
    service = WorkersProjectsService(store, runtime)

    project = service.create_project("demo-owner", "Heal Queue", "Recover completion and continue queued runs.", "codex-cli")
    worker = service.create_worker(
        project_id=project["project_id"],
        owner_id="demo-owner",
        name="Heal Queue Worker",
        role="coder",
        profile="codex-cli",
        backend="openclaw",
    )

    running = store.create_run(worker["worker_id"], project["project_id"], "original run", state="running")
    queued = store.create_run(worker["worker_id"], project["project_id"], "queued follow-up", state="queued")
    store.update_worker(worker["worker_id"], state="running")

    restart_requests: list[str] = []
    service._ensure_worker_processor = lambda worker_id: restart_requests.append(worker_id)  # type: ignore[method-assign]
    service._active_processors.add(worker["worker_id"])

    healed = service.heal_worker(worker["worker_id"])

    assert healed is not None
    assert store.get_run(running["run_id"])["state"] == "completed"
    assert store.get_run(queued["run_id"])["state"] == "queued"
    assert restart_requests == [worker["worker_id"]]
    assert runtime.collect_run_ids == [running["run_id"]]


def test_heal_worker_repairs_starting_worker_without_active_run(tmp_path):
    class ReconcileReadyRuntime(StubRuntime):
        def __init__(self):
            super().__init__()
            self.reconciled: list[str] = []

        def reconcile_worker(self, worker: dict) -> RuntimeInfo:
            self.reconciled.append(worker["worker_id"])
            return RuntimeInfo(
                runtime="openclaw",
                model="test-model",
                gateway_url="",
                gateway_port=None,
                gateway_token=None,
                session_key="session",
                state_dir="/tmp/state",
                workspace_dir="/tmp/workspace",
                pid=1234,
            )

    db_path = tmp_path / "runtime.db"
    store = Store(str(db_path))
    runtime = ReconcileReadyRuntime()
    service = WorkersProjectsService(store, runtime)

    project = service.create_project("demo-owner", "Heal No Active Run", "Repair stale starting state.", "codex-cli")
    worker = service.create_worker(
        project_id=project["project_id"],
        owner_id="demo-owner",
        name="Stale Worker",
        role="coder",
        profile="codex-cli",
        backend="openclaw",
    )
    store.update_worker(worker["worker_id"], state="starting", pid=None)

    healed = service.heal_worker(worker["worker_id"])

    assert healed is not None
    assert healed["state"] == "ready"
    assert healed["pid"] == 1234
    assert runtime.reconciled == [worker["worker_id"]]


class HealingRaceRuntime:
    def __init__(self) -> None:
        self.initial_started = Event()
        self.release_initial = Event()
        self.queued_started = Event()
        self.release_queued = Event()
        self.collect_run_ids: list[str | None] = []

    def resolve_model(self, profile: str) -> str:
        return "healing-race/test"

    def _info(self, worker: dict, pid: int | None = 4242) -> RuntimeInfo:
        return RuntimeInfo(
            runtime="healing-race",
            model=worker.get("model") or self.resolve_model(worker.get("profile", "")),
            gateway_url="",
            gateway_port=None,
            gateway_token=None,
            session_key=f"healing-race:{worker['worker_id']}",
            state_dir=f"/tmp/{worker['worker_id']}/state",
            workspace_dir=f"/tmp/{worker['worker_id']}/workspace",
            pid=pid,
        )

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        return self._info(worker)

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker, pid=None)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        return self._info(worker)

    def terminate_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker, pid=None)

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker)

    def collect_completed_run(self, worker: dict, run_id: str | None = None) -> dict[str, str] | None:
        self.collect_run_ids.append(run_id)
        return {
            "state": "completed",
            "output_text": "HEAL_RECOVERED_INITIAL",
            "error_text": "",
        }

    def run_task(self, worker: dict, instruction: str, timeout_sec: float | None = None, run_id: str | None = None) -> str:
        if "initial" in instruction:
            self.initial_started.set()
            assert self.release_initial.wait(timeout=3)
            return "INITIAL_RETURNED_LATE"
        self.queued_started.set()
        assert self.release_queued.wait(timeout=3)
        return "QUEUED_COMPLETED_OK"


def test_heal_worker_replacement_processor_keeps_running_state_while_follow_up_executes(tmp_path):
    db_path = tmp_path / "runtime.db"
    store = Store(str(db_path))
    runtime = HealingRaceRuntime()
    service = WorkersProjectsService(store, runtime)

    project = service.create_project("demo-owner", "Queue Race", "Ensure healed processors cannot overwrite a replacement run.", "codex-cli")
    worker = service.create_worker(
        project_id=project["project_id"],
        owner_id="demo-owner",
        name="Queue Race Worker",
        role="coder",
        profile="codex-cli",
        backend="openclaw",
    )

    initial = service.assign_run(worker["worker_id"], "initial run that will be healed")
    assert runtime.initial_started.wait(timeout=2)

    queued = service.assign_run(worker["worker_id"], "queued follow-up that must keep running")
    healed = service.heal_worker(worker["worker_id"])
    assert healed is not None
    assert runtime.collect_run_ids == [initial["run_id"]]
    assert runtime.queued_started.wait(timeout=2)

    runtime.release_initial.set()

    deadline = time.time() + 2
    while time.time() < deadline:
        refreshed_worker = store.get_worker(worker["worker_id"])
        active_run = store.get_active_run(worker["worker_id"])
        if refreshed_worker and refreshed_worker["state"] == "running" and active_run and active_run["run_id"] == queued["run_id"]:
            break
        time.sleep(0.05)
    else:
        raise AssertionError("Replacement processor did not keep the queued follow-up marked as running")

    runtime.release_queued.set()

    deadline = time.time() + 2
    while time.time() < deadline:
        queued_run = store.get_run(queued["run_id"])
        refreshed_worker = store.get_worker(worker["worker_id"])
        if queued_run and queued_run["state"] == "completed" and refreshed_worker and refreshed_worker["state"] == "ready":
            break
        time.sleep(0.05)
    else:
        raise AssertionError("Queued follow-up did not complete cleanly")

    assert store.get_run(initial["run_id"])["state"] == "completed"
    assert store.get_run(queued["run_id"])["output_text"] == "QUEUED_COMPLETED_OK"
    assert store.get_worker(worker["worker_id"])["last_run_id"] == queued["run_id"]


class TerminatedButCompletedRuntime(StubRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.collect_run_ids: list[str | None] = []

    def run_task(self, worker: dict, instruction: str, timeout_sec: float | None = None, run_id: str | None = None) -> str:
        raise WorkerTerminatedError("stale termination marker")

    def collect_completed_run(self, worker: dict, run_id: str | None = None) -> dict[str, str] | None:
        self.collect_run_ids.append(run_id)
        return {
            "state": "completed",
            "output_text": "Recovered final answer",
            "error_text": "",
        }


def test_worker_terminated_error_recovers_completed_artifacts(tmp_path):
    db_path = tmp_path / "runtime.db"
    store = Store(str(db_path))
    runtime = TerminatedButCompletedRuntime()
    service = WorkersProjectsService(store, runtime)
    try:
        project = service.create_project("demo-owner", "Recovered Run", "Recover stdout.", "codex-cli")
        worker = service.create_worker(
            project_id=project["project_id"],
            owner_id="demo-owner",
            name="Recovered Worker",
            role="coder",
            profile="codex-cli",
            backend="openclaw",
        )
        run = service.assign_run(worker["worker_id"], "finish despite stale marker")

        deadline = time.time() + 2
        while time.time() < deadline:
            refreshed = store.get_run(run["run_id"])
            if refreshed and refreshed["state"] == "completed":
                break
            time.sleep(0.05)
        else:
            raise AssertionError("Run did not recover completed artifacts")

        assert store.get_run(run["run_id"])["output_text"] == "Recovered final answer"
        assert store.get_worker(worker["worker_id"])["state"] == "ready"
        assert runtime.collect_run_ids == [run["run_id"]]
    finally:
        service.shutdown()


class DesktopStubRuntime:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.last_desktop_action: dict[str, object] | None = None

    def resolve_model(self, profile: str) -> str:
        return "desktop-stub/test"

    def _worker_paths(self, worker_id: str) -> tuple[Path, Path]:
        state_dir = self.root / worker_id / "state"
        workspace_dir = state_dir / "workspace"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        return state_dir, workspace_dir

    def _info(self, worker: dict, pid: int | None = 4242) -> RuntimeInfo:
        state_dir, workspace_dir = self._worker_paths(worker["worker_id"])
        return RuntimeInfo(
            runtime="desktop-stub",
            model=worker.get("model") or self.resolve_model(worker.get("profile", "")),
            gateway_url="",
            gateway_port=None,
            gateway_token=None,
            session_key=worker.get("session_key") or f"desktop:{worker['worker_id']}",
            state_dir=str(state_dir),
            workspace_dir=str(workspace_dir),
            pid=pid,
        )

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        return self._info(worker)

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker, pid=None)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        return self._info(worker)

    def terminate_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker, pid=None)

    def run_task(self, worker: dict, instruction: str, timeout_sec: float | None = None) -> str:
        return f"DESKTOP_OK: {instruction}"

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker)

    def describe_worker(self, worker: dict) -> dict[str, object]:
        _, workspace_dir = self._worker_paths(worker["worker_id"])
        return {
            "mode": "workstation-desktop",
            "runtime": "desktop-stub",
            "workspace_dir": str(workspace_dir),
            "state_dir": str(workspace_dir.parent),
            "container_name": f"wpr-{worker['worker_id']}",
            "view_url": "http://127.0.0.1:57906/?autoconnect=1",
        }

    def desktop_action(
        self,
        worker: dict,
        action: str,
        *,
        url: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, object]:
        self.last_desktop_action = {
            "worker_id": worker["worker_id"],
            "action": action,
            "url": url,
            "run_id": run_id,
        }
        return {
            "action": action,
            "status": "launched",
            "mode": "workstation-desktop",
            "url": "http://127.0.0.1:57906/?autoconnect=1",
            "view_url": "http://127.0.0.1:57906/?autoconnect=1",
            "notes": f"{action} launched",
        }


def test_desktop_action_refreshes_activity_before_idle_reaper(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_IDLE_TERMINATE_AFTER_S", "60")
    monkeypatch.setenv("GLASSHIVE_IDLE_REAPER_INTERVAL_S", "3600")
    store = Store(str(tmp_path / "runtime.db"))
    runtime = DesktopStubRuntime(tmp_path / "desktop")
    service = WorkersProjectsService(store, runtime)
    try:
        project = service.create_project("owner", "Desktop", "Refresh activity", "codex-cli")
        worker = service.create_worker(
            project_id=project["project_id"],
            owner_id="owner",
            name="Desktop Worker",
            role="operator",
            profile="codex-cli",
            backend="openclaw",
        )
        with store._connect() as conn:
            conn.execute(
                "UPDATE workers SET updated_at = ? WHERE worker_id = ?",
                ((datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat(), worker["worker_id"]),
            )

        service.desktop_action(worker["worker_id"], "terminal")

        refreshed = store.get_worker(worker["worker_id"])
        assert refreshed is not None
        assert refreshed["state"] == "ready"
        assert service.reap_idle_workers_once() == []
        assert runtime.last_desktop_action == {
            "worker_id": worker["worker_id"],
            "action": "terminal",
            "url": None,
            "run_id": None,
        }
    finally:
        service.shutdown()


class DeliverableDesktopRuntime(DesktopStubRuntime):
    def run_task(self, worker: dict, instruction: str, timeout_sec: float | None = None) -> str:
        workspace_dir = Path(str(worker["workspace_dir"]))
        workspace_dir.mkdir(parents=True, exist_ok=True)
        (workspace_dir / "index.html").write_text("<!doctype html><h1>Hello</h1>", encoding="utf-8")
        return f"FINAL REPORT:\nCreated the page for: {instruction}"


class UrlOnlyDesktopRuntime(DesktopStubRuntime):
    def run_task(self, worker: dict, instruction: str, timeout_sec: float | None = None) -> str:
        return "\n".join(
            [
                "FINAL REPORT:",
                "Preview is ready.",
                "Local preview: http://localhost:5173/private-preview",
                "External preview: https://example.com/public-preview",
            ]
        )


def test_completed_docker_run_opens_workspace_html_in_sandbox_browser_once(tmp_path):
    db_path = tmp_path / "runtime.db"
    runtime = DeliverableDesktopRuntime(tmp_path / "desktop")
    app = create_app(str(db_path), runtime_backend="stub", runtime=runtime)

    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            json={"owner_id": "demo-owner", "title": "Deliverable Promotion", "goal": "Open generated pages."},
        ).json()
        worker = client.post(
            f"/v1/projects/{project['project_id']}/workers",
            json={
                "owner_id": "demo-owner",
                "name": "Page Worker",
                "role": "builder",
                "profile": "codex-cli",
                "execution_mode": "docker",
            },
        ).json()
        run = client.post(
            f"/v1/workers/{worker['worker_id']}/assign",
            json={"instruction": "Create an index page."},
        ).json()

        completed = wait_for_run(client, run["run_id"])
        assert completed["state"] == "completed"
        wait_until(lambda: runtime.last_desktop_action is not None)

        assert runtime.last_desktop_action == {
            "worker_id": worker["worker_id"],
            "action": "browser",
            "url": "file:///workspace/project/index.html",
            "run_id": run["run_id"],
        }

        service = app.state.service
        refreshed_worker = service.require_worker(worker["worker_id"])
        deliverable = service._completion_deliverable(refreshed_worker, completed, completed["output_text"])
        service._promote_completed_deliverable(refreshed_worker, completed, deliverable)
        opened_events = [
            event
            for event in app.state.store.list_events(worker["worker_id"])
            if event["event_type"] == "deliverable.opened"
        ]
        assert len(opened_events) == 1


def test_completed_host_run_does_not_auto_open_real_desktop_browser(tmp_path):
    db_path = tmp_path / "runtime.db"
    runtime = DeliverableDesktopRuntime(tmp_path / "desktop")
    app = create_app(str(db_path), runtime_backend="stub", runtime=runtime)

    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            json={"owner_id": "demo-owner", "title": "Host Deliverable", "goal": "Do not auto-open host pages."},
        ).json()
        worker = client.post(
            f"/v1/projects/{project['project_id']}/workers",
            json={
                "owner_id": "demo-owner",
                "name": "Host Page Worker",
                "role": "builder",
                "profile": "codex-cli",
                "execution_mode": "host",
                "bootstrap_bundle": {
                    "callbacks": {
                        "events_webhook_url": "http://callback.local/glasshive",
                        "hmac_secret": "public-safe-callback-secret",
                        "user_id": "user-public-safe",
                        "conversation_id": "conv-public-safe",
                        "parent_message_id": "parent-public-safe",
                        "message_id": "assistant-public-safe",
                        "surface": "web",
                    }
                },
            },
        ).json()
        run = client.post(
            f"/v1/workers/{worker['worker_id']}/assign",
            json={"instruction": "Create an index page."},
        ).json()

        completed = wait_for_run(client, run["run_id"])
        assert completed["state"] == "completed"
        assert runtime.last_desktop_action is None

        def callback_payload() -> dict | None:
            with app.state.store._connect() as conn:
                row = conn.execute(
                    "SELECT payload_json FROM callback_outbox WHERE run_id = ? AND event_type = 'run.completed'",
                    (run["run_id"],),
                ).fetchone()
            return json.loads(row["payload_json"]) if row else None

        wait_until(lambda: callback_payload() is not None)
        payload = callback_payload()
        assert payload is not None
        assert payload["deliverable"]["kind"] == "webpage"
        assert payload["deliverable"]["source"] == "workspace_html"
        assert payload["deliverable"]["browser_url_available"] is False
        assert "browser_url" not in payload["deliverable"]
        assert "file://" not in json.dumps(payload)


def test_completed_host_run_url_output_does_not_auto_open_or_leak_urls(tmp_path):
    db_path = tmp_path / "runtime.db"
    runtime = UrlOnlyDesktopRuntime(tmp_path / "desktop")
    app = create_app(str(db_path), runtime_backend="stub", runtime=runtime)

    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            json={"owner_id": "demo-owner", "title": "Host URL Deliverable", "goal": "Do not auto-open host URLs."},
        ).json()
        worker = client.post(
            f"/v1/projects/{project['project_id']}/workers",
            json={
                "owner_id": "demo-owner",
                "name": "Host URL Worker",
                "role": "builder",
                "profile": "codex-cli",
                "execution_mode": "host",
            },
        ).json()
        run = client.post(
            f"/v1/workers/{worker['worker_id']}/assign",
            json={"instruction": "Print a local preview URL."},
        ).json()

        completed = wait_for_run(client, run["run_id"])
        assert completed["state"] == "completed"
        assert runtime.last_desktop_action is None

        service = app.state.service
        refreshed_worker = service.require_worker(worker["worker_id"])
        deliverable = service._completion_deliverable(refreshed_worker, completed, completed["output_text"])

        assert deliverable is not None
        assert deliverable["source"] == "run_url"
        assert deliverable["browser_url_available"] is False
        assert "browser_url" not in deliverable
        serialized = json.dumps(deliverable)
        assert "localhost" not in serialized
        assert "127.0.0.1" not in serialized
        assert "example.com" not in serialized


@pytest.mark.parametrize("surface", ["telegram", "voice"])
def test_completed_non_web_callback_omits_operator_url_but_keeps_deliverable(tmp_path, monkeypatch, surface):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "http://127.0.0.1:8780")
    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_BASE_DELAY_S", "0")
    db_path = tmp_path / "runtime.db"
    runtime = DeliverableDesktopRuntime(tmp_path / "desktop")
    app = create_app(str(db_path), runtime_backend="stub", runtime=runtime)

    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            json={"owner_id": "demo-owner", "title": "Non-web Callback", "goal": "Preserve safe callback metadata."},
        ).json()
        worker = client.post(
            f"/v1/projects/{project['project_id']}/workers",
            json={
                "owner_id": "demo-owner",
                "name": "Non-web Worker",
                "role": "builder",
                "profile": "codex-cli",
                "execution_mode": "docker",
                "bootstrap_bundle": {
                    "callbacks": {
                        "events_webhook_url": "http://callback.local/glasshive",
                        "hmac_secret": "public-safe-callback-secret",
                        "user_id": "user-public-safe",
                        "conversation_id": "conv-public-safe",
                        "parent_message_id": "parent-public-safe",
                        "message_id": "assistant-public-safe",
                        "surface": surface,
                    }
                },
            },
        ).json()
        run = client.post(
            f"/v1/workers/{worker['worker_id']}/assign",
            json={"instruction": "Create an index page."},
        ).json()
        completed = wait_for_run(client, run["run_id"])
        assert completed["state"] == "completed"

        def callback_payload() -> dict | None:
            with app.state.store._connect() as conn:
                row = conn.execute(
                    "SELECT payload_json FROM callback_outbox WHERE run_id = ? AND event_type = 'run.completed'",
                    (run["run_id"],),
                ).fetchone()
            return json.loads(row["payload_json"]) if row else None

        wait_until(lambda: callback_payload() is not None)
        payload = callback_payload()
        assert payload is not None
        assert payload["surface"] == surface
        assert "operator_url" not in payload
        assert "watch_url" not in payload
        assert payload["deliverable"]["kind"] == "webpage"
        assert payload["deliverable"]["browser_url"] == "file:///workspace/project/index.html"


def test_completed_web_callback_includes_operator_url_and_deliverable(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "http://127.0.0.1:8780")
    monkeypatch.setenv("GLASSHIVE_ARTIFACT_BASE_URL", "http://127.0.0.1:8780")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")
    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("GLASSHIVE_CALLBACK_RETRY_BASE_DELAY_S", "0")
    db_path = tmp_path / "runtime.db"
    runtime = DeliverableDesktopRuntime(tmp_path / "desktop")
    app = create_app(str(db_path), runtime_backend="stub", runtime=runtime)

    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            json={"owner_id": "demo-owner", "title": "Web Callback", "goal": "Surface operator metadata."},
        ).json()
        worker = client.post(
            f"/v1/projects/{project['project_id']}/workers",
            json={
                "owner_id": "demo-owner",
                "name": "Web Worker",
                "role": "builder",
                "profile": "codex-cli",
                "execution_mode": "docker",
                "bootstrap_bundle": {
                    "callbacks": {
                        "events_webhook_url": "http://callback.local/glasshive",
                        "hmac_secret": "public-safe-callback-secret",
                        "user_id": "user-public-safe",
                        "conversation_id": "conv-public-safe",
                        "parent_message_id": "parent-public-safe",
                        "message_id": "assistant-public-safe",
                        "surface": "web",
                    }
                },
            },
        ).json()
        run = client.post(
            f"/v1/workers/{worker['worker_id']}/assign",
            json={"instruction": "Create an index page."},
        ).json()
        completed = wait_for_run(client, run["run_id"])
        assert completed["state"] == "completed"

        def callback_payload() -> dict | None:
            with app.state.store._connect() as conn:
                row = conn.execute(
                    "SELECT payload_json FROM callback_outbox WHERE run_id = ? AND event_type = 'run.completed'",
                    (run["run_id"],),
                ).fetchone()
            return json.loads(row["payload_json"]) if row else None

        wait_until(lambda: callback_payload() is not None)
        payload = callback_payload()
        assert payload is not None
        expected_url = f"http://127.0.0.1:8780/watch/{worker['worker_id']}?surface=desktop&project_id={project['project_id']}"
        assert payload["operator_url"].startswith(expected_url)
        assert payload["watch_url"].startswith(expected_url)
        assert "gh_token=" in payload["operator_url"]
        assert "gh_token=" in payload["watch_url"]
        assert payload["deliverable"]["kind"] == "webpage"
        assert payload["deliverable"]["browser_url"] == "file:///workspace/project/index.html"
        assert "Download: [Download artifact](http://127.0.0.1:8780/v1/signed-links/" in payload["message"]
        assert "View / Steer: [Open GlassHive workspace]" in payload["message"]


def test_desktop_action_and_artifact_preview_surface_in_project_ui(tmp_path):
    db_path = tmp_path / "runtime.db"
    runtime = DesktopStubRuntime(tmp_path / "desktop")
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=runtime))

    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Desktop UX", "goal": "Expose workstation controls and artifact previews."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Desktop Worker", "role": "operator", "profile": "codex-cli"},
    ).json()

    workspace_dir = Path(worker["workspace_dir"])
    png_path = workspace_dir / "latest-proof.png"
    png_path.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360606060000000040001f61738550000000049454e44ae426082"
        )
    )

    action = client.post(
        f"/v1/workers/{worker['worker_id']}/desktop-action",
        json={"action": "codex", "run_id": "run_demo123"},
    )
    assert action.status_code == 202
    action_payload = action.json()
    assert action_payload["mode"] == "workstation-desktop"
    assert action_payload["status"] == "launched"
    assert action_payload["url"].startswith("http://127.0.0.1:57906/")
    assert runtime.last_desktop_action == {
        "worker_id": worker["worker_id"],
        "action": "codex",
        "url": None,
        "run_id": "run_demo123",
    }

    artifact = client.get(f"/v1/workers/{worker['worker_id']}/artifacts/latest-image")
    assert artifact.status_code == 200
    assert artifact.headers["content-type"] == "image/png"

    project_ui = client.get(f"/ui/projects/{project['project_id']}?worker_id={worker['worker_id']}")
    assert project_ui.status_code == 200
    assert "Workstation Tools" in project_ui.text
    assert "Open Codex" in project_ui.text
    assert "Latest Visual Artifact" in project_ui.text
    assert f"/v1/workers/{worker['worker_id']}/artifacts/latest-image" in project_ui.text


def test_launch_failed_endpoint_marks_worker_failed(tmp_path):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))

    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Launch Failure", "goal": "Record a failed launch clearly."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Launch Worker", "role": "operator", "profile": "codex-cli"},
    ).json()

    response = client.post(
        f"/v1/workers/{worker['worker_id']}/launch-failed",
        json={"reason": "assign failed during launch"},
    )
    assert response.status_code == 202
    payload = response.json()
    assert payload["state"] == "failed"
    assert payload["last_error"] == "assign failed during launch"

    events = client.get(f"/v1/workers/{worker['worker_id']}/events").json()["items"]
    assert any(event["event_type"] == "worker.launch_failed" for event in events)


def test_worker_metadata_favorite_round_trips(tmp_path):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))
    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Pinned Workspace", "goal": "Pin a reusable workspace."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Marketing Sandbox", "role": "operator", "profile": "codex-cli"},
    ).json()

    updated = client.patch(
        f"/v1/workers/{worker['worker_id']}",
        json={"favorite": True, "name": "Marketing Sandbox"},
    )

    assert updated.status_code == 200
    assert updated.json()["favorite"] is True
    assert updated.json()["name"] == "Marketing Sandbox"
    fetched = client.get(f"/v1/workers/{worker['worker_id']}").json()
    assert fetched["favorite"] is True


def test_async_worker_creation_parks_without_starting_compute(tmp_path):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))
    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Prepared Workspace", "goal": "Do not start compute until queued."},
    ).json()

    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Prepared",
            "role": "operator",
            "profile": "codex-cli",
            "start_synchronously": False,
        },
    )

    assert worker.status_code == 201
    assert worker.json()["state"] == "paused"
    events = client.get(f"/v1/workers/{worker.json()['worker_id']}/events").json()["items"]
    assert any(event["event_type"] == "worker.prepared" for event in events)


def test_native_schedule_queues_due_run(tmp_path):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))
    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Scheduled Workspace", "goal": "Queue work later."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Scheduler", "role": "operator", "profile": "codex-cli"},
    ).json()

    schedule = client.post(
        f"/v1/workers/{worker['worker_id']}/schedule",
        json={"instruction": "Write scheduled-proof.txt", "delay_seconds": 0, "schedule_text": "in 0 seconds"},
    )

    assert schedule.status_code == 202
    schedule_id = schedule.json()["schedule_id"]
    assert schedule.json()["state"] == "pending"

    processed = client.post("/v1/admin/schedules/run-due")

    assert processed.status_code == 200
    queued = client.get(f"/v1/schedules/{schedule_id}").json()
    assert queued["state"] == "queued"
    assert queued["queued_run_id"]
    run = wait_for_run(client, queued["queued_run_id"])
    assert run["state"] == "completed"
    assert "STUB_OK" in run["output_text"]
    done = client.get(f"/v1/schedules/{schedule_id}").json()
    assert done["state"] == "completed"
    assert done["queued_run_id"] == queued["queued_run_id"]


def test_worker_quota_enforced_per_user(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_MAX_WORKSPACES_PER_USER", "1")
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))
    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Quota", "goal": "Limit workspace count."},
    ).json()
    first = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "First", "role": "operator", "profile": "codex-cli"},
    )
    second = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Second", "role": "operator", "profile": "codex-cli"},
    )

    assert first.status_code == 201
    assert second.status_code == 429
    assert "GLASSHIVE_MAX_WORKSPACES_PER_USER=1" in second.json()["detail"]


def test_allowed_worker_profiles_guardrail_blocks_disallowed_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "codex-cli")
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))
    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Profile guardrail", "goal": "Limit worker profiles."},
    ).json()

    blocked = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Claude", "role": "operator", "profile": "claude-code"},
    )
    allowed = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Codex", "role": "operator", "profile": "codex-cli"},
    )

    assert blocked.status_code == 403
    assert "GLASSHIVE_ALLOWED_WORKER_PROFILES" in blocked.json()["detail"]
    assert allowed.status_code == 201
