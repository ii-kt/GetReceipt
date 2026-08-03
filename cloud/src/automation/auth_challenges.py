from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit

from .browser_session import AcquisitionDeadlineExceeded


class AuthChallengeClassification(str, Enum):
    CODE_INPUT = "code_input"
    INTERACTIVE = "interactive"
    UNSUPPORTED = "unsupported"
    NONE = "none"


class AuthChallengeError(RuntimeError):
    """Base error for fail-closed authentication challenge handling."""


class AuthCodeValidationError(AuthChallengeError):
    """Raised without retaining or echoing the rejected code."""


class AuthChallengeSubmissionError(AuthChallengeError):
    """Raised when the current page is unsafe or ambiguous."""

    def __init__(
        self,
        message: str,
        *,
        classification: AuthChallengeClassification = AuthChallengeClassification.NONE,
    ) -> None:
        super().__init__(message)
        self.classification = classification.value


@dataclass(frozen=True)
class AuthChallengeProfile:
    service_id: str
    allowed_hosts: tuple[str, ...]
    min_length: int
    max_length: int
    pattern: re.Pattern[str] = field(repr=False)
    input_label_hints: tuple[str, ...]
    submit_label_hints: tuple[str, ...]


@dataclass(frozen=True)
class AuthChallengeObservation:
    service_id: str
    classification: AuthChallengeClassification
    input_candidates: int = 0
    submit_candidates: int = 0
    # Providers that split the code across one box per digit report the count
    # here; a single labelled field leaves it at zero.
    split_candidates: int = 0


@dataclass(frozen=True)
class AuthSubmissionResult:
    service_id: str
    submitted: bool


class CurrentPageBrowser(Protocol):
    def current_page_target(self) -> Mapping[str, Any]: ...

    def current_page_summary(self) -> Mapping[str, Any]: ...

    def evaluate_current_page(
        self,
        expression: str,
        *,
        timeout: float = 30,
    ) -> Mapping[str, Any]: ...


_DIGITS_3 = re.compile(r"^[0-9]{3}$")
_DIGITS_6 = re.compile(r"^[0-9]{6}$")
_DIGITS_4_TO_8 = re.compile(r"^[0-9]{4,8}$")

_COMMON_SUBMIT_HINTS = (
    "認証",
    "確認",
    "送信",
    "次へ",
    "ログイン",
    "verify",
    "submit",
    "continue",
    "next",
)

SERVICE_PROFILES: dict[str, AuthChallengeProfile] = {
    "epos": AuthChallengeProfile(
        service_id="epos",
        allowed_hosts=("www.eposcard.co.jp",),
        min_length=3,
        max_length=3,
        pattern=_DIGITS_3,
        input_label_hints=(
            "セキュリティコード",
            "カード裏面",
            "3桁",
            "security code",
        ),
        submit_label_hints=_COMMON_SUBMIT_HINTS,
    ),
    "commufa": AuthChallengeProfile(
        service_id="commufa",
        allowed_hosts=("mypage.commufa.jp",),
        min_length=6,
        max_length=6,
        pattern=_DIGITS_6,
        input_label_hints=(
            "確認コード",
            "認証コード",
            "ワンタイム",
            "verification code",
            "one-time",
            "otp",
        ),
        submit_label_hints=_COMMON_SUBMIT_HINTS,
    ),
    "webbilling": AuthChallengeProfile(
        service_id="webbilling",
        allowed_hosts=(
            "webbilling.ntt-finance.co.jp",
            "id.smt.docomo.ne.jp",
            # The d-account sign-in and its verification step are served here.
            "cfg.smt.docomo.ne.jp",
        ),
        min_length=4,
        max_length=8,
        pattern=_DIGITS_4_TO_8,
        input_label_hints=(
            "ワンタイムパスワード",
            "セキュリティコード",
            "確認コード",
            "認証コード",
            "one-time password",
            "verification code",
            "otp",
        ),
        submit_label_hints=_COMMON_SUBMIT_HINTS,
    ),
}

