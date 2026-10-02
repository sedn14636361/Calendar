# 開発者向けメモ

利用者向けの説明は、すべてプロジェクト直下の `README.md` にあります。
このファイルは、配布物を変更する人のための設計メモです。

## ファイル構成

```
（プロジェクト直下）
├── README.md              … 利用者向けの説明書（1冊にまとめている）
├── start_wizard.bat       … ランチャー（Windows）。setup/setup_launcher.py を呼ぶ
├── start_wizard.command   … ランチャー（macOS / Linux）。同上
├── Calendar.py            … ボット本体
├── requirements.txt       … ボット本体とウィザードが使うライブラリ
├── LICENSE
├── .gitignore / .gitattributes
└── setup/
    ├── setup_launcher.py  … .venv の作成、ライブラリの導入、ウィザードの起動
    ├── setup_wizard.py    … 127.0.0.1 で動く検証サーバー。wizard.html を配る
    ├── setup_checks.py    … 検証ロジック。単体でも実行できる
    ├── wizard.html        … ウィザードの画面（スクリーンショットを埋め込み済み）
    ├── DEVELOPER_NOTES.md … このファイル
    ├── .venv/             … ランチャーが作る（配布物には含めない）
    └── setup_log.txt      … ランチャーが書く記録（配布物には含めない）
```

- `Calendar.py` と `requirements.txt` は直下から動かさない。
  Render の Build / Start Command（`pip install -r requirements.txt` / `python Calendar.py`）が直下を前提にしている。
- **直下に置くファイルは最小限にする。** 利用者がダウンロード直後に迷わないよう、直下には
  入口（README.md と2つのランチャー）、Render が必要とするボット本体、Git と GitHub が
  直下を前提にするファイル（LICENSE、.gitignore、.gitattributes）だけを置く。
  それ以外（ウィザードの部品、開発者向け資料）はすべて `setup/` に入れる。新しいファイルもここに置く。
- ランチャーが実行時に作るもの（`.venv`、`setup_log.txt`）も `setup/` の中に作る。直下は増やさない。
- `setup_launcher.py` は自分の1つ上の階層をプロジェクト直下とみなす（`ROOT`）。
  `requirements.txt` と、改行を検査する `start_wizard.bat` は `ROOT` から見る。
  `.venv` と `setup_log.txt` は自分と同じ `setup/` に作る（`VENV_PATH`、`LOG_PATH`）。
- `setup_wizard.py` は `wizard.html` と `setup_checks.py` を同じフォルダから読む。

## README.md の方針

- 利用者向けの説明は README.md の1冊にまとめる。以前の GUIDE.md は統合した。
- 冒頭の「まずはここから」で、ダウンロード後の操作（展開 → ランチャー → ウィザード → 後片付け）を示す。
- 内部の仕組み（ランチャーの制約、Python の判定方法、秘密情報の扱いの実装）は書かず、このファイルに置く。
  トラブル対処には、利用者がやること（ZIP の取り直し、`setup_log.txt` の確認など）だけを書く。
- **章番号を変えるときは、コード側の参照も直す。**
  `setup/setup_checks.py` と `setup/wizard.html` の案内文は、`README.md 3-1`、`4-2`、`6章`、
  「第III部 エラー対処」のように、README の章番号を直接書いている（定数 `GUIDE`）。
  次のコマンドで参照箇所を一覧できる。

  ```bash
  grep -n "{GUIDE}\|\${GUIDE}\|README.md" setup/setup_checks.py setup/wizard.html
  ```

## セットアップウィザード

### 2つの動作モード

`wizard.html` は2通りに開ける。

| 開き方 | できること |
|---|---|
| (A) HTML を直接開く | 手順の案内、値の形の検査、設定ファイルの出力。外部との通信なし |
| (B) ランチャー経由（`setup_wizard.py` が配る） | (A) に加えて、Discord と Google に実際に接続して確かめる |

(A) で実接続できないのは、Discord API がブラウザからの CORS リクエストを許可しないため。
Google のサービスアカウント方式も秘密鍵で JWT に署名する必要があり、ページ内で扱うべきではない。
カレンダーの共有漏れと MESSAGE CONTENT INTENT の設定忘れは、実接続しないと検出できない。

`setup_wizard.py` がページを配るとき、`__SETUP_KEY__` を起動ごとのランダムな鍵に置き換える。
置き換わっていなければ (A) と判断する。

