from __future__ import annotations

import os
from typing import Any

import httpx


class RuntimeClient:
    def __init__(self, base_url: str | None = None, timeout_sec: float = 120.0) -> None:
        self.base_url = (base_url or os.environ.get("GLASSHIVE_RUNTIME_BASE_URL", "http://127.0.0.1:8766")).rstrip("/")
        self.timeout_sec = timeout_sec

    def _request(self, method: str, path: str, *, json_body: dict[str, Any] | None = None) -> Any:
        with httpx.Client(timeout=self.timeout_sec) as client:
            response = client.request(method, f"{self.base_url}{path}", json=json_body)
            response.raise_for_status()
            return response.json()

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def list_projects(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/projects").get("items", [])

    def get_project(self, project_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/projects/{project_id}")

    def list_workers(self, project_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/v1/projects/{project_id}/workers").get("items", [])

    def get_worker(self, worker_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/workers/{worker_id}")

    def worker_live(self, worker_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/workers/{worker_id}/live")

    def create_project(self, owner_id: str, title: str, goal: str, default_worker_profile: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/projects",
            json_body={
                "owner_id": owner_id,
                "title": title,
                "goal": goal,
                "default_worker_profile": default_worker_profile,
            },
        )

    def create_worker(self, project_id: str, owner_id: str, profile: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/projects/{project_id}/workers",
            json_body={
                "owner_id": owner_id,
                "name": "Main Workspace",
                "role": "main",
                "profile": profile,
                "backend": "openclaw",
                "bootstrap_profile": {
                    "codex-cli": "codex-host",
                    "claude-code": "claude-host",
                    "openclaw-general": "host-login",
                }.get(profile, "host-login"),
            },
        )

    def duplicate_worker(self, project_id: str, source_worker_id: str, owner_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/projects/{project_id}/workers/duplicate",
            json_body={
                "owner_id": owner_id,
                "source_worker_id": source_worker_id,
                "name": "Main Workspace",
                "role": "main",
            },
        )

    def assign_run(self, worker_id: str, instruction: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/workers/{worker_id}/assign", json_body={"instruction": instruction})

    def launch_failed(self, worker_id: str, reason: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/workers/{worker_id}/launch-failed", json_body={"reason": reason})

    def message(self, worker_id: str, message: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/workers/{worker_id}/message", json_body={"message": message})

    def steer(self, worker_id: str, message: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/workers/{worker_id}/steer", json_body={"message": message})

    def lifecycle(self, worker_id: str, action: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/workers/{worker_id}/{action}")

    def desktop_action(
        self,
        worker_id: str,
        action: str,
        url: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"action": action}
        if url:
            payload["url"] = url
        if run_id:
            payload["run_id"] = run_id
        return self._request("POST", f"/v1/workers/{worker_id}/desktop-action", json_body=payload)
