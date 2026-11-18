# NSE Platform gaps — what must change on the Platform side

Owner: Architecture (Phase 3, client-only refactor). Status: 2026-11-18,
refreshed by the Phase E acceptance verification on branch
`fix/bridge-real-mt5-defects`, head `67f8ff5`, follow-up to merged PR #2
(merged 2026-11-18T10:05:35Z, merge commit `4f01068`, head `aae5075`).
Dependency IDs D1–D13 are from `docs/GAP-ANALYSIS.md` §4.

Status map (Phase E, refreshed): D1 open, D2 open, D3 open, D4 open (gated),
D5 open (local side now implemented, wire still missing),
D6 open (local collector now exists AND is wired in production, upload
channel still missing),
D7 open (local resolver/inventory now exist, server channel still
missing), D8 open (local labelling now exists, wire rule still
missing), D9 open, D10 deferred (v1.1), D11 open (live endpoint not
serving — deploy prep done, gateway not yet live; see §6), D12 open,
D13 open (MT5-surface gaps, workarounds in place).

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
- Phase E status (2026-11-18): **unchanged — still open.** Neither PR
  has merged; the duplication is unresolved and the live endpoint is
  still not serving (see §6). The gateway work on PR #107 (deploy to
  Germany, nginx vhost, live verification) is in progress in the main
  chat; the migration-0054 resolution is part of that work and must
  come from the #107 side — #103 is FROZEN, do not touch.

