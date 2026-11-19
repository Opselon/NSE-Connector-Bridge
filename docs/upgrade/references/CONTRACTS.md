# Inter-Engine Contracts — Agent Trade Engine (Phase 2)

Companion to `DESIGN.md` (v2, 2026-11-18). DESIGN.md states the
architecture; **this file states the wire formats**. A message that does
not validate against its schema here is rejected with a named reason —
schemas are contracts, not documentation.

Status: **DESIGN ONLY.** No code, no PR, no merge, no deploy.

## 1. Rules of the bus

1. **Transport.** Server-side: Postgres queue + `FOR UPDATE SKIP
   LOCKED` claim (the existing `trading_analytics/pipeline.rs`
   pattern). Server↔Bridge: WS frames (contract §17.1). No direct
   HTTP calls between engines.
2. **One event, one meaning.** An event states a fact that happened
   (`position.closed`) or a decision that was made
   (`risk.assessed`). It never carries an instruction to another
   engine — instructions are `ManagementAction` / commands, which
   travel through the decision record + EXE, not through the bus.
3. **Payloads are versioned.** Every payload carries
   `payload_version` (int, starts at 1). A consumer that cannot parse
   a payload version marks the event `failed` with reason
   `unsupported_payload_version` — it never guesses.
4. **No cross-engine table reads.** Engines communicate through
   events + the decision record + the shared read models
   (`factor_runs`, `managed_positions`,
   `strategy_param_snapshots`). Direct reads of another engine's
   tables are forbidden.
5. **Idempotency keys are mandatory** on every event that can move
   money (see §9). A redelivered event replays, never re-executes.

## 2. Event envelope

```json
{
  "event_id": "019a… (uuidv7)",
  "event_type": "prediction.scenarios_computed",
  "account_id": "uuid",
  "symbol": "EURUSD",
  "ticket": "152913954047",
  "payload_version": 1,
  "payload": { "...schema per §3..." },
  "emitted_by": "PRE",
  "emitted_at": "2026-11-18T18:00:00Z",
  "correlation_id": "decision uuid | position ticket | null",
  "idempotency_key": "evt:PRE:scenario:<inputs_hash>"
}
```

- `symbol` / `ticket` are nullable; `correlation_id` ties the event
  to the decision or position it belongs to.
- `idempotency_key` format: `evt:<engine>:<kind>:<stable-hash>`; the
  bus dedupes on it (UNIQUE).

## 3. Event catalog

| event_type | Producer | Consumers | Payload (§4) | Idempotent |
|---|---|---|---|---|
| `market.candles_ingested` | ingest | MAE | CandleBatch | yes (batch id) |
| `market.regime_changed` | MAE | STE, PRE, RCE | MarketRegime | yes |
| `strategy.selected` | STE | PRE, RCE, PME | StrategySelection | yes |
| `prediction.scenarios_computed` | PRE | RCE, PME, decision record | ScenarioSet | yes (inputs_hash) |
| `risk.assessed` | RCE | PME, EXE, decision record | RiskAssessment | yes (inputs_hash) |
| `position.opened` | ingest (sync) | PME, PRE, RCE | PositionState | yes (ticket) |
| `position.modified` | ingest (sync) / EXE | PME, PRE | PositionState | yes (ticket+version) |
| `position.closed` | ingest (sync) / EXE | PME, factors, behavior, coaching | PositionClosed | yes (ticket) |
| `position.adopted` | PME | PRE, RCE, decision record | PositionAdopted | yes (ticket) |
| `position.management_action` | PME | EXE, decision record | ManagementAction | yes (decision_id) |
| `news.impact` | NSE | MAE, STE, PME, RCE | NewsImpact | yes (item id) |
| `news.freeze_started` | NSE | STE, PME, EXE | FreezeWindow | yes |
| `news.freeze_ended` | NSE | STE, PME, EXE | FreezeWindow | yes |
| `behavior.flag` | behavior/FGE | FGE, STE, coaching | BehaviorFlag | yes |
| `feargreed.state_changed` | FGE | PME, RCE, coaching | FearGreedState | yes |
| `execution.command_enqueued` | EXE | decision record, PME | ExecutionResult | yes (command_id) |
| `execution.command_delivered` | EXE | decision record | ExecutionResult | yes |
| `execution.command_completed` | EXE | PME, factors, decision record | ExecutionResult | yes |
| `execution.command_failed` | EXE | PME, decision record | ExecutionResult | yes |
| `execution.reconciled` | EXE | PME, decision record | Reconciliation | yes |
| `execution.diverged` | EXE | admin, user, PME (halt on ticket) | Divergence | yes |
| `engine.decision_recorded` | any | dashboard log, audit | DecisionRecord | yes (decision_id) |
| `engine.kill_switch_tripped` | RCE/PME | all engines, EXE | KillSwitch | yes |

