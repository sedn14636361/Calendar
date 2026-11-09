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
- **`/date`・`/add` とも欄を持たず、送った人だけに見える設定パネル（`DatePanel`・`AddPanel`、`LayoutView`）を開く。**
  共通部分は `_Panel` にまとめている。日付を「年」「月」「日（1〜15）」「日（16〜31）」の4つのメニューで選ぶため、
  開始と終了で8つのメニューが要る。入力フォーム（欄5つまで）にも、従来のメッセージ（`View`、5行まで＝選択メニュー5つまで）にも
  収まらない。`LayoutView` は部品40個まで（今はどちらも37個。足すなら見出しの `TextDisplay` を減らすなどの工夫が要る）
  - 日を2つのメニューに分けているのは、1つのメニューの選択肢が25件までのため（31日は収まらない）。
    選んだ年月に実在する日だけを、曜日を添えて並べる（`_date_rows()`）。月を変えて日が無くなったら、
    `_date_error()` が既存の add と同じ「存在しない日付です」を要約に出す
  - 年は今年と来年。一覧はパネルを開くたびに今日から作る（固定の `choices` は登録時に決まって古くなるため）
  - パネルは選ばれた内容（状態）を持ち、操作のたびに `render()` で部品を作り直して `edit_message(view=self)` で書き換える。
    部品の識別子は作り直すたびに変わるが、discord.py が `edit_message` の後に登録し直し、古い登録は消す（テストで確認）
  - 上部の要約は `check()` の結果。今の選択を文字のコマンドに組み立て、`/add` は `parse_add_command()`、
    `/date` は `check_range()`（文字のコマンドと共用の範囲検査）に通す。誤りがあれば理由を出し、実行のボタンを押せなくする。
    判定を二重に持たない。`/date` の要約には、同じ結果になる文字のコマンドも出す
  - `/date` の曜日は、押すたびに選ぶ／外すが切り替わる7つのボタン（選ぶと緑）。チェックボックス（`CheckboxGroup`）は
    フォーム専用の部品でパネルには置けないため。7つすべて選んだら指定なしとして組み立てる（既存の処理と同じ扱い）
  - `/date` の「調べる」は `defer(thinking=True)` してから `_run_as_text_command()` に渡す。`/add` の名前は
    「名前を入れて追加」で開く入力欄（`NameModal`）で受け取り、`handle_add_command()` に渡す。どちらも結果はチャンネルに出る
  - 時は0〜23時の24件（「なし（終日）」と合わせて25件で、メニューの上限ちょうど。これ以上は足せない）、
    分は 00・15・30・45 の別のメニュー。時の範囲は利用者の指定。分は時を選ぶまで押せない
  - 追加先の一覧は開いたときに一度だけ取り、パネルが持ち続ける（10分の覚え書き `_calendar_choices()` も使う）。
    Google の窓口（`service`）はスレッド間で共有できないので、ほかの処理と同じく直接呼んでいる
  - 時間切れは14分（`PANEL_TIMEOUT_SEC`）。パネルを書き換えに使う `interaction` の鍵が15分で切れるため、その手前で閉じる。
    操作できるのは開いた本人だけ（`interaction_check`）
  - `/add` の日付の日のメニューは複数選べる（`_select(multi=True)`：`min_values=0`・`max_values`＝選択肢の数）。
    選んだ日は `AddPanel.days`（date の集合）に持ち、日のメニューで選び直すと、今出している年月のその組
    （1〜15日か16〜31日）の分だけを入れ替える。月を切り替えても他の月の選択は残る。上限は `ADD_MAX_DAYS`（31）
  - `check()` は日ごとに文字のコマンドを組み立てて `parse_add_command()` に通し、解釈結果の一覧を返す。
    1日だけなら今までどおり `handle_add_command()`（文字の add と同じ返信）、2日以上なら `handle_add_many()`
  - `handle_add_many()` は追加先の確認（`_writable_target()`、権限が読めて書けないなら追加を試さない）を1回だけ行い、
    日ごとに追加する。一部が失敗しても続け、結果は1通にまとめる。失敗は理由ごとに1行（日付だけを並べる）。
    返信が2000文字を超えるときは、日ごとの行を短い日付の並び＋時刻1行にする（31日・日をまたぐ時刻・200文字の名前で1735文字なので、通常は起きない）
  - 1件ずつの追加は、ほかの処理と同じく `service` を直接呼ぶ（スレッド間で共有できないため）。1件ごとに `await asyncio.sleep(0)` で順番を譲る
  - パネルの選択メニューは `required=False` で作る。discord.py の文字の選択メニューは既定で `required: true` を送る
    （本来はフォーム用の印）。付けたままだと、日の前半・後半の片方が空のときに実行のボタンが押せない症状が出た
  - 操作が届くと `パネル操作 /add: 日（1〜15） = 5` のような行と、その後の状態（実行できる／注意の文言）をログに残す
    （`_log()`）。残すのはコマンド名・項目・値・注意の文言だけで、利用者名・予定名・カレンダー名は残さない
