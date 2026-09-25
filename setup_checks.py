# -*- coding: utf-8 -*-
"""セットアップの値が本当に使えるかを確かめる検証ロジック。

setup_wizard.py（ブラウザ画面）と、このファイル単体のコマンド実行の
両方から使う。同じ関数を使うので、どちらで確かめても結果は同じ。

■ 診断の方針
  ボット本体と同じライブラリ（discord.py / google-api-python-client）に
  問い合わせをさせる。エンドポイントやフラグの値を自前で書くと、書いた側の
  記憶違いがそのまま誤診断になるため。

  結果は3種類に分ける。
    ok        … 確認できた
    ng        … 問題があり、直し方を示せる
    unknown   … 想定外の応答。診断せず、受け取った内容をそのまま見せる

  「原因はこれです」と断定できない場合は、候補を並べて両方の確認手順を出す。
  外れた診断は、何も言わないより害が大きい。
"""

import json
import re

# 依存ライブラリは無くても「形式チェックのみ」で動かせるようにする。
# ウィザードが起動しないのが最悪なので、ここで落とさない。
try:
    import discord
    HAS_DISCORD = True
except Exception:
    HAS_DISCORD = False

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    HAS_GOOGLE = True
except Exception:
    HAS_GOOGLE = False


GUIDE = "GUIDE.md"

# 鍵JSONに最低限必要なキー
REQUIRED_KEY_FIELDS = ["type", "project_id", "private_key", "client_email", "token_uri"]


# ===== 結果を表す形 =====

def _result(status, title, detail="", fixes=None, raw=""):
    """検証1項目の結果。fixes は「直し方」の候補リスト"""
    return {
        "status": status,           # "ok" / "ng" / "unknown" / "skip"
        "title": title,            # 一行の要約
        "detail": detail,          # 補足（成功時は確認材料、失敗時は症状）
        "fixes": fixes or [],      # 直し方。2件以上なら「どちらか判別できない」意味
        "raw": raw,                # 想定外のときに見せる生の応答
    }


def ok(title, detail=""):
    return _result("ok", title, detail)


def ng(title, detail="", fixes=None):
    return _result("ng", title, detail, fixes)


def unknown(title, detail="", raw=""):
    return _result("unknown", title, detail, raw=raw)


def skip(title, detail=""):
    return _result("skip", title, detail)


# ===== ① 鍵JSON（サービスアカウント）=====

def check_service_account_json(text):
    """鍵JSONの中身を検証する。ネットワークは使わない。

    最頻出の失敗は「値が途中で切れている」。これは末尾を見れば断定できる。"""
    if not text or not text.strip():
        return ng("SERVICE_ACCOUNT_JSON が空です",
                  fixes=[f"ダウンロードした鍵ファイルを開き、`{{` から `}}` まで"
                         f"全部を選択してコピーし、貼り付けてください（{GUIDE} 4-2）"])

    text = text.strip()

    try:
        info = json.loads(text)
    except json.JSONDecodeError as e:
        # 末尾が } でなければ、途中で切れていると断定できる。
        # このとき中身は絶対に出さない（private_key の断片が漏れる）
        if not text.endswith("}"):
            return ng(
                "鍵JSONが途中で切れています",
                f"末尾が `}}` で終わっていません"
                f"（{e.lineno}行{e.colno}文字目で解析に失敗／全体は{len(text)}文字）",
                fixes=[f"鍵ファイルの中身を、先頭の `{{` から末尾の `}}` まで"
                       f"全選択でコピーし直してください（{GUIDE} 4-2）"])
        return ng(
            "鍵JSONの形式が壊れています",
            f"{e.lineno}行{e.colno}文字目で解析に失敗: {e.msg}",
            fixes=["鍵ファイルを編集せず、そのままコピーし直してください",
                   "ダウンロードした .json ファイル以外のものを貼っていないか確認してください"])

    if not isinstance(info, dict):
        return ng("鍵JSONがオブジェクト形式ではありません",
                  f"読み取れた型: {type(info).__name__}",
                  fixes=["サービスアカウントの鍵ファイル（JSON）の中身を貼ってください"])

    missing = [k for k in REQUIRED_KEY_FIELDS if not info.get(k)]
    if missing:
        return ng(
            "鍵JSONに必要な項目がありません",
            "足りない項目: " + ", ".join(missing),
            fixes=[f"「サービスアカウント」の鍵（JSON）を作り直してください（{GUIDE} 4-2）。"
                   "OAuthクライアントIDの鍵など、別種の鍵を貼っている可能性があります"])

    if info.get("type") != "service_account":
        return ng(
            "サービスアカウントの鍵ではありません",
            f'type が "service_account" ではなく "{info.get("type")}" です',
            fixes=[f"認証情報の作成で「サービスアカウント」を選んで鍵を発行してください"
                   f"（{GUIDE} 4-2）"])

    email = info["client_email"]
    return ok(
        "鍵JSONは正常です",
        f"client_email: {email}\n"
        f"↑ この値を、読みたいカレンダーの「設定と共有」に追加します（{GUIDE} 4-3）")


