# GetReceipt production deployment

この配備の目的は、利用者が日常操作を **iPhone版Google Chromeだけ** で完結できるようにすることです。
VMはiPhoneから直接操作する端末ではなく、公式Google Chrome Stableとジョブを継続実行する常設ワーカーです。
Windows PC、VNC、公開CDPは使いません。

## 本番構成

- Streamlit: OIDCで所有者だけを許可するモバイル操作画面
- Worker: 日本リージョンの永続VM上で、1プロセス・1アカウントとして動作
- Browser: コンテナに署名済みGoogle APTリポジトリから導入したGoogle Chrome Stable
- Microsoft: 認可コード + PKCE、委任された `Mail.Read` のみ
- Storage: 共有済みGoogle Driveフォルダと、暗号化永続ディスク上のSQLite/Chromeプロファイル
- TLS: Caddyによる自動HTTPS

Streamlitからワーカーへの通信はサーバー間HTTPSです。APIトークン、CDP、Chromeのデバッグポート、
VNCポートはiPhoneへ公開しません。対話が必要な画面は、同じChromeターゲットの短命なPNGフレームと
クリック・キー入力だけを、認証済みStreamlit画面経由で中継します。

## 事前条件

- x86_64 Linux VM（日本リージョン、2 vCPU / 4 GB RAM以上）
- プロバイダー管理の暗号化ルートディスクと暗号化永続データディスク
- Docker EngineとComposeプラグイン
- VMを指す専用DNS名
- インバウンドTCP 80/443のみ。SSHは所有者の管理経路だけに制限
- GHCRイメージを取得する権限
- Google Drive対象フォルダを共有したサービスアカウント
- Microsoft Entraアプリ登録

`8080`、Chrome CDP、VNC、X11は絶対に公開しません。ワーカーのアウトバウンドHTTPSは、
各公式サービス、Microsoft Graph/OAuth、Google Drive、DNS/時刻同期に必要です。

## 1. テスト済みイメージを確定する

`main`へのpush後、GitHub Actionsの **Build receipt worker image** が次を順に実行します。

1. 全Pythonテスト
2. ComposeとCaddyfileの構文検証
3. `linux/amd64`イメージのビルド
4. サンドボックスを無効化しないGoogle Chromeの起動・製品名・プロファイル永続性スモークテスト
5. SBOM/provenance付きGHCR push

成功した実行のSummaryに表示される次の形式を、そのまま`GETRECEIPT_IMAGE_REF`へ設定します。

```text
ghcr.io/ii-kt/getreceipt-worker@sha256:<64桁のdigest>
```

コミットSHAタグではなくdigestを配備単位にすることで、同じ設定が別イメージを指すことを防ぎます。
ChromeはStableチャネルからビルド時に取り込み、その正確なバージョンをイメージdigestで固定します。
Composeも`BROWSER_EXECUTABLE=/usr/bin/google-chrome`を固定し、secretファイルから別ブラウザへ
差し替えられないようにします。
稼働中コンテナ内でChromeを更新せず、更新時はCIを再実行して新しいdigestを受入確認します。
GitHub Actions、Python基盤、Caddyもレビュー済みcommit/manifest digestへ固定しています。
それらの更新はDependabot等の別PRで差分を確認し、同じCIとiPhone受入を通してから反映します。

GHCRパッケージがprivateの場合、VMでread-onlyのPackages権限を持つトークンを使って一度だけ
`docker login ghcr.io`を実行します。そのトークンを`.env`や`worker.env`へ入れないでください。

## 2. Microsoft Entraを設定する

権限と秘密情報の影響範囲を分離するため、2つのWebアプリ登録を推奨します。

| 登録 | exact redirect URI | 配置 | 権限 |
| --- | --- | --- | --- |
| 所有者OIDCログイン | `https://get-receipt.streamlit.app/oauth2callback` | Streamlit `[auth.microsoft]` | OpenIDログインに必要な範囲 |
| Graphメール取得 | `https://get-receipt.streamlit.app/` | Worker `GETRECEIPT_MICROSOFT_*` | 委任された `Mail.Read` |

アプリURLを変更した場合は対応するURIを**完全一致**で変更します。Graph登録にApplication権限や
`Mail.ReadWrite`は付与しません。個人Microsoftアカウントだけならtenantは`consumers`です。
両登録のsupported account typeも「Personal Microsoft accounts」に合わせます。
クライアントシークレットの期限切れをそれぞれ監視し、該当するStreamlit Secretsまたは
`worker.env`だけをローテーションします。

