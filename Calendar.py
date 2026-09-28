# ===== ① 必要な道具（ライブラリ）を読み込む =====
import os                              # 環境変数（コード外から渡す値）を読むための道具
import json                           # JSON文字列を扱うための道具
import discord
import threading                      # 2つの処理を同時に動かすための道具
import re                              # 文字列のパターンを判定する道具
import calendar as _calendar           # 月末日を求める道具
import asyncio
import time                             # 締め出されたときに待つための道具
import io                               # 画像をメモリ上で扱う道具
import math                             # 応答時間が有限かを確かめる道具
import traceback                        # エラーの詳細をログに出す道具
from http.server import HTTPServer, BaseHTTPRequestHandler  # 簡易Webサーバー
from discord.ext import tasks
from google.oauth2 import service_account
from googleapiclient.discovery import build
import httplib2                         # Google API の通信部品（タイムアウトを付けるため）
import google_auth_httplib2             # 上の通信部品に認証を載せる道具
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
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

service_account_info = json.loads(os.environ["SERVICE_ACCOUNT_JSON"])
creds = service_account.Credentials.from_service_account_info(  
    service_account_info, scopes=SCOPES
)
service = build("calendar", "v3", credentials=creds)

# Google API の1回の問い合わせを待つ上限（秒）。
# 指定しないと httplib2 は無期限に待つ（timeout=None が既定）。応答が途絶えると
# その呼び出しは二度と戻らず、ボットが固まったまま「オンライン」に見え続ける。
# 参考：Google 自身の認証ライブラリの既定は 120 秒
# （google/auth/transport/requests.py の _DEFAULT_TIMEOUT）。
# ここでは人がチャットで返事を待っており、複数のカレンダーを順に読むため短くした。
GOOGLE_TIMEOUT_SEC = 30


def _new_http():
    """Google API 用の接続を1つ作る（タイムアウト付き・認証付き）。

    httplib2 はスレッド安全ではない（同ライブラリ自身が
    「Not thread-safe」と明記）。自動表示とコマンドが別々のスレッドから
    同時に読みに行くので、接続は呼び出しごとに新しく作り、共有しない。
    """
    return google_auth_httplib2.AuthorizedHttp(
        creds, http=httplib2.Http(timeout=GOOGLE_TIMEOUT_SEC))


# ===== ④ Discordへの接続準備 =====
intents = discord.Intents.default()
intents.message_content = True         # メッセージ本文を読めるようにする
client = discord.Client(intents=intents)


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
    "display_ok_at": None,     # 自動表示を最後に更新（または変化なしを確認）できた時刻
    "display_error": None,     # 自動表示の直近の失敗 (時刻, 説明)。成功したら消す
    "command_error": None,     # コマンド処理の直近の失敗 (時刻, 説明)。成功したら消す
}

# イベントループの見張り。LOOP_TICK_SEC ごとに時刻を記録し、
# LOOP_STALL_SEC 以上記録が途絶えたら「固まっている」と判断する。
# 60秒は、discord.py 自身が接続を死んだと判断する既定の待ち時間
# （discord/state.py の heartbeat_timeout = 60.0）に合わせた。
LOOP_TICK_SEC = 5
LOOP_STALL_SEC = 60


class SetupError(Exception):
    """設定の誤りなど、利用者が直せる原因。メッセージはそのまま見せてよい
    （秘密情報や ID を含めないこと）"""


def describe_error(e):
    """例外を、Discord や状態表示に出せる短い説明にする。

    例外の本文にはカレンダーID（メールアドレス）などが入りうるので、
    素性の分かっているもの以外は種類の名前だけを出す。詳細はログに残す。
    """
    if isinstance(e, SetupError):
        return str(e)
    if isinstance(e, discord.Forbidden):
        return f"Discord の権限が足りません（HTTP {e.status} / code {e.code}）"
    if isinstance(e, discord.HTTPException):
        return f"Discord がエラーを返しました（HTTP {e.status} / code {e.code}）"
    if isinstance(e, TimeoutError):
        return "応答がなく時間切れになりました"
    return type(e).__name__


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
    http = _new_http()                 # この呼び出し専用の接続（他のスレッドと共有しない）
    for _ in range(API_MAX_PAGES):
        result = service.events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            maxResults=API_PAGE_SIZE,
            singleEvents=True,
            orderBy="startTime",
            pageToken=page_token,
        ).execute(http=http)
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
    # tasks.loop は、通信系以外の例外が1度でも出るとループごと止まり、以後二度と
    # 実行されない（discord/ext/tasks の _loop で確認）。そうなると自動表示は
    # 古いまま残り、ボットはオンラインのまま「動いているように見える」。
    # ここで受け止めて記録し、次の周期でやり直す。
    try:
        await _update_calendar_once()
    except Exception as e:
        health["display_error"] = (datetime.now(JST), describe_error(e))
        print("自動表示の更新でエラーが発生しました。次の周期でやり直します。")
        traceback.print_exception(type(e), e, e.__traceback__)


