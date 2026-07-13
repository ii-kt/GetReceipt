from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Stage(str, Enum):
    """UI-independent stages for one automatic acquisition attempt."""

    CHECKING_DRIVE = "checking_drive"
    FETCHING = "fetching"
    EXTRACTING = "extracting"
    SAVING = "saving"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"


class AcquisitionOutcome(str, Enum):
    ACQUIRED = "acquired"
    ALREADY_EXISTS = "already_exists"
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


@dataclass(frozen=True)
class AcquisitionResult:
    service_id: str
    target_month: str
    outcome: AcquisitionOutcome
    events: tuple[ProgressEvent, ...] = field(default_factory=tuple)
    receipt: StoredReceiptReference | None = None
    failure: AcquisitionFailure | None = None

    @property
    def success(self) -> bool:
        return self.outcome is not AcquisitionOutcome.FAILED

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
