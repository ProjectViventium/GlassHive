from __future__ import annotations

import hashlib
import os
import pty
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import fcntl

from .control_plane import ControlPlaneConflict, ControlPlaneError
from .inference_broker import (
    InferenceBrokerError,
    inference_broker_config_from_environment,
)


SAFE_ACCOUNT_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
MAX_SETUP_OUTPUT_CHARS = 32_000
PROVIDER_SETUP_ENV_ALLOWLIST = {
    "ALL_PROXY",
    "COLORTERM",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "NODE_EXTRA_CA_CERTS",
    "NO_PROXY",
    "PATH",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TEMP",
    "TERM",
    "TMP",
    "TMPDIR",
    "TZ",
    "all_proxy",
    "http_proxy",
    "https_proxy",
    "no_proxy",
}

_SETUP_URL = re.compile(r"https://[^\s'\"<>]+")
_CODEX_DEVICE_CODE = re.compile(
    r"(?i:one-time\s+code(?:\s*\([^)]*\))?)\s*:?\s*"
    r"([A-Z0-9]{4,8}-[A-Z0-9]{4,8})(?![A-Z0-9-])",
)
_CODEX_SECURITY_SETTINGS_URL = "https://chatgpt.com/#settings/Security"


def _provider_setup_guidance(provider: str, output: str) -> dict[str, str]:
    """Extract bounded, clickable guidance without trusting arbitrary CLI output as a URL."""

    normalized_provider = str(provider or "").strip().lower()
    canonical_provider = (
        "codex"
        if normalized_provider in {"codex", "openai"}
        else "claude"
        if normalized_provider in {"claude", "anthropic"}
        else normalized_provider or "unknown"
    )
    setup_url = ""
    for match in _SETUP_URL.finditer(str(output or "")):
        candidate = match.group(0).rstrip(".,;:)")
        parsed = urlsplit(candidate)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            continue
        hostname = str(parsed.hostname or "").lower()
        if normalized_provider in {"codex", "openai"}:
            if hostname == "auth.openai.com" and parsed.path.rstrip("/") == "/codex/device":
                setup_url = candidate
                break
        elif normalized_provider in {"claude", "anthropic"}:
            if hostname in {"claude.ai", "console.anthropic.com"}:
                setup_url = candidate
                break

    setup_code = ""
    if setup_url and normalized_provider in {"codex", "openai"}:
        code_match = _CODEX_DEVICE_CODE.search(str(output or ""))
        if code_match:
            setup_code = code_match.group(1).upper()

    return {
        "provider": canonical_provider,
        "setup_url": setup_url,
        "setup_code": setup_code,
        "help_url": (
            _CODEX_SECURITY_SETTINGS_URL
            if normalized_provider in {"codex", "openai"}
            else ""
        ),
    }


