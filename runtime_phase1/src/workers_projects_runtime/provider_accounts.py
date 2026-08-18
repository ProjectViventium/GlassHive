from __future__ import annotations

import hashlib
import json
import os
import pty
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import termios
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

import fcntl

from .control_plane import (
    LEGACY_CREDENTIAL_CLEANUP_REASON,
    ControlPlaneConflict,
    ControlPlaneError,
)
from .inference_broker import (
    InferenceBrokerError,
    inference_broker_config_from_environment,
)


SAFE_ACCOUNT_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
ANSI_ESCAPE = re.compile(
    r"\x1B(?:\][^\x07]*?(?:\x07|\x1B\\)|\[[0-?]*[ -/]*[@-~]|[@-Z\\-_])"
)
MAX_SETUP_OUTPUT_CHARS = 32_000
MAX_SETUP_INPUT_BYTES = 1_024
PROVIDER_VERIFY_HEARTBEAT_INTERVAL_SECONDS = 10.0
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


def _provider_setup_guidance(provider: str, output: str) -> dict[str, str | bool]:
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
            is_native_claude_login = (
                hostname == "claude.com"
                and parsed.path.rstrip("/") == "/cai/oauth/authorize"
            )
            if hostname in {"claude.ai", "console.anthropic.com"} or is_native_claude_login:
                setup_url = candidate
                break

    setup_code = ""
    if setup_url and normalized_provider in {"codex", "openai"}:
        code_match = _CODEX_DEVICE_CODE.search(str(output or ""))
        if code_match:
            setup_code = code_match.group(1).upper()

    input_required = False
    if setup_url and normalized_provider in {"claude", "anthropic"}:
        code_values = parse_qs(urlsplit(setup_url).query).get("code", [])
        input_required = any(str(value).lower() == "true" for value in code_values)

    return {
        "provider": canonical_provider,
        "setup_url": setup_url,
        "setup_code": setup_code,
        "help_url": (
            _CODEX_SECURITY_SETTINGS_URL
            if normalized_provider in {"codex", "openai"}
            else ""
        ),
        "input_required": input_required,
    }


