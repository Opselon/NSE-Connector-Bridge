"""Reliability: structured telemetry pipeline + self-healing matrix
(ARCHITECTURE-CONTRACT §8, PLAN addendum §§2–3).

- :mod:`nse_bridge.reliability.telemetry` — structured JSON events,
  level rules, bounded diagnostic queue, inert diagnostics adapter.
- :mod:`nse_bridge.reliability.result_store` — durable persistence
  for unacknowledged command results (§6.5).
- :mod:`nse_bridge.reliability.self_healing` — the 15-scenario
  self-healing matrix.
"""

from __future__ import annotations

from .result_store import DurableResultQueue, DurableResultStore
from .self_healing import HealingContext, Scenario, SelfHealingRegistry
from .telemetry import (
    DiagnosticQueue,
    DiagnosticsSink,
    NullDiagnosticsSink,
    TelemetryEmitter,
    TelemetryEvent,
)

__all__ = [
    "DiagnosticQueue",
    "DiagnosticsSink",
    "DurableResultQueue",
    "DurableResultStore",
    "HealingContext",
    "NullDiagnosticsSink",
    "Scenario",
    "SelfHealingRegistry",
    "TelemetryEmitter",
    "TelemetryEvent",
]
