"""Server communication package (Phase 2 — CONTRACT v1).

Implements the contract-conformant channel to NSE Platform:

- :mod:`nse_bridge.server.rest` — REST client (contract §3):
  ``validate-key``, ``register-device``, ``sync/batch`` (the ONLY sync
  channel), REST ``heartbeat``, ``session/refresh`` + ``session/revoke``,
  ``releases/latest``, ``manifest`` (§4.1, [client-v1.1]).
- :mod:`nse_bridge.server.enrolment` — enrolment state machine
  (contract §3.1/§3.2, §5.1): validate-key → register-device at
  startup (background task), re-enrolment after a 401, FAILED_AUTH is
  terminal for the key; the WS channel gates on REGISTERED.
- :mod:`nse_bridge.server.sync` — sync engine: flat batch wire shape,
  checkpoint (max broker time, never backwards), bounded pending queue
  (32), 500-item pages with a 250 ms pause (§3.3).
- :mod:`nse_bridge.server.commands` — command envelope (§6.2),
  validation order (§6.3), lifecycle states (clarification #2),
  ``action.result`` envelope (§6.5), 3-op server allowlist (§6.1).
- :mod:`nse_bridge.server.chart` — chart protocol (§17.1): one-shot
  requests, subscriptions, error codes, 60 frames/min budget.
- :mod:`nse_bridge.server.manifest` — action manifest (§4.1,
  [client-v1.1]): only fields truthfully mappable from this Bridge.

The WebSocket channel itself lives in
:mod:`nse_bridge.realtime.channel` (rewritten in Phase 2 to §5/§6).

Every wire shape here is traceable to a contract section. Nothing is
invented: where the contract defines no endpoint (notably the
diagnostics/events upload, §4.2) this package does NOT invent one.
"""

from __future__ import annotations

__all__ = [
    "chart",
    "commands",
    "enrolment",
    "errors",
    "manifest",
    "queue",
    "rest",
    "sync",
    "uuid7",
]
