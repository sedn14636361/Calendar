# 📅 カレンダー空き日程ボット 構築・運用ガイド

Googleカレンダーと連動し、Discord上で「予定の自動表示」と「空き日程の検索」を行うボットの、
**ゼロから構築して常時稼働させるまで**の完全ガイドです。

---

## 目次

1. [全体像](#1-全体像)
2. [必要なもの一覧](#2-必要なもの一覧)
3. [Discord側の準備](#3-discord側の準備)
4. [Googleカレンダー側の準備](#4-googleカレンダー側の準備)
5. [ファイル構成](#5-ファイル構成)
6. [環境変数の一覧](#6-環境変数の一覧)
7. [ローカルでの動作確認](#7-ローカルでの動作確認)
8. [GitHubへのアップロード](#8-githubへのアップロード)
9. [Renderへのデプロイ](#9-renderへのデプロイ)
10. [UptimeRobotで常時稼働](#10-uptimerobotで常時稼働)
11. [カスタマイズできる部分](#11-カスタマイズできる部分)
12. [よくあるトラブルと対処](#12-よくあるトラブルと対処)

---

## 1. 全体像

```
Googleカレンダー ──(API)──> ボット(Python) ──> Discordチャンネルに表示
                                  ↑
                          Render(24時間稼働)
                                  ↑
                       UptimeRobot(スリープ防止)
```

- **予定の自動表示**：今日から90日分を、毎日自動更新でチャンネルに表示。
- **空き日程検索**：`/2026-9` のようなコマンドで、指定期間の空き日を返す。
- **稼働場所**：自分のPCではなくRender（クラウド）上で動かすため、PCを閉じても動き続ける。

### 使用技術

| 要素 | 使うもの |
|------|----------|
| 言語 | Python 3 |
| Discord操作 | discord.py |
| カレンダー取得 | Google Calendar API（google-api-python-client） |
| 稼働環境 | Render（Web Service / Free） |
| スリープ防止 | UptimeRobot |
| コード置き場 | GitHub |

---

## 2. 必要なもの一覧

事前に用意・作成するもの。すべて無料で始められます。

- [ ] Discordアカウントと、ボットを入れるサーバー（自分が管理者のもの）
- [ ] Googleアカウント（表示したいカレンダーを持っているもの）
- [ ] GitHubアカウント
- [ ] Renderアカウント
- [ ] UptimeRobotアカウント
- [ ] PC（初期設定・動作確認用。Mac/Windowsどちらでも可）

このガイドで取得する「鍵」や「ID」は次の4つ。あとで環境変数に入れます。

1. **Discord Bot トークン**
2. **Discord チャンネルID**
3. **Google カレンダーID**
4. **サービスアカウントの鍵JSON**（の中身）

---

## 3. Discord側の準備

### 3-1. ボットを作る

1. [Discord Developer Portal](https://discord.com/developers/applications) を開く
2. 「New Application」→ 名前を付けて作成
3. 左メニュー「Bot」→「Reset Token」でトークンを表示し、**コピーして控える**
   → これが環境変数 `DISCORD_BOT_TOKEN`
4. 同じ「Bot」画面で **「MESSAGE CONTENT INTENT」をON** にして保存
   （コマンド機能でメッセージ本文を読むために必須）

### 3-2. ボットをサーバーに招待する

1. 左メニュー「OAuth2」→「URL Generator」
2. SCOPES で **`bot`** にチェック
3. BOT PERMISSIONS で **`Send Messages`** にチェック
   （メッセージ編集も同権限で可能）
4. 生成されたURLをブラウザで開き、自分のサーバーを選んで招待

### 3-3. チャンネルIDを取得する

1. Discordアプリ → ユーザー設定 →「詳細設定」→「開発者モード」をON
2. 予定を表示したいチャンネルを右クリック →「チャンネルIDをコピー」
   → これが環境変数 `CHANNEL_ID`（数字のみ）

---

## 4. Googleカレンダー側の準備

ボットは「サービスアカウント」という**ボット専用のGoogleアカウント**を使ってカレンダーを読みます。
人間のアカウントとは別に作り、そのアカウントにカレンダーを共有する形です。

### 4-1. プロジェクトとAPIの有効化

1. [Google Cloud Console](https://console.cloud.google.com/) を開く
2. 上部でプロジェクトを新規作成（名前は任意）
3. 「APIとサービス」→「ライブラリ」→ **Google Calendar API** を検索して「有効にする」

### 4-2. サービスアカウントと鍵の作成

1. 「APIとサービス」→「認証情報」
2. 「認証情報を作成」→「サービスアカウント」→ 名前を付けて作成
3. 作成したサービスアカウントをクリック →「キー」タブ →「鍵を追加」→「新しい鍵を作成」→ **JSON** を選択
4. JSONファイルがダウンロードされる
   → このファイルを `service_account.json` にリネームして保管
   → **中身（`{` 〜 `}` 全部）** が環境変数 `SERVICE_ACCOUNT_JSON`

> ⚠️ この鍵ファイルは**絶対にGitHubに上げない**。漏れるとカレンダーにアクセスされます。

### 4-3. カレンダーを共有する

1. ダウンロードしたJSONを開き、`client_email` の値
   （`xxx@xxx.iam.gserviceaccount.com`）をコピー
2. Googleカレンダー画面 → 対象カレンダーの「設定と共有」
3. 「特定のユーザーやグループと共有」に、その`client_email`を追加
   （権限は「予定の表示（すべての予定の詳細）」でOK）
4. 同じ設定画面を下にスクロール →「カレンダーの統合」にある
   **カレンダーID** をコピー
   → これが環境変数 `CALENDAR_ID`
   （自分のメインカレンダーなら、自分のGmailアドレスがそのままIDのこともある）

---

## 5. ファイル構成

GitHub / Render に置くのは次の**3ファイルだけ**です。

```
（リポジトリ直下）
├── Calendar.py         … ボット本体
├── requirements.txt    … 必要なライブラリ一覧
└── .gitignore          … アップロード除外設定
```

### requirements.txt

```
discord.py
google-api-python-client
google-auth
```

### .gitignore

```
service_account.json
```

> `service_account.json` は**GitHubに上げず**、中身をRenderの環境変数に貼ります。
> `.gitignore` は「うっかり鍵を上げない」ための保険です。

---

## 6. 環境変数の一覧

コードは秘密情報をコードに直書きせず、**環境変数**から読み込みます。
ローカルでもRenderでも、次の4つを設定します。

| 変数名 | 内容 | 取得元 |
|--------|------|--------|
| `DISCORD_BOT_TOKEN` | Discordボットのトークン | 3-1 |
| `CHANNEL_ID` | 表示先チャンネルのID（数字） | 3-3 |
| `CALENDAR_ID` | 表示するカレンダーのID | 4-3 |
| `SERVICE_ACCOUNT_JSON` | 鍵JSONの中身を丸ごと（`{`〜`}`） | 4-2 |

> `SERVICE_ACCOUNT_JSON` は最頻出のつまずき箇所。
> **名前のスペル**と、**値が途中で切れていないか**（末尾が `}` で終わるか）を必ず確認。

---

## 7. ローカルでの動作確認

Renderに上げる前に、手元で動くか確認できます。
`Calendar.py` と `service_account.json` を同じフォルダに置いて、ターミナルで次を1行として実行。

```bash
DISCORD_BOT_TOKEN="あなたのトークン" \
CHANNEL_ID="あなたのチャンネルID" \
CALENDAR_ID="あなたのカレンダーID" \
SERVICE_ACCOUNT_JSON="$(cat service_account.json)" \
SSL_CERT_FILE=$(python3 -m certifi) \
python3 Calendar.py
```

- `$(cat service_account.json)` … 鍵ファイルの中身を読み込んで渡す
- `SSL_CERT_FILE=...` … Mac等で出るSSL証明書エラーの回避

必要ライブラリが未インストールなら先に：
```bash
pip install discord.py google-api-python-client google-auth
```

ログに `ログインしました: ...` が出れば成功。数分以内にチャンネルへ予定が表示されます。

---

## 8. GitHubへのアップロード

コマンド不要、ブラウザだけで可能です。

1. [github.com](https://github.com) →「New repository」
2. 名前を付け、**Private** を選んで作成
3. 「Add file」→「Upload files」
4. **3ファイル（Calendar.py / requirements.txt / .gitignore）** をドラッグ＆ドロップ
5. 「Commit changes」

> アップロード後、一覧に **`service_account.json` が無いこと**を必ず確認。
> あった場合はクリック→ゴミ箱アイコンで削除。

コードを直したいときは、GitHub上でファイルを開き、鉛筆アイコン（Edit）で編集 → Commit。
Renderが自動で再デプロイします。

---

## 9. Renderへのデプロイ

このボットはWebページを持たないが、Renderの無料枠で常時稼働させるため、
**Web Service + ダミーWebサーバー同居**の構成をとる（コードに実装済み）。

1. [render.com](https://render.com) → GitHubで登録
2. ダッシュボード →「New +」→「Web Service」
3. 該当リポジトリを接続
4. 設定：
   - **Build Command**：`pip install -r requirements.txt`
   - **Start Command**：`python Calendar.py`
   - **Instance Type**：Free
   - **Region**：任意（後述のトラブル時はここを変える）
5. 「Environment Variables」で **6章の4つ**を追加
6. 「Create Web Service」で起動

「Logs」タブで `ログインしました: ...` が出れば成功。
起動時にRenderが発行する **`https://〇〇〇.onrender.com`** のURLを控える（次章で使用）。

> Renderの **Background Worker** は無料枠がないため、Web Serviceを使う。
> Web Serviceは「ポートで待ち受け」が必要なので、コードに軽量Webサーバーを同居させてある。

---

## 10. UptimeRobotで常時稼働

Renderの無料Web Serviceは15分アクセスがないとスリープする。
UptimeRobotで定期的にアクセスして起こし続ける。

1. [uptimerobot.com](https://uptimerobot.com) で登録
2. 「Add New Monitor」
   - **Monitor Type**：HTTP(s)
   - **Friendly Name**：任意
   - **URL**：Renderの `https://〇〇〇.onrender.com`
   - **Monitoring Interval**：5 minutes
3. 「Create Monitor」

> サービスを作り直してURLが変わったら、**UptimeRobotのURLも更新**すること。

⚠️ **無料枠＋UptimeRobotは完全ではない**。稼働時間上限やIP事情でまれに停止することがある。
確実な常時稼働を求めるならRender有料（月7ドル程度）やKoyeb等の検討を。

---

## 11. カスタマイズできる部分

`Calendar.py` の上部の設定値を変えるだけで、挙動を調整できます。

### 表示日数

```python
DAYS_TO_SHOW = 90        # 何日先まで表示するか
DAYS_PER_MESSAGE = 25    # 1メッセージあたりの日数（Embed上限のため最大25）
```
- `DAYS_TO_SHOW` を増やすとメッセージ通数が増える（90日なら25×3＋15＝4通）。

### 自動更新の間隔

```python
@tasks.loop(minutes=60)  # 何分ごとに更新をチェックするか
```
- 短いほど反映が速いが、Discordへのアクセスが増える。
- **短くしすぎるとレート制限（429/1015）の原因**。15〜60分が無難。

### 昼・夜の時間帯

```python
DAY_START, DAY_END = 10, 18       # 昼：10:00-18:00
NIGHT_START, NIGHT_END = 21, 24   # 夜：21:00-24:00
```
- 空き判定に使う時間帯。数字（時）を変えれば定義を変更できる。

### 表示の色

```python
embed = discord.Embed(color=0x4285F4)   # 左端の帯の色（16進カラー）
```

### タイムゾーン

```python
JST = timezone(timedelta(hours=9))   # 日本時間(UTC+9)
```
- 他の国で使うなら時間数を変更。

### コマンドの記号・書式

`on_message` 内の正規表現でコマンド書式を定義：
```python
re.fullmatch(r"/([nau]?)([\d\-\.:]+)(r?)", text)
```
- 先頭 `n/a/u`（時間帯モード）、末尾 `r`（反転）を判定している。
- 記号を変えたい場合はここを編集（変更時は表示ラベルとの整合に注意）。

### コマンド仕様まとめ（利用者向け）

| コマンド | 意味 |
|----------|------|
| `/2026-9` | 昼が空いている日 |
| `/n2026-9` | 夜が空いている日 |
| `/a2026-9` | 昼夜とも空いている日 |
| `/u2026-9` | 昼か夜が空いている日 |
| `/2026-9r` | 昼に予定がある日（反転） |
| `/n2026-9r` | 夜に予定がある日（反転） |
| `/2026-8:2026-9` | 月単位の範囲 |
| `/2026-8.15:2026-9.30` | 日単位の範囲（`.`で日、`:`で範囲） |
| `/2026-12:2027-1` | 年をまたぐ範囲 |

- 反転 `r` は **昼・夜のみ**対象（`a`/`u`では無視）。
- 先頭・期間・末尾は組み合わせ可（例 `/n2026-8.15:2026-9.30r`）。

---

## 12. よくあるトラブルと対処

### `KeyError: 'SERVICE_ACCOUNT_JSON'`（など）
環境変数の**設定漏れ**か**名前のスペルミス**。Renderの「Environment」で4つ揃っているか、
名前が一字一句合っているか確認。

### `json.decoder.JSONDecodeError`
`SERVICE_ACCOUNT_JSON` の値が途中で切れている。`{`〜`}` を全選択でコピーし直す。

### `Improper token has been passed.`
`DISCORD_BOT_TOKEN` が間違い、または前後に空白・改行が混入。貼り直す。

### `FileNotFoundError: 'service_account.json'`（ローカル時）
鍵ファイルが実行フォルダに無い、または名前違い。同じフォルダに置く。

### `CERTIFICATE_VERIFY_FAILED`（ローカル時）
SSL証明書の問題。`pip install --upgrade certifi` 後、起動時に
`SSL_CERT_FILE=$(python3 -m certifi)` を付ける（Renderでは不要）。

### `No open ports detected`（Render）
Web Serviceなのにポートを開いていない。→ コードのダミーWebサーバー部分が
正しく入っているか確認（本体には実装済み）。

### `event registered must be a coroutine function`
`@client.event` の直後の関数が `async def` になっていない、または
`@client.event` と `async def` の間に別のコードが割り込んでいる。

### UptimeRobotで `501 Not Implemented`
HEADリクエストに未対応。→ `do_HEAD` がコードにあるか確認（本体には実装済み）。

### `429 Too Many Requests`（Discord）
短時間のアクセス過多。数分〜十数分待つ。更新間隔を延ばし、
編集ループの `asyncio.sleep(1)` を維持する。

### Cloudflare `1015 / rate limited`（Discord）
IP単位の一時ブロック。**ボットを止めて放置**（数十分〜数時間）。
Renderの共有IPが原因のことがあり、その場合は**別リージョンで作り直す**と
新しいIPになり解決することが多い。作り直したらUptimeRobotのURLも更新。

### 予定を変更したのに自動表示が変わらない
- 更新間隔（最大 `minutes` 分）待つ。
- 変更した予定が **90日より先** なら表示範囲外（正常）。
- 変化検知は予定の「タイトル・開始・終了・最終更新時刻」で判定している。

---

## 付録：起動の流れ（内部動作）

1. 起動時、環境変数を読み込み、Google/Discordに接続。
2. ダミーWebサーバーを別スレッドで起動（Renderのポート検査＆UptimeRobot対応）。
3. `on_ready` で定期更新タスクを開始。
4. 一定間隔で「今日の日付＋予定内容」を前回と比較し、変化があれば
   90日分のEmbed（複数メッセージ）を送信または編集。
5. 利用者が `/....` コマンドを送ると `on_message` が反応し、
   指定期間の空き日（または予定がある日）を計算して返信。
