from __future__ import annotations

import os
import sqlite3
import stat
from types import SimpleNamespace

import pytest

from workers_projects_runtime import store as store_module
from workers_projects_runtime import state_permissions as state_permissions_module
from workers_projects_runtime.control_plane import ControlPlaneStore
from workers_projects_runtime.schema_version import (
    UnsupportedSchemaVersionError,
    begin_schema_migration,
    record_schema_version,
    require_compatible_schema,
)
from workers_projects_runtime.store import Store
from workers_projects_runtime.state_permissions import state_directory_mode, state_file_mode


def test_schema_ledger_tracks_components_independently() -> None:
    connection = sqlite3.connect(":memory:")

    assert require_compatible_schema(
        connection,
        component="runtime_store",
        target_version=1,
    ) == 0
    record_schema_version(connection, component="runtime_store", version=1)

    assert require_compatible_schema(
        connection,
        component="runtime_store",
        target_version=1,
    ) == 1
    assert require_compatible_schema(
        connection,
        component="control_plane",
        target_version=1,
    ) == 0


def test_schema_ledger_rejects_newer_database_before_migration() -> None:
    connection = sqlite3.connect(":memory:")
    require_compatible_schema(connection, component="runtime_store", target_version=2)
    record_schema_version(connection, component="runtime_store", version=2)

    with pytest.raises(UnsupportedSchemaVersionError, match="newer than this runtime supports"):
        require_compatible_schema(
            connection,
            component="runtime_store",
            target_version=1,
        )


def test_schema_version_records_are_monotonic() -> None:
    connection = sqlite3.connect(":memory:")
    require_compatible_schema(connection, component="runtime_store", target_version=2)
    record_schema_version(connection, component="runtime_store", version=2)

    with pytest.raises(UnsupportedSchemaVersionError, match="Refusing to downgrade"):
        record_schema_version(connection, component="runtime_store", version=1)

    assert require_compatible_schema(
        connection,
        component="runtime_store",
        target_version=2,
    ) == 2


def test_schema_migration_lock_serializes_competing_versions(tmp_path) -> None:
    db_path = tmp_path / "runtime.db"
    first = sqlite3.connect(db_path, timeout=0.1)
    second = sqlite3.connect(db_path, timeout=0.1)
    begin_schema_migration(first)
    require_compatible_schema(first, component="runtime_store", target_version=2)

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        begin_schema_migration(second)

    record_schema_version(first, component="runtime_store", version=2)
    first.commit()
    begin_schema_migration(second)
    with pytest.raises(UnsupportedSchemaVersionError, match="newer than this runtime supports"):
        require_compatible_schema(second, component="runtime_store", target_version=1)
    second.rollback()


@pytest.mark.parametrize(
    ("component", "factory", "unexpected_table", "newer_version"),
    [
        ("runtime_store", Store, "projects", 9),
        ("control_plane", ControlPlaneStore, "provider_accounts", 5),
    ],
)
def test_stores_refuse_newer_schema_before_table_mutation(
    tmp_path,
    component,
    factory,
    unexpected_table,
    newer_version,
) -> None:
    db_path = tmp_path / f"{component}.db"
    connection = sqlite3.connect(db_path)
    require_compatible_schema(connection, component=component, target_version=newer_version)
    record_schema_version(connection, component=component, version=newer_version)
    connection.commit()
    connection.close()

    with pytest.raises(UnsupportedSchemaVersionError, match="newer than this runtime supports"):
        factory(str(db_path))

    inspection = sqlite3.connect(db_path)
    assert inspection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (unexpected_table,),
    ).fetchone() is None


