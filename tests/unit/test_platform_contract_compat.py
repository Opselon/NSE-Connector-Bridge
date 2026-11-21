"""Contract-compatibility tests: Bridge ↔ current NSE Platform behavior.

These pin the Bridge side of the Platform's 2026-11-21 connector fixes
(Platform PR #144, commit 9f27432) and the CONTRACT v1 rules they
interpret. The Platform repository is the integration authority —
when one of these tests fails after a Platform contract change, the
Bridge must be reconciled deliberately, never silently.

Pinned here:
- §7.4 demo fail-closed (Bug A): ``account_items`` OMITS ``type`` when
  MT5 does not report trade_mode; only real MT5 values 0/1/2 map to
  demo/contest/live. The server refuses missing/live/unknown types —
  the Bridge must never invent a type that would let a live account
  slip past the demo-only gate (or accidentally ban a demo one).
- §3.3 batch_id (Bug D): the Bridge's batch ids must parse as UUIDs —
  the server validates with ``Uuid::parse_str`` (any version passes,
  the contract words uuidv7).
- §5.3 liveness (Bug C mirror): the Bridge declares a session dead
  after 45 s without a server HEARTBEAT REPLY — server-originated
  pushes (commands, chart frames) do not keep the session alive;
  the Platform's mirror fix counts only client-originated frames.
- §6.6 replay (Bug B): a redelivered command is answered without a
  second execution (the replay cache is the Bridge half of the
  server's delivered-command replay).
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

pytestmark = pytest.mark.unit

from nse_bridge.server.sync import SyncEngine, build_batch
from nse_bridge.server.uuid7 import new_uuid7_str


# ---------------------------------------------------------------------------
# §7.4 demo fail-closed — account.type reporting
# ---------------------------------------------------------------------------


class _Acc:
    def __init__(self, trade_mode: int | None) -> None:
        self.login = 10011755849
        self.server = "MetaQuotes-Demo"
        self.name = "Tester"
        self.currency = "USD"
        self.balance = 1000.0
        self.equity = 1000.0
        self.margin = 0.0
        self.margin_free = 1000.0
        self.profit = 0.0
        self.credit = 0.0
        self.trade_mode = trade_mode


@pytest.mark.parametrize(
    ("raw_mode", "expected_type"),
    [
        (0, "demo"),
        (1, "contest"),
        (2, "live"),
        (None, None),  # unreported → OMIT the field entirely
        (7, None),  # unknown integer → OMIT (never guess)
    ],
)
def test_account_batch_type_fail_closed(raw_mode: int | None, expected_type: str | None):
    items = SyncEngine.account_items(_Acc(raw_mode), {"state": "CONNECTED", "terminal_version": "5.0"})
    account = items["account"]
    if expected_type is None:
        assert "type" not in account, (
            "an unreported/unknown trade_mode must never be labelled — the Platform "
            "fail-closed gate refuses absent types, so a fabricated 'demo' would let "
            "a live account slip past §7.4"
        )
    else:
        assert account["type"] == expected_type


# ---------------------------------------------------------------------------
# §3.3 batch_id — UUID parseability (Platform validates with Uuid::parse_str)
# ---------------------------------------------------------------------------


def test_build_batch_id_is_a_parseable_uuid():
    batch = build_batch("account", {"account": {}})
    parsed = uuid.UUID(batch["batch_id"])  # raises when not a UUID
    assert batch["batch_id"] == str(parsed)
    # uuidv7 shape per contract §3.3: version nibble 7.
    assert parsed.version == 7, f"expected uuidv7, got version {parsed.version}"


def test_build_batch_ids_are_unique():
    ids = {build_batch("account", {"account": {}})["batch_id"] for _ in range(1000)}
    assert len(ids) == 1000, "batch ids are idempotency keys — collisions would drop batches"


# ---------------------------------------------------------------------------
# §5.3 client-side liveness — heartbeat REPLY semantics
# ---------------------------------------------------------------------------


class _FakeWsForLiveness:
    """Records sends; the test drives what the 'server' answers."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed: str | None = None

    async def send(self, raw: str) -> None:
        self.sent.append(raw)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = reason or str(code)


