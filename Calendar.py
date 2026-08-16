# ===== ① 必要な道具（ライブラリ）を読み込む =====
import os                              # 環境変数（コード外から渡す値）を読むための道具
import json                           # JSON文字列を扱うための道具
import discord
import threading                      # 2つの処理を同時に動かすための道具
import re                              # 文字列のパターンを判定する道具
import calendar as _calendar           # 月末日を求める道具
from http.server import HTTPServer, BaseHTTPRequestHandler  # 簡易Webサーバー
from discord.ext import tasks
from google.oauth2 import service_account
from googleapiclient.discovery import build
from datetime import datetime, timezone, timedelta


# ===== ② 設定値（環境変数から読み込む） =====
DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
CHANNEL_ID = int(os.environ["CHANNEL_ID"])   
CALENDAR_ID = os.environ["CALENDAR_ID"]

DAYS_TO_SHOW = 90
DAYS_PER_MESSAGE = 25
JST = timezone(timedelta(hours=9))     # 日本時間


# ===== ③ Googleカレンダーへの接続準備（鍵も環境変数から） =====
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

service_account_info = json.loads(os.environ["SERVICE_ACCOUNT_JSON"])
creds = service_account.Credentials.from_service_account_info(  
    service_account_info, scopes=SCOPES
)
service = build("calendar", "v3", credentials=creds)


# ===== ④ Discordへの接続準備 =====
intents = discord.Intents.default()
intents.message_content = True         # メッセージ本文を読めるようにする
client = discord.Client(intents=intents)


# ===== ⑤ 前回の状態を覚えておく箱 =====
state = {"messages": [], "signature": None}


# ===== ⑥ カレンダーから予定を取ってくる関数 =====
def fetch_events():
    now = datetime.now(JST)
    time_max = now + timedelta(days=DAYS_TO_SHOW)
    result = service.events().list(
        calendarId=CALENDAR_ID,
        timeMin=now.isoformat(),
        timeMax=time_max.isoformat(),
        maxResults=250,
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    return result.get("items", [])


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
@tasks.loop(minutes=5)
async def update_calendar():
    events = fetch_events()
    today = datetime.now(JST).date().isoformat()      # 今日の日付（日本時間）
    signature = str(today) + str([(e.get("summary"), e["start"]) for e in events])
    if signature == state["signature"]:
        return
    state["signature"] = signature

    embeds = build_embeds(events)
    channel = client.get_channel(CHANNEL_ID)
    if not state["messages"]:
        for embed in embeds:
            msg = await channel.send(embed=embed)
            state["messages"].append(msg)
    else:
        for msg, embed in zip(state["messages"], embeds):
            await msg.edit(embed=embed)

# ===== ⑧-B 指定月の空き日程を調べる機能 ★追加 =====

# 判定に使う時間帯（時単位）
DAY_START, DAY_END = 10, 18            # 日中 10:00-18:00
NIGHT_START, NIGHT_END = 21, 24        # 夜 21:00-24:00


def fetch_events_between(start_dt, end_dt):
    """指定した日時範囲の予定を取得する"""
    result = service.events().list(
        calendarId=CALENDAR_ID,
        timeMin=start_dt.isoformat(),
        timeMax=end_dt.isoformat(),
        maxResults=2500,
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    return result.get("items", [])


def find_free_days(start_date, end_date, slot_start, slot_end):
    """start_date〜end_date（両端含む）で、指定時間帯に予定が無い日を返す"""
    # 範囲の開始0:00から、終了日の翌日0:00まで取得
    range_start = datetime(start_date.year, start_date.month, start_date.day, tzinfo=JST)
    range_end = datetime(end_date.year, end_date.month, end_date.day, tzinfo=JST) \
                + timedelta(days=1)
    events = fetch_events_between(range_start, range_end)

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
    return free_days

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


# ===== ⑧-C メッセージを受け取ったときの処理 ★追加 =====
@client.event
async def on_message(message):
    if message.author.bot:
        return

    text = message.content.strip()

    # 先頭が / で、次に n/a/無し、その後ろに範囲文字列、という形かを判定
    match = re.fullmatch(r"/([nau]?)([\d\-\.:]+)", text)

    if not match:
        return

    mode = match.group(1)              # "" or "n" or "a" or "u"
    body = match.group(2)              # 例 "2026-8:2026-9"

    # 日付への変換を試す（形式が変なら注意メッセージ）
    try:
        start_date, end_date = parse_range(body)
    except (ValueError, IndexError):
        await message.channel.send(
            "書式が正しくありません。例：/2026-9 、/a2026-8:2026-9 、/n2026-8.15:2026-9.30"
        )
        return

    if start_date > end_date:          # 開始と終了が逆なら注意
        await message.channel.send("開始日が終了日より後になっています")
        return

    # モードごとに空き日を求める
    if mode == "n":
        free_days = find_free_days(start_date, end_date, NIGHT_START, NIGHT_END)
        label = "夜が空いている日"
    elif mode == "a":
        day_free = find_free_days(start_date, end_date, DAY_START, DAY_END)
        night_free = find_free_days(start_date, end_date, NIGHT_START, NIGHT_END)
        free_days = sorted(set(day_free) & set(night_free))
        label = "一日空いている日"
    elif mode == "u":                  # 昼か夜のどちらか（あるいは両方）が空いている日
        day_free = find_free_days(start_date, end_date, DAY_START, DAY_END)
        night_free = find_free_days(start_date, end_date, NIGHT_START, NIGHT_END)
        free_days = sorted(set(day_free) | set(night_free))   # どちらかに含まれる日
        label = "昼か夜が空いている日"
    else:
        free_days = find_free_days(start_date, end_date, DAY_START, DAY_END)
        label = "昼が空いている日"

    await message.channel.send(format_free_days_range(start_date, end_date, free_days, label))

# ===== ⑨-A ダミーWebサーバー（ここに丸ごと置く） =====
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"bot is alive")
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()
    def log_message(self, *args):
        pass

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()

threading.Thread(target=run_web_server, daemon=True).start()


# ===== ⑨ ボット準備完了時の処理（この2行はくっつける） =====
@client.event
async def on_ready():
    print(f"ログインしました: {client.user}")
    update_calendar.start()


# ===== ⑩ ボットを起動する =====
client.run(DISCORD_BOT_TOKEN)
