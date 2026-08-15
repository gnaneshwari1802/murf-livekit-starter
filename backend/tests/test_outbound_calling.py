from __future__ import annotations

import pytest

from outbound_calling import (
    CallOutcome,
    CallState,
    ConfigurationError,
    OutboundCallService,
    OutboundConfig,
    detect_opt_out,
    is_valid_sip_uri,
    map_sip_status,
    should_retry,
)


class FakeStore:
    def __init__(self) -> None:
        self.keys: set[str] = set()
        self.suppressed: set[str] = set()

    def reserve(self, key: str, call_id: str, force: bool = False) -> bool:
        if key in self.keys and not force:
            return False
        self.keys.add(key)
        return True

    def is_suppressed(self, destination: str) -> bool:
        return destination in self.suppressed


def config(**overrides: object) -> OutboundConfig:
    values: dict[str, object] = {
        "livekit_url": "wss://project.livekit.cloud",
        "livekit_api_key": "key",
        "livekit_api_secret": "secret",
        "trunk_id": "ST_Example123",
        "destination": "sip:learner@sip.linphone.org",
        "agent_name": "my-agent",
    }
    values.update(overrides)
    return OutboundConfig(**values)  # type: ignore[arg-type]


def test_configuration_validation_is_safe_and_complete() -> None:
    errors = config(livekit_api_secret="", destination="not-a-uri").validate()
    assert "Missing required variable: LIVEKIT_API_SECRET" in errors
    assert any("OUTBOUND_SIP_URI" in error for error in errors)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("sip:learner@sip.linphone.org", True),
        ("sip:bad value@example.org", False),
        ("+919876543210", False),
    ],
)
def test_sip_destination_validation(value: str, expected: bool) -> None:
    assert is_valid_sip_uri(value) is expected


def test_call_state_transitions_reject_invalid_changes() -> None:
    assert (
        OutboundCallService._transition(CallState.PRE_CALL_CHECK, CallState.DIALING)
        == CallState.DIALING
    )
    with pytest.raises(Exception, match="Invalid call state transition"):
        OutboundCallService._transition(CallState.PRE_CALL_CHECK, CallState.COMPLETED)


@pytest.mark.parametrize(
    "phrase",
    ["stop", "Please don't call me", "remove me", "unsubscribe", "wrong number"],
)
def test_opt_out_detection(phrase: str) -> None:
    assert detect_opt_out(phrase)


def test_retry_policy_and_sip_outcome_mapping() -> None:
    assert map_sip_status("active") == CallOutcome.ANSWERED
    assert map_sip_status("busy") == CallOutcome.BUSY
    assert map_sip_status("no-answer") == CallOutcome.NO_ANSWER
    assert should_retry(CallOutcome.NO_ANSWER, 0, 1)
    assert not should_retry(CallOutcome.REJECTED, 0, 1)
    assert not should_retry(CallOutcome.NO_ANSWER, 1, 1)


def test_idempotency_and_suppression_are_checked_before_calls() -> None:
    store = FakeStore()
    assert store.reserve("same-request", "call-1")
    assert not store.reserve("same-request", "call-2")
    store.suppressed.add("sip:learner@sip.linphone.org")
    assert store.is_suppressed("sip:learner@sip.linphone.org")


def test_dry_run_validates_without_livekit_calls() -> None:
    service = OutboundCallService(config(), FakeStore())  # type: ignore[arg-type]
    plan = service.dry_run()
    assert plan["destination"] == "sip:le***@sip.linphone.org"
    assert plan["trunk_id"] == "ST_Example123"
    assert plan["agent_name"] == "my-agent"


def test_dry_run_reports_configuration_failure() -> None:
    service = OutboundCallService(
        config(trunk_id="invalid"),
        FakeStore(),  # type: ignore[arg-type]
    )
    with pytest.raises(ConfigurationError, match="trunk ID"):
        service.dry_run()


@pytest.mark.asyncio
async def test_suppressed_call_fails_before_network_access() -> None:
    store = FakeStore()
    store.suppressed.add("sip:learner@sip.linphone.org")
    result = await OutboundCallService(config(), store).place_call(
        idempotency_key="suppressed"
    )
    assert result.outcome == CallOutcome.OPTED_OUT
    assert result.opt_out is True
