"""CONTRACT v1 REST client (contract §3).

Base URL: ``https://bridge.nexusverse.space`` (production default from
``settings.server_base_url``; dev/staging/prod via configuration only
— no hardcoded dev endpoints). ``http://`` is accepted only in a
dev/test environment (see :func:`nse_bridge.config.validate_server_base_url`).

Device calls carry ``Authorization: Bearer <api_key>`` +
``X-NSE-Device-Id: <device_id>`` on every call (§3 preamble). The API
key comes from the secret store (Phase 1 — ``settings.server_token``,
runtime-only, never config.json, never logged).

Endpoints implemented (§3):

- ``validate-key`` (§3.1) — ``{device_id, connector_version}`` →
  ``valid``, server-resolved ``user_id`` (never client-asserted),
  ``subscription``, ``entitlements``. On 401/403: stop sync
  immediately, do NOT retry with the same key, do NOT fall back to
  unauthenticated mode.
- ``register-device`` (§3.2) — idempotent per ``(user_id, device_id)``;
  called on first run AND after any 401 on another endpoint
  (re-enrolment is the recovery path; the key is re-prompted only if
  ``register-device`` itself returns ``key_invalid``/``key_revoked``).
  Beyond ``max_devices`` → 403, never evict another device silently.
- ``sync/batch`` (§3.3) — the ONLY sync channel; flat wire shape.
- ``heartbeat`` (§3.4) — POST ``{}`` → ``{ok, server_time,
  pending_commands}``; keep-alive when WS is down, NOT a substitute
  for the WS heartbeat.
- ``session/refresh`` + ``session/revoke`` (§3.4, [client-v1.1]) —
  refresh ``{}`` → ``{refreshed, session_expires_at}``; revoke ``{}``
  → ``{revoked: true}`` (device session only; the API key is NOT
  revoked).
- ``releases/latest`` (§3.5) — signed artifact metadata for the
  updater (Phase 5); this client only READS it, never downloads or
  installs here.
- ``manifest`` (§4.1, [client-v1.1]) — POST the action manifest; the
  server answers ``{accepted, manifest_hash}``.

Error contract (§3.6): see :mod:`nse_bridge.server.errors`. Retry
policy: 429 and 5xx are retryable with backoff (bounded attempts);
401/403 are never retried with the same key.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Self

import httpx
from loguru import logger

from .. import BRIDGE_VERSION
from ..config import validate_server_base_url
from .errors import ServerError, ServerErrorCode, parse_error_response

if TYPE_CHECKING:
    from ..config import BridgeSettings

__all__ = [
    "Entitlements",
    "HeartbeatResult",
    "RegisterDeviceResult",
    "ReleaseInfo",
    "RestClient",
    "SyncAck",
    "ValidateKeyResult",
]

PATH_VALIDATE_KEY = "/api/v1/connector/validate-key"
PATH_REGISTER_DEVICE = "/api/v1/connector/register-device"
PATH_SYNC_BATCH = "/api/v1/connector/sync/batch"
PATH_HEARTBEAT = "/api/v1/connector/heartbeat"
PATH_SESSION_REFRESH = "/api/v1/connector/session/refresh"
PATH_SESSION_REVOKE = "/api/v1/connector/session/revoke"
PATH_RELEASES_LATEST = "/api/v1/connector/releases/latest"
PATH_MANIFEST = "/api/v1/connector/manifest"

#: Endpoints that must never trigger the re-enrolment retry (§3.2:
#: re-enrolment cannot fix key_invalid/key_revoked, and validate-key
#: itself is the auth check — retrying it with the same key is banned).
_NO_REAUTH_PATHS = frozenset({PATH_VALIDATE_KEY, PATH_REGISTER_DEVICE})


@dataclass
class Entitlements:
    """Server-resolved entitlements (§3.1, §2.4 secure-default).

    Every field is optional in the wire response; absent means
    disabled/unknown, so the secure default here is False everywhere.
    """

    history_sync: bool = False
    ai_coach: bool = False
    position_manager: bool = False
    auto_trade: bool = False
    max_devices: int = 0
    max_accounts: int = 0

    @classmethod
    def from_dict(cls, raw: Any) -> Entitlements:
        raw = raw if isinstance(raw, dict) else {}
        return cls(
            history_sync=bool(raw.get("history_sync", False)),
            ai_coach=bool(raw.get("ai_coach", False)),
            position_manager=bool(raw.get("position_manager", False)),
            auto_trade=bool(raw.get("auto_trade", False)),
            max_devices=int(raw.get("max_devices") or 0),
            max_accounts=int(raw.get("max_accounts") or 0),
        )


@dataclass
class ValidateKeyResult:
    valid: bool
    user_id: str | None = None  # server-resolved, never client-asserted
    subscription: dict[str, Any] = field(default_factory=dict)
    entitlements: Entitlements = field(default_factory=Entitlements)


@dataclass
class RegisterDeviceResult:
    registered: bool
    session_expires_at: str | None = None


@dataclass
class SyncAck:
    accepted: bool
    batch_id: str | None = None


@dataclass
class HeartbeatResult:
    ok: bool = True
    server_time: str | None = None
    pending_commands: int = 0


@dataclass
class ReleaseInfo:
    """Signed artifact metadata (§3.5). Read-only here; the updater
    (Phase 5) verifies SHA-256 + Ed25519 signature before any install."""

    version: str | None = None
    download_url: str | None = None
    sha256: str | None = None
    size_bytes: int | None = None
    min_connector_version: str | None = None
    signature: str | None = None  # base64 Ed25519 over the artifact bytes, or null
    released_at: str | None = None


class RestClient:
    """Async CONTRACT v1 REST client.

    ``sleep`` is injectable (default :func:`asyncio.sleep`) so tests
    can observe backoff without waiting.
    """

    def __init__(
        self,
        settings: BridgeSettings,
        device_id: str,
        *,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = 3,
        retry_base_s: float = 0.5,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        enrolment: Any | None = None,
    ) -> None:
        validate_server_base_url(settings.server_base_url, environment=settings.environment)
        self.settings = settings
        self.device_id = device_id
        self.api_key = api_key if api_key is not None else settings.server_token
        self.base_url = settings.server_base_url.rstrip("/")
        self.max_retries = max_retries
        self.retry_base_s = retry_base_s
        self._sleep = sleep or asyncio.sleep
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        # Enrolment state machine (nse_bridge.server.enrolment), wired
        # at app startup. When set, a 401 re-enrolment notifies it so
        # the state (REGISTERED / FAILED_AUTH / FAILED_TRANSIENT) stays
        # truthful; the re-enrolment itself still happens here (§3.2).
        # Duck-typed (note_reauth_*) to avoid a server↔rest import cycle.
        self.enrolment = enrolment
        # Server-resolved state (populated by validate_key / register_device).
        self.user_id: str | None = None
        self.entitlements = Entitlements()
        self.subscription: dict[str, Any] = {}
        self.session_expires_at: str | None = None

    # ------------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        # Never log these headers: they carry the API key.
        return {
            "Authorization": f"Bearer {self.api_key}",
            "X-NSE-Device-Id": self.device_id,
        }

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            # trust_env=False: the client carries API-key-bearing traffic
            # to a fixed server and must never be routed through an
            # ambient env proxy. (Also: some hosts export a NO_PROXY
            # with bare IPv6 literals that httpx cannot parse at
            # construction time — the client must not fail to construct
            # because of the ambient environment.) Proxy support, if
            # ever needed, must come from explicit Bridge config.
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                transport=self._transport,
                timeout=httpx.Timeout(30.0),
                trust_env=False,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ------------------------------------------------------------------
    @staticmethod
    def _retry_after(headers: httpx.Headers) -> float | None:
        raw = headers.get("Retry-After")
        if raw is None:
            return None
        try:
            return max(0.0, float(raw))
        except ValueError:
            return None

    @staticmethod
    def _payload(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return None

    async def _request(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None,
        *,
        allow_reauth: bool = True,
        _reattempted: bool = False,
    ) -> Any:
        """One request with the contract retry/reauth policy (§3.6, §3.2).

        - 429 / 5xx / transport errors: bounded retries with backoff.
        - 401 on a non-auth endpoint: re-enrol via register-device ONCE,
          then retry once. If register-device itself 401s, the error
          propagates (the user must issue a new key).
        - 401/403 otherwise: raised immediately, never retried.
        """
        client = self._get_client()
        last_error: ServerError | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = await client.request(
                    method, path, json=json_body, headers=self._headers()
                )
            except httpx.TransportError as exc:
                last_error = ServerError(
                    ServerErrorCode.TRANSPORT_ERROR,
                    "transport error reaching the server",
                    retryable=True,
                )
                logger.warning("[REST] transport error on {} (attempt {}): {}", path, attempt, type(exc).__name__)
                if attempt < self.max_retries:
                    await self._sleep(self.retry_base_s * (2**attempt))
                    continue
                raise last_error from exc
            if response.status_code == 401 and allow_reauth and path not in _NO_REAUTH_PATHS and not _reattempted:
                logger.warning(
                    "[REST] 401 on {} — re-enrolling this device (recovery path §3.2)",
                    path,
                )
                if self.enrolment is not None:
                    self.enrolment.note_reauth_start()
                    try:
                        await self.register_device()  # raises if the key itself is bad
                    except ServerError as exc:
                        self.enrolment.note_reauth_failed(exc)
                        raise
                    self.enrolment.note_reauth_succeeded()
                else:
                    await self.register_device()  # raises if the key itself is bad
                return await self._request(method, path, json_body, allow_reauth=False, _reattempted=True)
            if response.status_code in (429,) or response.status_code >= 500:
                err = parse_error_response(
                    response.status_code,
                    self._payload(response),
                    retry_after_s=self._retry_after(response.headers),
                )
                last_error = err
                if attempt < self.max_retries:
                    delay = err.retry_after_s if err.retry_after_s is not None else self.retry_base_s * (2**attempt)
                    logger.warning(
                        "[REST] {} on {} (attempt {}) — backing off {:.1f}s",
                        response.status_code, path, attempt, delay,
                    )
                    await self._sleep(delay)
                    continue
                raise err
            if response.status_code >= 400:
                raise parse_error_response(response.status_code, self._payload(response))
            return self._payload(response)
        # Unreachable in practice (loop always raises or returns), but
        # fail closed rather than returning a success-shaped value.
        raise last_error or ServerError(
            ServerErrorCode.TRANSPORT_ERROR, "request failed without a response", retryable=True
        )

    # ------------------------------------------------------------------
    async def validate_key(self) -> ValidateKeyResult:
        """§3.1 — device auth + entitlements.

        Never retried on 401/403, never falls back to unauthenticated
        mode. The returned ``user_id`` is server-resolved.
        """
        body = {"device_id": self.device_id, "connector_version": BRIDGE_VERSION}
        payload = await self._request("POST", PATH_VALIDATE_KEY, body, allow_reauth=False)
        if not isinstance(payload, dict):
            raise ServerError(ServerErrorCode.MALFORMED_RESPONSE, "validate-key returned a non-object response")
        result = ValidateKeyResult(
            valid=bool(payload.get("valid", False)),
            user_id=payload.get("user_id") if isinstance(payload.get("user_id"), str) else None,
            subscription=payload.get("subscription") if isinstance(payload.get("subscription"), dict) else {},
            entitlements=Entitlements.from_dict(payload.get("entitlements")),
        )
        if not result.valid:
            raise ServerError(
                ServerErrorCode.KEY_INVALID, "server reported the API key as invalid", http_status=200
            )
        self.user_id = result.user_id
        self.entitlements = result.entitlements
        self.subscription = result.subscription
        return result

    async def register_device(self) -> RegisterDeviceResult:
        """§3.2 — enrolment / re-registration (idempotent per user+device)."""
        body = {"device_id": self.device_id, "connector_version": BRIDGE_VERSION}
        payload = await self._request("POST", PATH_REGISTER_DEVICE, body, allow_reauth=False)
        if not isinstance(payload, dict):
            raise ServerError(ServerErrorCode.MALFORMED_RESPONSE, "register-device returned a non-object response")
        result = RegisterDeviceResult(
            registered=bool(payload.get("registered", False)),
            session_expires_at=payload.get("session_expires_at")
            if isinstance(payload.get("session_expires_at"), str)
            else None,
        )
        if not result.registered:
            raise ServerError(
                ServerErrorCode.MALFORMED_RESPONSE, "register-device answered registered=false without an error"
            )
        self.session_expires_at = result.session_expires_at
        return result

    async def send_batch(self, batch: dict[str, Any]) -> SyncAck:
        """§3.3 — send one sync batch; the ONLY sync channel.

        The REST 200 ack is authoritative (§5.2: the WS ``ack`` frame
        is informational only). ``accepted: false`` MUST carry an error
        object — a missing one is malformed.
        """
        payload = await self._request("POST", PATH_SYNC_BATCH, batch)
        if not isinstance(payload, dict):
            raise ServerError(ServerErrorCode.MALFORMED_RESPONSE, "sync/batch returned a non-object response")
        accepted = bool(payload.get("accepted", False))
        if not accepted:
            error = payload.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            raise ServerError(
                ServerErrorCode.MALFORMED_RESPONSE
                if not isinstance(code, str)
                else _code_or_malformed(code),
                f"batch {batch.get('batch_id')} rejected by the server",
            )
        return SyncAck(accepted=True, batch_id=payload.get("batch_id"))

    async def heartbeat(self) -> HeartbeatResult:
        """§3.4 — REST heartbeat (keep-alive when WS is down).

        POST ``{}`` → ``{ok, server_time, pending_commands}``. If
        ``pending_commands > 0`` and no WS is live, the caller should
        open/refresh the WS. This is NOT a WS heartbeat substitute.
        """
        payload = await self._request("POST", PATH_HEARTBEAT, {})
        if not isinstance(payload, dict):
            raise ServerError(ServerErrorCode.MALFORMED_RESPONSE, "heartbeat returned a non-object response")
        return HeartbeatResult(
            ok=bool(payload.get("ok", True)),
            server_time=payload.get("server_time") if isinstance(payload.get("server_time"), str) else None,
            pending_commands=int(payload.get("pending_commands") or 0),
        )

    async def session_refresh(self) -> RegisterDeviceResult:
        """§3.4 [client-v1.1] — refresh the device session (no re-enrol)."""
        payload = await self._request("POST", PATH_SESSION_REFRESH, {})
        if not isinstance(payload, dict) or not payload.get("refreshed"):
            raise ServerError(ServerErrorCode.MALFORMED_RESPONSE, "session/refresh did not confirm a refresh")
        expires = payload.get("session_expires_at") if isinstance(payload.get("session_expires_at"), str) else None
        self.session_expires_at = expires
        return RegisterDeviceResult(registered=True, session_expires_at=expires)

    async def session_revoke(self) -> bool:
        """§3.4 [client-v1.1] — revoke THIS device's session only.

        The API key itself is NOT revoked — only this device's session.
        """
        payload = await self._request("POST", PATH_SESSION_REVOKE, {})
        if not isinstance(payload, dict) or not payload.get("revoked"):
            raise ServerError(ServerErrorCode.MALFORMED_RESPONSE, "session/revoke did not confirm revocation")
        self.session_expires_at = None
        return True

    async def releases_latest(self) -> ReleaseInfo:
        """§3.5 — signed artifact metadata for the updater (read-only).

        The download/install + SHA-256/Ed25519 verification belong to
        the updater (Phase 5); nothing is downloaded here.
        """
        payload = await self._request("GET", PATH_RELEASES_LATEST, None)
        if not isinstance(payload, dict):
            raise ServerError(ServerErrorCode.MALFORMED_RESPONSE, "releases/latest returned a non-object response")
        return ReleaseInfo(
            version=payload.get("version") if isinstance(payload.get("version"), str) else None,
            download_url=payload.get("download_url") if isinstance(payload.get("download_url"), str) else None,
            sha256=payload.get("sha256") if isinstance(payload.get("sha256"), str) else None,
            size_bytes=int(payload["size_bytes"]) if isinstance(payload.get("size_bytes"), int) else None,
            min_connector_version=payload.get("min_connector_version")
            if isinstance(payload.get("min_connector_version"), str)
            else None,
            signature=payload.get("signature") if isinstance(payload.get("signature"), str) else None,
            released_at=payload.get("released_at") if isinstance(payload.get("released_at"), str) else None,
        )

    async def send_manifest(self, manifest: dict[str, Any]) -> dict[str, Any]:
        """§4.1 [client-v1.1] — POST the action manifest.

        Returns the server ack ``{accepted, manifest_hash}``.
        Re-posting an unchanged hash is a no-op ack server-side.
        """
        payload = await self._request("POST", PATH_MANIFEST, manifest)
        if not isinstance(payload, dict) or not payload.get("accepted"):
            raise ServerError(ServerErrorCode.MALFORMED_RESPONSE, "manifest was not accepted by the server")
        return payload

    # ------------------------------------------------------------------
    @property
    def session_expires_dt(self) -> datetime | None:
        if not self.session_expires_at:
            return None
        try:
            dt = datetime.fromisoformat(self.session_expires_at)
        except ValueError:
            return None
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _code_or_malformed(code: str) -> ServerErrorCode:
    try:
        return ServerErrorCode(code)
    except ValueError:
        return ServerErrorCode.MALFORMED_RESPONSE
