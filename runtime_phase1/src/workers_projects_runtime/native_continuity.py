"""GlassHive's credential-free, quiescent Native continuity boundary."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import time
from pathlib import Path

from .control_plane import CONTROL_PLANE_SCHEMA_VERSION, ControlPlaneStore
from .deliverables import NON_DELIVERABLE_DIR_NAMES
from .models import utc_now
from .profile_runtime import ProfiledWorkerRuntime
from .service import _copy_regular_workspace_file, _workspace_copy_plan
from .store import (CALLBACK_TRACE_AUTHORITY_FIELDS, COMPUTE_OPERATION_CLEAR_FIELDS,
    NONTERMINAL_RUN_STATES, RUNTIME_STORE_SCHEMA_VERSION, Store,
    _callback_trace_authority_values, _callback_trace_event_sha256_values,
    _text_sha256, canonical_parallel_clean_room_bootstrap, verified_callback_trace_snapshots)


def _require_schema(connection: sqlite3.Connection) -> None:
    observed = dict(connection.execute("SELECT component, version FROM glasshive_schema_versions"))
    if observed != {"runtime_store": RUNTIME_STORE_SCHEMA_VERSION, "control_plane": CONTROL_PLANE_SCHEMA_VERSION}:
        raise ValueError("GlassHive continuity requires the current reviewed component schema")
    # Build the expected shape through the actual schema owners, not a duplicate
    # column registry. Ledger equality alone cannot authorize unknown persisted data.
    def shape(database: sqlite3.Connection) -> tuple[dict, dict]:
        objects = database.execute("SELECT type, name, tbl_name, sql FROM sqlite_master").fetchall()
        tables = {}
        definitions = {}
        for kind, name, table, sql in objects:
            if kind == "table":
                tables[name] = {tuple(row)[1:] for row in database.execute("SELECT * FROM pragma_table_xinfo(?)", (name,))}
            else:
                definitions[(kind, name, table)] = sql
        return tables, definitions

    with tempfile.TemporaryDirectory(prefix="glasshive-continuity-schema-") as temporary:
        path = Path(temporary) / "schema.sqlite"
        baseline = Store(str(path))
        try:
            ControlPlaneStore(str(path))
            with baseline._connect() as reference:
                if shape(connection) != shape(reference):
                    raise ValueError("GlassHive continuity contains an unreviewed schema shape")
        finally:
            baseline.close()


def _private_tree(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        metadata = path.lstat()
        if path.is_symlink() or not (path.is_file() or path.is_dir()) or metadata.st_uid != os.getuid() or (path.is_file() and metadata.st_nlink != 1):
            raise ValueError("GlassHive continuity contains unsafe state")
        path.chmod(0o700 if path.is_dir() or metadata.st_mode & stat.S_IXUSR else 0o600)


def _worker_paths(runtime: ProfiledWorkerRuntime, worker: dict, root: Path) -> tuple[Path, Path]:
    worker_id = str(worker["worker_id"])
    if not worker_id or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for character in worker_id):
        raise ValueError("GlassHive continuity worker identity is unsafe")
    owner = runtime._runtime_for_worker(worker)
    state, workspace = owner._state_dir(worker_id), owner._workspace_dir(worker_id)
    state.relative_to(root)
    workspace.relative_to(root)
    return state, workspace


def _require_quiescent(connection: sqlite3.Connection, database: Path) -> None:
    placeholders = ",".join("?" for _ in NONTERMINAL_RUN_STATES)
    if connection.execute(f"SELECT 1 FROM runs WHERE state IN ({placeholders}) LIMIT 1", tuple(NONTERMINAL_RUN_STATES)).fetchone():
        raise ValueError("GlassHive active work must be quiesced before continuity")
    if connection.execute("SELECT 1 FROM workers WHERE state IN ('running','starting','resuming','terminating','termination_failed') LIMIT 1").fetchone():
        raise ValueError("GlassHive worker compute must be quiesced before continuity")
    if connection.execute("SELECT 1 FROM host_run_leases WHERE status IN ('active','reserved') LIMIT 1").fetchone():
        raise ValueError("GlassHive live worker leases must be quiesced before continuity")
    if connection.execute("SELECT 1 FROM workspace_gc_tombstones WHERE phase!='completed' LIMIT 1").fetchone():
        raise ValueError("GlassHive workspace cleanup must settle before continuity")
    connection.row_factory = sqlite3.Row
    workers = [dict(row) for row in connection.execute("SELECT * FROM workers")]
    if not workers:
        return
    runtime = ProfiledWorkerRuntime(base_dir=str(database.parent), create_directories=False)
    for worker in workers:
        owner = runtime._runtime_for_worker(worker)
        state_dir = owner._state_dir(worker["worker_id"])
        if worker.get("state_dir") and Path(worker["state_dir"]) != state_dir:
            raise ValueError("GlassHive recorded state path differs from its Native runtime owner")
        if worker["execution_mode"] == "host":
            session_path = owner._active_session_meta_path(worker["worker_id"])
            if session_path.is_symlink() or (session_path.exists() and not owner._read_active_session(worker["worker_id"])):
                raise ValueError("GlassHive worker session state is unreadable; absence is unproved")
            absent = runtime.host_active_process_status(worker).get("state") == "absent"
        else:
            absent = runtime.host_process_absence(worker, str(worker.get("last_run_id") or ""))
        if not absent:
            raise ValueError("GlassHive worker process absence is unproved; continuity cannot replace its state")


def check_quiescent(database: Path) -> None:
    metadata = database.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_nlink != 1:
        raise ValueError("GlassHive database is not an owned regular file")
    with sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True) as connection:
        _require_schema(connection)
        _require_quiescent(connection, database)


def _durable_bootstrap(value: object) -> dict:
    """Retain task meaning through the existing typed projection, without grants."""
    projected = canonical_parallel_clean_room_bootstrap(value)
    result = {key: projected[key] for key in (
        "project_definition", "developer_instructions", "system_instructions",
        "agents_md", "claude_md", "codex_md", "env", "execution_policy",
        "viventium_constraint_source", "viventium_run_liveness",
        "viventium_delegation_context", "viventium_delegation_identity",
        "viventium_feelings_projection",
    ) if key in projected}
    if "viventium_delegation_packet" in projected:
        packet = projected["viventium_delegation_packet"]
        result["viventium_delegation_packet"] = {key: packet[key] for key in ("version", "task", "explicit_constraints", "selected_files") if key in packet}
    return result


def _require_exportable_trace(connection: sqlite3.Connection) -> None:
    for row in connection.execute("SELECT snapshot_json FROM callback_trace_events"):
        if json.loads(row[0]).get("deliveryLeaseToken"):
            raise ValueError("GlassHive portable callback history contains delivery authority")


def _verified_traces(store: Store) -> tuple[dict[str, list], dict[str, list]]:
    """Use the live readers on the exact SQLite copy before changing any history."""
    callbacks: dict[str, list] = {}
    work: dict[str, list] = {}
    with store._connect() as connection:
        if connection.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("GlassHive continuity has invalid record references")
        for row in connection.execute("SELECT * FROM callback_trace_events ORDER BY run_id, run_sequence"):
            callbacks.setdefault(row["run_id"], []).append(row)
        for row in connection.execute("SELECT * FROM work_trace_events ORDER BY run_id, sequence"):
            work.setdefault(row["run_id"], []).append(row)
        projections = connection.execute("SELECT run_id, payload_json FROM events WHERE event_type='continuity.projected'").fetchall()
    for run_id, rows in callbacks.items():
        verified_callback_trace_snapshots(rows, run_id)
    for run_id, rows in work.items():
        if store.work_trace_detail(run_id=run_id, tenant_id=rows[0]["tenant_id"], owner_id=rows[0]["owner_id"]) is None:
            raise ValueError("GlassHive work trace has no authorized owner")
        ledger = {"sha256:" + row["event_sha256"]: row for row in callbacks.get(run_id, [])}
        for event in rows:
            if event["event_type"] != "callback.delivery":
                continue
            receipt = json.loads(event["payload_json"])
            callback = ledger.get(receipt.get("eventSha256"))
            if callback is None:
                raise ValueError("GlassHive work trace references missing callback history")
            expected = {"authoritySha256": callback["authority_sha256"], "payloadSha256": callback["payload_sha256"],
                "ledgerSequence": callback["run_sequence"], "callbackRevision": callback["callback_sequence"],
                "callbackRef": "callback_sha256:" + _text_sha256(callback["callback_id"]),
                "previousEventSha256": "sha256:" + callback["previous_event_sha256"] if callback["previous_event_sha256"] else None}
            if any(receipt.get(key) != value for key, value in expected.items()):
                raise ValueError("GlassHive work trace callback evidence disagrees")
    for row in projections:
        value = json.loads(row["payload_json"])
        if not isinstance(value, dict):
            raise ValueError("GlassHive continuity trace provenance is invalid")
        run_callbacks, run_work = callbacks.get(row["run_id"], []), work.get(row["run_id"], [])
        for prefix, rows in (("Callback", run_callbacks), ("Work", run_work)):
            count = value.get(f"exported{prefix}EventCount")
            if (value.get("version") != 1 or not isinstance(count, int) or isinstance(count, bool)
                or count < 0 or count > len(rows)):
                raise ValueError("GlassHive continuity trace provenance is invalid")
            expected = "sha256:" + (rows[count - 1]["event_sha256"] if count else "0" * 64)
            if value.get(f"exported{prefix}HeadSha256") != expected:
                raise ValueError("GlassHive continuity trace provenance is invalid")
            original = value.get(f"source{prefix}HeadSha256", "")
            if not isinstance(original, str) or len(original) != 71 or not original.startswith("sha256:") or any(c not in "0123456789abcdef" for c in original[7:]):
                raise ValueError("GlassHive continuity trace provenance is invalid")
        matching = [event for event in run_work if event["event_type"] == "continuity.projected"
                    and json.loads(event["payload_json"]) == value]
        if run_work and len(matching) != 1:
            raise ValueError("GlassHive continuity trace provenance is not linked")
    for run_id, rows in work.items():
        for event in rows:
            if event["event_type"] == "continuity.projected" and sum(
                row["run_id"] == run_id and json.loads(row["payload_json"]) == json.loads(event["payload_json"])
                for row in projections
            ) != 1:
                raise ValueError("GlassHive continuity trace provenance is not linked")
    return callbacks, work


def _project_callback_history(store: Store) -> set[str]:
    """Project only the disposable capture DB; live append-only triggers never change."""
    callbacks, work = _verified_traces(store)
    tokens = {str(json.loads(row["snapshot_json"])["deliveryLeaseToken"])
              for rows in callbacks.values() for row in rows
              if json.loads(row["snapshot_json"])["deliveryLeaseToken"]}
    if not tokens:
        return tokens

    def redact(value: str) -> str:
        def replace(text: str) -> str:
            for token in sorted(tokens, key=len, reverse=True):
                text = text.replace(token, "[REDACTED_CALLBACK_LEASE]")
            return text
        return _map_json_text(value, replace)

    changed_runs = {run_id for run_id, rows in callbacks.items()
                    if any(json.loads(row["snapshot_json"])["deliveryLeaseToken"] for row in rows)}
    changed_fields = {"callback_trace_events.snapshot_json", "callback_outbox.delivery_lease_token"}
    replacements: dict[str, dict] = {}
    trigger_names = ("callback_trace_events_append_only_update", "work_trace_events_append_only_update",
                     "callback_outbox_trace_update", "callback_outbox_authority_immutable")
    with store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        triggers = {name: connection.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone()[0]
                    for name in trigger_names}
        for name in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')
        # These are existing copies of callback observations, not a second history representation.
        for table, key, columns in (("callback_outbox", "callback_id", ("url", "payload_json", "last_error")),
                                    ("events", "event_id", ("message", "payload_json")),
                                    ("provider_activity", "sequence_id", ("summary", "payload_json"))):
            for row in connection.execute(f'SELECT * FROM "{table}"').fetchall():
                for column in columns:
                    original = row[column]
                    projected = redact(original) if isinstance(original, str) else original
                    if projected != original:
                        connection.execute(f'UPDATE "{table}" SET "{column}"=? WHERE "{key}"=?', (projected, row[key]))
                        changed_fields.add(f"{table}.{column}")
        connection.execute("UPDATE callback_outbox SET delivery_lease_token='', delivery_lease_expires_at=NULL")
        for run_id, rows in callbacks.items():
            previous = ""
            for row in rows:
                snapshot = json.loads(redact(row["snapshot_json"]))
                if snapshot["deliveryLeaseToken"]:
                    snapshot["deliveryLeaseExpiresAt"] = None
                snapshot["deliveryLeaseToken"] = ""
                encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                event_sha = _callback_trace_event_sha256_values(row["callback_id"], row["callback_sequence"], row["run_sequence"], encoded, previous)
                payload_sha = "sha256:" + _text_sha256(snapshot["payloadJson"])
                authority_sha = "sha256:" + _text_sha256(_callback_trace_authority_values(*(snapshot[field] for field in CALLBACK_TRACE_AUTHORITY_FIELDS)))
                if encoded != row["snapshot_json"]:
                    changed_runs.add(run_id)
                replacements[row["event_sha256"]] = {"eventSha256": "sha256:" + event_sha,
                    "previousEventSha256": "sha256:" + previous if previous else None,
                    "payloadSha256": payload_sha, "authoritySha256": authority_sha}
                connection.execute("UPDATE callback_trace_events SET snapshot_json=?, payload_sha256=?, authority_sha256=?, previous_event_sha256=?, event_sha256=? WHERE callback_trace_event_id=?",
                    (encoded, payload_sha, authority_sha, previous, event_sha, row["callback_trace_event_id"]))
                previous = event_sha
        for run_id, rows in work.items():
            previous = ""
            for row in rows:
                payload = json.loads(redact(row["payload_json"]))
                if row["event_type"] == "callback.delivery":
                    old_sha = str(payload.get("eventSha256") or "").removeprefix("sha256:")
                    if old_sha not in replacements:
                        raise ValueError("GlassHive work trace references missing callback history")
                    payload.update(replacements[old_sha])
                event_sha = store._work_trace_event_sha256(trace_event_id=row["trace_event_id"], run_id=run_id,
                    work_ref=row["work_ref"], sequence=row["sequence"], event_type=row["event_type"], payload=payload,
                    previous_event_sha256=previous, created_at=row["created_at"])
                if event_sha != row["event_sha256"]:
                    changed_runs.add(run_id)
                    changed_fields.add("work_trace_events.payload_json")
                connection.execute("UPDATE work_trace_events SET payload_json=?, previous_event_sha256=?, event_sha256=? WHERE trace_event_id=?",
                    (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")), previous, event_sha, row["trace_event_id"]))
                previous = event_sha
        for name, sql in triggers.items():
            connection.execute(sql)
        for run_id in sorted(changed_runs):
            source_callbacks, source_work = callbacks.get(run_id, []), work.get(run_id, [])
            value = {"version": 1, "redactedFields": sorted(changed_fields)}
            for prefix, table, rows in (("Callback", "callback_trace_events", source_callbacks), ("Work", "work_trace_events", source_work)):
                order = "run_sequence" if prefix == "Callback" else "sequence"
                latest = connection.execute(f'SELECT event_sha256 FROM "{table}" WHERE run_id=? ORDER BY "{order}" DESC LIMIT 1', (run_id,)).fetchone()
                value[f"source{prefix}HeadSha256"] = "sha256:" + (rows[-1]["event_sha256"] if rows else "0" * 64)
                value[f"exported{prefix}HeadSha256"] = "sha256:" + (latest[0] if latest else "0" * 64)
                value[f"exported{prefix}EventCount"] = len(rows)
            now = utc_now()
            identity = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
            run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            connection.execute("INSERT INTO events(event_id, project_id, worker_id, tenant_id, run_id, event_type, message, payload_json, created_at) VALUES (?, ?, ?, ?, ?, 'continuity.projected', 'Credential-free history captured; source heads retained', ?, ?)",
                ("evt_continuity_" + identity, run["project_id"], run["worker_id"], run["tenant_id"], run_id, json.dumps(value, sort_keys=True), now))
            if source_work:
                scope = source_work[0]
                store._append_work_trace_event_conn(connection, trace_event_id="trace_continuity_" + identity,
                    run_id=run_id, work_ref=scope["work_ref"], tenant_id=scope["tenant_id"], owner_id=scope["owner_id"],
                    event_type="continuity.projected", payload=value, created_at=now)
    _verified_traces(store)
    return tokens


def _map_json_text(value: str, transform) -> str:
    """Visit decoded values, keys and embedded JSON before inspecting serialized bytes."""
    def visit(item):
        if isinstance(item, str):
            return _map_json_text(item, transform)
        if isinstance(item, list):
            return [visit(child) for child in item]
        if isinstance(item, dict):
            projected = {visit(key): visit(child) for key, child in item.items()}
            if len(projected) != len(item):
                raise ValueError("GlassHive callback projection would merge history fields")
            return projected
        return item

    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return transform(value)
    projected = visit(decoded)
    if projected != decoded:
        return json.dumps(projected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return transform(value)


def _require_no_callback_credentials(root: Path, tokens: set[str] | frozenset[str] = frozenset()) -> None:
    """Reject unhandled duplicates in decoded state and serialized/free-page/file bytes."""
    known = tuple(token.encode() for token in tokens)
    def inspect_bytes(value: bytes) -> None:
        if any(token in value for token in known) or re.search(rb"cbdel_[0-9a-f]{32}", value):
            raise ValueError("GlassHive portable state contains a callback credential copy")

    def inspect_text(value: str) -> str:
        inspect_bytes(value.encode())
        return value

    with sqlite3.connect(f"{(root / 'runtime.sqlite').resolve().as_uri()}?mode=ro", uri=True) as connection:
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            quoted = table.replace('"', '""')
            for row in connection.execute(f'SELECT * FROM "{quoted}"'):
                for value in row:
                    if isinstance(value, str):
                        _map_json_text(value, inspect_text)
    # Decode Unicode escapes conservatively in any file, including serialized JSON
    # strings and SQLite free pages. Semantic JSON traversal above is unbounded by chunks.
    overlap = max(4096, max((len(value) * 6 for value in known), default=0))
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        tail = b""
        with path.open("rb") as handle:
            while block := handle.read(1024 * 1024):
                block = tail + block
                inspect_bytes(block)
                decoded = block
                while True:
                    projected = re.sub(rb"\\+u00([0-9a-fA-F]{2})", lambda match: bytes([int(match[1], 16)]), decoded)
                    if projected == decoded:
                        break
                    decoded = projected
                inspect_bytes(decoded)
                tail = block[-overlap:]


def _reset_authority(store: Store) -> None:
    """Invalidate execution authority without turning unfinished work into success."""
    now = utc_now()
    for worker in store.list_all_workers():
        store.update_worker(
            worker["worker_id"], **COMPUTE_OPERATION_CLEAR_FIELDS,
            state=worker["state"] if worker["state"] in {"terminated", "failed"} else "paused",
            pid=None, gateway_token=None, session_key=None, gateway_url=None,
            gateway_port=None, takeover_url=None, control_url=None,
            bootstrap_bundle_json=json.dumps(_durable_bootstrap(json.loads(worker.get("bootstrap_bundle_json") or "null"))),
        )
    with store._connect() as connection:
        connection.execute("PRAGMA secure_delete=ON")
        connection.execute("UPDATE provider_accounts SET status=CASE WHEN status='disconnected' THEN status ELSE 'action_required' END, secret_locator='', last_verified_at=NULL, reconnect_reason=CASE WHEN status='disconnected' THEN reconnect_reason ELSE 'restore_reauthentication_required' END")
        connection.execute("UPDATE control_plane_connections SET status='action_required', secret_locator='', last_verified_at=NULL, error_code='restore_reauthentication_required'")
        connection.execute("UPDATE provider_account_leases SET released_at=COALESCE(released_at, ?)", (time.time(),))
        connection.execute("UPDATE workspace_capability_grants SET revoked_at=COALESCE(revoked_at, ?), prior_bootstrap_bundle_json='{}', applied_bootstrap_bundle_json='{}'", (time.time(),))
        connection.execute("UPDATE control_plane_pending_changes SET status='expired', confirmation_hash='' WHERE status='pending'")
        connection.execute("UPDATE schedule_principal_authority SET enabled=0, authority_epoch=authority_epoch+1")
        connection.execute("UPDATE host_run_leases SET status='released', pid=NULL, process_group=NULL, process_start_identity='', startup_token='', startup_state='aborted', released_at=COALESCE(released_at, ?), release_reason='restore_reauthentication_required'", (now,))
        connection.execute("UPDATE preflight_capacity_reservations SET status='released', released_at=COALESCE(released_at, ?), release_reason='restore_reauthentication_required'", (now,))
        connection.execute("DELETE FROM internal_assertion_replay_cache")
        connection.execute("DELETE FROM service_assertion_nonces")
        for row in connection.execute("SELECT run_id, runtime_bundle_json FROM runs WHERE runtime_bundle_json IS NOT NULL").fetchall():
            connection.execute("UPDATE runs SET runtime_bundle_json=? WHERE run_id=?", (json.dumps(_durable_bootstrap(json.loads(row["runtime_bundle_json"]))), row["run_id"]))
        connection.execute("UPDATE runs SET native_capabilities_json='{}', capacity_reservation_json='{}', native_session_id=''")
        callbacks = connection.execute("SELECT callback_id FROM callback_outbox WHERE status NOT IN ('delivered','dead_lettered','superseded') OR delivery_lease_token!=''").fetchall()
        for callback in callbacks:
            connection.execute("UPDATE callback_outbox SET status=CASE WHEN status IN ('delivered','dead_lettered','superseded') THEN status ELSE 'dead_lettered' END, delivery_lease_token='', delivery_lease_expires_at=NULL, last_error='restore_reauthentication_required', updated_at=? WHERE callback_id=?", (now, callback["callback_id"]))
            store._append_callback_trace_conn(connection, callback_id=callback["callback_id"])
        connection.execute("UPDATE active_work_action_uses SET status='failed', executor_id='', lease_expires_at=NULL, last_error='restore_reauthentication_required' WHERE status NOT IN ('completed','failed')")
        connection.execute("UPDATE capability_grant_revocations SET status='failed', lease_owner='', lease_expires_at=NULL, last_error_code='restore_reauthentication_required' WHERE status!='applied'")
        # Historical effects remain auditable but must not replay external callbacks.
        connection.execute("UPDATE lifecycle_operation_effects SET status='failed', lease_owner='', lease_expires_at=NULL, next_attempt_at=NULL, last_error_code='restore_reauthentication_required' WHERE status!='applied'")
    # Remove old credential bytes from free pages; WAL/SHM are never artifacts.
    with store._connect() as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("VACUUM")


def capture_state(database: Path, output: Path) -> dict[str, int]:
    """Capture stable work and existing safe workspace files, never provider homes."""
    metadata = database.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_nlink != 1:
        raise ValueError("GlassHive database is not an owned regular file")
    if output.exists() or output.is_symlink():
        raise ValueError("GlassHive continuity output already exists")
    output.mkdir(parents=True, mode=0o700)
    store = None
    try:
        with sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True) as source:
            _require_schema(source)
            _require_quiescent(source, database)
            with sqlite3.connect(output / "runtime.sqlite") as target:
                source.backup(target)
        store = Store(str(output / "runtime.sqlite"))
        callback_tokens = _project_callback_history(store)
        runtime = ProfiledWorkerRuntime(base_dir=str(output), provider_account_db_path=str(output / "runtime.sqlite"))
        with store._connect() as connection:
            workers = [dict(row) for row in connection.execute("SELECT * FROM workers")]
        copied_files = 0
        copied_bytes = 0
        deadline = time.monotonic() + 300
        for worker in workers:
            state, workspace = _worker_paths(runtime, worker, output)
            workspace.mkdir(parents=True, mode=0o700, exist_ok=True)
            raw_source = str(worker.get("workspace_dir") or "")
            if raw_source:
                source_root = Path(raw_source)
                files, _, result = _workspace_copy_plan(source_root)
                if result == "missing":
                    raise ValueError("GlassHive recorded workspace is missing; recover it before capture")
                for source_file, relative in files:
                    if any(part.casefold() in NON_DELIVERABLE_DIR_NAMES for part in relative.parts):
                        continue
                    source_mode = source_file.lstat().st_mode
                    copied_bytes += _copy_regular_workspace_file(
                        source_file, workspace / relative, source_root,
                        max_bytes=20 * 1024**3 - copied_bytes, deadline=deadline,
                    )
                    if source_mode & stat.S_IXUSR:
                        (workspace / relative).chmod(0o700)
                    copied_files += 1
            with store._connect() as connection:
                connection.execute(
                    "UPDATE workers SET state_dir=?, workspace_dir=?, workspace_root=? WHERE worker_id=?",
                    (str(state.relative_to(output)), str(workspace.relative_to(output)), str(workspace.relative_to(output)), worker["worker_id"]),
                )
        _reset_authority(store)
        _verified_traces(store)
        store.close()
        with sqlite3.connect(output / "runtime.sqlite") as connection:
            connection.execute("PRAGMA journal_mode=DELETE")
            if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ValueError("GlassHive continuity failed SQLite integrity checking")
        _private_tree(output)
        _require_no_callback_credentials(output, callback_tokens)
        return {"workers": len(workers), "workspace_files": copied_files, "workspace_bytes": copied_bytes}
    except BaseException:
        if store is not None:
            store.close()
        shutil.rmtree(output)
        raise


def prepare_restored_state(stage: Path, final_root: Path) -> None:
    """Bind portable workspace references to the destination before activation."""
    _private_tree(stage)
    if not final_root.is_absolute():
        raise ValueError("GlassHive restore destination must be absolute")
    database = stage / "runtime.sqlite"
    _require_no_callback_credentials(stage)
    with sqlite3.connect(database) as connection:
        _require_schema(connection)
        _require_exportable_trace(connection)
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("GlassHive restore failed SQLite integrity checking")
        connection.row_factory = sqlite3.Row
        for row in connection.execute("SELECT worker_id, state_dir, workspace_dir, workspace_root FROM workers").fetchall():
            values = []
            for field in ("state_dir", "workspace_dir", "workspace_root"):
                relative = Path(row[field])
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("GlassHive restored workspace reference is unsafe")
                values.append(str(final_root / relative))
            connection.execute("UPDATE workers SET state_dir=?, workspace_dir=?, workspace_root=? WHERE worker_id=?", (*values, row["worker_id"]))
        connection.execute("UPDATE provider_sessions SET workspace_dir=(SELECT workspace_dir FROM workers WHERE workers.worker_id=provider_sessions.worker_id)")
    # Apply the same component-owned authority reset even to a supplied archive.
    store = Store(str(database))
    try:
        _verified_traces(store)
        _reset_authority(store)
        _verified_traces(store)
    finally:
        store.close()
    _private_tree(stage)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    capture = commands.add_parser("capture")
    capture.add_argument("database", type=Path)
    capture.add_argument("output", type=Path)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("stage", type=Path)
    prepare.add_argument("destination", type=Path)
    quiescence = commands.add_parser("quiescent")
    quiescence.add_argument("database", type=Path)
    args = parser.parse_args()
    if args.operation == "capture":
        capture_state(args.database, args.output)
    elif args.operation == "prepare":
        prepare_restored_state(args.stage, args.destination)
    else:
        check_quiescent(args.database)


if __name__ == "__main__":
    main()
