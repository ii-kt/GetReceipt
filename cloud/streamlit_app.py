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
    "caption": "iPhone\u304b\u3089PC\u3092\u8d77\u52d5\u305b\u305a\u306b\u3001\u9818\u53ce\u66f8\u30fb\u660e\u7d30\u3092Google Drive\u3078\u4fdd\u5b58\u3059\u308b\u30af\u30e9\u30a6\u30c9\u7248\u3067\u3059\u3002",
    "dashboard": "\u53d6\u5f97\u72b6\u6cc1",
    "manual": "\u624b\u52d5\u767b\u9332",
    "ledger": "\u4fdd\u5b58\u53f0\u5e33",
    "settings": "\u8a2d\u5b9a",
    "target_month": "\u5bfe\u8c61\u6708",
    "service": "\u30b5\u30fc\u30d3\u30b9",
    "unfetched": "\u672a\u53d6\u5f97",
    "uploaded": "\u53d6\u5f97\u6e08",
    "not_issued": "\u672a\u767a\u884c",
    "action_needed": "\u8981\u5bfe\u5fdc",
    "start": "\u53d6\u5f97\u958b\u59cb",
    "save_drive": "Drive\u3078\u4fdd\u5b58",
    "mark_not_issued": "\u672a\u767a\u884c\u3068\u3057\u3066\u8a18\u9332",
}