_CAPTCHA_MARKERS = (
    "captcha",
    "recaptcha",
    "hcaptcha",
    "私はロボットではありません",
    "ロボットではない",
    # The bot check in front of the d-account sign-in words itself in English
    # and never says "captcha". Recognising it is what lets the owner answer
    # it on the live page instead of the run failing as a login timeout.
    "confirm you are human",
    "verify you are human",
    "you are not a bot",
    "人間であることを確認",
    "あなたが人間であること",
    "ロボットでないこと",
)
_PASSKEY_MARKERS = (
    "passkey",
    "パスキー",
    "webauthn",
    "セキュリティキー",
    "生体認証",
)


def profile_for(service_id: str) -> AuthChallengeProfile:
    normalized = str(service_id or "").strip().lower()
    try:
        return SERVICE_PROFILES[normalized]
    except KeyError:
        raise ValueError("追加認証のサービス設定を確認できませんでした。") from None


def inspect_current_auth_challenge(
    browser: CurrentPageBrowser,
    service_id: str,
) -> AuthChallengeObservation:
    profile = profile_for(service_id)
    summary = _validated_current_page(browser, profile)
    summary_classification = _classification_from_summary(summary)
    if summary_classification is not AuthChallengeClassification.NONE:
        return AuthChallengeObservation(profile.service_id, summary_classification)

    result = _safe_evaluate(
        browser,
        _probe_expression(profile),
        failure_message="追加認証ページを安全に確認できませんでした。",
    )
    return _observation_from_probe(profile, result)


def submit_current_auth_code(
    browser: CurrentPageBrowser,
    service_id: str,
    code: str,
) -> AuthSubmissionResult:
    profile = profile_for(service_id)
    safe_code = _validate_code(profile, code)
    summary = _validated_current_page(browser, profile)
    summary_classification = _classification_from_summary(summary)
    if summary_classification is AuthChallengeClassification.INTERACTIVE:
        raise AuthChallengeSubmissionError(
            "CAPTCHAは利用者が現在のページで直接完了する必要があります。",
            classification=AuthChallengeClassification.INTERACTIVE,
        )
    if summary_classification is AuthChallengeClassification.UNSUPPORTED:
        raise AuthChallengeSubmissionError(
            "パスキー認証はこの自動入力境界では処理できません。",
            classification=AuthChallengeClassification.UNSUPPORTED,
        )

    probe = _safe_evaluate(
        browser,
        _probe_expression(profile),
        failure_message="追加認証ページを安全に確認できませんでした。",
    )
    observation = _observation_from_probe(profile, probe)
    if observation.classification is AuthChallengeClassification.INTERACTIVE:
        raise AuthChallengeSubmissionError(
            "CAPTCHAは利用者が現在のページで直接完了する必要があります。",
            classification=AuthChallengeClassification.INTERACTIVE,
        )
    if observation.classification is AuthChallengeClassification.UNSUPPORTED:
        raise AuthChallengeSubmissionError(
            "パスキー認証はこの自動入力境界では処理できません。",
            classification=AuthChallengeClassification.UNSUPPORTED,
        )
    if observation.split_candidates:
        # One box per digit: the count must match the code exactly.
        if observation.split_candidates != len(safe_code):
            raise AuthChallengeSubmissionError(
                "追加認証コードの入力欄の数がコードの桁数と一致しません。"
            )
    elif observation.input_candidates != 1:
        raise AuthChallengeSubmissionError(
            "追加認証コードの入力欄を一意に確認できませんでした。"
        )
    # The submit control is deliberately not required here. A code page keeps
    # it disabled until the code is complete, so before filling there is
    # nothing to find; it is checked inside the submission, after the boxes
    # have been filled.

    result = _safe_evaluate(
        browser,
        _submission_expression(profile, safe_code),
        failure_message="追加認証コードを安全に送信できませんでした。",
    )
    classification = _classification_from_raw(result.get("classification"))
    if classification is AuthChallengeClassification.INTERACTIVE:
        raise AuthChallengeSubmissionError(
            "CAPTCHAは利用者が現在のページで直接完了する必要があります。",
            classification=AuthChallengeClassification.INTERACTIVE,
        )
    if classification is AuthChallengeClassification.UNSUPPORTED:
        raise AuthChallengeSubmissionError(
            "パスキー認証はこの自動入力境界では処理できません。",
            classification=AuthChallengeClassification.UNSUPPORTED,
        )
    if not bool(result.get("ok")):
        error = str(result.get("error") or "")
        if error == "INPUT_AMBIGUOUS":
            message = "追加認証コードの入力欄が送信直前に変化しました。"
        elif error == "SUBMIT_AMBIGUOUS":
            message = "追加認証コードの送信ボタンが送信直前に変化しました。"
        elif error == "ORIGIN_MISMATCH":
            message = "追加認証の送信先が公式HTTPSページから変化しました。"
        else:
            message = "追加認証コードを安全に送信できませんでした。"
        raise AuthChallengeSubmissionError(message)
    return AuthSubmissionResult(service_id=profile.service_id, submitted=True)