- 入力欄（`NameModal`）は最初の応答として開く（`send_modal`）。先に `defer` すると開けない。
  実行の処理では、最初に `defer(thinking=True)` で「考え中…」を返してから処理する。
  失敗しても必ず返信し、「考え中…」のまま残さない
- 登録（`tree.sync()`）は `on_ready` から1回だけ、裏のタスクで行う。discord.py の説明に
  「表示させるには必ず呼ぶ」とある
- ただし `tree.sync()` そのものは使わず、同じ中身（`command.to_dict(tree)` の一覧）を `client.http.bulk_upsert_global_commands()` で送る。
  アクティビティを有効にすると Discord が Entry Point コマンド（「Launch」、種類4）を自動で作り、一括登録にそれが含まれないと
  400（error code 50240「You cannot remove this app's Entry Point command in a bulk update operation」）で拒否される。
  discord.py 2.7 は種類4を扱えないので、`_entry_point_payloads()` で登録済みのものを読み取り、id などを除いて一緒に送り直す
- `SLASH_COMMANDS=off` なら空の一覧を登録し、Discord 側の登録を消す。以前の版のコードは
  登録に触らないので、コードを戻す前にこれで一度起動しておく（README 16章）
- 導入前の状態は GitHub のブランチ `backup/before-slash-commands` に残してある
- **再起動のあとの古い自動表示の削除**は `remove_old_displays()`。どれが自動表示かは `state["messages"]`（メモリ）にしか無く、
  再起動すると前の表示が残るため、起動後に最初に表示を送る直前（読み取りに成功した回）に1回だけ、チャンネルの新しい方から
  200件（`_CLEANUP_SCAN`）を調べて消す（`state["cleaned"]`）。見分けは `is_display_message()`：ボット自身・本文なし・埋め込み1つ・
  色 `DISPLAY_COLOR`・項目名がすべて「11/5（木）」の形。埋め込みを使うのは自動表示だけなので、返信や人のメッセージは当たらない。
  ピン留めは残す。自分のメッセージなので「メッセージの管理」は不要（探すのに「メッセージ履歴を読む」を使う）。
  続けて消すと 429（速すぎる）が返り、discord.py の自動のやり直しでログに警告が残るので、1通ごとに1秒空ける（`_CLEANUP_INTERVAL_SEC`）。
  自動表示の見た目（色・項目名の形）を変えるときは、`is_display_message()` も合わせて直すこと。直さないと、変更前の表示を消せなくなる
