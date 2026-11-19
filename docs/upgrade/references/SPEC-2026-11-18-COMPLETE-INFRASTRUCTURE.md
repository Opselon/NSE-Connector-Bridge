# NSE CONNECTOR BRIDGE — COMPLETE INFRASTRUCTURE UPGRADE (GOVERNING SPEC)

Source: user spec received in the main chat, 2026-11-18 10:16 +0330.
Verbatim copy. This spec SUPERSEDES the client-only refactor as the
governing spec for the Bridge work. Standing boundaries unchanged:
all engines (analysis, AI, risk, position sizing, coaching) and
accounting/journal intelligence stay server-side; the local
90-command registry stays as LOCAL capability; only the server
channel enforces the contract's allowlist; NSE-Platform is NOT
modified in this task, no platform migrations.

Repository: "Opselon/NSE-Connector-Bridge"
Target Branch: "refactor/complete-mt5-infrastructure"
Delivery: One integrated Pull Request.
Platform Boundary: NSE Platform is an external system and must not be modified in this task.

---

0. MISSION — READ BEFORE IMPLEMENTATION

This is a complete infrastructure upgrade, not a cosmetic refactor.

We are building the local connectivity and execution infrastructure that will allow NSE Platform to communicate with, monitor, manage, and execute authorized operations through a user's MetaTrader 5 terminal.

The final objective is to allow NSE Platform to manage the user's trading environment through a reliable, secure, and comprehensive infrastructure.

The server must eventually be able to:

- Discover trading terminals.
- Discover connected accounts.
- Inspect account status.
- Discover broker-specific symbols.
- Retrieve market and instrument information.
- Retrieve positions and pending orders.
- Open positions.
- Close positions.
- Partially close positions.
- Modify positions.
- Modify SL/TP.
- Create, modify, and cancel pending orders.
- Monitor order execution.
- Monitor position changes.
- Retrieve deals and trading history.
- Collect account and financial information.
- Synchronize trading activity.
- Collect execution journals and operational events.
- Monitor the health of the local trading environment.
- Receive errors, warnings, and diagnostics.
- Recover from network, terminal, and execution failures.

The Bridge must provide the complete local infrastructure required to make these operations possible.

NSE Platform is the intelligence and control layer. NSE Connector Bridge is the local connectivity, data acquisition, monitoring, and execution layer. MetaTrader 5 is the actual trading environment.

Do not build another independent trading application.

Do not implement local AI agents, local trading strategies, local risk engines, or local trading decision-making.

Do not reduce this task to the existing three remote actions.

The existing platform contract must be respected, and every missing capability must be explicitly identified as a contract dependency rather than silently invented.

---

1. TARGET ARCHITECTURE

Implement a clean, modular architecture:

                    NSE PLATFORM
                  CENTRAL CONTROL
                          |
       ------------------------------------------
       |             |            |             |
   AI / ML       Trading       Accounting    Analytics
   Engines       Management     & Journals    & Coaching
       |             |            |             |
       ------------------------------------------
                          |
                  COMMAND ORCHESTRATOR
                          |
                 SECURE REST / WS API
                          |
                NSE CONNECTOR BRIDGE
                          |
       ------------------------------------------
       |             |            |             |
   Connection    Command       Account &       Telemetry
   Manager       Dispatcher    Market Data      & Logs
       |             |            |             |
       ------------------------------------------
                          |
                  MT5 ABSTRACTION
                          |
                     MT5 DRIVER
                          |
                    MT5 IPC / API
                          |
                   META TRADER 5
                          |
                    BROKER SERVER

The architecture must support future expansion without requiring another major rewrite.

Responsibility boundaries

NSE Platform owns

- AI and machine learning.
- Trading strategies.
- Technical analysis.
- Fundamental analysis.
- News analysis.
- Trading decisions.
- Risk calculations.
- Position sizing.
- Portfolio management.
- Trading coaching.
- Journaling intelligence.
- Accounting calculations and reporting.
- Performance analytics.
- Subscription and entitlement management.
- User-facing trading recommendations.
- Command orchestration and authorization.

