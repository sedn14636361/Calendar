# ===== ① 必要な道具（ライブラリ）を読み込む =====
import os                              # 環境変数（コード外から渡す値）を読むための道具
import json                           # JSON文字列を扱うための道具
import discord
from discord import app_commands       # スラッシュコマンド（/date・/add）
from typing import Optional
import threading                      # 2つの処理を同時に動かすための道具
import re                              # 文字列のパターンを判定する道具
import unicodedata                      # 全角の数字・記号を半角にそろえる道具
import calendar as _calendar           # 月末日を求める道具
import asyncio
import time                             # 処理が止まっていないかを測る道具
import io                               # 画像をメモリ上で扱う道具
import math                             # 応答時間が有限かを確かめる道具
from http.server import HTTPServer, BaseHTTPRequestHandler  # 簡易Webサーバー
from discord.ext import tasks
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError   # Google が返したエラー（403 など）
from datetime import datetime, timezone, timedelta, date
from PIL import Image, ImageDraw, ImageFont  # 画像生成の道具（要 Pillow）


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
state = {"messages": [], "signature": None}


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
        embed = discord.Embed(color=0x4285F4)
        for name, value in chunk:
            embed.add_field(name=name, value=value, inline=False)
        embeds.append(embed)
    return embeds


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


async def handle_add_command(message, parsed):
    """予定を追加して、結果を返信する"""
    async with message.channel.typing():
        try:
            index, cid, name, role = resolve_add_calendar(parsed["calendar"])
            label = _calendar_label(index, name)
            # 書き込み権限が無いことが分かっているなら、追加を試す前に理由を返す
            if role is not None and role not in _WRITABLE_ROLES:
                raise AddError(
                    f"{label}には書き込む権限がありません（いまの権限：{role}）。\n"
                    "Google カレンダーの共有設定で、サービスアカウントの権限を"
                    "「すべての予定の詳細の変更や表示ができます」にしてください")
            created = service.events().insert(
                calendarId=cid, body=build_event_body(parsed)).execute()
        except AddError as e:
            await message.channel.send(f"⚠️ {e}")
            return
        except HttpError as e:
            status = e.status_code
            print(f"予定の追加エラー（HTTP {status}）: {e}")
            if status == 403:
                reason = ("カレンダーに書き込む権限がありません。Google カレンダーの共有設定で、"
                          "サービスアカウントの権限を「すべての予定の詳細の変更や表示ができます」にしてください")
            elif status == 404:
                reason = "カレンダーが見つかりません。CALENDAR_ID と共有設定を確認してください"
            else:
                reason = f"Google がエラーを返しました（HTTP {status}）"
            await message.channel.send(f"⚠️ 予定を追加できませんでした。{reason}")
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
    "/date          フォームで月・時間帯・曜日（チェック）を選んで調べる\n"
    "/add           予定を、日付・時刻を選んで追加する\n"
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

    # 日付への変換を試す（形式が変なら注意メッセージ）
    try:
        start_date, end_date = parse_range(body)
    except (ValueError, IndexError):
        await message.channel.send(
            "書式が正しくありません。\n"
            "例：/2026-9 、/a2026-8:2026-9 、/n2026-8.15:2026-9.30 、/2026-9/sat/sun"
        )
        return True

    if start_date > end_date:          # 開始と終了が逆なら注意
        await message.channel.send("開始日が終了日より後になっています")
        return True

    # 範囲は最大12か月まで（超長期の指定によるAPI過負荷・レート制限を防ぐ）
    # 開始日の12か月後を上限日として比較する
    limit_year = start_date.year + 1
    limit_month = start_date.month
    limit_day = min(start_date.day, _calendar.monthrange(limit_year, limit_month)[1])
    limit_date = date(limit_year, limit_month, limit_day)
    if end_date > limit_date:
        await message.channel.send("指定できる範囲は最大12か月までです")
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
    """/add の date・end_date を「2026-10.5」の形にする。読めなければ None。
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
            return None, "時刻を指定するときは start と end の両方を選んでください（両方空なら終日）"
        st, en = _slash_time(start), _slash_time(end)
        if st is None or en is None:
            return None, "時刻は `14:00` の形で選ぶか入力してください"
        parts.append(f"{st}-{en}")
    parts.append("_")                          # 名前の代わり（後で本当の名前に差し替える）
    return " ".join(parts), None


# --- 候補（入力中に出る一覧）。どれも最大25件まで（Discord の上限） ---
def _filter(choices, current):
    cur = _nfkc(current or "").strip().casefold()
    if not cur:
        return choices
    return [c for c in choices if cur in _nfkc(c.name).casefold() or cur in c.value.casefold()]


def _month_choices(base, count=12):
    out = []
    for i in range(count):
        y = base.year + (base.month - 1 + i) // 12
        m = (base.month - 1 + i) % 12 + 1
        out.append(app_commands.Choice(name=f"{y}年{m}月", value=f"{y}-{m}"))
    return out


def _day_choices(base, count=14, today=None):
    today = today or _today()
    out = []
    for i in range(count):
        d = base + timedelta(days=i)
        note = "（今日）" if d == today else "（明日）" if d == today + timedelta(days=1) else ""
        out.append(app_commands.Choice(name=f"{d.year}/{_md(d)}{note}",
                                       value=f"{d.year}-{d.month}.{d.day}"))
    return out


def _day_suggestions(current, base):
    typed = _slash_day(current) if current else None
    if typed:
        try:
            y, md = typed.split("-")
            mo, d = md.split(".")
            return _day_choices(date(int(y), int(mo), int(d)))[:25]   # 打った日から2週間
        except ValueError:
            return [app_commands.Choice(name=typed, value=typed)]      # 存在しない日付もそのまま
    return _filter(_day_choices(base), current)[:25]


async def _date_autocomplete(interaction, current):
    return _day_suggestions(current, _today())


async def _end_date_autocomplete(interaction, current):
    base = _today()
    start = _slash_day(getattr(interaction.namespace, "date", None) or "")
    if start:
        try:
            y, md = start.split("-")
            mo, d = md.split(".")
            base = date(int(y), int(mo), int(d)) + timedelta(days=1)
        except ValueError:
            pass
    return _day_suggestions(current, base)


_HALF_HOURS = [f"{h}:{m:02d}" for h in range(24) for m in (0, 30)]


def _time_suggestions(current, after=None):
    if after is not None:                      # 開始の30分後から24時間ぶん（日またぎも選べる）
        i = _HALF_HOURS.index(after) if after in _HALF_HOURS else None
        if i is not None:
            seq = [_HALF_HOURS[(i + k) % 48] for k in range(1, 49)]
            labels = []
            for k, v in enumerate(seq, 1):
                if v == "0:00" and i + k == 48:
                    labels.append(("24:00", "24:00"))
                else:
                    labels.append((v + ("（翌日）" if i + k > 48 else ""), v))
            choices = [app_commands.Choice(name=n, value=v) for n, v in labels]
        else:
            choices = [app_commands.Choice(name=v, value=v) for v in _HALF_HOURS]
    else:
        choices = [app_commands.Choice(name=v, value=v) for v in _HALF_HOURS]
        if not (current or "").strip():
            choices = choices[16:41]           # 何も打っていなければ 8:00〜20:00 を出す
    cur = _nfkc(current or "").strip()
    if cur:
        found = [c for c in choices if c.value.startswith(cur) or c.name.startswith(cur)]
        typed = _slash_time(cur)
        if typed and all(c.value != typed for c in found):
            found = [app_commands.Choice(name=typed, value=typed)] + found   # 14:15 なども選べる
        choices = found
    return choices[:25]


async def _start_time_autocomplete(interaction, current):
    return _time_suggestions(current)


async def _end_time_autocomplete(interaction, current):
    start = _slash_time(getattr(interaction.namespace, "start", None) or "")
    return _time_suggestions(current, after=start)


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


async def _calendar_autocomplete(interaction, current):
    return _filter(_calendar_choices(), current)[:25]


# /date は入力フォーム（モーダル）を開く。曜日をチェックボックスで自由に選べるようにするため。
# スラッシュコマンドの欄は1つの値しか持てず、複数選択ができない。
# フォームに置ける部品は最大5つ（discord.py の制約）なので、
# 開始の月・終了の月・時間帯・曜日・形式でちょうど使い切る。
_WEEKDAY_VALUES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class DateModal(discord.ui.Modal):
    """/date で開くフォーム。送信されたら文字のコマンドに組み立てて実行する"""

    def __init__(self, today=None):
        super().__init__(title="空いている日を調べる")
        months = _month_choices(today or _today())    # 開くたびに「今月から12か月」を作る
        self.start = discord.ui.Select(
            options=[discord.SelectOption(label=c.name, value=c.value, default=(i == 0))
                     for i, c in enumerate(months)],
            min_values=1, max_values=1, required=True)
        self.end = discord.ui.Select(
            options=[discord.SelectOption(label=c.name, value=c.value) for c in months],
            placeholder="選ばなければ開始と同じ月",
            min_values=0, max_values=1, required=False)
        self.time = discord.ui.RadioGroup(options=[
            discord.RadioGroupOption(label=c.name, value=c.value, default=(c.value == "day"))
            for c in TIME_CHOICES], required=True)
        # 選択肢に「曜」は付けない（月 火 水 木 金 土 日）
        self.weekday = discord.ui.CheckboxGroup(options=[
            discord.CheckboxGroupOption(label=WEEKDAY_NAMES[i], value=v)
            for i, v in enumerate(_WEEKDAY_VALUES)],
            min_values=0, max_values=7, required=False)  # 既定の max_values は1なので明示する
        self.fmt = discord.ui.RadioGroup(options=[
            discord.RadioGroupOption(label=c.name, value=c.value, default=(c.value == "normal"))
            for c in FORMAT_CHOICES], required=True)
        self.add_item(discord.ui.Label(text="開始の月", component=self.start))
        self.add_item(discord.ui.Label(text="終了の月", component=self.end,
                                       description="選ばなければ開始と同じ月"))
        self.add_item(discord.ui.Label(text="時間帯", component=self.time))
        self.add_item(discord.ui.Label(text="曜日", component=self.weekday,
                                       description="選ばなければすべての曜日"))
        self.add_item(discord.ui.Label(text="出し方", component=self.fmt))

    def to_command(self):
        """選ばれた内容から文字のコマンドを組み立てる。誤りがあれば (None, 理由)"""
        start = self.start.values[0] if self.start.values else None
        if start is None:
            return None, "開始の月を選んでください"
        end = self.end.values[0] if self.end.values else None
        # 選ばれた順ではなく月→日の順で並べる（結果は同じだが、組み立てる文字列をそろえるため）
        picked = [v for v in _WEEKDAY_VALUES if v in set(self.weekday.values)]
        return build_date_command(start, end, self.time.value, ",".join(picked) or None,
                                  self.fmt.value)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)   # 先に「考え中…」を返す
        try:
            text, error = self.to_command()
            if error:
                await interaction.followup.send(f"⚠️ {error}")
                return
            await _run_as_text_command(interaction, text)
        except Exception as e:
            # 返信しないと「考え中…」のまま止まるので、失敗もその場で返す
            print(f"/date のエラー: {e!r}")
            await interaction.followup.send(f"⚠️ 処理中にエラーが発生しました（{type(e).__name__}）")


@tree.command(name="date", description="空いている日を調べる（フォームで月・時間帯・曜日を選ぶ）")
async def slash_date(interaction: discord.Interaction):
    # フォームは最初の応答として開く必要がある（先に「考え中…」を返すと開けない）
    await interaction.response.send_modal(DateModal())


@tree.command(name="add", description="Google カレンダーに予定を追加する（時刻が空なら終日）")
@app_commands.rename(date_="date")
@app_commands.describe(
    name="予定の名前",
    date_="日付（押すと2週間分の一覧。10/5 や 2026-10.5 と入れてもよい）",
    start="開始時刻（空なら終日）",
    end="終了時刻（開始より前なら翌日まで）",
    end_date="複数日の終日予定にするときの最終日",
    calendar="追加先のカレンダー（省略すると先頭のカレンダー）",
)
@app_commands.autocomplete(date_=_date_autocomplete, start=_start_time_autocomplete,
                           end=_end_time_autocomplete, end_date=_end_date_autocomplete,
                           calendar=_calendar_autocomplete)
async def slash_add(interaction: discord.Interaction, name: str, date_: str,
                    start: Optional[str] = None, end: Optional[str] = None,
                    end_date: Optional[str] = None, calendar: Optional[str] = None):
    await interaction.response.defer(thinking=True)
    try:
        name = name.strip()
        if not name:
            await interaction.followup.send("⚠️ 予定の名前を入れてください")
            return
        text, error = build_add_command(date_, start, end, end_date, calendar)
        if error:
            await interaction.followup.send(f"⚠️ {error}")
            return
        try:
            parsed = parse_add_command(text)
        except AddError as e:
            await interaction.followup.send(f"⚠️ {e}")
            return
        parsed["name"] = name                  # 解釈が済んでから本当の名前を入れる
        print(f"スラッシュコマンド: {text!r}（名前 {name!r}）")
        await handle_add_command(_SlashMessage(interaction, text), parsed)
    except Exception as e:
        print(f"/add のエラー: {e!r}")
        await interaction.followup.send(f"⚠️ 処理中にエラーが発生しました（{type(e).__name__}）")


_slash_synced = False
_sync_task = None


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
        synced = await tree.sync()
        if SLASH_COMMANDS_ENABLED:
            print("スラッシュコマンドを登録しました: " + ", ".join("/" + c.name for c in synced))
        else:
            print("SLASH_COMMANDS=off のため、スラッシュコマンドの登録を消しました")
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

    def do_GET(self):
        self._respond(with_body=True)

    def do_HEAD(self):
        self._respond(with_body=False)

    def log_message(self, *args):
        pass

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()

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