def client_email_of(text):
    """鍵JSONから client_email だけ取り出す（画面に表示して共有作業に使う）"""
    try:
        return json.loads(text).get("client_email", "")
    except Exception:
        return ""


# ===== ② Googleカレンダー =====

def _calendar_service(sa_json_text):
    info = json.loads(sa_json_text)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/calendar.readonly"])
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _http_error_reason(err):
    """HttpError から Google が返した reason を取り出す（切り分けに使う）"""
    try:
        body = json.loads(err.content.decode("utf-8"))
        errors = body.get("error", {}).get("errors") or []
        if errors:
            return errors[0].get("reason", "")
        return body.get("error", {}).get("status", "")
    except Exception:
        return ""


def check_calendar(sa_json_text, calendar_id, client_email=""):
    """1つのカレンダーIDについて、実際に予定を取得できるか確かめる。

    ボット本体と同じ events().list を使う。別の方法で確かめても意味がない。"""
    if not HAS_GOOGLE:
        return skip("Googleライブラリが無いため未検証",
                    "pip install -r requirements.txt を実行すると検証できます")

    calendar_id = (calendar_id or "").strip()
    if not calendar_id:
        return ng("カレンダーIDが空です",
                  fixes=[f"カレンダーの「設定と共有」→「カレンダーの統合」にある"
                         f"カレンダーIDを貼ってください（{GUIDE} 4-3）"])

    try:
        service = _calendar_service(sa_json_text)
    except Exception as e:
        return ng("鍵JSONから認証情報を作れませんでした",
                  f"{type(e).__name__}: {e}",
                  fixes=["前の手順に戻り、鍵JSONを貼り直してください"])

    try:
        # カレンダー名を取れれば、意図したカレンダーかを目視で確認できる
        meta = service.calendars().get(calendarId=calendar_id).execute()
        name = meta.get("summary", "(名称不明)")
        events = service.events().list(
            calendarId=calendar_id, maxResults=5, singleEvents=True,
            orderBy="startTime",
            timeMin=__import__("datetime").datetime.now(
                __import__("datetime").timezone.utc).isoformat(),
        ).execute()
        count = len(events.get("items", []))
        sample = ""
        if count:
            first = events["items"][0]
            start = first["start"].get("dateTime", first["start"].get("date", ""))
            sample = f"\n直近の予定: {first.get('summary', '(無題)')}（{start[:16]}）"
        return ok(f"読み取れました:「{name}」",
                  f"このカレンダーが意図したものか、名前で確認してください。"
                  f"{sample}" if count else
                  f"このカレンダーが意図したものか、名前で確認してください。\n"
                  f"（今後の予定は登録されていません。空でも設定としては正常です）")

    except HttpError as e:
        status = getattr(e.resp, "status", None)
        reason = _http_error_reason(e)
        raw = f"HTTP {status} / reason={reason or '(なし)'}"

        if status == 404:
            # 応答からは「ID誤り」と「共有漏れ」を区別できない。断定しない。
            share_to = client_email or client_email_of(sa_json_text) or "（鍵JSONの client_email）"
            return ng(
                "このカレンダーが見つかりません（HTTP 404）",
                "考えられる原因は2つあり、応答からはどちらか判別できません。",
                fixes=[
                    f"(a) カレンダーIDの誤り … カレンダーの「設定と共有」→"
                    f"「カレンダーの統合」のIDと、貼った値 `{calendar_id}` を照合してください",
                    f"(b) 共有漏れ … このカレンダーの「設定と共有」→「特定のユーザーや"
                    f"グループと共有」に `{share_to}` を追加してください"
                    f"（権限は「予定の表示」でOK／{GUIDE} 4-3）",
                ])

        if status == 403 and reason in ("accessNotConfigured", "SERVICE_DISABLED"):
            return ng(
                "Google Calendar API が有効になっていません（HTTP 403）",
                f"reason={reason}",
                fixes=[f"Google Cloud Console →「APIとサービス」→「ライブラリ」→"
                       f"「Google Calendar API」を有効化してください（{GUIDE} 4-1）"])

        if status == 403:
            return ng(
                "このカレンダーを読む権限がありません（HTTP 403）",
                f"reason={reason or '(なし)'}",
                fixes=[f"カレンダーの共有権限を確認してください。"
                       f"「予定の表示」以上が必要です（{GUIDE} 4-3）"])

        if status == 401:
            return ng(
                "認証が拒否されました（HTTP 401）",
                f"reason={reason or '(なし)'}",
                fixes=["鍵が失効しているか削除された可能性があります。"
                       f"サービスアカウントの鍵を作り直してください（{GUIDE} 4-2）"])

        return unknown("想定していない応答が返りました",
                       "この応答に対する診断は用意していません。内容をそのまま表示します。",
                       raw=f"{raw}\n{e}")

    except Exception as e:
        return unknown("検証中に予期しないエラーが発生しました",
                       "診断せず、そのまま表示します。",
                       raw=f"{type(e).__name__}: {e}")


