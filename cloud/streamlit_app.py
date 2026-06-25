from __future__ import annotations

from datetime import date

import streamlit as st

from src.config import (
    LEDGER_PATH,
    RECEIPT_DRIVE_FOLDER_ID,
    RECEIPT_DRIVE_FOLDER_URL,
    SERVICES,
    month_label,
    selectable_months,
    service_by_id,
)
from src.ledger import ReceiptLedger
from src.naming import (
    ReceiptMetadata,
    build_receipt_filename,
    normalize_amount_yen,
    normalize_extension,
    sha256_bytes,
)
from src.receipt_pipeline import (
    drive_storage_from_secrets,
    record_not_issued_to_drive,
)


st.set_page_config(
    page_title="GetReceipt",
    layout="wide",
)


TEXT = {
    "title": "GetReceipt",
    "dashboard": "取得状況",
    "manual": "手動登録",
    "ledger": "保存台帳",
    "settings": "設定",
    "target_month": "対象月",
    "service": "サービス",
    "unfetched": "未取得",
    "uploaded": "取得済",
    "not_issued": "未発行",
    "start": "取得開始",
    "save_drive": "Driveへ保存",
    "mark_not_issued": "未発行として記録",
}


def inject_design() -> None:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:wght@600;700;800&family=IBM+Plex+Mono:wght@500;600;700&family=Zen+Kaku+Gothic+New:wght@400;500;700;900&display=swap');

        :root {
          --gr-paper: #f8f7f3;
          --gr-paper-2: #eeeee7;
          --gr-surface: #ffffff;
          --gr-ink: #24231f;
          --gr-muted: #6e7069;
          --gr-line: #d8d9cf;
          --gr-blue: #2864f0;
          --gr-blue-dark: #193f9e;
          --gr-red: #ff5c3f;
          --gr-green: #18856f;
          --gr-amber: #d99a18;
          --gr-shadow: 0 18px 45px rgba(36, 35, 31, .10);
          --gr-radius: 18px;
          --gr-ease: cubic-bezier(.2, .75, .2, 1);
        }

        html {
          color-scheme: light;
          scroll-behavior: smooth;
        }

        body,
        .stApp {
          color: var(--gr-ink);
          font-family: "Zen Kaku Gothic New", system-ui, sans-serif;
          background:
            radial-gradient(circle at 12% 10%, rgba(40, 100, 240, .16), transparent 26rem),
            radial-gradient(circle at 88% 8%, rgba(255, 92, 63, .14), transparent 24rem),
            linear-gradient(90deg, rgba(36, 35, 31, .055) 1px, transparent 1px),
            linear-gradient(180deg, rgba(36, 35, 31, .045) 1px, transparent 1px),
            var(--gr-paper);
          background-size: auto, auto, 34px 34px, 34px 34px, auto;
        }

        [data-testid="stHeader"] {
          background: rgba(248, 247, 243, .84);
          border-bottom: 1px solid var(--gr-line);
          backdrop-filter: blur(16px);
        }

        [data-testid="stToolbar"] {
          right: 1rem;
        }

        .block-container {
          max-width: 1240px;
          padding: 1.65rem 1.25rem 4rem;
        }

        .gr-hero {
          position: relative;
          display: grid;
          grid-template-columns: minmax(0, 1.05fr) minmax(330px, .95fr);
          gap: 1rem;
          overflow: hidden;
          border: 1px solid var(--gr-ink);
          border-radius: calc(var(--gr-radius) + 8px);
          background: var(--gr-ink);
          box-shadow: 10px 10px 0 rgba(40, 100, 240, .20), var(--gr-shadow);
          animation: gr-rise .48s var(--gr-ease) both;
        }

        .gr-hero::before {
          content: "";
          position: absolute;
          inset: 0;
          pointer-events: none;
          background:
            linear-gradient(90deg, rgba(255,255,255,.08) 1px, transparent 1px),
            linear-gradient(180deg, rgba(255,255,255,.07) 1px, transparent 1px);
          background-size: 28px 28px;
          mask-image: linear-gradient(90deg, rgba(0,0,0,.9), rgba(0,0,0,.2));
        }

        .gr-hero-title,
        .gr-status-board {
          position: relative;
          z-index: 1;
        }

        .gr-hero-title {
          padding: 2rem 2rem 1.7rem;
          color: #fffdf5;
        }

        .gr-eyebrow,
        .gr-section-eyebrow,
        .gr-flow span,
        .gr-card span,
        .gr-status-key span,
        .gr-month-cell {
          font-family: "IBM Plex Mono", ui-monospace, monospace;
        }

        .gr-eyebrow {
          display: inline-flex;
          align-items: center;
          gap: .55rem;
          margin: 0 0 1rem;
          color: #fff7dc;
          font-size: .78rem;
          font-weight: 700;
          letter-spacing: .12em;
          text-transform: uppercase;
        }

        .gr-eyebrow::before {
          content: "";
          width: .72rem;
          height: .72rem;
          border-radius: 50%;
          background: var(--gr-red);
          box-shadow: 1.1rem 0 0 var(--gr-amber), 2.2rem 0 0 var(--gr-green);
        }

        .gr-hero h1 {
          margin: 0 !important;
          color: #fffdf5;
          font-family: "Bricolage Grotesque", "Zen Kaku Gothic New", sans-serif;
          font-size: clamp(3.1rem, 8vw, 6.9rem) !important;
          font-weight: 800 !important;
          letter-spacing: -.07em;
          line-height: .78 !important;
        }

        .gr-hero-note {
          max-width: 42rem;
          margin: 1.25rem 0 0;
          color: rgba(255, 253, 245, .76);
          font-size: 1rem;
          line-height: 1.8;
        }

        .gr-status-board {
          display: grid;
          grid-template-columns: repeat(2, minmax(0, 1fr));
          gap: .65rem;
          padding: 1rem;
          background:
            linear-gradient(135deg, rgba(255,255,255,.92), rgba(255,255,255,.72)),
            var(--gr-surface);
        }

        .gr-card {
          display: grid;
          align-content: space-between;
          min-height: 8.4rem;
          padding: 1rem;
          border: 1px solid rgba(36, 35, 31, .22);
          border-radius: 16px;
          background: rgba(255,255,255,.82);
        }

        .gr-card span {
          color: var(--gr-muted);
          font-size: .72rem;
          font-weight: 700;
          letter-spacing: .07em;
        }

        .gr-card strong {
          margin-top: .7rem;
          color: var(--gr-ink);
          font-family: "Bricolage Grotesque", "Zen Kaku Gothic New", sans-serif;
          font-size: clamp(1.75rem, 4vw, 3.15rem);
          font-weight: 800;
          letter-spacing: -.05em;
          line-height: .92;
        }

        .gr-card small {
          color: var(--gr-muted);
          font-size: .76rem;
        }

        .gr-card.is-blue { border-top: 7px solid var(--gr-blue); }
        .gr-card.is-red { border-top: 7px solid var(--gr-red); }
        .gr-card.is-green { border-top: 7px solid var(--gr-green); }
        .gr-card.is-amber { border-top: 7px solid var(--gr-amber); }

        .gr-flow {
          display: grid;
          grid-template-columns: repeat(4, minmax(0, 1fr));
          gap: .55rem;
          margin: 1rem 0 1.2rem;
          padding: 0;
          list-style: none;
          animation: gr-rise .46s .08s var(--gr-ease) both;
        }

        .gr-flow li {
          position: relative;
          min-height: 4.7rem;
          padding: .8rem .9rem .85rem;
          border: 1px solid var(--gr-ink);
          border-radius: 16px;
          background: var(--gr-surface);
          box-shadow: 4px 4px 0 rgba(36, 35, 31, .08);
        }

        .gr-flow li::after {
          content: "";
          position: absolute;
          right: .85rem;
          bottom: .75rem;
          width: 3rem;
          height: 1.25rem;
          background: repeating-linear-gradient(90deg, var(--gr-ink) 0 2px, transparent 2px 5px);
          opacity: .22;
        }

        .gr-flow b {
          display: block;
          color: var(--gr-blue);
          font-family: "Bricolage Grotesque", sans-serif;
          font-size: 1.1rem;
          line-height: 1;
        }

        .gr-flow span {
          display: block;
          margin-top: .45rem;
          color: var(--gr-ink);
          font-size: .86rem;
          font-weight: 700;
          letter-spacing: 0;
        }

        div[data-testid="stTabs"] {
          margin-top: .2rem;
          animation: gr-rise .46s .14s var(--gr-ease) both;
        }

        div[data-testid="stTabs"] [role="tablist"] {
          gap: .45rem;
          padding: .4rem;
          overflow-x: auto;
          border: 1px solid var(--gr-ink);
          border-radius: 999px;
          background: rgba(255,255,255,.82);
          box-shadow: 5px 5px 0 rgba(36, 35, 31, .08);
        }

        div[data-testid="stTabs"] button[role="tab"] {
          min-height: 2.7rem;
          padding: .55rem 1rem;
          border-radius: 999px;
          color: var(--gr-muted);
          font-weight: 800;
        }

        div[data-testid="stTabs"] button[role="tab"][aria-selected="true"] {
          background: var(--gr-ink);
          color: #fffdf5;
        }

        div[data-testid="stTabs"] button[role="tab"][aria-selected="true"] p {
          color: #fffdf5;
        }

        .gr-section-heading {
          display: grid;
          gap: .35rem;
          margin: 2rem 0 1rem;
        }

        .gr-section-eyebrow {
          margin: 0;
          color: var(--gr-blue);
          font-size: .74rem;
          font-weight: 700;
          letter-spacing: .12em;
        }

        .gr-section-heading h2 {
          margin: 0;
          color: var(--gr-ink);
          font-family: "Bricolage Grotesque", "Zen Kaku Gothic New", sans-serif;
          font-size: clamp(1.7rem, 3vw, 2.4rem);
          font-weight: 800;
          letter-spacing: -.04em;
        }

        .gr-section-heading p:last-child {
          margin: 0;
          color: var(--gr-muted);
          font-size: .95rem;
          line-height: 1.65;
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
          border: 1px solid var(--gr-line);
          border-radius: 999px;
          background: rgba(255,255,255,.82);
          color: var(--gr-muted);
          font-size: .78rem;
          font-weight: 700;
        }

        .gr-status-key i {
          display: inline-block;
          width: .62rem;
          height: .62rem;
          border-radius: 50%;
        }

        .gr-status-key .is-open i { background: var(--gr-blue); }
        .gr-status-key .is-done i { background: var(--gr-green); }
        .gr-status-key .is-none i { background: var(--gr-amber); }

        .gr-month-cell {
          display: flex;
          min-height: 44px;
          align-items: center;
          color: var(--gr-ink);
          font-size: .86rem;
          font-weight: 700;
        }

        [data-testid="stHorizontalBlock"] {
          gap: .75rem;
        }

        .stButton > button,
        .stDownloadButton > button,
        [data-testid="stLinkButton"] > a {
          min-height: 44px;
          border: 1px solid var(--gr-ink) !important;
          border-radius: 14px !important;
          background: var(--gr-surface) !important;
          color: var(--gr-ink) !important;
          font-weight: 800 !important;
          box-shadow: 3px 3px 0 rgba(36, 35, 31, .10) !important;
          transition: transform .18s var(--gr-ease), box-shadow .18s var(--gr-ease), background .18s var(--gr-ease), color .18s var(--gr-ease);
        }

        .stButton > button:hover,
        .stDownloadButton > button:hover,
        [data-testid="stLinkButton"] > a:hover {
          background: #edf2ff !important;
          color: var(--gr-blue-dark) !important;
          transform: translate(-1px, -1px);
          box-shadow: 5px 5px 0 rgba(40, 100, 240, .18) !important;
        }

        .stButton > button[kind="primary"],
        .stButton > button[data-testid="baseButton-primary"],
        [data-testid="stLinkButton"] > a[kind="primary"] {
          background: var(--gr-blue) !important;
          color: #ffffff !important;
          border-color: var(--gr-blue-dark) !important;
        }

        .stButton > button[kind="primary"]:hover,
        .stButton > button[data-testid="baseButton-primary"]:hover,
        [data-testid="stLinkButton"] > a[kind="primary"]:hover {
          background: var(--gr-blue-dark) !important;
          color: #ffffff !important;
        }

        [data-testid="stTextInput"] input,
        [data-testid="stNumberInput"] input,
        [data-testid="stDateInput"] input,
        [data-baseweb="select"] > div,
        [data-testid="stFileUploader"] section {
          min-height: 44px;
          border-color: var(--gr-ink) !important;
          border-radius: 14px !important;
          background: rgba(255,255,255,.9) !important;
          color: var(--gr-ink) !important;
          box-shadow: 3px 3px 0 rgba(36, 35, 31, .07) !important;
        }

        [data-testid="stTextInput"] label,
        [data-testid="stNumberInput"] label,
        [data-testid="stDateInput"] label,
        [data-testid="stSelectbox"] label,
        [data-testid="stFileUploader"] label {
          color: var(--gr-muted) !important;
          font-family: "IBM Plex Mono", ui-monospace, monospace;
          font-size: .78rem !important;
          font-weight: 700 !important;
        }

        [data-testid="stFileUploader"] section {
          border-style: dashed !important;
          border-width: 2px !important;
        }

        code,
        pre,
        [data-testid="stCodeBlock"] {
          font-family: "IBM Plex Mono", ui-monospace, monospace !important;
        }

        [data-testid="stCodeBlock"],
        div[data-testid="stAlert"],
        div[data-testid="stDataFrame"] {
          overflow: hidden;
          border: 1px solid var(--gr-ink);
          border-radius: 16px !important;
          background: rgba(255,255,255,.9) !important;
          box-shadow: 4px 4px 0 rgba(36, 35, 31, .08);
        }

        div[data-testid="stDataFrame"] [role="columnheader"] {
          background: var(--gr-paper-2);
          color: var(--gr-ink);
        }

        .stButton > button:focus-visible,
        .stDownloadButton > button:focus-visible,
        [data-testid="stLinkButton"] > a:focus-visible,
        input:focus-visible,
        textarea:focus-visible,
        [role="tab"]:focus-visible {
          outline: 4px solid var(--gr-red) !important;
          outline-offset: 3px !important;
        }

        @keyframes gr-rise {
          from { opacity: 0; transform: translateY(12px); }
          to { opacity: 1; transform: translateY(0); }
        }

        @media (max-width: 860px) {
          .block-container { padding: 1rem .8rem 3rem; }
          .gr-hero { grid-template-columns: 1fr; border-radius: 20px; }
          .gr-hero-title { padding: 1.45rem 1.15rem 1.1rem; }
          .gr-status-board { grid-template-columns: repeat(2, minmax(0, 1fr)); }
          .gr-card { min-height: 6.2rem; padding: .8rem; }
          .gr-flow { grid-template-columns: 1fr; }
          div[data-testid="stTabs"] [role="tablist"] { border-radius: 18px; }
          div[data-testid="stTabs"] button[role="tab"] { padding-right: .75rem; padding-left: .75rem; }
        }

        @media (max-width: 520px) {
          .gr-status-board { grid-template-columns: 1fr; }
        }

        @media (prefers-reduced-motion: reduce) {
          *, *::before, *::after {
            animation-duration: .001ms !important;
            animation-iteration-count: 1 !important;
            scroll-behavior: auto !important;
            transition-duration: .001ms !important;
          }
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

    st.markdown(
        f"""
        <header class="gr-hero">
          <div class="gr-hero-title">
            <p class="gr-eyebrow">Receipt command / Google Drive</p>
            <h1>Get<br>Receipt</h1>
            <p class="gr-hero-note">
              iPhoneで受け取った領収書と明細を、対象月・取引先・金額までそろえてGoogle Driveへ保管する個人用オペレーション画面。
            </p>
          </div>
          <div class="gr-status-board" aria-label="保存状況の概要">
            <div class="gr-card is-blue"><span>保存済ファイル</span><strong>{saved_count:02d}</strong><small>Drive archive</small></div>
            <div class="gr-card is-red"><span>未取得枠</span><strong>{open_slots:02d}</strong><small>Action queue</small></div>
            <div class="gr-card is-green"><span>保管完了枠</span><strong>{done_slots:02d}</strong><small>Monthly slots</small></div>
            <div class="gr-card is-amber"><span>現在の対象</span><strong>{current_month}</strong><small>Latest month</small></div>
          </div>
        </header>
        <ol class="gr-flow" aria-label="領収書保管の流れ">
          <li><b>01</b><span>対象月を確認</span></li>
          <li><b>02</b><span>公式サイトを開く</span></li>
          <li><b>03</b><span>ファイル名を確認</span></li>
          <li><b>04</b><span>Driveへ保存</span></li>
        </ol>
        """,
        unsafe_allow_html=True,
    )


def render_section_heading(eyebrow: str, title: str, detail: str) -> None:
    st.markdown(
        f"""
        <div class="gr-section-heading">
          <p class="gr-section-eyebrow">{eyebrow}</p>
          <h2>{title}</h2>
          <p>{detail}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_dashboard() -> None:
    render_section_heading("Archive index", TEXT["dashboard"], "対象の月とサービスを選択")
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
            button_type = "primary" if label == TEXT["unfetched"] else "secondary"
            if row[index].button(
                label,
                key=f"run:{target_month}:{service.id}",
                type=button_type,
                use_container_width=True,
            ):
                select_for_acquisition(service.id, target_month)
                st.success(
                    f"{service.label} / {month_label(target_month)}を選択しました。"
                    "「取得開始」で公式サイトを開いてください。"
                )


def select_for_acquisition(service_id: str, target_month: str) -> None:
    st.session_state["acq_service"] = service_id
    st.session_state["acq_month"] = target_month
    st.session_state["manual_service"] = service_id
    st.session_state["manual_month"] = target_month


def render_acquisition_form() -> None:
    render_section_heading("Source", "公式サイトから取得", "サービスと対象月を選択")
    months = selectable_months()
    service_ids = [service.id for service in SERVICES]
    selected_service = st.selectbox(
        TEXT["service"],
        service_ids,
        format_func=lambda value: service_by_id(value).label,
        key="acq_service",
    )
    selected_month = st.selectbox(
        TEXT["target_month"],
        months,
        index=len(months) - 1,
        format_func=month_label,
        key="acq_month",
    )
    service = service_by_id(selected_service)
    st.link_button(f"{service.label}の公式サイトを開く", service.portal_url, type="primary", use_container_width=True)
    st.info(
        f"{month_label(selected_month)}の領収書・明細をダウンロード後、"
        "「手動登録」でGoogle Driveへ保存します。"
    )


def render_manual_upload() -> None:
    render_section_heading("Intake", "ファイルを保管", "iPhoneのファイルをGoogle Driveへ保存")

    months = selectable_months()
    service_ids = [service.id for service in SERVICES]
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
            index=len(months) - 1,
            format_func=month_label,
            key="manual_month",
        )
        transaction_date = st.date_input("取引日 / 発行日", value=date.today())
    with right:
        service = service_by_id(service_id)
        partner_name = st.text_input("取引先", value=service.default_partner)
        amount_yen = st.number_input("金額（円）", min_value=0, step=1, value=0)
        source_url = st.text_input("取得元URL", value="")

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

tabs = st.tabs([TEXT["dashboard"], TEXT["start"], TEXT["manual"], TEXT["ledger"], TEXT["settings"]])
with tabs[0]:
    render_dashboard()
with tabs[1]:
    render_acquisition_form()
with tabs[2]:
    render_manual_upload()
with tabs[3]:
    render_history()
with tabs[4]:
    render_settings()