## 4. Message schemas (JSON Schema sketches)

All money is a JSON string decimal (`"1.08532"`), never a float.
All timestamps are RFC 3339 UTC. Enums are closed — an unknown enum
value fails validation.

### 4.1 MarketRegime (MAE → STE/PRE/RCE)

```json
{
  "type": "object",
  "required": ["symbol", "timeframe", "regime", "computed_at", "as_of"],
  "properties": {
    "symbol": { "type": "string" },
    "timeframe": { "enum": ["M1", "M5", "M15", "H1", "H4", "D1"] },
    "regime": { "enum": ["trend_up", "trend_down", "range", "chop", "unavailable"] },
    "trend_strength": { "type": "number", "minimum": 0, "maximum": 1 },
    "atr": { "type": "string" },
    "atr_percentile_90d": { "type": "number", "minimum": 0, "maximum": 100 },
    "spread_state": { "enum": ["normal", "wide", "unknown"] },
    "support": { "type": "array", "items": { "type": "string" } },
    "resistance": { "type": "array", "items": { "type": "string" } },
    "confidence": { "enum": ["low", "medium", "high"] },
    "unavailable_reason": { "type": ["string", "null"] },
    "as_of": { "type": "string", "format": "date-time" },
    "computed_at": { "type": "string", "format": "date-time" },
    "inputs_hash": { "type": "string" }
  }
}
```

### 4.2 StrategySelection (STE → PRE/RCE/PME)

```json
{
  "type": "object",
  "required": ["strategy_id", "strategy_version", "parameter_snapshot_id", "valid_from", "valid_until"],
  "properties": {
    "strategy_id": { "type": "string" },
    "strategy_version": { "type": "integer", "minimum": 1 },
    "parameter_snapshot_id": { "type": "string" },
    "allowed_symbols": { "type": "array", "items": { "type": "string" } },
    "allowed_sessions": { "type": "array", "items": { "enum": ["asian", "london", "new_york"] } },
    "max_simultaneous": { "type": "integer", "minimum": 0 },
    "selection_reason": { "type": "string" },
    "none_reason": { "type": ["string", "null"] },
    "valid_from": { "type": "string", "format": "date-time" },
    "valid_until": { "type": "string", "format": "date-time" }
  }
}
```

### 4.3 ScenarioSet (PRE → RCE/PME/decision record)

