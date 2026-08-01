from __future__ import annotations

import json
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .auth_challenges import (
    AuthChallengeClassification,
    AuthChallengeSubmissionError,
    AuthCodeValidationError,
    inspect_current_auth_challenge,
    submit_current_auth_code,
)
from .browser_session import ManagedBrowser
from .epos import AcquisitionError, FetchedStatement
from .security_challenge import (
    ChallengeKind,
    SecurityChallengeSubmissionError,
    SecurityCodeValidationError,
    inspect_commufa_security_challenge,
    normalize_challenge_kind,
    submit_commufa_security_code,
)
from .statement_validation import inspect_acquired_statement
from ..config import expected_transaction_month, parse_month_key, service_by_id
from ..domain.document_metadata import extract_pdf_text


@dataclass(frozen=True)
class ServiceAutomationConfig:
    target_url: str
    partner_name: str
    login_hints: tuple[str, ...] = ()
    logged_in_hints: tuple[str, ...] = ()
    mail_search_query_template: str = ""
    sender_hints: tuple[str, ...] = ()
    subject_hints: tuple[str, ...] = ()
    attachment_name_hints: tuple[str, ...] = ()


SERVICE_AUTOMATION_CONFIGS: dict[str, ServiceAutomationConfig] = {
    "commufa": ServiceAutomationConfig(
        target_url="https://mypage.commufa.jp/join/s/login/",
        partner_name="中部テレコミュニケーション株式会社",
        login_hints=("Myコミュファログイン", "ログインID", "メールアドレス", "パスワード", "ログイン"),
        logged_in_hints=("ログアウト", "ご契約内容", "ご請求額", "契約内容・ご請求額", "過去の請求額"),
    ),
    "tokuten": ServiceAutomationConfig(
        target_url="https://outlook.live.com/mail/0/",
        partner_name="フラットエナジー株式会社",
        mail_search_query_template="トクテン {year}年{month}月",
        # The billing mail is DMARC-authenticated as flat-energy-co.jp
        # (header.from); besender-s.jp is only the delivery vendor's
        # envelope sender, so matching on it rejected every real invoice.
        sender_hints=(
            "flat-energy-co.jp",
            "besender-s.jp",
            "トクテンでんき 総合サポートセンター",
        ),
        subject_hints=("【トクテンでんき】 請求額確定のお知らせ", "請求額確定のお知らせ"),
        attachment_name_hints=("【トクテンでんき】", "請求書"),
    ),
    "mobile": ServiceAutomationConfig(
        target_url="https://webbilling.ntt-finance.co.jp/mem/b0201/init",
        partner_name="株式会社NTTドコモ",
        login_hints=("Webビリング", "ログイン", "ID", "パスワード", "dアカウント"),
        logged_in_hints=("ログアウト", "請求内容のご確認", "料金支払証明書", "ご利用料金証明書"),
    ),
}


def build_tokuten_search_query(target_month: str, config: ServiceAutomationConfig | None = None) -> str:
    config = config or SERVICE_AUTOMATION_CONFIGS["tokuten"]
    year, month = parse_month_key(expected_transaction_month("tokuten", target_month))
    return (
        config.mail_search_query_template
        .replace("{year}", str(year))
        .replace("{month}", str(month))
        .replace("{month2}", f"{month:02d}")
    )


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))).strip().lower()


def _unissued_month_error(
    action: dict[str, Any],
    *,
    year: int,
    month: int,
) -> AcquisitionError:
    """Say plainly when the provider has not billed this month yet.

    The portal lists only months it has issued. Asking for a later one is not
    a fault to investigate - it simply has not happened.
    """

    months: list[tuple[int, int]] = []
    for value in action.get("availableMonths") or ():
        parsed = re.fullmatch(r"(20\d{2})/(\d{1,2})", str(value).strip())
        if parsed:
            months.append((int(parsed.group(1)), int(parsed.group(2))))
    newest = max(months, default=None)
    if newest is not None and (year, month) > newest:
        return AcquisitionError(
            f"コミュファに{year}年{month}月分の利用明細がまだ掲載されていません。",
            code="COMMUFA_MONTH_NOT_ISSUED",
            advice=(
                f"掲載済みの最新は{newest[0]}年{newest[1]}月分です。"
                "請求が確定してから再実行してください。"
            ),
        )
    return _action_error(
        action,
        f"コミュファで{year}年{month}月分の利用明細を見つけられませんでした。",
        "YEAR_MONTH_NOT_AVAILABLE",
    )


def _login_timeout_advice(
    summary: dict[str, Any],
    *,
    state: str,
    reason: str,
) -> str:
    """Describe what the login page actually showed when the wait timed out.

    page_summary never carries input values, so this reports UI text only:
    the provider's own error banner is what distinguishes a rejected
    password from a page the automation failed to drive.
    """

    from urllib.parse import urlsplit

    parsed = urlsplit(str(summary.get("url") or ""))
    location = f"{parsed.hostname or '?'}{parsed.path or ''}"
    text = re.sub(r"\s+", " ", str(summary.get("text") or "")).strip()
    parts = [f"状態={state}", f"画面={location}"]
    if reason:
        parts.append(f"直前の判定={reason}")
    if text:
        parts.append(f"画面表示={text[:180]}")
    return " / ".join(parts)


def _passed_security_code_gate(summary: dict[str, Any]) -> bool:
    """True once the verification step and the password form are both gone.

    The caller has already established that no security challenge is showing,
    so the remaining question is whether the login form is still up.
    """

    from urllib.parse import urlsplit

    if int(summary.get("passwordFields") or 0) > 0:
        return False
    parsed = urlsplit(str(summary.get("url") or ""))
    if parsed.hostname != "mypage.commufa.jp":
        return False
    path = (parsed.path or "").lower()
    # The verification step lives under its own identity path and carries no
    # password field, so it would otherwise look like a cleared login.
    if "/login" in path or "/identity/verification" in path:
        return False
    text = _normalize(str(summary.get("text") or ""))
    if not text:
        return False
    return not any(
        _normalize(marker) in text
        for marker in (
            "ログインに失敗しました",
            "ログインid（メールアドレス）",
            "id を検証",
            "確認コード入力",
            "コードを再送信",
        )
    )


def _is_commufa_host(url: Any) -> bool:
    """True only when the page has settled on the official Commufa host."""

    from urllib.parse import urlsplit

    parsed = urlsplit(str(url or ""))
    return parsed.scheme == "https" and parsed.hostname == "mypage.commufa.jp"


def _script(payload: dict[str, Any], body: str) -> str:
    return "(() => {\nconst payload = " + json.dumps(payload, ensure_ascii=False) + ";\n" + body + "\n})()"


def _pdf_or_raise(content: bytes, service_label: str) -> None:
    head = content[:256].decode("latin1", errors="ignore")
    lower = head.lower()
    if "<!doctype html" in lower or "<html" in lower:
        raise AcquisitionError(
            f"{service_label}からPDFではなくHTMLページが保存されました。",
            code="DOWNLOADED_HTML",
            advice="ログイン切れ、確認画面、またはサイト仕様変更の可能性があります。取得用ブラウザで表示状態を確認してください。",
        )
    if not content.startswith(b"%PDF"):
        raise AcquisitionError(
            f"{service_label}の取得結果がPDFとして確認できませんでした。",
            code="PDF_SIGNATURE_MISSING",
            advice="ダウンロードやPDF表示が完了しているか、対象月の明細が表示されているか確認してください。",
        )
    if len(content) < 32:
        raise AcquisitionError(
            f"{service_label}のPDFが空、または極端に小さいようです。",
            code="DOWNLOADED_FILE_TOO_SMALL",
            advice="対象月に明細が存在するか確認してください。",
        )


def _action_error(action: dict[str, Any], fallback_message: str, fallback_code: str) -> AcquisitionError:
    advice = str(
        action.get("advice")
        or "取得用ブラウザで対象月の明細が表示されているか確認してください。"
    )
    # The step script already collects what it could see. Discarding it left
    # "entry point not found" with no way to tell which page we were on.
    seen = action.get("visibleControls")
    if isinstance(seen, list) and seen:
        labels = " / ".join(str(label)[:28] for label in seen[:12] if str(label).strip())
        if labels:
            advice = f"{advice} 画面上の操作: {labels}"
    months = action.get("availableMonths")
    if isinstance(months, list) and months:
        advice = f"{advice} 確認できた年月: {' / '.join(str(m) for m in months[:12])}"
    return AcquisitionError(
        action.get("message") or fallback_message,
        code=action.get("code") or fallback_code,
        advice=advice[:600],
    )


def classify_configured_login_state(summary: dict[str, Any], config: ServiceAutomationConfig) -> str:
    text = _normalize(f"{summary.get('title', '')} {summary.get('url', '')} {summary.get('text', '')}")
    logged_in_score = sum(1 for hint in config.logged_in_hints if _normalize(hint) in text)
    login_score = sum(1 for hint in config.login_hints if _normalize(hint) in text)
    has_password = int(summary.get("passwordFields") or 0) > 0
    if logged_in_score > 0:
        return "logged-in"
    if has_password or login_score > 0:
        return "login-required"
    return "unknown"


def classify_tokuten_login_state(summary: dict[str, Any]) -> str:
    page_text = _normalize(f"{summary.get('title', '')} {summary.get('text', '')}")
    url = str(summary.get("url") or "").lower()
    has_password = int(summary.get("passwordFields") or 0) > 0
    login_like_url = bool(re.search(r"login|signin|oauth|microsoftonline|live\.com/login|account\.live\.com", url))
    outlook_like_url = bool(re.search(r"outlook\.(live|office)\.com|mail\.live\.com", url))
    visible_inputs = summary.get("visibleInputs") or []
    has_search_input = any("検索" in _normalize(str(item)) or "search" in _normalize(str(item)) for item in visible_inputs)
    mailbox_hints = ("受信トレイ", "検索", "メール", "inbox", "search", "mail", "message")
    if has_password or (login_like_url and not outlook_like_url):
        return "login-required"
    if outlook_like_url and (has_search_input or any(_normalize(hint) in page_text for hint in mailbox_hints)):
        return "logged-in"
    return "loading" if outlook_like_url else "unknown"


def _downloaded_pdf_content(path: Path, service_label: str) -> bytes:
    content = path.read_bytes()
    _pdf_or_raise(content, service_label)
    return content


def _commufa_usage_month(text: str) -> str | None:
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text or "")))
    match = re.search(r"ご利用年月((?:19|20)\d{2})年(\d{1,2})月分", compact)
    if not match:
        return None
    return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}"


def assert_commufa_usage_month(content: bytes, target_month: str) -> None:
    actual = _commufa_usage_month(extract_pdf_text(content))
    if actual is None:
        raise AcquisitionError(
            "コミュファ明細のご利用年月を確認できませんでした。",
            code="COMMUFA_USAGE_MONTH_NOT_FOUND",
            advice="別ページをPDF化している、またはコミュファ側の表記が変わった可能性があります。",
        )
    if actual != target_month:
        raise AcquisitionError(
            "コミュファで指定したご利用年月と保存対象の明細が一致しません。",
            code="COMMUFA_USAGE_MONTH_MISMATCH",
            advice=f"指定: {target_month} / 取得画面: {actual}。誤取得を避けるため停止しました。",
        )


def _login_payload(credentials: dict[str, str]) -> dict[str, str]:
    login_id = (
        credentials.get("login_id")
        or credentials.get("email")
        or credentials.get("dAccountId")
        or credentials.get("d_account_id")
        or credentials.get("id")
        or ""
    )
    return {"loginId": login_id, "password": credentials.get("password") or ""}


