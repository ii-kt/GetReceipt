"""Reconnect Google Drive from the phone, without a PC.

Google's long-lived credential is the one thing the app cannot keep for
itself: the receipts folder is where everything else is stored, so when
access to it lapses there is nowhere left to write a replacement. It has to
go back into Streamlit secrets by hand.

What this does remove is the PC. Re-issuing used to mean running a script
that opens a local callback server on a desktop. Here the owner taps a link,
approves on their phone, pastes back the address Google sends them to, and
copies the new value into secrets - all from the phone.

The redirect deliberately points at localhost. The registered client is a
desktop one, which accepts any localhost address and nothing else, so the
phone simply fails to load that page while showing the authorization code in
its address bar. That means no change to the Google client is needed.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from typing import Any


__all__ = ["GOOGLE_DRIVE_SCOPE", "authorization_url", "extract_code", "exchange_code",
           "render_google_reconnect"]


AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
# Any localhost address is accepted by a desktop client. Nothing listens on
# the phone, which is the point: the browser stops and shows the code.
REDIRECT_URI = "http://localhost:1/"
_CODE = re.compile(r"^[\x20-\x7E]{10,2048}$")


def authorization_url(client_id: str) -> str:
    """Build the consent URL that returns a fresh long-lived credential."""

    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": GOOGLE_DRIVE_SCOPE,
            # Both are required, or Google reuses the existing grant and
            # returns no refresh token at all.
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    return f"{AUTH_ENDPOINT}?{query}"


def extract_code(pasted: str) -> str:
    """Pull the authorization code out of whatever the owner pasted.

    Accepts the whole redirected address or just the code, because on a phone
    the address bar is the only place the code appears.
    """

    value = str(pasted or "").strip()
    if not value:
        return ""
    if "?" in value or value.startswith("http"):
        query = urllib.parse.urlparse(value).query or value.split("?", 1)[-1]
        found = urllib.parse.parse_qs(query).get("code") or [""]
        value = found[0].strip()
    value = urllib.parse.unquote(value)
    return value if _CODE.fullmatch(value) else ""


def exchange_code(*, client_id: str, client_secret: str, code: str) -> str:
    """Trade the authorization code for a refresh token. Returns "" on failure."""

    payload = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
        }
    ).encode("ascii")
    request = urllib.request.Request(
        TOKEN_ENDPOINT,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            tokens = json.loads(response.read().decode("utf-8"))
    except Exception:
        return ""
    return str(tokens.get("refresh_token") or "")


def render_google_reconnect(st: Any, secrets: Any) -> None:
    """Walk the owner through re-issuing the Drive credential on their phone."""

    section = _section(secrets, "google_oauth")
    client_id = _value(section, "client_id")
    client_secret = _value(section, "client_secret")
    if not client_id or not client_secret:
        st.info(
            "Streamlit Secrets の [google_oauth] に client_id と client_secret が"
            "必要です。",
            icon=":material/info:",
        )
        return

    with st.container(border=True):
        st.markdown("**Google Driveを接続し直す**")
        st.caption(
            "1. 下のボタンでGoogleの許可画面を開き、承認します。"
            "2. 承認後は「このサイトにアクセスできません」と表示されます。"
            "それで正常です。3. その画面のアドレスバーのURLをすべてコピーし、"
            "下の欄に貼り付けてください。"
        )
        st.link_button(
            "Googleの許可画面を開く",
            authorization_url(client_id),
            use_container_width=True,
            type="primary",
        )
        pasted = st.text_input(
            "承認後のURL",
            key="google_reconnect_url",
            placeholder="http://localhost:1/?code=...",
        )
        if not st.button("新しい接続情報を発行", use_container_width=True):
            return

        code = extract_code(pasted)
        if not code:
            st.error(
                "URLから認証コードを読み取れませんでした。"
                "アドレスバーの内容をそのまま貼り付けてください。",
                icon=":material/error:",
            )
            return
        refresh_token = exchange_code(
            client_id=client_id, client_secret=client_secret, code=code
        )
        if not refresh_token:
            st.error(
                "接続情報を発行できませんでした。許可画面をもう一度開き、"
                "新しいURLで試してください（コードは一度しか使えません）。",
                icon=":material/error:",
            )
            return

        st.success("発行できました。あとは下の内容をSecretsへ貼り替えるだけです。")
        st.caption(
            "Streamlit Cloud のアプリ設定 → Secrets を開き、[google_oauth] の "
            "refresh_token をこの値に置き換えて保存してください。"
            "この値は他人に見せないでください。"
        )
        st.code(
            "[google_oauth]\n"
            f'client_id = "{client_id}"\n'
            'client_secret = "（今の値のまま）"\n'
            f'refresh_token = "{refresh_token}"\n',
            language="toml",
        )


def _section(secrets: Any, name: str) -> Any | None:
    try:
        return secrets[name]
    except (KeyError, TypeError):
        return None
    except Exception as error:
        if type(error).__name__ == "StreamlitSecretNotFoundError":
            return None
        raise


def _value(section: Any, name: str) -> str:
    try:
        value = section.get(name)
    except (AttributeError, TypeError):
        value = None
    return str(value or "").strip()
