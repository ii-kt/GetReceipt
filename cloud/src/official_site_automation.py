from __future__ import annotations

import json
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .browser_session import ManagedBrowser
from .config import DATA_DIR, parse_month_key, service_by_id, shift_month
from .document_metadata import extract_pdf_text
from .epos_automation import AcquisitionError, FetchedStatement


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
        target_url="https://mypage.commufa.jp/join/s/",
        partner_name="中部テレコミュニケーション株式会社",
        login_hints=("Myコミュファログイン", "ログインID", "メールアドレス", "パスワード", "ログイン"),
        logged_in_hints=("ログアウト", "ご契約内容", "ご請求額", "契約内容・ご請求額", "過去の請求額"),
    ),
    "tokuten": ServiceAutomationConfig(
        target_url="https://outlook.live.com/mail/0/",
        partner_name="フラットエナジー株式会社",
        mail_search_query_template="トクテン {year}年{month}月",
        sender_hints=("besender-s.jp", "トクテンでんき 総合サポートセンター"),
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


def target_lookup_month(service_id: str, target_month: str) -> str:
    year, month = parse_month_key(target_month)
    if service_id == "tokuten":
        year, month = shift_month(year, month, 1)
    return f"{year:04d}-{month:02d}"


def build_tokuten_search_query(target_month: str, config: ServiceAutomationConfig | None = None) -> str:
    config = config or SERVICE_AUTOMATION_CONFIGS["tokuten"]
    year, month = parse_month_key(target_lookup_month("tokuten", target_month))
    return (
        config.mail_search_query_template
        .replace("{year}", str(year))
        .replace("{month}", str(month))
        .replace("{month2}", f"{month:02d}")
    )


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))).strip().lower()


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
    return AcquisitionError(
        action.get("message") or fallback_message,
        code=action.get("code") or fallback_code,
        advice=action.get("advice") or "取得用ブラウザで対象月の明細が表示されているか確認してください。",
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


class CommufaAutoFetcher:
    def __init__(self, browser: ManagedBrowser) -> None:
        self.browser = browser
        self.service = service_by_id("commufa")
        self.config = SERVICE_AUTOMATION_CONFIGS["commufa"]

    def open_portal(self) -> dict[str, Any]:
        self.browser.navigate(self.config.target_url, wait_seconds=1.5)
        return self.browser.page_summary()

    def fetch_pdf(self, target_month: str) -> FetchedStatement:
        year, month = parse_month_key(target_month)
        self.browser.clear_downloads()
        self.browser.navigate(self.config.target_url, wait_seconds=1.5)
        summary = self.browser.page_summary()
        if classify_configured_login_state(summary, self.config) == "login-required":
            raise AcquisitionError(
                "コミュファへのログインが必要です。",
                code="LOGIN_REQUIRED",
                advice="取得用ブラウザでMyコミュファにログインしてから、もう一度取得してください。",
            )

        metadata_texts: list[str] = []
        logs: list[str] = []
        action: dict[str, Any] = {}
        for _ in range(12):
            action = self.browser.evaluate(build_commufa_step_expression(year, month), timeout=30) or {}
            logs.extend(str(line) for line in action.get("logs") or [])
            if action.get("metadataText"):
                metadata_texts.append(str(action["metadataText"]))
            if action.get("ok"):
                break
            if action.get("directUrl"):
                self.browser.navigate(str(action["directUrl"]), wait_seconds=min(float(action.get("waitMs") or 2500) / 1000, 2.5))
                continue
            if action.get("click"):
                click = action["click"]
                self.browser.click_at(int(click["x"]), int(click["y"]))
                time.sleep(min(float(action.get("waitMs") or 1200) / 1000, 3.5))
                continue
            raise _action_error(action, "コミュファ明細の取得操作を進められませんでした。", "COMMUFA_ACTION_NOT_FOUND")
        else:
            raise AcquisitionError(
                "コミュファ明細の取得操作が完了しませんでした。",
                code="COMMUFA_ACTION_TIMEOUT",
                advice="取得用ブラウザで対象月の利用明細または印刷用ページが表示されているか確認してください。",
            )

        if not action.get("ok"):
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
        pdf_path = self.browser.print_to_pdf(DATA_DIR / "browser-downloads-commufa" / f"commufa-{target_month}-{int(time.time())}.pdf")
        content = _downloaded_pdf_content(pdf_path, self.service.label)
        assert_commufa_usage_month(content, target_month)
        return FetchedStatement(
            content=content,
            source_url=str(summary_before_print.get("url") or self.config.target_url),
            original_file_name=f"commufa_{target_month}.pdf",
            metadata_text=" ".join(metadata_texts),
            logs=tuple(logs),
        )


class TokutenAutoFetcher:
    def __init__(self, browser: ManagedBrowser) -> None:
        self.browser = browser
        self.service = service_by_id("tokuten")
        self.config = SERVICE_AUTOMATION_CONFIGS["tokuten"]

    def open_portal(self) -> dict[str, Any]:
        self.browser.navigate(self.config.target_url, wait_seconds=2.0)
        return self.browser.page_summary()

    def fetch_pdf(self, target_month: str) -> FetchedStatement:
        lookup_month = target_lookup_month("tokuten", target_month)
        year, month = parse_month_key(lookup_month)
        self.browser.clear_downloads()
        self.browser.navigate(self.config.target_url, wait_seconds=2.0)
        self._wait_for_mailbox()
        self._search_mail(target_month)

        logs: list[str] = []
        metadata_texts: list[str] = [build_tokuten_search_query(target_month, self.config)]
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
            if state == "login-required":
                result = self.browser.evaluate(build_microsoft_auto_login_expression(), timeout=15) or {}
                if result.get("attempted") and result.get("click"):
                    click = result["click"]
                    self.browser.click_at(int(click["x"]), int(click["y"]))
                    time.sleep(1.0)
                    continue
            time.sleep(1.0)
        raise AcquisitionError(
            "Outlook Webのログイン完了を検出できませんでした。",
            code="LOGIN_REQUIRED" if last_state == "login-required" else "MAILBOX_NOT_READY",
            advice="取得用ブラウザでOutlook WebのMicrosoftログインを完了してから、もう一度取得してください。",
        )

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

    def open_portal(self) -> dict[str, Any]:
        self.browser.navigate(self.config.target_url, wait_seconds=1.5)
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
        return FetchedStatement(
            content=content,
            source_url=str(summary.get("url") or self.config.target_url),
            original_file_name=downloaded.name,
            metadata_text=" ".join(metadata_texts),
            logs=tuple(logs),
        )

    def _wait_for_login(self, timeout_seconds: float = 120) -> None:
        deadline = time.time() + timeout_seconds
        last_state = "unknown"
        while time.time() < deadline:
            summary = self.browser.page_summary()
            state = classify_configured_login_state(summary, self.config)
            last_state = state
            if state == "logged-in":
                return
            auto_login = self.browser.evaluate(build_webbilling_auto_login_expression(self.credentials), timeout=15) or {}
            if auto_login.get("attempted") and auto_login.get("click"):
                click = auto_login["click"]
                self.browser.click_at(int(click["x"]), int(click["y"]))
                time.sleep(1.2)
                continue
            time.sleep(1.0)
        raise AcquisitionError(
            "Webビリングのログイン完了を検知できませんでした。",
            code="LOGIN_REQUIRED" if last_state == "login-required" else "LOGIN_TIMEOUT",
            advice="取得用ブラウザでWebビリングまたはdアカウントの認証を完了してから、もう一度取得してください。",
        )


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
    const match = labelOf(el).replace(/\s+/g, " ").match(/(20\d{2})\s*年\s*(\d{1,2})\s*月/);
    if (match) values.add(match[1] + "/" + String(Number(match[2])).padStart(2, "0"));
  }
  return [...values].slice(0, 40);
};
const text = pageText();
if (text.includes(normalize("利用料金のお知らせ")) && hasTargetMonth(text)) {
  const print = bestControl(["印刷用ページ"], ["ログアウト"]);
  if (print) return { ok: false, code: "CLICK_PRINT_PAGE", click: pointOf(print.el), waitMs: 2400, logs: ["印刷用ページを開きます: " + print.label.trim().slice(0, 120)] };
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
    if (usage) return { ok: false, code: "CLICK_USAGE_DETAIL", click: pointOf(usage.el), waitMs: 3000, logs: ["対象ご利用年月の利用明細を開きます: " + row.text.trim().slice(0, 120)] };
  }
  const availableMonths = collectMonths();
  return { ok: false, code: "YEAR_MONTH_NOT_AVAILABLE", message: targetYear + "/" + monthPad + " のご利用年月に対応する利用明細を見つけられませんでした。", advice: availableMonths.length ? "確認できた年月候補: " + availableMonths.join(" / ") : "過去の請求額一覧に対象年月の行が表示されているか確認してください。", availableMonths, logs: [] };
}
if (text.includes(normalize("ご利用料金・契約内容のご確認")) || text.includes(normalize("ご契約・料金トップ"))) {
  const past = bestControl(["過去の請求額の一覧"], ["ログアウト"]);
  if (past) return { ok: false, code: "CLICK_PAST_BILL_LIST", click: pointOf(past.el), waitMs: 3000, logs: ["過去の請求額一覧を開きます: " + past.label.trim().slice(0, 120)] };
}
const direct = [...document.querySelectorAll("a")].filter(visible).find((el) => String(el.href || "").includes("COM_RedirectPage") && String(el.href || "").includes("ApplicationTop"));
if (direct) return { ok: false, code: "NAVIGATE_TO_BILLING_TOP", directUrl: direct.href, waitMs: 3000, logs: ["請求確認画面への直接遷移を検出: " + direct.href] };
const entry = bestControl(["ご契約内容・ご請求額の確認", "ご利用料金の確認", "詳しくはこちら"], ["netflix", "youtube", "hulu", "ログアウト", "詳細はこちら"]);
if (entry) return { ok: false, code: "CLICK_BILLING_ENTRY", click: pointOf(entry.el), waitMs: 3500, logs: ["請求確認画面を開きます: " + entry.label.trim().slice(0, 120)] };
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


