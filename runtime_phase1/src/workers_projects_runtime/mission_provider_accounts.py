from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Callable, Iterator

from .control_plane import ControlPlaneConflict, ControlPlaneError, ControlPlaneStore
from .openclaw_runtime import RuntimeErrorBase
from .provider_accounts import ProviderAccountHomeManager


logger = logging.getLogger(__name__)

_POLICY_ALIASES = {
    "legacy": "legacy",
    "personal_optional": "personal_preferred",
    "personal_preferred": "personal_preferred",
    "personal_required": "personal_required",
}
_PROFILE_PROVIDERS = {
    "codex-cli": {"codex", "openai"},
    "claude-code": {"claude", "anthropic"},
}
_EXPECTED_HOME_KEYS = {
    "codex-cli": "CODEX_HOME",
    "claude-code": "CLAUDE_CONFIG_DIR",
}
_CONTAINER_ACCOUNT_MOUNT = "/workspace/.provider-account"
_CODEX_CONFLICTING_ENV = {
    "CODEX_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "OPENAI_REVERSE_PROXY",
    "PORTKEY_API_KEY",
    "PORTKEY_BASE_URL",
    "PORTKEY_PROVIDER",
    "PORTKEY_VIRTUAL_KEY",
    "PORTKEY_CONFIG",
}
_CLAUDE_CONFLICTING_ENV = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_API_URL",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
    "CLAUDE_CODE_OAUTH_SCOPES",
    "CLAUDE_CODE_USE_BEDROCK",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_BEARER_TOKEN_BEDROCK",
}


@dataclass(frozen=True)
class MissionProviderAccountSelection:
    policy: str
    account_id: str

    @property
    def requires_binding(self) -> bool:
        return bool(self.account_id)


def _bootstrap_bundle(worker: dict) -> dict[str, object]:
    raw = worker.get("bootstrap_bundle_json")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    candidate = worker.get("bootstrap_bundle")
    return candidate if isinstance(candidate, dict) else {}


def mission_provider_account_selection(
    worker: dict,
) -> MissionProviderAccountSelection | None:
    """Read the additive, mission-only provider account selection contract.

    An absent selection is the historical compatibility path. An explicitly selected account
    defaults to fail-closed ``personal_required`` behavior so a typo cannot silently spend a
    process-global account.
    """

    bundle = _bootstrap_bundle(worker)
    if str(bundle.get("run_mode") or "mission").strip().lower() == "conversation":
        return None
    if "provider_account" not in bundle:
        return None
    raw = bundle.get("provider_account")
    if not isinstance(raw, dict):
        raise RuntimeErrorBase("Mission provider account selection must be a structured object")
    account_id = str(raw.get("account_id") or "").strip()
    requested_policy = str(
        raw.get("policy") or ("personal_required" if account_id else "legacy")
    ).strip().lower()
    policy = _POLICY_ALIASES.get(requested_policy, "")
    if not policy:
        raise RuntimeErrorBase(
            "Mission provider account policy must be legacy, personal_preferred, or personal_required"
        )
    if policy == "legacy":
        if account_id:
            raise RuntimeErrorBase(
                "Legacy provider account policy cannot include a personal account selection"
            )
        return None
    if not account_id:
        if policy == "personal_required":
            raise RuntimeErrorBase("This mission requires a provider account selection")
        return None
    return MissionProviderAccountSelection(policy=policy, account_id=account_id)


def apply_bound_provider_account_environment(
    worker: dict,
    env: dict[str, str],
    *,
    runtime_name: str,
) -> dict[str, str]:
    """Apply only a trusted binding projected by ``MissionProviderAccountBinder``.

    Direct sub-runtime use with an explicit personal selection fails closed; callers cannot place
    an arbitrary home in the public bootstrap bundle and bypass owner/account validation.
    """

    selection = mission_provider_account_selection(worker)
    if selection is None:
        return env
    if worker.get("_glasshive_inference_broker_bound"):
        if runtime_name != "codex-cli":
            raise RuntimeErrorBase(
                "The OpenAI inference broker can be projected only into Codex workers"
            )
        return env
    if selection.policy == "personal_preferred" and worker.get(
        "_glasshive_provider_account_preferred_fallback"
    ):
        return env
    if not worker.get("_glasshive_provider_account_bound"):
        raise RuntimeErrorBase(
            "Mission provider account selection was not validated by the GlassHive control plane"
        )
    expected_key = _EXPECTED_HOME_KEYS.get(runtime_name)
    if expected_key is None:
        raise RuntimeErrorBase(
            "Personal provider accounts are supported only for Codex and Claude mission workers"
        )
    raw_environment = worker.get("_glasshive_provider_account_env")
    if not isinstance(raw_environment, dict):
        raise RuntimeErrorBase("Mission provider account binding is missing its private provider home")
    keys = {str(key) for key in raw_environment}
    if keys != {expected_key}:
        raise RuntimeErrorBase("Mission provider account binding contains an invalid provider home")
    account_home = str(raw_environment.get(expected_key) or "").strip()
    if not account_home or not Path(account_home).is_absolute():
        raise RuntimeErrorBase("Mission provider account binding contains an invalid provider home")

    conflicting = (
        _CODEX_CONFLICTING_ENV
        if runtime_name == "codex-cli"
        else _CLAUDE_CONFLICTING_ENV
    )
    for key in conflicting:
        env.pop(key, None)
    env[expected_key] = account_home
    return env