def _validated_current_page(
    browser: CurrentPageBrowser,
    profile: AuthChallengeProfile,
) -> Mapping[str, Any]:
    try:
        target = browser.current_page_target()
        summary = browser.current_page_summary()
    except AcquisitionDeadlineExceeded:
        # A spent budget is not a page fault, and calling it one sends the
        # owner looking for a broken sign-in that is not broken.
        raise
    except Exception:
        raise AuthChallengeSubmissionError(
            "追加認証用の現在ページを確認できませんでした。"
        ) from None

    target_url = str(target.get("url") or "")
    summary_url = str(summary.get("url") or "")
    if not _is_allowed_url(target_url, profile) or not _is_allowed_url(summary_url, profile):
        raise AuthChallengeSubmissionError(
            "追加認証コードの送信先が公式HTTPSページではありません。"
        )
    if target_url != summary_url:
        raise AuthChallengeSubmissionError(
            "追加認証用の現在ページが確認中に変化しました。"
        )
    return summary


def _validate_code(profile: AuthChallengeProfile, code: str) -> str:
    value = str(code or "")
    if (
        len(value) < profile.min_length
        or len(value) > profile.max_length
        or profile.pattern.fullmatch(value) is None
    ):
        raise AuthCodeValidationError(
            "追加認証コードの形式がサービス要件と一致しません。"
        )
    return value


def _is_allowed_url(url: str, profile: AuthChallengeProfile) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme.lower() == "https"
        and parsed.username is None
        and parsed.password is None
        and parsed.port in (None, 443)
        and (parsed.hostname or "").lower() in profile.allowed_hosts
    )


def _classification_from_summary(
    summary: Mapping[str, Any],
) -> AuthChallengeClassification:
    parts = [
        str(summary.get("title") or ""),
        str(summary.get("text") or ""),
    ]
    visible_inputs = summary.get("visibleInputs")
    if isinstance(visible_inputs, list):
        for item in visible_inputs:
            if isinstance(item, Mapping):
                parts.append(str(item.get("text") or ""))
                parts.append(str(item.get("href") or ""))
    combined = " ".join(parts).casefold()
    if any(marker.casefold() in combined for marker in _CAPTCHA_MARKERS):
        return AuthChallengeClassification.INTERACTIVE
    if any(marker.casefold() in combined for marker in _PASSKEY_MARKERS):
        return AuthChallengeClassification.UNSUPPORTED
    return AuthChallengeClassification.NONE


def _classification_from_raw(value: object) -> AuthChallengeClassification:
    raw = str(value or "").strip().lower()
    if raw == AuthChallengeClassification.INTERACTIVE.value:
        return AuthChallengeClassification.INTERACTIVE
    if raw == AuthChallengeClassification.UNSUPPORTED.value:
        return AuthChallengeClassification.UNSUPPORTED
    if raw == AuthChallengeClassification.CODE_INPUT.value:
        return AuthChallengeClassification.CODE_INPUT
    return AuthChallengeClassification.NONE


