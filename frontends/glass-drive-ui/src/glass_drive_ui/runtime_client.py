from __future__ import annotations

import os
from typing import Any

import httpx


class RuntimeClient:
    def __init__(
        self,
        base_url: str | None = None,
        timeout_sec: float = 120.0,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get("GLASSHIVE_RUNTIME_BASE_URL", "http://127.0.0.1:8766")).rstrip("/")
        self.timeout_sec = timeout_sec
        self.headers = dict(headers or {})

    def _request(self, method: str, path: str, *, json_body: dict[str, Any] | None = None) -> Any:
        with httpx.Client(timeout=self.timeout_sec) as client:
            response = client.request(
                method,
                f"{self.base_url}{path}",
                json=json_body,
                headers=self.headers or None,
            )
            response.raise_for_status()
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()

    def with_headers(self, headers: dict[str, str]) -> "RuntimeClient":
        merged = dict(self.headers)
        merged.update({key: value for key, value in headers.items() if value})
        return RuntimeClient(self.base_url, self.timeout_sec, merged)

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def list_projects(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/projects").get("items", [])

    def get_project(self, project_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/projects/{project_id}")

    def get_preferences(self) -> dict[str, Any]:
        return self._request("GET", "/v1/preferences")

    def update_preferences(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", "/v1/preferences", json_body=payload)

    def list_workers(self, project_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/v1/projects/{project_id}/workers").get("items", [])

    def get_worker(self, worker_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/workers/{worker_id}")

    def worker_live(self, worker_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/workers/{worker_id}/live")

    def record_worker_view_open(self, worker_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/workers/{worker_id}/view-opened")

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

    def create_worker(
        self,
        project_id: str,
        owner_id: str,
        profile: str,
        *,
        name: str = "Main Workspace",
        role: str = "main",
        bootstrap_bundle: dict[str, Any] | None = None,
        execution_mode: str = "docker",
        start_synchronously: bool = True,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/projects/{project_id}/workers",
            json_body={
                "owner_id": owner_id,
                "name": name,
                "role": role,
                "profile": profile,
                "execution_mode": execution_mode,
                "bootstrap_profile": {
                    "codex-cli": "codex-host",
                    "claude-code": "claude-host",
                    "openclaw-general": "host-login",
                }.get(profile, "host-login"),
                "bootstrap_bundle": bootstrap_bundle,
                "start_synchronously": start_synchronously,
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

    def schedule_run(
        self,
        worker_id: str,
        instruction: str,
        *,
        schedule_text: str | None = None,
        run_at: str | None = None,
        delay_seconds: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"instruction": instruction}
        if schedule_text:
            payload["schedule_text"] = schedule_text
        if run_at:
            payload["run_at"] = run_at
        if delay_seconds is not None:
            payload["delay_seconds"] = delay_seconds
        return self._request("POST", f"/v1/workers/{worker_id}/schedule", json_body=payload)

    def update_worker_metadata(self, worker_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", f"/v1/workers/{worker_id}", json_body=payload)

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