Bridge owns

- Secure communication.
- Device and terminal registration.
- MT5 discovery.
- Broker and symbol discovery.
- Account information acquisition.
- Market data acquisition.
- Order and position execution.
- Execution verification.
- State synchronization.
- Operational monitoring.
- Structured logging.
- Error reporting.
- Local diagnostics.
- Recovery and reconnection.
- Secure credential storage.
- Software updates.
- Future server-directed local computation interfaces.

The Bridge must not independently decide whether a user should buy, sell, close, or modify a position.

---

2. COMPLETE SERVER-TO-CLIENT COMMUNICATION

This is the foundation of everything.

Implement a production-grade communication layer.

REST API Client

Support the methods required by the actual platform contract:

- GET
- POST
- PUT
- PATCH
- DELETE

Requirements:

- Correct API base URL.
- TLS certificate verification.
- Authentication headers.
- Request correlation IDs.
- Structured payload serialization.
- Request validation.
- Configurable timeouts.
- Error classification.
- Retry policies.
- Exponential backoff with jitter.
- Request cancellation where supported.
- Connection pooling.
- Graceful shutdown.
- Response validation.
- Clear distinction between transport failures and application errors.

Never retry non-idempotent trading requests blindly.

Persistent WebSocket

Implement a persistent outbound WebSocket connection.

Required functionality:

- Contract-compliant authentication.
- Connection lifecycle management.
- Heartbeat.
- Connection health monitoring.
- Automatic reconnection.
- Exponential backoff and jitter.
- Network interruption recovery.
- Server restart recovery.
- Message validation.
- Message acknowledgement.
- Correlation IDs.
- Duplicate detection.
- Command expiration.
- Structured responses.
- Graceful disconnect.
- Connection state reporting.

The Bridge must be able to recover without requiring a manual application restart.

Protocol Compatibility

Inspect the complete official NSE Platform contract before implementation.

The actual contract is authoritative.

Do not invent:

- Endpoints.
- Message types.
- Authentication schemes.
- Event names.
- Payload formats.
- Synchronization rules.
- Command permissions.

Where the existing Bridge protocol conflicts with the platform contract, correct the Bridge.

Where the platform contract does not yet expose a required capability, create a clear compatibility report and a well-defined local interface for future support.

Do not modify NSE Platform in this task.

---

3. COMPLETE MT5 TERMINAL DISCOVERY & MANAGEMENT

Build a comprehensive terminal discovery and management subsystem.

It must support:

Terminal discovery

- Detect installed MT5 terminals.
- Detect multiple MT5 installations.
- Identify executable paths.
- Identify terminal data directories.
- Identify terminal versions.
- Identify running and stopped terminals.
- Detect inaccessible terminals.
- Detect invalid paths.
- Identify the selected terminal.
- Re-discover terminals after configuration changes.

Terminal lifecycle

- Connect to a selected terminal.
- Disconnect safely.
- Reconnect.
- Detect unexpected shutdown.
- Detect terminal restart.
- Detect IPC failures.
- Monitor terminal responsiveness.
- Detect account disconnection.
- Detect unsupported terminal states.

Multiple terminal support

The architecture must not assume that one user always has exactly one MT5 installation.

Support multiple discovered terminals with explicit selection and independent connection state.

Never accidentally send a command to the wrong terminal or account.

Each operation must be associated with the correct device, terminal, and account context.

---

4. BROKER & SYMBOL INTELLIGENCE

This is a critical requirement.

Do not hardcode symbols such as "XAUUSD", "EURUSD", or "GBPUSD".

Different brokers use different naming conventions.

Examples:

XAUUSD
XAUUSDm
XAUUSD.a
XAUUSDpro
GOLD
GOLD#
EURUSD
EURUSDm
EURUSD.a
EURUSD-ECN

These may represent similar instruments, but they are not automatically interchangeable.

Build a robust broker-aware symbol discovery and metadata subsystem.

Symbol discovery

Support:

- Discover all available symbols.
- Retrieve visible symbols.
- Retrieve hidden symbols.
- Search symbols.
- Resolve exact broker symbol names.
- Inspect symbol availability.
- Select symbols for market watch where permitted.
- Detect symbols that cannot currently be traded.
- Detect missing or unavailable symbols.
- Refresh symbol metadata.

Symbol metadata

Collect all useful broker-provided properties, including:

- Exact symbol name.
- Description.
- Base currency.
- Profit currency.
- Margin currency.
- Digits.
- Point.
- Tick size.
- Tick value.
- Contract size.
- Minimum volume.
- Maximum volume.
- Volume step.
- Stop level.
- Freeze level.
- Execution mode.
- Filling modes.
- Trade mode.
- Calculation mode.
- Swap information.
- Trading sessions.
- Expiration information where applicable.
- Spread information.
- Margin requirements where available.

Do not assume all brokers provide identical metadata.

Symbol resolution

Create a symbol-resolution abstraction that can:

1. Receive a canonical instrument identifier from the server.
2. Discover actual broker symbols.
3. Compare broker metadata.
4. Identify candidate matches.
5. Return exact broker symbol names.
6. Report ambiguity instead of guessing.
7. Cache valid mappings.
8. Refresh mappings when the broker environment changes.

Never automatically treat two similarly named instruments as identical without sufficient metadata.

For example, a gold CFD and another gold-related instrument may have different contract sizes, trading sessions, and tick values.

The server must receive the exact broker symbol and its metadata.

---

5. COMPLETE ACCOUNT INFORMATION & SYNCHRONIZATION

Build a reliable account-data acquisition layer.

Retrieve the actual information exposed by MT5, including:

- Account login identifier.
- Broker/server name.
- Account currency.
- Balance.
- Equity.
- Margin.
- Free margin.
- Margin level.
- Leverage.
- Account trade permissions.
- Expert Advisor permissions.
- Account trade mode.
- Account connection state.
- Account currency and relevant properties.

Do not expose credentials or passwords.

Account monitoring

Monitor relevant account-state changes.

The server must be able to obtain updated account information without unnecessary polling or excessive network traffic.

Use event-driven updates where possible and controlled polling where required.

Account identity

Every account-related message must be associated with the correct:

- User.
- Device.
- Terminal.
- Account.
- Broker/server.

Never mix data from different accounts.

---

6. COMPLETE ORDER EXECUTION INFRASTRUCTURE

This is one of the most important modules.

The Bridge must provide a robust execution layer for all operations supported by the local MT5 API and authorized by the current server contract.

Market orders

Support the infrastructure for:

- BUY.
- SELL.
- Requested volume.
- Exact broker symbol.
- Order type.
- Execution parameters.
- SL.
- TP.
- Deviation.
- Magic number.
- Comment.
- Filling mode.
- Execution result.

Position management

Support:

- Retrieve open positions.
- Inspect position details.
- Close full position.
- Close partial volume.
- Modify Stop Loss.
- Modify Take Profit.
- Modify SL and TP together.
- Close by supported position reference.
- Verify actual resulting position state.

Pending orders

Support:

- BUY LIMIT.
- SELL LIMIT.
- BUY STOP.
- SELL STOP.
- BUY STOP LIMIT.
- SELL STOP LIMIT, where supported.

Also:

- Retrieve pending orders.
- Modify pending orders.
- Cancel pending orders.
- Verify pending-order state.
- Detect expiration.
- Report broker rejection.

Order lifecycle monitoring

Track the difference between:

- Command received.
- Command validated.
- Command dispatched.
- Order request submitted.
- Broker response received.
- Order accepted.
- Order rejected.
- Order partially filled.
- Order fully filled.
- Position created.
- Position modified.
- Position closed.
- Execution state unknown.

Do not confuse a successfully submitted request with a successfully executed trade.

Broker-specific execution handling

Account for:

