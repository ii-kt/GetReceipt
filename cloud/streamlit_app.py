from __future__ import annotations

import csv
import os
import re
import subprocess
from datetime import date, datetime, timezone
from io import StringIO


def cleanup_orphan_acquisition_browsers() -> None:
    if os.name != "posix":
        return
    for pattern in (
        "chromium.*--remote-debugging-port",
        "chrome.*--remote-debugging-port",
    ):
        try:
            subprocess.run(["pkill", "-f", pattern], timeout=2, check=False)
        except Exception:
            pass


cleanup_orphan_acquisition_browsers()

import streamlit as st

from src.acquisition import acquisition_guidance, default_transaction_date
from src.config import (
    DATA_DIR,
    LEDGER_PATH,
    RECEIPT_DRIVE_FOLDER_ID,
    RECEIPT_DRIVE_FOLDER_URL,
    SERVICES,
    month_label,
    selectable_months,
    service_by_id,
)
from src.ledger import CSV_FIELDS, ReceiptLedger, rows_from_csv_bytes, rows_to_csv_bytes
from src.naming import (
    ReceiptMetadata,
    build_receipt_filename,
    normalize_amount_yen,
    normalize_extension,
    sha256_bytes,
)

st.set_page_config(
    page_title="GetReceipt",
    layout="wide",
)


TEXT = {
    "dashboard": "取得状況",
    "ledger": "保存台帳",
    "settings": "設定",
    "target_month": "対象月",
    "service": "サービス",
    "unfetched": "未取得",
    "uploaded": "取得済",
    "not_issued": "未発行",
    "save_drive": "Driveへ保存",
    "mark_not_issued": "未発行として記録",
}

HIDDEN_AUDIT_COLUMNS = {
    "ファイルID",
    "サービスID",
    "サービス名",
    "対象月",
    "通貨",
    "取得元URL",
    "元ファイル名",
}


