# ===== ① 必要な道具（ライブラリ）を読み込む =====
import os                              # 環境変数（コード外から渡す値）を読むための道具
import json                           # JSON文字列を扱うための道具
import discord
from discord import app_commands       # スラッシュコマンド（/date・/add）
import threading                      # 2つの処理を同時に動かすための道具
import re                              # 文字列のパターンを判定する道具
import unicodedata                      # 全角の数字・記号を半角にそろえる道具
import calendar as _calendar           # 月末日を求める道具
import asyncio
import time                             # 処理が止まっていないかを測る道具
import io                               # 画像をメモリ上で扱う道具
import math                             # 応答時間が有限かを確かめる道具
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler  # 簡易Webサーバー（同時に応答できる形）
import secrets                          # 推測できない乱数（カレンダー画面の合言葉）
import aiohttp                          # Discord のログイン確認（discord.py が使っているもの）
from discord.ext import tasks
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError   # Google が返したエラー（403 など）
from datetime import datetime, timezone, timedelta, date
from PIL import Image, ImageDraw, ImageFont  # 画像生成の道具（要 Pillow）
import sys

# print の出力を1行ごとに書き出す。Render のようにログへ流すとき、Python は出力をため込み、
# ボットのログ程度の量では書き出されないまま残る（「登録しました」などがログに出なくなる）
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(line_buffering=True)
    except AttributeError:
        pass                                # 差し替えられた出力先（テストなど）は設定できないのでそのまま


# ===== ② 設定値（環境変数から読み込む） =====
DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
CHANNEL_ID = int(os.environ["CHANNEL_ID"])   

# カレンダーIDは「,」または改行で区切って複数指定できる。1つだけでも従来どおり動く
CALENDAR_IDS = []
for _cid in re.split(r"[,\n]", os.environ["CALENDAR_ID"]):
    _cid = _cid.strip()
    # 同じIDを二重に書くと同じ予定が2回表示されてしまうので除く
    if _cid and _cid not in CALENDAR_IDS:
        CALENDAR_IDS.append(_cid)
if not CALENDAR_IDS:
    raise ValueError("CALENDAR_ID にカレンダーIDが1つも入っていません")

DAYS_TO_SHOW = 90
DAYS_PER_MESSAGE = 25
JST = timezone(timedelta(hours=9))     # 日本時間


# ===== ③ Googleカレンダーへの接続準備（鍵も環境変数から） =====
# 予定の読み取り（events.list）と追加（events.insert）の両方に使える範囲。
# calendar.readonly のままでは追加できない（API 定義の events.insert の scopes に無い）
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

service_account_info = json.loads(os.environ["SERVICE_ACCOUNT_JSON"])
creds = service_account.Credentials.from_service_account_info(  
    service_account_info, scopes=SCOPES
)
service = build("calendar", "v3", credentials=creds)


# ===== ④ Discordへの接続準備 =====
intents = discord.Intents.default()
intents.message_content = True         # メッセージ本文を読めるようにする
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)  # スラッシュコマンドの登録先

# スラッシュコマンドを使うか。off にして起動すると、Discord に登録済みの
# /date・/add を消してから動く（文字のコマンドはそのまま使える）。
# コードを以前の版に戻すときは、先にこれで一度起動して登録を消しておく。
# 以前の版は Discord 側の登録に触らないので、戻しただけでは一覧に残ってしまう
SLASH_COMMANDS_ENABLED = os.environ.get("SLASH_COMMANDS", "on").strip().lower() \
    not in ("off", "0", "false", "no")


# ===== ⑤ 前回の状態を覚えておく箱 =====
# cleaned：起動のあと古い自動表示を消し終えたか（起動ごとに1回だけ行う）
state = {"messages": [], "signature": None, "cleaned": False}


# ===== ⑤-A 動作状況の記録（ping と Web の状態表示で使う） =====
# 「動いていないのに動いて見える」状態を見分けるための記録。
# Webサーバー（別スレッド）からも読むが、値を丸ごと差し替えるだけなのでロックは不要。
health = {
    "started_at": datetime.now(JST),
    "loop_tick": None,         # イベントループが最後に動いた時刻（time.monotonic）
    "calendar_ok_at": None,    # カレンダーを最後に全件読めた時刻
    "calendar_error": None,    # 直近の読み取り失敗 (時刻, 説明)。全件読めたら消す
}

# イベントループの見張り。LOOP_TICK_SEC ごとに時刻を記録し、
# LOOP_STALL_SEC 以上記録が途絶えたら「固まっている」と判断する。
# 60秒は、discord.py 自身が接続を死んだと判断する既定の待ち時間
# （discord/state.py の heartbeat_timeout = 60.0）に合わせた。
LOOP_TICK_SEC = 5
LOOP_STALL_SEC = 60


# ===== ⑤-B 読み取れなかったカレンダーを伝える道具 =====

def merge_failures(*lists):
    """複数の失敗リストを、重複を除いてまとめる"""
    merged = []
    for lst in lists:
        for cid in lst:
            if cid not in merged:
                merged.append(cid)
    return merged


def failure_note(failures):
    """失敗があれば、結果に添える警告文を返す（なければ空文字）。
       カレンダーID自体は Discord に出さず件数だけ示し、詳細はログに済ませる"""
    if not failures:
        return ""
    return (f"\n⚠️ {len(failures)}件のカレンダーを読み取れませんでした。"
            "結果が不完全な可能性があります（詳細はサーバーのログ）")


# ===== ⑥ カレンダーから予定を取ってくる関数 =====

API_PAGE_SIZE = 2500                   # 1回の問い合わせで取る件数（APIの上限）
API_MAX_PAGES = 20                     # たどるページの上限（2500×20＝5万件相当）


def _event_sort_key(e):
    """終日予定と時刻付き予定が混ざっていても並べられるキーを作る。
       終日予定はその日の0:00として扱い、同じ時刻なら終日を先に置く"""
    start = e["start"]
    if "dateTime" in start:
        return (datetime.fromisoformat(start["dateTime"]).astimezone(JST), 1)
    d = date.fromisoformat(start["date"])
    return (datetime(d.year, d.month, d.day, tzinfo=JST), 0)


def _fetch_one_calendar(calendar_id, time_min, time_max):
    """1つのカレンダーから、指定範囲の予定を全ページ分取得する。
       nextPageToken をたどらないと、上限を超えた分が黙って消える"""
    events = []
    page_token = None
    for _ in range(API_MAX_PAGES):
        result = service.events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            maxResults=API_PAGE_SIZE,
            singleEvents=True,
            orderBy="startTime",
            pageToken=page_token,
        ).execute()
        events.extend(result.get("items", []))
        page_token = result.get("nextPageToken")
        if not page_token:                 # 次のページがなければ終わり
            return events
    # 上限までたどっても終わらない場合。黙って不完全な結果を返すと
    # 空き日を誤判定するので、失敗として呼び出し元に伝える
    raise RuntimeError(
        f"予定が多すぎて取りきれません（{API_MAX_PAGES}ページで打ち切り）: {calendar_id}"
    )


def fetch_events_multi(start_dt, end_dt):
    """全カレンダーから予定を集め、開始時刻順に並べて返す。
       戻り値は (予定のリスト, 読み取れなかったカレンダーIDのリスト)。
       1つが読めなくても他は返すが、失敗を黙って捨てない（空きの誤判定を防ぐ）"""
    time_min = start_dt.isoformat()
    time_max = end_dt.isoformat()
    events = []
    failures = []
    for calendar_id in CALENDAR_IDS:
        try:
            events.extend(_fetch_one_calendar(calendar_id, time_min, time_max))
        except Exception as e:
            failures.append(calendar_id)
            print(f"カレンダー読み取りエラー: {calendar_id} -> {e}")
    events.sort(key=_event_sort_key)       # 連結しただけでは順序が崩れるので並べ直す
    if failures:
        health["calendar_error"] = (
            datetime.now(JST), f"{len(failures)}件のカレンダーを読み取れませんでした")
    else:
        health["calendar_ok_at"] = datetime.now(JST)
        health["calendar_error"] = None
    return events, failures


def fetch_events():
    now = datetime.now(JST)
    time_max = now + timedelta(days=DAYS_TO_SHOW)
    return fetch_events_multi(now, time_max)


# ===== ⑦ 予定を日付ごとに整え、複数のEmbedに分割する関数 =====
def build_embeds(events):
    events_by_date = {}
    for e in events:
        start_raw = e["start"].get("dateTime", e["start"].get("date"))
        if "dateTime" in e["start"]:
            dt = datetime.fromisoformat(start_raw).astimezone(JST)
            date_key = dt.date().isoformat()
            time_part = dt.strftime("%H:%M")
            line = f"{time_part} {e.get('summary', '(無題)')}"
        else:
            date_key = start_raw[:10]
            line = f"終日 {e.get('summary', '(無題)')}"
        events_by_date.setdefault(date_key, []).append(line)

    today = datetime.now(JST).date()
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]
    all_days = []
    for i in range(DAYS_TO_SHOW):
        day = today + timedelta(days=i)
        date_key = day.isoformat()
        weekday = weekdays[day.weekday()]
        field_name = f"{day.month}/{day.day}（{weekday}）"
        day_events = events_by_date.get(date_key, [])
        field_value = "\n".join(day_events) if day_events else "—"
        all_days.append((field_name, field_value))

    embeds = []
    for start in range(0, len(all_days), DAYS_PER_MESSAGE):
        chunk = all_days[start:start + DAYS_PER_MESSAGE]
        page = start // DAYS_PER_MESSAGE + 1
        embed = discord.Embed(color=DISPLAY_COLOR)
        for name, value in chunk:
            embed.add_field(name=name, value=value, inline=False)
        embeds.append(embed)
    return embeds


# ===== ⑦-B 再起動のあとに、前の起動で送った自動表示を消す =====
# どのメッセージが自動表示かはメモリにしか持っていないので、再起動すると前の表示がチャンネルに残る。
# 最初に表示を送る直前に、チャンネルの新しい方から探して削除する。
# 見分け方：ボット自身が送った・本文なし・埋め込み1つ・自動表示の色・項目名がすべて「11/5（木）」の形。
# このボットで埋め込みを使うのは自動表示だけなので、コマンドへの返信や人のメッセージは当たらない
DISPLAY_COLOR = 0x4285F4               # 自動表示の埋め込みの色（Google カレンダーの青）
_CLEANUP_SCAN = 200                    # 探す範囲（チャンネルの新しい方から。利用者の指定）
_CLEANUP_INTERVAL_SEC = 1              # 1通消すごとに空ける秒数（Discord の速さの制限に当たらないように）
_DISPLAY_FIELD_RE = re.compile(r"\d{1,2}/\d{1,2}（[月火水木金土日]）")


def is_display_message(message, me):
    """自動表示のメッセージなら True"""
    if me is None or message.author.id != me.id or message.content:
        return False
    if len(message.embeds) != 1:
        return False
    e = message.embeds[0]
    if e.color is None or e.color.value != DISPLAY_COLOR or not e.fields:
        return False
    return all(_DISPLAY_FIELD_RE.fullmatch(f.name or "") for f in e.fields)


async def remove_old_displays(channel):
    """前の起動で送った自動表示を消す。戻り値は消した数。
    探せない・消せないときはログに残して続ける（新しい表示はそのまま送る）"""
    found = []
    try:
        async for m in channel.history(limit=_CLEANUP_SCAN):
            if is_display_message(m, client.user):
                found.append(m)
    except discord.HTTPException as e:         # 「メッセージ履歴を読む」が無いと Forbidden
        print(f"古い自動表示を探せませんでした（{e}）。新しい表示はそのまま送ります")
        return 0
    deleted = kept = 0
    first = True
    for m in found:
        if m.pinned:                           # ピン留めは利用者が残したいものなので消さない
            kept += 1
            continue
        if not first:
            # 続けて消すと Discord から「速すぎる」（429）と返る。discord.py が自動でやり直すが
            # ログに警告が残るので、送信と同じく1通ごとに1秒空ける
            await asyncio.sleep(_CLEANUP_INTERVAL_SEC)
        first = False
        try:
            await m.delete()                   # 自分のメッセージなので「メッセージの管理」は要らない
            deleted += 1
        except discord.NotFound:
            pass                               # すでに消されていた
        except discord.HTTPException as e:
            print(f"古い自動表示を消せませんでした: {e}")
    if found:
        print(f"古い自動表示を{deleted}通削除しました" + (f"（ピン留めの{kept}通は残しました）" if kept else ""))
    return deleted


# ===== ⑧ 定期的に実行される処理（5分ごと） =====
@tasks.loop(minutes=60)
async def update_calendar():
    events, failures = fetch_events()
    if failures:
        # 一部のカレンダーが読めないまま書き換えると、予定が抜けた表示になってしまう。
        # 前回の正しい表示を残し、次の周期でやり直す
        print(f"一部のカレンダーが読めないため、自動表示の更新を見送りました: {failures}")
        return
    today = datetime.now(JST).date().isoformat()      # 今日の日付（日本時間）
    signature = str(today) + str([
        (e.get("summary"), e.get("start"), e.get("end"), e.get("updated"))
        for e in events
    ])
    if signature == state["signature"]:
        return
    state["signature"] = signature

    embeds = build_embeds(events)
    channel = client.get_channel(CHANNEL_ID)
    
    if not state["messages"]:
        if not state["cleaned"]:
            # 読み取りに成功して新しい表示を送れるときだけ消す（失敗した回は古い表示を残す）
            state["cleaned"] = True
            await remove_old_displays(channel)
        for embed in embeds:
            try:
                msg = await channel.send(embed=embed)
                state["messages"].append(msg)
                await asyncio.sleep(1) # ★1秒待つ（スロットリング）
            except discord.HTTPException as e:
                print(f"送信エラー: {e}")
                await asyncio.sleep(5) # エラーが出たら長めに待つ
    else:
        for msg, embed in zip(state["messages"], embeds):
            try:
                await msg.edit(embed=embed)
                await asyncio.sleep(1) # ★1秒待つ（スロットリング）
            except discord.HTTPException as e:
                print(f"編集エラー: {e}")
                await asyncio.sleep(5) # エラーが出たら長めに待つ

# ===== ⑧-B 指定月の空き日程を調べる機能 ★追加 =====

# 判定に使う時間帯（時単位）
DAY_START, DAY_END = 10, 18            # 日中 10:00-18:00
NIGHT_START, NIGHT_END = 18, 24        # 夜 18:00-24:00