```json
{
  "type": "object",
  "required": ["subject", "scenarios", "expected_value_R", "recommendation", "computed_at"],
  "properties": {
    "subject": { "type": "object",
      "required": ["kind", "symbol", "direction"],
      "properties": {
        "kind": { "enum": ["candidate", "open_position"] },
        "ticket": { "type": ["string", "null"] },
        "symbol": { "type": "string" },
        "direction": { "enum": ["buy", "sell"] },
        "entry": { "type": "string" },
        "volume": { "type": "string" } } },
    "scenarios": { "type": "array", "minItems": 2, "items": {
      "type": "object",
      "required": ["name", "trigger_price", "pnl", "pnl_R", "pnl_pct_equity", "equity_after", "balance_after"],
      "properties": {
        "name": { "enum": ["tp_hit", "sl_hit", "adverse_to_support", "adverse_gap", "news_shock", "time_decay"] },
        "trigger_price": { "type": "string" },
        "probability": { "type": ["number", "null"], "minimum": 0, "maximum": 1 },
        "probability_n": { "type": ["integer", "null"] },
        "probability_status": { "enum": ["computed", "insufficient_data"] },
        "pnl": { "type": "string" },
        "pnl_R": { "type": "string" },
        "pnl_pct_equity": { "type": "string" },
        "equity_after": { "type": "string" },
        "balance_after": { "type": "string" } } } },
    "expected_value_R": { "type": "string" },
    "worst_case": { "type": "string" },
    "best_case": { "type": "string" },
    "time_to_decision": { "type": "string", "format": "date-time" },
    "manage_triggers": { "type": "array", "items": { "type": "string" } },
    "recommendation": { "enum": ["enter", "manage_existing", "wait", "skip"] },
    "recommendation_reason": { "type": "string" },
    "news_context": { "type": "array", "items": { "type": "object" } },
    "computed_at": { "type": "string", "format": "date-time" },
    "inputs_hash": { "type": "string" }
  }
}
```

Rule: `probability` is null exactly when `probability_status` is
`insufficient_data` (n < 30 for F-68). A scenario without a probability
still carries its deterministic P/L — the decision uses bounds, not a
guessed number.

### 4.4 RiskAssessment (RCE → PME/EXE/decision record)

```json
{
  "type": "object",
  "required": ["approved", "size_lots", "max_loss", "alternative", "computed_at"],
  "properties": {
    "approved": { "type": "boolean" },
    "size_lots": { "type": "string" },
    "sl": { "type": ["string", "null"] },
    "tp": { "type": ["string", "null"] },
    "max_loss": { "type": "string" },
    "max_loss_R": { "type": "string" },
    "max_loss_pct_equity": { "type": "string" },
    "portfolio_impact": { "type": "object",
      "properties": {
        "exposure_after": { "type": "string" },
        "margin_after": { "type": "string" },
        "drawdown_budget_remaining": { "type": "string" } } },
    "veto_reason": { "type": ["string", "null"],
      "enum": [null, "margin_floor", "daily_loss_limit", "kill_switch",
               "symbol_blocked", "max_simultaneous", "grant_exhausted"] },
    "alternative": { "type": "string",
      "enum": ["reduce_size", "wait_for_level", "different_symbol",
               "manage_existing", "none"] },
    "risk_check": { "type": "object" },
    "computed_at": { "type": "string", "format": "date-time" },
    "inputs_hash": { "type": "string" }
  }
}
```

Rule (D-4, enforced by test): `approved == false` requires
`veto_reason != null` **and** `alternative != "none"`.

### 4.5 PositionState (sync/EXE → PME/PRE/RCE)

```json
{
  "type": "object",
  "required": ["ticket", "account_id", "symbol", "direction", "volume", "entry", "state"],
  "properties": {
    "ticket": { "type": "string" },
    "account_id": { "type": "string" },
    "symbol": { "type": "string" },
    "direction": { "enum": ["buy", "sell"] },
    "volume": { "type": "string" },
    "entry": { "type": "string" },
    "sl": { "type": ["string", "null"] },
    "tp": { "type": ["string", "null"] },
    "floating_pnl": { "type": "string" },
    "floating_pnl_R": { "type": "string" },
    "state": { "enum": ["unmanaged", "adopted", "managing", "protecting", "scaling", "closing", "closed"] },
    "origin": { "enum": ["engine", "user", "unknown"] },
    "strategy_id": { "type": ["string", "null"] },
    "strategy_version": { "type": ["integer", "null"] },
    "opened_at": { "type": "string", "format": "date-time" },
    "updated_at": { "type": "string", "format": "date-time" }
  }
}
```

### 4.6 ManagementAction (PME → EXE)