def check_calendars(sa_json_text, calendar_ids, client_email=""):
    """複数のカレンダーIDをまとめて検証する"""
    return [
        {"calendar_id": cid, **check_calendar(sa_json_text, cid, client_email)}
        for cid in calendar_ids
    ]


def split_calendar_ids(text):
    """Calendar.py と同じ規則でカレンダーIDを分解する（「,」か改行、重複は除く）"""
    out = []
    for cid in re.split(r"[,\n]", text or ""):
        cid = cid.strip()
        if cid and cid not in out:
            out.append(cid)
    return out


# ===== ③ Discord =====

def _token_shape_problem(token):
    """接続する前に、形だけで分かる問題を潰しておく。
       ここで潰せば「トークンが無効」の原因から空白混入を除外できる"""
    if not token:
        return "トークンが空です"
    if token != token.strip():
        return "前後に空白または改行が混入しています"
    if any(c.isspace() for c in token):
        return "トークンの途中に空白または改行が混入しています"
    if token.lower().startswith("bot "):
        return "先頭に `Bot ` が付いています（トークン本体だけを貼ってください）"
    return ""


async def check_discord(token, channel_id_text=None, send_test=False):
    """Discordのトークン・MESSAGE CONTENT INTENT・チャンネルを検証する。

    ボット本体と同じ discord.py に問い合わせさせる。
    戻り値は {"token":…, "intent":…, "channel":…} の3項目。"""
    results = {"token": None, "intent": None, "channel": None}

    if not HAS_DISCORD:
        s = skip("discord.py が無いため未検証",
                 "pip install -r requirements.txt を実行すると検証できます")
        return {"token": s, "intent": dict(s), "channel": dict(s)}

    shape = _token_shape_problem(token or "")
    if shape:
        results["token"] = ng(
            f"トークンの形に問題があります：{shape}",
            fixes=[f"Developer Portal → Bot →「Reset Token」で表示された値を、"
                   f"前後に余計な文字を入れずに貼り直してください（{GUIDE} 3-1）"])
        results["intent"] = skip("トークンが使えないため未検証")
        results["channel"] = skip("トークンが使えないため未検証")
        return results

    client = discord.Client(intents=discord.Intents.default())
    try:
        # --- トークンの有効性 ---
        try:
            await client.login(token)
        except discord.LoginFailure:
            results["token"] = ng(
                "トークンが受け付けられませんでした",
                "形式上の問題（空白混入など）は除外済みです。"
                "考えられる原因は2つあり、応答からは判別できません。",
                fixes=[
                    "(a) 値の誤り … Developer Portal → Bot →「Reset Token」で"
                    "新しく発行し、表示された値をそのまま貼ってください",
                    "(b) 古いトークン … 一度 Reset すると以前のトークンは無効になります。"
                    "最後に発行した値を使っているか確認してください",
                ])
            results["intent"] = skip("トークンが使えないため未検証")
            results["channel"] = skip("トークンが使えないため未検証")
            return results
        except discord.HTTPException as e:
            results["token"] = unknown(
                "ログイン時に想定外の応答が返りました",
                "診断せず、そのまま表示します。",
                raw=f"HTTP {getattr(e, 'status', '?')}: {e}")
            results["intent"] = skip("トークンの確認が済んでいないため未検証")
            results["channel"] = skip("トークンの確認が済んでいないため未検証")
            return results

        bot_user = client.user
        results["token"] = ok(
            "トークンは有効です",
            f"ボット: {bot_user}（ID: {bot_user.id}）" if bot_user else "")

        # --- MESSAGE CONTENT INTENT ---
        # フラグのビット値は discord.ApplicationFlags の定義をそのまま使う
        try:
            app = await client.application_info()
            flags = app.flags
            enabled = flags.gateway_message_content or flags.gateway_message_content_limited
            if enabled:
                limited = (flags.gateway_message_content_limited
                           and not flags.gateway_message_content)
                results["intent"] = ok(
                    "MESSAGE CONTENT INTENT は ON です",
                    "（100サーバー未満向けの限定付与状態です。個人利用では問題ありません）"
                    if limited else "")
            else:
                results["intent"] = ng(
                    "MESSAGE CONTENT INTENT が OFF です",
                    "この設定が無いと、/cmds などのコマンドにボットが反応しません。",
                    fixes=[f"Developer Portal → 対象アプリ → Bot → "
                           f"Privileged Gateway Intents → "
                           f"「MESSAGE CONTENT INTENT」を ON にして保存（{GUIDE} 3-1）"])
        except discord.HTTPException as e:
            results["intent"] = unknown(
                "INTENT の状態を取得できませんでした",
                "診断せず、そのまま表示します。",
                raw=f"HTTP {getattr(e, 'status', '?')}: {e}")

        # --- チャンネル ---
        if channel_id_text is None:
            results["channel"] = skip("チャンネルIDは未入力です")
            return results

        results["channel"] = await _check_channel(client, channel_id_text, send_test)
        return results

    finally:
        await client.close()


