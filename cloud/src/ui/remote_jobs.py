from __future__ import annotations

import hashlib
import re
import unicodedata
from io import BytesIO
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping

import streamlit as st

from ..config import service_by_id
from ..jobs.client import WorkerApiError, WorkerClient


ACTIVE_STATES = {"queued", "running", "waiting_for_challenge"}
TERMINAL_STATES = {
    "succeeded",
    "failed",
    "intervention_required",
    "cancelled",
}
INPUT_CHALLENGE_KINDS = {
    "otp_email",
    "otp_sms",
    "otp_totp",
    "security_code",
    "verification_code",
}
PUSH_CHALLENGE_KINDS = {"push_approval"}
INTERACTIVE_CHALLENGE_KINDS = {
    "captcha_interactive",
    "consent_interactive",
    "security_question_interactive",
}
UNAVAILABLE_CHALLENGE_KINDS = {
    "passkey_unavailable",
    "passkey_platform",
    "unknown",
}
_SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")


@dataclass(frozen=True)
class RemoteJobView:
    job: dict[str, Any] | None
    api_error: WorkerApiError | None = None

    @property
    def active(self) -> bool:
        return job_state(self.job) in ACTIVE_STATES


def job_state(job: Mapping[str, Any] | None) -> str:
    if not isinstance(job, Mapping):
        return ""
    return str(job.get("state") or "").strip().lower()


def job_id_from_query_params(query_params: Mapping[str, Any]) -> str:
    value = query_params.get("job", "")
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    normalized = str(value or "").strip()
    return normalized if _SAFE_JOB_ID.fullmatch(normalized) else ""


def recover_remote_job(
    client: WorkerClient,
    *,
    target_month: str,
    query_params: Mapping[str, Any],
) -> RemoteJobView:
    requested_job_id = job_id_from_query_params(query_params)
    requested: dict[str, Any] | None = None
    try:
        if requested_job_id:
            try:
                requested = client.get_job(requested_job_id)
            except WorkerApiError as error:
                if error.status_code != 404 and error.code != "JOB_NOT_FOUND":
                    raise
            if (
                requested is not None
                and str(requested.get("target_month") or "") == target_month
                and job_state(requested) in ACTIVE_STATES
            ):
                return RemoteJobView(requested)
        active = client.find_active_job(target_month)
        if active is not None:
            return RemoteJobView(active)
        if (
            requested is not None
            and str(requested.get("target_month") or "") == target_month
        ):
            return RemoteJobView(requested)
        return RemoteJobView(None)
    except WorkerApiError as error:
        return RemoteJobView(None, api_error=error)


def idempotency_key(
    *,
    target_month: str,
    service_ids: list[str] | tuple[str, ...],
    previous_job_id: str = "",
) -> str:
    material = "\n".join(
        (
            str(target_month),
            ",".join(str(service_id) for service_id in service_ids),
            str(previous_job_id or ""),
        )
    ).encode("utf-8")
    return "mobile-" + hashlib.sha256(material).hexdigest()