def find_free_days(start_date, end_date, slot_start, slot_end):
    """start_date〜end_date（両端含む）で、指定時間帯に予定が無い日を返す。
       複数カレンダーのうちどれか1つでも予定があれば、その日は埋まっているとする。
       戻り値は (空いている日のリスト, 読み取れなかったカレンダーIDのリスト)"""
    # 範囲の開始0:00から、終了日の翌日0:00まで取得
    range_start = datetime(start_date.year, start_date.month, start_date.day, tzinfo=JST)
    range_end = datetime(end_date.year, end_date.month, end_date.day, tzinfo=JST) \
                + timedelta(days=1)
    events, failures = fetch_events_multi(range_start, range_end)

    busy_dates = set()
    for e in events:
        if "date" in e["start"]:                    # 終日予定
            d = datetime.fromisoformat(e["start"]["date"]).date()
            end_d = datetime.fromisoformat(e["end"]["date"]).date()
            while d < end_d:
                busy_dates.add(d)
                d += timedelta(days=1)
            continue

        ev_start = datetime.fromisoformat(e["start"]["dateTime"]).astimezone(JST)
        ev_end = datetime.fromisoformat(e["end"]["dateTime"]).astimezone(JST)
        day = ev_start.date()
        while day <= ev_end.date():
            slot_s = datetime(day.year, day.month, day.day, slot_start, tzinfo=JST)
            slot_e = datetime(day.year, day.month, day.day, 0, tzinfo=JST) \
                     + timedelta(hours=slot_end)
            if ev_start < slot_e and ev_end > slot_s:
                busy_dates.add(day)
            day += timedelta(days=1)

    # 範囲内の全日を並べ、埋まっていない日だけ残す
    free_days = []
    day = start_date
    while day <= end_date:
        if day not in busy_dates:
            free_days.append(day)
        day += timedelta(days=1)
    return free_days, failures

def parse_one_point(s, is_end):
    """'2026-9' や '2026-9.15' を日付に変換する。
       月だけの指定なら、開始側は1日、終了側は月末にする"""
    if "." in s:                        # 日にちまで指定あり（例 2026-9.15）
        ym, day = s.split(".")
        year, month = ym.split("-")
        return datetime(int(year), int(month), int(day)).date()
    else:                               # 月だけの指定（例 2026-9）
        year, month = s.split("-")
        year, month = int(year), int(month)
        if is_end:                      # 範囲の終わりなら月末の日にする
            last = _calendar.monthrange(year, month)[1]   # その月の末日（28〜31）
            return datetime(year, month, last).date()
        else:                           # 範囲の始まりなら1日にする
            return datetime(year, month, 1).date()


def parse_range(body):
    """'2026-8:2026-9' や '2026-9' を (開始日, 終了日) に変換する"""
    if ":" in body:                     # 範囲指定あり
        left, right = body.split(":")
        return parse_one_point(left, is_end=False), parse_one_point(right, is_end=True)
    else:                               # 単一（月 or 日）
        return parse_one_point(body, is_end=False), parse_one_point(body, is_end=True)

def check_range(body):
    """範囲の文字列を (開始日, 終了日, 誤り) にする。誤りが無ければ 誤り は None。
    文字のコマンドと /date のパネルの要約で同じ検査を使い、食い違わないようにする"""
    try:
        start_date, end_date = parse_range(body)
    except (ValueError, IndexError):
        return None, None, ("書式が正しくありません。\n"
                            "例：/2026-9 、/a2026-8:2026-9 、/n2026-8.15:2026-9.30 、/2026-9/sat/sun")

    if start_date > end_date:          # 開始と終了が逆なら注意
        return start_date, end_date, "開始日が終了日より後になっています"

    # 範囲は最大12か月まで（超長期の指定によるAPI過負荷・レート制限を防ぐ）
    # 開始日の12か月後を上限日として比較する
    limit_year = start_date.year + 1
    limit_month = start_date.month
    limit_day = min(start_date.day, _calendar.monthrange(limit_year, limit_month)[1])
    limit_date = date(limit_year, limit_month, limit_day)
    if end_date > limit_date:
        return start_date, end_date, "指定できる範囲は最大12か月までです"
    return start_date, end_date, None


def format_free_days_range(start_date, end_date, free_days, label):
    now = datetime.now(JST)
    stamp = now.strftime("%m/%d現在")
    period = f"{start_date.year}/{start_date.month}/{start_date.day}〜" \
             f"{end_date.year}/{end_date.month}/{end_date.day}"
    header = f"**{period}**\n{label}（{stamp}）"


    if not free_days:
        return f"{header}\n該当する日はありません"

    lines = []                         # 月ごとの1行を貯める
    current = []                       # 今の月の日にちを貯める
    prev_month = None
    for d in free_days:
        if d.month != prev_month:      # 月が変わったら
            if current:                # 前の月の分があれば1行として確定
                lines.append(", ".join(current))
            current = [f"{d.month}/{d.day}"]   # 新しい月は「9/1」から始める
            prev_month = d.month
        else:                          # 同じ月なら日にちだけ足す
            current.append(str(d.day))
    lines.append(", ".join(current))   # 最後の月の分を確定

    return header + "\n" + "\n".join(lines)


# ===== ⑦-B 曜日での絞り込み =====
# 並びは既存の伝助出力と同じで、Python の date.weekday()（月曜=0）に合わせている。
# 独立した方法（Zeller の公式）で照合済み。
WEEKDAY_NAMES = ["月", "火", "水", "木", "金", "土", "日"]

# 英語3文字と日本語1文字の両方を受け付ける。Discord では日本語入力から
# 切り替えずに打てるほうが早いため、どちらでも同じ曜日を指せるようにする。
WEEKDAY_TOKENS = {}
for _i, (_en, _ja) in enumerate([("mon", "月"), ("tue", "火"), ("wed", "水"),
                                 ("thu", "木"), ("fri", "金"), ("sat", "土"),
                                 ("sun", "日")]):
    WEEKDAY_TOKENS[_en] = _i
    WEEKDAY_TOKENS[_ja] = _i

WEEKDAY_HELP = ("使えるのは mon tue wed thu fri sat sun と 月 火 水 木 金 土 日 です。\n"
                "複数指定は /2026-9/sat/sun のように並べます（順番は自由）。")


def parse_weekdays(part):
    """'/sat/sun' や '/土,日' を曜日番号の集合にする。

    集合として扱うので、並べる順番は結果に影響しない。同じ曜日を
    重ねて書いても1回分として扱う。
    戻り値は (曜日番号の集合, 読めなかった語のリスト)。
    指定が無ければ空集合を返す＝絞り込みなし。
    """
    wanted = set()
    unknown = []
    for token in re.split(r"[/,]", part):
        token = token.strip()
        if not token:
            continue
        key = WEEKDAY_TOKENS.get(token.lower())
        if key is None:
            unknown.append(token)
        else:
            wanted.add(key)
    return wanted, unknown


def weekday_label(wanted):
    """選んだ曜日を見出し用の文字列にする。例）土曜・日曜

    入力の順番によらず同じ見た目になるよう、必ず月→日に並べ替える。
    「曜」を付けるのは、単独の「月」が月（month）と読めてしまうため。
    """
    return "・".join(WEEKDAY_NAMES[i] + "曜" for i in sorted(wanted))


def filter_by_weekday(days, wanted):
    """指定された曜日の日だけを残す。指定が無ければそのまま返す"""
    if not wanted:
        return days
    return [d for d in days if d.weekday() in wanted]


def format_free_days_densuke(free_days):
    """伝助用：1日1行・曜日付きで出力する。例）9/9(水)"""
    if not free_days:
        return "該当する日はありません"
    lines = [f"{d.month}/{d.day}({WEEKDAY_NAMES[d.weekday()]})" for d in free_days]
    return "\n".join(lines)

def format_free_days(year, month, free_days, label):
    """結果を見やすい文章に整える"""
    now = datetime.now(JST)                                    # 実行した時点の日時（日本時間）
    stamp = now.strftime("%m/%d現在")                 # 「2026/07/24 15:30現在」の形
    header = f"**{year}年{month}月**\n{label}が空いている日（{stamp}）"

    if not free_days:
        return f"{header}\n該当する日はありません"

    days = [f"{free_days[0].month}/{free_days[0].day}"]        # 最初だけ「9/1」の形
    days += [str(d.day) for d in free_days[1:]]                # 2件目以降は日にちだけ
    return header + "\n" + ", ".join(days)


# ===== ⑧-D 月間カレンダー画像を生成する機能 ★追加 =====

# サーバーに標準で入っている英字フォントを順に探す（無ければ内蔵フォント）
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "DejaVuSans.ttf",
]
IMG_BG = (250, 245, 230)               # 背景クリーム #faf5e6
IMG_CELL_BG = (255, 253, 246)          # 日付マスの中 #fffdf6（背景より薄い）
IMG_EVENT_COLOR = (120, 180, 235)      # 予定の塗り色 #78b4eb
IMG_EVENT_ALPHA = 217                  # 塗りの不透明度（CSS 0.85）
IMG_GRID = (60, 55, 45)                # 枠線 #3c372d
IMG_TEXT = (60, 50, 42)                # 文字 #3c322a
IMG_SUN = (210, 70, 55)                # 日曜=赤 #d24637
IMG_SAT = (70, 112, 205)               # 土曜=青 #4670cd


def _img_font(size):
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()    # どれも無ければ内蔵フォント


def collect_month_busy(year, month):
    """指定月について、昼が埋まっている日の集合・夜が埋まっている日の集合と、
       読み取れなかったカレンダーIDのリストを返す。
       判定は空き日程コマンド（find_free_days）と同じ基準を使う。"""
    first = date(year, month, 1)
    last_day = _calendar.monthrange(year, month)[1]
    last = date(year, month, last_day)

    # find_free_days は「空いている日」を返すので、その補集合が「埋まっている日」
    day_list, day_fail = find_free_days(first, last, DAY_START, DAY_END)
    night_list, night_fail = find_free_days(first, last, NIGHT_START, NIGHT_END)
    day_free = set(day_list)
    night_free = set(night_list)
    failures = merge_failures(day_fail, night_fail)

    day_busy = set()
    night_busy = set()
    d = first
    while d <= last:
        if d not in day_free:
            day_busy.add(d)
        if d not in night_free:
            night_busy.add(d)
        d += timedelta(days=1)
    return day_busy, night_busy, failures


def render_month_image(year, month):
    """月間カレンダー画像を生成し、(PNGのバイト列, 読み取れなかったカレンダーID) を返す"""
    day_busy, night_busy, failures = collect_month_busy(year, month)

    cal = _calendar.Calendar(firstweekday=6)   # 日曜始まり
    weeks = cal.monthdayscalendar(year, month)
    rows = len(weeks)
    cols = 7

    # --- 確定した寸法（CSSの値と一致）---
    CELL = 150                     # 正方形マスの一辺
    LINE = 2                       # 枠線の太さ
    PAD = 40                       # 左右下の余白
    GAP_MONTH_WEEK = 56            # 上端↔月、月↔曜日バー
    GAP_WEEK_GRID = 24             # 曜日バー↔カレンダー
    BAR_H = 34                     # 曜日バーの高さ
    MONTH_SIZE = 150               # 月数字のフォントサイズ
    YEAR_SIZE = 40                 # 年のフォントサイズ
    DAY_SIZE = 30                  # 日付数字のフォントサイズ

    f_month = _img_font(MONTH_SIZE)
    f_year = _img_font(YEAR_SIZE)
    f_day = _img_font(DAY_SIZE)

    grid_w = CELL * cols
    grid_h = CELL * rows
    W = grid_w + PAD * 2

    # 高さ：上端余白 + 月ブロック + 月↔曜日 + 曜日バー + 曜日↔grid + grid + 下余白
    month_block_h = MONTH_SIZE     # 月数字のぶんを高さとして確保
    H = (GAP_MONTH_WEEK + month_block_h + GAP_MONTH_WEEK
         + BAR_H + GAP_WEEK_GRID + grid_h + PAD)

    img = Image.new("RGB", (W, H), IMG_BG)
    draw = ImageDraw.Draw(img, "RGBA")

    # ===== ヘッダー：月を中央、年をそのすぐ左 =====
    # 月数字の描画位置（中央）。実寸を測って中央ぞろえする。
    mb = draw.textbbox((0, 0), str(month), font=f_month)
    mw = mb[2] - mb[0]
    mh = mb[3] - mb[1]
    month_top = GAP_MONTH_WEEK
    month_x = (W - mw) / 2 - mb[0]
    # ベースライン合わせ用に月の底を基準化
    month_baseline = month_top + month_block_h
    draw.text((month_x, month_baseline - mh - mb[1]), str(month), font=f_month, fill=IMG_TEXT)

    # 年「YYYY -」を月の左に、月の下端寄りに添える
    year_text = f"{year}"
    yb = draw.textbbox((0, 0), year_text, font=f_year)
    yw = yb[2] - yb[0]
    yh = yb[3] - yb[1]
    gap_year_month = 20
    year_x = month_x - yw - gap_year_month
    # 年のベースラインを月の下端に近づける（見た目で下ぞろえ気味に）
    draw.text((year_x, month_baseline - yh - yb[1] - 8), year_text, font=f_year, fill=IMG_TEXT)

    # ===== 曜日バー =====
    bar_top = month_top + month_block_h + GAP_MONTH_WEEK
    grid_left = PAD
    bar_left = grid_left
    # 外枠
    draw.rectangle([bar_left, bar_top, bar_left + grid_w, bar_top + BAR_H],
                   outline=IMG_GRID, width=LINE)
    seg = grid_w / cols
    # 左端(日)赤・右端(土)青の薄塗り（CSS rgba 0.28 相当）
    draw.rectangle([bar_left + LINE, bar_top + LINE,
                    bar_left + seg, bar_top + BAR_H - LINE],
                   fill=(IMG_SUN[0], IMG_SUN[1], IMG_SUN[2], 72))
    draw.rectangle([bar_left + grid_w - seg, bar_top + LINE,
                    bar_left + grid_w - LINE, bar_top + BAR_H - LINE],
                   fill=(IMG_SAT[0], IMG_SAT[1], IMG_SAT[2], 72))
    # 縦の区切り線
    for c in range(1, cols):
        xx = bar_left + seg * c
        draw.line([xx, bar_top, xx, bar_top + BAR_H], fill=IMG_GRID, width=LINE)

    # ===== 日付グリッド =====
    grid_top = bar_top + BAR_H + GAP_WEEK_GRID
    for r, week in enumerate(weeks):
        for c, daynum in enumerate(week):
            x0 = grid_left + c * CELL
            y0 = grid_top + r * CELL
            x1, y1 = x0 + CELL, y0 + CELL
            # 日付ありマスは中を薄ベージュに塗る（枠より内側）
            if daynum != 0:
                draw.rectangle([x0, y0, x1, y1], fill=IMG_CELL_BG)
            # 枠線
            draw.rectangle([x0, y0, x1, y1], outline=IMG_GRID, width=LINE)
            if daynum == 0:
                continue
            d = date(year, month, daynum)
            # 昼が埋まっていれば上半分、夜が埋まっていれば下半分を塗る
            day_filled = d in day_busy
            night_filled = d in night_busy
            mid = y0 + CELL / 2
            fill = (IMG_EVENT_COLOR[0], IMG_EVENT_COLOR[1], IMG_EVENT_COLOR[2], IMG_EVENT_ALPHA)
            if day_filled:
                draw.rectangle([x0 + LINE, y0 + LINE, x1 - LINE, mid], fill=fill)
            if night_filled:
                draw.rectangle([x0 + LINE, mid, x1 - LINE, y1 - LINE], fill=fill)
            # 日付数字
            col = IMG_SUN if c == 0 else (IMG_SAT if c == 6 else IMG_TEXT)
            draw.text((x0 + 6, y0 + 4), str(daynum), font=f_day, fill=col)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf, failures