def test_failed_store_migration_rolls_back_ledger_and_retries_safely(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "failed.db"

    def fail_after_partial_mutation(connection, script):
        connection.execute("CREATE TABLE partial_migration(value TEXT)")
        raise RuntimeError("synthetic migration failure")

    with monkeypatch.context() as scoped:
        scoped.setattr(store_module, "execute_schema_script", fail_after_partial_mutation)
        with pytest.raises(RuntimeError, match="synthetic migration failure"):
            Store(str(db_path))

    inspection = sqlite3.connect(db_path)
    assert inspection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'partial_migration'"
    ).fetchone() is None
    inspection.close()

    Store(str(db_path))
    verified = sqlite3.connect(db_path)
    assert require_compatible_schema(
        verified,
        component="runtime_store",
        target_version=8,
    ) == 8


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX permission contract")
@pytest.mark.parametrize("factory", [Store, ControlPlaneStore])
def test_split_service_state_permissions_are_group_accessible(tmp_path, monkeypatch, factory) -> None:
    state_dir = tmp_path / factory.__name__
    monkeypatch.setenv("GLASSHIVE_STATE_DIR_MODE", "0770")
    monkeypatch.setenv("GLASSHIVE_STATE_FILE_MODE", "0660")

    database = state_dir / "runtime.db"
    factory(str(database))

    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o770
    assert stat.S_IMODE(database.stat().st_mode) == 0o660


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX permission contract")
def test_runtime_accepts_prepared_root_owned_group_state_directory_without_chmod(
    tmp_path, monkeypatch
) -> None:
    state_dir = tmp_path / "prepared-state"
    state_dir.mkdir()
    descriptor = 91
    opened: dict[str, object] = {}
    chmod_calls: list[tuple[int, int]] = []

    def fake_open(path, flags):
        opened.update(path=path, flags=flags)
        return descriptor

    monkeypatch.setenv("GLASSHIVE_STATE_DIR_MODE", "0770")
    monkeypatch.setattr(state_permissions_module.os, "open", fake_open)
    monkeypatch.setattr(
        state_permissions_module.os,
        "fstat",
        lambda value: SimpleNamespace(
            st_mode=stat.S_IFDIR | 0o770,
            st_uid=0,
            st_gid=2200,
        ),
    )
    monkeypatch.setattr(state_permissions_module.os, "geteuid", lambda: 1200)
    monkeypatch.setattr(state_permissions_module.os, "getegid", lambda: 1200)
    monkeypatch.setattr(state_permissions_module.os, "getgroups", lambda: [2200])
    monkeypatch.setattr(
        state_permissions_module.os,
        "fchmod",
        lambda value, mode: chmod_calls.append((value, mode)),
    )
    monkeypatch.setattr(state_permissions_module.os, "close", lambda value: None)

    state_permissions_module.ensure_state_directory(state_dir)

    assert opened["path"] == state_dir
    assert int(opened["flags"]) & getattr(state_permissions_module.os, "O_DIRECTORY", 0)
    assert int(opened["flags"]) & getattr(state_permissions_module.os, "O_NOFOLLOW", 0)
    assert chmod_calls == []


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX permission contract")
@pytest.mark.parametrize(
    ("prepared_mode", "prepared_gid"),
    [(0o700, 2200), (0o775, 2200), (0o770, 3300)],
)
def test_runtime_rejects_untrusted_root_owned_state_directory(
    tmp_path,
    monkeypatch,
    prepared_mode,
    prepared_gid,
) -> None:
    state_dir = tmp_path / "unsafe-state"
    state_dir.mkdir()
    monkeypatch.setenv("GLASSHIVE_STATE_DIR_MODE", "0770")
    monkeypatch.setattr(state_permissions_module.os, "open", lambda path, flags: 92)
    monkeypatch.setattr(
        state_permissions_module.os,
        "fstat",
        lambda value: SimpleNamespace(
            st_mode=stat.S_IFDIR | prepared_mode,
            st_uid=0,
            st_gid=prepared_gid,
        ),
    )
    monkeypatch.setattr(state_permissions_module.os, "geteuid", lambda: 1200)
    monkeypatch.setattr(state_permissions_module.os, "getegid", lambda: 1200)
    monkeypatch.setattr(state_permissions_module.os, "getgroups", lambda: [2200])
    monkeypatch.setattr(state_permissions_module.os, "close", lambda value: None)

    with pytest.raises(PermissionError, match="prepared state directory"):
        state_permissions_module.ensure_state_directory(state_dir)


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX permission contract")
def test_runtime_accepts_prepared_root_owned_group_state_file_without_chmod(
    tmp_path, monkeypatch
) -> None:
    state_file = tmp_path / "runtime.db"
    state_file.touch()
    chmod_calls: list[tuple[object, int]] = []

    def refuse_open(path, flags, *args, **kwargs):
        raise AssertionError(f"state hardening must not open a descriptor on {path}")

    monkeypatch.setenv("GLASSHIVE_STATE_FILE_MODE", "0660")
    monkeypatch.setattr(state_permissions_module.os, "open", refuse_open)
    monkeypatch.setattr(
        state_permissions_module.os,
        "lstat",
        lambda value: SimpleNamespace(
            st_mode=stat.S_IFREG | 0o660,
            st_uid=0,
            st_gid=2200,
        ),
    )
    monkeypatch.setattr(state_permissions_module.os, "geteuid", lambda: 1200)
    monkeypatch.setattr(state_permissions_module.os, "getegid", lambda: 1200)
    monkeypatch.setattr(state_permissions_module.os, "getgroups", lambda: [2200])
    monkeypatch.setattr(
        state_permissions_module.os,
        "chmod",
        lambda value, mode, **kwargs: chmod_calls.append((value, mode)),
    )

    state_permissions_module.secure_state_file(state_file)

    assert chmod_calls == []


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX permission contract")
def test_runtime_rejects_root_owned_state_file_outside_process_group(
    tmp_path, monkeypatch
) -> None:
    state_file = tmp_path / "runtime.db"
    state_file.touch()
    monkeypatch.setenv("GLASSHIVE_STATE_FILE_MODE", "0660")
    monkeypatch.setattr(
        state_permissions_module.os,
        "lstat",
        lambda value: SimpleNamespace(
            st_mode=stat.S_IFREG | 0o660,
            st_uid=0,
            st_gid=3300,
        ),
    )
    monkeypatch.setattr(state_permissions_module.os, "geteuid", lambda: 1200)
    monkeypatch.setattr(state_permissions_module.os, "getegid", lambda: 1200)
    monkeypatch.setattr(state_permissions_module.os, "getgroups", lambda: [2200])

    with pytest.raises(PermissionError, match="prepared state file"):
        state_permissions_module.secure_state_file(state_file)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission contract")
