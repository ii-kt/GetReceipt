from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import streamlit as st

from ..config import month_label
from ..jobs.client import WorkerApiError, WorkerClient
from ..workflows.manual_upload import (
    ManualUploadError,
    inspect_manual_receipt,
    save_manual_receipt,
)


def render_manual_upload(
    *,
    storage: Any,
    target_month: str,
    missing_services: Sequence[Any],
    acquisition_active: Callable[[], bool] | None = None,
    worker_client: WorkerClient | None = None,
) -> None:
    if storage is None or not missing_services:
        return
    with st.expander("iPhoneからPDFを手動追加", expanded=False):
        st.caption(
            "公式サイトが遠隔ログインを許可しない場合の最終手段です。"
            "iPhoneの「ファイル」からPDFを選ぶと、同じDrive保存・重複確認処理を使います。"
        )
        services = {str(service.id): service for service in missing_services}
        service_id = st.selectbox(
            "追加する領収書",
            tuple(services),
            format_func=lambda value: services[value].label,
            key=f"manual_upload_service_{target_month}",
        )
        uploaded = st.file_uploader(
            f"{month_label(target_month)}のPDF",
            type=("pdf",),
            accept_multiple_files=False,
            key=f"manual_upload_file_{target_month}_{service_id}",
        )
        inspection = None
        content = b""
        if uploaded is not None:
            try:
                content = bytes(uploaded.getvalue())
                inspection = inspect_manual_receipt(
                    service_id=service_id,
                    target_month=target_month,
                    content=content,
                )
            except ManualUploadError as error:
                st.error(str(error))
            else:
                st.success(f"請求金額を {inspection.amount_yen:,}円 と確認しました。")
                for warning in inspection.warnings:
                    st.warning(warning)

        confirmed = False
        if inspection is not None and inspection.requires_confirmation:
            confirmed = st.checkbox(
                "PDFを目視し、選択した請求元・対象月で正しいことを確認しました",
                key=f"manual_upload_confirm_{target_month}_{service_id}",
            )

        if st.button(
            "Google Driveへ保存",
            type="primary",
            use_container_width=True,
            disabled=inspection is None or (inspection.requires_confirmation and not confirmed),
            key=f"manual_upload_save_{target_month}_{service_id}",
            icon=":material/upload_file:",
        ):
            if acquisition_active is not None and acquisition_active():
                st.error("自動取得を中止してから手動PDFを保存してください。")
                return
            try:
                if worker_client is not None:
                    result = worker_client.upload_manual_receipt(
                        service_id=service_id,
                        target_month=target_month,
                        content=content,
                        confirmed=confirmed,
                    )
                    successful = result.get("success") is True
                else:
                    result = save_manual_receipt(
                        service_id=service_id,
                        target_month=target_month,
                        content=content,
                        original_file_name=str(
                            getattr(uploaded, "name", "") or "iphone-upload.pdf"
                        ),
                        storage=storage,
                        confirmed=confirmed,
                    )
                    successful = bool(getattr(result, "success", False))
            except ManualUploadError as error:
                st.error(str(error))
            except WorkerApiError as error:
                st.error(str(error))
            except Exception:
                st.error("Google Driveへの手動保存に失敗しました。")
            else:
                if successful:
                    st.success("Google Driveへの保存を確認しました。")
                    st.rerun()
                else:
                    failure = getattr(result, "failure", None)
                    st.error(
                        str(getattr(failure, "message", "") or "PDFを保存できませんでした。")
                    )