async def _check_channel(client, channel_id_text, send_test):
    raw_text = (channel_id_text or "").strip()
    if not raw_text:
        return ng("チャンネルIDが空です",
                  fixes=[f"Discordの設定で開発者モードをONにし、チャンネルを右クリック→"
                         f"「チャンネルIDをコピー」で取得してください（{GUIDE} 3-3）"])

    if not raw_text.isdigit():
        return ng(
            "チャンネルIDが数字ではありません",
            f"入力値: {raw_text!r}",
            fixes=[f"チャンネル名やURLではなく、右クリック→「チャンネルIDをコピー」で"
                   f"得られる数字だけを貼ってください（{GUIDE} 3-3）"])

    if not 15 <= len(raw_text) <= 21:
        return ng(
            "チャンネルIDの桁数が想定外です",
            f"{len(raw_text)}桁です（通常は17〜19桁）",
            fixes=[f"右クリック→「チャンネルIDをコピー」で取得した値を"
                   f"そのまま貼ってください（{GUIDE} 3-3）"])

    channel_id = int(raw_text)
    try:
        channel = await client.fetch_channel(channel_id)
    except discord.NotFound:
        # 「IDが違う」と「ボットが未招待」は応答から区別できない
        return ng(
            "このチャンネルが見つかりません",
            "考えられる原因は2つあり、応答からはどちらか判別できません。",
            fixes=[
                f"(a) IDの誤り … 右クリック→「チャンネルIDをコピー」で取り直してください"
                f"（{GUIDE} 3-3）",
                f"(b) ボットが未招待 … OAuth2 → URL Generator で `bot` と "
                f"`Send Messages` を選び、生成URLからサーバーに招待してください"
                f"（{GUIDE} 3-2）",
            ])
    except discord.Forbidden:
        return ng(
            "このチャンネルを見る権限がありません",
            fixes=["チャンネルの権限設定で、ボットに「チャンネルを見る」を"
                   "許可してください"])
    except discord.HTTPException as e:
        return unknown("チャンネル取得で想定外の応答が返りました",
                       "診断せず、そのまま表示します。",
                       raw=f"HTTP {getattr(e, 'status', '?')}: {e}")

    if not isinstance(channel, discord.TextChannel):
        return ng(
            "テキストチャンネルではありません",
            f"種別: {type(channel).__name__}",
            fixes=["ボイスチャンネルやカテゴリではなく、予定を表示したい"
                   "テキストチャンネルのIDを指定してください"])

    guild_name = getattr(getattr(channel, "guild", None), "name", None)
    where = f"#{channel.name}"
    if guild_name:
        where += f"（サーバー: {guild_name}）"

    if not send_test:
        # 送信権限は gateway に接続しないと計算できないため、既定では確認しない
        return ok(
            f"チャンネルを確認しました: {where}",
            "送信権限は未確認です（読み取りだけでは判定できません）。\n"
            "確かめるにはテスト送信を実行してください。")

    # --- テスト送信（明示的に選んだときだけ）---
    try:
        msg = await channel.send("✅ セットアップ確認用のテストメッセージです")
    except discord.Forbidden:
        return ng(
            f"{where} にメッセージを送れません",
            "チャンネルは見えていますが、送信が拒否されました。",
            fixes=[f"チャンネルの権限設定で、ボットに「メッセージを送信」を"
                   f"許可してください。招待時に `Send Messages` を"
                   f"選んでいない場合は招待URLを作り直してください（{GUIDE} 3-2）"])
    except discord.HTTPException as e:
        return unknown("テスト送信で想定外の応答が返りました",
                       "診断せず、そのまま表示します。",
                       raw=f"HTTP {getattr(e, 'status', '?')}: {e}")

    try:
        await msg.delete()
        note = "テストメッセージは削除しました。"
    except Exception:
        note = ("テストメッセージの削除に失敗しました。"
                "チャンネルに残っているので手で消してください。")

    return ok(f"送信できました: {where}", note)


