# GetReceipt

GetReceiptは、各公式サービスから対象月の領収書PDFを取得し、検証・命名してGoogle Driveへ保存する
個人用アプリです。本番の操作端末は **iPhone版Google Chrome**、自動取得ブラウザは常設ワーカー上の
**公式Google Chrome Stable**です。Windows PCを起動しておく必要はありません。

## iPhone-onlyの意味

iPhoneから行う操作は、所有者ログイン、取得開始、進捗確認、OTP入力、Microsoft同意/MFA、
短命viewerでの公式画面操作、失敗サービスの再試行、手動PDF追加、Drive上の確認です。
Streamlitタブを閉じても、永続ジョブとワーカーChromeは継続します。

iPhone内だけで4社サイトをバックグラウンド自動操作する構成ではありません。常設ワーカーが必要ですが、
日常利用でWindows、SSH、VNC、CDPを操作することはありません。CAPTCHAやpasskeyを迂回せず、
公式の代替認証がない場合は安全に停止して手動PDF追加へ切り替えます。

## 構成

- `cloud/`: StreamlitモバイルUI、ジョブクライアント、取得・Drive保存処理
- `worker/`: 永続ジョブAPIとGoogle Chromeワーカー
- `deploy/`: Caddy + Composeによる本番配備
- `tests/`: ジョブ耐久性、認証、viewer、OAuth、取得パイプラインのテスト

設計上の保証境界と実サービス別の成立条件は
[iPhone-only architecture](docs/iphone-only-architecture.md)、
本番の構築・秘密情報・実機受入手順は
[production deployment](deploy/README.md)を参照してください。

## 完了判定

ローカルテスト（L1/L2）、ホスト済みiPhone統合（L3）、所有者の実アカウントによる全サービス確認（L4）を
区別します。「iPhoneだけで全機能を十分に使える」は、CI成功だけでなく、
`deploy/README.md`のiPhone版Chrome受入項目がL4まで通った時点で完了です。