```json
{
  "type": "object",
  "required": ["ticket", "action", "rule_id", "reason", "decision_id"],
  "properties": {
    "ticket": { "type": "string" },
    "action": { "enum": ["hold", "breakeven", "partial_close", "trailing",
                          "scale_in", "scale_out", "close", "modify_sltp"] },
    "volume": { "type": ["string", "null"] },
    "sl": { "type": ["string", "null"] },
    "tp": { "type": ["string", "null"] },
    "rule_id": { "type": "string" },
    "reason": { "type": "string" },
    "decision_id": { "type": "string" }
  }
}
```

Rule: `action == "hold"` carries no sl/tp/volume. `partial_close`
requires `volume` (validated ≤ open volume, §5 of DESIGN.md).
`scale_in` requires a strategy that explicitly allows scaling.

### 4.7 NewsImpact (NSE → MAE/STE/PME/RCE)

```json
{
  "type": "object",
  "required": ["item_id", "importance", "symbols", "computed_at"],
  "properties": {
    "item_id": { "type": "string" },
    "importance": { "type": "integer", "minimum": 0, "maximum": 3 },
    "importance_reasons": { "type": "array", "items": { "type": "string" } },
    "symbols": { "type": "array", "items": { "type": "string" } },
    "direction_lean": { "type": "object",
      "additionalProperties": { "enum": ["bullish", "bearish", "neutral"] } },
    "sentiment_score": { "type": ["number", "null"] },
    "freeze_window": { "type": ["object", "null"],
      "properties": {
        "symbol": { "type": "string" },
        "from": { "type": "string", "format": "date-time" },
        "to": { "type": "string", "format": "date-time" } } },
    "affected_positions": { "type": "array", "items": { "type": "string" } },
    "computed_at": { "type": "string", "format": "date-time" }
  }
}
```

### 4.8 FearGreedState (FGE → PME/RCE/coaching)

```json
{
  "type": "object",
  "required": ["state", "evidence", "computed_at"],
  "properties": {
    "state": { "enum": ["balanced", "fearful", "greedy", "tilted", "insufficient_data"] },
    "evidence": { "type": "array", "items": { "type": "object",
      "required": ["factor_id", "value", "trade_refs"],
      "properties": {
        "factor_id": { "type": "string" },
        "value": { "type": "string" },
        "trade_refs": { "type": "array", "items": { "type": "string" } } } } },
    "protection_delta": { "type": "integer", "minimum": -100, "maximum": 100 },
    "size_modulation": { "type": ["number", "null"], "minimum": 0, "maximum": 1 },
    "nudge": { "type": ["string", "null"] },
    "intervention": { "type": ["object", "null"],
      "properties": {
        "action": { "type": "string" },
        "ticket": { "type": "string" },
        "permission": { "type": "string" } } },
    "computed_at": { "type": "string", "format": "date-time" }
  }
}
```

Rule: `protection_delta` is applied only within the user's set bounds
on the Risk Free policy, never beyond them. `size_modulation`
multiplies RCE's approved size (1 = no change, < 1 = reduced); it
never increases size above RCE's assessment. `intervention` is null
unless the account is in `auto_execute` with the intervention
permission enabled.

### 4.9 ExecutionResult (EXE → reconciliation/decision record)

```json
{
  "type": "object",
  "required": ["command_id", "action", "status", "decision_id"],
  "properties": {
    "command_id": { "type": "string" },
    "action": { "type": "string" },
    "ticket": { "type": ["string", "null"] },
    "status": { "enum": ["enqueued", "delivered", "completed", "failed", "expired", "reconciled", "diverged"] },
    "broker_retcode": { "type": ["integer", "null"] },
    "order_check_result": { "type": ["object", "null"] },
    "fill_price": { "type": ["string", "null"] },
    "fill_volume": { "type": ["string", "null"] },
    "failure_reason": { "type": ["string", "null"] },
    "decision_id": { "type": "string" },
    "latency_ms": { "type": "integer", "minimum": 0 },
    "decision_to_send_ms": { "type": ["integer", "null"], "minimum": 0 },
    "broker_response_ms": { "type": ["integer", "null"], "minimum": 0 },
    "updated_at": { "type": "string", "format": "date-time" }
  }
}
```

### 4.10 PolicyEnvelope (server → Bridge, fast trailing only)

