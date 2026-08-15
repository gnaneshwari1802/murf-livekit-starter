"""Production-oriented LiveKit SIP outbound calling helpers.

This module deliberately contains no provider credentials.  It is usable from the
root ``run.py`` command and is separate from the long-running agent worker.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from dotenv import load_dotenv

logger = logging.getLogger("outbound_calling")

SIP_URI_PATTERN = re.compile(r"^sip:[^@\s]+@[^@\s;:]+(?::\d{1,5})?$", re.I)
TRUNK_ID_PATTERN = re.compile(r"^ST_[A-Za-z0-9]+$")


class CallOutcome(str, Enum):
    INITIATED = "initiated"
    RINGING = "ringing"
    ANSWERED = "answered"
    COMPLETED = "completed"
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    REJECTED = "rejected"
    FAILED = "failed"
    TIMEOUT = "timeout"
    USER_HANGUP = "user_hangup"
    AGENT_ERROR = "agent_error"
    OPTED_OUT = "opted_out"


class CallState(str, Enum):
    PRE_CALL_CHECK = "PRE_CALL_CHECK"
    DIALING = "DIALING"
    RINGING = "RINGING"
    CONNECTED = "CONNECTED"
    OPENING = "OPENING"
    CONSENT = "CONSENT"
    INTERACTION = "INTERACTION"
    CLOSING = "CLOSING"
    OPTED_OUT = "OPTED_OUT"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


_TRANSITIONS = {
    CallState.PRE_CALL_CHECK: {CallState.DIALING, CallState.FAILED},
    CallState.DIALING: {CallState.RINGING, CallState.CONNECTED, CallState.FAILED},
    CallState.RINGING: {CallState.CONNECTED, CallState.FAILED},
    CallState.CONNECTED: {CallState.OPENING, CallState.CLOSING, CallState.FAILED},
    CallState.OPENING: {CallState.CONSENT, CallState.OPTED_OUT, CallState.FAILED},
    CallState.CONSENT: {CallState.INTERACTION, CallState.CLOSING, CallState.OPTED_OUT},
    CallState.INTERACTION: {CallState.CLOSING, CallState.OPTED_OUT, CallState.FAILED},
    CallState.CLOSING: {CallState.COMPLETED, CallState.FAILED},
    CallState.OPTED_OUT: {CallState.COMPLETED},
    CallState.COMPLETED: set(),
    CallState.FAILED: set(),
}


class OutboundCallError(Exception):
    """A safe, user-actionable outbound calling error."""


class ConfigurationError(OutboundCallError):
    """Required outbound configuration is missing or invalid."""


class DuplicateCallError(OutboundCallError):
    """An idempotency key was already used."""


@dataclass(frozen=True)
class OutboundConfig:
    livekit_url: str
    livekit_api_key: str
    livekit_api_secret: str
    trunk_id: str
    destination: str
    agent_name: str
    answer_timeout_seconds: int = 60
    max_call_duration_seconds: int = 300
    max_retries: int = 1
    retry_delay_seconds: int = 30

    @classmethod
    def from_env(cls, destination: str | None = None) -> OutboundConfig:
        load_dotenv(Path(__file__).parents[1] / ".env.local")
        value = os.getenv
        return cls(
            livekit_url=value("LIVEKIT_URL", "").strip(),
            livekit_api_key=value("LIVEKIT_API_KEY", "").strip(),
            livekit_api_secret=value("LIVEKIT_API_SECRET", "").strip(),
            trunk_id=value("LIVEKIT_SIP_OUTBOUND_TRUNK_ID", "").strip(),
            destination=(destination or value("OUTBOUND_SIP_URI", "")).strip(),
            agent_name=value("AGENT_NAME", "my-agent").strip(),
            answer_timeout_seconds=_positive_int(
                value("OUTBOUND_CALL_TIMEOUT_SECONDS"), 60
            ),
            max_call_duration_seconds=_positive_int(
                value("MAX_CALL_DURATION_SECONDS"), 300
            ),
            max_retries=_nonnegative_int(value("OUTBOUND_MAX_RETRIES"), 1),
            retry_delay_seconds=_positive_int(
                value("OUTBOUND_RETRY_DELAY_SECONDS"), 30
            ),
        )

    def validate(self) -> list[str]:
        missing = [
            name
            for name, value in (
                ("LIVEKIT_URL", self.livekit_url),
                ("LIVEKIT_API_KEY", self.livekit_api_key),
                ("LIVEKIT_API_SECRET", self.livekit_api_secret),
                ("LIVEKIT_SIP_OUTBOUND_TRUNK_ID", self.trunk_id),
                ("OUTBOUND_SIP_URI", self.destination),
                ("AGENT_NAME", self.agent_name),
            )
            if not value
        ]
        errors = [f"Missing required variable: {name}" for name in missing]
        if self.livekit_url and urlparse(self.livekit_url).scheme not in {"ws", "wss"}:
            errors.append("LIVEKIT_URL must begin with ws:// or wss://.")
        if self.trunk_id and not TRUNK_ID_PATTERN.fullmatch(self.trunk_id):
            errors.append("LIVEKIT_SIP_OUTBOUND_TRUNK_ID must be a LiveKit trunk ID.")
        if self.destination and not is_valid_sip_uri(self.destination):
            errors.append(
                "OUTBOUND_SIP_URI must be a SIP URI such as sip:user@example.org."
            )
        if self.answer_timeout_seconds > self.max_call_duration_seconds:
            errors.append(
                "OUTBOUND_CALL_TIMEOUT_SECONDS cannot exceed MAX_CALL_DURATION_SECONDS."
            )
        return errors


def _positive_int(value: str | None, default: int) -> int:
    try:
        return max(1, int(value or default))
    except ValueError:
        return default


def _nonnegative_int(value: str | None, default: int) -> int:
    try:
        return max(0, int(value or default))
    except ValueError:
        return default


def is_valid_sip_uri(value: object) -> bool:
    return isinstance(value, str) and bool(SIP_URI_PATTERN.fullmatch(value.strip()))


def mask_destination(destination: str) -> str:
    """Mask a destination enough for useful operational correlation."""
    if "@" not in destination:
        return "***"
    scheme_user, domain = destination.split("@", 1)
    user = scheme_user[4:] if scheme_user.lower().startswith("sip:") else scheme_user
    return f"sip:{user[:2]}***@{domain}"


def detect_opt_out(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text.lower().replace("\u2019", "'"))
    phrases = (
        "stop",
        "don't call me",
        "do not call again",
        "remove me",
        "unsubscribe",
        "not interested",
        "wrong number",
        "do not call me",
    )
    return any(phrase in normalized for phrase in phrases)


def map_sip_status(status: str | None) -> CallOutcome:
    value = (status or "").lower().replace("_", " ").replace("-", " ")
    if value in {"active", "answered", "connected", "in progress"}:
        return CallOutcome.ANSWERED
    if "ring" in value or "dial" in value or value in {"trying", "initiated"}:
        return CallOutcome.RINGING
    if "busy" in value:
        return CallOutcome.BUSY
    if any(word in value for word in ("reject", "declin", "forbidden")):
        return CallOutcome.REJECTED
    if any(word in value for word in ("no answer", "timeout", "unavailable")):
        return CallOutcome.NO_ANSWER
    if any(word in value for word in ("hangup", "hang up", "disconnect")):
        return CallOutcome.USER_HANGUP
    return CallOutcome.FAILED


def should_retry(outcome: CallOutcome, retry_count: int, max_retries: int) -> bool:
    return outcome == CallOutcome.NO_ANSWER and retry_count < max_retries


class SuppressionStore:
    """Durable SQLite suppression list keyed by a SIP destination."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(
            path or Path(__file__).parents[1] / "data" / "outbound_calls.db"
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS suppressions (destination TEXT PRIMARY KEY, created_at TEXT NOT NULL, reason TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS idempotency (key TEXT PRIMARY KEY, call_id TEXT NOT NULL, created_at TEXT NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def is_suppressed(self, destination: str) -> bool:
        with self._connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM suppressions WHERE destination = ?", (destination,)
                ).fetchone()
                is not None
            )

    def suppress(self, destination: str, reason: str = "caller_opt_out") -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO suppressions VALUES (?, ?, ?)",
                (destination, datetime.now(UTC).isoformat(), reason),
            )

    def reserve(self, key: str, call_id: str, force: bool = False) -> bool:
        with self._connect() as db:
            if force:
                db.execute(
                    "INSERT OR REPLACE INTO idempotency VALUES (?, ?, ?)",
                    (key, call_id, datetime.now(UTC).isoformat()),
                )
                return True
            try:
                db.execute(
                    "INSERT INTO idempotency VALUES (?, ?, ?)",
                    (key, call_id, datetime.now(UTC).isoformat()),
                )
                return True
            except sqlite3.IntegrityError:
                return False