# ===== ④ 総合検証 =====

async def check_all(env, send_test=False):
    """デプロイ直前の状態を、まとめて確かめる。

    各ステップの検証は「その時点」の結果に過ぎない。途中でトークンを
    Reset したり別のカレンダーを共有し直すと前の結果は無効になるため、
    最後に同じ順序で通しでやり直す。"""
    items = []

    # --- 環境変数が揃っているか ---
    names = ["DISCORD_BOT_TOKEN", "CHANNEL_ID", "CALENDAR_ID", "SERVICE_ACCOUNT_JSON"]
    missing = [n for n in names if not (env.get(n) or "").strip()]
    if missing:
        items.append({"step": "環境変数", **ng(
            "設定されていない値があります",
            "足りない値: " + ", ".join(missing),
            fixes=[f"不足している値を設定してください。ウィザードなら該当ステップに戻り、"
                   f"コマンドで実行しているなら環境変数を設定します（一覧は {GUIDE} 6章）"])})
    else:
        items.append({"step": "環境変数", **ok("4つすべて揃っています")})

    # --- 鍵JSON ---
    sa_text = env.get("SERVICE_ACCOUNT_JSON", "")
    sa = check_service_account_json(sa_text)
    items.append({"step": "鍵JSON", **sa})

    # --- カレンダー ---
    cal_ids = split_calendar_ids(env.get("CALENDAR_ID", ""))
    if sa["status"] != "ok":
        items.append({"step": "カレンダー", **skip(
            "鍵JSONが使えないため未検証", "先に鍵JSONを直してください")})
    elif not cal_ids:
        items.append({"step": "カレンダー", **ng(
            "カレンダーIDが1つも指定されていません",
            fixes=[f"カレンダーIDを1つ以上入力してください（{GUIDE} 4-3 / 4-4）"])})
    else:
        email = client_email_of(sa_text)
        for r in check_calendars(sa_text, cal_ids, email):
            items.append({"step": f"カレンダー {r['calendar_id']}", **r})

    # --- Discord ---
    d = await check_discord(env.get("DISCORD_BOT_TOKEN", ""),
                            env.get("CHANNEL_ID", ""), send_test=send_test)
    items.append({"step": "Discordトークン", **d["token"]})
    items.append({"step": "MESSAGE CONTENT INTENT", **d["intent"]})
    items.append({"step": "表示先チャンネル", **d["channel"]})

    ng_count = sum(1 for i in items if i["status"] == "ng")
    unknown_count = sum(1 for i in items if i["status"] == "unknown")
    return {
        "items": items,
        "ng": ng_count,
        "unknown": unknown_count,
        "passed": ng_count == 0 and unknown_count == 0,
    }