```json
{
  "type": "object",
  "required": ["account_id", "version", "bounds", "rules", "issued_at", "expires_at"],
  "properties": {
    "account_id": { "type": "string" },
    "version": { "type": "integer", "minimum": 1 },
    "bounds": { "type": "object",
      "properties": {
        "max_risk_per_trade_pct": { "type": "number" },
        "max_daily_loss_pct": { "type": "number" },
        "max_simultaneous": { "type": "integer" },
        "allowed_symbols": { "type": "array", "items": { "type": "string" } },
        "allowed_strategies": { "type": "array", "items": { "type": "string" } } } },
    "rules": { "type": "array", "items": { "type": "object",
      "properties": {
        "rule_id": { "type": "string" },
        "params": { "type": "object" } } } },
    "risk_free_policy": { "type": "object" },
    "issued_at": { "type": "string", "format": "date-time" },
    "expires_at": { "type": "string", "format": "date-time" }
  }
}
```

Rule: `expires_at - issued_at ≤ 5 min`. The envelope authorizes
**fast trailing / breakeven only** — it never authorizes an open.
The Bridge acts only while `now < expires_at` and the WS is up;
otherwise it holds.

## 5. Decision record

Extends the existing `ai_agent_decisions` row (§17.3) — one table,
extended columns, not a second decision table:

```
ai_agent_decisions
  + engine_chain JSONB      -- ["MAE","STE","PRE","RCE","PME","EXE"] (engines that contributed, in order)
  + scenario_ref UUID       -- scenario_runs.id (nullable for non-trade decisions)
  + inputs_hash TEXT        -- hash of the inputs the decision was computed from
  + knowledge_version TEXT  -- strategy_param_snapshots.id + data vintage (what the engine knew)
  + data_vintage_ts TIMESTAMPTZ -- newest input timestamp the decision used
  + fear_greed_ref UUID     -- FearGreedState row (nullable)
  + latency_ms JSONB        -- {"MAE": 12, "PRE": 88, "RCE": 4, ...} per engine stage
  + decision_to_send_ms INT -- decision created_at -> command enqueued
  + broker_response_ms INT  -- command delivered -> action.result
  (existing: proposed_action, indicator_inputs, active_risk_profile_id,
   active_style_ids, proposed_params, risk_validation, command_id, outcome)
```

Rules:
- The row is written **before** any command is enqueued (D-2).
- `outcome` is filled when the position closes (or the command
  resolves): `completed | failed | vetoed | expired | reconciled`.
- A decision without `rule_id` + `risk_check` + `scenario_ref`
  (for trade decisions) is rejected at the enqueue boundary.
- The dashboard "show me why this trade closed" (OP-20) reads:
  decision → scenario_run → command → execution result → close
  outcome. One chain, no gaps.

### 5.1 OrderTrace (end-to-end audit, one query)

`GET /api/v1/connector/orders/{ticket}/trace` returns, in order:
decision (decision_id, engine_chain, scenario_ref, risk_ref,
knowledge_version, data_vintage_ts, fear_greed_ref) → command
(command_id, action, rule_id, risk_check) → `order_check` result →
broker execution (retcode, fill price/volume, delivered_at,
`action.result`) → position status (`managed_positions` state +
terminal state at last sync) → subsequent management (all later
`ManagementAction`s + decisions on the ticket) → close outcome (when
closed: net P/L, R, postmortem ref). The trace is append-only; a gap
(a command with no broker result and no reconciliation) is itself a
flagged anomaly. One order = one trace.

## 6. Position lifecycle (state machine)

```
unmanaged ──adopt──▶ adopted ──attach policy──▶ managing
  ▲                       │  ▲                         │
  │                       │  │                         ▼
  │                       │  └───protecting ◀── Risk Free trigger
  │                       │         │  ▲                │
  │                       │         ▼  │                ▼
  │                       │      scaling ─────────▶ closing ──▶ closed
  │                       │         │                (terminal)
  │                       └─────────┘
  │
  └── diverged (terminal state mismatch — halt on ticket, escalate)
```

Transitions and their triggers:

| From → To | Trigger | Who |
|---|---|---|
| `unmanaged → adopted` | First positions batch containing the ticket; strategy assigned or `unassigned`; RiskFreePolicy attached | PME |
| `adopted → managing` | First management cycle after adoption (protective default) | PME |
| `managing → protecting` | Risk Free trigger hit (+trigger_R) or FGE protection delta applied | PME |
| `protecting → managing` | Position back below trigger after a partial (re-armed per policy) | PME |
| `managing/protecting → scaling` | PRE+RCE approve a scale action (strategy allows scaling) | PME → EXE |
| `scaling → managing/protecting` | Scale executed + reconciled | EXE → PME |
| `* → closing` | `close` / `partial_close` action enqueued | PME → EXE |
| `closing → closed` | Terminal state confirms close (sync) or `action.result` completed + reconciled | EXE |
| `closing → managing` | Close failed and terminal still shows the position (reconciled, action retried only by a new decision) | EXE → PME |
| `* → diverged` | Terminal state contradicts the ledger (CONTRACTS.md §8) | EXE (halt on ticket) |

Rules: `closed` is terminal. `diverged` halts all engine action on
that ticket until a human or a fresh reconciliation resolves it. No
transition happens without an event on the bus.

## 7. Execution wire shapes (gateway → Execution Bridge → MT5)

The gateway is the single execution channel (DESIGN.md D-5, decided
2026-11-18 ~19:01). All four operations are defined in this contract
from the start; two are gated until the §17.5 close-out lands in
Phase 2b (demo). The gate is the `validate_operation` allowlist + a
phase flag — one code path, not two.

| Action | Wire operation | Gated until | MT5 action (Bridge) |
|---|---|---|---|
| `open` | `{ "type": "open_order", "symbol", "side": "buy"\|"sell", "volume", "sl": <finite\|null>, "tp": <finite\|null>, "type": "market" }` | §17.5 close-out, Phase 2b (demo) | `order_send` (TRADE_ACTION_DEAL) after `order_check` |
| `modify_sltp` / `breakeven` / `trailing` | `{ "type": "modify_sl_tp", "ticket": "<ticket>", "sl": <finite>, "tp": <finite\|null> }` | Sendable in v1 | `order_send` (TRADE_ACTION_SLTP) after `order_check` |
| `partial_close` (reduce-volume) | `{ "type": "close_position", "ticket": "<ticket>", "volume": "<lots>" }` | §17.5 close-out, Phase 2b (demo) — v2 wire shape; exact field name confirmed at close-out implementation | `trade_close_single_position` + `volume` (MQL5 `PositionClosePartial`; hedging-only; oversize volume closes in full) |
| `close` | `{ "type": "close_position", "ticket": "<ticket>" }` | Sendable in v1 | `trade_close_single_position` (full) |
| `modify_pending` (SL/TP) | Capability-gated (§17.4); SL/TP only. | Capability close-out (§17.4) | `trade_modify_sl_tp` (order ticket) |

Every enqueue carries: `command_id` (uuidv7, UNIQUE — idempotency
key), `decision_id`, `rule_id`, `risk_check` (RCE JSON),
`scenario_ref`, `created_by: "engine"`, TTL (5 min default, 15 max).

**Pre-send `order_check` (Bridge-side, mandatory).** On receipt of a
gateway command, the Bridge runs MT5 `order_check` before any
`order_send` and records the result (`order_check_result`) on the
command. A failed check ends the command as `failed` with reason
`order_check_failed` — nothing is sent. A passing check is **never**
a guarantee of successful execution: it proves validity and probable
effect (post-trade balance/equity/margin preview) at check time only.
Send retcodes: 10009 done, 10008 placed, 10010 partial. A `None`
result or retcode 10031 (no connection) does **not** mean the order
failed — reconcile from `positions_get` / `history_deals_get` before
anything else.

## 8. Reconciliation + recovery matrix

Reconciliation runs on every positions sync batch: terminal state vs
command ledger, per ticket. It runs only against a **complete** book:
the positions batch carries a `book_read` completeness flag, and an
incomplete batch never resolves a command.

