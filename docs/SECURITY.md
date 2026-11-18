# Security

Threat model: the Bridge runs on the user's own Windows machine, holds
the user's Platform API key, and can move real money through the MT5
terminal. The design assumes the local machine may run other software
(any local process could reach loopback) and the network may be
hostile (TLS + authenticated channel only). Secrets must never appear
in plaintext on disk, in logs, in error messages, or in result
payloads.

## 1. Secrets — storage

| Secret | Storage | Notes |
|---|---|---|
| `server_token` (user API key) | Windows Credential Manager via `keyring` (service `NSEBridge`) | Runtime-only field; never in `config.json` |
| `local_api_key` | Same | Auto-generated on first run when missing; mandatory (see §2) |
| Device credential | Same | |
| `update_public_key` | `config.json` / env `NSE_BRIDGE_UPDATE_PUBLIC_KEY` / bundled `nse_bridge/update_public_key.b64` | PUBLIC key material — safe to store in config; **no key is provisioned yet** (see §5) |

Implementation: `nse_bridge/secrets.py`. On Windows the primary
store is `KeyringSecretStore` (Credential Manager, DPAPI-backed);
if keyring is unavailable, `DpapiSecretStore` encrypts secrets with
`CryptProtectData` into `secrets.dpapi.json` under the data dir.
DPAPI failure is a hard error — never a silent plaintext fallback.
On non-Windows there is a clearly-marked **DEV-ONLY** file fallback
(`dev-secrets.json`); it is never used for tests and must never ship
in the exe path.

Legacy migration: any plaintext secret found in an old `config.json`
(`mt5_password`, `agent_llm_api_key`, `server_token`,
`local_api_key`) is migrated into the store and **deleted from the
file** on first run. `persist_settings` writes no secret field —
ever. Verified by `tests/unit/test_secrets.py`.

**MT5 password: never requested, never stored.** There is
deliberately no `mt5_password` field anywhere. The user signs in
through MT5 itself. A terminal with no logged-in account surfaces
`MT5_ACCOUNT_NOT_READY` with guidance instead of a password prompt.

## 2. Local API auth

- Bind is `127.0.0.1:8788` by default. `--host` is kept, with
  guardrails: a non-loopback bind requires explicit
  `allow_non_loopback=true` (config or
  `NSE_BRIDGE_ALLOW_NON_LOOPBACK=1`) AND the mandatory `X-API-Key`.
  Without the flag the Bridge refuses to start — it never binds
  `0.0.0.0` by accident (`config.validate_bind_host`).
- **Mandatory `X-API-Key` on EVERY local API request** (except the
  plain liveness probe `GET /health`). No empty-key = open mode. A
  missing or invalid key returns 401 `UNAUTHORIZED`. The key is
  auto-generated on first run and stored in the secret store;
  `main()` fails closed before serving anything when it cannot be
  ensured. The GUI authenticates its own API calls with a cookie
  derived from the same key (verified by
  `tests/unit/test_gui_control_center.py` — the key itself is never
  embedded in the served HTML).
- Outbound-only networking: no inbound ports, no port forwarding, no
  public IP, no externally accessible local API. Works behind NAT.

## 3. Server channel auth

- `Authorization: Bearer <api_key>` + `X-NSE-Device-Id: <device_id>`
  on every REST call and in the WS upgrade headers. No `hello` frame.
- `wss://` mandatory; `ws://` only for loopback test endpoints.
  TLS verification mandatory (`server/rest.py`, `config.
  validate_server_base_url` / `validate_server_url`).
- On 401/403 the Bridge stops unauthorized sync immediately, never
  retries with the same key, never falls back to unauthenticated
  mode. Re-enrolment (`register-device`) is the only recovery path
  after a 401 on a non-auth endpoint, and it cannot fix
  `key_invalid` / `key_revoked` — those need a new key from the user.
- Device revocation is honored: 403 → re-enrolment flow;
  `key_revoked` → new key from the user. Key rotation without
  reinstall is supported (enter the new key in Settings; the
  enrolment manager resets `failed_auth` and re-enrols).
- Commands are authenticated by the mutually-authenticated WSS
  session itself; there is no per-command signature in CONTRACT v1
  (§6.2, delta D-CMD-3).