def _apply_auto_login_result(browser: ManagedBrowser, result: dict[str, Any], service_label: str) -> bool:
    code = str(result.get("code") or "")
    if code in {"LOGIN_ID_NOT_CONFIGURED", "D_ACCOUNT_ID_NOT_CONFIGURED", "PASSWORD_NOT_CONFIGURED"}:
        raise AcquisitionError(
            f"{service_label}のログインSecretsが未設定です。",
            code=code,
            advice="Streamlit CloudのSecretsに対象サービスのID/メールアドレスとパスワードを設定してください。",
        )
    if code == "SECURITY_ORIGIN_MISMATCH":
        raise AcquisitionError(
            f"{service_label}の公式ログインページを確認できませんでした。",
            code=code,
            advice="認証情報を入力せず、安全のため取得を終了しました。",
            challenge_kind=ChallengeKind.OTHER,
        )
    if code in {"SECURITY_CHALLENGE", "WAIT_SECURITY_CODE"} or result.get("waitingForSecurityCode"):
        challenge_kind = normalize_challenge_kind(result.get("challengeKind"))
        raise AcquisitionError(
            f"{service_label}で追加認証が表示されました。",
            code="SECURITY_CHALLENGE",
            advice="ワンタイムコード、CAPTCHA、本人確認などサイト側の追加認証が出ているため、通常ログインの自動入力では続行できません。",
            challenge_kind=challenge_kind,
        )
    if code == "LOGIN_REJECTED":
        raise AcquisitionError(
            f"{service_label}がログインを拒否しました。",
            code=code,
            advice=(
                "登録されているログインID・パスワードが最新か確認してください。"
                "続けて試行するとアカウントがロックされる可能性があるため停止しました。"
            ),
        )
    if result.get("attempted") and result.get("directUrl"):
        # Some login entries are links that cannot be clicked because their
        # container is collapsed to zero size.
        browser.navigate(str(result["directUrl"]), wait_seconds=2.0)
        return True
    if result.get("attempted") and result.get("filled"):
        # Let the page commit the field values before the next pass submits.
        time.sleep(0.8)
        return True
    if result.get("attempted") and result.get("clickedInPage"):
        # The page already activated the control. A second click here would
        # submit the credentials twice.
        time.sleep(1.2)
        return True
    if result.get("attempted") and result.get("click"):
        click = result["click"]
        browser.click_at(int(click["x"]), int(click["y"]))
        time.sleep(1.2)
        return True
    if result.get("attempted") and result.get("pressEnter"):
        browser.press_key("Enter")
        time.sleep(1.2)
        return True
    return False


class CommufaAutoFetcher:
    def __init__(self, browser: ManagedBrowser, credentials: dict[str, str] | None = None) -> None:
        self.browser = browser
        self.credentials = credentials or {}
        self.service = service_by_id("commufa")
        self.config = SERVICE_AUTOMATION_CONFIGS["commufa"]
        self._credential_submission_attempted = False

    def open_portal(self) -> dict[str, Any]:
        self.browser.navigate(self.config.target_url, wait_seconds=1.5)
        self._advance_login(max_steps=4)
        return self.browser.page_summary()

    def fetch_pdf(self, target_month: str) -> FetchedStatement:
        self.browser.clear_downloads()
        self.browser.navigate(self.config.target_url, wait_seconds=1.5)
        self._wait_for_login()
        return self._fetch_statement_from_current_page(target_month)

    def resume_after_security_code(self, target_month: str, code: str) -> FetchedStatement:
        """Resume the exact live Commufa page after a six-digit user code.

        This deliberately does not navigate to the portal. A missing browser,
        changed target, unexpected origin, CAPTCHA, or ambiguous input field is
        a terminal error for this attempt.
        """

        try:
            submit_commufa_security_code(self.browser, code)
        except SecurityCodeValidationError as error:
            raise AcquisitionError(
                str(error),
                code="SECURITY_CODE_SUBMISSION_FAILED",
                challenge_kind=ChallengeKind.VERIFICATION_CODE,
            ) from error
        except SecurityChallengeSubmissionError as error:
            raise AcquisitionError(
                str(error),
                code="SECURITY_CODE_SUBMISSION_FAILED",
                challenge_kind=error.challenge_kind,
            ) from error
        self._wait_for_login_after_security_code()
        self.browser.clear_downloads()
        return self._fetch_statement_from_current_page(target_month)

    def _fetch_statement_from_current_page(self, target_month: str) -> FetchedStatement:
        year, month = parse_month_key(target_month)

        metadata_texts: list[str] = []
        logs: list[str] = []
        action: dict[str, Any] = {}
        # This portal renders its dashboard after the navigation completes, so
        # the first passes can legitimately see an empty shell. Give it time to
        # paint before treating a missing entry point as a failure.
        unrecognized_passes = 0
        print_view_opened = False
        for _ in range(20):
            action = self.browser.evaluate(build_commufa_step_expression(year, month), timeout=30) or {}
            logs.extend(str(line) for line in action.get("logs") or [])
            if action.get("metadataText"):
                metadata_texts.append(str(action["metadataText"]))
            if action.get("ok"):
                break
            if action.get("directUrl"):
                self.browser.navigate(str(action["directUrl"]), wait_seconds=min(float(action.get("waitMs") or 2500) / 1000, 2.5))
                continue
            if action.get("code") == "CLICK_PRINT_PAGE":
                # Opening the print view is the last navigation. Re-evaluating
                # would still see the detail page behind it and open the print
                # view again on every pass.
                time.sleep(min(float(action.get("waitMs") or 2400) / 1000, 3.5))
                print_view_opened = True
                break
            if action.get("clickedInPage"):
                # The page already activated the control; clicking the reported
                # point again would trigger the navigation twice.
                time.sleep(min(float(action.get("waitMs") or 1200) / 1000, 3.5))
                continue
            if action.get("click"):
                click = action["click"]
                self.browser.click_at(int(click["x"]), int(click["y"]))
                time.sleep(min(float(action.get("waitMs") or 1200) / 1000, 3.5))
                continue
            if action.get("code") == "YEAR_MONTH_NOT_AVAILABLE":
                raise _unissued_month_error(action, year=year, month=month)
            unrecognized_passes += 1
            if unrecognized_passes <= 10:
                time.sleep(2.0)
                continue
            raise _action_error(action, "コミュファ明細の取得操作を進められませんでした。", "COMMUFA_ACTION_NOT_FOUND")
        else:
            raise AcquisitionError(
                "コミュファ明細の取得操作が完了しませんでした。",
                code="COMMUFA_ACTION_TIMEOUT",
                advice="取得用ブラウザで対象月の利用明細または印刷用ページが表示されているか確認してください。",
            )

        if not action.get("ok") and not print_view_opened:
            raise _action_error(action, "コミュファ明細の取得操作を進められませんでした。", "COMMUFA_ACTION_NOT_FOUND")

        time.sleep(0.6)
        meiym = f"{year}{month:02d}"
        self.browser.switch_to_page(
            lambda target: "print" in f"{target.get('url', '')} {target.get('title', '')}".lower()
            or ("cw40001" in str(target.get("url", "")).lower() and f"meiym={meiym}" in str(target.get("url", "")).lower())
        )
        summary_before_print = self.browser.page_summary()
        if summary_before_print.get("text"):
            metadata_texts.append(str(summary_before_print["text"]))
        pdf_path = self.browser.print_to_pdf(
            self.browser.download_dir / f"commufa-{target_month}-{int(time.time())}.pdf"
        )
        content = _downloaded_pdf_content(pdf_path, self.service.label)
        assert_commufa_usage_month(content, target_month)
        return FetchedStatement(
            content=content,
            source_url=str(summary_before_print.get("url") or self.config.target_url),
            original_file_name=f"commufa_{target_month}.pdf",
            metadata_text=" ".join(metadata_texts),
            logs=tuple(logs),
        )

    def _wait_for_login(self, timeout_seconds: float = 90) -> None:
        deadline = time.time() + timeout_seconds
        last_state = "unknown"
        last_reason = ""
        summary: dict[str, Any] = {}
        while time.time() < deadline:
            summary = self.browser.page_summary()
            state = classify_configured_login_state(summary, self.config)
            last_state = state
            if state == "logged-in":
                return
            # A Salesforce Experience site redirects an unauthenticated visitor
            # to its login page. Do not run the credential script until the page
            # has actually settled on the official host, otherwise a transient
            # redirect origin is mistaken for a hijacked login page.
            if not _is_commufa_host(summary.get("url")):
                last_reason = f"公式ホストへの遷移を待機中（現在: {summary.get('url') or '空'}）。"
                time.sleep(1.0)
                continue
            result = self.browser.evaluate(build_configured_auto_login_expression(self.credentials), timeout=15) or {}
            last_reason = str(result.get("reason") or result.get("code") or "")
            try:
                progressed = self._apply_login_result(result)
            except AcquisitionError as error:
                # Tolerate a brief origin mismatch while the login page is still
                # settling; only surface it if the host never stabilizes.
                if error.code == "SECURITY_ORIGIN_MISMATCH" and time.time() < deadline - 5:
                    time.sleep(1.0)
                    continue
                raise
            if progressed:
                continue
            time.sleep(1.0)
        raise AcquisitionError(
            "コミュファの自動ログインを完了できませんでした。",
            code="LOGIN_REQUIRED" if last_state == "login-required" else "LOGIN_TIMEOUT",
            advice=_login_timeout_advice(summary, state=last_state, reason=last_reason),
        )

    def _wait_for_login_after_security_code(self, timeout_seconds: float = 120) -> None:
        deadline = time.time() + timeout_seconds
        summary: dict[str, Any] = {}
        while time.time() < deadline:
            summary = self.browser.current_page_summary()
            if classify_configured_login_state(summary, self.config) == "logged-in":
                return
            # The portal may land on an interstitial that carries none of the
            # logged-in markers. Once the code page and the password form are
            # both gone, the billing navigation can drive from wherever we are
            # and reports precisely if the page is unexpected.
            if _passed_security_code_gate(summary):
                return
            observation = inspect_commufa_security_challenge(self.browser)
            if observation is not None:
                if observation.kind is ChallengeKind.VERIFICATION_CODE:
                    if observation.code_rejected:
                        raise AcquisitionError(
                            "コミュファで確認コードが拒否されました。",
                            code="SECURITY_CODE_REJECTED",
                            advice="最新の確認コードを確認し、新しい取得操作からやり直してください。",
                            challenge_kind=ChallengeKind.VERIFICATION_CODE,
                        )
                    time.sleep(0.8)
                    continue
                raise AcquisitionError(
                    "コミュファで確認コード以外の追加認証が表示されました。",
                    code="SECURITY_CHALLENGE",
                    advice="CAPTCHA、本人確認、秘密の質問などは自動入力せず終了します。",
                    challenge_kind=observation.kind,
                )
            time.sleep(0.8)
        raise AcquisitionError(
            "コミュファで確認コード送信後のログイン完了を確認できませんでした。",
            code="SECURITY_CODE_TIMEOUT",
            advice=_login_timeout_advice(
                summary,
                state="after-security-code",
                reason="確認コード送信後にログイン済みと判定できませんでした。",
            ),
            challenge_kind=ChallengeKind.VERIFICATION_CODE,
        )

    def _advance_login(self, max_steps: int = 4) -> None:
        for _ in range(max_steps):
            summary = self.browser.page_summary()
            if classify_configured_login_state(summary, self.config) == "logged-in":
                return
            result = self.browser.evaluate(build_configured_auto_login_expression(self.credentials), timeout=8) or {}
            if not self._apply_login_result(result):
                return

    def _apply_login_result(self, result: dict[str, Any]) -> bool:
        if str(result.get("code") or "") in {
            "SUBMIT_PASSWORD",
            "SUBMIT_PASSWORD_ENTER",
        } and not self._allow_credential_submission():
            # Credentials were already sent in this attempt. The provider is
            # simply still working (or is showing a verification step), so keep
            # waiting instead of ending the job.
            return False
        return _apply_auto_login_result(
            self.browser,
            result,
            self.service.label,
        )

    def _allow_credential_submission(self) -> bool:
        """Send the password at most once per attempt, without failing the job.

        Repeating a password submission is what risks an account lock, so it
        stays blocked. Reaching this state is normal (the page has not
        navigated yet), so it must not abort the acquisition.
        """

        if self._credential_submission_attempted:
            return False
        self._credential_submission_attempted = True
        return True


