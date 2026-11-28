# Rollback / recovery strategy (P1-7)

A bad update must never brick the Bridge. This document states the
strategy the updater implements: what is kept, what is verified
before any switch, and exactly what happens when verification or the
new version's start fails. It is the written strategy the acceptance
record (`docs/ACCEPTANCE.md`, item 27) said was owed before the first
tagged release — v1.4.0 shipped without one.

Scope: the in-app updater path (`nse_bridge/updates.py` — check,
download, stage, restart-install). The manual installer path
(`scripts/install.ps1`) is covered separately in §7. Nothing here
needs the user's PC to verify: the mechanism is proven by mock tests
(`tests/unit/test_rollback.py`); live proof on Windows with a real
terminal is the P1-9 Windows run, still owed.

## 1. Principles

1. **Verify before switch.** No byte is ever installed that has not
   passed SHA-256 **and** (where a signature exists) Ed25519
   verification against the release metadata. Verification happens
   twice: at download (`verify_artifact`) and again at install time
   (the staged apply script re-hashes the staged file before any
   replacement). A failed verification means no switch, ever.
2. **Keep one previous version.** Before the target exe is replaced,
   the running exe is copied aside next to it, together with a small
   manifest (`rollback-state.json`) recording its version and SHA-256.
   Exactly one previous generation is retained; a newer update
   overwrites it. Retention is bounded by design — no unbounded
   accumulation of old exes.
3. **Fall back, don't strand.** If the new version fails
   verification or fails to start, the Bridge comes back on the
   previous version. A failed update leaves a running Bridge on the
   old version, never a dead install.
4. **Fail closed.** If the previous version is itself unverifiable
   (backup missing, or its SHA-256 does not match the recorded
   manifest) or absent (fresh install — there is no previous
   version), no fallback launch is attempted and no unverified bytes
   are ever executed. The failure is recorded in
   `apply-result.json` and surfaced through the updater status; the
   operator recovers manually (§8).
5. **Never execute downloaded bytes.** The staged artifact is only
   ever copied, never executed — including by the rollback path,
   which relaunches the previously installed exe, not the staged
   file.

## 2. Update flow with rollback

```
check            releases/latest → version / sha256 / signature
download         verify size → SHA-256 → Ed25519 signature (fail-closed:
                 NO_PUBLIC_KEY / SIGNATURE_MISSING / SHA256_MISMATCH /
                 SIGNATURE_INVALID — nothing staged, current version
                 keeps running)
stage            verified bytes → <data_dir>/updates/nse-bridge-<ver>.exe
                 + update-state.json (records installed_version, i.e.
                 the version being replaced)
restart          on shutdown, the staged apply script (our own code,
                 apply_update.py) is launched detached
install          script waits for the bridge PID to exit, re-verifies
                 the staged file's SHA-256, backs up the current exe
                 (see §3), replaces the target, restarts it
start check      the script watches the new process for a grace window
                 (default 15 s, --start-grace-seconds). A healthy
                 bridge never exits on its own in that window.
   ├─ alive      update complete; previous version retained (§3)
   └─ exited     failed start → restore the backup over the target,
                 relaunch the previous version, record rolled_back
```

The install step and the start check both run in the staged apply
script, not in the Bridge process — the Bridge is already exiting
when they run, so a crashing new version cannot take the installer
down with it.

## 3. Previous-version retention

