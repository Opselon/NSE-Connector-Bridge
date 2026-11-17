# NSE MT5 Bridge

**Secure client-side bridge between the user's MetaTrader 5 terminal and the NSE Platform server.**
One endpoint per command (90 commands), full Swagger docs, real-time WebSocket channel to your server,
client GUI with connection checks & settings — packaged as a **single-file Windows `.exe`**.

📖 **API Reference (hosted Swagger): https://opselon.github.io/NSE-Connector-Bridge/**

```
┌──────────────────────────── Client PC ────────────────────────────┐
│  MetaTrader 5 terminal64.exe                                      │
│      ▲ official MetaTrader5 C++ IPC driver (serialized worker)    │
│  NSE-MT5-Bridge.exe (single file)                                 │
│   ├─ FastAPI 127.0.0.1:8788 — POST /api/v1/{command} + /docs      │
│   ├─ /ws/ui            — live dashboard feed (ticks, account)     │
│   ├─ outbound WSS      — NSE server channel (no inbound ports)    │
│   ├─ /ui               — client GUI (checks, settings, live data) │
│   └─ logs/bridge.log + bridge.jsonl (structured)                  │
└───────────────────────────────────────────────────────────────────┘
```

- **All broker calls run on a dedicated worker thread** (the MT5 IPC channel is
  process-global and not thread-safe) — the async API never blocks the event loop.
- **Trade safety ported from the production adapter:** official retcode map
  (10004–10031), `retcode 0` = IPC failure → `TRADE_UNVERIFIED` (never blind-retry),
  ambiguous-fill recovery, close idempotency, filling-mode resolution from the
  broker bitmask, stops/freeze-level pre-validation, ownership magic guard.
- **Security:** localhost-only bind, optional `X-API-Key` gate, outbound-only
  authenticated WebSocket to the platform (Bearer token = API key from the site),
  registry-enforced command allowlist on the channel.

## GUI (client)

Sidebar dashboard with: **Overview** (live cards + tickers with sparklines),
**Positions**, **Pending Orders**, **Logs**, **Connection Checks** (pre-flight
checklist: driver, terminal link, **Algo Trading button**, permissions, market
data, server reachability — with actionable hints), and **Settings** (paste the
API key generated on the NSE Platform site + server URL, magic, symbols, log
level — saved locally and applied immediately).

## Quick start (development)

```bash
py -3.13 -m venv .venv          # any clean Python 3.12/3.13
.venv/Scripts/python -m pip install MetaTrader5 numpy fastapi "uvicorn[standard]" \
    pydantic-settings websockets loguru pywebview
.venv/Scripts/python -m nse_bridge            # bridge + GUI on http://127.0.0.1:8788
.venv/Scripts/python scripts/mock_server.py   # reference NSE server on :9099
.venv/Scripts/python scripts/export_openapi.py docs/openapi.json   # re-export schema
```

- Dashboard: http://127.0.0.1:8788/ui
- Swagger: http://127.0.0.1:8788/docs
- OpenAPI schema: http://127.0.0.1:8788/openapi.json

## Single-file .exe

```bash
.venv/Scripts/python -m PyInstaller bridge.spec --noconfirm --clean
# → dist/NSE-MT5-Bridge.exe  (CI also builds it on every push — see Actions)
dist\NSE-MT5-Bridge.exe            # GUI (native window, browser fallback)
dist\NSE-MT5-Bridge.exe --gui none # headless service mode
```

## Configuration

`%LOCALAPPDATA%\NSEBridge\config.json` (dev: `~/.nse-bridge/config.json`),
env overrides with `NSE_BRIDGE_` prefix, or edit live via the GUI Settings tab /
`PUT /api/v1/settings`. Key fields:

| Field | Default | Meaning |
|---|---|---|
| `port` | 8788 | local API port |
| `local_api_key` | empty | optional `X-API-Key` gate for /api/v1 |
| `server_url` | empty | `wss://…` NSE server channel URL (empty = disabled) |
| `server_token` | empty | **API key issued on the NSE Platform site** (Bearer) |
| `device_id` | auto | persistent bridge identity |
| `magic` | 888101 | trading magic stamped on bridge orders |
| `bot_symbol` | XAUUSD | primary chart symbol |
| `strict_magic_guard` | false | refuse to touch positions/orders with a foreign magic |
| `broker_utc_offset_minutes` | 0 | broker server UTC offset for history windows |

