"""Bounded-retry policy for the v1 execution path (S4, v1.3.0).

The v1 ops (``close_position`` / ``modify_sl_tp`` / ``modify_pending``)
used to retry ANY non-DONE retcode up to 3 times with a fixed 25 ms
sleep. That retried hard rejects (frozen positions, invalid volume,
trade disabled) pointlessly, and the fixed sleep ignored broker
rate-limiting. This module is the single rule for execution retries:

* Only RETRYABLE retcodes are ever re-sent: the TRANSIENT class
  (requote / timeout / price-changed / price-off) plus 10024
  TOO_MANY_REQUESTS, which is a rate limit — backing off and retrying
  is the correct response to it. Everything else (HARD_REJECT,
  REPAIRABLE, IDEMPOTENT) fails fast on the first attempt.
* Delays are bounded exponential backoff (0.1 s → 0.2 s → 0.4 s …,
  capped at 1.0 s), never unbounded and never a fixed sleep.
* Retcode 10031 CONNECTION is TRANSIENT per :func:`classify_retcode`
  but is DELIBERATELY excluded here: a lost connection means the
  outcome is unknown, and re-sending could double-fill. The callers
  raise ``TRADE_UNVERIFIED`` on 10031 / ``None`` and reconcile against
  live broker state instead — never a blind retry.
* A returned retcode is broker truth that the request was NOT applied
  (except the idempotent cases the callers handle explicitly, e.g.
  10025 NO_CHANGES for SL/TP and 10036 POSITION_CLOSED for close),
  so a retryable retcode is safe to re-send. An UNVERIFIED outcome
  (``None`` / 10031) is never re-sent.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from ..retcodes import classify_retcode

#: Retcodes that are safe to re-send after backoff. TRANSIENT minus
#: 10031 (CONNECTION — outcome unknown, reconcile instead), plus
#: 10024 TOO_MANY_REQUESTS (broker rate limit: back off, then retry).
RETRYABLE_RETCODES: frozenset[int] = frozenset({10004, 10012, 10020, 10021, 10024})


def retryable_retcode(retcode: int | None) -> bool:
    """True when a broker retcode may be re-sent after backoff.

    ``None`` (no result object) is never retryable — the outcome is
    unverified. 10031 is transient per :func:`classify_retcode` but
    is excluded on purpose (see the module docstring).
    """
    if retcode is None:
        return False
    return int(retcode) in RETRYABLE_RETCODES


@dataclass(frozen=True)
class ExecutionRetryPolicy:
    """Bounded exponential backoff for one execution attempt loop.

    ``max_attempts`` counts SEND attempts (3 = one send + up to two
    retries). ``sleep`` is injectable so tests can record delays
    without waiting; production passes the default ``time.sleep``.
    """

    max_attempts: int = 3
    base_delay_s: float = 0.10
    max_delay_s: float = 1.0
    multiplier: float = 2.0
    sleep: Callable[[float], None] = time.sleep

    def delay_for(self, attempt: int) -> float:
        """Backoff before attempt ``attempt`` (1-based: the delay paid
        after the first failed send is ``delay_for(1)``)."""
        delay = self.base_delay_s * (self.multiplier ** max(0, attempt - 1))
        return min(delay, self.max_delay_s)

    def backoff(self, attempt: int) -> float:
        """Sleep the bounded backoff after failed attempt ``attempt``;
        returns the delay slept (0.0 when no retry remains)."""
        if attempt >= self.max_attempts:
            return 0.0
        delay = self.delay_for(attempt)
        self.sleep(delay)
        return delay


def retcode_class_label(retcode: int | None) -> str:
    """Cause class for metrics/status payloads (``classify_retcode``
    verbatim — TRANSIENT / REPAIRABLE / HARD_REJECT / IDEMPOTENT)."""
    return classify_retcode(retcode)
