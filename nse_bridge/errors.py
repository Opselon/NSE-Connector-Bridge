"""Bridge error model + uniform JSON error envelope.

Every error surfaced by the API uses the same shape:

    {"ok": false, "error": {"code": "...", "message": "...",
                            "retcode": 10016?, "retcode_label": "..."?,
                            "details": {...}?, "request_id": "..."}}
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from .retcodes import translate_retcode


class ErrorCode(str, Enum):
    NOT_CONNECTED = "MT5_NOT_CONNECTED"
    MT5_CALL_FAILED = "MT5_CALL_FAILED"
    MT5_RETCODE = "MT5_RETCODE"
    SYMBOL_NOT_FOUND = "SYMBOL_NOT_FOUND"
    TICKET_NOT_FOUND = "TICKET_NOT_FOUND"
    VALIDATION = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    TRADE_REJECTED = "TRADE_REJECTED"
    TRADE_UNVERIFIED = "TRADE_UNVERIFIED"
    #: Official MT5 order_check refused the pre-send validation (W2 /
    #: CONTRACTS §7): order_send is never reached. Bridge-local code —
    #: the check's raw result travels in the error details.
    ORDER_CHECK_FAILED = "ORDER_CHECK_FAILED"
    INTERNAL = "INTERNAL_ERROR"
    UNAUTHORIZED = "UNAUTHORIZED"
    DISABLED = "FEATURE_DISABLED"
    TIMEOUT = "TIMEOUT"


class BridgeError(Exception):
    """Structured error carrying an ErrorCode + optional MT5 retcode."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retcode: int | None = None,
        details: dict[str, Any] | None = None,
        http_status: int = 200,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retcode = retcode
        self.details = details or {}
        self.http_status = http_status

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
        }
        if self.retcode is not None:
            payload["retcode"] = int(self.retcode)
            payload["retcode_label"] = translate_retcode(self.retcode)
        if self.details:
            payload["details"] = self.details
        return payload


def not_connected() -> BridgeError:
    return BridgeError(
        ErrorCode.NOT_CONNECTED,
        "MT5 terminal is not connected. Call connect_mt5 first.",
        http_status=200,
    )
