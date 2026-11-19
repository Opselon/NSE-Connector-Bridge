# Prompt — Upgrade the NSE-Connector-Bridge for the new NSE-Platform structure (Phase 2b enabler)

> Give this prompt to an agent to upgrade the Bridge and open a PR. The coordinating agent (main chat) reviews it; merge only with the user's explicit approval. **Do not merge, deploy, or tag.**

## 1. Mission

Upgrade `Opselon/NSE-Connector-Bridge` from **v1.0.0** to the Bridge the new NSE-Platform structure needs — the **Phase 2b execution enabler**: the gateway sends all four operations (open / modify / reduce-volume / close) on **demo accounts**, and the Bridge executes them against the user's MT5 terminal exactly once, verifiably, with recovery. Output: **one PR to `main`**, no merge, no deploy, no tag. The Bridge is the MT5 adapter, not a trading product — the Platform decides; the Bridge receives, validates, executes, and reports. That boundary does not move.

## 2. Sources — read first, in order

> The four files below are vendored snapshots, copied into this repo under `docs/upgrade/references/` so the upgrade work can be done from GitHub alone. The NSE-Platform repo stays the source of truth for the contract; do not edit the snapshots in place — propose changes in the PR body.

1. **The Bridge repo** (`main`, tag `v1.0.0` → `d7bbf3d`): `README.md`, `docs/API_PROTOCOL.md`, `docs/ARCHITECTURE.md`, `docs/ACCEPTANCE.md` (§25 is the governing acceptance record), `docs/PLATFORM-GAPS.md` (D1–D13, mostly Platform-side — read it so you don't re-invent a Platform endpoint), `nse_bridge/server/commands.py` (the §6.1 allowlist + §17.4 close-out steps in its docstring).
2. `docs/upgrade/references/NSE-CONNECTOR-CONTRACT.md` (2,525 lines) — **the governing contract, CONTRACT v1** with the §17 amendment: §6 (vocabulary, wire envelope, validation order, `action.result`, lifecycle), §17.1 (chart frames), §17.2 (effective config), §17.3 (decision audit), §17.4 (`modify_pending` close-out), §17.5 (`open_order` open decision), §18 (operation inventory). **Do not rewrite it** — contract changes go in the PR body as proposals; the Platform side amends it.
3. `docs/upgrade/references/SPEC-2026-11-18-COMPLETE-INFRASTRUCTURE.md` (§§0–25) — the Bridge's governing spec: broker-aware symbol discovery that never guesses, full execution incl. partial completion, raw-facts journaling, accounting from raw broker values, live-state monitoring, telemetry, self-healing.
4. `docs/upgrade/references/DESIGN.md` — §2 **D-5 (one execution channel: gateway → Execution Bridge → MT5)**, §3.8 (EXE: Bridge responsibilities, mandatory pre-send `order_check`, policy envelope), §10 **Phase 2b** (what this upgrade enables) and Phase 2c (what it must NOT enable yet); plus `docs/upgrade/references/CONTRACTS.md` §7 (wire shapes) and §8 (reconciliation + recovery matrix).
5. Verified current state (verify, don't re-derive): v1.0.0 released 2026-11-18; PR #3 (real-MT5 defects D1–D4) and PR #4 (Inno Setup installer, AppId GUID `b80bf455-6a18-41e5-bfa2-03a04736ee8f`) merged; gateway PR #107 merged and live (`https://bridge.nexusverse.space`, health 200); live proof exists — `close_position` ticket 152913954047 closed in 380 ms through Gateway → Bridge → MT5; 556 unit/integration tests pass, 104 e2e need a live MT5 terminal.

## 3. Scope — workstreams, in order

**W0. Baseline + compliance audit (no code yet).** Run the full suite (`pytest tests/unit tests/integration -q`), `ruff`, `mypy` (default + `--platform win32`), `bandit`, `pip-audit`, and the Windows exe build; record the numbers. Audit the Bridge against contract §6 + §17 and spec §§0–25; put a gap table (done / partial / missing / Platform-owned) in the PR body. Acceptance: gap table + baseline numbers, zero code changes.

**W1. `modify_pending` close-out (§17.4).** Add the `ModifyPending` variant + allowlist entry in `server/commands.py`; wire `trade_modify_sl_tp` with an **order** ticket, SL/TP only (price/volume/expiration are out — no verified MT5 action). Tests: allowlisted pass; wrong-device / wrong-account / replay / expiry rejections; a test pinning that `modify_pending` with a **position** ticket is still rejected (or that the ticket-type distinction is enforced — pin the semantics). Live proof at market open that `trade_modify_sl_tp` accepts a pending-order ticket (E-record, same style as E009). Acceptance: §17.4 steps (a)–(d) done or owed with the blocker named; the op stays `UnsupportedOperation` until the gate is proven and a minimum `connector_version` is published.

**W2. Mandatory pre-send `order_check` on the gateway command path (§3.8).** `order_check` exists as a local risk helper (`engine/risk.py`) — that is **not** this work. Wire it into the gateway command path: before **every** `order_send`, run MT5 `order_check`, record the result, refuse the send on failure (insufficient margin, invalid volume, market closed), and report the refusal with the check result as the command outcome. A passing `order_check` is **never** a guarantee of execution — say so in the code comment and the payload. Acceptance: tests proving a failing check refuses the send and reports the check result, and a passing check still surfaces a failed send honestly.

**W3. Partial completion + reduce-volume (§3.8, D-INV-4).** Wire the existing local `trade_close_partial_position` to the server channel: implement `close_position` **with `volume`** (the gateway reduce-volume operation). Partial facts go in `action.result` (requested vs filled volume, retcode 10010 `DONE_PARTIAL`); wire status stays `executed` with the partial facts in the payload. **Never retry a partial.** The `volume` wire shape is a contract change — propose the exact shape in the PR body, don't invent it unilaterally. Acceptance: a test with a mocked partial fill asserting the payload carries requested/filled volume and no retry is scheduled; the proposed wire shape in the PR body.

**W4. Unclear-result reconcile (never resend).** A `None` result or retcode **10031** (no connection) does **not** mean the order failed. On either, reconcile from `positions_get` / `history_deals_get` **before doing anything else**, then report the reconciled outcome. Never blind-retry, never resend. Acceptance: a test proving None/10031 triggers reconciliation and reports the reconciled state — not a retry, not a duplicate order.

**W5. `open_order` §17.5 close-out — implement, do NOT activate.** Implement the `open_order` path (market + pending) **behind a version gate**: it stays test-rejected as `UnsupportedOperation` until (1) the user explicitly approves, (2) the allowlist addition lands with tests, (3) Risk Engine + financial-consent proof on demo, (4) a `schema_version` bump. This PR does (2) and the code for (3); it must NOT do (1) or (4) on its own. The Bridge **never self-initiates an open** — opens arrive only as gateway commands. Acceptance: `open_order` implemented and test-covered, still rejected by default; the PR body lists the four §17.5 steps with their status and names the user's approval as outstanding.

**W6. Policy envelope (D-5) — the Bridge's only autonomous authority.** Implement the policy envelope: parse + validate the server-issued envelope (`tpsl_policy_envelopes`: version, bounds, rules, `issued_at`, `expires_at`); allow **fast trailing / breakeven only inside a fresh envelope** (≤ 5 min, WS up); every such move is reported via §17.3 with `rule_id` + `decision_id` and reconciled against the envelope version. Envelope stale or WS down → **hold**: in-flight trailing completes under the last envelope, then stops; no moves outside an envelope, ever. Acceptance: tests proving (a) a trailing move inside a fresh envelope executes and is reported with `rule_id` + envelope version, (b) a stale envelope blocks it, (c) WS down blocks new moves after the in-flight one completes.

**W7. Symbol discovery + chart frames (§17.1, spec §4).** Verify the broker-aware resolver/inventory against the live Platform symbol registry: canonical identifier in, exact broker symbol name out, **ambiguity reported instead of guessed**; cache valid mappings; refresh when the broker environment changes. Verify the §17.1 WS frames against the live gateway (`chart.request` / `chart.subscribe` / `chart.unsubscribe` in, `chart.data` / `chart.tick` out), with the Market Watch prerequisite chain (`add_marketwatch_symbol` first, cleanup skipped when E199 blocks it), the 20-subscription limit, the 60-frames/min sub-budget, and the §17.1.4 error codes. Acceptance: resolver tests pass against a live broker inventory (or the exact reason it cannot is recorded); chart frames proven against the live gateway or owed with the blocker named. No hardcoded symbol list anywhere.

**W8. Telemetry + diagnostics (spec §12, contract §4.2).** The 9-field structured telemetry pipeline exists locally with a bounded queue (retry/backoff/dedup/expiry) — keep it. The upload endpoint does **not** exist in contract v1 §4.2 (Platform gap D1) — **do not invent one**. Expose the well-defined local interface the Platform endpoint will consume when it lands, and keep reporting truthfully through existing channels (`action.result` errors, heartbeat/liveness, manifest). Bridge telemetry keeps **INFO for meaningful lifecycle events** (its governing spec requires it); do not let INFO creep into the Platform/Connector log layers (ERROR + WARNING only) — the two rules must not be conflated. Acceptance: events still carry exactly the 9 fields; no fake metrics (a missing value is reported as missing, never synthesized); the upload-ready interface in the PR body.

**W9. Traceability — raw broker facts in every `action.result`.** Every command outcome carries the raw broker facts the Platform's order trace needs (`GET /api/v1/connector/orders/{ticket}/trace`): `command_id` correlation, retcode, fill price/volume when known, the `order_check` result, `started_at`/`completed_at`/`duration_ms`, and the verbatim MT5 payload where available. No summarising away of the raw facts. Acceptance: a test asserting an `action.result` for a close carries `command_id`, retcode, and fill volume; the trace fields listed in the PR body.

**W10. Installer + release v1.1.0.** Bump `BRIDGE_VERSION` to `1.1.0`; regenerate `version_info.txt`, the release manifest, and `docs/openapi.json` (CI freshness checks must pass). **Never change the installer AppId GUID `b80bf455-6a18-41e5-bfa2-03a04736ee8f`** — same AppId = upgrade in place; changing it orphans installed copies. Provision the release-signing public key (the updater is fail-closed without it — currently unprovisioned, ACCEPTANCE row 13) and add a real `.ico` asset; document both in the PR body, including what still needs the user's action if the key cannot be provisioned by the agent. Acceptance: Windows CI green (`build-windows` incl. exe build + smoke check); tagged-release artifacts (exe + setup + SHA-256 + `release-manifest.json`) produced by CI, not by hand.

**W11. Live verification harness (p7-style, real MT5 demo).** The previous Wine/MT5 harness was wiped by a VM replacement — rebuild it (or run against a real Windows + MT5 demo terminal if one is available; say which you used). The proof bar is the p7 pattern: a real command executed end-to-end **Gateway → Bridge → MT5 on a demo account**, with the result reconciled against live MT5 state — the reference is the earlier proof (`close_position` ticket 152913954047 closed in 380 ms). Prove at minimum: a real `close_position` + a real `modify_sl_tp` on demo through the gateway, a partial close with the partial facts in `action.result`, and an unclear-result (None/10031) reconcile. Record ticket numbers, retcodes, and timings. Acceptance: the harness run is recorded (commands, tickets, retcodes, timings) in the PR body; anything not proven live is owed with the exact blocker — never claimed as proven.

## 4. Forbidden — do not build

No local AI, strategy, risk engine, trading decisions, backtest, or indicator computation in the Bridge — all intelligence stays server-side. No `open_order` activation without the user's explicit approval (W5 implements behind the gate; it does not open the gate). No real-money trading — demo accounts only; Phase 2c is a separate decision. No contract rewrite, no Platform changes, no Platform migrations (amendments go in the PR body as proposals). No change to the installer AppId GUID. No second local open path (one execution channel, D-5). No fake data, no invented metrics, no hardcoded symbol list, no silent guessing in symbol resolution.

## 5. Ground rules (never break)

- **One execution channel:** gateway → Bridge → MT5. The Bridge never opens orders itself and never invents policy; its only autonomous authority is fast trailing/breakeven inside a fresh policy envelope.
- **Order_check is mandatory** before every send; a passing check is never a guarantee.
- **Never resend on unclear:** None / 10031 → reconcile from `positions_get` / `history_deals_get` first.
- **Never guess a symbol:** broker-aware discovery only; ambiguity is reported, not resolved by similarity.
- **Only real data:** a missing value is reported as missing; nothing is synthesized. Raw broker facts are preserved verbatim in `action.result` and the journal.
- **Secrets:** Credential Manager / DPAPI only, never plaintext `config.json`, never in logs, never in code or chat. Log redaction stays on.
- **Logging:** Bridge telemetry keeps INFO for meaningful lifecycle events; do not add INFO to the Platform/Connector log layers.
- **Thin client:** the Bridge exposes operations; the Platform decides when to call them. The 90+ local commands stay local capability; the server channel enforces the allowlist at the boundary — never the reverse.
- **Tests prove, reports don't claim:** every acceptance criterion names the test or live proof that shows it. Anything unproven is listed as owed with the blocker, never claimed.

## 6. Open questions — do not guess

- **O-2 (§17.5):** the user's explicit approval for `open_order` — not yet given. Implement behind the gate; list it as outstanding.
- **Partial-close wire shape:** the `volume` field on `close_position` needs a contract amendment — propose the exact shape; the Platform side decides.
- **Diagnostics upload endpoint (D1):** contract v1 §4.2 defines none. Build the local upload-ready interface; do not invent the endpoint.
- **Minimum `connector_version` for `modify_pending`:** published by the Platform side; record the version this PR ships as the candidate.
- **Demo probation thresholds (O-3):** the user's numbers for earning `auto_execute` (≥20 closed demo trades, ≥80% follow-through, 14 clean days are the proposal) — do not set them yourself.
- **Live MT5 access:** whether the Wine harness is rebuilt or a real Windows terminal is used — state which, and what it proves.
- Anywhere else a user decision is needed, list it in the PR body with a safe (conservative) default implemented — **do not guess and do not take the user's decision**.

## 7. Method

1. New branch `feat/bridge-phase-2b` from `main` (work in a separate worktree).
2. Work through W0–W11 in order; keep the existing test suite green throughout (`pytest tests/unit tests/integration -q` — currently 556 passed).
3. Local verification before the PR: `ruff`, format check, `mypy` default + `--platform win32`, `bandit`, `pip-audit`, version/OpenAPI freshness checks, `pytest tests/unit tests/integration`, and the Windows exe build + smoke check.
4. If a test is timing-sensitive under load, fix the test or the code — never weaken an assertion to make it pass, and never claim an e2e pass without a live terminal.
5. Open the PR to `main`; PR body: workstreams done with their proof, the gap table (W0), contract-amendment proposals (W3/W5/W8), the live-verification record (W11), and the open questions (section 6).
6. **Do not merge, do not deploy, do not tag.** Final report: PR link + local verification numbers + CI status + blockers (if any).

## 8. Definition of done

- PR is open, CI is green (Linux + Windows).
- W1–W6, W9–W10 are implemented with the tests named in their acceptance criteria passing; W7/W8/W11 are proven live or explicitly listed as owed with the blocker named.
- `open_order` is implemented behind its gate and still rejected by default; no real-money path exists.
- The AppId GUID is unchanged; the contract document is untouched; no Platform code was modified.
- No fake data, no invented endpoint, no guessed symbol, no weakened test.