- **`/purge`** は `purge_messages()` で、新しい方から「残すもの」を飛ばしてちょうど指定の数を削除する。
  discord.py の `channel.purge(limit=…)` は `limit` が「調べる数」で、飛ばした分だけ消える数が減るため使っていない
  - 残すのは自動表示のメッセージ（`state["messages"]`。消すと以後の `msg.edit` が毎回失敗し、自動表示が復活しない）、
    ピン留め、`MessageType.is_deletable()` が偽のもの。さかのぼるのは1000件まで
  - Discord の一括削除は「14日以内・2〜100件」だけ。境目のものが送るまでに14日を過ぎないよう1時間の余裕をとり、
    それより古いものと1件だけのときは1件ずつ消す
  - 使える人は `default_permissions(manage_messages=True)`（一覧の表示）と
    `checks.has_permissions(manage_messages=True)`（実行時の確認）の両方で絞る。断るときは `slash_purge_error` が本人だけに返す
  - ログには件数とチャンネル番号だけを残す
- `print()` の出力は、起動時に `sys.stdout.reconfigure(line_buffering=True)` で1行ごとに書き出す。
  Render のように出力をログへ流すと Python はため込むので、これが無いとボットの `print` がログに出ない

### カレンダー画面（Discord のアクティビティ）

`/add` を送ると、Discord の中（スマホでも）でカレンダーの画面（アクティビティ）をすぐ開き（`launch_activity`）、そこで予定を追加する。
`DISCORD_CLIENT_SECRET` があるときだけ有効（`ACTIVITY_ENABLED`）。開けなかったとき（無効・開発チーム以外など）は、今までの設定パネルを出す。仕様は Discord の公式ドキュメント（docs.discord.com の Activities）で確認した。

- **画面**：`activity/`（`index.html`・`app.css`・`app.js`）。Web サーバー（`HealthHandler`）が `/activity/` の下で配る。
  配るファイルは `_ACTIVITY_FILES` に書いたものだけ（ファイルの置き場所を外れた読み出しを防ぐ）。
  Developer Portal の URL Mappings は「`/` → `<ホスト>/activity`」。アクティビティの中からは外部に通信できず（CSP）、
  外部の配布元から部品を読み込めないので、`@discord/embedded-app-sdk` 2.5.0 を esbuild で1つにまとめた `activity/discord-sdk.js` を同じ場所から配る
  （中の部品のライセンスは `activity/THIRD_PARTY_LICENSES.txt`）。画面の中のパスは相対パス（`app.js`・`api/start`）にしている。
  割り当ての付け方（`/activity` の後ろに要求のパスが付くか）がドキュメントに明記されていないため
- **本人確認**：画面が SDK の `authorize`（scope は `identify` だけ）で得たコードを `api/start` に送り、ボットが
  Discord の `oauth2/token`（クライアントシークレットを使う）と `users/@me` で利用者を確かめる。画面から届く情報は信じない
- **チケット**：`/add` を送ったとき、送った本人とチャンネルを15分だけ記録する（`issue_activity_ticket`）。追加先の候補は `api/start` で読む（`/add` の応答を3秒以内に返すため）。
  `api/start` で確かめた利用者にチケットがあるときだけ、合言葉（`secrets.token_urlsafe(32)`）を渡す。
  合言葉は `api/data`・`api/add` の本文で送られ、URL には載らない（Render のアクセスログに残さないため）。
  追加は1回だけ（`used` を立ててから追加する。同時に押されても1回）。開けなかったときは `revoke_activity_ticket_for` で消す
- **時間**：画面は「全部同じ時間」と「日ごとに指定」（終日の日と混在可）を切り替える。送るのは日ごとの `entries`（`{day, start, end}`、開始・終了が無ければ終日）。
  「日ごとに指定」の日は終日から始める（`allDayTime`）。切り替えたときに全部同じ時間で時間を決めていたときだけ、それを初期値にする。
  「終日」を押して外すと「未選択」になり、時間を決めるまで追加できない
