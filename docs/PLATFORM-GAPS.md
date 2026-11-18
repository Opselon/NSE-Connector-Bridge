# NSE Platform gaps — what must change on the Platform side

Owner: Architecture (Phase 3, client-only refactor). Status: 2026-11-18.

Scope: this document lists the pieces of the complete trading workflow
that the Bridge **cannot** finish on its own, because they live in NSE
Platform or in CONTRACT v1 (`NSE-CONNECTOR-CONTRACT.md`, merged to
Platform main via PR #102). The Bridge targets the CONTRACT v1 document
and touches nothing in NSE-Platform. Nothing in this document invents a
Platform endpoint, frame, or wire shape — where a contract amendment is
needed, the required capability is described and the endpoint itself is
explicitly left to the Platform side to define.

## 1. Migration 0054 is claimed by two open Platform PRs (#103 and #107)

- Platform PR #103 (frozen by the user 2026-11-17) and Platform PR #107
  (open, implements CONTRACT v1) **both claim migration 0054**.
- Resolution is Platform-side only: one of the two PRs must drop or
  renumber its migration before either merges, or the merge will
  conflict / double-apply the migration.
- Bridge impact: none on the code in this branch — the Bridge targets
  the CONTRACT v1 document (merged via PR #102), not either PR branch.
  Live integration testing against a deployed Platform is blocked until
  the Platform side resolves the duplication and deploys a build that
  carries the contract (see also §6).

## 2. No diagnostics / events upload endpoint (CONTRACT v1 §4.2)

- CONTRACT v1 §4.2 defines **no endpoint for uploading diagnostics or
  events**. The server derives device health from `last_seen_at`, WS
  liveness, the last accepted sync batch, and the manifest.
- This blocks acceptance criterion 11 (telemetry delivery): the Bridge
  can produce structured JSON diagnostics locally (commands 82–90,
  structured logs, bounded local queue with retry/backoff/dedup/limits/
  expiry) and can report truthfully through existing channels
  (`action.result` errors, heartbeat/liveness, manifest updates), but
  it has no channel for shipping a diagnostic event stream to the
  Platform — and it will not invent one.
- Required from the Platform: a contract amendment defining a
  diagnostics/events upload endpoint (path, auth, payload, rate limits,
  retention). No URL or payload shape is proposed here.

## 3. `open_order` / `cancel_order` are NOT sendable in v1 (§6.1)

- The v1 server allowlist is exactly `close_position`, `modify_sl_tp`,
  `modify_pending` (capability-gated, §4 below). `open_order` and
  `cancel_order` are explicitly NOT sendable in v1. The full 91-command
  registry stays available **locally**; the server channel enforces the
  3-op allowlist at the channel boundary.
- Before the allowlist can widen, the Platform needs to prove, on the
  ops that are already allowed:
  - the dispatcher gate chain (validation order §6.3, replay cache,
    expiry, account/device binding) under live load;
  - the Platform Risk Engine path for server-issued trading commands
    (pre-trade risk checks, entitlement enforcement, kill-switch);
  - live end-to-end runs of `close_position` and `modify_sl_tp`
    against a real terminal, with `action.result` reconciliation
    (no duplicate execution, no lost result left UNKNOWN).
- Required from the Platform: the proven gates above, published as a
  contract version bump (`schema_version`) that registers the new ops.
  The Bridge will not widen the allowlist unilaterally.

## 4. `modify_pending` capability gate (§17.4) — live proof still owed

- `modify_pending` is contract-defined but starts REJECTED: the Bridge
  enforces the §17.4 capability gate (`modify_pending_proven=False` by
  default in `nse_bridge.server.commands.CommandValidator`) and rejects
  the op until the gate is proven.
- The §17.4 close-out still owed: live proof at market open that a
  pending order's SL/TP can be modified through the server channel and
  reconciled against MT5 state, plus a published minimum
  `connector_version` from which the op is enabled.
- Required from the Platform: the live proof run and the published
  minimum connector version. Until both exist, the gate stays closed
  and `modify_pending` commands are rejected with a structured reason.

## 5. Task wire format is not in CONTRACT v1 (future server-assigned compute)

- The Bridge implements a local task-execution boundary
  (`nse_bridge/tasks/`: generic envelope, validation mirroring §6.3,
  bounded worker, `task.result` envelope). The kind registry starts
  EMPTY: the server cannot assign compute until a future contract
  version registers a kind.
- CONTRACT v1 defines **no wire format for tasks** — no frame, no
  endpoint, no kind registry. The boundary is therefore a local
  extension point only; it is NOT wired to the WS command path, and no
  task kind will execute from a server message until the format exists.
- Required from the Platform: a future contract amendment registering
  task kinds and their wire format (envelope, validation, resource
  limits, result reporting). Approved/trusted modules only — never
  arbitrary remote code execution.

## 6. Live Platform endpoint is unverified from here

- The production endpoint is `bridge.nexusverse.space`
  (`https://bridge.nexusverse.space`,
  `wss://bridge.nexusverse.space/api/v1/connector/ws`). Integration
  tests in this repo run against the CONTRACT-v1 mock server
  (`scripts/mock_server.py`); live verification against the deployed
  Platform needs PR #107 merged and deployed (see §1).
- This is a verification gap, not a code gap: nothing in the Bridge
  assumes the live endpoint behaves differently from the contract.
