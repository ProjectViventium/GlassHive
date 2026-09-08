from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from workers_projects_runtime.control_plane import ControlPlaneStore
from workers_projects_runtime.store import Store


def source_state(tmp_path: Path):
    database = tmp_path / "source" / "runtime.sqlite"
    database.parent.mkdir(mode=0o700)
    store = Store(str(database))
    control = ControlPlaneStore(str(database))
    project = store.create_project("owner-a", "Keep my work", "Finish the report", "codex-cli")
    workspace = database.parent / "workspace"
    workspace.mkdir()
    (workspace / "report.md").write_text("Useful finished work\n")
    (workspace / ".env").write_text("SYNTHETIC_SECRET=never-export\n")
    (workspace / ".codex").mkdir()
    (workspace / ".codex" / "auth.json").write_text('{"token":"never-export"}')
    worker = store.create_worker(
        project["project_id"], "owner-a", "Writer", "Help finish", "codex-cli", "codex",
        "codex-cli", "configured-model", execution_mode="host",
        bootstrap_bundle={"project_definition": "Finish the report", "mcp_servers": {"secret": "never-export"}},
    )
    store.update_worker(worker["worker_id"], workspace_dir=str(workspace), state="paused", pid=123,
                        gateway_token="never-export", session_key="old-live-session")
    account = control.create_provider_account(
        tenant_id="local", owner_id="owner-a", provider="codex", label="Account", auth_method="subscription",
        platform_support="supported", secret_locator="native-home://private-account", status="ready",
    )
    return database, store, worker, account


def test_native_capture_preserves_identity_files_and_requires_reconnect(tmp_path):
    from workers_projects_runtime.native_continuity import capture_state, prepare_restored_state

    database, source, worker, account = source_state(tmp_path)
    snapshot = tmp_path / "snapshot"
    report = capture_state(database, snapshot)
    assert report["workers"] == 1
    assert report["workspace_files"] == 1
    assert source.get_worker(worker["worker_id"])["gateway_token"] == "never-export"
    assert not any(b"never-export" in path.read_bytes() for path in snapshot.rglob("*") if path.is_file())

    final_root = tmp_path / "restored"
    prepare_restored_state(snapshot, final_root)
    restored = Store(str(snapshot / "runtime.sqlite"))
    result = restored.get_worker(worker["worker_id"])
    assert result["project_id"] == worker["project_id"]
    assert result["state"] == "paused"
    assert result["pid"] is None
    assert not result["gateway_token"] and not result["session_key"]
    assert json.loads(result["bootstrap_bundle_json"]) == {"project_definition": "Finish the report"}
    relative = Path(result["workspace_dir"]).relative_to(final_root)
    assert (snapshot / relative / "report.md").read_text() == "Useful finished work\n"
    restored_account = ControlPlaneStore(str(snapshot / "runtime.sqlite")).get_provider_account(
        account_id=account["account_id"], tenant_id="local", owner_id="owner-a",
    )
    assert restored_account["status"] == "action_required"


def test_native_capture_preserves_executable_work_and_clears_existing_compute_contract(tmp_path):
    from workers_projects_runtime.native_continuity import capture_state, prepare_restored_state
    from workers_projects_runtime.store import COMPUTE_OPERATION_CLEAR_FIELDS

    database, store, worker, _ = source_state(tmp_path)
    script = database.parent / "workspace" / "build.sh"
    script.write_text("#!/bin/sh\nprintf useful\\n\n")
    script.chmod(0o700)
    store.update_worker(worker["worker_id"], compute_release_token="stale", compute_release_container_id="old-container", control_url="http://old-control")
    snapshot = tmp_path / "snapshot"
    capture_state(database, snapshot)
    prepare_restored_state(snapshot, tmp_path / "destination")
    restored = Store(str(snapshot / "runtime.sqlite")).get_worker(worker["worker_id"])
    assert all(restored[key] == value for key, value in COMPUTE_OPERATION_CLEAR_FIELDS.items())
    assert restored["control_url"] is None
    captured_script = next(snapshot.rglob("build.sh"))
    assert captured_script.stat().st_mode & 0o777 == 0o700


def test_native_capture_rejects_workspace_escape_and_cleans_partial_output(tmp_path):
    from workers_projects_runtime.native_continuity import capture_state

    database, _, _, _ = source_state(tmp_path)
    (database.parent / "workspace" / "escape").symlink_to(tmp_path / "outside")
    destination = tmp_path / "snapshot"
    with pytest.raises(ValueError, match="unsafe workspace symlink"):
        capture_state(database, destination)
    assert not destination.exists()