# ===== ⑤ 出力用のテキスト =====

def render_env_text(env):
    """Render の環境変数に貼る値を、コピーしやすい形にまとめる"""
    lines = [
        "# カレンダー空き日程ボット 環境変数",
        "# Render の Environment Variables に、Key と Value を1つずつ追加してください。",
        "# このファイルは秘密情報を含みます。共有せず、リポジトリにも入れないでください。",
        "",
    ]
    for name in ["DISCORD_BOT_TOKEN", "CHANNEL_ID", "CALENDAR_ID"]:
        lines.append(f"{name}={(env.get(name) or '').strip()}")
    sa = (env.get("SERVICE_ACCOUNT_JSON") or "").strip()
    try:
        sa = json.dumps(json.loads(sa), ensure_ascii=False, separators=(",", ":"))
    except Exception:
        pass                       # 壊れていてもそのまま出す（利用者が直せるように）
    lines += [
        f"SERVICE_ACCOUNT_JSON={sa}",
        "",
        "# --- ローカルで動作確認するとき（1行として実行）---",
        "# DISCORD_BOT_TOKEN=\"...\" CHANNEL_ID=\"...\" CALENDAR_ID=\"...\" \\",
        "#   SERVICE_ACCOUNT_JSON=\"$(cat service_account.json)\" \\",
        "#   SSL_CERT_FILE=$(python3 -m certifi) python3 Calendar.py",
    ]
    return "\n".join(lines) + "\n"


# ===== ⑥ このファイル単体で実行したとき（デプロイ後の調査用）=====

def _print_item(item):
    mark = {"ok": "OK  ", "ng": "NG  ", "unknown": "??  ", "skip": "--  "}[item["status"]]
    print(f"{mark}{item.get('step', '')}: {item['title']}")
    for line in (item.get("detail") or "").splitlines():
        print(f"      {line}")
    for fix in item.get("fixes", []):
        for i, line in enumerate(fix.splitlines()):
            print(f"      {'→ ' if i == 0 else '   '}{line}")
    if item.get("raw"):
        for line in item["raw"].splitlines():
            print(f"      | {line}")


def main():
    import argparse
    import asyncio
    import os

    parser = argparse.ArgumentParser(
        description="環境変数の値が本当に使えるかを確かめます。"
                    "値は環境変数から読み込みます。")
    parser.add_argument("--send-test", action="store_true",
                        help="Discordチャンネルにテストメッセージを実際に送って"
                             "送信権限を確かめます（投稿は他の人にも見えます）")
    args = parser.parse_args()

    env = {n: os.environ.get(n, "") for n in
           ["DISCORD_BOT_TOKEN", "CHANNEL_ID", "CALENDAR_ID", "SERVICE_ACCOUNT_JSON"]}

    if not HAS_DISCORD or not HAS_GOOGLE:
        print("※ 一部のライブラリが見つかりません。"
              "pip install -r requirements.txt を実行すると全項目を検証できます。\n")

    report = asyncio.run(check_all(env, send_test=args.send_test))
    for item in report["items"]:
        _print_item(item)
    print()
    if report["passed"]:
        print("結果: 問題は見つかりませんでした。")
        return 0
    parts = []
    if report["ng"]:
        parts.append(f"{report['ng']}件の問題")
    if report["unknown"]:
        parts.append(f"{report['unknown']}件の判定不能")
    print(f"結果: {'と'.join(parts)}があります。上から順に確認してください。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