# ===== ⑧-G 予定の追加 =====
# 書式：add [カレンダー名] 日付 [時刻] 予定の名前
#   add 2026-10.5 14:00-16:00 打ち合わせ   … 時刻指定
#   add 2026-10.5 打ち合わせ               … 終日
#   add 2026-10.5:2026-10.7 合宿           … 複数日の終日
#   add [仕事] 2026-10.5 9-12 定例         … 追加先のカレンダーを名前で指定
# 名前を最後に置くので、空白を含む名前もそのまま書ける。
# 日付の次の語が「時:分-時:分」の形なら時刻指定、そうでなければ終日とみなす。

class AddError(Exception):
    """入力や設定の誤り。メッセージをそのまま利用者に返す"""


ADD_USAGE = (
    "書き方：`add 日付 [時刻] 予定の名前`\n"
    "例：`add 2026-10.5 14:00-16:00 打ち合わせ`（時刻指定）\n"
    "　　`add 2026-10.5 打ち合わせ`（終日）\n"
    "　　`add 2026-10.5:2026-10.7 合宿`（複数日の終日）\n"
    "　　`add [カレンダー名] 2026-10.5 打ち合わせ`（追加先を指定）"
)

_ADD_HEAD_RE = re.compile(r"/?add(?=\s|$)", re.IGNORECASE)
# 追加先の指定。半角 [ ] のほか、日本語入力で打ちやすい【 】と［ ］も受け付ける
_ADD_CAL_RE = re.compile(r"(?:\[([^\]]*)\]|【([^】]*)】|［([^］]*)］)")
_ADD_DATE_RE = re.compile(r"(\d{4})-(\d{1,2})\.(\d{1,2})(?::(\d{4})-(\d{1,2})\.(\d{1,2}))?")
# 区切りは - のほか、~ 〜 − ー も受け付ける（全角の －／～ は NFKC で半角になる）
_ADD_TIME_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?[-~〜−ー](\d{1,2})(?::(\d{2}))?")


def _nfkc(s):
    """全角の数字・コロン・記号を半角にそろえる（日付と時刻の部分にだけ使う。名前には使わない）"""
    return unicodedata.normalize("NFKC", s)


def _make_date(y, m, d):
    try:
        return date(int(y), int(m), int(d))
    except ValueError:
        raise AddError(f"{y}-{m}.{d} は存在しない日付です")


def _one_year_after(d):
    """d のちょうど1年後（2/29 の翌年は 2/28）。期間の上限に使う"""
    last = _calendar.monthrange(d.year + 1, d.month)[1]
    return date(d.year + 1, d.month, min(d.day, last))


def parse_add_command(text):
    """add コマンドを解釈する。

    このコマンドでなければ None を返す。会話中の「add me …」のような文に
    反応しないよう、/add で始まるか、add の後に [カレンダー名] か数字（日付）が
    続くときだけコマンドとみなす。add だけのときは使い方を返す。
    解釈できれば dict を返し、誤りがあれば AddError を投げる。
    dict の中身：
      calendar … 追加先の指定（[ ] の中身。無ければ None）
      name     … 予定の名前
      start, end … 時刻指定なら datetime（日本時間）、終日なら date。
                   終日の end は Google の仕様どおり「最終日の翌日」（排他的な終わり）
      all_day  … 終日なら True
      overnight … 終了が開始より前で、翌日にまたいだとみなしたら True
    """
    head = _ADD_HEAD_RE.match(text)
    if not head:
        return None
    rest = text[head.end():].strip()
    if not rest:
        raise AddError(ADD_USAGE)

    # --- 追加先のカレンダー（任意） ---
    calendar = None
    m = _ADD_CAL_RE.match(rest)
    if m:
        calendar = next(g for g in m.groups() if g is not None).strip()
        if not calendar:
            raise AddError("[ ] の中にカレンダー名がありません。\n" + ADD_USAGE)
        rest = rest[m.end():].strip()

    # --- 日付 ---
    parts = rest.split(None, 1)
    explicit = text.startswith("/") or calendar is not None
    if not explicit and not _nfkc(parts[0])[:1].isdigit():
        return None                            # 「add me to …」のような普通の文
    if not parts:
        raise AddError("日付がありません。\n" + ADD_USAGE)
    dm = _ADD_DATE_RE.fullmatch(_nfkc(parts[0]))
    if not dm:
        raise AddError("日付は `2026-10.5` の形で書いてください"
                       "（複数日なら `2026-10.5:2026-10.7`）。\n" + ADD_USAGE)
    first = _make_date(dm.group(1), dm.group(2), dm.group(3))
    last = _make_date(dm.group(4), dm.group(5), dm.group(6)) if dm.group(4) else None
    rest = parts[1].strip() if len(parts) > 1 else ""

    # --- 時刻（任意）：日付の次の語が丸ごと「時:分-時:分」のときだけ時刻とみなす ---
    parts = rest.split(None, 1)
    tm = _ADD_TIME_RE.fullmatch(_nfkc(parts[0])) if parts else None
    if tm:
        rest = parts[1].strip() if len(parts) > 1 else ""

    name = rest
    if not name:
        raise AddError("予定の名前がありません。\n" + ADD_USAGE)

    if tm:
        if last is not None:
            raise AddError("複数日の予定には時刻を付けられません。終日として追加するか、1日ずつ追加してください")
        sh, sm = int(tm.group(1)), int(tm.group(2) or 0)
        eh, em = int(tm.group(3)), int(tm.group(4) or 0)
        if sm > 59 or em > 59:
            raise AddError("分は 00〜59 で書いてください")
        if sh > 23:
            raise AddError("開始時刻は 0:00〜23:59 で書いてください")
        if eh > 24 or (eh == 24 and em != 0):
            raise AddError("終了時刻は 0:00〜24:00 で書いてください")
        start_min, end_min = sh * 60 + sm, eh * 60 + em
        if end_min == start_min:
            raise AddError("開始と終了が同じ時刻です")
        midnight = datetime(first.year, first.month, first.day, tzinfo=JST)
        start = midnight + timedelta(minutes=start_min)
        overnight = end_min < start_min          # 22:00-2:00 のような日またぎ
        end = midnight + timedelta(days=1 if overnight else 0, minutes=end_min)
        return {"calendar": calendar, "name": name, "start": start, "end": end,
                "all_day": False, "overnight": overnight}

    # 終日
    last = last or first
    if last < first:
        raise AddError("開始日が終了日より後になっています")
    if last > _one_year_after(first):
        raise AddError("終日の予定の期間は最大12か月までです")
    return {"calendar": calendar, "name": name, "start": first,
            "end": last + timedelta(days=1),   # Google の終日予定は「翌日まで」と書く
            "all_day": True, "overnight": False}


def build_event_body(parsed):
    """Google Calendar API の events.insert に渡す中身を作る。

    API 定義より：終日は date（yyyy-mm-dd）、時刻指定は dateTime（RFC3339、
    タイムゾーンのオフセット必須）。end は排他的（その時刻・その日を含まない）。
    """
    if parsed["all_day"]:
        start = {"date": parsed["start"].isoformat()}
        end = {"date": parsed["end"].isoformat()}
    else:
        start = {"dateTime": parsed["start"].isoformat()}
        end = {"dateTime": parsed["end"].isoformat()}
    return {"summary": parsed["name"], "start": start, "end": end}


def format_added_when(parsed):
    """返信に出す日時。日をまたぐときは終了側にも日付を付け、打ち間違いに気づけるようにする"""
    def md(d):
        return f"{d.month}/{d.day}({WEEKDAY_NAMES[d.weekday()]})"
    s = parsed["start"]
    if parsed["all_day"]:
        last = parsed["end"] - timedelta(days=1)          # 表示は最終日（含む）で
        head = f"{s.year}/{md(s)}"
        return f"{head} 終日" if last == s else f"{head}〜{md(last)} 終日"
    e = parsed["end"]
    head = f"{s.year}/{md(s.date())} {s:%H:%M}"
    if e.date() == s.date():
        return f"{head}〜{e:%H:%M}"
    if e.date() == s.date() + timedelta(days=1) and (e.hour, e.minute) == (0, 0) and not parsed["overnight"]:
        return f"{head}〜24:00"
    return f"{head}〜{md(e.date())} {e:%H:%M}（日をまたぐ予定として追加）"


# --- 追加先のカレンダーを決める ---
_WRITABLE_ROLES = ("writer", "owner", "writerWithoutPrivateAccess")


def _calendar_meta(calendar_id):
    """カレンダーの名前とこのボットの権限を取る（予定は1件も取らない小さな問い合わせ）。

    calendars.get は calendar.events の権限では使えないので、events.list の応答に
    含まれる summary（カレンダー名）と accessRole（権限）を使う。
    """
    r = service.events().list(calendarId=calendar_id, maxResults=1,
                              fields="summary,accessRole").execute()
    return r.get("summary"), r.get("accessRole")


def _calendar_label(index, name):
    """返信に出すカレンダーの呼び名。名前がメールアドレスのときは番号で呼ぶ
    （個人のメインカレンダーは名前がメールアドレスになっており、チャンネルに載せないため）"""
    if not name or "@" in name:
        return f"{index + 1}番目のカレンダー"
    return name


def _same_name(a, b):
    return _nfkc(a).strip().casefold() == _nfkc(b).strip().casefold()


def resolve_add_calendar(spec):
    """追加先を決める。戻り値は (番号, カレンダーID, 名前, 権限)。

    spec が None なら先頭。数字なら CALENDAR_ID の何番目か。
    CALENDAR_ID に書いた ID そのものでもよい。それ以外はカレンダー名で探す。
    """
    def pick(i):
        name, role = _calendar_meta(CALENDAR_IDS[i])
        return i, CALENDAR_IDS[i], name, role

    if spec is None:
        return pick(0)
    if spec.isdigit():
        i = int(spec) - 1
        if not 0 <= i < len(CALENDAR_IDS):
            raise AddError(f"カレンダーの番号は 1〜{len(CALENDAR_IDS)} で指定してください")
        return pick(i)
    if spec in CALENDAR_IDS:
        return pick(CALENDAR_IDS.index(spec))

    found, listing, unreadable = [], [], 0
    for i, cid in enumerate(CALENDAR_IDS):
        try:
            name, role = _calendar_meta(cid)
        except Exception as e:
            print(f"カレンダー名の取得エラー: {cid} -> {e}")
            unreadable += 1
            continue
        listing.append(f"{i + 1}: {_calendar_label(i, name)}")
        if name and _same_name(name, spec):
            found.append((i, cid, name, role))
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        raise AddError(f"「{spec}」という名前のカレンダーが複数あります。番号で指定してください（例：`add [1] …`）\n"
                       + "\n".join(listing))
    msg = f"「{spec}」という名前のカレンダーが見つかりません。使えるカレンダー：\n" + "\n".join(listing)
    if unreadable:
        msg += f"\n（ほかに読み取れなかったカレンダーが {unreadable} 件あります）"
    msg += "\n名前のほか、番号でも指定できます（例：`add [1] …`）"
    raise AddError(msg)


def _writable_target(spec):
    """追加先を決め、書き込めることを確かめる。戻り値は (カレンダーID, 返信に出す呼び名)。
    書き込み権限が無いことが分かっているなら、追加を試す前に AddError で理由を返す"""
    index, cid, name, role = resolve_add_calendar(spec)
    label = _calendar_label(index, name)
    if role is not None and role not in _WRITABLE_ROLES:
        raise AddError(
            f"{label}には書き込む権限がありません（いまの権限：{role}）。\n"
            "Google カレンダーの共有設定で、サービスアカウントの権限を"
            "「すべての予定の詳細の変更や表示ができます」にしてください")
    return cid, label


def _http_error_reason(e):
    """Google が返したエラー（HttpError）を、利用者向けの理由にする"""
    status = e.status_code
    print(f"予定の追加エラー（HTTP {status}）: {e}")
    if status == 403:
        return ("カレンダーに書き込む権限がありません。Google カレンダーの共有設定で、"
                "サービスアカウントの権限を「すべての予定の詳細の変更や表示ができます」にしてください")
    if status == 404:
        return "カレンダーが見つかりません。CALENDAR_ID と共有設定を確認してください"
    return f"Google がエラーを返しました（HTTP {status}）"


async def handle_add_command(message, parsed):
    """予定を追加して、結果を返信する"""
    async with message.channel.typing():
        try:
            cid, label = _writable_target(parsed["calendar"])
            created = service.events().insert(
                calendarId=cid, body=build_event_body(parsed)).execute()
        except AddError as e:
            await message.channel.send(f"⚠️ {e}")
            return
        except HttpError as e:
            await message.channel.send(f"⚠️ 予定を追加できませんでした。{_http_error_reason(e)}")
            return
        except Exception as e:
            print(f"予定の追加エラー: {e!r}")
            await message.channel.send(
                f"⚠️ 予定を追加できませんでした（{type(e).__name__}）。時間をおいて試してください")
            return

    lines = ["✅ 予定を追加しました",
             f"**{parsed['name']}**",
             format_added_when(parsed),
             f"追加先：{label}"]
    link = created.get("htmlLink")
    if link:
        lines.append(f"<{link}>")             # < > で囲み、リンクのプレビュー表示を抑える
    await message.channel.send("\n".join(lines))


ADD_MAX_DAYS = 31                      # /add のパネルで一度に追加できる日数（利用者の指定）


async def handle_add_many(message, parsed_list):
    """同じ名前・同じ時刻の予定を、日ごとに1件ずつ追加して、結果を1通にまとめて返信する。
    parsed_list は日ごとの解釈結果（追加先と名前はどれも同じ）。一部が失敗しても残りは続ける"""
    async with message.channel.typing():
        try:
            cid, label = _writable_target(parsed_list[0]["calendar"])   # 追加先の確認は1回だけ
        except AddError as e:
            await message.channel.send(f"⚠️ {e}")
            return
        except HttpError as e:
            await message.channel.send(f"⚠️ 予定を追加できませんでした。{_http_error_reason(e)}")
            return
        except Exception as e:
            print(f"予定の追加エラー: {e!r}")
            await message.channel.send(
                f"⚠️ 予定を追加できませんでした（{type(e).__name__}）。時間をおいて試してください")
            return
        added, failed = [], {}                 # failed は 理由 → 日時の一覧（同じ理由はまとめて1行にする）
        for p in parsed_list:
            try:
                service.events().insert(calendarId=cid, body=build_event_body(p)).execute()
                added.append(p)
            except HttpError as e:
                failed.setdefault(_http_error_reason(e), []).append(p)
            except Exception as e:
                print(f"予定の追加エラー: {e!r}")
                failed.setdefault(f"{type(e).__name__}。時間をおいて試してください", []).append(p)
            await asyncio.sleep(0)             # 1件ごとに、ほかの処理（自動表示など）に順番を譲る

    total = len(parsed_list)
    text = _many_reply(parsed_list[0]["name"], total, added, failed, label, compact=False)
    if len(text) > 2000:                       # 1通の上限を超えるなら、日ごとの行を短い日付の並びにする
        text = _many_reply(parsed_list[0]["name"], total, added, failed, label, compact=True)
    await message.channel.send(text)


