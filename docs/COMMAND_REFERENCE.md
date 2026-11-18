# Command Reference

Two surfaces, kept strictly separate. **Never imply the server can
send a local-only op.**

- **§1 — Server-channel commands (CONTRACT v1 §6.1):** exactly 3 ops
  the Platform may send over the WS channel. Auth, envelope,
  validation, and retry rules are the contract's.
- **§2 — Local commands (90):** the Bridge's local capabilities over
  the loopback REST API (`POST /api/v1/{name}`). Available to the
  local GUI/operator only — NOT to the server.

Full machine-readable schemas: `docs/openapi.json` (re-exported from
the registry with `scripts/export_openapi.py`; CI enforces freshness)
and Swagger at `http://127.0.0.1:8788/docs` while the Bridge runs.

## 1. Server-channel commands (CONTRACT v1 §6.1)

Auth: the mutually-authenticated WSS session itself (Bearer API key +
`X-NSE-Device-Id` in the upgrade headers). There is no per-command
signature in v1 (§6.2, delta D-CMD-3). Transport: WS frame
`{type: "command", …}` (§6.2 envelope — see docs/API_PROTOCOL.md §3.2).
Timeout: `expires_at` (default 5 min from `issued_at`, max 15 min);
a command that expires while queued is `EXPIRED`, never executed.
Idempotency: `command_id` (uuidv7, == `idempotency_key` in v1);
bounded replay cache of 10,000 ids; only a SUCCESSFUL validation
consumes the id — a rejected command's id stays reusable. Retry
policy: the Bridge NEVER self-retries a server command. On a
rejection the server decides; on `mcp_failed` with
`retry_safe: false` (every financial op in this phase) the server
must re-authorize with a NEW `command_id`. On `UNKNOWN` the server
must wait for reconciliation — never blind-retry. Error codes: the
§6.3 rejection reasons (`Malformed`, `Expired`, `WrongDevice`,
`WrongAccount`, `UnsupportedOperation`, `AutoTradeNotEnabled`,
`Replayed`) returned as `action.result` with `status: "rejected"`;
execution failures return `status: "mcp_failed"` with an `error`
object (`code`, `category`, `cause`, `recovery`, `retry_safe`).

| Operation | Request (`operation`) | Response (`action.result`) | Notes |
|---|---|---|---|
| `close_position` | `{type, ticket}` | `status: executed`, payload = raw MT5 close result | Idempotent ambiguous-close handling kept; on `TRADE_UNVERIFIED` reconciles (position gone ⇒ `executed`, recovered) |
| `modify_sl_tp` | `{type, ticket, sl?, tp?}` (nullable; absent = leave unchanged) | `status: executed`, payload = raw MT5 modify result | Absent sl/tp filled from the live position before dispatch |
| `modify_pending` | `{type, ticket, sl?, tp?}` (SL/TP only — no price/volume/expiration changes) | `status: executed`, payload = raw MT5 modify result | **Capability-gated (§17.4): starts REJECTED.** Live proof at market open + published minimum `connector_version` still owed (docs/PLATFORM-GAPS.md §4). Until `modify_pending_proven=True`, rejected as `UnsupportedOperation` exactly like a non-allowlisted op |

Explicitly NOT sendable in v1: `open_order`, `cancel_order`
(§6.1, §17.5). Any other `operation.type` → `UnsupportedOperation`
at validation step 5, before anything reaches MT5. Entitlement:
`position_manager` AND `auto_trade` (from `validate-key`) must both
be enabled, else `AutoTradeNotEnabled` (step 6).

