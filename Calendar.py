# ===== ① 必要な道具（ライブラリ）を読み込む =====
import os                              # 環境変数（コード外から渡す値）を読むための道具
import json                           # JSON文字列を扱うための道具
import discord
import threading                      # 2つの処理を同時に動かすための道具
import re                              # 文字列のパターンを判定する道具
import calendar as _calendar           # 月末日を求める道具
import asyncio
import io                               # 画像をメモリ上で扱う道具
from http.server import HTTPServer, BaseHTTPRequestHandler  # 簡易Webサーバー
from discord.ext import tasks
from google.oauth2 import service_account
from googleapiclient.discovery import build
from datetime import datetime, timezone, timedelta, date
from PIL import Image, ImageDraw, ImageFont  # 画像生成の道具（要 Pillow）


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
@tasks.loop(minutes=60)
async def update_calendar():
    events = fetch_events()
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
    """指定月について、昼が埋まっている日の集合・夜が埋まっている日の集合を返す。
       判定は空き日程コマンド（find_free_days）と同じ基準を使う。"""
    first = date(year, month, 1)
    last_day = _calendar.monthrange(year, month)[1]
    last = date(year, month, last_day)

    # find_free_days は「空いている日」を返すので、その補集合が「埋まっている日」
    day_free = set(find_free_days(first, last, DAY_START, DAY_END))
    night_free = set(find_free_days(first, last, NIGHT_START, NIGHT_END))

    day_busy = set()
    night_busy = set()
    d = first
    while d <= last:
        if d not in day_free:
            day_busy.add(d)
        if d not in night_free:
            night_busy.add(d)
        d += timedelta(days=1)
    return day_busy, night_busy


def render_month_image(year, month):
    """月間カレンダー画像を生成（確定CSSレイアウトをPillowで正確に再現）"""
    day_busy, night_busy = collect_month_busy(year, month)

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
    year_text = f"{year} "
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
    return buf


# ===== ⑧-C メッセージを受け取ったときの処理 ★追加 =====
@client.event
async def on_message(message):
    if message.author.bot:
        return

    text = message.content.strip()

    # --- /c2026-9 : 月間カレンダー画像を出力 ---
    cmatch = re.fullmatch(r"/c(\d{4})-(\d{1,2})", text)
    if cmatch:
        year = int(cmatch.group(1))
        month = int(cmatch.group(2))
        if not 1 <= month <= 12:
            await message.channel.send("月は1〜12で指定してください")
            return
        try:
            # 画像生成は重い処理なので、別スレッドで実行してボットを固めない
            buf = await asyncio.to_thread(render_month_image, year, month)
            file = discord.File(buf, filename=f"calendar_{year}_{month:02d}.png")
            await message.channel.send(f"📅 {year}年{month}月", file=file)
        except Exception as e:
            print(f"画像生成エラー: {e}")
            await message.channel.send("画像の生成に失敗しました")
        return

    # 先頭が / で、次に n/a/無し、その後ろに範囲文字列、という形かを判定
    match = re.fullmatch(r"/([nau]?)([\d\-\.:]+)(r?)", text)   # 末尾に r を許可

    if not match:
        return

    mode = match.group(1)              # "" or "n" or "a" or "u"
    body = match.group(2)              # 例 "2026-8:2026-9"
    negate = match.group(3) == "r"     # 末尾に r があれば「反転」＝予定がある日

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