1登録へ統合することも技術的には可能ですが、OIDCログインとメール読取のclient secret・権限・
redirect URIが同じ影響範囲になります。本番では分離を既定にします。

## 3. Google Driveを設定する

`GOOGLE_SERVICE_ACCOUNT_JSON`に使うサービスアカウントの`client_email`へ、対象Driveフォルダの
編集権限を共有します。JSONはSecret Managerから取得し、`worker.env`では改行をエスケープした
1行のJSONにします。共有先、フォルダID、保存後の閲覧権限をiPhone版Chromeで確認してください。

## 4. VMを準備する

新規VMでは、作成時に[`deploy/cloud-init.yaml`](cloud-init.yaml)をcloud-init / user-dataへ
貼り付けると、Docker Engine + Composeの導入、`/opt/getreceipt`への配備用checkout、
UID 10001所有の`/srv/getreceipt/data`作成までが自動で完了します。その場合は
`/opt/getreceipt/deploy`で以降の手順を続けます。

手動で準備する場合は、VM上で`deploy/`を所有者だけが読めるディレクトリへ配置します。暗号化永続ディスク上のデータ領域を
作り、コンテナ内ユーザーUID 10001だけが書けるようにします。

```sh
sudo install -d -m 0700 -o 10001 -g 10001 /srv/getreceipt/data
chmod 700 deploy
```

`host.env.example`を`.env`へコピーし、専用ドメイン、CI Summaryのdigest、
`/srv/getreceipt/data`を設定します。

```sh
cp deploy/host.env.example deploy/.env
chmod 600 deploy/.env
```

`worker.env.example`から`worker.env`を作ります。値はチャット、Issue、Git、イメージ、
シェル履歴へ貼らず、クラウドSecret ManagerからVMへ安全に投入してください。
JSON、パスワード、クライアントシークレットは例のように外側をシングルクォートで囲みます。
Composeは外側の引用符を除去し、値中の`$`、`#`、バックスラッシュを展開せず保持します。
値にシングルクォートが含まれる場合はCompose env-file形式に従って`\'`とし、JSON自体も必ず
正規のJSONエンコーダーで生成します。

```sh
chmod 600 deploy/worker.env
```

独立した値を生成します。生成結果は直接Secret Managerへ保存します。

