# GetReceipt

iPhoneからPCを起動せずに、領収書・明細を取得してGoogle Driveへ保存するStreamlit Community Cloud版です。

## Streamlit Cloud設定

- Repository: `ii-kt/GetReceipt`
- Branch: `main`
- Main file path: `cloud/streamlit_app.py`
- Python dependencies: `cloud/requirements.txt`
- OS packages: `packages.txt`
- Streamlit config: `.streamlit/config.toml`
- 保存先: https://drive.google.com/drive/folders/1jwaMMK-KGIyUampBWOjRIY3BULuj6W-M

## Secrets

Streamlit Community CloudのAdvanced settingsで、Google Drive用サービスアカウントを次の形で登録します。

```toml
[google_service_account]
type = "service_account"
project_id = "your-project-id"
private_key_id = "your-private-key-id"
private_key = "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
client_email = "your-service-account@your-project-id.iam.gserviceaccount.com"
client_id = "000000000000000000000"
auth_uri = "https://accounts.google.com/o/oauth2/auth"
token_uri = "https://oauth2.googleapis.com/token"
auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
client_x509_cert_url = "https://www.googleapis.com/robot/v1/metadata/x509/your-service-account%40your-project-id.iam.gserviceaccount.com"
universe_domain = "googleapis.com"
```

Google Driveの領収書フォルダは、サービスアカウントのメールアドレスに編集者として共有してください。

## 使い方

1. 「取得状況」で対象月とサービスを選びます。
2. 「取得開始」から公式サイトを開き、iPhoneで領収書・明細をダウンロードします。
3. このアプリの「手動登録」に戻り、ファイルと金額を登録してGoogle Driveへ保存します。

クラウド版はPCやNodeの常駐プロセスを使いません。ログイン情報は保存せず、各社の公式サイトで認証します。
