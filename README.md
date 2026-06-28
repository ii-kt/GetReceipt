# GetReceipt

最終確認日: 2026-06-28

GetReceiptは、iPhoneからPCを起動せずに、領収書・明細を取得してGoogle Driveへ保管するためのStreamlit Community Cloudアプリです。

公式サイトから取得したPDF/CSV/画像を、必要なメタ情報と一緒に登録し、ファイル名を整えてGoogle Driveへ保存します。保存台帳はアプリ内とDrive上の `_receipt_index.csv` に同期します。

## リンク

- 公開アプリURL: https://appapppy-vqqnxv3k523pevdaesrnu3.streamlit.app/
- Repository: `ii-kt/GetReceipt`
- Branch: `main`
- Google Drive保存先: https://drive.google.com/drive/folders/1jwaMMK-KGIyUampBWOjRIY3BULuj6W-M

この公開URLはStreamlit Community Cloud上のアプリです。ローカルPCでサーバーを起動していなくてもアクセスできます。

## 公開アプリの動き方

- 公開アプリURLは固定です。毎回変わりません。
- Streamlit Community Cloud上で動くWebアプリなので、PC側でローカルサーバーやNode.jsを起動する必要はありません。
- iPhone、PC、タブレットのブラウザから同じ公開URLを開いて使えます。
- Google Drive保存はStreamlit Cloudに登録したサービスアカウントで行います。
- 公式サイトへのログインや追加認証は、アプリ内の取得用ブラウザ画面で本人が操作します。

## スリープ表示について

Streamlit Community Cloudの仕様で、12時間アクセスがないアプリはスリープします。スリープ中に公開URLを開くと、GetReceipt本体ではなく `Zzzz` と `Yes, get this app back up!` の画面が表示されます。

この画面が出た場合は、青い `Yes, get this app back up!` ボタンを押してください。URLは変わらず、そのままGetReceipt本体が起動します。これはアプリのクラッシュやデプロイ失敗ではありません。

公開確認ではHTTPステータス `200` だけを合格条件にしません。`Zzzz` 画面もHTTP `200` を返すため、必ずブラウザで `GetReceipt`、`取得状況`、`自動取得` が表示されるところまで確認します。

## 現在のアプリ内容

- Streamlit Community Cloud向けのクラウド版です。
- PC常駐プロセス、Node.js、ユーザーPC側のローカルブラウザ自動操作は使いません。
- 家賃、Wi-Fi、電気、携帯はCloud内の取得用Chromiumで公式サイトやOutlook Webを操作し、PDFを自動取得してGoogle Driveへ保存します。
- ログイン情報はアプリコードやDriveに保存しません。認証は各社の公式サイト側で行います。
- Google Driveにはサービスアカウント経由で保存します。
- 保存済み・未取得・未発行を月別/サービス別に一覧できます。

## 対象サービス

| 表示名 | 取引先初期値 | 取得元 |
| --- | --- | --- |
| 家賃 | 株式会社エポスカード | エポスカード公式サイト |
| Wi-Fi | 中部テレコミュニケーション株式会社 | commufaマイページ |
| 電気 | フラットエナジー株式会社 | Outlookメール |
| 携帯 | 株式会社NTTドコモ | Webビリング |

## 最新UI

現在のUIは、領収書の保管作業を一画面で判断できるコマンドセンター風のデザインです。

- `RECEIPT COMMAND / GOOGLE DRIVE` のマストヘッド
- 保存済ファイル・未取得枠・保管完了枠・現在の対象月を見せるステータスカード
- `対象月を確認 → 自動取得 → ファイル名を確認 → Driveへ保存` のフロー
- 落ち着いた紙色をベースに、青・赤・緑・琥珀を使った操作盤風の配色
- モバイル幅でも崩れないレスポンシブレイアウト
- Streamlit標準UIを上書きした、ボタン・タブ・入力欄・台帳表示のカスタムスタイル

## Streamlit Community Cloud設定

Streamlit Community Cloudでは次の設定でデプロイします。

| 項目 | 値 |
| --- | --- |
| Repository | `ii-kt/GetReceipt` |
| Branch | `main` |
| Main file path | `cloud/streamlit_app.py` |
| Python dependencies | `cloud/requirements.txt` |
| OS packages | `packages.txt` |
| Streamlit config | `.streamlit/config.toml` |

## Secrets設定

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

1. `取得状況` で対象月とサービスの状態を確認します。
2. 未取得のセルを押すか、`自動取得` タブでサービスと対象月を選びます。
3. `ブラウザを開く` で取得用ブラウザを表示し、必要に応じて公式サイトやOutlook Webへログインします。
4. `PDFを自動取得してDriveへ保存` を押すと、対象月のPDF取得、ファイル名生成、Google Drive保存、台帳更新まで実行します。
5. 手元にあるPDF/CSV/画像を追加したい場合だけ、`手動登録` タブでファイル、取引日、取引先、金額、取得元URLを入力します。
6. 領収書が発行されない月は、`未発行として記録` で台帳に残します。
7. `保存台帳` タブで履歴確認とCSVダウンロードができます。

## 保存ファイル名

アップロードされたファイルは次の形式に整えて保存します。

```text
YYYYMMDD_取引先_金額円.ext
```

例:

```text
20260625_株式会社NTTドコモ_5280円.pdf
```

対応ファイル形式:

- PDF
- CSV
- PNG
- JPG / JPEG

## ローカル実行

アプリ本体の実行にNode.jsは不要です。ローカルで確認する場合はPythonだけで起動できます。

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r cloud\requirements.txt
streamlit run cloud\streamlit_app.py
```

Google Drive保存までローカルで試す場合は、`.streamlit/secrets.toml` にStreamlit Cloudと同じ `[google_service_account]` を設定してください。

## ディレクトリ構成

```text
.
├─ README.md
├─ packages.txt
├─ .streamlit/
│  └─ config.toml
└─ cloud/
   ├─ requirements.txt
   ├─ streamlit_app.py
   └─ src/
      ├─ acquisition.py
      ├─ browser_session.py
      ├─ config.py
      ├─ document_metadata.py
      ├─ drive_storage.py
      ├─ epos_automation.py
      ├─ ledger.py
      ├─ naming.py
      ├─ official_site_automation.py
      └─ receipt_pipeline.py
```

## 運用メモ

- 取得対象月は2026年1月から、現在月の2か月先まで表示します。
- 保存台帳は `cloud/data/receipt_index.csv` をローカル側の台帳として使います。
- Drive保存時には、同じ内容をGoogle Drive上の `_receipt_index.csv` にも同期します。
- Streamlitのテーマカラーは `.streamlit/config.toml` で、現在のUIデザインに合わせて設定しています。
- スリープを完全になくすには、Community Cloudではなく常時稼働できるホスティングへ移す必要があります。Community Cloud運用では、スリープ時に青い復帰ボタンを押して使います。
