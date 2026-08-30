# Deterministic local-QA fault controls

GlassHive local-QA faults are off unless all six canonical runtime values are present and exact:

- `VIVENTIUM_GLASSHIVE_LOCAL_QA_MODE=pwk_uc_016|pwk_uc_017`
- `VIVENTIUM_LOCAL_QA_CASE_ID=PWK-UC-016|PWK-UC-017`
- `VIVENTIUM_LOCAL_QA_CASE_TOKEN`
- `VIVENTIUM_LOCAL_QA_SESSION_REF`
- `VIVENTIUM_LOCAL_QA_CANDIDATE_DIGEST`
- `VIVENTIUM_LOCAL_QA_COMPONENT_ARTIFACT_DIGEST`

A partial tuple, a mode/case mismatch, an unknown case, or an inactive session cannot consume a
control. These values authorize only the matching one-shot control. They are not broad fault flags.
The component compares both digests with every private request. The installed launcher first clears
ambient digest values and projects the candidate digest only from the exact active parent session.
The root derives both digests from its independently checked installed artifact identity and
running-service measurement; the component never computes or accepts a self-attested digest.

## Private control input

Run the component CLI from `runtime_phase1` with an inherited private descriptor:

```bash
./glasshive-local-qa-control arm --input-fd 3
./glasshive-local-qa-control query --input-fd 3
./glasshive-local-qa-control clear --input-fd 3
./glasshive-local-qa-control cleanup --input-fd 3
```

The CLI never reads standard input. It accepts exactly one inherited descriptor numbered 3 or
higher, or an absolute explicit regular file owned by the current user with mode `0600` and one
link. It rejects nonregular descriptors, hard links, duplicate flags, duplicate JSON keys, invalid
UTF-8, unsafe or symlinked path components, writable parent directories, size overflow, and file or
descriptor replacement during validation. Errors are typed, bounded, and redacted. The parent must
use an opaque file name if it uses `--input-file`. Tokens and raw owner, work, run, or artifact IDs
must not enter the file name or command arguments.

An arm request has this strict shape:

```json
{
  "contractVersion": 1,
  "caseId": "PWK-UC-017",
  "caseToken": "<private session token>",
  "sessionRef": "<private session reference>",
  "candidateDigest": "<root-measured candidate digest>",
  "componentArtifactDigest": "<root-measured installed service digest>",
  "scopeKind": "synthetic_local_qa",
  "boundary": "callback_transport_interruption",
  "ownerId": "<private synthetic owner ID>",
  "workId": "<private synthetic work identity>",
  "runId": "<private synthetic run ID>",
  "artifactId": "",
  "ttlSeconds": 60,
  "parameters": {}
}
```

Arm requires an existing durable synthetic fixture in the selected GlassHive database. Its local
tenant, owner, work, project, worker, current run, artifact observation, synthetic marker fields,
and trusted idempotency identity must form one exact relation. Empty, missing, ordinary-user, or
cross-owner/work/run/artifact fixtures fail closed. Run-scoped boundaries require `runId`.
Artifact boundaries also require `artifactId`. Unknown and extra fields fail closed. Expiry is an
integer from 1 through 3600 seconds.

Query and cleanup use `contractVersion`, `caseId`, `caseToken`, `sessionRef`, `candidateDigest`, and
`componentArtifactDigest`. Clear also requires the exact `controlRef`. The private file or
descriptor is the only place where the raw token and raw scope exist.

## Durable contract

The runtime database stores only domain-separated SHA-256 hashes for the token, session, owner,
work, run, and artifact. Receipts contain the case, boundary, opaque control reference, original
expiry, state, and scope hashes. They never contain private input.

Each exact current case/session/candidate/service-artifact/boundary/scope can have only one armed
row. A later replay with the same TTL returns that row and its original expiry. A changed TTL
conflicts. Consumption is an atomic `armed -> consumed` transition with an append-only audit event.
Concurrent consumers can produce at most one directive. Controls survive process restart. Every
stored or returned timestamp uses exact ISO milliseconds with `+00:00`.

A durable hash-only arm ledger survives control cleanup. It prevents the same exact fixture
idempotency and arm identity from creating a replacement after consumption, expiry, clear, cleanup,
restart, or a concurrent replay. Cleanup may remove the exact terminal control and audit rows, but
not this one-shot tombstone.

Expired controls cannot run. A replaced component artifact cannot consume a control armed for the
earlier artifact. Token-authorized cleanup can expire or clear those rows and remove the exact
session state. Cleanup refuses a current live armed control until it is explicitly cleared.
The database must be one owner-only regular file reached through a no-follow, non-writable parent
chain. Parent and component operations bind the selected device/inode identity and reject path
replacement before or during use. Private source reads and destructive cleanup also compare the
exact descriptor identity, size, nanosecond timestamps, and content digest.

## Typed boundaries

`PWK-UC-016` supports:

- `provider_auth_missing`
- `provider_quota_cooldown_fallback`
- `provider_unavailable`
- `maximum_capacity_overflow`
- `measured_memory_4_3_gib_vs_5_gib`
- `last_reservation_competition`
- `low_disk`

`PWK-UC-017` supports:

- `callback_transport_interruption`
- `claimed_queue_stall`
- `admitted_queue_stall`
- `status_refresh_timeout_race`
- `expired_sender_lease_race`
- `duplicate_callback_replay`
- `artifact_link_expired`
- `artifact_unavailable_restart_recovery`

The catalog supplies fixed typed facts. Operator input cannot add a hidden fault parameter.

## Evidence boundary

Source tests prove the control plane and the typed Phase 2 runtime hooks. Every control is bound to
one owner, work item, and run. Artifact controls also bind one artifact. One callback attempt cannot
consume two independent callback faults before both effects occur. Importing the API module does
not create an application or start reconciliation; Uvicorn uses the application factory.

These tests do not prove the installed headed-browser, process-restart, receiver-delivery, or
artifact-recovery behavior. Installed `PWK-UC-016` and `PWK-UC-017` remain **PRE-GATE / NOT READY**
until the parent QA runner activates the exact installed session, arms each synthetic scope
privately, exercises every real boundary, and records the installed ledgers and visible results.
