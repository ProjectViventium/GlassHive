from __future__ import annotations

import os
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from workers_projects_runtime.api import create_app


pytestmark = pytest.mark.skipif(
    os.environ.get("WPR_RUN_CLI_LIVE_TESTS", "").strip().lower() not in {"1", "true", "yes", "on"},
    reason="Set WPR_RUN_CLI_LIVE_TESTS=1 to run live Codex/Claude worker tests",
)


def wait_for_run(client: TestClient, run_id: str, timeout: float = 300.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/v1/runs/{run_id}")
        assert response.status_code == 200
        run = response.json()
        if run["state"] in {"completed", "failed", "cancelled", "interrupted"}:
            return run
        time.sleep(1.0)
    raise AssertionError(f"Run {run_id} did not settle within {timeout}s")


def _create_project(client: TestClient, default_worker_profile: str) -> dict:
    return client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "CLI Workers Live",
            "goal": "Validate sandboxed CLI workers in the standalone runtime.",
            "default_worker_profile": default_worker_profile,
        },
    ).json()


def _claude_available() -> bool:
    env = dict(os.environ)
    if os.environ.get("WPR_CLAUDE_CODE_USE_API_KEY", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        env.pop("ANTHROPIC_API_KEY", None)
    try:
        result = subprocess.run(
            [
                "claude",
                "-p",
                "--output-format",
                "json",
                "Reply with exactly CLAUDE_PREFLIGHT_OK and no other text.",
            ],
            check=False,
            text=True,
            capture_output=True,
            timeout=90,
            env=env,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    text = f"{result.stdout}\n{result.stderr}"
    blockers = ("Not logged in", "Invalid API key", "Credit balance is too low")
    return not any(marker in text for marker in blockers)


def test_live_codex_worker_can_run_and_resume(tmp_path):
    db_path = tmp_path / "runtime-cli-live.db"
    client = TestClient(create_app(str(db_path), runtime_backend="openclaw"))

    project = _create_project(client, "codex-cli")

    codex_worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Codex Worker",
            "role": "coder",
            "profile": "codex-cli",
            "backend": "openclaw",
        },
    ).json()
    assert codex_worker["runtime"] == "codex-cli"

    codex_run = client.post(
        f"/v1/workers/{codex_worker['worker_id']}/assign",
        json={"instruction": "Reply with exactly CODEX_WORKER_OK and no other text."},
    ).json()
    codex_done = wait_for_run(client, codex_run["run_id"])
    assert codex_done["state"] == "completed", codex_done
    assert "CODEX_WORKER_OK" in codex_done["output_text"], codex_done["output_text"]

    codex_worker_after = client.get(f"/v1/workers/{codex_worker['worker_id']}").json()
    assert codex_worker_after["session_key"]
    assert not codex_worker_after["session_key"].startswith("codex-worker:"), codex_worker_after["session_key"]

    codex_resume_run = client.post(
        f"/v1/workers/{codex_worker['worker_id']}/message",
        json={"message": "Reply with exactly CODEX_RESUME_OK and no other text."},
    ).json()
    codex_resume_done = wait_for_run(client, codex_resume_run["run_id"])
    assert codex_resume_done["state"] == "completed", codex_resume_done
    assert "CODEX_RESUME_OK" in codex_resume_done["output_text"], codex_resume_done["output_text"]


def test_live_claude_worker_can_run_and_resume(tmp_path):
    if os.environ.get("WPR_RUN_CLAUDE_CODE_LIVE_TESTS", "").strip().lower() not in {"1", "true", "yes", "on"}:
        pytest.skip("Set WPR_RUN_CLAUDE_CODE_LIVE_TESTS=1 to run live Claude Code worker tests")
    if not _claude_available():
        pytest.skip("Claude Code auth or credits are not available for a live containerized run")

    db_path = tmp_path / "runtime-cli-live.db"
    client = TestClient(create_app(str(db_path), runtime_backend="openclaw"))
    project = _create_project(client, "claude-code")

    claude_worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Claude Worker",
            "role": "coder",
            "profile": "claude-code",
            "backend": "openclaw",
        },
    ).json()
    assert claude_worker["runtime"] == "claude-code"

    claude_run = client.post(
        f"/v1/workers/{claude_worker['worker_id']}/assign",
        json={"instruction": "Reply with exactly CLAUDE_WORKER_OK and no other text."},
    ).json()
    claude_done = wait_for_run(client, claude_run["run_id"])
    assert claude_done["state"] == "completed", claude_done
    assert "CLAUDE_WORKER_OK" in claude_done["output_text"], claude_done["output_text"]

    claude_resume_run = client.post(
        f"/v1/workers/{claude_worker['worker_id']}/message",
        json={"message": "Reply with exactly CLAUDE_RESUME_OK and no other text."},
    ).json()
    claude_resume_done = wait_for_run(client, claude_resume_run["run_id"])
    assert claude_resume_done["state"] == "completed", claude_resume_done
    assert "CLAUDE_RESUME_OK" in claude_resume_done["output_text"], claude_resume_done["output_text"]

    claude_worker_after = client.get(f"/v1/workers/{claude_worker['worker_id']}").json()
    assert claude_worker_after["session_key"]
