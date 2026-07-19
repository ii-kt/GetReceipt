from __future__ import annotations

import hmac
import re
import time
from dataclasses import dataclass, field
from threading import Condition, RLock
from typing import Callable


_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")


class ChallengeInboxError(RuntimeError):
    code = "CHALLENGE_INBOX_ERROR"


class ChallengeNotPendingError(ChallengeInboxError):
    code = "CHALLENGE_NOT_PENDING"


class ChallengeAlreadyAnsweredError(ChallengeInboxError):
    code = "CHALLENGE_ALREADY_ANSWERED"


class ChallengeExpiredError(ChallengeInboxError):
    code = "CHALLENGE_EXPIRED"


class ChallengeOwnerMismatchError(ChallengeInboxError):
    code = "CHALLENGE_OWNER_MISMATCH"


@dataclass
class _PendingResponse:
    challenge_id: str
    owner_id: str
    expires_monotonic: float
    condition: Condition = field(default_factory=Condition, repr=False)
    _response: bytearray | None = field(default=None, repr=False)
    _answered: bool = field(default=False, repr=False)
    _closed: bool = field(default=False, repr=False)

    def clear_response(self) -> None:
        if self._response is not None:
            for index in range(len(self._response)):
                self._response[index] = 0
        self._response = None


class ChallengeResponseInbox:
    """Process-local, single-consumer storage for sensitive challenge input.

    Plaintext responses never enter SQLite or job history. Losing the worker
    process intentionally loses the response and the associated live browser;
    recovery must issue a fresh challenge.
    """

    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._lock = RLock()
        self._pending: dict[str, _PendingResponse] = {}

    def register(self, *, challenge_id: str, owner_id: str, ttl_seconds: float) -> None:
        safe_challenge = self._safe_identifier(challenge_id, "challenge_id")
        safe_owner = str(owner_id or "").strip()
        if not safe_owner or len(safe_owner) > 200:
            raise ValueError("owner_idの形式が不正です。")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        pending = _PendingResponse(
            challenge_id=safe_challenge,
            owner_id=safe_owner,
            expires_monotonic=self._monotonic() + float(ttl_seconds),
        )
        with self._lock:
            current = self._pending.get(safe_challenge)
            if current is not None and not current._closed:
                raise ChallengeNotPendingError("追加認証は既に待機中です。")
            self._pending[safe_challenge] = pending

    def submit(self, *, challenge_id: str, owner_id: str, response: str) -> None:
        pending = self._get(challenge_id)
        safe_owner = str(owner_id or "")
        if not hmac.compare_digest(pending.owner_id.encode(), safe_owner.encode()):
            raise ChallengeOwnerMismatchError("この追加認証を操作する権限がありません。")
        encoded = str(response or "").encode("utf-8")
        if not encoded or len(encoded) > 256:
            raise ValueError("追加認証の入力値が不正です。")
        with pending.condition:
            if pending._closed:
                raise ChallengeNotPendingError("追加認証は既に終了しています。")
            if self._monotonic() >= pending.expires_monotonic:
                pending._closed = True
                pending.condition.notify_all()
                raise ChallengeExpiredError("追加認証の入力期限が切れました。")
            if pending._answered:
                raise ChallengeAlreadyAnsweredError("追加認証は既に回答済みです。")
            pending._response = bytearray(encoded)
            pending._answered = True
            pending.condition.notify_all()

    def wait_and_consume(self, challenge_id: str, *, timeout_seconds: float) -> str | None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        pending = self._get(challenge_id)
        deadline = min(
            pending.expires_monotonic,
            self._monotonic() + float(timeout_seconds),
        )
        with pending.condition:
            while not pending._answered and not pending._closed:
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    break
                pending.condition.wait(timeout=remaining)
            if not pending._answered or pending._response is None:
                if self._monotonic() >= pending.expires_monotonic:
                    pending._closed = True
                return None
            response_bytes = bytes(pending._response)
            pending.clear_response()
            pending._closed = True
        with self._lock:
            self._pending.pop(pending.challenge_id, None)
        return response_bytes.decode("utf-8")

    def discard(self, challenge_id: str) -> bool:
        safe_challenge = self._safe_identifier(challenge_id, "challenge_id")
        with self._lock:
            pending = self._pending.pop(safe_challenge, None)
        if pending is None:
            return False
        with pending.condition:
            pending.clear_response()
            pending._closed = True
            pending.condition.notify_all()
        return True

    def is_pending(self, challenge_id: str) -> bool:
        try:
            pending = self._get(challenge_id)
        except ChallengeNotPendingError:
            return False
        with pending.condition:
            return not pending._closed and self._monotonic() < pending.expires_monotonic

    def _get(self, challenge_id: str) -> _PendingResponse:
        safe_challenge = self._safe_identifier(challenge_id, "challenge_id")
        with self._lock:
            pending = self._pending.get(safe_challenge)
        if pending is None:
            raise ChallengeNotPendingError("待機中の追加認証がありません。")
        return pending

    @staticmethod
    def _safe_identifier(value: str, label: str) -> str:
        normalized = str(value or "").strip()
        if not _SAFE_ID.fullmatch(normalized):
            raise ValueError(f"{label}の形式が不正です。")
        return normalized