| Ledger state | Terminal state | Result |
|---|---|---|
| `completed` (close) | Position gone | `reconciled` — outcome recorded |
| `completed` (close) | Position still open | `diverged` — escalate, halt on ticket |
| `completed` (modify) | SL/TP match the command | `reconciled` |
| `completed` (modify) | SL/TP differ | `diverged` — escalate, halt on ticket |
| `pending`/`delivered`, TTL expired | Position gone / SL-TP changed as commanded | `reconciled` (resolved by terminal state) |
| `pending`/`delivered`, TTL expired | No change | `failed` (reason `no_result`) — a **new** decision is required to retry; never auto-resent |
| `failed` | Any | Stays `failed`; retry only via a new decision + new `command_id` |
| `failed` (`order_check_failed`) | Any | Stays `failed`; nothing was sent; retry only via a new decision + new `command_id` |
| No command | Position exists, not in `managed_positions` | `unmanaged` → adoption path (DESIGN.md §3.5) |
| No command | Position exists, in `managed_positions`, state `closed` | `diverged` — a position the engine thought closed is open; halt, escalate |

**Recovery matrix (disconnect / unclear result):**

| Situation | Action |
|---|---|
| WS to Bridge drops, command `pending` | Hold. Do not re-send. On reconnect, resolve by terminal state (§8 table). |
| `delivered`, no `action.result` after one sync cycle | Resolve by terminal state. |
| Broker retcode unclear / unknown | Treat as unknown, never as success. Resolve by terminal state. |
| `None` result or retcode 10031 on send | Reconcile from `positions_get` / `history_deals_get` before anything else; never re-send, never assume failure. |
| `order_check` refusal | Command ends `failed` (`order_check_failed`) with the check result; nothing is sent. |
| Local agent restart | Reload envelope + pending local actions from the server ledger; never from local memory alone. |
| Postgres down mid-decision | Decision not recorded → command not enqueued (fail-closed for entries). In-flight management holds. |
| Gateway down | Commands queue server-side with TTL; expired commands marked `expired`, never sent late. |
| Bridge offline | Server EXE sends nothing (no path to the terminal); PME queues actions with `bridge_offline` flag, visible in the dashboard. |
| Kill-switch trips mid-execution | In-flight close/modify completes (protective); no new opens; no new scale-ins. |

**The rule, in one line:** when the outcome is unknown, the engine
asks the terminal — it never guesses and never re-sends.

## 9. Idempotency + duplicate prevention (code rules)

1. `command_id` is UNIQUE. A retry with the same `command_id`
   replays the stored result; exactly one MT5 action happens.
2. Before enqueue, EXE checks: (a) same ticket + same action already
   `pending`/`delivered` → refuse `duplicate_pending`; (b) an open
   decision for a symbol+direction+strategy that already has an open
   position or a pending open → refuse `duplicate_position`, unless
   the strategy explicitly allows scaling (then it is a `scale_in`
   action with its own rule); (c) action incompatible with the current
   position state → refuse `stale_state` after a reconciliation read.
3. Events carry `idempotency_key`; the bus dedupes redelivery.
4. Scenario runs carry `inputs_hash`; an identical recomputation
   replays the stored ScenarioSet instead of recomputing (and the
   replay is labelled as such on the decision record).

## 10. Test matrix (summary — full plan in DESIGN.md §19)

| Layer | What is asserted |
|---|---|
| Unit | Factor math, PRE scenarios, RCE veto+alternative, PME rules, Risk Free levels, decision-record rejection without reason |
| Contract | Every event validates against §4 schemas; every prompt output validates; decision replay determinism |
| Integration | Full loop on fixtures; adoption; news freeze; kill-switch; grant exhaustion |
| Failure | All rows of the §8 recovery matrix + all §8 reconciliation rows |
| Live smoke | Demo account + real Bridge + MT5: open via the gateway (after the §17.5 close-out), partial close via the gateway reduce-volume operation, breakeven via the gateway modify, disconnect mid-command, adopt a manual position |