def _start_day(p):
    s = p["start"]
    return s.date() if isinstance(s, datetime) else s


def _time_only(p):
    """日付を除いた時刻の部分（例：14:00〜16:00、22:00〜翌2:00、終日）"""
    s, e = p["start"], p["end"]
    if not isinstance(s, datetime):
        return "終日"
    nxt = "翌" if e.date() > s.date() else ""
    return f"{s:%H:%M}〜{nxt}{e:%H:%M}"


def _many_reply(name, total, added, failed, label, compact):
    if not added:
        lines = [f"⚠️ 予定を追加できませんでした（{total}件とも）", f"**{name}**"]
    elif failed:
        lines = [f"⚠️ {total}件のうち{len(added)}件を追加しました", f"**{name}**"]
    else:
        lines = [f"✅ 予定を{total}件追加しました（1日ずつ別の予定）", f"**{name}**"]
    if compact and added:
        lines.append("、".join(f"{_start_day(p).year}/{_md(_start_day(p))}" for p in added))   # 年をまたいでも正しく
        lines.append(f"時刻：{_time_only(added[0])}")
    else:
        lines += [format_added_when(p) for p in added]
    lines.append(f"追加先：{label}")
    for reason, ps in failed.items():
        # 失敗した日は短い日付だけを並べる（同じ理由はまとめて1行）
        lines.append(f"追加できなかった日（{reason}）：" + "、".join(_md(_start_day(p)) for p in ps))
    return "\n".join(lines)


# ===== ⑧-E コマンド一覧のテキスト ★追加 =====
HELP_TEXT = (
    "**📅 コマンド一覧**\n"
    "```\n"
    "■ この一覧を出す\n"
    "cmds           /cmds でも同じ（/ は付けなくてよい）\n"
    "\n"
    "■ 予定を追加する（名前は最後に書く）\n"
    "add 2026-10.5 14:00-16:00 打ち合わせ   時刻指定\n"
    "add 2026-10.5 打ち合わせ               終日\n"
    "add 2026-10.5:2026-10.7 合宿           複数日の終日\n"
    "add 2026-10.5 22:00-2:00 飲み会        終了が前なら翌日まで\n"
    "add [仕事] 2026-10.5 9-12 定例         追加先をカレンダー名で（番号も可）\n"
    "\n"
    "■ 選ぶだけで使う（入力欄で / を打つと一覧に出る）\n"
    "/date          パネルで年月日・時間帯・曜日を選んで調べる\n"
    "/add           パネルで年月日・時刻・追加先を選んで追加（複数の日も可）\n"
    "/purge         メッセージを指定した数だけ削除（管理者向け）\n"
    "\n"
    "■ 動作確認（反応がおかしいとき）\n"
    "ping           ボットの状態を表示\n"
    "\n"
    "■ 空き日程を調べる（年-月）\n"
    "/2026-9        昼(10-18)が空いている日\n"
    "/n2026-9       夜(18-24)が空いている日\n"
    "/a2026-9       昼夜とも空いている日\n"
    "/u2026-9       昼か夜が空いている日\n"
    "\n"
    "■ 予定がある日（末尾 r ／昼・夜のみ）\n"
    "/2026-9r       昼に予定がある日\n"
    "/n2026-9r      夜に予定がある日\n"
    "\n"
    "■ 伝助形式で出す（末尾 d ／1日1行・曜日つき）\n"
    "/2026-9d       昼が空いている日を伝助形式で\n"
    "/n2026-9d      夜／a・u にも付けられる\n"
    "\n"
    "■ 月間カレンダー画像\n"
    "/c2026-9       その月のカレンダー画像\n"
    "\n"
    "■ 期間の指定（: で範囲、. で日にち）\n"
    "/2026-8:2026-9         月単位の範囲\n"
    "/2026-8.15:2026-9.30   日単位の範囲\n"
    "/2026-12:2027-1        年をまたぐ範囲\n"
    "\n"
    "■ 曜日で絞る（いちばん最後に付ける）\n"
    "/2026-9/sat            土曜だけ\n"
    "/2026-9/sat/sun        土日（複数は / を重ねる）\n"
    "/2026-9/sat,sun        カンマ区切りでも同じ\n"
    "/2026-9/土/日          日本語1文字でも同じ\n"
    "/n2026-9d/fri/sat      他の指定と組み合わせ可\n"
    "  mon tue wed thu fri sat sun ／ 月 火 水 木 金 土 日\n"
    "  並べる順番は自由（/sun/sat でも同じ結果）\n"
    "\n"
    "※ 先頭(n/a/u)・期間・末尾(r か d)・曜日は組み合わせ可\n"
    "※ r と d の同時使用は不可\n"
    "※ 画像(/c)は曜日で絞れません\n"
    "```"
)


# ===== ⑧-C メッセージを受け取ったときの処理 ★追加 =====
async def _handle_message(message):
    """コマンドを解釈して返信する。

    返信したら True を返す（このボット宛てでなく何もしなかったときは None）。
    """
    text = message.content.strip()

    # --- ping ／ /ping : 動作確認と状態表示 ---
    # Google に一切触らず、平文を返すだけ。これが返ればボットは生きていて、
    # そのチャンネルに書き込める。あわせて、ボットの状態を見せる
    if text.lower() in ("ping", "/ping"):
        level, lines = status_report()
        await message.channel.send("🏓 pong\n" + "\n".join(lines))
        return True

    # --- cmds ／ /cmds : コマンド一覧を表示 ---
    # Discord で / を打つとスラッシュコマンドの候補窓が開き、同じサーバーに
    # 入っている他のボットのコマンドと取り違えやすい。/ を付けずに打てる
    # 形も用意しておく。
    # 会話中の「cmds」という語に反応しないよう、メッセージ全体が一致する
    # ときだけ返す。前後の空白は無視し、大文字小文字は問わない。
    if text.lower() in ("cmds", "/cmds"):
        await message.channel.send(HELP_TEXT)
        return True

    # --- add ／ /add : Google カレンダーに予定を追加 ---
    try:
        parsed = parse_add_command(text)
    except AddError as e:
        await message.channel.send(f"⚠️ {e}")
        return True
    if parsed is not None:
        await handle_add_command(message, parsed)
        return True

    # --- /c2026-9 : 月間カレンダー画像を出力 ---
    # 画像は月まるごとを描くものなので、曜日で絞る指定は受けられない。
    # 黙って無反応にすると原因が分からないため、理由を返す。
    if re.fullmatch(r"/c\d{4}-\d{1,2}(?:/[^/\s]+)+", text):
        await message.channel.send(
            "カレンダー画像は月まるごとを描くため、曜日での絞り込みはできません。\n"
            "曜日で絞るときは画像でない形式を使ってください（例：/2026-9/sat）"
        )
        return True

    cmatch = re.fullmatch(r"/c(\d{4})-(\d{1,2})", text)
    if cmatch:
        year = int(cmatch.group(1))
        month = int(cmatch.group(2))
        if not 1 <= month <= 12:
            await message.channel.send("月は1〜12で指定してください")
            return True
        # 「入力中…」を出して、受け付けたことを見せる
        async with message.channel.typing():
            try:
                # 画像生成は重い処理なので、別スレッドで実行してボットを固めない
                buf, failures = await asyncio.to_thread(render_month_image, year, month)
                file = discord.File(buf, filename=f"calendar_{year}_{month:02d}.png")
                await message.channel.send(
                    f"📅 {year}年{month}月{failure_note(failures)}", file=file
                )
            except Exception as e:
                print(f"画像生成エラー: {e}")
                await message.channel.send("画像の生成に失敗しました")
        return True

    # 先頭が / で、次に n/a/無し、その後ろに範囲文字列、末尾に r または d、
    # さらに最後に曜日の指定（/sat/sun など）を任意個つけられる。
    # 範囲文字列に使える文字は数字と - . : だけなので、英字で始まる
    # 曜日の部分と取り違えることはない。
    match = re.fullmatch(r"/([nau]?)([\d\-\.:]+)(r|d)?((?:/[^/\s]+)*)", text)

    if not match:
        return

    mode = match.group(1)              # "" or "n" or "a" or "u"
    body = match.group(2)              # 例 "2026-8:2026-9"
    suffix = match.group(3) or ""      # "" or "r" or "d"
    negate = suffix == "r"             # 末尾 r ＝反転（予定がある日）
    densuke = suffix == "d"            # 末尾 d ＝伝助形式（全モードで使える／r とは排他）

    # 曜日の指定（無ければ空集合＝絞り込みなし）
    wanted_weekdays, unknown_weekdays = parse_weekdays(match.group(4))
    if unknown_weekdays:
        await message.channel.send(
            "曜日の指定が分かりません：" + "、".join(unknown_weekdays) + "\n"
            + WEEKDAY_HELP
        )
        return True

    # 7曜日すべてが揃った指定は、曜日を書かなかったのと同じ扱いにする。
    # 1日も除外されないので、見出しに「月曜・火曜・…・日曜の」と並べる意味がない。
    # ここで空集合に戻しておけば、絞り込みも見出しも分岐が1つで済み、
    # 「曜日なし」と「全曜日」の出力が作りからして同じになる。
    if len(wanted_weekdays) == len(WEEKDAY_NAMES):
        wanted_weekdays = set()

    # 日付への変換と範囲の検査（/date のパネルの要約も同じ関数を使う）
    start_date, end_date, range_error = check_range(body)
    if range_error:
        await message.channel.send(range_error)
        return True

    # モードごとに空き日を求める。
    # 「入力中…」を出して受け付けたことを見せ、Google への問い合わせは
    # 別スレッドで行う（応答を待つ間もボット本体を止めないため）
    async with message.channel.typing():
        if mode == "n":
            free_days, failures = find_free_days(start_date, end_date, NIGHT_START, NIGHT_END)
            label = "夜が空いている日"
        elif mode == "a":
            day_free, day_fail = find_free_days(start_date, end_date, DAY_START, DAY_END)
            night_free, night_fail = find_free_days(start_date, end_date, NIGHT_START, NIGHT_END)
            free_days = sorted(set(day_free) & set(night_free))
            failures = merge_failures(day_fail, night_fail)
            label = "一日空いている日"
        elif mode == "u":                  # 昼か夜のどちらか（あるいは両方）が空いている日
            day_free, day_fail = find_free_days(start_date, end_date, DAY_START, DAY_END)
            night_free, night_fail = find_free_days(start_date, end_date, NIGHT_START, NIGHT_END)
            free_days = sorted(set(day_free) | set(night_free))   # どちらかに含まれる日
            failures = merge_failures(day_fail, night_fail)
            label = "昼か夜が空いている日"
        else:
            free_days, failures = find_free_days(start_date, end_date, DAY_START, DAY_END)
            label = "昼が空いている日"

    # 末尾に r が付いていたら反転（昼・夜モードのみ対象。a と u には適用しない）
    if negate and mode in ("", "n"):
        free_set = set(free_days)
        all_days = []
        d = start_date
        while d <= end_date:
            if d not in free_set:      # 空き日でない日＝予定がある日
                all_days.append(d)
            d += timedelta(days=1)
        free_days = all_days
        label = "夜に予定がある日" if mode == "n" else "昼に予定がある日"

    # 曜日で絞る。r の反転を適用した「後」に行う。
    # こうすると /2026-9r/mon は「月曜で予定がある日」になる。
    # 先に絞ってから反転すると、月曜以外が予定のある日として並んでしまう。
    if wanted_weekdays:
        free_days = filter_by_weekday(free_days, wanted_weekdays)
        # 括弧は既存の「（09/28現在）」で使っているため重ねない
        label = "%sの%s" % (weekday_label(wanted_weekdays), label)

    if densuke:
        await message.channel.send(format_free_days_densuke(free_days) + failure_note(failures))
    else:
        await message.channel.send(
            format_free_days_range(start_date, end_date, free_days, label)
            + failure_note(failures)
        )
    return True


@client.event
async def on_message(message):
    if message.author.bot:
        return
    await _handle_message(message)


# ===== ⑧-H スラッシュコマンド（/date・/add） =====
# 欄を選ぶだけで、文字のコマンドと同じことができる。
# 選ばれた内容を文字のコマンド（例 /n2026-10:2026-12d/sat/sun）に組み立て、
# 既存の _handle_message にそのまま渡す。判定や表示の処理を二重に持たないので、
# 文字のコマンドとスラッシュコマンドの結果は作りからして同じになる。
# 返信先だけを、スラッシュコマンドへの返信（interaction.followup）に差し替える。

class _SlashChannel:
    """_handle_message が使う channel の代わり。送信先をスラッシュコマンドの返信にする"""
    def __init__(self, interaction):
        self._interaction = interaction

    async def send(self, content=None, **kwargs):
        await self._interaction.followup.send(content, **kwargs)

    def typing(self):
        # 最初に defer(thinking=True) で「考え中…」を出しているので、ここでは何もしない
        return _NoTyping()


class _NoTyping:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class _SlashMessage:
    """_handle_message に渡す message の代わり（本文・送信者・返信先だけを持つ）"""
    def __init__(self, interaction, text):
        self.content = text
        self.author = interaction.user
        self.channel = _SlashChannel(interaction)


async def _run_as_text_command(interaction, text):
    """組み立てた文字のコマンドを、既存の処理で実行して返信する"""
    print(f"スラッシュコマンド: {text!r}")
    handled = await _handle_message(_SlashMessage(interaction, text))
    if not handled:
        # 組み立てた文字列は必ずどれかのコマンドに当たるはずなので、ここに来たら不具合
        await interaction.followup.send("⚠️ 指定を解釈できませんでした。")


def _today():
    return datetime.now(JST).date()


def _md(d):
    return f"{d.month}/{d.day}({WEEKDAY_NAMES[d.weekday()]})"


# --- 入力の正規化（候補から選んだ値も、手で打った値も、ここで同じ形にそろえる） ---
_SLASH_POINT_RE = re.compile(r"(\d{4})[-/](\d{1,2})(?:[./-](\d{1,2}))?")
_SLASH_DAY_RE = re.compile(r"(?:(\d{4})[-/])?(\d{1,2})[./-](\d{1,2})")
_SLASH_TIME_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?")


def _slash_point(s):
    """/date の開始・終了の月を「2026-10」（日まであれば「2026-10.15」）の形にする。読めなければ None"""
    m = _SLASH_POINT_RE.fullmatch(_nfkc(s).strip())
    if not m:
        return None
    y, mo, d = m.groups()
    return f"{int(y)}-{int(mo)}" + (f".{int(d)}" if d else "")


def _slash_day(s, today=None):
    """/add の日付・最終日を「2026-10.5」の形にする。読めなければ None。
    年を省いた「10/5」は、今日以降で最も近いその日付とみなす（存在しない日付は
    そのまま渡し、既存の処理に「存在しない日付です」と返させる）"""
    m = _SLASH_DAY_RE.fullmatch(_nfkc(s).strip())
    if not m:
        return None
    y, mo, d = m.groups()
    mo, d = int(mo), int(d)
    if y is None:
        today = today or _today()
        # 月日の並びで比べる。date() を作って比べると、今年に存在しない日付（2/29 など）で
        # 年の推定が働かず、過去の年のまま「存在しない日付」と返してしまう
        y = today.year + (1 if (mo, d) < (today.month, today.day) else 0)
    return f"{int(y)}-{mo}.{d}"


