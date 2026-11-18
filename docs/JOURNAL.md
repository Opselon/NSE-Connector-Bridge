# Trading Journal & Accounting Source Data

Status: Phase C3 (gap §5), branch `refactor/complete-mt5-infrastructure`.
Governing spec: `SPEC-2026-11-18-COMPLETE-INFRASTRUCTURE.md` §§8–9.
Gap analysis: `docs/GAP-ANALYSIS.md` §2.8, §2.9, §4 D6/D8, §5 Phase C3.

The Bridge provides raw execution facts and raw financial facts. It
does NOT do journal intelligence, accounting calculations, or
reporting — those are server-side (spec §1). There is no local
accounting engine anywhere in this subsystem: no P/L aggregation, no
sums, no statistics, no equity curves.

## 1. Journal events — `nse_bridge/journal/`

### 1.1 Event shape (stable — consumed by the Phase C4 Execution screen)

`JournalEvent.to_dict()` always carries exactly these keys:

| Field | Meaning |
|---|---|
| `event_id` | `evt-<sha256(dedup key)[:32]>` for identified events (stable across re-syncs); random for unidentified ones |
| `event_type` | One of the 16 types below |
| `ticket` | MT5 ticket of the record the event came from |
| `position_id` | MT5 position identifier |
| `order_id` | MT5 order identifier |
| `deal_id` | MT5 deal identifier |
| `symbol` | Exact broker symbol, verbatim (never a canonical guess) |
| `volume`, `price`, `sl`, `tp` | Raw broker values; `None` = not reported |
| `commission`, `swap`, `profit` | Raw broker values; `None` = not reported, never zero |
| `execution_time` | Broker event time converted to UTC ISO (`+00:00`) |
| `broker_time` | Verbatim broker timestamp string when the source carried one |
| `magic` | Magic number |
| `broker_retcode`, `retcode_label` | Original MT5 return code + its label |
| `correlation_id`, `source_command_id` | Command correlation, when the event traces to a command |
| `server_timestamp` | UTC receipt time on this Bridge (`...Z`) — connector time, not broker time |
| `source` | Local evidence: `mt5_history`, `mt5_state_delta`, `execution_response`, `terminal`, `account` |
| `detail` | Short sanitized note (never a secret, never a stack trace) |
| `account_login` | MT5 login the event belongs to (account binding) |

Event types (spec §8): `order_submitted`, `order_accepted`,
`order_rejected`, `position_opened`, `position_modified`,
`position_partially_closed`, `position_fully_closed`,
`pending_created`, `pending_modified`, `pending_canceled`,
`deal_executed`, `execution_response_received`, `broker_error`,
`terminal_connection_lost`, `account_connection_changed`.

### 1.2 Source-of-truth rule (the one rule that matters)

