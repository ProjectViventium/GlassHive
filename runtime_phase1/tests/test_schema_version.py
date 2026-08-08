from __future__ import annotations

import sqlite3
import stat

import pytest

from workers_projects_runtime import store as store_module
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
        ("runtime_store", Store, "projects", 5),
        ("control_plane", ControlPlaneStore, "provider_accounts", 4),
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
        target_version=4,
    ) == 4


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