def _slash_time(s):
    """時刻を「14:00」の形にする。読めなければ None（範囲の検査は既存の処理に任せる）"""
    m = _SLASH_TIME_RE.fullmatch(_nfkc(s).strip())
    if not m:
        return None
    h, mi = m.groups()
    return f"{int(h)}:{int(mi or 0):02d}"


def _slash_weekdays(s):
    """/date の曜日（フォームでは「sat,sun」の形で渡す）を、文字のコマンドの末尾（/sat/sun）にする。
    「土日」「金,土」「平日」のような手打ちも受ける。読めない語はそのまま残し、
    既存の処理に「曜日の指定が分かりません」と返させる"""
    s = _nfkc(s or "").strip()
    if not s or s == "none" or s == "指定なし":
        return ""
    if s == "平日":
        s = "mon,tue,wed,thu,fri"
    tokens = []
    for tok in re.split(r"[/,、\s]+", s):
        if not tok:
            continue
        if len(tok) > 1 and all(c in WEEKDAY_NAMES for c in tok):
            tokens.extend(tok)                 # 「土日」→「土」「日」
        else:
            tokens.append(tok)
    return "".join("/" + tok for tok in tokens)


TIME_CHOICES = [
    app_commands.Choice(name="昼（10〜18時）", value="day"),
    app_commands.Choice(name="夜（18〜24時）", value="night"),
    app_commands.Choice(name="昼夜とも空いている", value="all"),
    app_commands.Choice(name="昼か夜が空いている", value="any"),
]
_TIME_TO_MODE = {"day": "", "night": "n", "all": "a", "any": "u"}

FORMAT_CHOICES = [
    app_commands.Choice(name="空いている日（通常）", value="normal"),
    app_commands.Choice(name="空いている日（伝助形式）", value="densuke"),
    app_commands.Choice(name="予定がある日", value="busy"),
]
_FORMAT_TO_SUFFIX = {"normal": "", "densuke": "d", "busy": "r"}


def build_date_command(start, end=None, time=None, weekday=None, fmt=None):
    """/date の入力から、文字のコマンド（例 /n2026-10:2026-12d/sat/sun）を組み立てる。
    入力に誤りがあれば (None, 理由) を返す"""
    s = _slash_point(start)
    if s is None:
        return None, "start は `2026-10` か `2026-10.15` の形で選ぶか入力してください"
    body = s
    if end:
        e = _slash_point(end)
        if e is None:
            return None, "end は `2026-12` か `2026-12.31` の形で選ぶか入力してください"
        body = f"{s}:{e}"
    mode = _TIME_TO_MODE.get(time or "day", "")
    suffix = _FORMAT_TO_SUFFIX.get(fmt or "normal", "")
    if suffix == "r" and mode in ("a", "u"):
        # 文字のコマンドでは無視される組み合わせ。黙って空き日を返すと誤解を招くので止める
        return None, "「予定がある日」は、時間帯が「昼」か「夜」のときだけ使えます"
    return f"/{mode}{body}{suffix}{_slash_weekdays(weekday)}", None


def build_add_command(date_, start=None, end=None, end_date=None, calendar=None):
    """/add の入力から、名前を除いた文字のコマンドを組み立てる。
    名前は文字列に入れない（「10-12 振り返り」のような名前の先頭を時刻と読まないため）。
    解釈が済んでから parsed["name"] に差し込む。誤りがあれば (None, 理由) を返す"""
    d = _slash_day(date_)
    if d is None:
        return None, "date は `10/5` か `2026-10.5` の形で選ぶか入力してください"
    when = d
    if end_date:
        e = _slash_day(end_date)
        if e is None:
            return None, "end_date は `10/7` か `2026-10.7` の形で選ぶか入力してください"
        when = f"{d}:{e}"
    parts = ["add"]
    if calendar:
        spec = calendar.strip()
        for left, right in (("[", "]"), ("【", "】"), ("［", "］")):
            if left not in spec and right not in spec:
                parts.append(f"{left}{spec}{right}")
                break
        else:
            return None, "カレンダー名に使えない記号が含まれています。番号で選んでください"
    parts.append(when)
    if start or end:
        if not (start and end):
            return None, "時刻を指定するときは開始と終了の両方の時を選んでください（どちらも空なら終日）"
        st, en = _slash_time(start), _slash_time(end)
        if st is None or en is None:
            return None, "時刻は `14:00` の形で選ぶか入力してください"
        parts.append(f"{st}-{en}")
    parts.append("_")                          # 名前の代わり（後で本当の名前に差し替える）
    return " ".join(parts), None


# /add の時刻は「時」と「分」を別の欄で選ぶ。
# メニューの選択肢は25件までなので、時と分を1つの欄にすると収まらない。
# 時は 0〜23時（利用者の指定）。パネルでは「なし（終日）」と合わせて25件で、上限ちょうど。
# 分は 00・15・30・45 の4つ（60通りは上限に収まらない）
HOUR_CHOICES = [app_commands.Choice(name=f"{h}時", value=h) for h in range(0, 24)]
MINUTE_CHOICES = [app_commands.Choice(name=f"{m:02d}分", value=m) for m in (0, 15, 30, 45)]


def _join_time(hour, minute, which):
    """時と分の選択から「14:30」を作る。時だけなら00分。分だけなら誤り。
    戻り値は (時刻の文字列 または None, 理由 または None)"""
    if hour is None:
        if minute is not None:
            return None, f"{which}の分だけが選ばれています。{which}の時も選んでください"
        return None, None
    return f"{hour}:{(minute or 0):02d}", None


# カレンダー名の候補。入力のたびに Google に問い合わせると遅いので、10分間覚えておく
_CALENDAR_CACHE_SEC = 600
_calendar_choice_cache = {"at": None, "items": []}


def _calendar_choices():
    now = time.monotonic()
    at = _calendar_choice_cache["at"]
    if at is not None and now - at < _CALENDAR_CACHE_SEC:
        return _calendar_choice_cache["items"]
    items = []
    for i, cid in enumerate(CALENDAR_IDS):
        try:
            name, _role = _calendar_meta(cid)
            label = _calendar_label(i, name)
        except Exception as e:
            print(f"カレンダー名の取得エラー: {cid} -> {e}")
            label = f"{i + 1}番目のカレンダー（読み取れません）"
        # 値は番号にする。名前に記号が入っていても、番号なら確実に指定できる
        items.append(app_commands.Choice(name=f"{i + 1}: {label}", value=str(i + 1)))
    _calendar_choice_cache.update(at=now, items=items)
    return items


# ===== /date・/add の設定パネル =====
# どちらも、送った人だけに見える設定パネルを開く。メニューで選ぶたびに上の要約がその場で書き換わる。
# 日付は「年」「月」「日」を別々のメニューで選ぶ。1つのメニューの選択肢は25件までなので、
# 日は「1〜15日」と「16〜31日」の2つのメニューに分け、どちらか一方で選ぶ。
# 入力フォーム（モーダル）は欄が5つまでで、開始と終了の年・月・日を置けない。従来のメッセージ（View）は
# 5行まで＝選択メニュー5つまでなので、部品を40個まで置ける新しい形式のメッセージ（LayoutView）にしている。
# 中身は文字のコマンドに組み立てて、既存の処理（_handle_message・parse_add_command など）に渡す。

PANEL_TIMEOUT_SEC = 840                # 14分。応答の書き換えに使う鍵が15分で切れるので、その手前で閉じる
_NONE = "__none__"                     # 選択肢「なし」「指定なし」の値
_DAY_HALVES = ((1, 15), (16, 31))      # 日のメニューの分け方（それぞれ25件以内）
_WEEKDAY_VALUES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _date_error(y, m, d):
    """年・月・日が実在する日付なら None、しなければ理由（既存の add と同じ言い回し）"""
    try:
        date(y, m, d)
        return None
    except ValueError:
        return f"{y}-{m}.{d} は存在しない日付です"


def _closed_view(text):
    """パネルを閉じたあとの表示（操作できる部品は無い）"""
    view = discord.ui.LayoutView()
    view.add_item(discord.ui.Container(discord.ui.TextDisplay(text)))
    return view


class _Panel(discord.ui.LayoutView):
    """/date と /add のパネルに共通の部分。選ばれた内容（状態）を持ち、操作のたびに作り直して表示を更新する"""

    def __init__(self, origin, today=None):
        super().__init__(timeout=PANEL_TIMEOUT_SEC)
        self.origin = origin                   # 開いたときのやり取り。閉じるときにパネルを書き換えるのに使う
        self.owner_id = origin.user.id
        self.today = today or _today()
        self.note = None                       # 一度だけ要約の下に出す一言

    # --- 部品を作る ---
    def _select(self, options, placeholder, on_pick, disabled=False, multi=False):
        """選択メニュー。multi なら何件でも（0件も）選べ、on_pick には選んだ値の一覧を渡す"""
        # required=False：discord.py は文字の選択メニューに既定で「必須」の印を付けて送る。
        # 本来フォーム用の印だが、パネルの空のメニュー（日の前半・後半の片方など）まで必須と扱われ、
        # 実行のボタンが押せなくなる症状が出たため外す
        sel = discord.ui.Select(options=options, placeholder=placeholder,
                                min_values=0 if multi else 1, max_values=len(options) if multi else 1,
                                disabled=disabled, required=False)

        async def callback(interaction):
            self._log(f"{placeholder} = {(','.join(sel.values) or '（選択なし）') if multi else sel.values[0]}")
            await on_pick(interaction, list(sel.values) if multi else sel.values[0])
        sel.callback = callback
        return sel

    def _button(self, label, style, on_press, disabled=False):
        btn = discord.ui.Button(label=label, style=style, disabled=disabled)

        async def callback(interaction):
            self._log(f"ボタン「{label}」")
            await on_press(interaction)
        btn.callback = callback
        return btn

    def _log(self, what):
        """操作がボットに届いたことをログに残す（コマンド名と、選んだ項目・値だけ。利用者名や予定名は残さない）"""
        print(f"パネル操作 /{self.command}: {what}")

    def _date_rows(self, y, m, d, on_pick, none_label=None, day_none_label=None, multi_days=None):
        """年・月・日（1〜15）・日（16〜31）の4つのメニューを、1つずつ行に入れて返す。
        on_pick(interaction, "y"|"m"|"d", 値)。none_label があれば年に「なし」を置き、なしの間は月・日を押せない。
        day_none_label があれば日に「指定なし」を置く（/date で月単位に調べるため）。
        multi_days（選んだ日の集合）を渡すと、日のメニューは複数選べる形になり、
        on_pick(interaction, "days", (その組の最初の日, 最後の日, 選んだ値の一覧)) を呼ぶ"""
        years = [self.today.year, self.today.year + 1]
        year_opts = []
        if none_label:
            year_opts.append(discord.SelectOption(label=none_label, value=_NONE, default=y is None))
        year_opts += [discord.SelectOption(label=f"{yy}年", value=str(yy), default=(yy == y)) for yy in years]
        rows = [discord.ui.ActionRow(self._select(year_opts, "年", lambda i, v: on_pick(i, "y", v)))]
        off = y is None
        rows.append(discord.ui.ActionRow(self._select(
            [discord.SelectOption(label=f"{mm}月", value=str(mm), default=(mm == m)) for mm in range(1, 13)],
            "月", lambda i, v: on_pick(i, "m", v), disabled=off)))
        # 選んだ年月に実在する日だけを並べ、曜日を添える（月を変えて日が無くなったときは要約で知らせる）
        last = _calendar.monthrange(y, m)[1] if (y and m) else 31
        for n, (lo, hi) in enumerate(_DAY_HALVES):
            opts = []
            if day_none_label:
                opts.append(discord.SelectOption(label=day_none_label, value=_NONE,
                                                 default=(d is None and n == 0)))
            for dd in range(lo, min(hi, last) + 1):
                wd = f"({WEEKDAY_NAMES[date(y, m, dd).weekday()]})" if (y and m) else ""
                on = (date(y, m, dd) in multi_days) if multi_days is not None else (dd == d)
                opts.append(discord.SelectOption(label=f"{dd}日{wd}", value=str(dd), default=on))
            if multi_days is not None:
                pick = (lambda lo_, hi_: lambda i, v: on_pick(i, "days", (lo_, hi_, v)))(lo, hi)
                sel = self._select(opts, f"日（{lo}〜{hi}）", pick, disabled=off, multi=True)
            else:
                sel = self._select(opts, f"日（{lo}〜{hi}）", lambda i, v: on_pick(i, "d", v), disabled=off)
            rows.append(discord.ui.ActionRow(sel))
        return rows

    # --- 共通の操作 ---
    async def _update(self, interaction):
        self.render()
        head, ok = self.summary()
        # 注意の文言は日付や時刻だけで、カレンダー名や予定名は含まない
        self._log("→ 実行できる状態" if ok else f"→ 注意を表示中（実行のボタンは押せない）：{head.splitlines()[0]}")
        await interaction.response.edit_message(view=self)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:   # 開いた本人だけが操作できる
            self._log("開いた本人以外の操作のため受け付けませんでした")
            return False
        return True

    async def _press_cancel(self, interaction):
        self.stop()
        revoke_activity_ticket(self)           # カレンダー画面からも追加できなくする
        await interaction.response.edit_message(view=_closed_view(self.cancel_text))

    async def finish(self, text):
        """処理が済んだら、パネルを閉じた表示に書き換える"""
        self.stop()
        revoke_activity_ticket(self)
        try:
            await self.origin.edit_original_response(view=_closed_view(text))
        except discord.HTTPException as e:
            print(f"設定パネルを閉じられませんでした: {e}")

    async def on_timeout(self):
        await self.finish(f"{PANEL_TIMEOUT_SEC // 60}分操作が無かったので閉じました。"
                          f"もう一度 /{self.command} を送ってください。")


# ----- /date：空いている日を調べる -----
_TIME_LABELS = {c.value: c.name for c in TIME_CHOICES}
_FORMAT_LABELS = {c.value: c.name for c in FORMAT_CHOICES}