class TokutenAutoFetcher:
    def __init__(self, browser: ManagedBrowser, credentials: dict[str, str] | None = None) -> None:
        self.browser = browser
        self.credentials = credentials or {}
        self.service = service_by_id("tokuten")
        self.config = SERVICE_AUTOMATION_CONFIGS["tokuten"]

    def open_portal(self) -> dict[str, Any]:
        self.browser.navigate(self.config.target_url, wait_seconds=2.0)
        self._advance_mailbox_login(max_steps=4)
        return self.browser.page_summary()

    def fetch_pdf(self, target_month: str) -> FetchedStatement:
        lookup_month = expected_transaction_month(self.service.id, target_month)
        year, month = parse_month_key(lookup_month)
        self.browser.clear_downloads()
        self.browser.navigate(self.config.target_url, wait_seconds=2.0)
        self._wait_for_mailbox()
        self._search_mail(target_month)

        logs: list[str] = []
        # Do not use the requested search query as statement evidence. Only
        # text and filenames observed in the provider response may prove month.
        metadata_texts: list[str] = []
        downloaded: Path | None = None
        last_action: dict[str, Any] = {}
        for _ in range(16):
            last_action = self.browser.evaluate(build_tokuten_step_expression(year, month, self.config), timeout=30) or {}
            logs.extend(str(line) for line in last_action.get("logs") or [])
            if last_action.get("click"):
                marker = time.time()
                click = last_action["click"]
                self.browser.click_at(int(click["x"]), int(click["y"]))
                if last_action.get("expectsDownload"):
                    downloaded = self._wait_for_tokuten_download(marker, year, month)
                    if downloaded:
                        break
                    raise AcquisitionError(
                        "トクテンでんき添付PDFのダウンロード完了を検出できませんでした。",
                        code="DOWNLOAD_TIMEOUT",
                        advice="Outlook Webで添付PDFのダウンロードボタンが表示され、ダウンロードがブロックされていないか確認してください。",
                    )
                time.sleep(min(float(last_action.get("waitMs") or 1800) / 1000, 3.5))
                continue
            raise _action_error(last_action, "トクテンでんきの請求メールまたは添付PDFを見つけられませんでした。", "TOKUTEN_MAIL_NOT_FOUND")

        if downloaded is None:
            raise _action_error(last_action, "トクテンでんき添付PDFの取得操作が完了しませんでした。", "TOKUTEN_DOWNLOAD_NOT_FOUND")

        content = _downloaded_pdf_content(downloaded, self.service.label)
        summary = self.browser.page_summary()
        metadata_texts.append(str(summary.get("text") or ""))
        metadata_texts.append(downloaded.name)
        validation = inspect_acquired_statement(
            service_id=self.service.id,
            target_month=target_month,
            content=content,
            metadata_text=" ".join(metadata_texts),
            original_file_name=downloaded.name,
        )
        if not validation.partner_found:
            raise AcquisitionError(
                "取得したPDFをトクテンでんきの請求書として確認できませんでした。",
                code="TOKUTEN_PROVIDER_NOT_CONFIRMED",
                advice="別メールの添付PDFを保存しないよう処理を停止しました。対象メールを確認してください。",
            )
        if not validation.month_found:
            raise AcquisitionError(
                "取得したトクテンでんき請求書の対象月が指定月と一致しません。",
                code="TOKUTEN_MONTH_MISMATCH",
                advice="対象月が記載された請求メールと添付PDFを確認してください。",
            )
        return FetchedStatement(
            content=content,
            source_url=str(summary.get("url") or self.config.target_url),
            original_file_name=downloaded.name,
            metadata_text=" ".join(metadata_texts),
            logs=tuple(logs),
        )

    def _wait_for_mailbox(self, timeout_seconds: float = 90) -> None:
        deadline = time.time() + timeout_seconds
        last_state = ""
        while time.time() < deadline:
            ready = self.browser.evaluate(build_mailbox_ready_expression(), timeout=15)
            if ready:
                return
            summary = self.browser.page_summary()
            state = classify_tokuten_login_state(summary)
            last_state = state
            if state in {"login-required", "loading", "unknown"}:
                result = self.browser.evaluate(build_microsoft_auto_login_expression(self.credentials), timeout=15) or {}
                if _apply_auto_login_result(self.browser, result, self.service.label):
                    continue
            time.sleep(1.0)
        raise AcquisitionError(
            "Outlook Webのログイン完了を検出できませんでした。",
            code="LOGIN_REQUIRED" if last_state == "login-required" else "MAILBOX_NOT_READY",
            advice="Streamlit Cloud SecretsのMicrosoft/Outlookログイン情報とOutlook Webのログイン画面を確認してください。",
        )

    def _advance_mailbox_login(self, max_steps: int = 4) -> None:
        for _ in range(max_steps):
            ready = self.browser.evaluate(build_mailbox_ready_expression(), timeout=8)
            if ready:
                return
            summary = self.browser.page_summary()
            state = classify_tokuten_login_state(summary)
            if state not in {"login-required", "loading", "unknown"}:
                return
            result = self.browser.evaluate(build_microsoft_auto_login_expression(self.credentials), timeout=8) or {}
            if not _apply_auto_login_result(self.browser, result, self.service.label):
                return

    def _search_mail(self, target_month: str) -> None:
        query = build_tokuten_search_query(target_month, self.config)
        result = self.browser.evaluate(build_outlook_search_expression(query), timeout=20) or {}
        if not result.get("ok"):
            raise _action_error(result, "Outlook Webでメール検索を開始できませんでした。", "SEARCH_FAILED")
        time.sleep(0.2)
        self.browser.press_key("Enter")
        time.sleep(3.0)

    def _wait_for_tokuten_download(self, marker: float, year: int, month: int) -> Path | None:
        end = time.time() + 45
        dirs = [self.browser.download_dir, Path.home() / "Downloads"]
        while time.time() < end:
            for directory in dirs:
                if not directory.exists():
                    continue
                candidates = [
                    path for path in directory.iterdir()
                    if path.is_file()
                    and path.name.lower().endswith(".pdf")
                    and not path.name.endswith((".crdownload", ".tmp"))
                    and path.stat().st_mtime >= marker - 1
                    and _filename_matches_month(path.name, year, month)
                ]
                candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
                if candidates:
                    return self.browser._wait_for_stable_file(candidates[0])
            time.sleep(0.3)
        return None


class WebBillingAutoFetcher:
    def __init__(self, browser: ManagedBrowser, credentials: dict[str, str] | None = None) -> None:
        self.browser = browser
        self.credentials = credentials or {}
        self.service = service_by_id("mobile")
        self.config = SERVICE_AUTOMATION_CONFIGS["mobile"]
        self._credential_submission_attempted = False

    def _navigation_credentials(self) -> dict[str, str]:
        """Credentials for the login script, minus what it no longer needs.

        The script keeps running after the password has gone, because it is
        what walks the portal on to the d-account page. It cannot send the
        password a second time, so carrying it into the page on every poll -
        a hundred times over a two minute wait - achieves nothing.
        """

        if not self._credential_submission_attempted:
            return self.credentials
        return {
            key: value
            for key, value in self.credentials.items()
            if key != "password"
        }

    def open_portal(self) -> dict[str, Any]:
        self.browser.navigate(self.config.target_url, wait_seconds=1.5)
        self._advance_login(max_steps=4)
        return self.browser.page_summary()

    def fetch_pdf(self, target_month: str) -> FetchedStatement:
        year, month = parse_month_key(target_month)
        self.browser.clear_downloads()
        self.browser.navigate(self.config.target_url, wait_seconds=1.5)
        self._wait_for_login()
        self.browser.switch_to_page(lambda target: "webbilling.ntt-finance.co.jp" in str(target.get("url", "")).lower())

        logs: list[str] = []
        metadata_texts: list[str] = []
        downloaded: Path | None = None
        last_action: dict[str, Any] = {}
        for _ in range(28):
            last_action = self.browser.evaluate(build_webbilling_step_expression(year, month), timeout=30) or {}
            logs.extend(str(line) for line in last_action.get("logs") or [])
            if last_action.get("metadataText"):
                metadata_texts.append(str(last_action["metadataText"]))
            if last_action.get("continue"):
                time.sleep(min(float(last_action.get("waitMs") or 900) / 1000, 1.8))
                continue
            if last_action.get("click"):
                marker = time.time()
                click = last_action["click"]
                self.browser.click_at(int(click["x"]), int(click["y"]))
                if last_action.get("expectsDownload") or last_action.get("mayDownload"):
                    downloaded = self.browser.wait_for_download("pdf", marker, 60 if last_action.get("expectsDownload") else 4)
                    if downloaded:
                        break
                    if last_action.get("expectsDownload"):
                        raise AcquisitionError(
                            "Webビリング証明書PDFのダウンロード完了を検知できませんでした。",
                            code="DOWNLOAD_TIMEOUT",
                            advice="ダウンロード確認やブロック表示が出ていないか取得用ブラウザで確認してください。",
                        )
                time.sleep(min(float(last_action.get("waitMs") or 1200) / 1000, 2.0))
                self.browser.switch_to_page(lambda target: "webbilling.ntt-finance.co.jp" in str(target.get("url", "")).lower())
                continue
            raise _action_error(last_action, "Webビリング証明書の取得操作を進められませんでした。", "WEBBILLING_ACTION_NOT_FOUND")

        if downloaded is None:
            raise _action_error(last_action, "Webビリング証明書PDFの取得操作が完了しませんでした。", "WEBBILLING_DOWNLOAD_NOT_FOUND")

        content = _downloaded_pdf_content(downloaded, self.service.label)
        summary = self.browser.page_summary()
        metadata_texts.append(str(summary.get("text") or ""))
        metadata_texts.append(downloaded.name)
        validation = inspect_acquired_statement(
            service_id=self.service.id,
            target_month=target_month,
            content=content,
            metadata_text=" ".join(metadata_texts),
            original_file_name=downloaded.name,
        )
        if not validation.partner_found:
            raise AcquisitionError(
                "取得したPDFをNTTファイナンスのWebビリング証明書として確認できませんでした。",
                code="WEBBILLING_PROVIDER_NOT_CONFIRMED",
                advice="別ページのPDFを保存しないよう処理を停止しました。Webビリングの対象明細を確認してください。",
            )
        if not validation.month_found:
            raise AcquisitionError(
                "取得したWebビリング証明書の対象月が指定月と一致しません。",
                code="WEBBILLING_MONTH_MISMATCH",
                advice="Webビリングで指定したご利用年月を確認してください。",
            )
        return FetchedStatement(
            content=content,
            source_url=str(summary.get("url") or self.config.target_url),
            original_file_name=downloaded.name,
            metadata_text=" ".join(metadata_texts),
            logs=tuple(logs),
        )

    def resume_after_security_code(self, target_month: str, code: str) -> FetchedStatement:
        """Resume the exact Chrome page after Web Billing/d-account OTP."""

        try:
            submit_current_auth_code(self.browser, "webbilling", code)
        except AuthCodeValidationError as error:
            raise AcquisitionError(
                str(error),
                code="SECURITY_CODE_SUBMISSION_FAILED",
                challenge_kind=ChallengeKind.VERIFICATION_CODE,
            ) from error
        except AuthChallengeSubmissionError as error:
            challenge_kind = (
                ChallengeKind.CAPTCHA
                if error.classification == AuthChallengeClassification.INTERACTIVE.value
                else ChallengeKind.OTHER
            )
            raise AcquisitionError(
                str(error),
                code="SECURITY_CODE_SUBMISSION_FAILED",
                challenge_kind=challenge_kind,
            ) from error
        self._wait_for_login_after_security_code()
        return self.fetch_pdf(target_month)

    def _wait_for_login(self, timeout_seconds: float = 120) -> None:
        deadline = time.time() + timeout_seconds
        last_state = "unknown"
        last_reason = ""
        summary: dict[str, Any] = {}
        # The d-account sign-in happens on another host and can bounce back
        # here, so the final page never shows why it was refused.
        offsite_summary: dict[str, Any] = {}
        trail: list[str] = []
        while time.time() < deadline:
            summary = self.browser.page_summary()
            host = urlsplit(str(summary.get("url") or "")).hostname or ""
            if host and (not trail or trail[-1] != host):
                trail.append(host)
            if host.endswith("smt.docomo.ne.jp"):
                offsite_summary = summary
            state = classify_configured_login_state(summary, self.config)
            last_state = state
            if state == "logged-in":
                return
            auto_login = self.browser.evaluate(
                build_webbilling_auto_login_expression(self._navigation_credentials()),
                timeout=15,
            ) or {}
            last_reason = str(
                auto_login.get("reason") or auto_login.get("code") or ""
            )
            if self._apply_login_result(auto_login):
                continue
            time.sleep(1.0)
        raise AcquisitionError(
            "Webビリングのログイン完了を検知できませんでした。",
            code="LOGIN_REQUIRED" if last_state == "login-required" else "LOGIN_TIMEOUT",
            advice=_login_timeout_advice(
                offsite_summary or summary,
                state=last_state,
                reason=(
                    f"{last_reason} / 経路: {' → '.join(trail[:6])}"
                    if trail
                    else last_reason
                ),
            ),
        )

    def _wait_for_login_after_security_code(self, timeout_seconds: float = 90) -> None:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            summary = self.browser.current_page_summary()
            if classify_configured_login_state(summary, self.config) == "logged-in":
                return
            try:
                observation = inspect_current_auth_challenge(
                    self.browser,
                    "webbilling",
                )
            except AuthChallengeSubmissionError:
                time.sleep(0.8)
                continue
            if observation.classification is AuthChallengeClassification.CODE_INPUT:
                raise AcquisitionError(
                    "Webビリングで確認コードを受け付けられませんでした。",
                    code="SECURITY_CODE_REJECTED",
                    advice="最新のメールまたはSMSコードを確認してください。",
                    challenge_kind=ChallengeKind.VERIFICATION_CODE,
                )
            if observation.classification is AuthChallengeClassification.INTERACTIVE:
                raise AcquisitionError(
                    "WebビリングまたはdアカウントでCAPTCHAが表示されました。",
                    code="SECURITY_CHALLENGE",
                    challenge_kind=ChallengeKind.CAPTCHA,
                )
            if observation.classification is AuthChallengeClassification.UNSUPPORTED:
                raise AcquisitionError(
                    "dアカウントで遠隔ワーカー非対応のパスキー認証が表示されました。",
                    code="SECURITY_CHALLENGE",
                    challenge_kind=ChallengeKind.OTHER,
                )
            time.sleep(0.8)
        raise AcquisitionError(
            "Webビリングで確認コード送信後のログイン完了を確認できませんでした。",
            code="SECURITY_CODE_TIMEOUT",
            challenge_kind=ChallengeKind.VERIFICATION_CODE,
        )

    def _advance_login(self, max_steps: int = 4) -> None:
        for _ in range(max_steps):
            summary = self.browser.page_summary()
            if classify_configured_login_state(summary, self.config) == "logged-in":
                return
            auto_login = self.browser.evaluate(
                build_webbilling_auto_login_expression(self._navigation_credentials()),
                timeout=8,
            ) or {}
            if self._apply_login_result(auto_login):
                continue
            return

    def _apply_login_result(self, result: dict[str, Any]) -> bool:
        if str(result.get("code") or "") in {
            "SUBMIT_PASSWORD",
            "SUBMIT_PASSWORD_ENTER",
        } and not self._allow_credential_submission():
            # Already submitted in this attempt: keep waiting for the provider
            # instead of ending the job.
            return False
        return _apply_auto_login_result(
            self.browser,
            result,
            self.service.label,
        )

    def _allow_credential_submission(self) -> bool:
        """Send the password at most once per attempt, without failing the job."""

        if self._credential_submission_attempted:
            return False
        self._credential_submission_attempted = True
        return True


