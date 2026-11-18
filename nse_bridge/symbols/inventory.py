"""Broker symbol discovery (spec §4).

:class:`SymbolInventory` is the Bridge's view of what the connected
broker offers, built ONLY from what MT5 reports — never from a
hardcoded list, never from a guess.

Hidden-symbol completeness caveat (read this before trusting a count)
----------------------------------------------------------------------
``symbols_get()`` is the only inventory source the MT5 Python API
offers, and what it returns for HIDDEN symbols is driver/broker
dependent: the MCP action behind contract §17.1.2
(``get_marketwatch_symbols {include_hidden: true}``) is documented as
the complete inventory (4096 symbols in the Platform's E101), but
whether the Python package's ``symbols_get()`` returns the same
complete set on every broker/terminal was NOT verified on this
platform (no MT5 runs on Linux) and is not asserted here. The
inventory therefore reports exactly what ``symbols_get()`` returned —
``hidden_symbols()`` lists the hidden rows the driver DID report —
and :meth:`SymbolInventory.summary` says so. A symbol that is absent
from the inventory is "not reported by this terminal", not "does not
exist at this broker". This is an honest-reporting boundary, not a
defect to paper over: if a broker hides symbols from ``symbols_get()``,
only a live-MT5 verification run can show it.

Availability
------------
:meth:`SymbolInventory.availability` reports tradability from
metadata only (existence + visibility + ``trade_mode``). "Market
closed" is NOT detectable from metadata — a closed market still
reports the same symbol record; it is detectable only from a live
tick (no fresh tick) or from the chart path, which reports
``market_closed`` per contract §17.1.4. The availability reason says
so rather than claiming a market state the inventory cannot see.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from .metadata import SymbolMetadata


class SymbolSource(Protocol):
    """Minimal MT5 surface the inventory needs.

    :class:`~nse_bridge.mt5.driver.Mt5Driver` satisfies this protocol
    (``symbols_get`` / ``symbol_info`` / ``symbol_select``); tests use
    a fake source. The inventory never touches the MT5 module itself,
    so it runs on Linux with no terminal attached.
    """

    def symbols_get(self) -> list[Any] | None: ...

    def symbol_info(self, name: str) -> Any | None: ...

    def symbol_select(self, name: str, enable: bool = True) -> bool: ...


@dataclass(frozen=True)
class Availability:
    """Tradability verdict for one symbol, from metadata only."""

    name: str
    #: The broker reported this symbol at all.
    exists: bool
    #: Visible in Market Watch (None when the broker did not report it).
    visible: bool | None
    trade_mode: int | None
    trade_mode_label: str | None
    #: Tradable in at least one direction (exists + trade mode != DISABLED).
    tradable: bool
    #: Tradable without direction restriction (trade mode FULL).
    fully_tradable: bool
    #: Human-readable reason — never claims a market state the
    #: inventory cannot see (see module docstring).
    reason: str


class SymbolInventory:
    """Broker symbol inventory, rebuilt from MT5 on :meth:`refresh`.

    The index is a snapshot: :meth:`all_symbols` / :meth:`search` /
    :meth:`get` read the last snapshot (loading it once on first use).
    The snapshot changes only via :meth:`refresh` (re-read MT5 now) or
    :meth:`invalidate` (drop the snapshot; the next read re-loads).
    Callers that need a live single-symbol record use
    :meth:`metadata`, which calls ``symbol_info(name)`` directly and
    does not touch the snapshot.
    """

    def __init__(self, source: SymbolSource) -> None:
        self._source = source
        self._index: dict[str, SymbolMetadata] | None = None
        self._loaded_at: datetime | None = None
        #: Rows ``symbols_get()`` returned on the last refresh
        #: (including rows without a usable name — the count the driver
        #: reported, for the completeness caveat above).
        self._rows_reported: int | None = None

    # -- loading --------------------------------------------------------
    def refresh(self) -> list[SymbolMetadata]:
        """Re-read the broker symbol list and rebuild the index.

        Returns the new snapshot. Raises the driver's error unchanged
        when MT5 cannot be reached — a failed refresh never silently
        keeps a stale snapshot AND never silently empties it: on
        failure the previous snapshot is left in place and the
        exception propagates, so callers see the failure.
        """
        raw = self._source.symbols_get()
        if raw is None:
            raise ValueError("symbols_get() returned None — broker symbol list unavailable")
        index: dict[str, SymbolMetadata] = {}
        for row in raw:
            meta = SymbolMetadata.from_mt5(row)
            if not meta.name:
                continue
            index[meta.name.upper()] = meta
        self._index = index
        self._rows_reported = len(raw)
        self._loaded_at = datetime.now(UTC)
        return list(index.values())

    def invalidate(self) -> None:
        """Drop the snapshot. The next read re-loads from MT5.

        Cheap and side-effect-free (no MT5 call): this is the hook the
        app calls when the broker environment changes — e.g. the B3
        ``broker_connection_change`` self-healing scenario
        (``HealingContext.on_refresh_symbols``) — because a mapping
        built against the old broker server must never be assumed to
        carry over to the new one.
        """
        self._index = None
        self._loaded_at = None
        self._rows_reported = None

    def _ensure_loaded(self) -> dict[str, SymbolMetadata]:
        if self._index is None:
            self.refresh()
        assert self._index is not None  # refresh() sets it or raises
        return self._index

    # -- discovery ------------------------------------------------------
    def all_symbols(self) -> list[SymbolMetadata]:
        """Every symbol the broker reported, in broker order."""
        return list(self._ensure_loaded().values())

    def visible_symbols(self) -> list[SymbolMetadata]:
        """Symbols visible in Market Watch (``visible`` is True)."""
        return [m for m in self._ensure_loaded().values() if m.visible is True]

    def hidden_symbols(self) -> list[SymbolMetadata]:
        """Symbols the driver reported with ``visible`` False.

        Completeness caveat: this is the hidden set the driver
        reported, not a proven-complete broker inventory — see the
        module docstring. Symbols with ``visible`` ``None`` (broker
        did not report visibility) appear in NEITHER list; they are
        still in :meth:`all_symbols`.
        """
        return [m for m in self._ensure_loaded().values() if m.visible is False]

    def search(self, substring: str) -> list[SymbolMetadata]:
        """Case-insensitive substring search over name and description."""
        needle = (substring or "").strip().upper()
        if not needle:
            return self.all_symbols()
        return [
            m
            for m in self._ensure_loaded().values()
            if needle in m.name.upper() or (m.description and needle in m.description.upper())
        ]

    def get(self, name: str) -> SymbolMetadata | None:
        """Snapshot lookup by exact broker symbol name (case-insensitive)."""
        return self._ensure_loaded().get((name or "").strip().upper())

    def metadata(self, name: str) -> SymbolMetadata | None:
        """Live single-symbol record via ``symbol_info(name)``.

        Bypasses the snapshot (fresh broker data for one symbol).
        Returns ``None`` when MT5 reports no such symbol. Does not
        mutate the snapshot.
        """
        raw = self._source.symbol_info(name)
        if raw is None:
            return None
        meta = SymbolMetadata.from_mt5(raw)
        return meta if meta.name else None

    def select_for_market_watch(self, name: str) -> bool:
        """Add ``name`` to Market Watch where permitted (``symbol_select``).

        Returns the broker's answer. ``False`` covers both "broker
        refused" and "IPC/source failure" — callers that need the
        distinction should check :meth:`availability` first and treat
        a ``False`` here as "not selected, do not assume it is
        watched". Never raises for a refused select; a transport
        failure inside the source propagates as the source raises it.
        """
        try:
            return bool(self._source.symbol_select(name, True))
        except Exception:  # noqa: BLE001 — a refused/failed select is a False, never a crash
            return False

    # -- availability ---------------------------------------------------
    def availability(self, name: str) -> Availability:
        """Tradability verdict for ``name`` from the snapshot.

        ``tradable`` is False when the symbol is absent or its trade
        mode is DISABLED. A present symbol with an unreported trade
        mode is NOT tradable either — the Bridge never assumes
        tradability it cannot see. Market-closed is not detectable
        here (see module docstring); the reason never claims it.
        """
        meta = self.get(name)
        if meta is None:
            return Availability(
                name=name,
                exists=False,
                visible=None,
                trade_mode=None,
                trade_mode_label=None,
                tradable=False,
                fully_tradable=False,
                reason=f"symbol '{name}' was not reported by this broker/terminal",
            )
        if meta.trade_mode is None:
            return Availability(
                name=meta.name,
                exists=True,
                visible=meta.visible,
                trade_mode=None,
                trade_mode_label=None,
                tradable=False,
                fully_tradable=False,
                reason=f"symbol '{meta.name}' is present but the broker did not report a trade mode",
            )
        if meta.trade_mode == 0:
            reason = f"symbol '{meta.name}' is present but trading is disabled (trade_mode=DISABLED)"
        elif meta.trade_mode == 3:
            reason = f"symbol '{meta.name}' is close-only (trade_mode=CLOSE_ONLY)"
        elif meta.trade_mode == 1:
            reason = f"symbol '{meta.name}' is long-only (trade_mode=LONG_ONLY)"
        elif meta.trade_mode == 2:
            reason = f"symbol '{meta.name}' is short-only (trade_mode=SHORT_ONLY)"
        else:
            reason = f"symbol '{meta.name}' is tradable (trade_mode=FULL)"
        return Availability(
            name=meta.name,
            exists=True,
            visible=meta.visible,
            trade_mode=meta.trade_mode,
            trade_mode_label=meta.trade_mode_label,
            tradable=meta.trade_mode != 0,
            fully_tradable=meta.trade_mode == 4,
            reason=reason,
        )

    # -- reporting ------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        """Inventory counts + the completeness caveat, for diagnostics/GUI."""
        index = self._ensure_loaded()
        return {
            "total": len(index),
            "visible": sum(1 for m in index.values() if m.visible is True),
            "hidden": sum(1 for m in index.values() if m.visible is False),
            "visibility_unknown": sum(1 for m in index.values() if m.visible is None),
            "tradable": sum(1 for m in index.values() if m.is_trade_enabled),
            "rows_reported_by_driver": self._rows_reported,
            "loaded_at": self._loaded_at.isoformat() if self._loaded_at else None,
            "completeness": (
                "as reported by MT5 symbols_get(); hidden-symbol completeness "
                "is driver/broker-dependent and unverified on this platform"
            ),
        }
