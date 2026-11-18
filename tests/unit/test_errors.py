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
