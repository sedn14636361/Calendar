# ===== あわせ暦（日程あわせのページ /m） =====
# ボットのいないサーバーでも使えるように、URL を開くだけで参加できる日程調整のページ。
# 伝助のように、日ごとの「昼（10〜18時）」「夜（18〜24時）」に ○△✕ を付けて、
# 全員の都合をカレンダーの形で俯瞰する。
#
#   ・大雑把入力：日ごとの昼・夜に、自分で ○△✕ を付ける
#   ・詳細入力　：予定の時間（ICS・CSV・テキスト・手入力）から ○✕ を自動で決める。
#                 予定が少しでも重なれば ✕（ボットの /2026-9 と同じ判定）。△ は自動では付けず、
#                 本人がマスを選んで付ける（可否が分からないときに使う）
#   ・配色　　　：シナリオ名から「ことばパレット」（Cloudflare Workers AI）で5色を作る。
#                 作れなかったときは、シナリオ名から計算で伝統色を選ぶ
#   ・保存先　　：Google スプレッドシート（ボットの Google の鍵をそのまま使う）。
#                 期間の最終日から 90 日たった調整は自動で消す
#
# 安全のための決まり：
#   ・予定の名前はサーバーに送らない（画面の中で時間だけにしてから送る）
#   ・編集できるのは、その参加者を追加したブラウザ（編集の鍵を持つ人）と、調整を作った人だけ。
#     鍵はサーバーには SHA-256 の値でしか残さない
#   ・作る・参加するの回数に上限を設ける（短い時間に大量に作られないように）
#
# このファイルは Discord を使わない。Calendar.py が Web サーバーの /m の下をここへ回す。

import os
import re
import io
import json
import html
import math
import time
import hashlib
import secrets
import threading
import urllib.request
import urllib.error
import calendar as _calendar
from collections import deque
from datetime import datetime, date, timedelta, timezone

JST = timezone(timedelta(hours=9))
MATCHER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "matcher")

# 時間帯（分）。ボットの DAY_START〜DAY_END（10〜18時）・NIGHT_START〜NIGHT_END（18〜24時）と同じ
SLOTS = ("d", "n")                     # d＝昼、n＝夜
SLOT_RANGE = {"d": (10 * 60, 18 * 60), "n": (18 * 60, 24 * 60)}   # 時間帯を持たない、前からある日程あわせの昼・夜
DEFAULT_SLOTS = {"d": [10 * 60, 18 * 60], "n": [21 * 60, 24 * 60]}  # 新しく作るときの初期値（利用者の指定：昼 10〜18、夜 21〜24）


def slot_ranges(event):
    """その日程あわせの昼・夜の時間帯（分）。作るときに決め、あとから直せる"""
    sl = event.get("slots") if isinstance(event, dict) else None
    return {k: tuple(sl[k]) for k in SLOTS} if sl else dict(SLOT_RANGE)


def clean_slots(raw):
    """画面から来た時間帯 {"d": [開始, 終了], "n": [...]}（分。1時間きざみ）を確かめる"""
    if not isinstance(raw, dict):
        raise MatcherError("時間帯の形が正しくありません")
    out = {}
    for k, name in (("d", "昼"), ("n", "夜")):
        v = raw.get(k)
        if not (isinstance(v, list) and len(v) == 2 and all(type(x) is int and x % 60 == 0 and 0 <= x <= 1440 for x in v)):
            raise MatcherError(f"{name}の時間帯の形が正しくありません")
        if v[0] >= v[1]:
            raise MatcherError(f"{name}の終わりは、始まりより後にしてください")
        out[k] = [v[0], v[1]]
    return out
MARKS = ("o", "t", "x")                # ○・△・✕
MODES = ("rough", "detail")            # 大雑把入力・詳細入力

# 文字数・日数の上限は、スプレッドシートの1マス（50,000 文字）に収めるためだけのもの。画面には書かない（利用者の指定）
MAX_DAYS = 120                         # 1つの調整の候補日の数（作るときの期間も、あとから足した日も含めて）
MAX_PEOPLE = 50
MAX_TITLE = 100
MAX_DESC = 2000                        # 概要
MAX_MEMO = 300                         # 参加者ごとのメモ
MAX_NAME = 30
MAX_INTERVALS_PER_DAY = 48
MAX_RECORD_CHARS = 45000               # スプレッドシートの1マスは 50,000 文字まで。余裕を持たせる
MAX_BODY = 192 * 1024                  # 画面から受け取る中身の上限
KEEP_DAYS = 90                         # 最終日からこの日数がたった調整は消す
BEST_LIMIT = 5

PALETTE_API = "https://kotoba-palette.senchaoishi.workers.dev/api/palette"
PALETTE_TIMEOUT = 25
PEOPLE_COLORS = ["#4FD1C5", "#FF7A64", "#A78BFA", "#FBBF24", "#60A5FA", "#F472B6", "#8BC34A", "#F97316"]

_ID_RE = re.compile(r"[A-Za-z0-9_-]{6,32}")
_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


class MatcherError(Exception):
    """画面に返す、利用者向けの理由"""
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _log(what):
    print(f"あわせ暦: {what}")


def today_jst():
    return datetime.now(JST).date()


# ---------- 日付 ----------
def parse_iso_day(s):
    if not isinstance(s, str) or not _DAY_RE.fullmatch(s):
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def parse_day_input(s, today=None):
    """人が打つ日付を読む。2026-12-1・2026/12/1・12/1（年は今年。過ぎていれば来年）。読めなければ None"""
    today = today or today_jst()
    t = (s or "").strip().translate(str.maketrans("０１２３４５６７８９／－", "0123456789/-"))
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", t)
    try:
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})", t)
        if m:
            d = date(today.year, int(m.group(1)), int(m.group(2)))
            return d if d >= today - timedelta(days=31) else date(today.year + 1, d.month, d.day)
    except ValueError:
        return None
    return None


def check_period(first, last, today=None):
    """期間を確かめる。誤りがあれば MatcherError"""
    today = today or today_jst()
    if first is None or last is None:
        raise MatcherError("開始日と終了日を正しく入れてください")
    if last < first:
        raise MatcherError("終了日が開始日より前になっています")
    if (last - first).days + 1 > MAX_DAYS:
        raise MatcherError("期間が長すぎます。短くしてください")
    if last < today:
        raise MatcherError("終了日が過ぎています")
    if first > today + timedelta(days=730):
        raise MatcherError("開始日は2年以内にしてください")


