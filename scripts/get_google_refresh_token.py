"""Obtain a Google Drive refresh token for the receipt owner.

A service account cannot create files in a consumer Google Drive
("Service Accounts do not have storage quota"), so GetReceipt saves
receipts as the owner instead. Run this once to mint the long-lived
refresh token that goes into Streamlit secrets as [google_oauth].

Usage:
    python scripts/get_google_refresh_token.py --client-secrets path/to/client_secret.json

The script opens a local callback server, prints an authorization URL,
and prints the TOML block to paste into Streamlit secrets once approved.
"""

from __future__ import annotations

import argparse
import json
import secrets
import socket
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"


class _CallbackHandler(BaseHTTPRequestHandler):
    code = ""
    state = ""
    error = ""

    def do_GET(self) -> None:  # noqa: N802
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CallbackHandler.code = (query.get("code") or [""])[0]
        _CallbackHandler.state = (query.get("state") or [""])[0]
        _CallbackHandler.error = (query.get("error") or [""])[0]
        body = (
            "<html><body style='font-family:sans-serif;padding:2rem'>"
            "<h2>GetReceipt</h2><p>認証が完了しました。"
            "このタブを閉じて、ターミナルへ戻ってください。</p></body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _load_client(path: str) -> tuple[str, str]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    section = data.get("installed") or data.get("web") or {}
    client_id = str(section.get("client_id") or "")
    client_secret = str(section.get("client_secret") or "")
    if not client_id or not client_secret:
        raise SystemExit("client_secret JSON に client_id / client_secret がありません。")
    return client_id, client_secret


def exchange_code(
    *, client_id: str, client_secret: str, code: str, redirect_uri: str
) -> dict:
    payload = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
    ).encode("ascii")
    request = urllib.request.Request(
        TOKEN_ENDPOINT,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-secrets", required=True)
    args = parser.parse_args()

    client_id, client_secret = _load_client(args.client_secrets)
    port = _free_port()
    redirect_uri = f"http://localhost:{port}"
    state = secrets.token_urlsafe(24)
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": DRIVE_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )
    authorization_url = f"{AUTH_ENDPOINT}?{query}"

    print("\nブラウザで次のURLを開いて許可してください:\n")
    print(authorization_url)
    try:
        webbrowser.open(authorization_url)
    except Exception:
        pass

    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.timeout = 300
    print("\n承認を待っています…")
    server.handle_request()
    server.server_close()

    if _CallbackHandler.error:
        print("認証が拒否されました:", _CallbackHandler.error)
        return 1
    if _CallbackHandler.state != state:
        print("stateが一致しません。もう一度実行してください。")
        return 1
    if not _CallbackHandler.code:
        print("認証コードを受け取れませんでした。")
        return 1

    tokens = exchange_code(
        client_id=client_id,
        client_secret=client_secret,
        code=_CallbackHandler.code,
        redirect_uri=redirect_uri,
    )
    refresh_token = str(tokens.get("refresh_token") or "")
    if not refresh_token:
        print("refresh_token を取得できませんでした。応答:", tokens.get("error", tokens))
        return 1

    output = Path("_output/streamlit-secrets-google-oauth.toml")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "\n[google_oauth]\n"
        f'client_id = "{client_id}"\n'
        f'client_secret = "{client_secret}"\n'
        f'refresh_token = "{refresh_token}"\n',
        encoding="utf-8",
    )
    print(f"\n完了しました。Streamlit Secrets へ貼る内容を {output} に書き出しました。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
