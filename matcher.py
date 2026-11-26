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
#   ・参加者の入力は、伝助と同じく誰でも編集・削除できる（ブラウザの記録を消しても直せるように）。
#     日程あわせ全体の削除と候補日の削除は、作った人（作った人用の鍵）だけ。鍵はサーバーに SHA-256 の値でしか残さない
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


NIGHT_MAX = 27 * 60                    # 夜の終わりは 27時（翌3時）まで


def next_day(iso):
    return (date.fromisoformat(iso) + timedelta(days=1)).isoformat()


def slot_ranges(event):
    """その日程あわせの昼・夜の時間帯（分）。作るときに決め、あとから直せる"""
    sl = event.get("slots") if isinstance(event, dict) else None
    return {k: tuple(sl[k]) for k in SLOTS} if sl else dict(SLOT_RANGE)


def clean_tone(tone):
    if tone is None:
        return "auto"
    if tone not in TONES:
        raise MatcherError("雰囲気の指定が正しくありません")
    return tone


def clean_slots(raw):
    """画面から来た時間帯 {"d": [開始, 終了], "n": [...]}（分。1時間きざみ）を確かめる"""
    if not isinstance(raw, dict):
        raise MatcherError("時間帯の形が正しくありません")
    out = {}
    # 夜は日をまたいで 27時（翌3時）まで選べる（利用者の指定）。昼は 0〜24時
    for k, name, top in (("d", "昼", 1440), ("n", "夜", NIGHT_MAX)):
        v = raw.get(k)
        if not (isinstance(v, list) and len(v) == 2 and all(type(x) is int and x % 60 == 0 and 0 <= x <= top for x in v)):
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


# ---------- 祝日（内閣府「国民の祝日」の一覧） ----------
# 日程あわせを作るとき（と、候補日を足すとき）に内閣府の一覧を読み込み、候補日の祝日を日程あわせに保存する（利用者の指定）。
# 読めないときは、リポジトリに入れた一覧のコピー（matcher/holidays.csv）を使う
HOLIDAY_CSV_URL = "https://www8.cao.go.jp/chosei/shukujitsu/syukujitsu.csv"


def parse_holiday_csv(text):
    """「2027/1/11,成人の日」の形の行から {"2027-01-11": "成人の日"} を作る（見出しや読めない行は飛ばす）"""
    out = {}
    for line in text.splitlines():
        m = re.fullmatch(r"\s*(\d{4})/(\d{1,2})/(\d{1,2})\s*,\s*(.+?)\s*", line)
        if m:
            try:
                out[date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()] = m.group(4)[:20]
            except ValueError:
                continue
    return out


def _bundled_holidays():
    try:
        with open(os.path.join(MATCHER_DIR, "holidays.csv"), encoding="utf-8") as f:
            return parse_holiday_csv(f.read())
    except OSError:
        return {}


BUNDLED_HOLIDAYS = _bundled_holidays()


def fetch_holidays():
    """内閣府の一覧を読み込む（Shift_JIS）。読めないとき、中身がおかしいときは、リポジトリのコピーを使う"""
    try:
        req = urllib.request.Request(HOLIDAY_CSV_URL, headers={"User-Agent": "calendar-bot-matcher"})
        with urllib.request.urlopen(req, timeout=10) as r:
            got = parse_holiday_csv(r.read(200_000).decode("cp932", errors="replace"))
        if len(got) >= 100:                       # 1955年からの一覧なので、少なすぎるときは読み違いとみなす
            global _LATEST_HOLIDAYS
            _LATEST_HOLIDAYS = got                # 作るページのカレンダーの色にも使う
            return got, "cao"
        _log("内閣府の祝日の一覧が少なすぎたので、コピーを使います")
    except (urllib.error.URLError, OSError, ValueError) as e:
        _log(f"内閣府の祝日の一覧を読めなかったので、コピーを使います（{type(e).__name__}）")
    return BUNDLED_HOLIDAYS, "bundled"


_LATEST_HOLIDAYS = None                   # 最後に内閣府から読めた一覧（まだ無ければコピー）


