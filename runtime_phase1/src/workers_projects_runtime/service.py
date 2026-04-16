from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Lock

from .openclaw_runtime import (
    RuntimeErrorBase,
    RuntimeInfo,
    WorkerInterruptedError,
    WorkerPausedError,
    WorkerRuntime,
    WorkerTerminatedError,
)
from .store import Store


class WorkersProjectsService:
    def __init__(self, store: Store, runtime: WorkerRuntime, max_workers: int = 8) -> None:
        self.store = store
        self.runtime = runtime
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="wpr-runner")
        self._processors_lock = Lock()
        self._active_processors: set[str] = set()
        self.reconcile_all_workers()

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=False)

    def create_project(self, owner_id: str, title: str, goal: str, default_worker_profile: str) -> dict:
        return self.store.create_project(owner_id, title, goal, default_worker_profile)

    def create_worker(
        self,
        project_id: str,
        owner_id: str,
        name: str,
        role: str,
        profile: str,
        backend: str,
        bootstrap_profile: str | None = None,
        bootstrap_bundle: dict | None = None,
    ) -> dict:
        model = self.runtime.resolve_model(profile)
        worker = self.store.create_worker(
            project_id=project_id,
            owner_id=owner_id,
            name=name,
            role=role,
            profile=profile,
            backend=backend,
            runtime="openclaw",
            model=model,
            bootstrap_profile=bootstrap_profile,
            bootstrap_bundle=bootstrap_bundle,
        )
        self.store.update_worker_state(worker["worker_id"], "starting")
        try:
            info = self.runtime.ensure_worker_ready(worker)
        except Exception as exc:
            updated = self.store.update_worker(
                worker["worker_id"],
                state="failed",
                last_error=str(exc),
            )
            self.store.add_event(project_id, worker["worker_id"], None, "worker.failed", str(exc))
            return updated or worker
        updated = self._apply_runtime_info(worker["worker_id"], info, state="ready", last_error="")
        self.store.add_event(project_id, worker["worker_id"], None, "worker.ready", f"Worker ready on {info.gateway_url}")
        return updated or worker

    def assign_run(self, worker_id: str, instruction: str, event_type: str = "run.queued") -> dict:
        worker = self.require_worker(worker_id)
        run = self.store.create_run(worker_id, worker["project_id"], instruction, state="queued")
        self.store.add_event(worker["project_id"], worker_id, run["run_id"], event_type, instruction)
        self._ensure_worker_processor(worker_id)
        return run

    def record_launch_failed(self, worker_id: str, reason: str) -> dict:
        worker = self.require_worker(worker_id)
        self.store.cancel_pending_runs(worker_id, error_text=reason, state="failed")
        updated = self.store.update_worker(worker_id, state="failed", last_error=reason)
        self.store.add_event(worker["project_id"], worker_id, None, "worker.launch_failed", reason)
        return updated or worker

    def send_message(self, worker_id: str, message: str) -> dict:
        instruction = f"Operator message for the current worker session:\n\n{message}"
        return self.assign_run(worker_id, instruction, event_type="worker.message")

    def desktop_action(
        self,
        worker_id: str,
        action: str,
        *,
        url: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, object]:
        worker = self.require_worker(worker_id)
        if not hasattr(self.runtime, "desktop_action"):
            raise RuntimeErrorBase("Desktop actions are not supported by the configured runtime")
        try:
            launched = self.runtime.desktop_action(worker, action, url=url, run_id=run_id)
        except TypeError as exc:
            if "run_id" not in str(exc):
                raise
            launched = self.runtime.desktop_action(worker, action, url=url)
        self.store.add_event(
            worker["project_id"],
            worker_id,
            self.store.get_active_run(worker_id)["run_id"] if self.store.get_active_run(worker_id) else None,
            "worker.desktop_action",
            f"{action}: {launched.get('notes') or launched.get('status') or 'launched'}",
        )
        return launched

    def pause_worker(self, worker_id: str) -> dict:
        worker = self.require_worker(worker_id)
        info = self.runtime.pause_worker(worker)
        updated = self._apply_runtime_info(worker_id, info, state="paused", last_error=worker.get("last_error") or "")
        active_run = self.store.get_active_run(worker_id)
        self.store.add_event(worker["project_id"], worker_id, active_run["run_id"] if active_run else None, "worker.paused", "Worker paused")
        return updated or worker

    def interrupt_worker(self, worker_id: str) -> dict:
        worker = self.require_worker(worker_id)
        info = self.runtime.interrupt_worker(worker)
        updated = self._apply_runtime_info(worker_id, info, state="ready", last_error="")
        active_run = self.store.get_active_run(worker_id)
        if active_run:
            self.store.finalize_run(
                active_run["run_id"],
                state="interrupted",
                output_text=active_run.get("output_text", ""),
                error_text="Interrupted by operator",
            )
        self.store.add_event(worker["project_id"], worker_id, active_run["run_id"] if active_run else None, "worker.interrupted", "Worker interrupted")
        return updated or worker

    def resume_worker(self, worker_id: str) -> dict:
        worker = self.require_worker(worker_id)
        updated = self._start_worker_again(worker, event_type="worker.resumed", message="Worker resumed")
        active_run = self.store.get_active_run(worker_id)
        if active_run:
            refreshed = self.store.update_worker_state(worker_id, "running", last_error="")
            return refreshed or updated
        else:
            self._ensure_worker_processor(worker_id)
        return updated

    def terminate_worker(self, worker_id: str) -> dict:
        worker = self.require_worker(worker_id)
        info = self.runtime.terminate_worker(worker)
        self.store.cancel_pending_runs(worker_id, error_text="Worker terminated by operator", state="cancelled")
        updated = self._apply_runtime_info(worker_id, info, state="terminated", last_error="")
        self.store.add_event(worker["project_id"], worker_id, None, "worker.terminated", "Worker terminated")
        return updated or worker

    def reconcile_all_workers(self) -> None:
        for worker in self.store.list_all_workers():
            if worker["state"] == "terminated":
                continue
            info = self.runtime.reconcile_worker(worker)
            state = worker["state"]
            if state in {"running", "ready", "starting"}:
                state = "ready" if info.pid else "paused"
            self._apply_runtime_info(worker["worker_id"], info, state=state, last_error=worker.get("last_error") or "")

    def require_project(self, project_id: str) -> dict:
        project = self.store.get_project(project_id)
        if not project:
            raise KeyError("Project not found")
        return project

    def require_worker(self, worker_id: str) -> dict:
        worker = self.store.get_worker(worker_id)
        if not worker:
            raise KeyError("Worker not found")
        return worker

    def require_run(self, run_id: str) -> dict:
        run = self.store.get_run(run_id)
        if not run:
            raise KeyError("Run not found")
        return run

    def heal_worker(self, worker_id: str) -> dict | None:
        worker = self.store.get_worker(worker_id)
        if not worker or worker.get("state") != "running":
            return worker
        active_run = self.store.get_active_run(worker_id)
        if not active_run or not hasattr(self.runtime, "collect_completed_run"):
            return worker
        recovered = self.runtime.collect_completed_run(worker)
        if not recovered:
            return worker
        state = str(recovered.get("state") or "failed")
        output_text = str(recovered.get("output_text") or "")
        error_text = str(recovered.get("error_text") or "")
        if state == "completed":
            self.store.finalize_run(active_run["run_id"], state="completed", output_text=output_text)
            self.store.update_worker(worker_id, state="ready", last_error="", last_run_id=active_run["run_id"])
            preview = output_text.replace("\n", " ")[:240]
            self.store.add_event(worker["project_id"], worker_id, active_run["run_id"], "run.completed", preview or "Run completed")
            self._refresh_runtime_info(worker_id, state="ready", last_error="")
        else:
            self.store.finalize_run(active_run["run_id"], state="failed", error_text=error_text)
            self.store.update_worker(worker_id, state="ready", last_error=error_text, last_run_id=active_run["run_id"])
            self.store.add_event(worker["project_id"], worker_id, active_run["run_id"], "run.failed", error_text or "Run failed")
            self._refresh_runtime_info(worker_id, state="ready", last_error=error_text)
        with self._processors_lock:
            self._active_processors.discard(worker_id)
        return self.store.get_worker(worker_id)

    def _start_worker_again(self, worker: dict, event_type: str, message: str) -> dict:
        self.store.update_worker_state(worker["worker_id"], "starting")
        try:
            info = self.runtime.ensure_worker_ready(worker)
        except Exception as exc:
            updated = self.store.update_worker(worker["worker_id"], state="failed", last_error=str(exc))
            self.store.add_event(worker["project_id"], worker["worker_id"], None, "worker.failed", str(exc))
            return updated or worker
        updated = self._apply_runtime_info(worker["worker_id"], info, state="ready", last_error="")
        self.store.add_event(worker["project_id"], worker["worker_id"], None, event_type, message)
        return updated or worker

    def _apply_runtime_info(self, worker_id: str, info: RuntimeInfo, state: str, last_error: str) -> dict | None:
        return self.store.update_worker(
            worker_id,
            runtime=info.runtime,
            model=info.model,
            state=state,
            gateway_url=info.gateway_url,
            gateway_port=info.gateway_port,
            gateway_token=info.gateway_token,
            session_key=info.session_key,
            state_dir=info.state_dir,
            workspace_dir=info.workspace_dir,
            pid=info.pid,
            takeover_url=f"/ui/workers/{worker_id}",
            control_url=f"/ui/workers/{worker_id}",
            last_error=last_error,
        )

    def _refresh_runtime_info(self, worker_id: str, state: str, last_error: str = "") -> dict | None:
        worker = self.store.get_worker(worker_id)
        if not worker:
            return None
        try:
            info = self.runtime.reconcile_worker(worker)
        except Exception:
            return worker
        return self._apply_runtime_info(worker_id, info, state=state, last_error=last_error) or worker

    def _ensure_worker_processor(self, worker_id: str) -> None:
        with self._processors_lock:
            if worker_id in self._active_processors:
                return
            self._active_processors.add(worker_id)
        self.executor.submit(self._process_worker_queue, worker_id)

    def _process_worker_queue(self, worker_id: str) -> None:
        try:
            while True:
                worker = self.store.get_worker(worker_id)
                if not worker or worker["state"] in {"paused", "terminated"}:
                    return

                run = self.store.claim_next_queued_run(worker_id)
                if not run:
                    current = self.store.get_worker(worker_id)
                    if current and current["state"] not in {"paused", "terminated", "failed"}:
                        self.store.update_worker_state(worker_id, "ready", last_error="")
                    return

                worker = self.store.get_worker(worker_id) or worker
                self.store.update_worker_state(worker_id, "running", last_error="")
                self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.started", run["instruction"])

                try:
                    try:
                        output = self.runtime.run_task(worker, run["instruction"], run_id=run["run_id"])
                    except TypeError as exc:
                        if "run_id" not in str(exc):
                            raise
                        output = self.runtime.run_task(worker, run["instruction"])
                except WorkerPausedError as exc:
                    self.store.update_worker_state(worker_id, "paused", last_error="")
                    self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.paused", str(exc))
                    return
                except WorkerInterruptedError as exc:
                    self.store.finalize_run(run["run_id"], state="interrupted", error_text=str(exc))
                    self.store.update_worker_state(worker_id, "ready", last_error="")
                    self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.interrupted", str(exc))
                    continue
                except WorkerTerminatedError as exc:
                    self.store.finalize_run(run["run_id"], state="cancelled", error_text=str(exc))
                    self.store.update_worker_state(worker_id, "terminated", last_error=str(exc))
                    self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.cancelled", str(exc))
                    return
                except RuntimeErrorBase as exc:
                    worker_state = (self.store.get_worker(worker_id) or worker)["state"]
                    final_state = "failed"
                    if worker_state == "paused":
                        final_state = "interrupted"
                    elif worker_state == "terminated":
                        final_state = "cancelled"
                    self.store.finalize_run(run["run_id"], state=final_state, error_text=str(exc))
                    self.store.update_worker_state(worker_id, worker_state if worker_state in {"paused", "terminated"} else "ready", last_error=str(exc))
                    self.store.add_event(worker["project_id"], worker_id, run["run_id"], f"run.{final_state}", str(exc))
                    if worker_state in {"paused", "terminated"}:
                        return
                    continue
                except Exception as exc:
                    self.store.finalize_run(run["run_id"], state="failed", error_text=str(exc))
                    self.store.update_worker_state(worker_id, "ready", last_error=str(exc))
                    self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.failed", str(exc))
                    continue

                self.store.finalize_run(run["run_id"], state="completed", output_text=output)
                self.store.update_worker(worker_id, state="ready", last_error="", last_run_id=run["run_id"])
                preview = output.replace("\n", " ")[:240]
                self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.completed", preview or "Run completed")
                self._refresh_runtime_info(worker_id, state="ready", last_error="")
        finally:
            with self._processors_lock:
                self._active_processors.discard(worker_id)
            pending = self.store.get_worker(worker_id)
            if pending and pending["state"] not in {"paused", "terminated"}:
                queued = next((item for item in self.store.list_runs_for_worker(worker_id, limit=10) if item["state"] == "queued"), None)
                if queued:
                    self._ensure_worker_processor(worker_id)
