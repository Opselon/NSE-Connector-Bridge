"""Official MQL5 trade-server return-code map (10004..10046).

Ported from the production adapter (BUG-226 forensics): labels follow
mql5.com/en/docs/constants/errorswarnings/enum_trade_return_codes exactly —
10016 is INVALID_STOPS (not TRADE_DISABLED) and the 10017+ sequence is not
shifted. Real-MT5 defect 1 (2026-11-18, verified against the MetaTrader5
package constants + the MQL5 reference): 10025 is NO_CHANGES (not
UNKNOWN_SYMBOL), 10026 SERVER_DISABLES_AT, 10027 CLIENT_DISABLES_AT,
10028 LOCKED, 10030 INVALID_FILL, 10031 CONNECTION — and 10032..10046
were missing entirely (the real LONG_ONLY is 10042, not 10027). 10037
is unassigned in the official enum, so it stays unmapped (reported as
UNKNOWN_MT5_RETCODE) rather than invented.

Also carries the pending-recovery cause classes:

TRANSIENT   — broker-side state may resolve on its own; retry with backoff.
REPAIRABLE  — the request itself is structurally invalid; one validated repair.
HARD_REJECT — never re-send, never repair (includes unknown codes, 10029 FROZEN).
IDEMPOTENT  — not a failure at all: NO_CHANGES means the requested state
              already holds. Treat as success, never retry.
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
    10025: "NO_CHANGES - No changes in request",
    10026: "SERVER_DISABLES_AT - Autotrading disabled by server",
    10027: "CLIENT_DISABLES_AT - Autotrading disabled by client terminal",
    10028: "LOCKED - Request locked for processing",
    10029: "FROZEN - Order or position frozen by broker (close/modification banned)",
    10030: "INVALID_FILL - Invalid order filling type",
    10031: "CONNECTION - No connection with the trade server",
    10032: "ONLY_REAL - Operation is allowed only for live accounts",
    10033: "LIMIT_ORDERS - The number of pending orders has reached the limit",
    10034: "LIMIT_VOLUME - The volume of orders and positions for the symbol has reached the limit",
    10035: "INVALID_ORDER - Incorrect or prohibited order type",
    10036: "POSITION_CLOSED - Position with the specified POSITION_IDENTIFIER has already been closed",
    # 10037 is unassigned in the official MQL5 enum — deliberately unmapped.
    10038: "INVALID_CLOSE_VOLUME - A close volume exceeds the current position volume",
    10039: "CLOSE_ORDER_EXIST - A close order already exists for a specified position",
    10040: "LIMIT_POSITIONS - The number of open positions on the account has reached the server limit",
    10041: "REJECT_CANCEL - The pending order activation request is rejected, the order is canceled",
    10042: "LONG_ONLY - Only long positions are allowed for the symbol",
    10043: "SHORT_ONLY - Only short positions are allowed for the symbol",
    10044: "CLOSE_ONLY - Only position closing is allowed for the symbol",
    10045: "FIFO_CLOSE - Position closing is allowed only by FIFO rule for the trading account",
    10046: "HEDGE_PROHIBITED - Opposite positions on a single symbol are disabled for the trading account",
}

# retcode 0 is NOT a trade-server code; the package marks requests that never
# reached the server (structural validation failure / lost IPC) with it.
RETCODE_MAP[0] = "IPC_FAIL - Request never reached the trade server (retcode 0)"

PENDING_CLASS_TRANSIENT: frozenset[int] = frozenset({10004, 10012, 10020, 10021, 10031})
PENDING_CLASS_REPAIRABLE: frozenset[int] = frozenset({10013, 10015, 10016})

TRADE_RETCODE_DONE = 10009
TRADE_RETCODE_DONE_PARTIAL = 10010  # DONE_PARTIAL — request partially completed
TRADE_RETCODE_NO_CHANGES = 10025  # NO_CHANGES — requested state already holds; idempotent success
TRADE_RETCODE_PLACED = 10008  # PLACED — order accepted and placed (pending), not yet filled
TRADE_RETCODE_CONNECTION = 10031  # CONNECTION — no connection with the trade server (outcome UNKNOWN)
#: 10036 POSITION_CLOSED — the position is already closed. For a close
#: this is an IDEMPOTENT success once the live position book confirms
#: the ticket is gone (S4: a close that reports 10036 must not be
#: retried or reported as a failure). ``classify_retcode`` still maps
#: it to HARD_REJECT — the close path handles it explicitly, because
#: only the close path can verify the idempotent state.
TRADE_RETCODE_POSITION_CLOSED = 10036
#: order_check pass code — the package reports 0 when the pre-send check
#: succeeded (verified live: scripts/phase2b_probe_order_check.py, E-OC-1).
ORDER_CHECK_RETCODE_OK = 0


def translate_retcode(retcode: int | None) -> str:
    if retcode is None:
        return "NO_RESPONSE - order_send returned no result object"
    return RETCODE_MAP.get(int(retcode), f"UNKNOWN_MT5_RETCODE ({retcode})")


def classify_retcode(retcode: int | None) -> str:
    """Cause class for a trade retcode (TRANSIENT/REPAIRABLE/HARD_REJECT/IDEMPOTENT)."""
    if retcode == TRADE_RETCODE_NO_CHANGES:
        return "IDEMPOTENT"
    if retcode in PENDING_CLASS_TRANSIENT:
        return "TRANSIENT"
    if retcode in PENDING_CLASS_REPAIRABLE:
        return "REPAIRABLE"
    return "HARD_REJECT"