def inject_design() -> None:
    st.markdown(
        """
        <style>
        .block-container {
          max-width: 1120px;
          padding-top: 1.5rem;
        }

        div[data-testid="stTabs"] [role="tablist"] {
          gap: .4rem;
          overflow-x: auto;
        }

        .gr-status-key {
          display: flex;
          flex-wrap: wrap;
          gap: .55rem;
          margin: .2rem 0 1rem;
        }

        .gr-status-key span {
          display: inline-flex;
          align-items: center;
          gap: .45rem;
          min-height: 2.1rem;
          padding: .25rem .65rem;
          border: 1px solid #d5d7de;
          border-radius: 999px;
          font-size: .78rem;
          font-weight: 700;
        }

        .gr-status-key i {
          display: inline-block;
          width: .62rem;
          height: .62rem;
          border-radius: 50%;
        }

        .gr-status-key .is-open i { background: #2563eb; }
        .gr-status-key .is-done i { background: #16a34a; }
        .gr-status-key .is-none i { background: #d97706; }

        .gr-month-cell {
          display: flex;
          min-height: 44px;
          align-items: center;
          font-size: .86rem;
          font-weight: 700;
        }

        .stButton > button,
        .stDownloadButton > button,
        [data-testid="stLinkButton"] > a {
          min-height: 42px;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def ledger() -> ReceiptLedger:
    return ReceiptLedger(LEDGER_PATH)


def secrets_configured() -> bool:
    try:
        return "google_service_account" in st.secrets
    except Exception:
        return False


CREDENTIAL_SECTIONS: dict[str, tuple[str, ...]] = {
    "epos": ("epos",),
    "commufa": ("commufa",),
    "tokuten": ("tokuten", "outlook", "microsoft"),
    "mobile": ("webbilling", "mobile", "d_account"),
}

LOGIN_ID_KEYS = (
    "login_id",
    "loginId",
    "user_id",
    "userId",
    "username",
    "email",
    "mail",
    "account",
    "account_id",
    "d_account_id",
    "dAccountId",
    "id",
)
PASSWORD_KEYS = ("password", "pass")


def secret_value(section: object, keys: tuple[str, ...]) -> str:
    for key in keys:
        try:
            value = section.get(key)  # type: ignore[attr-defined]
        except Exception:
            value = None
        if value:
            return str(value).strip()
    return ""


def service_credentials(service_id: str) -> dict[str, str]:
    try:
        for section_name in CREDENTIAL_SECTIONS.get(service_id, (service_id,)):
            if section_name not in st.secrets:
                continue
            section = st.secrets[section_name]
            login_id = secret_value(section, LOGIN_ID_KEYS)
            password = secret_value(section, PASSWORD_KEYS)
            return {
                "login_id": login_id,
                "id": login_id,
                "email": login_id,
                "dAccountId": login_id,
                "password": password,
            }
    except Exception:
        pass
    return {}


def login_secrets_configured(service_id: str) -> bool:
    credentials = service_credentials(service_id)
    return bool(credentials.get("login_id") and credentials.get("password"))


def automation_browser(service_id: str) -> ManagedBrowser:
    from src.browser_session import ManagedBrowser

    browser_key = f"_automation_browser_{service_id}"
    browser = st.session_state.get(browser_key)
    if browser is None:
        browser = ManagedBrowser(
            profile_dir=DATA_DIR / f"browser-profile-{service_id}",
            download_dir=DATA_DIR / f"browser-downloads-{service_id}",
        )
        st.session_state[browser_key] = browser
    return browser


def browser_image_key(service_id: str) -> str:
    return f"browser_image_{service_id}"


def update_browser_image(service_id: str, browser: ManagedBrowser) -> None:
    st.session_state[browser_image_key(service_id)] = browser.screenshot()


def release_automation_browser(service_id: str, browser: ManagedBrowser, *, clear_profile: bool = True) -> None:
    try:
        browser.close(clear_profile=clear_profile)
    finally:
        st.session_state.pop(f"_automation_browser_{service_id}", None)


def service_fetcher(service_id: str, browser: ManagedBrowser):
    if service_id == "epos":
        from src.epos_automation import EposAutoFetcher

        return EposAutoFetcher(browser, credentials=service_credentials(service_id))
    if service_id == "commufa":
        from src.official_site_automation import CommufaAutoFetcher

        return CommufaAutoFetcher(browser, credentials=service_credentials(service_id))
    if service_id == "tokuten":
        from src.official_site_automation import TokutenAutoFetcher

        return TokutenAutoFetcher(browser, credentials=service_credentials(service_id))
    if service_id == "mobile":
        from src.official_site_automation import WebBillingAutoFetcher

        return WebBillingAutoFetcher(browser, credentials=service_credentials(service_id))
    raise KeyError(service_id)


def latest_status_by_month(records: list[dict[str, str]]) -> dict[tuple[str, str], dict[str, str]]:
    latest: dict[tuple[str, str], dict[str, str]] = {}
    for record in records:
        key = (record.get("target_month", ""), record.get("service_id", ""))
        if key not in latest:
            latest[key] = record
    return latest


def status_text(record: dict[str, str] | None) -> str:
    if not record:
        return TEXT["unfetched"]
    if record.get("status") == "uploaded":
        return TEXT["uploaded"]
    if record.get("status") == "not_issued":
        return TEXT["not_issued"]
    return TEXT["unfetched"]


def render_workspace_header() -> None:
    records = ledger().read()
    latest = latest_status_by_month(records)
    months = selectable_months()
    slots = [(target_month, service.id) for target_month in months for service in SERVICES]
    slot_records = [latest.get(slot) for slot in slots]
    saved_count = sum(record.get("status") == "uploaded" for record in records)
    done_slots = sum(record is not None and record.get("status") == "uploaded" for record in slot_records)
    not_issued_slots = sum(record is not None and record.get("status") == "not_issued" for record in slot_records)
    open_slots = max(len(slots) - done_slots - not_issued_slots, 0)
    current_month = month_label(months[-1]) if months else "-"

    st.title("GetReceipt")
    st.caption("領収書・明細を取得してGoogle Driveへ保存します。")
    cols = st.columns(4)
    cols[0].metric("保存済ファイル", saved_count)
    cols[1].metric("未取得枠", open_slots)
    cols[2].metric("保管完了枠", done_slots)
    cols[3].metric("現在の対象", current_month)


def render_section_heading(eyebrow: str, title: str, detail: str) -> None:
    st.subheader(title)
    st.caption(f"{eyebrow}: {detail}")


def render_dashboard() -> bool:
    render_section_heading("Archive index", TEXT["dashboard"], "未取得からそのまま取得へ進む")
    service_names = "、".join(service.label for service in SERVICES)
    st.warning(
        f"事前準備: Streamlit Cloud Secretsに{service_names}のログイン情報を設定してください。"
        "未取得を押すと、この画面内の取得フォームに対象月とサービスを反映します。"
        "ログイン情報は取得用ブラウザへ自動入力されます。"
    )

    acquisition_active = bool(st.session_state.get("_acq_active_from_status"))
    if acquisition_active:
        notice = st.session_state.pop("_acq_status_notice", "")
        if notice:
            st.success(notice)
        render_acquisition_form()
        st.divider()

    records = ledger().read()
    latest = latest_status_by_month(records)
    months = list(reversed(selectable_months()))

    st.markdown(
        """
        <div class="gr-status-key" aria-label="保管状態の凡例">
          <span class="is-open"><i></i>未取得 — クリックして取得へ</span>
          <span class="is-done"><i></i>取得済 — 保管済み</span>
          <span class="is-none"><i></i>未発行 — 記録済み</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    header_cols = st.columns([1.35, 1, 1, 1, 1])
    header_cols[0].markdown(f"**{TEXT['target_month']}**")
    for index, service in enumerate(SERVICES, start=1):
        header_cols[index].markdown(f"**{service.label}**")

    for target_month in months:
        row = st.columns([1.35, 1, 1, 1, 1])
        row[0].markdown(f'<div class="gr-month-cell">{month_label(target_month)}</div>', unsafe_allow_html=True)
        for index, service in enumerate(SERVICES, start=1):
            record = latest.get((target_month, service.id))
            label = status_text(record)
            is_unfetched = label == TEXT["unfetched"]
            button_type = "primary" if is_unfetched else "secondary"
            if row[index].button(
                label,
                key=f"run:{target_month}:{service.id}",
                type=button_type,
                disabled=not is_unfetched,
                use_container_width=True,
            ):
                select_for_acquisition(service.id, target_month)
                st.session_state["_acq_active_from_status"] = True
                st.session_state["_acq_status_notice"] = (
                    f"{service.label} / {month_label(target_month)}を取得対象にしました。"
                    "下の取得フォームでログイン状態を確認してから保存してください。"
                )
                st.rerun()

    return acquisition_active


def select_for_acquisition(service_id: str, target_month: str) -> None:
    st.session_state["acq_service"] = service_id
    st.session_state["acq_month"] = target_month
    prime_manual_defaults(service_id, target_month, force=True)


def prime_manual_defaults(service_id: str, target_month: str, *, force: bool = False) -> None:
    defaults_key = f"{service_id}:{target_month}"
    if not force and st.session_state.get("_manual_defaults_key") == defaults_key:
        return

    service = service_by_id(service_id)
    st.session_state["manual_service"] = service_id
    st.session_state["manual_month"] = target_month
    st.session_state["manual_transaction_date"] = default_transaction_date(service_id, target_month)
    st.session_state["manual_source_url"] = service.portal_url
    st.session_state["_manual_defaults_key"] = defaults_key


def render_acquisition_form() -> None:
    render_section_heading("Acquire", "取得", "サービスと対象月を選択")
    months = selectable_months()
    service_ids = [service.id for service in SERVICES]
    st.session_state.setdefault("acq_service", service_ids[0])
    st.session_state.setdefault("acq_month", months[-1])
    selected_service = st.selectbox(
        TEXT["service"],
        service_ids,
        format_func=lambda value: service_by_id(value).label,
        key="acq_service",
    )
    selected_month = st.selectbox(
        TEXT["target_month"],
        months,
        format_func=month_label,
        key="acq_month",
    )
    service = service_by_id(selected_service)
    prime_manual_defaults(selected_service, selected_month)
    guidance = acquisition_guidance(selected_service, selected_month)

    st.info(
        f"{service.label}のログイン情報はStreamlit Secretsから読み込み、取得用ブラウザへ自動入力します。"
        "Secrets未設定のサービスは取得前に停止します。"
    )
    st.markdown(f"**{guidance.heading}**")
    st.code(guidance.target_hint, language="text")
    for step in guidance.steps:
        st.write(f"- {step}")
    st.info(guidance.note)

    render_official_auto_acquisition(selected_service, selected_month)


def render_official_auto_acquisition(service_id: str, selected_month: str) -> None:
    st.markdown("**取得用ブラウザ**")
    service = service_by_id(service_id)
    image_key = browser_image_key(service_id)

    controls = st.columns([1, 1, 1])
    if controls[0].button("自動ログイン開始", key=f"open_browser:{service_id}", type="primary", use_container_width=True):
        if not login_secrets_configured(service_id):
            st.error(f"{service.label}のログインSecretsが未設定です。設定タブの形式でStreamlit Cloud Secretsへ追加してください。")
            return
        status_box = None
        browser = automation_browser(service_id)
        fetcher = service_fetcher(service_id, browser)
        try:
            status_box = st.status(f"{service.label}の自動ログインを実行しています。", expanded=True)
            with status_box as status:
                status.write("取得用ブラウザを起動し、ログイン画面へ進みます。")
                fetcher.open_portal()
                status.write("ログイン完了を確認しました。")
                update_browser_image(service_id, browser)
                status.update(label=f"{service.label}のログイン完了を確認しました。", state="complete")
                release_automation_browser(service_id, browser)
        except Exception as error:
            if status_box:
                status_box.update(label=f"{service.label}の自動ログインに失敗しました。", state="error")
            st.error(f"自動ログインに失敗しました: {error}")
            release_automation_browser(service_id, browser)

    if controls[1].button("画面更新", key=f"refresh_browser:{service_id}", use_container_width=True):
        try:
            browser = st.session_state.get(f"_automation_browser_{service_id}")
            if browser is None:
                st.info("取得用ブラウザはまだ起動していません。")
            else:
                update_browser_image(service_id, browser)
        except Exception as error:
            st.error(f"画面更新に失敗しました: {error}")

    if controls[2].button("セッション終了", key=f"close_browser:{service_id}", use_container_width=True):
        try:
            browser = st.session_state.get(f"_automation_browser_{service_id}")
            if browser is not None:
                release_automation_browser(service_id, browser)
            st.session_state.pop(image_key, None)
            st.success("取得用ブラウザのセッションを終了しました。")
        except Exception as error:
            st.error(f"セッション終了に失敗しました: {error}")

    image_bytes = st.session_state.get(image_key)
    if image_bytes:
        st.image(image_bytes)

    if st.button("PDFを取得してDriveへ保存", key=f"fetch_pdf:{service_id}", type="primary", use_container_width=True):
        if not secrets_configured():
            st.error("Google Drive用のSecretsが未設定です。先に設定してください。")
            return
        if not login_secrets_configured(service_id):
            st.error(f"{service.label}のログインSecretsが未設定です。設定タブの形式でStreamlit Cloud Secretsへ追加してください。")
            return
        status_box = None
        from src.browser_session import BrowserAutomationError
        from src.epos_automation import AcquisitionError

        browser = automation_browser(service_id)
        fetcher = service_fetcher(service_id, browser)
        try:
            status_box = st.status(f"{service.label}の取得を実行しています。", expanded=True)
            with status_box as status:
                status.write("ログイン、明細取得、PDF生成を自動で進めます。")
                statement = fetcher.fetch_pdf(selected_month)
                status.write("Google Driveへ保存しています。")
                from src.receipt_pipeline import drive_storage_from_secrets, upload_auto_receipt_to_drive

                storage = drive_storage_from_secrets(st.secrets)
                saved = upload_auto_receipt_to_drive(
                    service_id=service_id,
                    target_month=selected_month,
                    content=statement.content,
                    original_file_name=statement.original_file_name,
                    source_url=statement.source_url,
                    metadata_text=statement.metadata_text,
                    storage=storage,
                    ledger=ledger(),
                )
                status.update(label=f"{service.label}の取得とDrive保存が完了しました。", state="complete")
        except AcquisitionError as error:
            if status_box:
                status_box.update(label=f"{service.label}の取得に失敗しました。", state="error")
            st.warning(str(error))
            if error.advice:
                st.info(error.advice)
            try:
                update_browser_image(service_id, browser)
            except Exception:
                pass
            release_automation_browser(service_id, browser)
            return
        except (BrowserAutomationError, Exception) as error:
            if status_box:
                status_box.update(label=f"{service.label}の取得に失敗しました。", state="error")
            st.error(f"取得に失敗しました: {error}")
            release_automation_browser(service_id, browser)
            return

        st.success("PDFを取得し、Google Driveへ保存しました。")
        for line in statement.logs:
            st.caption(line)
        if saved.get("drive_web_view_link"):
            st.link_button("Driveで開く", saved["drive_web_view_link"])
        release_automation_browser(service_id, browser)


def render_acquisition_workspace() -> None:
    acquisition_active = render_dashboard()
    st.divider()
    if not acquisition_active:
        render_acquisition_form()
        st.divider()
    with st.expander("ファイルを追加"):
        render_manual_upload()


def render_manual_upload() -> None:
    render_section_heading("Intake", "ファイルを保管", "iPhoneのファイルをGoogle Driveへ保存")

    months = selectable_months()
    service_ids = [service.id for service in SERVICES]
    st.session_state.setdefault("manual_service", st.session_state.get("acq_service", service_ids[0]))
    st.session_state.setdefault("manual_month", st.session_state.get("acq_month", months[-1]))
    st.session_state.setdefault(
        "manual_transaction_date",
        default_transaction_date(st.session_state["manual_service"], st.session_state["manual_month"]),
    )
    st.session_state.setdefault(
        "manual_source_url",
        service_by_id(st.session_state["manual_service"]).portal_url,
    )
    left, right = st.columns(2)
    with left:
        service_id = st.selectbox(
            TEXT["service"],
            service_ids,
            format_func=lambda value: service_by_id(value).label,
            key="manual_service",
        )
        target_month = st.selectbox(
            TEXT["target_month"],
            months,
            format_func=month_label,
            key="manual_month",
        )
        transaction_date = st.date_input("取引日 / 発行日", key="manual_transaction_date")
    with right:
        service = service_by_id(service_id)
        partner_name = st.text_input("取引先", value=service.default_partner)
        amount_yen = st.number_input("金額（円）", min_value=0, step=1, value=0)
        source_url = st.text_input("取得元URL", key="manual_source_url")

    uploaded_file = st.file_uploader("保存するPDF/CSV/画像", type=["pdf", "csv", "png", "jpg", "jpeg"])
    preview_extension = normalize_extension(uploaded_file.name if uploaded_file else None)
    metadata = ReceiptMetadata(
        service_id=service_id,
        service_label=service_by_id(service_id).label,
        target_month=target_month,
        transaction_date=transaction_date,
        partner_name=partner_name,
        amount_yen=normalize_amount_yen(amount_yen),
        source_url=source_url,
        original_file_name=uploaded_file.name if uploaded_file else "",
    )
    preview_name = build_receipt_filename(metadata, preview_extension)
    st.code(preview_name, language="text")

    cols = st.columns([1, 1, 2])
    if cols[0].button(TEXT["save_drive"], type="primary", use_container_width=True):
        if uploaded_file is None:
            st.error("保存するファイルを選択してください。")
            return
        if amount_yen <= 0:
            st.error("金額を入力してください。")
            return
        try:
            from src.receipt_pipeline import drive_storage_from_secrets

            content = uploaded_file.getvalue()
            storage = drive_storage_from_secrets(st.secrets)
            result = storage.upload_bytes(
                file_name=preview_name,
                content=content,
                mime_type=_mime_type(preview_extension),
            )
            saved = ledger().append_upload(
                metadata=metadata,
                file_name=preview_name,
                drive_file_id=result.id,
                drive_web_view_link=result.web_view_link,
                sha256=sha256_bytes(content),
            )
            storage.upsert_bytes(
                file_name="_receipt_index.csv",
                content=ledger().to_csv_bytes(),
                mime_type="text/csv",
            )
        except Exception as error:
            st.error(f"Google Driveへの保存に失敗しました: {error}")
            return
        st.success("Google Driveへ保存しました。")
        if saved.get("drive_web_view_link"):
            st.link_button("Driveで開く", saved["drive_web_view_link"])

    if cols[1].button(TEXT["mark_not_issued"], use_container_width=True):
        try:
            from src.receipt_pipeline import drive_storage_from_secrets, record_not_issued_to_drive

            storage = drive_storage_from_secrets(st.secrets) if secrets_configured() else None
            record_not_issued_to_drive(
                service_id=service_id,
                target_month=target_month,
                storage=storage,
                ledger=ledger(),
            )
        except Exception as error:
            st.error(f"記録に失敗しました: {error}")
            return
        st.success("未発行として記録しました。")


def render_history() -> None:
    render_section_heading("Ledger", TEXT["ledger"], "保存済みの領収書・明細")
    records = ledger().read()
    if not records:
        st.info("まだ保存履歴はありません。")
        return
    st.dataframe(records, use_container_width=True, hide_index=True)
    st.download_button(
        "台帳CSVをダウンロード",
        data=ledger().to_csv_bytes(),
        file_name="receipt_index.csv",
        mime="text/csv",
    )


def render_drive_filename_audit() -> None:
    render_section_heading("Drive folder", "ファイル名チェック", "保存先フォルダ内の全ファイルを確認")
    st.code("YYYYMMDD_取引先_金額円.拡張子\n例: 20260701_株式会社NTTドコモ_8250円.pdf", language="text")
    st.caption("YYYYMMDDは取引日 / 発行日です。保存台帳のメタデータから取得本体と同じ生成関数で照合します。")
    if not secrets_configured():
        st.warning("Google Drive用のSecretsが未設定です。")
        return

    notice = st.session_state.pop("drive_filename_audit_notice", "")
    if notice:
        st.success(notice)

    if st.button("フォルダ内のファイル名を確認", type="primary", use_container_width=True):
        try:
            from src.receipt_pipeline import drive_storage_from_secrets

            storage = drive_storage_from_secrets(st.secrets)
            rows = refresh_drive_filename_audit(storage)
        except Exception as error:
            st.error(f"Driveフォルダの確認に失敗しました: {error}")
            return

    rows = st.session_state.get("drive_filename_audit_rows", [])
    if not rows:
        return

    total = len(rows)
    ok_count = sum(row["判定"] == "OK" for row in rows)
    review_count = sum(row["判定"] == "要確認" for row in rows)
    managed_count = sum(row["判定"] == "管理" for row in rows)
    cols = st.columns(4)
    cols[0].metric("確認ファイル", total)
    cols[1].metric("OK", ok_count)
    cols[2].metric("要確認", review_count)
    cols[3].metric("管理", managed_count)

    display_rows = audit_display_rows(rows)
    st.dataframe(
        display_rows,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Drive": st.column_config.LinkColumn("Drive"),
        },
    )
    render_drive_rename_tools(rows)
    st.download_button(
        "確認結果CSVをダウンロード",
        data=audit_rows_to_csv_bytes(display_rows),
        file_name="drive_filename_audit.csv",
        mime="text/csv",
        use_container_width=True,
    )