def build_microsoft_auto_login_expression() -> str:
    return r"""(() => {
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
const controls = () => [...document.querySelectorAll("button, input[type='button'], input[type='submit'], a, [role='button']")].filter(visible);
const byText = (words, excludes = []) => controls()
  .map((el) => ({ el, text: normalize(labelOf(el)) }))
  .filter((item) => words.some((word) => item.text.includes(normalize(word))))
  .filter((item) => excludes.every((word) => !item.text.includes(normalize(word))))
  .sort((a, b) => a.text.length - b.text.length)[0]?.el || null;
const submit = document.querySelector("#idSIButton9, input[type='submit']");
const staySignedIn = byText(["はい", "yes", "続行", "continue", "サインインの状態を維持"], ["いいえ", "no"]);
if (staySignedIn) return { attempted: true, action: "stay-signed-in", click: pointOf(staySignedIn) };
const passwordInput = [...document.querySelectorAll("input[type='password']")].find(visible);
if (passwordInput) {
  if (!String(passwordInput.value || "").trim()) return { attempted: false, reason: "Microsoftログイン画面のパスワード欄が未入力です。" };
  const button = (submit && visible(submit)) ? submit : byText(["サインイン", "sign in", "ログイン", "login", "次へ", "next"]);
  if (!button) return { attempted: false, reason: "Microsoftログイン画面の送信ボタンを見つけられませんでした。" };
  return { attempted: true, action: "submit-password", click: pointOf(button) };
}
const accountInput = [...document.querySelectorAll("input[type='email'], input[name='loginfmt'], input[type='text']")]
  .filter(visible)
  .find((input) => {
    const label = normalize(labelOf(input));
    return label.includes("メール") || label.includes("email") || label.includes("account") || label.includes("login");
  });
if (accountInput) {
  if (!String(accountInput.value || "").trim()) return { attempted: false, reason: "Microsoftログイン画面のアカウント欄が未入力です。" };
  const button = (submit && visible(submit)) ? submit : byText(["次へ", "next", "続行", "continue"]);
  if (!button) return { attempted: false, reason: "Microsoftログイン画面の次へボタンを見つけられませんでした。" };
  return { attempted: true, action: "submit-account", click: pointOf(button) };
}
return { attempted: false, reason: "自動で押せるMicrosoftログイン操作は見つかりませんでした。" };
})()"""


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
        {"dAccountId": credentials.get("dAccountId") or credentials.get("id") or "", "password": credentials.get("password") or ""},
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
const securityWords = ["セキュリティコード", "確認コード", "認証コード", "ワンタイム", "2段階", "二段階", "本人確認", "verification code"];
if (securityWords.some((word) => pageText.includes(normalize(word)))) return { attempted: false, waitingForSecurityCode: true, code: "WAIT_SECURITY_CODE", reason: "セキュリティコード入力待ちです。" };
const passwordInput = [...document.querySelectorAll("input[type='password']")].find(visible);
const dAccountLogin = bestControl(["dアカウントログイン", "dアカウントでログイン", "dアカウント", "d account"], ["新規", "作成", "登録", "お忘れ", "戻る", "キャンセル"]);
if (dAccountLogin && !passwordInput) return { attempted: true, code: "CLICK_D_ACCOUNT_LOGIN", click: pointOf(dAccountLogin.el) };
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