def upcoming_holidays(today=None):
    """作るページのカレンダーで色を付けるための祝日（今月の初めから2年先まで）。
    日程あわせを作るときには、あらためて内閣府の一覧を読み込む"""
    today = today or datetime.now(JST).date()
    lo, hi = today.replace(day=1).isoformat(), (today + timedelta(days=731)).isoformat()
    return {d: n for d, n in (_LATEST_HOLIDAYS or BUNDLED_HOLIDAYS).items() if lo <= d <= hi}


def holidays_for(days, table):
    return {d: table[d] for d in days if d in table}


def event_holidays(event):
    """日程あわせの候補日の祝日。作ったときに保存したものを使い、無い（前から作られている）ときはコピーで補う"""
    saved = event.get("holidays")
    return saved if isinstance(saved, dict) else holidays_for(period_days(event), BUNDLED_HOLIDAYS)


def picker_holidays(event):
    """「候補日を足す」カレンダーで色を付けるための祝日（最初の候補日の月から、最後の候補日の1年あとまで）。
    候補日に足すと、そのときにあらためて内閣府の一覧から読み込む"""
    lo = event["first"][:8] + "01"
    hi = (date.fromisoformat(event["last"]) + timedelta(days=366)).isoformat()
    got = {d: n for d, n in BUNDLED_HOLIDAYS.items() if lo <= d <= hi}
    got.update(event_holidays(event))
    return got


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
HEAT_MIN_SPREAD = 110                  # 予定の枠の色の、R・G・B の最大と最小の差の下限（0〜255。灰色に近いと小さい）


def rgb_spread(h):
    """RGB の偏り（R・G・B のうち最大と最小の差）。灰色は 0、原色に近いほど大きい"""
    r, g, b = _rgb(h)
    return max(r, g, b) - min(r, g, b)


def heat_base(colors, sheet):
    """みんなのカレンダーの枠に付ける色。5色のうち RGB の偏りが最も大きい色を選び（利用者の指定）、
    それでも灰色に近ければ、色合いを保ったまま彩度を上げる。いちばん濃い段階（地と 9:1 で混ぜた色）が
    地とコントラスト比 3 以上になるよう、明るさも整える"""
    import colorsys
    base = max((c["hex"] for c in colors), key=lambda h: (rgb_spread(h), contrast(h, sheet)))
    r, g, b = (v / 255 for v in _rgb(base))
    hue, light, sat = colorsys.rgb_to_hls(r, g, b)

    def to_hex(hh, ll, ss):
        return "#" + "".join(f"{round(v * 255):02x}" for v in colorsys.hls_to_rgb(hh, ll, ss))

    if rgb_spread(base) < HEAT_MIN_SPREAD:
        light = min(max(light, 0.35), 0.6)
        for s_ in [x / 100 for x in range(int(sat * 100), 101, 2)]:
            if rgb_spread(to_hex(hue, light, s_)) >= HEAT_MIN_SPREAD:
                sat = s_
                break
        else:
            sat = 1.0
        base = to_hex(hue, light, sat)
    for _ in range(40):                        # 濃い段階が地に埋もれないよう、地と反対の向きに明るさを動かす
        if contrast(mix(sheet, base, HEAT_STEPS[-1]), sheet) >= 3:
            break
        light = max(0.05, light - 0.02) if luminance(sheet) > 0.3 else min(0.95, light + 0.02)
        base = to_hex(hue, light, sat)
    return base


TEXT_MIN_CONTRAST = 4.5                # 文字と地のコントラスト比の下限（WCAG 2.1 達成基準 1.4.3、通常の文字）


def ensure_contrast(fg, bgs, ratio=TEXT_MIN_CONTRAST):
    """文字色 fg を、地の色 bgs のどれに対してもコントラスト比 ratio 以上になるまで、
    色合いを保ったまま黒か白に寄せる（地が明るければ黒、暗ければ白の側へ）"""
    if all(contrast(fg, b) >= ratio for b in bgs):
        return fg
    target = "#000000" if contrast(bgs[0], "#000000") >= contrast(bgs[0], "#ffffff") else "#ffffff"
    for i in range(1, 21):
        c = mix(fg, target, i / 20)
        if all(contrast(c, b) >= ratio for b in bgs):
            return c
    return target