class DatePanel(_Panel):
    command = "date"
    cancel_text = "空き日の検索を取り消しました。"

    def __init__(self, origin, today=None):
        super().__init__(origin, today)
        self.sy, self.sm, self.sd = self.today.year, self.today.month, None   # 最初は今月（1日から）
        self.ey = self.em = self.ed = None                                     # 終了なし＝開始の月だけ
        self.time = "day"
        self.weekdays = set()
        self.fmt = "normal"
        self.render()

    @staticmethod
    def _point(y, m, d):
        return f"{y}-{m}" + (f".{d}" if d else "")

    def check(self):
        """(文字のコマンド, 開始日, 終了日, 誤り)。誤りが無ければ 誤り は None"""
        for y, m, d in ((self.sy, self.sm, self.sd), (self.ey, self.em, self.ed)):
            if y and d:
                error = _date_error(y, m, d)
                if error:
                    return None, None, None, error
        start = self._point(self.sy, self.sm, self.sd)
        end = self._point(self.ey, self.em, self.ed) if self.ey else None
        picked = [v for v in _WEEKDAY_VALUES if v in self.weekdays]   # 月→日の順にそろえる
        if len(picked) == len(_WEEKDAY_VALUES):
            picked = []                        # 7曜日すべて＝指定なし（既存の処理と同じ扱い。コマンドも短くなる）
        text, error = build_date_command(start, end, self.time, ",".join(picked) or None, self.fmt)
        if error:
            return None, None, None, error
        start_date, end_date, error = check_range(f"{start}:{end}" if end else start)
        return text, start_date, end_date, error

    def summary(self):
        text, start_date, end_date, error = self.check()
        if error:
            head = f"⚠️ {error}"
        else:
            days = "すべての曜日" if len(self.weekdays) in (0, 7) else weekday_label(
                {_WEEKDAY_VALUES.index(v) for v in self.weekdays})
            head = (f"📅 {start_date.year}/{_md(start_date)}〜{end_date.year}/{_md(end_date)}\n"
                    f"{_TIME_LABELS[self.time]}・{days}・{_FORMAT_LABELS[self.fmt]}\n"
                    f"文字のコマンドでは `{text}`")
        if self.note:
            head += f"\n{self.note}"
        return head, error is None

    def render(self):
        self.clear_items()
        head, ok = self.summary()
        self.note = None
        week1 = [self._weekday_button(i) for i in range(5)]
        week2 = [self._weekday_button(i) for i in range(5, 7)]
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"### 空いている日を調べる\n{head}"),
            discord.ui.TextDisplay("**開始**（日が「指定なし」なら月の1日から）"),
            *self._date_rows(self.sy, self.sm, self.sd, self._pick_start, day_none_label="指定なし（1日から）"),
            discord.ui.TextDisplay("**終了**（日が「指定なし」なら月末まで）"),
            *self._date_rows(self.ey, self.em, self.ed, self._pick_end,
                             none_label="なし（開始の月・日だけ）", day_none_label="指定なし（月末まで）"),
            discord.ui.ActionRow(self._select(
                [discord.SelectOption(label=f"時間帯：{c.name}", value=c.value, default=(c.value == self.time))
                 for c in TIME_CHOICES], "時間帯", self._pick_time)),
            discord.ui.TextDisplay("**曜日**（押すと緑になる。選ばなければすべての曜日）"),
            discord.ui.ActionRow(*week1),
            discord.ui.ActionRow(*week2),
            discord.ui.ActionRow(self._select(
                [discord.SelectOption(label=f"出し方：{c.name}", value=c.value, default=(c.value == self.fmt))
                 for c in FORMAT_CHOICES], "出し方", self._pick_fmt)),
            discord.ui.ActionRow(
                self._button("調べる", discord.ButtonStyle.primary, self._press_run, disabled=not ok),
                self._button("やめる", discord.ButtonStyle.danger, self._press_cancel),
            ),
        ))

    def _weekday_button(self, i):
        value = _WEEKDAY_VALUES[i]
        on = value in self.weekdays
        style = discord.ButtonStyle.success if on else discord.ButtonStyle.secondary

        async def toggle(interaction):
            self.weekdays.symmetric_difference_update({value})   # 押すたびに選ぶ／外すが切り替わる
            await self._update(interaction)
        return self._button(WEEKDAY_NAMES[i], style, toggle)   # 「曜」は付けない

    async def _pick_start(self, interaction, part, value):
        v = None if value == _NONE else int(value)
        setattr(self, {"y": "sy", "m": "sm", "d": "sd"}[part], v)
        await self._update(interaction)

    async def _pick_end(self, interaction, part, value):
        v = None if value == _NONE else int(value)
        if part == "y":
            if v is None:
                self.ey = self.em = self.ed = None
            else:
                if self.ey is None:            # 終了を付けたときは、開始と同じ月の月末から選び直す
                    self.em, self.ed = self.sm, None
                self.ey = v
        else:
            setattr(self, {"m": "em", "d": "ed"}[part], v)
        await self._update(interaction)

    async def _pick_time(self, interaction, value):
        self.time = value
        await self._update(interaction)

    async def _pick_fmt(self, interaction, value):
        self.fmt = value
        await self._update(interaction)

    async def _press_run(self, interaction):
        await interaction.response.defer(thinking=True)   # 結果はチャンネルに出す（文字のコマンドと同じ）
        try:
            text, _s, _e, error = self.check()
            if error:
                await interaction.followup.send(f"⚠️ {error}")
                return
            await _run_as_text_command(interaction, text)
            await self.finish("このパネルは閉じました。結果はチャンネルに表示しています。")
        except Exception as e:
            # 返信しないと「考え中…」のまま止まるので、失敗もその場で返す
            print(f"/date のエラー: {e!r}")
            await interaction.followup.send(f"⚠️ 処理中にエラーが発生しました（{type(e).__name__}）")


@tree.command(name="date", description="空いている日を調べる（パネルで年・月・日・時間帯・曜日を選ぶ）")
async def slash_date(interaction: discord.Interaction):
    panel = DatePanel(interaction)
    await interaction.response.send_message(view=panel, ephemeral=True)   # 送った人だけに見える
    panel._log("パネルを開きました")


# ----- /add：予定を追加する -----
class AddPanel(_Panel):
    command = "add"
    cancel_text = "予定の追加を取り消しました。"

    def __init__(self, origin, calendars, today=None):
        super().__init__(origin, today)
        t = self.today
        self.y, self.m = t.year, t.month       # 日のメニューに出している年月
        self.days = {t}                        # 選んだ日（複数可。月を切り替えても残る）。最初は今日・終日・先頭のカレンダー
        self.ey = self.em = self.ed = None                 # 最終日なし＝1日だけ
        self.start_hour = self.start_minute = None
        self.end_hour = self.end_minute = None
        self.calendars = calendars             # 開いた時点の候補を使い続ける（操作のたびに Google に問い合わせない）
        self.calendar = "1"
        self.render()

    # --- 今の選択を文字のコマンドにして確かめる ---
    def check(self):
        """選んだ日ごとに文字のコマンドを組み立て、既存の parse_add_command に通す。
        戻り値は (解釈結果の一覧 または None, 組み立てた文字列の一覧 または None, 誤り または None)。
        一覧は日付の順。日ごとに別の予定になる（同じ名前・同じ時刻）"""
        days = sorted(self.days)
        error = end_date = None
        if not days:
            error = "日付を選んでください"
        elif len(days) > ADD_MAX_DAYS:
            error = f"一度に追加できるのは{ADD_MAX_DAYS}日までです（いま{len(days)}日選んでいます）"
        elif self.ey:
            if len(days) > 1:
                error = "複数の日を選んだときは最終日を使えません（最終日の年を「なし」にしてください）"
            elif self.ed is None:
                error = "最終日の日を選んでください（1日だけなら最終日の年を「なし」に）"
            else:
                error = _date_error(self.ey, self.em, self.ed)
                end_date = f"{self.ey}-{self.em}.{self.ed}"
        start = end = None
        if not error:
            start, error = _join_time(self.start_hour, self.start_minute, "開始")
        if not error:
            end, error = _join_time(self.end_hour, self.end_minute, "終了")
        texts = []
        for d in days if not error else []:
            text, error = build_add_command(f"{d.year}-{d.month}.{d.day}", start, end, end_date, self.calendar)
            if error:
                break
            texts.append(text)
        if error:
            return None, None, error
        try:
            return [parse_add_command(t) for t in texts], texts, None
        except AddError as e:
            return None, texts, str(e)

    def summary(self):
        parsed, _texts, error = self.check()
        cal = next((c.name.split(": ", 1)[-1] for c in self.calendars if c.value == self.calendar),
                   f"{self.calendar}番目のカレンダー")
        if error:
            head = f"⚠️ {error}"
        elif len(parsed) == 1:
            head = f"📅 {format_added_when(parsed[0])} → {cal}"
        else:                                  # 日ごとに1件ずつ。何件できるかと、それぞれの日時を並べる
            head = f"📅 {len(parsed)}件（1日ずつ別の予定） → {cal}\n" + "\n".join(format_added_when(p) for p in parsed)
        if self.note:
            head += f"\n{self.note}"
        return head, error is None

    def _hour_select(self, current, placeholder, on_pick):
        opts = [discord.SelectOption(label="なし（終日）", value=_NONE, default=current is None)]
        opts += [discord.SelectOption(label=c.name, value=str(c.value), default=(c.value == current))
                 for c in HOUR_CHOICES]
        return self._select(opts, placeholder, on_pick)

    def _minute_select(self, hour, current, placeholder, on_pick):
        opts = [discord.SelectOption(label=c.name, value=str(c.value), default=(c.value == (current or 0)))
                for c in MINUTE_CHOICES]
        return self._select(opts, placeholder, on_pick, disabled=hour is None)   # 時を選ぶまでは分を選べない

    def render(self):
        self.clear_items()
        head, ok = self.summary()
        self.note = None                       # 一言は一度出したら消す
        all_day = self.start_hour is None and self.end_hour is None
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"### 予定を追加\n{head}"),
            *([discord.ui.ActionRow(self._button(
                "📅 カレンダーで選ぶ（日付と時間をタップ）", discord.ButtonStyle.success, self._press_calendar))]
              if ACTIVITY_ENABLED else []),
            discord.ui.TextDisplay(f"**日付**（日は何日でも選べる。月を切り替えても選んだ日は残る。{ADD_MAX_DAYS}日まで）"),
            *self._date_rows(self.y, self.m, None, self._pick_date, multi_days=self.days),
            discord.ui.TextDisplay("**最終日**（1日だけ選んで、連続した終日予定にするときだけ）"),
            *self._date_rows(self.ey, self.em, self.ed, self._pick_end_date, none_label="なし（1日だけ）"),
            discord.ui.TextDisplay("**開始**"),
            discord.ui.ActionRow(self._hour_select(self.start_hour, "開始の時（なしなら終日）", self._pick_start_hour)),
            discord.ui.ActionRow(self._minute_select(self.start_hour, self.start_minute, "開始の分", self._pick_start_minute)),
            discord.ui.TextDisplay("**終了**"),
            discord.ui.ActionRow(self._hour_select(self.end_hour, "終了の時（開始より前なら翌日まで）", self._pick_end_hour)),
            discord.ui.ActionRow(self._minute_select(self.end_hour, self.end_minute, "終了の分", self._pick_end_minute)),
            discord.ui.TextDisplay("**追加先**"),
            discord.ui.ActionRow(self._select(
                [discord.SelectOption(label=c.name, value=c.value, default=(c.value == self.calendar))
                 for c in self.calendars][:25], "追加先のカレンダー", self._pick_calendar)),
            discord.ui.ActionRow(
                self._button("終日にする", discord.ButtonStyle.secondary, self._press_all_day, disabled=all_day),
                self._button("名前を入れて追加", discord.ButtonStyle.primary, self._press_add, disabled=not ok),
                self._button("やめる", discord.ButtonStyle.danger, self._press_cancel),
            ),
        ))

    # --- 操作 ---
    async def _pick_date(self, interaction, part, value):
        if part == "days":                     # 日のメニュー（前半か後半）で選び直した
            lo, hi, values = value
            # 今出している年月のその組（1〜15日か16〜31日）の分だけを、選び直した内容に入れ替える
            self.days = {d for d in self.days
                         if not (d.year == self.y and d.month == self.m and lo <= d.day <= hi)}
            self.days |= {date(self.y, self.m, int(v)) for v in values}
        else:
            setattr(self, part, int(value))    # part は "y"・"m"。選んだ日はそのまま残す
        await self._update(interaction)

    async def _pick_end_date(self, interaction, part, value):
        v = None if value == _NONE else int(value)
        if part == "y":
            if v is None:
                self.ey = self.em = self.ed = None
            else:
                if self.ey is None:            # 最終日を付けたときは、日付と同じ月で日を選んでもらう
                    first = min(self.days) if self.days else self.today
                    self.em, self.ed = first.month, None
                self.ey = v
        else:
            setattr(self, {"m": "em", "d": "ed"}[part], v)
        await self._update(interaction)

    async def _pick_start_hour(self, interaction, value):
        self.start_hour = None if value == _NONE else int(value)
        if self.start_hour is None:
            self.start_minute = None
        await self._update(interaction)

    async def _pick_start_minute(self, interaction, value):
        self.start_minute = int(value)
        await self._update(interaction)

    async def _pick_end_hour(self, interaction, value):
        self.end_hour = None if value == _NONE else int(value)
        if self.end_hour is None:
            self.end_minute = None
        await self._update(interaction)

    async def _pick_end_minute(self, interaction, value):
        self.end_minute = int(value)
        await self._update(interaction)

    async def _pick_calendar(self, interaction, value):
        self.calendar = value
        await self._update(interaction)

    async def _press_all_day(self, interaction):
        self.start_hour = self.start_minute = self.end_hour = self.end_minute = None
        await self._update(interaction)

    async def _press_add(self, interaction):
        await interaction.response.send_modal(NameModal(self))

    async def _press_calendar(self, interaction):
        """カレンダー画面（アクティビティ）を開く。押した本人とチャンネルを14分（パネルの時間切れまで）だけ記録してから開く"""
        issue_activity_ticket(self.owner_id, interaction.channel, self, self.calendars)
        try:
            await interaction.response.launch_activity()
        except discord.HTTPException as e:
            revoke_activity_ticket(self)
            print(f"カレンダー画面を開けませんでした: {e}")
            await interaction.response.send_message(
                "⚠️ カレンダー画面を開けませんでした。Developer Portal でアクティビティが有効になっているか確認してください"
                "（README 16章）。このパネルからはそのまま追加できます", ephemeral=True)


class NameModal(discord.ui.Modal):
    """「名前を入れて追加」で開く入力欄。送信すると予定を追加し、結果をチャンネルに出す"""

    def __init__(self, panel):
        super().__init__(title="予定の名前")
        self.panel = panel
        self.name = discord.ui.TextInput(placeholder="例：打ち合わせ", max_length=200)
        self.add_item(discord.ui.Label(text="予定の名前", component=self.name))

    async def on_submit(self, interaction):
        await interaction.response.defer(thinking=True)   # 結果はチャンネルに出す（文字のコマンドと同じ）
        try:
            name = self.name.value.strip()
            parsed, texts, error = self.panel.check()
            if not name:
                await interaction.followup.send("⚠️ 予定の名前を入れてください")
                return
            if error:
                await interaction.followup.send(f"⚠️ {error}")
                return
            for p in parsed:
                p["name"] = name               # 解釈が済んでから本当の名前を入れる
            print(f"スラッシュコマンド: {texts!r}（予定の名前はログに残さない）")
            if len(parsed) == 1:               # 1日だけなら、文字の add と同じ処理・同じ返信
                await handle_add_command(_SlashMessage(interaction, texts[0]), parsed[0])
            else:
                await handle_add_many(_SlashMessage(interaction, texts[0]), parsed)
            await self.panel.finish("このパネルは閉じました。結果はチャンネルに表示しています。")
        except Exception as e:
            print(f"/add のエラー: {e!r}")
            await interaction.followup.send(f"⚠️ 処理中にエラーが発生しました（{type(e).__name__}）")


