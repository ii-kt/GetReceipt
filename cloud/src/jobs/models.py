from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping
from uuid import UUID, uuid4


_TARGET_MONTH = re.compile(r"^[0-9]{4}-(?:0[1-9]|1[0-2])$")
_SERVICE_ID = re.compile(r"^[a-z0-9_-]{1,64}$")
_FORBIDDEN_PAYLOAD_KEYS = {
    "access_token",
    "answer",
    "auth_header",
    "authorization",
    "challenge_response",
    "cookie",
    "cookies",
    "credential",
    "credentials",
    "id_token",
    "otp",
    "otp_code",
    "passphrase",
    "password",
    "refresh_token",
    "response_value",
    "secret",
    "secret_value",
    "security_code",
    "session_cookie",
    "token",
    "verification_code",
}


class BatchJobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_CHALLENGE = "waiting_for_challenge"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERVENTION_REQUIRED = "intervention_required"
    CANCELLED = "cancelled"


class ChallengeType(str, Enum):
    OTP_SMS = "otp_sms"
    OTP_EMAIL = "otp_email"
    OTP_TOTP = "otp_totp"
    SECURITY_CODE = "security_code"
    PUSH_APPROVAL = "push_approval"
    CAPTCHA_INTERACTIVE = "captcha_interactive"
    CONSENT_INTERACTIVE = "consent_interactive"
    PASSKEY_HYBRID = "passkey_hybrid"
    PASSKEY_PLATFORM = "passkey_platform"
    PASSKEY_UNAVAILABLE = "passkey_unavailable"
    MAGIC_LINK = "magic_link"
    SECURITY_QUESTION = "security_question"
    CONSENT = "consent"
    UNKNOWN = "unknown"
    OTHER = "other"


class ChallengeInputType(str, Enum):
    NONE = "none"
    CODE = "code"
    TEXT = "text"
    ACTION_ONLY = "action_only"
    REMOTE_BROWSER = "remote_browser"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def datetime_to_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _require_utc_datetime(value, "datetime").isoformat()