TONES = ("auto", "light", "dark")      # 雰囲気：おまかせ（配色の「背景」のまま）・明るめ・暗め


def toned_colors(colors, tone):
    """雰囲気に合わせて、配色の「背景」（4番目の色）だけを明るく／暗くした5色を返す（色合いは保つ）。
    明るめ：地の明るさ（相対輝度）0.80 以上、暗め：0.03 以下になるまで、白／黒へ寄せる"""
    if tone not in ("light", "dark"):
        return colors
    out = [dict(c) for c in colors]
    bg = out[3]["hex"]
    if tone == "light":
        src = max((c["hex"] for c in colors), key=luminance) if luminance(bg) < 0.2 else bg   # 暗い背景なら、いちばん明るい色から
        new = next((mix(src, "#ffffff", i / 20) for i in range(21) if luminance(mix(src, "#ffffff", i / 20)) >= 0.80), "#ffffff")
    else:
        src = min((c["hex"] for c in colors), key=luminance) if luminance(bg) > 0.2 else bg   # 明るい背景なら、いちばん暗い色から
        new = next((mix(src, "#000000", i / 20) for i in range(21) if luminance(mix(src, "#000000", i / 20)) <= 0.03), "#000000")
    out[3] = {"hex": new, "name": out[3].get("name", "")}
    return out


def build_theme(palette, tone="auto"):
    """画面と画像で使う色をまとめて決める（CSS の変数名で返す）。
    文字の色は、置かれる地の色（paper・sheet）に対してコントラスト比 4.5 以上になるようにする"""
    colors = toned_colors(palette["colors"], tone)
    palette = {**palette, "colors": colors}
    t = theme_assign(colors)
    bg, ink = t["bg"], t["ink"]
    dark = contrast(bg, "#000000") < contrast(bg, "#ffffff")
    # 地の色が中くらいの明るさだと、どんな文字色でも 4.5 に届かないことがある。
    # 明るい地は黒い文字と、暗い地は白い文字とコントラスト比 7 以上になるまで、色合いを保って地のほうを寄せる
    bg = ensure_contrast(bg, ["#ffffff" if dark else "#000000"], 7)
    sheet = mix(bg, "#ffffff", 0.06 if dark else 0.55)
    grounds = [bg, sheet]                                  # 文字が置かれる地（ページの地と、欄の地）
    if dark:
        # 暗い地では、配色の明るい色（橙など）をそのまま本文に使うと、見出しや強調との差が付かない（利用者の指定）。
        # 本文は白に寄せ、色合いは少しだけ残す
        ink = mix(ink, "#ffffff", 0.85)
    ink = ensure_contrast(ink, grounds)
    ink_soft = ensure_contrast(mix(ink, bg, 0.3), grounds)  # 薄めた文字色も、読める濃さまでしか薄めない

    def edge(h):
        return mix(ink, bg, 0.3) if contrast(h, bg) < 1.5 else h

    def readable(h):
        return ensure_contrast(h, grounds)

    def on(h):
        """文字を載せる地の色 h と、その上の文字色（白か黒）。中くらいの明るさだと白でも黒でも 4.5 に届かないので、
        地の色のほうを、色合いを保ったまま文字と反対の側へ寄せて 4.5 以上にする"""
        ink_ = text_on(h)
        return ensure_contrast(h, [ink_]), ink_

    btn, btn_ink = on(t["btn"])
    go, go_ink = on(t["goHover"])
    hov, hov_ink = on(t["hovHover"])
    # 濃さ 0（白い枠）は欄の地そのものなので、文字は 4.5 以上を確かめた通常の文字色（ink）にする
    heat_pairs = [on(mix(sheet, heat_base(palette["colors"], sheet), s)) if s else (sheet, ink) for s in HEAT_STEPS]
    heat = [h for h, _ in heat_pairs]
    return {
        "--paper": bg, "--sheet": sheet, "--ink": ink, "--ink-soft": ink_soft, "--rule": mix(ink, bg, 0.8),
        "--ai": btn, "--ai-ink": btn_ink, "--ai-edge": edge(btn),
        "--second": go, "--second-ink": go_ink, "--second-edge": edge(go),
        "--accent": hov, "--accent-ink": hov_ink, "--accent-edge": edge(hov),
        "--deco": t["deco"],
        "--ok": readable(t["btn"]), "--maybe": readable(t["goHover"]), "--ng": ink_soft,
        # 日曜の赤・土曜の青・注意の赤も、地に対して読める濃さにする（配色に関係なく固定の色だと、暗い地で読めない）
        "--sun": readable("#e0443a" if dark else "#c62f24"), "--sat": readable("#6f9cf0" if dark else "#2f5fc4"),
        "--warn": readable("#f08a70" if dark else "#a1361f"),
        **{f"--heat{i}": c for i, c in enumerate(heat)},
        **{f"--heat-ink{i}": ink_ for i, (_, ink_) in enumerate(heat_pairs)},
        "color-scheme": "dark" if dark else "light",
    }