def _env_enabled(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def provider_setup_binary(provider: str) -> str | None:
    """Resolve the native setup CLI from the same canonical worker settings used at runtime."""

    normalized = str(provider or "").strip().lower()
    if normalized in {"codex", "openai"}:
        executable = "codex"
        env_names = ("WPR_CODEX_BIN", "WPR_CODEX_CLI_PATH")
    elif normalized in {"claude", "anthropic"}:
        executable = "claude"
        env_names = ("WPR_CLAUDE_CODE_BIN", "WPR_CLAUDE_CODE_PATH")
    else:
        return None
    configured = [str(os.environ.get(name) or "").strip() for name in env_names]
    configured = [value for value in configured if value]
    for value in configured:
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
            continue
        if resolved := shutil.which(value):
            return resolved
    # An explicit but invalid binary is deployment drift; do not silently select another CLI.
    if configured:
        return None
    return shutil.which(executable)


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
        return "supported" if provider_setup_binary(normalized_provider) else "setup_cli_required"
    if normalized_provider in {"codex", "openai"}:
        if not _env_enabled("GLASSHIVE_ENABLE_CODEX_PERSONAL_ACCOUNTS"):
            return "proof_required"
        return "supported" if provider_setup_binary(normalized_provider) else "setup_cli_required"
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
            return {
                "CLAUDE_CONFIG_DIR": str(target),
                "CLAUDE_SECURESTORAGE_CONFIG_DIR": str(target),
            }
        raise ControlPlaneError("Unsupported provider account home")

    def prepare_interactive_home(self, *, provider: str, account_home: Path) -> None:
        """Make a verified Claude login immediately reusable by its interactive CLI."""

        if provider not in {"claude", "anthropic"}:
            return
        config_dir = account_home / "claude"
        config_path = config_dir / ".claude.json"
        try:
            metadata = config_path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1
                or metadata.st_size > 1_048_576
            ):
                raise ControlPlaneError("Claude account state is not a safe managed file")
            state = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ControlPlaneError("Claude account state is unavailable") from exc
        if not isinstance(state, dict):
            raise ControlPlaneError("Claude account state is invalid")
        if state.get("hasCompletedOnboarding") is True:
            return
        state["hasCompletedOnboarding"] = True
        serialized = json.dumps(state, indent=2, sort_keys=True) + "\n"
        temp_fd, temp_name = tempfile.mkstemp(
            dir=config_dir,
            prefix=".glasshive-claude-onboarding-",
        )
        try:
            os.fchmod(temp_fd, 0o600)
            with os.fdopen(temp_fd, "w", encoding="utf-8", closefd=True) as handle:
                temp_fd = -1
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, config_path)
            directory_fd = os.open(
                config_dir,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temp_fd >= 0:
                os.close(temp_fd)
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def tighten_permissions(self, *, account_home: Path) -> None:
        """Validate and privatize credential state through no-follow directory descriptors."""

        if os.name == "nt":
            return
        resolved_root = self.root.resolve(strict=True)
        lexical_home = Path(os.path.abspath(account_home))
        try:
            relative_home = lexical_home.relative_to(resolved_root)
        except ValueError as exc:
            raise ControlPlaneError(
                "Provider account home is outside the managed credential root"
            ) from exc
        if not relative_home.parts:
            raise ControlPlaneError("Provider account home is too broad")
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        directory_fd = os.open(resolved_root, directory_flags)
        try:
            for part in relative_home.parts:
                child_fd = os.open(part, directory_flags, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = child_fd
            self._tighten_directory_fd(directory_fd)
        except (OSError, ValueError) as exc:
            raise ControlPlaneError(
                "Provider account home contains unsafe credential state"
            ) from exc
        finally:
            os.close(directory_fd)

    def _tighten_directory_fd(self, directory_fd: int) -> None:
        directory_stat = os.fstat(directory_fd)
        if not stat.S_ISDIR(directory_stat.st_mode) or directory_stat.st_uid != os.geteuid():
            raise ControlPlaneError("Provider account directory ownership is unsafe")
        os.fchmod(directory_fd, 0o700)
        for name in os.listdir(directory_fd):
            entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISLNK(entry.st_mode):
                raise ControlPlaneError("Provider account home contains an unsafe link")
            if entry.st_uid != os.geteuid():
                raise ControlPlaneError("Provider account entry ownership is unsafe")
            if stat.S_ISDIR(entry.st_mode):
                child_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_fd,
                )
                try:
                    opened = os.fstat(child_fd)
                    if (opened.st_dev, opened.st_ino) != (entry.st_dev, entry.st_ino):
                        raise ControlPlaneError("Provider account directory changed during validation")
                    self._tighten_directory_fd(child_fd)
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
                raise ControlPlaneError("Provider account home contains an unsafe file")
            os.chmod(name, 0o600, dir_fd=directory_fd, follow_symlinks=False)
            secured = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (secured.st_dev, secured.st_ino) != (entry.st_dev, entry.st_ino):
                raise ControlPlaneError("Provider account file changed during validation")

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
    input_submitted: bool = False
    finalizing: bool = False


class ProviderSetupManager:
    """Runs provider-native sign-in in a private per-user home.

    Setup output is capped and held in memory only. Provider credentials remain in the
    provider's own native home, outside workspace storage and the control-plane database.
    """

    def __init__(
        self,
        *,
        store: Any,
        home_root: Path,
        reconcile_provider_account_binding: Callable[[Path], None] | None = None,
    ) -> None:
        self.store = store
        self.homes = ProviderAccountHomeManager(home_root)
        self.reconcile_provider_account_binding = reconcile_provider_account_binding
        self._sessions: dict[str, _SetupSession] = {}
        self._lock = threading.RLock()

    def _reconcile_if_isolated(self, account_home: Path) -> None:
        isolation = str(
            os.environ.get("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION") or ""
        ).strip().lower()
        if isolation != "per_worker_container":
            return
        if self.reconcile_provider_account_binding is None:
            raise ControlPlaneError(
                "The reviewed provider-account container substrate is unavailable"
            )
        self.reconcile_provider_account_binding(account_home)

    def _binary(self, provider: str) -> str:
        binary = provider_setup_binary(provider)
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
        account_home = self.homes.account_home_path(
            tenant_id=tenant_id,
            owner_id=owner_id,
            account_id=account_id,
        )
        setup_command, _ = self._commands(provider)
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
                    required_recovery_code=str(account.get("recovery_code") or ""),
                )
                self._reconcile_if_isolated(account_home)
                account_home = self.homes.ensure_home(
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                    account_id=account_id,
                    provider=provider,
                )
                environment = self._environment(provider=provider, account_home=account_home)
                lock_path = account_home / ".setup.lock"
                lock_file = lock_path.open("a+b")
                if os.name != "nt":
                    lock_path.chmod(0o600)
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    lock_file.close()
                    raise ControlPlaneConflict("Provider account setup is already running") from exc
            except Exception:
                if "lease" in locals():
                    try:
                        self.store.update_provider_account_status(
                            account_id=account_id,
                            tenant_id=tenant_id,
                            owner_id=owner_id,
                            status="action_required",
                            reconnect_reason=LEGACY_CREDENTIAL_CLEANUP_REASON,
                            recovery_code="credential_cleanup_failed",
                        )
                    finally:
                        self.store.release_provider_lease(
                            lease_id=str(lease.get("lease_id") or ""),
                            tenant_id=tenant_id,
                            owner_id=owner_id,
                        )
                raise
            master_fd, slave_fd = pty.openpty()
            try:
                terminal_attributes = termios.tcgetattr(slave_fd)
                terminal_attributes[3] &= ~(
                    termios.ECHO | getattr(termios, "ECHONL", 0)
                )
                termios.tcsetattr(slave_fd, termios.TCSANOW, terminal_attributes)
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

    @staticmethod
    def _setup_input_bytes(value: str) -> bytes:
        normalized = str(value or "").strip()
        if len(normalized) > MAX_SETUP_INPUT_BYTES:
            raise ControlPlaneError("The authentication code is too long")
        if not normalized or any(
            not 0x21 <= ord(character) <= 0x7E for character in normalized
        ):
            raise ControlPlaneError("Enter the authentication code shown by the provider")
        encoded = normalized.encode("ascii")
        # A physical Enter key arrives as CR in raw terminal UIs such as Ink;
        # canonical line discipline maps it to NL for ordinary readline clients.
        return encoded + b"\r"

    def submit_input(
        self,
        *,
        account_id: str,
        tenant_id: str,
        owner_id: str,
        value: str,
    ) -> dict[str, object]:
        account = self._account(
            account_id=account_id, tenant_id=tenant_id, owner_id=owner_id
        )
        payload = self._setup_input_bytes(value)
        with self._lock:
            session = self._sessions.get(account_id)
            if (
                session is None
                or session.tenant_id != tenant_id
                or session.owner_id != owner_id
                or session.process.poll() is not None
            ):
                raise ControlPlaneConflict("Provider account setup is not waiting for input")
            if str(account.get("provider") or "").strip().lower() not in {"claude", "anthropic"}:
                raise ControlPlaneConflict("This provider sign-in does not accept browser input")
            if session.input_submitted:
                raise ControlPlaneConflict("The authentication code was already submitted")
            guidance = _provider_setup_guidance(session.provider, session.output)
            if not guidance.get("input_required"):
                raise ControlPlaneConflict("Provider account setup is not waiting for input")
            try:
                write_fd = os.dup(session.master_fd)
            except OSError as exc:
                raise ControlPlaneConflict(
                    "Provider account setup is no longer waiting for input"
                ) from exc
            # Reserve the one submission before releasing the process-wide lock.
            # The duplicate pins this exact PTY even if Cancel/Restart closes and
            # recycles the session's original descriptor before the write completes.
            session.input_submitted = True
            session.output = ""
        written = 0
        try:
            while written < len(payload):
                count = os.write(write_fd, payload[written:])
                if count <= 0:
                    raise OSError("provider input closed")
                written += count
        except OSError as exc:
            owns_cleanup = False
            with self._lock:
                if self._sessions.get(account_id) is session:
                    self._terminate_session_process(session)
                    self._sessions.pop(account_id, None)
                    owns_cleanup = True
            if owns_cleanup:
                self._release_session(session)
                self.store.update_provider_account_status(
                    account_id=account_id,
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                    status="action_required",
                    reconnect_reason="Provider sign-in input could not be delivered; restart sign-in",
                )
            raise ControlPlaneConflict(
                "Provider account setup is no longer waiting for input"
            ) from exc
        finally:
            os.close(write_fd)
        return self.status(account_id=account_id, tenant_id=tenant_id, owner_id=owner_id)

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
        account_home = self.homes.account_home_path(
            tenant_id=tenant_id,
            owner_id=owner_id,
            account_id=account_id,
        )
        finalization_in_progress = False
        with self._lock:
            session = self._sessions.get(account_id)
            if session is not None and (session.tenant_id != tenant_id or session.owner_id != owner_id):
                raise ControlPlaneError("Provider account not found for this user")
            return_code = session.process.poll() if session is not None else None
            if session is None or session.input_submitted:
                output = ""
            else:
                output = session.output
            if session is not None and return_code is not None:
                if session.finalizing:
                    finalization_in_progress = True
                else:
                    session.finalizing = True
        if session is not None and return_code is None:
            guidance = _provider_setup_guidance(provider, output)
            if session.input_submitted:
                guidance["input_required"] = False
            return {
                "account_id": account_id,
                "status": "connecting",
                "instructions": output,
                "complete": False,
                "input_submitted": session.input_submitted,
                **guidance,
            }
        if session is not None and finalization_in_progress:
            guidance = _provider_setup_guidance(provider, output)
            if session.input_submitted:
                guidance["input_required"] = False
            return {
                "account_id": account_id,
                "status": "connecting",
                "instructions": output,
                "complete": False,
                "input_submitted": session.input_submitted,
                **guidance,
            }
        verification_lease: dict[str, Any] | None = None
        verification_lease_stop = threading.Event()
        verification_lease_lost = threading.Event()
        verification_lease_thread: threading.Thread | None = None
        if session is None:
            verification_lease = self.store.acquire_provider_lease(
                account_id=account_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
                lane="provider-verify",
                worker_id="provider-verify",
                run_id=f"verify:{account_id}",
                ttl_seconds=60,
                allowed_statuses=(str(account.get("status") or "action_required"),),
                required_recovery_code=str(account.get("recovery_code") or ""),
            )
        try:
            if verification_lease is not None:
                lease_id = str(verification_lease.get("lease_id") or "")

                def renew_verification_lease() -> None:
                    while not verification_lease_stop.wait(
                        PROVIDER_VERIFY_HEARTBEAT_INTERVAL_SECONDS
                    ):
                        try:
                            self.store.heartbeat_provider_lease(
                                lease_id=lease_id,
                                tenant_id=tenant_id,
                                owner_id=owner_id,
                                ttl_seconds=120,
                            )
                        except (ControlPlaneError, OSError, sqlite3.OperationalError):
                            verification_lease_lost.set()
                            return

                # Extend before any Docker/image work, then keep the exclusive
                # lease alive across both seals and the provider CLI check.
                self.store.heartbeat_provider_lease(
                    lease_id=lease_id,
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                    ttl_seconds=120,
                )
                heartbeat_thread = threading.Thread(
                    target=renew_verification_lease,
                    name=f"glasshive-provider-verify-lease-{account_id[:24]}",
                    daemon=True,
                )
                heartbeat_thread.start()
                verification_lease_thread = heartbeat_thread

            def require_verification_lease() -> None:
                if verification_lease_lost.is_set():
                    raise ControlPlaneError(
                        "Provider verification lease was lost; check the connection again"
                    )

            self._reconcile_if_isolated(account_home)
            require_verification_lease()
            account_home = self.homes.ensure_home(
                tenant_id=tenant_id,
                owner_id=owner_id,
                account_id=account_id,
                provider=provider,
            )
            environment = self._environment(provider=provider, account_home=account_home)
            authenticated = verify and self._verify(
                provider=provider, environment=environment, account_home=account_home
            )
            require_verification_lease()
            if authenticated:
                self.homes.prepare_interactive_home(
                    provider=provider,
                    account_home=account_home,
                )
                # Provider status commands may recreate private cache wrappers
                # after the pre-verification seal. Reconcile again while the
                # exclusive verify lease is still held, then perform the final
                # descriptor-based host validation.
                self._reconcile_if_isolated(account_home)
                require_verification_lease()
                self.homes.tighten_permissions(account_home=account_home)
                status = "ready"
                reason = ""
            elif session is not None and return_code not in {None, 0}:
                status = "error"
                reason = "Provider sign-in did not complete"
            else:
                status = "action_required"
                reason = "Complete provider sign-in to use this account"
            if verification_lease is not None:
                self.store.heartbeat_provider_lease(
                    lease_id=str(verification_lease.get("lease_id") or ""),
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                    ttl_seconds=120,
                )
                require_verification_lease()
            updated = self.store.update_provider_account_status(
                account_id=account_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
                status=status,
                reconnect_reason=reason,
                verified=authenticated,
                recovery_code="" if authenticated else None,
            )
        except Exception:
            self.store.update_provider_account_status(
                account_id=account_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
                status="action_required",
                reconnect_reason="Provider credentials need a safe connection check",
                recovery_code="credential_cleanup_failed",
            )
            raise
        finally:
            if session is not None:
                owns_session = False
                with self._lock:
                    if self._sessions.get(account_id) is session:
                        self._sessions.pop(account_id, None)
                        owns_session = True
                if owns_session:
                    self._release_session(session)
            elif verification_lease is not None:
                verification_lease_stop.set()
                if verification_lease_thread is not None:
                    verification_lease_thread.join(timeout=2)
                self.store.release_provider_lease(
                    lease_id=str(verification_lease.get("lease_id") or ""),
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                )
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