def period_days(event):
    """候補日の一覧（"YYYY-MM-DD" の昇順）。あとから日を足した調整は days に持つ。無ければ first〜last の毎日"""
    if event.get("days"):
        return list(event["days"])
    first = date.fromisoformat(event["first"])
    last = date.fromisoformat(event["last"])
    return [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]


def clean_memo(s, limit, what):
    """空でもよい文章（概要・メモ）。改行は残し、制御文字は消す"""
    if s is None:
        return ""
    if not isinstance(s, str):
        raise MatcherError(f"{what}の形が正しくありません")
    s = re.sub(r"\r\n?", "\n", s)
    s = re.sub(r"[\x00-\x09\x0b-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]", "", s).strip()
    if len(s) > limit:
        raise MatcherError(f"{what}が長すぎます。短くしてください")
    return s


# ---------- 文字の検査 ----------
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f​-‏‪-‮⁦-⁩]")


def clean_text(s, limit, what):
    if not isinstance(s, str):
        raise MatcherError(f"{what}を入れてください")
    s = _CTRL_RE.sub("", s).strip()
    if not s:
        raise MatcherError(f"{what}を入れてください")
    if len(s) > limit:
        raise MatcherError(f"{what}が長すぎます。短くしてください")
    return s


# ---------- 色（ことばパレットの計算を Python に移したもの） ----------
def _rgb(h):
    n = int(h[1:], 16)
    return (n >> 16) & 255, (n >> 8) & 255, n & 255


def _lin(v):
    v /= 255
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def oklab(h):
    r, g, b = (_lin(v) for v in _rgb(h))
    l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
            1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
            0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s)


def luminance(h):
    r, g, b = (_lin(v) for v in _rgb(h))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    """WCAG のコントラスト比（1〜21）"""
    x, y = sorted((luminance(a), luminance(b)), reverse=True)
    return (x + 0.05) / (y + 0.05)


def text_on(h):
    L = luminance(h)
    return "#14161a" if (L + 0.05) / 0.05 > 1.05 / (L + 0.05) else "#ffffff"


def mix(a, b, t):
    A, B = _rgb(a), _rgb(b)
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(A, B))


