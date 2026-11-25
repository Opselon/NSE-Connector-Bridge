"""Reliability: structured telemetry pipeline + self-healing matrix
(ARCHITECTURE-CONTRACT §8, PLAN addendum §§2–3).

- :mod:`nse_bridge.reliability.telemetry` — structured JSON events,
  level rules, bounded diagnostic queue, diagnostics sink adapter
  (``HttpDiagnosticsSink`` posts to the Platform endpoint; the inert
  ``NullDiagnosticsSink`` is the no-sink default).
- :mod:`nse_bridge.reliability.diagnostic_store` — durable JSONL
  backing for the diagnostic queue (same pattern as the result store).
- :mod:`nse_bridge.reliability.result_store` — durable persistence
  for unacknowledged command results (§6.5).
- :mod:`nse_bridge.reliability.self_healing` — the 20-scenario
  self-healing matrix (spec §13).
"""

from __future__ import annotations

from .diagnostic_store import DurableDiagnosticQueue, DurableDiagnosticStore
from .result_store import DurableResultQueue, DurableResultStore
from .self_healing import HealingContext, Scenario, SelfHealingRegistry
from .telemetry import (
    DiagnosticQueue,
    DiagnosticsSink,
    HttpDiagnosticsSink,
    NullDiagnosticsSink,
    TelemetryEmitter,
    TelemetryEvent,
)

__all__ = [
    "DiagnosticQueue",
    "DiagnosticsSink",
    "DurableDiagnosticQueue",
    "DurableDiagnosticStore",
    "DurableResultQueue",
    "DurableResultStore",
    "HealingContext",
    "HttpDiagnosticsSink",
    "NullDiagnosticsSink",
    "Scenario",
    "SelfHealingRegistry",
    "TelemetryEmitter",
    "TelemetryEvent",
]
