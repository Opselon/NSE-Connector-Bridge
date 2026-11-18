"""Broker-aware symbol discovery and resolution (spec §4, gap §5 C1).

Stable public API — the C2 execution agent resolves canonical IDs
through this package; see :mod:`nse_bridge.symbols.resolver` for the
resolution rules and :mod:`nse_bridge.symbols.canonical` for the
BRIDGE-LOCAL canonical-ID scheme (contract dependency D7: CONTRACT v1
defines no canonical format and no symbol-inventory sync channel).
"""

from __future__ import annotations

from .canonical import normalize_canonical_id, parse_canonical_id
from .inventory import Availability, SymbolInventory, SymbolSource
from .metadata import SymbolMetadata
from .resolver import (
    DEFAULT_CACHE_TTL_S,
    ResolutionResult,
    ResolutionStatus,
    SymbolResolver,
    differing_fields,
)

__all__ = [
    "DEFAULT_CACHE_TTL_S",
    "Availability",
    "ResolutionResult",
    "ResolutionStatus",
    "SymbolInventory",
    "SymbolMetadata",
    "SymbolResolver",
    "SymbolSource",
    "differing_fields",
    "normalize_canonical_id",
    "parse_canonical_id",
]