秘密情報の扱いの設計は `setup/setup_wizard.py` 冒頭の docstring にまとめてある
（127.0.0.1 のみで待ち受け、セッション鍵の照合、Host ヘッダの検証、無操作30分で終了 など）。

### setup_log.txt をウィザード起動の直前で閉じる理由

`setup_wizard.py` は起動URLにセッション鍵を含めて表示する。
そこまで記録すると鍵がディスクに残り、「入力した値はファイルにもログにも書かない」という設計に反する。

### ランチャーを使わずに起動する

システムの Python に直接 `pip install` すると、Debian 12以降・Ubuntu 23.04以降・Fedora 36以降・
Homebrew の Python では `externally-managed-environment` で拒否される。venv を使う。

```bash
python3 -m venv setup/.venv
setup/.venv/bin/python -m pip install -r requirements.txt   # Windows は setup\.venv\Scripts\python.exe
setup/.venv/bin/python setup/setup_wizard.py                # --port / --no-browser も使える
```

Windows では `python3` を使わない。Python が入っていなくても `python3.exe` のスタブ（App execution alias）が置かれており、
実行すると Microsoft Store が開くだけになる。`py` はスタブに置き換えられない。

### 検証だけをコマンドで実行する

`setup/setup_checks.py` は、環境変数の4つの値を読んで、ウィザードの「総合確認」と同じ検証をする。

```bash
setup/.venv/bin/python setup/setup_checks.py              # Windows は setup\.venv\Scripts\python.exe
setup/.venv/bin/python setup/setup_checks.py --send-test  # チャンネルに実際にテスト投稿する
```

## start_wizard.bat

### 守るべき制約

1. **ASCII だけで書く。**
   cmd.exe はバッチファイルをシステムのコードページ（日本語環境なら CP932）で読む。
   UTF-8 の日本語を書くと文字化けし、化けた行がコマンドとして実行される。
   日本語の案内と実際の処理は `setup_launcher.py` に置き、バッチは Python を探して呼ぶだけにする。

2. **ラベル・`goto`・`call`・括弧ブロックを使わない。**
   これらを使うと、cmd.exe はファイル内をバイト位置で探し回る。
   改行コードが壊れると（LF だけのコピー、改行を書き換えるエディタ）、行の途中に着地し、
   読めないエラーを1行出して窓が閉じる。
   単純なコマンドを並べるだけなら探し回る先がないので、壊れても症状が画面に残る。
   そのため分岐はすべて `if defined PY` / `if not defined PY` の1行で書いている。

3. **改行は CRLF のまま保つ。**
   `.gitattributes` の `*.bat -text` で、コミットしたバイト列をそのまま配布している。
   `eol=crlf` は checkout 時にしか変換しないため、GitHub の ZIP には LF のまま入ってしまう。
   `setup_launcher.py` は起動時にこのファイルの CRLF / 単独 LF の数を数えて表示する。
   編集後は単独 LF が 0 であることを確かめる。
   Git Bash の `sed -i` は CR を落とすことがあるので使わない。

4. **どの経路でも最後に `pause` する。**
   黙って閉じる窓からは、利用者も開発者も何も分からない。
   `setup_launcher.py` には `--no-pause` を渡し、Enter を押す回数を1回にする。

### Python の見つけ方

`py -3` → `python` → `python3` → `py` の順に試す。
`py` を先にするのは、App execution alias（Microsoft Store へ誘導するスタブ）に置き換えられないため。

候補を採用する条件は、終了コードではなく「実際にマーカーファイルを作れたか」にしている。
Windows の `WindowsApps` にある `python.exe` / `python3.exe` のスタブは、
終了コードが当てにならない一方で、コードは実行できないのでファイルを作れない。

判定用のコードは `PYCHECK` 変数に1つだけ置いている。

```
import os,sys;sys.version_info[0]>=3 and open(os.environ['MARKER'],'w').write('ok')
```

- **パスは環境変数から読む。**
  パスを Python の文字列リテラルに直接埋め込むと、TEMP に `'` が入っている環境
  （ユーザー名が O'Brien など）で構文エラーになり、Python があるのに「見つからない」と表示される。
- **Python 2 は採用しない。**
  Python 2 でもファイルを作れてしまうため、メジャーバージョンを条件に入れている。
  Python 2 で `setup_launcher.py` を動かすと、日本語の案内が文字化けする。
  3.10 未満の Python 3 はここでは弾かない。
  `setup_launcher.py` が日本語で「バージョンが足りない」と伝えられるので、そちらに任せる。