def _make_liveness_channel(tmp_path, dead_after_s: float):
    from nse_bridge.config import BridgeSettings
    from nse_bridge.engine.common import BridgeContext
    from nse_bridge.mt5.driver import Mt5Driver
    from nse_bridge.realtime.channel import ServerChannel

    # heartbeat_interval_s=1.0 is the loop's clamp floor: tests must
    # not wait out the production 15 s between ticks.
    settings = BridgeSettings(data_dir=tmp_path, heartbeat_interval_s=1.0)
    ctx = BridgeContext(settings=settings, driver=Mt5Driver(settings))
    channel = ServerChannel(ctx, bus=None, dead_after_s=dead_after_s)
    return channel, ctx


async def test_session_survives_when_heartbeat_replies_arrive(tmp_path):
    channel, _ctx = _make_liveness_channel(tmp_path, dead_after_s=0.05)
    ws = _FakeWsForLiveness()
    # Simulate the session clock: a fresh reply marks the path alive.
    channel._last_heartbeat_reply_at = time.monotonic()

    async def replier():
        while not ws.closed and len(ws.sent) < 3:
            await asyncio.sleep(0.01)
            # The server answers every client heartbeat...
            channel._last_heartbeat_reply_at = time.monotonic()

    task = asyncio.create_task(replier())
    heartbeat = asyncio.create_task(channel._heartbeat_loop(ws))
    await asyncio.sleep(2.4)  # ≥ 2 ticks at the 1 s clamp floor
    heartbeat.cancel()
    task.cancel()
    try:
        await heartbeat
    except asyncio.CancelledError:
        pass
    assert ws.closed is None, "replied heartbeats must keep the session alive"
    assert len(ws.sent) >= 1, "the client must keep sending heartbeats"


async def test_session_dies_without_heartbeat_replies_despite_other_frames(tmp_path):
    channel, _ctx = _make_liveness_channel(tmp_path, dead_after_s=0.05)
    ws = _FakeWsForLiveness()
    # Server-originated pushes arrive continuously, but NO heartbeat
    # reply: per §5.3 (and the Platform's Bug C mirror) these frames
    # do NOT prove the heartbeat path is alive.
    channel._last_heartbeat_reply_at = time.monotonic() - 999.0
    channel._last_server_frame_at = time.monotonic()  # "frames" are flowing

    heartbeat = asyncio.create_task(channel._heartbeat_loop(ws))
    await asyncio.wait_for(heartbeat, timeout=3.0)  # first tick at the 1 s floor
    assert ws.closed is not None, "no heartbeat reply for >dead_after must close the session"


# ---------------------------------------------------------------------------
# §6.6 replay — redelivered command answered once
# ---------------------------------------------------------------------------


async def test_redelivered_command_is_not_executed_twice():
    """The Platform replays delivered-but-unacked commands on reconnect
    (PR #144 Bug B). The Bridge's replay cache is the client half: the
    same command_id must produce the cached outcome, not a second MT5
    execution."""
    from nse_bridge.server.commands import CommandValidator, RejectionReason
    from nse_bridge.server.rest import Entitlements

    validator = CommandValidator(
        "device-1",
        get_account_id=lambda: "10011755849",
        entitlements=Entitlements(history_sync=True, position_manager=True, auto_trade=True),
    )
    envelope = {
        "type": "command",
        "command_id": new_uuid7_str(),
        "account_id": "10011755849",
        "device_id": "device-1",
        "operation": {"type": "close_position", "ticket": 123456},
        "issued_at": "2026-11-21T10:00:00.000Z",
        "expires_at": "2099-01-01T00:00:00.000Z",
        "idempotency_key": "ignored-by-validator",
    }
    first = validator.validate(envelope)
    assert first.ok is True
    # The server re-delivers the same command after a reconnect:
    second = validator.validate(envelope)
    assert second.ok is False, "a redelivered command must be answered from the replay cache, not re-executed"
    assert second.rejection is RejectionReason.REPLAYED