async def _update_calendar_once():
    # Google への問い合わせは別スレッドで行う。応答を待つ間も、
    # コマンドへの返信などボット本体の処理が止まらないようにするため
    events, failures = await asyncio.to_thread(fetch_events)
    if failures:
        # 一部のカレンダーが読めないまま書き換えると、予定が抜けた表示になってしまう。
        # 前回の正しい表示を残し、次の周期でやり直す
        print(f"一部のカレンダーが読めないため、自動表示の更新を見送りました: {failures}")
        health["display_error"] = (
            datetime.now(JST),
            f"{len(failures)}件のカレンダーを読み取れないため、更新を見送りました")
        return
    today = datetime.now(JST).date().isoformat()      # 今日の日付（日本時間）
    signature = str(today) + str([
        (e.get("summary"), e.get("start"), e.get("end"), e.get("updated"))
        for e in events
    ])
    if signature == state["signature"]:
        health["display_ok_at"] = datetime.now(JST)   # 変化なしを確認できた
        health["display_error"] = None
        return

    channel = client.get_channel(CHANNEL_ID)
    if channel is None:
        # 以前はここで None のまま send を呼び、AttributeError でループが止まっていた
        print(f"CHANNEL_ID（{CHANNEL_ID}）のチャンネルが見つかりません")
        raise SetupError(
            "自動表示の送り先チャンネルが見つかりません。CHANNEL_ID が正しいか、"
            "ボットがそのチャンネルを見られるか確認してください")

    embeds = build_embeds(events)
    errors = []

    if not state["messages"]:
        for embed in embeds:
            try:
                msg = await channel.send(embed=embed)
                state["messages"].append(msg)
                await asyncio.sleep(1) # ★1秒待つ（スロットリング）
            except discord.HTTPException as e:
                print(f"送信エラー: {e}")
                errors.append(e)
                await asyncio.sleep(5) # エラーが出たら長めに待つ
    else:
        for msg, embed in zip(state["messages"], embeds):
            try:
                await msg.edit(embed=embed)
                await asyncio.sleep(1) # ★1秒待つ（スロットリング）
            except discord.HTTPException as e:
                print(f"編集エラー: {e}")
                errors.append(e)
                await asyncio.sleep(5) # エラーが出たら長めに待つ

    if errors:
        # 失敗したのに「反映済み」と覚えると、次の周期で内容が同じだとして
        # やり直さなくなる。覚えを消して、次の周期で必ず再送させる
        state["signature"] = None
        health["display_error"] = (
            datetime.now(JST),
            f"{len(errors)}件の送信・編集に失敗しました（{describe_error(errors[0])}）")
    else:
        state["signature"] = signature
        health["display_ok_at"] = datetime.now(JST)
        health["display_error"] = None

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