```sh
python -c 'import secrets; print(secrets.token_urlsafe(48))'
python -c 'import secrets; print(secrets.token_urlsafe(32))'
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

- 1つ目: `GETRECEIPT_API_TOKEN`
- 2つ目: `GETRECEIPT_OWNER_ID`
- 3つ目: `GETRECEIPT_TOKEN_ENCRYPTION_KEY`
- 4つ目: Streamlit `[auth].cookie_secret`

プロバイダー認証情報は1行JSONにします。TokutenはGraphを使うためOutlookパスワードを保存しません。

```json
{"epos":{"login_id":"...","password":"..."},"commufa":{"login_id":"...","password":"..."},"webbilling":{"d_account_id":"...","password":"..."}}
```

VMのroot権限またはDocker管理権限を持つ利用者はコンテナ環境を読めます。SSH/IAMを所有者だけに絞り、
ディスク暗号化と監査ログを有効にしてください。`docker compose config`の出力には環境値が展開される
場合があるため、出力を保存・共有しません。

## 5. ワーカーを起動する

`deploy/`ディレクトリで実行します。

```sh
docker compose --env-file .env pull
docker compose --env-file .env up -d
docker compose --env-file .env ps
docker compose --env-file .env exec worker python /app/worker/healthcheck.py
```

最後のコマンドが終了コード0であることを確認します。外部からは、認証なしのリクエストが401で、
TLS証明書とホスト名が正しいことだけを確認します。

```sh
test "$(curl -sS -o /dev/null -w '%{http_code}' "https://receipt-worker.example.com/healthz")" = 401
```

APIトークンをコマンドライン引数、URL、ログへ入れません。Caddyはアクセスログを有効化しておらず、
アプリ応答には`no-store`とブラウザ向け制限ヘッダーを付けます。

### Manual PDF transport boundary

The default worker API request-body limit is 16 KiB. Caddy grants only
`POST /v1/manual-receipts` a 21 MiB transport allowance; the worker itself
enforces the authoritative 20 MiB PDF limit. The route accepts raw
`application/pdf`, not base64 or multipart data, and requires the same bearer
token and owner header as all other worker operations. Keep access logging
disabled so PDF metadata and authentication headers are never recorded.

## 6. Streamlitを所有者限定にしてからワーカーを接続する

`docs/streamlit-secrets.example.toml`を基に、先に`[auth]`、`[auth.microsoft]`、
`[app_access]`だけを反映します。許可した所有者がログインでき、別アカウントと未ログイン状態では
領収書一覧・Drive・取得操作へ到達できないことを確認します。`[app_access]`はワーカー未接続でも
有効なので、既存の公開URLを先に閉じられます。

最初は`allowed_identity_hashes = ["sha256:replace-after-first-owner-login"]`のまま
所有者アカウントでログインします。ロック画面に表示される`sha256:`から始まる値だけをコピーし、
`[app_access].allowed_identity_hashes`へ完全一致で設定して再起動します。生のID tokenや
`iss`/`sub`をチャットへ貼る必要はありません。別アカウントで表示されるfingerprintは一致せず、
fingerprint自体はログイン資格情報として使えません。

その確認後、`[receipt_worker]`と`[google_service_account]`を反映します。
`receipt_worker`だけを先に追加すると、アプリは安全のためfail-closedになります。
`receipt_worker.api_token`と`owner_id`はワーカー側と完全一致させます。`[app_access]`は
検証済みOIDC ID tokenの`iss|sub`からアプリが生成した正確なfingerprintを
`allowed_identity_hashes`へ設定します。
メールで許可する場合は`allowed_issuers`も固定し、`email_verified=true`のclaimだけが通ります。

Graph接続のcallbackは、OIDCで所有者ログインが完了した後にだけワーカーへ転送されます。
OAuthの`code`と`state`はStreamlitのURLから直ちに削除され、ジョブ履歴へ保存されません。

## 7. iPhone版Chromeで本番受入を行う

以下をすべてiPhone版Google Chromeだけで実施します。

1. Streamlitを開き、許可したMicrosoft所有者アカウントでOIDCログイン
2. Microsoftメール接続を開始し、公式ドメインを確認して同意/MFAを完了
3. アプリへ戻り、接続済み表示と`Mail.Read`取得を確認
4. 対象月の自動取得を開始し、Chromeを閉じる・再表示・再読込して同じジョブを復元
5. SMS/メールコードをiPhoneで受け取り、同じジョブへ一度だけ送信
6. CAPTCHA、同意、pushなどは短命viewerで同じワーカーChromeを操作し、完了後にviewerが失効
7. 各サービスで対象月PDFがGoogle Driveへ1件だけ保存され、iPhone版Chromeから閲覧可能
8. 途中失敗したサービスだけ再試行し、既存PDFを重複作成しない
9. 公式サイトから入手したPDFを手動追加し、月/サービス警告、明示確認、Drive保存を確認
10. iPhoneのローカルUI状態を失っても、OTP値や認証情報がURL・ジョブJSON・イベントへ現れない

プラットフォームpasskeyしか選べない画面は、遠隔VMからiPhoneの秘密鍵や近接性を利用できません。
公式のSMS/メール/別認証方式へ切り替え、提供されない場合は手動PDF追加を使います。
CAPTCHAを自動解決・外注・別ブラウザへコピーすることはありません。

このチェックの1〜6がL3（ホスト済み統合）、各実アカウントで7まで通ることがL4（実サービス受入）です。
ユニットテストやローカルChromeスモークだけでL3/L4完了とは扱いません。

## 更新、バックアップ、復旧

更新は新しいCI digestを`.env`へ設定し、`pull`、`up -d`、healthcheck、iPhone受入の順で行います。
問題があれば直前に受入済みのdigestへ戻します。`main`タグを本番設定に使いません。

データディスクのスナップショットは可能ならワーカー停止中に取得します。

```sh
docker compose --env-file .env stop worker
# クラウド側で /srv/getreceipt/data を含むディスクのスナップショットを取得
docker compose --env-file .env start worker
```

復旧訓練では次を確認します。

- `jobs.sqlite3`が開け、直近ジョブと暗号化Microsoft refresh tokenレコードが復元される
- サービス別Chromeプロファイルが所有者UID 10001で読める
- 一時download、OTP平文、ブラウザlockファイルを復旧必須データとして扱わない
- 復旧後のin-flight challengeは失効し、新しい安全なログイン試行として再開する
- Google Drive既存PDFを再取得時に重複保存しない

API疎通不能、worker heartbeat停止、連続ログイン拒否、Chromeプロファイル破損、
プロバイダー画面変更、Microsoftシークレット期限を監視対象にします。