@tree.command(name="add", description="Google カレンダーに予定を追加する（設定パネルが開く）")
async def slash_add(interaction: discord.Interaction):
    # Google の窓口（service）は複数のスレッドから同時に使えないので、ほかの処理と同じくここで直接呼ぶ。
    # 結果は10分間覚えておくので、問い合わせが起きるのは久しぶりに開いたときだけ
    calendars = _calendar_choices()
    panel = AddPanel(interaction, calendars)
    await interaction.response.send_message(view=panel, ephemeral=True)   # 送った人だけに見える
    panel._log("パネルを開きました")


# ===== /purge：このチャンネルのメッセージをまとめて削除する =====
# 新しい方から、指定した数だけ削除する（誰のメッセージかは問わない）。
# 自動表示のメッセージ（state["messages"]）は必ず残す。消すと以後の書き換えが毎回失敗し、自動表示が復活しないため。
# ピン留めも残す。残したものは数に含めず、その分さかのぼって指定の数を消す。
# Discord は「14日以内のメッセージを100件まで」しか一度に消せないので、それより古いものは1件ずつ消す。
# 使えるのは「メッセージの管理」の権限を持つ人だけ（ほかの人には一覧にも出ない）。

PURGE_MAX = 100                        # 一度に消せる数の上限（利用者の指定。一括削除の上限とも同じ）
_PURGE_SCAN_MAX = 1000                 # 残すメッセージを飛ばしながら、さかのぼる数の上限
# 一括削除できるのは14日以内のメッセージだけ。境目ちょうどのものが送るまでに14日を過ぎて
# 全体が失敗しないよう、1時間の余裕をとり、それより古いものは1件ずつ消す
_BULK_DELETE_AGE = timedelta(days=14) - timedelta(hours=1)


def _purge_keep_reason(message):
    """残すメッセージなら理由、消してよいなら None"""
    if any(message.id == m.id for m in state["messages"]):
        return "自動表示"
    if message.pinned:
        return "ピン留め"
    if not message.type.is_deletable():
        return "削除できない種類"
    return None


async def purge_messages(channel, count, now=None):
    """新しい方から、残すべきものを飛ばして count 件削除する。戻り値は (削除した数, {残した理由: 件数})"""
    now = now or discord.utils.utcnow()
    targets, kept = [], {}
    async for message in channel.history(limit=_PURGE_SCAN_MAX):
        reason = _purge_keep_reason(message)
        if reason:
            kept[reason] = kept.get(reason, 0) + 1
            continue
        targets.append(message)
        if len(targets) >= count:
            break
    recent = [m for m in targets if now - m.created_at < _BULK_DELETE_AGE]
    old = [m for m in targets if now - m.created_at >= _BULK_DELETE_AGE]
    for i in range(0, len(recent), 100):
        chunk = recent[i:i + 100]
        if len(chunk) == 1:
            await chunk[0].delete()            # 一括削除は2件以上から
        else:
            await channel.delete_messages(chunk)
    for message in old:
        await message.delete()                 # 待ち時間の調整は discord.py が行う
    return len(targets), kept


PURGE_PERMISSION_HELP = ("ボットに「メッセージの管理」と「メッセージ履歴を読む」の権限がありません。\n"
                         "サーバー設定 →「ロール」→ ボットのロールで、この2つを有効にしてください"
                         "（チャンネルごとの権限で外されていないかも確認してください。README 3-2）")


@tree.command(name="purge", description="このチャンネルの新しいメッセージを指定した数だけ削除する（自動表示とピン留めは残す）")
@app_commands.describe(count=f"削除する数（1〜{PURGE_MAX}）")
@app_commands.guild_only()
@app_commands.default_permissions(manage_messages=True)       # 既定では「メッセージの管理」を持つ人にだけ一覧に出る
@app_commands.checks.has_permissions(manage_messages=True)    # サーバー設定で広げられても、実行時に本人の権限を確かめる
async def slash_purge(interaction: discord.Interaction, count: app_commands.Range[int, 1, PURGE_MAX]):
    await interaction.response.defer(ephemeral=True, thinking=True)   # 消す数が多いと時間がかかる
    channel = interaction.channel
    try:
        deleted, kept = await purge_messages(channel, count)
    except discord.Forbidden:
        await interaction.followup.send(f"⚠️ {PURGE_PERMISSION_HELP}", ephemeral=True)
        return
    except discord.HTTPException as e:
        print(f"/purge のエラー（チャンネル {channel.id}）: {e}")
        await interaction.followup.send(
            f"⚠️ 削除の途中で失敗しました（{type(e).__name__}）。一部だけ消えている可能性があります", ephemeral=True)
        return
    print(f"/purge: {deleted}件削除（チャンネル {channel.id}）")   # 中身や送信者は残さない
    text = f"🗑️ {deleted}件削除しました"
    if kept:
        text += "（" + "・".join(f"{k}{v}件" for k, v in kept.items()) + "はそのまま）"
    if deleted < count:
        text += "\n指定より少ないのは、さかのぼれる範囲に消せるメッセージがそれ以上無かったためです"
    await interaction.followup.send(text, ephemeral=True)


@slash_purge.error
async def slash_purge_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        text = "⚠️ /purge は「メッセージの管理」の権限を持つ人だけが使えます"
    else:
        print(f"/purge のエラー: {error!r}")
        text = f"⚠️ 処理中にエラーが発生しました（{type(error).__name__}）"
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


_slash_synced = False
_sync_task = None


ENTRY_POINT_COMMAND_TYPE = 4           # PRIMARY_ENTRY_POINT（アクティビティを開くコマンド）
# 一括登録に含め直す項目。id などの読み取り専用の項目は送らない（名前と種類で同じコマンドとみなされる）
_ENTRY_POINT_FIELDS = ("type", "name", "description", "handler", "integration_types", "contexts",
                       "name_localizations", "description_localizations", "default_member_permissions", "nsfw")


async def _entry_point_payloads():
    """Discord に登録済みの Entry Point コマンドを、一括登録に含められる形にして返す（無ければ空）"""
    existing = await client.http.get_global_commands(client.application_id)
    return [{k: c[k] for k in _ENTRY_POINT_FIELDS if c.get(k) is not None}
            for c in existing if c.get("type") == ENTRY_POINT_COMMAND_TYPE]


async def sync_slash_commands():
    """スラッシュコマンドを Discord に登録する（起動ごとに1回だけ）。
    SLASH_COMMANDS=off なら、空の一覧を登録して既存の登録を消す"""
    global _slash_synced
    if _slash_synced:
        return
    _slash_synced = True
    try:
        if not SLASH_COMMANDS_ENABLED:
            tree.clear_commands(guild=None)
        # tree.sync() は使わない。一覧を丸ごと置き換えるため、アクティビティを有効にすると Discord が自動で作る
        # Entry Point コマンド（「Launch」、種類4）が抜け、Discord に拒否される（error code 50240）。
        # discord.py 2.7 はこの種類を扱えないので、登録済みのものを読み取り、そのまま一緒に登録し直す
        payload = [c.to_dict(tree) for c in tree.get_commands()]
        entry_points = await _entry_point_payloads()
        data = await client.http.bulk_upsert_global_commands(client.application_id, payload + entry_points)
        names = [d["name"] for d in data if d.get("type") != ENTRY_POINT_COMMAND_TYPE]
        kept = "（アクティビティの Entry Point コマンドはそのまま残しました）" if entry_points else ""
        if SLASH_COMMANDS_ENABLED:
            print("スラッシュコマンドを登録しました: " + ", ".join("/" + n for n in names) + kept)
        else:
            print("SLASH_COMMANDS=off のため、スラッシュコマンドの登録を消しました" + kept)
    except Exception as e:
        # 登録に失敗しても、文字のコマンドと自動表示はそのまま動く
        print(f"スラッシュコマンドの登録に失敗しました: {e!r}"
              "（文字のコマンドはそのまま使えます）")


# ===== ⑧-F 動作状況のまとめ（ping の返信と Web の状態表示で共用） =====
def _fmt_time(dt):
    return dt.strftime("%m/%d %H:%M") if dt else "まだありません"


def status_report():
    """いまの動作状況を (水準, 行のリスト) で返す。

    水準は "ok"（正常）、"warn"（動いているが一部に問題）、
    "error"（ボットが応答できない）のいずれか。
    Webサーバーの別スレッドからも呼ばれるので、読むだけで何も書き換えない。
    ID やカレンダー名など、公開の URL に出せないものは含めない。
    """
    errors = []
    warnings = []
    lines = []

    # --- Discord との接続 ---
    if client.is_closed():
        errors.append("Discord との接続が切れています")
    elif not client.is_ready():
        errors.append("Discord にまだ接続できていません（起動中か、締め出されています）")
    else:
        latency = client.latency
        if math.isfinite(latency):
            lines.append(f"Discord: 接続中（応答 {latency * 1000:.0f}ms）")
        else:
            lines.append("Discord: 接続中")

    # --- イベントループが固まっていないか ---
    tick = health["loop_tick"]
    if tick is not None:
        stalled = time.monotonic() - tick
        if stalled > LOOP_STALL_SEC:
            errors.append(f"ボットの処理が {stalled:.0f} 秒止まっています（固まっています）")

    # --- カレンダーの読み取り ---
    if health["calendar_error"]:
        at, what = health["calendar_error"]
        warnings.append(f"カレンダー: {what}（{_fmt_time(at)}）")
    elif health["calendar_ok_at"] is None:
        # まだ一度も読んでいないのに「正常」と出すと、分かっていないことが正常に見える
        lines.append("カレンダー: まだ読み取っていません")
    else:
        lines.append(f"カレンダー: 前回の読み取りは正常（{_fmt_time(health['calendar_ok_at'])}）")

    if errors:
        level, head = "error", "❌ ボットが応答できない状態です"
    elif warnings:
        level, head = "warn", "⚠️ 動いていますが、問題があります"
    else:
        level, head = "ok", "✅ 正常に動いています"

    body = [head]
    body += [f"❌ {x}" for x in errors]
    body += [f"⚠️ {x}" for x in warnings]
    body += lines
    body.append(f"起動: {_fmt_time(health['started_at'])}")
    return level, body


# ===== ⑨-B アクティビティ（Discord の中で開くカレンダー画面） =====
# /add のパネルの「📅 カレンダーで選ぶ」を押すと、Discord の中（スマホでも）でカレンダーの画面が開き、
# 日をタップし、時間の枠を「開始→終了」の順にタップして予定を追加できる。
# 画面のファイルは activity/ にあり、このファイルの Web サーバーが /activity/ の下で配る。
# Discord の Developer Portal で URL Mappings を「/ → <Render のホスト>/activity」にしておく。
#
# 安全のための決まり：
#   ・開いた人は Discord のログイン確認（OAuth2）で確かめる。画面から届く情報はそのまま信じない
#   ・追加できるのは、/add のパネルでボタンを押した本人だけ。押したときに「誰が・どのチャンネルで」を
#     14分（パネルの時間切れまで）だけ記録し（チケット）、ログイン確認の結果と一致したときだけ受け付ける
#   ・画面とのやり取りに使う合言葉（セッション）は推測できない乱数で、メモリにだけ置き、ログに書かない。
#     追加に使えるのは1回だけ
#   ・追加の中身は、パネルと同じく文字のコマンドに組み立てて既存の処理に通す（判定を二重に持たない）
#   ・Google と Discord への問い合わせは、ボット本体の処理の流れ（_bot_loop）で行う。
#     Web サーバーは別のスレッドで動くが、Google の窓口（service）はスレッド間で共有できないため
ACTIVITY_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "").strip()   # 値は表示もログもしない
ACTIVITY_ENABLED = bool(ACTIVITY_CLIENT_SECRET)
ACTIVITY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "activity")
# ボタンを押してから使える時間。利用者の指定は15分だが、/add のパネルは最後の操作から14分で閉じ、
# 閉じるとチケットも無効にするので、パネルと同じ14分にそろえる（パネルの14分は Discord の応答の鍵の15分から決まる）
ACTIVITY_TICKET_SEC = PANEL_TIMEOUT_SEC
_ACTIVITY_MAX_BODY = 16 * 1024         # 画面から受け取る中身の大きさの上限
_ACTIVITY_MAX_SPAN_DAYS = 43           # 1回に読む予定の期間（月表示の6週＋余り）
_ACTIVITY_FILES = {                    # 配るファイル（これ以外は配らない）
    "app.js": "text/javascript; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    "discord-sdk.js": "text/javascript; charset=utf-8",
}
_DISCORD_API = "https://discord.com/api"
_activity_lock = threading.Lock()
_activity_tickets = {}                 # 利用者ID → {"expires", "channel", "panel", "calendars"}
_activity_sessions = {}                # 合言葉 → {"user_id", "expires", "used"}
_bot_loop = None                       # ボット本体の処理の流れ（on_ready で記録）


class ActivityError(Exception):
    """画面に返す、利用者向けの理由"""
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _activity_log(what):
    """アクティビティの動きをログに残す（合言葉・ログイン確認の値・予定の名前は残さない）"""
    print(f"アクティビティ: {what}")


def issue_activity_ticket(user_id, channel, panel, calendars, now=None):
    """パネルのボタンが押されたときに呼ぶ。同じ人の前のチケットは置き換える"""
    now = now if now is not None else time.monotonic()
    with _activity_lock:
        _activity_tickets[user_id] = {"expires": now + ACTIVITY_TICKET_SEC, "channel": channel,
                                      "panel": panel, "calendars": calendars}


def revoke_activity_ticket(panel):
    """パネルが閉じたら、そのパネルのチケットと合言葉を使えなくする"""
    with _activity_lock:
        for uid, t in list(_activity_tickets.items()):
            if t["panel"] is panel:
                del _activity_tickets[uid]
                for k, s in list(_activity_sessions.items()):
                    if s["user_id"] == uid:
                        del _activity_sessions[k]


def _valid_ticket(user_id, now):
    t = _activity_tickets.get(user_id)
    if t is None or t["expires"] <= now:
        return None
    return t


def _session_for(token, now):
    """合言葉から、まだ使えるセッションと、その人のチケットを返す。使えなければ ActivityError"""
    if not isinstance(token, str) or not token:
        raise ActivityError("画面を開き直してください（確認の情報がありません）", 401)
    s = _activity_sessions.get(token)
    if s is None or s["expires"] <= now:
        raise ActivityError("時間が経ったため使えなくなりました。Discord の /add のパネルから開き直してください", 401)
    if s["used"]:
        raise ActivityError("この画面からはもう追加しました。続けて追加するときは、/add からもう一度開いてください", 409)
    t = _valid_ticket(s["user_id"], now)
    if t is None:
        raise ActivityError("/add のパネルが閉じたか、時間が経ちました。/add からもう一度開いてください", 401)
    return s, t


async def _exchange_code(code):
    """ログイン確認のコードを Discord に渡して、(アクセストークン, 利用者ID) を得る"""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
        async with http.post(f"{_DISCORD_API}/oauth2/token", data={
                "client_id": str(client.application_id), "client_secret": ACTIVITY_CLIENT_SECRET,
                "grant_type": "authorization_code", "code": code}) as r:
            if r.status != 200:
                _activity_log(f"ログイン確認に失敗しました（Discord の応答 HTTP {r.status}）")
                raise ActivityError("Discord のログイン確認に失敗しました。画面を開き直してください", 502)
            token = (await r.json()).get("access_token")
        async with http.get(f"{_DISCORD_API}/users/@me", headers={"Authorization": f"Bearer {token}"}) as r:
            if r.status != 200:
                _activity_log(f"利用者の確認に失敗しました（Discord の応答 HTTP {r.status}）")
                raise ActivityError("Discord のログイン確認に失敗しました。画面を開き直してください", 502)
            user = await r.json()
    return token, int(user["id"])