- Invalid volume.
- Invalid price.
- Invalid stops.
- Insufficient margin.
- Market closed.
- Trading disabled.
- Symbol unavailable.
- Unsupported filling mode.
- Price changed.
- Requote.
- Partial execution.
- Execution timeout.
- Connection interruption.
- Broker rejection.

Return structured, normalized errors while preserving the original MT5 return code and relevant diagnostic details.

---

7. ORDER SAFETY, IDEMPOTENCY & RECOVERY

Every command must have a well-defined lifecycle.

Use a state model such as:

RECEIVED
   |
VALIDATED
   |
AUTHORIZED
   |
DISPATCHED
   |
EXECUTING
   |
   +---- COMPLETED
   |
   +---- PARTIALLY_COMPLETED
   |
   +---- FAILED
   |
   +---- UNKNOWN
   |
   +---- EXPIRED

Requirements:

- Command ID.
- Correlation ID.
- Idempotency key where supported.
- Expiration timestamp.
- Duplicate detection.
- Account and terminal binding.
- Authorization validation.
- Execution timeout handling.
- State reconciliation.
- Structured execution result.
- Safe recovery after restart.

Critical rule

If an opening-order request times out, the Bridge must not blindly send the same order again.

It must first inspect actual MT5 orders, positions, and relevant deals to determine whether the original operation was executed.

Implement safe reconciliation for uncertain execution outcomes.

Do not claim exactly-once execution unless the underlying broker/API guarantees it. Design for duplicate resistance and recoverable execution.

---

8. TRADING JOURNAL & EXECUTION EVENT DATA

NSE Platform will own the intelligent trading journal.

The Bridge must provide reliable raw events and execution facts.

Collect relevant events such as:

- Order request submitted.
- Order accepted.
- Order rejected.
- Position opened.
- Position modified.
- Position partially closed.
- Position fully closed.
- Pending order created.
- Pending order modified.
- Pending order canceled.
- Deal executed.
- Execution response received.
- Broker error.
- Terminal connection lost.
- Account connection changed.

Where available, include:

- Ticket.
- Position identifier.
- Order identifier.
- Deal identifier.
- Exact broker symbol.
- Volume.
- Price.
- SL.
- TP.
- Commission.
- Swap.
- Profit.
- Execution time.
- Magic number.
- Broker return code.
- Correlation ID.
- Source command ID.

The Bridge must not invent trading events or infer fills from a submitted request.

Use actual MT5 state and history as the source of execution facts.

Support history retrieval with bounded time ranges and pagination or incremental synchronization where possible.

Prevent duplicate journal events during synchronization.

Do not download unlimited historical data by default.

The server decides which history ranges and data are required.

---

9. ACCOUNTING & FINANCIAL DATA

The Bridge must expose the underlying financial facts needed by the server's accounting and reporting engines.

Collect available data concerning:

- Balance.
- Equity.
- Credit.
- Margin.
- Free margin.
- Floating profit/loss.
- Realized profit/loss.
- Commission.
- Swap.
- Deal history.
- Deposit operations.
- Withdrawal operations.
- Balance adjustments.
- Trading fees where exposed.
- Account currency.
- Relevant timestamps.

Do not implement a second accounting engine locally.

Do not independently calculate final accounting reports.

Return raw broker-provided values and accurately identified derived fields.

The server will calculate:

- Net performance.
- Trading statistics.
- Profit factors.
- Win/loss ratios.
- Drawdown.
- Equity curves.
- Account performance.
- Fees and accounting reports.
- Journal analytics.

Clearly distinguish values supplied by MT5 from values calculated locally.

---

10. MARKET DATA & SYMBOL MONITORING

Provide a reliable interface for server-requested market data.

Support available MT5 capabilities for:

- Bid.
- Ask.
- Last price where available.
- Spread.
- Tick timestamp.
- Tick data.
- OHLC rates.
- Requested timeframes.
- Historical rates.
- Symbol trading state.
- Market session information.

Requirements:

- Explicit symbol identification.
- Explicit timeframe.
- Bounded historical queries.
- Efficient requests.
- Cache only where beneficial.
- Detect stale data.
- Detect missing data.
- Detect unavailable symbols.
- Preserve broker timestamps.
- Avoid unnecessary continuous transmission.