## 4. Redaction

`nse_bridge/logging_setup.py` (`sanitize_payload` / `redact_text`)
scrubs tokens, keys, and passwords from logs, error messages, and
result payloads — including values registered at runtime
(`register_secret`) and labelled patterns (e.g. `api_key`,
`password`, `token`) even when the exact value was never registered.
Telemetry events pass through the same redaction BEFORE they are
built, queued, or delivered. Verified by
`tests/unit/test_redaction.py` and `tests/unit/test_telemetry.py`
(a secret value never appears in an emitted event). The WS channel
never logs raw server frames beyond a truncated, redacted form, and
exception text is never copied into a result payload unredacted.

## 5. Updates — fail-closed verification

The updater (`nse_bridge/updates.py`, CONTRACT v1 §3.5) verifies
SHA-256 **and** the Ed25519 signature BEFORE any install; a failed
verification stages nothing. The rules, in order:

- No public key configured → refuse (`NO_PUBLIC_KEY`). The key comes
  from `NSE_BRIDGE_UPDATE_PUBLIC_KEY` (base64 raw 32-byte Ed25519
  public key) or a bundled `nse_bridge/update_public_key.b64` file.
  **No such file exists in this repo and no key value is invented
  here.** The real NSE release-signing public key must be provisioned
  by the operator. Until then the updater can only *check*, never
  install — this is the honest, fail-closed state, not a bug.
- `signature` null in the release metadata → refuse
  (`SIGNATURE_MISSING`); SHA-256 missing → refuse
  (`SHA256_MISSING`); size mismatch → refuse; bad signature → refuse.
- `min_connector_version` newer than the running version → refuse
  (`MIN_VERSION_NOT_SATISFIED`).
- Download-only staging under `<data_dir>/updates/`; nothing is
  executed or installed in the Bridge process. Install happens on
  restart via a staged, self-contained Python script
  (`apply_update.py` — our own code, not the downloaded artifact)
  that waits for the bridge process to exit, **re-verifies the staged
  file's SHA-256**, then replaces the target exe. On Linux/dev runs
  there is no exe target and the installer honestly reports
  `not_frozen_no_target` instead of pretending to install.

## 6. What the Bridge does NOT do (security boundaries)

- No local AI / autonomous trading: the Bridge never dispatches an
  order on its own analysis. Orders arrive only from the server
  channel (3-op allowlist, §6.1) or from the local operator via the
  loopback API.
- No arbitrary remote code execution: the task boundary
  (`nse_bridge/tasks/`) maps a task kind to a registered, reviewed
  local module only; the kind registry starts EMPTY, and no task kind
  executes from a server message until a future contract version
  registers it.
- No backtest, no indicator/risk engine in the client.
- No hardcoded trading identity: no default magic, symbol, or
  timeframe. Magic/symbol come from the server per command or from
  explicit device configuration — never from client defaults.
- The Bridge never overwrites user terminal config, never evicts
  another device on `max_devices`, never mixes records across an
  account switch, and never executes an old account's command on a
  new account.

## 7. Residual risks (honest list)

- **Release-signing public key missing** (§5): updates cannot install
  until the operator provisions the NSE release-signing public key.
  This is a deployment dependency, not a code gap.
- **Telemetry delivery gap** (CONTRACT v1 §4.2): no diagnostics
  upload endpoint exists; ERROR/WARN events queue locally and age out
  after 1 h if no sink is ever attached (counted, never silent).
  Server-side health is derived from `last_seen_at`, WS liveness,
  last accepted batch, and the manifest.
- **Local API key on a shared machine:** any process running as the
  same user that can read the secret store could present the key.
  That is the platform's trust boundary (same-user local processes);
  the Bridge does not try to defend against the user themself.
- **15 pre-existing ruff findings** in `nse_bridge/engine`,
  `nse_bridge/mt5`, `nse_bridge/timeutil`, `nse_bridge/main.py`
  (unused imports, blind `except Exception`, one duplicate class
  field, one `try/except-pass`) — tracked in docs/RELEASE.md, not
  security defects, but unclean code in the trust path should be
  cleaned before a production release.
