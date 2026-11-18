"""Server error contract (CONTRACT v1 §3.6).

Wire envelope: ``{error: {code, message}}``. The client parses
``error.code`` at ``/error/code`` (falling back to a top-level
``code``). An unknown code on a 401/403 is a MALFORMED RESPONSE,
never a success.

Mapping (contract §3.6 table):

- 401 ``key_invalid`` / ``key_revoked`` — no retry, ever.
- 403 ``subscription_expired`` / ``device_limit_exceeded`` /
  ``account_not_authorized`` / ``feature_not_enabled`` — mapped
  distinctly; no retry with the same key.
- 429 ``rate_limited`` — back off; do not tight-loop.
- 5xx (any code) — retryable with backoff.

Error messages never contain key material, secrets, SQL, or stack
traces — this module never includes request bodies or headers in a
message, and callers must not log :class:`ServerError` with a body.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

__all__ = ["ServerError", "ServerErrorCode", "parse_error_response"]


class ServerErrorCode(str, Enum):
    KEY_INVALID = "key_invalid"
    KEY_REVOKED = "key_revoked"
    SUBSCRIPTION_EXPIRED = "subscription_expired"
    DEVICE_LIMIT_EXCEEDED = "device_limit_exceeded"
    ACCOUNT_NOT_AUTHORIZED = "account_not_authorized"
    FEATURE_NOT_ENABLED = "feature_not_enabled"
    RATE_LIMITED = "rate_limited"
    SERVER_UNAVAILABLE = "server_unavailable"
    MALFORMED_RESPONSE = "malformed_response"
    TRANSPORT_ERROR = "transport_error"


#: 401/403 codes the contract defines. Anything else on a 401/403 is
#: malformed (§3.6: "An unknown code on a 401/403 is a malformed
#: response, never a success").
_KNOWN_AUTH_CODES = {
    ServerErrorCode.KEY_INVALID,
    ServerErrorCode.KEY_REVOKED,
    ServerErrorCode.SUBSCRIPTION_EXPIRED,
    ServerErrorCode.DEVICE_LIMIT_EXCEEDED,
    ServerErrorCode.ACCOUNT_NOT_AUTHORIZED,
    ServerErrorCode.FEATURE_NOT_ENABLED,
}

#: Codes after which the client MUST stop and MUST NOT retry with the
#: same key (§3.1/§3.6). Re-enrolment (register-device) is the only
#: recovery path for a 401 on a non-auth endpoint, and it cannot fix
#: key_invalid/key_revoked.
FATAL_AUTH_CODES = frozenset(
    {
        ServerErrorCode.KEY_INVALID,
        ServerErrorCode.KEY_REVOKED,
        ServerErrorCode.SUBSCRIPTION_EXPIRED,
        ServerErrorCode.DEVICE_LIMIT_EXCEEDED,
        ServerErrorCode.ACCOUNT_NOT_AUTHORIZED,
        ServerErrorCode.FEATURE_NOT_ENABLED,
    }
)


class ServerError(Exception):
    """A contract §3.6 error from the Platform server."""

    def __init__(
        self,
        code: ServerErrorCode,
        message: str,
        *,
        http_status: int | None = None,
        retryable: bool = False,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.retryable = retryable
        self.retry_after_s = retry_after_s

    @property
    def is_fatal_auth(self) -> bool:
        """True when the client must stop and never retry with this key."""
        return self.code in FATAL_AUTH_CODES

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"ServerError(code={self.code.value!r}, http_status={self.http_status}, retryable={self.retryable})"


def _extract_code(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict) and isinstance(error.get("code"), str):
        return error["code"]
    if isinstance(payload.get("code"), str):  # top-level fallback (§3.6)
        return payload["code"]
    return None


def _extract_message(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"]
    if isinstance(payload.get("message"), str):
        return payload["message"]
    return None


def parse_error_response(http_status: int, payload: Any, *, retry_after_s: float | None = None) -> ServerError:
    """Build the :class:`ServerError` for an HTTP error response (§3.6)."""
    raw_code = _extract_code(payload)
    message = _extract_message(payload) or f"server returned HTTP {http_status}"

    if http_status == 429:
        return ServerError(
            ServerErrorCode.RATE_LIMITED,
            message,
            http_status=429,
            retryable=True,
            retry_after_s=retry_after_s,
        )
    if http_status >= 500:
        return ServerError(
            ServerErrorCode.SERVER_UNAVAILABLE,
            message,
            http_status=http_status,
            retryable=True,
        )
    if http_status in (401, 403):
        if raw_code is None:
            return ServerError(
                ServerErrorCode.MALFORMED_RESPONSE,
                f"malformed error response (HTTP {http_status} with no error code)",
                http_status=http_status,
            )
        try:
            code = ServerErrorCode(raw_code)
        except ValueError:
            return ServerError(
                ServerErrorCode.MALFORMED_RESPONSE,
                f"malformed error response (unknown {http_status} code {raw_code!r})",
                http_status=http_status,
            )
        if code not in _KNOWN_AUTH_CODES:
            return ServerError(
                ServerErrorCode.MALFORMED_RESPONSE,
                f"malformed error response (code {raw_code!r} is not a {http_status} code)",
                http_status=http_status,
            )
        return ServerError(code, message, http_status=http_status)
    # Any other 4xx: not a defined contract code — malformed, never success.
    return ServerError(
        ServerErrorCode.MALFORMED_RESPONSE,
        f"malformed error response (unexpected HTTP {http_status})",
        http_status=http_status,
    )