Do not send every market tick to the server by default.

The server must control subscriptions, symbols, timeframes, and update frequency.

---

11. LIVE STATE MONITORING

Build a state-monitoring subsystem for:

- Terminal health.
- Account connection.
- Account permissions.
- Current positions.
- Pending orders.
- Order changes.
- Deal updates.
- Symbol availability.
- Trading permissions.
- Connection status.
- Execution status.

Use the most efficient supported combination of event notifications and controlled polling.

The server must be able to detect important state changes without requiring excessive polling.

Support periodic reconciliation to identify missed events.

Do not assume that every MT5 change produces a reliable push notification.

---

12. CENTRALIZED LOGGING, TELEMETRY & DIAGNOSTICS

Logging is a core production feature, not an optional debugging feature.

NSE Platform must be able to monitor the operational health of connected Bridge installations.

Log levels

Level| Policy
ERROR| Immediate reporting when connectivity permits
WARN| Reliable reporting of important problems
INFO| Meaningful lifecycle and operational events
DEBUG| Local diagnostic mode, disabled by default

Required event categories

- Application startup and shutdown.
- Authentication.
- Device registration.
- REST communication.
- WebSocket communication.
- Heartbeat.
- Reconnection.
- MT5 discovery.
- MT5 connection.
- Account connection.
- Symbol discovery.
- Symbol resolution.
- Order execution.
- Position management.
- Pending-order management.
- Synchronization.
- History retrieval.
- Broker errors.
- Command validation.
- Command authorization.
- Command expiration.
- Duplicate detection.
- Configuration errors.
- Security events.
- Update operations.
- Unexpected exceptions.

Structured log schema

Each event should include applicable fields:

{
  "timestamp": "UTC timestamp",
  "severity": "ERROR",
  "event_type": "mt5.execution.failed",
  "component": "execution",
  "device_id": "registered-device-id",
  "terminal_id": "terminal-id",
  "account_id": "account-id",
  "command_id": "command-id",
  "correlation_id": "correlation-id",
  "symbol": "XAUUSD.a",
  "error_code": "normalized-error-code",
  "broker_code": 100xx,
  "message": "Sanitized diagnostic message",
  "retryable": false
}

This is an illustrative schema, not a new platform contract. Adapt the actual payload to the official contract.

Reliable log delivery

Implement:

- Bounded local event queue.
- Persistent delivery of important unsent events.
- Retry with backoff.
- Duplicate protection.
- Batch transmission where supported.
- Queue limits.
- Expiration.
- Successful-delivery acknowledgement.
- Safe cleanup.
- Queue-health monitoring.

If the network disconnects, important ERROR and WARN events must not disappear immediately.

Do not build an unlimited logging database.

Do not store unnecessary successful-operation noise.

Security

Never log:

- Passwords.
- Access tokens.
- API secrets.
- Full authentication headers.
- Sensitive credentials.

Sanitize all exception messages and diagnostic payloads.

---

13. AUTOMATIC RECOVERY & SELF-HEALING

Implement recovery for at least these conditions:

1. Internet disconnects.
2. Server becomes unavailable.
3. WebSocket disconnects.
4. Authentication expires.
5. Device registration becomes invalid.
6. MT5 terminal closes.
7. MT5 terminal restarts.
8. MT5 IPC becomes unavailable.
9. Account disconnects.
10. Broker connection changes.
11. Selected terminal path becomes invalid.
12. Symbol becomes unavailable.
13. Command response is lost.
14. Command is duplicated.
15. Command expires.
16. Application restarts during execution.
17. Local diagnostic queue becomes full.
18. Configuration becomes invalid.
19. Update download fails.
20. Update verification fails.

Every failure must have:

- Detection.
- Classification.
- Structured reporting.
- Safe recovery.
- Final status.
- Relevant diagnostic context.

Do not silently swallow exceptions.

Do not implement fake recovery.