**Events are built ONLY from actual MT5 state and history. The
Bridge never invents a trading event and never infers a fill from a
submitted request.** A submitted order becomes `order_accepted` /
`deal_executed` only when MT5 history (or the broker's own response)
says so.

Source table — event → authoritative evidence:

| Event | Evidence |
|---|---|
| `order_submitted` | The execution path actually submitted the request (`record_order_submitted`). Records the submission only — no fill implied. |
| `order_accepted` | History order FILLED/PARTIAL, or broker retcode DONE (10009) on the execution response. |
| `order_rejected` | History order REJECTED, or a broker error retcode on the response. |
| `position_opened` | Snapshot diff: ticket appears in live positions. |
| `position_modified` | Snapshot diff: SL/TP changed (or volume changed with no corroborating deal — the change is reported, a close is NOT asserted). |
| `position_partially_closed` | Closed-position summary (`volume_out < volume_in`), or a snapshot volume drop corroborated by an OUT/INOUT deal for the same `position_id`. |
| `position_fully_closed` | Closed-position summary (`volume_out >= volume_in`), or a snapshot disappearance corroborated by an OUT deal. |
| `pending_created` / `pending_modified` | Snapshot diff on live orders. |
| `pending_canceled` | History order CANCELED/EXPIRED on a pending type. |
| `deal_executed` | History deal, trade type BUY/SELL — exactly one per deal. |
| `execution_response_received` | A real broker response to an execution request (the receipt fact, distinct from the outcome). |
| `broker_error` | A real broker error retcode / driver error. |
| `terminal_connection_lost` | A real driver connection-state transition. |
| `account_connection_changed` | A real account/login transition. |

Deliberate non-events:

- A position that merely **vanishes** from a snapshot — with no
  corroborating OUT deal and no closed-position summary — emits NO
  close event. The terminal may have restarted, the poll may have
  failed, or the close may not have happened. The next history pass
  emits the close with its deal facts.
- An order that vanishes from the live book emits nothing until
  history shows CANCELED / EXPIRED / FILLED.
- The first snapshot establishes the baseline and emits nothing:
  pre-existing positions are not "opened" by the Bridge.
- Balance/credit/fee deals (BALANCE, CREDIT, CHARGE, CORRECTION,
  BONUS, COMMISSION_*) emit NO journal trade event; they are
  accounting facts (§3 below).
- Records without a ticket id are dropped (cannot be reconciled —
  same rule as sync §3.3).

### 1.3 Dedup key

```
(event_type, deal_id | order_id | position_id | ticket, execution_time)
```

The first non-`None` identifier wins, in that order. An event
carrying neither an identifier nor an execution time (e.g. an order
submitted before the broker assigns a ticket) has **no key and is
never deduped** — each such submission is a distinct event. The
seen-set is bounded (default 10,000 keys, oldest evicted first) and
the emitted-event store is bounded too (default 1,000 events, oldest
dropped) — spec §16 data minimization. `JournalCollector.reset()`
clears both; the sync engine calls it on account switch (§7.3), so
the new account never inherits the old account's dedup state.

### 1.4 Range / pagination contract

- Every history collection takes **explicit `from_dt`/`to_dt`
  bounds** (both required; `from_dt > to_dt` raises `ValueError`).
  There is **no unlimited download by default**: the normal path is
  the sync checkpoint range `[checkpoint, now]`, and the server
  decides ranges (via `sync_request` or a manual resync).
- `collect_range(from_dt, to_dt, fetch_history_orders,
  fetch_history_positions)` passes the EXACT requested bounds to the
  fetchers (never widens them) and processes records in pages of
  `page_size` (default 500, mirroring sync §3.3).
- Records whose event time falls outside `[from_dt, to_dt]`, or that
  carry no parseable event time, are dropped and counted
  (`out_of_range_dropped`) — an event that cannot be placed in the
  requested range is not emitted.
- The collector **reuses the durable sync checkpoint** (B1:
  `SyncCheckpoint` + `SyncCheckpointStore`, owned by the sync
  engine). It advances the shared checkpoint from event execution
  times (max, never backwards) and calls the engine's persist
  callback. It builds **no second checkpoint mechanism**.
- The sync engine feeds the collector from records it already
  fetched (`sync_history` → `collect_from_history`,
  `send_positions` → `diff_snapshots`), so history is never fetched
  twice for the journal, and journaling never breaks sync (failures
  are logged, not raised).
- `SyncEngine.journal_events()` returns the current events as
  stable dicts — the Phase C4 Execution screen input.

### 1.5 What the collector does NOT do

No upload. No new endpoint, frame, or sync kind is invented here.

## 2. Accounting labelling — `nse_bridge/journal/accounting.py`

Every accounting value carries its provenance:

| Provenance | Meaning |
|---|---|
| `mt5_supplied` | Read verbatim from an MT5 record. |
| `locally_derived` | A local mapping/reconstruction over broker values (the underlying numbers are still the broker's). |
| `not_reported` | The broker did not supply this value in this context. |

`label_account_snapshot(account)` labels the `account`-batch fields:
balance, equity, margin, free margin, margin level, leverage,
floating profit, swap, commission, credit, currency, login, server,
name are all `mt5_supplied` (one `mt5.account_info()` record, read
verbatim). Two corrections to the gap analysis are recorded here:
`margin_level` **is** broker-supplied — `account_info()` exposes it
and the Bridge reads it verbatim; the local
`get_account_margin_level` command only formats it. `type`
(`demo`/`contest`/`live`) is `locally_derived` — the Bridge's label
over the broker's integer `trade_mode`. `realized_profit` is always
`not_reported` in the snapshot: realized P/L lives in deal history,
and this module never computes it.

`label_deal(deal)` labels per-field provenance: money/identity fields
`mt5_supplied`; `type_label`/`entry_label` `locally_derived` (local
label mapping over broker integer codes).

`label_closed_position(summary)` labels closed-position summaries
`locally_derived`: the summary is reconstructed locally from MT5
history deals (grouped by `position_id`), so its volumes and money
totals are local work over broker-supplied deals. The server must
treat the **deals**, not the summary, as the authoritative raw facts.

## 3. Balance operations — deposits, withdrawals, adjustments, fees

MT5 deal types are explicit (`DEAL_TYPE_LABELS`): BALANCE (2),
CREDIT (3), CHARGE (4), CORRECTION (5), BONUS (6), COMMISSION_* (7–12),
DEAL_SPLIT (13). `classify_deal(deal)` maps each deal to
`trade` | `deposit` | `withdrawal` | `balance_adjustment` | `fee` |
`credit`, keeping the raw broker values (amount = broker `profit`,
commission/swap/fee, time). BALANCE splits by the sign of its amount:
positive → deposit, negative → withdrawal, exactly zero → balance
adjustment. Unknown deal types classify as `balance_adjustment` with
the raw type preserved — never dropped, never guessed as a trade.

`surface_balance_operations(deals)` groups the non-trade deals into
`{deposits, withdrawals, balance_adjustments, fees, credits}`. Raw
broker values only — **no sums, no totals, no statistics**. Trade
deals are excluded (they are journal/deal facts, not balance
operations).

## 4. Wire placement (no new sync kind)

The labelling rides inside the existing §3.3 batches:

- `account` batch items gain `provenance: {field: provenance}` next
  to `account` / `terminal` (`SyncEngine.account_items`).
- Every `history_orders` page gains `provenance` and
  `balance_operations` (classified from that page's own deals;
  empty on orders pages).
- Every `history_positions` page gains `provenance` with
  `positions: locally_derived` (reconstructed from deals).

`count_items` is unaffected (`balance_operations` is not a counted
record list). No new batch kind, no new endpoint, no new WS frame.

## 5. Dependencies — D6 / D8 (Platform-side, not Bridge defects)

- **D6 — journal/event upload channel: NOT in CONTRACT v1.** No
  journal/event endpoint or frame exists. The only sync kinds are
  `account | positions | history_orders | history_positions` (§3.3);
  WS client→server frames are only `heartbeat`, `action.result`,
  `chart.data`, `chart.tick` (§5.3). Journal facts today can only be
  *derived server-side* from the sync batches. The collector is
  built and the event shape is stable and documented here, but the
  events have nowhere to be sent until a contract amendment defines
  a journal/event channel — or a formal statement that derivation
  from sync batches is the intended design (in which case the Bridge
  keeps only the local collector for the GUI/diagnostics, not an
  upload path). The Bridge invents no frame.
- **D8 — accounting channel beyond the four sync kinds: partially
  in CONTRACT v1 §3.3.** Balance/equity/margin/credit/deals already
  flow inside `account` / `history_*` batches. What was missing —
  deposit/withdrawal/adjustment/fee surfacing and MT5-vs-derived
  labelling — is now supplied as a labelling convention inside those
  same batches (§4 above). A contract statement is still owed on
  whether the four kinds suffice with this convention, or a new
  kind/field set is wanted. Until then, no new kind is invented.

## 6. What is still unobservable on Linux

MT5 history behaviour (history order states, deal entry/type values,
closed-position reconstruction) cannot be observed on this Linux VM:
there is no MT5 terminal here, and the MetaTrader5 package is
Windows-only. The mapping tables above are built from the MT5
record shapes the Bridge already models (`nse_bridge/mt5/types.py`:
`ORDER_STATE_LABELS`, `DEAL_TYPE_LABELS`, `DEAL_ENTRY_LABELS`) and
are covered by unit tests over model-dict fixtures — but the
end-to-end behaviour against a live terminal (real history-order
states for rejected/expired orders, real BALANCE-deal shapes, the
exact `volume_out`/`volume_in` relationship on partial closes) is
**unverified until the real-MT5 tests run on Windows**. A test must
not silently pass when the MT5 connection fails (spec §20); the
existing `tests/e2e/` labelling (unit / mock-integration / real-MT5 /
manual) applies unchanged.
