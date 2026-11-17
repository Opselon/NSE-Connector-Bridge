"""Reference NSE Platform server for bridge channel development/testing.

Listens on ws://127.0.0.1:9099, authenticates the bridge token, prints the
hello handshake, dispatches a sample command, subscribes to ticks, and logs
everything it receives. Use as the template for the real server integration.

Usage:  python scripts/mock_server.py [--token test-token] [--port 9099]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal

import websockets

BRIDGE_TOKEN = "test-token"


async def bridge_handler(ws) -> None:  # type: ignore[no-untyped-def]
    peer = ws.remote_address
    print(f"[SERVER] bridge connected from {peer}")
    auth = ws.request.headers.get("Authorization", "")
    if f"Bearer {BRIDGE_TOKEN}" not in auth:
        print("[SERVER] missing/invalid token — closing")
        await ws.close(code=4401, reason="unauthorized")
        return

    hello = json.loads(await ws.recv())
    print(f"[SERVER] hello: device={hello.get('device_id')} version={hello.get('bridge_version')} "
          f"mt5={hello.get('mt5', {}).get('state')} commands={hello.get('capabilities', {}).get('commands_count')}")

    # dispatch a sample command
    await ws.send(json.dumps({"type": "command", "id": "demo-1", "command": "get_account_balance", "params": {}}))
    result = json.loads(await ws.recv())
    print(f"[SERVER] command_result: {result}")

    # subscribe to live ticks
    await ws.send(json.dumps({"type": "subscribe", "symbol": "XAUUSD", "interval_ms": 500}))
    print("[SERVER] subscribed to XAUUSD ticks — streaming will follow (Ctrl+C to stop)")

    async for raw in ws:
        msg = json.loads(raw)
        mtype = msg.get("type")
        if mtype == "tick":
            d = msg.get("data", {})
            print(f"[SERVER] tick {d.get('symbol')}: bid={d.get('bid')} ask={d.get('ask')}")
        elif mtype == "account_update":
            d = msg.get("data", {})
            print(f"[SERVER] account: balance={d.get('balance')} equity={d.get('equity')}")
        elif mtype == "ping":
            print(f"[SERVER] heartbeat (uptime={msg.get('uptime_s')}s)")
        elif mtype == "command_result":
            print(f"[SERVER] command_result: {msg}")
        else:
            print(f"[SERVER] {mtype}: {str(msg)[:120]}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9099)
    parser.add_argument("--token", type=str, default="test-token")
    args = parser.parse_args()
    global BRIDGE_TOKEN
    BRIDGE_TOKEN = args.token

    async with websockets.serve(bridge_handler, "127.0.0.1", args.port):
        print(f"[SERVER] mock NSE server listening on ws://127.0.0.1:{args.port} (token required)")
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("[SERVER] stopped")
