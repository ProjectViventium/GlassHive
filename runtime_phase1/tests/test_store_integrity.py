from __future__ import annotations

import gc
import os
import sqlite3

import pytest

import workers_projects_runtime.store as store_module
from workers_projects_runtime.store import Store


def _open_file_descriptor_count() -> int:
    for directory in ("/dev/fd", "/proc/self/fd"):
        if os.path.isdir(directory):
            return len(os.listdir(directory))
    pytest.skip("This platform does not expose process file descriptors")


def _assert_connection_enforces_foreign_keys(
    store: Store,
    *,
    project_id: str,
) -> None:
    with store._connect() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS foreign_key_cascade_probe (
                probe_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(project_id)
                    ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "INSERT INTO foreign_key_cascade_probe (probe_id, project_id) "
            "VALUES (?, ?)",
            (f"probe-{project_id}", project_id),
        )
        conn.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM foreign_key_cascade_probe WHERE project_id = ?",
                (project_id,),
            ).fetchone()[0]
            == 0
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="FOREIGN KEY constraint failed",
        ):
            conn.execute(
                "INSERT INTO foreign_key_cascade_probe (probe_id, project_id) "
                "VALUES (?, ?)",
                (f"orphan-{project_id}", "missing-project"),
            )


def _project(store: Store, suffix: str) -> dict:
    return store.create_project(
        "owner",
        f"Foreign-key integrity {suffix}",
        "Reject orphans and honor declared cascades",
        "codex-cli",
    )


def test_store_enforces_foreign_keys_on_every_connection_and_reopen(tmp_path):
    db_path = tmp_path / "runtime.db"
    store = Store(str(db_path))

    first_project = _project(store, "first connection")
    _assert_connection_enforces_foreign_keys(
        store,
        project_id=first_project["project_id"],
    )

    second_project = _project(store, "second connection")
    _assert_connection_enforces_foreign_keys(
        store,
        project_id=second_project["project_id"],
    )

    reopened = Store(str(db_path))
    reopened_project = _project(reopened, "reopened store")
    _assert_connection_enforces_foreign_keys(
        reopened,
        project_id=reopened_project["project_id"],
    )


def test_store_enforces_foreign_keys_after_additive_migration(tmp_path):
    db_path = tmp_path / "runtime.db"
    store = Store(str(db_path))
    with store._connect() as conn:
        try:
            conn.execute("ALTER TABLE workers DROP COLUMN compute_released_at")
        except sqlite3.OperationalError as exc:
            pytest.skip(f"SQLite runtime does not support DROP COLUMN: {exc}")

    migrated = Store(str(db_path))
    with migrated._connect() as conn:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(workers)")
        }
        assert "compute_released_at" in columns
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    migrated_project = _project(migrated, "post migration")
    _assert_connection_enforces_foreign_keys(
        migrated,
        project_id=migrated_project["project_id"],
    )


def test_store_reopen_fails_closed_on_preexisting_foreign_key_violation(tmp_path):
    db_path = tmp_path / "runtime.db"
    Store(str(db_path))
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE foreign_key_reopen_probe (
                probe_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(project_id)
            )
            """
        )
        conn.execute(
            "INSERT INTO foreign_key_reopen_probe (probe_id, project_id) "
            "VALUES ('orphan', 'missing-project')"
        )

    with pytest.raises(
        RuntimeError,
        match="SQLite foreign-key integrity check failed",
    ):
        Store(str(db_path))


def test_store_closes_and_fails_when_foreign_keys_cannot_be_enabled(
    tmp_path,
    monkeypatch,
):
    class DisabledForeignKeyConnection:
        def __init__(self) -> None:
            self.closed = False

        def execute(self, statement: str):
            _ = statement
            return self

        def fetchone(self):
            return (0,)

        def close(self) -> None:
            self.closed = True

    connection = DisabledForeignKeyConnection()
    monkeypatch.setattr(
        store_module.sqlite3,
        "connect",
        lambda *_args, **_kwargs: connection,
    )

    with pytest.raises(
        RuntimeError,
        match="SQLite foreign-key enforcement could not be enabled",
    ):
        Store(str(tmp_path / "runtime.db"))

    assert connection.closed is True


def test_store_connection_context_closes_file_descriptors_without_gc(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    garbage_collection_was_enabled = gc.isenabled()
    if garbage_collection_was_enabled:
        gc.disable()

    try:
        before = _open_file_descriptor_count()
        for _ in range(256):
            with store._connect() as conn:
                assert conn.execute("SELECT 1").fetchone()[0] == 1
        after = _open_file_descriptor_count()

        assert after - before <= 4
    finally:
        if garbage_collection_was_enabled:
            gc.enable()
        gc.collect()
