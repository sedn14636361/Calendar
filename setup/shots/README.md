# スクリーンショット

セットアップウィザード（`setup/wizard.html`）の各ステップに表示される画像です。
ウィザードが参照している **22枚がすべて揃っています**。

## 一覧

### Discord（ステップ1〜3）

| ファイル名 | 内容 |
|---|---|
| `01-new-application.png` | Developer Portal の「New Application」名前入力 |
| `02-reset-token.png` | Bot タブ、「Reset Token」でトークンが表示された状態 |
| `03-message-content-intent.png` | MESSAGE CONTENT INTENT を ON にした状態 |
| `04-oauth2-url-generator.png` | OAuth2 → URL Generator で `bot` と `Send Messages` をチェック |
| `04b-install-link.png` | 「インストール」画面の出来合いの招待リンク |
| `05-invite-server-select.png` | 招待URLを開いたときのサーバー選択・認証画面 |
| `06-developer-mode.png` | Discord設定 → 詳細設定 → 開発者モードを ON |
| `07-copy-channel-id.png` | チャンネルを右クリック →「チャンネルIDをコピー」 |

### Google Cloud / カレンダー（ステップ4〜5）

| ファイル名 | 内容 |
|---|---|
| `08-gcp-new-project.png` | プロジェクト選択ダイアログの「新しいプロジェクト」 |
| `08b-gcp-project-form.png` | プロジェクト名を入力する作成フォーム |
| `09a-search-calendar-api.png` | 検索欄で「google calendar」を探した状態 |
| `09-enable-calendar-api.png` | Google Calendar API の「有効にする」ボタン |
| `10-create-service-account.png` | 「認証情報を作成」→「サービス アカウント」 |
| `11-add-json-key.png` | 「キー」タブ →「鍵を追加」→ JSON を選択 |
| `11b-service-account-email.png` | サービスアカウントの詳細画面（`client_email` の場所） |
| `12-share-calendar.png` | カレンダーの「設定と共有」に `client_email` を追加した状態 |
| `13-calendar-id.png` | 「カレンダーの統合」にあるカレンダーID |

### Render / UptimeRobot（ステップ7〜8）

| ファイル名 | 内容 |
|---|---|
| `14-render-new-web-service.png` | 「New +」→「Web Service」を選ぶ |
| `16-render-build-start.png` | Build / Start Command と Instance Type = Free |
| `17-render-env-vars.png` | Environment Variables に4つ追加した状態 |
| `18-render-service-url.png` | サービス画面に表示される公開URL |
| `20-uptimerobot-monitor.png` | UptimeRobot の Add single monitor |

## 差し替えるとき

ファイル名を変えずに置き換えれば、そのまま反映されます。
ウィザードは `setup/wizard.html` の `shot("ファイル名", "説明")` で参照しています。
画像が無い場合は枠に「画像未設定」と出て、テキスト手順だけで進める作りになっているので、
一時的に削除しても壊れません。

形式は PNG、横幅 1700px 程度までにしておくと軽く収まります。

## 写り込みへの注意

このリポジトリは公開されています。トークンや鍵だけでなく、
**アカウントを特定できる情報**も画面に写り込みます。
認証情報ではないので単体で乗っ取られることはありませんが、塗りつぶしを推奨します。

収録済みの画像では、次のものを塗りつぶしてあります。

| 塗りつぶしたもの | 写っていた場所 |
|---|---|
| Discord のトークン | Bot タブのトークン欄 |
| Discord アプリケーションID | ブラウザのURLバー、招待リンクの `client_id=` |
| Discord のサーバー名 | 招待画面の「サーバーに追加」 |
| Google Cloud プロジェクトID | プロジェクト作成フォーム、ステータスバーのURL |
| サービスアカウントの数値ID | 鍵ページのパンくず、詳細画面の「一意の ID」 |
| サービスアカウントのメール接頭辞 | 詳細画面の「メール」 |
| カレンダー名・オーナー・共有相手 | カレンダーの「設定と共有」 |
| カレンダーIDと各種URL | 「カレンダーの統合」 |
| Render の Service ID・リポジトリ・公開URL | サービス画面 |
| 通知先メールアドレス・アカウント名 | UptimeRobot の設定画面 |

塗りつぶすときは、**塗った後に該当箇所を拡大して確認**してください。
座標を目測で決めると、1〜2文字はみ出したまま気づかないことがあります。