# ---------- ○△✕ の判定と集計 ----------
def busy_mark(intervals, slot, ranges=None, next_intervals=()):
    """その日の予定（分の区間）から、時間帯の ○✕ を決める。少しでも重なれば ✕（ボットの判定と同じ）。
    夜が 24時を越えるとき（例：21〜27時）は、越えた分を翌日の 0時からの予定（next_intervals）で調べる"""
    s0, e0 = (ranges or SLOT_RANGE)[slot]
    if any(s < min(e0, 1440) and e > s0 for s, e in intervals):
        return "x"
    if e0 > 1440 and any(s < e0 - 1440 and e > max(s0 - 1440, 0) for s, e in next_intervals):
        return "x"
    return "o"


def resolve_mark(person, day, slot, ranges=None):
    """本人が付けた印があればそれ、詳細入力で予定を共有済みなら自動の ○✕、どちらでもなければ None（未入力）"""
    m = person.get("marks", {}).get(day, {}).get(slot)
    if m:
        return m
    if person.get("mode") == "detail" and person.get("busy_set"):
        busy = person.get("busy", {})
        return busy_mark(busy.get(day, []), slot, ranges, busy.get(next_day(day), []))
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
    def create_event(self, title, first, last, palette, now=None, description="", slots=None, tone="auto", holidays=None):
        with self.lock:
            self._require()
            eid = secrets.token_urlsafe(9)
            while eid in self.events:
                eid = secrets.token_urlsafe(9)
            admin = secrets.token_urlsafe(24)
            data = {"id": eid, "title": title, "description": description,
                    "slots": slots or json.loads(json.dumps(DEFAULT_SLOTS)), "tone": tone,
                    "first": first.isoformat(), "last": last.isoformat(),
                    "palette": palette, "admin": _hash_key(admin),
                    "created": (now or datetime.now(JST)).isoformat(timespec="seconds")}
            data["holidays"] = holidays_for(period_days(data), holidays or {})
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

    def edit_event(self, eid, changes, admin=None, today=None, now=None, holiday_table=None):
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
            if "tone" in changes:                      # 雰囲気（おまかせ・明るめ・暗め）
                data["tone"] = clean_tone(changes["tone"])
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
            if add or remove or "holidays" not in data:  # 候補日の祝日を付け直す（足した日は、読み込んだ一覧から）
                table = holiday_table if holiday_table is not None else BUNDLED_HOLIDAYS
                old = event_holidays(entry["data"])
                data["holidays"] = {d: old.get(d) or table.get(d) for d in data["days"] if (old.get(d) or table.get(d))}
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

    def save(self, eid, pid, key, changes, admin=None, now=None):
        with self.lock:
            self._require()
            self.get(eid)
            # 誰でも、どの参加者の入力も編集できる（利用者の指定。ブラウザの記録を消しても直せるように。誰が直したかは残さない）
            entry = self._person_entry(eid, pid)
            data = dict(entry["data"])
            data.update(changes)
            data["updated"] = (now or datetime.now(JST)).isoformat(timespec="seconds")
            self.people[eid][pid] = self._write(entry, f"p:{eid}:{pid}", data)
            return data

    def delete_person(self, eid, pid, key=None, admin=None):
        with self.lock:
            self._require()
            self.get(eid)
            entry = self._person_entry(eid, pid)      # 削除も誰でもできる（利用者の指定）
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
        # 予定は、候補日と、その翌日（夜が 24時を越えるときの未明の分）を受け取る
        busy_days = days | {next_day(d) for d in days}
        busy = body["busy"]
        if not isinstance(busy, dict):
            raise MatcherError("予定の形が正しくありません")
        clean = {}
        for day, ivs in busy.items():
            if not isinstance(ivs, list) or len(ivs) > MAX_INTERVALS_PER_DAY:
                raise MatcherError("予定の形が正しくないか、多すぎる予定が含まれています")
            if day not in busy_days:
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
def content_version(event, people):
    """日程あわせの中身が変わるたびに変わる短い印。プレビューの画像と、コピーする URL の ?v= に使う
    （Discord はプレビューを約30分キャッシュするが、URL が変われば取り直す）"""
    stamp = json.dumps([event.get("edited"), event.get("slots"), event.get("tone"), event.get("title")]
                       + [p.get("updated") for p in people] + [len(people)])
    return hashlib.sha256(stamp.encode("utf-8")).hexdigest()[:8]