@dataclass
class CallResult:
    call_id: str
    room_name: str
    outcome: CallOutcome
    state: CallState
    destination: str
    retry_count: int = 0
    duration_seconds: float = 0.0
    opt_out: bool = False
    error_code: str | None = None
    message: str = ""


class LiveKitClient(Protocol):
    room: Any
    agent_dispatch: Any
    sip: Any

    async def aclose(self) -> None: ...


class OutboundCallService:
    def __init__(
        self, config: OutboundConfig, store: SuppressionStore | None = None
    ) -> None:
        self.config = config
        self.store = store or SuppressionStore()

    def dry_run(self) -> dict[str, object]:
        self._validate()
        return {
            "destination": mask_destination(self.config.destination),
            "trunk_id": self.config.trunk_id,
            "agent_name": self.config.agent_name,
            "answer_timeout_seconds": self.config.answer_timeout_seconds,
            "max_call_duration_seconds": self.config.max_call_duration_seconds,
            "max_retries": self.config.max_retries,
            "retry_delay_seconds": self.config.retry_delay_seconds,
        }

    async def place_call(
        self, *, idempotency_key: str, force: bool = False
    ) -> CallResult:
        self._validate()
        call_id = str(uuid.uuid4())
        room_name = f"outbound-practice-{call_id}"
        if self.store.is_suppressed(self.config.destination):
            return self._result(
                call_id,
                room_name,
                CallOutcome.OPTED_OUT,
                CallState.OPTED_OUT,
                "Destination has opted out of automated calls.",
                opt_out=True,
            )
        if not self.store.reserve(idempotency_key, call_id, force):
            return self._result(
                call_id,
                room_name,
                CallOutcome.FAILED,
                CallState.FAILED,
                "Duplicate call request blocked by idempotency key.",
                error_code="duplicate_request",
            )

        from livekit import api

        client = api.LiveKitAPI(
            self.config.livekit_url,
            self.config.livekit_api_key,
            self.config.livekit_api_secret,
        )
        try:
            retry_count = 0
            while True:
                attempt_room = (
                    room_name if retry_count == 0 else f"{room_name}-r{retry_count}"
                )
                result = await self._attempt(
                    client, call_id, attempt_room, idempotency_key
                )
                result.retry_count = retry_count
                if not should_retry(
                    result.outcome, retry_count, self.config.max_retries
                ):
                    return result
                retry_count += 1
                logger.warning(
                    "outbound_call_retry",
                    extra={"call_id": call_id, "retry_count": retry_count},
                )
                await asyncio.sleep(self.config.retry_delay_seconds)
        finally:
            await client.aclose()

    async def _attempt(
        self, client: LiveKitClient, call_id: str, room_name: str, idempotency_key: str
    ) -> CallResult:
        from google.protobuf.duration_pb2 import Duration
        from livekit import api

        started = asyncio.get_running_loop().time()
        state = CallState.PRE_CALL_CHECK
        try:
            await client.room.create_room(
                api.CreateRoomRequest(
                    name=room_name, empty_timeout=60, departure_timeout=30
                )
            )
            await client.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    room=room_name,
                    agent_name=self.config.agent_name,
                    metadata=json.dumps(
                        {
                            "outbound": True,
                            "call_id": call_id,
                            "destination": self.config.destination,
                            "idempotency_key": idempotency_key,
                        }
                    ),
                )
            )
            state = self._transition(state, CallState.DIALING)
            logger.info(
                "outbound_call",
                extra={
                    "event": "dialing",
                    "call_id": call_id,
                    "room_name": room_name,
                    "destination": mask_destination(self.config.destination),
                    "agent_name": self.config.agent_name,
                },
            )
            request = api.CreateSIPParticipantRequest(
                room_name=room_name,
                sip_trunk_id=self.config.trunk_id,
                sip_call_to=self.config.destination,
                participant_identity=call_id,
                participant_name="VoiceForBharat learner",
                wait_until_answered=False,
            )
            request.ringing_timeout.CopyFrom(
                Duration(seconds=self.config.answer_timeout_seconds)
            )
            request.max_call_duration.CopyFrom(
                Duration(seconds=self.config.max_call_duration_seconds)
            )
            await client.sip.create_sip_participant(request)
            result = await self._monitor(client, call_id, room_name, state, started)
            return result
        except Exception as exc:
            logger.error(
                "outbound_call_failed",
                extra={
                    "call_id": call_id,
                    "room_name": room_name,
                    "destination": mask_destination(self.config.destination),
                    "error_code": type(exc).__name__,
                },
            )
            return self._result(
                call_id,
                room_name,
                CallOutcome.FAILED,
                CallState.FAILED,
                "LiveKit could not create or connect the outbound call.",
                error_code=type(exc).__name__,
                duration=asyncio.get_running_loop().time() - started,
            )

    async def _monitor(
        self,
        client: LiveKitClient,
        call_id: str,
        room_name: str,
        state: CallState,
        started: float,
    ) -> CallResult:
        from livekit import api

        deadline = started + self.config.max_call_duration_seconds
        answered = False
        while asyncio.get_running_loop().time() < deadline:
            response = await client.room.list_participants(
                api.ListParticipantsRequest(room=room_name)
            )
            participant = next(
                (item for item in response.participants if item.identity == call_id),
                None,
            )
            if participant is None:
                if answered:
                    if self.store.is_suppressed(self.config.destination):
                        return self._result(
                            call_id,
                            room_name,
                            CallOutcome.OPTED_OUT,
                            CallState.OPTED_OUT,
                            "The caller opted out of future automated calls.",
                            duration=asyncio.get_running_loop().time() - started,
                            opt_out=True,
                        )
                    return self._result(
                        call_id,
                        room_name,
                        CallOutcome.COMPLETED,
                        CallState.COMPLETED,
                        "Call ended.",
                        duration=asyncio.get_running_loop().time() - started,
                    )
                if (
                    asyncio.get_running_loop().time()
                    >= started + self.config.answer_timeout_seconds
                ):
                    return self._result(
                        call_id,
                        room_name,
                        CallOutcome.NO_ANSWER,
                        CallState.FAILED,
                        "The SIP endpoint did not answer before the configured timeout.",
                        duration=asyncio.get_running_loop().time() - started,
                    )
                await asyncio.sleep(1)
                continue
            outcome = map_sip_status(participant.attributes.get("sip.callStatus"))
            if outcome == CallOutcome.RINGING:
                state = self._transition(state, CallState.RINGING)
            elif outcome == CallOutcome.ANSWERED:
                answered = True
                if state != CallState.CONNECTED:
                    state = self._transition(state, CallState.CONNECTED)
                logger.info(
                    "outbound_call",
                    extra={
                        "event": "answered",
                        "call_id": call_id,
                        "room_name": room_name,
                    },
                )
            elif outcome in {
                CallOutcome.BUSY,
                CallOutcome.REJECTED,
                CallOutcome.NO_ANSWER,
                CallOutcome.FAILED,
            }:
                return self._result(
                    call_id,
                    room_name,
                    outcome,
                    CallState.FAILED,
                    "The SIP endpoint did not connect.",
                    duration=asyncio.get_running_loop().time() - started,
                )
            await asyncio.sleep(1)
        outcome = CallOutcome.TIMEOUT if not answered else CallOutcome.COMPLETED
        return self._result(
            call_id,
            room_name,
            outcome,
            CallState.FAILED if not answered else CallState.COMPLETED,
            "Call timed out." if not answered else "Maximum call duration reached.",
            duration=asyncio.get_running_loop().time() - started,
        )

    def _validate(self) -> None:
        errors = self.config.validate()
        if errors:
            raise ConfigurationError(" ".join(errors))

    @staticmethod
    def _transition(current: CallState, target: CallState) -> CallState:
        if target not in _TRANSITIONS[current]:
            raise OutboundCallError(
                f"Invalid call state transition: {current} -> {target}"
            )
        return target

    def _result(
        self,
        call_id: str,
        room_name: str,
        outcome: CallOutcome,
        state: CallState,
        message: str,
        *,
        error_code: str | None = None,
        duration: float = 0.0,
        opt_out: bool = False,
    ) -> CallResult:
        result = CallResult(
            call_id,
            room_name,
            outcome,
            state,
            mask_destination(self.config.destination),
            duration_seconds=round(duration, 2),
            opt_out=opt_out,
            error_code=error_code,
            message=message,
        )
        logger.info("outbound_call_result %s", json.dumps(asdict(result), default=str))
        return result