def _filename_matches_month(file_name: str, year: int, month: int) -> bool:
    text = _normalize(file_name)
    month_no_pad = str(int(month))
    month_pad = f"{int(month):02d}"
    return any(
        _normalize(token) in text
        for token in (
            f"{year}年{month_no_pad}月",
            f"{year}年{month_pad}月",
            f"{year}/{month_no_pad}",
            f"{year}/{month_pad}",
            f"{year}-{month_no_pad}",
            f"{year}-{month_pad}",
            f"{year}{month_pad}",
        )
    )


def build_commufa_step_expression(year: int, month: int) -> str:
    return _script(
        {"year": year, "month": month},
        r"""
const targetYear = String(payload.year);
const targetMonth = Number(payload.month);
const monthNoPad = String(targetMonth);
const monthPad = String(targetMonth).padStart(2, "0");
const normalize = (value) => String(value || "").normalize("NFKC").replace(/\s+/g, " ").trim().toLowerCase();
const visible = (el) => {
  if (!el || el.disabled) return false;
  const style = getComputedStyle(el);
  const rect = el.getBoundingClientRect();
  return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
};
const labelOf = (el) => {
  if (!el) return "";
  const imageText = [...(el.querySelectorAll ? el.querySelectorAll("img") : [])]
    .map((img) => [img.alt, img.title, img.getAttribute("aria-label")].filter(Boolean).join(" "))
    .join(" ");
  return [el.innerText, el.textContent, el.value, el.alt, el.title, el.getAttribute && el.getAttribute("aria-label"), el.getAttribute && el.getAttribute("name"), el.getAttribute && el.getAttribute("id"), el.href, imageText].filter(Boolean).join(" ");
};
const contextOf = (el, maxDepth = 5) => {
  const values = [];
  let cursor = el;
  for (let depth = 0; cursor && depth < maxDepth; depth += 1, cursor = cursor.parentElement) values.push(labelOf(cursor));
  return values.join(" ");
};
const pointOf = (el) => {
  el.scrollIntoView({ block: "center", inline: "center" });
  const rect = el.getBoundingClientRect();
  return { x: Math.round(rect.left + rect.width / 2), y: Math.round(rect.top + rect.height / 2) };
};
// This portal is a single-page app that re-renders between measuring an
// element and clicking its coordinates, which silently misses. Activate the
// control in the page and report the point for diagnostics only.
const activate = (el) => { const point = pointOf(el); el.click(); return point; };
const controls = () => [...document.querySelectorAll("a, button, input[type='button'], input[type='submit'], input[type='image'], [role='button'], [onclick], [tabindex]")].filter(visible);
const pageText = () => normalize(document.body?.innerText || "");
const hasAny = (text, words) => words.some((word) => text.includes(normalize(word)));
const hasTargetMonth = (text) => {
  const t = normalize(text);
  const japaneseMonth = new RegExp(targetYear + "\\s*年\\s*0?" + targetMonth + "\\s*月(?!\\s*\\d{1,2}\\s*日)");
  return japaneseMonth.test(t) || t.includes(targetYear + "/" + monthNoPad) || t.includes(targetYear + "/" + monthPad) || t.includes(targetYear + "-" + monthNoPad) || t.includes(targetYear + "-" + monthPad);
};
const bestControl = (keywords, excludes = []) => controls()
  .map((el) => {
    const label = labelOf(el);
    const text = normalize(label);
    const context = normalize(contextOf(el, 4));
    let score = 0;
    for (const word of keywords) {
      const key = normalize(word);
      if (text.includes(key)) score += 180 + key.length;
      if (context.includes(key)) score += 35;
    }
    for (const word of excludes) {
      const key = normalize(word);
      if (text.includes(key) || context.includes(key)) score -= 220;
    }
    return { el, label, score };
  })
  .filter((item) => item.score > 90)
  .sort((a, b) => b.score - a.score || a.label.length - b.label.length)[0] || null;
const collectMonths = () => {
  const values = new Set();
  for (const el of [...document.querySelectorAll("tr, option, li, div")].filter(visible)) {
    // Skip a timestamp such as "2026年07月29日": it is the page's own clock,
    // not a billed month, and it made an unissued month look available.
    const match = labelOf(el).replace(/\s+/g, " ").match(/(20\d{2})\s*年\s*(\d{1,2})\s*月(?!\s*\d{1,2}\s*日)/);
    if (match) values.add(match[1] + "/" + String(Number(match[2])).padStart(2, "0"));
  }
  return [...values].slice(0, 40);
};
const text = pageText();
if (text.includes(normalize("利用料金のお知らせ")) && hasTargetMonth(text)) {
  const print = bestControl(["印刷用ページ"], ["ログアウト"]);
  if (print) return { ok: false, code: "CLICK_PRINT_PAGE", click: activate(print.el), clickedInPage: true, waitMs: 2400, logs: ["印刷用ページを開きます: " + print.label.trim().slice(0, 120)] };
  return { ok: true, code: "DETAIL_PAGE_READY", fallbackPrint: true, metadataText: document.body?.innerText || "", logs: ["対象ご利用年月の利用明細ページをPDF保存します。"] };
}
const onPastBillList = text.includes(normalize("過去の請求額の一覧")) && (text.includes(normalize("ご利用年月")) || text.includes(normalize("請求金額")) || location.href.includes("CW40004"));
if (onPastBillList) {
  const rows = [...document.querySelectorAll("tr, li, section, article, div")]
    .filter(visible)
    .map((el) => ({ el, text: labelOf(el) }))
    .filter((item) => hasTargetMonth(item.text))
    .sort((a, b) => a.text.length - b.text.length)
    .slice(0, 80);
  for (const row of rows) {
    const usage = [...row.el.querySelectorAll("a, button, input[type='button'], input[type='submit'], [role='button']")]
      .filter(visible)
      .map((el) => ({ el, label: labelOf(el), text: normalize(labelOf(el)) }))
      .filter((item) => item.text.includes(normalize("利用明細")) && !item.text.includes(normalize("通話明細")))
      .sort((a, b) => a.label.length - b.label.length)[0];
    if (usage) return { ok: false, code: "CLICK_USAGE_DETAIL", click: activate(usage.el), clickedInPage: true, waitMs: 3000, logs: ["対象ご利用年月の利用明細を開きます: " + row.text.trim().slice(0, 120)] };
  }
  const availableMonths = collectMonths();
  return { ok: false, code: "YEAR_MONTH_NOT_AVAILABLE", message: targetYear + "/" + monthPad + " のご利用年月に対応する利用明細を見つけられませんでした。", advice: availableMonths.length ? "確認できた年月候補: " + availableMonths.join(" / ") : "過去の請求額一覧に対象年月の行が表示されているか確認してください。", availableMonths, logs: [] };
}
if (text.includes(normalize("ご利用料金・契約内容のご確認")) || text.includes(normalize("ご契約・料金トップ"))) {
  const past = bestControl(["過去の請求額の一覧"], ["ログアウト"]);
  if (past) return { ok: false, code: "CLICK_PAST_BILL_LIST", click: activate(past.el), clickedInPage: true, waitMs: 3000, logs: ["過去の請求額一覧を開きます: " + past.label.trim().slice(0, 120)] };
}
const direct = [...document.querySelectorAll("a")].filter(visible).find((el) => String(el.href || "").includes("COM_RedirectPage") && String(el.href || "").includes("ApplicationTop"));
if (direct) return { ok: false, code: "NAVIGATE_TO_BILLING_TOP", directUrl: direct.href, waitMs: 3000, logs: ["請求確認画面への直接遷移を検出: " + direct.href] };
const entry = bestControl(["ご契約内容・ご請求額の確認", "ご利用料金の確認", "詳しくはこちら"], ["netflix", "youtube", "hulu", "ログアウト", "詳細はこちら"]);
if (entry) return { ok: false, code: "CLICK_BILLING_ENTRY", click: activate(entry.el), clickedInPage: true, waitMs: 3500, logs: ["請求確認画面を開きます: " + entry.label.trim().slice(0, 120)] };
return { ok: false, code: "CONTRACT_BILLING_PAGE_NOT_FOUND", message: "コミュファ画面で請求確認画面への入口を見つけられませんでした。", advice: "Myコミュファにログイン後、請求確認画面へ進める状態か確認してください。", visibleControls: controls().slice(0, 50).map((el) => labelOf(el).trim().slice(0, 120)).filter(Boolean), logs: [] };
""",
    )


