"""Official MQL5 trade-server return-code map (10004..10031).

Ported from the production adapter (BUG-226 forensics): labels follow
mql5.com/en/docs/constants/errorswarnings/enum_trade_return_codes exactly —
10016 is INVALID_STOPS (not TRADE_DISABLED) and the 10017+ sequence is not
shifted. Also carries the pending-recovery cause classes:

TRANSIENT   — broker-side state may resolve on its own; retry with backoff.
REPAIRABLE  — the request itself is structurally invalid; one validated repair.
HARD_REJECT — never re-send, never repair (includes unknown codes, 10029 FROZEN).
"""

from __future__ import annotations

RETCODE_MAP: dict[int, str] = {
    10004: "REQUOTE - Price changed during order dispatch",
    10006: "REJECTED - Broker rejected the execution request",
    10007: "CANCEL - Request cancelled by trader or expert",
    10008: "PLACED - Order placed (pending accepted)",
    10009: "DONE - Request completed successfully",
    10010: "DONE_PARTIAL - Request partially completed",
    10011: "ERROR - Request processing error",
    10012: "TIMEOUT - Request timed out",
    10013: "INVALID - Invalid request",
    10014: "INVALID_VOLUME - Invalid volume in the request",
    10015: "INVALID_PRICE - Invalid price in the request",
    10016: "INVALID_STOPS - Invalid stops in the request",
    10017: "TRADE_DISABLED - Trade is disabled",
    10018: "MARKET_CLOSED - Financial market is currently closed",
    10019: "NO_MONEY - Insufficient account free margin",
    10020: "PRICE_CHANGED - Prices changed (re-quote)",
    10021: "PRICE_OFF - There are no quotes to process the request",
    10022: "INVALID_EXPIRATION - Invalid order expiration date",
    10023: "ORDER_CHANGED - Order state changed",
    10024: "TOO_MANY_REQUESTS - Too frequent requests",
    10025: "UNKNOWN_SYMBOL - Unknown symbol",
    10026: "ORDER_LOCKED - Order or position frozen",
    10027: "LONG_ONLY - Only long positions allowed on symbol",
    10028: "SHORT_ONLY - Only short positions allowed on symbol",
    10029: "FROZEN - Order or position frozen by broker (close/modification banned)",
    10030: "UNSUPPORTED_FILLING - Filling mode unsupported by broker",
    10031: "NO_CONNECTION - No connection with trade server",
}

# retcode 0 is NOT a trade-server code; the package marks requests that never
# reached the server (structural validation failure / lost IPC) with it.
RETCODE_MAP[0] = "IPC_FAIL - Request never reached the trade server (retcode 0)"

PENDING_CLASS_TRANSIENT: frozenset[int] = frozenset({10004, 10012, 10020, 10021, 10031})
PENDING_CLASS_REPAIRABLE: frozenset[int] = frozenset({10013, 10015, 10016})

TRADE_RETCODE_DONE = 10009
TRADE_RETCODE_DONE_PARTIAL = 10010  # DONE_PARTIAL — request partially completed


def translate_retcode(retcode: int | None) -> str:
    if retcode is None:
        return "NO_RESPONSE - order_send returned no result object"
    return RETCODE_MAP.get(int(retcode), f"UNKNOWN_MT5_RETCODE ({retcode})")


def classify_retcode(retcode: int | None) -> str:
    """Cause class for a failed trade retcode (TRANSIENT/REPAIRABLE/HARD_REJECT)."""
    if retcode in PENDING_CLASS_TRANSIENT:
        return "TRANSIENT"
    if retcode in PENDING_CLASS_REPAIRABLE:
        return "REPAIRABLE"
    return "HARD_REJECT"