class MissionProviderAccountBinder:
    """Owner-scoped native account home and durable mission lease projection."""

    def __init__(
        self,
        *,
        db_path: str | None,
        home_root: Path,
    ) -> None:
        self.store = ControlPlaneStore(db_path) if db_path else None
        self.home_root = Path(home_root)

    def selected_account_record(
        self,
        worker: dict,
        selection: MissionProviderAccountSelection,
    ) -> dict | None:
        """Return the internal owner-scoped account record for route selection only.

        Secret locators are intentionally not used by the broker consumer. The method exists so
        ``ProfiledWorkerRuntime`` can distinguish a provider-native subscription home from a
        LibreChat-held API key or enterprise route before either credential path is projected.
        """

        if self.store is None:
            return None
        tenant_id = str(worker.get("tenant_id") or "local").strip() or "local"
        owner_id = str(worker.get("owner_id") or "").strip()
        if not owner_id:
            raise RuntimeErrorBase(
                "Mission provider account binding requires an authenticated owner"
            )
        return self.store.get_provider_account_record(
            account_id=selection.account_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
        )

    def update_selected_account_status(
        self,
        worker: dict,
        selection: MissionProviderAccountSelection,
        *,
        status: str,
        reconnect_reason: str = "",
    ) -> None:
        if self.store is None:
            return
        self.store.update_provider_account_status(
            account_id=selection.account_id,
            tenant_id=str(worker.get("tenant_id") or "local").strip() or "local",
            owner_id=str(worker.get("owner_id") or "").strip(),
            status=status,
            reconnect_reason=reconnect_reason,
        )

    @staticmethod
    def _lease_ttl_seconds(timeout_sec: float | None) -> int:
        # Leases are heartbeated for the full mission, so the expiry is a crash-detection
        # window rather than the mission duration. Keeping it short prevents an API crash
        # from locking a user's personal subscription for the rest of the day.
        default_ttl = 180
        configured = str(
            os.environ.get("GLASSHIVE_PROVIDER_ACCOUNT_LEASE_TTL_SECONDS") or ""
        ).strip()
        if configured:
            try:
                requested = int(configured)
            except ValueError:
                requested = default_ttl
        else:
            requested = default_ttl
        return max(15, min(requested, 60 * 60))

    @staticmethod
    def _preferred_fallback(worker: dict) -> dict:
        return {
            **worker,
            "_glasshive_provider_account_preferred_fallback": True,
        }

    @staticmethod
    def _lease_heartbeat_interval(ttl_seconds: int) -> float:
        configured = str(
            os.environ.get("GLASSHIVE_PROVIDER_ACCOUNT_LEASE_HEARTBEAT_SECONDS")
            or ""
        ).strip()
        if configured:
            try:
                return max(0.05, min(float(configured), max(1.0, ttl_seconds / 3)))
            except ValueError:
                pass
        return max(1.0, min(60.0, ttl_seconds / 3))

    def _start_lease_heartbeat(
        self,
        *,
        lease_id: str,
        tenant_id: str,
        owner_id: str,
        ttl_seconds: int,
        worker_id: str,
        runtime_name: str,
        account_id: str,
        on_lease_lost: Callable[[], None] | None = None,
    ) -> tuple[Event, Thread, Event]:
        stop = Event()
        lease_lost = Event()

        def heartbeat() -> None:
            interval = self._lease_heartbeat_interval(ttl_seconds)
            while not stop.wait(interval):
                try:
                    assert self.store is not None
                    self.store.heartbeat_provider_lease(
                        lease_id=lease_id,
                        tenant_id=tenant_id,
                        owner_id=owner_id,
                        ttl_seconds=ttl_seconds,
                    )
                except (ControlPlaneError, OSError, sqlite3.OperationalError):
                    lease_lost.set()
                    logger.exception(
                        "Failed to renew provider account mission lease",
                        extra={"worker_id": worker_id, "runtime": runtime_name},
                    )
                    try:
                        assert self.store is not None
                        self.store.update_provider_account_status(
                            account_id=account_id,
                            tenant_id=tenant_id,
                            owner_id=owner_id,
                            status="action_required",
                            reconnect_reason="Mission lease renewal failed; reconnect after the worker is safely stopped",
                        )
                    except (ControlPlaneError, OSError, sqlite3.OperationalError):
                        logger.exception(
                            "Failed to quarantine provider account after lease renewal loss",
                            extra={"worker_id": worker_id, "runtime": runtime_name},
                        )
                    if on_lease_lost is not None:
                        try:
                            on_lease_lost()
                        except Exception:
                            logger.exception(
                                "Failed to stop provider-bound worker after lease renewal loss",
                                extra={"worker_id": worker_id, "runtime": runtime_name},
                            )
                    return

        thread = Thread(
            target=heartbeat,
            name=f"glasshive-provider-lease-{worker_id[:24]}",
            daemon=True,
        )
        thread.start()
        return stop, thread, lease_lost

    @contextmanager
    def bind(
        self,
        worker: dict,
        *,
        runtime_name: str,
        run_id: str,
        timeout_sec: float | None,
        release_binding: Callable[[dict], None] | None = None,
        abort_binding: Callable[[dict], None] | None = None,
        reconcile_binding: Callable[[Path], None] | None = None,
    ) -> Iterator[dict]:
        selection = mission_provider_account_selection(worker)
        if selection is None:
            yield worker
            return
        execution_mode = str(worker.get("execution_mode") or "docker").strip().lower()
        security_mode = str(
            os.environ.get("GLASSHIVE_SECURITY_MODE") or ""
        ).strip().lower()
        isolation_mode = str(
            os.environ.get("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION") or ""
        ).strip().lower()
        isolated_container = (
            execution_mode == "docker"
            and isolation_mode == "per_worker_container"
        )
        if isolated_container and (release_binding is None or reconcile_binding is None):
            raise RuntimeErrorBase(
                "The reviewed provider-account container substrate cannot prove credential mount reconciliation"
            )
        if security_mode == "multi_user" and not isolated_container:
            raise RuntimeErrorBase(
                "Personal subscription workers are disabled in multi-user deployments until "
                "GlassHive can place each account and worker behind a dedicated OS or container boundary"
            )
        preferred = selection.policy == "personal_preferred"
        if self.store is None:
            if preferred:
                yield self._preferred_fallback(worker)
                return
            raise RuntimeErrorBase(
                "Mission provider accounts are unavailable because the control-plane store is not configured"
            )
        if runtime_name not in _PROFILE_PROVIDERS:
            if preferred:
                yield self._preferred_fallback(worker)
                return
            raise RuntimeErrorBase(
                "Personal provider accounts are supported only for Codex and Claude mission workers"
            )
        if execution_mode not in {"host", "docker"} or (
            execution_mode == "docker" and not isolated_container
        ):
            if preferred:
                yield self._preferred_fallback(worker)
                return
            raise RuntimeErrorBase(
                "Personal provider account missions require a host-native worker or the reviewed per-worker container substrate"
            )
        tenant_id = str(worker.get("tenant_id") or "local").strip() or "local"
        owner_id = str(worker.get("owner_id") or "").strip()
        worker_id = str(worker.get("worker_id") or "").strip()
        if not owner_id or not worker_id:
            raise RuntimeErrorBase(
                "Mission provider account binding requires an authenticated owner and worker"
            )
        account = self.store.get_provider_account(
            account_id=selection.account_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
        if account is None:
            if preferred:
                yield self._preferred_fallback(worker)
                return
            raise RuntimeErrorBase("Selected provider account is not available for this user")
        provider = str(account.get("provider") or "").strip().lower()
        if provider not in _PROFILE_PROVIDERS[runtime_name]:
            raise RuntimeErrorBase(
                "Selected provider account does not match this worker profile"
            )
        if str(account.get("status") or "").strip().lower() != "ready":
            if preferred:
                yield self._preferred_fallback(worker)
                return
            raise RuntimeErrorBase(
                "Selected provider account is not ready; reconnect or verify it before running"
            )
        try:
            homes = ProviderAccountHomeManager(self.home_root)
            homes.require_supported_route(
                provider=provider,
                auth_method=str(account.get("auth_method") or ""),
                execution_mode=execution_mode,
                platform_name=sys.platform,
                hosted_consumer_auth_enabled=str(
                    os.environ.get("GLASSHIVE_ENABLE_HOSTED_CLAUDE_CONSUMER_AUTH") or ""
                ).strip().lower()
                in {"1", "true", "yes", "on"},
            )
            account_home = homes.ensure_home(
                tenant_id=tenant_id,
                owner_id=owner_id,
                account_id=selection.account_id,
                provider=provider,
            )
            environment = homes.runtime_environment(
                provider=provider,
                account_home=account_home,
            )
            if execution_mode == "docker":
                environment = {
                    key: f"{_CONTAINER_ACCOUNT_MOUNT}/{Path(value).name}"
                    for key, value in environment.items()
                }
            lease_ttl_seconds = self._lease_ttl_seconds(timeout_sec)
            lease = self.store.acquire_provider_lease(
                account_id=selection.account_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
                lane=f"{runtime_name}:mission",
                worker_id=worker_id,
                run_id=run_id,
                ttl_seconds=lease_ttl_seconds,
            )
        except ControlPlaneConflict as exc:
            if preferred:
                yield self._preferred_fallback(worker)
                return
            raise RuntimeErrorBase("Selected provider account is already in use") from exc
        except ControlPlaneError as exc:
            if preferred:
                yield self._preferred_fallback(worker)
                return
            raise RuntimeErrorBase(str(exc)) from exc

        if execution_mode == "docker":
            try:
                assert reconcile_binding is not None
                reconcile_binding(account_home)
            except BaseException as exc:
                try:
                    self.store.update_provider_account_status(
                        account_id=selection.account_id,
                        tenant_id=tenant_id,
                        owner_id=owner_id,
                        status="action_required",
                        reconnect_reason="A stale provider credential mount could not be removed",
                    )
                finally:
                    self.store.release_provider_lease(
                        lease_id=str(lease.get("lease_id") or ""),
                        tenant_id=tenant_id,
                        owner_id=owner_id,
                    )
                raise RuntimeErrorBase(
                    "GlassHive could not prove stale provider credentials were unmounted"
                ) from exc

        bound_worker = {
            **worker,
            "_glasshive_provider_account_bound": True,
            "_glasshive_provider_account_env": environment,
        }
        if execution_mode == "docker":
            bound_worker.update(
                {
                    "_glasshive_provider_account_mount_host": str(
                        account_home.resolve(strict=True)
                    ),
                    "_glasshive_provider_account_mount_target": _CONTAINER_ACCOUNT_MOUNT,
                }
            )
        binding_release_lock = Lock()
        binding_released = Event()
        binding_release_errors: list[BaseException] = []

        def release_bound_credentials() -> None:
            if execution_mode != "docker" or binding_released.is_set():
                return
            with binding_release_lock:
                if binding_released.is_set():
                    return
                assert release_binding is not None
                try:
                    release_binding(bound_worker)
                    homes.tighten_permissions(account_home=account_home)
                except BaseException as exc:
                    binding_release_errors.append(exc)
                    try:
                        assert self.store is not None
                        self.store.update_provider_account_status(
                            account_id=selection.account_id,
                            tenant_id=tenant_id,
                            owner_id=owner_id,
                            status="action_required",
                            reconnect_reason="Provider credential cleanup failed; operator cleanup is required",
                        )
                    except (ControlPlaneError, OSError):
                        logger.exception(
                            "Failed to quarantine provider account after credential unmount failure",
                            extra={"worker_id": worker_id, "runtime": runtime_name},
                        )
                    raise
                binding_released.set()

        def abort_bound_credentials() -> None:
            if execution_mode == "docker":
                release_bound_credentials()
            elif abort_binding is not None:
                abort_binding(bound_worker)

        lease_stop, lease_thread, lease_lost = self._start_lease_heartbeat(
            lease_id=str(lease.get("lease_id") or ""),
            tenant_id=tenant_id,
            owner_id=owner_id,
            ttl_seconds=lease_ttl_seconds,
            worker_id=worker_id,
            runtime_name=runtime_name,
            account_id=selection.account_id,
            on_lease_lost=abort_bound_credentials,
        )
        body_error: BaseException | None = None
        try:
            yield bound_worker
        except BaseException as exc:
            body_error = exc
            raise
        finally:
            try:
                release_bound_credentials()
            except BaseException:
                if body_error is None:
                    body_error = RuntimeErrorBase(
                        "GlassHive could not remove the provider credential mount; the account was quarantined"
                    )
            lease_stop.set()
            lease_thread.join(timeout=2)
            if not binding_release_errors:
                try:
                    self.store.release_provider_lease(
                        lease_id=str(lease.get("lease_id") or ""),
                        tenant_id=tenant_id,
                        owner_id=owner_id,
                    )
                except ControlPlaneError as exc:
                    if body_error is None:
                        raise RuntimeErrorBase(
                            "GlassHive could not release the provider account mission lease"
                        ) from exc
                    logger.exception(
                        "Failed to release provider account lease after mission failure",
                        extra={"worker_id": worker_id, "runtime": runtime_name},
                    )
            if binding_release_errors and body_error is not None:
                raise body_error
            if lease_lost.is_set() and body_error is None:
                raise RuntimeErrorBase(
                    "Provider account lease renewal failed; GlassHive stopped the bound worker and requires reconnection"
                )