def _observation_from_probe(
    profile: AuthChallengeProfile,
    result: Mapping[str, Any],
) -> AuthChallengeObservation:
    classification = _classification_from_raw(result.get("classification"))
    input_count = _safe_count(result.get("inputCount"))
    submit_count = _safe_count(result.get("submitCount"))
    if (
        classification is AuthChallengeClassification.CODE_INPUT
        and input_count == 0
        and submit_count == 0
    ):
        classification = AuthChallengeClassification.NONE
    if classification is AuthChallengeClassification.NONE and (
        input_count or submit_count
    ):
        classification = AuthChallengeClassification.CODE_INPUT
    return AuthChallengeObservation(
        service_id=profile.service_id,
        classification=classification,
        input_candidates=input_count,
        submit_candidates=submit_count,
        split_candidates=_safe_count(result.get("splitCount")),
    )


def _safe_count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if 0 <= parsed <= 100 else 0


def _safe_evaluate(
    browser: CurrentPageBrowser,
    expression: str,
    *,
    failure_message: str,
) -> Mapping[str, Any]:
    try:
        result = browser.evaluate_current_page(expression, timeout=10) or {}
    except AcquisitionDeadlineExceeded:
        # Raised before the expression is ever sent, so it carries no code and
        # is safe to report as what it is.
        raise
    except Exception:
        # Browser errors may include the evaluated expression. Never propagate
        # those diagnostics because the submission expression contains a code.
        raise AuthChallengeSubmissionError(failure_message) from None
    if not isinstance(result, Mapping):
        raise AuthChallengeSubmissionError(failure_message)
    return result


