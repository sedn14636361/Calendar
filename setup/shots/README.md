# スクリーンショットの置き場

ここに画像を置くと、セットアップウィザードの該当ステップに表示されます。
**画像が無くてもウィザードは動きます**（枠に「画像未設定」と出て、テキスト手順だけで進めます）。
あとから追加・差し替えできます。

## 置き方

- ファイル名は下の表のとおりにしてください（名前が一致しないと表示されません）
- 形式は PNG 推奨。横幅 1200px 程度まで縮小しておくと軽くなります
- ⚠️ が付いているものは、**トークン・鍵・IDを必ず塗りつぶしてから**保存してください

### 公開リポジトリに置く際の注意

トークンや鍵以外にも、画面には個人を特定しうる情報が写り込みます。
認証情報ではないためそれ単体で乗っ取られることはありませんが、塗りつぶしておくことを推奨します。

| 写り込みやすいもの | どこに出るか |
|---|---|
| メールアドレス | UptimeRobot の通知設定、カレンダーの共有相手 |
| Render の公開URL | サービス概要、UptimeRobot の監視対URL |
| Discord アプリケーションID | ブラウザのURLバー |
| Google Cloud のプロジェクトID | 画面上部のプロジェクト選択、URL |
| サービスアカウントの数値ID | パンくずリスト |
| Discord のサーバー名・チャンネル名 | 招待画面、サイドバー |
| プロフィール画像 | 各サービスの右上 |

## 現在の状態

✓ が配置済みです。未配置のものは、ウィザード上で「画像未設定」と表示され、
テキスト手順だけで進める状態になります。

## 一覧

★ は特に効果が大きいものです。この7枚だけでも十分に役立ちます。

### Discord

| ファイル名 | 内容 |
|---|---|
| ✓ `01-new-application.png` | Developer Portal の「New Application」名前入力画面 |
| ✓ `02-reset-token.png` | ⚠️ Bot タブ、「Reset Token」でトークンが表示された状態 |
| ✓ `03-message-content-intent.png` | ★ Privileged Gateway Intents で MESSAGE CONTENT INTENT を ON にした状態 |
| ✓ `04-oauth2-url-generator.png` | ★ OAuth2 → URL Generator で `bot` と `Send Messages` をチェックした状態 |
| ✓ `04b-install-link.png` | 「インストール」画面の出来合いの招待リンク（簡易版の招待手順） |
| ✓ `05-invite-server-select.png` | 招待URLを開いたときのサーバー選択・認証画面 |
| ✓ `06-developer-mode.png` | Discord設定 → 詳細設定 → 開発者モードを ON にした状態 |
| ✓ `07-copy-channel-id.png` | ★ チャンネルを右クリック →「チャンネルIDをコピー」 |

### Google Cloud / カレンダー

| ファイル名 | 内容 |
|---|---|
| ✓ `08-gcp-new-project.png` | プロジェクト選択ダイアログの「新しいプロジェクト」ボタン |
| ✓ `08b-gcp-project-form.png` | プロジェクト名を入力する作成フォーム |
| ✓ `09a-search-calendar-api.png` | 検索欄で「google calendar」を探した状態 |
| ✓ `09-enable-calendar-api.png` | ★ Google Calendar API の「有効にする」ボタン |
| ✓ `10-create-service-account.png` | 「認証情報を作成」→「サービス アカウント」を選ぶ画面 |
| ✓ `11-add-json-key.png` | ★ サービスアカウントの「キー」タブ →「鍵を追加」→ JSON を選択 |
| `12-share-calendar.png` | ★ カレンダーの「設定と共有」に `client_email` を追加した状態 |
| `13-calendar-id.png` | ★ 同じ画面の「カレンダーの統合」にあるカレンダーID |

### Render

| ファイル名 | 内容 |
|---|---|
| `14-render-new-web-service.png` | 「New +」→「Web Service」を選ぶ画面 |
| `15-render-connect-repo.png` | リポジトリ接続画面 |
| `16-render-build-start.png` | ★ Build Command / Start Command / Instance Type=Free の設定 |
| `17-render-env-vars.png` | ⚠️ Environment Variables に4つ追加した状態 |
| `18-render-logs-success.png` | Logs に `ログインしました: ...` が出た状態 |
| `19-github-app-access.png` | GitHub App の Repository access 設定（Private運用する場合のみ） |

### UptimeRobot

| ファイル名 | 内容 |
|---|---|
| `20-uptimerobot-monitor.png` | Add New Monitor の設定（HTTP(s) / URL / 5 minutes） |

## 未使用の画像について

ウィザードが参照しているのは、上の表のうち `01`〜`13`、`16`〜`18` です。
`14`, `15`, `19`, `20` は `GUIDE.md` から参照するために用意しています。
