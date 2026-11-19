# Agent Trade Engine — Design (Phase 2) — v2

Status: **DESIGN ONLY.** No code, no PR, no merge, no deploy. Not part of PR #107.
Author: design-revision agent, 2026-11-18. Grounded in the code on branch
`feat/connector-gateway` (worktree `~/workspace/bridge-gw-107`, read-only
reference) at main 3359fe9 + PR #107 head b93bb1f, and in the contract
`~/workspace/NSE-Platform/docs/NSE-CONNECTOR-CONTRACT.md` (v1 + §17.5).

**v2 — direction change (user, 2026-11-18 ~18:51).** v1 framed the engine
as an analyzer / signal / coach system: factors → prompts → proposals,
with execution as a later, cautious add-on. The user corrected that
framing fundamentally: **this is not an analyzer. It is an independent,
intelligent Trading Engine that manages the user's trading account —
opens orders, closes orders, manages positions, preserves capital, and
grows profit.** Everything below is rewritten around that. What v1 got
right is kept (grounding §1, prompt system §5, factor catalog §6, news
§7, coaching §8, transparency §13, scalability §14); what v1 got wrong
is the center of gravity: advisory was the product, execution was the
exception. In v2, **execution is the product** and advisory is a surface
on top of the same decisions.

**v2.1 — execution decisions (user, 2026-11-18 ~19:01).** Eight
decisions folded in: (1) the Gateway v1 contradiction is **resolved** —
one execution channel, contract v2 (D-5 revised, O-2 resolved, §3.8);
(2) `order_check` runs before every send and is never a guarantee;
(3) fear/greed is a decision input from internal proxies, not a
display number; (4) market data is classified by real delay and gated
by timestamp + freshness (§3.9); (5) the dedicated Execution Bridge
is the basis (no mature open-source FX/CFD+MT5 engine exists);
(6) parallel engine cooperation is the top priority (§2.4); (7) an
end-to-end order trace for every order (§4, CONTRACTS.md §5);
(8) real latency metrics (§12). The document is not longer for its
own sake: duplicate decisions are merged (§18) and every contract is
stated once, in CONTRACTS.md.

Parent spec: `~/workspace/agent-trade-dashboard/SPEC.md` (§ "PHASE 2 —
Trading engine") and `~/workspace/agent-trade-dashboard/PROGRAM-ROADMAP.md`.
Research: `~/workspace/agent-trade-engine/RESEARCH-SCALPING.md`.
Inter-engine contracts: `~/workspace/agent-trade-engine/CONTRACTS.md`
(event catalog, message schemas, decision record, execution state
machine, recovery matrix, test matrix) — read it with this document.

## 0. The one-sentence version

Eight parallel engines — Market Analysis, Strategy, Prediction, Risk &
Capital, Position Management, News & Sentiment, Fear & Greed, Execution —
continuously sense the market and the user's account, compute scenarios
for every position before and during the trade, and **execute real orders
through the user's own MT5 terminal** (open, manage, close, partial
close, breakeven, trailing) under explicit permission, with every
decision recorded, every number computed by code, and AI used only where
reasoning is actually needed — never in the critical order-send path.

## 1. What already exists (grounding — do not rebuild)

| Piece | Where | What it gives Phase 2 |
|---|---|---|
| Gateway commands | `backend/src/modules/connector/commands.rs` | `POST /api/v1/connector/commands`: enqueue `close_position` / `modify_sl_tp` (+ capability-gated `modify_pending`, SL/TP only). Command TTL default 5 min, max 15 min. `open_order` / `cancel_order` are **explicitly NOT sendable in v1** — the gateway refuses them at enqueue (test `open_order_is_refused_in_v1`). SL/TP must be finite numbers. |
| `open_order` close-out path | Contract §17.5, D-INV-2 | `open_order` is a DEFINED operation with status B ("pending user decision") — neither silently included nor excluded. Adding it later needs **no contract rework**, only: (1) user approval, (2) allowlist addition, (3) Risk Engine / financial-consent proof on demo, (4) `schema_version` bump. This is the contract-v2 decision (O-2). |
| Partial close | Contract D-INV-4, MQL5 `PositionClosePartial` | MT5 supports partial close (`trade_close_single_position` + `volume`; hedging-only; oversize volume closes in full). **But the v1 gateway wire shape for `close_position` is `{type, ticket}` — no `volume` field.** So: partial close needs a wire-shape addition (contract change, same class as the `open_order` close-out, executed in Phase 2b); the Bridge can already execute it against MT5 — but only as a gateway command, never self-initiated. |
| Pending-order limits | Contract §17.4, D-INV-3 | `modify_pending` is SL/TP only, capability-gated. Price/volume/expiration changes have NO verified MT5 MCP action; pending-order volume change is NOT POSSIBLE per MQL5 (delete + replace instead). The engine must never plan a "change pending volume" action. |
| Command lifecycle | `connector_commands` table, `ws.rs` | `pending → delivered → completed/failed` with `action.result` round-trip; `risk_check` column exists but is **written NULL** — Risk Engine integration is a Phase 2a deliverable (it fills this column, §3.4). `command_id` UNIQUE — the idempotency key the Execution Engine relies on. |
| Sync batches | `connector/contract.rs` §3.3, `gateway.rs` | Batch kinds `account \| positions \| history_orders \| history_positions`. Closed-trade history already arrives; `account_snapshots` + `trading_accounts` stored server-side. The `positions` batch is the Position Management Engine's source of truth for adoption + reconciliation. |
| Coaching endpoint | `connector/dashboard.rs` (`GET /api/v1/connector/coaching`) | Computed **only** from stored history: closed positions (`history_positions`) + command outcomes. No invented data. Phase 2 extends it with behavior + scenario outcomes. |
| Agent config (§17) | `connector/agent.rs` | Risk profiles, trading styles, per-account settings (`signal_execution: off \| suggest_only \| auto_execute`), effective-config endpoint that **downgrades `auto_execute` → `suggest_only`** when the §6.4 gates fail. Decision audit ingest (§17.3) into `ai_agent_decisions` (proposed_action vocabulary: `open_order \| close_position \| modify_sl_tp \| modify_pending \| hold \| no_trade`; fields: `indicator_inputs`, `active_risk_profile_id`, `active_style_ids`, `proposed_params`, `risk_validation`, `command_id`, `outcome`). The engine decision record (§4, CONTRACTS.md) extends this row — it does not replace it. |
| Entitlements | `connector/contract.rs` (`Entitlements`), `auth.rs` `plan_entitlements`, `grants.rs` | Flags: `history_sync, ai_coach, position_manager, auto_trade, max_devices, max_accounts`. Grant → demo → paid-plan order (G-6). |
| Consent gate | `api_keys.trade_opt_in`, `agent.rs` `auto_trade_allowed` | Auto-trade requires a **live, unrevoked API key with `trade_opt_in = TRUE` and `expires_at > now()`** AND the G-6 entitlement. Never silently enabled. Real-money execution additionally requires the per-account opt-in screen (§10.3). |
| Risk profiles | `agent.rs` `validate_risk_profile` | Bounds already enforced: `max_risk_per_trade_pct (0,5]`, `max_daily_drawdown_pct (0,20]`, `max_daily_loss_pct (0,20]` (≥ per-trade risk), `max_simultaneous_positions 1..=50`, `margin_level_floor_pct ≥ 100`, `stop_loss_required` (default true), `max_position_size_lots`, `allowed_symbols`, free-form `parameters` JSON. |
| AI gateway | `ai_gateway/service.rs` `infer` | Single inference entrypoint. Per-engine config in `ai_engine_configs` (primary/fallback model, timeout default 90s, per-user daily limit, enabled). Primary → fallback **once** on classified failure. Exactly one usage event per `infer` call. 9Router OpenAI-compatible chat completions; 10s connect timeout. Failure is never reported as success. |
| Prompt system | `ai_gateway/prompts.rs`, migration `0020_ai_gateway.sql` | Code-owned default prompts per engine; admin-managed templates in `ai_prompt_templates` (`UNIQUE (engine_key, version)`, `system_prompt` + `user_prompt_template`, ≤20k chars each); `ai_engine_configs.prompt_template_id` pins a template, NULL = latest; **the version used is recorded on the analysis**. Injection defense: user context fenced as DATA (`fence_untrusted`); epistemic labels every engine must use: `OBSERVED / CALCULATED / AI_INTERPRETED / ASSUMED / UNAVAILABLE`; never fabricate exact prices or statistics not provided. |
| Prompt reliability ladder | `trading_analytics/coach.rs` (spec §7.9) | Parse model JSON → validate typed contract → **ONE strict-JSON retry** (2x token budget if truncated, `finish_reason == "length"`) → raw fallback with honest markers (`report_kind: "raw_fallback"`, `degraded: true`). Typed reports carry `version: 2`, `report_kind: "trading_coach_report"`, and the note *"AI interpretation of deterministic engine output; the model computes no numbers."* Unparseable output is never dropped and never rewritten into fake structure. Quota units are reserved up front and released on every failure path. |
| Deterministic engines | `trading_analytics/engines/` | `performance` (win rate + CI bounds, expectancy, profit factor gross/net, streaks, drawdown + recovery), `session` (session/weekday/hour buckets in resolved local tz), `symbol_edge` (per-symbol edge, style, consistency incl. CV + drift gates), `risk` (volume stats, drawdown), `trade_management`, `behavior`. Shared `EngineOutput` (`engine_id`, `engine_version`, metrics, evidence-backed `Finding`s with `sample_size` + `supporting_trade_references`, `Coverage` with `unsupported` list). Break-even = `\|net\| < 0.005` currency units, excluded from win/loss rates. Profit factor is `None` (not fabricated) when there are no losing trades. |
| Analysis pipeline | `trading_analytics/pipeline.rs` | Postgres queue + 2 workers, claim with `FOR UPDATE SKIP LOCKED`, 15-min stuck requeue, terminal failure marking. Runs selected deterministic engines → consensus → AI coach last. **This queue pattern is the event bus for the 8 engines (§4)** — reused, not reinvented. |
| Dataset | `trading_analytics/dataset.rs` `Position` | `reference, symbol, direction, volume, avg_entry/exit_price, opened_at, closed_at, holding_seconds, gross_pnl, commission, swap, fee, net_pnl, stop_loss, take_profit, status` + cash flows + `equity_curve`. All money in `rust_decimal::Decimal` — never float. |
| News + sentiment | `market_intelligence/news.rs`, `news_sentiment.rs`, migration `0014_news_items.sql` | `news_items` table, capped 10,000 rows, oldest pruned. Deterministic lexicon scoring at ingest: score in `[-1, 1]`, `|score| ≤ 0.1` is noise, negation flips within 3 words, matched terms stored. Sources: FXStreet, ForexLive, CoinDesk, CoinTelegraph (live). **No importance field exists yet** — Phase 2a adds it (§7). |
| Market data | `market_intelligence/market.rs`, `candles.rs`, `indicators.rs` | Live quote cache (MT5/broker feed primary for FX/gold, Binance public klines primary for crypto, TradingView MCP for research/outlook only, Yahoo best-effort only, er-api FX fallback — RESEARCH §3.4–3.5), candles, indicators. `risk/specs.rs`: compile-time contract specs per instrument (pip size, contract size, quote currency) — deterministic position-sizing math. |
| Logs/audit | `connector/dashboard.rs` `GET /api/v1/connector/logs` | Unified feed from `connector_audit` + `connector_commands` + sync batches. Phase 2 decisions/commands land here automatically. |

### 1.1 Hard constraints inherited from the code (non-negotiable)

1. **The gateway is the single execution channel — contract v2
   (decided 2026-11-18 ~19:01, D-5 revised, O-2 resolved).** The v1
   gateway refuses `open_order` and its `close_position` has no
   `volume` field. That is a **phase gate, not an architectural
   fork**: the architecture contract (CONTRACTS.md §7) defines all
   four operations — open, modify, reduce-volume (partial close),
   close — from the start, and the gateway sends all four. The
   §17.5 close-out (user approval + allowlist + Risk Engine /
   financial-consent proof on demo + schema bump) is executed in
   Phase 2b on demo accounts; until it lands, Phase 2a ships no
   opens (`suggest_only` + server close/modify only). The Bridge
   never self-initiates an open — it executes gateway commands
   against MT5 and runs the pre-send `order_check` (§3.8). There is
   exactly one open path, one ledger, one audit trail.
2. **The server can close and modify.** `close_position` and
   `modify_sl_tp` are sendable today — that is the server-side
   execution surface. TP/SL management, breakeven, trailing (server
   side), and the kill-switch are built on it.
3. **Numbers are computed by code, never by the model.** `coach.rs`
   states it and the epistemic rules enforce it: the model interprets
   deterministic engine output; it computes no numbers. Phase 2 keeps
   this split everywhere: factors, scenarios, sizing, probabilities →
   code; interpretation, narrative, unusual-condition reads → prompts.
4. **Consent is a key, not a toggle in the UI.** `trade_opt_in` lives
   on `api_keys` and is checked at dispatch time against a live key.
   The dashboard may surface it, never bypass it. Real-money execution
   additionally requires the per-account opt-in screen (§10.3).
5. **Money is `Decimal`.** Every new factor, scenario, sizing, and P/L
   computation uses `rust_decimal::Decimal` like the existing engines.
   Float is for display only, never for decisions.
6. **No engine is an island.** Every engine's output must name its
   consumer(s) (§2 table). An engine whose output no other engine
   consumes is deleted, not kept as a dashboard widget.
7. **AI is never in the critical order-send path.** The path
   decision → validation → enqueue → Bridge → MT5 is code-only. An AI
   outage degrades analysis; it never blocks or delays an execution
   that the code path has already decided.

## 2. Architecture: eight parallel engines, one decision loop

The engine is not a pipeline that ends in a report. It is a **closed
loop** that runs continuously per account:

```
sense → analyze → select → predict → risk → decide → execute → reconcile
  ↑                                                              │
  └──────── outcomes feed factors, behavior, coaching ───────────┘
```

```
  ┌──────────────────────────────────────────────────────────────────┐
  │  EVENT BUS (Postgres queue + SKIP LOCKED claim — §4)             │
  │  market events · position events · news events · decision events │
  └──────┬──────┬──────┬──────┬──────┬──────┬──────┬──────┬──────────┘
         │      │      │      │      │      │      │      │
       ┌─┴─┐  ┌─┴─┐  ┌─┴─┐  ┌─┴─┐  ┌─┴─┐  ┌─┴─┐  ┌─┴─┐  ┌─┴─┐
       │MAE│  │STE│  │PRE│  │RCE│  │PME│  │NSE│  │FGE│  │EXE│
       └───┘  └───┘  └───┘  └───┘  └───┘  └───┘  └───┘  └───┘
       Market Strategy Predict Risk  Position News Fear  Execution
       Analysis Engine  ion    &Cap  Mgmt    &Sent &Greed Engine
  ┌──────────────────────────────────────────────────────────────────┐
  │  SERVER PLANE (Germany): MAE/STE/PRE/RCE/PME/NSE/FGE + server   │
  │  EXE (gateway sends open/modify/reduce/close) · factors · AI     │
  │  LOCAL PLANE (Execution Bridge, user's PC): MT5 adapter          │
  │  (order_check/order_send/reads) + fast trailing under envelope   │
  └──────────────────────────────────────────────────────────────────┘
```

### 2.1 The eight engines (summary — detail in §3)

| # | Engine | Owns | Consumes (from) | Produces (to) | Trigger | Compute | Latency budget (code path, p95) |
|---|---|---|---|---|---|---|---|
| 1 | **MAE** Market Analysis | Price, structure, trend, liquidity, volatility per symbol+TF | Candles/quotes (ingest), NewsImpact (NSE) | `MarketRegime` → STE, PRE, RCE | New candle batch, regime change, on demand | Code (indicators/factors); AI only for pattern naming + unusual-condition read | < 50 ms / symbol |
| 2 | **STE** Strategy | Which versioned strategy + parameters apply today/this hour | `MarketRegime` (MAE), factors, daily knowledge (§7.3), user profile, BehaviorFlags (FGE) | `StrategySelection` → PRE, RCE, PME | Regime change, session start, daily refresh | Code (deterministic selection); AI proposes parameter changes, code validates + evals | < 100 ms |
| 3 | **PRE** Prediction | Scenarios for every candidate and every open position | `StrategySelection` (STE), `MarketRegime` (MAE), historical distributions (factor store), account state | `ScenarioSet` → RCE, PME, decision record | Before every entry decision; on position change; periodic re-eval | **Code only for all numbers**; AI for narrative only | < 100 ms / position |
| 4 | **RCE** Risk & Capital | Sizing, SL/TP, portfolio exposure, drawdown budget — **active optimizer, not a brake** | `ScenarioSet` (PRE), account state, open exposure, limits, RiskFreePolicy, `FearGreedState` (FGE — size modulation) | `RiskAssessment` → PME, EXE, decision record | Every entry/manage decision | **Code only** | < 20 ms |
| 5 | **PME** Position Management | Lifecycle of every open position, incl. ones the engine did not open | `PositionState` (sync), `ScenarioSet` (PRE), `RiskAssessment` (RCE), `NewsImpact` (NSE), `FearGreedState` (FGE) | `ManagementAction` → EXE; `PositionState` → PRE, RCE | 1-min server loop; position events; news freeze | Code (rules); AI explains only, never decides | < 50 ms / position / cycle |
| 6 | **NSE** News & Sentiment | News, calendar, sentiment → impact per symbol/position | News feed, calendar, MCP outlook (research) | `NewsImpact` → MAE, STE, PME, RCE (freeze) | Ingest event | Code (importance rules, lexicon); AI triage/interpretation | Async; < 5 s / item |
| 7 | **FGE** Fear & Greed | Balance between risk-taking and capital protection, from the user's own behavior | Behavior factors (F-36/F-37/F-41), `PositionState`, `RiskAssessment` | `FearGreedState` (protection delta, nudge, intervention) → PME, RCE, coaching | Position events, behavior flag, periodic | Code (proxies + thresholds); AI for nudge text only | < 50 ms |
| 8 | **EXE** Execution | The only engine that touches the terminal | `ManagementAction` (PME), open decisions (STE+PRE+RCE chain), `PolicyEnvelope` | `ExecutionResult` → reconciliation, decision record, factors; `OrderTrace` (§4) | Decision event | **Code only. Zero prompts. Zero AI.** | Enqueue < 50 ms; decision-to-send p95 < 1 s; delivery bounded by command TTL |