def public_view(event, people, viewer_admin=False):
    table = tally(event, people)
    return {
        "ok": True,
        "event": {"id": event["id"], "title": event["title"], "description": event.get("description", ""),
                  "first": event["first"], "last": event["last"],
                  "days": period_days(event), "palette": event["palette"], "tone": event.get("tone", "auto"),
                  "holidays": event_holidays(event), "picker_holidays": picker_holidays(event),
                  # 見る人が雰囲気を切り替えられるように、3つとも返す（theme は作った人が選んだ雰囲気）
                  "theme": build_theme(event["palette"], event.get("tone", "auto")),
                  "themes": {t_: build_theme(event["palette"], t_) for t_ in ("light", "dark")},
                  "version": content_version(event, people)},
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


_FONT_CACHE = {}


def _font(size, jp=False):
    """フォントは大きさごとに1回だけ読み込む（画像を作るたびに読み込まない）"""
    key = (size, jp)
    if key not in _FONT_CACHE:
        _FONT_CACHE[key] = _load_font(size, jp)
    return _FONT_CACHE[key]


def _load_font(size, jp=False):
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
    theme = build_theme(event["palette"], event.get("tone", "auto"))
    table = tally(event, people)
    n = len(people)
    days = set(period_days(event))
    hols = event_holidays(event)
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
                num_col = ink if inside else soft          # 候補日でない日も、読める濃さの薄い文字色で
                if inside and (c == 0 or key in hols):     # 日曜と祝日は日曜の色
                    num_col = theme["--sun"]
                elif inside and c == 6:
                    num_col = theme["--sat"]
                d.text((x0 + 8, y0 + 4), str(dn), font=f_day, fill=_hex_rgb(num_col))
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
from concurrent.futures import ThreadPoolExecutor
_RENDER_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="matcher-render")


