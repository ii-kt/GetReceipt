from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

from .browser_session import BrowserAutomationError, ManagedBrowser
from .config import parse_month_key, service_by_id


class AcquisitionError(RuntimeError):
    def __init__(self, message: str, *, code: str = "ACQUISITION_ERROR", advice: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.advice = advice


@dataclass(frozen=True)
class FetchedStatement:
    content: bytes
    source_url: str
    original_file_name: str
    metadata_text: str
    logs: tuple[str, ...]


def _normalize(value: str) -> str:
    return " ".join(str(value or "").lower().split())


def classify_login_state(summary: dict[str, Any]) -> str:
    text = _normalize(f"{summary.get('title', '')} {summary.get('url', '')} {summary.get('text', '')}")
    if int(summary.get("passwordFields") or 0) > 0 or "login" in text or "ログイン" in text:
        return "login-required"
    if "お支払明細書照会" in summary.get("text", "") or "ログアウト" in summary.get("text", ""):
        return "logged-in"
    return "unknown"


class EposAutoFetcher:
    def __init__(self, browser: ManagedBrowser) -> None:
        self.browser = browser
        self.service = service_by_id("epos")

    def open_portal(self) -> dict[str, Any]:
        self.browser.navigate(self.service.portal_url, wait_seconds=1.5)
        return self.browser.page_summary()

    def fetch_pdf(self, target_month: str) -> FetchedStatement:
        year, month = parse_month_key(target_month)
        self.browser.navigate(self.service.portal_url, wait_seconds=1.0)
        summary = self.browser.page_summary()
        state = classify_login_state(summary)
        if state == "login-required":
            raise AcquisitionError(
                "エポスカードへのログインが必要です。",
                code="LOGIN_REQUIRED",
                advice="取得用ブラウザでログインを完了してから、もう一度自動取得してください。",
            )

        form = self._prepare_pdf_form(year, month)
        cookies = self.browser.cookies_for(form["action"])
        content = self._post_pdf_form(form, cookies)
        if content[:4] != b"%PDF":
            head = content[:300].decode("utf-8", errors="ignore")
            if "<html" in head.lower() or "<!doctype" in head.lower():
                raise AcquisitionError(
                    "エポスカードからPDFではなくHTMLが返りました。",
                    code="DOWNLOADED_HTML",
                    advice="セッション切れ、確認画面、またはサイト仕様変更の可能性があります。取得用ブラウザを更新してログイン状態を確認してください。",
                )
            raise AcquisitionError(
                "取得したファイルがPDFとして確認できませんでした。",
                code="PDF_SIGNATURE_MISSING",
                advice="エポスカード側のPDF照会仕様が変わった可能性があります。",
            )

        return FetchedStatement(
            content=content,
            source_url=form["pageUrl"],
            original_file_name=f"epos_{target_month}.pdf",
            metadata_text=form["metadataText"],
            logs=tuple(form.get("logs") or ()),
        )

    def _prepare_pdf_form(self, year: int, month: int) -> dict[str, Any]:
        payload = {"year": year, "month": month}
        result = self.browser.evaluate(
            f"""(() => {{
              const payload = {payload!r};
              const logs = [];
              const visible = (el) => {{
                if (!el || el.disabled) return false;
                const style = getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
              }};
              const paymentYm = (option) => {{
                const text = String(option.textContent || "");
                let match = text.match(/(\\d{{4}})年(\\d{{1,2}})月\\d{{1,2}}日\\s*お支払(?:い)?分/);
                if (!match) match = String(option.value || "").match(/^(\\d{{4}})[-/](\\d{{1,2}})[-/]/);
                if (!match) return null;
                return {{ year: Number(match[1]), month: Number(match[2]) }};
              }};
              const selects = [...document.querySelectorAll("select")].filter(visible);
              const paymentSelect = selects.find((select) => [...select.options].some(paymentYm));
              if (!paymentSelect) {{
                return {{ ok: false, code: "PAYMENT_SELECT_NOT_FOUND", message: "お支払年月の選択欄を見つけられませんでした。", logs }};
              }}
              const options = [...paymentSelect.options]
                .map((option) => ({{ option, ym: paymentYm(option), label: String(option.textContent || "").trim(), value: option.value }}))
                .filter((item) => item.ym);
              const matched = options.find((item) => item.ym.year === payload.year && item.ym.month === payload.month);
              if (!matched) {{
                return {{
                  ok: false,
                  code: "YEAR_MONTH_NOT_AVAILABLE",
                  message: `${{payload.year}}/${{String(payload.month).padStart(2, "0")}} のお支払年月は選択肢にありません。`,
                  available: options.map((item) => item.label),
                  logs
                }};
              }}
              paymentSelect.value = matched.value;
              paymentSelect.dispatchEvent(new Event("input", {{ bubbles: true }}));
              paymentSelect.dispatchEvent(new Event("change", {{ bubbles: true }}));
              logs.push(`お支払年月セレクトを ${{matched.label}} に変更しました。`);
              const form = paymentSelect.closest("form");
              if (!form) {{
                return {{ ok: false, code: "FORM_NOT_FOUND", message: "明細照会フォームを見つけられませんでした。", logs }};
              }}
              const data = new FormData(form);
              data.set(paymentSelect.name, matched.value);
              data.set("nextPDFButton", "PDFで照会する");
              return {{
                ok: true,
                action: form.action,
                method: String(form.method || "post").toLowerCase(),
                fields: [...data.entries()].map(([name, value]) => [name, String(value)]),
                pageUrl: location.href,
                selectedLabel: matched.label,
                metadataText: `${{matched.label}} ${{document.body ? document.body.innerText : ""}}`,
                logs
              }};
            }})()""",
            timeout=20,
        )
        if not result or not result.get("ok"):
            raise AcquisitionError(
                (result or {}).get("message", "エポスカードの明細フォームを準備できませんでした。"),
                code=(result or {}).get("code", "FORM_PREPARE_FAILED"),
                advice="取得用ブラウザでお支払明細書照会画面が表示されているか確認してください。",
            )
        return result

    def _post_pdf_form(self, form: dict[str, Any], cookies: list[dict[str, Any]]) -> bytes:
        session = requests.Session()
        for cookie in cookies:
            session.cookies.set(
                cookie.get("name", ""),
                cookie.get("value", ""),
                domain=cookie.get("domain") or "www.eposcard.co.jp",
                path=cookie.get("path") or "/",
            )
        headers = {
            "User-Agent": "Mozilla/5.0 GetReceipt",
            "Referer": form["pageUrl"],
            "Accept": "application/pdf,application/octet-stream,*/*",
        }
        try:
            response = session.post(form["action"], data=form["fields"], headers=headers, timeout=60)
        except requests.RequestException as error:
            raise AcquisitionError(
                f"エポスカードへのPDF照会リクエストに失敗しました: {error}",
                code="PDF_REQUEST_FAILED",
            ) from error
        if response.status_code >= 400:
            raise AcquisitionError(
                f"エポスカードのPDF照会がHTTP {response.status_code}で失敗しました。",
                code="PDF_REQUEST_HTTP_ERROR",
            )
        return response.content


def acquisition_error_message(error: Exception) -> str:
    if isinstance(error, AcquisitionError):
        return f"{error} ({error.code})"
    if isinstance(error, BrowserAutomationError):
        return str(error)
    return str(error)