**The no-islands rule, enforced:** the table above names a consumer for
every output. If a review finds an engine output with no consumer, that
output (and, if nothing remains, the engine) is removed.

### 2.2 Decisions

**D-1 — Execution is the product; advisory is a surface.**
The system manages the account. Coaching, signals-as-information, and
the dashboard are views over the same decisions the engine makes — not
the goal. A phase that ships analysis without a path to execution is
not shippable. (Reverses v1's center of gravity.)

**D-2 — The server is the only place a trade decision becomes
official.** A local decision is provisional until its §17.3 ingest row
exists server-side with `risk_validation` and (when it commands) a
`command_id`. The dashboard, the audit, and the coaching all read the
server-side record, never the client's word alone. The decision record
(§4, CONTRACTS.md §5) is written **before** any command is enqueued.

**D-3 — `suggest_only` is the default everywhere; `auto_execute` is
earned per account.** New accounts start `off` → user enables
`suggest_only` → after a demo probation (see §10) and explicit
`trade_opt_in` on a live key, `auto_execute` becomes available, still
bounded by the risk profile + grant + daily limits. The
effective-config endpoint already downgrades `auto_execute` →
`suggest_only` when gates fail; Phase 2 adds the probation and
kill-switch to those gates.

**D-4 — Risk is an optimizer, not a brake.** The Risk & Capital Engine
must always return the best risk-adjusted action available — trade at
size X, trade at reduced size, wait for a better level, manage the
existing position instead — and may veto only on a **hard limit**
(margin floor, daily loss limit, kill-switch, blocked symbol). **Every
veto names an alternative.** A bare "no" with no alternative is a
design violation and a test failure (§19). The goal is trading with
controlled risk, not preventing trading.

**D-5 — One execution channel: gateway → Execution Bridge → MT5
(decided 2026-11-18 ~19:01; replaces the v2 two-plane formulation).**
The gateway is the **only sender** of orders: it sends all four
operations (open, modify, reduce-volume, close — CONTRACTS.md §7).
The Execution Bridge is the **MT5 adapter**: it receives gateway
commands, runs the pre-send `order_check`, executes `order_send`
against the terminal via the official MT5 Python API, reads
positions/orders/history for reconciliation, and reports every result
via §17.3. The Bridge never self-initiates an open and never invents
policy. Its only autonomous authority is **fast trailing / breakeven
inside a fresh policy envelope** (≤ 5 min, WS up): sub-minute SL moves
that cannot wait for a round-trip. Every such move is reported with
its rule id and reconciled against the envelope version; if the
envelope is stale or the WS is down, the Bridge holds (in-flight
trailing completes under the last envelope, then stops). Both the
gateway and the Bridge write to the **same command ledger**, so any
action can be replayed against the policy that authorized it. **Why
one channel, not two:** the user requires the Gateway itself to open
orders (decision 1, 19:01) — a local-only open path would leave the
gateway without opens and split the audit trail across two senders,
two lifecycles, and two reconciliation rules. The §17.5 close-out
already specifies the path to gateway opens (approval + allowlist +
demo proof + schema bump) and needs no contract rework; the
alternative (a second, local open path) would need a second execution
stack — local decision authority, local enforcement, local audit as
source of truth — built, tested, and reconciled forever. Entries are
decided on candle/session cadence, not per tick, so the gateway
round-trip is acceptable for opens; trailing — the only per-tick work
— stays local under the envelope.

**D-6 — Scale by queue depth, not by hope.** Each engine exposes its
queue depth + p95 latency (from the usage/audit tables); scaling
decisions (more workers, more servers) are made from those numbers. An
engine that cannot report its own depth is not shippable. (v1 D-5.)

**D-7 — A decision without a recorded reason is not executable.**
Every engine command carries `rule_id` + `risk_check` + `scenario_ref`
+ `decision_id`. The enqueue path rejects engine commands missing any
of them. The audit chain answers, for every trade: who (user/engine),
what, why (rule + scenario + factors at decision time), when, and what
happened (outcome + resulting P/L when the position closes).

### 2.3 The Execution Bridge (build, not adopt — verified gap)

No mature open-source FX/CFD engine with MT5 execution exists
(RESEARCH §7, verified 2026-11-18): Freqtrade and Jesse are
crypto-first (no MT5 driver; Jesse live trading is a paid plugin);
NautilusTrader has no MT5 adapter; backtrader is GPL-3.0 (borrow
ideas, not code). That gap is the basis for the **dedicated
Execution Bridge**: purpose-built on MetaQuotes' own Python package
plus the gateway this program already has (PR #107). The Bridge is
a named component with a defined interface (CONTRACTS.md §7): it
receives gateway commands; runs the pre-send `order_check`;
executes `order_send` against the terminal; reads
positions/orders/history for reconciliation; reports every result
with its retcode and fill. What is borrowed from the open-source
engines is **patterns, not code**: NautilusTrader's one message bus
+ risk-checks-every-order-before-it-leaves + execution-turns-replies-
into-events; Freqtrade's `Edge` sizing-from-expectancy and tiered
exits; Jesse's Monte Carlo trade-order shuffling and train/test
separation; riskfolio-lib / PyPortfolioOpt VaR–CVaR math. Anything
copied must be licence-compatible (MIT/BSD/Apache fine; LGPL-3.0
usable with conditions — dynamic linking, no relicensing of our
code; check before embedding). No open-source engine's storage is
adopted: the program is Postgres-only (§11), and the borrowed
patterns carry no SQLite/Redis/Parquet dependency with them.

### 2.4 Cooperation contract (how the eight engines stay in sync)

Parallel cooperation is the top priority — the engines are
concurrent consumers of one bus, not a sequential pipeline with a
single brain:

- **Fan-out, not fetch.** Ingest publishes one event; MAE, NSE, and
  FGE consume it in parallel; STE, PRE, and RCE join when their
  inputs are fresh. No engine fetches another engine's output
  directly (§4 rule 6); no engine recomputes what another engine
  already published.
- **Every output has a TTL.** `MarketRegime`: the symbol's timeframe.
  `ScenarioSet`: 1 minute. `RiskAssessment`: one decision only — it
  is never reused across decisions. `NewsImpact`: until its
  importance decays. `FearGreedState`: 1 hour, or until a behavior
  flag fires. `StrategySelection`: until the next regime change,
  session start, or daily refresh. A decision cycle uses only inputs
  younger than their TTL.
- **The decision cycle (per account).** Trigger (new batch, regime
  change, position event, news freeze) → MAE + NSE + FGE in parallel
  → STE selects → PRE computes scenarios → RCE assesses → decision
  record written → EXE executes → reconciliation. A stale input
  blocks the decision with reason `stale_input` (or the cycle
  proceeds in degraded mode with the staleness labelled — never
  silently).
- **AI joins only where reasoning is needed** (§5.2). The reasoning
  outputs (news interpretation, scenario narrative, nudge text) are
  async inputs to the *next* cycle, with their own TTLs. Nothing in
  this section waits for a model.

Two cross-cutting properties are designed in §13 and §14, not bolted on
later: **transparency** (every engine job reports its progress stages —
no silent thinking) and **scalability** (each service is separable and
stateless, so the core can spread across 5–6 servers behind a load
balancer).

## 3. The eight engines in detail

Each subsection states: responsibility, inputs → outputs (schema names
live in CONTRACTS.md), trigger, code-vs-AI split, latency budget, and
failure behavior. **Failure behavior is part of the contract:** an
engine that cannot produce its output reports `unavailable` with a
reason; it never emits a guessed value, and the loop proceeds in
degraded mode (defined per engine below).

### 3.1 MAE — Market Analysis Engine

**Responsibility.** Price, market structure, trend, liquidity, and
volatility per symbol + timeframe — the shared market picture every
other engine reads. MAE is the only engine that reads raw candles and
quotes; every other engine reads MAE's `MarketRegime`, never raw ticks.

- **Inputs:** candle batches + quote cache (ingest); `NewsImpact` (NSE)
  for event-adjusted volatility.
- **Outputs:** `MarketRegime` { symbol, timeframe, regime
  (trend/range/chop), trend direction + strength, ATR + ATR percentile
  vs 90d, liquidity note (spread state), S/R levels with strength,
  regime_confidence, computed_at, inputs_hash } → STE, PRE, RCE.
- **Trigger:** new candle batch; regime change; on demand (PRE asks for
  a fresh regime before a scenario run).
- **Code vs AI:** indicators, structure, ATR, S/R — code (existing
  `indicators.rs` + factors F-44..F-51 when they ship). AI (prompts
  MA-09 chart-pattern-reader, MA-14 trend-exhaustion, MA-27
  scenario-builder) may **name** a pattern and flag an unusual
  condition; it never sets a regime, a level, or a number.
- **Latency:** p95 < 50 ms / symbol (code path). AI reads are async
  and never block a regime computation.
- **Failure:** no candles → `MarketRegime.unavailable` with reason
  (`no_data`); STE falls back to the last regime with its age labelled,
  and no new entry is decided from a regime older than the symbol's
  timeframe (a stale regime blocks entries, never justifies them).

### 3.2 STE — Strategy Engine

**Responsibility.** Which versioned strategy (and which parameters)
applies to this account, today, this hour — and which symbols/sessions
are tradeable under it. STE does not invent strategies; it selects from
the **strategy registry** (§3.2.1) and applies the daily knowledge
snapshot (§7.3).

- **Inputs:** `MarketRegime` (MAE), factors (§6), daily knowledge
  snapshot (§7.3), user profile + risk profile, `BehaviorFlags` (FGE —
  e.g. a tilt flag narrows the session list).
- **Outputs:** `StrategySelection` { strategy_id, strategy_version,
  parameter_snapshot_id, allowed_symbols, allowed_sessions,
  max_simultaneous, size_hint (from RCE, not STE), valid_from,
  valid_until, reason } → PRE, RCE, PME.
- **Trigger:** regime change, session start, daily refresh, on demand.
- **Code vs AI:** selection is deterministic code (regime + profile +
  strategy eligibility rules). AI proposes **parameter changes** and
  **new strategy hypotheses** (prompts SG-*, MA-*); a proposal becomes a
  registry version only after the eval gate (§5.4) and a canary — the
  model never promotes itself.
- **Latency:** p95 < 100 ms.
- **Failure:** no eligible strategy → `StrategySelection.none` with
  reason (`no_strategy_for_regime`); the loop idles (manages existing
  positions only) instead of forcing a trade.

#### 3.2.1 Strategy registry (versioned, auditable)

```
strategies: id, name, family (scalp|swing|breakout|mean_reversion|...),
  version INT, rules_hash TEXT, rules JSONB (deterministic entry/exit rules),
  params JSONB (current parameter set), status (draft|eval|canary|promoted|retired),
  eval_set_id, eval_result JSONB, created_by, created_at, promoted_at
strategy_param_snapshots: id, strategy_id, version, params JSONB,
  data_timestamp, source_list JSONB, created_at   -- one per daily refresh
```

- A strategy version is **immutable**; editing rules creates a new
  version. Parameters change daily (§7.3) as new snapshots — the rules
  do not.
- Every trade references `strategy_id + strategy_version +
  parameter_snapshot_id` (stored on the decision record and the
  managed position), so any trade can be traced to the exact strategy
  version that produced it.
- **Seed strategies (from RESEARCH §6, in order):** #3 windowed EMA 9 +
  VWAP + RSI (first to encode — the most honestly documented);
  #1 EMA pullback; #2 VWAP pullback; #6 RSI mean reversion on gold
  (smallest complete rule set). The rest of the shortlist enters only
  after its eval set exists.
- **Rules change slowly, parameters change daily.** Entry rules change
  only after a weekly evaluation from accumulated R-multiples (§7.3);
  a daily refresh that rewrote entry rules would make the strategy
  untestable churn (RESEARCH §5.4).

### 3.3 PRE — Prediction Engine (scenarios, not forecasts)

**Responsibility.** For every candidate trade **before entry**, and for
every open position **on re-evaluation**, compute the scenarios that
decide the trade. PRE does not predict the future; it prices the
possibilities from real data.

**The scenario computation (code, Decimal, every number reproducible):**

| Question (user's §2) | How PRE answers it (code) |
|---|---|
| If price reaches TP, how much does the account grow? | `pnl_tp` in currency + R + % of equity and % of balance, from size × (TP − entry) − costs (spread/commission/swap) |
| If price reaches SL, how much of Equity/Balance is affected? | `pnl_sl` (negative) in currency + R + % of equity/balance; `equity_after_sl`, `balance_after_sl`; drawdown contribution |
| If price moves against us, what scenarios exist? | Adverse scenarios: SL hit; partial adverse move to the next S/R level (from MAE); gap/weekend move (historical gap distribution); news shock (importance-3 event in window → widened adverse band) |
| Probability of reaching each price level? | **Historical frequency, not a model guess:** for this symbol + session + regime, how often price reached level X within horizon T — computed from stored candles (factor F-68) with `n` shown. `n < 30` → probability reported as `insufficient_data`, never as a number. |
| When to close or manage, given current conditions? | `re_evaluate_at` (time/price trigger) + `manage_triggers[]` (price levels that change the management action) |
| New trade vs manage existing positions? | PRE computes the ScenarioSet for the candidate **and** a refreshed ScenarioSet for each open position in the same symbol/direction; RCE compares expected value per unit of risk (EV/R) and PME acts on the better one. The comparison is stored on the decision record. |

- **Inputs:** candidate/position, `StrategySelection` (STE),
  `MarketRegime` (MAE), historical distributions (factor store:
  F-68 level-reach probability, F-69 adverse-move distribution, F-70
  session ATR distribution), account state (balance, equity, margin,
  open exposure).
- **Outputs:** `ScenarioSet` { scenarios[] (name, trigger_price,
  probability + n, pnl currency/R/%equity, equity_after,
  balance_after), expected_value_R, worst_case (SL), best_case (TP),
  time_to_decision, manage_triggers[], recommendation
  (enter|manage_existing|wait|skip) + reason, confidence (sample n),
  computed_at, inputs_hash } → RCE, PME, decision record. Schema in
  CONTRACTS.md §4.3.
- **Trigger:** before every entry decision; on position state change;
  periodic re-eval (per position, at most once per minute server-side;
  sub-minute re-eval is the local agent's job under the policy
  envelope).
- **Two probability methods, one conservative rule.** The primary
  probability is the counted historical frequency (F-68). The
  cross-check is a Monte Carlo price-path simulation (10,000 paths
  from ATR-scaled volatility — the riskfolio/NumPy pattern, RESEARCH
  §7 E3) giving the probability of reaching TP / SL / each level.
  When the two disagree materially (outside the Monte Carlo's own
  confidence band), the ScenarioSet reports the disagreement and the
  **lower (more conservative) probability governs** the decision. A
  third check — trade-order shuffling (Jesse Monte Carlo) — runs in
  the weekly strategy review to test whether a strategy's result
  depends on the sequence of trades, not on the price paths.
- **Code vs AI:** **all numbers are code.** AI (prompts PR-01..PR-08,
  §5.6) writes the narrative explanation and flags unusual conditions
  ("this setup has no precedent in the last 90 days"); it never sets a
  probability, a P/L, or a recommendation.
- **Latency:** p95 < 100 ms / position (code path).
- **Failure:** insufficient history → probabilities
  `insufficient_data`; the scenario set still ships with the
  deterministic bounds (TP/SL P/L) and recommendation `wait` unless
  the deterministic case alone justifies entry (RCE decides, with the
  gap labelled).

### 3.4 RCE — Risk & Capital Engine (active optimizer)

**Responsibility.** Turn a ScenarioSet into the best risk-adjusted
action: size, SL/TP, portfolio impact — and, when the trade cannot be
taken as proposed, the best alternative. RCE is **not a brake**: it
optimizes within the budget and vetoes only on hard limits.

- **Inputs:** `ScenarioSet` (PRE), account state, open exposure
  (PME), risk profile + grant limits, `RiskFreePolicy` (§3.5.1).
- **Outputs:** `RiskAssessment` { approved (bool), size_lots,
  sl, tp, max_loss currency/R/%equity, portfolio_impact
  (exposure after, margin after, drawdown budget remaining),
  veto_reason (null or hard-limit id), **alternative**
  (reduce_size|wait_for_level|different_symbol|manage_existing|none
  — always present when vetoed), risk_check JSON (fills the staged
  `connector_commands.risk_check` column: {rule_id, checks_passed,
  bounds}), computed_at, inputs_hash } → PME, EXE, decision record.
- **Trigger:** every entry/manage decision.
- **Code vs AI:** **code only.** RR prompts (§5.6) review and explain
  a risk assessment after the fact; they never compute it and never
  veto.
- **Latency:** p95 < 20 ms.
- **The veto rule (D-4, enforced by test):** a veto must name a hard
  limit **and** an alternative. Veto reasons are enumerated:
  `margin_floor`, `daily_loss_limit`, `kill_switch`, `symbol_blocked`,
  `max_simultaneous`, `grant_exhausted`. Anything else is not a veto —
  it is a sizing or timing adjustment (reduce size, wait, manage
  existing). A bare "no" is a bug.
- **Optimizer toolkit (the active part).** Sizing and thresholds are
  modulated, not just gated (RESEARCH §7 E4): fractional Kelly
  (fraction 0.25–0.4, never full Kelly — F-27 stays informational
  and the cap is a hard bound); fixed fractional 0.25–0.50% of
  capital per trade as the baseline every other method is compared
  against; ATR-scaled stops (never a fixed pip number);
  regime-conditional thresholds (the entry bar tightens/loosens by
  regime, with the threshold itself versioned per strategy);
  portfolio heat (stake per trade shrinks as open positions
  increase; correlation-aware sizing via F-32); account-level
  VaR/CVaR (95% / 99% + expected shortfall) as a pre-trade account
  check. **Two layers:** the active modulation above, and underneath
  it a separate safety layer (daily loss cap −2R / −1% of capital,
  kill-switch) that only ever stops, never optimizes. A risk engine
  that only says "no" is a brake; this one modulates first and gates
  only at the safety layer.
- **Failure:** account state unavailable → no assessment, no trade
  (fail-closed for entries); existing positions keep being managed
  under the last assessment + PME rules.

### 3.5 PME — Position Management Engine

**Responsibility.** Own the lifecycle of **every open position** on the
account — including positions the engine did not open. PME is the
engine that makes "manage the account" real.

**Position lifecycle (state machine, CONTRACTS.md §6):**

```
unmanaged (seen in sync, not yet adopted)
  → adopted (PME owns it: strategy ref assigned or "unassigned",
     RiskFreePolicy attached, management rules attached)
  → managing (normal: monitoring, trailing per rules)
  → protecting (Risk Free active: SL at/above breakeven, partials taken)
  → scaling (adding/reducing volume per plan)
  → closing (close/partial in flight via EXE)
  → closed (terminal; outcome feeds factors + behavior + coaching)
```

- **Inputs:** `PositionState` (positions sync batch), `ScenarioSet`
  (PRE, refreshed), `RiskAssessment` (RCE), `NewsImpact` (NSE —
  freeze windows), `FearGreedState` (FGE — protection delta).
- **Outputs:** `ManagementAction` { ticket, action
  (hold|breakeven|partial_close|trailing|scale_in|scale_out|close|
  modify_sltp), volume (for partials), sl, tp, rule_id, reason,
  decision_id } → EXE. And `PositionState` updates → PRE, RCE.
- **Trigger:** 1-minute server loop over open positions; position
  events (opened/closed/modified in sync); news freeze events.
- **Code vs AI:** rules are code (§3.5.2). TS prompts explain a move
  to the user (TS-08) and postmortem it (TS-09); they never decide it.
- **Latency:** p95 < 50 ms / position / cycle.
- **Adoption (positions the engine did not open):** on first sight in
  a positions batch, a position is `unmanaged`. PME adopts it on the
  next cycle: assigns the current strategy (or `unassigned` if none
  fits), attaches the account's RiskFreePolicy, and — per the adoption
  policy (O-17) — either starts managing it immediately or flags it
  for the user first. An adopted position is managed exactly like an
  engine-opened one; the decision record marks `adopted: true` and the
  origin (`engine|user|unknown`). PME never closes an adopted position
  without a rule + decision record, and never assumes it knows the
  user's intent for it — the first management action on an adopted
  position is protective (breakeven/partial per policy), never an
  aggressive scale-in.

#### 3.5.1 Risk Free policy (configurable, user-set)

The user asked for an **optional, configurable** Risk Free priority.
It is a first-class policy object, not a hardcoded rule:

```
risk_free_policies: account_id, enabled, protection_level (0-100),
  trigger_R (default +1R), partial_pct_at_trigger (default 50),
  breakeven_buffer_pips, trailing_mode (atr|r_steps|off),
  trailing_start_R, max_protection_R, updated_at
```

- `protection_level` is the user's dial: 0 = off (normal TP/SL rules
  only); 100 = maximum protection (breakeven at +1R, 50% partial,
  trailing from +1R). Intermediate levels interpolate the trigger and
  the partial fraction. The user sets the level; FGE may adjust it
  **within the user's set bounds** (never beyond them) when behavior
  flags fire.
- When enabled and a position reaches `trigger_R`: SL → entry price
  (+ buffer), `partial_pct_at_trigger` of the volume closes (via the
  gateway reduce-volume operation — gated until the §17.5 close-out
  in Phase 2b; in 2a the trigger moves SL only and records the
  partial as a proposal), the remainder trails per `trailing_mode`.
- If market conditions change (regime flip, importance-3 news), PME
  re-evaluates: an earlier exit is assessed by PRE (scenario refresh)
  and executed only with a decision record — never as a panic close.
- Default level is open question O-16; the policy object, the dial,
  and the bounds exist regardless of the default.

#### 3.5.2 Management rules (TP/SL — v1 §5, kept and owned by PME)

| Rule | Trigger | Action | Parameters (per symbol, from history) |
|---|---|---|---|
| R-TP-1 Trailing (ATR) | Price moves +k×ATR in favour | Move SL to entry + m×ATR, ratchet only | k, m from TS-15 tuner; ATR from `indicators.rs` |
| R-TP-2 Trailing (R-steps) | Trade reaches +1R, +2R, +3R | Move SL to breakeven, then +1R, then +2R (lock-in ladder) | R steps fixed: 1/2/3R; never move SL backwards |
| R-TP-3 Breakeven | Trade reaches +1R (or Risk Free trigger) | SL → entry price (+ spread buffer in pips from `risk/specs.rs`) | buffer in pips per symbol |
| R-TP-4 Time decay | Position age > N hours without reaching +1R | Tighten TP toward current price / tighten SL to breakeven | N per symbol class (scalp 2h, swing 48h) |
| R-TP-5 Volatility adjust | ATR regime changes (MA-03) | Widen/narrow SL/TP by ATR multiple (within bounds §3.5.3) | ATR multiple clamp |
| R-TP-6 News freeze | Importance-3 event within window (§7) | Freeze all TP/SL moves; optionally tighten SL to breakeven pre-event | window minutes (default 30 before / 15 after) |
| R-TP-7 Session close | Illiquid hours approaching | Tighten SL to breakeven or propose close (suggest_only) | session map per symbol |
| R-TP-8 Kill-switch tighten | Kill-switch trips (§10) | Tighten every SL to breakeven immediately (or close, only if the user pre-authorized close-all) | — |

#### 3.5.3 Safety bounds (enforced in code, not in prompts)

- **Trailing is a per-strategy test result, never an assumed good.**
  One documented strategy removed trailing stops after backtests
  showed they "destroyed winners" (RESEARCH §7 E5). R-TP-1 / R-TP-2
  are enabled per strategy version only when that strategy's own
  tests support them; the default is breakeven + partial (R-TP-3 +
  partial), trailing opt-in per version.

1. **SL never moves against the trade.** A proposed SL worse than the
   current SL is rejected by the validator before any command is built.
   (Long: new SL > old SL; short: new SL < old SL.)
2. **SL never widens beyond the risk profile.** New SL distance ≤
   `max_risk_per_trade_pct` of equity (from `account_snapshots` +
   `risk/specs.rs`). Widening is only allowed under R-TP-5 and only
   inside this bound.
3. **Minimum distance from price.** SL/TP must be ≥ the broker's stops
   level + a safety buffer in pips (per-symbol, `risk/specs.rs`).
4. **Rate limit.** At most one `modify_sl_tp` per position per 60
   seconds (server-side), and at most N per account per hour (profile
   parameter, default 20). Prevents tick-storms and runaway loops.
5. **TP only moves by rule, never by hope.** TP moves require a rule
   id + a computed reason; "let it run" is not a rule.
6. **Every move is validated twice:** once by PME (bounds above) and
   once by the existing `validate_operation` (finite SL/TP) + the
   `risk_check` column (filled by RCE, §3.4) before enqueue.
7. **Partial-close volume is validated against the open volume**
   (MQL5: oversize volume closes in full — PME clamps to open volume
   minus the broker's minimum, and never issues a partial that would
   leave dust below `volume_min`).

### 3.6 NSE — News & Sentiment Engine

**Responsibility.** News, economic calendar, and sentiment → impact per
symbol and per open position, with freeze windows. NSE turns the daily
information flow into engine inputs — not into stored articles.

- **Inputs:** news feed (FXStreet, ForexLive, CoinDesk, CoinTelegraph
  — already wired), economic calendar (MT5 calendar primary; Finance
  Calendar / FCS / biquote candidates — RESEARCH §2.2), TradingView
  MCP outlook (research only, §16).
- **Outputs:** `NewsImpact` { item/event, importance 0–3 +
  importance_reasons, symbols[], direction lean per symbol,
  freeze_window {symbol, from, to}, affected_positions[] } → MAE, STE,
  PME, RCE. Importance rules in §7.1.
- **Trigger:** ingest event; calendar event approaching.
- **Code vs AI:** importance + freeze windows are **code** (§7.1
  rules); lexicon sentiment is deterministic code. AI (prompts NT-*)
  triages, tags, summarizes, and explains ("why this is very
  important"); it never sets an importance level or a freeze window.
- **Latency:** async; < 5 s / item.
- **Failure:** feed down → last impact snapshot stands with its age
  labelled; freeze windows from the calendar (which is local to MT5)
  still apply — a news-feed outage never silently lifts a freeze.

### 3.7 FGE — Fear & Greed Engine

**Responsibility.** Detect and adjust the balance between
risk-taking and capital protection — from **this user's own behavior**,
never from a generic script.

- **Inputs (six proxy families — internal only):** volatility (ATR
  regime, F-45); price behavior (early exits F-36,
  momentum/mean-reversion reads); drawdown (current DD vs max F-04,
  drawdown velocity F-33); liquidity (spread state, F-48); news (NSE
  impact on open positions); trading behavior (revenge index F-34,
  SL-moving rate F-37, tilt score F-41, frequency vs baseline F-06).
  Plus `PositionState` and `RiskAssessment`. No external fear/greed
  index is an input — none exists for intraday FX/gold
  (alternative.me is BTC-only and daily; CNN is US-stocks with no
  public API; VIX is equities-implied — RESEARCH §7 E7). Public
  indices are regime context only, never a decision input.
- **Outputs:** `FearGreedState` { state (balanced|fearful|greedy|
  tilted), evidence[] (factor refs + trade refs), protection_delta
  (adjustment to the account's RiskFreePolicy level, within user-set
  bounds), size_modulation (size multiplier for RCE, e.g. 0.5 when
  tilted), nudge (text, AI-written, evidence-tagged), intervention
  (null or a PME action recommendation) } → PME, RCE, coaching.
  **FGE is a decision input, not a display number:** the dashboard
  may show the state, but its purpose is the protection delta and
  the size modulation that shape the next decision — and every
  decision record carries `fear_greed_ref` so the influence is
  traceable.
- **Trigger:** position events, behavior flag, periodic (hourly).
- **Mechanism — decided (narrows O-11):** **both, with a written
  boundary.** (a) **Mechanical protection is automatic** once the
  account's Risk Free policy is enabled: breakeven / partial / trailing
  are the plan's own rules (§3.5.1) and fire on price, no judgment
  involved. (b) **Judgment is coached, and intervened on only in auto
  mode with permission:** a nudge fires when the user (or the engine)
  is about to act against their own plan (early exit, SL moved against
  the trade, size escalation); in `auto_execute` mode with the
  intervention permission enabled, PME may execute the protective
  action the nudge recommends (e.g. breakeven an out-of-strategy
  position) — always with a decision record citing the behavior
  evidence. What remains open (O-11, narrowed): the exact thresholds
  (tilt score value, early-exit count) and the default intervention
  permission (off until the user enables it).
- **Code vs AI:** proxies, thresholds, protection delta — code. Nudge
  text — AI (BG prompts), labelled `AI_INTERPRETED`, carrying its
  evidence (the trades it cites), never presented as a fact about the
  user's emotions.
- **Latency:** p95 < 50 ms.
- **Failure:** too little data → `insufficient_data`; FGE emits
  nothing (no state, no nudge) rather than a generic reading.

### 3.8 EXE — Execution Engine (the only engine that touches the terminal)

**Responsibility.** Take a decided action and make it real on the
user's MT5 terminal — exactly once, verifiably, with recovery. EXE is
the **single execution channel** (§2, D-5): the gateway sends, the
Execution Bridge executes, one ledger.

- **Inputs:** `ManagementAction` (PME), open decisions (STE+PRE+RCE
  chain, with decision record), `PolicyEnvelope` (server-issued bounds
  for the local plane).
- **Outputs:** `ExecutionResult` { command_id, action, ticket,
  status (enqueued|delivered|completed|failed|reconciled),
  broker_result (retcode, fill price/volume when known),
  order_check_result (pre-send preview, recorded, never a guarantee),
  latency_ms, decision_id } → reconciliation, decision record,
  factors. Wire shapes in CONTRACTS.md §7.
- **Server EXE (gateway):** sends all four operations — `open_order`,
  `modify_sl_tp`, `close_position` (with `volume` for reduce-volume),
  `close_position` (full) — via `POST /api/v1/connector/commands`
  (wire shapes in CONTRACTS.md §7). `open_order` and the
  `volume` form of `close_position` are **gated until the §17.5
  close-out lands in Phase 2b (demo)**; the gate is the
  `validate_operation` allowlist + a phase flag, not a second code
  path. Enqueue path: validate operation (finite SL/TP, sendable in
  the current phase) → check idempotency (`command_id` UNIQUE; a
  retry replays the stored result, never re-sends) → check duplicate
  (same ticket + action already pending → refuse with
  `duplicate_pending`) → fill `risk_check` (RCE) + `rule_id` +
  `scenario_ref` + `decision_id` → enqueue with TTL. The pre-send
  `order_check` runs Bridge-side (§3.8, Execution Bridge), not here.
- **Execution Bridge (local, the MT5 adapter):** receives gateway
  commands and executes them against the terminal via the official
  MT5 Python API (`order_send` for DEAL / PENDING / SLTP / MODIFY /
  REMOVE / CLOSE_BY; `positions_get` / `orders_get` /
  `history_orders_get` / `history_deals_get` for reads). It never
  self-initiates an open. Its only autonomous authority is
  sub-minute trailing / breakeven **inside a fresh policy envelope**
  (≤ 5 min, WS up); every such move is reported via §17.3 with
  `rule_id` + `decision_id`, and the server reconciles it against
  the envelope version. Envelope stale or WS down → hold.
- **Pre-send `order_check` (Bridge-side, mandatory).** Before any
  `order_send`, the Bridge runs MT5 `order_check` — the pre-trade
  validation that returns the post-trade balance / equity / margin
  preview **without sending**. An open that fails `order_check`
  (insufficient margin, invalid volume, market closed) is refused
  before it reaches the broker, and the refusal (with the check
  result) is reported as the command outcome. **A passing
  `order_check` is never a guarantee of successful execution:**
  requotes, disconnects, and market moves can still fail the send —
  the result only proves validity and probable effect at check time.
  Send retcodes: 10009 done, 10008 placed, 10010 partial. A `None`
  result or retcode 10031 (no connection) does **not** mean the
  order failed — reconcile from `positions_get` /
  `history_deals_get` before doing anything else, or the trade opens
  twice.
- **Code vs AI:** **code only. Zero prompts. Zero AI.** No prompt
  family exists for EXE by design (§5.6).
- **Latency:** enqueue p95 < 50 ms; decision-to-send p95 < 1 s
  (decision `created_at` → command enqueued); broker response time
  measured per command (delivered → `action.result`, §12); delivery
  bounded by command TTL (5 min default); local trailing reacts per
  tick within the envelope.
- **Duplicate prevention (code, before enqueue):** (1) same ticket +
  same action already `pending`/`delivered` → refuse; (2) an open
  decision for a symbol+direction+strategy that already has an open
  position or a pending open → refuse with `duplicate_position`
  (unless the strategy explicitly allows scaling — then it is a
  `scale_in` action with its own rule, not a duplicate); (3) an action
  incompatible with the current position state (e.g. close on a
  ticket already closed) → refuse with `stale_state` after a
  reconciliation read. A failed send is never blindly resent:
  reconcile from positions/history first (§8 recovery matrix).
- **Reconciliation (the loop that makes execution trustworthy):**
  every positions sync batch is compared against the command ledger
  (CONTRACTS.md §8). Reconciliation runs only against a **complete**
  book: the positions batch carries a `book_read` completeness flag,
  and an incomplete batch never resolves a command. A command whose
  terminal outcome is unknown
  after TTL + one sync cycle is marked `reconciled` by comparing
  terminal state (position exists/closed, SL/TP values, volume) —
  never re-sent blindly. A terminal state that matches no command is
  an **unmanaged position** → PME adoption (§3.5). A command the
  terminal contradicts (e.g. reported completed but the position is
  still open with the old SL) is flagged `diverged` and escalated to
  the user + admin; the engine stops acting on that ticket until a
  human or a fresh reconciliation resolves it.
- **Recovery after disconnect / unclear broker result:** the recovery
  matrix (CONTRACTS.md §8) is explicit per state: `pending` + WS down
  → hold, do not re-send, resolve on reconnect by terminal state;
  `delivered` + no `action.result` → wait one sync cycle, then resolve
  by terminal state; broker retcode unclear → treat as unknown, never
  as success, resolve by terminal state; `order_check` refusal → the
  command ends `failed` with reason `order_check_failed` + the check
  result attached, and nothing is sent; local agent restart → reload
  the last envelope + pending local actions from the server ledger,
  never from local memory alone. **The rule: when the outcome is
  unknown, the engine asks the terminal — it never guesses and never
  re-sends.**
- **Failure:** gateway down → commands queue server-side with TTL;
  expired commands are marked `expired`, never sent late. Bridge
  offline → server EXE still closes/modifies nothing (no path to the
  terminal); PME keeps computing and queues actions with a
  `bridge_offline` flag, visible in the dashboard.

### 3.9 Market data freshness — the rule for every engine

No market data enters a trading decision as live without a timestamp
and a freshness check (user decision 4, 19:01). Every market input
carries `as_of` (the data's own timestamp), `received_at`, `source`,
and `delay_class`:

| Source | Delay class | Real delay (verified) | May feed a trading decision? |
|---|---|---|---|
| MT5 / broker feed (via the Bridge) | `live` | ~0 (the execution feed) | Yes — primary for FX/gold |
| Binance public klines | `live` | seconds | Yes — primary for crypto |
| TradingView official MCP | `delayed-research` | Delayed — official docs state only "delayed", no number; community measurements report 15+ minutes | **No** — daily loop / outlooks only |
| TradingView widgets / scanner endpoint | `delayed-research` | Delayed / undocumented | **No** — display / secondary only |
| Yahoo Finance (unofficial endpoint) | `best-effort` | Unknown, actively rate-limited/blocked 2025–2026 | **No** — display only |
| er-api (FX rates) | `delayed-research` | Minutes | No — FX-rate fallback for display/conversion only |
| MT5 economic calendar | `live` | Scheduled times are exact; actuals arrive at release | Yes — freeze windows, impact |

**The freshness rule (code, enforced at PRE/RCE input assembly):**
a market input is usable for a decision only if
`now − as_of ≤ max_age` for that decision's timeframe (bars: the
timeframe length, e.g. M15 bars ≤ 15 min; ticks: ≤ 5 s; calendar
events: the event time itself). A stale input blocks the decision
with reason `stale_data`, or the cycle proceeds in degraded mode
with the staleness labelled on the decision record — never silently.
MAE labels every `MarketRegime` with the `as_of` of its newest input;
PRE records the `data_vintage_ts` of the inputs it used; the
decision record carries both, so any decision can be replayed
against exactly the data it saw. A source whose delay is unknown or
unmeasured is `best-effort` by default and feeds no decision until
its delay is measured.

## 4. Event bus + inter-engine contracts (summary)

The full contracts live in `CONTRACTS.md`. This section states the
rules the bus enforces; the file states the shapes.

1. **Transport.** Postgres queue + `FOR UPDATE SKIP LOCKED` claim
   (the existing pipeline pattern, §1) for server-side events; WS
   frames for server↔Bridge events (contract §17.1). No direct
   HTTP calls between engines — an engine emits an event, the bus
   delivers it, the consumer claims it. A slow engine backs up in its
   own queue instead of blocking the caller.
2. **Envelope.** Every event: `event_id` (uuidv7), `event_type`,
   `account_id`, `symbol` (nullable), `ticket` (nullable), `payload`
   (versioned JSON, `payload_version` int), `emitted_by` (engine id),
   `emitted_at`, `correlation_id` (the decision or position the event
   belongs to), `idempotency_key`. Payload schemas are versioned;
   a consumer that cannot parse a payload version marks the event
   `failed` with reason, never guesses.
3. **Event catalog (condensed — full table in CONTRACTS.md §3):**
   `market.regime_changed`, `market.candles_ingested`,
   `strategy.selected`, `prediction.scenarios_computed`,
   `risk.assessed`, `position.opened|modified|closed`,
   `position.adopted`, `position.management_action`,
   `news.impact`, `news.freeze_started|ended`,
   `behavior.flag`, `feargreed.state_changed`,
   `execution.command_enqueued|delivered|completed|failed`,
   `execution.reconciled`, `execution.diverged`,
   `engine.kill_switch_tripped`, `engine.decision_recorded`.
4. **The decision record (CONTRACTS.md §5).** Every decision that can
   move money is a row before any command exists: `decision_id`,
   `account_id`, `engine_chain` (which engines contributed, in order),
   `action`, `inputs_hash`, `scenario_ref`, `risk_ref`,
   `strategy_ref` (id+version+parameter snapshot), `knowledge_version`
   + `data_vintage_ts` (what the engine knew, and when — §7.3),
   `fear_greed_ref`, `rule_id`, `reason`, `command_id` (nullable),
   `outcome` (nullable until the position closes), `latency_ms` per
   engine stage, `decision_to_send_ms`, `broker_response_ms`,
   `created_at`. It extends the existing `ai_agent_decisions` row
   (§17.3) with `engine_chain`, `scenario_ref`, `inputs_hash`,
   `knowledge_version`, `data_vintage_ts`, `fear_greed_ref`, and
   per-stage latency — one table, not two.
5. **Schemas are contracts, not documentation.** Every inter-engine
   message is validated against its JSON Schema on receipt (the same
   validation the prompt reliability ladder uses for model output).
   A message that fails validation is rejected with a named reason and
   the emitting engine is flagged — silent schema drift is a bug.
6. **No engine reads another engine's database tables directly.**
   Engines communicate through events + the decision record + the
   shared read models (`factor_runs`, `managed_positions`,
   `strategy_param_snapshots`). Direct table reads across engine
   boundaries are forbidden — that is what "no islands" means in code.
7. **The order trace (end-to-end audit, one query).** For every order,
   the full chain is traceable: decision → command → `order_check`
   result → broker execution (retcode, fill price/volume) → real
   result → position status → subsequent management (CONTRACTS.md
   §5, `OrderTrace`). One API call returns the whole trace:
   `GET /api/v1/connector/orders/{ticket}/trace`. The trace is the
   unit of audit — every order has exactly one trace, append-only,
   and a gap in it (e.g. a command with no broker result and no
   reconciliation) is itself a flagged anomaly.

## 5. Prompt-mastery system (~213 prompts, deterministic) + bounded AI

### 5.1 What "deterministic" honestly means

LLMs are not bit-deterministic, even at temperature 0. The system
therefore does **not** promise identical tokens; it promises
**deterministic decisions**: for a fixed prompt version + fixed inputs,
the output always (1) parses against the same JSON Schema, (2) carries
the same discrete decision fields (e.g. `action: hold|enter|exit`,
`confidence: low|medium|high`), and (3) quotes only numbers the server
computed and handed to it. Non-determinism is confined to prose. The
eval harness (§5.4) measures exactly that: schema pass rate,
decision-field agreement across runs, and forbidden-content rate
(invented numbers, missing epistemic tags).

### 5.2 Where AI is allowed — and where it is forbidden

AI is used **only** for work that actually needs reasoning. The call
sites are enumerated; anything not on this list does not call a model:

| Allowed (reasoning) | Prompt families | Forbidden (code only) |
|---|---|---|
| News/event interpretation, importance explanation | NT-* | Importance level, freeze windows, sentiment score |
| Strategy parameter proposals, new strategy hypotheses | SG-* (proposals), MA-* | Strategy selection, rule changes (code + eval gate) |
| Scenario narrative + unusual-condition flags | PR-* (§5.6) | Scenario numbers, probabilities, P/L, recommendation |
| Risk review explanation (after the fact) | RR-* | Sizing, SL/TP, veto, risk_check |
| TP/SL move explanation + postmortem | TS-08/TS-09 | The move itself, bounds, rate limits |
| Coaching narrative, behavior nudges | CO-*, BG-* | Factor values, behavior flags, intervention decision |
| Operations text (digests, changelogs, support drafts) | OP-* | Any execution, any number |

**Rules:**
- **The model never computes numbers.** It interprets numbers the
  server computed and handed to it (existing `coach.rs` rule, extended
  to every family).
- **The model is never called per tick and never for a simple
  computation.** A model call needs a reason recorded on the call
  (which decision it informs); a call with no linked decision is a
  bug. Sizing, SL/TP math, scenario math, and probability math are
  code — calling a model for them is forbidden, not wasteful.
- **AI is never in the critical order-send path** (§1.1.7). AI outputs
  are async inputs to the *next* decision cycle, with a TTL; if the
  model is unavailable or slow, engines proceed with code-only
  defaults in degraded mode (labelled as such on the decision record).
- **Every AI request is measured for value, not just cost.** The
  existing `ai_usage_events` row (tokens, cost, latency) gains a
  `value` link: `linked_decision_id` + `changed_decision` (bool — did
  the output change a decision field vs the code-only default) +
  `value_note`. A prompt family whose outputs never change a decision
  over 30 days is reviewed for retirement. Token, cost, latency, and
  value are reported per engine per week (OP-16 cost report extended).

### 5.3 Library layout (extends what exists, does not replace it)

```
ai_prompt_templates (existing table, migration 0020)
  engine_key  TEXT      -- prompt family, e.g. "signal_eurusd_m15"
  version     INT       -- UNIQUE (engine_key, version); immutable once published
  system_prompt / user_prompt_template  (≤20k chars each)
  notes       TEXT

prompt_jobs (one row per prompt in the inventory, §5.6)
  prompt_id   TEXT PK   -- e.g. "MA-014" (stable, human-readable)
  job         TEXT      -- market_analysis | signal | news | risk | coaching | tpsl | ops | prediction | behavioral
  engine_key  TEXT      -- FK to ai_engine_configs.engine_key (many prompts share an engine)
  template_id UUID      -- pinned ai_prompt_templates row (exact version in use)
  model       TEXT      -- pinned model id from ai_model_catalog (NULL = engine default)
  temperature REAL      -- 0 for all decision prompts; ≤0.3 for prose-only prompts
  max_tokens  INT
  input_schema  JSONB   -- JSON Schema of the inputs the prompt consumes
  output_schema JSONB   -- JSON Schema of the output (validated on every call)
  eval_set_id UUID      -- golden set that must pass before promotion
  status      TEXT      -- draft | eval | canary | promoted | retired
  owner       TEXT      -- who is accountable for this prompt's behavior

prompt_eval_runs
  prompt_id, template_version, model, run_at, n_cases, schema_pass, decision_agreement,
  invented_number_hits, verdict (pass|fail), notes

prompt_outputs (audit, not a log)
  prompt_id, template_version, model, input_hash, output_json, decision_fields,
  latency_ms, created_at, linked decision_id / command_id / analysis_id

prompt_benchmarks (§5.7)
  prompt_id, template_version, model, eval_set_id, weight, components JSONB,
  n_cases, run_at
```

Rules:
- **A prompt version is immutable.** Editing a prompt creates a new
  version; the old version stays addressable.
  `ai_engine_configs.prompt_template_id` pins the exact version in
  production; NULL (latest) is forbidden for any prompt that can move
  money.
- **Every prompt has an output schema.** No free-prose outputs in the
  money path. Prose lives inside schema fields (`rationale: string`,
  max length).
- **Every prompt run is recorded** with `template_version`, `model`,
  and `input_hash`, so any decision can be reproduced: same version +
  same inputs → same schema + same decision fields (see §5.4 for the
  tolerance).
- **Code-owned defaults stay in `ai_gateway/prompts.rs`** as the
  fallback when no DB template is pinned; DB templates win when
  assigned (existing rule, unchanged).
- **Fencing and epistemic rules are inherited verbatim** from
  `prompts.rs`: user/market content is fenced DATA, never
  instructions; every finding carries one of `OBSERVED / CALCULATED /
  AI_INTERPRETED / ASSUMED / UNAVAILABLE`; never fabricate a number
  that was not provided.
- **Temperature 0 for every decision prompt.** Prose-only prompts
  (coaching narrative, news summaries) may use ≤0.3 and must still
  pass schema validation. The `temperature` column is part of the
  prompt's identity — changing it is a new version.

### 5.4 Eval harness (how a prompt earns the right to ship)

A prompt moves `draft → eval → canary → promoted` only by passing
gates:

1. **Schema gate.** 100% of eval-set outputs must parse and validate
   against `output_schema`. One failure = the prompt does not ship.
2. **Determinism gate.** Each eval case is run **5 times**
   (temperature 0, same model, same inputs). All decision fields
   (enums, booleans, bounded numbers) must agree on **≥4 of 5 runs**;
   prose may vary. Below that, the prompt is reworded or the decision
   is moved to code.
3. **Honesty gate.** Zero invented numbers across the eval set: every
   number in the output must appear in the inputs (checked
   mechanically) or be labelled `ASSUMED` with the assumption stated.
   Zero missing epistemic tags.
4. **Adversarial gate.** Eval cases include: empty history, 1-trade
   history, contradictory inputs, a news item with an instruction
   embedded in it ("ignore previous instructions…"), and a truncated
   provider reply. The prompt must fail safe (named `unavailable` /
   `hold`), never act.
5. **Canary.** A promoted prompt first serves **5% of eligible
   requests** (or a fixed admin-chosen cohort) for at least 7 days;
   decision-field disagreement vs. the previous version is tracked.
   Rollback is one admin action: re-pin `prompt_template_id` to the
   previous version.
6. **Regression suite.** Every promotion re-runs the full eval suite
   of every prompt in the same job family. A prompt never ships alone
   without its family's suite green.

Eval sets are **golden, versioned, and human-written**: each case is a
real (anonymized) or synthetic-but-plausible input bundle with the
expected decision fields recorded. Writing them is a trading decision,
not a coding task — the user (or a trader they trust) signs off the
signal/coaching golden sets. See open question O-4.

### 5.5 AI-driven vs prompt-driven (the user's split, made concrete)

- **Prompt-driven (deterministic shell):** the prompt's job is fixed,
  its inputs are assembled by code, its output schema is fixed, and
  its decision fields are enumerated. The model fills the shell.
  Applies to: scenario narrative, news triage, risk review, TP/SL
  explanations, behavior nudges. If the model disagrees with the
  deterministic factors, the factors win and the disagreement is
  logged.
- **AI-driven (bounded exploration):** the model is allowed to propose
  structure the code did not predefine — e.g. coaching narrative, new
  factor hypotheses, pattern descriptions, strategy parameter
  proposals. These outputs are **advisory only**, carry
  `AI_INTERPRETED` tags, and can never trigger an execution or change
  a number. A human (admin) promotes a good AI-proposed hypothesis
  into a coded factor or a prompt-driven prompt; the model never
  promotes itself.
- **The line is enforced in code, not in prose:** execution paths only
  accept outputs of prompt-driven prompts with a passing schema;
  AI-driven outputs are routed only to read surfaces (dashboard,
  coaching notes) and to the eval-gated promotion path (§5.4).

### 5.6 Inventory: ~213 prompts by job (v1's 205 + PR family)

Conventions: `MA` market analysis (MAE), `SG` strategy candidates
(STE), `NT` news triage (NSE), `RR` risk review (RCE, explain-only),
`CO` coaching, `TS` TP/SL explanation (PME), `OP` operations, `BG`
behavioral (FGE), `PR` prediction narrative (PRE). Counts: MA 30, SG
30, NT 25, RR 30, CO 40, TS 15, OP 30, BG 5, PR 8 = **213**.
Prompts marked ★ are the Phase 2a seed set (30 prompts, §10).
**EXE has no prompts by design** — the execution path is code-only
(§3.8).

The v1 inventory (§3.6 of v1: MA-01..MA-30, SG-01..SG-30, NT-01..NT-25,
RR-01..RR-30, CO-01..CO-40, TS-01..TS-15, OP-01..OP-30, BG-01..BG-05)
is carried over unchanged, with two re-mappings:
- **SG-* now feed STE as strategy candidates**, not "signals" as a
  product. SG-22 (signal-exit-advisor) feeds PME as an advisory input;
  SG-13/SG-14 (duplicate/correlation checks) are **code checks in EXE**
  (§3.8) — the prompts remain as explainers for why a candidate was
  blocked, they never perform the check.
- **RR-* are review/explain-only.** RR-01 (risk-pretrade-check) fills
  the `risk_check` column with RCE's code-computed result plus a
  model-written explanation; the numbers are RCE's, never the model's.
  RR-14 (kill-switch review) is advisory; the switch itself is code.

#### PR — Prediction narrative (8, NEW — PRE's prompt surface)
| ID | Prompt | Purpose |
|---|---|---|
| PR-01 ★ | scenario-narrative | Explain a ScenarioSet in plain language: what each scenario means for this account, in currency the user understands (numbers quoted from the ScenarioSet, never computed) |
| PR-02 ★ | scenario-unusual-flag | Flag when a setup has no precedent in the stored history (low-n scenarios, regime never seen for this symbol) — structured, evidence-tagged |
| PR-03 | scenario-timing-note | Explain the `time_to_decision` / `manage_triggers` of a ScenarioSet (why now, what changes the answer) |
| PR-04 | scenario-compare | Explain new-trade-vs-manage-existing comparison from PRE (which wins and why, quoting EV/R) |
| PR-05 | scenario-risk-story | Tell the adverse-scenario story for a position ("if this goes wrong, here is exactly what happens to your equity") |
| PR-06 | scenario-probability-caveat | Explain what a historical-frequency probability means and its sample limits (n shown, never hidden) |
| PR-07 | scenario-postmortem | After a position closes: which scenario happened, what PRE got right/wrong, what changes next time (feeds the weekly rule review, §7.3) |
| PR-08 | scenario-regime-note | Explain how the current regime changes the scenarios vs the strategy's usual conditions |

### 5.7 Model/prompt selection: the weight metric (bake-off, then bind)

For every job, the system does not assume a model or a prompt version
is good — it **measures** candidates and binds the winner:

1. **Candidates.** A candidate is (prompt version × model). New prompt
   versions and newly available models enter as candidates; the
   incumbent stays in production until beaten.
2. **Bake-off.** Every candidate runs the job family's full eval set
   (§5.4): schema pass rate, decision-field agreement (5 runs),
   honesty (invented-number hits), latency (p50/p95), and cost per
   1,000 calls (from `ai_usage_events` token counts).
3. **The weight.** One score per candidate, computed by code:
   `weight = 0.40 × schema_pass + 0.25 × decision_agreement + 0.20 ×
   honesty (1 − invented_rate) + 0.10 × latency_score + 0.05 ×
   cost_score`, each component normalized to [0,1] on the eval set.
   The weights are the proposal — the user approves or changes them
   (they encode "correctness first, speed and cost last"). A candidate
   with any schema failure on a money-path prompt scores 0 regardless
   of the other components.
4. **User feedback counts.** After serving, each output that the user
   (or admin) marks right/wrong feeds a `feedback_score` that decays
   the weight of a losing candidate. Feedback never overrides a schema
   failure — it only breaks ties between passing candidates.
5. **Bind.** The winner is pinned: `prompt_jobs.model` +
   `ai_engine_configs.prompt_template_id` point at the winning version,
   and the runner records `prompt_id + template_version + model` on
   every output (§5.3). The user sees which model/prompt produced a
   result — "chosen because it scored 0.91 on the eval set" is shown in
   the dashboard, not hidden. A challenger that beats the incumbent by
   ≥0.03 on weight (and passes the canary, §5.4 step 5) replaces it;
   the old winner stays one re-pin away.
6. **Storage.** `prompt_benchmarks`: `prompt_id, template_version,
   model, eval_set_id, weight, components JSONB, n_cases, run_at`.
   Benchmarks are re-run on every new model arrival and on every
   prompt promotion — never assumed stale.

This is the mechanism behind the roadmap's "compare which model/prompt
is more OK, bind the chosen output and return it to the user". The
comparison is mechanical, the binding is explicit, and the user can see
why this output came from this model.

## 6. Factor engine: 6–7 core factors → 70, all server-side

### 6.1 Rules for every factor

1. **Computed by code, in Rust, from stored data** — never by the
   model, never in the browser. Money in `Decimal`.
2. **Every factor carries its sample size and confidence.** A factor
   with too little data reports `insufficient_data` with the minimum
   it needs — never a number dressed up as knowledge. (Existing
   engines already do this: `bucket_reliability`,
   `evidence_strength`, consistency drift gates requiring ≥5 trades
   per half.)
3. **Every factor is reproducible:** `factor_id`, `engine_version`,
   input snapshot hash, computed_at. Re-running the same inputs gives
   the same numbers.
4. **Every factor names its source tables** (below). A factor whose
   source data does not exist yet is listed as `planned`, not computed.
5. **Factors feed engines as inputs only** (MAE, STE, PRE, RCE, FGE).
   The prompt interprets; the factor is the ground truth.

### 6.2 The 7 core factors (Phase 2a)

| # | Factor | Definition | Source data | Computation (server-side) | Coaching surface |
|---|---|---|---|---|---|
| F-01 | **Win rate** (with confidence) | Wins / (wins + losses), break-even excluded (`\|net\| < 0.005`) | `history_positions` batches → closed `Position`s (`trading_analytics::Dataset`) | Extends `engines::performance`: Wilson-score CI bounds (already implemented as `win_rate_ci_bounds`); report `win_rate`, `ci_low`, `ci_high`, `n`. Minimum n=20 for a "reliable" label, n<10 → `insufficient_data` | "You win 47% of trades (95% CI 38–56%, n=62) — the edge is not the win rate, it's what happens after." |
| F-02 | **Edge / R-expectancy** | Expected R per trade: `E[R] = (avg win_R × win_rate) − (avg loss_R × loss_rate)`, where R = net P/L ÷ initial risk (entry→SL distance × size) | Same closed positions + stored `stop_loss` per position; initial risk from `risk/specs.rs` pip values | Extends `engines::performance`: compute R per trade in `Decimal`; expectancy in R and in account currency; also `profit_factor` (gross + net, already implemented, `None` when no losers) | "Your expectancy is +0.35R per trade — you make 35 cents per dollar risked. That is the number the whole plan protects." |
| F-03 | **Session profitability** | Net P/L, win rate, expectancy per session (Asian / London / New York) and per weekday/hour | Closed positions with `opened_at`/`closed_at` (UTC) → resolved local tz | Extends `engines::session` (`session_of`, `local_weekday_hour`): per-bucket P/L + win rate + reliability gate (bucket needs ≥8 trades, else `insufficient_data`) | "London is your session: +1.8R/trade (n=31). Asian is −0.6R (n=14) — the plan says: don't trade Asian." |
| F-04 | **Drawdown** | Max drawdown (peak-to-trough on the equity curve), current drawdown, recovery time | `equity_curve` (from `Dataset`) + cash flows (deposits excluded — already implemented: deposits do not inflate P/L or drawdown) | Extends `engines::performance`/`risk`: `max_dd`, `current_dd`, `recovery_days` (or `still_underwater`), drawdown as % of peak equity | "Your worst drawdown was 12% and it took 9 days to recover. The daily-loss limit exists so you never repeat it." |
| F-05 | **R-multiples distribution** | Distribution of trade outcomes in R: median win R, median loss R, tail (best/worst), % of trades ≥ +2R, % ≤ −1R | Same closed positions, R per trade | New code in `engines::performance` (or a small `r_multiples` engine): histogram buckets in R, median/quartiles, tail counts | "Your median win is +1.4R but your median loss is −1.0R — you cut winners short. The TP rule targets +2R." |
| F-06 | **Trade frequency** | Trades per day/week, active days, overtrading flag vs the user's own 30-day baseline | Closed positions + `opened_at` | Extends `engines::behavior`/`risk`: rolling 7d/30d frequency, baseline mean + stdev, flag when today > baseline + 2σ | "You took 11 trades today vs your 3/day baseline. That is overtrading, not opportunity." |
| F-07 | **Symbol mix / symbol edge** | Per-symbol net P/L, win rate, expectancy; concentration (top symbol share) | Closed positions grouped by `symbol` | Extends `engines::symbol_edge` (`run_symbol_edge`, `run_style`, `run_consistency`): per-symbol rows with `evidence_strength(n)`; symbols with n<5 are `unclassified` | "EURUSD is your edge (+0.5R, n=40). XAUUSD is −0.4R (n=18) — the plan drops gold until the edge returns." |

### 6.3 Growth path to 70 factors

The catalog grows by category, in priority order. Each factor lists its
source; `planned` means the source data does not exist yet and the
factor cannot ship before it.

**A. Execution quality (8)** — F-08 slippage vs requested price (needs
fill data from MT5 deals — `planned`, source: `history_orders` fills);
F-09 requote/reject rate (`planned`); F-10 hold-time distribution
(exists: `holding_seconds`); F-11 entry quality — MFE/MAE per trade
(`planned`: needs intra-trade equity ticks, currently in
`Coverage.unsupported`); F-12 exit quality — captured vs available move
(needs MFE — `planned`); F-13 partial-close usage (exists: position
splits in history); F-14 stop-hunt exposure — SL hit then price
reverses (`planned`, needs tick data); F-15 commission+swap drag per
symbol (exists: `commission`, `swap`, `fee`).

**B. Timing (8)** — F-16 hour-of-day edge (extends F-03); F-17
day-of-week edge (extends F-03); F-18 holding-time vs outcome
(exists); F-19 time-to-first-profit (needs tick data — `planned`);
F-20 news-proximity performance — P/L of trades opened within ±30 min
of importance-3 news (new: needs news join, §7); F-21 weekend-hold
performance (exists); F-22 session-transition performance (extends
F-03); F-23 duration buckets (<1h, 1–4h, 4–24h, >24h) (exists).

**C. Sizing & risk (10)** — F-24 risk-per-trade distribution (exists:
volume + SL distance); F-25 size consistency / CV (extends `risk.rs`
volume stats); F-26 risk-of-ruin estimate (code formula from win rate
+ payoff, labelled `CALCULATED` with assumptions stated); F-27 Kelly
fraction (informational only, never auto-applied); F-28 margin usage
(exists: `account_snapshots`); F-29 consecutive-loss tolerance
(exists: streaks); F-30 max adverse excursion tolerance (needs tick —
`planned`); F-31 position-size vs profile violations (exists: profile
bounds); F-32 correlation-adjusted exposure (needs quotes — exists:
market cache); F-33 drawdown velocity — how fast DD accumulates
(exists: equity curve).

**D. Behavior (10)** — F-34 revenge-trading index (loss → re-entry <
15 min, larger size) (exists: timestamps + sizes); F-35 FOMO entries
(entry after a large move, no pullback) (`planned`: needs candle
context); F-36 early-exit rate — closed before TP with profit left
(exists: TP vs exit price); F-37 SL-moving rate — SL moved against
the trade (exists: command history `modify_sl_tp` vs price); F-38
plan-adherence — % of trades with SL+TP set at open (exists:
`stop_loss`, `take_profit` nulls); F-39 journaling rate (exists:
journal module); F-40 break-rule rate (exists: audit + decisions);
F-41 tilt score — composite of F-06/F-34/F-36 (code composite,
labelled as such); F-42 recovery behavior after a loss day (exists);
F-43 discipline trend (rolling 30d, exists).

**E. Market regime interaction (8)** — F-44 trend-regime performance
(needs regime labels from MA-01 — `planned` until MA ships); F-45
volatility-regime performance (ATR percentile — `planned`); F-46
range vs trend split (needs MA-01); F-47 news-regime performance
(needs NT-01 — §7); F-48 spread-regime cost (needs spread data —
`planned`); F-49 symbol-regime fit (extends F-07); F-50 drawdown
clustering by regime (extends F-04); F-51 best/worst market condition
(composite, `planned`).

**F. Consistency & stability (8)** — F-52 monthly consistency — % of
profitable months (exists); F-53 equity-curve smoothness / R²
(exists: equity curve); F-54 profit concentration — top-5 trades share
of profit (exists); F-55 loss concentration (exists); F-56 rolling
30d expectancy trend (exists); F-57 expectancy stability / CV across
halves (extends consistency drift gates); F-58 sample adequacy —
total n + per-factor n (exists: coverage); F-59 regime-adjusted
expectancy (`planned`).

**G. Cost & account (8)** — F-60 total cost drag (commission+swap+fees
as % of gross profit) (exists); F-61 cost per symbol (exists); F-62
deposit/withdrawal-adjusted return (exists: cash flows); F-63 account
growth rate (exists); F-64 funding cost of holds (swap per day held)
(exists); F-65 broker comparison (only if multiple accounts —
`planned`); F-66 tax-relevant totals (jurisdiction-dependent —
`planned`, open question O-9); F-67 grant/request usage vs limits
(new: SPEC ADDITION C).

**H. Scenario distributions (3, NEW — PRE's probability source)** —
F-68 level-reach probability: for symbol + session + regime, the
historical frequency of price reaching a level within horizon T
(from stored candles; `n` always shown; `n < 30` →
`insufficient_data`); F-69 adverse-move distribution: distribution
of maximum adverse excursion within T after entry-like conditions
(from stored candles); F-70 session ATR distribution: ATR by session
for stop/target scaling. These three are the **only** source of
probabilities in the system — PRE reads them, never invents its own.

Total: 7 core + 60 + 3 = **70 factors**. The catalog is a table, not a
wish: each factor gets a row (id, name, formula, source, status,
engine) before it is built, and the dashboard shows only factors whose
status is `computed` — the rest are hidden, never shown as zeros.

### 6.4 Where factors live

- **Compute:** new module `trading_analytics/factors/` (or extend
  `engines/` — implementer's choice, same `EngineOutput` shape, same
  `ENGINE_VERSION` discipline). Reuse `Dataset`/`Position`; add a
  `FactorRun` record: `account_id, factor_id, engine_version,
  inputs_hash, values JSONB, sample_n, confidence, computed_at`.
- **Trigger:** on every `history_positions` sync batch (incremental) +
  a nightly full recompute + on demand from the coaching endpoint.
  The pipeline pattern (`trading_analytics/pipeline.rs`: queue +
  workers + stuck requeue) is reused, not reinvented.
- **Read:** `GET /api/v1/connector/factors?account_id=` (new) and the
  existing coaching endpoint. The dashboard never computes factors; it
  renders what the server computed, with `sample_n` and `confidence`
  visible next to every number.

## 7. News-aware engine + daily knowledge loop

### 7.1 Importance (new — the "very important" model)

`news_items` gains `importance INT 0–3` + `importance_reasons JSONB`,
computed at ingest by code (NT-01 proposes, code decides — importance
is never model-only):

| Importance | Rule (all code-computed) |
|---|---|
| 3 — very important | Event type in {rate decision, CPI, NFP/jobs, central-bank speech} OR source tier-1 + `\|sentiment\| ≥ 0.5` + symbol match with an open position or a watched symbol |
| 2 — important | Source tier-1/2 + `\|sentiment\| ≥ 0.3` + symbol match, or calendar event with high expected impact |
| 1 — notable | Any scored headline with a symbol match |
| 0 — noise | Everything else (`\|score\| ≤ 0.1` is already noise by the lexicon rule) |

Source tiers are **admin data** (NT-12), not model opinion. Initial
tiers (open question O-5): tier-1 = FXStreet, ForexLive; tier-2 =
CoinDesk, CoinTelegraph; tier-3 = the rest. Importance decays with
time (NT-22): importance-3 lasts 4h, importance-2 lasts 2h,
importance-1 lasts 1h.

### 7.2 How news plugs into the engines

1. **Freeze windows (code, not model).** Around an importance-3 event
   for a symbol: no new auto-trades for that symbol from 30 min
   before to 15 min after (RR-12, NT-10). The window is computed from
   the economic calendar + news ingest time. A signal generated inside
   a window is vetoed with reason `news_freeze`, visible in the
   dashboard. PME freezes trailing moves inside the window (R-TP-6).
2. **Engine input.** Every ScenarioSet carries `news_context`: top-3
   items by importance for the symbol (NT-01/NT-03 outputs) with their
   scores, labels, and matched terms. PRE reads it; STE reads it for
   session eligibility.
3. **TP/SL interaction.** R-TP-6 freezes trailing moves around
   importance-3 events and optionally tightens SL to breakeven
   pre-event (user setting, default on for auto_execute accounts).
4. **"This is very important" surface.** The dashboard shows
   importance-3 items in a dedicated strip with NT-08's explainer
   ("why this is very important") and the affected symbols/positions.
   No importance-3 item is ever silently dropped from the feed.
5. **News performance factor.** F-20 (trades near importance-3 news)
   closes the loop: if the user systematically loses around news,
   coaching says so with numbers.

### 7.3 Daily knowledge loop (information → knowledge, not storage)

The daily loop turns the day's information into **usable knowledge for
STE** — versioned parameter snapshots, not stored articles:

```
ingest (calendar + news + bars + MCP outlook, pre-session)
  → impact filter (which symbols are blocked, and when — NSE)
  → regime/volatility read (ATR, session bias — MAE)
  → parameter snapshot (versioned, timestamped — STE input)
  → outlook (one structured JSON per symbol: bias, sessions,
     blackouts, parameter set, reasoning — PR/NT prompts, code numbers)
  → publish to the engine + show the outlook in the dashboard
  → audit (every trade references its strategy version + parameter
     snapshot id)
```

- The loop runs once before the London session and once before New
  York; intraday it only **narrows** (adds a blackout), never widens,
  a tradeable window — a safety property.
- **Parameters update daily; rules do not.** A daily refresh updates
  blackouts, session windows, and ATR-scaled stops, and versions them
  (`strategy_param_snapshots`). Entry **rules** change only on the
  weekly review: the accumulated R-multiples per strategy version are
  evaluated (PR-07 postmortems + factor review), a rule change is
  proposed as a new strategy version, passes the eval gate (§5.4), and
  enters canary. A daily rule rewrite would be untestable churn
  (RESEARCH §5.4).
- **Champion / challenger (MLflow registry pattern, RESEARCH §8.2).**
  The live engine runs the `champion` strategy version; a candidate
  becomes `challenger`, runs on demo (probation), and is promoted to
  `champion` only on measured R-multiples over a real sample — never
  on a single day. Rollback = retag the previous champion: no
  redeploy, no code change. A version stamp always carries the
  **data vintage timestamp** alongside the rules hash, parameter hash,
  prompt/template version, and model id — a version without its data
  vintage is not a version, and any decision must replay exactly
  point-in-time.
- **Knowledge objects, not articles.** The loop stores: the parameter
  snapshot, the outlook JSON, and the impact assessments. News
  articles themselves are not the product — the `news_items` table
  keeps its 10,000-row cap and oldest-first pruning; the loop's output
  is what STE consumes.
- **Sources (RESEARCH §2.2–§3):** MT5 economic calendar (primary,
  in-terminal, free); Finance Calendar API / FCS / biquote (calendar
  candidates); FXStreet/ForexLive/CoinDesk/CoinTelegraph RSS (already
  wired); TradingView official MCP (research/outlook only — delayed,
  no official number; never live signals); CFTC CoT / CME FedWatch (weekly
  regime input). Yahoo is best-effort only (demoted, RESEARCH §3.4).

## 8. Coaching from real history + user behavior

### 8.1 The pipeline (already exists — Phase 2 extends it, not replaces it)

```
connect account → history_positions sync → Dataset
  → deterministic engines (performance/session/symbol_edge/risk/
     trade_management/behavior + factors F-01..F-07)
  → consensus (trading_analytics/consensus.rs)
  → coach prompts (CO-01..CO-40, fenced evidence, MAX_PAYLOAD_CHARS)
  → typed report v2 (report_kind: trading_coach_report) or raw fallback
  → dashboard coaching panel + weekly plan
```

What Phase 2 adds: the factor engine (§6) as the evidence base, the
CO prompt family (§5.6), the scenario outcomes (PR-07 postmortems) as
evidence, and the **coaching plan** as a first-class object (below).
What it does not change: the rule that the model computes no numbers,
the reliability ladder, the quota reservation, or the epistemic tags.

### 8.2 Deriving "who they were / what they did / what happened"

| Question | Derived from | Example output |
|---|---|---|
| Who they were | Style + consistency + symbol edge engines (F-07, F-43, F-52) | "A London-session EURUSD scalper who holds 40 minutes on average and overtrades after losses." |
| What they did | Trade-management + behavior engines (F-06, F-36, F-37, F-38) + decision audit | "You moved your stop against the trade 9 times in 62 trades; 7 of those became your worst losses." |
| What happened | Performance + risk engines (F-01, F-02, F-04, F-05) | "Expectancy +0.35R, max drawdown 12%, profit concentrated in 5 trades." |
| Why | Cross-factor read (CO-01 + RR-29 bridge) | "The edge is real but small; the drawdown came from Asian-session revenge trades, not from the strategy." |
| Which sessions paid | F-03 session profitability | "London +1.8R/trade; Asian −0.6R. Trade London, skip Asian." |

### 8.3 The coaching plan (a stored object, not a chat reply)

```
coaching_plans: id, user_id, account_id, period (week|month),
  diagnosis (CO-01 output, typed), priorities[3] (finding + factor refs),
  steps[] (CO-10 plan steps, each: action, metric, target, deadline),
  targets (e.g. expectancy ≥ +0.3R, Asian trades = 0, max 4 trades/day),
  status (active | completed | superseded), created_at, reviewed_at
```

- Each plan has **at most 3 priorities** and **measurable targets**; a
  plan without a number is not a plan.
- The next plan is generated from the new history (CO-12 milestone
  check, CO-20 goal tracker) — the loop is weekly, not one-off.
- Minimum history: a plan requires n≥20 closed trades; below that the
  system shows CO-37 (honest empty state: "not enough history yet")
  and the onboarding plan (CO-36) instead of a fake diagnosis.
- Coaching never claims emotional states without evidence (existing
  `Finding` rule, spec §7.3) and never invents peer comparisons unless
  a real cohort exists (CO-22 returns UNAVAILABLE otherwise).

### 8.4 User behavior analysis + intervention (auto mode, per permission)

The engine analyzes the user's trading behavior — not to judge, but
because behavior is the largest controllable contributor to the
account's outcome. Detection is code (behavior engine + factors
F-34/F-36/F-37/F-41); the response depends on the mode:

| Behavior flag (code-detected) | Evidence | `suggest_only` response | `auto_execute` response (only with the intervention permission) |
|---|---|---|---|
| Out-of-strategy trade | Trade matches no promoted strategy version (or violates its rules) | Nudge + coaching note citing the rule | PME applies the protective default: breakeven/partial per RiskFreePolicy; no scale-in on it |
| Emotional entry (revenge/FOMO) | F-34 (loss → re-entry < 15 min, larger size) or F-35 (entry after a large move, no pullback) | Nudge (BG-03) + session note | Next entry for that symbol requires a fresh ScenarioSet + RCE approval (no auto-entry for 30 min after the flag); size capped at the profile minimum |
| Repeated losing pattern | Same setup losing ≥3 times in the strategy's recent R-multiples | Coaching plan priority | STE suspends that strategy version for the account (canary → retired path) until the weekly review |
| Irrational size increase | Size > 2σ above the user's 30-day size baseline (F-25) | Warning on the position card | Size clamped to profile max for the next trade; the increase itself is never auto-matched |
| Drawdown contribution | Trade's R loss vs the day's drawdown budget | Daily digest entry | Daily-loss kill-switch path (§10): stop new actions, protect open positions |

- **Detection definitions (measurable, RESEARCH §9.1):**
  re-entry share (trades opened within 15 min of a losing trade ÷
  total trades); size-after-loss ratio (avg size after a loss ÷ avg
  size overall); frequency escalation (avg minutes between trades,
  rolling 1h, vs the user's own baseline); post-loss performance gap
  (win rate and avg R of post-loss trades vs all other trades);
  off-strategy share (trades matching no promoted strategy version ÷
  total); behavior-driven drawdown (drawdown attributable to flagged
  trades vs total drawdown). One month of a pattern is a hint; 3–4
  months is a habit — the engine says which it is seeing.
- **Intervention ladder (escalates; auto steps need explicit
  permission — RESEARCH §9.2):** 1. **Nudge** (label + reason on the
  trade); 2. **Speed bump** (30–60 s forced pause before an emotional
  order reaches the broker, when 2+ tilt signals fire together);
  3. **Circuit breaker** (15-min detachment — no new orders — after a
  loss streak or as drawdown approaches the cap); 4. **Daily cap**
  (−2R or −1% of capital — non-negotiable safety layer). Post-trade
  tagging alone is the "Coroner's Report Fallacy": it categorizes
  the loss on Sunday and protects nothing while the user is clicking
  on Tuesday — detection runs in-session; the journal is the record,
  not the protection.
- **What the engine must NOT do:** diagnose mental health, or present
  a behavioral label as a medical fact. Labels describe *trade
  patterns* ("3 trades within 12 minutes of a loss, size 2×
  average"), never the person.
- **Intervention is permissioned, explicit, and auditable.** The
  intervention permission is a separate per-account setting (not
  implied by `trade_opt_in`): which interventions are allowed
  (protect-only / protect+size-cap / protect+strategy-suspend) is
  chosen by the user, shown in the dashboard, and every intervention
  writes a decision record citing the behavior evidence (factor refs +
  trade refs). An intervention without evidence is rejected at
  enqueue, like any other reason-less command.
- **The user can always see and override.** Every behavior flag and
  every intervention is visible in the dashboard with its evidence;
  the user can disable any intervention type at any time (takes
  effect on the next decision cycle, ≤ 1 min).

## 9. Real performance metrics (closed trades only)

Win rate and every other performance number are computed **only from
real closed trades** — never from a model estimate, never from a vendor
claim, never from a backtest. The formulas are the industry-standard
ones in RESEARCH §4; this section states the counting rules the engine
implements.

| Metric | Definition (code) | Counting rule |
|---|---|---|
| Win rate | Winning closed trades ÷ total closed trades | Break-even excluded (`\|net\| < 0.005`); shown with `n` and Wilson CI (F-01); n < 20 → labelled unreliable, n < 10 → `insufficient_data` |
| Profit factor | Gross profit ÷ \|gross loss\| (net of costs) | `None` when there are no losing trades (never fabricated) |
| Net PnL | Sum of net P/L (after commission, swap, fees, spread) | Deposits/withdrawals excluded; per account, strategy, session, hour, symbol, period |
| Max drawdown | Largest peak-to-trough fall on the equity curve | Measured on equity (open positions count), not balance; recovery time + gain-to-recover shown |
| Average win / average loss | Mean net P/L of winning / losing closed trades | In currency and in R (R = net P/L ÷ initial risk) |
| Risk/reward (realized) | Avg win R ÷ \|avg loss R\| | Distinct from the *planned* R:R of a strategy version |
| Expectancy | (win rate × avg win) − (loss rate × \|avg loss\|), in currency and R | The headline number; sample-size rule: needs 100+ trades to stabilize (RESEARCH §4.5) |
| R-multiples | Per-trade P/L ÷ initial risk | Stopped out as planned = −1R; distribution + median + tail (F-05) |
| SQN | (avg R ÷ std-dev of R) × √N | Needs 100+ trades; shown only above that |
| Costs | Commission + swap + fees + spread + slippage, as % of gross profit | Slippage is measured per deal: `actual fill price − expected price` (expected = the price the engine sent the order at), tracked per symbol and per session (RESEARCH §4.6); until fill data exists it is `planned` (F-08). A strategy whose edge disappears after slippage + spread + commission + swap has no edge. Spread/commission/swap are computed now (F-15/F-60) |
| Recovery factor | Net profit ÷ max drawdown (currency) | Per strategy and per account |
| Session / hour attribution | Win rate, expectancy, PF per session and per hour bucket | Session = session of *open* time (stated once, everywhere); bucket needs ≥8 trades else `insufficient_data` |
| Strategy attribution | All of the above per strategy_id + version | A strategy version with < 20 trades carries no verdict — only its raw numbers with `n` |
| Account growth | Equity curve split at the connection date; time-weighted return (TWR) for before/after comparison | Growth % = (equity_end − equity_start − net deposits) ÷ equity_start, per period, always with the deposits/withdrawals context (RESEARCH §4.4) |

- **No vendor win rate is ever quoted.** The 65–85% figures in the
  scalping sources are unverified marketing (RESEARCH §5.3). The
  program measures every strategy on the user's own history (and on
  demo before any real-money claim).
- **The system win rate on the home overview (Phase 3)** is an
  aggregation of real closed trades across accounts that opted into
  aggregation, with the sample count attached — and it is shown only
  once enough real trades exist. Public vs private split is O-14.
- **Every metric carries its `n`.** A metric without its sample count
  is not shown. This is the "never invented numbers" rule applied to
  statistics.
- **Demo history is real history for that account.** The user connects
  their own terminal (Demo or Real); the engine measures what really
  happened on that account. Demo history is not "paper trading" as a
  product (RESEARCH §4.6).

## 10. Phased build plan (revised — execution is the destination)

### Phase 2a — Foundation (demo only, `suggest_only`) — first

Ship: the 8 engines as **code skeletons with their contracts**
(CONTRACTS.md) wired to the event bus; factor engine v1 (F-01..F-07 +
F-68..F-70 scenario distributions, server-side, with
sample/confidence); PRE v1 (code-only scenarios, no AI numbers);
strategy registry v1 (seed strategies: windowed EMA 9 + VWAP + RSI
first, then EMA pullback, VWAP pullback, RSI gold reversion) + daily
knowledge loop v1 (parameter snapshots only); RCE v1 (sizing + veto
with alternative — the optimizer rule, §3.4); PME v1 **read-only
management proposals + adoption** (adopts unmanaged positions, shows
what it *would* do, enqueues nothing); server EXE (close/modify) +
Execution Bridge **spec** (policy envelope format, `order_check`
path, §17.3 reporting) with the Bridge change scoped; the §17.5
close-out prepared (allowlist + demo proof plan) but not executed;
prompt library skeleton (`prompt_jobs`,
`prompt_eval_runs`, `prompt_outputs`, `prompt_benchmarks`) + the 30 ★
seed prompts with eval sets; news importance v1 (NT-01/NT-02/NT-03 +
importance column + freeze windows as vetoes in the decision path,
advisory only for entries, enforced for PME); coaching v2 (CO-01..CO-10
on the existing engines + factors); behavior flags v1 (detection only,
no intervention); `risk_check` filled by RCE on every command;
kill-switch scaffolding (admin + user toggle, stops new engine
actions, alerts); transparency (§13: `analysis_jobs` + decision feed).

Depends on: PR #107 merged + deployed (gateway, grants, dashboard);
`history_positions` sync coverage verified live (the p0–p7 harness
proves sync works; 2a re-verifies it carries enough history for
F-01..F-07); `ai_prompt_templates` + `ai_engine_configs` in
production.

Must NOT build in 2a: any auto-execution, any `open_order`, any
real-money path, any change to the v1 contract, any intervention.

Safety rails: `suggest_only` everywhere; proposals are labelled as
such; kill-switch stops proposals; every proposal carries rule id +
scenario ref + factor snapshot; no command is enqueued by the engine.

### Phase 2b — Execution (demo accounts, explicit opt-in)

Ship: **the §17.5 close-out executed on demo** — user approval +
allowlist + Risk Engine/financial-consent proof on demo + schema
bump — so the **gateway sends all four operations on demo accounts**
(open/modify/reduce/close); PME
auto-manage on **demo accounts only** (R-TP-1..R-TP-8, Risk Free
policy auto, partial closes via the gateway reduce-volume operation); duplicate prevention +
reconciliation + recovery matrix live (§3.8, CONTRACTS.md §8); RCE
veto-with-alternative enforced; behavior intervention v1 (protect-only
by default, per permission, §8.4); decision audit loop live (§17.3
ingest → dashboard); prompt library grows to ~120 prompts (all SG +
TS + RR + PR families); factor catalog to ~30 factors (categories
A–C + H); news freeze enforced in the execution path (not just
advisory); real performance metrics v1 (§9) from closed trades.

Depends on: 2a live for ≥2 weeks with proposals reviewed (do the
proposals match what a human would do? measured by TS-09/PR-07
postmortems); demo probation criteria met (see below); the Execution
Bridge implements the gateway command channel + `order_check` +
trailing under the policy envelope (§2, D-5) —
this is a Bridge change, coordinated with the Bridge repo, not a
server-only change.

Must NOT build in 2b: real accounts (opens stay demo-only until the
2c gate); any leverage/margin change; copy/social trading.

Safety rails: demo-only gate (enforced by `trade_mode = demo` check at
enqueue, not by UI hiding); daily loss kill-switch (auto-deactivate
account settings to `suggest_only` + alert + optional close-all only
if pre-authorized); per-account rate limits; full audit; rollback =
re-pin prompt versions + disable the rule set (one admin action).

**Demo probation (to earn `auto_execute`):** ≥20 closed demo trades
under `suggest_only` with the engine's proposals followed ≥80% of the
time and no risk-profile violation in 14 days. The numbers are the
proposal; the user sets the final thresholds (open question O-3).

### Phase 2c — Real money (gated, per-account, admin-approved)

Ship: contract **v2 live on real accounts** (the §17.5 close-out was
executed on demo in 2b; 2c extends the same channel to real accounts
after the demo proof). Real-account rollout
behind: admin grant + per-account risk profile + `trade_opt_in` on a
live key + probation completed + **explicit per-account opt-in screen**
(plain-language risk disclosure, OP-22, human-reviewed) + intervention
permissions chosen by the user. Full 213-prompt library; 70 factors;
live monitoring (decision digest, anomaly flags, cost/latency/value
watch); Risk Free policy live with the user's chosen default level
(O-16).

Depends on: 2b live on demo for ≥30 days with measured results
(expectancy, drawdown, violation rate, scenario calibration — did the
predicted probabilities match the realized frequencies?) reviewed by
the user; the gateway open channel proven on demo in 2b; the Bridge policy-envelope
implementation proven in 2b; a written risk disclosure the user has
approved.

Must NOT build in 2c (or ever, without a separate decision): copy
trading between users, leverage changes, margin trading features,
any use of one user's data to trade another user's account, any
fully-automatic mode without a kill-switch and without a human-visible
audit.

Safety rails: everything in 2b, plus: per-account daily loss limit
that **closes all positions and deactivates the account** when hit
(only with the user's pre-authorization, recorded); admin kill-switch
per user and global; real-money accounts start with the smallest risk
profile and can only be loosened by admin after 30 clean days; all
real-money decisions are kept for audit (retention: see open question
O-8).

## 11. Data model (PostgreSQL only, minimal)

PostgreSQL is the **only** database for the project's main data. No
SQLite, no parallel database for primary data. The design rule is:
**minimal tables, indexes per real queries, no live data in the DB.**

### 11.1 New tables (only what holds new truth — reuse first)

| Table | Key columns | Why it must exist (no existing table holds this) | Phase |
|---|---|---|---|
| `strategies` | `id, name, family, version, rules_hash, rules JSONB, params JSONB, status, eval_set_id, eval_result JSONB, created_by, created_at, promoted_at` | Versioned strategy registry (§3.2.1) — no existing table versions rules | 2a |
| `strategy_param_snapshots` | `id, strategy_id, version, params JSONB, data_timestamp, source_list JSONB, created_at` | Daily parameter versions (§7.3) — one row per refresh, referenced by trades | 2a |
| `managed_positions` | `ticket, account_id, symbol, direction, volume, entry, sl, tp, state (unmanaged\|adopted\|managing\|protecting\|scaling\|closing\|closed), strategy_id, strategy_version, parameter_snapshot_id, risk_free_policy_id, adopted BOOL, origin (engine\|user\|unknown), updated_at` | PME's server-side position mirror (§3.5) — the sync batch is transient; PME needs durable state | 2a |
| `scenario_runs` | `id, decision_id, account_id, symbol, inputs_hash, scenarios JSONB, expected_value_R, recommendation, computed_at` | PRE's output, stored so a decision can be replayed (§3.3) — the decision record references it, it does not inline it | 2a |
| `risk_free_policies` | `account_id PK, enabled, protection_level, trigger_R, partial_pct_at_trigger, breakeven_buffer_pips, trailing_mode, trailing_start_R, max_protection_R, updated_at` | The user's Risk Free dial (§3.5.1) — per-account, user-set | 2a |
| `behavior_flags` | `id, account_id, flag_type, evidence JSONB (factor refs + trade refs), state (open\|acknowledged\|acted), created_at, resolved_at` | Behavior detection output (§8.4) — drives FGE + intervention + coaching | 2a detect, 2b act |
| `prompt_jobs` | (see §5.3) | Prompt inventory | 2a |
| `prompt_eval_runs` | (see §5.3) | Eval audit | 2a |
| `prompt_outputs` | (see §5.3) | Prompt audit | 2a |
| `prompt_benchmarks` | (see §5.7) | Bake-off results | 2a |
| `factor_runs` | `account_id, factor_id, engine_version, inputs_hash, values JSONB, sample_n, confidence, computed_at` | Factor outputs | 2a |
| `coaching_plans` | (see §8.3) | Coaching plans | 2a |
| `analysis_jobs` | `id, user_id, account_id, kind, stage, stage_detail, progress_pct, status, started_at, updated_at, result_ref` (§13) | Transparency | 2a |
| `tpsl_rules` | `id, account_id, rule_id (R-TP-1..8), params JSONB, enabled, version, updated_at` | Per-account TP/SL rule config | 2b |
| `tpsl_policy_envelopes` | `id, account_id, version, bounds JSONB, rules JSONB, issued_at, expires_at` | Server-issued envelopes for the local agent (§2, D-5) | 2b |
| `engine_signals` | `id, account_id, symbol, direction, entry, sl, tp, r_multiple, confidence, news_context JSONB, factor_snapshot JSONB, prompt_id, prompt_version, status (proposed\|approved\|executed\|vetoed\|expired), veto_reason, decision_id, command_id, created_at` | Strategy candidates (STE) awaiting decision — the audit trail for "why this candidate, why not" | 2b |
| `kill_switch_events` | `id, scope (global\|user\|account), reason, triggered_by, triggered_at, resolved_at` | Kill-switch audit | 2a scaffold, 2b enforced |
| `news_items.importance` | `importance INT 0–3, importance_reasons JSONB` (columns, not a table) | News importance (§7.1) | 2a |
| `ai_agent_decisions` (extend) | + `engine_chain JSONB, scenario_ref UUID, inputs_hash TEXT, knowledge_version TEXT, data_vintage_ts TIMESTAMPTZ, fear_greed_ref UUID, latency_ms JSONB, decision_to_send_ms INT, broker_response_ms INT` | The decision record (§4) — one table, extended, not a second decision table | 2a |
| `connector_access_grants.max_requests / requests_used` | already specified in SPEC ADDITION C (migration 0062, PR #107) — the engine counts auto-trade requests against the grant in 2b | #107 | |

**Not created:** no tick store (ticks stay in the quote cache +
aggregated candles), no per-engine log tables (the unified log feed
already exists), no separate decision table (the §17.3 row is
extended), no paper-trading ledger (paper trading is not a product,
§17), no backtest store (no heavy server backtests, §17).

### 11.2 Indexes — only per real queries

| Query (real, named) | Index |
|---|---|
| Decision record by account + time (dashboard, audit) | `ai_agent_decisions (account_id, created_at DESC)` |
| Decision by command (outcome join) | `ai_agent_decisions (command_id)` |
| Managed positions by account + state (PME loop) | `managed_positions (account_id, state, updated_at)` |
| Managed position by ticket (reconciliation) | `managed_positions (ticket)` UNIQUE per account |
| Factor runs latest per factor (read path) | `factor_runs (account_id, factor_id, computed_at DESC)` |
| Scenario run by decision (replay) | `scenario_runs (decision_id)` |
| Strategy version lookup (STE selection) | `strategies (name, version)` + `strategies (status)` |
| Param snapshot by strategy + time | `strategy_param_snapshots (strategy_id, created_at DESC)` |
| Behavior flags open per account | `behavior_flags (account_id, state, created_at DESC)` |
| Commands pending per account (EXE duplicate check) | `connector_commands (account_id, status, created_at)` — verify against the existing indexes first; add only if missing |

No index is created speculatively. Each index above names the query
it serves; a query that does not exist yet gets no index.

### 11.3 Storage rules (from the user's §9)

- Only truly necessary information is stored; no unnecessary tables
  (the table above is the complete list — anything else needs a new
  justification).
- Partitioning only where volume + access pattern justify it: **not in
  2a/2b.** Revisit for `ai_agent_decisions` / `prompt_outputs` if
  either passes ~10M rows (decision: time-based partitioning then).
- No duplicated data and no stored recomputable calculations:
  `factor_runs.values` stores the computed values (the computation is
  the product); derived views (per-strategy metrics, session tables)
  are queries, not tables.
- **No unnecessary live data in the DB.** Ticks, live quotes, and
  forming candles stay in memory/cache; only decision-relevant
  snapshots (regime at decision time, scenario inputs hash) are
  stored — and they are stored because the audit needs them, not
  because the engine needs them live.
- History is preserved: trade history, order changes, important
  decisions, audit — retention per O-8 (proposal: decisions + audit
  2 years, factor runs 1 year, prompt outputs 90 days, scenario runs
  1 year).
- Connection pooling, query optimization, and DB pressure control are
  taken seriously: per-service connection pools (§15), statement
  timeouts on the read paths, and the queue-depth backpressure rule
  (§2, D-6) applies to DB writers too.

Migration numbering: coordinate with the highest migration on main at
build time (0061 on #107, 0062 for grants limits); never reuse a
number, never rewrite a merged migration.

## 12. Performance (millisecond internal evals)

- **Internal evaluations run in milliseconds.** Budgets are in the
  §2.1 table (RCE < 20 ms, MAE/PME/FGE < 50 ms, STE/PRE < 100 ms,
  code path, p95). A decision cycle (sense → decide, excluding AI)
  has a **p95 budget of 500 ms per account**; the budget is measured
  per engine stage on the decision record (`latency_ms` JSONB) and
  reported weekly. A stage that persistently exceeds its budget is a
  bug, not a tuning note.
- **Deterministic computation runs in the internal engines** (Rust,
  Decimal). No model call is on the timing-critical path.
- **Parallel processing:** engines run concurrently off the event bus;
  per-account cycles are independent (no cross-account locks); the
  PME loop is per position, parallelizable.
- **Event-driven architecture:** no polling between engines (the bus
  delivers); the only polling loops are the 1-minute PME server cycle
  and the dashboard's 5-second UI poll (existing pattern).
- **Market data is distributed once, not fetched per engine.** Ingest
  publishes one `market.candles_ingested` / `market.regime_changed`
  event; MAE computes once; every other engine reads MAE's output.
  An engine that re-fetches quotes for itself is a bug.
- **The execution path is separate from analysis and AI** (§1.1.7,
  §3.8): EXE has its own queue, its own timeouts, and its own
  workers. A slow analysis queue never delays an execution, and an
  execution never waits for a model.
- **AI is never in the critical order-send path** (§5.2). AI latency
  is measured separately (p50/p95 per prompt family) and never counted
  in the decision-cycle budget — because it is not in it.
- **Latency is measured per section, separately:** per engine stage,
  per queue (depth + wait time), per command (enqueue → delivered →
  completed), per AI call. The weekly ops report (OP-17 extended)
  shows all of them; a number that is not measured does not exist.

### 12.1 Real performance metrics (user decision 8, 19:01)

| Metric | Definition | Target / reporting |
|---|---|---|
| Per-engine latency | p50/p95 of each engine stage on the decision record (`latency_ms` JSONB) | Budgets in §2.1; a stage persistently over budget is a bug |
| Decision-to-send time | Decision `created_at` → command enqueued | p95 < 1 s (server-side) |
| Broker response time | Command delivered → `action.result` received | Measured per command, reported p50/p95; no target set by us — the broker's number, reported honestly |
| Data receipt delay | Source timestamp (`as_of`) → `received_at`, per source | Reported per source class (§3.9); a source whose delay grows past its class bound is flagged |
| Queue depth + wait time | Per service queue (§14) | Backpressure threshold per queue; shedding is a named outcome, never silent |
| AI call latency / cost / value | Per prompt family: p50/p95 latency, tokens, cost, `changed_decision` rate (§5.2) | Weekly per-engine report; a family whose outputs never change a decision over 30 days is reviewed for retirement |

## 13. Transparency: the system always says what it is doing

Ground rule from the roadmap: even while it is "thinking", the system
tells the user what it is analyzing and how far along it is. **No
silent thinking anywhere** — dashboard live log, analysis progress,
coaching runs, signal generation, and now: **engine state** (which
engine is doing what, right now, for this account).

### 13.1 Progress is a first-class output

Every long-running engine job (factor run, scenario computation,
coaching run, news triage batch, daily knowledge loop) is an
`analysis_jobs` row with a stage machine:

```
queued → ingesting → factors → prompts → coaching → done
                                  ↘ failed (with reason, never silent)
```

- Each stage carries `stage_detail` (e.g. "scoring 62 closed trades",
  "running prompt SG-01 v3 on qwen-32b") and `progress_pct` (0–100,
  computed from work done / work total — never faked; if the total is
  unknown the stage reports `indeterminate: true` instead of a fake %).
- The row is written **before** the work starts and updated as stages
  change, so a user who opens the dashboard mid-run sees the run, not
  an empty page.
- Failure writes `status: failed` + the coarse failure reason (same
  vocabulary as `ai_usage_events.failure_path`: timeout, auth,
  provider_error, rate_limited, invalid_response, quota_exceeded,
  engine_disabled, cancelled) — never a silent disappearance.

### 13.2 How the user sees it

- **Dashboard:** the Agent Trade live-log feed (`GET
  /api/v1/connector/logs`, Phase 1) gains job progress entries and
  **decision entries** (each decision record is a log line: what the
  engine decided, why, and what happened); the status cards show the
  running job + stage + the engine currently active. Polling first
  (existing pattern); SSE later only if trivial.
- **API:** `GET /api/v1/connector/jobs/{id}` (new) returns the job
  row; `GET /api/v1/connector/jobs?account_id=` lists recent jobs;
  `GET /api/v1/connector/decisions?account_id=` lists decision
  records. API-first per the roadmap: the UI consumes the same API.
- **Analysis pages:** a coaching run or scenario computation shows its
  stages inline ("Analyzing your 62 trades… 3/5 stages") with the
  elapsed time. A run that takes longer than its stage timeout
  surfaces that fact instead of spinning forever.
- **What is never shown as progress:** a fake percentage, a stage
  that did not happen, or "done" for a job that failed. The existing
  reliability ladder (§1) already forbids this for reports; §13
  extends the rule to progress itself.

### 13.3 Grounding

The pattern already exists: `trading_analytics/pipeline.rs` runs
analyses as queued rows with `status` + bounded `status_detail`, and
the dashboard logs endpoint merges audit + command + sync events. §13
generalizes that row to all engine jobs and makes the stage machine
explicit. No new transport is introduced in Phase 2.

## 14. Scalability: a separable, stateless trading core

Roadmap requirement: the core that processes on the server must be
separable and stateless so that tomorrow it runs on **5–6 servers**,
using the other servers' CPUs under load, with a load balancer and fast
requests. Each service scales independently.

### 14.1 Service decomposition (logical first, physical later)

| Service | Owns (engines) | State it holds | Scales by |
|---|---|---|---|
| **ingest** | Sync batches (`account \| positions \| history_orders \| history_positions`), `account_snapshots`, quote/cache fan-out | None in-process; all state in Postgres | More ingest workers; batch size caps |
| **factors** | Factor computation (§6) + scenario distributions (F-68..F-70) from stored history | None in-process; reads DB, writes `factor_runs` | More pipeline workers (existing `FOR UPDATE SKIP LOCKED` claim already supports this) |
| **prompts-AI** | `infer` calls to 9Router, prompt library, evals, bake-offs (§5) | None in-process; config + templates in DB; usage events in DB | More API workers; per-engine concurrency caps; provider timeouts |
| **execution** | Command enqueue, PME rules, RCE gates, kill-switch (§3.4, §3.5, §3.8) | None in-process; commands + decisions + audit in DB | More API workers; per-account rate limits; idempotency keys (`command_id` UNIQUE — already exists) |
| **accounting** | P/L, deposits/withdrawals/fees, realized/unrealized (Phase 3, §15) | None in-process; ledger tables in DB | More workers; ledger writes are the contention point — see §14.3 |

### 14.2 Statelessness rules (what "stateless" means here)

1. **No in-process truth.** Anything another request or another server
   needs lives in Postgres (or an explicit shared store) — never in a
   local `HashMap` that only one process sees. The one exception today
   is the market quote cache (`market_intelligence::market::MarketCache`,
   in-memory per process): it is a *cache*, not truth — a cold process
   refetches, and a stale quote is labelled with its age. If a service
   needs shared quotes across servers, the cache moves to a shared
   store (Redis or Postgres) in Phase 4; Phase 2 only requires that no
   *decision* reads an unlabelled cache value.
2. **Idempotency everywhere it moves money.** Commands already carry a
   UNIQUE `command_id`; factor runs carry `inputs_hash`; prompt outputs
   carry `input_hash`; scenario runs carry `inputs_hash`. A retried
   request replays the stored result instead of double-executing. Any
   new money-moving endpoint must carry an idempotency key from day one.
3. **Queues, not direct calls, between services.** The existing
   pipeline pattern (Postgres queue + `SKIP LOCKED` claim + Notify wake
   + 15-min stuck requeue) is the template: ingest enqueues factor work,
   factors enqueue prompt work, prompts enqueue nothing that executes.
   A slow service backs up in its own queue instead of blocking the
   caller.
4. **Per-service timeouts and backpressure.** Every service call has a
   timeout (AI: engine `timeout_secs`, default 90s; provider connect:
   10s, existing). When a queue depth passes its threshold the service
   sheds load with a named outcome (`queued`, never a silent drop, and
   never a partial execution).

### 14.3 The path from one binary to 5–6 servers (no big bang)

Phase 2 ships the **logical** split inside the current single binary:
separate modules, separate queues, separate timeouts, no shared
in-process state except the labelled quote cache. That is already
deployable on one server and already horizontally safe for the worker
pools (the pipeline workers are N processes claiming from one queue).

Phase 4 (see §15) makes it **physical**: the services become separately
deployable units behind a load balancer; the only new infrastructure is
the balancer + a shared cache for quotes + per-service connection
pools. Nothing in Phase 2 may assume a single process (no in-process
locks across services, no local files as shared state, no "the other
service runs in this binary" shortcuts). The load balancer routes by
service (path prefix), health-checks each service, and spreads load;
under pressure it uses the other servers' CPUs because the work is in
the queues, not in one process's memory.

## 15. Phases 3 & 4 (next, not designed here)

Detailed design stays on Phase 2. These are the recorded next phases
so Phase 2 does not accidentally block them.

### Phase 3 — Accounting + unified history + home overview

- **Unified history loading.** Trading history taken from the user loads
  uniformly across the whole site: connect once, and it is available
  everywhere (dashboard, coaching, accounting, overview) from the same
  stored history — no per-page re-imports, no divergent copies. Phase 2
  already stores history once (`history_positions` → `Dataset`); Phase 3
  makes every surface read that one store.
- **Accounting section.** Clear accountings: deposits, withdrawals, fees,
  realized/unrealized P/L, per-symbol and per-session breakdown. Built on
  the `accounting` service (§14.1) and the existing cash-flow data
  (`Dataset.cash_flows`, `equity_curve`). Decimal throughout.
- **Home overview.** System win rate shown as an overview on the main
  home page, with per-user profiles and their profitable trades. Open
  question O-14 decides the public/private split (system-level numbers
  public; per-user numbers private, only after login). Homepage shows
  only real data — the standing rule — so the overview needs a real
  aggregation job over closed trades (§9), not a marketing number.
- **Account growth charts: before vs after connecting.** Equity curve
  split at the connection date, from stored snapshots + history (TWR,
  §9).
- **Virtual requests.** The number of virtual (simulated) requests a
  user can make — definition and quota model to be defined (O-13).
  Phase 2 must not assume any definition; the grants request limits
  (SPEC ADDITION C, migration 0062) are the *real*-request model and
  are not the same thing.

### Phase 4 — Scale-out + daily research loop

- **Multi-server core + load balancer:** the Phase-2 core (§14) spread
  across servers — the physical split described in §14.3.
- **Daily research loop:** strategies refreshed daily from news sources
  and analysis sources already connected — outlooks, and how an account
  moves forward with just this. Scalping algorithms on 5 / 10 /
  15-minute timeframes; the daily loop updates **parameters** (§7.3),
  rules change on the weekly review. The research agent owns the loop's
  content; the engine owns its inputs (factors, news importance,
  prompts).
- **TradingView constraint — see §16.**

## 16. TradingView constraint (corrected by research, 2026-11-18)

From RESEARCH §3 (verified 2026-11-18): **TradingView has NO public
REST data API** (its own help center + independent reviews). The
deployed scanner API is an undocumented internal endpoint — secondary
only. **But** there is now an official channel: a public **MCP server
in beta since 2026-09-16** (`https://mcp.tradingview.com/mcp`,
Essential plan or higher, OAuth 2.1, ~35 tools incl. `get_ohlcv` up to
5,000 bars) — legitimate for **research / outlooks in the daily loop**,
NEVER for live signals (data delayed — the official docs state only
'delayed', no number; community measurements report 15+ minutes — no
`end_time`/paging,
no Pine Script, no backtests, no orders). Yahoo Finance is DEMOTED
(2025–2026 rate-limiting/blocking; best-effort only). Primary live
feed: MT5/broker feed (FX/gold) + Binance public klines (crypto).

Design consequences:
1. **No engine input depends on TradingView alone.** Quotes, candles,
   and indicators come from the multi-source market cache with MT5 /
   Binance primary. If TradingView is unreachable, the engine degrades
   to the other sources and says so — it never blocks on it.
2. **The MCP is a research source, not a live feed.** It feeds the
   daily knowledge loop (§7.3) and the outlook — never PRE's live
   scenarios, never an entry decision. Its use still needs one
   authenticated live check with a paid (Essential+) account before
   the loop depends on it (RESEARCH §5, "uncertain").
3. **The scanner endpoint is not a data contract.** Nothing in Phase 2
   or 3 may treat the undocumented scanner endpoint as a stable API:
   no retries that assume its shape, no factor that needs it, no
   signal that waits for it.
4. **Alert webhooks (paid plan, 2FA, signed payload)** are an
   *optional additional* signal input only — never the sole trigger —
   and only after the bridge that translates them into a gateway
   command is designed (not in Phase 2).

## 17. Open questions (need the user — do not guess)

- **O-1 — Tick speed split.** TP/SL trailing needs sub-minute reaction.
  Proposal (§3.5, D-5): policy server-side (1-min loop + per-batch),
  fast trailing in the Bridge local agent under a server-issued policy
  envelope. Is that acceptable, or must all trailing be server-side
  even if it is slower?
- **O-2 — RESOLVED (user, 2026-11-18 ~19:01): contract v2, one
  channel (D-5).** The gateway sends all four operations (open,
  modify, reduce-volume, close); the Execution Bridge executes them
  against MT5. The §17.5 close-out (user approval + allowlist + Risk
  Engine/financial-consent proof on demo + schema bump) is executed
  in Phase 2b on demo; the Bridge never self-initiates an open.
  Kept here as a resolved record, not an open question.
- **O-3 — Probation thresholds.** Proposal: ≥20 demo trades, ≥80%
  proposal adherence, 14 days clean, to earn `auto_execute`. What are
  the real numbers?
- **O-4 — Golden eval sets.** The signal/coaching/scenario golden sets
  are trading decisions. Who writes and signs them off — you, or a
  trader you trust? The eval harness cannot run without them.
- **O-5 — News source tiers.** Proposal: tier-1 = FXStreet, ForexLive;
  tier-2 = CoinDesk, CoinTelegraph. Correct? Any source to add or drop?
  And the freeze window: 30 min before / 15 min after an importance-3
  event — right?
- **O-6 — Kill-switch scope.** When the daily-loss kill-switch trips:
  stop new engine actions only, or also close all open positions?
  (Close requires explicit pre-authorization per account.)
- **O-7 — 9Router determinism.** Temperature 0 is assumed supported
  and the model is assumed to accept a `seed`/deterministic mode.
  Which model(s) should the money-path prompts pin, and is temperature
  0 + strict JSON actually available on 9Router for them? Needs a live
  check before 2a promises determinism.
- **O-8 — Retention.** How long are decisions, factor runs, prompt
  outputs, scenario runs, and audit rows kept? (Proposal: decisions +
  audit 2 years, factor runs 1 year, scenario runs 1 year, prompt
  outputs 90 days — confirm or change.)
- **O-9 — Real-money scope.** Which brokers/account types first (demo
  broker in 2b is MetaQuotes-Demo; real in 2c is TBD), and is there any
  jurisdiction/tax reporting the engine must support? (F-66 is planned,
  not designed.)
- **O-10 — Liability and disclosure.** Auto-trade loses real money
  sometimes. The risk disclosure (OP-22) needs human review and the
  user's approval before 2c. Who reviews it, and what does "the user
  accepted the risk" look like in the product (per-account screen)?
- **O-11 — Fear/greed thresholds (mechanism decided, numbers open).**
  Mechanism is **C with a written boundary** (§3.7): mechanical
  protection automatic under the Risk Free policy; judgment coached,
  and intervened on only in auto mode with the intervention
  permission. What remains open: the exact thresholds (tilt-score
  value, early-exit count that fires a nudge/intervention) and the
  default intervention permission (off until the user enables it?).
- **O-12 — Dynamic TP/SL: default on or opt-in?** Are the TP/SL rules
  on by default for an entitled account, or does each user opt in per
  rule (or per account)? Consent model to be decided — the answer
  changes the onboarding and the settings UI.
- **O-13 — Virtual requests.** What counts as one virtual (simulated)
  request, and what quota per user/plan? (Phase 3 item; the definition
  does not exist yet and nothing in Phase 2 may assume one.)
- **O-14 — Home overview: public vs private.** Which metrics are public
  on the main home page (e.g. system win rate) vs per-user private
  (only after login)? The roadmap wants the system win rate as a home
  overview — the exact public set needs the user's call.
- **O-15 — TradingView / research sources.** Which sources are actually
  usable for the daily research loop (§16)? The MCP channel is verified
  as a research source; the authenticated live check (Essential+
  account, FX/gold CFD symbol coverage in the screener — community
  notes say it is thin on CFD-broker symbols — delay stability, any
  daily caps) is still open.
- **O-16 — Risk Free default.** What is the default `protection_level`
  for a new account (off / breakeven-only / breakeven+partial), and the
  default `trigger_R` and `partial_pct_at_trigger`? The policy object
  exists regardless; the default is the user's call.
- **O-17 — Adopted positions policy.** When PME first sees a position
  the engine did not open: start managing it immediately under the
  account's policy, or flag it for the user first and manage only
  after acknowledgement? (The design's default: manage protectively —
  breakeven/partial per policy — never scale in; the user confirms or
  changes this.)
- **O-18 — Duplicate handling strictness.** When a candidate duplicates
  an open position or a pending open: refuse outright, or allow it as
  an explicit `scale_in` with its own rule? (The design's default:
  refuse with `duplicate_position` unless the strategy explicitly
  allows scaling.)
- **O-19 — Unclear-result recovery policy.** When a command's broker
  outcome is unknown after TTL + one sync cycle: reconcile-and-hold
  (resolve by terminal state, never re-send — the design's default),
  or retry once with the same `command_id`? The retry path needs the
  user's call because a double-send is the failure it risks.

## 18. What is specified vs what is open (honest status)

The user ordered: **do not declare capabilities complete on paper
until the architecture, the inter-engine contracts, the real execution
path, and its tests are specified.** This table is that accounting.
"Specified" means written down here + in CONTRACTS.md with schemas,
state machines, and failure behavior. "Specified" does **not** mean
built, tested, or complete — nothing in this document is a claim that
the engine works. The engine works when §19's tests pass on demo.

| Specified in this revision | Still open (needs a decision or a test) |
|---|---|
| 8-engine architecture, inputs/outputs/consumers, latency budgets (§2–§3) | O-1 tick-speed split confirmation |
| Event bus rules + event catalog + message schemas (CONTRACTS.md) | O-3 probation thresholds |
| Decision record (extends `ai_agent_decisions`, §4) | |
| Scenario computation: TP/SL P/L, adverse scenarios, historical-frequency probabilities with n, new-vs-manage comparison (§3.3) | O-4 golden eval sets (author + sign-off) |
| Risk as optimizer: veto only on hard limits, every veto names an alternative (§3.4, D-4) | O-5 news tiers + freeze window |
| Position lifecycle incl. adoption of unmanaged positions; Risk Free policy object (§3.5) | O-6 kill-switch scope (close-all or not) |
| Execution path: one channel (gateway → Execution Bridge → MT5), all four operations, `order_check` pre-send, idempotency, duplicate prevention, reconciliation, recovery matrix (§3.8, CONTRACTS.md §7–§8) | O-7 9Router determinism live check |
| AI boundaries: allowed/forbidden call sites, never per tick, never in the order-send path, value measured per request (§5.2) | O-8 retention periods |
| Daily knowledge loop: parameters daily, rules weekly, versioned snapshots (§7.3) | O-9 real-money brokers + tax scope |
| Behavior detection + permissioned intervention (§8.4) | O-10 disclosure review + acceptance UX |
| Real metrics from closed trades only, with n (§9) | O-11 thresholds + default intervention permission |
| Postgres-only minimal data model + indexes per real query (§11) | O-12 TP/SL default on vs opt-in |
| Performance budgets + per-stage latency measurement (§12); data freshness classes + timestamp gate (§3.9); order trace for every order (§4); real latency metrics (§12.1) | O-13 virtual-request definition/quota |
| Transparency: jobs + decision feed (§13) | O-14 public vs private home metrics |
| Scalability: 5 services, statelessness rules, path to 5–6 servers (§14) | O-15 MCP authenticated live check |
| Test plan: unit/contract/integration/failure/live (§19) | O-16 Risk Free defaults |
| | O-17 adopted-positions policy |
| | O-18 duplicate-handling strictness |
| | O-19 unclear-result recovery policy |

## 19. Test plan (the execution path is not real until these pass)

Tests are specified here; the build phases run them. A capability is
**not** declared working until its row passes.

### 19.1 Unit tests (deterministic, fixtures, no network, no model)

- Every factor (F-01..F-70 as shipped): fixed `Dataset` fixture →
  exact expected values in `Decimal`; sample-size gates return
  `insufficient_data` below threshold.
- PRE scenario math: fixed position + fixed candles → exact
  `pnl_tp`/`pnl_sl`/EV_R; probability = counted frequency from the
  fixture (assert the count, not just the ratio).
- RCE: fixed ScenarioSet + account → exact size/SL/TP; every veto
  case asserts an alternative is present (D-4 test).
- PME rules R-TP-1..R-TP-8: fixed position states → exact
  `ManagementAction` (or `hold`); SL-never-against-trade asserted on
  every case; rate-limit asserted (second modify within 60 s refused).
- Risk Free policy: each `protection_level` 0/50/100 → exact trigger
  + partial fraction.
- Decision record: a decision without `rule_id`/`risk_check`/
  `scenario_ref` is rejected at the enqueue boundary (assert the
  rejection, not just the happy path).

### 19.2 Contract tests (schemas)

- Every event in the catalog validates against its JSON Schema
  (CONTRACTS.md §4); an invalid payload is rejected with a named
  reason and the emitting engine is flagged.
- Every prompt output validates against its `output_schema` (the
  §5.4 gates, run in CI).
- Decision-record replay: same `inputs_hash` + same versions →
  same decision fields (determinism gate, §5.4).

### 19.3 Integration tests (event flow, local Postgres, mocked MT5)

- Full loop on fixtures: candles batch → regime → strategy →
  scenarios → risk → decision → command → mocked `action.result` →
  reconciliation → factor update. Assert the decision record chain is
  complete at every step.
- Adoption: a positions batch containing a position the engine did
  not open → `managed_positions` row in `adopted` state with
  `origin: user|unknown`, and the first management action is
  protective, never a scale-in.
- News freeze: importance-3 event in window → entry vetoed with
  `news_freeze`; trailing frozen; freeze lifts after the window.
- Kill-switch: trip → no new engine actions; existing positions
  tightened to breakeven (R-TP-8); admin + user toggle clears it.
- Grant exhaustion: auto-trade request beyond `max_requests` →
  `403 grant_requests_exhausted` (SPEC ADDITION C behavior).

### 19.4 Failure tests (the ones that decide whether this is a real engine)

| Failure | Expected behavior (asserted) |
|---|---|
| WS to Bridge drops mid-command (`pending`) | No re-send; on reconnect, resolve by terminal state; command ends `reconciled`, never `completed` by assumption |
| `delivered` but no `action.result` | Wait one sync cycle, resolve by terminal state |
| Broker retcode unclear / unknown | Treated as unknown, never as success; resolved by terminal state |
| Duplicate open (same symbol+direction+strategy already open) | Refused with `duplicate_position` before enqueue; no second command row |
| Duplicate command retry (same `command_id`) | Replays the stored result; exactly one MT5 action |
| Stale price (regime/quote older than the symbol's timeframe) | Entry blocked; management continues under the last assessment |
| Partial close with volume > open volume | Clamped; oversize closes in full per MQL5 — asserted, not assumed; never leaves dust below `volume_min` |
| Local agent restart | Reloads envelope + pending actions from the server ledger, never from local memory alone |
| Postgres down mid-decision | Decision not recorded → command not enqueued (fail-closed for entries); in-flight management holds |
| AI provider down / timeout | Engines proceed code-only in degraded mode (labelled); no execution blocked or delayed by AI |
| Kill-switch trips mid-execution | In-flight close/modify completes (it is protective); no new opens; no new scale-ins |
| Terminal reports a position the ledger never commanded | `unmanaged` → adoption path (§3.5); engine never closes it without a rule + decision record |
| Command contradicts terminal state (`diverged`) | Flagged, escalated to user + admin; engine stops acting on that ticket until resolved |

### 19.5 Live smoke (demo account, Bridge + MT5 — like the p0–p7 harness)

- On MetaQuotes-Demo, with the real Bridge: open a demo position
  via the gateway (after the §17.5 close-out) → verify the decision
  record, the §17.3 ingest row, and the terminal state agree;
  partial-close it via the gateway reduce-volume operation → verify
  volume; move SL to breakeven via the gateway modify → verify;
  disconnect the Bridge mid-command → verify recovery
  by terminal state; adopt a manually opened demo position → verify
  PME manages it. **The live smoke is the gate for 2b, not an
  afterthought.** The p0–p7 harness (`~/workspace/bridge-verify-local/
  harness/verify_bridge.py`) is the template; it is extended, not
  replaced.

## 20. What this design deliberately does not contain

- No promise that the model is bit-deterministic (§5.1 is explicit
  about what determinism means here).
- **No paper trading as a product.** The user connects their own
  terminal (Demo or Real); execution runs on that terminal with
  explicit permission. A demo account is the test ground, not a
  simulated product.
- **No heavy server backtests.** Strategy evaluation uses existing
  historical data, real closed-trade results, and statistical analysis
  (§9, §7.3 weekly review) — not a server backtest engine.
- No new ML model training (the lexicon sentiment engine stays
  deterministic; the factor engine is arithmetic; scenario
  probabilities are counted frequencies).
- No social/copy trading, no cross-user data use, no leverage product.
- No change to PR #107, the gateway contract v1, or any live behavior.
- No dependency on TradingView (§16); no server-initiated `open_order`
  until the §17.5 close-out is executed in Phase 2b (demo first); no
  second open path — the Bridge never self-initiates an open.
- No capability declared "complete": §18 lists what is specified and
  what is open; completion is earned by §19's tests passing, starting
  on demo.