Validation order (§6.3, exact): malformed → expired → wrong-device →
wrong-account → allowlist (→ `modify_pending` gate) → entitlement →
replay. Lifecycle (clarification #2): `RECEIVED → VALIDATED →
AUTHORIZED → DISPATCHED → EXECUTING → COMPLETED | FAILED | UNKNOWN |
EXPIRED`. Full detail: docs/API_PROTOCOL.md §3.

## 2. Local commands (90)

Auth: mandatory `X-API-Key` header on every local API request
(auto-generated on first run, stored in the OS secret store; empty
key = no access, never open). Transport: `POST /api/v1/{name}` on
`127.0.0.1:8788` (loopback by default; a non-loopback bind requires
explicit `allow_non_loopback` AND the same mandatory key).
Timeout: each broker call runs on the serialized MT5 worker thread
with a 60 s job timeout; individual MT5 calls use `mt5_timeout_ms`
(5,000 ms) with `mt5_retries` (3) bounded linear backoff inside
connect. Idempotency: the local API has NO request idempotency —
each POST executes. The exceptions are idempotent by design:
`trade_close_single_position` (close-by-ticket tolerates an
already-closed ticket via reconciliation) and the read-only
commands. Do not re-POST a mutating command to "make sure" — check
state first (`get_position_info` / `get_order_info`).
Retry policy: the Bridge does not retry a dispatched broker call
after an uncertain result; ambiguous outcomes surface as
`TRADE_UNVERIFIED` and must be reconciled against live MT5 state.
Error codes: every response is `{"ok": true, "data": …}` or
`{"ok": false, "error": {code, message, retcode?, details?}}` with
`code` from the local catalog: `MT5_NOT_CONNECTED`,
`MT5_CALL_FAILED`, `MT5_RETCODE` (with the official MT5 retcode
10004–10031 + label), `SYMBOL_NOT_FOUND`, `TICKET_NOT_FOUND`,
`VALIDATION_ERROR`, `NOT_FOUND`, `TRADE_REJECTED`,
`TRADE_UNVERIFIED`, `INTERNAL_ERROR`, `UNAUTHORIZED`,
`FEATURE_DISABLED`, `TIMEOUT`. HTTP status stays 200 for business
errors (the envelope carries the failure); 401 only for a missing or
invalid `X-API-Key`.

Request/response models below are the Pydantic models from the
registry; `—` means the command takes free-form JSON params (see the
field docs in Swagger / `docs/openapi.json`). `mutates = yes` means
the command dispatches a broker write.

| # | Name | Category | Mutates | Request model | Response model | Title |
|---|---|---|---|---|---|---|
| 1 | `get_bridge_status` | system | no | — | BridgeStatusResponse | Get Bridge Status |
| 2 | `get_mt5_connection_status` | connection | no | — | TerminalInfoResponse | Get MT5 Connection Status |
| 3 | `connect_mt5` | connection | no | ConnectRequest | ConnectResponse | Connect to MT5 |
| 4 | `disconnect_mt5` | connection | no | — | DisconnectResponse | Disconnect from MT5 |
| 5 | `reconnect_mt5` | connection | no | — | ConnectResponse | Reconnect to MT5 |
| 6 | `get_mt5_terminal_info` | connection | no | — | TerminalInfoResponse | Get MT5 Terminal Info |
| 7 | `get_mt5_terminal_version` | connection | no | — | TerminalVersionResponse | Get MT5 Terminal Version |
| 8 | `get_mt5_server_info` | connection | no | — | ServerInfoResponse | Get MT5 Server Info |
| 9 | `get_mcp_server_info` | system | no | — | McpServerInfoResponse | Get MCP Server Info |
| 10 | `get_trading_account_info` | account | no | — | object | Get Full Trading Account Info |
| 11 | `get_account_balance` | account | no | — | BalanceResponse | Get Account Balance |
| 12 | `get_account_equity` | account | no | — | EquityResponse | Get Account Equity |
| 13 | `get_account_margin` | account | no | — | MarginResponse | Get Used Margin |
| 14 | `get_account_free_margin` | account | no | — | FreeMarginResponse | Get Free Margin |
| 15 | `get_account_margin_level` | account | no | — | MarginLevelResponse | Get Margin Level |
| 16 | `get_account_profit` | account | no | — | ProfitResponse | Get Floating Profit |
| 17 | `get_account_currency` | account | no | — | CurrencyResponse | Get Account Currency |
| 18 | `get_trading_permissions` | account | no | — | TradingPermissionsResponse | Get Trading Permissions |
| 19 | `get_trading_symbols` | symbols | no | SymbolsListRequest | SymbolsListResponse | Get Trading Symbols |
| 20 | `get_symbol_info` | symbols | no | — | SymbolInfoResponse | Get Symbol Info |
| 21 | `get_symbol_price` | symbols | no | — | SymbolPriceResponse | Get Symbol Price |
| 22 | `get_symbol_tick_size` | symbols | no | — | TickSizeResponse | Get Symbol Tick Size |
| 23 | `get_symbol_point` | symbols | no | — | PointResponse | Get Symbol Point |
| 24 | `get_symbol_digits` | symbols | no | — | DigitsResponse | Get Symbol Digits |
| 25 | `get_symbol_spread` | symbols | no | — | SpreadResponse | Get Symbol Spread |
| 26 | `get_symbol_trading_sessions` | symbols | no | — | SessionsResponse | Get Symbol Trading Sessions |
| 27 | `get_symbol_trade_constraints` | symbols | no | — | ConstraintsResponse | Get Symbol Trade Constraints |
| 28 | `get_market_tick` | market | no | — | MarketTickResponse | Get Latest Market Tick |
| 29 | `get_market_bid` | market | no | — | MarketSideResponse | Get Market Bid |
| 30 | `get_market_ask` | market | no | — | MarketSideResponse | Get Market Ask |
| 31 | `get_market_last_price` | market | no | — | MarketLastPriceResponse | Get Last Trade Price |
| 32 | `get_market_depth` | market | no | — | MarketDepthResponse | Get Market Depth |
| 33 | `get_market_spread_history` | market | no | — | SpreadHistoryResponse | Get Spread History |
| 34 | `get_market_tick_volume` | market | no | — | TickVolumeResponse | Get Tick Volume |
| 35 | `get_market_watchlist` | market | no | — | WatchlistResponse | Get Market Watch List |
| 36 | `subscribe_market_ticks` | market | no | SubscribeRequest | SubscribeResponse | Subscribe to Live Ticks |
| 37 | `get_chart_history` | charts | no | ChartHistoryRequest | BarsResponse | Get Chart History |
| 38 | `get_chart_ticks_history` | charts | no | TicksHistoryRequest | TicksHistoryResponse | Get Tick History |
| 39 | `get_chart_current_timeframe` | charts | no | — | TimeframeInfoResponse | Get Current Timeframe |
| 40 | `get_chart_available_timeframes` | charts | no | — | AvailableTimeframesResponse | Get Available Timeframes |
| 41 | `get_chart_ohlcv` | charts | no | ChartHistoryRequest | OhlcvResponse | Get OHLCV Series |
| 42 | `get_chart_indicators` | charts | no | — | CapabilitiesNoteResponse | Get Chart Indicators |
| 43 | `get_chart_objects` | charts | no | — | CapabilitiesNoteResponse | Get Chart Objects |
| 44 | `get_chart_active_info` | charts | no | — | ChartActiveInfoResponse | Get Active Chart Info |
| 45 | `get_chart_multi_timeframe_history` | charts | no | MultiTimeframeRequest | MultiTimeframeResponse | Get Multi-Timeframe History |
| 46 | `get_trading_open_positions` | positions | no | PositionsRequest | PositionsResponse | Get Open Positions |
| 47 | `get_position_info` | positions | no | TicketRequest | PositionInfoResponse | Get Position Info |
| 48 | `get_position_profit` | positions | no | TicketRequest | PositionProfitResponse | Get Position Profit |
| 49 | `get_position_volume` | positions | no | TicketRequest | PositionVolumeResponse | Get Position Volume |
| 50 | `get_position_sl_tp` | positions | no | TicketRequest | PositionSlTpResponse | Get Position SL/TP |
| 51 | `get_position_risk` | positions | no | TicketRequest | PositionRiskResponse | Get Position Risk |
| 52 | `trade_modify_sl_tp` | positions | yes | ModifySlTpRequest | object | Modify Position SL/TP |
| 53 | `trade_close_single_position` | positions | yes | ClosePositionRequest | object | Close Single Position |
| 54 | `trade_close_all_positions` | positions | yes | CloseAllRequest | CloseAllResponse | Close All Positions |
| 55 | `get_trading_pending_orders` | orders | no | OrdersListRequest | OrdersListResponse | Get Pending Orders |
| 56 | `get_order_info` | orders | no | TicketRequest | OrderInfoResponse | Get Order Info |
| 57 | `get_order_status` | orders | no | TicketRequest | OrderInfoResponse | Get Order Status |
| 58 | `get_trading_history_orders` | orders | no | HistoryRequest | HistoryOrdersResponse | Get History Orders |
| 59 | `get_trading_history_positions` | orders | no | HistoryRequest | HistoryPositionsResponse | Get History Positions |
| 60 | `get_trading_deals_history` | orders | no | HistoryRequest | HistoryDealsResponse | Get Deals History |
| 61 | `trade_cancel_pending_order` | orders | yes | TicketRequest | CancelResponse | Cancel Pending Order |
| 62 | `trade_modify_pending_order` | orders | yes | ModifyPendingRequest | CancelResponse | Modify Pending Order |
| 63 | `get_order_execution_details` | orders | no | TicketRequest | OrderExecutionDetailsResponse | Get Order Execution Details |
| 64 | `trade_send_market_order` | trading | yes | MarketOrderRequest | TradeExecutionResponse | Send Market Order |
| 65 | `trade_send_buy_order` | trading | yes | SimpleOrderRequest | TradeExecutionResponse | Send Buy Order |
| 66 | `trade_send_sell_order` | trading | yes | SimpleOrderRequest | TradeExecutionResponse | Send Sell Order |
| 67 | `trade_send_pending_order` | trading | yes | PendingOrderRequest | TradeExecutionResponse | Send Pending Order |
| 68 | `trade_close_partial_position` | trading | yes | PartialCloseRequest | TradeExecutionResponse | Close Partial Position |
| 69 | `trade_reverse_position` | trading | yes | TicketOnlyRequest | object | Reverse Position |
| 70 | `trade_set_position_sl` | trading | yes | SetSlRequest | object | Set Position Stop Loss |
| 71 | `trade_set_position_tp` | trading | yes | SetTpRequest | object | Set Position Take Profit |
| 72 | `trade_calculate_order_result` | trading | no | CalcResultRequest | object | Calculate Order Result |
| 73 | `calculate_position_size` | risk | no | PositionSizeRequest | PositionSizeResponse | Calculate Position Size |
| 74 | `calculate_trade_risk` | risk | no | TradeRiskRequest | TradeRiskResponse | Calculate Trade Risk |
| 75 | `calculate_stop_loss_price` | risk | no | StopPriceRequest | PriceLevelResponse | Calculate Stop Loss Price |
| 76 | `calculate_take_profit_price` | risk | no | StopPriceRequest | PriceLevelResponse | Calculate Take Profit Price |
| 77 | `calculate_margin_required` | risk | no | MarginRequest | MarginResponse | Calculate Margin Required |
| 78 | `calculate_margin_level_after_trade` | risk | no | MarginLevelRequest | MarginLevelAfterResponse | Calculate Margin Level After Trade |
| 79 | `calculate_pip_value` | risk | no | PipValueRequest | PipValueResponse | Calculate Pip Value |
| 80 | `calculate_profit_loss` | risk | no | ProfitLossRequest | object | Calculate Profit/Loss |
| 81 | `validate_trade_request` | risk | no | ValidateTradeRequest | ValidateTradeResponse | Validate Trade Request |
| 82 | `get_mt5_last_error` | diagnostics | no | — | LastErrorResponse | Get MT5 Last Error |
| 83 | `get_bridge_logs` | diagnostics | no | LogsRequest | LogsResponse | Get Bridge Logs |
| 84 | `get_bridge_health` | diagnostics | no | — | HealthResponse | Get Bridge Health |
| 85 | `get_bridge_latency` | diagnostics | no | — | LatencyResponse | Get Bridge Latency |
| 86 | `get_mt5_connection_metrics` | diagnostics | no | — | ConnectionMetricsResponse | Get MT5 Connection Metrics |
| 87 | `get_trade_execution_metrics` | diagnostics | no | — | TradeMetricsResponse | Get Trade Execution Metrics |
| 88 | `get_bridge_capabilities` | diagnostics | no | — | CapabilitiesResponse | Get Bridge Capabilities |
| 89 | `get_mcp_available_tools` | diagnostics | no | — | McpToolsResponse | Get MCP Available Tools |
| 90 | `get_mt5_diagnostic_report` | diagnostics | no | — | object | Get MT5 Diagnostic Report |

### 2.1 Categories

- **system (1, 9):** bridge status, MCP server info.
- **connection (2–8):** MT5 connect/disconnect/reconnect, terminal
  info/version, server info. `connect_mt5` accepts an optional
  terminal path; discovery otherwise runs env →
  `NSE_MT5_TERMINAL_PATH`/`MT5_PATH` → standard install locations →
  App Paths registry → custom/portable directories → running
  processes.
- **account (10–18):** full account info, balance, equity, margin,
  free margin, margin level, floating profit, currency, trading
  permissions. Read-only.
- **symbols (19–27):** symbol list/info/price/tick size/point/
  digits/spread/sessions/constraints. Read-only.
- **market (28–36):** tick, bid, ask, last price, market depth (DOM),
  spread history, tick volume, watchlist, tick subscribe. Read-only.
- **charts (37–45):** candle history (position/range), tick history,
  timeframes, OHLCV, indicators, chart objects, active chart info,
  multi-timeframe history. Raw series only — no indicator math is
  computed here beyond returning what the terminal reports.
- **positions (46–54):** open positions, info, profit, volume,
  SL/TP, risk (read-only); `trade_modify_sl_tp`, `trade_close_single_position`,
  `trade_close_all_positions` (mutating; close-all accepts an optional
  `magic` filter and never touches manual positions when it is set).
- **orders (55–63):** pending orders, order info/status, history
  orders, history positions, deals history (read-only);
  `trade_cancel_pending_order`, `trade_modify_pending_order`
  (mutating).
- **trading (64–72):** market/buy/sell/pending orders, partial
  close, reverse, set SL/TP (mutating); `trade_calculate_order_result`
  (read-only preview).
- **risk (73–81):** position size, trade risk, SL/TP price, margin
  required, margin level after trade, pip value, PnL, validate trade
  request. **Pure calculators over MT5 symbol info — local capability
  only, never sent by the server.** CONTRACT §18 classifies position
  sizing as E (derived, Platform-side only); these endpoints exist
  for local validation/diagnostics, and the server channel excludes
  them via the §6.1 allowlist.
- **diagnostics (82–90):** MT5 last error, bridge logs, health,
  latency, connection metrics, execution metrics, capabilities, MCP
  tools, diagnostic report. Read-only. `get_bridge_capabilities`
  reports the local registry; it is NOT a server entitlement.

### 2.2 Local-only ≠ server-sendable — the boundary, stated once

Commands 1–90 are ALL local capability. The server channel (§1)
accepts exactly 3 operation types. A local-only command (for
example `trade_send_buy_order`, `calculate_position_size`,
`trade_cancel_pending_order`) can never arrive from the server: the
WS command path validates `operation.type` against the 3-op
allowlist at step 5 and rejects anything else as
`UnsupportedOperation` before MT5 is touched. The registry is the
same object in both paths; the authorization is different by design.

## 3. Bridge-management endpoints (local API, `/api/v1`)

Same auth (`X-API-Key`), same envelope. Not registry commands.

| Endpoint | Method | Purpose |
|---|---|---|
| `/settings` | GET / PUT | Current configuration (secrets masked); update + apply live |
| `/run_connection_checks` | POST | Pre-flight checks (driver, terminal, Algo Trading, permissions, market data, server reachability) with actionable hints |
| `/server_channel/restart` | POST | Restart the NSE server channel |
| `/account/subscription` | GET | Read-only account & subscription (server-resolved) |
| `/account/validate-key` | POST | Validate the API key against NSE Platform |
| `/updates/latest` | GET | Latest release from `releases/latest` (§3.5) |
| `/updates/status` | GET | Verified updater status |
| `/updates/check` | POST | Check for updates |
| `/updates/download` | POST | Download + verify + stage the latest release |
| `/mt5/terminals` | GET | Detected MT5 terminals (path, version, running) |
| `/mt5/terminals/open` | POST | Open the selected MT5 terminal |
| `/diagnostics/summary` | GET | Guided diagnostics summary (telemetry + self-healing states) |
| `/health` | GET | Plain liveness probe — **no auth** |
| `/ui`, `/docs`, `/openapi.json`, `/ws/ui` | GET / WS | Control Center, Swagger, schema, live GUI feed |