def _load_names():
    try:
        with open(os.path.join(MATCHER_DIR, "colors.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        _log(f"色名の表を読めませんでした（{e!r}）")
        return {"白": "#ffffff", "黒": "#000000"}


NAMES = _load_names()                  # 伝統色名・JIS 慣用色名（ことばパレットと同じ 420 色）
_NAME_LAB = [(n, h, oklab(h)) for n, h in NAMES.items()]


def name_of(hex_):
    p = oklab(hex_)
    return min(_NAME_LAB, key=lambda e: sum((x - y) ** 2 for x, y in zip(p, e[2])))[0]


def _hue(lab):
    return math.degrees(math.atan2(lab[2], lab[1])) % 360


def _hue_diff(a, b):
    d = abs(a - b) % 360
    return min(d, 360 - d)


def calc_palette(title):
    """AI を使わない配色。シナリオ名の SHA-256 から、伝統色の表の中で5色を選ぶ（同じ名前なら毎回同じ）"""
    h = hashlib.sha256(title.encode("utf-8")).digest()

    def pick(items, i):
        return items[int.from_bytes(h[i * 4:i * 4 + 4], "big") % len(items)]

    entries = [(n, hx, lab, math.hypot(lab[1], lab[2]), _hue(lab)) for n, hx, lab in _NAME_LAB]
    vivid = [e for e in entries if e[3] >= 0.07 and 0.45 <= e[2][0] <= 0.85]
    pale = [e for e in entries if e[2][0] >= 0.9 and e[3] <= 0.05]     # 背景は淡く、色みの弱いもの
    dark = [e for e in entries if e[2][0] <= 0.38]
    main = pick(vivid, 0)
    near = [e for e in vivid if 25 <= _hue_diff(e[4], main[4]) <= 70] or vivid
    second = pick(near, 1)
    far = [e for e in vivid if _hue_diff(e[4], main[4]) >= 120] or vivid
    accent = pick(far, 2)
    pale = sorted(pale, key=lambda e: (_hue_diff(e[4], main[4]) if e[3] > 0.01 else 90, e[1]))[:8]
    dark = sorted(dark, key=lambda e: (_hue_diff(e[4], main[4]) if e[3] > 0.02 else 90, e[1]))[:8]
    picked = [main, second, accent, pick(pale, 3), pick(dark, 4)]
    return [e[1] for e in picked]


def fetch_ai_palette(title):
    """ことばパレットの API にシナリオ名を送り、5色（#rrggbb）と理由を返す。だめなら None"""
    data = json.dumps({"sentence": title, "fresh": False}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(PALETTE_API, data=data, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "calendar-bot-matcher"})
    try:
        with urllib.request.urlopen(req, timeout=PALETTE_TIMEOUT) as r:
            body = json.loads(r.read(65536).decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        _log(f"ことばパレットで配色を作れませんでした（{type(e).__name__}）")
        return None
    hexes = []
    colors = body.get("colors") if isinstance(body, dict) else None
    for c in colors if isinstance(colors, list) else []:
        hx = str(c if isinstance(c, str) else (c or {}).get("hex", "")).strip().lower()
        if re.fullmatch(r"#[0-9a-f]{6}", hx):
            hexes.append(hx)
    if len(hexes) < 5:
        _log("ことばパレットの答えに5色そろっていませんでした")
        return None
    return hexes[:5], str(body.get("reason") or "")[:160]


def make_palette(title, use_ai=True):
    got = fetch_ai_palette(title) if use_ai else None
    if got:
        hexes, reason = got
        source = "ai"
    else:
        hexes, reason, source = calc_palette(title), "シナリオ名から計算で選んだ伝統色です", "calc"
    return {"colors": [{"hex": h, "name": name_of(h)} for h in hexes], "reason": reason, "source": source}


def theme_assign(colors):
    """ことばパレットの themeAssign と同じ割り当て。5色を地・文字・ボタン・ホバー・飾りに振り分ける"""
    hexes = list(dict.fromkeys(c["hex"] for c in colors))
    bg = colors[3]["hex"]
    pool = [h for h in hexes if h != bg]
    cand = list(dict.fromkeys([colors[4]["hex"]] + pool))
    ink_pick = next((h for h in cand if h != bg and contrast(bg, h) >= 4.5), None)
    ink = ink_pick or ("#000000" if contrast(bg, "#000000") >= contrast(bg, "#ffffff") else "#ffffff")
    rest1 = [h for h in pool if h != ink]
    order = [h for h in (colors[0]["hex"], colors[4]["hex"], colors[1]["hex"], colors[2]["hex"]) if h in rest1]
    btn = next((h for h in order if contrast(h, bg) >= 3), None) \
        or (sorted(rest1, key=lambda h: -contrast(bg, h))[0] if rest1 else ink)
    rest = [h for h in rest1 if h != btn]
    go = rest[0] if rest else btn
    hov = rest[1] if len(rest) > 1 else go
    deco = rest[2] if len(rest) > 2 else go
    return {"bg": bg, "ink": ink, "btn": btn, "goHover": go, "hovHover": hov, "deco": deco}


HEAT_STEPS = (0, 0.18, 0.36, 0.54, 0.72, 0.9)


def build_theme(palette):
    """画面と画像で使う色をまとめて決める（CSS の変数名で返す）"""
    t = theme_assign(palette["colors"])
    bg, ink = t["bg"], t["ink"]
    dark = contrast(bg, "#000000") < contrast(bg, "#ffffff")
    sheet = mix(bg, "#ffffff", 0.06 if dark else 0.55)
    ink_soft = mix(ink, bg, 0.3)
    if min(contrast(ink_soft, bg), contrast(ink_soft, sheet)) < 4.5:
        ink_soft = ink                 # 薄めた文字色が読みにくくなるなら、薄めない

    def edge(h):
        return mix(ink, bg, 0.3) if contrast(h, bg) < 1.5 else h

    def readable(h, fallback):
        return h if contrast(h, sheet) >= 3 else fallback

    heat = [mix(sheet, t["btn"], s) for s in HEAT_STEPS]
    return {
        "--paper": bg, "--sheet": sheet, "--ink": ink, "--ink-soft": ink_soft, "--rule": mix(ink, bg, 0.8),
        "--ai": t["btn"], "--ai-ink": text_on(t["btn"]), "--ai-edge": edge(t["btn"]),
        "--second": t["goHover"], "--second-ink": text_on(t["goHover"]), "--second-edge": edge(t["goHover"]),
        "--accent": t["hovHover"], "--accent-ink": text_on(t["hovHover"]), "--accent-edge": edge(t["hovHover"]),
        "--deco": t["deco"],
        "--ok": readable(t["btn"], ink), "--maybe": readable(t["goHover"], ink_soft), "--ng": ink_soft,
        **{f"--heat{i}": c for i, c in enumerate(heat)},
        **{f"--heat-ink{i}": text_on(c) for i, c in enumerate(heat)},
        "color-scheme": "dark" if dark else "light",
    }


# ---------- ○△✕ の判定と集計 ----------
def busy_mark(intervals, slot, ranges=None):
    """その日の予定（分の区間）から、時間帯の ○✕ を決める。少しでも重なれば ✕（ボットの判定と同じ）"""
    s0, e0 = (ranges or SLOT_RANGE)[slot]
    return "x" if any(s < e0 and e > s0 for s, e in intervals) else "o"


def resolve_mark(person, day, slot, ranges=None):
    """本人が付けた印があればそれ、詳細入力で予定を共有済みなら自動の ○✕、どちらでもなければ None（未入力）"""
    m = person.get("marks", {}).get(day, {}).get(slot)
    if m:
        return m
    if person.get("mode") == "detail" and person.get("busy_set"):
        return busy_mark(person.get("busy", {}).get(day, []), slot, ranges)
    return None


def tally(event, people):
    """日・時間帯ごとの人数（o・t・x・-＝未入力）"""
    out = {}
    ranges = slot_ranges(event)
    for day in period_days(event):
        out[day] = {}
        for slot in SLOTS:
            c = {"o": 0, "t": 0, "x": 0, "-": 0}
            for p in people:
                c[resolve_mark(p, day, slot, ranges) or "-"] += 1
            out[day][slot] = c
    return out


def best_slots(event, table, limit=BEST_LIMIT):
    """候補の並べ方：✕（と未入力）の人が少ない順 → ○ の人が多い順 → 日付・昼夜の順。○も△もない枠は出さない"""
    rows = []
    for day in period_days(event):
        for i, slot in enumerate(SLOTS):
            c = table[day][slot]
            if c["o"] + c["t"] == 0:
                continue
            rows.append(((c["x"] + c["-"], -c["o"], day, i), {"day": day, "slot": slot, **c}))
    rows.sort(key=lambda r: r[0])
    return [r[1] for r in rows[:limit]]


# ---------- 保存先 ----------
class MemoryBackend:
    """テスト用。スプレッドシートと同じく「行番号・キー・JSON」で持つ"""
    def __init__(self):
        self.rows = {}
        self.next = 1

    def load(self):
        return [(r, k, j) for r, (k, j) in sorted(self.rows.items())]

    def append(self, key, data):
        row = self.next
        self.next += 1
        self.rows[row] = (key, data)
        return row

    def update(self, row, key, data):
        self.rows[row] = (key, data)

    def clear(self, row):
        self.rows.pop(row, None)


class SheetsBackend:
    """Google スプレッドシートの1つのシート（タブ）を、A列＝キー・B列＝JSON・C列＝更新時刻として使う。
    消すときは行を空にする（行番号がずれないように、行そのものは消さない）"""
    SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

    def __init__(self, sheet_id, service_account_info=None, tab="awasegoyomi", service=None):
        self.sheet_id = sheet_id
        self.tab = tab
        self._service = service
        self._info = service_account_info
        self.free, self.next_row, self.grid_id, self.row_count = set(), 1, 0, 1000

    def _svc(self):
        if self._service is None:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
            creds = service_account.Credentials.from_service_account_info(self._info, scopes=self.SCOPES)
            self._service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        return self._service

    def _ensure_tab(self):
        """シート（タブ）が無ければ作り、シートの番号と行数を覚える"""
        meta = self._svc().spreadsheets().get(
            spreadsheetId=self.sheet_id, fields="sheets.properties(title,sheetId,gridProperties.rowCount)").execute()
        props = next((s["properties"] for s in meta.get("sheets", []) if s["properties"].get("title") == self.tab), None)
        if props is None:
            res = self._svc().spreadsheets().batchUpdate(
                spreadsheetId=self.sheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": self.tab}}}]}).execute()
            props = res["replies"][0]["addSheet"]["properties"]
            _log(f"スプレッドシートにシート「{self.tab}」を作りました")
        self.grid_id = props.get("sheetId", 0)
        self.row_count = props.get("gridProperties", {}).get("rowCount", 1000)

    def load(self):
        self._ensure_tab()
        res = self._svc().spreadsheets().values().get(
            spreadsheetId=self.sheet_id, range=f"{self.tab}!A:B").execute()
        out = []
        values = res.get("values", [])
        self.free = set()                               # 空いている行（消した行）。新しい行はまずここに書く
        for i, row in enumerate(values, start=1):
            if len(row) >= 2 and row[0] and row[1]:
                out.append((i, row[0], row[1]))
            else:
                self.free.add(i)
        self.next_row = len(values) + 1                 # 使った行の次
        return out

    @staticmethod
    def _now():
        return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")

    def append(self, key, data):
        # values.append は使わない。途中に空いた行があると、そこに行を「差し込んで」後ろの行番号がずれ、
        # 覚えている行番号で別の人の行を上書きしてしまうため。書く行はここで決める
        # （ボットは1つのプロセスで動き、書き込みは Store の lock で1つずつなので、同じ行を2人に配ることはない）
        if self.free:
            row = min(self.free)
            self.free.discard(row)
        else:
            row = self.next_row
            self.next_row += 1
        try:
            if row > self.row_count:                    # シートの行が足りなければ 1000 行増やす
                self._svc().spreadsheets().batchUpdate(spreadsheetId=self.sheet_id, body={"requests": [
                    {"appendDimension": {"sheetId": self.grid_id, "dimension": "ROWS", "length": 1000}}]}).execute()
                self.row_count += 1000
            self.update(row, key, data)
        except Exception:
            self.free.add(row)                          # 書けなかった行は、次に使う
            raise
        return row

    def update(self, row, key, data):
        self._svc().spreadsheets().values().update(
            spreadsheetId=self.sheet_id, range=f"{self.tab}!A{row}:C{row}", valueInputOption="RAW",
            body={"values": [[key, data, self._now()]]}).execute()

    def clear(self, row):
        self._svc().spreadsheets().values().clear(
            spreadsheetId=self.sheet_id, range=f"{self.tab}!A{row}:C{row}", body={}).execute()
        self.free.add(row)


def _hash_key(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _key_ok(key, hashed):
    return isinstance(key, str) and 10 <= len(key) <= 100 and bool(hashed) \
        and secrets.compare_digest(_hash_key(key), hashed)


class Store:
    """調整と参加者を、メモリとスプレッドシートの両方に持つ。読むのはメモリ、書くときは両方。
    スプレッドシートへの書き込みに失敗したら、メモリも変えない"""
    def __init__(self, backend):
        self.backend = backend
        self.lock = threading.RLock()
        self.events = {}              # id → {"row", "data"}
        self.people = {}              # 調整の id → {参加者の id → {"row", "data"}}
        self.ready = False
        self.error = None

    def load(self, today=None):
        with self.lock:
            try:
                rows = self.backend.load()
            except Exception as e:
                self.error = f"{type(e).__name__}"
                _log(f"スプレッドシートを読めませんでした（{e!r}）")
                raise
            self.events, self.people = {}, {}
            for row, key, raw in rows:
                try:
                    data = json.loads(raw)
                except ValueError:
                    continue
                if key.startswith("e:"):
                    self.events[key[2:]] = {"row": row, "data": data}
                elif key.startswith("p:") and key.count(":") == 2:
                    _, eid, pid = key.split(":")
                    self.people.setdefault(eid, {})[pid] = {"row": row, "data": data}
            self.ready, self.error = True, None
            removed = self.purge_expired(today)
            _log(f"読み込みました（調整 {len(self.events)} 件" + (f"、期限切れを {removed} 件削除" if removed else "") + "）")

    def _require(self):
        if not self.ready:
            raise MatcherError("準備中です。少し待ってから開き直してください", 503)

    @staticmethod
    def _dump(data):
        s = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        if len(s) > MAX_RECORD_CHARS:
            raise MatcherError("入力が多すぎて保存できません。予定を減らしてください", 413)
        return s

    def _write(self, entry, key, data):
        """entry が None なら新しい行に足す。戻り値は {"row", "data"}"""
        raw = self._dump(data)
        try:
            if entry is None:
                row = self.backend.append(key, raw)
            else:
                row = entry["row"]
                self.backend.update(row, key, raw)
        except MatcherError:
            raise
        except Exception as e:
            _log(f"保存に失敗しました（{e!r}）")
            raise MatcherError("保存できませんでした。少し待ってからもう一度試してください", 502)
        return {"row": row, "data": data}

    # --- 調整 ---
    def create_event(self, title, first, last, palette, now=None, description="", slots=None):
        with self.lock:
            self._require()
            eid = secrets.token_urlsafe(9)
            while eid in self.events:
                eid = secrets.token_urlsafe(9)
            admin = secrets.token_urlsafe(24)
            data = {"id": eid, "title": title, "description": description,
                    "slots": slots or json.loads(json.dumps(DEFAULT_SLOTS)),
                    "first": first.isoformat(), "last": last.isoformat(),
                    "palette": palette, "admin": _hash_key(admin),
                    "created": (now or datetime.now(JST)).isoformat(timespec="seconds")}
            self.events[eid] = self._write(None, "e:" + eid, data)
            self.people[eid] = {}
            return data, admin

    def get(self, eid):
        with self.lock:
            self._require()
            e = self.events.get(eid) if isinstance(eid, str) else None
            if e is None:
                raise MatcherError("この調整は見つかりません（削除されたか、URL が違います）", 404)
            people = sorted((p["data"] for p in self.people.get(eid, {}).values()), key=lambda d: d["joined"])
            return json.loads(json.dumps(e["data"])), json.loads(json.dumps(people))

    def is_admin(self, eid, admin):
        with self.lock:
            e = self.events.get(eid)
            return bool(e) and _key_ok(admin, e["data"]["admin"])

    def edit_event(self, eid, changes, admin=None, today=None, now=None):
        """タイトル・概要・候補日を直す。誰でもできる（利用者の指定）。候補日を消すのは作った人だけ"""
        today = today or today_jst()
        with self.lock:
            self._require()
            entry = self.events.get(eid)
            if entry is None:
                raise MatcherError("この調整は見つかりません（削除されたか、URL が違います）", 404)
            data = json.loads(json.dumps(entry["data"]))
            if "title" in changes:
                data["title"] = clean_text(changes["title"], MAX_TITLE, "タイトル")
            if "description" in changes:
                data["description"] = clean_memo(changes["description"], MAX_DESC, "概要")
            if "slots" in changes:                     # 昼・夜の時間帯（詳細入力の自動の ○✕ は、新しい時間帯で計算し直される）
                data["slots"] = clean_slots(changes["slots"])
            days = set(period_days(data))
            add, remove = changes.get("add_days") or [], changes.get("remove_days") or []
            if not isinstance(add, list) or not isinstance(remove, list):
                raise MatcherError("日付の形が正しくありません")
            if remove and not self.is_admin(eid, admin):
                raise MatcherError("候補日を消せるのは、作った人だけです", 403)
            for s_ in add:
                d = parse_iso_day(s_)
                if d is None:
                    raise MatcherError("日付の形が正しくありません")
                if d < today or d > today + timedelta(days=730):
                    raise MatcherError("今日より前の日と、2年より先の日は足せません")
                days.add(d.isoformat())
            for s_ in remove:
                if isinstance(s_, str):
                    days.discard(s_)
            if not days:
                raise MatcherError("候補日を全部消すことはできません")
            if len(days) > MAX_DAYS:
                raise MatcherError("候補日が多すぎます")
            data["days"] = sorted(days)
            data["first"], data["last"] = data["days"][0], data["days"][-1]
            data["edited"] = (now or datetime.now(JST)).isoformat(timespec="seconds")
            self.events[eid] = self._write(entry, "e:" + eid, data)
            return data

    def delete_event(self, eid, admin):
        with self.lock:
            self._require()
            if not self.is_admin(eid, admin):
                raise MatcherError("この調整を消せるのは、作った人だけです", 403)
            self._remove_event(eid)

    def _remove_event(self, eid):
        for p in list(self.people.get(eid, {}).values()):
            self.backend.clear(p["row"])
        self.backend.clear(self.events[eid]["row"])
        self.people.pop(eid, None)
        self.events.pop(eid, None)

    def purge_expired(self, today=None):
        today = today or today_jst()
        removed = 0
        with self.lock:
            for eid, e in list(self.events.items()):
                try:
                    last = date.fromisoformat(e["data"]["last"])
                except (KeyError, ValueError):
                    continue
                if last + timedelta(days=KEEP_DAYS) < today:
                    try:
                        self._remove_event(eid)
                        removed += 1
                    except Exception as ex:
                        _log(f"期限切れの調整を消せませんでした（{ex!r}）")
        return removed

    # --- 参加者 ---
    def join(self, eid, name, mode, now=None):
        with self.lock:
            self._require()
            event, people = self.get(eid)
            if len(people) >= MAX_PEOPLE:
                raise MatcherError(f"参加できるのは {MAX_PEOPLE} 人までです", 409)
            used = {p.get("color") for p in people}
            color = next((c for c in PEOPLE_COLORS if c not in used), PEOPLE_COLORS[len(people) % len(PEOPLE_COLORS)])
            pid = secrets.token_urlsafe(6)
            while pid in self.people.get(eid, {}):
                pid = secrets.token_urlsafe(6)
            key = secrets.token_urlsafe(24)
            data = {"id": pid, "name": name, "mode": mode, "color": color, "memo": "", "marks": {}, "busy": {},
                    "busy_set": False, "key": _hash_key(key),
                    "joined": (now or datetime.now(JST)).isoformat(timespec="microseconds"),
                    "updated": (now or datetime.now(JST)).isoformat(timespec="seconds")}
            self.people.setdefault(eid, {})[pid] = self._write(None, f"p:{eid}:{pid}", data)
            return data, key

    def _person_entry(self, eid, pid):
        e = self.people.get(eid, {}).get(pid) if isinstance(pid, str) else None
        if e is None:
            raise MatcherError("この参加者は見つかりません（削除された可能性があります）", 404)
        return e

    def _can_edit(self, eid, entry, key, admin):
        return _key_ok(key, entry["data"].get("key")) or (admin and self.is_admin(eid, admin))

    def save(self, eid, pid, key, changes, admin=None, now=None):
        with self.lock:
            self._require()
            self.get(eid)
            entry = self._person_entry(eid, pid)
            if not self._can_edit(eid, entry, key, admin):
                raise MatcherError("この参加者を編集できるのは、追加したブラウザだけです", 403)
            data = dict(entry["data"])
            data.update(changes)
            data["updated"] = (now or datetime.now(JST)).isoformat(timespec="seconds")
            self.people[eid][pid] = self._write(entry, f"p:{eid}:{pid}", data)
            return data

    def delete_person(self, eid, pid, key=None, admin=None):
        with self.lock:
            self._require()
            self.get(eid)
            entry = self._person_entry(eid, pid)
            if not self._can_edit(eid, entry, key, admin):
                raise MatcherError("この参加者を消せるのは、追加したブラウザと調整を作った人だけです", 403)
            try:
                self.backend.clear(entry["row"])
            except Exception as e:
                _log(f"削除に失敗しました（{e!r}）")
                raise MatcherError("削除できませんでした。少し待ってからもう一度試してください", 502)
            self.people[eid].pop(pid, None)


# ---------- 送られた入力の検査 ----------
def validate_changes(event, body):
    """保存の中身（名前・メモ・入力のしかた・印・予定の時間）を確かめて、保存する形にする。
    候補日でない日の印・予定は黙って捨てる（作った人が候補日を消したあとに、画面の古い印が届くことがあるため）"""
    days = set(period_days(event))
    out = {}
    if "name" in body:
        out["name"] = clean_text(body["name"], MAX_NAME, "名前")
    if "memo" in body:
        out["memo"] = clean_memo(body["memo"], MAX_MEMO, "メモ")
    if "mode" in body:
        if body["mode"] not in MODES:
            raise MatcherError("入力のしかたが正しくありません")
        out["mode"] = body["mode"]
    if "marks" in body:
        marks = body["marks"]
        if not isinstance(marks, dict):
            raise MatcherError("印の形が正しくありません")
        clean = {}
        for day, slots in marks.items():
            if not isinstance(slots, dict):
                raise MatcherError("印の形が正しくありません")
            if day not in days:
                continue
            row = {s: m for s, m in slots.items() if s in SLOTS and m in MARKS}
            if len(row) != len(slots):
                raise MatcherError("印の形が正しくありません")
            if row:
                clean[day] = row
        out["marks"] = clean
    if "busy" in body:
        busy = body["busy"]
        if not isinstance(busy, dict):
            raise MatcherError("予定の形が正しくありません")
        clean = {}
        for day, ivs in busy.items():
            if not isinstance(ivs, list) or len(ivs) > MAX_INTERVALS_PER_DAY:
                raise MatcherError("予定の形が正しくないか、多すぎる予定が含まれています")
            if day not in days:
                continue
            got = []
            for iv in ivs:
                if not (isinstance(iv, list) and len(iv) == 2 and all(type(v) is int for v in iv)
                        and 0 <= iv[0] < iv[1] <= 1440):
                    raise MatcherError("予定の時間の形が正しくありません")
                got.append(iv)
            got.sort()
            merged = []
            for s, e in got:                          # 重なる区間はまとめる
                if merged and s <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], e)
                else:
                    merged.append([s, e])
            if merged:
                clean[day] = merged
        out["busy"] = clean
        out["busy_set"] = True
    if "fixed" in body:                                # プルダウンで個別に選んだ枠（まとめて選択で変えない）
        fixed = body["fixed"]
        if not isinstance(fixed, dict):
            raise MatcherError("印の形が正しくありません")
        clean = {}
        for day, slots in fixed.items():
            if not isinstance(slots, dict) or any(k not in SLOTS or v is not True for k, v in slots.items()):
                raise MatcherError("印の形が正しくありません")
            if day in days and slots:
                clean[day] = dict(slots)
        out["fixed"] = clean
    return out


# ---------- 回数の上限 ----------
class RateLimiter:
    def __init__(self):
        self.hits = {}
        self.lock = threading.Lock()

    def allow(self, name, who, limit, window, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            q = self.hits.setdefault((name, who), deque())
            while q and q[0] <= now - window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            if len(self.hits) > 20000:                 # 古い記録をため込まない
                for k in [k for k, v in self.hits.items() if not v]:
                    del self.hits[k]
            return True


LIMITS = {
    # 名前: (回数, 秒)。全体の上限は、ことばパレットの無料枠（1日におよそ 700 回分）を使い切らない量
    "create_ip": (10, 3600), "create_all": (200, 86400),
    "join_ip": (40, 3600), "write_ip": (900, 3600),
}


# ---------- 画面に返す形 ----------
def public_view(event, people, viewer_admin=False):
    table = tally(event, people)
    return {
        "ok": True,
        "event": {"id": event["id"], "title": event["title"], "description": event.get("description", ""),
                  "first": event["first"], "last": event["last"],
                  "days": period_days(event), "palette": event["palette"], "theme": build_theme(event["palette"])},
        "people": [{"id": p["id"], "name": p["name"], "mode": p["mode"], "color": p.get("color"), "memo": p.get("memo", ""),
                    "marks": p.get("marks", {}), "fixed": p.get("fixed", {}),
                    "busy": p.get("busy", {}) if p["mode"] == "detail" else {},
                    "busy_set": bool(p.get("busy_set")), "updated": p.get("updated")} for p in people],
        "tally": table, "best": best_slots(event, table), "admin": bool(viewer_admin),
        "slots": {s: list(r) for s, r in slot_ranges(event).items()},
    }


# ---------- 画像（PNG。Discord のプレビューと「画像を保存」で使う） ----------
_JP_FONTS = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
]
_LATIN_FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "DejaVuSans.ttf",
]


def _font(size, jp=False):
    from PIL import ImageFont
    for path in (_JP_FONTS if jp else []) + _LATIN_FONTS:
        try:
            return ImageFont.truetype(path, size), path in _JP_FONTS
        except OSError:
            continue
    return ImageFont.load_default(), False


def _hex_rgb(h, a=255):
    return (*_rgb(h), a)


def _draw_mark(draw, kind, cx, cy, r, color, width=3):
    """○△✕★ を図形で描く（フォントに頼らない）"""
    if kind == "o":
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=width)
    elif kind == "t":
        draw.polygon([(cx, cy - r), (cx + r * 0.95, cy + r * 0.75), (cx - r * 0.95, cy + r * 0.75)],
                     outline=color, width=width)
    elif kind == "x":
        draw.line([cx - r, cy - r, cx + r, cy + r], fill=color, width=width)
        draw.line([cx - r, cy + r, cx + r, cy - r], fill=color, width=width)
    elif kind == "star":
        pts = []
        for i in range(10):
            ang = -math.pi / 2 + i * math.pi / 5
            rr = r if i % 2 == 0 else r * 0.45
            pts.append((cx + rr * math.cos(ang), cy + rr * math.sin(ang)))
        draw.polygon(pts, fill=color)


def heat_level(c, n=None):
    """枠の濃さ（0〜5）。利用者の指定：
       ・1人でも ✕ がいる枠、まだ誰も入力していない枠は白（0）
       ・入力した全員が ○ の枠はいちばん濃い（5）
       ・○ と △ だけの枠は、入力した人数に対する ○ の割合で 1〜4（全員 △ が 1、○ が多いほど濃い）
       未入力の人は分母に入れない（入力した人の中での割合）"""
    entered = c["o"] + c["t"] + c["x"]
    if c["x"] > 0 or entered == 0:
        return 0
    if c["o"] == entered:
        return 5
    return 1 + min(3, int(c["o"] / entered * 4))


def render_png(event, people):
    from PIL import Image, ImageDraw
    theme = build_theme(event["palette"])
    table = tally(event, people)
    n = len(people)
    days = set(period_days(event))
    first = date.fromisoformat(event["first"])
    last = date.fromisoformat(event["last"])
    # 候補日のある月だけ（あとから離れた日を足しても、間の空の月は描かない）
    months = sorted({(int(k[:4]), int(k[5:7])) for k in days})

    CELL, PAD, TOP, HEAD, BAR, GAP = 120, 36, 24, 64, 30, 28
    title_font, has_jp = _font(34, jp=True)
    f_month, _ = _font(36)
    f_small, _ = _font(16)
    f_day, _ = _font(20)
    W = CELL * 7 + PAD * 2
    title_h = 60 if has_jp else 0
    def weeks_of(yy, mm):                       # 期間の日を含む週だけを描く（画像を縦に長くしない）
        return [w for w in _calendar.Calendar(6).monthdayscalendar(yy, mm)
                if any(dn and date(yy, mm, dn).isoformat() in days for dn in w)]

    month_h = [HEAD + BAR + 6 + CELL * len(weeks_of(yy, mm)) for yy, mm in months]
    H = TOP + title_h + sum(month_h) + GAP * (len(months) - 1) + 70 + PAD
    paper, sheet, ink, soft, rule = (theme[k] for k in ("--paper", "--sheet", "--ink", "--ink-soft", "--rule"))
    img = Image.new("RGB", (W, H), _hex_rgb(paper)[:3])
    d = ImageDraw.Draw(img, "RGBA")
    d.rectangle([0, 0, W - 1, 9], fill=_hex_rgb(theme["--deco"]))
    yy0 = TOP
    if has_jp:
        d.text((PAD, yy0 + 8), event["title"][:30], font=title_font, fill=_hex_rgb(ink))
        yy0 += title_h
    for (yy, mm), mh in zip(months, month_h):
        d.text((PAD, yy0 + 10), f"{yy} / {mm:02d}", font=f_month, fill=_hex_rgb(ink))
        bar_top = yy0 + HEAD
        for c, wd in enumerate(("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT")):
            x0 = PAD + c * CELL
            col = theme["--accent"] if c in (0, 6) else rule
            d.rectangle([x0 + 1, bar_top, x0 + CELL - 1, bar_top + BAR], fill=_hex_rgb(col))
            d.text((x0 + CELL / 2, bar_top + BAR / 2), wd, font=f_small, anchor="mm",
                   fill=_hex_rgb(text_on(col)))
        grid_top = bar_top + BAR + 6
        for r, week in enumerate(weeks_of(yy, mm)):
            for c, dn in enumerate(week):
                x0, y0 = PAD + c * CELL, grid_top + r * CELL
                x1, y1 = x0 + CELL, y0 + CELL
                if dn == 0:
                    continue
                key = date(yy, mm, dn).isoformat()
                inside = key in days
                d.rectangle([x0, y0, x1, y1], fill=_hex_rgb(sheet if inside else paper),
                            outline=_hex_rgb(rule), width=2)
                d.text((x0 + 8, y0 + 4), str(dn), font=f_day,
                       fill=_hex_rgb(ink if inside else rule))
                if not inside:
                    continue
                half_top = y0 + 28
                half_h = (CELL - 30) / 2
                for i, slot in enumerate(SLOTS):
                    c_ = table[key][slot]
                    lv = heat_level(c_, n)
                    hy0 = half_top + i * half_h
                    fill = theme[f"--heat{lv}"]
                    # 昼と夜の間に余白を置かず、内訳（人数）も描かない（利用者の指定。色だけで俯瞰する）
                    d.rectangle([x0 + 3, hy0, x1 - 3, hy0 + half_h], fill=_hex_rgb(fill))
        yy0 += mh + GAP
    # 凡例：上＝昼、下＝夜
    ly = H - PAD - 34
    rg = slot_ranges(event)
    hh = lambda m: f"{m // 60}"
    d.text((PAD, ly), f"upper {hh(rg['d'][0])}-{hh(rg['d'][1])} / lower {hh(rg['n'][0])}-{hh(rg['n'][1])}",
           font=f_small, fill=_hex_rgb(soft))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ---------- Web（/m の下） ----------
class Matcher:
    """Calendar.py の Web サーバーから呼ばれる窓口"""
    def __init__(self, store=None, use_ai=True, public_url=""):
        self.store = store
        self.use_ai = use_ai
        self.public_url = public_url.rstrip("/")
        self.limiter = RateLimiter()
        self._img_cache = {}

    @property
    def enabled(self):
        return self.store is not None

    # --- 作る（Web とスラッシュコマンドの共用） ---
    def create(self, title, first, last, who="?", today=None, description="", slots=None):
        if not self.enabled:
            raise MatcherError("この機能の準備ができていません（MATCHER_SHEET_ID が未設定）", 503)
        title = clean_text(title, MAX_TITLE, "タイトル")
        description = clean_memo(description, MAX_DESC, "概要")
        slots = clean_slots(slots) if slots is not None else None
        check_period(first, last, today)
        self.store._require()
        if not self.limiter.allow("create_ip", who, *LIMITS["create_ip"]):
            raise MatcherError("短い時間に作りすぎです。しばらく待ってからもう一度試してください", 429)
        if not self.limiter.allow("create_all", "*", *LIMITS["create_all"]):
            raise MatcherError("今日はこれ以上作れません。明日もう一度試してください", 429)
        palette = make_palette(title, self.use_ai)    # 時間がかかるので、保存先の鍵（lock）の外で作る
        event, admin = self.store.create_event(title, first, last, palette, description=description, slots=slots)
        _log(f"調整を作りました（{(last - first).days + 1}日、配色：{palette['source']}）")
        return event, admin

    def url_for(self, eid, base=None):
        return f"{(base or self.public_url).rstrip('/')}/m/{eid}"

    # --- HTTP ---
    def handle(self, method, path, headers, body_bytes):
        """(状態コード, Content-Type, 中身, 追加のヘッダー) を返す"""
        sub = path[len("/m"):].strip("/")
        try:
            if method in ("GET", "HEAD"):
                return self._get(sub, headers)
            if method != "POST" or not sub.startswith("api/"):
                return 405, "text/plain; charset=utf-8", b"method not allowed\n", {}
            if len(body_bytes) > MAX_BODY:
                raise MatcherError("送られた中身が大きすぎます", 413)
            try:
                body = json.loads(body_bytes.decode("utf-8") or "{}")
            except (UnicodeDecodeError, ValueError):
                raise MatcherError("送られた中身が読めません")
            if not isinstance(body, dict):
                raise MatcherError("送られた中身が読めません")
            result = self._post(sub[4:], body, self._client_ip(headers))
            return 200, "application/json; charset=utf-8", _json(result), {}
        except MatcherError as e:
            return e.status, "application/json; charset=utf-8", _json({"ok": False, "error": str(e)}), {}
        except Exception as e:
            _log(f"エラー: {e!r}")
            return 500, "application/json; charset=utf-8", \
                _json({"ok": False, "error": f"処理中にエラーが発生しました（{type(e).__name__}）"}), {}

    @staticmethod
    def _client_ip(headers):
        # Render は X-Forwarded-For に接続元を入れる。先頭は偽れるが、ここでは回数の目安にしか使わない
        # （全体の上限は別にあるので、偽っても大量には作れない）
        xff = (headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        return xff or headers.get("_peer", "?")

    def _base_url(self, headers):
        if self.public_url:
            return self.public_url
        host = re.sub(r"[^A-Za-z0-9.:-]", "", headers.get("Host") or "")
        proto = "https" if (headers.get("X-Forwarded-Proto") or "https") == "https" else "http"
        return f"{proto}://{host}" if host else ""

    def _get(self, sub, headers):
        if sub in ("static/app.js", "static/app.css"):
            name = sub.split("/")[1]
            with open(os.path.join(MATCHER_DIR, name), "rb") as f:
                ctype = "text/javascript; charset=utf-8" if name.endswith(".js") else "text/css; charset=utf-8"
                return 200, ctype, f.read(), {"Cache-Control": "public, max-age=31536000, immutable"}
        if sub == "":
            return 200, "text/html; charset=utf-8", self._page(None, headers), {}
        m = re.fullmatch(r"([A-Za-z0-9_-]{6,32})(/image\.png)?", sub)
        if m and m.group(2):
            return self._image(m.group(1))
        if m:
            return 200, "text/html; charset=utf-8", self._page(m.group(1), headers), {}
        m = re.fullmatch(r"api/event/([A-Za-z0-9_-]{6,32})", sub)
        if m:
            if not self.enabled:
                raise MatcherError("この機能の準備ができていません", 503)
            event, people = self.store.get(m.group(1))
            return 200, "application/json; charset=utf-8", _json(public_view(event, people)), {}
        return 404, "text/plain; charset=utf-8", b"not found\n", {}

    def _image(self, eid):
        if not self.enabled:
            raise MatcherError("この機能の準備ができていません", 503)
        event, people = self.store.get(eid)
        stamp = json.dumps([p.get("updated") for p in people] + [len(people)])
        cached = self._img_cache.get(eid)
        if cached and cached[0] == stamp:
            png = cached[1]
        else:
            png = render_png(event, people)
            if len(self._img_cache) > 200:
                self._img_cache.clear()
            self._img_cache[eid] = (stamp, png)
        return 200, "image/png", png, {"Cache-Control": "public, max-age=60"}

    def _page(self, eid, headers):
        with open(os.path.join(MATCHER_DIR, "index.html"), encoding="utf-8") as f:
            page = f.read()
        title, og = "あわせ暦", ""
        if eid and self.enabled:
            try:
                event, people = self.store.get(eid)
                title = f"{event['title']} — あわせ暦"
                base = self._base_url(headers)
                first, last = date.fromisoformat(event["first"]), date.fromisoformat(event["last"])
                desc = f"{first.month}/{first.day}〜{last.month}/{last.day} の日程あわせ（{len(people)}人が入力）"
                if event.get("description"):            # 概要の冒頭も、Discord のプレビューに出す
                    head = event["description"].replace("\n", " ")
                    desc = (head[:120] + "…" if len(head) > 120 else head) + "\n" + desc
                og = "\n".join([
                    f'<meta property="og:title" content="{html.escape(event["title"])}">',
                    f'<meta property="og:description" content="{html.escape(desc)}">',
                    f'<meta property="og:type" content="website">',
                    f'<meta property="og:url" content="{html.escape(self.url_for(eid, base))}">',
                    f'<meta property="og:image" content="{html.escape(self.url_for(eid, base))}/image.png">',
                    '<meta name="twitter:card" content="summary_large_image">',
                    f'<meta name="theme-color" content="{event["palette"]["colors"][0]["hex"]}">',
                ])
            except MatcherError:
                pass
        return (page.replace("{{TITLE}}", html.escape(title))
                    .replace("{{OG}}", og)
                    .replace("{{EVENT_ID}}", eid or "")
                    .replace("{{VER_JS}}", _file_version("app.js"))
                    .replace("{{VER_CSS}}", _file_version("app.css"))).encode("utf-8")

    def _post(self, action, body, ip):
        if action == "create":
            event, admin = self.create(body.get("title"), parse_iso_day(body.get("first")),
                                       parse_iso_day(body.get("last")), who=ip, description=body.get("description"),
                                       slots=body.get("slots"))
            return {"ok": True, "id": event["id"], "admin": admin}
        m = re.fullmatch(r"event/([A-Za-z0-9_-]{6,32})/(view|join|save|delete|remove|edit)", action)
        if not m or not self.enabled:
            raise MatcherError("見つかりません", 404)
        eid, verb = m.groups()
        if not self.limiter.allow("write_ip", ip, *LIMITS["write_ip"]):
            raise MatcherError("短い時間に操作しすぎです。少し待ってください", 429)
        admin = body.get("admin") if isinstance(body.get("admin"), str) else None
        if verb == "view":                             # 作った人の鍵を確かめて、管理のボタンを出すかを返す
            event, people = self.store.get(eid)
            return public_view(event, people, self.store.is_admin(eid, admin))
        if verb == "join":
            if not self.limiter.allow("join_ip", ip, *LIMITS["join_ip"]):
                raise MatcherError("短い時間に参加者を増やしすぎです。少し待ってください", 429)
            name = clean_text(body.get("name"), MAX_NAME, "名前")
            mode = body.get("mode") if body.get("mode") in MODES else "rough"
            person, key = self.store.join(eid, name, mode)
            event, people = self.store.get(eid)
            return {**public_view(event, people, self.store.is_admin(eid, admin)), "joined": person["id"], "key": key}
        if verb == "save":
            event, _ = self.store.get(eid)
            changes = validate_changes(event, body.get("changes") or {})
            self.store.save(eid, body.get("pid"), body.get("key"), changes, admin=admin)
        elif verb == "delete":
            self.store.delete_person(eid, body.get("pid"), body.get("key"), admin=admin)
        elif verb == "remove":
            self.store.delete_event(eid, admin)
            return {"ok": True, "removed": True}
        elif verb == "edit":                           # タイトル・概要・候補日の修正（誰でも。候補日を消すのは作った人だけ）
            changes = {k: body[k] for k in ("title", "description", "slots", "add_days", "remove_days") if k in body}
            self.store.edit_event(eid, changes, admin=admin)
        event, people = self.store.get(eid)
        return public_view(event, people, self.store.is_admin(eid, admin))


def _json(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _file_version(name):
    try:
        with open(os.path.join(MATCHER_DIR, name), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:12]
    except OSError:
        return "0"


def setup(service_account_info, sheet_id, public_url=""):
    """Calendar.py から呼ぶ。MATCHER_SHEET_ID が無ければ、ページは「準備ができていません」と出す"""
    sheet_id = (sheet_id or "").strip()
    if not sheet_id:
        _log("MATCHER_SHEET_ID が未設定のため、あわせ暦（/m）は使えません")
        return Matcher(None, public_url=public_url)
    store = Store(SheetsBackend(sheet_id, service_account_info))
    app = Matcher(store, public_url=public_url)

    def loader():
        # 起動時に読み込み、その後は6時間ごとに期限切れを消す。読めなければ1分おきに読み直す
        while True:
            try:
                if not store.ready:
                    store.load()
                else:
                    n = store.purge_expired()
                    if n:
                        _log(f"期限切れの調整を {n} 件削除しました")
                time.sleep(6 * 3600)
            except Exception:
                time.sleep(60)

    threading.Thread(target=loader, daemon=True).start()
    return app
