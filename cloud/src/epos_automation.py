from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

import requests

from .browser_session import ManagedBrowser
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


def epos_login_error(summary: dict[str, Any]) -> str:
    text = str(summary.get("text") or "")
    normalized = _normalize(text)
    phrases = (
        "idまたはパスワード",
        "パスワードまたは",
        "誤っています",
        "ログインできません",
        "入力内容を確認",
    )
    if not any(_normalize(phrase) in normalized for phrase in phrases):
        return ""
    compact_text = re.sub(r"\s+", " ", text).strip()
    match = re.search(r"(IDまたはパスワード[^。]{0,120}。)", compact_text)
    if match:
        return match.group(1)
    for line in text.splitlines():
        compact = line.strip()
        if compact and any(_normalize(phrase) in _normalize(compact) for phrase in phrases):
            return compact
    return "IDまたはパスワードがエポスカード側で拒否されました。"


def _login_payload(credentials: dict[str, str]) -> dict[str, str]:
    login_id = (
        credentials.get("login_id")
        or credentials.get("email")
        or credentials.get("id")
        or credentials.get("user_id")
        or ""
    )
    return {"loginId": login_id, "password": credentials.get("password") or ""}


def _script(payload: dict[str, Any], body: str) -> str:
    return "(() => {\nconst payload = " + json.dumps(payload, ensure_ascii=False) + ";\n" + body + "\n})()"


def build_epos_auto_login_expression(credentials: dict[str, str]) -> str:
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
const securityWords = ["ワンタイム", "認証コード", "確認コード", "セキュリティコード", "本人確認", "秘密の質問", "captcha", "recaptcha"];
if (securityWords.some((word) => pageText.includes(normalize(word)))) return { attempted: false, code: "SECURITY_CHALLENGE", reason: "追加認証が表示されています。" };
const passwordInput = [...document.querySelectorAll("input[type='password']")].find(visible);
const textInputs = [...document.querySelectorAll("input, textarea")]
  .filter(visible)
  .filter((input) => ["", "text", "email", "tel"].includes(String(input.type || "").toLowerCase()));
const accountInput = textInputs
  .map((input) => {
    const text = normalize(labelOf(input) + " " + contextOf(input, 5));
    let score = 0;
    if (text.includes("id") || text.includes(normalize("ログインID")) || text.includes(normalize("エポスnet"))) score += 260;
    if (text.includes("mail") || text.includes("email") || text.includes(normalize("メール"))) score += 180;
    if (text.includes(normalize("検索")) || text.includes("search")) score -= 500;
    return { input, score };
  })
  .filter((item) => item.score > 0)
  .sort((a, b) => b.score - a.score)[0]?.input || textInputs[0] || null;
