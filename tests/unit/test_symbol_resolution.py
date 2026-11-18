"""Unit tests for symbol discovery + resolution (spec §4, gap §5 C1).

Label: unit. No MT5 — the resolver/inventory run against fake driver
objects (SimpleNamespace rows + a FakeSource). The mock server has no
symbol channel (contract dependency D7), so there is no mock-server
integration test for symbols; the driver accessors are covered here
with an injected fake MT5 module.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nse_bridge.config import BridgeSettings
from nse_bridge.mt5.driver import Mt5Driver
from nse_bridge.symbols.canonical import (
    broker_name_matches,
    normalize_broker_name,
    normalize_canonical_id,
    parse_canonical_id,
)
from nse_bridge.symbols.inventory import SymbolInventory
from nse_bridge.symbols.metadata import SymbolMetadata
from nse_bridge.symbols.resolver import (
    ResolutionResult,
    ResolutionStatus,
    SymbolResolver,
    differing_fields,
)

pytestmark = pytest.mark.unit


# ----------------------------------------------------------------------
# fakes
# ----------------------------------------------------------------------
def _row(name: str, **overrides):
    """One fake MT5 SymbolInfo row (SimpleNamespace)."""
    fields = {
        "name": name,
        "description": f"{name} description",
        "path": f"Test\\{name}",
        "visible": True,
        "selected": True,
        "trade_mode": 4,
        "digits": 5,
        "point": 0.00001,
        "spread": 10,
        "spread_float": 1.0,
        "trade_tick_size": 0.00001,
        "trade_tick_value": 1.0,
        "trade_tick_value_loss": 1.0,
        "trade_contract_size": 100000.0,
        "volume_min": 0.01,
        "volume_max": 100.0,
        "volume_step": 0.01,
        "volume_limit": 0.0,
        "trade_stops_level": 10,
        "trade_freeze_level": 0,
        "trade_exemode": 2,
        "trade_calc_mode": 0,
        "filling_mode": 3,
        "swap_mode": 1,
        "swap_long": 0.5,
        "swap_short": -0.7,
        "swap_rollover3days": 3,
        "currency_base": None,
        "currency_profit": None,
        "currency_margin": None,
        "margin_initial": 1000.0,
        "margin_maintenance": 500.0,
        "margin_hedged": 250.0,
        "order_gtc_mode": 0,
        "start_time": 0,
        "expiration_time": 0,
        "session_deals": 12,
        "session_turnover": 345.0,
        "session_open": 1.10,
        "session_close": 1.11,
        "session_volume": 7.5,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


class FakeSource:
    """Fake SymbolSource with call counters and mutable rows."""

    def __init__(self, rows):
        self.rows = list(rows)
        self.symbols_get_calls = 0
        self.symbol_info_calls: list[str] = []
        self.select_calls: list[tuple[str, bool]] = []
        self.select_results: dict[str, bool] = {}
        self.select_raises = False
        self.return_none = False

    def symbols_get(self):
        self.symbols_get_calls += 1
        if self.return_none:
            return None
        return list(self.rows)

    def symbol_info(self, name):
        self.symbol_info_calls.append(name)
        for row in self.rows:
            if row.name.upper() == name.upper():
                return row
        return None

    def symbol_select(self, name, enable=True):
        self.select_calls.append((name, enable))
        if self.select_raises:
            raise RuntimeError("IPC down")
        return self.select_results.get(name, True)


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _inventory(*rows) -> tuple[SymbolInventory, FakeSource]:
    source = FakeSource(list(rows))
    return SymbolInventory(source), source


def _resolver(*rows, **kwargs) -> tuple[SymbolResolver, FakeSource]:
    inv, source = _inventory(*rows)
    return SymbolResolver(inv, **kwargs), source


# ----------------------------------------------------------------------
# metadata
# ----------------------------------------------------------------------
def test_metadata_from_mt5_maps_all_spec_fields():
    meta = SymbolMetadata.from_mt5(
        _row(
            "XAUUSD.a",
            description="Gold vs US Dollar",
            currency_base="XAU",
            currency_profit="USD",
            currency_margin="USD",
            digits=2,
            point=0.01,
            trade_tick_size=0.01,
            trade_tick_value=1.0,
            trade_contract_size=100.0,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
            trade_stops_level=5,
            trade_freeze_level=2,
            trade_exemode=2,
            trade_calc_mode=3,
            filling_mode=3,
            swap_long=1.5,
            swap_short=-2.5,
            spread=25,
            spread_float=2.5,
            expiration_time=1790000000,
        )
    )
    assert meta.name == "XAUUSD.a"
    assert meta.description == "Gold vs US Dollar"
    assert (meta.currency_base, meta.currency_profit, meta.currency_margin) == ("XAU", "USD", "USD")
    assert (meta.digits, meta.point) == (2, 0.01)
    assert (meta.tick_size, meta.tick_value) == (0.01, 1.0)
    assert meta.contract_size == 100.0
    assert (meta.volume_min, meta.volume_max, meta.volume_step) == (0.01, 100.0, 0.01)
    assert (meta.stop_level, meta.freeze_level) == (5, 2)
    assert meta.execution_mode == 2 and meta.execution_mode_label == "MARKET"
    assert meta.filling_mode == 3 and meta.filling_modes == ["FOK", "IOC"]
    assert meta.trade_mode == 4 and meta.trade_mode_label == "FULL"
    assert meta.calculation_mode == 3 and meta.calculation_mode_label == "CFD"
    assert (meta.swap_long, meta.swap_short) == (1.5, -2.5)
    assert meta.expiration_time == 1790000000
    assert (meta.spread, meta.spread_float) == (25, 2.5)
    assert meta.margin_initial == 1000.0
    assert meta.session_stats["session_deals"] == 12
    assert meta.is_fully_tradable and meta.is_trade_enabled


def test_metadata_missing_fields_are_none_never_invented():
    meta = SymbolMetadata.from_mt5(SimpleNamespace(name="ODD"))
    assert meta.name == "ODD"
    assert meta.contract_size is None
    assert meta.tick_value is None
    assert meta.digits is None
    assert meta.trade_mode is None
    assert meta.execution_mode is None
    assert meta.calculation_mode is None
    assert meta.filling_modes == []
    assert meta.session_stats == {}
    assert meta.session_schedule is None
    # Unreported trade mode is NOT assumed tradable.
    assert not meta.is_trade_enabled
    assert not meta.is_fully_tradable


def test_metadata_execution_mode_is_not_calculation_mode():
    # trade_exemode=3 (EXCHANGE) with trade_calc_mode=2 (FUTURES):
    # the two properties must not be conflated (see metadata.py note
    # on the pre-existing SymbolInfoModel mapping).
    meta = SymbolMetadata.from_mt5(_row("FUT", trade_exemode=3, trade_calc_mode=2))
    assert meta.execution_mode == 3 and meta.execution_mode_label == "EXCHANGE"
    assert meta.calculation_mode == 2 and meta.calculation_mode_label == "FUTURES"


def test_metadata_session_schedule_always_none():
    # The MT5 Python API exposes session aggregates, never an hourly
    # schedule — the field must stay None even for a full row.
    meta = SymbolMetadata.from_mt5(_row("XAUUSD"))
    assert meta.session_schedule is None
    assert meta.session_stats  # aggregates ARE carried


def test_metadata_matches_expected_and_rejects_unknown_keys():
    meta = SymbolMetadata.from_mt5(_row("XAUUSD", trade_contract_size=100.0))
    assert meta.matches_expected({"contract_size": 100.0})
    assert meta.matches_expected({"contract_size": 100.0000000001})  # float tolerance
    assert not meta.matches_expected({"contract_size": 10.0})
    assert not meta.matches_expected({"trade_mode": 0})
    assert meta.matches_expected({"currency_profit": None})
    assert not meta.matches_expected({"currency_profit": "USD"})
    with pytest.raises(ValueError, match="unknown SymbolMetadata field"):
        meta.matches_expected({"contractsize": 100.0})


def test_metadata_distinguishing_signature():
    a = SymbolMetadata.from_mt5(_row("XAUUSD", trade_contract_size=100.0))
    b = SymbolMetadata.from_mt5(_row("XAUUSDm", trade_contract_size=10.0))
    c = SymbolMetadata.from_mt5(_row("XAUUSD.a", trade_contract_size=100.0))
    assert a.distinguishing_signature() != b.distinguishing_signature()
    assert a.distinguishing_signature() == c.distinguishing_signature()


# ----------------------------------------------------------------------
# canonical IDs
# ----------------------------------------------------------------------
def test_parse_canonical_id_variants():
    assert parse_canonical_id("EUR/USD") == ("EUR", "USD")
    assert parse_canonical_id("eurusd") == ("EUR", "USD")
    assert parse_canonical_id("EUR-USD") == ("EUR", "USD")
    assert parse_canonical_id("eur_usd") == ("EUR", "USD")
    assert parse_canonical_id(" xauusd ") == ("XAU", "USD")
    assert parse_canonical_id("BTC/USD") == ("BTC", "USD")
    assert normalize_canonical_id("eurusd") == "EUR/USD"


def test_parse_canonical_id_refuses_to_guess_a_split():
    assert parse_canonical_id("") is None
    assert parse_canonical_id(None) is None
    assert parse_canonical_id("EUR") is None
    assert parse_canonical_id("US30") is None  # 4 letters: split unknowable
    assert parse_canonical_id("SPX500USD") is None  # 9 chars, no slash
    assert parse_canonical_id("EUR/USD/JPY") is None


def test_normalize_broker_name_and_suffix_matching():
    assert normalize_broker_name("XAUUSD.a") == "XAUUSDA"
    assert normalize_broker_name("GOLD#") == "GOLD"
    assert normalize_broker_name("EURUSD-ECN") == "EURUSDECN"
    assert broker_name_matches("XAUUSD.a", "XAU", "USD")
    assert broker_name_matches("XAUUSDm", "XAU", "USD")
    assert broker_name_matches("XAUUSDPRO", "XAU", "USD")
    assert broker_name_matches("GOLD#", "XAU", "USD")  # documented alias
    assert broker_name_matches("GOLD", "XAU", "USD")
    assert not broker_name_matches("XAGUSD", "XAU", "USD")
    assert not broker_name_matches("EURUSD", "XAU", "USD")
    # A long alphabetic tail is a different instrument, not a suffix.
    assert not broker_name_matches("XAUUSDFUTURES", "XAU", "USD")
    assert not broker_name_matches("XAUJPY", "XAU", "USD")


# ----------------------------------------------------------------------
# inventory / discovery
# ----------------------------------------------------------------------
def test_inventory_all_visible_hidden_search_get():
    inv, _ = _inventory(
        _row("EURUSD", description="Euro vs US Dollar", currency_base="EUR", currency_profit="USD"),
        _row("XAUUSD.a", description="Gold spot", visible=False, currency_base="XAU", currency_profit="USD"),
        _row("GBPUSD", visible=None, currency_base="GBP", currency_profit="USD"),
    )
    assert [m.name for m in inv.all_symbols()] == ["EURUSD", "XAUUSD.a", "GBPUSD"]
    assert [m.name for m in inv.visible_symbols()] == ["EURUSD"]
    # Hidden = the rows the driver reported as hidden (completeness caveat: see docs).
    assert [m.name for m in inv.hidden_symbols()] == ["XAUUSD.a"]
    # visible=None is in NEITHER list but still in the inventory.
    assert inv.get("GBPUSD") is not None
    assert [m.name for m in inv.search("gold")] == ["XAUUSD.a"]
    assert [m.name for m in inv.search("usd")] == ["EURUSD", "XAUUSD.a", "GBPUSD"]
    assert inv.search("") == inv.all_symbols()
    assert inv.get("eurusd").name == "EURUSD"
    assert inv.get("NOPE") is None


def test_inventory_refresh_rebuilds_and_summary_counts():
    inv, source = _inventory(_row("EURUSD"))
    assert len(inv.all_symbols()) == 1
    source.rows.append(_row("GBPUSD", visible=False))
    assert len(inv.all_symbols()) == 1  # snapshot: no silent re-read
    refreshed = inv.refresh()
    assert [m.name for m in refreshed] == ["EURUSD", "GBPUSD"]
    summary = inv.summary()
    assert summary["total"] == 2
    assert summary["visible"] == 1 and summary["hidden"] == 1
    assert summary["rows_reported_by_driver"] == 2
    assert "driver/broker-dependent" in summary["completeness"]


def test_inventory_refresh_failure_keeps_old_snapshot():
    inv, source = _inventory(_row("EURUSD"))
    assert len(inv.all_symbols()) == 1
    source.return_none = True
    with pytest.raises(ValueError, match="symbols_get\\(\\) returned None"):
        inv.refresh()
    assert [m.name for m in inv.all_symbols()] == ["EURUSD"]  # old snapshot kept


def test_inventory_metadata_live_fallback_and_select():
    inv, source = _inventory(_row("EURUSD"))
    assert inv.all_symbols()  # load snapshot
    source.rows.append(_row("GBPUSD"))
    # metadata() reads live, bypassing the snapshot.
    assert inv.metadata("GBPUSD").name == "GBPUSD"
    assert inv.metadata("NOPE") is None
    assert source.symbol_info_calls.count("GBPUSD") == 1
    assert inv.select_for_market_watch("GBPUSD") is True
    source.select_results["GBPUSD"] = False
    assert inv.select_for_market_watch("GBPUSD") is False
    source.select_raises = True
    assert inv.select_for_market_watch("GBPUSD") is False


def test_inventory_availability():
    inv, _ = _inventory(
        _row("EURUSD", trade_mode=4),
        _row("XAUUSD", trade_mode=0),
        _row("GBPUSD", trade_mode=3),
        _row("USDJPY", trade_mode=None),
    )
    ok = inv.availability("EURUSD")
    assert ok.exists and ok.tradable and ok.fully_tradable
    assert "FULL" in ok.reason
    disabled = inv.availability("XAUUSD")
    assert disabled.exists and not disabled.tradable
    assert "DISABLED" in disabled.reason
    close_only = inv.availability("GBPUSD")
    assert close_only.exists and not close_only.fully_tradable and close_only.tradable
    assert "CLOSE_ONLY" in close_only.reason
    unknown_mode = inv.availability("USDJPY")
    assert not unknown_mode.tradable  # unreported mode: never assumed tradable
    missing = inv.availability("NOPE")
    assert not missing.exists and not missing.tradable
    assert "not reported" in missing.reason


# ----------------------------------------------------------------------
# resolver — required cases (a)–(e)
# ----------------------------------------------------------------------
def test_resolve_exact_single_match_resolves():
    resolver, _ = _resolver(
        _row("XAUUSD", currency_base="XAU", currency_profit="USD", trade_contract_size=100.0),
    )
    result = resolver.resolve("XAU/USD")
    assert result.status is ResolutionStatus.RESOLVED
    assert result.exact_symbol == "XAUUSD"
    assert result.canonical_id == "XAU/USD"
    assert len(result.candidates) == 1


def test_resolve_two_similar_names_with_expected_metadata():
    resolver, _ = _resolver(
        _row("XAUUSD", currency_base="XAU", currency_profit="USD", trade_contract_size=100.0),
        _row("XAUUSDm", currency_base="XAU", currency_profit="USD", trade_contract_size=10.0),
    )
    # Without expected metadata: ambiguous — the Bridge never guesses.
    ambiguous = resolver.resolve("XAU/USD")
    assert ambiguous.status is ResolutionStatus.AMBIGUOUS
    assert ambiguous.exact_symbol is None
    assert {c.name for c in ambiguous.candidates} == {"XAUUSD", "XAUUSDm"}
    assert "contract_size" in ambiguous.reason
    # With the metadata the server expects: resolves to the matching one.
    resolved = resolver.resolve("XAU/USD", expected={"contract_size": 10.0})
    assert resolved.status is ResolutionStatus.RESOLVED
    assert resolved.exact_symbol == "XAUUSDm"


def test_resolve_two_indistinguishable_candidates_is_ambiguous():
    resolver, _ = _resolver(
        _row("XAUUSD.a", currency_base="XAU", currency_profit="USD", trade_contract_size=100.0),
        _row("XAUUSDm", currency_base="XAU", currency_profit="USD", trade_contract_size=100.0),
    )
    result = resolver.resolve("XAU/USD")
    assert result.status is ResolutionStatus.AMBIGUOUS
    assert result.exact_symbol is None
    assert {c.name for c in result.candidates} == {"XAUUSD.a", "XAUUSDm"}
    assert "indistinguishable" in result.reason
    # Even expected metadata cannot separate them — still ambiguous.
    again = resolver.resolve("XAU/USD", expected={"contract_size": 100.0})
    assert again.status is ResolutionStatus.AMBIGUOUS
    assert len(again.candidates) == 2


def test_resolve_unknown_canonical_id_is_not_found():
    resolver, _ = _resolver(_row("EURUSD", currency_base="EUR", currency_profit="USD"))
    result = resolver.resolve("GBP/USD")
    assert result.status is ResolutionStatus.NOT_FOUND
    assert result.exact_symbol is None
    assert result.candidates == []
    unparseable = resolver.resolve("US30")
    assert unparseable.status is ResolutionStatus.NOT_FOUND
    assert "not a valid BASE/QUOTE" in unparseable.reason


def test_resolve_cache_hit_refresh_and_invalidate():
    clock = FakeClock()
    resolver, source = _resolver(
        _row("EURUSD", currency_base="EUR", currency_profit="USD"),
        cache_ttl_s=300.0,
        clock=clock,
    )
    assert resolver.resolve("EUR/USD").status is ResolutionStatus.RESOLVED
    assert source.symbols_get_calls == 1
    # Cache hit: a second resolve does NOT re-read MT5.
    assert resolver.resolve("EUR/USD").status is ResolutionStatus.RESOLVED
    assert source.symbols_get_calls == 1
    assert resolver.cache_state()["cached"] is True
    # Explicit refresh re-reads.
    resolver.refresh()
    assert source.symbols_get_calls == 2
    # Invalidate drops the cache without an MT5 call; the next resolve re-reads.
    resolver.invalidate()
    assert source.symbols_get_calls == 2
    assert resolver.cache_state()["cached"] is False
    assert resolver.resolve("EUR/USD").status is ResolutionStatus.RESOLVED
    assert source.symbols_get_calls == 3


def test_resolve_cache_ttl_expiry_forces_reload():
    clock = FakeClock(now=1000.0)
    resolver, source = _resolver(
        _row("EURUSD", currency_base="EUR", currency_profit="USD"),
        cache_ttl_s=60.0,
        clock=clock,
    )
    resolver.resolve("EUR/USD")
    assert source.symbols_get_calls == 1
    clock.now += 61.0  # past the TTL
    resolver.resolve("EUR/USD")
    assert source.symbols_get_calls == 2


# ----------------------------------------------------------------------
# resolver — further rules
# ----------------------------------------------------------------------
def test_resolve_single_suffix_variant_resolves():
    resolver, _ = _resolver(
        _row("XAUUSDm", currency_base="XAU", currency_profit="USD"),
    )
    result = resolver.resolve("XAU/USD")
    assert result.status is ResolutionStatus.RESOLVED
    assert result.exact_symbol == "XAUUSDm"


def test_resolve_gold_alias_and_verbatim_broker_name():
    resolver, _ = _resolver(
        _row("GOLD#", currency_base="XAU", currency_profit="USD"),
    )
    assert resolver.resolve("XAU/USD").exact_symbol == "GOLD#"
    assert resolver.resolve("GOLD#").exact_symbol == "GOLD#"
    assert resolver.resolve("gold#").status is ResolutionStatus.RESOLVED


def test_resolve_verbatim_broker_name_wins():
    resolver, _ = _resolver(
        _row("XAUUSD.a", currency_base="XAU", currency_profit="USD"),
        _row("XAUUSDm", currency_base="XAU", currency_profit="USD"),
    )
    result = resolver.resolve("XAUUSD.a")
    assert result.status is ResolutionStatus.RESOLVED
    assert result.exact_symbol == "XAUUSD.a"


def test_resolve_currency_guard_excludes_wrong_currency_candidate():
    resolver, _ = _resolver(
        _row("EURUSD", currency_base="EUR", currency_profit="EUR"),  # similar name, wrong quote
        _row("EURUSDm", currency_base="EUR", currency_profit="USD"),
    )
    result = resolver.resolve("EUR/USD")
    assert result.status is ResolutionStatus.RESOLVED
    assert result.exact_symbol == "EURUSDm"
    # With only the wrong-currency symbol present: not found, not a guess.
    alone, _ = _resolver(_row("EURUSD", currency_base="EUR", currency_profit="EUR"))
    assert alone.resolve("EUR/USD").status is ResolutionStatus.NOT_FOUND


def test_resolve_expected_metadata_with_no_match_is_ambiguous():
    resolver, _ = _resolver(
        _row("XAUUSD", currency_base="XAU", currency_profit="USD", trade_contract_size=100.0),
        _row("XAUUSDm", currency_base="XAU", currency_profit="USD", trade_contract_size=10.0),
    )
    result = resolver.resolve("XAU/USD", expected={"contract_size": 999.0})
    assert result.status is ResolutionStatus.AMBIGUOUS
    assert result.exact_symbol is None
    assert len(result.candidates) == 2
    assert "none matches the expected metadata" in result.reason


def test_resolve_expected_unknown_key_raises():
    resolver, _ = _resolver(_row("EURUSD", currency_base="EUR", currency_profit="USD"))
    with pytest.raises(ValueError, match="unknown SymbolMetadata field"):
        resolver.resolve("EUR/USD", expected={"contractsize": 100.0})


def test_candidates_returns_metadata_comparison_basis():
    resolver, _ = _resolver(
        _row("XAUUSD", currency_base="XAU", currency_profit="USD", trade_contract_size=100.0),
        _row("XAUUSDm", currency_base="XAU", currency_profit="USD", trade_contract_size=10.0),
        _row("EURUSD", currency_base="EUR", currency_profit="USD"),
    )
    found = resolver.candidates("XAU/USD")
    assert {c.name for c in found} == {"XAUUSD", "XAUUSDm"}
    assert {c.contract_size for c in found} == {100.0, 10.0}
    assert resolver.candidates("GBP/USD") == []
    assert differing_fields(found) == ["contract_size"]


def test_resolution_result_shape():
    result = ResolutionResult(
        status=ResolutionStatus.NOT_FOUND,
        requested="GBP/USD",
        canonical_id="GBP/USD",
        exact_symbol=None,
        candidates=[],
        reason="no broker symbol matches",
    )
    assert result.status.value == "not_found"
    assert result.exact_symbol is None


# ----------------------------------------------------------------------
# driver accessors (fake MT5 module injected into the worker)
# ----------------------------------------------------------------------
class _FakeMt5:
    def __init__(self, rows):
        self._rows = list(rows)
        self.calls: list[str] = []

    def symbols_get(self):
        self.calls.append("symbols_get")
        return list(self._rows)

    def symbol_info(self, name):
        self.calls.append(f"symbol_info:{name}")
        for row in self._rows:
            if row.name == name:
                return row
        return None

    def symbol_select(self, name, enable=True):
        self.calls.append(f"symbol_select:{name}:{enable}")
        return name != "REFUSED"

    def shutdown(self):
        pass


def test_driver_symbol_accessors(tmp_path):
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    driver = Mt5Driver(settings)
    fake = _FakeMt5([_row("EURUSD")])
    driver._mt5 = fake
    try:
        rows = driver.symbols_get()
        assert [r.name for r in rows] == ["EURUSD"]
        assert driver.symbol_info("EURUSD").name == "EURUSD"
        assert driver.symbol_info("NOPE") is None
        assert driver.symbol_select("EURUSD") is True
        assert driver.symbol_select("REFUSED") is False
        assert fake.calls.count("symbols_get") == 1
    finally:
        driver.disconnect()