## Command surface (90)

| # | Category | Commands |
|---|---|---|
| 1–9 | connection | bridge status, MT5 connect/disconnect/reconnect, terminal info/version, server info, MCP info |
| 10–18 | account | full account, balance, equity, margin, free margin, margin level, profit, currency, permissions |
| 19–27 | symbols | list, info, price, tick size, point, digits, spread, sessions, constraints |
| 28–36 | market | tick, bid, ask, last price, depth (DOM), spread history, tick volume, watchlist, tick subscribe |
| 37–45 | charts | candle history (pos/range), tick history, timeframes, OHLCV, MTF history, active context |
| 46–54 | positions | open positions, info, profit, volume, SL/TP, risk, modify SL/TP, close single, close all (magic-filterable) |
| 55–63 | orders | pending orders, order info/status, history orders, history positions (deals-reconstructed), deals, cancel, modify pending, execution details |
| 64–72 | trading | market order, buy, sell, pending order (pre-validated), partial close, reverse, set SL, set TP, calc result |
| 73–81 | risk | position size, trade risk, SL/TP price, margin, margin level after trade, pip value, PnL, validate request |
| 82–90 | diagnostics | last error, logs, health, latency, connection metrics, execution metrics, capabilities, MCP tools, diagnostic report |

Plus bridge-management endpoints: `GET/PUT /api/v1/settings`,
`POST /api/v1/run_connection_checks`, `POST /api/v1/server_channel/restart`,
`GET /api/v1/commands`, `GET /health`.

Every response: `{"ok": true, "data": …}` or `{"ok": false, "error": {code, message, retcode?, retcode_label?, details?}}`.
Broker rejections carry the official MT5 retcode (10004–10031) + label.

## WebSocket channel protocol (bridge ⇄ NSE server)

Outbound `wss`, Bearer token (= site API key) in the handshake.

| Direction | Message |
|---|---|
| bridge → server | `hello {device_id, bridge_version, protocol_version, mt5{state, connected, login}, capabilities}` |
| server → bridge | `command {id, command, params}` — registry-enforced allowlist |
| bridge → server | `command_result {id, ok, data\|error, latency_ms}` |
| server → bridge | `subscribe {symbol, interval_ms}` / `unsubscribe {symbol}` |
| server → bridge | `request_full_state` → bridge replies `full_state` (account + positions + orders + metrics) |
| bridge → server | `tick {symbol, bid, ask, …}` · `account_update` (1s) · `positions_update` (2s) · `orders_update` (3s) · `status_update` (10s) · `trade_event` (on every dispatch) · `ping` heartbeat |

`scripts/mock_server.py` is the working reference implementation.

## Tests (end-to-end, live terminal)

```bash
.venv/Scripts/python -m pytest tests/e2e/ -v          # read-only + calculations + upgrades
.venv/Scripts/python -m pytest tests/e2e/ -v -m trade # real-trade suites (0.01 lots, demo, auto-cleanup)
```

Trade suites (commands 46–72) execute real orders on the connected account and
require **Algo Trading ON** in the terminal (`Ctrl+E` until the toolbar button is
green — the Options checkbox alone is not enough). Positions/orders opened by
tests are cleaned up automatically.

> ⚠ `trade_close_all_positions` supports `{"magic": …}` filtering — pass the
> bridge magic so the user's manual positions are never touched (see
> `strict_magic_guard`).

## Known environment notes

- **Python 3.14 on dev machines with broken numpy/pip**: use a clean 3.12/3.13
  (the venv here uses uv-managed CPython 3.13.15).
- `terminal_info().trade_allowed` reflects the toolbar **Algo Trading button**;
  when OFF every dispatch is rejected with `10027` + comment
  "AutoTrading disabled by client" — no API can toggle it (UI security control).
  The GUI's *Connection Checks* tab detects this and shows the exact fix.
- Some brokers report unreliable `trade_tick_value` — the bridge computes value-
  per-unit via broker-native `order_calc_profit` instead.
