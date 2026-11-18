# API Protocol — CONTRACT v1 (§3, §5, §6, §17.1)

This document describes the Bridge ↔ NSE Platform protocol exactly
as implemented. Every shape is CONTRACT v1 (`NSE-CONNECTOR-CONTRACT.md`,
merged to Platform main via PR #102). Nothing here is invented; where
the contract is silent, the gap is named, not filled.

Base URLs (production defaults; dev/staging/prod via configuration
only — no hardcoded dev endpoints):

- REST: `https://bridge.nexusverse.space`
- WS: `wss://bridge.nexusverse.space/api/v1/connector/ws`

TLS verification is mandatory. `ws://` / `http://` are accepted only
for loopback test endpoints (`scripts/mock_server.py`,
`ws://127.0.0.1:9099`, REST `http://127.0.0.1:9098`) or an explicit
dev/test environment.

Auth (both channels): `Authorization: Bearer <api_key>` +
`X-NSE-Device-Id: <device_id>` on every REST call and in the WS
upgrade headers. The API key comes from the secret store (Phase 1);
it is never logged, never in `config.json`, never in an error message.

## 1. REST endpoints (§3)

All paths are under `/api/v1/connector/`. Request/response bodies are
JSON. Error envelope everywhere: `{error: {code, message}}` (§3.6).

### 1.1 `POST validate-key` (§3.1)

Request: `{device_id, connector_version}`.
Response: `{valid, user_id, subscription, entitlements}` where
`entitlements = {history_sync, ai_coach, position_manager, auto_trade,
max_devices, max_accounts}`. `user_id` is server-resolved — never
client-asserted.

On 401/403: stop unauthorized sync immediately, do NOT retry with the
same key, do NOT fall back to unauthenticated mode.

### 1.2 `POST register-device` (§3.2)

Request: `{device_id, connector_version}` (+ Bearer + device headers).
Response: `{registered, session_expires_at}`. Idempotent per
`(user_id, device_id)`. Called on first run AND after any 401 on
another endpoint — re-enrolment is the recovery path (the key is
re-prompted only if `register-device` itself returns
`key_invalid`/`key_revoked`). Beyond `max_devices` → 403; the Bridge
never evicts another device silently.

### 1.3 `POST sync/batch` (§3.3) — the ONLY sync channel

Flat wire shape: `{batch_id (uuidv7, idempotency key), kind, items,
count, produced_at}`. Kinds exactly: `account | positions |
history_orders | history_positions`. No per-batch `account_id` (the
server attributes by device binding, §7.2). Broker timestamps
verbatim + UTC `produced_at`; never re-localize.

- Checkpoint = max broker time seen, never backwards. It is
  persisted to `sync_checkpoint.json` in the bridge data dir on
  every advance and restored at startup (spec §16) — a restart
  resumes history from the last synced broker time instead of
  re-running a full backfill. A corrupt record (or one from another
  account) is ignored with a warning and falls back to epoch; an
  account switch resets the checkpoint to epoch and persists the
  reset (§7.3).
- Page size 500, 250 ms between pages.
- Pending queue bounded at 32; the oldest batch is evicted WITH a
  warning (never silently discarded).
- Ack `200 {accepted, batch_id}`; `accepted: false` carries an error.

### 1.4 `POST heartbeat` (§3.4)

Request: `{}`. Response: `{ok, server_time, pending_commands}`. If
`pending_commands > 0` and no WS is live, open/refresh the WS: the
REST heartbeat loop wakes the WS supervisor immediately
(`ServerChannel._request_ws_open`) instead of waiting out the
reconnect backoff (up to 30 s). Keep-alive when WS is down — NOT a
substitute for the WS heartbeat.

### 1.5 `POST session/refresh` + `POST session/revoke` (§3.4, [client-v1.1])

Fully specified in the contract; implemented now. Refresh `{}` →
`{refreshed, session_expires_at}`. Revoke `{}` → `{revoked: true}`
(device session only; the API key is NOT revoked).

### 1.6 `GET releases/latest` (§3.5)

Response: `{version, download_url, sha256, size_bytes,
min_connector_version, signature (base64 Ed25519 over the artifact
bytes, or null), released_at}`. This endpoint is the update
authority; GitHub Releases remains the artifact host behind it. The
updater verifies SHA-256 + signature BEFORE any install (see
docs/RELEASE.md — the release-signing public key is not yet
provisioned, so the updater can only check, never install, today).

### 1.7 `POST manifest` (§4.1, [client-v1.1])

Only fields truthfully mappable from the Bridge are sent:
`device_id`, `manifest_hash` (over the local registry),
`action_count`, action names. `terminal_build` is OMITTED — the
driver exposes no numeric terminal build, and the Bridge never
invents one. No tier assignments are ever claimed. Server answers
`{accepted, manifest_hash}`.

Delivery: the manifest is POSTed once on enrolment (after
`register-device` succeeds) and again only when its hash changes
(unchanged hash = no-op, tracked by `ManifestTracker`, which the
enrolment manager and the WS channel share). When the manifest
changes mid-session, the channel also sends a `manifest.update`
MessageEnvelope over the WS (same payload; the server treats it the
same as the REST post). While the WS is down, a changed manifest is
POSTed over REST from the heartbeat loop. A manifest failure never
un-enrols the device — it is logged and retried on the next change.

### 1.8 Request cancellation (spec §2)

In-flight REST requests are cancellable via `asyncio` task
cancellation: `RestClient.cancel_all()` cancels every in-flight
request (each raises `asyncio.CancelledError` promptly in the task
that awaited it — never a `ServerError`, never retried), and
`close()` cancels in-flight requests on shutdown instead of
awaiting their full timeout. A cancelled request's outcome is
unknown (it may have reached the server), so the caller decides
whether to retry — the client never silently retries it.

### 1.9 Error contract + retry policy (§3.6)

| HTTP | Code | Client behaviour |
|---|---|---|
| 401 | `key_invalid`, `key_revoked` | No retry, ever. Re-enrolment cannot fix these; a new key is required. |
| 403 | `subscription_expired`, `device_limit_exceeded`, `account_not_authorized`, `feature_not_enabled` | Mapped distinctly; no retry with the same key. |
| 429 | `rate_limited` | Back off (honours `Retry-After` when present); do not tight-loop. |
| 5xx | any code | Retryable with backoff. |
| 401/403 | unknown code | MALFORMED RESPONSE — never treated as success. |

Retry policy (`server/rest.py`, `RestClient`): max 3 retries, base
0.5 s doubling (`retry_base_s * 2**attempt`), `Retry-After`
preferred when the server sends one. A 401 on a non-auth endpoint
triggers ONE re-enrolment via `register-device` and then a single
retry; if `register-device` itself 401s, the error propagates.
Transport errors are retryable the same way. Error messages never
contain key material, secrets, SQL, or stack traces.

## 2. WebSocket (§5)

### 2.1 Session (§5.1, §5.4)

- URL: `wss://bridge.nexusverse.space/api/v1/connector/ws`.
- Auth in the upgrade headers (see above). **NO `hello` frame.**
- Heartbeat: client sends `{type: "heartbeat", device_id}` every
  15 s; the server replies `heartbeat`. Session dead after 45 s
  without a frame.
- Reconnect: fresh session, no resume payload; backoff 500 ms
  doubling → 30 s + jitter (0–100 ms), reset on success; clean
  `close()` on shutdown. The server re-sends only `pending` commands
  on reconnect — never `executed` / `rejected` / `expired`.
- Outbound bound 128 messages; results first, chart frames second;
  the oldest unsent chart frame is dropped with a warning when full.

### 2.2 Frames

Server → client (§5.2): `command` (CommandEnvelope §6.2),
`sync_request` (re-send `account` batch then `positions`; also the
account-switch trigger, §7.3), `ack`, `heartbeat`,
`chart.request` / `chart.subscribe` / `chart.unsubscribe` (§17.1).
Unknown frames are silently ignored, never fatal.

Client → server (§5.3): ONLY `heartbeat`, `action.result` (§6.5),
`chart.data`, `chart.tick` — plus the §4.1 `manifest.update`
envelope when the manifest changes mid-session. No `ping`, no
`account.snapshot`, no `command.ack`. Snapshots/events go over REST
`sync/batch` (§1.3).

## 3. Commands (§6)

### 3.1 Server allowlist (§6.1) — exactly three operations in v1

| Operation | Maps to (Python driver, NOT the MCP transport) | Notes |
|---|---|---|
| `close_position` | `trade_close_single_position` (close-by-ticket; idempotent ambiguous-close handling) | |
| `modify_sl_tp` | `trade_modify_sl_tp` (SL/TP by ticket; absent sl/tp filled from the live position) | |
| `modify_pending` | `trade_modify_sl_tp` on a pending order (SL/TP only) | **Capability-gated (§17.4): starts REJECTED.** Live proof at market open + a published minimum `connector_version` are still owed (docs/PLATFORM-GAPS.md §4). Until the gate is proven (`modify_pending_proven=True`), the op is rejected as `UnsupportedOperation`. |

`open_order` and `cancel_order` are explicitly NOT sendable in v1.
The full 91-command registry stays available **locally**; only the
server channel enforces this allowlist, at this boundary.

### 3.2 Envelope (§6.2)

```json
{
  "type": "command",
  "command_id": "<uuidv7, unique per command>",
  "account_id": "12345678901",
  "device_id": "<device uuid>",
  "operation": {"type": "close_position", "ticket": "9000001"},
  "issued_at": "2026-11-16T00:00:00Z",
  "expires_at": "2026-11-16T01:00:00Z",
  "idempotency_key": "<== command_id in v1>"
}
```

`operation` is a tagged object: `close_position` takes
`{type, ticket}`; `modify_sl_tp` / `modify_pending` take
`{type, ticket, sl?, tp?}` with `sl`/`tp` nullable (absent = leave
unchanged). `ticket` is a string (MT5 ticket ids exceed 32-bit ints).
Unknown fields are ignored. There is no per-command signature in v1
(§6.2, delta D-CMD-3): commands are authenticated by the
mutually-authenticated WSS session itself.

### 3.3 Validation order (§6.3) — exact

```
malformed → expired → wrong-device → wrong-account → allowlist
(→ modify_pending gate) → entitlement → replay
```

1. Envelope parses; `expires_at` is RFC 3339 → `Malformed`.
2. `expires_at` > now (UTC); default 5 min from `issued_at`, max
   15 min → `Expired`.
3. `device_id` == this device → `WrongDevice`.
4. `account_id` ∈ authorized accounts for this device (fail closed
   when no account is bound) → `WrongAccount`.
5. `operation.type` ∈ allowlist → `UnsupportedOperation`.
6. Feature enabled locally (`position_manager` AND `auto_trade`
   entitlements from `validate-key`) → `AutoTradeNotEnabled`.
7. `command_id` not in the bounded replay cache (10,000 ids) →
   `Replayed`. **Only a SUCCESSFUL validation consumes the replay id.**

Rejections return `action.result` with `status: "rejected"` + reason.
The Bridge never executes a rejected command and never self-retries one.

### 3.4 Lifecycle (clarification #2, exact states)

```
RECEIVED → VALIDATED → AUTHORIZED → DISPATCHED → EXECUTING →
    COMPLETED | FAILED | UNKNOWN | EXPIRED
```

`AUTHORIZED` = entitlement + server authorization confirmed locally.
`UNKNOWN` = result uncertain after timeout/disconnect — reconcile the
actual MT5 state before any retry; never blind-retry a non-idempotent
op. The server-side lifecycle (§6.6: `pending → delivered →
(executed | rejected | mcp_failed) | expired`) is the Platform's; the
Bridge maps its local states onto `action.result` truthfully.

### 3.5 Results (§6.5)

`action.result` MessageEnvelope: `protocol_version: "1.0"`,
`schema_version: "2026-11-17"`, `message_id` uuidv7,
`correlation_id` = command_id, status exactly
`rejected | executed | mcp_failed`, `started_at` / `completed_at` /
`duration_ms` measured client-side, raw MT5 payload preserved, `error`
object only on failure with a `retry_safe` flag (`False` for every
financial op in this phase — the server must re-authorize with a NEW
`command_id` rather than rely on a blind client retry). If the socket
is down, results persist in the bounded pending queue and are delivered
on reconnect — never dropped silently. Round-trip invariant: every
issued command reaches exactly one terminal state; a command never
executes twice.

`error.category` note: §6.5's `error` object carries a `category`
from the Connector's error catalog (`ERROR_CATALOG.md`) — that catalog
is Platform-side and NOT part of CONTRACT v1, so the categories used
here (`Validation`, `Execution`, `Prerequisite`, `Unknown`) are
bridge-local approximations, documented as such.

### 3.6 Execution safety (kept hardening)

Stops/freeze checks, filling-mode resolution, retcode map,
`TRADE_UNVERIFIED` on ambiguous results, close idempotency, never
blind-retry after an uncertain result — reconcile MT5 state first.
On `TRADE_UNVERIFIED` the executor reconciles (`_reconcile`): if the
op demonstrably landed, the result is `executed` (recovered, with
`ambiguous_fill_recovered`); otherwise the lifecycle is `UNKNOWN`
and the result is `mcp_failed` with `retry_safe: false`.

## 4. Chart (§17.1)

- One-shot `chart.request`: timeframes M1..MN1, count ≤ 1000, 30 s
  timeout, retry once with a NEW `request_id`.
- Subscriptions: max 20/device, poll 5–3600 s (default 15), deltas
  only; burst 60 chart frames/min.
- Error codes: `symbol_not_found`, `symbol_not_in_market_watch`,
  `unsupported_timeframe`, `market_closed`, `chart_timeout`,
  `rate_limited`, `subscription_limit`, `manifest_missing_action`.
- Raw candles/ticks/specs only — no indicator math in the client.
  Market-Watch prerequisite chain applies (add symbol first, cleanup
  optional).

## 5. Accounts (§7)

A device is bound to exactly ONE active MT5 account; identity =
login + broker server + broker identity + currency + terminal
identity. On switch: stop tagging old-account events, notify the
cloud, invalidate pending old-account commands, fresh sync, never mix
records, never execute an old account's command on the new account.
Demo-only in v1.

## 6. What is NOT in the protocol

- **No diagnostics/events upload endpoint** (CONTRACT v1 §4.2) — see
  docs/ARCHITECTURE.md §5 and docs/PLATFORM-GAPS.md §2. The Bridge
  implements the local pipeline + an inert adapter; it invents no URL.
- **No task wire format** (future server-assigned compute) — the
  local boundary exists (`nse_bridge/tasks/`, kind registry EMPTY),
  is NOT wired to the WS command path, and needs a future contract
  amendment (docs/PLATFORM-GAPS.md §5).
- **No backtest** in this phase — intentionally excluded.
