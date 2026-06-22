Warning: truncated output (original token count: 6857)
Total output lines: 764

from __future__ import annotations

from datetime import date
from io import BytesIO
import time

import streamlit as st

from src.backend_client import BackendClient, BackendError
from src.config import (
    LEDGER_PATH,
    RECEIPT_DRIVE_FOLDER_ID,
    RECEIPT_DRIVE_FOLDER_URL,
    SERVICES,
    month_label,
    parse_month_key,
    selectable_months,
    service_by_id,
)
from src.drive_storage import DriveConfigError
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
    upload_backend_record_to_drive,
)


try:
    from PIL import Image
    from streamlit_image_coordinates import streamlit_image_coordinates
except Exception:
    Image = None
    streamlit_image_coordinates = None


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
    "browser": "\u53d6\u5f97\u7528\u30d6\u30e9\u30a6\u30b6",
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
          font-family: "Inter", "…4857 tokens truncated…nt != last:
                    try:
                        client.click(service=service_id, x=point[0], y=point[1])
                        st.session_state["last_browser_click"] = point
                        time.sleep(0.5)
                        refresh_browser_image(client, service_id)
                    except BackendError as error:
                        st.error(str(error))
        else:
            st.image(image_bytes)
            st.info("streamlit-image-coordinates\u304c\u672a\u30a4\u30f3\u30b9\u30c8\u30fc\u30eb\u306e\u305f\u3081\u3001\u5ea7\u6a19\u5165\u529b\u3067\u64cd\u4f5c\u3057\u307e\u3059\u3002")

    click_cols = st.columns([1, 1, 1])
    x = click_cols[0].number_input("X", min_value=0, value=0, step=1)
    y = click_cols[1].number_input("Y", min_value=0, value=0, step=1)
    if click_cols[2].button("\u5ea7\u6a19\u3092\u30af\u30ea\u30c3\u30af", use_container_width=True):
        try:
            client.click(service=service_id, x=int(x), y=int(y))
            refresh_browser_image(client, service_id)
        except BackendError as error:
            st.error(str(error))

    text = st.text_input("\u30c6\u30ad\u30b9\u30c8\u5165\u529b", type="password")
    input_cols = st.columns([1, 1, 2])
    if input_cols[0].button("\u5165\u529b", use_container_width=True):
        try:
            client.text(service=service_id, text=text)
            refresh_browser_image(client, service_id)
        except BackendError as error:
            st.error(str(error))
    if input_cols[1].button("Enter", use_container_width=True):
        try:
            client.key(service=service_id, key="Enter")
            refresh_browser_image(client, service_id)
        except BackendError as error:
            st.error(str(error))


def refresh_browser_image(client: BackendClient, service_id: str) -> None:
    try:
        st.session_state["browser_image"] = client.screenshot(service=service_id)
    except BackendError as error:
        st.warning(str(error))


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

    try:
        url = backend_client().ensure_started()
        st.success(f"Node backend: {url}")
    except BackendError as error:
        st.warning(f"Node backend: {error}")

    st.write("\u53f0\u5e33\u30d5\u30a1\u30a4\u30eb")
    st.code(str(LEDGER_PATH), language="text")


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

tabs = st.tabs([TEXT["dashboard"], TEXT["start"], TEXT["manual"], TEXT["browser"], TEXT["ledger"], TEXT["settings"]])
with tabs[0]:
    render_dashboard()
with tabs[1]:
    render_acquisition_form()
with tabs[2]:
    render_manual_upload()
with tabs[3]:
    render_browser_panel()
with tabs[4]:
    render_history()
with tabs[5]:
    render_settings()