class Matcher:
    """Calendar.py の Web サーバーから呼ばれる窓口"""
    def __init__(self, store=None, use_ai=True, public_url="", fetch_holidays=True):
        self.store = store
        self.use_ai = use_ai
        self.fetch_holidays = fetch_holidays
        self.public_url = public_url.rstrip("/")
        self.limiter = RateLimiter()
        self._img_cache = {}

    @property
    def enabled(self):
        return self.store is not None

    # --- 作る（Web とスラッシュコマンドの共用） ---
    def create(self, title, first, last, who="?", today=None, description="", slots=None, tone="auto"):
        if not self.enabled:
            raise MatcherError("この機能の準備ができていません（MATCHER_SHEET_ID が未設定）", 503)
        title = clean_text(title, MAX_TITLE, "タイトル")
        description = clean_memo(description, MAX_DESC, "概要")
        slots = clean_slots(slots) if slots is not None else None
        tone = clean_tone(tone)
        check_period(first, last, today)
        self.store._require()
        if not self.limiter.allow("create_ip", who, *LIMITS["create_ip"]):
            raise MatcherError("短い時間に作りすぎです。しばらく待ってからもう一度試してください", 429)
        if not self.limiter.allow("create_all", "*", *LIMITS["create_all"]):
            raise MatcherError("今日はこれ以上作れません。明日もう一度試してください", 429)
        palette = make_palette(title, self.use_ai)    # 時間がかかるので、保存先の鍵（lock）の外で作る
        holidays, src = fetch_holidays() if self.fetch_holidays else (BUNDLED_HOLIDAYS, "bundled")
        event, admin = self.store.create_event(title, first, last, palette, description=description, slots=slots, tone=tone,
                                               holidays=holidays)
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
        if sub == "api/holidays":
            return 200, "application/json; charset=utf-8", _json({"ok": True, "holidays": upcoming_holidays()}), \
                {"Cache-Control": "public, max-age=3600"}
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
        stamp = content_version(event, people)
        cached = self._img_cache.get(eid)
        if cached and cached[0] == stamp:
            png = cached[1]
        else:
            # 画像は決まったスレッド1つで作る。Web サーバーは要求ごとに別のスレッドで動くので、そのまま作ると
            # スレッドごとに大きな作業用のメモリが残り、プロセス全体のメモリが膨らむことがある
            png = _RENDER_POOL.submit(render_png, event, people).result(timeout=60)
            if len(self._img_cache) > 50:
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
                # 期間と人数を先に、概要をあとに。Discord は説明を 350 バイト（20行）で切るので、収まるように切る
                desc = f"{first.month}/{first.day}〜{last.month}/{last.day} の日程あわせ（{len(people)}人が入力）"
                if event.get("description"):
                    desc = fit_bytes(desc + "\n" + event["description"].replace("\n", " "), PREVIEW_DESC_BYTES)
                og = "\n".join([
                    f'<meta property="og:title" content="{html.escape(event["title"])}">',
                    f'<meta property="og:description" content="{html.escape(desc)}">',
                    f'<meta property="og:type" content="website">',
                    f'<meta property="og:url" content="{html.escape(self.url_for(eid, base))}">',
                    # 中身が変わるたびに画像の URL も変える（古い画像が使い回されないように）
                    f'<meta property="og:image" content="{html.escape(self.url_for(eid, base))}/image.png?v={content_version(event, people)}">',
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
                                       slots=body.get("slots"), tone=body.get("tone") or "auto")
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
            changes = {k: body[k] for k in ("title", "description", "slots", "tone", "add_days", "remove_days") if k in body}
            table = None
            if changes.get("add_days"):                # 足した日の祝日は、内閣府の一覧を読み込んで付ける
                table, _src = fetch_holidays() if self.fetch_holidays else (BUNDLED_HOLIDAYS, "bundled")
            self.store.edit_event(eid, changes, admin=admin, holiday_table=table)
        event, people = self.store.get(eid)
        return public_view(event, people, self.store.is_admin(eid, admin))


PREVIEW_DESC_BYTES = 350                # Discord のプレビューの説明の上限（公式資料：350 bytes, 20 lines）


def fit_bytes(text, limit):
    """UTF-8 で limit バイトに収まるように、文字の途中で切らずに縮める（切ったら … を付ける）"""
    if len(text.encode("utf-8")) <= limit:
        return text
    out = ""
    for ch in text:
        if len((out + ch + "…").encode("utf-8")) > limit:
            break
        out += ch
    return out + "…"


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