- **マーカー名に `%RANDOM%` を入れる。**
  前回の実行のマーカーが何らかの理由で消えずに残っていても、スタブを誤って採用しない。

### 動作確認

実行するとウィザードまで起動するので、確認するときは Python を探す部分
（`setlocal` から、`setup_launcher.py` を呼ぶ行の手前まで）を別のバッチに切り出し、
最後に `echo PY=[%PY%]` を足して実行するとよい。確認してきた条件は次のとおり。

| 条件 | 期待する `PY` |
|------|---------------|
| 通常の環境 | `py -3` |
| TEMP のパスに `'` を含む | `py -3` |
| PATH に Python がない | 空（案内文が出る） |
| PATH に Python 2 しかない | 空 |
| 古い固定名のマーカー `calendar_bot_pycheck.tmp` が TEMP に残っている、かつ Python がない | 空 |
| フォルダ名に空白と括弧を含む（`Calendar-main (1)`） | `py -3` |

偽の Python 2 は、venv を作って `Lib\site-packages\sitecustomize.py` に
`sys.version_info = (2, 7, 18)` と書けば作れる。
`python.bat` のような偽物は使えない。
`call` なしでバッチからバッチを呼ぶと、制御が戻らないためである。

## start_wizard.command

改行は LF（`.gitattributes` の `*.command text eol=lf`）。

- **移動先は `${BASH_SOURCE[0]}` から文字列操作で求める。**
  `dirname` などの外部コマンドは使わない。
  使えない環境では `cd ""` が成功扱いになり、別の場所で動き続けてしまうため。
- **Python の判定は `start_wizard.bat` と同じ考え方にしている。**
  `python3` → `python` の順に試し、判定用コード `PYCHECK` もバッチと同じ。
  マーカーのパスは環境変数 `MARKER` で渡し、コードに埋め込まない。
  マーカー名にはプロセス ID と `$RANDOM` を入れる。

### 動作確認

Python を探す部分（`MARKER=` の行から `done` の次の `rm -f` まで）を別のファイルに切り出し、
最後に `echo "PY=[$PY]"` を足して `bash` で実行する。
確認してきた条件は、通常の環境 → `python3`、Python 2 しかない → 空、Python がない → 空。

Windows の Git Bash で試す場合、`'` を含むパスを `/c/...` の形で渡すと、
Git Bash が Windows 形式に変換しないまま Python に渡すため、判定は失敗する。
macOS / Linux では起きない。
`cygpath -m` で `C:/...` の形にしてから渡すと確認できる。


## Calendar.py

### Render の無料枠で動かすための実装

- Render の Background Worker には無料枠がないため、Web Service として動かしている。
  Web Service はポートを開かないと `No open ports detected` で失敗するので、
  `PORT` 環境変数（既定 8080）でダミーの Web サーバーを同居させている。
- UptimeRobot は HEAD リクエストを送ることがある。`do_HEAD` が無いと `501 Not Implemented` になる。
- 起動は `client.run(DISCORD_BOT_TOKEN)` だけ。ログイン時に 429（IP ごとの一時的な締め出し）を受けると、
  例外のまま終了する（終了コード 1）。特別な処理はしない（利用者の方針で PR #3/#4 の形に戻した）。
  - 締め出されていることが Render 上でエラーとして見えるようにするため。
  - 代わりに、Render が自動で再起動するとログインを繰り返し、締め出しを長引かせることがある。
    README では、そのときは Suspend Service で止めて時間をおくよう案内している。
  - 過去に「自動で待ってログインし直す」「ログインを止めて待機する」「Retry-After だけ待って1回だけ再ログイン」
    「429 の応答ヘッダーをログに出す」も試したが、利用者の方針で外した。
- 接続まわり（Google API のタイムアウト・別スレッド化、コマンド失敗時の通知、自動表示の例外の受け止め）は
  利用者の方針で PR #3/#4 の形に戻している。コマンドの機能と、`/`・`/health` の状態表示は残している。
  - そのため、Google の応答待ちの間はボット全体が止まる。`update_calendar` で例外が出るとループは止まったままになる。
  - コマンドの失敗は discord.py がログに出すだけで、利用者には何も返らない（`/c` の画像生成の失敗だけは返信する）。
- `on_ready` は再接続のたびに呼ばれることがある。`loop_heartbeat`（状態表示用）は動いていないときだけ start する。
  `update_calendar.start()` は PR #3/#4 のまま無条件に呼ぶので、再接続時は `RuntimeError` がログに出る
  （ループ自体は動き続ける）。
