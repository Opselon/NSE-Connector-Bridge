"""Unit tests for log/error redaction (Phase 1 — security).

A log record containing a fake token must come out without it.
Label: unit.
"""

from __future__ import annotations

import pytest
from loguru import logger

from nse_bridge.logging_setup import (
    REDACTED,
    clear_registered_secrets,
    make_redaction_filter,
    redact_text,
    register_secret,
    sanitize_payload,
)

FAKE_TOKEN = "tok-fake-1234567890abcdef"
FAKE_KEY = "key-fake-abcdef1234567890"


@pytest.fixture(autouse=True)
def _clean_secrets():
    clear_registered_secrets()
    yield
    clear_registered_secrets()


def test_redact_bearer_token():
    out = redact_text(f"connecting with Authorization: Bearer {FAKE_TOKEN} now")
    assert FAKE_TOKEN not in out
    assert "Bearer [REDACTED]" in out


def test_redact_api_key_header_and_assignment():
    out = redact_text(f"X-API-Key: {FAKE_KEY}")
    assert FAKE_KEY not in out
    assert REDACTED in out
    out = redact_text(f'{{"api_key": "{FAKE_KEY}"}}')
    assert FAKE_KEY not in out
    out = redact_text(f"server_token={FAKE_TOKEN}")
    assert FAKE_TOKEN not in out


def test_redact_password_in_json_and_query():
    out = redact_text(f'{{"password": "{FAKE_TOKEN}"}}')
    assert FAKE_TOKEN not in out
    out = redact_text(f"login failed: password={FAKE_TOKEN}")
    assert FAKE_TOKEN not in out


def test_redact_leaves_plain_prose_alone():
    # "password is required" has no = or : separator — it is prose,
    # not a credential, and must survive redaction.
    assert redact_text("password is required to log in") == "password is required to log in"
    assert redact_text("no secrets here") == "no secrets here"


def test_redact_registered_secret_value_verbatim():
    register_secret(FAKE_TOKEN)
    out = redact_text(f"unexpected error carrying {FAKE_TOKEN} in the middle")
    assert FAKE_TOKEN not in out
    assert REDACTED in out


def test_redact_short_values_are_not_registered():
    register_secret("short")
    assert redact_text("short") == "short"


def test_sanitize_payload_scrubs_secret_keys_recursively():
    payload = {
        "server_token": FAKE_TOKEN,
        "nested": {"local_api_key": FAKE_KEY, "symbol": "EURUSD"},
        "items": [{"password": FAKE_TOKEN}, {"ok": True}],
        "message": f"Bearer {FAKE_TOKEN} failed",
    }
    clean = sanitize_payload(payload)
    assert clean["server_token"] == REDACTED
    assert clean["nested"]["local_api_key"] == REDACTED
    assert clean["nested"]["symbol"] == "EURUSD"  # non-secret data survives
    assert clean["items"][0]["password"] == REDACTED
    assert clean["items"][1]["ok"] is True
    assert FAKE_TOKEN not in str(clean)
    assert FAKE_KEY not in str(clean)


def test_loguru_redaction_filter_scrubs_sink_output():
    captured: list[str] = []
    handler_id = logger.add(
        captured.append, filter=make_redaction_filter(), format="{message}"
    )
    try:
        logger.info("auth failed for token={}", FAKE_TOKEN)
        logger.info("Bearer {} rejected", FAKE_TOKEN)
        logger.info("plain line, nothing secret")
    finally:
        logger.remove(handler_id)
    assert captured, "expected log records to be captured"
    assert all(FAKE_TOKEN not in line for line in captured)
    assert any(REDACTED in line for line in captured)
    assert any("plain line, nothing secret" in line for line in captured)
