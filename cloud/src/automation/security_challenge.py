from __future__ import annotations

import re
import secrets
import shutil
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from threading import RLock, Timer
from typing import Callable, Iterator
from urllib.parse import urlsplit

from ..config import DATA_DIR
from .browser_session import ManagedBrowser


COMMUFA_HOST = "mypage.commufa.jp"
DEFAULT_LEASE_TTL_SECONDS = 10 * 60
_RUNTIME_ROOT = DATA_DIR / "acquisition-runtime"
_SAFE_SERVICE_ID = re.compile(r"^[a-z0-9_-]{1,64}$")
_SAFE_TARGET_MONTH = re.compile(r"^[0-9]{4}-(?:0[1-9]|1[0-2])$")
_SIX_DIGIT_CODE = re.compile(r"^[0-9]{6}$")


class ChallengeKind(str, Enum):
    VERIFICATION_CODE = "verification_code"
    CAPTCHA = "captcha"
    INTERACTIVE = "interactive"
    CONSENT = "consent"
    PUSH_APPROVAL = "push_approval"
    PASSKEY_UNAVAILABLE = "passkey_unavailable"
    OTHER = "other"

    def __str__(self) -> str:
        return self.value


class SecurityChallengeError(RuntimeError):
    """Base error for safe, resumable security-challenge operations."""


class SecurityCodeValidationError(SecurityChallengeError):
    """Raised without echoing a rejected one-time code."""

    challenge_kind = ChallengeKind.VERIFICATION_CODE.value


class SecurityChallengeSubmissionError(SecurityChallengeError):
    """Raised when the current page is not safe for one-time-code submission."""

    def __init__(
        self,
        message: str,
        *,
        challenge_kind: ChallengeKind | str = ChallengeKind.OTHER,
    ) -> None:
        super().__init__(message)
        self.challenge_kind = normalize_challenge_kind(challenge_kind)


class BrowserLeaseUnavailableError(SecurityChallengeError):
    """Raised when a browser lease is missing, expired, or mismatched."""


class BrowserAttemptUnavailableError(BrowserLeaseUnavailableError):
    """Raised when another process-local acquisition already owns the slot."""


@dataclass(frozen=True)
class SecurityChallengeObservation:
    kind: ChallengeKind
    code_rejected: bool = False


@dataclass(frozen=True)
class BrowserLeaseMetadata:
    service_id: str
    target_month: str
    expires_at: datetime


@dataclass(frozen=True)
class BrowserLeaseTicket:
    token: str = field(repr=False)
    service_id: str
    target_month: str
    expires_at: datetime


@dataclass(frozen=True)
class BrowserAttemptTicket:
    token: str = field(repr=False)
    service_id: str
    target_month: str
    expires_at: datetime


@dataclass
class BrowserAttemptClaim:
    service_id: str
    target_month: str
    expires_at: datetime
    _expires_monotonic: float = field(repr=False)
    _timer: Timer | None = field(default=None, repr=False)


@dataclass
class BrowserLease:
    service_id: str
    target_month: str
    browser: ManagedBrowser = field(repr=False)
    run_dir: Path
    expires_at: datetime
    _expires_monotonic: float = field(repr=False)
    _operation_lock: RLock = field(default_factory=RLock, repr=False)
    _timer: Timer | None = field(default=None, repr=False)
    _closed: bool = field(default=False, repr=False)

    def metadata(self) -> BrowserLeaseMetadata:
        return BrowserLeaseMetadata(
            service_id=self.service_id,
            target_month=self.target_month,
            expires_at=self.expires_at,
        )


TimerFactory = Callable[..., Timer]


def normalize_challenge_kind(value: ChallengeKind | str | None) -> str:
    if isinstance(value, ChallengeKind):
        return value.value
    normalized = str(value or "").strip().lower()
    if normalized in {item.value for item in ChallengeKind}:
        return normalized
    return ChallengeKind.OTHER.value if normalized else ""


def new_attempt_run_dir(
    service_id: str,
    target_month: str,
    *,
    runtime_root: Path = _RUNTIME_ROOT,
) -> Path:
    _validate_lease_identity(service_id, target_month)
    root = runtime_root.resolve()
    return root / f"{service_id}-{target_month}-{secrets.token_hex(16)}"


