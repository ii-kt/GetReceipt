"""Obtain a Google Drive refresh token for the receipt owner.

A service account cannot create files in a consumer Google Drive
("Service Accounts do not have storage quota"), so GetReceipt saves
receipts as the owner instead. Run this once to mint the long-lived
refresh token that goes into Streamlit secrets as [google_oauth].

Usage:
    python scripts/get_google_refresh_token.py --client-id ... --client-secret ...

The script prints a URL, you approve it in any browser, paste the code
back, and it prints the TOML block to copy into Streamlit secrets.
"""

from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request


AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
# Google's loopback flow is unavailable in a headless shell, so use the
# manual copy/paste redirect that still works for installed-app clients.
REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"


def build_authorization_url(client_id: str) -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": DRIVE_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    return f"{AUTH_ENDPOINT}?{query}"


def exchange_code(*, client_id: str, client_secret: str, code: str) -> dict:
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
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--client-secret", required=True)
    args = parser.parse_args()

    print("\n1) このURLをブラウザで開いて許可してください:\n")
    print(build_authorization_url(args.client_id))
    print("\n2) 表示された認証コードを貼り付けてください。")
    code = input("   コード: ").strip()
    if not code:
        print("コードが入力されませんでした。")
        return 1

    tokens = exchange_code(
        client_id=args.client_id,
        client_secret=args.client_secret,
        code=code,
    )
    refresh_token = str(tokens.get("refresh_token") or "")
    if not refresh_token:
        print("refresh_token を取得できませんでした。応答:", tokens.get("error", tokens))
        return 1

    print("\n3) 次のブロックを Streamlit Secrets に追記してください:\n")
    print("[google_oauth]")
    print(f'client_id = "{args.client_id}"')
    print(f'client_secret = "{args.client_secret}"')
    print(f'refresh_token = "{refresh_token}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