if (accountInput && !String(accountInput.value || "").trim()) {
  if (!payload.loginId) return { attempted: false, code: "LOGIN_ID_NOT_CONFIGURED", reason: "エポスNet IDが未設定です。" };
  setValue(accountInput, payload.loginId);
}
if (passwordInput) {
  if (!payload.password && !String(passwordInput.value || "").trim()) return { attempted: false, code: "PASSWORD_NOT_CONFIGURED", reason: "パスワードが未設定です。" };
  if (payload.password) setValue(passwordInput, payload.password);
  const button = byText(["ログイン", "login", "送信", "submit", "次へ", "next"], ["戻る", "キャンセル", "お忘れ", "新規", "登録"]);
  if (button) return { attempted: true, code: "SUBMIT_PASSWORD", click: pointOf(button) };
  return { attempted: true, code: "SUBMIT_PASSWORD_ENTER", pressEnter: true };
}
if (accountInput) {
  const button = byText(["次へ", "next", "ログイン", "login", "続行", "continue"], ["戻る", "キャンセル", "お忘れ", "新規", "登録"]);
  if (button) return { attempted: true, code: "SUBMIT_LOGIN_ID", click: pointOf(button) };
  return { attempted: true, code: "SUBMIT_LOGIN_ID_ENTER", pressEnter: true };
}
const loginEntry = byText(["ログイン", "login", "エポスnet"], ["新規", "登録", "お忘れ", "キャンセル"]);
if (loginEntry) return { attempted: true, code: "CLICK_LOGIN_ENTRY", click: pointOf(loginEntry) };
return { attempted: false, code: "LOGIN_STEP_NOT_FOUND", reason: "自動ログイン対象の入力欄またはボタンを見つけられませんでした。" };
""",
    )


def build_epos_login_layout_expression() -> str:
    return r"""(() => {
const visible = (el) => {
  if (!el || el.disabled) return false;
  const style = getComputedStyle(el);
  const rect = el.getBoundingClientRect();
  return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
};
const labelOf = (el) => [el.innerText, el.textContent, el.value, el.placeholder, el.title, el.alt, el.getAttribute && el.getAttribute("aria-label"), el.getAttribute && el.getAttribute("name"), el.getAttribute && el.getAttribute("id")].filter(Boolean).join(" ");
const pointOf = (el) => {
  el.scrollIntoView({ block: "center", inline: "center" });
  const rect = el.getBoundingClientRect();
  return { x: Math.round(rect.left + rect.width / 2), y: Math.round(rect.top + rect.height / 2) };
};
const loginId = [...document.querySelectorAll("input")].find((el) => visible(el) && String(el.name || "").toLowerCase() === "loginid");
const password = [...document.querySelectorAll("input")].find((el) => visible(el) && String(el.name || "").toLowerCase() === "password");
const loginLink = [...document.querySelectorAll("a, button, input[type='submit'], [role='button']")]
  .filter(visible)
  .find((el) => {
    const text = labelOf(el).trim();
    return text.includes("ログイン") || String(el.getAttribute("href") || "").includes("login()");
  });
if (!loginId || !password || !loginLink) return { ok: false, code: "EPOS_LOGIN_LAYOUT_NOT_FOUND" };
const rect = loginLink.getBoundingClientRect();
const centerY = Math.round(rect.top + rect.height / 2);
return {
  ok: true,
  loginIdPoint: pointOf(loginId),
  passwordPoint: pointOf(password),
  buttonRect: {
    left: Math.round(rect.left),
    right: Math.round(rect.right),
    top: Math.round(rect.top),
    bottom: Math.round(rect.bottom),
    width: Math.round(rect.width),
    height: Math.round(rect.height),
  },
  buttonPoint: { x: Math.round(rect.left + rect.width / 2), y: centerY },
  bubblePoint: { x: Math.round(rect.right - Math.min(20, rect.width * 0.07)), y: centerY },
  bubbleDragTarget: { x: Math.round(rect.left + rect.width * 0.95), y: centerY },
};
})()"""


def build_epos_login_diagnostics_expression(credentials: dict[str, str]) -> str:
    return _script(
        _login_payload(credentials),
        r"""