# ===== ⑧-E コマンド一覧のテキスト ★追加 =====
HELP_TEXT = (
    "**📅 コマンド一覧**\n"
    "```\n"
    "■ この一覧を出す\n"
    "cmds           /cmds でも同じ（/ は付けなくてよい）\n"
    "\n"
    "■ 動作確認（反応がおかしいとき）\n"
    "ping           ボットの状態と、直近の失敗を表示\n"
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
def _is_command_text(text):
    """このボット宛てのコマンドらしい文字列か（ログと成功記録の判定に使う）"""
    return text.startswith("/") or text.lower() in ("cmds", "ping")


async def _handle_message(message):
    """コマンドを解釈して返信する。

    返信したら True を返す（このボット宛てでなく何もしなかったときは None）。
    失敗は例外のまま on_message に返し、そこで知らせる。
    """
    text = message.content.strip()

    # 受け取ったことをログに残す。返信が無いとき、「そもそも届いていない」のか
    # 「届いたが返信に失敗した」のかを、ログにこの行があるかで見分けられる
    if _is_command_text(text):
        where = getattr(message.channel, "name", None) or "DM"
        print(f"受信: {text[:100]!r}（#{where}）")

    # --- ping ／ /ping : 動作確認と状態表示 ---
    # Google に一切触らず、平文を返すだけ。これが返ればボットは生きていて、
    # そのチャンネルに書き込める。あわせて、直近の失敗の記録を見せる
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
        # 「入力中…」を出して、受け付けたことを見せる。
        # 画像生成は重い処理なので、別スレッドで実行してボットを固めない。
        # 失敗は on_message でまとめて知らせる（ここで飲み込むと原因がログに残らない）
        async with message.channel.typing():
            buf, failures = await asyncio.to_thread(render_month_image, year, month)
        file = discord.File(buf, filename=f"calendar_{year}_{month:02d}.png")
        await message.channel.send(
            f"📅 {year}年{month}月{failure_note(failures)}", file=file
        )
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
            free_days, failures = await _find_free_days_async(start_date, end_date, NIGHT_START, NIGHT_END)
            label = "夜が空いている日"
        elif mode == "a":
            day_free, day_fail = await _find_free_days_async(start_date, end_date, DAY_START, DAY_END)
            night_free, night_fail = await _find_free_days_async(start_date, end_date, NIGHT_START, NIGHT_END)
            free_days = sorted(set(day_free) & set(night_free))
            failures = merge_failures(day_fail, night_fail)
            label = "一日空いている日"
        elif mode == "u":                  # 昼か夜のどちらか（あるいは両方）が空いている日
            day_free, day_fail = await _find_free_days_async(start_date, end_date, DAY_START, DAY_END)
            night_free, night_fail = await _find_free_days_async(start_date, end_date, NIGHT_START, NIGHT_END)
            free_days = sorted(set(day_free) | set(night_free))   # どちらかに含まれる日
            failures = merge_failures(day_fail, night_fail)
            label = "昼か夜が空いている日"
        else:
            free_days, failures = await _find_free_days_async(start_date, end_date, DAY_START, DAY_END)
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

async def _find_free_days_async(start_date, end_date, slot_start, slot_end):
    """find_free_days を別スレッドで実行する（イベントループを止めないため）"""
    return await asyncio.to_thread(
        find_free_days, start_date, end_date, slot_start, slot_end)


@client.event
async def on_message(message):
    if message.author.bot:
        return
    # discord.py は、ここで起きた例外をログに出すだけで握りつぶす
    # （discord/client.py の _run_event）。利用者から見ると無反応になり、
    # 動いていないのに動いて見える。失敗はすべてここで受け止めて知らせる
    try:
        replied = await _handle_message(message)
    except discord.Forbidden as e:
        await _report_forbidden(message, e)
        return
    except Exception as e:
        await _report_command_error(message, e)
        return
    # 実際に返信できたら、前回のコマンドの失敗の記録は消す。
    # このボット宛てでない文字列（/hello など）では消さない（何も確かめていないため）。
    # ping では消さない（ping は記録を見せるためのもので、見せた途端に消えると困る）
    if replied and message.content.strip().lower() not in ("ping", "/ping"):
        health["command_error"] = None


async def _report_forbidden(message, e):
    """返信する権限がないとき。そのチャンネルには書けないので、ログと本人への DM で知らせる"""
    where = getattr(message.channel, "name", None) or "DM"
    health["command_error"] = (datetime.now(JST), f"#{where} に返信する権限がありません")
    print(f"#{where} に返信できません。ボットの権限が足りません"
          f"（HTTP {e.status} / code {e.code}: {e.text}）。"
          "「チャンネルを見る」「メッセージを送信」「埋め込みリンク」「ファイルを添付」を確認してください")
    try:
        await message.author.send(
            f"⚠️ #{where} ではボットに書き込む権限がないため、返信できませんでした。\n"
            "サーバーの管理者に、このボットへ「メッセージを送信」「埋め込みリンク」"
            "「ファイルを添付」の権限を付けてもらってください。")
    except discord.HTTPException as dm_error:
        print(f"DM でも知らせられませんでした（HTTP {dm_error.status} / code {dm_error.code}）")


async def _report_command_error(message, e):
    """コマンドの処理中に想定外のエラーが出たとき。黙らず、その場で知らせる"""
    what = describe_error(e)
    health["command_error"] = (datetime.now(JST), what)
    print(f"コマンドの処理中にエラーが発生しました: {message.content.strip()[:100]!r}")
    traceback.print_exception(type(e), e, e.__traceback__)
    try:
        await message.channel.send(
            f"⚠️ 処理中にエラーが発生しました（{what}）。\n"
            "時間をおいても直らない場合は、`ping` で状態を確認するか、"
            "管理者にサーバーのログを見てもらってください。")
    except discord.HTTPException as send_error:
        print(f"エラーの通知も送れませんでした（HTTP {send_error.status} / code {send_error.code}）")


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

    # --- 自動表示 ---
    if health["display_error"]:
        at, what = health["display_error"]
        warnings.append(f"自動表示: {what}（{_fmt_time(at)}）")
    elif health["display_ok_at"] is None:
        lines.append("自動表示: まだ更新していません")
    else:
        lines.append(f"自動表示: 前回の更新は正常（{_fmt_time(health['display_ok_at'])}）")

    # --- コマンド ---
    if health["command_error"]:
        at, what = health["command_error"]
        warnings.append(f"コマンド: 直近で失敗しました — {what}（{_fmt_time(at)}）")

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
    # 再接続のたびに on_ready が呼ばれることがある。二重に start すると例外になる
    if not loop_heartbeat.is_running():
        loop_heartbeat.start()
    if not update_calendar.is_running():
        update_calendar.start()


# ===== ⑩ ボットを起動する =====
# Discord に一時的に締め出される（429）と、ログインは例外で終わる。
# 応答の Retry-After（あと何秒待てばよいか）が読めたら、その秒数＋10ミリ秒だけ
# 待って、1回だけログインし直す。読めないときや、再試行も失敗したときは、
# 締め出されていることが Render 上で分かるよう、エラーのまま終了させる。
RETRY_MARGIN_SEC = 0.010               # Retry-After に足す余裕（10ミリ秒）


def report_429(e):
    """締め出しの種類と解除までの時間を調べられるよう、Discord の応答ヘッダーを残す
    （Retry-After、X-RateLimit-Global / X-RateLimit-Scope、Via、CF-Ray など）。
    出すのは応答ヘッダーだけ。トークンを含むリクエスト側は出さない。Cookie も省く"""
    headers = getattr(getattr(e, "response", None), "headers", None) or {}
    print("429 の応答ヘッダー:")
    for name, value in headers.items():
        if name.lower() != "set-cookie":
            print(f"  {name}: {value}")


def retry_after_seconds(e):
    """Retry-After を秒数として読む。無い・数字でない（日時の形など）ときは None"""
    headers = getattr(getattr(e, "response", None), "headers", None) or {}
    try:
        seconds = float(headers.get("Retry-After", ""))
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


try:
    client.run(DISCORD_BOT_TOKEN)
    wait = None
except discord.HTTPException as e:
    if e.status != 429:
        raise
    print("Discord に一時的に締め出されています（429）。")
    report_429(e)
    wait = retry_after_seconds(e)
    if wait is None:
        print("Retry-After を読み取れなかったため、再試行せずに終了します。"
              "1時間ほど待ってから、Render で再デプロイまたは再起動してください。")
        raise

if wait is not None:
    wait += RETRY_MARGIN_SEC
    print(f"Retry-After に従い {wait:.3f} 秒待ってから、1回だけログインし直します。")
    time.sleep(wait)
    client.clear()                     # 一度閉じたボットを、もう一度ログインできる状態に戻す
    # 1回目の接続部品（コネクタ）は閉じていて、前のイベントループにも結びついている。
    # clear() では戻らないので捨て、ログイン時に新しく作らせる
    client.http.connector = discord.utils.MISSING
    try:
        client.run(DISCORD_BOT_TOKEN, log_handler=None)   # ログの設定は1回目で済んでいる
    except discord.HTTPException as e:
        if e.status == 429:
            print("再試行でも締め出されていました（429）。これ以上は再試行しません。"
                  "時間をおいてから、Render で再デプロイまたは再起動してください。")
            report_429(e)
        raise
