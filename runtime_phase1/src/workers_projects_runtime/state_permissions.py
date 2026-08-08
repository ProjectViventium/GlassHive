from __future__ import annotations

import os
from pathlib import Path


_ALLOWED_DIRECTORY_MODES = {0o700, 0o770}
_ALLOWED_FILE_MODES = {0o600, 0o660}


def _configured_mode(name: str, *, default: int, allowed: set[int]) -> int:
    raw = str(os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        mode = int(raw, 8)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an octal permission mode") from exc
    if mode not in allowed:
        choices = ", ".join(f"{candidate:04o}" for candidate in sorted(allowed))
        raise RuntimeError(f"{name} must be one of: {choices}")
    return mode


def state_directory_mode() -> int:
    return _configured_mode(
        "GLASSHIVE_STATE_DIR_MODE",
        default=0o700,
        allowed=_ALLOWED_DIRECTORY_MODES,
    )


def state_file_mode() -> int:
    return _configured_mode(
        "GLASSHIVE_STATE_FILE_MODE",
        default=0o600,
        allowed=_ALLOWED_FILE_MODES,
    )


def ensure_state_directory(path: Path) -> None:
    mode = state_directory_mode()
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    if os.name != "nt":
        path.chmod(mode)


def secure_state_file(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        path.chmod(state_file_mode())
    except FileNotFoundError:
        # SQLite may unlink transient WAL/SHM files between lookup and chmod.
        return
