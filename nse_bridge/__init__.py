"""NSE MT5 Bridge — secure local bridge between the MetaTrader 5 terminal
and the NSE Platform server.

- FastAPI REST API (one endpoint per command) + full Swagger UI at /docs
- Real-time outbound WebSocket channel to the NSE server (outbound-only)
- Live dashboard GUI at /ui (pywebview window or browser)
- Official MetaTrader5 C++ IPC driver, serialized on a dedicated worker thread
"""

BRIDGE_VERSION = "1.3.0"
PROTOCOL_VERSION = 1

__all__ = ["BRIDGE_VERSION", "PROTOCOL_VERSION"]