- 自動表示の編集ループでは、メッセージごとに `asyncio.sleep(1)` を入れている。
  外すと Discord の `429 Too Many Requests` や Cloudflare の `1015` にかかりやすくなる。

### スラッシュコマンド（/date・/add）

`app_commands.CommandTree` に `/date` と `/add` を登録している。作りの要点：

- **選ばれた内容を文字のコマンドに組み立てて、既存の `_handle_message` に渡す**
  （`build_date_command()`・`build_add_command()`）。返信先は `_SlashMessage` で
  `interaction.followup` に差し替える。判定や表示の処理を二重に持たないので、文字の
  コマンドと結果が食い違わない。テストもこの同一性を確かめている
- `/add` の名前だけは組み立てた文字列に入れず、`parse_add_command()` の後で `parsed["name"]` に
  差し込む。文字列に入れると「10-12 振り返り」の先頭を時刻と、「[仕事] 定例」を追加先と読んでしまう
- 月・日付・時刻は固定の選択肢（`choices`）ではなく候補（`autocomplete`）。固定の選択肢は
  登録時に一度だけ決まるので、「2026年10月」が翌年も残ってしまう
- 候補・選択肢は最大25件（discord.py の制約）。時刻は30分刻みで48件あるので、空欄のときは 8:00〜20:00 だけ出す
- 最初に `defer(thinking=True)` で「考え中…」を返してから処理する。失敗しても必ず返信し、「考え中…」のまま残さない
- 登録（`tree.sync()`）は `on_ready` から1回だけ、裏のタスクで行う。discord.py の説明に
  「表示させるには必ず呼ぶ」とある
- `SLASH_COMMANDS=off` なら空の一覧を登録し、Discord 側の登録を消す。以前の版のコードは
  登録に触らないので、コードを戻す前にこれで一度起動しておく（README 16章）
- 導入前の状態は GitHub のブランチ `backup/before-slash-commands` に残してある

### コマンドの書式

`on_message` 内の正規表現で判定している。

```python
re.fullmatch(r"/([nau]?)([\d\-\.:]+)(r|d)?", text)   # 空き日の検索
re.fullmatch(r"/c(\d{4})-(\d{1,2})", text)            # 月間カレンダー画像
```

先頭 `n/a/u` が時間帯モード、末尾 `r` が反転、`d` が伝助形式。書式を変えたら、`/cmds` の表示と README 第II部も直す。

### Google の権限範囲と予定の追加

`Calendar.py` は `calendar.events` を使う（`events.list` と `events.insert` の両方に有効）。
同梱の API 定義（`googleapiclient/discovery_cache/documents/calendar.v3.json`）で確認できる注意点：

- `calendar.events` では `calendars.get` を呼べない（`calendar.readonly` は呼べる）。
  そのため `add` の追加先の名前と権限は、`events.list` の応答にある `summary`（カレンダー名）と
  `accessRole`（このアカウントの権限）を `fields="summary,accessRole"`・`maxResults=1` で取っている
- `setup/setup_checks.py` は `calendars.get` を使うので、自前で `calendar.readonly` を要求している。
  ここを `Calendar.py` の `SCOPES` にそろえると検証が壊れる
- 終日の予定は `date`、時刻つきは `dateTime`（オフセット必須）。`end` は排他的なので、
  終日の終わりは最終日の翌日を書く

`add` の入力の解釈は `parse_add_command()` に、Google に送る中身は `build_event_body()` に分けてあり、
どちらも Discord と Google に触らずに単体で試せる。

### 画像の寸法

`render_month_image()` 内の `CELL`（マスの一辺）と `LINE`（線の太さ）で調整する。
日本語フォントは同梱していない。月・日付は数字、曜日は色で表しているため不要。

### 既知の食い違い

`update_calendar` の見出しコメントは「5分ごと」だが、実際は `@tasks.loop(minutes=60)`（60分ごと）。

### コードを変更したときに出やすいエラー

| エラー | 原因 |
|---|---|
| `No open ports detected`（Render） | ダミー Web サーバーの部分を消した、または `PORT` を読んでいない |
| `501 Not Implemented`（UptimeRobot） | `do_HEAD` を消した |
| `event registered must be a coroutine function` | `@client.event` の直後の関数が `async def` でない、または間に別のコードが入っている |
| `ModuleNotFoundError: No module named 'PIL'` | `requirements.txt` から `Pillow` を消した（画像生成に必須） |