async def activity_start(code, now=None):
    """画面を開いたとき。ログイン確認をして、チケットがあれば合言葉を渡す"""
    if not ACTIVITY_ENABLED:
        raise ActivityError("この機能は有効になっていません（DISCORD_CLIENT_SECRET が設定されていません）", 503)
    if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", code):
        raise ActivityError("確認の情報が正しくありません。画面を開き直してください")
    access_token, user_id = await _exchange_code(code)
    now = now if now is not None else time.monotonic()
    with _activity_lock:
        t = _valid_ticket(user_id, now)
        if t is None:
            _activity_log("チケットの無い人が開きました（/add のパネルを通さずに開いた）")
            raise ActivityError("Discord の /add のパネルにある「📅 カレンダーで選ぶ」から開いてください", 403)
        session = secrets.token_urlsafe(32)     # 256ビットの乱数
        _activity_sessions[session] = {"user_id": user_id, "expires": t["expires"], "used": False}
        calendars = t["calendars"]
    _activity_log("画面を開きました（本人の確認ができました）")
    today = _today()
    return {"access_token": access_token, "session": session,
            "calendars": [{"value": c.value, "label": c.name} for c in calendars],
            "today": today.isoformat(), "max_days": ADD_MAX_DAYS,
            "titles_until": (today + timedelta(days=DAYS_TO_SHOW)).isoformat()}


def _parse_iso_day(s):
    if not isinstance(s, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        raise ActivityError("日付の形が正しくありません")
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise ActivityError(f"{s} は存在しない日付です")


def _event_segments(events, first, last):
    """予定を、日ごとの帯（開始・終了の分）に分ける。名前は自動表示と同じ範囲（今日から DAYS_TO_SHOW 日）だけ出す"""
    today = _today()
    titles_until = today + timedelta(days=DAYS_TO_SHOW)
    out = []
    for e in events:
        title = e.get("summary") or "(無題)"
        st, en = e["start"], e["end"]
        if "dateTime" in st:
            s = datetime.fromisoformat(st["dateTime"]).astimezone(JST)
            f = datetime.fromisoformat(en["dateTime"]).astimezone(JST)
            d = s.date()
            while d <= f.date() and d <= last:
                day_start = datetime(d.year, d.month, d.day, tzinfo=JST)
                a = max(s, day_start)
                b = min(f, day_start + timedelta(days=1))
                if b > a and d >= first:
                    out.append({"date": d.isoformat(), "all_day": False,
                                "start": int((a - day_start).total_seconds() // 60),
                                "end": int((b - day_start).total_seconds() // 60),
                                "title": title if today <= d < titles_until else None})
                d += timedelta(days=1)
        else:
            d = date.fromisoformat(st["date"])
            end_excl = date.fromisoformat(en["date"])      # 終日の終わりは翌日（含まない）
            while d < end_excl and d <= last:
                if d >= first:
                    out.append({"date": d.isoformat(), "all_day": True, "start": 0, "end": 1440,
                                "title": title if today <= d < titles_until else None})
                d += timedelta(days=1)
    return out


async def activity_data(session_token, first_s, last_s, now=None):
    """画面に出す予定を返す（月表示・週表示の範囲）"""
    now = now if now is not None else time.monotonic()
    with _activity_lock:
        _session_for(session_token, now)
    first, last = _parse_iso_day(first_s), _parse_iso_day(last_s)
    if last < first or (last - first).days >= _ACTIVITY_MAX_SPAN_DAYS:
        raise ActivityError("読み込む期間が正しくありません")
    start_dt = datetime(first.year, first.month, first.day, tzinfo=JST)
    end_dt = datetime(last.year, last.month, last.day, tzinfo=JST) + timedelta(days=1)
    events, failures = fetch_events_multi(start_dt, end_dt)
    return {"events": _event_segments(events, first, last),
            "warning": f"⚠️ {len(failures)}件のカレンダーを読み取れませんでした。表示は不完全です" if failures else None}


def build_web_requests(days, start, end, calendar):
    """画面で選んだ内容を、日ごとの文字のコマンドに組み立てて parse_add_command に通す。
    戻り値は (解釈結果の一覧, 文字列の一覧, 誤り)。誤りの文言はパネルと同じ"""
    if not days:
        return None, None, "日付を選んでください"
    if len(days) > ADD_MAX_DAYS:
        return None, None, f"一度に追加できるのは{ADD_MAX_DAYS}日までです（いま{len(days)}日選んでいます）"
    texts = []
    for d in sorted(days):
        text, error = build_add_command(f"{d.year}-{d.month}.{d.day}", start, end, None, calendar)
        if error:
            return None, None, error
        texts.append(text)
    try:
        return [parse_add_command(t) for t in texts], texts, None
    except AddError as e:
        return None, texts, str(e)


class _ActivityChannel:
    """既存の追加処理の返信先。チャンネルに送りつつ、画面にも同じ文を返すため控えておく"""
    def __init__(self, channel):
        self.channel = channel
        self.sent = []
        self.send_error = None

    def typing(self):
        return _NoTyping()                     # 入力中の表示は送らない（権限が無いと追加の前に止まるため）

    async def send(self, content=None, **kw):
        self.sent.append(content)
        try:
            await self.channel.send(content, **kw)
        except discord.HTTPException as e:
            self.send_error = e                # 予定の追加は済んでいるので、ここで止めずに画面へ伝える


class _ActivityMessage:
    def __init__(self, channel, text):
        self.content = text
        self.channel = channel


_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")


async def activity_add(session_token, body, now=None):
    """画面の「追加」。中身を検査し、既存の処理で追加して、結果をチャンネルと画面に返す"""
    now = now if now is not None else time.monotonic()
    raw_days = body.get("days")
    if not isinstance(raw_days, list) or len(raw_days) > 400:
        raise ActivityError("日付の指定が正しくありません")
    days = {_parse_iso_day(x) for x in raw_days}
    start, end = body.get("start"), body.get("end")
    for t in (start, end):
        if t is not None and not (isinstance(t, str) and _TIME_RE.fullmatch(t)):
            raise ActivityError("時刻の形が正しくありません")
    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ActivityError("予定の名前を入れてください")
    name = name.strip()
    if len(name) > 200:
        raise ActivityError("予定の名前は200文字までにしてください")
    calendar = body.get("calendar")
    with _activity_lock:
        session, ticket = _session_for(session_token, now)
        if calendar not in {c.value for c in ticket["calendars"]}:
            raise ActivityError("追加先のカレンダーを選び直してください")
        parsed, texts, error = build_web_requests(days, start, end, calendar)
        if error:
            raise ActivityError(error)
        session["used"] = True                 # 二重に押されても1回だけ（ここから先は取り消さない）
        channel, panel = ticket["channel"], ticket["panel"]
        del _activity_tickets[session["user_id"]]
    for p in parsed:
        p["name"] = name                       # 解釈が済んでから本当の名前を入れる
    print(f"スラッシュコマンド: {texts!r}（予定の名前はログに残さない。カレンダー画面から）")
    out = _ActivityChannel(channel)
    message = _ActivityMessage(out, texts[0])
    if len(parsed) == 1:
        await handle_add_command(message, parsed[0])
    else:
        await handle_add_many(message, parsed)
    if panel is not None:
        await panel.finish("このパネルは閉じました。カレンダー画面から追加し、結果はチャンネルに表示しています。")
    reply = "\n".join(x for x in out.sent if x)
    if out.send_error is not None:
        _activity_log(f"チャンネルへの返信に失敗しました: {out.send_error}")
        reply += "\n（チャンネルへの返信に失敗しました。ボットの「メッセージを送信」の権限を確認してください）"
    _activity_log(f"追加の処理をしました（{len(parsed)}件）")
    return {"ok": True, "message": reply}


def _run_on_bot_loop(coro, timeout=120):
    """Web サーバーのスレッドから、ボット本体の処理の流れで coro を実行して結果を待つ"""
    if _bot_loop is None:
        coro.close()
        raise ActivityError("ボットの起動が終わっていません。少し待ってから開き直してください", 503)
    return asyncio.run_coroutine_threadsafe(coro, _bot_loop).result(timeout)


def _activity_page():
    """画面の HTML。アプリの ID（公開してよい値）だけを埋め込む"""
    with open(os.path.join(ACTIVITY_DIR, "index.html"), encoding="utf-8") as f:
        html = f.read()
    return html.replace("{{CLIENT_ID}}", str(client.application_id or ""))


_ACTIVITY_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                 "img-src 'self' data:; base-uri 'none'; form-action 'none'; object-src 'none'")


def handle_activity_request(method, path, body_bytes):
    """/activity の下へのリクエストを処理し、(状態コード, Content-Type, 中身) を返す"""
    sub = path[len("/activity"):].lstrip("/")
    if method in ("GET", "HEAD"):
        if sub in ("", "index.html"):
            return 200, "text/html; charset=utf-8", _activity_page().encode("utf-8")
        if sub in _ACTIVITY_FILES:
            with open(os.path.join(ACTIVITY_DIR, sub), "rb") as f:
                return 200, _ACTIVITY_FILES[sub], f.read()
        return 404, "text/plain; charset=utf-8", b"not found\n"
    if method != "POST" or not sub.startswith("api/"):
        return 405, "text/plain; charset=utf-8", b"method not allowed\n"
    try:
        if len(body_bytes) > _ACTIVITY_MAX_BODY:
            raise ActivityError("送られた中身が大きすぎます", 413)
        try:
            body = json.loads(body_bytes.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ActivityError("送られた中身が読めません")
        if not isinstance(body, dict):
            raise ActivityError("送られた中身が読めません")
        if sub == "api/start":
            result = _run_on_bot_loop(activity_start(body.get("code")))
        elif sub == "api/data":
            result = _run_on_bot_loop(activity_data(body.get("session"), body.get("first"), body.get("last")))
        elif sub == "api/add":
            result = _run_on_bot_loop(activity_add(body.get("session"), body))
        else:
            raise ActivityError("見つかりません", 404)
        code = 200
    except ActivityError as e:
        result, code = {"ok": False, "error": str(e)}, e.status
    except Exception as e:
        print(f"アクティビティのエラー: {e!r}")
        result, code = {"ok": False, "error": f"処理中にエラーが発生しました（{type(e).__name__}）"}, 500
    if code != 200:
        _activity_log(f"{sub} → {code}")
    return code, "application/json; charset=utf-8", json.dumps(result, ensure_ascii=False).encode("utf-8")


# ===== ⑨-A ダミーWebサーバー（ここに丸ごと置く） =====
class HealthHandler(BaseHTTPRequestHandler):
    """Render のポート確認と、UptimeRobot などの監視から叩かれる。

    以前は状態に関係なく常に「bot is alive」を返していたため、ボットが
    止まっていても監視上は「Up」のままだった。いまは実際の状態を返す。
      /        … 状態を文章で返す。応答コードは従来どおり常に 200
      /health  … ボットが応答できない状態なら 503 を返す。
                 監視をこちらに向けると、止まったときに「Down」と通知される
    """
    def _respond(self, with_body):
        try:
            level, lines = status_report()
        except Exception as e:
            # 状態の取得自体に失敗したら、それも「応答できない」として返す
            level, lines = "error", [f"❌ 状態を取得できませんでした（{type(e).__name__}）"]
        path = self.path.split("?", 1)[0]
        code = 503 if (path == "/health" and level == "error") else 200
        body = ("\n".join(lines) + "\n").encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if with_body:
            self.wfile.write(body)

    def _respond_activity(self, method, with_body):
        """カレンダー画面（アクティビティ）の要求。/activity の下だけを扱う"""
        path = self.path.split("?", 1)[0]       # ? 以降（Discord が付ける画面の情報）はログにも使わない
        body = b""
        if method == "POST":
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > _ACTIVITY_MAX_BODY:
                code, ctype, data = 413, "application/json; charset=utf-8", \
                    json.dumps({"ok": False, "error": "送られた中身が大きすぎます"}, ensure_ascii=False).encode("utf-8")
                return self._send(code, ctype, data, with_body)
            body = self.rfile.read(length)
        code, ctype, data = handle_activity_request(method, path, body)
        if method != "POST":
            _activity_log(f"{method} {path} → {code}")   # URL Mappings の確認に使う（合言葉は ? や本文にあり、ここには無い）
        self._send(code, ctype, data, with_body)

    def _send(self, code, ctype, data, with_body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy", _ACTIVITY_CSP)
        self.end_headers()
        if with_body:
            self.wfile.write(data)

    def _is_activity(self):
        path = self.path.split("?", 1)[0]
        return path == "/activity" or path.startswith("/activity/")

    def do_GET(self):
        if self._is_activity():
            return self._respond_activity("GET", with_body=True)
        self._respond(with_body=True)

    def do_HEAD(self):
        if self._is_activity():
            return self._respond_activity("HEAD", with_body=False)
        self._respond(with_body=False)

    def do_POST(self):
        if self._is_activity():
            return self._respond_activity("POST", with_body=True)
        self._send(405, "text/plain; charset=utf-8", b"method not allowed\n", True)

    def log_message(self, *args):
        pass

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    # 同時に応答できる形。カレンダー画面から予定を追加している間も、監視（UptimeRobot）に応答できるように
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    server.daemon_threads = True
    server.serve_forever()

threading.Thread(target=run_web_server, daemon=True).start()


# ===== ⑨ ボット準備完了時の処理（この2行はくっつける） =====
@tasks.loop(seconds=LOOP_TICK_SEC)
async def loop_heartbeat():
    """イベントループが動いている証拠として、時刻を記録し続ける。
    何かがループを塞ぐとこの記録が途絶え、状態表示が「固まっている」と示す"""
    health["loop_tick"] = time.monotonic()


@client.event
async def on_ready():
    print(f"ログインしました: {client.user}")
    global _bot_loop
    _bot_loop = asyncio.get_running_loop()    # カレンダー画面の処理をここで動かす（Web サーバーのスレッドから渡す）
    if ACTIVITY_ENABLED:
        print("カレンダー画面（アクティビティ）を使えます（/add のパネルに「📅 カレンダーで選ぶ」が出ます）")
    # 処理が止まっていないかの見張り（状態表示で使う）。
    # 再接続のたびに on_ready が呼ばれることがあり、二重に start すると例外になる
    if not loop_heartbeat.is_running():
        loop_heartbeat.start()
    # スラッシュコマンドの登録は裏で行い、自動表示の開始を待たせない。
    # タスクは参照を持っておかないと途中で片付けられることがあるため、変数に残す
    global _sync_task
    _sync_task = asyncio.create_task(sync_slash_commands())
    update_calendar.start()


# ===== ⑩ ボットを起動する =====
client.run(DISCORD_BOT_TOKEN)
