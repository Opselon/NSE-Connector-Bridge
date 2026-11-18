# Symbols — discovery, metadata, resolution

Status: gap §5 C1 (spec §4). Branch `refactor/complete-mt5-infrastructure`.
Source of truth for the server contract: CONTRACT v1
(`NSE-CONNECTOR-CONTRACT.md`, NSE-Platform). Section references (§x)
are contract sections unless noted.

## 1. What this package is

`nse_bridge/symbols/` is the broker-aware symbol layer: it discovers
what the connected broker offers, carries full broker metadata, and
maps a server-supplied canonical instrument ID to the exact broker
symbol name MT5 needs. It is the layer the C2 execution agent uses to
get exact symbols — it never executes anything itself, and it invents
no server surface.

```
server canonical ID ("XAU/USD")
        │
        ▼
SymbolResolver.resolve() ──► ResolutionResult
        │                      status: resolved | ambiguous | not_found
        │                      exact_symbol: "XAUUSD.a" (verbatim for MT5)
        │                      candidates: [SymbolMetadata, ...]
        ▼
SymbolInventory ──► Mt5Driver.symbols_get() / symbol_info() / symbol_select()
        │
        ▼
MT5 terminal (the broker's symbol list — the only source of truth)
```

## 2. Canonical instrument IDs (BRIDGE-LOCAL scheme)

A canonical ID is `BASE/QUOTE` — both parts uppercase, slash-separated:

| Canonical ID | Meaning |
|---|---|
| `EUR/USD` | euro vs US dollar |
| `XAU/USD` | gold vs US dollar |
| `BTC/USD` | bitcoin vs US dollar |

`BASE` is the underlying instrument code: an ISO 4217 currency code
for FX and metals (`EUR`, `USD`, `XAU`, `XAG`), the asset code for
crypto (`BTC`, `ETH`), or — for indices, stocks and other
non-currency instruments — the root code the server defines (`US30`,
`SPX500`). `QUOTE` is the quote currency code.

Accepted input spellings are normalized, never reinterpreted:
`eurusd` → `EUR/USD`; `eur-usd`, `eur_usd`, `eur usd`, `eur/usd` →
`EUR/USD`. A bare 6-letter all-alpha code splits 3+3 (`EURUSD` →
`EUR/USD`). Anything else without a slash (`US30`, `SPX500USD`) is
UNPARSEABLE: `resolve()` reports `not_found` with that reason rather
than guessing where the base ends and the quote begins.

**BRIDGE-LOCAL — contract dependency D7.** CONTRACT v1 defines NO
canonical instrument format and NO symbol-inventory sync channel.
The only symbol data path the contract defines is the per-symbol
chart protocol (§17.1: `chart.request` / `chart.subscribe` /
`chart.data` / `chart.tick`); no sync kind carries symbols, and no
endpoint accepts a symbol inventory, metadata snapshot, or resolution
request/response. The `BASE/QUOTE` scheme and this resolver are
therefore a Bridge-local interface: they let the Bridge map a
server-supplied identifier to an exact broker symbol, but the server
never receives a full inventory through this package, and no frame or
endpoint is invented here. If the contract later defines a canonical
format or an inventory channel, `nse_bridge/symbols/canonical.py` is
the single place that adapts. Do NOT present a canonical ID to the
server as a contract format.

One documented broker alias exists: some brokers name gold `GOLD`
instead of `XAU` (spec §4 lists `GOLD` / `GOLD#` as gold symbols).
`BROKER_BASE_ALIASES = {"GOLD": "XAU"}` in `canonical.py` maps that
alias. It is an explicit, reviewable table — not a heuristic — and it
is the only alias.

## 3. Symbol metadata

`SymbolMetadata` (`nse_bridge/symbols/metadata.py`) carries every
spec §4 field: exact name, description, base/profit/margin currency,
digits, point, tick size, tick value, contract size, min/max volume,
volume step, stop level, freeze level, execution mode, filling modes,
trade mode, calculation mode, swap info, trading sessions, expiration
info, spread, margin requirements. Every field the broker does not
provide is `None` — never invented. `SymbolMetadata.MT5_FIELD_MAP`
documents, per field, which MT5 `SymbolInfo` property feeds it.

Two facts callers must know:

* **Execution mode ≠ calculation mode.** `trade_exemode` is the
  execution mode (request/instant/market/exchange);
  `trade_calc_mode` is the calculation mode (forex/futures/CFD/…).
  They are different properties and are read separately here. (Note:
  the pre-existing `SymbolInfoModel` in `nse_bridge/mt5/types.py`
  maps `trade_execution` from `trade_calc_mode`; that model is out of
  scope for this package and is NOT used here. Fixing that mapping is
  a follow-up, not part of C1.)
* **Session schedules are not exposed.** The MT5 Python API does not
  expose hourly quote/trade session schedules. `session_schedule` is
  therefore always `None` ("not exposed by the API", never a
  fabricated timetable). What the terminal does expose is live session
  aggregates (deals, turnover, open/close prices), carried in
  `session_stats`. Session schedules consequently cannot take part in
  ambiguity comparison — see §5.

## 4. Discovery

`SymbolInventory` (`nse_bridge/symbols/inventory.py`) wraps a
`SymbolSource` (the `Mt5Driver` satisfies it via the minimal
`symbols_get()` / `symbol_info()` / `symbol_select()` accessors added
in C1; tests use a fake source):

| Method | Meaning |
|---|---|
| `all_symbols()` | every symbol the broker reported |
| `visible_symbols()` | `visible` is True (Market Watch) |
| `hidden_symbols()` | `visible` is False, as reported |
| `search(substring)` | case-insensitive substring over name + description |
| `get(name)` | snapshot lookup by exact broker name |
| `metadata(name)` | live single-symbol record via `symbol_info(name)` |
| `select_for_market_watch(name)` | `symbol_select(name, True)` where permitted |
| `availability(name)` | tradability verdict (§6) |
| `refresh()` / `invalidate()` | re-read MT5 now / drop the snapshot |
| `summary()` | counts + the completeness caveat |