def build_mailbox_ready_expression() -> str:
    return r"""(() => {
const normalize = (value) => String(value || "").normalize("NFKC").replace(/\s+/g, " ").trim().toLowerCase();
const visible = (el) => {
  if (!el) return false;
  const style = getComputedStyle(el);
  const rect = el.getBoundingClientRect();
  return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
};
const labelOf = (el) => [el.innerText, el.textContent, el.value, el.placeholder, el.title, el.getAttribute && el.getAttribute("aria-label"), el.getAttribute && el.getAttribute("name"), el.getAttribute && el.getAttribute("id")].filter(Boolean).join(" ");
const searchBox = [...document.querySelectorAll("input, textarea, [contenteditable='true'], [role='searchbox']")]
  .filter(visible)
  .find((el) => {
    const label = normalize(labelOf(el));
    return label.includes("検索") || label.includes("search");
  });
const pageText = normalize(document.body?.innerText || "");
const mailboxLoaded = pageText.includes("受信トレイ") || pageText.includes("inbox") || pageText.includes("新規メール") || pageText.includes("優先") || pageText.includes("その他") || pageText.includes("message list");
return Boolean(searchBox) && mailboxLoaded;
})()"""


def build_configured_auto_login_expression(credentials: dict[str, str]) -> str:
    return _script(
        _login_payload(credentials),
        r"""
const normalize = (value) => String(value || "").normalize("NFKC").replace(/\s+/g, " ").trim().toLowerCase();
const visible = (el) => {
  if (!el || el.disabled) return false;
  const style = getComputedStyle(el);
  const rect = el.getBoundingClientRect();
  return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
};
const labelOf = (el) => [el.innerText, el.textContent, el.value, el.placeholder, el.title, el.alt, el.getAttribute && el.getAttribute("aria-label"), el.getAttribute && el.getAttribute("name"), el.getAttribute && el.getAttribute("id")].filter(Boolean).join(" ");
const contextOf = (el, depth = 4) => {
  const values = [];
  let cursor = el;
  for (let i = 0; cursor && i < depth; i += 1, cursor = cursor.parentElement) values.push(labelOf(cursor));
  return values.join(" ");
};
const pointOf = (el) => {
  el.scrollIntoView({ block: "center", inline: "center" });
  const rect = el.getBoundingClientRect();
  return { x: Math.round(rect.left + rect.width / 2), y: Math.round(rect.top + rect.height / 2) };
};
const setValue = (el, value) => {
  el.focus();
  if (typeof el.select === "function") el.select();
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
  if (setter) setter.call(el, value);
  else el.value = value;
  el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
};
const controls = () => [...document.querySelectorAll("button, input[type='button'], input[type='submit'], a, [role='button'], [onclick], [tabindex]")].filter(visible);
const byText = (words, excludes = []) => controls()
  .map((el) => ({ el, text: normalize(labelOf(el)) }))
  .filter((item) => words.some((word) => item.text.includes(normalize(word))))
  .filter((item) => excludes.every((word) => !item.text.includes(normalize(word))))
  .sort((a, b) => a.text.length - b.text.length)[0]?.el || null;
const pageText = normalize(document.body?.innerText || "");
if (location.protocol !== "https:" || location.hostname !== "mypage.commufa.jp") {
  return { attempted: false, code: "SECURITY_ORIGIN_MISMATCH", challengeKind: "other", reason: "公式ログインページではありません（着地: " + location.protocol + "//" + location.hostname + location.pathname + "）。" };
}
const captchaPresent = Boolean(document.querySelector("iframe[src*='recaptcha'], iframe[src*='hcaptcha'], [data-sitekey], .g-recaptcha, .h-captcha")) || ["captcha", "recaptcha", "hcaptcha", "画像認証", "ロボットではありません"].some((word) => pageText.includes(normalize(word)));
if (captchaPresent) return { attempted: false, code: "SECURITY_CHALLENGE", challengeKind: "captcha", reason: "CAPTCHAが表示されています。" };
const otpWords = ["ワンタイム", "確認コード", "認証コード", "セキュリティコード", "verification code", "one-time", "otp"];
const otpInputs = [...document.querySelectorAll("input")].filter(visible).filter((input) => {
  const type = String(input.type || "text").toLowerCase();
  if (!["text", "tel", "number", "password"].includes(type)) return false;
  const label = normalize(labelOf(input) + " " + contextOf(input, 4));
  const autocomplete = normalize(input.getAttribute("autocomplete"));
  const inputMode = normalize(input.getAttribute("inputmode"));
  const maxLength = Number(input.getAttribute("maxlength") || 0);
  const identified = autocomplete === "one-time-code" || otpWords.some((word) => label.includes(normalize(word)));
  const numericCompatible = type === "number" || inputMode === "numeric" || autocomplete === "one-time-code" || /code|otp|コード|認証/.test(label);
  return identified && numericCompatible && (maxLength === 0 || maxLength === 6);
});
if (otpInputs.length === 1 && location.protocol === "https:" && location.hostname === "mypage.commufa.jp") {
  return { attempted: false, code: "SECURITY_CHALLENGE", waitingForSecurityCode: true, challengeKind: "verification_code", reason: "確認コード入力待ちです。" };
}
// The identity verification view is the emailed-code step, which this app
// completes automatically. Its own text says 確認コード, so matching that word
// as a different challenge stopped the run on the very page it can handle.
// Commufa's published login second factor is that code (its SMS step belongs
// to initial ID registration), so no other wording is treated as a challenge
// here; a CAPTCHA is still detected from the DOM above.
if (location.hostname === "mypage.commufa.jp" && (
  location.pathname.toLowerCase().includes("/identity/verification")
  || pageText.includes(normalize("id を検証"))
  || pageText.includes(normalize("コードを再送信"))
)) {
  return { attempted: false, code: "SECURITY_CHALLENGE", waitingForSecurityCode: true, challengeKind: "verification_code", reason: "確認コード入力画面です。" };
}
const passwordInput = [...document.querySelectorAll("input[type='password']")].find(visible);
const textInputs = [...document.querySelectorAll("input, textarea")]
  .filter(visible)
  .filter((input) => ["", "text", "email", "tel"].includes(String(input.type || "").toLowerCase()));
const accountInput = textInputs
  .map((input) => {
    const text = normalize(labelOf(input) + " " + contextOf(input, 5));
    let score = 0;
    if (text.includes("id") || text.includes(normalize("ログインID"))) score += 260;
    if (text.includes("mail") || text.includes("email") || text.includes(normalize("メール"))) score += 220;
    if (text.includes("account") || text.includes(normalize("アカウント"))) score += 180;
    if (String(input.type || "").toLowerCase() === "email") score += 120;
    if (text.includes(normalize("検索")) || text.includes("search")) score -= 500;
    return { input, score };
  })
  .filter((item) => item.score > 0)
  .sort((a, b) => b.score - a.score)[0]?.input || textInputs[0] || null;
if (/ログインに失敗しました|ユーザー名とパスワードが正しいか/.test(pageText)) {
  return { attempted: false, code: "LOGIN_REJECTED", reason: "サイトがログインを拒否しました。" };
}
let filledNow = false;
if (accountInput && !String(accountInput.value || "").trim()) {
  if (!payload.loginId) return { attempted: false, code: "LOGIN_ID_NOT_CONFIGURED", reason: "ログインIDまたはメールアドレスが未設定です。" };
  setValue(accountInput, payload.loginId);
  filledNow = true;
}
if (passwordInput) {
  const passwordFilled = String(passwordInput.value || "").trim().length > 0;
  if (!payload.password && !passwordFilled) return { attempted: false, code: "PASSWORD_NOT_CONFIGURED", reason: "パスワードが未設定です。" };
  if (payload.password && !passwordFilled) {
    setValue(passwordInput, payload.password);
    filledNow = true;
  }
  if (filledNow) {
    // Submitting in the same tick as the fill loses the values: this login
    // view commits field state on its own render cycle. Let that happen
    // first and submit on the next pass.
    return { attempted: true, code: "CREDENTIALS_FILLED", filled: true, reason: "入力値の反映待ちです。" };
  }
  const button = byText(["ログイン", "login", "サインイン", "sign in", "送信", "submit", "次へ", "next"], ["戻る", "キャンセル", "お忘れ", "新規", "登録"]);
  if (button) {
    // Activate the control in the page. A coordinate click can miss when the
    // layout shifts between measuring and clicking, and these single-page
    // login views re-render as the fields are filled.
    const point = pointOf(button);
    button.click();
    return { attempted: true, code: "SUBMIT_PASSWORD", clickedInPage: true, click: point };
  }
  return { attempted: true, code: "SUBMIT_PASSWORD_ENTER", pressEnter: true };
}
if (accountInput) {
  const button = byText(["次へ", "next", "ログイン", "login", "サインイン", "sign in", "続行", "continue"], ["戻る", "キャンセル", "お忘れ", "新規", "登録"]);
  if (button) return { attempted: true, code: "SUBMIT_LOGIN_ID", click: pointOf(button) };
  return { attempted: true, code: "SUBMIT_LOGIN_ID_ENTER", pressEnter: true };
}
const loginEntry = byText(["ログイン", "login", "サインイン", "sign in"], ["新規", "登録", "お忘れ", "キャンセル"]);
if (loginEntry) return { attempted: true, code: "CLICK_LOGIN_ENTRY", click: pointOf(loginEntry) };
return { attempted: false, code: "LOGIN_STEP_NOT_FOUND", reason: "自動ログイン対象の入力欄またはボタンを見つけられませんでした。" };
""",
    )


