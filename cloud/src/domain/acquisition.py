from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Stage(str, Enum):
    """UI-independent stages for one automatic acquisition attempt."""

    CHECKING_DRIVE = "checking_drive"
    FETCHING = "fetching"
    AWAITING_SECURITY_CODE = "awaiting_security_code"
    AWAITING_USER_ACTION = "awaiting_user_action"
    EXTRACTING = "extracting"
    SAVING = "saving"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"


class AcquisitionOutcome(str, Enum):
    ACQUIRED = "acquired"
    ALREADY_EXISTS = "already_exists"
    ACTION_REQUIRED = "action_required"
    FAILED = "failed"


@dataclass(frozen=True)
class ProgressEvent:
    stage: Stage
    message: str


@dataclass(frozen=True)
class StoredReceiptReference:
    file_id: str
    file_name: str
    web_view_link: str = ""


@dataclass(frozen=True)
class AcquisitionFailure:
    code: str
    message: str
    stage: Stage
    detail: str = ""


@dataclass(frozen=True)
class SecurityChallenge:
    kind: str
    message: str


@dataclass(frozen=True)
class AcquisitionResult:
    service_id: str
    target_month: str
    outcome: AcquisitionOutcome
    events: tuple[ProgressEvent, ...] = field(default_factory=tuple)
    receipt: StoredReceiptReference | None = None
    failure: AcquisitionFailure | None = None
    challenge: SecurityChallenge | None = None

    @property
    def success(self) -> bool:
        return self.outcome in {
            AcquisitionOutcome.ACQUIRED,
            AcquisitionOutcome.ALREADY_EXISTS,
        }

    @property
    def action_required(self) -> bool:
        return self.outcome is AcquisitionOutcome.ACTION_REQUIRED

    @property
    def skipped(self) -> bool:
        return self.outcome is AcquisitionOutcome.ALREADY_EXISTS

    @property
    def stage(self) -> Stage:
        if self.events:
            return self.events[-1].stage
        return Stage.FAILED if self.failure is not None else Stage.COMPLETED

    @property
    def status(self) -> str:
        return self.outcome.value

    @property
    def error_code(self) -> str:
        return self.failure.code if self.failure is not None else ""

    @property
    def message(self) -> str:
        if self.events:
            return self.events[-1].message
        return self.failure.message if self.failure is not None else ""

    @property
    def file_name(self) -> str:
        return self.receipt.file_name if self.receipt is not None else ""

    @property
    def drive_web_view_link(self) -> str:
        return self.receipt.web_view_link if self.receipt is not None else ""