def _profile_payload(profile: AuthChallengeProfile) -> str:
    return json.dumps(
        {
            "hosts": profile.allowed_hosts,
            "inputHints": profile.input_label_hints,
            "submitHints": profile.submit_label_hints,
            "minLength": profile.min_length,
            "maxLength": profile.max_length,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _probe_expression(profile: AuthChallengeProfile) -> str:
    return _DOM_HELPERS.replace("__PROFILE_JSON__", _profile_payload(profile)) + """
return inspect();
})()"""


def _submission_expression(profile: AuthChallengeProfile, code: str) -> str:
    payload = json.dumps(code, ensure_ascii=True)
    return _DOM_HELPERS.replace("__PROFILE_JSON__", _profile_payload(profile)) + f"""
const before = inspect();
if (before.classification === "interactive" || before.classification === "unsupported") {{
  return {{ ok: false, classification: before.classification }};
}}
const inputs = codeInputs();
const boxes = splitCodeBoxes();
const value = {payload};
const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
const fill = (element, text) => {{
  element.focus();
  setter.call(element, text);
  element.dispatchEvent(new Event("input", {{ bubbles: true }}));
  element.dispatchEvent(new Event("change", {{ bubbles: true }}));
}};
if (boxes.length) {{
  // One box per digit: the provider advances focus itself, so each box gets
  // exactly its own character.
  if (boxes.length !== value.length) return {{ ok: false, error: "INPUT_AMBIGUOUS" }};
  boxes.forEach((box, index) => fill(box, value.charAt(index)));
  boxes[boxes.length - 1].dispatchEvent(new Event("blur", {{ bubbles: true }}));
}} else {{
  if (before.inputCount !== 1) return {{ ok: false, error: "INPUT_AMBIGUOUS" }};
  if (inputs.length !== 1) return {{ ok: false, error: "INPUT_AMBIGUOUS" }};
  fill(inputs[0], value);
}}
// Only now look for the button. A code page keeps its submit disabled until
// the code is complete, and a disabled control is not one the owner could
// press either - so searching before filling found nothing and the whole
// submission was abandoned with the code already in hand.
const submits = submitControls();
if (submits.length !== 1) return {{ ok: false, error: "SUBMIT_AMBIGUOUS", submitCount: submits.length }};
submits[0].click();
return {{ ok: true, classification: "code_input" }};
}})()"""


_DOM_HELPERS = r"""(() => {
"use strict";
const profile = __PROFILE_JSON__;
const normalize = (value) => String(value || "").replace(/\s+/g, " ").trim().toLowerCase();
const visible = (element) => {
  if (!(element instanceof Element)) return false;
  const rect = element.getBoundingClientRect();
  const style = getComputedStyle(element);
  return rect.width > 0 && rect.height > 0 &&
    style.display !== "none" && style.visibility !== "hidden" &&
    style.opacity !== "0" && !element.disabled;
};
const labelText = (element) => {
  const labels = element.labels ? [...element.labels].map((label) => label.innerText) : [];
  return normalize([
    ...labels,
    element.getAttribute("aria-label"),
    element.getAttribute("placeholder"),
    element.getAttribute("name"),
    element.getAttribute("id"),
    element.getAttribute("autocomplete"),
    element.getAttribute("title")
  ].join(" "));
};
const hasHint = (text, hints) => hints.some((hint) => text.includes(normalize(hint)));
const captchaPresent = () => {
  const selectors = [
    "iframe[src*='recaptcha' i]", "iframe[src*='hcaptcha' i]",
    ".g-recaptcha", ".h-captcha", "[data-sitekey]",
    "input[name*='captcha' i]", "[id*='captcha' i]"
  ];
  if (selectors.some((selector) => [...document.querySelectorAll(selector)].some(visible))) return true;
  return /captcha|recaptcha|hcaptcha|私はロボットではありません|ロボットではない/i.test(
    document.body ? document.body.innerText : ""
  );
};
const passkeyPresent = () => {
  const text = document.body ? document.body.innerText : "";
  return /passkey|パスキー|webauthn|セキュリティキー|生体認証/i.test(text);
};
const originAllowed = () => (
  location.protocol === "https:" &&
  location.port !== "" && location.port !== "443" ? false :
  location.protocol === "https:" && profile.hosts.includes(location.hostname.toLowerCase())
);
const typedInputs = () => [...document.querySelectorAll("input")]
  .filter(visible)
  .filter((input) => ["text", "tel", "number", "password"].includes(
    (input.getAttribute("type") || "text").toLowerCase()
  ));
// Some providers split the code across one box per digit. Those boxes carry
// no label, so the hint match below never sees them and the whole step looked
// like something only a human could complete.
const splitCodeBoxes = () => {
  const boxes = typedInputs().filter((input) => {
    const max = Number(input.getAttribute("maxlength") || 0);
    // One box per digit does not mean maxlength is one: d-account sets the
    // whole code length on every box. Anything up to a code's length counts,
    // and it is the number of identical unlabelled boxes that identifies the
    // layout, not the attribute.
    if (max >= 1 && max <= profile.maxLength) return true;
    return max === 0 && String(input.className || "").toLowerCase().includes("digit");
  });
  return boxes.length >= profile.minLength && boxes.length <= profile.maxLength ? boxes : [];
};
const codeInputs = () => {
  const boxes = splitCodeBoxes();
  if (boxes.length) return boxes;
  return typedInputs().filter((input) => hasHint(labelText(input), profile.inputHints));
};
const submitControls = () => [...document.querySelectorAll(
  "button, input[type='submit'], input[type='button'], [role='button']"
)]
  .filter(visible)
  .filter((control) => hasHint(normalize([
    control.innerText,
    control.getAttribute("value"),
    control.getAttribute("aria-label"),
    control.getAttribute("title"),
    control.getAttribute("name"),
    control.getAttribute("id")
  ].join(" ")), profile.submitHints));
const inspect = () => {
  if (!originAllowed()) return { classification: "none", inputCount: 0, submitCount: 0, error: "ORIGIN_MISMATCH" };
  if (captchaPresent()) return { classification: "interactive", inputCount: 0, submitCount: 0 };
  if (passkeyPresent()) return { classification: "unsupported", inputCount: 0, submitCount: 0 };
  const boxes = splitCodeBoxes();
  return {
    classification: "code_input",
    inputCount: codeInputs().length,
    splitCount: boxes.length,
    submitCount: submitControls().length
  };
};
"""
