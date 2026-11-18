"""UUIDv7 generation (RFC 9562).

The contract uses uuidv7 for ``batch_id``, ``command_id``,
``message_id``, ``request_id`` and ``subscription_id`` (§3.3, §6.2,
§6.5, §17.1). Python 3.12 has no ``uuid.uuid7`` (added in 3.14), so
this module implements it: 48-bit Unix-millisecond timestamp,
version nibble ``7``, variant bits ``10``, 74 bits of randomness.
IDs sort roughly in creation order and are unique per call.
"""

from __future__ import annotations

import secrets
import time
import uuid

__all__ = ["new_uuid7", "new_uuid7_str"]


def new_uuid7() -> uuid.UUID:
    """Return a fresh UUIDv7."""
    ms = int(time.time() * 1000)
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)
    value = (ms << 80) | (0x7 << 76) | (rand_a << 64) | (0x2 << 62) | rand_b
    return uuid.UUID(int=value)


def new_uuid7_str() -> str:
    """Return a fresh UUIDv7 as a string."""
    return str(new_uuid7())