- **時間の帯を動かす**：決まった帯（`.pick.live`）は上下のつまみ（`.grip`）で開始・終了、真ん中で帯ごと動かす（15分きざみ・`dragTo`）。
  指の追跡は Pointer Events（`setPointerCapture` を列に付ける）。動かしている間は帯だけ描き直す（`redrawBands`。列ごと描き直すと追跡が切れる）。
  帯とつまみは `touch-action: none`（その上ではスクロールしない）、空いている枠はスクロールできる。開始と終了は逆転・一致しないように止める
- **名前の入力とキーボード**：画面は Discord の中の iframe で動く（SDK は `window.parent` に送る）ので、スマホのブラウザが入力欄を
  キーボードの上まで自動で動かさないことがあり、下に固定した「追加する」のバーも欄に重なっていた。スマホ（`platform=mobile` か指で操作する端末）では、
  名前の欄に入ると `body.typing` を付けてバーを隠し、下に余白を足して、欄を見えている範囲の上から2割の所まで動かす（`keepFieldVisible`）。
  キーボードは iPhone・Android とも画面の下4〜5割ほど（予測変換の欄を含む）なので、高さが分からなくても隠れない位置にした。
  `visualViewport` がキーボードの分だけ縮む端末では、縮んだ範囲に合わせて動かし直す。Enter（「完了」）で閉じる。日本語の変換を確定する Enter（`isComposing`）では閉じない
- **近くのタップ**：決まった時間の近く（`NEAR` = 60分＝表の2マス＝48px）の枠をタップすると、近い方の端をそこへ動かす（`nearMove`）。
  どちらの端かは、帯の外なら上か下か、帯の中なら帯の真ん中より上か下かで決める（日をまたぐ帯は、空いている所で近い方）。
  帯の中のタップは、帯を押して動かさずに離したときに扱う（`endDrag`）。離れた枠は今まで通り選び直し。
  48px は、押しやすい大きさの目安（Apple の 44pt、Material Design の 48dp）を下回らないようにした値。
  テストのタッチ操作（CDP）では、指を離して0.15秒ほどで次をタップすると Chromium が click にしないので、離してから0.4秒待つ
- **追加**：`build_web_requests` で日ごとに文字のコマンドを組み立てて `parse_add_command` に通し、`handle_add_command`／`handle_add_many` に渡す。
  返信先は `_ActivityChannel`（チャンネルに送りつつ控え、画面にも返す。送れなくても追加は済んでいるので画面に伝える）
- **スレッド**：Web サーバーは `ThreadingHTTPServer`（追加中も監視に応答するため）。Google と Discord への問い合わせは
  `asyncio.run_coroutine_threadsafe` でボット本体の処理の流れ（`_bot_loop`、`on_ready` で記録）に渡す（`service` はスレッド間で共有できない）
- **ログ**：`アクティビティ: …` の行。要求のパス（`?` 以降は除く）と結果の状態だけを残し、合言葉・アクセストークン・クライアントシークレット・予定名は残さない
- **見せる予定**：`api/data` は最大43日分。予定の名前は自動表示と同じ範囲（今日から `DAYS_TO_SHOW` 日）だけ返し、それより先は名前なし
- **古いファイルの使い回し**：iPhone の Discord（か Discord の中継）は `Cache-Control: no-store` を守らず、古い `app.css`・`app.js` を使い回すことがあった
  （新しい HTML に古い CSS が当たり、見た目が崩れた。ログに `GET /activity/app.css` が出ていなかった）。
  そのため HTML を配るときに `app.css?v=<中身の SHA-256 の先頭12文字>`・`app.js?v=…` に書き換え、`app.js` の中の SDK の読み込みにも同じ印を付ける（`_asset_version`）。
  中身が変われば読み込み先も変わるので、古いものは使われない。CSS では iPhone の標準のボタンの見た目（`appearance`）と一覧の黒丸も打ち消している
- 画面に入れる文字はすべて `textContent`（`innerHTML` は使わない）。CSP は `default-src 'self'` ほか。テストでは予定名の `<img onerror>` が動かないことを確かめている

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
