from __future__ import annotations

from typing import Any

from ..oauth.drive_token_store import DriveMicrosoftTokenStore
from ..oauth.microsoft import MicrosoftOAuthConfig, MicrosoftOAuthManager


_DEFAULT_REDIRECT_URI = "https://get-receipt.streamlit.app/"


def graph_manager_from_secrets(
    secrets: Any,
    storage: Any,
) -> MicrosoftOAuthManager | None:
    """Build a worker-less Microsoft Graph manager from Streamlit secrets.

    The refresh token is persisted (encrypted) in the same Google Drive
    folder as the receipts, so the electricity provider's monthly email can
    be read with delegated Mail.Read without any always-on worker.
    """

    section = _section(secrets, "microsoft_graph")
    if section is None or storage is None:
        return None
    client_id = _value(section, "client_id")
    client_secret = _value(section, "client_secret")
    encryption_key = _value(section, "encryption_key")
    if not (client_id and client_secret and encryption_key):
        return None
    config = MicrosoftOAuthConfig(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=_value(section, "redirect_uri") or _DEFAULT_REDIRECT_URI,
        encryption_key=encryption_key,
        tenant=_value(section, "tenant") or "consumers",
    )
    token_store = DriveMicrosoftTokenStore(
        drive_service=storage.service,
        folder_id=storage.folder_id,
        encryption_key=encryption_key,
    )
    return MicrosoftOAuthManager(config=config, token_store=token_store)


def render_graph_connection(
    st: Any,
    manager: MicrosoftOAuthManager,
    *,
    required: bool,
) -> bool:
    """Show the Microsoft connect control; return True when connected."""

    try:
        status = manager.status()
    except Exception:
        status = {"connected": False}
    if bool(status.get("connected")):
        with st.expander("電気（トクテン）: Microsoftメール接続済み", expanded=False):
            st.caption(
                "フラットエナジーの請求メールを読み取り専用で取得します。"
                "解除すると電気の自動取得は使えなくなります。"
            )
            if st.button("Microsoftメール接続を解除", use_container_width=True):
                try:
                    manager.disconnect()
                except Exception:
                    pass
                st.rerun()
        return True

    if not required:
        return False

    with st.container(border=True):
        st.markdown("**電気（トクテン）の自動取得にはMicrosoftメール接続が必要です**")
        st.caption(
            "ブラウザ操作ではなく、Microsoft公式のメール読み取り（Mail.Read）で"
            "請求PDFを取得します。1回だけ許可すれば次回以降は自動です。"
        )
        try:
            payload = manager.start()
            authorization_url = str(payload.get("authorization_url") or "")
        except Exception as error:
            st.error(f"Microsoft接続を開始できませんでした（{type(error).__name__}）。")
            return False
        st.link_button(
            "Microsoftメールを接続する",
            authorization_url,
            use_container_width=True,
            type="primary",
        )
    return False


def _section(secrets: Any, name: str) -> Any | None:
    try:
        section = secrets[name]
    except (KeyError, TypeError):
        return None
    except Exception as error:
        if type(error).__name__ == "StreamlitSecretNotFoundError":
            return None
        raise
    return section


def _value(section: Any, name: str) -> str:
    try:
        value = section.get(name)
    except (AttributeError, TypeError):
        value = None
    return str(value or "").strip()