**Hidden-symbol completeness caveat.** `symbols_get()` is the only
inventory source the MT5 Python API offers, and what it returns for
HIDDEN symbols is driver/broker-dependent: the MCP action behind
contract §17.1.2 (`get_marketwatch_symbols {include_hidden: true}`)
is documented as the complete inventory (4096 symbols in the
Platform's E101), but whether the Python package's `symbols_get()`
returns the same complete set on every broker/terminal was NOT
verified on this platform (no MT5 runs on Linux) and is not asserted
here. The inventory reports exactly what `symbols_get()` returned —
`hidden_symbols()` lists the hidden rows the driver DID report — and
`summary()` says so. A symbol absent from the inventory is "not
reported by this terminal", not "does not exist at this broker". If a
broker hides symbols from `symbols_get()`, only a live-MT5
verification run can show it.

## 5. Resolution

`SymbolResolver` (`nse_bridge/symbols/resolver.py`) — the stable
public API the C2 execution agent consumes:

```python
from nse_bridge.symbols.resolver import SymbolResolver, ResolutionResult, SymbolMetadata
from nse_bridge.symbols.inventory import SymbolInventory

resolver = SymbolResolver(SymbolInventory(driver))
result = resolver.resolve("XAU/USD")
if result.status is ResolutionStatus.RESOLVED:
    broker_symbol = result.exact_symbol  # e.g. "XAUUSD.a" — send verbatim to MT5
```

| Method | Contract |
|---|---|
| `resolve(canonical_id, expected=None) -> ResolutionResult` | §5 rules below |
| `candidates(canonical_id) -> list[SymbolMetadata]` | all candidates, with full metadata — the comparison basis |
| `refresh()` | re-read the broker symbol list and rebuild the index now |
| `invalidate()` | drop the cached snapshot WITHOUT an MT5 call; the next read re-loads |
| `cache_state()` | cache diagnostics (`cached`, `age_s`, `ttl_s`, `symbols`) |

`ResolutionResult` has `status: resolved | ambiguous | not_found`,
`exact_symbol: str | None` (set ONLY when resolved),
`candidates: list[SymbolMetadata]`, `reason: str`, plus `requested`
(the input as received) and `canonical_id` (normalized `BASE/QUOTE`,
or `None` when unparseable).

### Resolution rules (never guess)

1. A verbatim broker-name match wins: if the input IS an exact broker
   symbol name (case-insensitive), it resolves to itself.
2. Otherwise the input must parse as a canonical `BASE/QUOTE` ID.
   Unparseable input → `not_found`.
3. Candidates are broker symbols whose normalized name equals
   `BASE+QUOTE` or that core plus a short alphabetic broker suffix
   (`M`, `PRO`, `ECN`, `A`, … — at most 5 letters; a longer tail is
   a different instrument, not a suffix), AND whose reported
   `currency_base` / `currency_profit` (when reported) equal `BASE` /
   `QUOTE`. A similar name with different currencies is a different
   instrument, not a candidate.
4. One candidate → `resolved`.
5. Several candidates → `ambiguous`, ALWAYS, unless the caller
   supplies `expected` metadata (e.g. `{"contract_size": 100}`) that
   leaves exactly one candidate standing. The full candidate list
   (with metadata) is returned so the server — never the Bridge — can
   decide.
6. Two candidates that are indistinguishable on all available
   metadata are `ambiguous` even WITH `expected` metadata: there is
   nothing left to distinguish them with.
7. No candidates → `not_found`.

Ambiguity comparison uses the distinguishing signature
(`SymbolMetadata.distinguishing_signature`): contract size, tick
value, tick size, digits, base/profit currency, trade mode,
calculation mode. The `ambiguous` reason names the fields that
actually differ, so the server can see what `expected` metadata
would disambiguate. `expected` keys must be `SymbolMetadata` field
names — an unknown key raises `ValueError` instead of being silently
ignored.

## 6. Availability

`SymbolInventory.availability(name)` returns an `Availability`
(`exists`, `visible`, `trade_mode`, `tradable`, `fully_tradable`,
`reason`):

* absent → not tradable ("not reported by this broker/terminal");
* trade mode DISABLED (0) → not tradable;
* LONG_ONLY / SHORT_ONLY / CLOSE_ONLY → tradable in the restricted
  sense, not fully tradable;
* FULL (4) → fully tradable;
* unreported trade mode → NOT tradable (the Bridge never assumes
  tradability it cannot see).

"Market closed" is NOT detectable from metadata — a closed market
still reports the same symbol record. It is detectable only from a
live tick (no fresh tick) or from the chart path, which reports
`market_closed` per contract §17.1.4. The availability reason never
claims a market state the inventory cannot see.

## 7. Cache and broker-environment changes

The resolver caches the broker inventory snapshot (not individual
mappings): `resolve()` / `candidates()` use the snapshot while it is
fresh (TTL, default 300 s) and re-read MT5 when it expires.
`refresh()` re-reads immediately; `invalidate()` drops the snapshot
without an MT5 call.

The app MUST call `invalidate()` (or `refresh()`) when the broker
environment changes. The tie-in is the B3 self-healing scenario
`broker_connection_change` (spec §13.10): its
`HealingContext.on_refresh_symbols` callback is the place to call the
resolver's `invalidate()`/`refresh()`, because a symbol list built
against the old broker server must never be assumed to carry over to
the new one. The resolver never re-resolves silently against a stale
broker.

## 8. What this package does NOT do

* No server channel: no endpoint, frame, or sync kind is invented
  (D7 — see §2).
* No execution: resolving a symbol never trades; C2 consumes the
  exact symbol name.
* No hardcoded broker symbols anywhere in `nse_bridge/` (the driver
  probe-symbol deviation was already fixed in B2: probes use the
  configured `bot_symbol` plus broker-reported symbols).
* No market-closed detection from metadata (see §6) and no claim
  about hidden-symbol completeness beyond what the driver reports
  (see §4).

## 9. Tests

`tests/unit/test_symbol_resolution.py` (label: unit, no MT5) — 29
tests against fake MT5 objects: metadata mapping incl. the
execution/calculation distinction, canonical parsing incl. refusal to
guess a split, discovery (all/visible/hidden/search), availability,
and resolution incl. the five required cases: (a) exact single match
resolves; (b) two similar names with differing contract size resolve
to the metadata-matching one; (c) two indistinguishable candidates
report ambiguous; (d) unknown canonical ID reports `not_found`;
(e) cache hit vs refresh/invalidate/TTL behaviour. Driver accessors
are covered with an injected fake MT5 module.