When automatic recovery is unsafe, stop and report the exact reason.

---

14. SECURITY & DEVICE TRUST

Implement production-grade security.

Requirements:

- Windows Credential Manager or DPAPI.
- Secure token storage.
- Secure token rotation.
- Token revocation support.
- Device registration.
- Device revocation.
- Account-device association.
- TLS certificate verification.
- Local API authentication.
- Strict input validation.
- Replay protection.
- Request expiration.
- Command authorization.
- Secret redaction.
- Safe configuration handling.
- Secure update verification.

The local API must never be open to unauthenticated trading requests.

Bind local services to loopback by default.

Never accept arbitrary remote executable code.

The Bridge may execute only known, validated, authorized operations and approved future workload modules.

---

15. CLIENT RESOURCE MANAGEMENT & PERFORMANCE

The Bridge must remain lightweight.

Do not introduce unnecessary databases, background workers, polling loops, or large dependencies.

Requirements:

- Non-blocking GUI.
- Non-blocking communication.
- Efficient async or threaded execution boundaries.
- Bounded queues.
- Connection pooling.
- Efficient serialization.
- Controlled polling.
- Minimal memory overhead.
- Graceful shutdown.
- No unbounded task creation.
- No duplicate polling loops.
- No unnecessary market-data transmission.

The user's local hardware may eventually support server-directed computational workloads, but this phase must only establish a safe extensibility boundary.

Do not implement speculative AI or GPU engines now.

---

16. LOCAL STORAGE & DATABASE POLICY

Follow a strict data-minimization policy.

The Bridge is not an accounting database or trading-history warehouse.

Persist only what is necessary for:

- Secure configuration references.
- Device identity.
- Connection recovery.
- Pending important diagnostic events.
- Execution recovery metadata.
- Update and installation state.
- Necessary synchronization checkpoints.

Avoid storing:

- Full market tick history.
- Unlimited trading history.
- Duplicate account snapshots.
- Full AI analysis.
- Trading strategies.
- Redundant calculated statistics.
- Unnecessary event copies.

If persistent storage is required, use the simplest suitable solution.

Preserve low latency and bounded storage.

Support clean operation without introducing PostgreSQL or another server-style database locally unless a demonstrated technical requirement makes it necessary.

---

17. GUI — PROFESSIONAL CONNECTOR CONTROL CENTER

The UI must reflect actual infrastructure state.

Required sections:

Connection

- NSE connection.
- Authentication.
- Device registration.
- WebSocket.
- Heartbeat.
- Last successful synchronization.
- Reconnection state.

MetaTrader

- Discovered terminals.
- Selected terminal.
- Terminal version.
- Running/stopped state.
- IPC status.
- Account connection.
- Broker/server information.

Account

- Account identity.
- Account currency.
- Balance.
- Equity.
- Margin.
- Free margin.
- Account connection state.
- Server-provided subscription and permissions.

Symbols

- Available symbols.
- Exact broker symbol names.
- Search.
- Symbol metadata.
- Symbol availability.
- Resolution ambiguity.

Execution

- Recent command lifecycle.
- Execution status.
- Failures.
- Pending operations.
- Reconciliation state.

Diagnostics

- ERROR events.
- WARN events.
- Connection history.
- MT5 health.
- Server synchronization.
- Diagnostic report export.

Updates

- Installed version.
- Latest stable version.
- Update status.
- Release notes.
- Download progress.
- Verification status.

Do not turn the GUI into a second AI trading dashboard.

Remove obsolete local AI Agent functionality and local trading decision-making.

---

18. SOFTWARE UPDATE & RELEASE ENGINEERING

Implement a reliable update foundation.

Requirements:

- Version detection.
- Stable and prerelease channels.
- Release metadata.
- Signed manifest verification.
- SHA-256 artifact verification.
- Signature verification using a trusted public key.
- Secure download.
- Download integrity checks.
- Safe installation.
- Rollback or recovery strategy.
- Release notes.
- Clear update status.

Never install an unverified executable.

Do not trust SHA-256 alone if the expected hash itself is obtained from an unauthenticated source.