def test_runtime_rejects_fifo_state_file_without_blocking(tmp_path, monkeypatch) -> None:
    state_file = tmp_path / "runtime.db"
    os.mkfifo(state_file)
    monkeypatch.setenv("GLASSHIVE_STATE_FILE_MODE", "0660")

    with pytest.raises(PermissionError, match="not a file"):
        state_permissions_module.secure_state_file(state_file)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GLASSHIVE_STATE_DIR_MODE", "0777"),
        ("GLASSHIVE_STATE_FILE_MODE", "0666"),
        ("GLASSHIVE_STATE_FILE_MODE", "not-octal"),
    ],
)
def test_state_permissions_reject_unsafe_or_invalid_modes(tmp_path, monkeypatch, name, value) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match=name):
        if name == "GLASSHIVE_STATE_DIR_MODE":
            state_directory_mode()
        else:
            state_file_mode()


def test_parallel_schema_replaces_stale_callback_trace_triggers(tmp_path) -> None:
    """A database from a runtime without the run-id guard keeps its old triggers under IF NOT EXISTS.

    The installed runtime hit exactly this: `worker.resumed_by_alias` on pre-run alias reuse is a
    runless lifecycle callback, and the stale trace trigger inserted a NULL run into the NOT NULL
    trace fence, failing `POST /workers/find-or-resume` with a 500 on a scheduled journey.
    """

    import re

    from workers_projects_runtime import parallel_orchestration_schema as schema_module

    db_path = tmp_path / "legacy-triggers.sqlite3"
    store = Store(str(db_path))
    project = store.create_project("owner-a", "Legacy triggers", "Goal", "codex-cli")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Legacy worker",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode="host",
    )
    stale_bodies: dict[str, str] = {}
    for name in schema_module.RECONCILED_TRIGGERS:
        expected = schema_module.expected_trigger_statement(name)
        stale = re.sub(
            r"\)\s*WHERE NEW\.run_id IS NOT NULL AND NEW\.run_id <> '';", ");", expected
        )
        assert stale != expected
        stale_bodies[name] = stale
    with store._connect() as conn:
        for name, stale in stale_bodies.items():
            conn.execute(f"DROP TRIGGER {name}")
            conn.execute(stale)

    def runless_callback(target: Store, callback_id: str):
        return target.upsert_callback_outbox(
            callback_id=callback_id,
            project_id=project["project_id"],
            worker_id=worker["worker_id"],
            run_id=None,
            event_type="worker.resumed_by_alias",
            url="https://callbacks.example/hook",
            payload_json="{}",
        )

    # Legacy shape reproduced: the pre-run lifecycle callback trips the trace fence.
    with pytest.raises(sqlite3.IntegrityError):
        runless_callback(store, "cb-legacy")

    reopened = Store(str(db_path))
    with reopened._connect() as conn:
        for name in schema_module.RECONCILED_TRIGGERS:
            installed = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?", (name,)
            ).fetchone()[0]
            assert schema_module.normalized_trigger_sql(
                installed
            ) == schema_module.normalized_trigger_sql(
                schema_module.expected_trigger_statement(name)
            )
        # Deterministic: a second pass finds nothing to replace.
        assert schema_module.reconcile_stale_triggers(conn) == []

    record = runless_callback(reopened, "cb-runless")
    assert record["event_type"] == "worker.resumed_by_alias"
    assert record["run_id"] is None
    with reopened._connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM callback_trace_events WHERE callback_id = ?",
                ("cb-runless",),
            ).fetchone()[0]
            == 0
        )
        # The update trigger was replaced too: delivery bookkeeping on the runless row succeeds.
        conn.execute(
            "UPDATE callback_outbox SET attempts = attempts + 1, updated_at = updated_at WHERE callback_id = ?",
            ("cb-runless",),
        )