def _env_enabled(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def provider_platform_support(
    *,
    provider: str,
    auth_method: str,
    platform_name: str | None = None,
) -> str:
    """Return deployment-owned support truth; clients cannot opt themselves into a route."""

    normalized_provider = str(provider or "").strip().lower()
    normalized_method = str(auth_method or "").strip().lower()
    current_platform = str(platform_name or sys.platform).strip().lower()
    if normalized_method in {"api_key", "enterprise_route"}:
        if normalized_provider in {"codex", "openai"}:
            try:
                broker_config = inference_broker_config_from_environment()
            except InferenceBrokerError:
                return "broker_configuration_invalid"
            if broker_config is not None:
                return "supported"
    if normalized_method == "api_key":
        return (
            "managed_connection_required"
            if _env_enabled("GLASSHIVE_PROVIDER_SECRET_STORE_ENABLED")
            else "secret_store_required"
        )
    if normalized_method == "enterprise_route":
        return "managed_connection_required"
    if normalized_method != "subscription":
        return "supported"
    if str(os.environ.get("GLASSHIVE_SECURITY_MODE") or "").strip().lower() == "multi_user":
        isolation_mode = str(
            os.environ.get("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION") or ""
        ).strip().lower()
        if isolation_mode != "per_worker_container":
            return "isolated_substrate_required"
    if normalized_provider in {"claude", "anthropic"}:
        if current_platform == "darwin":
            return "unsupported_macos_host"
        if not _env_enabled("GLASSHIVE_ENABLE_HOSTED_CLAUDE_CONSUMER_AUTH"):
            return "provider_permission_required"
        return "supported"
    if normalized_provider in {"codex", "openai"}:
        return (
            "supported"
            if _env_enabled("GLASSHIVE_ENABLE_CODEX_PERSONAL_ACCOUNTS")
            else "proof_required"
        )
    return "proof_required"


def _identity_segment(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:24]


def _valid_account_id(value: str) -> bool:
    account_id = str(value or "")
    return bool(SAFE_ACCOUNT_ID.fullmatch(account_id) and account_id not in {".", ".."})


class ProviderAccountHomeManager:
    """Owns provider-native homes outside workspace storage."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        if self.root.is_symlink():
            raise ControlPlaneError("Provider account root is not a safe managed directory")
        self.root.mkdir(parents=True, exist_ok=True)
        self._private(self.root)

    def _private(self, path: Path) -> None:
        if os.name != "nt":
            path.chmod(0o700)

    def ensure_home(self, *, tenant_id: str, owner_id: str, account_id: str, provider: str) -> Path:
        if not _valid_account_id(account_id):
            raise ControlPlaneError("Provider account id is invalid")
        if provider not in {"codex", "claude", "openai", "anthropic", "custom"}:
            raise ControlPlaneError("Unsupported provider")
        tenant_home = self.root / _identity_segment(tenant_id)
        owner_home = tenant_home / _identity_segment(owner_id)
        account_home = owner_home / account_id
        for directory in (tenant_home, owner_home, account_home):
            if directory.is_symlink():
                raise ControlPlaneError("Provider account home is not a safe managed directory")
            directory.mkdir(exist_ok=True)
            self._private(directory)
        provider_home = account_home / ("codex" if provider in {"codex", "openai"} else "claude")
        if provider_home.is_symlink():
            raise ControlPlaneError("Provider account home is not a safe managed directory")
        provider_home.mkdir(exist_ok=True)
        self._private(provider_home)
        return account_home

    def account_home_path(self, *, tenant_id: str, owner_id: str, account_id: str) -> Path:
        if not _valid_account_id(account_id):
            raise ControlPlaneError("Provider account id is invalid")
        return self.root / _identity_segment(tenant_id) / _identity_segment(owner_id) / account_id

    def remove_home(self, *, tenant_id: str, owner_id: str, account_id: str) -> None:
        account_home = self.account_home_path(
            tenant_id=tenant_id,
            owner_id=owner_id,
            account_id=account_id,
        )
        if not account_home.exists() and not account_home.is_symlink():
            return
        if account_home.is_symlink():
            raise ControlPlaneError("Provider account home is not a safe managed directory")
        resolved_root = self.root.resolve(strict=True)
        resolved_home = account_home.resolve(strict=True)
        if resolved_root not in resolved_home.parents:
            raise ControlPlaneError("Provider account home is outside the managed credential root")
        shutil.rmtree(resolved_home)

    def runtime_environment(self, *, provider: str, account_home: Path) -> dict[str, str]:
        if provider in {"codex", "openai"}:
            target = account_home / "codex"
            target.mkdir(parents=True, exist_ok=True)
            self._private(target)
            return {"CODEX_HOME": str(target)}
        if provider in {"claude", "anthropic"}:
            target = account_home / "claude"
            target.mkdir(parents=True, exist_ok=True)
            self._private(target)
            return {"CLAUDE_CONFIG_DIR": str(target)}
        raise ControlPlaneError("Unsupported provider account home")

    def tighten_permissions(self, *, account_home: Path) -> None:
        """Make provider-created credential state private without following symlinks."""

        resolved_root = self.root.resolve(strict=True)
        resolved_home = Path(account_home).resolve(strict=True)
        if resolved_root not in resolved_home.parents:
            raise ControlPlaneError("Provider account home is outside the managed credential root")
        for current_root, directory_names, file_names in os.walk(resolved_home, followlinks=False):
            current = Path(current_root)
            if current.is_symlink():
                raise ControlPlaneError("Provider account home contains an unsafe directory link")
            if os.name != "nt":
                current.chmod(0o700)
            for directory_name in list(directory_names):
                directory = current / directory_name
                if directory.is_symlink():
                    directory_names.remove(directory_name)
                    continue
                if os.name != "nt":
                    directory.chmod(0o700)
            if os.name != "nt":
                for file_name in file_names:
                    credential_file = current / file_name
                    if not credential_file.is_symlink():
                        credential_file.chmod(0o600)

    def require_supported_route(
        self,
        *,
        provider: str,
        auth_method: str,
        execution_mode: str,
        platform_name: str,
        hosted_consumer_auth_enabled: bool,
    ) -> None:
        normalized_provider = str(provider).strip().lower()
        if normalized_provider in {"claude", "anthropic"} and auth_method == "subscription":
            if execution_mode == "host" and platform_name == "darwin":
                raise ControlPlaneError(
                    "macOS host-native Claude supports only the current OS user's account; use an approved enterprise route"
                )
            if not hosted_consumer_auth_enabled:
                raise ControlPlaneError(
                    "Hosted Claude consumer login requires explicit provider permission or a supported contract"
                )


@dataclass
class _SetupSession:
    account_id: str
    tenant_id: str
    owner_id: str
    provider: str
    process: subprocess.Popen[bytes]
    master_fd: int
    lock_file: Any
    lease_id: str
    lease_stop: threading.Event = field(default_factory=threading.Event)
    lease_thread: threading.Thread | None = None
    output: str = ""
    started_at: float = field(default_factory=time.time)
    reader_done: bool = False


class ProviderSetupManager:
    """Runs provider-native sign-in in a private per-user home.

    Setup output is capped and held in memory only. Provider credentials remain in the
    provider's own native home, outside workspace storage and the control-plane database.
    """

    def __init__(self, *, store: Any, home_root: Path) -> None:
        self.store = store
        self.homes = ProviderAccountHomeManager(home_root)
        self._sessions: dict[str, _SetupSession] = {}
        self._lock = threading.RLock()

    def _binary(self, provider: str) -> str:
        env_name = "WPR_CODEX_CLI_PATH" if provider in {"codex", "openai"} else "WPR_CLAUDE_CODE_PATH"
        configured = str(os.environ.get(env_name) or "").strip()
        binary = configured or shutil.which("codex" if provider in {"codex", "openai"} else "claude")
        if not binary:
            raise ControlPlaneError(f"{provider.title()} CLI is not installed in this GlassHive runtime")
        return binary

    def _environment(self, *, provider: str, account_home: Path) -> dict[str, str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in PROVIDER_SETUP_ENV_ALLOWLIST
        }
        environment.update(self.homes.runtime_environment(provider=provider, account_home=account_home))
        # Provider CLIs sometimes consult HOME even when their documented config-home override is
        # present. Keep every incidental login file inside the same private account tree.
        environment["HOME"] = str(account_home)
        environment["NO_COLOR"] = "1"
        return environment

    def _commands(self, provider: str) -> tuple[list[str], list[str]]:
        binary = self._binary(provider)
        if provider in {"codex", "openai"}:
            return [binary, "login", "--device-auth"], [binary, "login", "status"]
        if provider in {"claude", "anthropic"}:
            return [binary, "auth", "login", "--claudeai"], [binary, "auth", "status", "--json"]
        raise ControlPlaneError("Unsupported provider setup")

    def _account(self, *, account_id: str, tenant_id: str, owner_id: str) -> dict[str, Any]:
        account = self.store.get_provider_account_record(
            account_id=account_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
        if account is None:
            raise ControlPlaneError("Provider account not found for this user")
        if str(account.get("auth_method") or "") != "subscription":
            raise ControlPlaneError("Interactive setup is only available for provider subscription accounts")
        current_support = provider_platform_support(
            provider=str(account.get("provider") or ""),
            auth_method=str(account.get("auth_method") or ""),
        )
        if current_support != "supported":
            raise ControlPlaneError("This provider account setup is not supported by the current deployment")
        return account

    def _append_output(self, session: _SetupSession, chunk: bytes) -> None:
        text = ANSI_ESCAPE.sub("", chunk.decode("utf-8", errors="replace"))
        text = "".join(character for character in text if character in "\n\r\t" or ord(character) >= 32)
        with self._lock:
            session.output = (session.output + text)[-MAX_SETUP_OUTPUT_CHARS:]

    def _read_output(self, session: _SetupSession) -> None:
        try:
            while True:
                try:
                    chunk = os.read(session.master_fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                self._append_output(session, chunk)
        finally:
            with self._lock:
                session.reader_done = True

    def start(self, *, account_id: str, tenant_id: str, owner_id: str) -> dict[str, object]:
        account = self._account(account_id=account_id, tenant_id=tenant_id, owner_id=owner_id)
        provider = str(account.get("provider") or "").strip().lower()
        account_home = self.homes.ensure_home(
            tenant_id=tenant_id,
            owner_id=owner_id,
            account_id=account_id,
            provider=provider,
        )
        setup_command, _ = self._commands(provider)
        environment = self._environment(provider=provider, account_home=account_home)
        with self._lock:
            current = self._sessions.get(account_id)
            if current is not None and current.process.poll() is None:
                raise ControlPlaneConflict("Provider account setup is already running")
            active_sessions = [
                session for session in self._sessions.values() if session.process.poll() is None
            ]
            if len(active_sessions) >= 8:
                raise ControlPlaneConflict("Provider account setup capacity is temporarily full")
            if sum(
                session.tenant_id == tenant_id and session.owner_id == owner_id
                for session in active_sessions
            ) >= 2:
                raise ControlPlaneConflict("This user already has the maximum active account setups")
            lock_path = account_home / ".setup.lock"
            lock_file = lock_path.open("a+b")
            if os.name != "nt":
                lock_path.chmod(0o600)
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                lock_file.close()
                raise ControlPlaneConflict("Provider account setup is already running") from exc
            try:
                lease = self.store.acquire_provider_lease(
                    account_id=account_id,
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                    lane="provider-setup",
                    worker_id="provider-setup",
                    run_id=f"setup:{account_id}",
                    ttl_seconds=60,
                    allowed_statuses=(
                        "disconnected",
                        "connecting",
                        "ready",
                        "action_required",
                        "unavailable",
                        "error",
                    ),
                )
            except Exception:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                lock_file.close()
                raise
            master_fd, slave_fd = pty.openpty()
            try:
                process = subprocess.Popen(
                    setup_command,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    cwd=str(account_home),
                    env=environment,
                    start_new_session=True,
                    close_fds=True,
                    # The provider child retains the account-home flock. If the
                    # API process crashes, a replacement cannot overlap a still-
                    # running login against the same credential bytes.
                    pass_fds=(lock_file.fileno(),),
                )
            except Exception:
                os.close(master_fd)
                os.close(slave_fd)
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                lock_file.close()
                self.store.release_provider_lease(
                    lease_id=str(lease.get("lease_id") or ""),
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                )
                raise
            finally:
                try:
                    os.close(slave_fd)
                except OSError:
                    pass
            session = _SetupSession(
                account_id=account_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
                provider=provider,
                process=process,
                master_fd=master_fd,
                lock_file=lock_file,
                lease_id=str(lease.get("lease_id") or ""),
            )
            self._sessions[account_id] = session
            self._start_setup_lease_heartbeat(session)
            threading.Thread(target=self._read_output, args=(session,), daemon=True).start()
        try:
            self.store.update_provider_account_status(
                account_id=account_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
                status="connecting",
            )
        except Exception:
            with self._lock:
                self._sessions.pop(account_id, None)
            self._terminate_session_process(session)
            self._release_session(session)
            raise
        return self.status(account_id=account_id, tenant_id=tenant_id, owner_id=owner_id, verify=False)

    def _verify(self, *, provider: str, environment: dict[str, str], account_home: Path) -> bool:
        _, status_command = self._commands(provider)
        try:
            result = subprocess.run(
                status_command,
                cwd=str(account_home),
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=12,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def _release_session(self, session: _SetupSession) -> None:
        session.lease_stop.set()
        if session.lease_thread is not None:
            session.lease_thread.join(timeout=2)
        try:
            os.close(session.master_fd)
        except OSError:
            pass
        try:
            fcntl.flock(session.lock_file.fileno(), fcntl.LOCK_UN)
        except (OSError, ValueError):
            pass
        session.lock_file.close()
        self.store.release_provider_lease(
            lease_id=session.lease_id,
            tenant_id=session.tenant_id,
            owner_id=session.owner_id,
        )

    def _start_setup_lease_heartbeat(self, session: _SetupSession) -> None:
        def heartbeat() -> None:
            while not session.lease_stop.wait(10):
                try:
                    self.store.heartbeat_provider_lease(
                        lease_id=session.lease_id,
                        tenant_id=session.tenant_id,
                        owner_id=session.owner_id,
                        ttl_seconds=60,
                    )
                except (ControlPlaneError, OSError, sqlite3.OperationalError):
                    self._terminate_session_process(session)
                    try:
                        self.store.update_provider_account_status(
                            account_id=session.account_id,
                            tenant_id=session.tenant_id,
                            owner_id=session.owner_id,
                            status="action_required",
                            reconnect_reason="Provider setup lease was lost; reconnect safely",
                        )
                    except (ControlPlaneError, OSError, sqlite3.OperationalError):
                        pass
                    return

        session.lease_thread = threading.Thread(
            target=heartbeat,
            name=f"glasshive-provider-setup-lease-{session.account_id[:24]}",
            daemon=True,
        )
        session.lease_thread.start()

    @staticmethod
    def _terminate_session_process(session: _SetupSession) -> None:
        if session.process.poll() is not None:
            return
        try:
            os.killpg(session.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            session.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(session.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                session.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    def shutdown(self) -> None:
        """Stop every login process group and release its account-home lock."""

        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            self._terminate_session_process(session)
            self._release_session(session)
            try:
                self.store.update_provider_account_status(
                    account_id=session.account_id,
                    tenant_id=session.tenant_id,
                    owner_id=session.owner_id,
                    status="action_required",
                    reconnect_reason="Provider setup stopped because GlassHive shut down",
                )
            except ControlPlaneError:
                pass

    def status(
        self,
        *,
        account_id: str,
        tenant_id: str,
        owner_id: str,
        verify: bool = True,
    ) -> dict[str, object]:
        account = self._account(account_id=account_id, tenant_id=tenant_id, owner_id=owner_id)
        provider = str(account.get("provider") or "").strip().lower()
        account_home = self.homes.ensure_home(
            tenant_id=tenant_id,
            owner_id=owner_id,
            account_id=account_id,
            provider=provider,
        )
        environment = self._environment(provider=provider, account_home=account_home)
        with self._lock:
            session = self._sessions.get(account_id)
            if session is not None and (session.tenant_id != tenant_id or session.owner_id != owner_id):
                raise ControlPlaneError("Provider account not found for this user")
            return_code = session.process.poll() if session is not None else None
            output = session.output if session is not None else ""
        if session is not None and return_code is None:
            return {
                "account_id": account_id,
                "status": "connecting",
                "instructions": output,
                "complete": False,
                **_provider_setup_guidance(provider, output),
            }
        authenticated = verify and self._verify(provider=provider, environment=environment, account_home=account_home)
        if authenticated:
            self.homes.tighten_permissions(account_home=account_home)
            status = "ready"
            reason = ""
        elif session is not None and return_code not in {None, 0}:
            status = "error"
            reason = "Provider sign-in did not complete"
        else:
            status = "action_required"
            reason = "Complete provider sign-in to use this account"
        updated = self.store.update_provider_account_status(
            account_id=account_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
            status=status,
            reconnect_reason=reason,
            verified=authenticated,
        )
        if session is not None:
            with self._lock:
                self._sessions.pop(account_id, None)
            self._release_session(session)
        return {
            "account_id": account_id,
            "status": updated.get("status", status),
            "instructions": output,
            "complete": True,
            **_provider_setup_guidance(provider, output),
        }

    def cancel(self, *, account_id: str, tenant_id: str, owner_id: str) -> dict[str, object]:
        self._account(account_id=account_id, tenant_id=tenant_id, owner_id=owner_id)
        with self._lock:
            session = self._sessions.get(account_id)
            if session is None or session.tenant_id != tenant_id or session.owner_id != owner_id:
                raise ControlPlaneError("Provider account setup is not running")
            self._terminate_session_process(session)
            self._sessions.pop(account_id, None)
        self._release_session(session)
        updated = self.store.update_provider_account_status(
            account_id=account_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
            status="action_required",
            reconnect_reason="Provider setup was cancelled",
        )
        return {"account_id": account_id, "status": updated.get("status"), "complete": True}

    def disconnect(self, *, account_id: str, tenant_id: str, owner_id: str) -> dict[str, object]:
        account = self.store.get_provider_account_record(
            account_id=account_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
        if account is None:
            raise ControlPlaneError("Provider account not found for this user")
        with self._lock:
            session = self._sessions.get(account_id)
            if session is not None and (session.tenant_id != tenant_id or session.owner_id != owner_id):
                raise ControlPlaneError("Provider account not found for this user")
            if session is not None:
                self._terminate_session_process(session)
                self._sessions.pop(account_id, None)
        if session is not None:
            self._release_session(session)

        disconnect_lease = self.store.acquire_provider_lease(
            account_id=account_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
            lane="provider-disconnect",
            worker_id="provider-disconnect",
            run_id=f"disconnect:{account_id}",
            ttl_seconds=60,
            allowed_statuses=(
                "disconnected",
                "connecting",
                "ready",
                "action_required",
                "unavailable",
                "error",
            ),
        )
        self.store.update_provider_account_status(
            account_id=account_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
            status="action_required",
            reconnect_reason="Disconnect in progress",
        )

        provider = str(account.get("provider") or "").strip().lower()
        account_home = self.homes.account_home_path(
            tenant_id=tenant_id,
            owner_id=owner_id,
            account_id=account_id,
        )
        if account_home.exists() and not account_home.is_symlink():
            environment = self._environment(provider=provider, account_home=account_home)
            binary_name = "codex" if provider in {"codex", "openai"} else "claude"
            binary_env = "WPR_CODEX_CLI_PATH" if binary_name == "codex" else "WPR_CLAUDE_CODE_PATH"
            binary = str(os.environ.get(binary_env) or "").strip() or shutil.which(binary_name)
            if not binary:
                self.store.release_provider_lease(
                    lease_id=str(disconnect_lease["lease_id"]),
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                )
                raise ControlPlaneError(
                    "Provider logout is unavailable; the private account home was preserved"
                )
            logout_command = (
                [binary, "logout"]
                if binary_name == "codex"
                else [binary, "auth", "logout"]
            )
            try:
                logout_result = subprocess.run(
                    logout_command,
                    cwd=str(account_home),
                    env=environment,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=12,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                self.store.release_provider_lease(
                    lease_id=str(disconnect_lease["lease_id"]),
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                )
                raise ControlPlaneError(
                    "Provider logout failed; the private account home was preserved"
                ) from exc
            if logout_result.returncode != 0:
                self.store.release_provider_lease(
                    lease_id=str(disconnect_lease["lease_id"]),
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                )
                raise ControlPlaneError(
                    "Provider logout failed; the private account home was preserved"
                )
        self.homes.remove_home(
            tenant_id=tenant_id,
            owner_id=owner_id,
            account_id=account_id,
        )
        updated = self.store.disconnect_provider_account(
            account_id=account_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
        return {
            "account_id": account_id,
            "status": str(updated.get("status") or "disconnected"),
            "complete": True,
        }