---

19. FUTURE SERVER-DIRECTED COMPUTATION

Create a clean architectural boundary for future local workloads.

NSE Platform may eventually assign expensive computations to local hardware to reduce unnecessary server load.

The future architecture must allow:

- Server-assigned tasks.
- Task identification.
- Task authorization.
- Input validation.
- Execution limits.
- Resource limits.
- Cancellation.
- Timeout.
- Result serialization.
- Result delivery.
- Task status reporting.

Only approved and trusted modules may execute.

Do not implement arbitrary remote code execution.

Do not build speculative analysis engines in this phase.

---

20. TESTING & CI — REAL EVIDENCE REQUIRED

A successful EXE build is not sufficient.

Create a complete automated testing foundation.

Unit tests

- REST client.
- WebSocket lifecycle.
- Authentication.
- Token handling.
- Command validation.
- Command routing.
- Idempotency.
- Expiration.
- Symbol resolution.
- Symbol metadata.
- Account mapping.
- Execution state machine.
- Reconciliation.
- Logging.
- Diagnostic queue.
- Retry policies.
- Configuration.
- Update verification.

Integration tests

- REST server mock.
- WebSocket server mock.
- Connection interruption.
- Reconnection.
- Duplicate messages.
- Expired commands.
- Lost acknowledgements.
- Server restart.
- Local API authentication.
- Diagnostic event delivery.

MT5 tests

Use a dedicated Windows environment and controlled DEMO accounts.

Test where possible:

- Terminal discovery.
- Multiple terminals.
- Account discovery.
- Symbol discovery.
- Symbol metadata.
- Market data.
- Position retrieval.
- Order retrieval.
- Order execution.
- Position modification.
- Partial close.
- Full close.
- Pending orders.
- History retrieval.
- Execution reconciliation.

Never use real-money trading in CI.

A test must not silently pass when the MT5 connection fails.

Clearly report tests as:

- Unit.
- Mock integration.
- Real MT5 integration.
- Manual verification.

CI pipeline

Add:

- Lint.
- Formatting checks.
- Static analysis.
- Unit tests.
- Integration tests.
- Security checks where applicable.
- Windows EXE build.
- Artifact verification.

Keep mandatory CI fast.

Do not introduce slow Wine-based execution into mandatory PR checks when native Windows testing can provide the required coverage.

---

21. MULTI-AGENT ENGINEERING CONTRACT

Use specialized agents where appropriate.

Suggested responsibilities:

1. Architecture and platform-contract audit.
2. REST and WebSocket communication.
3. MT5 driver and terminal management.
4. Symbol discovery and broker compatibility.
5. Order execution and recovery.
6. Account data and journal synchronization.
7. Logging, telemetry, and diagnostics.
8. Security and credential management.
9. GUI integration.
10. Testing and CI.

All agents must work against one shared architecture and agreed interfaces.

No agent may independently introduce a competing command protocol, duplicate data store, or conflicting execution state machine.

Require integration reviews before merging agent work into the final branch.

No fake implementations.

No placeholder code for mandatory functionality.

Future extension interfaces are allowed only when clearly documented as future scope.

---

22. DATABASE & PERFORMANCE RULES

These rules apply to every implementation decision:

- Store only useful data.
- Avoid duplicate state.
- Keep critical execution paths lightweight.
- Do not persist data merely because it is available.
- Do not store market ticks indefinitely.
- Do not replicate server-side accounting.
- Do not introduce unnecessary abstractions.
- Keep interfaces modular.
- Prefer measurable performance improvements.
- Preserve existing useful functionality.
- Do not break the existing MT5 driver without a tested replacement.

The best architecture is not the one with the most components.

It is the one that provides the complete required capabilities with the smallest reliable implementation.

---

23. DELIVERY PLAN

Perform the work as one integrated infrastructure upgrade.

First, inspect:

- Complete repository structure.
- Existing MT5 driver.
- Existing 90-command registry.
- REST client.
- WebSocket implementation.
- Configuration.
- GUI.
- Existing tests.
- CI workflows.
- Supplied NSE domain.
- Complete official platform contract.