def test_native_capture_rejects_unreviewed_future_database_without_touching_source(tmp_path):
    from workers_projects_runtime.native_continuity import capture_state

    database, _, _, _ = source_state(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE glasshive_schema_versions SET version = 999 WHERE component = 'runtime_store'")
    with pytest.raises(ValueError, match="schema"):
        capture_state(database, tmp_path / "snapshot")


def test_native_capture_does_not_confuse_api_shutdown_with_worker_quiescence(tmp_path):
    from workers_projects_runtime.native_continuity import capture_state

    database, store, worker, _ = source_state(tmp_path)
    store.update_worker(worker["worker_id"], state="running")
    with pytest.raises(ValueError, match="worker compute must be quiesced"):
        capture_state(database, tmp_path / "snapshot")
    assert store.get_worker(worker["worker_id"])["state"] == "running"


def test_native_quiescence_is_read_only_and_uses_actual_session_state(tmp_path):
    from workers_projects_runtime.native_continuity import check_quiescent
    database, store, worker, _ = source_state(tmp_path)
    before = {str(path.relative_to(database.parent)): path.read_bytes() if path.is_file() else None for path in database.parent.rglob("*") if not path.name.endswith("-shm")}
    check_quiescent(database)
    # SQLite read locks update transient shared-memory read marks, not durable state.
    after = {str(path.relative_to(database.parent)): path.read_bytes() if path.is_file() else None for path in database.parent.rglob("*") if not path.name.endswith("-shm")}
    assert after == before
    state = database.parent / "host_codex_cli_runtime/workers" / worker["worker_id"] / "state"
    state.mkdir(parents=True)
    (state / "active_terminal_session.json").write_text("malformed")
    with pytest.raises(ValueError, match="unreadable"):
        check_quiescent(database)


@pytest.mark.parametrize("mutation", [
    "CREATE TABLE unknown_credentials (secret TEXT)",
    "CREATE TABLE sqliteXcredentials (secret TEXT)",
    "ALTER TABLE workers ADD COLUMN unknown_secret TEXT",
    "ALTER TABLE workers ADD COLUMN unknown_secret TEXT GENERATED ALWAYS AS ('synthetic') VIRTUAL",
])
def test_native_capture_rejects_unknown_shape_at_supported_ledger(tmp_path, mutation):
    from workers_projects_runtime.native_continuity import capture_state
    database, _, _, _ = source_state(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(mutation)
    with pytest.raises(ValueError, match="unreviewed schema shape"):
        capture_state(database, tmp_path / "snapshot")
    assert not (tmp_path / "snapshot").exists()


@pytest.mark.parametrize("state", ["queued", "claimed", "admitted", "running", "settling", "paused", "needs_input"])
def test_native_continuity_rejects_unsettled_runs_even_when_api_and_worker_are_paused(tmp_path, state):
    from workers_projects_runtime.native_continuity import capture_state, check_quiescent
    database, store, worker, _ = source_state(tmp_path)
    run = store.create_run(worker["worker_id"], worker["project_id"], "Finish the work")
    store.update_run(run["run_id"], state=state)
    with pytest.raises(ValueError, match="active work must be quiesced"):
        capture_state(database, tmp_path / "snapshot")
    with pytest.raises(ValueError, match="active work must be quiesced"):
        check_quiescent(database)


def test_native_capture_rejects_missing_durable_workspace(tmp_path):
    from workers_projects_runtime.native_continuity import capture_state
    database, _, _, _ = source_state(tmp_path)
    shutil.rmtree(database.parent / "workspace")
    with pytest.raises(ValueError, match="recorded workspace is missing"):
        capture_state(database, tmp_path / "snapshot")


def test_native_capture_preserves_task_constraints_and_user_disconnect_without_browser_state(tmp_path):
    from workers_projects_runtime.native_continuity import capture_state
    database, store, worker, account = source_state(tmp_path)
    bundle = {"project_definition": "Finish the report", "developer_instructions": "Keep uncertainties visible", "agents_md": "Use the complete source", "viventium_constraint_source": {"version": 1, "instruction": "Do not send anything"}, "env": {"WPR_CLAUDE_CODE_EFFORT": "max", "API_KEY": "never-export"}, "callbacks": {"hmac_secret": "never-export"}}
    store.update_worker(worker["worker_id"], bootstrap_bundle_json=json.dumps(bundle))
    browser = database.parent / "workspace/browser-profile/Default/Local Storage/leveldb"
    browser.mkdir(parents=True)
    (browser / "000003.log").write_text("never-export")
    with store._connect() as connection:
        connection.execute("UPDATE provider_accounts SET status='disconnected', is_default=0 WHERE account_id=?", (account["account_id"],))
    target = tmp_path / "snapshot"
    capture_state(database, target)
    restored = Store(str(target / "runtime.sqlite"))
    saved = json.loads(restored.get_worker(worker["worker_id"])["bootstrap_bundle_json"])
    assert saved["developer_instructions"] == bundle["developer_instructions"]
    assert saved["agents_md"] == bundle["agents_md"]
    assert saved["viventium_constraint_source"] == bundle["viventium_constraint_source"]
    assert saved["env"] == {"WPR_CLAUDE_CODE_EFFORT": "max"}
    assert not any(b"never-export" in path.read_bytes() for path in target.rglob("*") if path.is_file())
    with restored._connect() as connection:
        assert connection.execute("SELECT status, is_default FROM provider_accounts WHERE account_id=?", (account["account_id"],)).fetchone()[:] == ("disconnected", 0)


def callback_history(tmp_path, *, delegated=True):
    database, store, worker, _ = source_state(tmp_path)
    if delegated:
        record = store.reserve_delegation(tenant_id="local", owner_id="owner-a", idempotency_key="native-portable-history",
            request_digest="synthetic-history", origin_ref="origin_native_history", title="Keep the report",
            goal="Finish the report with uncertainty intact", instruction="Keep the table and do not send anything",
            origin_surface="web", worker_name="Writer", worker_role="writer", profile="codex-cli",
            backend="codex-cli", runtime="codex-cli", model="configured-model", execution_mode="host", bootstrap_bundle={})
        worker = store.get_worker(record["worker_id"])
        store.update_worker(worker["worker_id"], state="paused")
        run_id = record["current_run_id"]
    else:
        run_id = store.create_run(worker["worker_id"], worker["project_id"], "Keep the table and do not send anything")["run_id"]
    callback = store.insert_callback_outbox_once(callback_id="callback-test", project_id=worker["project_id"],
        worker_id=worker["worker_id"], run_id=run_id, attempt_number=None, event_type="run.queued",
        url="https://callback.example.invalid/events", payload_json='{"callback_ts":1700000001,"result":"The report is ready; uncertainty remains"}')
    claimed = store.claim_pending_callback(callback["callback_id"])
    assert claimed
    token = claimed["delivery_lease_token"]
    store.mark_callback_pending(callback["callback_id"], lease_token=token,
        delivery_generation=claimed["delivery_generation"], attempts=1, payload_json=claimed["payload_json"], last_error="Retry after reconnect")
    store.add_event(worker["project_id"], worker["worker_id"], run_id, "callback.diagnostic", "Delivery attempt retained",
        payload={"receipt": {"deliveryLeaseToken": token}, "encodedReceipt": json.dumps({"lease": token}), "result": "Keep the report"})
    store.update_run(run_id, state="completed", output_text="The report is ready; uncertainty remains")
    return database, store, worker, run_id, token


@pytest.mark.parametrize("delegated", [True, False])
def test_native_capture_projects_callback_history_without_losing_meaning(tmp_path, delegated):
    from workers_projects_runtime.native_continuity import capture_state, prepare_restored_state
    database, store, worker, run_id, token = callback_history(tmp_path, delegated=delegated)
    with store._connect() as connection:
        original = "\n".join(connection.iterdump())
        identities = connection.execute("SELECT callback_trace_event_id, callback_id, run_sequence, status FROM callback_trace_events ORDER BY run_sequence").fetchall()
    before = store.work_trace_detail(run_id=run_id, tenant_id="local", owner_id="owner-a") if delegated else None
    snapshot = tmp_path / "snapshot"
    capture_state(database, snapshot)
    assert not any(token.encode() in p.read_bytes() for p in snapshot.rglob("*") if p.is_file())
    with store._connect() as connection:
        assert "\n".join(connection.iterdump()) == original
    restored_root = tmp_path / "restored"
    prepare_restored_state(snapshot, restored_root)
    restored = Store(str(snapshot / "runtime.sqlite"))
    with restored._connect() as connection:
        rows = connection.execute("SELECT callback_trace_event_id, callback_id, run_sequence, status FROM callback_trace_events ORDER BY run_sequence").fetchall()
        assert [tuple(row) for row in rows[:len(identities)]] == [tuple(row) for row in identities]
        assert connection.execute("SELECT COUNT(*) FROM events WHERE event_type='callback.diagnostic'").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE callback_trace_events SET snapshot_json='{}'")
        if delegated:
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute("DELETE FROM work_trace_events")
    assert restored.get_run(run_id)["output_text"] == "The report is ready; uncertainty remains"
    assert restored.claim_pending_callback("callback-test") is None
    assert restored.mark_callback_pending("callback-test", lease_token=token, delivery_generation=1,
        attempts=2, payload_json="{}", last_error="old authority") is None
    if delegated:
        after = restored.work_trace_detail(run_id=run_id, tenant_id="local", owner_id="owner-a")
        assert after["traceability"]["continuityProjections"][0]["sourceWorkHeadSha256"] == before["traceability"]["integrity"]["headSha256"]
        assert after["traceability"]["promptLayers"] == before["traceability"]["promptLayers"]
        assert [row["status"] for row in after["callbackDeliveries"][:3]] == ["pending", "delivering", "pending"]
    # The copy is a valid later source, rather than a one-use exception to import validation.
    shutil.move(snapshot, restored_root)
    restored.close()
    second = tmp_path / "second-snapshot"
    capture_state(restored_root / "runtime.sqlite", second)
    assert not any(token.encode() in p.read_bytes() for p in second.rglob("*") if p.is_file())


@pytest.mark.parametrize("table", ["callback_trace_events", "work_trace_events"])
def test_native_capture_rejects_tampered_history_before_projection(tmp_path, table):
    from workers_projects_runtime.native_continuity import capture_state
    database, store, _, _, _ = callback_history(tmp_path)
    trigger = f"{table}_append_only_update"
    column = "snapshot_json" if table == "callback_trace_events" else "payload_json"
    with store._connect() as connection:
        sql = connection.execute("SELECT sql FROM sqlite_master WHERE name=?", (trigger,)).fetchone()[0]
        connection.execute(f'DROP TRIGGER "{trigger}"')
        connection.execute(f'UPDATE "{table}" SET "{column}"=?', ('{"changed":"untrusted history"}',))
        connection.execute(sql)
        original = "\n".join(connection.iterdump())
    snapshot = tmp_path / "snapshot"
    with pytest.raises((RuntimeError, ValueError), match="integrity"):
        capture_state(database, snapshot)
    assert not snapshot.exists()
    with store._connect() as connection:
        assert "\n".join(connection.iterdump()) == original


@pytest.mark.parametrize("tamper", ["provenance", "provenance_unlinked", "token_copy", "callback"])
def test_native_import_rejects_tampered_portable_history(tmp_path, tamper):
    from workers_projects_runtime.native_continuity import capture_state, prepare_restored_state
    database, _, _, _, token = callback_history(tmp_path)
    snapshot = tmp_path / "snapshot"
    capture_state(database, snapshot)
    staged = Store(str(snapshot / "runtime.sqlite"))
    with staged._connect() as connection:
        if tamper == "provenance":
            connection.execute("UPDATE events SET payload_json=json_set(payload_json, '$.exportedCallbackHeadSha256', ?) WHERE event_type='continuity.projected'", ("sha256:" + "0" * 64,))
        elif tamper == "provenance_unlinked":
            connection.execute("DELETE FROM events WHERE event_type='continuity.projected'")
        elif tamper == "token_copy":
            connection.execute("UPDATE events SET payload_json=? WHERE event_type='callback.diagnostic'", (json.dumps({"nested": {"copy": token}}),))
        else:
            sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='callback_trace_events_append_only_update'").fetchone()[0]
            connection.execute("DROP TRIGGER callback_trace_events_append_only_update")
            connection.execute("UPDATE callback_trace_events SET authority_sha256=?", ("sha256:" + "0" * 64,))
            connection.execute(sql)
    staged.close()
    target = tmp_path / "must-not-exist"
    with pytest.raises((RuntimeError, ValueError), match="provenance|credential|integrity"):
        prepare_restored_state(snapshot, target)
    assert not target.exists()


@pytest.mark.parametrize("location", ["value", "key", "encoded_string"])
def test_native_capture_redacts_json_encoded_credential_copies(tmp_path, location):
    from workers_projects_runtime.native_continuity import capture_state
    database, store, _, _, token = callback_history(tmp_path)
    escaped = "".join("\\u%04x" % ord(char) for char in token)
    payload = ('{"' + escaped + '":"Keep the report"}' if location == "key" else
               '{"copy":"' + escaped + '","result":"Keep the report"}')
    if location == "encoded_string":
        payload = json.dumps({"encodedReceipt": payload})
    with store._connect() as connection:
        connection.execute("UPDATE events SET payload_json=? WHERE event_type='callback.diagnostic'", (payload,))
    snapshot = tmp_path / "snapshot"
    capture_state(database, snapshot)
    with sqlite3.connect(snapshot / "runtime.sqlite") as connection:
        exported = connection.execute("SELECT payload_json FROM events WHERE event_type='callback.diagnostic'").fetchone()[0]
    assert token not in str(json.loads(exported))
    if location == "encoded_string":
        assert token not in str(json.loads(json.loads(exported)["encodedReceipt"]))
    assert "Keep the report" in exported


@pytest.mark.parametrize("location", ["value", "key", "encoded_string", "workspace"])
def test_native_import_rejects_encoded_credential_copies(tmp_path, location):
    from workers_projects_runtime.native_continuity import capture_state, prepare_restored_state
    database, _, _, _, token = callback_history(tmp_path)
    snapshot = tmp_path / "snapshot"
    capture_state(database, snapshot)
    escaped = "".join("\\u%04x" % ord(char) for char in token)
    payload = ('{"' + escaped + '":"Keep the report"}' if location == "key" else
               '{"copy":"' + escaped + '","result":"Keep the report"}')
    if location == "encoded_string":
        payload = json.dumps({"encodedReceipt": payload})
    if location == "workspace":
        (snapshot / "receipt.json").write_text(payload)
    else:
        with sqlite3.connect(snapshot / "runtime.sqlite") as connection:
            connection.execute("UPDATE events SET payload_json=? WHERE event_type='callback.diagnostic'", (payload,))
    with pytest.raises(ValueError, match="credential"):
        prepare_restored_state(snapshot, tmp_path / "must-not-exist")


def test_native_reexport_preserves_prior_projection_after_new_callback_history(tmp_path):
    from workers_projects_runtime.native_continuity import capture_state, prepare_restored_state
    database, _, worker, run_id, token = callback_history(tmp_path)
    snapshot = tmp_path / "snapshot"
    capture_state(database, snapshot)
    restored_root = tmp_path / "restored"
    prepare_restored_state(snapshot, restored_root)
    shutil.move(snapshot, restored_root)
    restored = Store(str(restored_root / "runtime.sqlite"))
    before = restored.work_trace_detail(run_id=run_id, tenant_id="local", owner_id="owner-a")
    prior_projections = before["traceability"]["continuityProjections"]
    callback = restored.insert_callback_outbox_once(callback_id="callback-after-restore", project_id=worker["project_id"],
        worker_id=worker["worker_id"], run_id=run_id, attempt_number=None, event_type="run.queued",
        url="https://callback.example.invalid/events", payload_json='{"callback_ts":1700000002,"result":"The preserved report is available"}')
    claimed = restored.claim_pending_callback(callback["callback_id"])
    new_token = claimed["delivery_lease_token"]
    restored.mark_callback_pending(callback["callback_id"], lease_token=new_token,
        delivery_generation=claimed["delivery_generation"], attempts=1,
        payload_json=claimed["payload_json"], last_error="Retry after reconnect")
    second = tmp_path / "second"
    capture_state(restored_root / "runtime.sqlite", second)
    prepare_restored_state(second, tmp_path / "second-restored")
    imported = Store(str(second / "runtime.sqlite"))
    after = imported.work_trace_detail(run_id=run_id, tenant_id="local", owner_id="owner-a")
    assert after["traceability"]["continuityProjections"][:-1] == prior_projections
    assert len(after["traceability"]["continuityProjections"]) == 2
    assert after["traceability"]["promptLayers"] == before["traceability"]["promptLayers"]
    assert imported.get_run(run_id)["output_text"] == "The report is ready; uncertainty remains"
    assert all(credential.encode() not in path.read_bytes() for path in second.rglob("*") if path.is_file()
               for credential in (token, new_token))


def test_native_capture_rejects_unhandled_credential_copy_without_losing_source(tmp_path):
    from workers_projects_runtime.native_continuity import capture_state
    database, source, _, run_id, token = callback_history(tmp_path)
    # Do not silently rewrite user task/result text to make a portability check pass.
    source.update_run(run_id, output_text=json.dumps({"diagnostic": token, "result": "Keep the report"}))
    with source._connect() as connection:
        before = "\n".join(connection.iterdump())
    output = tmp_path / "snapshot"
    with pytest.raises(ValueError, match="credential"):
        capture_state(database, output)
    assert not output.exists()
    with source._connect() as connection:
        assert "\n".join(connection.iterdump()) == before