class BrowserLeaseRegistry:
    """Own live browser attempts between Streamlit reruns for at most ten minutes.

    Only the opaque ticket token belongs in ``st.session_state``. The browser,
    profile and temporary files remain process-local and are deleted when the
    lease is consumed, discarded, or expires.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS,
        runtime_root: Path = _RUNTIME_ROOT,
        timer_factory: TimerFactory = Timer,
        monotonic: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ブラウザ保持時間は正の値である必要があります。")
        self.ttl_seconds = float(ttl_seconds)
        self.runtime_root = runtime_root.resolve()
        self._timer_factory = timer_factory
        self._monotonic = monotonic
        self._utcnow = utcnow
        self._lock = RLock()
        # Lifecycle operations use a second lock so an expiring browser is
        # fully closed before another caller can claim the single process-local
        # acquisition slot. Checkout deliberately does not take this lock: an
        # expiry waits for the checked-out operation to finish instead.
        self._slot_lock = RLock()
        self._claims: dict[str, BrowserAttemptClaim] = {}
        self._leases: dict[str, BrowserLease] = {}
        self._owned_run_dirs: set[Path] = set()

    def claim_attempt(self, *, service_id: str, target_month: str) -> BrowserAttemptTicket:
        """Atomically reserve the sole acquisition slot before login starts.

        A short-lived claim prevents another Streamlit session from opening a
        second browser or issuing a replacement one-time code for the same
        personal account. Callers must either promote the returned token when
        an OTP page is reached or release it on every terminal path.
        """

        _validate_lease_identity(service_id, target_month)
        with self._slot_lock:
            self._release_expired_attempts_under_slot()
            with self._lock:
                if self._claims or self._leases:
                    raise BrowserAttemptUnavailableError(
                        "別の自動取得または確認コード待機が進行中です。"
                    )
                token = self._new_token_locked()
                now_monotonic = self._monotonic()
                now_utc = self._normalized_utcnow()
                expires_at = now_utc + timedelta(seconds=self.ttl_seconds)
                claim = BrowserAttemptClaim(
                    service_id=service_id,
                    target_month=target_month,
                    expires_at=expires_at,
                    _expires_monotonic=now_monotonic + self.ttl_seconds,
                )
                timer = self._timer_factory(
                    self.ttl_seconds,
                    self._expire_claim,
                    args=(token, claim),
                )
                timer.daemon = True
                claim._timer = timer
                self._claims[token] = claim
                try:
                    timer.start()
                except Exception:
                    self._claims.pop(token, None)
                    claim._timer = None
                    raise

        return BrowserAttemptTicket(
            token=token,
            service_id=service_id,
            target_month=target_month,
            expires_at=expires_at,
        )

    def promote_claim_to_lease(
        self,
        token: str,
        *,
        browser: ManagedBrowser,
        run_dir: Path,
    ) -> BrowserLeaseTicket:
        """Promote a pre-login claim to a resumable browser lease.

        The opaque token remains unchanged, while the ten-minute lease window
        is restarted from promotion. A generation-aware timer prevents a late
        callback from the cancelled claim timer from closing the new lease.
        """

        safe_token = str(token or "")
        with self._slot_lock:
            claim = self._active_claim_under_slot(safe_token)
            resolved_run_dir = self._validate_lease_runtime(
                service_id=claim.service_id,
                target_month=claim.target_month,
                browser=browser,
                run_dir=run_dir,
            )
            if self._is_expired(claim):
                self._release_attempt_under_slot(safe_token)
                raise BrowserLeaseUnavailableError("自動取得の開始権限の保持期限が切れました。")

            now_monotonic = self._monotonic()
            now_utc = self._normalized_utcnow()
            expires_at = now_utc + timedelta(seconds=self.ttl_seconds)
            lease = BrowserLease(
                service_id=claim.service_id,
                target_month=claim.target_month,
                browser=browser,
                run_dir=resolved_run_dir,
                expires_at=expires_at,
                _expires_monotonic=now_monotonic + self.ttl_seconds,
            )
            timer = self._timer_factory(
                self.ttl_seconds,
                self._expire_lease,
                args=(safe_token, lease),
            )
            timer.daemon = True
            lease._timer = timer

            with self._lock:
                if self._claims.get(safe_token) is not claim:
                    raise BrowserLeaseUnavailableError("自動取得の開始権限を確認できませんでした。")
                if resolved_run_dir in self._owned_run_dirs:
                    raise ValueError("同じ一時ディレクトリを複数のブラウザ保持に使用できません。")
                if claim._timer is not None:
                    claim._timer.cancel()
                    claim._timer = None
                self._claims.pop(safe_token, None)
                self._leases[safe_token] = lease
                self._owned_run_dirs.add(resolved_run_dir)
                try:
                    timer.start()
                except Exception:
                    self._leases.pop(safe_token, None)
                    self._owned_run_dirs.discard(resolved_run_dir)
                    lease._timer = None
                    self._close_lease(lease)
                    raise

        return BrowserLeaseTicket(
            token=safe_token,
            service_id=lease.service_id,
            target_month=lease.target_month,
            expires_at=expires_at,
        )

    def create(
        self,
        *,
        service_id: str,
        target_month: str,
        browser: ManagedBrowser,
        run_dir: Path,
    ) -> BrowserLeaseTicket:
        """Create a lease directly while preserving the legacy public API."""

        _validate_lease_identity(service_id, target_month)
        with self._slot_lock:
            self._release_expired_attempts_under_slot()
            resolved_run_dir = self._validate_lease_runtime(
                service_id=service_id,
                target_month=target_month,
                browser=browser,
                run_dir=run_dir,
            )
            now_monotonic = self._monotonic()
            now_utc = self._normalized_utcnow()
            expires_at = now_utc + timedelta(seconds=self.ttl_seconds)
            lease = BrowserLease(
                service_id=service_id,
                target_month=target_month,
                browser=browser,
                run_dir=resolved_run_dir,
                expires_at=expires_at,
                _expires_monotonic=now_monotonic + self.ttl_seconds,
            )

            with self._lock:
                if resolved_run_dir in self._owned_run_dirs:
                    raise ValueError("同じ一時ディレクトリを複数のブラウザ保持に使用できません。")
                if self._claims or self._leases:
                    raise BrowserAttemptUnavailableError(
                        "別の自動取得または確認コード待機が進行中です。"
                    )
                token = self._new_token_locked()
                timer = self._timer_factory(
                    self.ttl_seconds,
                    self._expire_lease,
                    args=(token, lease),
                )
                timer.daemon = True
                lease._timer = timer
                self._leases[token] = lease
                self._owned_run_dirs.add(resolved_run_dir)
                try:
                    timer.start()
                except Exception:
                    self._leases.pop(token, None)
                    self._owned_run_dirs.discard(resolved_run_dir)
                    lease._timer = None
                    raise

        return BrowserLeaseTicket(
            token=token,
            service_id=service_id,
            target_month=target_month,
            expires_at=expires_at,
        )

    def metadata(self, token: str) -> BrowserLeaseMetadata:
        lease = self._active_lease(token)
        return lease.metadata()

    @contextmanager
    def checkout(
        self,
        token: str,
        *,
        expected_service_id: str | None = None,
        expected_target_month: str | None = None,
    ) -> Iterator[BrowserLease]:
        lease = self._active_lease(token)
        lease._operation_lock.acquire()
        try:
            with self._lock:
                active = self._leases.get(token) is lease and not lease._closed
            if not active or self._is_expired(lease):
                if active:
                    self.discard(token)
                raise BrowserLeaseUnavailableError("追加認証用ブラウザの保持期限が切れました。")
            if expected_service_id is not None and lease.service_id != expected_service_id:
                raise BrowserLeaseUnavailableError("追加認証用ブラウザのサービスが一致しません。")
            if expected_target_month is not None and lease.target_month != expected_target_month:
                raise BrowserLeaseUnavailableError("追加認証用ブラウザの対象月が一致しません。")
            lease.browser.current_page_target()
            yield lease
        finally:
            lease._operation_lock.release()

    def discard(self, token: str) -> bool:
        """Discard either a pending claim or its promoted browser lease."""

        return self.release_attempt(token)

    def release_attempt(self, token: str) -> bool:
        """Release the single acquisition slot on every terminal path."""

        with self._slot_lock:
            return self._release_attempt_under_slot(str(token or ""))

    def close_all(self) -> None:
        with self._slot_lock:
            with self._lock:
                tokens = tuple(dict.fromkeys((*self._claims, *self._leases)))
            for token in tokens:
                self._release_attempt_under_slot(token)

    def __len__(self) -> int:
        with self._lock:
            return len(self._claims) + len(self._leases)

    def _active_lease(self, token: str) -> BrowserLease:
        safe_token = str(token or "")
        with self._lock:
            lease = self._leases.get(safe_token)
        if lease is None:
            raise BrowserLeaseUnavailableError("追加認証用ブラウザを確認できませんでした。")
        if self._is_expired(lease):
            self.discard(safe_token)
            raise BrowserLeaseUnavailableError("追加認証用ブラウザの保持期限が切れました。")
        return lease

    def _is_expired(self, lease: BrowserLease) -> bool:
        return self._monotonic() >= lease._expires_monotonic

    def _new_token_locked(self) -> str:
        while True:
            token = secrets.token_urlsafe(32)
            if token not in self._claims and token not in self._leases:
                return token

    def _active_claim_under_slot(self, token: str) -> BrowserAttemptClaim:
        with self._lock:
            claim = self._claims.get(token)
        if claim is None:
            raise BrowserLeaseUnavailableError("自動取得の開始権限を確認できませんでした。")
        if self._is_expired(claim):
            self._release_attempt_under_slot(token)
            raise BrowserLeaseUnavailableError("自動取得の開始権限の保持期限が切れました。")
        return claim

    def _release_expired_attempts_under_slot(self) -> None:
        with self._lock:
            expired_tokens = [
                token
                for token, attempt in (*self._claims.items(), *self._leases.items())
                if self._is_expired(attempt)
            ]
        for token in expired_tokens:
            self._release_attempt_under_slot(token)

    def _release_attempt_under_slot(self, token: str) -> bool:
        with self._lock:
            claim = self._claims.pop(token, None)
            lease = self._leases.pop(token, None)
        if claim is None and lease is None:
            return False
        if claim is not None and claim._timer is not None:
            claim._timer.cancel()
            claim._timer = None
        if lease is not None:
            with lease._operation_lock:
                self._close_lease(lease)
            with self._lock:
                self._owned_run_dirs.discard(lease.run_dir)
        return True

    def _expire_claim(self, token: str, expected: BrowserAttemptClaim) -> None:
        with self._slot_lock:
            with self._lock:
                current = self._claims.get(token)
            if current is expected:
                self._release_attempt_under_slot(token)

    def _expire_lease(self, token: str, expected: BrowserLease) -> None:
        with self._slot_lock:
            with self._lock:
                current = self._leases.get(token)
            if current is expected:
                self._release_attempt_under_slot(token)

    def _normalized_utcnow(self) -> datetime:
        now_utc = self._utcnow()
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)
        return now_utc.astimezone(timezone.utc)

    def _validate_lease_runtime(
        self,
        *,
        service_id: str,
        target_month: str,
        browser: ManagedBrowser,
        run_dir: Path,
    ) -> Path:
        resolved_run_dir = Path(run_dir).resolve()
        if resolved_run_dir == self.runtime_root or self.runtime_root not in resolved_run_dir.parents:
            raise ValueError("ブラウザ保持対象の一時ディレクトリが不正です。")
        expected_name = re.compile(
            rf"^{re.escape(service_id)}-{re.escape(target_month)}-[0-9a-f]{{32}}$"
        )
        if not expected_name.fullmatch(resolved_run_dir.name):
            raise ValueError("ブラウザ保持対象は試行ごとに固有の一時ディレクトリが必要です。")
        profile_dir = Path(browser.profile_dir).resolve()
        download_dir = Path(browser.download_dir).resolve()
        if resolved_run_dir not in profile_dir.parents or resolved_run_dir not in download_dir.parents:
            raise ValueError("ブラウザのプロファイルまたはダウンロード先が一時ディレクトリ外です。")
        if browser.process is None or browser.process.poll() is not None:
            raise BrowserLeaseUnavailableError("追加認証用ブラウザは既に終了しています。")
        browser.current_page_target()
        return resolved_run_dir

    @staticmethod
    def _close_lease(lease: BrowserLease) -> None:
        if lease._closed:
            return
        lease._closed = True
        if lease._timer is not None:
            lease._timer.cancel()
            lease._timer = None
        try:
            lease.browser.close(clear_profile=True)
        except Exception:
            pass
        if lease.run_dir.exists():
            shutil.rmtree(lease.run_dir, ignore_errors=True)


def inspect_commufa_security_challenge(
    browser: ManagedBrowser,
) -> SecurityChallengeObservation | None:
    target = browser.current_page_target()
    result = browser.evaluate_current_page(_COMMUFA_CHALLENGE_PROBE, timeout=10) or {}
    raw_kind = str(result.get("kind") or "")
    if not raw_kind:
        return None
    kind = ChallengeKind(normalize_challenge_kind(raw_kind))
    if kind is ChallengeKind.VERIFICATION_CODE and not _is_exact_commufa_url(str(target.get("url") or "")):
        return SecurityChallengeObservation(ChallengeKind.OTHER)
    return SecurityChallengeObservation(
        kind=kind,
        code_rejected=bool(result.get("codeRejected")),
    )


def submit_commufa_security_code(browser: ManagedBrowser, code: str) -> None:
    safe_code = _validate_security_code(code)
    target = browser.current_page_target()
    if not _is_exact_commufa_url(str(target.get("url") or "")):
        raise SecurityChallengeSubmissionError(
            "確認コードを入力できるコミュファ公式ページを確認できませんでした。",
            challenge_kind=ChallengeKind.OTHER,
        )
    expression = _COMMUFA_CODE_FILL_TEMPLATE.replace(
        "__SECURITY_CODE_JSON__",
        _json_string(safe_code),
    )
    try:
        result = browser.evaluate_current_page(expression, timeout=10) or {}
        if result.get("filled"):
            # The page commits the entered value on its own render cycle;
            # submitting in the same tick sends an empty code.
            time.sleep(0.8)
            result = browser.evaluate_current_page(
                _COMMUFA_CODE_SUBMIT_TEMPLATE,
                timeout=10,
            ) or {}
    except Exception:
        # CDP/browser diagnostics must never echo the expression containing the
        # one-time code into workflow errors or application logs.
        raise SecurityChallengeSubmissionError(
            "コミュファの確認コード送信処理を完了できませんでした。"
        ) from None
    if result.get("ok"):
        return
    error_code = str(result.get("error") or "")
    if error_code == "CAPTCHA_PRESENT":
        raise SecurityChallengeSubmissionError(
            "CAPTCHAが表示されているため、確認コードを自動入力できません。",
            challenge_kind=ChallengeKind.CAPTCHA,
        )
    if error_code == "FIELD_NOT_FOUND":
        raise SecurityChallengeSubmissionError(
            "コミュファの確認コード入力欄を一意に確認できませんでした。"
        )
    if error_code == "SUBMIT_NOT_FOUND":
        # The labels are page UI text, never the one-time code.
        seen = result.get("controls")
        detail = ""
        if isinstance(seen, list) and seen:
            detail = "（画面上のボタン: " + " / ".join(
                str(label)[:24] for label in seen[:6]
            ) + "）"
        raise SecurityChallengeSubmissionError(
            "コミュファの確認コード送信ボタンを確認できませんでした。" + detail
        )
    raise SecurityChallengeSubmissionError(
        "コミュファの確認コードを安全に送信できませんでした。"
    )


def _validate_security_code(code: str) -> str:
    value = str(code or "")
    if not _SIX_DIGIT_CODE.fullmatch(value):
        raise SecurityCodeValidationError("確認コードは半角数字6桁で入力してください。")
    return value


def _validate_lease_identity(service_id: str, target_month: str) -> None:
    if not _SAFE_SERVICE_ID.fullmatch(str(service_id or "")):
        raise ValueError("ブラウザ保持対象のサービスIDが不正です。")
    if not _SAFE_TARGET_MONTH.fullmatch(str(target_month or "")):
        raise ValueError("ブラウザ保持対象の対象月が不正です。")


def _is_exact_commufa_url(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme.lower() == "https" and (parsed.hostname or "").lower() == COMMUFA_HOST


def _json_string(value: str) -> str:
    # A six-digit ASCII string only; keeping this tiny avoids retaining broader
    # credential payloads in reusable helpers.
    return '"' + value + '"'


_COMMUFA_CHALLENGE_PROBE = r"""(() => {
  const normalize = (value) => String(value || "").normalize("NFKC").replace(/\s+/g, " ").trim().toLowerCase();
  const visible = (el) => {
    if (!el || el.disabled) return false;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
  };
  const labelOf = (el) => [el.placeholder, el.title, el.getAttribute("aria-label"), el.getAttribute("name"), el.getAttribute("id"), el.closest("label")?.innerText, ...[...(el.labels || [])].map((label) => label.innerText)].filter(Boolean).join(" ");
  const pageText = normalize(document.body?.innerText || "");
  const captchaPresent = Boolean(document.querySelector("iframe[src*='recaptcha'], iframe[src*='hcaptcha'], [data-sitekey], .g-recaptcha, .h-captcha")) || /captcha|recaptcha|hcaptcha|画像認証|ロボットではありません/.test(pageText);
  if (captchaPresent) return { kind: "captcha", codeRejected: false };
  const codeWords = ["ワンタイム", "確認コード", "認証コード", "セキュリティコード", "verification code", "one-time", "otp"];
  const candidates = [...document.querySelectorAll("input")].filter(visible).filter((input) => {
    const type = String(input.type || "text").toLowerCase();
    if (!["text", "tel", "number", "password"].includes(type)) return false;
    const label = normalize(labelOf(input));
    const autocomplete = normalize(input.getAttribute("autocomplete"));
    const inputMode = normalize(input.getAttribute("inputmode"));
    const maxLength = Number(input.getAttribute("maxlength") || 0);
    const identified = autocomplete === "one-time-code" || codeWords.some((word) => label.includes(normalize(word)));
    const sixDigitCompatible = maxLength === 0 || maxLength === 6;
    const numericCompatible = type === "number" || inputMode === "numeric" || autocomplete === "one-time-code" || /code|otp|コード|認証/.test(label);
    return identified && sixDigitCompatible && numericCompatible;
  });
  const rejected = /コード[^。\n]{0,30}(正しく|誤|無効|期限|一致し)|入力[^。\n]{0,30}(やり直|確認)/.test(pageText);
  if (candidates.length === 1 && location.protocol === "https:" && location.hostname === "mypage.commufa.jp") {
    return { kind: "verification_code", codeRejected: rejected };
  }
  const challengeWords = ["ワンタイム", "確認コード", "認証コード", "セキュリティコード", "本人確認", "追加認証", "秘密の質問"];
  if (challengeWords.some((word) => pageText.includes(normalize(word)))) return { kind: "other", codeRejected: false };
  return { kind: "", codeRejected: false };
})()"""


_COMMUFA_CODE_FILL_TEMPLATE = r"""(() => {
  const securityCode = __SECURITY_CODE_JSON__;
  const normalize = (value) => String(value || "").normalize("NFKC").replace(/\s+/g, " ").trim().toLowerCase();
  const visible = (el) => {
    if (!el || el.disabled) return false;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
  };
  if (location.protocol !== "https:" || location.hostname !== "mypage.commufa.jp") return { ok: false, error: "ORIGIN_MISMATCH" };
  const pageText = normalize(document.body?.innerText || "");
  const captchaPresent = Boolean(document.querySelector("iframe[src*='recaptcha'], iframe[src*='hcaptcha'], [data-sitekey], .g-recaptcha, .h-captcha")) || /captcha|recaptcha|hcaptcha|画像認証|ロボットではありません/.test(pageText);
  if (captchaPresent) return { ok: false, error: "CAPTCHA_PRESENT" };
  const labelOf = (el) => [el.placeholder, el.title, el.getAttribute("aria-label"), el.getAttribute("name"), el.getAttribute("id"), el.closest("label")?.innerText, ...[...(el.labels || [])].map((label) => label.innerText)].filter(Boolean).join(" ");
  const codeWords = ["ワンタイム", "確認コード", "認証コード", "セキュリティコード", "verification code", "one-time", "otp"];
  const candidates = [...document.querySelectorAll("input")].filter(visible).filter((input) => {
    const type = String(input.type || "text").toLowerCase();
    if (!["text", "tel", "number", "password"].includes(type)) return false;
    const label = normalize(labelOf(input));
    const autocomplete = normalize(input.getAttribute("autocomplete"));
    const inputMode = normalize(input.getAttribute("inputmode"));
    const maxLength = Number(input.getAttribute("maxlength") || 0);
    const identified = autocomplete === "one-time-code" || codeWords.some((word) => label.includes(normalize(word)));
    const sixDigitCompatible = maxLength === 0 || maxLength === 6;
    const numericCompatible = type === "number" || inputMode === "numeric" || autocomplete === "one-time-code" || /code|otp|コード|認証/.test(label);
    return identified && sixDigitCompatible && numericCompatible;
  });
  if (candidates.length !== 1) return { ok: false, error: "FIELD_NOT_FOUND" };
  const input = candidates[0];

  // Enter the code and stop. This login view commits field state on its own
  // render cycle, so submitting in the same tick sends an empty code and the
  // provider simply redisplays the form without mailing a new code.
  input.focus();
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  if (setter) setter.call(input, securityCode);
  else input.value = securityCode;
  input.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText" }));
  input.dispatchEvent(new Event("change", { bubbles: true }));
  input.dispatchEvent(new Event("blur", { bubbles: true }));
  return { ok: true, filled: true };
})()"""


# Runs after the page has had time to commit the value entered above. It never
# contains the one-time code.
_COMMUFA_CODE_SUBMIT_TEMPLATE = r"""(() => {
  const normalize = (value) => String(value || "").normalize("NFKC").replace(/\s+/g, " ").trim().toLowerCase();
  const visible = (el) => {
    if (!el || el.disabled) return false;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
  };
  if (location.protocol !== "https:" || location.hostname !== "mypage.commufa.jp") return { ok: false, error: "ORIGIN_MISMATCH" };
  const filled = [...document.querySelectorAll("input")].filter(visible).find((el) => String(el.value || "").trim().length >= 4);
  if (!filled) return { ok: false, error: "FIELD_NOT_FOUND" };
  const form = filled.closest("form");
  const scope = form || document;
  const controls = [
    ...scope.querySelectorAll(
      "button, input[type='submit'], input[type='button'], [role='button'], a[href='#']"
    ),
  ].filter(visible);
  // "検証" is this provider's submit label. "再送信" must never be treated as a
  // submit: clicking it reissues the code and redisplays the same form, which
  // is indistinguishable from a rejected code.
  const submitWords = ["検証", "確認", "認証", "送信", "次へ", "進む", "ログイン", "verify", "submit", "continue", "next"];
  const submitExcludes = ["再送信", "送信し直", "resend", "キャンセル", "戻る", "cancel"];
  const submit = controls
    .map((el) => ({ el, label: normalize(el.innerText || el.value || el.getAttribute("aria-label")) }))
    .filter((item) => submitWords.some((word) => item.label.includes(normalize(word))))
    .filter((item) => submitExcludes.every((word) => !item.label.includes(normalize(word))))
    .sort((a, b) => a.label.length - b.label.length)[0]?.el;
  if (submit) {
    submit.click();
    return { ok: true, submitted: normalize(submit.innerText || submit.value || "").slice(0, 24) };
  }
  // Some providers submit on Enter with no button at all.
  if (form && typeof form.requestSubmit === "function") {
    form.requestSubmit();
    return { ok: true, submitted: "form" };
  }
  const enter = { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true };
  filled.dispatchEvent(new KeyboardEvent("keydown", enter));
  filled.dispatchEvent(new KeyboardEvent("keypress", enter));
  filled.dispatchEvent(new KeyboardEvent("keyup", enter));
  if (form) return { ok: true, submitted: "enter" };
  // Report the visible control labels so an unknown layout is diagnosable.
  return {
    ok: false,
    error: "SUBMIT_NOT_FOUND",
    controls: controls.slice(0, 8).map((el) => normalize(el.innerText || el.value || el.getAttribute("aria-label")).slice(0, 24)),
  };
})()"""


browser_lease_registry = BrowserLeaseRegistry()
