"""Phase 2b W2 probe — official mt5.order_check behavior per trade action.

Runs READ-ONLY checks against the attached MT5 Demo terminal:
order_check() never sends an order, so this probe cannot mutate the
account. The one mutation (a far-off test-owned pending order) is
created and deleted inside this script and is used only to give
TRADE_ACTION_MODIFY a real ticket to be checked against.

Evidence recorded in docs/upgrade/PHASE-2B-VERIFICATION.md (E-OC-1).
"""

from __future__ import annotations

import json
import sys

import MetaTrader5 as mt5


def as_dict(obj: object) -> object:
    """Convert a package named-tuple into a JSON-safe dict."""
    if obj is None:
        return None
    if hasattr(obj, "_asdict"):
        return {k: as_dict(v) for k, v in obj._asdict().items()}
    if isinstance(obj, (list, tuple)):
        return [as_dict(v) for v in obj]
    if isinstance(obj, dict):
        return {k: as_dict(v) for k, v in obj.items()}
    return obj


def main() -> int:
    if not mt5.initialize():
        print(json.dumps({"step": "initialize", "ok": False, "last_error": mt5.last_error()}))
        return 1
    try:
        out: dict[str, object] = {}
        acct = mt5.account_info()
        out["account"] = {
            "login": acct.login,
            "server": acct.server,
            "trade_mode": acct.trade_mode,
            "margin_mode": acct.margin_mode,
        }
        out["margin_mode_constants"] = {
            "RETAIL_NETTING": mt5.ACCOUNT_MARGIN_MODE_RETAIL_NETTING,
            "EXCHANGE": mt5.ACCOUNT_MARGIN_MODE_EXCHANGE,
            "RETAIL_HEDGING": mt5.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING,
        }

        tick = mt5.symbol_info_tick("EURUSD")
        spec = mt5.symbol_info("EURUSD")
        out["tick_time"] = tick.time
        out["filling"] = {
            "symbol_filling_mode": spec.filling_mode,
            "ORDER_FILLING_FOK": mt5.ORDER_FILLING_FOK,
            "ORDER_FILLING_IOC": mt5.ORDER_FILLING_IOC,
            "ORDER_FILLING_RETURN": mt5.ORDER_FILLING_RETURN,
        }
        # Resolve a permitted filling mode from the bitmask (same rule as
        # engine/common.resolve_filling_mode).
        fm_mask = int(spec.filling_mode)
        if fm_mask & 1:
            filling = mt5.ORDER_FILLING_FOK
        elif fm_mask & 2:
            filling = mt5.ORDER_FILLING_IOC
        else:
            filling = mt5.ORDER_FILLING_RETURN

        # --- E-OC-1a: order_check on a DEAL (market open) request -------
        deal_req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": "EURUSD",
            "volume": 0.01,
            "type": mt5.ORDER_TYPE_BUY,
            "price": float(tick.ask),
            "sl": 0.0,
            "tp": 0.0,
            "deviation": 20,
            "comment": "NSE-P2B-CHECK",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }
        res = mt5.order_check(deal_req)
        out["order_check_deal"] = as_dict(res)

        # --- E-OC-1b: order_check on an insufficient-margin DEAL --------
        big_req = dict(deal_req, volume=1000.0)
        res_big = mt5.order_check(big_req)
        out["order_check_deal_no_money"] = as_dict(res_big)

        # --- E-OC-1c: order_check on an invalid-volume DEAL -------------
        badvol_req = dict(deal_req, volume=0.001)
        out["order_check_deal_bad_volume"] = as_dict(mt5.order_check(badvol_req))

        # --- test-owned pending order for the MODIFY probe ---------------
        pending_price = round(
            float(tick.bid) - max(500 * spec.point, spec.trade_stops_level * spec.point, 0.001), spec.digits
        )
        pend_req = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": "EURUSD",
            "volume": 0.01,
            "type": mt5.ORDER_TYPE_BUY_LIMIT,
            "price": pending_price,
            "sl": 0.0,
            "tp": 0.0,
            "comment": "NSE-P2B-PROBE",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }
        placed = mt5.order_send(pend_req)
        out["pending_place_retcode"] = int(placed.retcode) if placed is not None else None
        probe_ticket = (
            int(placed.order)
            if (placed is not None and int(placed.retcode) == mt5.TRADE_RETCODE_DONE and placed.order)
            else None
        )

        # --- E-OC-1d: order_check on a TRADE_ACTION_SLTP request --------
        positions = mt5.positions_get()
        out["positions_count"] = len(positions) if positions else 0
        sltp_req = None
        if positions:
            pos = positions[0]
            sltp_req = {
                "action": mt5.TRADE_ACTION_SLTP,
                "position": int(pos.ticket),
                "symbol": str(pos.symbol),
                "sl": float(pos.sl or 0.0),
                "tp": float(pos.tp or 0.0),
            }
            out["order_check_sltp"] = as_dict(mt5.order_check(sltp_req))
            out["order_send_sltp_probe"] = None  # deliberately NOT sent: read-only probe

        # --- E-OC-1e: order_check on a TRADE_ACTION_MODIFY request ------
        if probe_ticket is not None:
            orders = mt5.orders_get(ticket=probe_ticket)
            if orders:
                od = orders[0]
                modify_req = {
                    "action": mt5.TRADE_ACTION_MODIFY,
                    "order": int(od.ticket),
                    "price": float(od.price_open),
                    "sl": float(od.sl or 0.0),
                    "tp": float(od.tp or 0.0),
                }
                out["order_check_modify"] = as_dict(mt5.order_check(modify_req))

            # --- E-OC-1f: order_check on a TRADE_ACTION_REMOVE request ---
            out["order_check_remove"] = as_dict(
                mt5.order_check({"action": mt5.TRADE_ACTION_REMOVE, "order": probe_ticket})
            )
            # cleanup: delete the probe order (test-owned, never touched live positions)
            rm = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": probe_ticket})
            out["pending_cleanup_retcode"] = int(rm.retcode) if rm is not None else None
        else:
            out["order_check_modify"] = "skipped — pending probe order was not placed"
            out["order_check_remove"] = "skipped"

        print(json.dumps(out, indent=2, default=str))
        return 0
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    sys.exit(main())