Kept alongside the installed exe (the install directory, e.g.
`%LOCALAPPDATA%\Programs\NSE MT5 Bridge\`):

| File | Contents |
|---|---|
| `NSE-MT5-Bridge.exe.previous` | Byte copy of the exe being replaced (the previous version). Created by the apply script immediately before the replacement, after the staged file's SHA-256 re-check passes. |
| `<data_dir>\updates\rollback-state.json` | The previous version's manifest: `previous_version`, `previous_sha256` (hash of the target taken before replacement), `previous_path`, `new_version`, `new_sha256`, `backed_up_at`. Written by the apply script at backup time. |

The manifest is what makes the backup trustworthy: a rollback is
only attempted when the backup file exists **and** its SHA-256
matches `previous_sha256`. A mismatch means the backup was tampered
with or corrupted — fail closed, do not launch it (§5).

One generation only: the next update overwrites
`NSE-MT5-Bridge.exe.previous` and `rollback-state.json`. The
updater's staging pruning (`_prune_stale_staged_artifacts`) is
unchanged — staged (not yet installed) artifacts are still pruned
to the incoming one; retention applies to the *installed* previous
version, not to staged downloads.

The updater status (`GET /api/v1/updates/status`, and the Updates
screen) reports the rollback state:

- `rollback_available`: true only when a backup file exists and its
  SHA-256 matches the recorded manifest.
- `previous_version` / `previous_sha256`: the retained version, or
  null when no verifiable previous version exists.

## 4. Fallback triggers

| Trigger | When it is caught | What happens |
|---|---|---|
| Failed verify at download | `Updater.download()` — size / SHA-256 / signature check | Structured error (`NO_PUBLIC_KEY`, `SIGNATURE_MISSING`, `SHA256_MISMATCH`, `SIGNATURE_INVALID`, …); nothing is staged; the running Bridge is untouched and keeps serving. This is the normal "fallback": the current version *is* the fallback. |
| Failed verify at install | Apply script re-hashes the staged file before replacing the target | Install refused; the target exe is never touched, no backup is created, the previous version stays installed. Recorded in `apply-result.json` (`error: "staged file SHA-256 mismatch …"`). |
| New version fails to start | Apply script's start check: the relaunched process exits within the 15 s grace window | The backup is copied back over the target, the previous version is relaunched, and `apply-result.json` records `rolled_back: true` with the reason. The update is reported as failed (`ok: false`) — the Bridge is running again, but on the previous version. |
| Install itself fails | Replace/copy error in the apply script | If a backup was already taken, the script attempts to restore it before reporting failure; the target is never left half-written by design (`os.replace` is atomic; the copy fallback restores from backup on error). |

Note the deliberate asymmetry: a *quick exit* is the failure signal.
A healthy Bridge is a long-running process — it never exits on its
own within seconds of a fresh start. The one exception is the
single-instance guard (a second launch exits `2` while another copy
is already running): the start check does not special-case exit
codes, so that case also rolls back. That is the safe outcome — the
previous version is restored and the update simply retries on the
next restart — and it is recorded, not silent.

## 5. Fail-closed cases (stated explicitly)

- **No previous version (fresh install).** There is nothing to back
  up and nothing to fall back to. The install proceeds without a
  backup and `rollback-state.json` records
  `previous_version: null`; `rollback_available` is false. A failed
  start then leaves no Bridge running — recovery is a manual
  reinstall (§8). This is the only case where a failed start strands
  the install, and it only applies to the very first install.
- **Previous version unverifiable.** Backup file missing, or its
  SHA-256 does not match `rollback-state.json`. No fallback launch
  is attempted — launching bytes that do not match their manifest
  would violate principle 1. `rollback_available` is false and the
  failure is recorded in `apply-result.json`.
- **Rollback relaunch also fails.** The restored previous version
  exits within its own short observation window (5 s). Recorded as
  `rollback_launch_failed: true` in `apply-result.json`; the target
  on disk is still the verified previous version, so a manual launch
  or reinstall (§8) starts from a known-good file, not from a
  half-written one.
- **No restart requested.** When the apply script is run without
  `--restart-cmd` (e.g. tests), no start check is possible and none
  is claimed: `apply-result.json` records
  `start_check: "skipped_no_restart"`.

In every fail-closed case the Bridge never launches, installs, or
restores anything it has not verified.

## 6. What the mechanism does not do

- **No automatic re-download.** A rolled-back update is not
  retried automatically; the next update check re-offers the same
  release and the operator (or the GUI) decides whether to try
  again. Retrying a deterministically broken build in a loop would
  only brick the Bridge repeatedly.
- **No multi-generation history.** Only the immediately previous
  version is retained. Going back two versions is a manual
  reinstall (§8).
- **No health-endpoint check.** The start check is process
  liveness only (the new process stays alive for the grace window),
  not a `/health` probe: the apply script is stdlib-only and does
  not know the Bridge's configured port. A process that stays up
  but serves nothing is a live Bridge with a fault, not a failed
  start — it is visible in the Diagnostics screen and the logs,
  and the operator rolls back manually (§8).
- **No installer-path retention.** `scripts/install.ps1` upgrades
  in place and does not create a `.previous` backup (see §7).

## 7. Manual installer path (`scripts/install.ps1`)

The installer upgrades in place: it verifies the downloaded
artifact three ways (manifest SHA-256 + `.sha256` sidecar +
recomputed file hash), reports the Ed25519 signature status
honestly, then runs the setup exe or copies the portable exe over
the installed one. It keeps **no** previous-version backup — a
manual install is an operator action, and the recovery is also
manual: re-run `install.ps1 -Version <previous-version>` to
reinstall the previous release from GitHub Releases (the same
triple SHA-256 verification applies). The in-app updater path (§2)
is the one with automatic rollback.

## 8. Operator recovery (when automatic rollback cannot help)

If both the new version and the automatic fallback fail, or the
previous version is unverifiable:

1. Check `<data_dir>\updates\apply-result.json` — it records
   `ok`, `error`, `rolled_back`, and (on a failed rollback
   relaunch) `rollback_launch_failed`. Check
   `<data_dir>\updates\rollback-state.json` for the retained
   version and its SHA-256.
2. If `NSE-MT5-Bridge.exe.previous` exists and its SHA-256 matches
   `rollback-state.json`, copy it back over `NSE-MT5-Bridge.exe`
   and launch the Bridge — that is the manual form of the automatic
   rollback.
3. Otherwise reinstall the previous release:
   `powershell scripts/install.ps1 -Version <previous-version>`
   (verified the same way as any install; see
   `docs/INSTALLATION.md`).
4. Configuration, credentials, logs, and the diagnostic store live
   outside the exe (data dir `%LOCALAPPDATA%\NSEBridge`), so a
   reinstall or a rollback never loses settings — a fresh install
   over the same data dir picks them up.

## 9. Proof status

| Claim | Proof |
|---|---|
| Fallback after a failed verify (download refuses, nothing staged, current version untouched; staged file tampered after staging → target untouched) | Mock tests, `tests/unit/test_rollback.py` — no user PC, no MT5 |
| Fallback after a failed start (new exe exits in the grace window → previous exe restored and relaunched) | Mock tests, `tests/unit/test_rollback.py` — fake "exes" are inert shell scripts, never executed as real Bridges |
| Previous-version retention (backup + manifest written at install time; status reports it; tampered backup → `rollback_available: false`) | Mock tests, `tests/unit/test_rollback.py` |
| Live proof on Windows (real exe swap, real start, real rollback on a real machine) | **Owed** — the P1-9 Windows run; do not claim it until it is observed |