## 2. No diagnostics / events upload endpoint (CONTRACT v1 §4.2) — D1

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
- Phase E status (2026-11-18): **unchanged — still open, and now the
  single hardest blocker in the §25 table.** Spec §25 item 22 ("ERROR
  and WARN events reach the server reliably") is **Not-proven** and
  item 23 ("diagnostic delivery survives temporary network failures")
  is **Partial**, both because of D1. The local pipeline is complete
  (`TelemetryEmitter` + bounded `DiagnosticQueue`,
  `nse_bridge/reliability/telemetry.py:163,351`) but the sink is inert
  by design (`NullDiagnosticsSink` semantics — see the comment at
  `nse_bridge/api/app.py:141–147`): no ERROR/WARN event reaches the
  server today. A durable queue also exists now
  (`DurableDiagnosticQueue`, `reliability/diagnostic_store.py:201`)
  but is not yet wired into production — that wiring is Bridge-side
  work (see §12), not a Platform dependency.

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

## 6. Live Platform endpoint is unverified from here — D11

- The production endpoint is `bridge.nexusverse.space`
  (`https://bridge.nexusverse.space`,
  `wss://bridge.nexusverse.space/api/v1/connector/ws`). Integration
  tests in this repo run against the CONTRACT-v1 mock server
  (`scripts/mock_server.py`); live verification against the deployed
  Platform needs PR #107 merged and deployed (see §1).
- This is a verification gap, not a code gap: nothing in the Bridge
  assumes the live endpoint behaves differently from the contract.
- Phase E status (2026-11-18, refreshed): **unchanged — still open, endpoint
  still not serving, but deploy prep is done.** The gateway is INSIDE
  the NSE app (same binary, container 8088 ← host 8090) — the nginx
  vhost file `/etc/nginx/sites-available/bridge.nexusverse.space` is
  written (61 lines, `nginx -t` OK) but NOT enabled and nginx NOT
  reloaded; an LE cert for `bridge.nexusverse.space` is ISSUED
  (ECDSA, notAfter 2027-02-16, auto-renewal via the existing certbot
  timer); DNS resolves to Germany 2.31.201.91 everywhere (old
  198.18.182.79 gone), no AAAA. The gateway agent (Platform PR #107)
  and live-verification agent are still running. The holding-vhost
  option (80-only, ACME + return 503) was proposed and DECLINED by
  the user — do not re-offer. Every live-dependent §25 item (1–9,
  12–14, 18, 21, plus the live half of 17) is therefore Partial at
  best until D11 closes; no live round-trip and no real-terminal pass
  is claimed. Runbook: `~/workspace/bridge-deploy-prep/RUNBOOK.md`.

## 7. Partial-close / close-by wire shape + `PARTIALLY_COMPLETED` — D5

- Phase E status (2026-11-18): **open on the wire; the local side is
  now implemented.** The §6.2 envelope still carries only
  `{type, ticket, sl?, tp?}` — no `volume` field for partial close, no
  `close_by_ticket` field for close-by — and CONTRACT v1 §6.5
  `action.result` statuses are still exactly
  `rejected | executed | mcp_failed`, so the Bridge's
  `PARTIALLY_COMPLETED` state has nowhere to be reported on the wire.
- What changed on the Bridge side since this gap was first written:
  the `CommandState` machine now includes `PARTIALLY_COMPLETED`
  (`nse_bridge/server/commands.py:209`), partial close exists locally
  (`trade_close_single_position`, `engine/positions.py:287`), close-by
  exists locally (`trade_close_position_by`, `positions.py:306`),
  SL/TP are serialized per ticket (`tests/unit/
  test_modify_serialization.py`), and partial closes reconcile against
  live MT5 truth (requested volume or retcode 10010 `DONE_PARTIAL`).
  This closes §25 items 13 and 15 only locally; the wire halves stay
  blocked on D5.
- Required from the Platform: a contract amendment adding `volume`
  (partial close) and `close_by_ticket` (close-by) to the envelope,
  and a wire representation for partial completion (a new
  `action.result` status or a `partial` payload marker) — plus the
  user/Platform decision on hedging-only semantics (MQL5
  `PositionClosePartial` constraints; netting = opposing deal). No
  wire shape is proposed here.

## 8. Journal / execution-event upload channel — D6

- Phase E status (2026-11-18): **open on the wire; the local
  collector now exists.** CONTRACT v1 still defines no journal/event
  endpoint or frame: the only sync kinds are
  `account | positions | history_orders | history_positions` (§3.3)
  and the only WS client→server frames are `heartbeat`,
  `action.result`, `chart.data`, `chart.tick` (§5.3).
- What changed on the Bridge side: the journal collector is built —
  `JournalCollector` (`nse_bridge/journal/collector.py:324`) with
  dedup keys (`journal/events.py:71`), bounded ranges and snapshot
  diff, 28 tests in `tests/unit/test_journal.py` — **and is now WIRED
  in production** (`c27bf0b`, closes gap N1): the `create_app`
  lifespan constructs it and assigns it to `SyncEngine.journal`,
  pinned by `tests/unit/test_production_wiring.py`. (The
  `docs/GAP-ANALYSIS.md` §2.8/§4 D6 statement "no journal collector
  exists" is now stale; this document is the current record.)
  History batches still carry the raw facts from which the Platform
  derives journal entries server-side.
- Required from the Platform: a contract amendment defining a
  journal/event channel, or a formal statement that journal derivation
  from sync batches is the intended design (in which case the Bridge
  builds only the local collector for GUI/diagnostics, not an upload
  path). No frame or endpoint shape is proposed here.

## 9. Symbol inventory / metadata sync + resolution service — D7

- Phase E status (2026-11-18): **open; the local side is now
  implemented.** CONTRACT v1 still has no sync kind carrying symbols
  and no endpoint accepting a symbol inventory, metadata snapshot, or
  resolution request/response. The only symbol data path is §17.1
  chart frames (per-symbol, on request/subscription).
- What changed on the Bridge side: the resolver, inventory, and
  metadata subsystems are built — `SymbolResolver`
  (`nse_bridge/symbols/resolver.py:140`, ambiguity reported, never
  guessed), `SymbolInventory` (`symbols/inventory.py:81`: all/visible/
  hidden/search/select/availability), `SymbolMetadata`
  (`symbols/metadata.py:82`, full broker field set), 29 tests in
  `tests/unit/test_symbol_resolution.py`. (The `docs/GAP-ANALYSIS.md`
  §2.4/§4 D7 statement "no resolver exists" is now stale.)
- Required from the Platform: a contract decision on how the server
  learns the broker's symbol inventory — e.g. a new sync kind, a
  chart-frame extension, or "on-demand only via chart frames" (in
  which case the resolver stays Bridge work, but the server never
  receives a full inventory). No shape is proposed here.

## 10. Accounting source-data channel — D8

- Phase E status (2026-11-18): **open; local labelling now exists.**
  Balance/equity/margin/credit/deals still flow only inside the
  `account` / `history_*` batches (CONTRACT v1 §3.3); there is still
  no dedicated accounting snapshot kind, no
  deposit/withdrawal/adjustment/fee surfacing, and no MT5-vs-derived
  labelling rule on the wire.
- What changed on the Bridge side: MT5-vs-derived labelling is
  implemented locally — `label_account_snapshot` /
  `label_deal` (`nse_bridge/journal/accounting.py:115,143`),
  `classify_deal` (199), `surface_balance_operations` (247); 6 tests
  in `tests/unit/test_accounting.py`. The labelled values have nowhere
  to be reported distinctly until the wire gains a labelling
  convention.
- Required from the Platform: a contract statement on whether the
  four sync kinds suffice (likely yes, with a labelling convention
  added to the batch payloads) or a new kind/field set. No shape is
  proposed here.

## 11. New Platform dependencies surfaced by Phase E

- **None.** The Phase E §25 verification (docs/ACCEPTANCE.md,
  "Phase E" table) surfaced no Platform dependency beyond D1–D13.
  The remaining §25 gaps split three ways: Platform-side (D1, D4,
  D5, D6, D7, D8, D9, D11 — listed above), CI/live-proof (merged main
  is green at `aae5075`, runs 44274450546 / 44274443061; the follow-up
  branch's CI is pending; live Platform and real-terminal proof), and
  Bridge-side operator items (§12 below, not Platform dependencies).
  The 4 real-MT5 defects (D1–D4 in `docs/GAP-ANALYSIS-PHASE-E.md`)
  surfaced no new Platform dependency either — all four were fixed
  Bridge-side.

## 12. Bridge-side open items surfaced by Phase E (NOT Platform dependencies)

Recorded here so they are not lost; none of these needs a Platform
change.

- **~~Durable diagnostic queue unwired~~ — CLOSED in `c27bf0b`** (§25
  item 23): `DurableDiagnosticQueue` is now wired as the
  `TelemetryEmitter`'s queue in the `create_app` lifespan
  (`nse_bridge/api/app.py`), pinned by
  `tests/unit/test_production_wiring.py`. Diagnostics survive a Bridge
  restart. (Kept here as a closed record so the earlier gap is not
  re-reported.)
- **~~Journal collector unwired~~ — CLOSED in `c27bf0b`** (§25 item
  19, gap N1): `JournalCollector` is now constructed in the
  `create_app` lifespan and assigned to `SyncEngine.journal`, pinned
  by `tests/unit/test_production_wiring.py`. The Execution screen's
  journal feed is fed in production.
- **Release-signing public key unprovisioned** (§25 item 27,
  Partial): no `nse_bridge/update_public_key.b64` and no
  `NSE_BRIDGE_UPDATE_PUBLIC_KEY` set; the updater refuses installs
  fail-closed (`NO_PUBLIC_KEY`). An operator must provision the
  Ed25519 public key before any update can install.
- **No `.ico` asset** (§25 item 27): `windows/` holds only the exe
  manifest; no icon asset exists for the frozen exe.
- **No rollback/recovery strategy written** (§25 item 27): zero
  matches for "rollback" in `nse_bridge/` and the release docs. A
  written rollback strategy is owed before the first tagged release.
- **`docs/GAP-ANALYSIS.md` is stale on the local side of D5/D6/D7:**
  its §2.8/§4 statements ("no journal collector exists", "no resolver
  exists", "close-by does not exist locally") predate this branch's
  implementations. The gap analysis needs a refresh pass; it is out of
  scope for this task (only this file and docs/ACCEPTANCE.md were
  editable).
