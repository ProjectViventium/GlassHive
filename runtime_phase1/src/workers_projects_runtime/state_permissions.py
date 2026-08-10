from __future__ import annotations

import os
import stat
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


def _secure_state_path(path: Path, *, mode: int, directory: bool) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    else:
        flags |= getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        expected_type = stat.S_ISDIR if directory else stat.S_ISREG
        label = "directory" if directory else "file"
        if not expected_type(metadata.st_mode):
            raise PermissionError(f"GlassHive state path is not a {label}")
        if metadata.st_uid == os.geteuid():
            os.fchmod(descriptor, mode)
            return
        process_groups = {os.getegid(), *os.getgroups()}
        prepared_mode = 0o770 if directory else 0o660
        if (
            metadata.st_uid == 0
            and mode == prepared_mode
            and metadata.st_gid in process_groups
            and stat.S_IMODE(metadata.st_mode) == mode
        ):
            return
        raise PermissionError(
            f"GlassHive prepared state {label} has unexpected ownership or permissions"
        )
    finally:
        os.close(descriptor)


def ensure_state_directory(path: Path) -> None:
    mode = state_directory_mode()
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    if os.name != "nt":
        _secure_state_path(path, mode=mode, directory=True)


def secure_state_file(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        _secure_state_path(path, mode=state_file_mode(), directory=False)
    except FileNotFoundError:
        # SQLite may unlink transient WAL/SHM files between lookup and chmod.
        return