def refresh_drive_filename_audit(storage) -> list[dict[str, str]]:
    rows = build_drive_filename_audit_rows(storage.list_files(), load_synced_ledger_rows(storage))
    st.session_state["drive_filename_audit_rows"] = rows
    return rows


def load_synced_ledger_rows(storage) -> list[dict[str, str]]:
    rows_by_drive_id: dict[str, dict[str, str]] = {}
    rows_without_drive_id: list[dict[str, str]] = []

    def collect(rows: list[dict[str, str]]) -> None:
        for row in rows:
            drive_file_id = row.get("drive_file_id", "")
            if drive_file_id:
                rows_by_drive_id[drive_file_id] = row
            else:
                rows_without_drive_id.append(row)

    collect(ledger().read())
    drive_content = storage.download_bytes_by_name("_receipt_index.csv")
    if drive_content:
        collect(rows_from_csv_bytes(drive_content))
    return [*rows_by_drive_id.values(), *rows_without_drive_id]


def build_drive_filename_audit_rows(
    files: list[dict[str, str]],
    ledger_rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    ledger_by_drive_id = {
        row.get("drive_file_id", ""): row
        for row in ledger_rows
        if row.get("drive_file_id")
    }
    rows: list[dict[str, str]] = []
    for file in files:
        name = file.get("name", "")
        mime_type = file.get("mimeType", "")
        if mime_type == "application/vnd.google-apps.folder":
            status = "管理"
            reason = "フォルダです。"
            transaction_date = partner_name = amount_yen = extension = ""
            expected_name = ""
            ledger_status = "管理"
            record = {}
        elif name == "_receipt_index.csv":
            status = "管理"
            reason = "保存台帳の同期ファイルです。"
            transaction_date = partner_name = amount_yen = extension = ""
            expected_name = ""
            ledger_status = "管理"
            record = {}
        else:
            extension = normalize_extension(name, "pdf")
            record = ledger_by_drive_id.get(file.get("id", ""))
            if record:
                ledger_status = "登録済"
                try:
                    expected_name = expected_filename_from_ledger_record(record, extension)
                    status = "OK" if name == expected_name else "要確認"
                    reason = (
                        "保存台帳のメタデータから生成したファイル名と一致しています。"
                        if status == "OK"
                        else "保存台帳のメタデータから生成したファイル名と一致していません。"
                    )
                    transaction_date = transaction_date_key_from_record(record)
                    partner_name = record.get("partner_name", "")
                    amount_yen = record.get("amount_yen", "")
                except Exception as error:
                    status = "要確認"
                    reason = f"保存台帳のメタデータから期待ファイル名を生成できません: {error}"
                    transaction_date = record.get("transaction_date", "")
                    partner_name = record.get("partner_name", "")
                    amount_yen = record.get("amount_yen", "")
                    expected_name = ""
            else:
                status = "要確認"
                reason = "保存台帳に未登録です。下のフォームで取引日・取引先・金額を入れると、台帳登録と名前変更を同時に実行できます。"
                transaction_date = partner_name = amount_yen = ""
                expected_name = ""
                ledger_status = "未登録"
                record = {}
        rows.append({
            "ファイルID": file.get("id", ""),
            "判定": status,
            "台帳": ledger_status,
            "ファイル名": name,
            "期待ファイル名": expected_name,
            "理由": reason,
            "取引日": transaction_date,
            "取引先": partner_name,
            "金額": amount_yen,
            "拡張子": extension,
            "更新日時": file.get("modifiedTime", ""),
            "Drive": file.get("webViewLink", ""),
            "サービスID": record.get("service_id", ""),
            "サービス名": record.get("service_label", ""),
            "対象月": record.get("target_month", ""),
            "通貨": record.get("currency", "JPY"),
            "取得元URL": record.get("source_url", ""),
            "元ファイル名": record.get("original_file_name", ""),
        })

    order = {"要確認": 0, "OK": 1, "管理": 2}
    return sorted(rows, key=lambda row: (order.get(row["判定"], 9), row["ファイル名"]))


def expected_filename_from_ledger_record(record: dict[str, str], extension: str) -> str:
    metadata = ReceiptMetadata(
        service_id=record.get("service_id", ""),
        service_label=record.get("service_label", ""),
        target_month=record.get("target_month", ""),
        transaction_date=parse_ledger_transaction_date(record.get("transaction_date", "")),
        partner_name=record.get("partner_name", ""),
        amount_yen=normalize_amount_yen(record.get("amount_yen", "")),
        currency=record.get("currency", "JPY") or "JPY",
        source_url=record.get("source_url", ""),
        original_file_name=record.get("original_file_name", ""),
    )
    return build_receipt_filename(metadata, extension)


def transaction_date_key_from_record(record: dict[str, str]) -> str:
    return parse_ledger_transaction_date(record.get("transaction_date", "")).strftime("%Y%m%d")


def parse_ledger_transaction_date(value: str) -> date:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{8}", text):
        return datetime.strptime(text, "%Y%m%d").date()
    return date.fromisoformat(text)


def audit_display_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return [{key: value for key, value in row.items() if key not in HIDDEN_AUDIT_COLUMNS} for row in rows]


def audit_rows_to_csv_bytes(rows: list[dict[str, str]]) -> bytes:
    buffer = StringIO()
    fieldnames = list(rows[0].keys()) if rows else []
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def render_drive_rename_tools(rows: list[dict[str, str]]) -> None:
    targets = [row for row in rows if row.get("判定") == "要確認" and row.get("ファイルID")]
    if not targets:
        return

    st.divider()
    st.markdown("**ファイル名を変更**")
    existing_names = {row.get("ファイル名", ""): row.get("ファイルID", "") for row in rows}

    for row in targets:
        file_id = row["ファイルID"]
        current_name = row["ファイル名"]
        with st.expander(current_name):
            if row.get("Drive"):
                st.link_button("Driveで開く", row["Drive"])

            transaction_date = st.date_input(
                "取引日",
                value=default_rename_date(row),
                key=f"rename_date:{file_id}",
            )
            partner_name = st.text_input(
                "取引先",
                value=default_rename_partner(row),
                key=f"rename_partner:{file_id}",
            )
            amount_yen = st.number_input(
                "金額（円）",
                min_value=0,
                step=1,
                value=default_rename_amount(row),
                key=f"rename_amount:{file_id}",
            )
            extension = st.text_input(
                "拡張子",
                value=default_rename_extension(row),
                key=f"rename_extension:{file_id}",
            )

            preview_name = build_rename_preview_name(
                transaction_date=transaction_date,
                partner_name=partner_name,
                amount_yen=int(amount_yen),
                extension=extension,
            )
            metadata = build_rename_metadata(
                row=row,
                transaction_date=transaction_date,
                partner_name=partner_name,
                amount_yen=int(amount_yen),
            )
            duplicate_id = existing_names.get(preview_name)
            has_duplicate = bool(duplicate_id and duplicate_id != file_id)
            missing_partner = not partner_name.strip()
            st.code(preview_name, language="text")
            if has_duplicate:
                st.error("同じ名前のファイルが既にあります。")
            if amount_yen <= 0:
                st.warning("金額を入力してください。")
            if missing_partner:
                st.warning("取引先を入力してください。")

            if st.button(
                "この名前に変更して台帳登録" if row.get("台帳") == "未登録" else "この名前に変更",
                key=f"rename_apply:{file_id}",
                type="primary",
                use_container_width=True,
                disabled=has_duplicate or amount_yen <= 0 or missing_partner,
            ):
                rename_drive_file(
                    file_id=file_id,
                    new_name=preview_name,
                    metadata=metadata,
                    drive_web_view_link=row.get("Drive", ""),
                    original_file_name=current_name,
                )


def rename_drive_file(
    *,
    file_id: str,
    new_name: str,
    metadata: ReceiptMetadata,
    drive_web_view_link: str,
    original_file_name: str,
) -> None:
    try:
        from src.receipt_pipeline import drive_storage_from_secrets

        storage = drive_storage_from_secrets(st.secrets)
        storage.rename_file(file_id=file_id, new_name=new_name)
        rename_synced_ledger_file(
            storage,
            drive_file_id=file_id,
            file_name=new_name,
            metadata=metadata,
            drive_web_view_link=drive_web_view_link,
            original_file_name=original_file_name,
        )
        refresh_drive_filename_audit(storage)
    except Exception as error:
        st.error(f"ファイル名の変更に失敗しました: {error}")
        return
    st.session_state["drive_filename_audit_notice"] = "ファイル名を変更し、保存台帳を同期しました。"
    st.rerun()


def rename_synced_ledger_file(
    storage,
    *,
    drive_file_id: str,
    file_name: str,
    metadata: ReceiptMetadata,
    drive_web_view_link: str,
    original_file_name: str,
) -> bool:
    rows = load_synced_ledger_rows(storage)
    metadata_record = metadata.to_record()
    for row in rows:
        if row.get("drive_file_id") == drive_file_id:
            row.update(metadata_record)
            row["status"] = "uploaded"
            row["file_name"] = file_name
            row["drive_web_view_link"] = drive_web_view_link or row.get("drive_web_view_link", "")
            row["source_url"] = metadata.source_url or row.get("source_url", "")
            row["original_file_name"] = metadata.original_file_name or original_file_name
            break
    else:
        row = {field: "" for field in CSV_FIELDS}
        row.update({
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "status": "uploaded",
            **metadata_record,
            "file_name": file_name,
            "drive_file_id": drive_file_id,
            "drive_web_view_link": drive_web_view_link,
            "source_url": metadata.source_url,
            "original_file_name": metadata.original_file_name or original_file_name,
        })
        rows.insert(0, row)

    ledger().replace_all(rows)
    storage.upsert_bytes(
        file_name="_receipt_index.csv",
        content=rows_to_csv_bytes(rows),
        mime_type="text/csv",
    )
    return True


def default_rename_date(row: dict[str, str]) -> date:
    value = row.get("取引日", "")
    parsed_date = parse_rename_date(value)
    if parsed_date:
        return parsed_date
    return date.today()


def default_rename_partner(row: dict[str, str]) -> str:
    if row.get("取引先"):
        return row["取引先"]
    return ""


def default_rename_amount(row: dict[str, str]) -> int:
    value = row.get("金額", "")
    if value.isdigit():
        return int(value)
    return 0


def default_rename_extension(row: dict[str, str]) -> str:
    return row.get("拡張子") or normalize_extension(row.get("ファイル名"), "pdf")


def parse_rename_date(value: str) -> date | None:
    if not re.fullmatch(r"\d{8}", value or ""):
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None


def build_rename_metadata(
    *,
    row: dict[str, str],
    transaction_date: date,
    partner_name: str,
    amount_yen: int,
) -> ReceiptMetadata:
    return ReceiptMetadata(
        service_id=row.get("サービスID", ""),
        service_label=row.get("サービス名", ""),
        target_month=row.get("対象月") or transaction_date.strftime("%Y-%m"),
        transaction_date=transaction_date,
        partner_name=partner_name,
        amount_yen=normalize_amount_yen(amount_yen),
        currency=row.get("通貨", "JPY") or "JPY",
        source_url=row.get("取得元URL") or row.get("Drive", ""),
        original_file_name=row.get("元ファイル名") or row.get("ファイル名", ""),
    )


def build_rename_preview_name(
    *,
    transaction_date: date,
    partner_name: str,
    amount_yen: int,
    extension: str,
) -> str:
    metadata = ReceiptMetadata(
        service_id="",
        service_label="",
        target_month="",
        transaction_date=transaction_date,
        partner_name=partner_name,
        amount_yen=amount_yen,
    )
    return build_receipt_filename(metadata, extension)


def render_settings() -> None:
    render_section_heading("Connection", TEXT["settings"], "Google Driveとの連携状態")
    st.write("Google Drive保存先")
    st.code(RECEIPT_DRIVE_FOLDER_ID, language="text")
    st.link_button("領収書フォルダを開く", RECEIPT_DRIVE_FOLDER_URL)

    if secrets_configured():
        st.success("Google Drive用のStreamlit Secretsが設定されています。")
    else:
        st.warning("Google Drive用のStreamlit Secretsが未設定です。")
    st.caption("保存台帳はGoogle Driveの `_receipt_index.csv` にも同期されます。")

    st.write("ログインSecrets")
    for service in SERVICES:
        if login_secrets_configured(service.id):
            st.success(f"{service.label}: 設定済み")
        else:
            st.warning(f"{service.label}: 未設定")
    st.code(
        """
[epos]
login_id = "エポスNet ID"
password = "エポスNetパスワード"

[commufa]
login_id = "Myコミュファ ログインIDまたはメールアドレス"
password = "Myコミュファ パスワード"

[tokuten]
email = "Outlook / Microsoftアカウント"
password = "Outlook / Microsoftパスワード"

[webbilling]
d_account_id = "dアカウントID"
password = "dアカウントパスワード"
""".strip(),
        language="toml",
    )
    st.caption("Secretsの実値は画面に表示しません。GitHubには入れず、Streamlit CloudのSecretsに設定してください。")


def _mime_type(extension: str) -> str:
    return {
        "pdf": "application/pdf",
        "csv": "text/csv",
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
    }.get(extension.lower(), "application/octet-stream")


inject_design()
render_workspace_header()

tabs = st.tabs([TEXT["dashboard"]])
with tabs[0]:
    render_acquisition_workspace()
    with st.expander("ファイル名チェック"):
        render_drive_filename_audit()
    with st.expander(TEXT["ledger"]):
        render_history()
    with st.expander(TEXT["settings"]):
        render_settings()