def inject_design() -> None:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Noto+Sans+JP:wght@400;500;700;800&family=JetBrains+Mono:wght@500;600&display=swap');

        :root {
          --gr-bg: #f6f8fc;
          --gr-surface: #ffffff;
          --gr-surface-raised: #fbfdff;
          --gr-text: #172033;
          --gr-muted: #667085;
          --gr-faint: #dbe3ef;
          --gr-accent: #2563eb;
          --gr-accent-2: #0f9f8e;
          --gr-warning: #d98a11;
          --gr-success: #198754;
          --gr-radius: 8px;
          --gr-shadow: 0 10px 30px rgba(18, 32, 56, 0.08);
          --gr-shadow-soft: 0 4px 14px rgba(18, 32, 56, 0.06);
          --gr-ease: cubic-bezier(.2, .8, .2, 1);
        }

        html {
          color-scheme: light;
          scroll-behavior: smooth;
        }

        body,
        .stApp {
          font-family: "Inter", "Noto Sans JP", system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
          color: var(--gr-text);
          background:
            radial-gradient(circle at 12% 0%, rgba(37, 99, 235, 0.08), transparent 32rem),
            radial-gradient(circle at 92% 8%, rgba(15, 159, 142, 0.08), transparent 28rem),
            linear-gradient(180deg, #fbfdff 0%, var(--gr-bg) 24rem);
        }

        [data-testid="stHeader"] {
          background: rgba(246, 248, 252, 0.72);
          backdrop-filter: blur(18px);
          border-bottom: 1px solid rgba(219, 227, 239, 0.72);
        }

        .block-container {
          max-width: 1180px;
          padding: 2.4rem 1.35rem 4rem;
        }

        .block-container > div {
          animation: gr-rise .52s var(--gr-ease) both;
        }

        .block-container > div:nth-child(2) { animation-delay: .05s; }
        .block-container > div:nth-child(3) { animation-delay: .1s; }
        .block-container > div:nth-child(4) { animation-delay: .15s; }

        h1 {
          position: relative;
          width: fit-content;
          margin-bottom: .25rem !important;
          letter-spacing: 0;
          font-family: "Inter", "Noto Sans JP", system-ui, sans-serif;
          font-size: 3.4rem !important;
          font-weight: 800 !important;
          line-height: .95 !important;
          color: var(--gr-text);
        }

        h1::after {
          content: "";
          display: block;
          width: min(68vw, 520px);
          height: 4px;
          margin-top: 1rem;
          border-radius: 999px;
          background: linear-gradient(90deg, var(--gr-accent), var(--gr-accent-2), #91b7ff, var(--gr-accent));
          background-size: 220% 100%;
          animation: gr-rail 5.5s linear infinite;
          box-shadow: 0 8px 24px rgba(37, 99, 235, 0.18);
        }

        h2, h3, [data-testid="stMarkdownContainer"] strong {
          letter-spacing: 0;
        }

        [data-testid="stCaptionContainer"] {
          max-width: 720px;
          color: var(--gr-muted);
          font-size: 1.02rem;
          line-height: 1.8;
        }

        [data-testid="stVerticalBlock"] > [style*="flex-direction: column;"] > [data-testid="stVerticalBlock"] {
          gap: .85rem;
        }

        div[data-testid="stTabs"] {
          margin-top: 1.4rem;
        }

        div[data-testid="stTabs"] [role="tablist"] {
          gap: .35rem;
          padding: .35rem;
          width: fit-content;
          max-width: 100%;
          overflow-x: auto;
          border: 1px solid rgba(219, 227, 239, .9);
          border-radius: var(--gr-radius);
          background: rgba(255, 255, 255, .78);
          box-shadow: var(--gr-shadow-soft);
        }

        div[data-testid="stTabs"] button[role="tab"] {
          min-height: 2.45rem;
          padding: .45rem .85rem;
          border-radius: 7px;
          color: var(--gr-muted);
          font-weight: 700;
          transition: color .2s ease, background .2s ease, box-shadow .2s ease;
        }

        div[data-testid="stTabs"] button[role="tab"][aria-selected="true"] {
          color: var(--gr-text);
          background: #eef4ff;
          box-shadow: inset 0 0 0 1px rgba(37, 99, 235, .16);
        }

        div[data-testid="stTabs"] button[role="tab"] p {
          font-size: .92rem;
        }

        [data-testid="stVerticalBlockBorderWrapper"],
        [data-testid="stForm"],
        [data-testid="stFileUploader"],
        div[data-testid="stDataFrame"],
        div[data-testid="stAlert"] {
          border-radius: var(--gr-radius) !important;
        }

        div[data-testid="stDataFrame"] {
          overflow: hidden;
          border: 1px solid rgba(219, 227, 239, .9);
          box-shadow: var(--gr-shadow-soft);
        }

        [data-testid="stMetric"] {
          padding: 1rem;
          border: 1px solid rgba(219, 227, 239, .9);
          border-radius: var(--gr-radius);
          background: var(--gr-surface);
          box-shadow: var(--gr-shadow-soft);
        }

        .stButton > button,
        .stDownloadButton > button,
        a[data-testid="stLinkButton"] {
          border-radius: var(--gr-radius) !important;
          border: 1px solid rgba(37, 99, 235, .14) !important;
          background: linear-gradient(180deg, #ffffff 0%, #f6f9ff 100%) !important;
          color: var(--gr-text) !important;
          font-weight: 700 !important;
          letter-spacing: 0 !important;
          box-shadow: 0 1px 0 rgba(255, 255, 255, .9) inset, var(--gr-shadow-soft);
          transition: transform .2s ease, box-shadow .2s ease, border-color .2s ease, background .2s ease;
        }

        .stButton > button:hover,
        .stDownloadButton > button:hover,
        a[data-testid="stLinkButton"]:hover {
          transform: translateY(-1px);
          border-color: rgba(37, 99, 235, .38) !important;
          box-shadow: 0 12px 28px rgba(37, 99, 235, .12);
        }

        .stButton > button:active,
        .stDownloadButton > button:active,
        a[data-testid="stLinkButton"]:active {
          transform: translateY(0) scale(.99);
        }

        .stButton > button[kind="primary"],
        .stButton > button[data-testid="baseButton-primary"] {
          background: linear-gradient(135deg, var(--gr-accent), #174ccf) !important;
          color: #ffffff !important;
          border-color: rgba(37, 99, 235, .8) !important;
          box-shadow: 0 16px 30px rgba(37, 99, 235, .22);
        }

        .stButton > button:focus-visible,
        .stDownloadButton > button:focus-visible,
        a[data-testid="stLinkButton"]:focus-visible,
        input:focus-visible,
        textarea:focus-visible,
        [role="tab"]:focus-visible {
          outline: 3px solid rgba(37, 99, 235, .32) !important;
          outline-offset: 2px !important;
        }

        [data-testid="stTextInput"] input,
        [data-testid="stNumberInput"] input,
        [data-testid="stDateInput"] input,
        [data-baseweb="select"] > div,
        [data-testid="stFileUploader"] section {
          border-radius: var(--gr-radius) !important;
          border-color: rgba(219, 227, 239, .95) !important;
          background: rgba(255, 255, 255, .9) !important;
          box-shadow: 0 1px 0 rgba(255,255,255,.7) inset;
        }

        [data-testid="stTextInput"] label,
        [data-testid="stNumberInput"] label,
        [data-testid="stDateInput"] label,
        [data-testid="stSelectbox"] label,
        [data-testid="stRadio"] label,
        [data-testid="stFileUploader"] label {
          color: var(--gr-muted) !important;
          font-size: .82rem !important;
          font-weight: 800 !important;
          text-transform: uppercase;
          letter-spacing: .08em;
        }

        code,
        pre,
        [data-testid="stCodeBlock"] {
          font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Consolas, monospace !important;
          border-radius: var(--gr-radius) !important;
        }

        [data-testid="stProgress"] > div > div > div {
          background: linear-gradient(90deg, var(--gr-accent), var(--gr-accent-2)) !important;
        }

        [data-testid="stSpinner"] {
          color: var(--gr-accent) !important;
        }

        div[data-testid="stAlert"] {
          border: 1px solid rgba(219, 227, 239, .9);
          box-shadow: var(--gr-shadow-soft);
        }

        [data-testid="stImage"] img {
          border-radius: var(--gr-radius);
          border: 1px solid rgba(219, 227, 239, .9);
          box-shadow: var(--gr-shadow);
        }

        @keyframes gr-rise {
          from { opacity: 0; transform: translateY(10px); }
          to { opacity: 1; transform: translateY(0); }
        }

        @keyframes gr-rail {
          from { background-position: 0% 50%; }
          to { background-position: 220% 50%; }
        }

        @media (max-width: 760px) {
          .block-container {
            padding: 1.35rem .85rem 2.6rem;
          }

          h1 {
            font-size: 2.35rem !important;
          }

          h1::after {
            width: 82vw;
          }

          div[data-testid="stTabs"] [role="tablist"] {
            width: 100%;
          }
        }

        @media (prefers-reduced-motion: reduce) {
          *,
          *::before,
          *::after {
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


def render_dashboard() -> None:
    st.subheader(TEXT["dashboard"])
    records = ledger().read()
    latest = latest_status_by_month(records)
    months = list(reversed(selectable_months()))

    header_cols = st.columns([1.35, 1, 1, 1, 1])
    header_cols[0].markdown(f"**{TEXT['target_month']}**")
    for index, service in enumerate(SERVICES, start=1):
        header_cols[index].markdown(f"**{service.label}**")

    for target_month in months:
        row = st.columns([1.35, 1, 1, 1, 1])
        row[0].write(month_label(target_month))
        for index, service in enumerate(SERVICES, start=1):
            record = latest.get((target_month, service.id))
            label = status_text(record)
            if row[index].button(label, key=f"run:{target_month}:{service.id}", use_container_width=True):
                select_for_acquisition(service.id, target_month)
                st.success(
                    f"{service.label} / {month_label(target_month)}\u3092\u9078\u629e\u3057\u307e\u3057\u305f\u3002"
                    "\u300c\u53d6\u5f97\u958b\u59cb\u300d\u3067\u516c\u5f0f\u30b5\u30a4\u30c8\u3092\u958b\u3044\u3066\u304f\u3060\u3055\u3044\u3002"
                )


def select_for_acquisition(service_id: str, target_month: str) -> None:
    """Keep the chosen receipt context while the user moves between tabs."""
    st.session_state["acq_service"] = service_id
    st.session_state["acq_month"] = target_month
    st.session_state["manual_service"] = service_id
    st.session_state["manual_month"] = target_month


def render_acquisition_form() -> None:
    st.subheader(TEXT["start"])
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
    st.link_button(f"{service.label}\u306e\u516c\u5f0f\u30b5\u30a4\u30c8\u3092\u958b\u304f", service.portal_url, type="primary", use_container_width=True)
    st.info(
        f"{month_label(selected_month)}\u306e\u9818\u53ce\u66f8\u30fb\u660e\u7d30\u3092\u30c0\u30a6\u30f3\u30ed\u30fc\u30c9\u5f8c\u3001"
        "\u300c\u624b\u52d5\u767b\u9332\u300d\u3067Google Drive\u3078\u4fdd\u5b58\u3057\u307e\u3059\u3002"
    )


def render_manual_upload() -> None:
    st.subheader(TEXT["manual"])
    st.caption(
        "iPhone\u306b\u30c0\u30a6\u30f3\u30ed\u30fc\u30c9\u3057\u305f\u9818\u53ce\u66f8\u30fb\u660e\u7d30\u3092\u3001"
        "\u6b63\u3057\u3044\u540d\u524d\u3067Google Drive\u306b\u4fdd\u5b58\u3057\u307e\u3059\u3002"
    )

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
        transaction_date = st.date_input("\u53d6\u5f15\u65e5 / \u767a\u884c\u65e5", value=date.today())
    with right:
        service = service_by_id(service_id)
        partner_name = st.text_input("\u53d6\u5f15\u5148", value=service.default_partner)
        amount_yen = st.number_input("\u91d1\u984d\uff08\u5186\uff09", min_value=0, step=1, value=0)
        source_url = st.text_input("\u53d6\u5f97\u5143URL", value="")

    uploaded_file = st.file_uploader("\u4fdd\u5b58\u3059\u308bPDF/CSV/\u753b\u50cf", type=["pdf", "csv", "png", "jpg", "jpeg"])
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
            st.error("\u4fdd\u5b58\u3059\u308b\u30d5\u30a1\u30a4\u30eb\u3092\u9078\u629e\u3057\u3066\u304f\u3060\u3055\u3044\u3002")
            return
        if amount_yen <= 0:
            st.error("\u91d1\u984d\u3092\u5165\u529b\u3057\u3066\u304f\u3060\u3055\u3044\u3002")
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
            st.error(f"Google Drive\u3078\u306e\u4fdd\u5b58\u306b\u5931\u6557\u3057\u307e\u3057\u305f: {error}")
            return
        st.success("Google Drive\u3078\u4fdd\u5b58\u3057\u307e\u3057\u305f\u3002")
        if saved.get("drive_web_view_link"):
            st.link_button("Drive\u3067\u958b\u304f", saved["drive_web_view_link"])

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
            st.error(f"\u8a18\u9332\u306b\u5931\u6557\u3057\u307e\u3057\u305f: {error}")
            return
        st.success("\u672a\u767a\u884c\u3068\u3057\u3066\u8a18\u9332\u3057\u307e\u3057\u305f\u3002")


def render_history() -> None:
    st.subheader(TEXT["ledger"])
    records = ledger().read()
    if not records:
        st.info("\u307e\u3060\u4fdd\u5b58\u5c65\u6b74\u306f\u3042\u308a\u307e\u305b\u3093\u3002")
        return
    st.dataframe(records, use_container_width=True, hide_index=True)
    st.download_button(
        "\u53f0\u5e33CSV\u3092\u30c0\u30a6\u30f3\u30ed\u30fc\u30c9",
        data=ledger().to_csv_bytes(),
        file_name="receipt_index.csv",
        mime="text/csv",
    )


def render_settings() -> None:
    st.subheader(TEXT["settings"])
    st.write("Google Drive\u4fdd\u5b58\u5148")
    st.code(RECEIPT_DRIVE_FOLDER_ID, language="text")
    st.link_button("\u9818\u53ce\u66f8\u30d5\u30a9\u30eb\u30c0\u3092\u958b\u304f", RECEIPT_DRIVE_FOLDER_URL)

    if secrets_configured():
        st.success("Google Drive\u7528\u306eStreamlit Secrets\u304c\u8a2d\u5b9a\u3055\u308c\u3066\u3044\u307e\u3059\u3002")
    else:
        st.warning("Google Drive\u7528\u306eStreamlit Secrets\u304c\u672a\u8a2d\u5b9a\u3067\u3059\u3002")
    st.caption("\u4fdd\u5b58\u53f0\u5e33\u306fGoogle Drive\u306e `_receipt_index.csv` \u306b\u3082\u540c\u671f\u3055\u308c\u307e\u3059\u3002")


def _mime_type(extension: str) -> str:
    return {
        "pdf": "application/pdf",
        "csv": "text/csv",
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
    }.get(extension.lower(), "application/octet-stream")


inject_design()

st.title(TEXT["title"])
st.caption(TEXT["caption"])

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