def build_microsoft_auto_login_expression(credentials: dict[str, str]) -> str:
    return _script(
        _login_payload(credentials),
        r"""
const normalize = (value) => String(value || "").normalize("NFKC").replace(/\s+/g, " ").trim().toLowerCase();
const visible = (el) => {
  if (!el || el.disabled) return false;
  const style = getComputedStyle(el);
  const rect = el.getBoundingClientRect();
  return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
};
const labelOf = (el) => [el.innerText, el.textContent, el.value, el.placeholder, el.title, el.getAttribute && el.getAttribute("aria-label"), el.getAttribute && el.getAttribute("name"), el.getAttribute && el.getAttribute("id")].filter(Boolean).join(" ");
const pointOf = (el) => {
  el.scrollIntoView({ block: "center", inline: "center" });
  const rect = el.getBoundingClientRect();
  return { x: Math.round(rect.left + rect.width / 2), y: Math.round(rect.top + rect.height / 2) };
};
const setValue = (el, value) => {
  el.focus();
  if (typeof el.select === "function") el.select();
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  if (setter) setter.call(el, value);
  else el.value = value;
  el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
};
const controls = () => [...document.querySelectorAll("button, input[type='button'], input[type='submit'], a, [role='button']")].filter(visible);
const byText = (words, excludes = []) => controls()
  .map((el) => ({ el, text: normalize(labelOf(el)) }))
  .filter((item) => words.some((word) => item.text.includes(normalize(word))))
  .filter((item) => excludes.every((word) => !item.text.includes(normalize(word))))
  .sort((a, b) => a.text.length - b.text.length)[0]?.el || null;
const pageText = normalize(document.body?.innerText || "");
const captchaPresent = Boolean(document.querySelector("iframe[src*='recaptcha'], iframe[src*='hcaptcha'], [data-sitekey], .g-recaptcha, .h-captcha")) || ["captcha", "recaptcha", "hcaptcha", "画像認証", "ロボットではありません"].some((word) => pageText.includes(normalize(word)));
if (captchaPresent) return { attempted: false, code: "SECURITY_CHALLENGE", challengeKind: "captcha", reason: "CAPTCHAが表示されています。" };
const passkeyWords = ["passkey", "パスキー", "webauthn", "セキュリティキー", "生体認証"];
if (passkeyWords.some((word) => pageText.includes(normalize(word)))) return { attempted: false, code: "SECURITY_CHALLENGE", challengeKind: "passkey_unavailable", reason: "遠隔ワーカー非対応のパスキー認証が表示されています。" };
const securityWords = ["ワンタイム", "認証コード", "確認コード", "セキュリティコード", "本人確認"];
if (securityWords.some((word) => pageText.includes(normalize(word)))) return { attempted: false, code: "SECURITY_CHALLENGE", challengeKind: "interactive", reason: "追加認証が表示されています。" };
const submit = document.querySelector("#idSIButton9, input[type='submit']");
const staySignedIn = byText(["はい", "yes", "続行", "continue", "サインインの状態を維持"], ["いいえ", "no"]);
if (staySignedIn) return { attempted: true, code: "STAY_SIGNED_IN", click: pointOf(staySignedIn) };
const passwordInput = [...document.querySelectorAll("input[type='password']")].find(visible);
if (passwordInput) {
  if (!payload.password && !String(passwordInput.value || "").trim()) return { attempted: false, code: "PASSWORD_NOT_CONFIGURED", reason: "Microsoftログインのパスワードが未設定です。" };
  if (payload.password) setValue(passwordInput, payload.password);
  const button = (submit && visible(submit)) ? submit : byText(["サインイン", "sign in", "ログイン", "login", "次へ", "next"]);
  if (!button) return { attempted: true, code: "SUBMIT_PASSWORD_ENTER", pressEnter: true };
  return { attempted: true, code: "SUBMIT_PASSWORD", click: pointOf(button) };
}
const accountInput = [...document.querySelectorAll("input[type='email'], input[name='loginfmt'], input[type='text']")]
  .filter(visible)
  .find((input) => {
    const label = normalize(labelOf(input));
    return label.includes("メール") || label.includes("email") || label.includes("account") || label.includes("login");
  });
if (accountInput) {
  if (!payload.loginId && !String(accountInput.value || "").trim()) return { attempted: false, code: "LOGIN_ID_NOT_CONFIGURED", reason: "Microsoftログインのアカウントが未設定です。" };
  if (payload.loginId) setValue(accountInput, payload.loginId);
  const button = (submit && visible(submit)) ? submit : byText(["次へ", "next", "続行", "continue"]);
  if (!button) return { attempted: true, code: "SUBMIT_LOGIN_ID_ENTER", pressEnter: true };
  return { attempted: true, code: "SUBMIT_LOGIN_ID", click: pointOf(button) };
}
return { attempted: false, code: "LOGIN_STEP_NOT_FOUND", reason: "自動で押せるMicrosoftログイン操作は見つかりませんでした。" };
""",
    )


def build_outlook_search_expression(query: str) -> str:
    return _script(
        {"query": query},
        r"""
const normalize = (value) => String(value || "").normalize("NFKC").replace(/\s+/g, " ").trim().toLowerCase();
const visible = (el) => {
  if (!el || el.disabled) return false;
  const style = getComputedStyle(el);
  const rect = el.getBoundingClientRect();
  return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
};
const labelOf = (el) => [el.innerText, el.textContent, el.value, el.placeholder, el.title, el.getAttribute && el.getAttribute("aria-label"), el.getAttribute && el.getAttribute("name"), el.getAttribute && el.getAttribute("id")].filter(Boolean).join(" ");
const setValue = (el, value) => {
  el.focus();
  if (typeof el.select === "function") el.select();
  if (el.isContentEditable) {
    document.execCommand("selectAll", false, null);
    document.execCommand("insertText", false, value);
  } else {
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter) setter.call(el, value);
    else el.value = value;
  }
  el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
};
const candidates = [...document.querySelectorAll("input, textarea, [contenteditable='true'], [role='searchbox']")]
  .filter(visible)
  .map((el) => {
    const label = normalize(labelOf(el));
    let score = 0;
    if (label.includes("検索")) score += 200;
    if (label.includes("search")) score += 200;
    if (el.getAttribute("role") === "searchbox") score += 80;
    if (String(el.type || "").toLowerCase() === "search") score += 80;
    if (label.includes("mail") || label.includes("メール")) score += 20;
    return { el, label, score };
  })
  .filter((item) => item.score > 0)
  .sort((a, b) => b.score - a.score || a.label.length - b.label.length);
const searchBox = candidates[0]?.el;
if (!searchBox) return { ok: false, code: "SEARCH_BOX_NOT_FOUND", message: "Outlook Webでメール検索欄を見つけられませんでした。", advice: "Outlook Webのメール画面が表示されているか確認してください。" };
searchBox.scrollIntoView({ block: "center", inline: "center" });
setValue(searchBox, payload.query);
const rect = searchBox.getBoundingClientRect();
return { ok: true, query: payload.query, click: { x: Math.round(rect.left + rect.width / 2), y: Math.round(rect.top + rect.height / 2) } };
""",
    )


def build_tokuten_step_expression(year: int, month: int, config: ServiceAutomationConfig) -> str:
    hints = [*config.sender_hints, *config.subject_hints, *config.attachment_name_hints, "トクテンでんき", "請求書"]
    return _script(
        {"year": year, "month": month, "hints": hints},
        r"""
const year = String(payload.year);
const month = Number(payload.month);
const monthNoPad = String(month);
const monthPad = String(month).padStart(2, "0");
const hints = payload.hints || [];
const normalize = (value) => String(value || "").normalize("NFKC").replace(/\s+/g, " ").trim().toLowerCase();
const visible = (el) => {
  if (!el) return false;
  const style = getComputedStyle(el);
  const rect = el.getBoundingClientRect();
  return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
};
const labelOf = (el) => {
  if (!el) return "";
  const imageText = [...(el.querySelectorAll ? el.querySelectorAll("img") : [])].map((img) => [img.alt, img.title, img.getAttribute("aria-label")].filter(Boolean).join(" ")).join(" ");
  return [el.innerText, el.textContent, el.value, el.alt, el.placeholder, el.title, el.getAttribute && el.getAttribute("aria-label"), el.getAttribute && el.getAttribute("name"), el.getAttribute && el.getAttribute("id"), el.getAttribute && el.getAttribute("data-testid"), el.href, imageText].filter(Boolean).join(" ");
};
const pointOf = (el) => {
  el.scrollIntoView({ block: "center", inline: "center" });
  const rect = el.getBoundingClientRect();
  return { x: Math.round(rect.left + rect.width / 2), y: Math.round(rect.top + rect.height / 2) };
};
const attachmentPreviewPointOf = (el) => {
  el.scrollIntoView({ block: "center", inline: "center" });
  const rect = el.getBoundingClientRect();
  return { x: Math.round(rect.left + Math.min(Math.max(rect.width * 0.32, 44), Math.max(rect.width - 8, 44))), y: Math.round(rect.top + rect.height / 2) };
};
const contextOf = (el, maxDepth = 5) => {
  const values = [];
  let cursor = el;
  for (let depth = 0; cursor && depth < maxDepth; depth += 1, cursor = cursor.parentElement) values.push(labelOf(cursor));
  return values.join(" ");
};
const hasTargetMonth = (text) => {
  const value = normalize(text);
  return [year + "年" + monthNoPad + "月", year + "年" + monthPad + "月", year + "/" + monthNoPad, year + "/" + monthPad, year + "-" + monthNoPad, year + "-" + monthPad, year + monthPad].some((token) => value.includes(normalize(token)));
};
const hasTokutenHint = (text) => {
  const value = normalize(text);
  return hints.some((hint) => value.includes(normalize(hint)));
};
const controls = () => [...document.querySelectorAll("a, button, input[type='button'], input[type='submit'], [role='button'], [role='menuitem'], [tabindex]")].filter(visible);
const isDownloadText = (text) => {
  const value = normalize(text);
  return value.includes("ダウンロード") || value.includes("download");
};
const snippets = () => [...document.querySelectorAll("[role='option'], [role='listitem'], [role='row'], a, button, div, span")]
  .filter(visible)
  .map((el) => labelOf(el).replace(/\s+/g, " ").trim())
  .filter(Boolean)
  .filter((text, index, all) => all.indexOf(text) === index)
  .slice(0, 40);
const pageText = labelOf(document.body || document.documentElement);
const targetVisible = hasTargetMonth(pageText) && hasTokutenHint(pageText);
const normalizedPageText = normalize(pageText);
const noConversationSelected = normalizedPageText.includes(normalize("会話が選択されていません")) || normalizedPageText.includes(normalize("読むアイテムを選択してください")) || normalizedPageText.includes(normalize("何も選択されていません"));
const messageOpen = !noConversationSelected && targetVisible && (/\/id\//.test(location.href) || normalizedPageText.includes(normalize("宛先")) || normalizedPageText.includes(normalize("から")) || normalizedPageText.includes("kb"));
const previewDialog = [...document.querySelectorAll("[role='dialog']")]
  .filter(visible)
  .map((el) => ({ el, label: labelOf(el) }))
  .find((item) => hasTargetMonth(item.label) && normalize(item.label).includes("pdf"));
if (previewDialog) {
  const previewDownload = [...previewDialog.el.querySelectorAll("a, button, [role='button'], [role='menuitem']")]
    .filter(visible)
    .map((el) => {
      const label = labelOf(el);
      let score = 0;
      if (isDownloadText(label)) score += 500;
      if (el.matches("button, a, [role='button'], [role='menuitem']")) score += 80;
      if (hasTargetMonth(label)) score += 40;
      return { el, label, score };
    })
    .filter((item) => item.score >= 500)
    .sort((a, b) => b.score - a.score || a.label.length - b.label.length)[0];
  if (previewDownload) return { ok: false, code: "CLICK_PREVIEW_DOWNLOAD", click: pointOf(previewDownload.el), expectsDownload: true, waitMs: 1500, logs: ["トクテンでんき添付PDFプレビューのダウンロードを押します: " + previewDownload.label.trim().slice(0, 120)] };
  return { ok: false, code: "PREVIEW_DOWNLOAD_NOT_FOUND", message: year + "/" + monthPad + " の添付PDFプレビューでダウンロードボタンを見つけられませんでした。", advice: "Outlook WebのPDFプレビュー画面上部にダウンロードボタンが表示されているか確認してください。", snippets: snippets(), logs: [] };
}
if (!messageOpen) {
  const mail = [...document.querySelectorAll("[role='option'], [role='listitem'], [role='row']")]
    .filter(visible)
    .map((el) => {
      const label = labelOf(el);
      const context = contextOf(el, 4);
      const joined = label + " " + context;
      const text = normalize(joined);
      let score = 0;
      if (hasTargetMonth(joined)) score += 340;
      if (text.includes("トクテンでんき")) score += 240;
      if (text.includes("請求額確定")) score += 180;
      if (text.includes("請求書")) score += 100;
      if (text.includes("pdf")) score += 80;
      if (text.includes("履歴の候補") || text.includes("searchsuggestion")) score -= 1000;
      if (text.includes("フォルダー") || text.includes("folder") || text.includes("設定") || text.includes("settings")) score -= 300;
      return { el, label: label || context, score };
    })
    .filter((item) => item.score >= 300)
    .sort((a, b) => b.score - a.score || a.label.length - b.label.length)[0];
  if (mail) return { ok: false, code: "CLICK_MAIL", click: pointOf(mail.el), expectsDownload: false, waitMs: 3000, logs: ["トクテンでんきの対象メールを開きます: " + mail.label.trim().slice(0, 120)] };
}
const menuDownload = controls()
  .map((el) => {
    const label = labelOf(el);
    const context = contextOf(el, 5);
    let score = 0;
    if (isDownloadText(label) && (targetVisible || hasTargetMonth(context))) score += 560;
    if (isDownloadText(label) && hasTokutenHint(context)) score += 120;
    if (hasTargetMonth(context)) score += 80;
    return { el, label, score };
  })
  .filter((item) => item.score >= 300)
  .sort((a, b) => b.score - a.score || a.label.length - b.label.length)[0];
if (menuDownload) return { ok: false, code: "CLICK_DOWNLOAD", click: pointOf(menuDownload.el), expectsDownload: true, waitMs: 1500, logs: ["トクテンでんき添付PDFのダウンロード操作を押します: " + menuDownload.label.trim().slice(0, 120)] };
const attachment = [...document.querySelectorAll("[role='option'], a, button, [role='button'], [role='listitem'], div, span")]
  .filter(visible)
  .map((el) => {
    const label = labelOf(el);
    const context = contextOf(el, 5);
    const selfText = normalize(label);
    let score = 0;
    if (hasTargetMonth(label)) score += 420;
    if (hasTokutenHint(label)) score += 260;
    if (selfText.includes("pdf")) score += 180;
    if (selfText.includes("請求書")) score += 120;
    if (el.matches("a, button, [role='button']")) score += 30;
    if (!hasTargetMonth(label) || !selfText.includes("pdf")) score -= 700;
    if (selfText.includes("未開封") || selfText.includes("開封済み")) score -= 1000;
    if (normalize(context).length > 1300) score -= 700;
    return { el, label: label || context, score };
  })
  .filter((item) => item.score >= 300)
  .sort((a, b) => b.score - a.score || a.label.length - b.label.length)[0];
if (attachment) return { ok: false, code: "CLICK_ATTACHMENT_PREVIEW", click: attachmentPreviewPointOf(attachment.el), expectsDownload: false, waitMs: 3000, logs: ["トクテンでんきの添付PDFを開きます: " + attachment.label.trim().slice(0, 120)] };
if (hasTokutenHint(pageText)) return { ok: false, code: "ATTACHMENT_NOT_FOUND", message: year + "/" + monthPad + " のトクテンでんき添付PDFをメール画面上で見つけられませんでした。", advice: "対象メールは開けていますが、対象年月が含まれる添付PDFが見つかりません。", snippets: snippets(), logs: [] };
return { ok: false, code: "MAIL_NOT_FOUND", message: year + "/" + monthPad + " のトクテンでんき請求メールをOutlook Webの検索結果から見つけられませんでした。", advice: "Outlook Webで対象年月のトクテンでんき請求メールが存在するか確認してください。", snippets: snippets(), logs: [] };
""",
    )


