"""Unit tests for the uniform error envelope (contract §3.6 shape).

Every API error is `{"ok": false, "error": {"code", "message", ...}}`.
These tests cover the `errors` module only — no MT5, no HTTP layer.
"""

from __future__ import annotations

import pytest

from nse_bridge.errors import BridgeError, ErrorCode, not_connected
from nse_bridge.retcodes import classify_retcode, translate_retcode

pytestmark = pytest.mark.unit


def _envelope(err: BridgeError) -> dict:
    """The envelope shape the API layer builds around BridgeError."""
    return {"ok": False, "error": err.to_dict()}


def test_error_envelope_shape():
    err = BridgeError(ErrorCode.VALIDATION, "bad symbol")
    body = _envelope(err)
    assert body["ok"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["message"] == "bad symbol"
    assert "retcode" not in body["error"]


def test_error_envelope_carries_retcode_and_label():
    err = BridgeError(ErrorCode.MT5_RETCODE, "invalid stops", retcode=10016)
    body = _envelope(err)
    assert body["error"]["retcode"] == 10016
    assert body["error"]["retcode_label"].startswith("INVALID_STOPS")


def test_error_envelope_carries_details_when_given():
    err = BridgeError(ErrorCode.VALIDATION, "bad", details={"field": "symbol"})
    assert _envelope(err)["error"]["details"] == {"field": "symbol"}


def test_not_connected_helper():
    err = not_connected()
    assert err.code is ErrorCode.NOT_CONNECTED
    assert err.code.value == "MT5_NOT_CONNECTED"
    assert "connect_mt5" in err.message


def test_error_codes_are_unique_strings():
    values = [c.value for c in ErrorCode]
    assert len(set(values)) == len(values)
    assert all(isinstance(v, str) and v for v in values)


def test_translate_retcode_known_and_unknown():
    assert translate_retcode(10009).startswith("DONE")
    assert translate_retcode(10016).startswith("INVALID_STOPS")
    assert translate_retcode(0).startswith("IPC_FAIL")
    assert translate_retcode(99999).startswith("UNKNOWN_MT5_RETCODE")
    assert translate_retcode(None).startswith("NO_RESPONSE")


def test_translate_retcode_real_mt5_defect_1_labels():
    # Real-MT5 defect 1 (2026-11-18): six labels were wrong, verified
    # against the MetaTrader5 package constants + the MQL5 reference
    # (mql5.com/en/docs/constants/errorswarnings/enum_trade_return_codes).
    assert translate_retcode(10025).startswith("NO_CHANGES")
    assert translate_retcode(10026).startswith("SERVER_DISABLES_AT")
    assert translate_retcode(10027).startswith("CLIENT_DISABLES_AT")
    assert translate_retcode(10028).startswith("LOCKED")
    assert translate_retcode(10030).startswith("INVALID_FILL")
    assert translate_retcode(10031).startswith("CONNECTION")
    # 10032..10045 were entirely missing (the real LONG_ONLY is 10042).
    assert translate_retcode(10032).startswith("ONLY_REAL")
    assert translate_retcode(10033).startswith("LIMIT_ORDERS")
    assert translate_retcode(10034).startswith("LIMIT_VOLUME")
    assert translate_retcode(10035).startswith("INVALID_ORDER")
    assert translate_retcode(10036).startswith("POSITION_CLOSED")
    assert translate_retcode(10038).startswith("INVALID_CLOSE_VOLUME")
    assert translate_retcode(10039).startswith("CLOSE_ORDER_EXIST")
    assert translate_retcode(10040).startswith("LIMIT_POSITIONS")
    assert translate_retcode(10041).startswith("REJECT_CANCEL")
    assert translate_retcode(10042).startswith("LONG_ONLY")
    assert translate_retcode(10043).startswith("SHORT_ONLY")
    assert translate_retcode(10044).startswith("CLOSE_ONLY")
    assert translate_retcode(10045).startswith("FIFO_CLOSE")
    assert translate_retcode(10046).startswith("HEDGE_PROHIBITED")
    # 10037 is unassigned in the official enum — honestly unknown, never invented.
    assert translate_retcode(10037).startswith("UNKNOWN_MT5_RETCODE")


def test_classify_retcode_cause_classes():
    assert classify_retcode(10004) == "TRANSIENT"  # requote
    assert classify_retcode(10012) == "TRANSIENT"  # timeout
    assert classify_retcode(10031) == "TRANSIENT"  # no connection
    assert classify_retcode(10016) == "REPAIRABLE"  # invalid stops
    assert classify_retcode(10013) == "REPAIRABLE"  # invalid request
    assert classify_retcode(10006) == "HARD_REJECT"  # rejected
    assert classify_retcode(10029) == "HARD_REJECT"  # frozen
    assert classify_retcode(99999) == "HARD_REJECT"  # unknown
    assert classify_retcode(None) == "HARD_REJECT"
    # NO_CHANGES is not a failure: the requested state already holds.
    assert classify_retcode(10025) == "IDEMPOTENT"
