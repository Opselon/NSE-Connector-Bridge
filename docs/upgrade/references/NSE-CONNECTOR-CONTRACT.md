# NSE Connector — Shared Integration Contract (CONTRACT v1, reconciled)

> **Status: ADOPTED v1 — the as-built contract.**
> This document is the single shared contract between NSE-Platform (server)
> and NSE-Connector (Windows client). It is written as the
> **proposed Connector contract ADOPTED**, with every place the Platform's
> existing architecture — or the Connector's already-built client code —
> forces a change listed explicitly in [§14 Deltas register](#14-deltas-register).
>
> - **Base adopted:** `docs/PROPOSED_NSE_PLATFORM_INTEGRATION.md` in
>   `Opselon/NSE-Connector` @ `5f45603c` ("PROPOSED, NOT VERIFIED" — this
>   document is the reconciliation that adopts it).
> - **Wire truth where code exists:** `crates/connector-core/src/nse/`
>   (`gateway.rs`, `ws.rs`, `contract.rs`), `src/sync/mod.rs`,
>   `src/security/commands.rs`, `src/actions/` (`manifest.rs`, `risk.rs`,
>   `dispatcher.rs`, `kill.rs`, `contract.rs`), `src/mt5/models.rs`,
>   `src/mcp/client.rs`, and the mock NSE server
>   `crates/connector-core/tests/common/mock_nse.rs` + `nse_tests.rs`.
>   Where the proposed doc and the client code disagree, **the code wins**:
>   the server must accept what the client actually sends.
> - **Platform baseline verified against:** NSE-Platform `origin/main`
>   @ `10220be` (migrations `0001`–`0053`; see [§11](#11-platform-migration)
>   for why the connector migration is `0054`, not `0049`).
> - **Order of work (user directive 2026-11-17, "Do api first"):**
>   Platform API first → Connector client adaptation second → Windows
>   GUI/EXE/installer last.
> - **Amendment §17 (2026-11-17, user scope clarification ~13:39):**
>   chart/symbol data protocol over WS + server-side AI Agent
>   configuration API + decision audit trail + `modify_pending`
>   promotion. The dashboard "AI Agent" UI is Platform frontend work
>   AFTER the API milestone — the API is designed so the UI needs no
>   rework (§17.2). `open_order` remains an explicit OPEN DECISION
>   (§17.5). §17 supersedes the D-WS-1 "ticks out of v1" note
>   (delta D-WS-6, §14.4).
> - **Amendment §18 (2026-11-17, user clarification #2 ~13:42):**
>   full operation inventory — 123 operations, one row each, with
>   params/response/idempotency/scope/tier/read-vs-executing/status.
>   Supersedes D-CMD-1 as a scope limit (delta D-INV-1, §14.5).
>   The inventory is normative; per-operation status is tracked in
>   §18.15 (A=3, B=21, C=42, D=44, E=9, F=4).
> - **Scope:** demo accounts only. No real-money trades, ever, in v1.
>   No live MT5 terminal exists on the contract author's VM; nothing in
>   this document claims live MT5 verification beyond what the Connector
>   repo itself records as live-verified (see [§8](#8-mt5-local-method)).
>
> Normative language: **MUST / MUST NOT / SHOULD / MAY** follow RFC 2119.
> "Client" = NSE-Connector (`connector-core` + app shell).
> "Server"/"Platform" = NSE-Platform. "MT5 MCP server" = the local
> MetaTrader 5 built-in MCP server on the customer's PC (§8).

## 0. Sources and verification log

Every claim about existing code in this document was read from the
referenced file before being written (verify-before-claiming). Evidence:

| Claim | Source (read 2026-11-17) |
|---|---|
| Proposed REST/WS surface, error codes, envelope | `NSE-Connector/docs/PROPOSED_NSE_PLATFORM_INTEGRATION.md` |
| Implemented client endpoints, Bearer auth, error mapping | `NSE-Connector/crates/connector-core/src/nse/gateway.rs` |
| WS frames, 15 s heartbeat, backoff 500 ms→30 s, channels bound 128 | `.../src/nse/ws.rs` |
| `ValidateKeyResponse` / entitlements secure-default, flat `SyncBatchRequest` | `.../src/nse/contract.rs` |
| Command envelope, 2-op allowlist, validation order, replay cache 10,000 | `.../src/security/commands.rs` (+ `tests/nse_tests.rs` wire frames) |
| Sync bounds (32 pending, 50,000 dedup, page 500), checkpoint rule | `.../src/sync/mod.rs` |
| Tier model, permission strings, six Tier-3 actions, kill scopes | `.../src/actions/risk.rs`, `kill.rs`, `dispatcher.rs`, `manifest.rs` |
| `MessageEnvelope` (`protocol_version` `"1.0"`, `schema_version` `"2026-11-17"`), `action.result` shape | `.../src/actions/contract.rs`, `docs/ERROR_CATALOG.md` |
| MT5 MCP wire facts (127.0.0.1:22346/mcp, 2025-06-18, JSON-in-text, 107 actions) | `.../src/mcp/client.rs`, `src/mt5/models.rs`, `README.md`, `docs/GAP_ANALYSIS.md` |
| `desktop_sessions` / `client_tokens` (`nsc_` prefix, 30-day TTL, `sha256_hex`) | `NSE-Platform/backend/src/modules/identity/desktop.rs`, `backend/migrations/0002_client_connect.sql` |
| `trading_accounts` already exists (0031); migrations run to 0053 | `backend/migrations/0031_trading_import.sql`, `backend/migrations/` listing |
| `usage_counters` reserve/release pattern; deny codes | `backend/src/modules/feature_access/quota.rs`, `eval.rs` |
| Router composition; route strings are contract (PR #16); middleware order | `backend/src/bootstrap/router.rs` |
| Risk = pure-math sizing; journal = pure math, server-computed at write | `backend/src/modules/risk/specs.rs`, `backend/src/modules/journal/compute.rs` |
| Position reconstruction + dedup from deals | `backend/src/modules/trading_import/reconstruct.rs` |
| `ai_gateway` is an OUTBOUND client only; no MCP server, no WS gateway, no `api_keys`/`connector_devices` tables | `backend/src/modules/ai_gateway/client.rs`, `discovery.rs`; repo-wide grep |
| Worktree `feat/nse-connector-contract` @ `10220be` pushed to origin | `git push` exit 0, 2026-11-17 (see §16) |
| Chart MT5 actions (`get_chart_history`, `get_chart_ticks_history`, `get_marketwatch_symbols`, `market_book_*`, `add_marketwatch_symbol`, `get_time_information`) + pending-order modify gap | `NSE-Connector/docs/CAPABILITY_MATRIX.md` (#12–17, #52–56, #98–101), `docs/e2e-evidence/E005,E007,E009,E010,E100,E110–E114`, `docs/GAP_ANALYSIS.md`, `crates/connector-core/src/actions/risk.rs` (Tier 0 `get_` prefix, Tier 1 `market_book_subscribe`, Tier 3 `trade_modify_sl_tp`) |
| `modify_pending` has NO allowlist entry today; `CommandOperation` is only `ClosePosition` / `ModifySlTp` | `NSE-Connector/crates/connector-core/src/security/commands.rs` (allowlist check line ~143, `unsupported_operations_never_execute` test) |
| §18 inventory sources: 136-item matrix, tier histogram 41/26/34/6, `TRADE_ACTION_MODIFY` + `ENUM_ORDER_TYPE` + `PositionClosePartial` + `ACCOUNT_LEVERAGE` | `NSE-Connector/docs/CAPABILITY_MATRIX.md`, `docs/GAP_ANALYSIS.md`, `docs/ERROR_CATALOG.md`, `docs/e2e-evidence/E001–E199`, `crates/connector-core/src/actions/risk.rs`; MQL5 Reference (mql5.com): `enum_trade_request_actions`, `orderproperties` (`ENUM_ORDER_TYPE`), `ctradepositionclosepartial`, `mqltraderequest`, `accountinformation`; book "Modifying a pending order" (mql5.com) |

## 1. Principles

1. **Platform is sole authority** for identity, subscription, entitlements,
   roles, risk, and persistence. The Connector adds no tables, no accounts,
   no subscriptions of its own (proposed doc §0).
2. **The MT5 MCP credential never leaves the customer's machine.**
   The Connector proves this by test
   (`mt5_credential_never_reaches_nse_core` in the Connector repo). No
   Platform endpoint ever accepts it, and the Connector MUST NOT send it
   in any header, body, query string, or log line.
3. **Delivery is not execution.** Every command is re-authorized locally
   (§6); a server frame alone never moves money.
4. **All trading intelligence stays on the Platform:** strategy, AI, risk
   decisions, signals, journal math, news/sentiment. The Connector is
   ONLY access / discovery / communication / execution. The Connector
   repo MUST NOT contain risk, strategy, AI, news, signal, or journal
   logic — only security checks (signature/expiry/duplicate/account
   match, allowlist, kill switch) as defined in §6.
5. **Demo only.** v1 accepts demo accounts only (§7.4). A live account
   MUST be refused server-side.
6. **Secure defaults.** Absent entitlement fields deserialize to
   *disabled*; unknown MT5 actions default to Tier 2 (fail-closed);
   unknown server error codes are treated as malformed, never as success.
7. **No silent loss.** Bounded queues evict oldest-with-warning, never
   silently; records that cannot be reconciled are dropped only with a
   logged reason and a `data_quality.validation_warnings` entry (§3.3).

## 2. Identities and authentication

### 2.1 User API key

- **Format (contract decision, Platform-side; the Connector treats the
  key as an opaque Bearer token and never parses the prefix):**
  `nse_live_` + 40 URL-safe base64 characters (240 bits from a CSPRNG).
  Example shape (not a real key): `nse_live_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA`.
- **Storage (Platform):** only the key **prefix** (first 12 characters,
  `nse_live_AAA…`) and the **SHA-256 hex digest** of the full value are
  stored (same hashing precedent as `desktop.rs`: `shared::crypto::sha256_hex`,
  constant-time comparison with `subtle::ConstantTimeEq`). The full value
  is shown **once** at creation and is never stored, logged, or returned
  again. If it is lost, the key MUST be rotated, not recovered.
- **Scopes** (stored per key; checked on every device call):
  `connector:read`, `connector:sync`, `connector:trade`
  (**OFF by default — explicit user opt-in required**),
  `mcp:read`, `mcp:trade`.
  Scope semantics: `connector:read` = validate/register/heartbeat/status;
  `connector:sync` = send sync batches and receive commands for
  allowlisted local-modify ops (§6.3); `connector:trade` = receive
  Tier-3 financial commands (§6.4) — inert without the `auto_trade`
  entitlement (§2.4) and the user's signal-execution setting (§6.4).
  `mcp:read` / `mcp:trade` gate the Platform MCP server (§9) only.
- **Management (website, session-auth, §3.5):** create, list (prefix,
  scopes, `last_used_at`, `revoked`, `expires_at` — never the digest),
  revoke, rotate. **Rotation** issues a new full value (shown once),
  keeps the same key id and scopes, and revokes the old value
  immediately; in-flight device sessions authenticated with the old
  value fail closed at the next call with `key_revoked`.
- **Expiry:** keys expire 1 year after creation by default (contract
  decision); expiry is returned in `validate-key` via the subscription
  block only when relevant — key expiry itself is signalled by
  `key_invalid`/`key_revoked` semantics (§3.6), and the website MUST
  surface `expires_at` in the key list.

### 2.2 Device identity and enrolment

- **Device id:** a UUID (v4) generated once by the Connector on first
  run, persisted in Windows Credential Manager alongside the API key,
  stable across restarts. A reinstall generates a new id and therefore
  counts as a new device against `max_devices`.
- **Enrolment = `POST /api/v1/connector/register-device`** (§3.2).
  The API key travels ONLY in the `Authorization: Bearer` header, on
  every request, together with `X-NSE-Device-Id`. The key is never in
  a body, query string, or log line (as implemented in `gateway.rs`).
- **Session:** `register-device` returns `session_expires_at`.
  Session lifetime is **30 days** (contract decision, aligning with the
  desktop precedent `CLIENT_TOKEN_TTL_DAYS = 30` in `desktop.rs`).
  The Connector re-registers (same device id) before expiry; the
  server refreshes the session. `POST /api/v1/connector/session/refresh`
  and `/session/revoke` (§3.4) are the explicit session operations.
- **Device credential exchange (DEFERRED — see delta D-AUTH-1, §15):**
  v1 does NOT put a separate per-device credential on the wire. The
  as-built client sends the API key as Bearer on every call, and the
  proposed doc agrees ("the key travels ONLY here"). A distinct
  per-device credential exchanged from the API key at enrolment is a
  defined v1.1 follow-up: the Platform MUST store sessions so the
  exchange can be added without breaking the client
  (`register-device`'s response object is extensible — the client
  ignores unknown fields — so a future `device_credential` field is
  wire-compatible), and revocation/rotation of the API key is the v1
  revocation mechanism.

### 2.3 MCP client credential (Platform MCP server only)

- A **separate** credential type for clients of the Platform MCP server
  (`POST /mcp`, §9), e.g. Claude Desktop. It is NOT the Connector API
  key and MUST NOT be accepted on Connector endpoints. Scopes
  `mcp:read` / `mcp:trade` apply. Issued/revoked from the website;
  same prefix+SHA-256 storage rule as §2.1 with prefix `nse_mcp_`.

### 2.4 Entitlements (server-resolved, secure-default)

Returned by `validate-key` (§3.1). The Connector MUST NOT hardcode
plans or quotas; every capability decision comes from this object.

```json
{
  "history_sync": true, "ai_coach": true, "position_manager": true,
  "auto_trade": false, "max_devices": 3, "max_accounts": 2
}
```

- All booleans default `false`, both maxima default `0` when absent
  (as implemented: `#[serde(default)]` on every field, `Entitlements:
  Default`). `max_devices: 0` means "no device may enrol".
- The Platform derives entitlements from the subscription/tier system
  via `feature_access` (the capability authority, §11.3). The
  Connector's `validate-key` response MUST be consistent with
  `GET /api/me/capabilities` for the same user.
- `position_manager` gates the §6.3 allowlist ops; `auto_trade` gates
  Tier-3 financial commands (§6.4). `history_sync: false` means the
  Connector MUST NOT send sync batches (it MUST still send heartbeats
  so the server can tell it why).

### 2.5 Rate limits and usage counters

- **Burst throttling** reuses the Platform's in-memory limiter
  (`shared::ratelimit`, per-second/per-minute; resets on restart).
  Contract v1 per-device burst limits (decisions, not existing code):
  `validate-key` 10/min, `register-device` 5/min,
  `sync/batch` 60/min, `heartbeat` 12/min, WS frames 120/min.
  Exceeding a burst limit returns `429 rate_limited` (§3.6); the
  Connector backs off (same 500 ms→30 s doubling schedule as WS
  reconnect, §5.4) and MUST NOT retry a 429 in a tight loop.
- **Durable quotas** reuse the `feature_access::quota` pattern
  (`usage_counters`, conditional-upsert `reserve()` /
  decrement-on-failure `release()`, `quota_exhausted` on exhaustion):
  sync batches are counted per user per day under feature key
  `connector:sync_batch`; API-key validations under
  `connector:validate_key`. Daily limits are **plan-derived** through
  `feature_access` (no hardcoded number in this contract); an
  unbounded plan skips the counter exactly as `quota.rs` prescribes.
- **Usage counters per key:** every device call increments a
  per-key counter (`last_used_at`, calls per day) used for the website
  key list and for abuse detection. Counters MUST NOT contain key
  material, only the key id.

## 3. REST surface (adopted)

Base URL (production default, as configured in the Connector client):
`https://bridge.nexusverse.space`. All paths below are **versioned
`/api/v1/connector/`** (adopted from the proposed doc — see delta D-REST-1).
Route strings are contract: they are scanned by the PR #16 route-coverage
checks, so a string MUST NOT change silently; moving a route to another
file is fine, changing its string is not (`router.rs` header).

Auth summary: **device calls** — `Authorization: Bearer <api_key>` +
`X-NSE-Device-Id: <device_id>` on every call (as implemented in
`gateway.rs:post()`). **Management calls** — website session auth
(cookie + CSRF, like every other website route; the CSRF guard in
`bootstrap` rejects cookie-bearing cross-site state changes, so Bearer
device calls MUST NOT be CSRF-gated). **MCP calls** — §9.

The Connector client as-built calls exactly five endpoints
(`validate-key`, `register-device`, `sync/batch`, `heartbeat`, WS).
Endpoints marked **[client-v1.1]** are defined here so the Platform
implements them now; the Connector adaptation phase adds the calls.
Endpoints marked **[added]** are not in the proposed doc or the client;
they are required by the website/management side and conflict with
nothing.

### 3.1 `POST /api/v1/connector/validate-key` — device auth + entitlements

Request (as implemented in `gateway.rs:validate_key()`; body carries no
secrets):

```json
{ "device_id": "550e8400-e29b-41d4-a716-446655440000", "connector_version": "0.1.0" }
```

- `connector_version` is the Connector's `CARGO_PKG_VERSION`
  (currently `0.1.0`). The server SHOULD record it per device and MAY
  answer `feature_not_enabled` for versions below a published minimum
  (the website publishes the minimum; the Connector surfaces the
  server's message verbatim).
- Response 200 (as implemented in `contract.rs:ValidateKeyResponse` —
  every field except `valid` and `entitlements` is optional; absent
  means disabled/unknown):

```json
{
  "valid": true,
  "user_id": "server-resolved-uuid",
  "subscription": { "active": true, "plan": "pro", "expires_at": "2099-01-01T00:00:00Z" },
  "entitlements": {
    "history_sync": true, "ai_coach": true, "position_manager": true,
    "auto_trade": false, "max_devices": 3, "max_accounts": 2
  }
}
```

- `user_id` is **server-resolved, never client-asserted** (the
  Connector MUST echo it back only as received, never invent it).
- Errors: `401 key_invalid` | `key_revoked`; `403 subscription_expired` |
  `device_limit_exceeded` | `account_not_authorized` | `feature_not_enabled`;
  `429 rate_limited` (§3.6). On any 401/403 the Connector MUST stop
  unauthorized synchronization immediately (proposed doc §1) and surface
  the distinct user-facing state for the code — it MUST NOT retry with
  the same key, and MUST NOT fall back to an unauthenticated mode.

### 3.2 `POST /api/v1/connector/register-device` — enrolment / re-registration

Request (as implemented): `{ "device_id": "…", "connector_version": "0.1.0" }`
with Bearer + `X-NSE-Device-Id` headers. Response 200 (as implemented
in `contract.rs:RegisterDeviceResponse`):

```json
{ "registered": true, "session_expires_at": "2099-01-01T00:00:00Z" }
```

- Enrolment is idempotent per `(user_id, device_id)`: re-registering
  the same device refreshes the session instead of creating a duplicate
  row. Enrolling beyond `max_devices` returns `403 device_limit_exceeded`
  and MUST NOT evict another device silently.
- The server records per device: `device_id`, `connector_version`,
  `first_seen_at`, `last_seen_at`, `session_expires_at`, and the bound
  account (§7.2). The Connector MUST call `register-device` on first
  run and after any 401 on another endpoint (re-enrolment is the
  recovery path; it MUST NOT prompt for the API key again unless
  `register-device` itself returns `key_invalid`/`key_revoked`).
- **This endpoint replaces the v1-brief `POST /api/connector/devices/enroll`**
  (delta D-REST-2). There is no separate enrolment endpoint.

### 3.3 `POST /api/v1/connector/sync/batch` — the ONLY sync channel

One bounded batch per call (as implemented: `gateway.rs:send_batch()`
serializes `SyncBatchRequest` verbatim). **Wire shape is the flat struct
below — NOT the full `MessageEnvelope`** (delta D-SYNC-1). The envelope
(`actions/contract.rs`, `protocol_version` `"1.0"`, `schema_version`
`"2026-11-17"`, `message_id` uuidv7, `event_type: "sync.batch"`,
`data_quality`, `sync_metadata.checkpoint`) is the Connector's logical /
diagnostic shape; the REST wire shape is:

```json
{
  "batch_id": "019a… (uuidv7)",
  "kind": "account | positions | history_orders | history_positions",
  "items": { "…raw MT5 JSON, broker timestamps verbatim…" },
  "count": 42,
  "produced_at": "2026-11-17T00:00:00Z"
}
```

- `batch_id` is a uuidv7 generated per batch by the Connector
  (`sync/mod.rs:make_batch`); it is the **idempotency key** for the
  batch. The server MUST dedup on `(account_id, batch_id)` and answer
  the same ack for a re-delivered batch without re-ingesting it.
- `kind` values are exactly the four above (`BatchKind::as_str`).
  `items` per kind (as built by `SyncEngine`):
  - `account`: a serialized `AccountSnapshot` — `{ "account": { "login",
    "server", "broker", "name", "type": "demo|live|contest",
    "currency", "balance", "equity", … }, "terminal": { "timestamp",
    "connection": { "state", … }, "build", "company", "program_name" } }`.
    This is the **authoritative account-identity message** (§7.2).
  - `positions`: `OpenPositionsResponse` — `{ "positions": […],
    "orders": […] }` (open positions + pending orders, live field names).
  - `history_orders`: `{ "orders": […], "deals": […] }` — already
    deduped client-side by `order_id` / `deal_id`; records without a
    ticket id are **dropped client-side** (they cannot be reconciled
    server-side) and MUST be reported in `data_quality` when the
    Connector later adopts the envelope on this path.
  - `history_positions`: `{ "positions": […] }` — closed positions,
    the realized-P/L source.
- **Broker-time rule (adopted verbatim from the proposed doc §2):**
  timestamps inside payloads are the broker's verbatim strings (ISO
  without timezone on the live server, e.g. `"2026-11-16T22:33:39"`).
  The Platform MUST store them as opaque broker time **plus** the
  connector-reported UTC receipt time (`produced_at` + server
  `received_at`). It MUST NOT re-localize them. Ordering/comparison
  uses the Connector's `parse_broker_time` rule (RFC 3339 or
  timezone-less ISO; unparseable values never order anything).
- **Account attribution (decision, delta D-SYNC-2):** batches carry NO
  per-batch `account_id` on the wire. The server attributes a batch to
  the account bound to the device (§7.2), established by the latest
  `account`-kind batch. A device is bound to **exactly one active MT5
  account at a time**. (A future client MAY add an `account_id` field;
  the server MUST ignore unknown fields, so this is wire-compatible.)
- **Checkpoint / resume (Connector-local in v1):** the sync engine keeps
  the history checkpoint as the MAXIMUM broker time seen, never moving
  backwards; the next history query range is
  `[checkpoint | "1970-01-01T00:00:00Z", now]`; page size 500; 250 ms
  pause between pages. The server does NOT need the checkpoint to
  resume — after downtime the Connector re-queries from its checkpoint
  and the server dedups by ticket id. A fresh device starts from
  `1970-01-01T00:00:00Z` (full backfill; server dedup makes it safe).
  The server MUST persist the highest ingested broker time per account
  as `last_synced_broker_time` for the website status endpoint (§3.5).
- **Pending-queue bound:** the Connector keeps at most 32 unacknowledged
  batches; the oldest is evicted **with a warning log** when full —
  never silently discarded (spec: bounded memory beats unbounded
  queues). The server MUST ack promptly so eviction stays a rare path;
  an evicted batch is re-created from the checkpoint on the next cycle.
- **Ack 200** (as implemented in `contract.rs:SyncBatchAck` and the
  mock): `{ "accepted": true, "batch_id": "…" }`. `accepted: false`
  MUST carry an `error` object (§3.6) explaining why. Unacked batches
  stay in the bounded local queue; recovery after downtime uses the
  server checkpoint + history range queries (proposed doc §2).
- **Ingestion rule:** the server MUST feed accepted batches through the
  SAME services as the website/MT5-report import — `trading_import`
  normalization + `reconstruct.rs` position reconstruction, `journal`
  math, `trading_analytics` — no duplicated logic, no second
  implementation of deal→position→journal (§11). Deals are deduped
  server-side on `(account_id, deal_id)` / `(account_id, order_id)`
  (UNIQUE constraints, §11.1); per-deal `commission`/`swap`/`fee` may
  be absent on some brokers (GAP_ANALYSIS E003) — the server MUST
  handle absence, never invent zeros as "measured" (store NULL, surface
  "not reported by broker").

### 3.4 `POST /api/v1/connector/heartbeat`, `session/refresh`, `session/revoke`

- **Heartbeat** (as implemented: `gateway.rs:heartbeat()` posts an empty
  JSON body `{}` and discards the response). Response 200, defined here
  (the client ignores the body in v1, so this shape is safe to define
  now): `{ "ok": true, "server_time": "RFC3339",
  "pending_commands": 0 }`. `pending_commands` is the number of
  commands awaiting delivery for this device — the Connector SHOULD
  open/refresh its WS session when it is non-zero and no WS is live.
  Heartbeat is the keep-alive used when the WS is down; it is NOT a
  substitute for the WS heartbeat (§5.3).
- **`POST /api/v1/connector/session/refresh`** **[client-v1.1]** (from
  the proposed doc §1; NOT implemented in the client): body `{}` with
  Bearer + device headers; response
  `{ "refreshed": true, "session_expires_at": "RFC3339" }`. Refreshes
  the device session without re-enrolling. A revoked device answers
  `401 key_revoked` (device row `revoked = true`).
- **`POST /api/v1/connector/session/revoke`** **[client-v1.1]** (from
  the proposed doc §1; NOT implemented in the client): body `{}` with
  Bearer + device headers; response `{ "revoked": true }`. Revokes the
  calling device's session immediately (logout). The API key itself is
  NOT revoked — only this device's session.

### 3.5 Management endpoints (website, session-auth) **[added]**

These are not in the proposed doc or the client; they are required for
the website to manage keys/devices/accounts and conflict with nothing.

| Method + path | Purpose |
|---|---|
| `POST /api/v1/connector/api-keys` | Create a key. Body: `{ "label": "…", "scopes": ["connector:read","connector:sync"], "expires_in_days": 365 }`. Response 201: `{ "id", "label", "prefix", "scopes", "expires_at", "created_at", "key": "<full value, ONLY here>" }`. `connector:trade` MUST be rejected unless the user explicitly opts in (separate `trade_opt_in: true` flag in the body — a bare scopes list containing it is an error). |
| `GET /api/v1/connector/api-keys` | List the caller's keys: `{ "id", "label", "prefix", "scopes", "revoked", "expires_at", "last_used_at", "created_at" }`. Never the digest or the full value. |
| `POST /api/v1/connector/api-keys/{id}/revoke` | Revoke now. Response `{ "revoked": true }`. In-flight device sessions fail closed at the next call. |
| `POST /api/v1/connector/api-keys/{id}/rotate` | New full value (shown once), same id + scopes, old value revoked immediately. Response as create. |
| `GET /api/v1/connector/devices` | List the caller's devices: `{ "device_id", "connector_version", "first_seen_at", "last_seen_at", "session_expires_at", "revoked", "account": { "login_masked", "broker", "server", "currency", "trade_mode" } }`. |
| `POST /api/v1/connector/devices/{id}/revoke` | Revoke a device session (and stop its commands). Response `{ "revoked": true }`. |
| `GET /api/v1/connector/accounts` | Website connected-account status: per account `{ "account_id", "login_masked", "broker", "server", "currency", "trade_mode", "connection": "connected|stale|disconnected", "last_sync_at", "last_synced_broker_time", "open_positions", "device_id" }`. This is the endpoint the E2E test (§13) and the website status UI read. `connection` is `stale` when `last_sync_at` is older than 5 minutes with a live WS expected, `disconnected` when the device session is revoked/expired or no WS is live and no heartbeat arrived within 90 s. |
| `GET /api/v1/connector/releases/latest` | Signed artifact metadata for the download manager / auto-update (§12.4). Response: `{ "version": "0.2.0", "download_url": "https://…", "sha256": "…", "size_bytes": 12345678, "min_connector_version": "0.1.0", "signature": "<base64 Ed25519 over the artifact bytes, or null>", "released_at": "RFC3339" }`. Session-auth is NOT required (the Connector fetches it with its API key); Bearer device auth is accepted and anonymous fetch returns only the public fields above. |

- **No separate management `validate` endpoint** (delta D-REST-3): the
  v1-brief `GET /api/connector/api-keys/validate` is folded into
  `POST /api/v1/connector/validate-key` (§3.1, the device endpoint the
  client implements) plus the key list's per-key status. Two validate
  semantics would diverge; one is the contract.

### 3.6 Error contract

Envelope (adopted from the proposed doc §1; identical in shape to the
Platform's existing `{"error": {"code", "message"}}` envelope, e.g. the
panic response in `bootstrap/router.rs`):

```json
{ "error": { "code": "key_invalid", "message": "human-readable, no secrets" } }
```

| HTTP | `error.code` | Meaning / client mapping (`NseErrorCode::parse`) |
|---|---|---|
| 401 | `key_invalid` | Unknown / malformed / expired API key → `NseError::InvalidApiKey`. No retry. |
| 401 | `key_revoked` | Key or device revoked → `NseError::ApiKeyRevoked`. No retry. Re-enrolment cannot fix it; the user must issue a new key. |
| 403 | `subscription_expired` | Subscription inactive/expired → `NseError::SubscriptionExpired`. |
| 403 | `device_limit_exceeded` | `max_devices` reached → `NseError::DeviceLimitReached`. |
| 403 | `account_not_authorized` | Account not authorized for this user — includes live accounts refused under the demo-only rule (§7.4) and accounts bound to another user → `NseError::AccountNotAuthorized`. |
| 403 | `feature_not_enabled` | Requested feature not entitled (e.g. sync with `history_sync: false`, trade without `auto_trade`) → `NseError::FeatureNotEnabled`. |
| 429 | `rate_limited` | Burst or durable quota hit → `NseError::RateLimited`. Back off; do not tight-loop. |
| 5xx | (any) | Transport-level unavailability → `NseError::ServerUnavailable { status }`. Retryable with backoff. |

- The client parses `error.code` at `/error/code` (falling back to a
  top-level `code`), exactly as `gateway.rs:code_error()` does. An
  unknown code on a 401/403 is a **malformed response**, never a
  success, and MUST be surfaced as such.
- Error messages MUST NOT contain key material, device secrets, SQL,
  or stack traces. Validation failures on batches MUST name the
  offending `batch_id` and `kind`, never echo the payload.

## 4. Manifest and diagnostics

### 4.1 Action manifest (MT5 MCP discovery)

- On each MT5 MCP connect, the Connector discovers the terminal's tools
  (`tools/list`) and classifies them into Tier 0–3 (§8.3). Live
  discovery on the reference terminal (MetaTrader 5 MCP v1.0, build
  6306, MetaQuotes-Demo) found **107 actions** (106 expected + 1;
  the +1 is handled per "more than expected", never by inventing
  actions). Current manifest hash: `b3df4d9fb2a3b7e0`.
- **`POST /api/v1/connector/manifest`** **[client-v1.1]** (from the
  proposed doc §4; the path is defined by this contract — the doc named
  no path — and the client does not call it yet): the Connector POSTs
  the discovered manifest so the website can show per-action
  availability and broker compatibility:

```json
{
  "device_id": "…",
  "manifest_hash": "b3df4d9fb2a3b7e0",
  "terminal_build": 6306,
  "mcp_server_version": "1.0",
  "action_count": 107,
  "tiers": { "tier0_read_only": 41, "tier1_local_modify": 26, "tier2_fs_code": 34, "tier3_financial": 6 },
  "actions": [
    { "name": "get_trading_account_info", "tier": "tier0_read_only",
      "category": "account", "required_permission": "",
      "input_schema": { "type": "object", "properties": {} } }
  ]
}
```

- The server stores the manifest per `(user_id, device_id)` and
  answers with `{ "accepted": true, "manifest_hash": "…" }`.
  Re-posting an unchanged hash is a no-op ack. Unknown actions in a
  manifest default to Tier 2 server-side too (fail-closed, §8.3).
- The Connector also sends a `manifest.update` envelope
  (`event_type: "manifest.update"`, `actions/contract.rs`) over the WS
  when the manifest changes mid-session (e.g. terminal upgrade adds
  tools); the server treats it the same as the REST post.

### 4.2 Diagnostics

- Connector health, MCP health, last sync, and per-tier counts are
  derived server-side from: device `last_seen_at`, WS liveness,
  last accepted batch per kind, and the stored manifest. No separate
  diagnostics endpoint is needed in v1 — `GET /api/v1/connector/accounts`
  (§3.5) and `GET /api/v1/connector/devices` carry the fields the
  website needs. If a dedicated diagnostics payload is added later it
  MUST reuse these fields, not invent new ones.

## 5. WebSocket — `/api/v1/connector/ws` (command / ack channel)

The WS is the **command/ack channel AND the chart-data channel**.
Sync travels over REST (§3.3); chart/symbol data travels over WS
(§17.1) — this supersedes the v1 statement that there is no
tick streaming (delta D-WS-6, §14.4).

### 5.1 Upgrade and authentication

- URL (production): `wss://bridge.nexusverse.space/api/v1/connector/ws`
  (`ws://` only for the local mock in tests). The upgrade is a plain
  `GET` with RFC 6455 headers **plus**:
  `Authorization: Bearer <api_key>` and `X-NSE-Device-Id: <device_id>`
  (as implemented in `ws.rs:open_session()` — auth travels in the
  upgrade headers, never in a frame body). A missing/invalid key fails
  the upgrade with `401`; a revoked device with `403`. The server MUST
  record the WS session in `connector_sessions` (§11.1) on upgrade and
  remove it on close.
- **There is no `hello` frame** (delta D-WS-2): the v1-brief
  `hello{device_id, connector_version, protocol_version=1}` does not
  exist in the client. Identity is established by the upgrade headers
  plus the prior `register-device` call. `connector_version` is already
  known server-side from `validate-key` / `register-device`.
- Production MUST be `wss://` (TLS). The client rejects `http`/`ws`
  base URLs except in tests (the mock uses `ws://127.0.0.1:0`).

### 5.2 Server → client frames (as parsed by `ws.rs:parse_server_message`)

Anything that does not parse as one of these is **silently ignored**
— a malformed frame MUST NOT crash the sync loop (as implemented).

| `type` | Payload | Meaning |
|---|---|---|
| `command` | §6.2 envelope (whole frame is the envelope) | Authorized command for this device/account. |
| `sync_request` | `{ "type": "sync_request" }` | Server asks for an out-of-band refresh. The Connector MUST re-send an `account` batch and then `positions` (this is also the account-switch trigger path, §7.3). |
| `ack` | `{ "type": "ack", "batch_id": "…" }` | Server acknowledges a batch over WS (in addition to the REST 200 ack, §3.3). Informational: the REST ack is authoritative. |
| `heartbeat` | `{ "type": "heartbeat" }` | Server heartbeat reply (see §5.3). |
| `chart.request` | §17.1 request | Server asks for candles (one-shot). |
| `chart.subscribe` | §17.1 subscription | Server asks for polled candle/tick updates. |
| `chart.unsubscribe` | `{ "type": "chart.unsubscribe", "subscription_id": "…" }` | Server ends a subscription. |

- **There is no `ping` frame and no `account.switch_required` frame**
  (delta D-WS-3): the server's `heartbeat` frame is the liveness
  reply, and account switching is driven by `sync_request` + the
  account-batch identity rule (§7.3), both of which the client already
  implements. Adding new frame types later MUST keep `parse_server_message`
  total (unknown types ignored, never fatal).

### 5.3 Client → server frames (as sent by `ws.rs:run_loop`)

- **Heartbeat:** `{ "type": "heartbeat", "device_id": "…" }` every
  **15 seconds** (as implemented: `heartbeat_interval` default 15 s —
  delta D-WS-4 against the v1-brief 30 s). The server MUST reply with a
  `heartbeat` frame and MUST consider the session dead after **45 s**
  (3× interval) without a heartbeat or any other frame, then close it.
  The Connector treats a missing reply as a dead connection and
  reconnects (§5.4).
- **Command results:** `action.result` envelopes (§6.5) are sent as
  text frames over this same socket (outbound channel, bound 128).
- **Chart data:** `chart.data` and `chart.tick` frames (§17.1) are
  sent as text frames over this same socket, sharing the 128-message
  outbound bound. Chart frames MUST NOT starve heartbeats or
  `action.result` delivery: the Connector sends results first, then
  chart frames, and drops the oldest unsent chart frame (with a
  warning) when the channel is full — chart data is re-derivable by
  re-request, command results are not.
- **No other client frames exist in v1.** In particular there is no
  `account.snapshot`, `account.event`, or `command.ack`
  frame: snapshots/events go over REST batches, and command
  acknowledgement IS the `action.result` envelope (§6.5).

### 5.4 Reconnect and resume

- Reconnect is a **fresh session**: `open_session()` carries no resume
  payload and no `last_event_id` (delta D-WS-5). Resume state lives in
  REST sync (checkpoints, §3.3) and in server-side command state (§6.6),
  not in the WS handshake.
- **Backoff** (as implemented): initial 500 ms, doubling, capped at
  30 s, plus jitter of `subsec_nanos % 100` ms. Backoff resets on a
  successful session. Shutdown is clean (`close()` frame; the
  supervisor task exits).
- **Missed commands:** on (re)connect the server MUST send every
  command in state `pending` (accepted by the server, not yet
  delivered-or-acked) for this device, oldest first. It MUST NEVER
  re-send a command in state `executed`, `rejected`, or `expired`
  (idempotency on `command_id`, §6.6). The client ALSO dedups
  (replay cache, §6.2), so a double delivery is rejected locally even
  if the server replays it.
- **Bounded channels:** 128 messages in each direction. A full outbound
  channel MUST NOT block the heartbeat loop; the oldest unsent result
  is dropped with a warning and re-derived from server state on the
  next `sync_request` (results are also recoverable: the server marks a
  command `delivered` but un-acked and re-sends it on reconnect).

## 6. Commands (server → Connector → MT5)

### 6.1 Vocabulary: what the server may send in v1

The Tier model (§8.3) classifies **106–107** MT5 MCP actions, but the
server-delivered command surface in v1 is exactly **three operations**
(the local allowlist in `security/commands.rs` — anything else is
rejected as `UnsupportedOperation` before it ever reaches MT5).
`modify_pending` is contract-defined here (§17.4) but is **NOT
sendable until its capability gate passes** — the Connector's
as-built allowlist still contains only the first two ops, and a
server MUST NOT send `modify_pending` to a Connector that has not
proven the extension (capability rule, §17.4):

| `operation.type` | MT5 MCP action | Tier / permission | Notes |
|---|---|---|---|
| `close_position` | `trade_close_single_position` | Tier 3 / `trading.execute` | Close one position by ticket. |
| `modify_sl_tp` | `trade_modify_sl_tp` | Tier 3 / `trading.execute` | Set SL/TP by ticket; `sl`/`tp` nullable (absent = leave unchanged). |
| `modify_pending` | `trade_modify_sl_tp` (order ticket) | Tier 3 / `trading.execute` | Set SL/TP on a **pending order** by ticket; `sl`/`tp` nullable. SL/TP only — price/volume/expiration changes have no verified MT5 MCP action (§17.4). Capability-gated: NOT sendable until §17.4 close-out. |

- **NOT sendable in v1 (explicit):** `open_order`
  (`trade_send_market_order`, `trade_send_pending_order`),
  `cancel_order` (`trade_delete_order`),
  `trade_close_by_position`, and every Tier 0–2 action.
  The v1-brief kinds `open_order | close_position | modify_sl_tp |
  modify_pending | cancel_order` are superseded (delta D-CMD-1),
  as amended by D-CMD-4 (§14.4) which promotes `modify_pending`
  into the contract (capability-gated) while `open_order` and
  `cancel_order` stay out. The old D-CMD-1 mapping of
  `modify_pending` to `trade_send_pending_order` /
  `trade_delete_order` was WRONG and is corrected by §17.4:
  modifying a pending order's SL/TP goes through
  `trade_modify_sl_tp` with an order ticket; placing a new pending
  order is `trade_send_pending_order`, i.e. the `open_order`
  family, which stays out (open decision, §17.5).
  Rationale: the as-built `CommandValidator` allowlist contains only
  the two ops above, and its tests assert `trade_send_market_order`
  is rejected. Order placement is the highest-risk surface and stays
  out of v1 until the Connector's dispatcher gate chain and the
  Platform's signal-execution + Risk Engine path (§6.4) are proven
  end-to-end on the two safe ops.
- The six Tier-3 action names are the **constraint** on any future
  expansion: a future v1.x that adds `trade_send_market_order` MUST
  add it to the allowlist in `security/commands.rs` first, with the
  financial-consent gate (§6.4), and bump `schema_version` (§10).

### 6.2 Wire envelope (test-verified)

The frame the server sends is the `CommandEnvelope` (whole JSON frame;
the `"type": "command"` discriminator sits alongside it and is ignored
by the validator's serde parse — unknown fields are ignored). This is
the exact shape asserted in `tests/nse_tests.rs`
(`expired_command_over_ws_is_rejected_locally`,
`duplicate_command_over_ws_is_rejected_as_replay`):

```json
{
  "type": "command",
  "command_id": "019a… (uuidv7, unique per command)",
  "account_id": "12345678901",
  "device_id": "550e8400-e29b-41d4-a716-446655440000",
  "operation": { "type": "close_position", "ticket": "9000001" },
  "issued_at": "2026-11-16T00:00:00Z",
  "expires_at": "2026-11-16T01:00:00Z",
  "idempotency_key": "019a… (== command_id in v1)"
}
```

```json
{
  "type": "command",
  "command_id": "019a…",
  "account_id": "12345678901",
  "device_id": "550e8400-e29b-41d4-a716-446655440000",
  "operation": { "type": "modify_sl_tp", "ticket": "9000001", "sl": 1.083, "tp": 1.087 },
  "issued_at": "2026-11-16T00:00:00Z",
  "expires_at": "2026-11-16T01:00:00Z",
  "idempotency_key": "019a…"
}
```

```json
{
  "type": "command",
  "command_id": "019a…",
  "account_id": "12345678901",
  "device_id": "550e8400-e29b-41d4-a716-446655440000",
  "operation": { "type": "modify_pending", "ticket": "9000002", "sl": 1.083, "tp": 1.087 },
  "issued_at": "2026-11-16T00:00:00Z",
  "expires_at": "2026-11-16T01:00:00Z",
  "idempotency_key": "019a…"
}
```

`modify_pending` uses the same shape as `modify_sl_tp`; the only
difference is that `ticket` names a **pending order**, not an open
position (§17.4). The server MUST NOT send it unless the §17.4
capability rule passes.

- `account_id` is the **MT5 login as a string** (e.g. `"12345678901"`),
  NOT the Platform `trading_accounts.id` UUID. The server MUST map
  between the two (§7.1).
- `operation` is a tagged object: `{ "type": "close_position",
  "ticket": "…" }` or `{ "type": "modify_sl_tp", "ticket": "…",
  "sl": <number|null>, "tp": <number|null> }`. `ticket` is a string
  (MT5 ticket ids exceed 32-bit ints; the tolerant MT5 models accept
  string-or-number on ingest but the command wire format is string).
- **The proposed doc §3 frame is NOT the wire format** (delta D-CMD-2):
  its `action_name` / `parameters` / `request_id` /
  `authorization_context` fields do not exist in the client. The
  server MUST send the envelope above. `authorization_context`
  (`user_id`, `grants`, `risk`) is a **server-side record** (stored in
  `connector_commands` + `connector_audit`, §11.1), not a frame field.
  The server MAY add `request_id`, `authorization_context`, or a
  `signature` field to the frame in future — the client ignores unknown
  fields — but MUST NOT rely on the client reading them in v1.
- **No per-command signature in v1** (delta D-CMD-3, against the
  v1-brief `signature` requirement): commands are authenticated by the
  mutually-authenticated WSS session itself (Bearer device credential
  in the upgrade headers + TLS) and by the server-side audit trail.
  Per-command Ed25519 signatures are a defined v1.1 addition; the
  frame reserves the `signature` field name for it.
- **Expiry:** `expires_at` MUST be short-lived (contract decision:
  default 5 minutes from `issued_at`, max 15 minutes). Expired
  commands are rejected locally and MUST be marked `expired`
  server-side without execution. Clock skew: the Connector uses UTC;
  the server MUST issue `expires_at` in UTC (RFC 3339, `Z`).

### 6.3 Local validation (Connector — security checks ONLY)

`CommandValidator::validate()` order — the server MUST assume this
order when interpreting rejections (malformed → expired → device →
account → allowlist → feature → replay; only a *successful* validation
consumes the replay id):

| Step | Check | Rejection |
|---|---|---|
| 1 | Envelope parses; `expires_at` is RFC 3339 | `Malformed` |
| 2 | `expires_at` > now (UTC) | `Expired` |
| 3 | `device_id` == this device | `WrongDevice` |
| 4 | `account_id` ∈ authorized accounts for this device | `WrongAccount` |
| 5 | `operation.type` ∈ {`close_position`, `modify_sl_tp`, `modify_pending`} | `UnsupportedOperation` |
| 5a | `modify_pending` only: the Connector build MUST contain the allowlist extension (§17.4); until it does, `modify_pending` is rejected as `UnsupportedOperation` exactly like any other non-allowlisted op | `UnsupportedOperation` |
| 6 | Feature enabled locally (`position_manager` / `auto_trade` entitlement as received in `validate-key`) | `AutoTradeNotEnabled` |
| 7 | `command_id` not in the bounded replay cache (10,000 ids) | `Replayed` |

Rejections are reported back as `action.result` with
`status: "rejected"` and the reason (§6.5). The Connector MUST NOT
execute a command that fails any step, and MUST NOT retry a rejected
command on its own — the server decides (re-issue with a new
`command_id` if the user re-authorizes).

### 6.4 Server-side authorization (Platform — the risk/strategy side)

Before issuing a command the server MUST pass its own gate chain
(mirroring the Connector dispatcher's gate chain in
`actions/dispatcher.rs`: kill switch → expiry → device binding →
account binding → manifest membership → input-schema validation →
tier permission → explicit financial consent for Tier 3 → replay
protection):

1. **Kill switch** (scopes: `global`, `user`, `account`, `device`,
   `auto_trade`, `category` — as implemented in `actions/kill.rs`):
   an engaged kill switch at any scope blocks instantly. The server
   MUST expose kill-switch state per user/account/device to the website
   and MUST refuse to issue commands while engaged. The Connector's
   local kill switch is independent: either side can block.
2. **Entitlement + scope:** `connector:trade` scope on the key AND
   `auto_trade` entitlement AND `position_manager` for the allowlist
   ops. Missing any one → do not issue (or issue and let the client
   reject with `AutoTradeNotEnabled` — the server MUST prefer not
   issuing, and MUST record the refusal in `connector_audit`).
3. **Risk Engine validation:** every financial command MUST pass the
   server Risk Engine (`modules/risk/` — the pure-math sizing engine
   in `specs.rs` plus the Platform's risk rules) *before* issue:
   position exists, ticket belongs to the account, SL/TP are sane for
   the symbol (pip size / stops level from the symbol registry), and
   the resulting exposure is within the user's risk limits. A command
   failing risk validation MUST NOT be issued.
4. **Signal-execution setting:** commands originating from a signal
   (as opposed to a direct user action in the website) additionally
   require the user's **signal-execution setting enabled**. When
   disabled, the server MUST NOT issue auto commands — no command
   row, no frame. The setting is per user (website setting, default
   OFF). Direct user actions (user clicks "close" in the website)
   bypass the signal setting but still require (1)–(3).
5. **Grants:** the effective grant recorded in `connector_commands`
   is `trading.execute` (the Tier-3 permission string from
   `RiskTier::required_permission()`), plus `local.modify` for the
   allowlist ops' local-modify aspect. The proposed doc's
   `authorization_context.grants: ["trading.execute"]` is stored
   server-side with the command, not sent on the wire (§6.2).
6. **AI Agent origin (§17):** a command originating from an active
   AI Agent profile/style additionally requires: the account's AI
   Agent settings enabled with `signal_execution` permitting
   execution (§17.2); Risk Engine validation against the account's
   **effective config** (§17.2 — profile limits, margin-level floor,
   simultaneous-position cap); and a `ai_agent_decisions` row written
   BEFORE the command is issued, recording the indicator inputs, the
   active profile/style, the proposed action, and the validation
   result (§17.3). A command failing any of these MUST NOT be issued.

### 6.5 Results — `action.result` (Connector → server, over WS)

The Connector reports every command outcome as a `MessageEnvelope`
with `event_type: "action.result"` (`actions/contract.rs`,
`ERROR_CATALOG.md` §1). Status values are exactly
`rejected | executed | mcp_failed` (`ActionStatus`, `dispatcher.rs`):

```json
{
  "protocol_version": "1.0",
  "schema_version": "2026-11-17",
  "message_id": "019a… (uuidv7)",
  "correlation_id": "<command_id>",
  "event_type": "action.result",
  "event_timestamp": "RFC3339 (UTC)",
  "device_id": "…",
  "user_id": "server-resolved-uuid (as received; omit if unknown)",
  "account_id": "12345678901",
  "broker_id": "MetaQuotes Ltd.",
  "source_tool": "trade_close_single_position",
  "payload": { "…raw MT5 result, verbatim where available…" },
  "data_quality": { "validation_warnings": [], "raw_available": true },
  "status": "executed",
  "started_at": "RFC3339", "completed_at": "RFC3339", "duration_ms": 42,
  "actual_mcp_result": { "…raw MT5 payload…" },
  "validation_errors": [],
  "execution_error": null,
  "error": {
    "code": "SYMBOL_NOT_IN_MARKET_WATCH",
    "category": "Prerequisite",
    "source_action": "trade_close_single_position",
    "raw": "<verbatim MT5 text>",
    "cause": "<one line>",
    "recovery": "<strategy>",
    "retry_safe": false,
    "server_intervention_required": false
  }
}
```

- The `error` object is present only when `status` is `rejected` or
  `mcp_failed`; its `code` comes from the Connector's error catalog
  (`ERROR_CATALOG.md` — e.g. `SYMBOL_NOT_IN_MARKET_WATCH`,
  `TRADING_NOT_PERMITTED`, `TOOL_NOT_FOUND`, `VOLUME_INVALID`).
  `retry_safe: false` means the server MUST NOT re-issue automatically.
- `duration_ms` is measured client-side (`started_at` → `completed_at`).
  `actual_mcp_result` preserves the raw MT5 payload (JSON-in-text
  unwrapped); mutation tools may answer plain text
  (`{"kind":"text","text":"…"}`), which is a success, not an error.
- **Delivery:** the envelope is sent as a WS text frame on the command
  socket. If the socket is down, the Connector MUST persist the result
  in its bounded pending queue and deliver it on reconnect (same
  32-batch bound semantics as §3.3; results are never dropped
  silently). The server MUST ack a result by marking the command
  terminal (`executed` / `rejected` / `mcp_failed`) and MUST persist
  it in `connector_command_results` (§11.1) before acking.
- **Round-trip invariant (test, §13):** every issued command reaches
  exactly one terminal state; a command never executes twice
  (server idempotency on `command_id` + client replay cache).

### 6.6 Server command lifecycle

States (stored in `connector_commands`): `pending` → `delivered` →
(`executed` | `rejected` | `mcp_failed`) | `expired`. Transitions:

- Issue → `pending` (row written with `command_id`,
  `idempotency_key`, `account_id` (UUID), `device_id`, `operation`
  JSON, `issued_at`, `expires_at`, `grants`, risk-validation
  reference, `signal_execution` flag, `created_by`).
- WS send → `delivered` (with `delivered_at`). On reconnect, `pending`
  commands are re-sent (§5.4); `delivered`-but-unacked commands are
  re-sent once, then marked `expired` if the device never acks before
  `expires_at`.
- Result received → terminal state + `connector_command_results` row.
- `expires_at` passes with no result → `expired` (a sweeper marks it;
  the Connector also rejects it locally if it arrives late).
- **Routing:** a command is delivered ONLY to the device it names
  (`device_id`) and ONLY while that device is bound to the named
  account (§7.2). A command for a different device/account is never
  re-routed. Tenant isolation: every query is scoped by `user_id`
  (§11.1); user A MUST NOT see, issue, or ack user B's commands
  (test, §13).

## 7. Accounts — identity, binding, switching, demo-only

### 7.1 Account identity

The MT5 account identity is the `AccountInfo` object from
`get_trading_account_info` (`mt5/models.rs` — all fields optional so
partial information is tolerated):

| Field | Type | Notes |
|---|---|---|
| `login` | string (number-as-string) | MT5 login, e.g. `"12345678901"`. Masked in all diagnostics (`security::mask_login`). The stable identity. |
| `server` | string | Broker server, e.g. `"MetaQuotes-Demo"`. |
| `broker` | string | Broker/company name, e.g. `"MetaQuotes Ltd."`. |
| `name` | string | Account holder name as reported. |
| `type` | `"demo" \| "live" \| "contest"` | As reported by the server. |
| `currency` | string (ISO 4217) | Account currency, e.g. `"USD"`. |
| `balance` / `equity` / `margin` / `margin_free` / `profit` / `credit` / `swaps` / `commissions` | number | Snapshot money values. |
| `read_only` / `margin_mode` | bool / string | Terminal flags. |
| `terminal.timestamp` / `terminal.connection.state` / `terminal.build` / `terminal.company` / `terminal.program_name` | — | Broker-time context, preserved verbatim, never re-localized. |

The Platform identity tuple is `{ login, broker_server, broker_name,
currency, trade_mode }` where `trade_mode` is the MT5 `type` value.
- **Platform mapping:** the Platform `trading_accounts` row for the
  account is matched on `external_ref_hash` = SHA-256 of
  `login + broker + server` (as created by migration `0031`), i.e.
  `UNIQUE (user_id, external_ref_hash)`. The raw login is never stored
  in `trading_accounts` — only `masked_external_reference` (e.g.
  `****4321`) and the hash. The Connector's `account_id` on the wire
  (the login string) MUST be resolved server-side to the UUID via this
  hash; the login string itself MUST NOT be persisted in any new
  connector table except where the command path needs it transiently
  (command rows reference the UUID; the login string appears only in
  the in-memory routing key and in audit entries as masked).
- **`terminal_id` is OPTIONAL and unset in v1** (delta D-ACC-1): the
  MT5 MCP surface exposes no stable terminal id (`TerminalInfo` has
  `build`/`company`/`program_name` but no id). The stable
  terminal-side identity is the `device_id`. A future terminal
  fingerprint (device + login + build) MAY be added as `terminal_id`
  without breaking the wire format (envelope fields are optional).

### 7.2 Device ↔ account binding

- A device is bound to **exactly one active MT5 account at a time**.
  The binding is established by the latest `account`-kind batch (§3.3)
  and stored in `connector_devices.active_trading_account_id` (§11.1).
- Positions/history batches are attributed to the bound account
  (§3.3, delta D-SYNC-2). The server MUST reject a positions/history
  batch from a device with no bound account (`403 account_not_authorized`)
  instead of guessing.
- `max_accounts` (entitlement) bounds how many distinct accounts a user
  may have bound across their devices (not per device). Binding a
  further account returns `403 account_not_authorized` (the entitlement
  error code for account-count overflow is `account_not_authorized`,
  matching the client's `AccountNotAuthorized` mapping).

### 7.3 Account switch

When the MT5 terminal logs into a different account, the Connector
sends a new `account`-kind batch whose `login` differs from the bound
account. The server MUST then, in one transaction:

1. Record the switch in `connector_audit` (`account_switched`, old →
   new masked logins) and in `connector_sessions` history.
2. **Invalidate pending commands for the old account** (mark them
   `expired` with reason `account_switched`; never deliver them after
   the switch).
3. Re-bind the device to the new account (new or existing
   `trading_accounts` row via `external_ref_hash`).
4. Require a **fresh sync**: the Connector sends `account` then
   `positions` then history from its checkpoint; the server MUST NOT
   merge the new account's positions into the old account's state.
5. **Never execute an old account's command on the new account:**
   the local `WrongAccount` check (§6.3) and the server routing rule
   (§6.6) both enforce this; a command whose `account_id` does not
   match the currently bound account is rejected, not re-targeted.
6. **History is preserved per account:** the old account's deals,
   positions, and journal entries remain queryable under that account;
   the switch does not delete or re-attribute them. A switch is a
   binding change, not a data migration.

The server MAY also proactively send `sync_request` (§5.2) when it
detects (e.g. via heartbeat metadata) that the terminal account may
have changed. There is no `account.switch_required` frame (§5.2).

### 7.4 Demo-only (v1)

- **Enforcement is server-side in v1** (the Connector parses
  `account_type` but enforces nothing on it — no demo gate exists in
  `connector-core`). The server MUST refuse `account`-kind batches
  with `type: "live"` (or `"contest"` — only `"demo"` is accepted in
  v1) with `403 account_not_authorized` and a message stating live
  accounts are not accepted; it MUST NOT create or bind a
  `trading_accounts` row for a live account, and MUST NOT issue
  commands for one. The website MUST surface the refusal and the
  account's `trade_mode` in `GET /api/v1/connector/accounts`.
- A Connector-side demo gate (refuse to sync a live account locally)
  is a defined adaptation follow-up, not a v1 requirement.

## 8. MT5 local method — the local MetaTrader 5 MCP server

**The MQL5 EA file-bridge approach from the original CONTRACT v1 brief
is VOID and superseded** (delta D-MT5-1). The integration method is
already decided and live-verified by the user (2026-11-16/17). This
section records the as-built method; neither side may re-decide it in
the v1 implementation.

### 8.1 The local server (live-verified facts)

- **Endpoint:** `http://127.0.0.1:22346/mcp` — MetaTrader 5 MCP
  server v1.0, terminal build 6306. Loopback only; the Connector is
  the only client. The MCP credential (Bearer token) never leaves the
  machine (§1.2).
- **Auth:** Bearer token mandatory; missing/wrong token → HTTP 401 +
  `WWW-Authenticate: Bearer`. The token is stored in Windows
  Credential Manager (§12.3), never in a file, log, or Platform call.
- **Sessions:** `Mcp-Session-Id` response header, echoed on every
  request. Unknown session → HTTP 404 (empty body); the client
  re-initializes once and recovers (`mcp/client.rs`).
- **Protocol:** JSON-RPC 2025-06-18, Streamable HTTP
  (`SUPPORTED_PROTOCOL_VERSION = "2025-06-18"`, sent as the
  `MCP-Protocol-Version` header; the client verifies the negotiated
  version matches). Handshake: `initialize` → `notifications/initialized`
  (→ HTTP 202). Unsupported protocol versions are negotiated down by
  the server — the client MUST verify the negotiated version.
- **Calls:** `tools/list` (discovery) and `tools/call`. Unknown tool →
  HTTP 200 + JSON-RPC `-32602 "tool not found"`.
- **Payloads:** tool results arrive as a JSON string inside
  `result.content[0].text` (**JSON-in-text**). Mutation tools may
  answer plain text with `isError: false` (e.g. `"market watch symbol
  added"`) — recorded as `{"kind":"text","text":"…"}`; that is a
  success, not an error.
- **Record identity:** `position_id` / `order_id` / `deal_id`,
  `action`, `create_time` / `open_time` (ISO without timezone),
  `price_last`, `price_open`, `contract_size`. Alternative field-name
  variants are accepted through serde aliases (tolerant models).

### 8.2 Why this method (and not an EA file bridge)

MT5's supported local surface for a third-party client is the built-in
MCP server: it is the vendor-provided, versioned, authenticated
interface, and it is the surface the user live-verified (107 actions,
real demo account, real positions/history). A custom MQL5 EA writing
JSON files under `MQL5/Files/` would be a second, unverified,
unauthenticated local protocol with its own install/update/diagnose
burden, no versioning, and no vendor support; it would also duplicate
the action surface the MCP server already exposes. Alternatives
considered and recorded: (a) MQL5 EA file bridge — rejected for the
reasons above; (b) native Rust→MT5 API — none stable/supported exists
(the original brief's premise, still true); (c) MT5 Manager API —
server-side broker API, not available to a retail customer terminal.
The Connector therefore speaks MCP to the local terminal and REST/WSS
to the Platform, and nothing else.

### 8.3 The 106-action / tier model (constrains §6)

- Discovery returns **107 actions** live (106 expected + 1; handled
  per "more than expected"). Classification over all live actions:
  **41 read-only (Tier 0), 26 local modifications (Tier 1), 34
  filesystem/code/network (Tier 2), 6 financial (Tier 3)**.
- Tiers and their required permission strings (`risk.rs`):
  Tier 0 `ReadOnly` (permission `""` — needs nothing),
  Tier 1 `LocalModification` (`local.modify`),
  Tier 2 `FilesystemOrCode` (`advanced.execute`),
  Tier 3 `Financial` (`trading.execute`).
  Unknown actions default to **Tier 2 (fail-closed)**.
- The six Tier-3 actions (verified financial execution paths):
  `trade_send_market_order`, `trade_send_pending_order`,
  `trade_close_single_position`, `trade_close_by_position`,
  `trade_modify_sl_tp`, `trade_delete_order`.
  The four `trade_check_*` / `trade_order_calc_*` tools are NOT
  Tier 3 (checks/calcs, not execution).
- The dispatcher gate chain (`dispatcher.rs`, spec §3.3/§7) is enforced
  locally regardless of what the server sends: kill switch → expiry →
  device binding → account binding → manifest membership →
  input-schema validation → tier permission → explicit financial
  consent for Tier 3 → replay protection. Rejections carry the reason
  back to the Platform as `action.result` (§6.5).
- **Kill switch scopes** (`kill.rs`): `global`, `user`, `account`,
  `device`, `auto_trade`, `category`. An engaged kill switch blocks
  instantly at any scope.
- **Broker-variation law** (`mt5/models.rs`): other MT5 MCP
  implementations or brokers may use different field names, so
  earlier/alternative names are kept as serde aliases; the Platform
  MUST tolerate the documented variants and MUST NOT reject a batch
  solely for a renamed-but-aliased field.
- **Known MT5 data gaps** (from `GAP_ANALYSIS.md`, the Platform MUST
  handle all of them): per-deal `commission`/`swap`/`fee` may be
  absent (account-level aggregates exist instead); SL/TP exist only
  on open positions, never in historical trades; account leverage is
  not a field (derivable via `trade_order_calc_margin`); no by-ticket
  lookup tool (filter from lists); market-book subscription is not
  idempotent (double-unsubscribe errors); market-closed is a distinct
  error (`TRADING_NOT_PERMITTED` — retry only at market open, after
  re-validation, never blindly); calc/trade tools require the symbol
  in Market Watch (prerequisite chain `add_marketwatch_symbol` →
  operation → cleanup).

### 8.4 Connector crates (as-built vs target)

As-built today (`NSE-Connector` @ `5f45603c`): exactly one crate,
`crates/connector-core` (Rust edition 2024; tokio / reqwest /
tokio-tungstenite / keyring), with 122 deterministic tests + 10
env-gated live MT5 tests. The GUI/EXE/installer is **not started**.
Target layout for the Windows app (the Platform agent does not build
this; the Connector agent does, GUI last per the API-first order):

- `nse-connector` — eframe/egui UI wizard (API-key screen with masked
  key + validate, MCP config/test/discover-tools, MT5 discovery,
  diagnostics screen with `READY | MISSING | OUTDATED |
  NEEDS_PERMISSION | FAILED | NOT_APPLICABLE` + Download/Install for
  missing, Run screen with `RUNNING | STOPPED`, health, last sync).
- `nse-connector-core` — the existing crate (pure-Rust protocol, sync
  state machine, account-switch guard, command validation). It MUST
  NOT gain risk/strategy/AI/news/signal/journal logic.
- `nse-mt5-discovery` — registry (`HKCU`/`HKLM` MetaQuotes),
  `%APPDATA%/MetaQuotes/Terminal`, Program Files, running processes;
  `cfg(windows)`-gated, Linux stubs so CI compiles.
- `nse-mt5-bridge` — **redefined by this contract (delta D-MT5-2):**
  the MCP client adapter to the local MT5 MCP server (§8.1) — account /
  positions / orders / deals / prices out, execution ops in, no cloud
  comms, no logic. It is NOT a file bridge and NOT an EA. (The crate
  name is kept so existing references stay valid; its contents are the
  MCP adapter, already substantially implemented as `connector-core`'s
  `mcp/` + `mt5/` + `actions/` modules.)
- The Connector MUST NOT ship an MQL5 EA, MUST NOT write files under
  any terminal data dir, and MUST NOT install anything into MT5.

## 9. Platform MCP server — `POST /mcp` (separate surface)

- **Transport:** Streamable HTTP, JSON-RPC **2025-06-18** (same
  protocol version as §8.1), `POST /mcp`. Auth: the separate MCP
  client credential (§2.3) as `Authorization: Bearer`, plus scopes
  `mcp:read` / `mcp:trade`. This surface is **new** — no MCP server
  exists in the Platform today (`ai_gateway` is an outbound client
  only, §11.3). It is NOT in the proposed doc or the Connector client;
  it is defined here because the v1 brief requires it and it conflicts
  with nothing.
- **Tools** (exact names are contract):
  `nse.get_account`, `nse.get_positions`, `nse.get_history`,
  `nse.get_performance`, `nse.evaluate_risk`, `nse.get_journal`,
  `nse.list_signals`, `nse.request_trade`.
  Read tools require `mcp:read`. `nse.request_trade` requires
  `mcp:trade` AND the `connector:trade` scope on the calling user's
  key AND the `auto_trade` entitlement AND the Risk Engine validation
  (§6.4) AND the user signal-execution setting; when the setting is
  disabled, `nse.request_trade` MUST NOT produce auto commands (it
  answers with an explicit `auto_trade_disabled` error, not a silent
  no-op).
- **Same-services rule:** every MCP tool MUST call the same Platform
  services as the REST endpoints and the website — `trading_analytics`
  for performance, `journal` for journal, `risk` for `evaluate_risk`,
  `market_intelligence` for signal context. No duplicated logic in
  the MCP layer; the MCP layer is a thin JSON-RPC adapter with
  tool-level audit (every call logged in `connector_audit` with
  `tool`, `user_id`, `account_id`, `result`, `duration_ms`).
- **MCP is NOT the account-sync channel.** Sync is §3.3 REST batches
  only. An MCP client MUST NOT be able to inject sync data, and the
  Connector MUST NOT use `POST /mcp` for sync.
- **Tool-level audit + rate limits:** MCP calls are rate-limited per
  credential (burst 60/min, same 429 contract as §3.6) and every call
  is audited. `nse.request_trade` calls additionally require a
  per-call idempotency key (`idempotency_key` in params, uuidv7);
  a repeated key returns the original result without re-executing.

## 10. Versioning and OpenAPI alignment

- **Protocol version:** `"1.0"` (`PROTOCOL_VERSION` in
  `actions/contract.rs`). **Schema version:** `"2026-11-17"`
  (`SCHEMA_VERSION`). Both are carried in every `MessageEnvelope`
  (§6.5). A breaking wire change MUST bump `schema_version` (date)
  and `protocol_version` (major) together; additive changes (new
  optional fields, new frame types the client ignores) MUST NOT bump
  either.
- **REST versioning:** the `/api/v1/` prefix is the version. A v2
  MUST be a new prefix; v1 paths MUST keep serving until the website
  announces removal with at least one Connector release in between.
- **OpenAPI:** every new Platform route MUST be annotated for the
  existing `apidocs` (utoipa) generation, following the `desktop.rs`
  precedent (its `#[utoipa::path(...)]` schema mirrors for
  `/api/desktop/session/start|poll`). The connector routes MUST appear
  in the generated OpenAPI document under the `connector` tag, with
  the exact request/response schemas from §3. The PR #16
  route-coverage scan covers route strings; the Platform agent MUST
  extend its expectations with the §3 strings (new strings are added,
  existing strings are never silently changed).
- **Connector version negotiation:** `connector_version`
  (`CARGO_PKG_VERSION`, currently `0.1.0`) is sent in `validate-key`
  and `register-device`. The server SHOULD maintain a minimum
  supported version and answer `feature_not_enabled` below it. The
  Connector MUST surface the server's message and MUST NOT fake a
  newer version.
- **MT5 MCP version:** the local protocol is pinned to `2025-06-18`
  (§8.1). If a newer MT5 build negotiates a different version, the
  Connector MUST verify it and MUST NOT silently accept an
  unverified version (the client errors unless negotiated ==
  `2025-06-18` as implemented).

## 11. Platform implementation

### 11.1 Migration `0054_connector.sql` (number is normative)

The migration number is **`0054`** (delta D-MIG-1): the v1-brief
`0049_connector.sql` is already taken
(`0049_customer_telegram_phase0.sql`; migrations run `0001`–`0053`).
The migration MUST be idempotent on re-run (the Platform's convention
— e.g. migration 0045) and MUST have a down migration that drops only
what it created (never `trading_accounts`).

**`trading_accounts` already exists** (migration `0031`: `id UUID PK`,
`user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE`,
`broker_label`, `server_label`, `account_currency` ISO-4217,
`account_type hedging|netting|unknown`, `masked_external_reference`,
`external_ref_hash`, `report_timezone`, `UNIQUE (user_id,
external_ref_hash)`). The migration MUST NOT recreate it. It MUST
extend it with connector-owned columns only:

```sql
ALTER TABLE trading_accounts
  ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'import'
    CHECK (source IN ('import', 'connector')),
  ADD COLUMN IF NOT EXISTS trade_mode TEXT
    CHECK (trade_mode IN ('demo', 'live', 'contest')),
  ADD COLUMN IF NOT EXISTS last_synced_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS last_synced_broker_time TEXT;
```

New tables (logical grouping only — one migration file, several
tables; `user_id` FK everywhere for tenant isolation):

- **`api_keys`** — `id UUID PK`, `user_id UUID NOT NULL REFERENCES
  users(id) ON DELETE CASCADE`, `label TEXT`, `key_prefix TEXT NOT
  NULL` (first 12 chars), `key_hash TEXT NOT NULL UNIQUE` (SHA-256
  hex), `scopes TEXT[] NOT NULL` (subset of §2.1; `connector:trade`
  only with explicit opt-in recorded), `trade_opt_in BOOLEAN NOT NULL
  DEFAULT FALSE`, `revoked BOOLEAN NOT NULL DEFAULT FALSE`,
  `expires_at TIMESTAMPTZ NOT NULL`, `last_used_at TIMESTAMPTZ`,
  `created_at / updated_at TIMESTAMPTZ NOT NULL DEFAULT now()`.
  Index: `(user_id, revoked)`, `(key_hash)`.
- **`connector_devices`** — `device_id TEXT PK` (UUID string),
  `user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE`,
  `api_key_id UUID REFERENCES api_keys(id) ON DELETE SET NULL`,
  `connector_version TEXT`, `active_trading_account_id UUID
  REFERENCES trading_accounts(id) ON DELETE SET NULL`,
  `session_expires_at TIMESTAMPTZ`, `revoked BOOLEAN NOT NULL DEFAULT
  FALSE`, `first_seen_at / last_seen_at TIMESTAMPTZ NOT NULL DEFAULT
  now()`. Index: `(user_id, revoked)`,
  `(active_trading_account_id)`. Uniqueness: one row per
  `(user_id, device_id)` (device_id is globally unique by
  construction; the PK enforces it).
- **`connector_sessions`** — `id UUID PK`, `device_id TEXT NOT NULL
  REFERENCES connector_devices(device_id) ON DELETE CASCADE`,
  `user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE`,
  `transport TEXT NOT NULL CHECK (transport IN ('rest','ws'))`,
  `started_at TIMESTAMPTZ NOT NULL DEFAULT now()`,
  `ended_at TIMESTAMPTZ`, `last_heartbeat_at TIMESTAMPTZ`.
  Index: `(device_id, ended_at)` for liveness queries. A WS row is
  created on upgrade and closed (`ended_at`) on disconnect.
- **`account_snapshots`** — `id UUID PK`, `trading_account_id UUID NOT
  NULL REFERENCES trading_accounts(id) ON DELETE CASCADE`,
  `user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE`,
  `snapshot JSONB NOT NULL` (the `AccountSnapshot` as received),
  `broker_time TEXT` (verbatim `terminal.timestamp`),
  `received_at TIMESTAMPTZ NOT NULL DEFAULT now()`,
  `batch_id TEXT NOT NULL`. Retention: keep the latest snapshot per
  account plus an **hourly rollup** (one snapshot per account per
  hour); older rows are pruned by a scheduled job. Index:
  `(trading_account_id, received_at DESC)`, `(batch_id)`.
- **`connector_events`** — the idempotent ingestion ledger:
  `event_id TEXT NOT NULL` (the batch `batch_id`, uuidv7),
  `trading_account_id UUID NOT NULL REFERENCES trading_accounts(id)
  ON DELETE CASCADE`, `user_id UUID NOT NULL REFERENCES users(id)
  ON DELETE CASCADE`, `device_id TEXT NOT NULL`,
  `kind TEXT NOT NULL CHECK (kind IN ('account','positions',
  'history_orders','history_positions'))`, `count INT NOT NULL`,
  `produced_at TIMESTAMPTZ`, `received_at TIMESTAMPTZ NOT NULL
  DEFAULT now()`, `raw JSONB NOT NULL` (the batch as received, for
  replay/audit), `UNIQUE (trading_account_id, event_id)` — a
  re-delivered batch is a no-op ack, never a double ingest. Index:
  `(trading_account_id, received_at DESC)`.
  Deal/order-level dedup is enforced where the data lands:
  `UNIQUE (trading_account_id, deal_id)` / `UNIQUE
  (trading_account_id, order_id)` on the import tables the
  `trading_import` pipeline writes (or, if those tables lack the
  constraint today, this migration MUST add it — the Platform agent
  verifies against the actual `0031` schema and records the outcome).
- **`connector_commands`** — `id UUID PK`,
  `command_id TEXT NOT NULL UNIQUE` (uuidv7, the idempotency key),
  `idempotency_key TEXT NOT NULL`, `user_id UUID NOT NULL REFERENCES
  users(id) ON DELETE CASCADE`, `trading_account_id UUID NOT NULL
  REFERENCES trading_accounts(id) ON DELETE CASCADE`,
  `device_id TEXT NOT NULL REFERENCES connector_devices(device_id)`,
  `operation JSONB NOT NULL`, `state TEXT NOT NULL DEFAULT 'pending'
  CHECK (state IN ('pending','delivered','executed','rejected',
  'mcp_failed','expired'))`, `grants TEXT[] NOT NULL DEFAULT '{}'`,
  `risk_check JSONB` (Risk Engine validation reference),
  `signal_execution BOOLEAN NOT NULL DEFAULT FALSE`,
  `issued_at / expires_at / delivered_at TIMESTAMPTZ`,
  `created_by TEXT NOT NULL` (`user` | `signal:<id>` | `mcp:<tool>`).
  Index: `(device_id, state)`, `(user_id, state)`,
  `(trading_account_id, state)`.
- **`connector_command_results`** — `id UUID PK`,
  `command_id TEXT NOT NULL REFERENCES
  connector_commands(command_id) ON DELETE CASCADE`,
  `user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE`,
  `status TEXT NOT NULL CHECK (status IN
  ('rejected','executed','mcp_failed'))`, `result JSONB NOT NULL`
  (the full `action.result` envelope), `duration_ms INT`,
  `received_at TIMESTAMPTZ NOT NULL DEFAULT now()`.
  Index: `(command_id)`.
- **`connector_audit`** — append-only: `id UUID PK`,
  `user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE`,
  `actor TEXT NOT NULL` (`user:<id>` | `device:<id>` | `system` |
  `mcp:<credential_id>`), `action TEXT NOT NULL`
  (`key.created|revoked|rotated`, `device.enrolled|revoked`,
  `account.switched`, `command.issued|delivered|result|expired`,
  `mcp.call`, `kill_switch.engaged|released`, …),
  `trading_account_id UUID REFERENCES trading_accounts(id)`,
  `device_id TEXT`, `detail JSONB NOT NULL DEFAULT '{}'`,
  `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`.
  Index: `(user_id, created_at DESC)`,
  `(trading_account_id, created_at DESC)`.
- **MCP credential table** (for §2.3/§9): `mcp_credentials` —
  same shape as `api_keys` with `key_prefix 'nse_mcp_'` and scopes
  restricted to `mcp:read` / `mcp:trade`. (May live in this migration
  or a follow-up; the Platform agent decides and records it.)
- **AI Agent tables (§17, same migration `0054`):**
  `risk_profiles`, `trading_styles`, `account_ai_agent_settings`,
  `ai_agent_decisions` — full column lists in §17.2/§17.3.
  `ai_agent_decisions` is append-only; `risk_profiles` /
  `trading_styles` are per-user named records; a profile/style that
  is active on any account MUST NOT be hard-deleted (archive it
  instead — `archived BOOLEAN`, excluded from new activations).
- **Chart cache table (§17.1):** `connector_chart_cache` —
  `trading_account_id UUID NOT NULL REFERENCES
  trading_accounts(id) ON DELETE CASCADE`, `user_id UUID NOT NULL
  REFERENCES users(id) ON DELETE CASCADE`, `symbol TEXT NOT NULL`,
  `timeframe TEXT NOT NULL CHECK (timeframe IN ('M1','M5','M15',
  'M30','H1','H4','D1','W1','MN1'))`, `candle_time TEXT NOT NULL`
  (verbatim broker time), `candle JSONB NOT NULL`, `received_at
  TIMESTAMPTZ NOT NULL DEFAULT now()`, `UNIQUE
  (trading_account_id, symbol, timeframe, candle_time)`. Retention
  per §17.1 (max 5,000 candles per account/symbol/timeframe,
  oldest-first prune; ticks are NOT persisted here).
- **Retention:** snapshots latest + hourly rollup (above); raw
  `connector_events` **90 days** (then prune, keeping the per-account
  `last_synced_broker_time` and the dedup keys needed for
  re-ingestion safety — the prune job MUST NOT delete the newest
  event per account); `connector_audit` 1 year; command rows 1 year
  after terminal state; `ai_agent_decisions` 1 year (§17.3);
  chart cache per §17.1 (5,000 candles per
  account/symbol/timeframe, oldest-first; ticks not persisted).

### 11.2 Router composition

- New module `modules/connector/` (or `modules/identity/connector.rs`
  if the Platform agent prefers — the module path is an implementation
  choice, the route strings are not). Its `router()` is merged in
  `bootstrap/router.rs::build()` alongside `desktop::router()` and the
  others. The WS route is registered with axum's WS support
  (`axum::extract::ws::WebSocketUpgrade`); the MCP route
  (`POST /mcp`) is registered in the same module.
- **No existing route string changes.** All §3 strings are new.
  Middleware order is unchanged (maintenance outermost → security
  headers → trace → CSRF → request-id innermost); device Bearer
  routes MUST NOT be CSRF-gated (they carry no cookies), while the
  §3.5 management routes ARE session+CSRF like other website routes.

### 11.3 Existing Platform reuse (verified — read before implementing)

| Reuse | Location | How the connector work builds on it |
|---|---|---|
| Desktop session flow | `modules/identity/desktop.rs` (`/api/desktop/session/start\|poll`, `desktop_sessions`, `client_tokens` `nsc_`, 30-day TTL, `sha256_hex`, constant-time compare) | **Connector auth MUST build on this, not duplicate it.** The API-key + device-session design (§2) mirrors its patterns: hashed secrets, prefix for leak-scanning, revocable tokens, session TTL. If the Connector's website login flow can reuse `session/start\|poll` for the initial key handoff, it SHOULD. |
| Risk Engine | `modules/risk/` (`specs.rs` pure-math sizing, `router()`) | Server-side command validation (§6.4 step 3). No risk logic in the Connector. |
| Journal | `modules/journal/` (`compute.rs` pure math, server-computed at write) | Journal entries from synced events; `nse.get_journal` reads the same service. |
| Trading import | `modules/trading_import/` (`normalize.rs`, `parsers/`, `reconstruct.rs` position reconstruction from deals, `pipeline.rs`) | Sync ingestion (§3.3) feeds this pipeline. No second implementation. |
| Market intelligence | `modules/market_intelligence/` (`news.rs`, `news_sentiment.rs`, `market.rs`, `candles.rs`, `indicators.rs`) | Signal context for `nse.list_signals` / `nse.evaluate_risk`; never in the Connector. |
| Feature access (capability authority) | `modules/feature_access/` (`eval.rs` decisions + deny codes, `quota.rs` `reserve()`/`release()` on `usage_counters`, `GET /api/me/capabilities`) | **API-key scopes MUST integrate with it** (§2.4/§2.5): entitlements derive from decisions; durable quotas use `reserve()`/`release()`; deny codes are the vocabulary for `feature_not_enabled` / `quota_exhausted`. |
| Trading analytics | `modules/trading_analytics/` (`api.rs`, `pipeline.rs`, `engines/`) | `nse.get_performance` + website account status read the same service. |
| Error envelope | `shared::errors::AppError`, `bootstrap/router.rs` panic envelope | `{"error": {"code", "message"}}` (§3.6) — already the house shape. |
| `ai_gateway` | `modules/ai_gateway/` (`client.rs`, `discovery.rs`) | **Outbound client only** (calls external `/v1/models` with a provider key). It is NOT an MCP server and MUST NOT be mistaken for one. The Platform MCP server (§9) is a new, separate module. |
| Model distribution | `modules/model_distribution/models.rs` (`/api/client/models/{id}/download`) | Precedent for signed-artifact download metadata (`releases/latest`, §3.5) — same shape discipline (version, URL, hash), new endpoint. |

Verified absent (repo-wide grep, 2026-11-17): no user API-key system,
no `connector_devices` / `api_keys` tables, no WebSocket gateway, no
MCP server, no `/api/connector` or `/mcp` route. The Platform agent
creates all of them under this contract.

## 12. Connector (Windows app) — target behaviour

(Target layout; only `connector-core` exists today — §8.4. GUI/EXE last.)

### 12.1 UI wizard (`nse-connector`, eframe/egui)

- **API-key screen:** masked key field + Validate button (calls
  `validate-key`; shows entitlements or the distinct error state per
  §3.6 code). The key is stored via §12.3 on success, never displayed
  again in full.
- **MCP config / test / discover-tools:** endpoint
  `http://127.0.0.1:22346/mcp` (editable, default as shown), Bearer
  token field (masked, stored via §12.3), Test (initialize handshake),
  Discover tools (runs `tools/list`, shows the 107-action manifest +
  tier histogram; a count mismatch is shown, never hidden).
- **MT5 discovery:** registry `HKCU`/`HKLM` MetaQuotes,
  `%APPDATA%/MetaQuotes/Terminal`, Program Files, running processes;
  `cfg(windows)`-gated with Linux stubs so CI compiles.
- **Diagnostics screen:** per-item state `READY | MISSING | OUTDATED |
  NEEDS_PERMISSION | FAILED | NOT_APPLICABLE` for: MT5 terminal
  present, MT5 MCP server reachable, MCP credential present, NSE API
  key valid, device enrolled, WS connected, last sync fresh. Missing
  items offer Download/Install where applicable (MT5 installer link;
  the Connector never bundles MT5).
- **Run screen:** `RUNNING | STOPPED`, connection health, last sync
  time, pending batches, last command result. Stop MUST stop sync and
  close the WS cleanly; it MUST NOT revoke the key or the device.

### 12.2 Core rules (`nse-connector-core`)

- Pure-Rust protocol, sync state machine, account-switch guard,
  command validation (§6.3). Security checks only — NO risk /
  strategy / AI / news / signal / journal logic anywhere in the repo.
- The Connector MUST stop sync immediately on 401/403 with the
  distinct user-facing state for the code (§3.1).

### 12.3 Secrets

- **Windows Credential Manager** via the `keyring` crate
  (`WindowsCredentialStore`; `MemorySecretStore` in tests) — as
  implemented in `security/secrets.rs`. Entry naming (contract
  decision — no app layer exists yet to verify against, so this is the
  name both sides agree now): service `NSE Connector`; entries
  `nse-api-key`, `nse-device-id`, `mt5-mcp-token`. Secrets are never
  logged (redaction in `security/redact.rs`), never hardcoded, never
  written to a file. The MT5 MCP token MUST NOT be sent to the
  Platform under any circumstance.
- Login masking: `security::mask_login` for any diagnostic display of
  the MT5 login (`****4321` style, matching the Platform's
  `masked_external_reference`).

### 12.4 Auto-update and installer

- Auto-update via `GET /api/v1/connector/releases/latest` (§3.5):
  the Connector compares `version` with its own, downloads from
  `download_url`, verifies **SHA-256** (mandatory — a mismatch aborts
  the update and the Connector MUST NOT execute the artifact), and
  verifies the Ed25519 `signature` when present (mandatory when the
  server signs; the server MUST sign production releases). Update
  applies on next start; a failed update MUST leave the running
  version intact.
- **Installer:** document the WiX (`.msi`) / NSIS (`.exe`) path in the
  Connector repo; ship a build script + install instructions even if
  the `.msi` is built in CI. The installer MUST NOT bundle MT5, MUST
  NOT write into any MT5 data dir, and MUST register the Credential
  Manager entries only at first run (never at install time).

## 13. Testing (conformance)

Both sides MUST test against this contract, not against each other by
vibes. The Connector's existing suites (122 deterministic + 10
env-gated live MT5 tests) are the client-side baseline; the Platform
adds the server-side suites below. **No test may claim live MT5
verification on a machine without a terminal** — mock tests are
reported as mock-verified, live tests as env-gated.

### 13.1 Connector (client) tests — as-built + required additions

Existing (in `NSE-Connector`, mock-verified): key validation (mock
server: valid / `key_invalid` / `key_revoked` / `subscription_expired` /
`device_limit_exceeded` / `account_not_authorized` /
`feature_not_enabled` / `rate_limited`); MCP test (handshake, 401,
404 session recovery, unknown tool `-32602`, JSON-in-text); discovery
(fixtures + Windows-only live test, ignored on Linux CI); diagnostics
states (all six); download checksum failure; Run/Stop; account switch;
MT5 restart; disconnect/reconnect/resume; duplicate events; expired
command; ack/result round-trip.
**Required additions for this contract (Connector adaptation phase):**
manifest POST shape; `session/refresh` + `session/revoke` calls;
`sync_request` handling (re-send account + positions); pending-command
replay on reconnect (no replay of executed ones); heartbeat 15 s /
45 s death detection; demo-account refusal surfacing (server 403 path);
per-command `action.result` delivery over WS with the offline queue.
**Required additions for §17 (Connector adaptation phase):**
`chart.request` handling (one-shot `get_chart_history` /
`get_chart_ticks_history` via the §17.1 mapping, with the
`add_marketwatch_symbol` prerequisite); `chart.subscribe` /
`chart.unsubscribe` with controlled polling (no MCP push exists);
`chart.data` / `chart.tick` emission with `request_id` /
`subscription_id` correlation; `modify_pending` allowlist extension
in `security/commands.rs` with tests + live proof that
`trade_modify_sl_tp` accepts an order ticket (§17.4 close-out).

### 13.2 Platform (server) tests — required

- **Migrations up/down on real Postgres** (`0054` applies cleanly on
  a database at `0053` and rolls back without touching
  `trading_accounts` data).
- **Auth:** key creation shows the full value once and never again;
  only prefix + SHA-256 stored; wrong key → `key_invalid`; revoked →
  `key_revoked`; rotated old value → `key_revoked`; expired key →
  `key_invalid`; scope missing → `feature_not_enabled`.
- **Enrolment:** idempotent re-register; `max_devices` enforced
  without silent eviction; device revoke blocks the next call.
- **WS lifecycle:** upgrade auth (missing Bearer → 401); heartbeat
  15 s / death at 45 s; reconnect replays `pending` only; malformed
  frames ignored; `sync_request` → Connector re-sync (mock client).
- **Isolation:** user A cannot see, issue, or ack user B's accounts,
  devices, commands, or MCP results (every query scoped by `user_id`).
- **Sync dedup:** re-delivered `batch_id` acks without re-ingest;
  duplicate `deal_id`/`order_id` across batches ingests once;
  ticket-less records dropped with a warning, never fabricated.
- **Command routing:** a command is delivered only to its named
  device and only while bound to its named account; account switch
  invalidates pending commands for the old account; an old account's
  command is never executed on the new account.
- **MCP auth/scopes:** read tools need `mcp:read`; `nse.request_trade`
  without `mcp:trade` / `connector:trade` / `auto_trade` / Risk Engine
  pass / signal-execution setting → refused, never executed; repeated
  idempotency key returns the original result.
- **Rate limits:** burst 429s; durable quota via `usage_counters`
  (`reserve`/`release`, `quota_exhausted`).
- **Journal consistency:** journal entries computed from synced events
  match the `trading_import` reconstruction for the same deals
  (one definition of the money math — `journal/compute.rs`).
- **AI Agent API (§17.2):** risk-profile / trading-style CRUD with
  validation of the core fields; per-account settings (activate /
  deactivate / effective config); tenant isolation on all four
  tables; a profile/style active on an account cannot be
  hard-deleted; `signal_execution: auto_execute` requires the
  `auto_trade` entitlement + `connector:trade` scope, otherwise
  refused.
- **Decision audit (§17.3):** every AI-originated command has a
  `ai_agent_decisions` row written before issue; rows are
  append-only (only `outcome` / `command_id` may be set once);
  indicator inputs and validation results are recorded, never
  recomputed from memory.
- **Chart protocol (§17.1):** `chart.request` validation (symbol,
  timeframe enum, `count` ≤ 1000, `from`/`to` range); duplicate
  `request_id` dedup; subscription cap (20 per device) and poll
  interval floor (5 s); burst limit 60 chart frames/min per device;
  error codes for unknown symbol / market-closed / timeout.
- **`modify_pending` capability gate (§17.4):** the server MUST NOT
  send `modify_pending` to a device whose recorded
  `connector_version` is below the published minimum or whose
  stored manifest lacks `trade_modify_sl_tp`; the gate is tested
  with a below-minimum version (refused, audited) and an
  above-minimum version (delivered to the mock client).

### 13.3 E2E (demo data only)

Mock MT5 adapter (the Connector's mock MCP server) feeding the MCP client → Connector → Platform →
Postgres → website status endpoint (`GET /api/v1/connector/accounts`).
Scenario: enrol device → validate-key → account batch (demo) →
positions batch → history batches → website shows the account
connected with matching balances → user issues `modify_sl_tp` from
the website → command delivered over WS → mock MT5 executes →
`action.result` (`executed`) → website shows the terminal state.
Then: duplicate batch delivery (dedup), WS drop + reconnect (resume),
account switch (old commands invalidated), expired command (rejected),
revoked key (sync stops). **Demo data only — no real-money trades,
no live accounts anywhere in the test path.**

## 14. Deltas register (normative)

Every place this contract differs from the two inputs. "Adopted" means
the proposed doc / client code is taken as-is; "delta" means this
contract changes it, with the reason. The Platform agent implements
the deltas; the Connector agent adapts its `nse/` client to the
`[client-v1.1]` items.

### 14.1 Deltas vs the proposed doc (`PROPOSED_NSE_PLATFORM_INTEGRATION.md`)

| ID | Change | Reason |
|---|---|---|
| D-REST-1 | Paths are `/api/v1/connector/*` (adopted as proposed). The v1-brief `/api/connector/*` prefix is superseded. | The proposed doc and the as-built client both use `/api/v1/connector/*`; the mock asserts those exact strings. One prefix, no alias. |
| D-REST-2 | Device enrolment is `POST /api/v1/connector/register-device` (adopted). The v1-brief `POST /api/connector/devices/enroll` is superseded. | Client-implemented endpoint; same reason. |
| D-REST-3 | No separate management `validate` endpoint; one `validate-key` (§3.1) + key-list status (§3.5). | Two validate semantics would diverge. |
| D-SYNC-1 | REST batch wire shape is the flat `SyncBatchRequest` (`batch_id, kind, items, count, produced_at`), NOT the full `MessageEnvelope`. | `gateway.rs:send_batch()` serializes the flat struct; the mock asserts it. The envelope is the logical/diagnostic shape. |
| D-SYNC-2 | Batches carry no per-batch `account_id`; the server binds device → one active account (§7.2). | The flat batch has no account field; attribution must come from the binding established by the `account` batch. |
| D-CMD-2 | Command frame is the `CommandEnvelope` (`operation` object), NOT the proposed doc's `action_name`/`parameters`/`authorization_context`/`request_id` frame. | `security/commands.rs` + `nse_tests.rs` wire frames are the as-built truth; the server must send what the client parses. `authorization_context` is stored server-side instead. |
| D-MIG-1 | Migration is `0054_connector.sql`, not `0049`. | `0049` is taken (`0049_customer_telegram_phase0.sql`); migrations run to `0053`. |
| D-MIG-2 | `trading_accounts` is extended, not created. | It exists since migration `0031` with `UNIQUE (user_id, external_ref_hash)`. |
| D-AUTH-1 | No separate per-device credential on the wire in v1; the API key travels as Bearer on every call. Exchange is a v1.1 follow-up (§2.2). | Both the proposed doc ("the key travels ONLY here") and the as-built client send the key every call. Revocation/rotation of the key is the v1 mechanism. |
| D-WS-4 | WS heartbeat is 15 s, not the v1-brief 30 s; death at 45 s. | `ws.rs` default `heartbeat_interval` is 15 s. |
| D-MT5-1 | The MQL5 EA file-bridge method is VOID; the local MT5 MCP server (§8.1) is the method. | User-decided and live-verified 2026-11-16/17; correction to the original brief. |
| D-MT5-2 | `nse-mt5-bridge` is redefined as the MCP client adapter, not a file bridge / EA. | Same reason; crate name kept, contents are the MCP adapter. |

### 14.2 Deltas vs the original CONTRACT v1 brief

| ID | Change | Reason |
|---|---|---|
| D-WS-1 | Sync is REST batches only; no `account.snapshot` / `account.event` / `market.tick` WS frames. | The client has no such frames; sync is `POST /sync/batch` (§3.3). Tick streaming is out of v1 scope entirely. |
| D-WS-2 | No `hello` frame on WS connect. | Auth is in the upgrade headers (`ws.rs:open_session()`); identity is established by `register-device` + headers. |
| D-WS-3 | No `ping` frame and no `account.switch_required` frame. | Server `heartbeat` frame is the liveness reply; switching is driven by `sync_request` + the account-batch rule (§7.3), both implemented. |
| D-WS-5 | Reconnect is a fresh session; no `last_event_id` resume on WS. | `open_session()` sends no resume payload. Resume lives in REST checkpoints (§3.3) + server command state (§6.6). |
| D-CMD-1 | Server may send only `close_position` + `modify_sl_tp` in v1 — NOT `open_order` / `modify_pending` / `cancel_order`. | The as-built `CommandValidator` allowlist (tests assert `trade_send_market_order` is rejected). Order placement stays out until the two safe ops are proven end-to-end. |
| D-CMD-3 | No per-command signature in v1 (field reserved for v1.1). | The client has no signature field; commands are authenticated by the WSS session + server audit. |
| D-ACC-1 | `terminal_id` is optional and unset in v1. | The MT5 MCP surface exposes no stable terminal id; `device_id` is the stable identity. |
| D-WS-4 | (as above) WS heartbeat 15 s, not 30 s. | As-built default. |

### 14.3 Additions (in neither input, required, no conflict)

- Management endpoints (§3.5: api-keys CRUD/rotate, devices list/revoke,
  accounts status, releases/latest); `POST /api/v1/connector/manifest`
  (path defined here; proposed doc named no path); `session/refresh` +
  `session/revoke` (named in the proposed doc, paths defined here);
  Platform MCP server `POST /mcp` (§9); `mcp_credentials` table;
  kill-switch exposure to the website (§6.4); `pending_commands` in
  the heartbeat response; per-key usage counters (§2.5).

### 14.4 Deltas from the §17 amendment (2026-11-17, user scope clarification ~13:39)

History above is intact; these entries amend it. Where an entry below
contradicts an earlier one, the entry below wins and says so.

| ID | Change | Reason |
|---|---|---|
| D-WS-6 | **SUPERSEDES the D-WS-1 note "ticks out of v1":** chart/symbol data now travels over WS (§17.1: `chart.request` / `chart.subscribe` / `chart.unsubscribe` server→client, `chart.data` / `chart.tick` client→server). The rest of D-WS-1 stands — account snapshots/events still travel over REST batches, never WS. | User scope clarification: the Platform must pull chart data for ANY symbol through the Connector. The Connector-side actions were already live-verified (`get_chart_history`, `get_chart_ticks_history`, E009/E010); the contract was the only thing missing. |
| D-CMD-4 | `modify_pending` is PROMOTED into the contract (§17.4): wire shape `{ "type": "modify_pending", "ticket", "sl"?, "tp"? }`, MT5 action `trade_modify_sl_tp` with an order ticket, SL/TP only. It is capability-gated and NOT sendable until the §17.4 close-out (Connector allowlist extension + live proof). Corrects D-CMD-1's mapping of `modify_pending` to `trade_send_pending_order` / `trade_delete_order` — that mapping was wrong: placing a new pending order is the `open_order` family, cancelling one is `cancel_order`. | `CAPABILITY_MATRIX.md` #54/#55 map "modify pending order SL/TP" to `trade_modify_sl_tp` (live-pending: verify order ticket accepted); #52/#53/#56 record price/volume/expiration changes as a possible gap with no dedicated tool. |
| D-AI-1 | New server-side REST surface `POST/GET/PATCH/DELETE /api/v1/connector/ai-agent/risk-profiles`, `.../trading-styles`, and per-account settings + effective config (§17.2). Tables `risk_profiles`, `trading_styles`, `account_ai_agent_settings`, `ai_agent_decisions` added to migration `0054` (§11.1). | User scope clarification: the dashboard gets an "AI Agent" section with activatable risk profiles and trading styles; the API must be complete now so the UI later needs no rework. |
| D-AI-2 | New append-only server-side record `ai_agent_decisions` (§17.3). Indicator computation is server-side only; the Connector never computes indicators or decides. AI-originated commands additionally require the §6.4 step-6 gate (effective-config Risk Engine validation + decision row written before issue). | Same clarification: the Platform is the main engine (analyzes, combines indicators, decides, manages risk); the Connector is chart-data-out / authorized-commands-in. |
| D-MIG-3 | Migration `0054` gains the four AI Agent tables plus `connector_chart_cache` (§11.1). | §17 retention rules need a bounded home for cached candles; ticks are never persisted. |

### 14.5 Deltas from the §18 inventory (2026-11-17, user clarification #2 ~13:42)

History above is intact; these entries amend it.

| ID | Change | Reason |
|---|---|---|
| D-INV-1 | **D-CMD-1 SUPERSEDED as a scope limit:** the full §18 inventory (123 operations) is now the target contract surface. D-CMD-1's proof-first rationale survives as a *sequencing* rule (status order A → B close-outs → rest), not as permission to drop operations from the inventory. | User clarification #2: the contract/API surface must be COMPLETE and EXHAUSTIVE because the analysis, AI Agent, and risk engines will all be built on it. |
| D-INV-2 | `open_order` (all 8 order-type variants, §18.8.1–18.8.8) is a DEFINED operation with status B ("pending user decision", §17.5) — neither silently included nor silently excluded. Its close-out path is: user approval + allowlist addition + Risk Engine/financial-consent proof on demo + `schema_version` bump. | Same clarification; the inventory must not hide the operation the AI Agent needs most. |
| D-INV-3 | `modify_pending` price/expiration changes (§18.9.3/18.9.4) are MQL5-supported (`TRADE_ACTION_MODIFY`) but MCP-coverage-UNVERIFIED; pending-order volume change (§18.9.5) is recorded as NOT POSSIBLE per MQL5 (delete + replace instead). | Official MQL5 docs (cited in §18.9) vs the live manifest gap (`CAPABILITY_MATRIX.md` #52/#53/#56). |
| D-INV-4 | Partial close (§18.8.10) is `trade_close_single_position` + `volume`, with the MQL5 `PositionClosePartial` constraints (hedging-only; oversize volume closes in full); close-all (§18.8.11) is a Platform-orchestrated composite with per-ticket commands — there is no single MT5 action for it. | MQL5 Reference, `CTrade::PositionClosePartial`; no single action exists for close-all. |

## 15. Ambiguities and open items

Things the proposed contract leaves ambiguous, and how this contract
resolves them (or leaves them open with an owner):

1. **Where the action manifest is stored** (proposed doc §5.4) —
   RESOLVED: Platform DB, per `(user_id, device_id)`, via
   `POST /api/v1/connector/manifest` (§4.1). The Connector MUST NOT
   introduce a new database.
2. **Role/permission model for `authorization_context.grants`**
   (proposed doc §5.2) — RESOLVED for v1: grants are the tier
   permission strings (`trading.execute`, `local.modify`, …) plus the
   API-key scopes (§2.1) and entitlements (§2.4); there is no separate
   Viewer/Analyst/Trader/Bot-Trader role model in v1. A role model is
   a Platform product decision, not a connector contract item.
3. **Heartbeat interval / reconnect window / session expiry**
   (proposed doc §5.3) — RESOLVED: WS heartbeat 15 s, death 45 s,
   backoff 500 ms→30 s + jitter, session 30 days (§5.3/§5.4/§2.2).
4. **WSS frame schema + ack semantics** (proposed doc §5.3) —
   RESOLVED: §5.2 frames; REST 200 ack authoritative, WS `ack` frame
   informational; results are `action.result` envelopes (§6.5).
5. **Endpoint paths + error codes** (proposed doc §5.1) — RESOLVED:
   §3.1–§3.6 (paths adopted from the proposed doc; codes adopted
   verbatim, with the client's `NseErrorCode::parse` mapping).
6. **The `sync.batch` envelope vs the flat batch** — RESOLVED by
   D-SYNC-1 (flat on the wire; envelope is logical). The Connector
   adaptation phase SHOULD align `MessageEnvelope::for_sync` usage
   with the wire shape (today `for_sync` exists but `send_batch`
   transmits the flat struct) and record the outcome in its repo.
7. **How `action.result` is delivered** — RESOLVED: WS text frames on
   the command socket (§6.5), with the bounded pending queue as the
   offline path. There is no separate REST result endpoint in v1.
8. **Manifest hash for a different terminal build** — OPEN (Connector
   agent): the hash `b3df4d9fb2a3b7e0` is for build 6306; the contract
   does not pin a hash per build. The server MUST accept any
   well-formed manifest and key availability by `(terminal_build,
   manifest_hash)`, never by a single hardcoded hash.
9. **Per-command signature (v1.1)** — OPEN (both agents): field name
   reserved (`signature`), algorithm to be Ed25519, key distribution
   to be defined before any v1.1 work starts.
10. **Per-device credential exchange (v1.1)** — OPEN (Platform agent):
    §2.2 defines the compatibility constraint (extensible
    `register-device` response); the exchange design itself is not in
    v1.
11. **Order placement (`trade_send_market_order` etc.)** — OPEN,
    deliberately out of v1 (D-CMD-1). Requires: allowlist change in
    `security/commands.rs`, financial-consent gate proof, Risk Engine
    path proof on the two safe ops first, and a `schema_version` bump.
    **Amended by §17.5:** `open_order` is now an explicit OPEN
    DECISION for the user (auto-trading needs it eventually; the
    D-CMD-1 proof-first rationale stands; command plumbing is
    generic so adding it later needs no rework). `modify_pending`
    is no longer part of this item — it is promoted by D-CMD-4
    (§17.4); `cancel_order` (`trade_delete_order`) stays out with
    `open_order`.
12. **`modify_pending` live proof** — OPEN (Connector agent):
    `trade_modify_sl_tp` accepting a pending-order ticket is
    live-pending (`CAPABILITY_MATRIX.md` #54/#55); pending-order
    price/volume/expiration changes have NO verified MT5 MCP action
    (#52/#53/#56, "possible gap") and are out of §17.4 scope until
    a schema check proves otherwise. Close-out is defined in §17.4.
13. **Chart subscription scale** — OPEN (Platform agent): the §17.1
    caps (20 subscriptions/device, 5 s poll floor, 60 chart
    frames/min/device) are contract decisions, not measured loads.
    The Platform agent measures MT5/WS load at those caps on demo
    data and records the outcome; caps may be tightened, never
    silently raised.
14. **Indicator set** — OPEN (Platform agent): §17.3 records
    indicator inputs as opaque name→value JSONB. Which indicators
    the AI Agent computes (and their exact parameter schemas) is a
    Platform product decision, server-side only; the contract does
    not name indicators, so the set can grow without a contract
    change.
15. **§18 inventory close-outs** — OPEN (both agents, tracked per
    row in §18.15): every B row needs its close-out (Connector
    allowlist extension + tests + market-open live proof +
    published minimum `connector_version`); every §18.16
    unverified item needs its input_schema recorded from live
    `tools/list` or a market-open probe. A B row MUST NOT be
    shipped as A.
16. **`open_order` user decision** — OPEN (user, §17.5/§18.8):
    the decision the whole AI Agent section waits on. Until the
    user approves, `trade_send_market_order` /
    `trade_send_pending_order` stay test-rejected and unsent.

## 16. Verification and conformance notes (for the implementers)

- This contract was written without a live MT5 terminal on the
  author's VM and without a running Platform instance. Nothing here
  claims a live end-to-end run. The Platform agent MUST run the §13.2
  suites against real Postgres; the Connector agent MUST run its
  suites against the mock NSE server and, where the env-gated live
  tests apply, a real terminal.
- **Conformance checklist for the Platform agent:** every §3 endpoint
  with the exact path + method + auth + error codes; migration `0054`
  up/down on real Postgres; `trading_accounts` extended, not
  recreated; command lifecycle states + routing rules; demo-only
  refusal; tenant isolation tests; OpenAPI entries; PR #16 route
  coverage updated. **§17/§18 additions:** every §17.2 AI Agent
  endpoint with validation + tenant isolation; `ai_agent_decisions`
  append-only + written-before-issue; chart protocol (§17.1) with
  caps/timeouts/retention; `modify_pending` capability gate
  (never send below the published minimum version); the §18
  inventory is normative — implement against it, track per-row
  status in §18.15, never ship a B row as A. Do not merge without
  explicit user approval; do not force-push; do not weaken or bypass
  failing tests.
- **Conformance checklist for the Connector agent:** adapt `nse/`
  to the `[client-v1.1]` endpoints (manifest, session refresh/revoke);
  keep the wire shapes in `gateway.rs` / `ws.rs` / `contract.rs` /
  `commands.rs` byte-compatible with §3/§5/§6 (they already are —
  the contract was written to match them); add the §13.1 additions;
  do NOT reintroduce an EA/file bridge; GUI last. **§17/§18
  additions:** chart handlers (§17.1 mapping, polling, prerequisite
  chain); `modify_pending` allowlist extension + tests + live proof
  (§17.4 close-out); per-B-row allowlist extensions with market-open
  proof and a published minimum version (§18.17); record every
  §18.16 unverified input_schema from live `tools/list`.
- **Change control:** changes to this document go through the same
  PR path as code (branch `feat/nse-connector-contract`, PR opened by
  the coordinator). A change to a route string, a wire field, or an
  error code is a breaking change and MUST bump the schema version
  (§10) and be called out in the PR description.

## 17. Chart data, AI Agent configuration, decision audit, `modify_pending` (amendment 2026-11-17)

User scope clarification (2026-11-17 ~13:39): the Connector's ONLY
job on the user's machine is to let the Platform reach the MT5
installed on their OS. Through it the Platform pulls CHART DATA for
ANY symbol the user has. The dashboard gets a NEW "AI Agent" section
(risk-management profiles + trading styles, each individually
activatable; when active, trades happen automatically via the
Connector). The PLATFORM is the main engine: analyzes, combines
several indicators, decides, manages risk, can change TP/SL. The
dashboard UI is Platform frontend work AFTER the API milestone — but
the API + contract in this section MUST NOT need rework when it
lands. No part of this logic goes into the Connector: chart data out,
authorized commands in (thin-client rule, §1.4).

### 17.1 Chart / symbol data protocol (WS)

Chart and tick data travel over the WS gateway (§5), NOT over REST
batches. This SUPERSEDES the D-WS-1 note "ticks out of v1"
(delta D-WS-6, §14.4). There is no MCP push in the MT5 MCP server
(`CAPABILITY_MATRIX.md` #100: "no MCP push; controlled polling
design") — live updates are produced by Connector-side polling of
the MT5 actions below, at the subscribed interval.

#### 17.1.1 Frames

**Server → Connector:**

| `type` | Shape | Meaning |
|---|---|---|
| `chart.request` | `{ "type": "chart.request", "request_id": "<uuidv7>", "symbol": "EURUSD", "timeframe": "M15", "from": "<broker time, optional>", "to": "<broker time, optional>", "count": 100, "include_ticks": false }` | One-shot candle request. `timeframe` ∈ `M1 M5 M15 M30 H1 H4 D1 W1 MN1` (the period enum in `get_chart_history`; E009 used `M15`). `from`/`to` are broker-time strings (same verbatim rule as §3.3); if both are absent the latest `count` candles are returned. `count` ≤ 1000; larger ranges MUST be paged by the server with successive requests. `include_ticks: true` additionally returns recent ticks (§17.1.2). |
| `chart.subscribe` | `{ "type": "chart.subscribe", "subscription_id": "<uuidv7>", "symbol": "EURUSD", "timeframe": "M15", "poll_interval_s": 15, "include_ticks": false }` | Polled subscription. The Connector re-queries the MT5 actions every `poll_interval_s` and pushes `chart.data` deltas (new/updated candles only) plus `chart.tick` when `include_ticks` is true. `poll_interval_s` default 15, minimum 5, maximum 3600. Max **20 concurrent subscriptions per device**; the 21st is refused with `chart.error` code `subscription_limit`. |
| `chart.unsubscribe` | `{ "type": "chart.unsubscribe", "subscription_id": "…" }` | Ends a subscription. Unsubscribing an unknown id is a no-op ack, never an error (subscriptions die with the WS session anyway; the server re-subscribes on reconnect). |

**Connector → server:**

| `type` | Shape | Meaning |
|---|---|---|
| `chart.data` | `{ "type": "chart.data", "request_id": "<uuidv7, one-shot> or null", "subscription_id": "<uuidv7, subscribed> or null", "symbol": "EURUSD", "timeframe": "M15", "candles": [ { "time": "2026-11-16T00:00:00", "open": 1.10226, "high": 1.10248, "low": 1.10201, "close": 1.10248, "tick_volume": 438, "spread": 1 } ], "broker_time": "<verbatim terminal time>", "received_at": "<RFC3339 UTC>", "complete": true, "status": "ok" }` | Candle payload. `candles[]` is the verbatim `history[]` array from `get_chart_history` (E009 shape: `time/open/high/low/close/tick_volume`, `spread` present only sometimes — the server MUST tolerate its absence). For subscriptions only new/changed candles are sent (the last candle is re-sent while still forming). `complete: false` with `status: "error"` carries `error_code` + `error_message` instead of candles (see §17.1.4). |
| `chart.tick` | `{ "type": "chart.tick", "subscription_id": "…", "request_id": "… or null", "symbol": "EURUSD", "ticks": [ { "time_ms": "2026-11-16T19:53:00.034", "bid": 1.10042, "ask": 1.10043 } ], "broker_time": "…", "received_at": "<RFC3339 UTC>" }` | Tick payload. `ticks[]` is the verbatim `history[]` array from `get_chart_ticks_history` (E010 shape: `time_ms/bid/ask`). Batched: one frame per poll cycle, never one frame per tick. |

Frames that do not parse as one of the above are silently ignored,
as for every other WS frame (§5.2). Chart frames share the
128-message outbound bound and MUST NOT starve heartbeats or
`action.result` (§5.3).

#### 17.1.2 Connector-side mapping (grounded in the live MT5 MCP actions)

| Need | MT5 MCP action (exact name) | Tier | Evidence |
|---|---|---|---|
| OHLC candles per timeframe | `get_chart_history` `{symbol, period, datetime_from, datetime_to, limit}` | Tier 0 (read-only) | `CAPABILITY_MATRIX.md` #17/#101; E009 (`EURUSD`, `M15`, `limit: 100`, `history[]` with `time/open/high/low/close/tick_volume`); `period` enum covers `M1..MN1` |
| Historical ticks / bid-ask | `get_chart_ticks_history` `{symbol, datetime_from, datetime_to, limit}` | Tier 0 | #14/#16; E010 (50 ticks, `time_ms/bid/ask`, `latency_ms: 2`) |
| Symbol inventory + full specs (38 fields: `digits`, `point`, `contract_size`, `tick_size`, `tick_value`, `volume_min/max/step`, `trade_stops_level`, `trade_freeze_level`, `swap_*`, `trade_mode`, `trade_exemode`, `trade_filling_mode`, `trade_order_mode`, margins) | `get_marketwatch_symbols` `{include_hidden: true}` | Tier 0 | #12/#13/#103–108; E101 (4096 symbols); `GAP_ANALYSIS.md` §2: the name misleads — with `include_hidden: true` it is the complete inventory |
| Live bid/ask + order book | `market_book_subscribe` → `market_book_get` → `market_book_unsubscribe` | `market_book_get` Tier 0; subscribe/unsubscribe Tier 1 | #14/#15/#98/#99/#109; E110–E114 (mechanics verified; book EMPTY while market closed — content is market-gated, not a protocol failure) |
| Market Watch prerequisite | `add_marketwatch_symbol` (then optional `remove_marketwatch_symbol` cleanup) | Tier 1 | E100 (`"market watch symbol added"`, plain-text success); `GAP_ANALYSIS.md` §3.2: calc/trade tools require the symbol in Market Watch; E199: removal is refused while the symbol is in use by a chart/position/order/EA |
| Broker-time basis | `get_time_information` `{utc_time, local_time, trade_server_last_known_time}` | Tier 0 | E005; the broker-time rule (§3.3) applies verbatim to chart data: candle/tick `time`/`time_ms` are stored as opaque broker time PLUS connector-reported UTC `received_at`; the Platform MUST NOT re-localize them |

- **No indicator computation in the Connector.** The Connector
  returns raw candles/ticks/specs only. All indicator math,
  signal combination, and decisions are server-side (§17.3).
- **Symbol availability:** any symbol the terminal has is
  requestable — the inventory comes from `get_marketwatch_symbols`,
  never from a hardcoded Platform list. An unknown symbol is an
  error (`symbol_not_found`, §17.1.4), never an invented empty
  series.
- **Prerequisite chain:** before a chart/tick query for a symbol
  not in Market Watch, the Connector MUST run
  `add_marketwatch_symbol` first (and MAY clean up with
  `remove_marketwatch_symbol` afterwards, skipping cleanup when
  E199 blocks it — a blocked cleanup is not an error).

#### 17.1.3 Throttling, batching, timeouts, idempotency

- **Burst limit:** 60 chart frames/min per device (WS frames count
  toward the §2.5 WS 120/min budget; chart frames have their own
  60/min sub-budget). Exceeding it returns `chart.error`
  `rate_limited`; the server backs off with the §5.4 schedule.
- **Batching:** one `chart.data` frame carries at most 1000
  candles; one `chart.tick` frame at most 1000 ticks. The server
  pages larger ranges. Subscription deltas send only new/changed
  candles.
- **Timeout:** the server waits **30 s** for a `chart.data`
  answer to a `chart.request`. On timeout it records `chart_timeout`
  and MAY retry once with a NEW `request_id`. A retry MUST NOT reuse
  the old `request_id` (a late answer to the old id is then
  identifiable as stale and discarded).
- **Idempotency:** `request_id` is a uuidv7 and is the idempotency
  key for one-shot requests. A duplicate `chart.request` with the
  same `request_id` MUST be answered from the Connector's
  in-memory cache of the previous answer (or re-queried once and
  answered identically) — never executed as a second independent
  MT5 call with side effects. The server dedups `chart.data` by
  `(request_id)` and subscription deltas by
  `(subscription_id, candle.time)`. Subscriptions are NOT resumable
  across WS reconnects: the server re-subscribes, and the Connector
  treats a re-subscribe as a fresh subscription (new
  `subscription_id`).

#### 17.1.4 Error codes (`chart.data` with `status: "error"`, or a `chart.error` frame of the same shape)

| `error_code` | Meaning | Retry |
|---|---|---|
| `symbol_not_found` | Symbol does not exist on this broker/terminal | No — flag the symbol in the registry |
| `symbol_not_in_market_watch` | Prerequisite failed even after `add_marketwatch_symbol` | Yes, once, after the prerequisite |
| `unsupported_timeframe` | `timeframe` outside `M1..MN1` | No — client bug |
| `market_closed` | No fresh data because the market is closed (book empty / no new ticks) — distinct from an error in the data path | Yes, at market open; never blind-retry in a tight loop |
| `chart_timeout` | MT5 MCP call exceeded the Connector's internal timeout | Yes, once, with a new `request_id` |
| `rate_limited` | §17.1.3 budget hit | Yes, with backoff |
| `subscription_limit` | > 20 concurrent subscriptions on the device | No — unsubscribe first |
| `manifest_missing_action` | The device's manifest lacks the needed chart action (e.g. older terminal build) | No — surface as terminal-capability gap |

Error payloads MUST NOT echo raw MT5 internals beyond the
`error_message` string, and MUST carry the `request_id` /
`subscription_id` so the server can correlate them.

#### 17.1.5 Retention (server-side)

- **Candles:** the server MAY cache recent candles per
  `(trading_account_id, symbol, timeframe)` in
  `connector_chart_cache` (§11.1), capped at **5,000 candles per
  account/symbol/timeframe**, pruned oldest-first by a scheduled
  job. The cache exists only so indicator computation does not
  re-fetch the same candles; it is a cache, not a history store.
- **Ticks:** raw ticks are NOT persisted beyond a short rolling
  window — at most the latest tick per `(account, symbol)` is kept
  durably (last bid/ask for display), and any in-memory tick
  buffer is capped at 24 hours. **No unbounded raw-tick
  persistence, ever.**
- **Decisions, not raw data:** what is kept long-term is the
  `ai_agent_decisions` record (§17.3 — indicator values + outcome),
  not the tick stream that produced it.

### 17.2 AI Agent configuration API (server-side REST)

All endpoints are **website session-auth** (cookie + CSRF, like
§3.5 management endpoints). They are **data, not logic**: profiles
and styles are named records with parameters; the Platform engine
reads them when deciding — nothing here executes a trade, and no
endpoint is callable by a device Bearer key. **The dashboard "AI
Agent" UI is OUT OF SCOPE for the API milestone**; these endpoints
are designed so the UI needs no API rework when it lands (list/get/
effective-config cover every read the UI will need; activate/
deactivate cover every write).

Base: `/api/v1/connector/ai-agent/`.

#### 17.2.1 Risk profiles

A risk profile is a named, per-user record. Core fields are typed
and validated; anything else goes in the extensible `parameters`
object (keys `^[a-z][a-z0-9_]{0,63}$`, values scalar or flat array
only — no nested objects, so future UI form generation stays
trivial).

Core fields (all REQUIRED on create except where marked):

| Field | Type | Validation |
|---|---|---|
| `name` | string | 1–80 chars, unique per user among non-archived profiles |
| `description` | string, optional | ≤ 500 chars |
| `max_risk_per_trade_pct` | number | `0 < x ≤ 5` (percent of equity risked on one trade) |
| `max_daily_drawdown_pct` | number | `0 < x ≤ 20` (percent of equity; breaching it disables auto-execution for the rest of the day) |
| `max_daily_loss_pct` | number | `0 < x ≤ 20`, and `≥ max_risk_per_trade_pct` |
| `max_simultaneous_positions` | integer | `1 ≤ x ≤ 50` |
| `margin_level_floor_pct` | number | `≥ 100` (default 150 when omitted — the Risk Engine MUST refuse a new auto-trade that would take margin level below the floor) |
| `stop_loss_required` | boolean | default `true` — when `true`, the Risk Engine MUST refuse any auto-trade without an SL |
| `max_position_size_lots` | number, optional | `> 0` when present |
| `allowed_symbols` | string[], optional | `null` = all symbols the terminal has; otherwise an allowlist of symbol names |
| `parameters` | object, optional | extensible extra parameters (see above); MUST NOT contain a key that duplicates a core field name |
| `archived` | boolean | default `false`; archived profiles cannot be newly activated (§11.1) |

Endpoints:

| Method + path | Purpose |
|---|---|
| `POST /api/v1/connector/ai-agent/risk-profiles` | Create. Body: the fields above. Response 201: the created record `{ "id", "user_id", "name", "description", "max_risk_per_trade_pct", "max_daily_drawdown_pct", "max_daily_loss_pct", "max_simultaneous_positions", "margin_level_floor_pct", "stop_loss_required", "max_position_size_lots", "allowed_symbols", "parameters", "archived", "created_at", "updated_at" }`. Validation failure → `400` with `error.code: "validation_failed"` and per-field messages. |
| `GET /api/v1/connector/ai-agent/risk-profiles` | List the caller's non-archived profiles (query `?include_archived=true` to include them). |
| `GET /api/v1/connector/ai-agent/risk-profiles/{id}` | Get one. 404 when it belongs to another user (tenant isolation — never reveal existence across users). |
| `PATCH /api/v1/connector/ai-agent/risk-profiles/{id}` | Update any mutable field (same validation as create). Changing a profile that is active on an account takes effect for the NEXT decision, never retroactively rewrites past `ai_agent_decisions` rows. |
| `DELETE /api/v1/connector/ai-agent/risk-profiles/{id}` | Archive (soft-delete: sets `archived: true`). Hard-delete is FORBIDDEN while the profile is active on any account (409 `profile_in_use`); an archived profile MUST NOT be activatable. |

#### 17.2.2 Trading styles

A trading style is a named, per-user record of **parameters only**
(data, not logic — the style names a way of trading; the Platform
engine implements what the name means, server-side). Same
`parameters` key/value rules as §17.2.1, same archive rule.

| Field | Type | Notes |
|---|---|---|
| `name` | string | 1–80 chars, unique per user among non-archived styles (e.g. `trend-following`, `mean-reversion`, `breakout`, `scalping` — examples only; the Platform MUST NOT hardcode a style list) |
| `description` | string, optional | ≤ 500 chars |
| `parameters` | object | Style parameters (e.g. `preferred_timeframes: ["M15","H1"]`, `indicator_set: ["ema","rsi"]`, `symbols: ["EURUSD"]`). Values are data the engine reads; the contract does not fix indicator names (§15 item 14). |
| `archived` | boolean | default `false` |

Endpoints (same shapes as profiles, with `trading-styles` in the
path): `POST` (201), `GET` list, `GET {id}`, `PATCH {id}`,
`DELETE` (archive; 409 `style_in_use` while active on any account).

#### 17.2.3 Per-account activation state

One settings row per `(user_id, trading_account_id)`:

| Field | Type | Notes |
|---|---|---|
| `trading_account_id` | UUID | The account the settings apply to |
| `active_risk_profile_id` | UUID, nullable | The active profile; `null` = no profile (auto-execution impossible) |
| `active_style_ids` | UUID[] | Zero or more active styles (empty = none) |
| `enabled` | boolean | default `false` — the master switch for AI Agent auto-trading on this account |
| `signal_execution` | enum `off \| suggest_only \| auto_execute` | default `off`. `suggest_only` = decisions are recorded (§17.3) but NO commands are issued. `auto_execute` = decisions may issue commands, and additionally requires the `auto_trade` entitlement + `connector:trade` scope (§6.4) — otherwise the server MUST treat it as `suggest_only` and record that downgrade in the settings response. |

Endpoints:

| Method + path | Purpose |
|---|---|
| `GET /api/v1/connector/ai-agent/accounts/{account_id}/settings` | Get the account's settings (creates nothing; 404 with `error.code: "settings_not_found"` when no row exists yet — the UI treats that as "never configured"). |
| `PUT /api/v1/connector/ai-agent/accounts/{account_id}/settings` | Create-or-replace the settings row. Body: `{ "active_risk_profile_id", "active_style_ids", "enabled", "signal_execution" }`. Referenced profile/styles MUST belong to the caller and MUST NOT be archived (else 400 `validation_failed`). Response: the stored row. |
| `POST /api/v1/connector/ai-agent/accounts/{account_id}/activate` | Set `enabled: true` (body `{}`; optionally `{ "active_risk_profile_id", "active_style_ids", "signal_execution" }` to set them atomically). Requires an active profile (400 `no_active_profile` otherwise) and, for `signal_execution: auto_execute`, the §6.4 gates. Response: the stored row. |
| `POST /api/v1/connector/ai-agent/accounts/{account_id}/deactivate` | Set `enabled: false` immediately. In-flight pending commands for the account are NOT retro-cancelled by deactivation (they were already issued and validated); no NEW AI-originated command may be issued afterwards. Response: the stored row. |
| `GET /api/v1/connector/ai-agent/accounts/{account_id}/effective-config` | **The UI's single read.** Returns the fully resolved config the engine uses: `{ "trading_account_id", "enabled", "signal_execution", "signal_execution_effective": "off|suggest_only|auto_execute", "risk_profile": { …full profile record or null… }, "trading_styles": [ …full style records… ], "limits": { "max_risk_per_trade_pct", "max_daily_drawdown_pct", "max_simultaneous_positions", "margin_level_floor_pct", "stop_loss_required", … } }`. `signal_execution_effective` is the entitlement-adjusted value (downgraded when the gates fail, with `downgrade_reason` when it differs from the stored setting). The UI MUST read this endpoint, not re-derive limits client-side. |

- **Tenant isolation:** every query is scoped by `user_id`
  (§11.1); user A MUST NOT see or change user B's profiles,
  styles, or settings (test, §13.2).
- **Audit:** every create/update/activate/deactivate is written to
  `connector_audit` (`ai_agent.profile.created|updated|archived`,
  `ai_agent.style.*`, `ai_agent.settings.updated|activated|
  deactivated`).

### 17.3 Decision audit trail (server-side, append-only)

Every AI Agent decision — whether or not it leads to a command —
is recorded in `ai_agent_decisions` (§11.1) BEFORE any command is
issued (§6.4 step 6). Columns:

| Column | Type | Notes |
|---|---|---|
| `decision_id` | UUID PK | uuidv7, generated per decision |
| `user_id` | UUID NOT NULL | tenant scope |
| `trading_account_id` | UUID NOT NULL | the account decided for |
| `symbol` | TEXT NOT NULL | e.g. `EURUSD` |
| `timeframe` | TEXT | e.g. `M15`; null when the decision is not timeframe-bound |
| `indicator_inputs` | JSONB NOT NULL | `{ "<indicator>": { "value": <number>, "params": {…}, "candle_time": "<broker time>" }, … }` — the exact inputs + values the engine used, with the candle times they came from. Opaque to the contract (indicator names are a Platform decision, §15 item 14); the Platform MUST record what it actually used, never reconstruct it later. |
| `active_risk_profile_id` | UUID NULL | the profile in force at decision time (snapshot by id; the profile record itself is the history — a later profile edit never rewrites this row) |
| `active_style_ids` | UUID[] NOT NULL DEFAULT '{}' | styles in force at decision time |
| `proposed_action` | TEXT NOT NULL | `open_order \| close_position \| modify_sl_tp \| modify_pending \| hold \| no_trade`, plus a `proposed_params` JSONB (e.g. `{ "side": "buy", "volume": 0.1, "sl": 1.083, "tp": 1.087 }` for `open_order` — recorded even though `open_order` is not yet sendable, §17.5) |
| `risk_validation` | JSONB NOT NULL | `{ "passed": true/false, "checks": [ { "check": "margin_level_floor", "passed": true, "detail": "…" } ], "reason": "…" }` — the Risk Engine result against the effective config (§17.2.3). A failed validation MUST NOT produce a command. |
| `command_id` | TEXT NULL | set (once) when a command is issued from this decision; null when no command was issued (`hold` / `no_trade` / validation failed / `suggest_only`) |
| `outcome` | TEXT NOT NULL DEFAULT 'pending' | `pending \| executed \| rejected \| mcp_failed \| expired \| superseded \| no_command`. Set to `no_command` at write time when no command is issued; otherwise updated ONCE from the command result (§6.5). |
| `created_at` | TIMESTAMPTZ NOT NULL DEFAULT now() | decision time |

- **Append-only:** rows are NEVER updated except for the single
  `outcome` (+ `command_id`) transition described above, and NEVER
  deleted. A correction is a NEW decision row (with
  `proposed_action: "no_trade"` and a note in `risk_validation`),
  never an edit.
- **Retention:** 1 year (§11.1).
- **Indicator computation is server-side only.** The Connector
  never computes indicators, never combines them, never decides.
  If a decision row ever references an indicator value the server
  cannot show the candles for, that is a Platform bug, not a
  Connector gap — the candles came over §17.1 and are cached per
  §17.1.5.

### 17.4 `modify_pending` — promoted into scope (capability-gated)

- **Wire shape** (`CommandEnvelope.operation`, §6.2):
  `{ "type": "modify_pending", "ticket": "<pending-order ticket>", "sl": <number|null>, "tp": <number|null> }`.
  Same shape as `modify_sl_tp`; the ticket names a **pending
  order**, not an open position. `sl`/`tp` absent or null = leave
  unchanged. **SL/TP only.** Price, volume, and expiration changes
  are NOT in this operation: the official MQL5 reference states a
  pending order's type and volume cannot be changed (delete +
  replace instead), and price/expiration changes go through
  `TRADE_ACTION_MODIFY` — for which NO MT5 MCP action has been
  verified in the live manifest (`CAPABILITY_MATRIX.md` #52/#53/#56:
  "possible gap … schema check required"). Until that schema check
  proves an MCP action covers them, they are out of scope (§18
  lists them as unverified, not as operations).
- **MT5 action:** `trade_modify_sl_tp` with an order ticket
  (`CAPABILITY_MATRIX.md` #54/#55: "verify order ticket
  accepted" — live-pending). The Connector adaptation MUST prove
  this at market open before the server sends anything.
- **Capability rule (normative):** the server MUST NEVER send
  `modify_pending` to a Connector unless BOTH hold:
  1. the device's recorded `connector_version` (from
     `validate-key` / `register-device`) is **≥ the published
     minimum version that contains the `modify_pending` allowlist
     extension** — the server maintains this minimum (published on
     the website; default: NO version qualifies until the
     Connector adaptation lands and proves it); AND
  2. the device's stored manifest (§4.1) contains
     `trade_modify_sl_tp` (Tier 3).
  If either fails, the server MUST NOT send the command; it MUST
  record the refusal in `connector_audit`
  (`command.refused`, reason `modify_pending_not_supported`) and,
  for AI-originated attempts, in the decision's `risk_validation`.
- **Known gap (close-out path):** the as-built Connector allowlist
  (`security/commands.rs`) contains ONLY `close_position` +
  `modify_sl_tp` and test-rejects everything else. Close-out
  requires, in the Connector repo: (a) a `ModifyPending` variant in
  `CommandOperation` + allowlist entry; (b) tests — allowlisted
  pass, wrong-device/wrong-account, replay, expiry, and a test
  asserting `modify_pending` with a POSITION ticket is still
  executed via `trade_modify_sl_tp` only when the ticket is an
  order (or that the distinction is enforced — the test MUST pin
  the chosen semantics); (c) live proof at market open that
  `trade_modify_sl_tp` accepts a pending-order ticket (an
  E-record like E009, with the order ticket + result); (d) a
  published minimum `connector_version` recorded in the contract
  (this document is updated with the version number when it
  lands). Until (a)–(d) are done, `modify_pending` is
  contract-defined but NOT sendable — the server gate in the
  capability rule enforces that.
- **Correction to D-CMD-1:** the old mapping of `modify_pending`
  to `trade_send_pending_order` / `trade_delete_order` was wrong
  and is superseded by this section (delta D-CMD-4, §14.4).
  Placing a new pending order is `trade_send_pending_order` — the
  `open_order` family (§17.5). Cancelling one is
  `trade_delete_order` — `cancel_order`, still out of v1 sendable
  scope (§18).

### 17.5 `open_order` — OPEN DECISION for the user

> **OPEN DECISION — needs the user's explicit call.** Auto-trading
> needs `open_order` eventually: an AI Agent that can only close
> positions and move stops can manage risk but can never enter a
> trade, so the "AI Agent" section cannot do what the user asked
> ("when active, trades happen automatically") until `open_order`
> is approved. The D-CMD-1 proof-first rationale still stands:
> order placement is the highest-risk surface, and it stays out
> until the two safe ops (`close_position`, `modify_sl_tp`) are
> proven end-to-end on demo accounts AND the `modify_pending`
> close-out (§17.4) is complete. The command plumbing is generic
> (§6.2 envelope + §6.4 gate chain + §17.3 decision audit), so
> adding `open_order` later needs NO contract rework — only: (1)
> the user's explicit approval, (2) a `CommandOperation`
> allowlist addition in `security/commands.rs` with tests, (3)
> Risk Engine + financial-consent gate proof on demo, and (4) a
> `schema_version` bump (§10). Until the user approves, the server
> MUST NOT send `open_order`, and `trade_send_market_order` /
> `trade_send_pending_order` MUST stay test-rejected in the
> Connector. The full inventory (§18) lists `open_order` as a
> DEFINED operation with status "pending user decision" — it is
> neither silently included nor silently excluded.

## 18. Full operation inventory (normative target surface)

User clarification #2 (2026-11-17 ~13:42): the contract/API surface
MUST be COMPLETE and EXHAUSTIVE, not a summary — every operation its
own entry — because the analysis, AI Agent, and risk engines added
later will all be built on it, and the user will live with it daily.
This section is that inventory. It SUPERSEDES D-CMD-1 **as a scope
limit** (delta D-INV-1, §14.5): the full inventory below is now the
target contract surface. D-CMD-1's proof-first rationale still stands
as a *sequencing* rule — operations are rolled out in status order
(A, then B close-outs, then the rest) — but no operation is dropped
from the inventory to match the current allowlist.

- **Sources of truth (read before writing this section):**
  `NSE-Connector/docs/CAPABILITY_MATRIX.md` (136 checklist items →
  107 live-discovered actions, statuses per item),
  `docs/GAP_ANALYSIS.md`, `docs/ERROR_CATALOG.md`,
  `docs/e2e-evidence/` (E001–E199), the live 107-action manifest
  as classified in `crates/connector-core/src/actions/risk.rs`
  (`classify()` overrides + read-prefix rule) and
  `src/actions/manifest.rs` (dynamic discovery from `tools/list`,
  never a hand-typed list), and the official MQL5 documentation
  (cited per addition below). The user estimates ~60–70 different
  APIs; the honest count is **124 entries** (§18.15) — the manifest
  alone holds 107 actions, and the inventory additionally lists
  composite operations (close-all), derived operations (margin
  level), and protocol frames (chart.*).
- **Thin-client rule stands throughout:** the Connector EXPOSES
  these operations; the Platform DECIDES when to call them. Nothing
  in this section puts strategy, indicator, risk-decision, or signal
  logic into the Connector (§1.4).
- **Sequencing (user's words):** once the Connector/contract side
  is DONE and complete, then the Platform side is built on top of
  it. The contract inventory is NORMATIVE and the Platform
  implementation MUST conform to it, with per-operation status
  tracked (§18.15). The Platform agent is concurrently building
  the API-first foundation against this inventory; a Platform
  operation whose status here is B/C/D/E/F MUST NOT be shipped as
  if it were A.
- **§17 vs §18:** §17 is the protocol/config (chart frames,
  AI Agent endpoints, audit, capability rules); §18 is the
  inventory. Chart/data operations live in both places BY
  REFERENCE, not duplicated definitions — §18 rows point at §17.1
  for wire shapes.
- **A row is an operation, not a field.** Field-level capabilities
  of one action (e.g. balance/equity/margin inside
  `get_trading_account_info`) are listed as separate rows because
  the user lives with them daily — but each row names the single
  underlying MT5 action, so the mapping stays auditable.

### 18.1 Status taxonomy (honest implementation status)

| Status | Meaning | Server may send/use it? |
|---|---|---|
| **A — Connector-allowlisted now** | In `CommandOperation` today; the Connector's tests prove it. | YES, under the §6.4 gates. |
| **B — Contract-defined, pending close-out** | Wire shape + capability rule defined here; needs a Connector allowlist extension and/or live proof (and, for `open_order`, the user's decision). | NO — not until its close-out is done and the minimum `connector_version` is published. |
| **C — Read-only, Connector-executable now** | Tier 0 MT5 MCP read the Connector's dispatcher can already execute (Tier-0 live dispatch verified); driven by the server via §3.3 sync, §5.2 `sync_request`, or §17.1 chart frames. No allowlist change needed. | YES, via those channels only. |
| **D — Local-only (Tier 1/2)** | Exists in the live manifest, but is a local-terminal operation the server MUST NOT invoke in v1 (chart UI, alerts, profiles, tester, files, services). Listed for completeness and for future scoping. | NO. |
| **E — Derived / Platform-side** | No MT5 action; computed server-side from verified inputs (fields proven in E001–E004/E101/E103/E104). | YES — server-side, no Connector call. |
| **F — Gap / not available** | No MT5 MCP action exposes it (verified absent in E-records), and no MQL5 equivalent is reachable through the MCP server. The operation is listed so its absence is explicit, not hidden. | NO — do not promise it. |

Common rules for every row: **idempotency** — every executing
command carries `command_id` (uuidv7) = `idempotency_key` and
`expires_at` (default 5 min from `issued_at`, max 15 min, §6.2);
re-issue means a NEW `command_id`. A timeout/transport failure
during a Tier-3 command is NEVER blind-retried — local state
read-back first, then reconciliation (`ERROR_CATALOG.md` §4 rule 1).
**Expiry** — `expires_at` is UTC RFC 3339; an expired command is
rejected locally (`Expired`) and marked `expired` server-side.
**Scope** — executing commands require `connector:trade` scope +
`auto_trade` entitlement + (for signal/AI origin) the
signal-execution setting (§6.4); allowlist ops additionally require
`position_manager`. Reads require `connector:read` (`connector:sync`
for batches). **Broker-time rule** (§3.3) applies to every timestamp
in every response.

### 18.2 Market data & ticks

| # | Operation | Direction | Params | Response | Idempotency / expiry | Scope | Tier | R/W | Status |
|---|---|---|---|---|---|---|---|---|---|
| 18.2.1 | `chart.request` (candles, one-shot) | S→C, WS frame (§17.1.1) | `request_id` uuidv7, `symbol`, `timeframe` M1..MN1, `from`/`to` broker time (optional), `count` ≤ 1000, `include_ticks` bool | `chart.data` with verbatim `candles[]` (`time/open/high/low/close/tick_volume`, `spread` sometimes) | `request_id` dedup; server timeout 30 s, retry once with NEW `request_id` | `connector:read` | — (frame) | Read | B — Connector chart handler pending |
| 18.2.2 | `chart.subscribe` (polled candles/ticks) | S→C, WS frame (§17.1.1) | `subscription_id` uuidv7, `symbol`, `timeframe`, `poll_interval_s` 5–3600 (default 15), `include_ticks` bool | Repeated `chart.data` deltas + `chart.tick` per poll cycle | `subscription_id`; dies with WS; re-subscribe on reconnect | `connector:read` | — (frame) | Read | B — Connector chart handler pending |
| 18.2.3 | `chart.unsubscribe` | S→C, WS frame | `subscription_id` | No-op ack (unknown id is not an error) | Idempotent by design | `connector:read` | — (frame) | Read | B — Connector chart handler pending |
| 18.2.4 | `chart.data` | C→S, WS frame (§17.1.1) | — (answers 18.2.1/18.2.2) | `candles[]`, `broker_time`, `received_at` UTC, `complete`, `status` | Correlated by `request_id`/`subscription_id`; server dedups by `(request_id)` / `(subscription_id, candle.time)` | — | — (frame) | Read | B — Connector chart handler pending |
| 18.2.5 | `chart.tick` | C→S, WS frame (§17.1.1) | — (answers 18.2.2 / 18.2.1 with `include_ticks`) | `ticks[]` (`time_ms/bid/ask`), batched per poll cycle, never one frame per tick | Correlated by `subscription_id`/`request_id` | — | — (frame) | Read | B — Connector chart handler pending |
| 18.2.6 | `get_chart_history` | C→MT5 (MCP) | `symbol`, `period` (M1..MN1), `datetime_from`, `datetime_to`, `limit` | `history[]` OHLC + `tick_volume` (+`spread` sometimes), `data_available_from` | Read-only: naturally idempotent; no expiry | `connector:read` | Tier 0 | Read | C — E009 verified (M15, `limit: 100`) |
| 18.2.7 | `get_chart_ticks_history` | C→MT5 (MCP) | `symbol`, `datetime_from`, `datetime_to`, `limit` | `history[]` (`time_ms/bid/ask`), `data_available_from` | Read-only: naturally idempotent | `connector:read` | Tier 0 | Read | C — E010 verified (50 ticks) |
| 18.2.8 | `market_book_subscribe` | C→MT5 (MCP) | `symbol` | Subscription handle / ack (plain-text success possible) | Prerequisite for 18.2.9; double-subscribe semantics per terminal — Connector tracks state | `connector:read` | Tier 1 | Executing (local) | C — E110 verified; Connector-orchestrated only, never directly server-issued |
| 18.2.9 | `market_book_get` | C→MT5 (MCP) | `symbol` | Order-book levels (`total_levels`, bid/ask levels) — EMPTY while market closed (E111), which is market state, not failure | Requires active 18.2.8 (`BOOK_SUBSCRIPTION_REQUIRED` otherwise) | `connector:read` | Tier 0 | Read | C — E008/E102/E111 verified (mechanics; content market-gated) |
| 18.2.10 | `market_book_unsubscribe` | C→MT5 (MCP) | `symbol` | Ack; double-unsubscribe ERRORS (E113) — Connector MUST track subscription state and never double-send | Not idempotent at the MCP layer — Connector-side state tracking makes it safe | `connector:read` | Tier 1 | Executing (local) | C — E112/E113 verified; Connector-orchestrated only |
| 18.2.11 | `get_time_information` | C→MT5 (MCP) | (none) | `utc_time`, `local_time`, `local_utc_offset_minutes`, `daylight_saving_time`, `trade_server_last_known_time` | Read-only: naturally idempotent | `connector:read` | Tier 0 | Read | C — E005 verified; broker-time basis for all ranges |

### 18.3 Symbols

| # | Operation | Direction | Params | Response | Idempotency / expiry | Scope | Tier | R/W | Status |
|---|---|---|---|---|---|---|---|---|---|
| 18.3.1 | List all symbols the terminal has | C→MT5 (MCP) `get_marketwatch_symbols` | `include_hidden: true` | `symbols[]` — 4096 on the reference terminal (E101) | Read-only: naturally idempotent | `connector:read` | Tier 0 | Read | C — E101 verified; the name misleads, it is the complete inventory |
| 18.3.2 | Symbol specification (per symbol) | C→MT5 (MCP) `get_marketwatch_symbols` | `include_hidden: true`, filter client-side by symbol | 38 spec fields: `digits`, `point`, `contract_size`, `tick_size`, `tick_value`, `volume_min/max/step`, `trade_stops_level`, `trade_freeze_level`, `swap_*`, `margin_initial/maintenance`, `calculation_mode`, `trade_mode`, `trade_exemode`, `trade_filling_mode`, `trade_order_mode`, `expiration_mode` | Read-only: naturally idempotent. NOTE: EURUSD reports `tick_value: 0.0` / `tick_size: 0.0` on the reference broker (E101) — the Platform MUST derive via calc tools when zero, never treat 0 as measured | `connector:read` | Tier 0 | Read | C — E101 verified |
| 18.3.3 | Add symbol to Market Watch | C→MT5 (MCP) `add_marketwatch_symbol` | `symbol` | Plain-text success (`"market watch symbol added"`, `isError: false`) — a success, not an error | Prerequisite step; safe to repeat (already-present is not a failure) | `connector:read` | Tier 1 | Executing (local) | C — E100 verified; Connector-orchestrated prerequisite (§17.1.2) |
| 18.3.4 | Remove symbol from Market Watch | C→MT5 (MCP) `remove_marketwatch_symbol` | `symbol` | Ack, or refusal when the symbol is in use (`MARKET_WATCH_REMOVE_BLOCKED`, E199) — a blocked cleanup is NOT an error | State-protected: skip cleanup when refused | `connector:read` | Tier 1 | Executing (local) | C — E199 verified |
| 18.3.5 | Current bid / ask for a symbol | C→MT5 (MCP) `get_chart_ticks_history` / `market_book_get` | `symbol` (18.2.7/18.2.9 params) | Latest `bid`/`ask` from ticks or book levels | Read-only: naturally idempotent | `connector:read` | Tier 0 | Read | C — E010 (bid in ticks); book levels market-gated (E111) |
| 18.3.6 | Symbol trading sessions | — | — | Explicit session times are NOT in the symbol record (E101); only `swap_rollover3day` + trade modes are present | — | — | — | — | F — partially available; explicit session times not exposed by the MCP server |
| 18.3.7 | Spread information for a symbol | C→MT5 (MCP) `get_marketwatch_symbols` / `market_book_get` | `symbol` | `spread_float` flag (verified E101); numeric spread from bid/ask at market open | Read-only: naturally idempotent | `connector:read` | Tier 0 | Read | C — flag verified; numeric spread market-gated |

### 18.4 Account (full account info)

| # | Operation | Direction | Params | Response | Idempotency / expiry | Scope | Tier | R/W | Status |
|---|---|---|---|---|---|---|---|---|---|
| 18.4.1 | Full account info | C→MT5 (MCP) `get_trading_account_info` | (none) | `account` object + `terminal` context (E001: login, server, broker, name, type, currency, balance, equity, margin, margin_free, profit, credit, swaps, commissions, read_only, margin_mode) | Read-only: naturally idempotent; re-fetched on every `sync_request` / account batch | `connector:read` | Tier 0 | Read | C — E001 verified; authoritative account-identity message (§7.1) |
| 18.4.2 | Account balance | C→MT5 (MCP) `get_trading_account_info` | (none) | `account.balance` (E001: 26237.73 USD on the reference demo) | As 18.4.1 | `connector:read` | Tier 0 | Read | C — E001 verified |
| 18.4.3 | Account equity | C→MT5 (MCP) `get_trading_account_info` | (none) | `account.equity` | As 18.4.1 | `connector:read` | Tier 0 | Read | C — E001 verified |
| 18.4.4 | Used margin | C→MT5 (MCP) `get_trading_account_info` | (none) | `account.margin` | As 18.4.1 | `connector:read` | Tier 0 | Read | C — E001 verified |
| 18.4.5 | Free margin | C→MT5 (MCP) `get_trading_account_info` | (none) | `account.margin_free` | As 18.4.1 | `connector:read` | Tier 0 | Read | C — E001 verified |
| 18.4.6 | Margin level | S-side (derived) | — | `equity / margin × 100` from 18.4.3/18.4.4; undefined when `margin == 0` (report as null, never divide by zero) | Computed server-side, no Connector call | `connector:read` | — | Read | E — derived; fields verified E001 |
| 18.4.7 | Account currency | C→MT5 (MCP) `get_trading_account_info` | (none) | `account.currency` (ISO 4217, E001: USD) | As 18.4.1 | `connector:read` | Tier 0 | Read | C — E001 verified |
| 18.4.8 | Account leverage | S-side (derived) | — | NOT a field of `get_trading_account_info` or `terminal_get_info` (verified absent, E001/E006). Derivable: `trade_order_calc_margin` → `leverage = contract_size × price ÷ margin` (E103: 100000×1.10033÷110.03 ≈ 1000:1). MQL5 itself exposes it as `AccountInfoInteger(ACCOUNT_LEVERAGE)` (MQL5 Reference, Account Properties — https://www.mql5.com/en/docs/constants/environment_state/accountinformation), but the MT5 MCP server does not surface the field, so the Platform MUST use the derived value and label it derived. | Computed server-side | `connector:read` | — | Read | E — derived; direct field NOT available in the MCP surface |
| 18.4.9 | Account trading permissions | C→MT5 (MCP) `get_trading_account_info` + `terminal_get_info` | (none) | `read_only`, `type`, `mcp_trade_allowed`, `experts_trade_allowed` (E001/E006) | As 18.4.1 | `connector:read` | Tier 0 | Read | C — E001/E006 verified |
| 18.4.10 | Account connection status | C→MT5 (MCP) `terminal_get_info` | (none) | `terminal.connection.state` (E006: `synchronized`), build/company/program_name | As 18.4.1 | `connector:read` | Tier 0 | Read | C — E006 verified |

### 18.5 Positions

| # | Operation | Direction | Params | Response | Idempotency / expiry | Scope | Tier | R/W | Status |
|---|---|---|---|---|---|---|---|---|---|
| 18.5.1 | List open positions (+ pending orders) | C→MT5 (MCP) `get_trading_open_positions` | `include_orders: true` | `positions[]` + `orders[]` (E002: 3 live positions; `orders[]` empty verified — an empty list is a verified answer, not missing data) | Read-only: naturally idempotent; sync batches dedup by ticket (§3.3) | `connector:read` / `connector:sync` | Tier 0 | Read | C — E002 verified |
| 18.5.2 | Position details by ticket | S-side / C-side (derived filter) | `ticket` | Single position filtered from 18.5.1 by `position_id` — there is NO by-ticket MT5 MCP tool (verified, `GAP_ANALYSIS.md` §2) | Derived; no extra MT5 call | `connector:read` | — | Read | E — derived from 18.5.1 |
| 18.5.3 | Unrealized P/L per position | C→MT5 (MCP) `get_trading_open_positions` | (none) | Per-position `profit` field (E002) | As 18.5.1 | `connector:read` | Tier 0 | Read | C — E002 verified |
| 18.5.4 | Detect manually opened / closed / modified positions | S-side (sync diff) | — | Diff of 18.5.1 / 18.7.2 snapshots: new position, disappeared position, SL/TP change (CAPABILITY_MATRIX #42–44) | Sync-engine dedup + checkpoint (§3.3) | `connector:sync` | — | Read | E — mock-verified (sync engine); live-poll E2E pending |

### 18.6 Orders (pending)

| # | Operation | Direction | Params | Response | Idempotency / expiry | Scope | Tier | R/W | Status |
|---|---|---|---|---|---|---|---|---|---|
| 18.6.1 | List pending orders | C→MT5 (MCP) `get_trading_open_positions` | `include_orders: true` | `orders[]` (pending orders live in the same response as positions, E002) | As 18.5.1 | `connector:read` | Tier 0 | Read | C — E002 verified |
| 18.6.2 | Order details by ticket | S-side / C-side (derived filter) | `ticket` | Single order filtered from 18.6.1 / 18.7.1 by `order_id` — no by-ticket tool (verified, `GAP_ANALYSIS.md` §2) | Derived; no extra MT5 call | `connector:read` | — | Read | E — derived |
| 18.6.3 | Pending-order execution status | C→MT5 (MCP) `get_trading_history_orders` | Date range (18.7.1 params) | `state` field per order (`filled` / `placed`, verified E003) | As 18.7.1 | `connector:read` | Tier 0 | Read | C — E003 verified |

### 18.7 History & deals

| # | Operation | Direction | Params | Response | Idempotency / expiry | Scope | Tier | R/W | Status |
|---|---|---|---|---|---|---|---|---|---|
| 18.7.1 | History orders + deals for a date range | C→MT5 (MCP) `get_trading_history_orders` | `datetime_from`, `datetime_to`, `include_orders`, `include_orders_canceled`, `include_deals` | `orders[]` + `deals[]` with `order_id`/`position_id` links (E003: 7-day range, 3 orders + 3 deals); inverted range is an error (E906) | Read-only; server dedups on `(account_id, order_id)` / `(account_id, deal_id)` (§11.1) | `connector:read` / `connector:sync` | Tier 0 | Read | C — E003 verified |
| 18.7.2 | Historical (closed) positions | C→MT5 (MCP) `get_trading_history_positions` | Date range (same shape as 18.7.1) | `positions[]` (closed positions — the realized-P/L source; E004: empty on a fresh demo, shape verified) | As 18.7.1 | `connector:read` / `connector:sync` | Tier 0 | Read | C — E004 verified |
| 18.7.3 | Deals / executions list | C→MT5 (MCP) `get_trading_history_orders` | As 18.7.1 with `include_deals: true` | `deals[]` (entry/exit, `price_open`, `open_time`/`done_time`, `action`, volume, direction) | As 18.7.1 | `connector:read` | Tier 0 | Read | C — E003 verified |
| 18.7.4 | Realized P/L | S-side (derived) | Date range | Sum of `profit` over out-deals from 18.7.1/18.7.3 (fields verified E003) | Computed server-side | `connector:read` | — | Read | E — derived |
| 18.7.5 | Per-deal commission / swap / fee | — | — | Verified ABSENT from deal records on the reference broker (E003, MetaQuotes demo); only account-level `commissions`/`swaps` aggregates exist (`GAP_ANALYSIS.md` §2). Broker-dependent — the Platform MUST store NULL and surface "not reported by broker", never invent zeros (§3.3) | — | — | — | — | F — not available per-deal in the MCP surface |
| 18.7.6 | SL/TP on historical trades | — | — | Verified ABSENT from order/deal records (E003); SL/TP exist only on open positions (E002) | — | — | — | — | F — not available in the MCP surface |

### 18.8 Trade execution

All rows in this table are executing Tier-3 commands sent as
`CommandEnvelope` (§6.2) with `command_id`/`idempotency_key` uuidv7
and `expires_at` (default 5 min, max 15 min). Exact MCP input
schemas for the six Tier-3 actions were NOT captured in
e2e-evidence (only names + tier verified) — params below are the
MQL5-level fields (`MqlTradeRequest`: `symbol`, `volume`, `price`,
`sl`, `tp`, `type`, `deviation`, `type_filling`, `type_time`,
`expiration`, `stoplimit`, `magic`, `comment`; MQL5 Reference,
Trade Request Structure — https://www.mql5.com/en/docs/constants/structures/mqltraderequest)
and MUST be confirmed against the live `tools/list` input_schema
before any allowlist extension lands (§18.16).

| # | Operation (`operation.type`) | Direction | Params | Response | Idempotency / expiry | Scope | Tier | R/W | Status |
|---|---|---|---|---|---|---|---|---|---|
| 18.8.1 | Place buy, market (`open_order`) | S→C WS `command` → C→MT5 `trade_send_market_order` | `symbol`, `volume`, `sl`?, `tp`?, `deviation`?, `type: ORDER_TYPE_BUY` | `action.result` `executed` + deal/position ticket (read-back via 18.5.1), or `rejected`/`mcp_failed` with `ERROR_CATALOG.md` code | `command_id` idempotency; never blind-retry | `connector:trade` + `auto_trade` + signal-execution (§6.4) | Tier 3 / `trading.execute` | Executing | B — OPEN DECISION (§17.5): defined, NOT sendable until the user approves + allowlist change + demo proof + `schema_version` bump |
| 18.8.2 | Place sell, market (`open_order`) | As 18.8.1 | As 18.8.1 with `type: ORDER_TYPE_SELL` | As 18.8.1 | As 18.8.1 | As 18.8.1 | Tier 3 | Executing | B — OPEN DECISION (§17.5), same close-out as 18.8.1 |
| 18.8.3 | Buy limit (`open_order`) | S→C WS `command` → C→MT5 `trade_send_pending_order` | `symbol`, `volume`, `price` (below Ask), `sl`?, `tp`?, `type_time`?, `expiration`?, `type: ORDER_TYPE_BUY_LIMIT` | `action.result` + pending-order ticket | `command_id` idempotency; never blind-retry | As 18.8.1 | Tier 3 | Executing | B — OPEN DECISION (§17.5); order-type enum verified against MQL5 `ENUM_ORDER_TYPE` (https://www.mql5.com/en/docs/constants/tradingconstants/orderproperties); live-pending (#46) |
| 18.8.4 | Sell limit (`open_order`) | As 18.8.3 | As 18.8.3 with `price` above Bid, `type: ORDER_TYPE_SELL_LIMIT` | As 18.8.3 | As 18.8.1 | As 18.8.1 | Tier 3 | Executing | B — OPEN DECISION (§17.5); live-pending (#47) |
| 18.8.5 | Buy stop (`open_order`) | As 18.8.3 | As 18.8.3 with `price` above Ask, `type: ORDER_TYPE_BUY_STOP` | As 18.8.3 | As 18.8.1 | As 18.8.1 | Tier 3 | Executing | B — OPEN DECISION (§17.5); live-pending (#48) |
| 18.8.6 | Sell stop (`open_order`) | As 18.8.3 | As 18.8.3 with `price` below Bid, `type: ORDER_TYPE_SELL_STOP` | As 18.8.3 | As 18.8.1 | As 18.8.1 | Tier 3 | Executing | B — OPEN DECISION (§17.5); live-pending (#49) |
| 18.8.7 | Buy stop-limit (`open_order`) | As 18.8.3 | As 18.8.3 + `stoplimit` (limit price placed when `price` is reached), `type: ORDER_TYPE_BUY_STOP_LIMIT` | As 18.8.3 | As 18.8.1 | As 18.8.1 | Tier 3 | Executing | B — OPEN DECISION (§17.5); supported per `trade_order_mode` bitmask 127 (E101); schema/enum verification pending (#50) |
| 18.8.8 | Sell stop-limit (`open_order`) | As 18.8.3 | As 18.8.7 with `type: ORDER_TYPE_SELL_STOP_LIMIT` | As 18.8.3 | As 18.8.1 | As 18.8.1 | Tier 3 | Executing | B — OPEN DECISION (§17.5); same as 18.8.7 (#51) |
| 18.8.9 | Close position, full (`close_position`) | S→C WS `command` → C→MT5 `trade_close_single_position` | `ticket` (position ticket, string) | `action.result` `executed` + deal record; position gone on read-back (18.5.1) | `command_id` idempotency; never blind-retry | `connector:trade` + `auto_trade` + `position_manager` (§6.4) | Tier 3 / `trading.execute` | Executing | A — Connector-allowlisted now (`CommandOperation::ClosePosition`, `security/commands.rs`) |
| 18.8.10 | PARTIAL close (`close_position` with volume) | S→C WS `command` → C→MT5 `trade_close_single_position` | `ticket`, `volume` (portion to close; MUST be ≥ `volume_min`, a multiple of `volume_step`, and leave a remainder ≥ `volume_min` — else the broker rejects it) | `action.result` + reduced position volume on read-back | `command_id` idempotency; never blind-retry; a timeout MUST trigger read-back reconciliation, never a blind re-close | As 18.8.9 | Tier 3 | Executing | B — MQL5 supports partial close as `CTrade::PositionClosePartial(ticket, volume, deviation)` (MQL5 Reference — https://www.mql5.com/en/docs/standardlibrary/tradeclasses/ctrade/ctradepositionclosepartial): hedging accounting ONLY, and a volume exceeding the position volume closes it IN FULL. MCP `volume`-parameter semantics for `trade_close_single_position` are live-pending (#30) and MUST be proven before sendable. In netting accounts a partial close is an opposing deal, not this method — the server MUST check the account's margin mode first. |
| 18.8.11 | Close all positions (`close_all`, composite) | S→C WS, one `close_position` command per position | Server lists positions (18.5.1), then issues one 18.8.9/18.8.10 command per ticket | One `action.result` per command; the composite succeeds only when every ticket reaches a terminal state | Each command has its OWN `command_id` — there is NO single "close-all" idempotency key and NO single MT5 MCP action for this; a retry re-issues only the commands that never reached a terminal state | As 18.8.9 | Tier 3 | Executing | B — composite over 18.8.9/18.8.10; no single MT5 action exists, so the Platform orchestrates it and MUST report per-ticket outcomes, never a single blanket "closed all" |
| 18.8.12 | Close by opposite position (`close_by_position`) | S→C WS `command` → C→MT5 `trade_close_by_position` | `ticket` (position to close) + `close_by_ticket` (opposite position on the same symbol) | `action.result`; the smaller position closes fully and the larger is reduced by the smaller's volume (MQL5 `TRADE_ACTION_CLOSE_BY`) | `command_id` idempotency; never blind-retry | As 18.8.9 | Tier 3 | Executing | B — hedging accounts only; allowlist extension + demo proof pending. The command wire shape for the second ticket (`close_by_ticket`) is defined here; the Connector adaptation MUST pin the exact field name against the MCP input_schema. |

### 18.9 Order modification & risk / TP-SL

| # | Operation (`operation.type`) | Direction | Params | Response | Idempotency / expiry | Scope | Tier | R/W | Status |
|---|---|---|---|---|---|---|---|---|---|
| 18.9.1 | Change SL / TP on a position (`modify_sl_tp`) | S→C WS `command` → C→MT5 `trade_modify_sl_tp` | `ticket` (position), `sl` (number\|null), `tp` (number\|null); absent/null = leave unchanged | `action.result` + position read-back showing new SL/TP | `command_id` idempotency; never blind-retry | `connector:trade` + `auto_trade` + `position_manager` (§6.4) | Tier 3 / `trading.execute` | Executing | A — Connector-allowlisted now (`CommandOperation::ModifySlTp`) |
| 18.9.2 | Modify pending order SL / TP (`modify_pending`) | S→C WS `command` → C→MT5 `trade_modify_sl_tp` (order ticket) | `ticket` (pending-order ticket), `sl`?, `tp`? — SL/TP ONLY | `action.result` + order read-back | `command_id` idempotency; capability rule §17.4 (never send unless `connector_version` ≥ published minimum AND manifest contains `trade_modify_sl_tp`) | As 18.9.1 | Tier 3 | Executing | B — in scope with the §17.4 capability rule; close-out (allowlist + live proof that an order ticket is accepted) pending |
| 18.9.3 | Modify pending order price (`modify_pending`) | S→C WS `command` → C→MT5 action TBD | `ticket`, `price` (new trigger price), `sl`?, `tp`? (MQL5: re-send existing SL/TP so they are kept), `type_time`?, `expiration`? | `action.result` + order read-back | `command_id` idempotency; capability rule as 18.9.2 once an action is proven | As 18.9.1 | Tier 3 | Executing | B — MQL5 SUPPORTS this (`TRADE_ACTION_MODIFY`, required fields `action/order/price/type_time/expiration/type_filling`, optional `sl/tp`; MQL5 Reference — https://www.mql5.com/en/docs/constants/tradingconstants/enum_trade_request_actions and book "Modifying a pending order" — https://www.mql5.com/en/book/automation/experts/experts_modify_order) but NO MT5 MCP action covering it has been verified in the live manifest (#52 "possible gap"). MCP coverage is UNVERIFIED — see §18.16. NOT sendable until the schema check names the action. |
| 18.9.4 | Modify pending order expiration (`modify_pending`) | S→C WS `command` → C→MT5 action TBD | `ticket`, `price` (MQL5 requires `price` on every `TRADE_ACTION_MODIFY`), `type_time`, `expiration`, `sl`?, `tp`? | `action.result` + order read-back | As 18.9.3 | As 18.9.1 | Tier 3 | Executing | B — same MQL5 basis and same MCP-coverage gap as 18.9.3 (#56) |
| 18.9.5 | Modify pending order volume | — | — | — | — | — | — | — | F — NOT POSSIBLE. MQL5 Reference is explicit: a pending order's type and volume cannot be changed; the order must be deleted and replaced (book "Modifying a pending order", same URL as 18.9.3). The contract operation for this is therefore `cancel_order` (18.9.6) + `open_order` (18.8.3–18.8.8), never a modify. |
| 18.9.6 | Cancel order (`cancel_order`) | S→C WS `command` → C→MT5 `trade_delete_order` | `ticket` (pending-order ticket) | `action.result` + order gone on read-back (18.6.1) | `command_id` idempotency; never blind-retry | As 18.9.1 | Tier 3 | Executing | B — defined here; allowlist extension + demo proof pending (live-pending #57). NOT sendable in v1 until close-out. |
| 18.9.7 | Remove SL / remove TP | S→C WS `command` → C→MT5 `trade_modify_sl_tp` | `ticket`, `sl: null` (or 0 — semantics live-pending) / `tp: null` (or 0) | `action.result` + read-back showing SL/TP cleared | `command_id` idempotency | As 18.9.1 | Tier 3 | Executing | A — via the allowlisted `modify_sl_tp` op; null-vs-0 removal semantics are live-pending (#35/#36) and MUST be proven at market open — until then the server sends null only and treats a broker rejection as a failed removal, never as a cleared stop |

### 18.10 Risk checks & calculations (pre-flight and sizing)

| # | Operation | Direction | Params | Response | Idempotency / expiry | Scope | Tier | R/W | Status |
|---|---|---|---|---|---|---|---|---|---|
| 18.10.1 | Pre-flight check, market order | C→MT5 (MCP) `trade_check_market_order` | `symbol`, `type` (`buy`/`sell`, verified E013), `volume` | Validation result — NEVER places an order. Captured rejections: `TRADING_NOT_PERMITTED` (market closed, E105–E108), `VOLUME_INVALID` (E902/E106), `SYMBOL_INVALID` (E108) | Read-only: naturally idempotent; MUST be re-run immediately before any execution it gates (a stale check is not a pass) | `connector:read` | Tier 0 | Read | C — E013/E105–E108/E902 verified |
| 18.10.2 | Pre-flight check, pending order | C→MT5 (MCP) `trade_check_pending_order` | `symbol`, `type`, `volume`, `price` (per discovered input_schema — §18.16) | Validation result, as 18.10.1 | As 18.10.1 | `connector:read` | Tier 0 | Read | C — Tier 0 by `risk.rs` classification ("validation calculation only"); individual E2E record pending (no E-record for this action yet) |
| 18.10.3 | Required margin for an order | C→MT5 (MCP) `trade_order_calc_margin` | `symbol`, `type`, `volume`, `price` (per discovered input_schema; E011's probe input was incomplete and returned a tool error — the full param set is §18.16) | Margin amount (E103: 110.03 USD for 0.1 lot EURUSD at live price) | Read-only: naturally idempotent | `connector:read` | Tier 0 | Read | C — E103 verified (mechanics); full input schema §18.16 |
| 18.10.4 | Profit / loss for a price move | C→MT5 (MCP) `trade_order_calc_profit` | `symbol`, `type`, `volume`, `price_open`, `price_close` (per discovered input_schema; E012's probe input was incomplete — §18.16) | Profit amount (E104: +10 pips × 0.1 lot EURUSD = +10.00 USD; 1 USD per pip) | Read-only: naturally idempotent | `connector:read` | Tier 0 | Read | C — E104 verified (mechanics); full input schema §18.16 |
| 18.10.5 | Position sizing (risk math) | S-side (Platform formula) | `equity`, `risk_pct`, `sl_distance`, `pip_value` | `volume = equity × risk% ÷ (SL distance × pip value)`; inputs verified (E001/E101/E104) | Computed server-side; re-derived per decision, never cached as an order | — (server) | — | Read | E — derived; Platform-side only, never in the Connector |
| 18.10.6 | Aggregate open exposure | S-side (derived) | — | Sum of open-position `volume × contract_size` (E002/E101) | Computed server-side | — (server) | — | Read | E — derived |
| 18.10.7 | Margin-level floor check (Risk Engine) | S-side (Risk Engine) | Effective config (§17.2.3) + proposed trade | Pass/fail against `margin_level_floor_pct` (default 150) and `max_simultaneous_positions`; recorded in the decision's `risk_validation` (§17.3) | Per decision; a failed check MUST NOT produce a command | — (server) | — | Read | E — server-side; the Risk Engine is the authority, the Connector never checks this |

### 18.11 Sync & account events

| # | Operation | Direction | Params | Response | Idempotency / expiry | Scope | Tier | R/W | Status |
|---|---|---|---|---|---|---|---|---|---|
| 18.11.1 | Sync batch, account | C→S REST `POST /api/v1/connector/sync/batch` (§3.3) | `batch_id` uuidv7, `kind: "account"`, `items` = `AccountSnapshot`, `count`, `produced_at` | `SyncBatchAck { accepted, batch_id }` | `batch_id` dedup on `(account_id, batch_id)`; re-delivery acks without re-ingest | `connector:sync` | — | Read (up) | C — as implemented in `gateway.rs:send_batch()` |
| 18.11.2 | Sync batch, positions | C→S REST (§3.3) | `batch_id`, `kind: "positions"`, `items` = open positions + pending orders | `SyncBatchAck` | As 18.11.1 | `connector:sync` | — | Read (up) | C — as implemented |
| 18.11.3 | Sync batch, history orders | C→S REST (§3.3) | `batch_id`, `kind: "history_orders"`, `items` = orders + deals | `SyncBatchAck` | As 18.11.1; ticket-less records dropped client-side with a `data_quality` warning | `connector:sync` | — | Read (up) | C — as implemented |
| 18.11.4 | Sync batch, history positions | C→S REST (§3.3) | `batch_id`, `kind: "history_positions"`, `items` = closed positions | `SyncBatchAck` | As 18.11.1 | `connector:sync` | — | Read (up) | C — as implemented |
| 18.11.5 | `sync_request` (server asks for refresh) | S→C, WS frame (§5.2) | `{ "type": "sync_request" }` | Connector re-sends an `account` batch then `positions` | Idempotent — a duplicate request just re-sends the same snapshots | — | — | Read | C — as implemented in `ws.rs` |
| 18.11.6 | Account switch | C→S, via 18.11.1 with a new login | New `account`-kind batch whose `login` differs from the bound account | Server: audit + invalidate old-account pending commands + re-bind + fresh sync (§7.3) | Binding change, not data migration; old account's history stays under the old account | `connector:sync` | — | Read (up) | C — as implemented; server transaction per §7.3 |

### 18.12 Terminal, diagnostics & lifecycle

| # | Operation | Direction | Params | Response | Idempotency / expiry | Scope | Tier | R/W | Status |
|---|---|---|---|---|---|---|---|---|---|
| 18.12.1 | Terminal info | C→MT5 (MCP) `terminal_get_info` | (none) | `terminal` object: build, company, program_name, connection state, trade flags | Read-only: naturally idempotent | `connector:read` | Tier 0 | Read | C — E006 verified |
| 18.12.2 | Terminal journal | C→MT5 (MCP) `get_terminal_journal` | Date range / `limit` (per discovered input_schema — §18.16) | Journal entries (terminal log lines) | Read-only: naturally idempotent | `connector:read` | Tier 0 | Read | C — Tier 0 by `get_` prefix in `risk.rs`; individual E2E record pending |
| 18.12.3 | Discover all MCP tools | C→MT5 (MCP) `tools/list` | (none) | Full action list + descriptions + input/output schemas (107 actions on the reference terminal) | Read-only; re-run on every MT5 MCP connect; count validated (`more_than_expected` / `fewer_than_expected` reported, never hidden) | — (local) | — | Read | C — live-verified, 3 probes (`GAP_ANALYSIS.md` §1) |
| 18.12.4 | Post action manifest | C→S REST `POST /api/v1/connector/manifest` (§4.1) | `device_id`, `manifest_hash`, `terminal_build`, `mcp_server_version`, `action_count`, `tiers`, `actions[]` | `{ accepted, manifest_hash }`; unchanged hash = no-op ack | Idempotent per `(user_id, device_id, manifest_hash)` | `connector:read` | — | Read (up) | B — [client-v1.1]: path defined, Connector adaptation pending |
| 18.12.5 | Economic calendar events | C→MT5 (MCP) `economic_calendar_list_events_by_country` | Country / date range (per discovered input_schema — §18.16) | Calendar events | Read-only: naturally idempotent | `connector:read` | Tier 0 | Read | C — Tier 0 by `economic_calendar_` prefix in `risk.rs`; individual E2E record pending |
| 18.12.6 | News list | C→MT5 (MCP) `get_news_list` | Date range / `limit` (per discovered input_schema — §18.16) | News items (terminal news feed — NOT the Platform's news section; the Connector MUST NOT send these to the Platform as news content, §1.4) | Read-only: naturally idempotent | `connector:read` | Tier 0 | Read | C — Tier 0 by `get_` prefix; individual E2E record pending |

### 18.13 Local-only terminal operations (Tier 1/2 — server MUST NOT invoke)

These actions exist in the live 107-action manifest (classified in
`risk.rs`, named in `CAPABILITY_MATRIX.md` #136 as "67 non-trading
actions; read-only sweep next") but are LOCAL terminal operations.
The server MUST NOT issue them, the Connector MUST NOT expose them
to the server, and no command envelope exists for them. They are
listed — one row each — so the inventory is complete and so a future
scoping decision has the names in front of it. Params/response for
each are per the discovered `tools/list` input_schema (NOT
individually E2E-verified — §18.16); idempotency is "n/a — local
only, never server-issued"; scope is "— (local permission only)".

**Charts (Tier 1 unless noted):**

| # | Operation (MT5 MCP action) | Tier | R/W | Status |
|---|---|---|---|---|
| 18.13.1 | `chart_open` | Tier 1 / `local.modify` | Executing (local) | D — local-only |
| 18.13.2 | `chart_close` | Tier 1 | Executing (local) | D — local-only |
| 18.13.3 | `chart_navigate` | Tier 1 | Executing (local) | D — local-only |
| 18.13.4 | `chart_set_properties` | Tier 1 | Executing (local) | D — local-only |
| 18.13.5 | `chart_set_symbol_period` | Tier 1 | Executing (local) | D — local-only |
| 18.13.6 | `chart_add_indicator` | Tier 1 | Executing (local) | D — local-only (note: indicators on the USER's chart are display only; Platform indicator computation is server-side, §17.3) |
| 18.13.7 | `chart_remove_indicator` | Tier 1 | Executing (local) | D — local-only |
| 18.13.8 | `chart_object_create` | Tier 1 | Executing (local) | D — local-only |
| 18.13.9 | `chart_object_update` | Tier 1 | Executing (local) | D — local-only |
| 18.13.10 | `chart_object_delete` | Tier 1 | Executing (local) | D — local-only |
| 18.13.11 | `chart_save_template` | Tier 1 | Executing (local) | D — local-only |
| 18.13.12 | `chart_delete_template` | Tier 1 | Executing (local) | D — local-only |
| 18.13.13 | `chart_remove_expert` | Tier 1 | Executing (local) | D — local-only |
| 18.13.14 | `chart_add_expert` | Tier 2 / `advanced.execute` | Executing (local) | D — local-only; attaches an EA, can initiate trading — the Connector MUST NEVER expose this to the server |
| 18.13.15 | `chart_add_script` | Tier 2 | Executing (local) | D — local-only; runs a script on a chart |
| 18.13.16 | `chart_apply_template` | Tier 2 | Executing (local) | D — local-only; a template may embed an EA |

(Chart read-back has no separate action: chart state is read via 18.2.6/18.2.7.)

**Alerts (Tier 1):**

| # | Operation (MT5 MCP action) | Tier | R/W | Status |
|---|---|---|---|---|
| 18.13.18 | `alerts_create` | Tier 1 / `local.modify` | Executing (local) | D — local-only |
| 18.13.19 | `alerts_update` | Tier 1 | Executing (local) | D — local-only |
| 18.13.20 | `alerts_delete` | Tier 1 | Executing (local) | D — local-only |
| 18.13.21 | `alerts_set_enabled` | Tier 1 | Executing (local) | D — local-only |

**Profiles (Tier 1):**

| # | Operation (MT5 MCP action) | Tier | R/W | Status |
|---|---|---|---|---|
| 18.13.22 | `profile_add` | Tier 1 / `local.modify` | Executing (local) | D — local-only |
| 18.13.23 | `profile_apply` | Tier 1 | Executing (local) | D — local-only |
| 18.13.24 | `profile_delete` | Tier 1 | Executing (local) | D — local-only |

**Strategy tester (Tier 2, except stop/close):**

| # | Operation (MT5 MCP action) | Tier | R/W | Status |
|---|---|---|---|---|
| 18.13.25 | `tester_prepare_config` | Tier 2 / `advanced.execute` | Executing (local) | D — local-only; writes tester config files |
| 18.13.26 | `tester_prepare_inputs` | Tier 2 | Executing (local) | D — local-only; writes tester input files |
| 18.13.27 | `tester_run_backtest` | Tier 2 | Executing (local) | D — local-only; executes MQL programs |
| 18.13.28 | `tester_run_optimization` | Tier 2 | Executing (local) | D — local-only; executes MQL programs |
| 18.13.29 | `tester_stop` | Tier 1 / `local.modify` | Executing (local) | D — local-only |
| 18.13.30 | `tester_close_result_window` | Tier 1 | Executing (local) | D — local-only |

**Files (Tier 2, except reads):**

| # | Operation (MT5 MCP action) | Tier | R/W | Status |
|---|---|---|---|---|
| 18.13.31 | `list_directory` | Tier 0 | Read | D — local-only (read-only but terminal-filesystem scope; the server MUST NOT browse the user's terminal files) |
| 18.13.32 | `read_file` | Tier 0 | Read | D — local-only, same reason as 18.13.31 |
| 18.13.33 | `search_text` | Tier 0 | Read | D — local-only, same reason as 18.13.31 |
| 18.13.34 | `write_file` | Tier 2 / `advanced.execute` | Executing (local) | D — local-only |
| 18.13.35 | `write_binary_file` | Tier 2 | Executing (local) | D — local-only |
| 18.13.36 | `create_new_file` | Tier 2 | Executing (local) | D — local-only |
| 18.13.37 | `create_new_folder` | Tier 2 | Executing (local) | D — local-only |
| 18.13.38 | `delete_file` | Tier 2 | Executing (local) | D — local-only |
| 18.13.39 | `delete_folder` | Tier 2 | Executing (local) | D — local-only |
| 18.13.40 | `replace_text_in_file` | Tier 2 | Executing (local) | D — local-only |

**Services & network (Tier 2):**

| # | Operation (MT5 MCP action) | Tier | R/W | Status |
|---|---|---|---|---|
| 18.13.41 | `service_register` | Tier 2 / `advanced.execute` | Executing (local) | D — local-only |
| 18.13.42 | `service_start` | Tier 2 | Executing (local) | D — local-only |
| 18.13.43 | `service_stop` | Tier 2 | Executing (local) | D — local-only |
| 18.13.44 | `service_unregister` | Tier 2 | Executing (local) | D — local-only |
| 18.13.45 | `send_web_request` | Tier 2 | Executing (local) | D — local-only; arbitrary outbound HTTP from the terminal — the Connector MUST NEVER expose this to the server |

### 18.14 Gaps — operations with no operation (explicit, not hidden)

| # | Wanted operation | Why it has no row above | What to do instead |
|---|---|---|---|
| 18.14.1 | Modify pending order volume | MQL5 forbids it (18.9.5) | `cancel_order` + `open_order` (delete + replace) |
| 18.14.2 | Per-deal commission / swap / fee | No MCP field (18.7.5) | Account-level aggregates; store NULL per deal |
| 18.14.3 | SL/TP on historical trades | No MCP field (18.7.6) | SL/TP known only while the position is open |
| 18.14.4 | Symbol trading-session times | No MCP field (18.3.6) | `swap_rollover3day` + trade modes only; session schedule is a Platform data problem, not a Connector operation |
| 18.14.5 | By-ticket position/order lookup | No MCP tool (18.5.2/18.6.2) | Filter from the list responses (derived) |
| 18.14.6 | "Close all" as one MT5 call | No MCP action (18.8.11) | Composite over per-ticket closes |
| 18.14.7 | `run_program` as an MT5 MCP action | NOT a verified action name. It appears only as an example of an arbitrary operation in the Connector's `unsupported_operations_never_execute` test (`security/commands.rs`) — it is a test fixture string, not a discovered tool. | Do NOT list it as an operation. If a future manifest contains it, it classifies Tier 2 (fail-closed) by default. |

### 18.15 Counts and status summary (normative — update on every status change)

| Category | Rows | A | B | C | D | E | F |
|---|---|---|---|---|---|---|---|
| 18.2 Market data & ticks | 11 | 0 | 5 | 6 | 0 | 0 | 0 |
| 18.3 Symbols | 7 | 0 | 0 | 6 | 0 | 0 | 1 |
| 18.4 Account | 10 | 0 | 0 | 8 | 0 | 2 | 0 |
| 18.5 Positions | 4 | 0 | 0 | 2 | 0 | 2 | 0 |
| 18.6 Orders | 3 | 0 | 0 | 2 | 0 | 1 | 0 |
| 18.7 History & deals | 6 | 0 | 0 | 3 | 0 | 1 | 2 |
| 18.8 Trade execution | 12 | 1 | 11 | 0 | 0 | 0 | 0 |
| 18.9 Order modification & risk/TP-SL | 7 | 2 | 4 | 0 | 0 | 0 | 1 |
| 18.10 Risk checks & calculations | 7 | 0 | 0 | 4 | 0 | 3 | 0 |
| 18.11 Sync & account events | 6 | 0 | 0 | 6 | 0 | 0 | 0 |
| 18.12 Terminal, diagnostics & lifecycle | 6 | 0 | 1 | 5 | 0 | 0 | 0 |
| 18.13 Local-only terminal operations | 44 | 0 | 0 | 0 | 44 | 0 | 0 |
| **TOTAL** | **123** | **3** | **21** | **42** | **44** | **9** | **4** |

- **Per-operation status summary:** **3** Connector-allowlisted now
  (18.8.9 close-full, 18.9.1 modify SL/TP, 18.9.7 remove SL/TP —
  all through the two existing `CommandOperation` variants);
  **21** contract-defined, pending close-out (5 chart frames,
  11 trade-execution ops including all 8 `open_order` variants,
  4 order-modification ops, 1 manifest POST); **42** read-only,
  Connector-executable now; **44** local-only (never server-issued);
  **9** derived / Platform-side; **4** gaps / not available.
- **What "done" means:** the Connector/contract side is DONE when
  every B row has either reached A (allowlist + proof + published
  minimum version) or been re-decided by the user with a recorded
  reason — and the Platform side is built on the inventory as it
  stands, never on a subset remembered from a summary.

### 18.16 What could NOT be verified (unverified — do not invent)

No live MT5 terminal exists on the contract author's VM; nothing
here claims live verification beyond what the Connector repo itself
records. The following were NOT verifiable from the sources and
MUST be recorded from the live `tools/list` input_schema (or a
market-open live probe) before the dependent close-out lands:

1. **Exact MCP input schemas for the six Tier-3 actions**
   (`trade_send_market_order`, `trade_send_pending_order`,
   `trade_close_single_position`, `trade_close_by_position`,
   `trade_modify_sl_tp`, `trade_delete_order`). Only names + tier
   are verified (risk.rs + live_mt5_tests.rs name list). Params in
   §18.8/§18.9 are MQL5-level fields from the official
   `MqlTradeRequest` reference — the MCP server's actual parameter
   names (e.g. whether `trade_close_single_position` takes
   `volume`, whether `trade_modify_sl_tp` accepts an order ticket,
   the exact field name for the second ticket in
   `trade_close_by_position`) are unverified.
2. **MCP coverage for pending-order price/expiration modification**
   (18.9.3/18.9.4). MQL5 supports it via `TRADE_ACTION_MODIFY`;
   whether any live MCP action exposes it is unverified — the
   schema check in `CAPABILITY_MATRIX.md` #52/#56 is still open.
3. **Full input schemas for the calc/check actions**
   (`trade_order_calc_margin`, `trade_order_calc_profit`,
   `trade_check_pending_order`): E011/E012 probe inputs were
   incomplete and returned tool errors, so the required parameter
   set is unverified beyond the E013 fragment
   (`trade_check_market_order`: `symbol`, `type: "buy"`, `volume`).
4. **Input schemas for the 44 local-only actions (§18.13)** and for
   `get_terminal_journal`, `economic_calendar_list_events_by_country`,
   `get_news_list`: names + tiers are verified via `risk.rs`; per-action
   E2E records do not exist yet (`CAPABILITY_MATRIX.md` #136:
   "read-only sweep next").
5. **The full 107-name manifest as a static list.** The manifest is
   built dynamically from `tools/list` (`manifest.rs`); no static
   list exists in the Connector repo. The 107 count and the
   41/26/34/6 tier histogram are verified; the inventory above
   names 71 distinct MCP actions (plus 5 chart protocol frames,
   composites, and derived operations), and any live action not
   named here is by definition Tier 2 fail-closed until
   classified — it MUST be added to this inventory when
   discovered, not silently used.
6. **Market-open content** for book levels, numeric spread,
   cross-symbol calc verification, and all trading-op E2E
   (#26–60 live-pending): the reference probes ran on a Sunday
   with the market closed. Nothing in §18 claims a market-open
   execution result.

### 18.17 Conformance note (for the implementers)

- **Platform agent:** implement against this inventory, not against
  a summary of it. Every B row you build toward MUST keep its
  status B until its close-out is proven; shipping a B operation
  as if it were A is a contract violation. Per-operation status
  lives in §18.15 — update the table (and the §14 deltas) when a
  status changes; do not edit a row's status silently.
- **Connector agent:** the allowlist extensions (one per B
  executing op, or grouped where the wire shape is shared, e.g.
  all `open_order` variants behind one `OpenOrder` variant) go in
  `security/commands.rs` with the §13.1 tests + market-open live
  proof + a published minimum `connector_version` per op. The
  chart handlers (§17.1) and the manifest POST (§4.1) are the
  other adaptation items.
- **Change control (§16):** adding an operation to this inventory
  is an additive change (no version bump); changing a row's wire
  shape, scope, or tier is a breaking change (bump
  `schema_version`, call it out in the PR).