def build_webbilling_auto_login_expression(credentials: dict[str, str]) -> str:
    return _script(
        {
            "dAccountId": (
                credentials.get("dAccountId")
                or credentials.get("d_account_id")
                or credentials.get("login_id")
                or credentials.get("email")
                or credentials.get("id")
                or ""
            ),
            "password": credentials.get("password") or "",
            # The portal offers its own Web billing ID form and a d-account
            # button on the same page. Which one is correct depends on which
            # identity the owner registered, so carry that decision here
            # rather than guessing from the page.
            "prefersDAccount": bool(
                credentials.get("dAccountId")
                or credentials.get("d_account_id")
                or "@" in str(credentials.get("login_id") or credentials.get("id") or "")
            ),
        },
        r"""
const normalize = (value) => String(value || "").normalize("NFKC").replace(/\s+/g, " ").trim().toLowerCase();
const visible = (el) => {
  if (!el || el.disabled) return false;
  const style = getComputedStyle(el);
  const rect = el.getBoundingClientRect();
  return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
};
const labelOf = (el) => [el.innerText, el.textContent, el.value, el.alt, el.title, el.placeholder, el.getAttribute && el.getAttribute("aria-label"), el.getAttribute && el.getAttribute("name"), el.getAttribute && el.getAttribute("id")].filter(Boolean).join(" ");
const contextOf = (el, depth = 3) => {
  const parts = [];
  let node = el;
  for (let i = 0; node && i < depth; i += 1, node = node.parentElement) parts.push(labelOf(node));
  return parts.join(" ");
};
const controls = () => [...document.querySelectorAll("button, input, a, [role='button'], [onclick], [tabindex]")].filter(visible);
const pointOf = (el) => {
  el.scrollIntoView({ block: "center", inline: "center" });
  const rect = el.getBoundingClientRect();
  return { x: Math.round(rect.left + rect.width / 2), y: Math.round(rect.top + rect.height / 2) };
};
const setValue = (el, value) => {
  el.focus();
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  if (setter) setter.call(el, value);
  else el.value = value;
  el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
};
const scoreControl = (el, keywords, excludes = []) => {
  const text = normalize(labelOf(el) + " " + contextOf(el, 2));
  if (!text || excludes.some((word) => text.includes(normalize(word)))) return null;
  let score = 0;
  for (const word of keywords) {
    const key = normalize(word);
    if (text === key) score += 500;
    else if (text.includes(key)) score += 250;
  }
  if (String(el.type || "").toLowerCase() === "submit") score += 30;
  return score > 0 ? { el, text, score } : null;
};
const bestControl = (keywords, excludes = [], predicate = () => true) => controls().filter(predicate).map((el) => scoreControl(el, keywords, excludes)).filter(Boolean).sort((a, b) => b.score - a.score || a.text.length - b.text.length)[0];
const pageText = normalize(document.body?.innerText || "");
const captchaPresent = Boolean(document.querySelector("iframe[src*='recaptcha'], iframe[src*='hcaptcha'], [data-sitekey], .g-recaptcha, .h-captcha")) || ["captcha", "recaptcha", "hcaptcha", "画像認証", "ロボットではありません"].some((word) => pageText.includes(normalize(word)));
if (captchaPresent) return { attempted: false, code: "SECURITY_CHALLENGE", challengeKind: "captcha", reason: "CAPTCHAが表示されています。" };
const passkeyWords = ["passkey", "パスキー", "webauthn", "セキュリティキー", "生体認証"];
if (passkeyWords.some((word) => pageText.includes(normalize(word)))) return { attempted: false, code: "SECURITY_CHALLENGE", challengeKind: "passkey_unavailable", reason: "遠隔ワーカー非対応のパスキー認証が表示されています。" };
const securityWords = ["セキュリティコード", "確認コード", "認証コード", "ワンタイム", "2段階", "二段階", "本人確認", "verification code"];
const codeHints = ["ワンタイムパスワード", "セキュリティコード", "確認コード", "認証コード", "one-time password", "verification code", "otp"];
const codeInputs = [...document.querySelectorAll("input")]
  .filter(visible)
  .filter((input) => ["text", "tel", "number", "password"].includes(String(input.type || "text").toLowerCase()))
  .filter((input) => codeHints.some((word) => normalize(labelOf(input) + " " + contextOf(input, 4)).includes(normalize(word))));
const exactSecurityOrigin = location.protocol === "https:" && ["webbilling.ntt-finance.co.jp", "id.smt.docomo.ne.jp", "cfg.smt.docomo.ne.jp"].includes(location.hostname);
// The d-account code page is six one-character boxes with no labels, so it
// matches neither the single-field test above nor any code wording. Left
// unrecognised it was reported as some other kind of check, and the owner was
// never offered the box to type the code they had just been sent.
const splitCodeBoxes = [...document.querySelectorAll("input")]
  .filter(visible)
  .filter((input) => ["text", "tel", "number", "password"].includes(String(input.type || "text").toLowerCase()))
  .filter((input) => Number(input.getAttribute("maxlength") || 0) === 1);
const codeFieldPresent = codeInputs.length === 1 || (splitCodeBoxes.length >= 4 && splitCodeBoxes.length <= 8);
if (codeFieldPresent && exactSecurityOrigin) return { attempted: false, waitingForSecurityCode: true, code: "WAIT_SECURITY_CODE", challengeKind: "verification_code", splitCount: splitCodeBoxes.length, reason: "セキュリティコード入力待ちです。" };
if (securityWords.some((word) => pageText.includes(normalize(word)))) {
  // On the provider's own security page this wording means a code is being
  // asked for, even when the field has not rendered yet. Anywhere else it is
  // something the owner has to deal with by hand.
  const kind = exactSecurityOrigin ? "verification_code" : "interactive";
  return { attempted: false, waitingForSecurityCode: exactSecurityOrigin, code: exactSecurityOrigin ? "WAIT_SECURITY_CODE" : "SECURITY_CHALLENGE", challengeKind: kind, reason: "追加認証が表示されています。" };
}
const passwordInput = [...document.querySelectorAll("input[type='password']")].find(visible);
// The portal shows its own ID/password form and a d-account entry on the
// same page, and that entry is a zero-sized link inside a collapsed block,
// so it can be neither seen nor clicked. Go to its destination directly.
const dAccountEntry = [...document.querySelectorAll("a[href]")]
  .map((el) => String(el.getAttribute("href") || ""))
  .find((href) => /a0105|logindcm/i.test(href));
if (payload.prefersDAccount && dAccountEntry && location.hostname === "webbilling.ntt-finance.co.jp" && !/a0105|logindcm/i.test(location.pathname)) {
  return { attempted: true, code: "OPEN_D_ACCOUNT_LOGIN", directUrl: new URL(dAccountEntry, location.href).href, reason: "dアカウントのログイン画面へ移動します。" };
}
const dAccountLogin = bestControl(["dアカウントログイン", "dアカウントでログイン", "dアカウント", "d account"], ["新規", "作成", "登録", "お忘れ", "戻る", "キャンセル"]);
// The Web billing top page shows its own ID/password form next to the
// d-account button. Typing a d-account address into that form can never
// succeed, so take the d-account route whenever that is the owner's identity.
const onDAccountHost = /(^|\.)smt\.docomo\.ne\.jp$/.test(location.hostname);
if (dAccountLogin && (payload.prefersDAccount || !passwordInput) && !onDAccountHost) {
  return { attempted: true, code: "CLICK_D_ACCOUNT_LOGIN", click: pointOf(dAccountLogin.el) };
}
if (passwordInput) {
  if (!payload.password && !String(passwordInput.value || "").trim()) return { attempted: false, code: "PASSWORD_NOT_CONFIGURED", reason: "パスワードが未入力です。" };
  if (payload.password) setValue(passwordInput, payload.password);
  const loginButton = bestControl(["ログイン", "login"], ["戻る", "キャンセル", "お忘れ", "新規", "登録", "表示"]);
  if (!loginButton) return { attempted: false, code: "LOGIN_BUTTON_NOT_FOUND", reason: "ログインボタンを見つけられませんでした。" };
  return { attempted: true, code: "SUBMIT_PASSWORD", click: pointOf(loginButton.el) };
}
const textInputs = [...document.querySelectorAll("input")]
  .filter(visible)
  .filter((input) => ["text", "email", "tel", "search"].includes(String(input.type || "text").toLowerCase()));
const idInput = textInputs
  .map((input) => {
    const text = normalize(labelOf(input) + " " + contextOf(input, 4));
    let score = 0;
    if (text.includes(normalize("dアカウントID"))) score += 400;
    if (text.includes(normalize("アカウントID"))) score += 300;
    if (text.includes(normalize("ログインID"))) score += 200;
    if (text.includes("mail") || text.includes(normalize("メール"))) score += 80;
    if (String(input.type || "").toLowerCase() === "email") score += 80;
    if (text.includes(normalize("検索"))) score -= 250;
    return { input, score };
  })
  .filter((item) => item.score > 0)
  .sort((a, b) => b.score - a.score)[0]?.input || textInputs[0];
if (idInput) {
  if (!payload.dAccountId && !String(idInput.value || "").trim()) return { attempted: false, code: "D_ACCOUNT_ID_NOT_CONFIGURED", reason: "dアカウントIDが未入力です。" };
  if (payload.dAccountId) setValue(idInput, payload.dAccountId);
  const nextButton = bestControl(["次へ", "next"], ["戻る", "キャンセル", "お忘れ", "登録", "新規"]);
  if (!nextButton) return { attempted: false, code: "NEXT_BUTTON_NOT_READY", reason: "次へボタンの有効化を待っています。" };
  return { attempted: true, code: "SUBMIT_D_ACCOUNT_ID", click: pointOf(nextButton.el) };
}
return { attempted: false, code: "LOGIN_STEP_NOT_FOUND", reason: "自動で進められるWebビリング/dアカウントのログイン操作を見つけられませんでした。" };
""",
    )


