# ===== ① 必要な道具（ライブラリ）を読み込む =====
import os                              # 環境変数（コード外から渡す値）を読むための道具 ★追加
import json                           # JSON文字列を扱うための道具 ★追加
import discord
import threading                      # 2つの処理を同時に動かすための道具 ★追加
import re                              # 文字列のパターンを判定する道具 ★追加
from http.server import HTTPServer, BaseHTTPRequestHandler  # 簡易Webサーバー ★追加
from discord.ext import tasks
from google.oauth2 import service_account
from googleapiclient.discovery import build
from datetime import datetime, timezone, timedelta


# ===== ② 設定値（環境変数から読み込む） =====
# os.environ[...] は「サーバー側で設定した値をここに読み込む」という意味 ★変更
DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
CHANNEL_ID = int(os.environ["CHANNEL_ID"])   # 環境変数は文字なので数字に変換する
CALENDAR_ID = os.environ["CALENDAR_ID"]

DAYS_TO_SHOW = 90
DAYS_PER_MESSAGE = 25
JST = timezone(timedelta(hours=9))     # 日本時間


# ===== ③ Googleカレンダーへの接続準備（鍵も環境変数から） =====
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

# 鍵JSONの「中身そのもの」を環境変数から受け取り、辞書に変換する ★変更
service_account_info = json.loads(os.environ["SERVICE_ACCOUNT_JSON"])
creds = service_account.Credentials.from_service_account_info(  # ファイルではなく中身から作る
    service_account_info, scopes=SCOPES
)
service = build("calendar", "v3", credentials=creds)


# ===== ④ Discordへの接続準備 =====
intents = discord.Intents.default()
intents.message_content = True         # ★追加：メッセージ本文を読めるようにする
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


def fetch_month_events(year, month):
    """指定した年月の予定をカレンダーから取得する"""
    start = datetime(year, month, 1, tzinfo=JST)          # その月の1日 0:00
    if month == 12:                                       # 12月なら翌年1月が終わり
        end = datetime(year + 1, 1, 1, tzinfo=JST)
    else:
        end = datetime(year, month + 1, 1, tzinfo=JST)    # 翌月1日が終わり
    result = service.events().list(
        calendarId=CALENDAR_ID,
        timeMin=start.isoformat(),
        timeMax=end.isoformat(),
        maxResults=2500,
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    return result.get("items", []), start, end


def find_free_days(year, month, slot_start, slot_end):
    """指定時間帯に予定が無い日の一覧を返す"""
    events, month_start, month_end = fetch_month_events(year, month)

    busy_dates = set()                 # 予定で埋まっている日を記録する入れ物

    for e in events:
        # --- 終日予定は、その日全体を埋まり扱いにする ---
        if "date" in e["start"]:
            d = datetime.fromisoformat(e["start"]["date"]).date()
            end_d = datetime.fromisoformat(e["end"]["date"]).date()
            while d < end_d:           # 複数日にまたがる場合も全部埋める
                busy_dates.add(d)
                d += timedelta(days=1)
            continue

        # --- 時刻付き予定は、時間帯が重なるかを判定する ---
        ev_start = datetime.fromisoformat(e["start"]["dateTime"]).astimezone(JST)
        ev_end = datetime.fromisoformat(e["end"]["dateTime"]).astimezone(JST)

        # 予定が日をまたぐ場合に備え、1日ずつ確認する
        day = ev_start.date()
        while day <= ev_end.date():
            # その日の対象時間帯（例 21:00〜24:00）を作る
            slot_s = datetime(day.year, day.month, day.day, slot_start, tzinfo=JST)
            slot_e = datetime(day.year, day.month, day.day, 0, tzinfo=JST) \
                     + timedelta(hours=slot_end)
            # 予定と時間帯が少しでも重なっていれば「埋まっている」とみなす
            if ev_start < slot_e and ev_end > slot_s:
                busy_dates.add(day)
            day += timedelta(days=1)

    # --- 月の全日を並べ、埋まっていない日だけ残す ---
    free_days = []
    day = month_start.date()
    while day < month_end.date():
        if day not in busy_dates:
            free_days.append(day)
        day += timedelta(days=1)
    return free_days


def format_free_days(year, month, free_days, label):
    """結果を見やすい文章に整える"""
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]
    if not free_days:
        return f"**{year}年{month}月**：{label}に空いている日はありません"
    days = [f"{free_days[0].month}/{free_days[0].day}"]        # 最初だけ「9/1」の形
    days += [str(d.day) for d in free_days[1:]]                # 2件目以降は日にちだけ
    return f"**{year}年{month}月 / {label}が空いている日**\n" + ", ".join(days)


# ===== ⑧-C メッセージを受け取ったときの処理 ★追加 =====
@client.event
async def on_message(message):
    if message.author.bot:             # ボット自身の投稿には反応しない
        return

    text = message.content.strip()     # 送られた文字（前後の空白を除去）

    # 「/2026-09」または「/n2026-09」の形かを判定する
    match = re.fullmatch(r"/(n?)(\d{4})-(\d{1,2})", text)
    if not match:
        return                         # 該当しなければ何もしない

    is_night = match.group(1) == "n"   # 先頭に n があれば夜モード
    year = int(match.group(2))
    month = int(match.group(3))

    if not 1 <= month <= 12:           # 月の値が変なら注意して終了
        await message.channel.send("月は1〜12で指定してください")
        return

    # 夜か日中かで、調べる時間帯とラベルを切り替える
    if is_night:
        slot_start, slot_end, label = NIGHT_START, NIGHT_END, "夜（21:00-24:00）"
    else:
        slot_start, slot_end, label = DAY_START, DAY_END, "日中（10:00-18:00）"

    free_days = find_free_days(year, month, slot_start, slot_end)
    await message.channel.send(format_free_days(year, month, free_days, label))
    
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