def datetime_from_text(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _require_utc_datetime(value, "datetime")
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return _require_utc_datetime(parsed, "datetime")


def assert_public_payload(value: Any, *, label: str = "payload") -> None:
    """Reject values that could place credentials in durable metadata or events.

    This deliberately validates key names rather than trying to guess whether
    an arbitrary string happens to be a secret. Callers must use descriptive,
    non-secret fields such as ``error_code`` and ``masked_destination``.
    """

    def inspect(item: Any, path: str) -> None:
        if item is None or isinstance(item, (str, int, float, bool)):
            return
        if isinstance(item, bytes):
            raise ValueError(f"{path} must not contain bytes")
        if isinstance(item, Mapping):
            for raw_key, nested in item.items():
                key = str(raw_key).strip().lower().replace("-", "_")
                if key in _FORBIDDEN_PAYLOAD_KEYS:
                    raise ValueError(f"{path} contains forbidden secret field: {raw_key}")
                inspect(nested, f"{path}.{raw_key}")
            return
        if isinstance(item, (list, tuple)):
            for index, nested in enumerate(item):
                inspect(nested, f"{path}[{index}]")
            return
        raise ValueError(f"{path} contains unsupported value type: {type(item).__name__}")

    inspect(value, label)


def _require_nonempty(value: str, label: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{label} is required")
    return normalized


def _require_utc_datetime(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _uuid(value: UUID | str, label: str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise ValueError(f"{label} must be a UUID") from error


@dataclass(frozen=True)
class ChallengeInputSchema:
    input_type: ChallengeInputType = ChallengeInputType.NONE
    label: str = ""
    required: bool = False
    min_length: int | None = None
    max_length: int | None = None
    pattern: str | None = None
    autocomplete: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_type", ChallengeInputType(self.input_type))
        if self.min_length is not None and self.min_length < 0:
            raise ValueError("min_length must not be negative")
        if self.max_length is not None and self.max_length < 0:
            raise ValueError("max_length must not be negative")
        if (
            self.min_length is not None
            and self.max_length is not None
            and self.min_length > self.max_length
        ):
            raise ValueError("min_length must not exceed max_length")
        if self.pattern is not None:
            re.compile(self.pattern)

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_type": self.input_type.value,
            "label": self.label,
            "required": self.required,
            "min_length": self.min_length,
            "max_length": self.max_length,
            "pattern": self.pattern,
            "autocomplete": self.autocomplete,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ChallengeInputSchema:
        return cls(
            input_type=ChallengeInputType(str(value.get("input_type", "none"))),
            label=str(value.get("label", "")),
            required=bool(value.get("required", False)),
            min_length=(
                int(value["min_length"]) if value.get("min_length") is not None else None
            ),
            max_length=(
                int(value["max_length"]) if value.get("max_length") is not None else None
            ),
            pattern=str(value["pattern"]) if value.get("pattern") is not None else None,
            autocomplete=(
                str(value["autocomplete"]) if value.get("autocomplete") is not None else None
            ),
        )


@dataclass(frozen=True)
class BatchJob:
    id: UUID
    owner: str
    target_month: str
    service_ids: tuple[str, ...]
    state: BatchJobState = BatchJobState.QUEUED
    completed: tuple[str, ...] = ()
    current: str | None = None
    version: int = 0
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    error: dict[str, Any] | None = None
    result: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _uuid(self.id, "id"))
        object.__setattr__(self, "owner", _require_nonempty(self.owner, "owner"))
        object.__setattr__(self, "state", BatchJobState(self.state))
        object.__setattr__(self, "service_ids", tuple(str(item) for item in self.service_ids))
        object.__setattr__(self, "completed", tuple(str(item) for item in self.completed))
        object.__setattr__(self, "created_at", _require_utc_datetime(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", _require_utc_datetime(self.updated_at, "updated_at"))

        if not _TARGET_MONTH.fullmatch(str(self.target_month)):
            raise ValueError("target_month must use YYYY-MM")
        if not self.service_ids:
            raise ValueError("service_ids must not be empty")
        if len(set(self.service_ids)) != len(self.service_ids):
            raise ValueError("service_ids must not contain duplicates")
        for service_id in self.service_ids:
            if not _SERVICE_ID.fullmatch(service_id):
                raise ValueError(f"invalid service_id: {service_id}")
        if len(set(self.completed)) != len(self.completed):
            raise ValueError("completed must not contain duplicates")
        if not set(self.completed).issubset(self.service_ids):
            raise ValueError("completed must be a subset of service_ids")
        if self.current is not None and self.current not in self.service_ids:
            raise ValueError("current must be one of service_ids")
        if self.version < 0:
            raise ValueError("version must not be negative")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        if self.error is not None:
            assert_public_payload(self.error, label="error")
        if self.result is not None:
            assert_public_payload(self.result, label="result")

    @classmethod
    def new(
        cls,
        *,
        owner: str,
        target_month: str,
        service_ids: tuple[str, ...] | list[str],
        now: datetime | None = None,
        job_id: UUID | None = None,
    ) -> BatchJob:
        timestamp = _require_utc_datetime(now or utc_now(), "now")
        return cls(
            id=job_id or uuid4(),
            owner=owner,
            target_month=target_month,
            service_ids=tuple(service_ids),
            created_at=timestamp,
            updated_at=timestamp,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "owner": self.owner,
            "target_month": self.target_month,
            "service_ids": list(self.service_ids),
            "state": self.state.value,
            "completed": list(self.completed),
            "current": self.current,
            "version": self.version,
            "created_at": datetime_to_text(self.created_at),
            "updated_at": datetime_to_text(self.updated_at),
            "error": self.error,
            "result": self.result,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> BatchJob:
        return cls(
            id=_uuid(value["id"], "id"),
            owner=str(value["owner"]),
            target_month=str(value["target_month"]),
            service_ids=tuple(str(item) for item in value["service_ids"]),
            state=BatchJobState(str(value.get("state", BatchJobState.QUEUED.value))),
            completed=tuple(str(item) for item in value.get("completed", ())),
            current=str(value["current"]) if value.get("current") is not None else None,
            version=int(value.get("version", 0)),
            created_at=datetime_from_text(value["created_at"]) or utc_now(),
            updated_at=datetime_from_text(value["updated_at"]) or utc_now(),
            error=dict(value["error"]) if value.get("error") is not None else None,
            result=dict(value["result"]) if value.get("result") is not None else None,
        )


@dataclass(frozen=True)
class Challenge:
    id: UUID
    job_id: UUID
    type: ChallengeType
    message: str
    input_schema: ChallengeInputSchema = field(default_factory=ChallengeInputSchema)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _uuid(self.id, "id"))
        object.__setattr__(self, "job_id", _uuid(self.job_id, "job_id"))
        object.__setattr__(self, "type", ChallengeType(self.type))
        object.__setattr__(self, "message", _require_nonempty(self.message, "message"))
        if isinstance(self.input_schema, Mapping):
            object.__setattr__(
                self,
                "input_schema",
                ChallengeInputSchema.from_dict(self.input_schema),
            )
        object.__setattr__(self, "created_at", _require_utc_datetime(self.created_at, "created_at"))
        if self.expires_at is not None:
            object.__setattr__(
                self,
                "expires_at",
                _require_utc_datetime(self.expires_at, "expires_at"),
            )
            if self.expires_at <= self.created_at:
                raise ValueError("expires_at must follow created_at")
        assert_public_payload(self.metadata, label="challenge.metadata")

    @classmethod
    def new(
        cls,
        *,
        job_id: UUID,
        type: ChallengeType,
        message: str,
        input_schema: ChallengeInputSchema | None = None,
        metadata: Mapping[str, Any] | None = None,
        created_at: datetime | None = None,
        expires_at: datetime | None = None,
        challenge_id: UUID | None = None,
    ) -> Challenge:
        return cls(
            id=challenge_id or uuid4(),
            job_id=job_id,
            type=type,
            message=message,
            input_schema=input_schema or ChallengeInputSchema(),
            metadata=dict(metadata or {}),
            created_at=created_at or utc_now(),
            expires_at=expires_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "job_id": str(self.job_id),
            "type": self.type.value,
            "message": self.message,
            "input_schema": self.input_schema.to_dict(),
            "metadata": self.metadata,
            "created_at": datetime_to_text(self.created_at),
            "expires_at": datetime_to_text(self.expires_at),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Challenge:
        return cls(
            id=_uuid(value["id"], "id"),
            job_id=_uuid(value["job_id"], "job_id"),
            type=ChallengeType(str(value["type"])),
            message=str(value["message"]),
            input_schema=ChallengeInputSchema.from_dict(value.get("input_schema", {})),
            metadata=dict(value.get("metadata", {})),
            created_at=datetime_from_text(value["created_at"]) or utc_now(),
            expires_at=datetime_from_text(value.get("expires_at")),
        )


@dataclass(frozen=True)
class JobEvent:
    id: int
    job_id: UUID
    event_type: str
    payload: dict[str, Any]
    created_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "job_id", _uuid(self.job_id, "job_id"))
        object.__setattr__(self, "event_type", _require_nonempty(self.event_type, "event_type"))
        object.__setattr__(self, "created_at", _require_utc_datetime(self.created_at, "created_at"))
        assert_public_payload(self.payload, label="event.payload")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "job_id": str(self.job_id),
            "event_type": self.event_type,
            "payload": self.payload,
            "created_at": datetime_to_text(self.created_at),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> JobEvent:
        return cls(
            id=int(value["id"]),
            job_id=_uuid(value["job_id"], "job_id"),
            event_type=str(value["event_type"]),
            payload=dict(value.get("payload", {})),
            created_at=datetime_from_text(value["created_at"]) or utc_now(),
        )