def build_webbilling_step_expression(year: int, month: int) -> str:
    return _script(
        {"year": year, "month": month},
        r"""
const targetYear = String(payload.year);
const targetMonth = Number(payload.month);
const monthNoPad = String(targetMonth);
const monthPad = String(targetMonth).padStart(2, "0");
const normalize = (value) => String(value || "").normalize("NFKC").replace(/\s+/g, " ").trim().toLowerCase();
const compact = (value) => normalize(value).replace(/\s+/g, "");
const visible = (el) => {
  if (!el || el.disabled) return false;
  const style = getComputedStyle(el);
  const rect = el.getBoundingClientRect();
  return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
};
const labelOf = (el) => {
  if (!el) return "";
  const imageText = [...(el.querySelectorAll ? el.querySelectorAll("img") : [])].map((img) => [img.alt, img.title, img.getAttribute("aria-label")].filter(Boolean).join(" ")).join(" ");
  return [el.innerText, el.textContent, el.value, el.alt, el.title, el.placeholder, el.getAttribute && el.getAttribute("aria-label"), el.getAttribute && el.getAttribute("name"), el.getAttribute && el.getAttribute("id"), el.href, imageText].filter(Boolean).join(" ");
};
const pointOf = (el) => {
  el.scrollIntoView({ block: "center", inline: "center" });
  const rect = el.getBoundingClientRect();
  return { x: Math.round(rect.left + rect.width / 2), y: Math.round(rect.top + rect.height / 2) };
};
const contextOf = (el, maxDepth = 8) => {
  const values = [];
  let cursor = el;
  for (let depth = 0; cursor && depth < maxDepth; depth += 1, cursor = cursor.parentElement) values.push(labelOf(cursor));
  return values.join(" ");
};
const controls = () => [...document.querySelectorAll("a, button, input[type='button'], input[type='submit'], input[type='image'], [role='button'], [onclick], [tabindex]")].filter(visible);
const disabledLike = (el) => Boolean(el?.disabled) || String(el?.getAttribute?.("aria-disabled") || "").toLowerCase() === "true" || String(el?.className || "").toLowerCase().includes("disabled");
const bestControl = (keywords, excludes = []) => controls()
  .map((el) => {
    const label = labelOf(el);
    const text = normalize(label);
    const context = normalize(contextOf(el, 4));
    let score = 0;
    for (const word of keywords) {
      const key = normalize(word);
      if (text.includes(key)) score += 160 + key.length;
      if (context.includes(key)) score += 40;
    }
    for (const word of excludes) {
      const key = normalize(word);
      if (text.includes(key) || context.includes(key)) score -= 220;
    }
    if (disabledLike(el)) score -= 500;
    return { el, label, score };
  })
  .filter((item) => item.score > 90)
  .sort((a, b) => b.score - a.score || a.label.length - b.label.length)[0] || null;
const certificateDownloadControl = () => [...document.querySelectorAll("#btnDl, .btn-item-download, a[href$='#modal']")]
  .filter(visible)
  .map((el) => {
    const label = labelOf(el);
    const className = String(el.className || "");
    let score = 0;
    if (el.id === "btnDl") score += 500;
    if (className.includes("btn-item-download")) score += 300;
    if (className.includes("btn-item-pdf")) score += 120;
    if (String(el.href || "").endsWith("#modal")) score += 80;
    if (disabledLike(el)) score -= 1000;
    return { el, label, score };
  })
  .filter((item) => item.score > 0)
  .sort((a, b) => b.score - a.score || a.label.length - b.label.length)[0] || null;
const hasTargetMonth = (text) => {
  const value = compact(text);
  return value.includes(targetYear + "年" + monthNoPad + "月分") || value.includes(targetYear + "年" + monthPad + "月分") || value.includes(targetYear + "/" + monthNoPad) || value.includes(targetYear + "/" + monthPad) || value.includes(targetYear + "-" + monthNoPad) || value.includes(targetYear + "-" + monthPad);
};
const collectAvailableMonths = () => {
  const values = new Set();
  const source = document.body?.innerText || "";
  for (const match of source.matchAll(/(20\d{2})\s*年\s*(\d{1,2})\s*月\s*分/g)) values.add(match[1] + "/" + String(Number(match[2])).padStart(2, "0"));
  return [...values].slice(0, 80);
};
const extractMetadataText = (text) => {
  const payment = /((?:19|20)\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日/.exec(text);
  const amount = /([0-9０-９][0-9０-９,，\s]*)\s*円/.exec(text);
  const paymentText = payment ? "支払日 " + payment[1] + "年" + payment[2] + "月" + payment[3] + "日" : "";
  const amountText = amount ? "ご請求額 " + amount[1].replace(/\s+/g, "") + "円" : "";
  return ["請求年月 " + targetYear + "年" + monthNoPad + "月分", paymentText, amountText, text].filter(Boolean).join(" ");
};
const checkedLike = (el) => {
  if (!el) return false;
  const ariaChecked = String(el.getAttribute && el.getAttribute("aria-checked") || "").toLowerCase();
  const className = String(el.className || "").toLowerCase();
  return Boolean(el.checked) || ariaChecked === "true" || className.includes("checked") || className.includes("selected") || className.includes("active");
};
const pageText = document.body?.innerText || "";
const normalizedPageText = normalize(pageText);
const logs = [];
const finalDownload = bestControl(["ダウンロードする"], ["戻る", "キャンセル", "閉じる"]);
if (finalDownload && normalizedPageText.includes(normalize("ダウンロード"))) return { ok: false, code: "CLICK_FINAL_DOWNLOAD", click: pointOf(finalDownload.el), expectsDownload: true, waitMs: 1200, logs: ["Webビリングの最終ダウンロードを押します: " + finalDownload.label.trim().slice(0, 120)] };
if (normalizedPageText.includes(normalize("上記の注意事項に同意します")) || (normalizedPageText.includes(normalize("注意事項")) && normalizedPageText.includes(normalize("同意")))) {
  const consentCheckbox = [...document.querySelectorAll("input[type='checkbox']")]
    .filter((el) => !el.disabled)
    .map((el) => ({ el, text: normalize(labelOf(el) + " " + contextOf(el, 5)) }))
    .filter((item) => item.text.includes(normalize("同意")) || item.text.includes(normalize("上記の注意事項")))
    .sort((a, b) => a.text.length - b.text.length)[0]?.el;
  if (consentCheckbox && !consentCheckbox.checked) {
    const target = consentCheckbox.closest("label")?.querySelector(".checkbox-parts") || consentCheckbox.closest("label") || consentCheckbox;
    return { ok: false, code: "CLICK_CONSENT", click: pointOf(target), waitMs: 800, logs: ["Webビリングの注意事項同意チェックを入れます。"] };
  }
  const download = certificateDownloadControl() || bestControl(["ダウンロード"], ["戻る", "キャンセル", "閉じる"]);
  if (download) return { ok: false, code: "CLICK_DOWNLOAD", click: pointOf(download.el), mayDownload: true, waitMs: 1000, logs: ["Webビリングのダウンロードを押します: " + download.label.trim().slice(0, 120)] };
  return { ok: false, code: "DOWNLOAD_BUTTON_NOT_FOUND", message: "Webビリングのダウンロードボタンを見つけられませんでした。", advice: "注意事項同意後の画面でダウンロードボタンが表示されているか確認してください。", logs };
}
const onCertificateList = normalizedPageText.includes(normalize("請求年月")) && (normalizedPageText.includes(normalize("支払年月日")) || normalizedPageText.includes(normalize("支払/ご利用金額")));
if (onCertificateList) {
  const rowElements = [...document.querySelectorAll("tr"), ...document.querySelectorAll("li, section, article, div")];
  const rows = [...new Set(rowElements)]
    .filter(visible)
    .map((row) => {
      const text = labelOf(row);
      const normalized = normalize(text);
      let score = 0;
      if (row.tagName === "TR") score += 300;
      if (hasTargetMonth(text)) score += 500;
      if (normalized.includes(normalize("ＮＴＴドコモ")) || normalized.includes(normalize("NTTドコモ"))) score += 80;
      if (normalized.includes(normalize("支払年月日"))) score += 40;
      if (normalized.includes(normalize("お客様住所"))) score -= 600;
      if (normalized.includes(normalize("全選択")) || normalized.includes(normalize("全解除"))) score -= 250;
      return { row, text, score };
    })
    .filter((item) => item.score >= 500)
    .sort((a, b) => b.score - a.score || a.text.length - b.text.length);
  const target = rows[0];
  if (!target) {
    const scroller = document.scrollingElement || document.documentElement;
    if (scroller && scroller.scrollTop + window.innerHeight < scroller.scrollHeight - 24) {
      window.scrollBy({ top: Math.round(window.innerHeight * 0.7), left: 0, behavior: "instant" });
      return { ok: false, code: "SCROLL_CERTIFICATE_LIST", continue: true, waitMs: 700, logs: ["Webビリングの証明書一覧を下へスクロールして対象月を探します。"] };
    }
    const availableMonths = collectAvailableMonths();
    return { ok: false, code: "YEAR_MONTH_NOT_AVAILABLE", message: targetYear + "/" + monthPad + " のWebビリング証明書行を見つけられませんでした。", advice: availableMonths.length ? "確認できた請求年月: " + availableMonths.join(" / ") : "証明書データ一覧に対象月が表示されているか確認してください。", availableMonths, logs };
  }
  const metadataText = extractMetadataText(target.text);
  const checkbox = target.row.querySelector("input[type='checkbox']");
  const next = bestControl(["次へ"], ["戻る", "キャンセル", "ログアウト"]);
  if (next) return { ok: false, code: "CLICK_NEXT", click: pointOf(next.el), waitMs: 1400, metadataText, logs: ["Webビリングの次へを押します。"] };
  const selected = checkedLike(checkbox) || checkedLike(target.row);
  if (checkbox && !selected) return { ok: false, code: "CLICK_TARGET_CHECKBOX", click: pointOf(checkbox.closest("label") || checkbox), waitMs: 900, metadataText, logs: ["Webビリングの対象請求年月にチェックを入れます: " + target.text.trim().replace(/\s+/g, " ").slice(0, 140)] };
  const checkboxControl = !checkbox && [...target.row.querySelectorAll("[role='checkbox'], label, button, [onclick], [tabindex]")]
    .filter(visible)
    .map((el) => ({ el, score: normalize(labelOf(el) + " " + contextOf(el, 4)).includes("checkbox") || normalize(labelOf(el) + " " + contextOf(el, 4)).includes(normalize("チェック")) ? 200 : 0 }))
    .filter((item) => item.score > 0)
    .sort((a, b) => b.score - a.score)[0]?.el;
  if (checkboxControl) return { ok: false, code: "CLICK_TARGET_CHECKBOX", click: pointOf(checkboxControl), waitMs: 900, metadataText, logs: ["Webビリングの対象請求年月にチェックを入れます。"] };
  return { ok: false, code: "NEXT_BUTTON_NOT_FOUND", message: "Webビリングの対象月チェック後も次へボタンが有効になりませんでした。", advice: "対象月行のチェック状態が反映されているか確認してください。", metadataText, logs };
}
const certificateMenu = bestControl(["料金支払証明書", "ご利用料金証明書"], ["適格", "インボイス", "ログアウト"]);
if (certificateMenu) return { ok: false, code: "CLICK_CERTIFICATE_MENU", click: pointOf(certificateMenu.el), waitMs: 1600, logs: ["料金支払証明書・ご利用料金証明書を開きます: " + certificateMenu.label.trim().slice(0, 120)] };
const search = bestControl(["検索"], ["ログアウト"]);
if (String(location.href).includes("/mem/c0301/") && search) return { ok: false, code: "CLICK_SEARCH", click: pointOf(search.el), waitMs: 1000, logs: ["Webビリングの証明書検索を押します。"] };
return { ok: false, code: "CERTIFICATE_MENU_NOT_FOUND", message: "Webビリングで料金支払証明書・ご利用料金証明書を見つけられませんでした。", advice: "Webビリングにログイン後、左メニューに証明書メニューが表示されているか確認してください。", visibleControls: controls().slice(0, 50).map((el) => labelOf(el).trim().slice(0, 120)).filter(Boolean), logs };
""",
    )