Produce a gap analysis and implementation plan.

Then implement the actual infrastructure.

Do not stop at the audit.

Do not ask for another approval merely because you discovered expected implementation details.

If an external platform capability is missing, document it as a dependency and implement the compatible Bridge-side boundary without inventing a server protocol.

Do not modify NSE Platform.

Do not modify platform migrations.

---

24. REQUIRED PULL REQUEST LIFECYCLE

Create one integrated PR:

"refactor/complete-mt5-infrastructure"

The required workflow:

INSPECT
   |
PLAN
   |
IMPLEMENT
   |
TEST
   |
STATIC ANALYSIS
   |
COMMIT
   |
PUSH
   |
CREATE ONE PR
   |
CI / REVIEW
   |
   +---- FAILURE
   |       |
   |    INSPECT
   |       |
   |    DIAGNOSE
   |       |
   |      FIX
   |       |
   |    RETEST
   |       |
   |    PUSH UPDATE
   |       |
   |    RETURN TO CI
   |
   +---- SUCCESS
           |
       VERIFY ACCEPTANCE
           |
       REPORT RESULTS
           |
       WAIT FOR MY
       EXPLICIT MERGE APPROVAL

Do not stop after creating the PR.

Do not merge.

Do not push directly to main.

Do not split the implementation into multiple unrelated PRs.

Do not claim success without test evidence.

---

25. FINAL ACCEPTANCE CRITERIA

The infrastructure upgrade is complete only when:

- [ ] REST communication works against the actual platform contract.
- [ ] Persistent WebSocket works correctly.
- [ ] Authentication and device registration are implemented.
- [ ] Connection recovery works.
- [ ] MT5 terminal discovery works.
- [ ] Multiple terminal handling is safe.
- [ ] Account information is collected correctly.
- [ ] Broker-aware symbol discovery works.
- [ ] Symbol metadata is available.
- [ ] Symbol ambiguity is handled safely.
- [ ] The existing useful MT5 command infrastructure is preserved.
- [ ] Supported remote execution operations work.
- [ ] Position management is reliable.
- [ ] Pending-order management is reliable.
- [ ] Order execution states are accurate.
- [ ] Duplicate and expired commands are handled.
- [ ] Uncertain executions are reconciled.
- [ ] Trading history and deal data can be synchronized.
- [ ] Journal events are delivered without duplication.
- [ ] Accounting source data is available to the server.
- [ ] Operational monitoring works.
- [ ] ERROR and WARN events reach the server reliably.
- [ ] Diagnostic delivery survives temporary network failures.
- [ ] Credentials are stored securely.
- [ ] Local API is protected.
- [ ] GUI reflects real infrastructure state.
- [ ] Secure update verification is implemented.
- [ ] Automated tests run in CI.
- [ ] Windows EXE builds successfully.
- [ ] No fake tests or swallowed failures remain.
- [ ] No unnecessary local database or duplicated server engine has been introduced.
- [ ] Future server-directed local computation has a clean extension boundary.
- [ ] One integrated PR is created and all CI failures are addressed.
- [ ] PR remains unmerged pending explicit authorization.

---

FINAL DIRECTIVE

Do not think of this as a small MT5 connector.

Think of it as the local infrastructure layer of a much larger algorithmic trading platform.

NSE Platform must eventually be able to manage the user's trading environment comprehensively: positions, orders, accounts, symbols, execution, history, journals, monitoring, and diagnostics.

The Bridge must make these operations possible through secure, reliable, observable, and recoverable infrastructure.

The server will own intelligence, strategies, analysis, coaching, accounting, and trading decisions.

The Bridge will own communication, local data acquisition, MT5 interaction, execution, monitoring, and operational reliability.

Upgrade the infrastructure once, properly, and comprehensively. Do not leave fundamental capabilities for another superficial refactor.

Implement, test, push, create the PR, resolve CI failures, and wait for explicit merge approval.