const loginId = document.querySelector("input[name='loginId']");
const password = document.querySelector("input[name='passWord']");
const cookieNames = document.cookie.split(";").map((value) => value.trim().split("=")[0]).filter(Boolean);
return {
  loginIdMatches: !!loginId && String(loginId.value || "") === payload.loginId,
  loginIdLength: loginId ? String(loginId.value || "").length : 0,
  expectedLoginIdLength: String(payload.loginId || "").length,
  passwordMatches: !!password && String(password.value || "") === payload.password,
  passwordLengthMatches: !!password && String(password.value || "").length === String(payload.password || "").length,
  passwordLength: password ? String(password.value || "").length : 0,
  expectedPasswordLength: String(payload.password || "").length,
  headlessUserAgent: String(navigator.userAgent || "").includes("Headless"),
  webdriver: navigator.webdriver === true,
  hasAbckCookie: cookieNames.includes("_abck"),
  hasBmCookie: cookieNames.includes("bm_sz"),
};
""",
    )


class EposAutoFetcher:
    def __init__(self, browser: ManagedBrowser, credentials: dict[str, str] | None = None) -> None:
        self.browser = browser
        self.credentials = credentials or {}
        self.service = service_by_id("epos")
        self.last_login_diagnostics: dict[str, Any] = {}

    def open_portal(self) -> dict[str, Any]:
        self.browser.navigate(self.service.portal_url, wait_seconds=1.5)
        self._advance_login(max_steps=4)
        return self.browser.page_summary()

    def fetch_pdf(self, target_month: str) -> FetchedStatement:
        year, month = parse_month_key(target_month)
        self.browser.navigate(self.service.portal_url, wait_seconds=1.0)
        self._wait_for_login()

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

    def _apply_login_result(self, result: dict[str, Any]) -> bool:
        code = str(result.get("code") or "")
        if code in {"LOGIN_ID_NOT_CONFIGURED", "PASSWORD_NOT_CONFIGURED"}:
            raise AcquisitionError(
                "エポスカードのログインSecretsが未設定です。",
                code=code,
                advice="Streamlit CloudのSecretsにエポスNet IDとパスワードを設定してください。",
            )
        if code == "SECURITY_CHALLENGE":
            raise AcquisitionError(
                "エポスカードで追加認証が表示されました。",
                code="SECURITY_CHALLENGE",
                advice="ワンタイムコード、CAPTCHA、本人確認などサイト側の追加認証が出ているため、通常ログインの自動入力では続行できません。",
            )
        if result.get("attempted") and result.get("click"):
            click = result["click"]
            self.browser.click_at(int(click["x"]), int(click["y"]))
            time.sleep(1.2)
            return True
        if result.get("attempted") and result.get("pressEnter"):
            self.browser.press_key("Enter")
            time.sleep(1.2)
            return True
        return False

    def _wait_for_login_page_ready(self, timeout_seconds: float = 8.0) -> None:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            try:
                state = self.browser.evaluate(
                    r"""(() => {
                      const cookieNames = document.cookie.split(";").map((value) => value.trim().split("=")[0]).filter(Boolean);
                      return {
                        readyState: document.readyState,
                        hasLoginId: !!document.querySelector("input[name='loginId']"),
                        hasPassword: !!document.querySelector("input[name='passWord']"),
                        hasAbckCookie: cookieNames.includes("_abck"),
                        hasBmCookie: cookieNames.includes("bm_sz"),
                      };
                    })()""",
                    timeout=5,
                ) or {}
            except Exception:
                state = {}
            fields_ready = state.get("hasLoginId") and state.get("hasPassword")
            cookies_ready = state.get("hasAbckCookie") and state.get("hasBmCookie")
            if state.get("readyState") == "complete" and fields_ready and cookies_ready:
                return
            if fields_ready and time.time() > deadline - 3:
                return
            time.sleep(0.35)

    def _perform_human_login_attempt(self) -> bool:
        payload = _login_payload(self.credentials)
        if not payload["loginId"]:
            raise AcquisitionError(
                "エポスカードのログインSecretsが未設定です。",
                code="LOGIN_ID_NOT_CONFIGURED",
                advice="Streamlit CloudのSecretsにエポスNet IDを設定してください。",
            )
        if not payload["password"]:
            raise AcquisitionError(
                "エポスカードのログインSecretsが未設定です。",
                code="PASSWORD_NOT_CONFIGURED",
                advice="Streamlit CloudのSecretsにエポスNetパスワードを設定してください。",
            )
        self._wait_for_login_page_ready()
        layout = self.browser.evaluate(build_epos_login_layout_expression(), timeout=10) or {}
        if not layout.get("ok"):
            result = self.browser.evaluate(build_epos_auto_login_expression(self.credentials), timeout=10) or {}
            return self._apply_login_result(result)

        login_point = layout["loginIdPoint"]
        self.browser.move_at(int(login_point["x"]) - 20, int(login_point["y"]) - 6)
        time.sleep(0.08)
        self.browser.click_at(int(login_point["x"]), int(login_point["y"]))
        time.sleep(0.15)
        self.browser.clear_focused_text()
        time.sleep(0.08)
        self.browser.type_text(payload["loginId"])
        time.sleep(0.2)

        password_point = layout["passwordPoint"]
        self.browser.move_at(int(password_point["x"]) - 18, int(password_point["y"]) - 5)
        time.sleep(0.08)
        self.browser.click_at(int(password_point["x"]), int(password_point["y"]))
        time.sleep(0.15)
        self.browser.clear_focused_text()
        time.sleep(0.08)
        self.browser.type_text(payload["password"])
        time.sleep(0.3)
        self.last_login_diagnostics = self.browser.evaluate(
            build_epos_login_diagnostics_expression(self.credentials),
            timeout=10,
        ) or {}

        button_rect = layout["buttonRect"]
        button_y = int(layout["buttonPoint"]["y"])
        left_x = int(button_rect["left"]) + 24
        right_x = int(button_rect["right"]) - 24
        center_x = int(layout["buttonPoint"]["x"])
        for x, y_offset in (
            (left_x, -10),
            (int(button_rect["left"]) + 90, 7),
            (center_x, -4),
            (right_x, 5),
            (center_x, 0),
        ):
            self.browser.move_at(x, button_y + y_offset)
            time.sleep(0.08)
        button = layout["buttonPoint"]
        self.browser.click_at(int(button["x"]), int(button["y"]))
        time.sleep(3.0)
        return True

    def _raise_login_error_if_present(self, summary: dict[str, Any]) -> None:
        message = epos_login_error(summary)
        if not message:
            return
        diagnostics = self._diagnostic_summary()
        suffix = f" 診断: {diagnostics}" if diagnostics else ""
        raise AcquisitionError(
            "エポスカードのログインに失敗しました。",
            code="LOGIN_REJECTED",
            advice=f"エポスカード側メッセージ: {message} Streamlit Secretsの [epos] login_id / password を確認してください。{suffix}",
        )

    def _diagnostic_summary(self) -> str:
        if not self.last_login_diagnostics:
            return ""
        values = self.last_login_diagnostics
        return (
            f"ID入力一致={values.get('loginIdMatches')}, "
            f"ID長={values.get('loginIdLength')}/{values.get('expectedLoginIdLength')}, "
            f"パスワード入力一致={values.get('passwordMatches')}, "
            f"パスワード長一致={values.get('passwordLengthMatches')}, "
            f"HeadlessUA={values.get('headlessUserAgent')}, "
            f"webdriver={values.get('webdriver')}, "
            f"_abck={values.get('hasAbckCookie')}, "
            f"bm_sz={values.get('hasBmCookie')}"
        )

    def _advance_login(self, max_steps: int = 4) -> None:
        for _ in range(max_steps):
            summary = self.browser.page_summary()
            self._raise_login_error_if_present(summary)
            if classify_login_state(summary) == "logged-in":
                return
            if not self._perform_human_login_attempt():
                break
            time.sleep(2.0)
        summary = self.browser.page_summary()
        self._raise_login_error_if_present(summary)
        if classify_login_state(summary) != "logged-in":
            raise AcquisitionError(
                "エポスカードのログイン完了を確認できませんでした。",
                code="LOGIN_NOT_CONFIRMED",
                advice="ログイン画面が残っています。Streamlit Secretsの [epos] login_id / password を確認してください。",
            )

    def _wait_for_login(self, timeout_seconds: float = 90) -> None:
        deadline = time.time() + timeout_seconds
        last_state = "unknown"
        last_reason = ""
        while time.time() < deadline:
            summary = self.browser.page_summary()
            self._raise_login_error_if_present(summary)
            state = classify_login_state(summary)
            last_state = state
            if state == "logged-in":
                return
            try:
                progressed = self._perform_human_login_attempt()
                last_reason = ""
            except AcquisitionError:
                raise
            if progressed:
                time.sleep(2.0)
                continue
            time.sleep(1.0)
        diagnostics = self._diagnostic_summary()
        raise AcquisitionError(
            "エポスカードの自動ログインを完了できませんでした。",
            code="LOGIN_REQUIRED" if last_state == "login-required" else "LOGIN_TIMEOUT",
            advice=last_reason
            or (
                "Streamlit Cloud Secretsのログイン情報とエポスカードのログイン画面を確認してください。"
                + (f" 診断: {diagnostics}" if diagnostics else "")
            ),
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