def validate_challenge_response(challenge: Mapping[str, Any], value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip()
    schema = challenge.get("input_schema")
    if not isinstance(schema, Mapping):
        schema = {}
    minimum = _bounded_int(schema.get("min_length"), default=1, lower=1, upper=128)
    maximum = _bounded_int(schema.get("max_length"), default=12, lower=minimum, upper=128)
    if not minimum <= len(normalized) <= maximum:
        raise ValueError(f"入力は{minimum}〜{maximum}文字で確認してください。")
    pattern = str(schema.get("pattern") or r"^[0-9A-Za-z-]+$")
    if len(pattern) > 256:
        raise ValueError("追加認証の入力形式が不正です。")
    try:
        matches = re.fullmatch(pattern, normalized)
    except re.error as error:
        raise ValueError("追加認証の入力形式が不正です。") from error
    if matches is None:
        raise ValueError(str(schema.get("validation_message") or "入力形式を確認してください。"))
    return normalized


def service_progress_job(job: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(job, Mapping):
        return None
    service_ids = [str(value) for value in job.get("service_ids") or ()]
    completed = [str(value) for value in job.get("completed_service_ids") or ()]
    result = job.get("result")
    result_map = result if isinstance(result, Mapping) else {}
    raw_failed = job.get("failed_service_ids")
    if raw_failed is None:
        raw_failed = result_map.get("failed_service_ids")
    failed_service_ids = [
        str(value)
        for value in raw_failed or ()
        if str(value) in service_ids
    ]
    raw_failures = job.get("service_failures")
    if not isinstance(raw_failures, Mapping):
        raw_failures = result_map.get("service_failures")
    failure_map = raw_failures if isinstance(raw_failures, Mapping) else {}
    failures = {
        service_id: {
            "code": str(
                (failure_map.get(service_id) or {}).get("code")
                or "ACQUISITION_FAILED"
            ),
            "message": "このサービスの自動取得を完了できませんでした。",
        }
        for service_id in failed_service_ids
        if isinstance(failure_map.get(service_id), Mapping)
    }
    current_service = str(job.get("current_service_id") or "")
    return {
        "service_ids": service_ids,
        "completed": completed,
        "failed_service_ids": failed_service_ids,
        "failed": failures,
        "current_service": current_service,
    }


@st.fragment(run_every="5s")
def render_remote_month(
    *,
    client: WorkerClient,
    target_month: str,
    receipts: dict[str, Any],
    missing_service_ids: list[str],
    render_hero: Callable[..., None],
    render_progress: Callable[..., None],
    render_rows: Callable[..., None],
    acquirable_service_ids: list[str] | None = None,
) -> None:
    start_service_ids = (
        list(missing_service_ids)
        if acquirable_service_ids is None
        else list(acquirable_service_ids)
    )
    view = recover_remote_job(
        client,
        target_month=target_month,
        query_params=st.query_params,
    )
    if view.api_error is not None:
        saved_count = sum(value is not None for value in receipts.values())
        render_hero(
            saved_count=saved_count,
            detail="常設ワーカーへ接続できません",
            state="failed",
        )
        st.error(
            f"{view.api_error}（{view.api_error.code}）",
            icon=":material/cloud_off:",
        )
        render_rows(receipts, target_month, None)
        return

    job = view.job
    state = job_state(job)
    saved_count = sum(value is not None for value in receipts.values())
    failed_service_ids = [
        str(value)
        for value in (job or {}).get("failed_service_ids") or ()
    ]
    current_service_id = str((job or {}).get("current_service_id") or "")
    current_label = ""
    if current_service_id:
        try:
            current_label = service_by_id(current_service_id).label
        except KeyError:
            current_label = current_service_id

    if state == "waiting_for_challenge":
        detail = f"{current_label or '取得先'}の本人確認をiPhoneで待っています"
        hero_state = "running"
    elif state in {"queued", "running"}:
        detail = f"{current_label or '未取得PDF'}を常設ワーカーで取得しています"
        hero_state = "running"
    elif saved_count == len(receipts):
        detail = f"{len(receipts)}件すべてのPDFをGoogle Driveで確認しました"
        hero_state = "complete"
    elif state == "failed" and failed_service_ids:
        detail = (
            f"取得できたPDFを保存し、{len(failed_service_ids)}件は再試行が必要です"
        )
        hero_state = "failed"
    elif state in {"failed", "intervention_required"}:
        detail = "常設ワーカーが安全に停止しました"
        hero_state = "failed"
    else:
        detail = f"未取得は{len(missing_service_ids)}件です"
        hero_state = "ready"

    render_hero(saved_count=saved_count, detail=detail, state=hero_state)
    render_progress(
        completed=saved_count,
        active_label=current_label if state in ACTIVE_STATES else "",
        state=(
            "running"
            if state in ACTIVE_STATES
            else ("complete" if saved_count == len(receipts) else "idle")
        ),
    )

    if job and str(job.get("id") or ""):
        if st.query_params.get("job") != job["id"]:
            st.query_params["job"] = str(job["id"])
        updated = _format_timestamp(str(job.get("updated_at") or ""))
        if updated:
            st.caption(f"常設ワーカー最終更新: {updated}")

    error = (job or {}).get("error")
    if state in {"failed", "intervention_required"} and isinstance(error, Mapping):
        st.error(
            str(error.get("message") or "自動取得を完了できませんでした。"),
            icon=":material/error:",
        )
        if error.get("code"):
            st.caption(f"診断コード: {error['code']}")

    challenge = (job or {}).get("challenge")
    if state == "waiting_for_challenge" and isinstance(challenge, Mapping):
        _render_challenge(client=client, job=job or {}, challenge=challenge)

    if state in {"queued", "running"}:
        st.info(
            "この画面を閉じても常設ワーカーは処理を続けます。iPhoneからいつでも同じ利用月を開き直せます。",
            icon=":material/cloud_sync:",
        )

    worker_status = (job or {}).get("worker")
    if (
        state in ACTIVE_STATES
        and isinstance(worker_status, Mapping)
        and not bool(worker_status.get("running", False))
    ):
        st.error(
            "常設ワーカーの処理スレッドが停止しています。ホストの再起動またはログ確認が必要です。",
            icon=":material/heart_broken:",
        )

    if state in TERMINAL_STATES and missing_service_ids and job:
        refresh_key = f"remote_terminal_refresh_{job.get('id')}_{job.get('version')}"
        if not st.session_state.get(refresh_key):
            st.session_state[refresh_key] = True
            st.rerun()

    if start_service_ids and state not in ACTIVE_STATES:
        previous_job_id = str((job or {}).get("id") or "") if state in TERMINAL_STATES else ""
        label = "安全に再試行" if state in {"failed", "intervention_required", "cancelled"} else "iPhoneから自動取得"
        if st.button(
            label,
            type="primary",
            use_container_width=True,
            icon=":material/cloud_download:",
            key=f"remote_start_{target_month}",
        ):
            try:
                created = client.create_job(
                    target_month=target_month,
                    service_ids=start_service_ids,
                    idempotency_key=idempotency_key(
                        target_month=target_month,
                        service_ids=start_service_ids,
                        previous_job_id=previous_job_id,
                    ),
                )
            except WorkerApiError as error:
                st.error(f"{error}（{error.code}）")
            else:
                st.query_params["job"] = str(created["id"])
                st.rerun()

    if state in ACTIVE_STATES:
        col_refresh, col_cancel = st.columns(2)
        with col_refresh:
            if st.button(
                "最新状態に更新",
                use_container_width=True,
                icon=":material/refresh:",
                key=f"remote_refresh_{target_month}",
            ):
                st.rerun()
        with col_cancel:
            if st.button(
                "取得を中止",
                use_container_width=True,
                icon=":material/stop_circle:",
                key=f"remote_cancel_{target_month}",
            ):
                try:
                    client.cancel_job(str(job["id"]))
                except WorkerApiError as error:
                    st.error(f"{error}（{error.code}）")
                else:
                    st.rerun()

    progress_job = (
        service_progress_job(job)
        if state in ACTIVE_STATES or failed_service_ids
        else None
    )
    render_rows(receipts, target_month, progress_job)


def render_microsoft_connection(
    client: WorkerClient,
    *,
    required: bool,
) -> bool:
    if not required:
        return True
    try:
        status = client.microsoft_oauth_status()
    except WorkerApiError as error:
        st.error(f"Microsoftメール接続状態を確認できません: {error}")
        return False
    if not bool(status.get("configured")):
        st.error(
            "常設ワーカーにMicrosoft OAuthが設定されていません。"
            "トクテンでんきはiPhoneからの手動PDF追加を利用できます。",
            icon=":material/mail_lock:",
        )
        return False
    if bool(status.get("connected")):
        st.success(
            "MicrosoftメールはMail.Readの読み取り専用権限で接続済みです。",
            icon=":material/mark_email_read:",
        )
        disconnect_key = "microsoft_disconnect_confirmed"
        confirmed = st.checkbox(
            "接続中のMicrosoftアカウントを解除する",
            key=disconnect_key,
        )
        if st.button(
            "Microsoftアカウントを変更",
            use_container_width=True,
            disabled=not confirmed,
            key="microsoft_oauth_disconnect",
            icon=":material/sync_lock:",
        ):
            try:
                client.disconnect_microsoft_oauth()
            except WorkerApiError as error:
                st.error(str(error))
            else:
                st.session_state.pop("microsoft_authorization_url", None)
                st.session_state.pop(disconnect_key, None)
                st.success("Microsoftメール接続を解除しました。再接続してください。")
                st.rerun()
        return True

    st.warning(
        "トクテンでんきの添付PDFを取得するには、iPhoneでMicrosoftメールを接続してください。",
        icon=":material/alternate_email:",
    )
    session_key = "microsoft_authorization_url"
    if st.button(
        "Microsoftメール接続を準備",
        type="primary",
        use_container_width=True,
        key="microsoft_oauth_start",
        icon=":material/login:",
    ):
        try:
            payload = client.start_microsoft_oauth()
        except WorkerApiError as error:
            st.error(str(error))
        else:
            st.session_state[session_key] = str(payload.get("authorization_url") or "")

    authorization_url = str(st.session_state.get(session_key) or "")
    if authorization_url.startswith("https://login.microsoftonline.com/"):
        st.link_button(
            "Microsoft公式画面を開く",
            authorization_url,
            type="primary",
            use_container_width=True,
            icon=":material/open_in_new:",
        )
        st.caption("同意後、自動的にGetReceiptへ戻ります。")
    return False


def _render_challenge(
    *,
    client: WorkerClient,
    job: Mapping[str, Any],
    challenge: Mapping[str, Any],
) -> None:
    kind = str(challenge.get("kind") or "").strip().lower()
    message = str(challenge.get("message") or "追加の本人確認が必要です。")
    st.warning(message, icon=":material/verified_user:")
    expires_at = _format_timestamp(str(challenge.get("expires_at") or ""))
    if expires_at:
        st.caption(f"入力期限: {expires_at}")

    if kind in INPUT_CHALLENGE_KINDS:
        schema = challenge.get("input_schema")
        if not isinstance(schema, Mapping):
            schema = {}
        maximum = _bounded_int(schema.get("max_length"), default=12, lower=1, upper=128)
        label = str(schema.get("label") or "確認コード")
        widget_key = f"remote_challenge_{challenge.get('id')}"
        with st.form(
            f"remote_challenge_form_{challenge.get('id')}",
            clear_on_submit=True,
            border=True,
        ):
            response = st.text_input(
                label,
                type="password",
                max_chars=maximum,
                autocomplete="one-time-code",
                key=widget_key,
            )
            submitted = st.form_submit_button(
                "本人確認を続行",
                type="primary",
                use_container_width=True,
                icon=":material/verified_user:",
            )
        if submitted:
            try:
                normalized = validate_challenge_response(challenge, response)
                client.submit_challenge_response(
                    job_id=str(job["id"]),
                    challenge_id=str(challenge["id"]),
                    response=normalized,
                )
            except ValueError as error:
                st.error(str(error))
            except WorkerApiError as error:
                st.error(f"{error}（{error.code}）")
            else:
                # Do not deliberately retain sensitive widget state.
                st.session_state.pop(widget_key, None)
                normalized = ""
                response = ""
                st.rerun()
        return

    if kind in PUSH_CHALLENGE_KINDS:
        st.info(
            "Authenticatorまたはサービス公式アプリへ切り替えて承認してください。"
            "承認後も同じワーカーブラウザが確認を続けます。",
            icon=":material/phonelink_lock:",
        )
        if bool(challenge.get("viewer_available")):
            _render_interactive_viewer(client=client, job=job, challenge=challenge)
        return

    if kind in INTERACTIVE_CHALLENGE_KINDS:
        if bool(challenge.get("viewer_available")):
            _render_interactive_viewer(client=client, job=job, challenge=challenge)
        else:
            st.error(
                "安全なリモート操作画面を準備できないため、自動取得は続行しません。",
                icon=":material/block:",
            )
        return

    if kind in UNAVAILABLE_CHALLENGE_KINDS:
        st.error(
            "パスキーそのものは遠隔Chromeへ引き継げません。"
            "下の同じChrome画面で「別の方法」を選び、SMS・メールコード等の公式方式へ切り替えてください。",
            icon=":material/passkey:",
        )
        if bool(challenge.get("viewer_available")):
            _render_interactive_viewer(client=client, job=job, challenge=challenge)
        return

    st.error(
        "未確認の追加認証が表示されたため、安全のため自動入力せず停止しています。",
        icon=":material/shield_lock:",
    )


def _render_interactive_viewer(
    *,
    client: WorkerClient,
    job: Mapping[str, Any],
    challenge: Mapping[str, Any],
) -> None:
    from PIL import Image
    from streamlit_image_coordinates import streamlit_image_coordinates

    job_id = str(job.get("id") or "")
    challenge_id = str(challenge.get("id") or "")
    try:
        frame = client.get_viewer_frame(
            job_id=job_id,
            challenge_id=challenge_id,
        )
        image = Image.open(BytesIO(frame))
        image.load()
    except (WorkerApiError, OSError, ValueError) as error:
        st.error(f"取得ブラウザの画面を表示できません: {error}")
        return

    natural_width, natural_height = image.size
    display_width = min(720, natural_width)
    st.caption(
        "画像内をタップすると、同じGoogle Chromeへその位置のクリックを送ります。"
        "認証が終わったら「操作完了」を押してください。"
    )
    clicked = streamlit_image_coordinates(
        image,
        width=display_width,
        key=f"remote_viewer_image_{challenge_id}",
        cursor="pointer",
    )
    if isinstance(clicked, Mapping):
        click_token = str(clicked.get("unix_time") or "")
        processed_key = f"remote_viewer_processed_{challenge_id}"
        if click_token and st.session_state.get(processed_key) != click_token:
            rendered_width = max(1, int(clicked.get("width") or display_width))
            rendered_height = max(
                1,
                int(
                    clicked.get("height")
                    or round(display_width * natural_height / max(1, natural_width))
                ),
            )
            x = round(int(clicked.get("x") or 0) * natural_width / rendered_width)
            y = round(int(clicked.get("y") or 0) * natural_height / rendered_height)
            try:
                client.send_viewer_input(
                    job_id=job_id,
                    challenge_id=challenge_id,
                    action="click",
                    x=x,
                    y=y,
                )
            except (WorkerApiError, ValueError) as error:
                st.error(str(error))
            else:
                st.session_state[processed_key] = click_token
                st.rerun()

    text_key = f"remote_viewer_text_{challenge_id}"
    with st.form(f"remote_viewer_text_form_{challenge_id}", clear_on_submit=True):
        text_value = st.text_input(
            "選択中の入力欄へ文字を送る",
            type="password",
            max_chars=512,
            key=text_key,
        )
        send_text = st.form_submit_button(
            "文字を送信",
            use_container_width=True,
            icon=":material/keyboard:",
        )
    if send_text:
        try:
            client.send_viewer_input(
                job_id=job_id,
                challenge_id=challenge_id,
                action="text",
                text=text_value,
            )
        except (WorkerApiError, ValueError) as error:
            st.error(str(error))
        else:
            st.session_state.pop(text_key, None)
            text_value = ""
            st.rerun()

    key_columns = st.columns(4)
    for column, key_name, label in zip(
        key_columns,
        ("Enter", "Tab", "Backspace", "Escape"),
        ("Enter", "Tab", "削除", "戻る"),
    ):
        if column.button(
            label,
            key=f"remote_viewer_key_{challenge_id}_{key_name}",
            use_container_width=True,
        ):
            try:
                client.send_viewer_input(
                    job_id=job_id,
                    challenge_id=challenge_id,
                    action="key",
                    key=key_name,
                )
            except (WorkerApiError, ValueError) as error:
                st.error(str(error))
            else:
                st.rerun()

    refresh_column, complete_column = st.columns(2)
    if refresh_column.button(
        "画面を更新",
        key=f"remote_viewer_refresh_{challenge_id}",
        use_container_width=True,
        icon=":material/refresh:",
    ):
        st.rerun()
    if complete_column.button(
        "操作完了",
        key=f"remote_viewer_complete_{challenge_id}",
        type="primary",
        use_container_width=True,
        icon=":material/check_circle:",
    ):
        try:
            client.complete_interactive_challenge(
                job_id=job_id,
                challenge_id=challenge_id,
            )
        except WorkerApiError as error:
            st.error(str(error))
        else:
            st.rerun()


def _bounded_int(value: Any, *, default: int, lower: int, upper: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(lower, min(upper, parsed))


def _format_timestamp(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")
