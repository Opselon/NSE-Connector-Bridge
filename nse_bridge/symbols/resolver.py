"""Symbol resolution — canonical ID → exact broker symbol (spec §4).

Stable public API for the C2 execution agent (and the future Symbols
GUI screen)::

    from nse_bridge.symbols.resolver import SymbolResolver, ResolutionResult, SymbolMetadata
    from nse_bridge.symbols.inventory import SymbolInventory

    resolver = SymbolResolver(SymbolInventory(driver))
    result = resolver.resolve("XAU/USD")
    if result.status is ResolutionStatus.RESOLVED:
        broker_symbol = result.exact_symbol  # e.g. "XAUUSD.a" — send verbatim to MT5

Resolution rules (never guess)
------------------------------
1. A verbatim broker-name match wins: if the input IS an exact broker
   symbol name (case-insensitive), it resolves to itself. The server
   may send a broker-exact name it already knows.
2. Otherwise the input must parse as a canonical ``BASE/QUOTE`` ID
   (see :mod:`nse_bridge.symbols.canonical`). Unparseable input →
   ``not_found``.
3. Candidates are broker symbols whose normalized name equals
   ``BASE+QUOTE`` or that core plus a broker suffix, AND whose
   reported ``currency_base`` / ``currency_profit`` (when reported)
   equal ``BASE`` / ``QUOTE``. A similar name with different
   currencies is a different instrument, not a candidate.
4. One candidate → ``resolved``.
5. Several candidates → ``ambiguous``, ALWAYS, unless the caller
   supplies ``expected`` metadata (e.g. ``{"contract_size": 100}``)
   that leaves exactly one candidate standing. The full candidate
   list (with metadata) is returned so the server — never the Bridge
   — can decide. Two candidates that are indistinguishable on all
   available metadata are ``ambiguous`` even WITH ``expected``
   metadata: there is nothing left to distinguish them with.
6. No candidates → ``not_found``.

Cache
-----
The resolver caches the broker inventory snapshot (not individual
mappings): :meth:`SymbolResolver.resolve` / :meth:`candidates` use
the snapshot while it is fresh (TTL, default 300 s) and re-read MT5
when it expires. :meth:`refresh` re-reads immediately;
:meth:`invalidate` drops the snapshot without an MT5 call (the next
read re-loads). The app MUST call :meth:`invalidate` (or
:meth:`refresh`) when the broker environment changes — a broker-server
change (B3 self-healing scenario ``broker_connection_change``,
``HealingContext.on_refresh_symbols``) invalidates every mapping,
because a symbol list built against the old broker must never be
assumed to carry over to the new one. The resolver never re-resolves
silently against a stale broker.

Contract dependency (D7)
------------------------
There is NO symbol-inventory sync channel in CONTRACT v1 and NO
contract-defined canonical format — the only symbol data path the
contract defines is the per-symbol chart protocol (§17.1). The
canonical ``BASE/QUOTE`` scheme and this resolver are therefore
BRIDGE-LOCAL: they let the Bridge map a server-supplied identifier to
an exact broker symbol, but the server never receives a full
inventory through this package, and no frame or endpoint is invented
here. If the contract later defines a canonical format or an
inventory channel, this module is the single place that adapts.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .canonical import (
    broker_name_matches,
    canonical_base_for_broker_base,
    normalize_canonical_id,
    parse_canonical_id,
)
from .inventory import SymbolInventory
from .metadata import SymbolMetadata

#: Default cache TTL for the inventory snapshot (seconds).
DEFAULT_CACHE_TTL_S = 300.0


class ResolutionStatus(str, Enum):
    """Outcome of :meth:`SymbolResolver.resolve`."""

    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"


@dataclass(frozen=True)
class ResolutionResult:
    """Result of resolving one canonical ID.

    ``exact_symbol`` is set ONLY when ``status`` is
    :attr:`ResolutionStatus.RESOLVED` — it is the exact broker symbol
    name to send to MT5 verbatim. For ``ambiguous`` / ``not_found``
    it is ``None`` and ``candidates`` carries the evidence.
    """

    status: ResolutionStatus
    #: The input as received (never normalized away — the caller sees
    #: what it asked for).
    requested: str
    #: Normalized ``BASE/QUOTE`` form, or ``None`` when unparseable.
    canonical_id: str | None
    exact_symbol: str | None
    candidates: list[SymbolMetadata] = field(default_factory=list)
    reason: str = ""


def differing_fields(candidates: list[SymbolMetadata]) -> list[str]:
    """Distinguishing-signature fields that actually differ across candidates.

    Used in ``ambiguous`` reasons so the server can see WHAT differs
    (and therefore what ``expected`` metadata would disambiguate).
    """
    names = (
        "contract_size",
        "tick_value",
        "tick_size",
        "digits",
        "currency_base",
        "currency_profit",
        "trade_mode",
        "calculation_mode",
    )
    differing: list[str] = []
    for name in names:
        values = {getattr(c, name) for c in candidates}
        if len(values) > 1:
            differing.append(name)
    return differing


class SymbolResolver:
    """Canonical-ID → exact-broker-symbol resolver over a :class:`SymbolInventory`.

    Thread-safe: the snapshot is guarded by a lock, so
    :meth:`invalidate` (e.g. from a self-healing callback) cannot race
    a :meth:`resolve` in flight.
    """

    def __init__(
        self,
        inventory: SymbolInventory,
        *,
        cache_ttl_s: float = DEFAULT_CACHE_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inventory = inventory
        self._ttl_s = float(cache_ttl_s)
        self._clock = clock
        self._lock = threading.Lock()
        self._snapshot: list[SymbolMetadata] | None = None
        self._snapshot_at: float | None = None

    @property
    def inventory(self) -> SymbolInventory:
        """The underlying broker inventory (C4 GUI input).

        The resolver and the Symbols screen share this one inventory,
        so the screen shows exactly the symbols resolution runs
        against — never a second, diverging snapshot.
        """
        return self._inventory

    # -- cache ----------------------------------------------------------
    def refresh(self) -> None:
        """Re-read the broker symbol list and rebuild the index NOW.

        Use after a known broker-environment change, or when the
        caller needs a guaranteed-fresh answer. Raises the driver's
        error unchanged when MT5 cannot be reached (the old snapshot
        is left in place — see :meth:`SymbolInventory.refresh`).
        """
        snapshot = self._inventory.refresh()
        with self._lock:
            self._snapshot = list(snapshot)
            self._snapshot_at = self._clock()

    def invalidate(self) -> None:
        """Drop the cached snapshot WITHOUT an MT5 call.

        The next :meth:`resolve` / :meth:`candidates` re-reads the
        broker. This is the hook the app calls on a broker-connection
        change (B3 ``broker_connection_change``): mappings built
        against the old broker server must never be reused. Cheap
        enough to call from a callback or a signal handler.
        """
        with self._lock:
            self._snapshot = None
            self._snapshot_at = None

    def cache_state(self) -> dict[str, Any]:
        """Cache diagnostics (for tests, diagnostics, GUI)."""
        with self._lock:
            if self._snapshot is None or self._snapshot_at is None:
                return {"cached": False, "age_s": None, "ttl_s": self._ttl_s, "symbols": 0}
            return {
                "cached": True,
                "age_s": round(self._clock() - self._snapshot_at, 3),
                "ttl_s": self._ttl_s,
                "symbols": len(self._snapshot),
            }

    def _ensure_snapshot(self) -> list[SymbolMetadata]:
        with self._lock:
            fresh = (
                self._snapshot is not None
                and self._snapshot_at is not None
                and (self._clock() - self._snapshot_at) <= self._ttl_s
            )
            if fresh:
                assert self._snapshot is not None
                return list(self._snapshot)
        # Outside the lock: the MT5 read can block; a concurrent
        # invalidate() during the read only drops what we are about to
        # replace, which refresh() then rebuilds from live data.
        snapshot = self._inventory.refresh()
        with self._lock:
            self._snapshot = list(snapshot)
            self._snapshot_at = self._clock()
            return list(self._snapshot)

    # -- candidate search ----------------------------------------------
    def _find_candidates(self, requested: str) -> tuple[str | None, list[SymbolMetadata]]:
        """Return ``(canonical_id, candidates)`` for ``requested``.

        Step 0 — verbatim broker name: the input IS an exact broker
        symbol (case-insensitive) → that single symbol. Step 1 —
        canonical parse + name/currency matching (see module
        docstring). Returns ``(None, [])`` when the input is
        unparseable.
        """
        snapshot = self._ensure_snapshot()
        verbatim = self._inventory.get(requested)
        if verbatim is not None:
            # The input IS a broker symbol name. Its canonical form is
            # the normalized parse when the name itself parses, else
            # the currency pair the broker reports for it (when both
            # currencies are known); otherwise there is no canonical
            # form to report and it stays None.
            canonical = normalize_canonical_id(requested)
            if canonical is None and verbatim.currency_base and verbatim.currency_profit:
                canonical = f"{verbatim.currency_base.upper()}/{verbatim.currency_profit.upper()}"
            return canonical, [verbatim]
        parsed = parse_canonical_id(requested)
        if parsed is None:
            return None, []
        base, quote = parsed
        canonical = f"{base}/{quote}"
        candidates: list[SymbolMetadata] = []
        for meta in snapshot:
            if not broker_name_matches(meta.name, base, quote):
                continue
            # Currency guard: when the broker reports a currency, it
            # must agree with the canonical ID — a similar name with
            # different currencies is a different instrument. The
            # base comparison is alias-aware (a GOLD symbol may
            # report currency_base "GOLD").
            if meta.currency_base is not None and canonical_base_for_broker_base(meta.currency_base) != base:
                continue
            if meta.currency_profit is not None and meta.currency_profit.upper() != quote:
                continue
            candidates.append(meta)
        candidates.sort(key=lambda m: m.name.upper())
        return canonical, candidates

    def candidates(self, canonical_id: str) -> list[SymbolMetadata]:
        """All broker symbols that could denote ``canonical_id``.

        The metadata comparison basis for the server: each candidate
        carries its full :class:`SymbolMetadata`, so the server can
        compare contract size, tick value, sessions, digits and trade
        mode itself. Returns ``[]`` for unknown or unparseable IDs.
        """
        _, found = self._find_candidates(canonical_id)
        return found

    # -- resolution ------------------------------------------------------
    def resolve(
        self,
        canonical_id: str,
        expected: Mapping[str, Any] | None = None,
    ) -> ResolutionResult:
        """Resolve ``canonical_id`` to one exact broker symbol.

        ``expected`` is an optional mapping of
        :class:`SymbolMetadata` field names to values the caller
        requires (e.g. ``{"contract_size": 100}``) — the
        disambiguation basis when several candidates share a name.
        Unknown ``expected`` keys raise :class:`ValueError` (fail
        loudly, never silently ignore a disambiguation constraint).
        """
        requested = str(canonical_id)
        canonical, found = self._find_candidates(requested)
        if not found:
            if canonical is None:
                return ResolutionResult(
                    status=ResolutionStatus.NOT_FOUND,
                    requested=requested,
                    canonical_id=None,
                    exact_symbol=None,
                    candidates=[],
                    reason=(
                        f"canonical id {requested!r} is not a valid BASE/QUOTE identifier "
                        "(expected e.g. 'EUR/USD', or a 6-letter code such as 'EURUSD')"
                    ),
                )
            return ResolutionResult(
                status=ResolutionStatus.NOT_FOUND,
                requested=requested,
                canonical_id=canonical,
                exact_symbol=None,
                candidates=[],
                reason=f"no broker symbol matches canonical id '{canonical}' on this terminal",
            )
        # From here on `found` is non-empty. `canonical` can still be
        # None for a verbatim broker-name match whose currencies the
        # broker did not report — reasons then quote the input.
        display = canonical if canonical is not None else requested
        if expected:
            # Validate keys up front — an unknown field raises
            # ValueError so a typo in a disambiguation constraint can
            # never silently resolve to the wrong symbol.
            unknown = [k for k in expected if k not in SymbolMetadata.__dataclass_fields__]
            if unknown:
                raise ValueError(f"unknown SymbolMetadata field(s) for disambiguation: {unknown!r}")
            matching = [m for m in found if m.matches_expected(expected)]
            if len(matching) == 1:
                return ResolutionResult(
                    status=ResolutionStatus.RESOLVED,
                    requested=requested,
                    canonical_id=canonical,
                    exact_symbol=matching[0].name,
                    candidates=matching,
                    reason=(f"single candidate '{matching[0].name}' matches the expected metadata for '{display}'"),
                )
            if not matching:
                return ResolutionResult(
                    status=ResolutionStatus.AMBIGUOUS,
                    requested=requested,
                    canonical_id=canonical,
                    exact_symbol=None,
                    candidates=list(found),
                    reason=(
                        f"{len(found)} candidate(s) for '{display}' but none matches the "
                        f"expected metadata {dict(expected)}"
                    ),
                )
            return ResolutionResult(
                status=ResolutionStatus.AMBIGUOUS,
                requested=requested,
                canonical_id=canonical,
                exact_symbol=None,
                candidates=matching,
                reason=(
                    f"{len(matching)} candidates for '{display}' match the expected metadata "
                    "and are indistinguishable on available metadata — refusing to guess"
                    if len({m.distinguishing_signature() for m in matching}) == 1
                    else f"{len(matching)} candidates for '{display}' match the expected metadata "
                    f"but differ on {differing_fields(matching)} — supply more metadata to disambiguate"
                ),
            )
        if len(found) == 1:
            return ResolutionResult(
                status=ResolutionStatus.RESOLVED,
                requested=requested,
                canonical_id=canonical,
                exact_symbol=found[0].name,
                candidates=list(found),
                reason=f"single broker symbol '{found[0].name}' matches '{display}'",
            )
        if len({m.distinguishing_signature() for m in found}) == 1:
            return ResolutionResult(
                status=ResolutionStatus.AMBIGUOUS,
                requested=requested,
                canonical_id=canonical,
                exact_symbol=None,
                candidates=list(found),
                reason=(
                    f"{len(found)} candidates for '{display}' are indistinguishable on "
                    "available metadata (contract size, tick value, tick size, digits, "
                    "currencies, trade mode, calculation mode) — refusing to guess"
                ),
            )
        return ResolutionResult(
            status=ResolutionStatus.AMBIGUOUS,
            requested=requested,
            canonical_id=canonical,
            exact_symbol=None,
            candidates=list(found),
            reason=(
                f"{len(found)} candidates for '{display}' differ on "
                f"{differing_fields(found)} — supply expected metadata to disambiguate"
            ),
        )
