// カレンダー画面（Discord のアクティビティ）。
// 予定の名前など、外から来た文字はすべて textContent で入れる（HTML として解釈させない）。
// 判定はボット側でも必ずやり直すので、ここでの検査は使いやすさのためだけ。
import { DiscordSDK } from "./discord-sdk.js";

const CLIENT_ID = document.body.dataset.clientId;
const WD = ["日", "月", "火", "水", "木", "金", "土"];
const SLOT = 30;                                   // 週表示の1枠（分）
const STEP = 15;                                   // つまみで動かすきざみ（分）

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};
const pad = (n) => String(n).padStart(2, "0");
const iso = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const fromIso = (s) => { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); };
const addDays = (d, n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
const md = (d) => `${d.getMonth() + 1}/${d.getDate()}(${WD[d.getDay()]})`;
const mdk = (key) => md(fromIso(key));
const hm = (min) => (min === 1440 ? "24:00" : `${Math.floor(min / 60)}:${pad(min % 60)}`);
const weekStart = (d) => addDays(d, -d.getDay());   // 日曜はじまり
const allDayTime = () => ({ allDay: true, start: null, end: null });   // 日ごとに指定の、日の初めの状態（終日）

const S = {
  session: null, calendars: [], today: null, maxDays: 31,
  month: null,                 // その月の1日
  week: null,                  // 週表示の日曜
  days: new Set(),             // 選んだ日（"YYYY-MM-DD"）
  mode: "same",                // "same"＝全部同じ時間、"each"＝日ごとに指定
  same: { allDay: true, start: null, end: null },
  per: new Map(),              // 日ごとの時間（"each" のとき）
  editDay: null,               // 日ごとに指定しているときの、いま時間を決めている日
  pending: null,               // 開始だけタップした状態 { day, start }
  events: new Map(),           // "YYYY-MM-DD" → 帯の一覧
  loaded: [],                  // 読み込んだ期間 [first, last]
  busy: false, done: false, scrolled: false,
};

function setStatus(text, isError) {
  const s = $("status");
  s.textContent = text;
  s.classList.toggle("error", !!isError);
}

async function api(path, body) {
  const r = await fetch(path, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  let j = null;
  try { j = await r.json(); } catch (_) { /* 下でまとめて扱う */ }
  if (!r.ok || !j || j.ok === false) throw new Error((j && j.error) || `通信に失敗しました（HTTP ${r.status}）`);
  return j;
}

// ---------- 時間の持ち方 ----------
const timed = () => S.mode === "each" || !S.same.allDay;   // 週表示を出すか
function slotOf(key) {                                     // その日の時間（「全部同じ」なら共通の時間）
  if (S.mode === "same") return S.same;
  if (!S.per.has(key)) S.per.set(key, allDayTime());
  return S.per.get(key);
}
function timeText(t) {
  if (t.allDay) return "終日";
  if (t.start === null) return "未選択";
  if (t.end === null) return `${hm(t.start)}〜（終了をタップ）`;
  return `${hm(t.start)}〜${t.end <= t.start ? "翌" : ""}${hm(t.end)}`;
}

// ---------- 予定の読み込み ----------
function covered(first, last) {
  return S.loaded.some(([a, b]) => a <= first && last <= b);
}
async function ensureEvents(first, last) {
  const f = iso(first), l = iso(last);
  if (covered(f, l)) return;
  const j = await api("api/data", { session: S.session, first: f, last: l });
  for (let d = first; d <= last; d = addDays(d, 1)) S.events.set(iso(d), []);
  for (const e of j.events) {
    if (!S.events.has(e.date)) S.events.set(e.date, []);
    S.events.get(e.date).push(e);
  }
  S.loaded.push([f, l]);
  const w = $("warning");
  w.hidden = !j.warning;
  w.textContent = j.warning || "";
}
function monthRange() {
  const first = weekStart(S.month);
  return [first, addDays(first, 41)];
}
async function refreshData() {
  try {
    const [a, b] = monthRange();
    await ensureEvents(a, b);
    if (timed()) await ensureEvents(S.week, addDays(S.week, 6));
  } catch (e) {
    setStatus(e.message, true);
  }
  render();
}

// ---------- 月表示 ----------
function toggleDay(key) {
  if (S.done) return;
  if (S.days.has(key)) {                   // 外したときは週表示を動かさない
    S.days.delete(key);
    if (S.editDay === key) S.editDay = null;
    if (S.pending && S.pending.day === key) S.pending = null;
  } else if (S.days.size >= S.maxDays) {
    setStatus(`一度に選べるのは${S.maxDays}日までです`, true);
    return;
  } else {
    S.days.add(key);
    S.week = weekStart(fromIso(key));      // 選んだ日の週を週表示に出す
    if (S.mode === "each") S.editDay = key;
  }
  setStatus("");
  render();
  if (timed()) refreshData();
}
function renderMonth() {
  $("month-title").textContent = `${S.month.getFullYear()}年${S.month.getMonth() + 1}月`;
  const grid = $("month-grid");
  grid.replaceChildren();
  const [first] = monthRange();
  const wk = iso(S.week);
  for (let i = 0; i < 42; i++) {
    const d = addDays(first, i);
    const key = iso(d);
    const b = el("button", "day");
    b.type = "button";
    b.dataset.date = key;
    if (d.getMonth() !== S.month.getMonth()) b.classList.add("other");
    if (d < S.today) b.classList.add("past");
    if (key === iso(S.today)) b.classList.add("today");
    if (d.getDay() === 0) b.classList.add("sun");
    if (d.getDay() === 6) b.classList.add("sat");
    if (S.days.has(key)) b.classList.add("selected");
    if (timed() && iso(weekStart(d)) === wk) b.classList.add("week");
    b.setAttribute("aria-pressed", S.days.has(key) ? "true" : "false");
    b.setAttribute("aria-label", `${md(d)}${S.days.has(key) ? "（選択中）" : ""}`);
    b.append(el("span", "num", String(d.getDate())));
    const marks = el("span", "marks");
    for (const e of (S.events.get(key) || []).slice(0, 4)) marks.append(el("span", e.all_day ? "mark all" : "mark"));
    b.append(marks);
    b.addEventListener("click", () => toggleDay(key));
    grid.append(b);
  }
}

// ---------- 日ごとの一覧 ----------
function focusDay(key) {
  S.editDay = key;
  S.week = weekStart(fromIso(key));
  if (S.pending && S.pending.day !== key) S.pending = null;
  refreshData();
}
function renderEachList() {
  const ul = $("each-list");
  ul.replaceChildren();
  for (const key of [...S.days].sort()) {
    const t = slotOf(key);
    const li = el("li", "each-row" + (S.editDay === key ? " editing-row" : ""));
    const pick = el("button", "pickday");
    pick.type = "button";
    pick.dataset.day = key;
    const pendingHere = S.pending && S.pending.day === key;
    pick.append(el("span", "", mdk(key)),
      el("span", "t" + (!t.allDay && (t.start === null || t.end === null) ? " missing" : ""),
        pendingHere ? `${hm(S.pending.start)}〜（終了をタップ）` : timeText(t)));
    pick.addEventListener("click", () => focusDay(key));
    const ad = el("button", "allday", "終日");
    ad.type = "button";
    ad.dataset.day = key;
    ad.setAttribute("aria-pressed", String(t.allDay));
    ad.addEventListener("click", () => {
      t.allDay = !t.allDay;
      if (S.pending && S.pending.day === key) S.pending = null;
      S.editDay = key;
      render();
    });
    li.append(pick, ad);
    ul.append(li);
  }
}

// ---------- 週表示（時間の枠） ----------
const NEAR = 60;                            // 開始・終了から1時間（表の2マス＝48px）以内のタップは「直す」と見なす
function nearMove(t, m) {
  // 決まった時間の近くの枠をタップしたら、近い方の端（開始か終了）をその枠まで動かす。動かしたら true。
  // 開始はタップした枠の始まり、終了はタップした枠の終わりに合わせる（最初に選ぶときと同じ）
  if (t.allDay || t.start === null || t.end === null) return false;
  const c = m + SLOT / 2;                   // タップした枠の真ん中
  const over = t.end <= t.start;            // 日をまたぐ帯（空いているのは 終了〜開始）
  let which;
  if (!over) {
    which = c < t.start ? "start" : c > t.end ? "end" : (c < (t.start + t.end) / 2 ? "start" : "end");
  } else if (c > t.end && c < t.start) {
    which = t.start - c < c - t.end ? "start" : "end";
  } else {
    which = c >= t.start ? "start" : "end"; // 帯の中：夜側の帯は開始、朝側の帯は終了
  }
  if (which === "start") {
    if (Math.abs(m - t.start) > NEAR) return false;
    t.start = over ? Math.max(m, t.end + STEP) : m;
  } else {
    if (Math.abs(m + SLOT - t.end) > NEAR) return false;
    t.end = over ? Math.min(m + SLOT, t.start - STEP) : m + SLOT;
  }
  return true;
}
function tapSlot(key, min, onBand) {
  if (S.done) return;
  if (!S.days.has(key)) {
    setStatus("時間は、選んだ日（色の付いた列）をタップして指定してください", true);
    return;
  }
  setStatus("");
  const t = slotOf(key);
  if (S.mode === "each") S.editDay = key;
  const pendingHere = S.pending && (S.mode === "same" || S.pending.day === key);
  if (!pendingHere && nearMove(t, min)) {    // 決まった時間の近く：つまみをそこへ動かす
    S.pending = null;
    render();
    return;
  }
  if (onBand) { render(); return; }          // 帯の中の、端から離れたところ：何もしない
  t.allDay = false;
  const sameDay = S.mode === "same" || (S.pending && S.pending.day === key);
  if (S.pending && sameDay && min >= S.pending.start) {   // 2回目：終了（その枠の終わりまで）
    t.start = S.pending.start; t.end = min + SLOT; S.pending = null;
  } else {                                                 // 1回目・開始より前・離れた枠：ここから選び直す
    S.pending = { day: key, start: min }; t.start = min; t.end = null;
  }
  render();
}
function band(col, a, b, cls) {
  const p = el("div", cls);
  p.style.top = `calc(var(--row) * ${a / SLOT})`;
  if (b !== null) p.style.height = `calc(var(--row) * ${(b - a) / SLOT})`;
  col.append(p);
  return p;
}

// ---------- 帯のドラッグ（つまみで開始・終了、真ん中で帯ごと。15分きざみ）----------
let drag = null;                            // ドラッグ中の情報
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
function dragTo(t, kind, s0, e0, dmin) {
  // 開始と終了が逆転しない・同じにならないようにする。日をまたぐ帯（終了が開始以前）はまたいだまま
  const over = e0 <= s0;
  if (kind === "start") {
    t.start = over ? clamp(s0 + dmin, e0 + STEP, 1440 - STEP) : clamp(s0 + dmin, 0, e0 - STEP);
  } else if (kind === "end") {
    t.end = over ? clamp(e0 + dmin, STEP, s0 - STEP) : clamp(e0 + dmin, s0 + STEP, 1440);
  } else {                                  // 帯ごと：長さを保つ（0:00〜24:00 の外へは出さない範囲で、日をまたぐのは可）
    const len = over ? e0 + 1440 - s0 : e0 - s0;
    const ns = clamp(s0 + dmin, 0, 1440 - STEP);
    t.start = ns;
    t.end = ns + len <= 1440 ? ns + len : ns + len - 1440;
  }
}
function startDrag(e, key, kind) {
  if (S.done) return;
  e.preventDefault();
  e.stopPropagation();
  const t = slotOf(key);
  const col = e.currentTarget.closest(".daycol");
  const row = col.querySelector(".slot").getBoundingClientRect().height;   // 30分の高さ（px）
  drag = { key, kind, t, s0: t.start, e0: t.end, y0: e.clientY, row, col, moved: false };
  if (S.mode === "each") S.editDay = key;
  col.setPointerCapture(e.pointerId);       // 指が帯から外れても追い続ける（列は描き直さない）
  col.addEventListener("pointermove", onDragMove);
  col.addEventListener("pointerup", endDrag);
  col.addEventListener("pointercancel", endDrag);
}
function onDragMove(e) {
  if (!drag) return;
  const dmin = Math.round(((e.clientY - drag.y0) / drag.row) * (SLOT / STEP)) * STEP;
  if (dmin !== 0) drag.moved = true;
  dragTo(drag.t, drag.kind, drag.s0, drag.e0, dmin);
  redrawBands();                            // 帯だけを描き直す（列ごと描き直すと指の追跡が切れる）
  renderSummary();
  syncFine();
}
function endDrag(e) {
  if (!drag) return;
  const col = drag.col;
  col.removeEventListener("pointermove", onDragMove);
  col.removeEventListener("pointerup", endDrag);
  col.removeEventListener("pointercancel", endDrag);
  try { col.releasePointerCapture(e.pointerId); } catch (_) { /* すでに外れている */ }
  const d = drag;
  drag = null;
  if (e.type === "pointerup" && !d.moved && d.kind === "move") {
    // 帯の上を動かさずにタップした：その位置の枠をタップしたのと同じに扱う（端の近くならつまみが来る）
    const top = col.getBoundingClientRect().top;
    const min = clamp(Math.floor((e.clientY - top) / d.row) * SLOT, 0, 1440 - SLOT);
    tapSlot(d.key, min, true);
    return;
  }
  render();
}
function drawBands(col, key) {
  for (const old of col.querySelectorAll(".pick, .alldayband")) old.remove();
  if (!S.days.has(key)) return;              // 選んだ時間は、選んだ日の列にだけ出す
  const t = slotOf(key);
  if (S.pending && (S.mode === "same" || S.pending.day === key)) {
    band(col, S.pending.start, null, "pick pending");
  } else if (t.allDay) {
    if (S.mode === "each") col.append(el("div", "alldayband", "終日"));
  } else if (t.start !== null && t.end !== null) {
    const label = timeText(t).replace("〜", "〜\n");   // 列が狭いので2行に
    const parts = t.end > t.start ? [[t.start, t.end]] : [[t.start, 1440], [0, t.end]];   // 日をまたぐなら2つ
    parts.forEach(([a, b], i) => {
      const p = band(col, a, b, "pick live");
      p.dataset.day = key;
      p.append(el("span", "picklabel", label));
      p.addEventListener("pointerdown", (e) => startDrag(e, key, "move"));
      if (i === 0) {                         // 開始のつまみは最初の帯の上端
        const h = el("div", "grip top");
        h.setAttribute("aria-label", `開始を動かす（${mdk(key)}）`);
        h.addEventListener("pointerdown", (e) => startDrag(e, key, "start"));
        p.append(h);
      }
      if (i === parts.length - 1) {          // 終了のつまみは最後の帯の下端
        const h = el("div", "grip bottom");
        h.setAttribute("aria-label", `終了を動かす（${mdk(key)}）`);
        h.addEventListener("pointerdown", (e) => startDrag(e, key, "end"));
        p.append(h);
      }
    });
  }
}
function redrawBands() {
  for (const col of $("week-grid").querySelectorAll(".daycol")) drawBands(col, col.dataset.day);
}
function renderWeek() {
  const ws = S.week;
  $("week-title").textContent = `${md(ws)} 〜 ${md(addDays(ws, 6))}`;
  const head = $("week-head");
  head.replaceChildren(el("span"));
  const grid = $("week-grid");
  grid.replaceChildren();
  const hours = el("div", "hours");
  for (let h = 0; h < 24; h++) hours.append(el("div", "", `${h}:00`));
  grid.append(hours);
  for (let i = 0; i < 7; i++) {
    const d = addDays(ws, i);
    const key = iso(d);
    const sel = S.days.has(key);
    const editing = S.mode === "each" && S.editDay === key;
    const hb = el("button", "col" + (sel ? " selected" : ""));
    hb.type = "button";
    hb.append(el("div", "", WD[d.getDay()]), el("div", "", `${d.getMonth() + 1}/${d.getDate()}`));
    hb.addEventListener("click", () => (sel && S.mode === "each" ? focusDay(key) : toggleDay(key)));
    head.append(hb);

    const col = el("div", "daycol" + (sel ? " selected" : "") + (editing ? " editing" : ""));
    col.dataset.day = key;
    for (let m = 0; m < 1440; m += SLOT) {
      const s = el("button", "slot" + (m % 60 === 0 ? " hour" : ""));
      s.type = "button";
      s.setAttribute("aria-label", `${md(d)} ${hm(m)}`);
      s.addEventListener("click", () => tapSlot(key, m));
      col.append(s);
    }
    for (const e of S.events.get(key) || []) {
      const box = el("div", e.all_day ? "ev all" : "ev");
      box.style.top = `calc(var(--row) * ${e.start / SLOT})`;
      box.style.height = `calc(var(--row) * ${Math.max(e.end - e.start, 15) / SLOT})`;
      box.textContent = e.title || "";        // 名前は文字としてだけ入れる
      col.append(box);
    }
    drawBands(col, key);
    grid.append(col);
  }
  if (!S.scrolled) {                        // 最初に開いたときは朝8時あたりを見せる
    S.scrolled = true;
    requestAnimationFrame(() => {
      const row = $("week-grid").querySelector(".slot");
      if (row) $("week-scroll").scrollTop = row.getBoundingClientRect().height * 16;
    });
  }
}
function fillTimeSelects() {
  const ss = $("start-sel"), es = $("end-sel");
  ss.replaceChildren(); es.replaceChildren();
  ss.append(new Option("—", ""));
  es.append(new Option("—", ""));
  for (let m = 0; m < 1440; m += 15) ss.append(new Option(hm(m), String(m)));
  for (let m = 15; m <= 1440; m += 15) es.append(new Option(hm(m), String(m)));
}
function fineTarget() {                     // 開始・終了の欄が直す相手
  if (S.mode === "same") return S.same.allDay ? null : S.same;
  return S.editDay && S.days.has(S.editDay) ? slotOf(S.editDay) : null;
}

// ---------- 全体 ----------
function entries() {
  return [...S.days].sort().map((key) => {
    const t = slotOf(key);
    return t.allDay ? { day: key, start: null, end: null } : { day: key, start: hm(t.start), end: hm(t.end) };
  });
}
function valid() {
  if (S.days.size === 0) return "日付を選んでください";
  if (S.pending) return S.mode === "each" ? `終了の枠をタップしてください（${mdk(S.pending.day)}）` : "終了の枠をタップしてください";
  if (S.mode === "same") {
    const t = S.same;
    if (!t.allDay && (t.start === null || t.end === null)) return "時間の枠をタップして選んでください";
    if (!t.allDay && t.start === t.end) return "開始と終了が同じ時刻です";
  } else {
    const missing = [...S.days].sort().filter((k) => { const t = slotOf(k); return !t.allDay && (t.start === null || t.end === null); });
    if (missing.length) return `時間が未選択の日があります：${missing.map(mdk).join("、")}`;
    const same = [...S.days].sort().find((k) => { const t = slotOf(k); return !t.allDay && t.start === t.end; });
    if (same) return `開始と終了が同じ時刻です（${mdk(same)}）`;
  }
  if (!$("name").value.trim()) return "予定の名前を入れてください";
  return null;
}
function renderSummary() {
  const keys = [...S.days].sort();
  let text;
  if (!keys.length) text = "日付が未選択";
  else if (S.mode === "same") text = `${keys.length}件：${keys.map(mdk).join("・")}　${timeText(S.same)}`;
  else text = `${keys.length}件：${keys.map((k) => `${mdk(k)} ${timeText(slotOf(k))}`).join("・")}`;
  const problem = valid();
  $("summary").textContent = text + (problem ? `\n${problem}` : "");
  $("add").disabled = !!problem || S.busy || S.done;
}
function render() {
  renderMonth();
  $("apply-same").setAttribute("aria-checked", String(S.mode === "same"));
  $("apply-each").setAttribute("aria-checked", String(S.mode === "each"));
  $("same-box").hidden = S.mode !== "same";
  $("each-box").hidden = S.mode !== "each";
  $("mode-allday").setAttribute("aria-checked", String(S.same.allDay));
  $("mode-time").setAttribute("aria-checked", String(!S.same.allDay));
  if (S.mode === "each") renderEachList();
  $("time-box").hidden = !timed();
  if (timed()) renderWeek();
  syncFine();
  renderSummary();
}
function syncFine() {
  const ft = fineTarget();
  const editing = $("editing");
  editing.hidden = S.mode !== "each";
  editing.textContent = S.mode !== "each" ? ""
    : ft ? `編集中：${mdk(S.editDay)}` : "時間を決める日を、上の一覧か表の列からタップしてください";
  $("start-sel").disabled = !ft; $("end-sel").disabled = !ft;
  $("start-sel").value = ft && ft.start !== null ? String(ft.start) : "";
  $("end-sel").value = ft && ft.end !== null ? String(ft.end) : "";
  $("overnight").hidden = !ft || ft.allDay || ft.start === null || ft.end === null || ft.end > ft.start;
}

async function submit() {
  if (valid() || S.busy) return;
  S.busy = true;
  renderSummary();
  setStatus("追加しています…");
  const body = { session: S.session, entries: entries(), name: $("name").value, calendar: $("calendar").value };
  try {
    const j = await api("api/add", body);
    S.done = true;
    $("editor").hidden = true; $("bar").hidden = true; $("done").hidden = false;
    // ボットの返信を文字として出す。Discord 用の書式（行全体の **太字** と <リンク>）だけ外す
    $("result").textContent = j.message
      .replace(/^\*\*(.*)\*\*$/gm, "$1")
      .replace(/^<(https?:\/\/\S+)>$/gm, "$1");
    setStatus("追加しました。結果はチャンネルにも表示しています。");
  } catch (e) {
    setStatus(e.message, true);
  } finally {
    S.busy = false;
    renderSummary();
  }
}

function setApply(mode) {
  if (S.mode === mode) return;
  if (mode === "each") {
    // 「全部同じ時間」で時間を決めていたときは、それを、まだ決めていない日の初期値にする。それ以外は終日から始める
    const t = S.same;
    const copy = !t.allDay && t.start !== null && t.end !== null;
    for (const k of S.days) {
      if (!S.per.has(k)) S.per.set(k, copy ? { ...t } : allDayTime());
    }
    S.editDay = [...S.days].sort()[0] || null;
    if (S.editDay) S.week = weekStart(fromIso(S.editDay));   // 表も、編集中の日の週にする
  }
  S.mode = mode;
  S.pending = null;
  setStatus("");
  refreshData();
}
// ---------- 名前の入力：キーボードに隠れないように ----------
// 画面は Discord の中の iframe で動くので、スマホのブラウザが入力欄を自動で見える所まで動かしてくれないことがある。
// キーボードは画面の下から4〜5割ほどを覆うので、入力中は欄を画面の上から2割の所まで動かし、下の「追加する」バーを隠す
const TOUCH = new URLSearchParams(location.search).get("platform") === "mobile"
  || (window.matchMedia && window.matchMedia("(pointer: coarse)").matches);
function keepFieldVisible() {
  const input = $("name");
  if (document.activeElement !== input) return;
  const vv = window.visualViewport;          // キーボードの分だけ小さくなる端末では、見えている範囲に合わせる
  const viewTop = vv ? vv.offsetTop : 0;
  const viewH = vv ? Math.min(vv.height, window.innerHeight) : window.innerHeight;
  const top = input.closest(".field").getBoundingClientRect().top;   // 「予定の名前」の見出しごと見せる
  window.scrollBy(0, top - (viewTop + viewH * 0.2));
}
function wireTyping() {
  if (!TOUCH) return;                        // パソコンではキーボードが画面を覆わないので何もしない
  const input = $("name");
  input.addEventListener("focus", () => {
    document.body.classList.add("typing");   // バーを隠し、下に余白を足す（一番下の欄でも上まで動かせるように）
    requestAnimationFrame(keepFieldVisible);
    setTimeout(keepFieldVisible, 300);       // キーボードが出きったあと（端末が自分で動かした分も直す）
    setTimeout(keepFieldVisible, 700);
  });
  input.addEventListener("blur", () => document.body.classList.remove("typing"));
  input.addEventListener("keydown", (e) => {   // 「完了」でキーボードを閉じる。日本語の変換を確定する Enter では閉じない
    if (e.key === "Enter" && !e.isComposing && e.keyCode !== 229) input.blur();
  });
  if (window.visualViewport) window.visualViewport.addEventListener("resize", keepFieldVisible);
}
function wire() {
  wireTyping();
  $("prev-month").addEventListener("click", () => { S.month = new Date(S.month.getFullYear(), S.month.getMonth() - 1, 1); refreshData(); });
  $("next-month").addEventListener("click", () => { S.month = new Date(S.month.getFullYear(), S.month.getMonth() + 1, 1); refreshData(); });
  $("prev-week").addEventListener("click", () => { S.week = addDays(S.week, -7); refreshData(); });
  $("next-week").addEventListener("click", () => { S.week = addDays(S.week, 7); refreshData(); });
  $("apply-same").addEventListener("click", () => setApply("same"));
  $("apply-each").addEventListener("click", () => setApply("each"));
  $("mode-allday").addEventListener("click", () => { S.same.allDay = true; S.pending = null; render(); });
  $("mode-time").addEventListener("click", () => { S.same.allDay = false; refreshData(); });
  $("start-sel").addEventListener("change", (e) => {
    const t = fineTarget(); if (!t) return;
    S.pending = null; t.allDay = false; t.start = e.target.value === "" ? null : Number(e.target.value); render();
  });
  $("end-sel").addEventListener("change", (e) => {
    const t = fineTarget(); if (!t) return;
    S.pending = null; t.allDay = false; t.end = e.target.value === "" ? null : Number(e.target.value); render();
  });
  $("name").addEventListener("input", renderSummary);
  $("add").addEventListener("click", submit);
}

async function main() {
  fillTimeSelects();
  wire();
  if (!CLIENT_ID || CLIENT_ID.startsWith("{{")) {
    setStatus("ボットの準備が終わっていません。少し待ってから開き直してください", true);
    return;
  }
  setStatus("Discord と接続しています…");
  const sdk = new DiscordSDK(CLIENT_ID);
  await sdk.ready();
  setStatus("ログインを確認しています…");
  const { code } = await sdk.commands.authorize({
    client_id: CLIENT_ID, response_type: "code", state: "", prompt: "none", scope: ["identify"],
  });
  const st = await api("api/start", { code });
  await sdk.commands.authenticate({ access_token: st.access_token });
  S.session = st.session;
  S.today = fromIso(st.today);
  S.maxDays = st.max_days;
  S.month = new Date(S.today.getFullYear(), S.today.getMonth(), 1);
  S.week = weekStart(S.today);
  const cal = $("calendar");
  for (const c of st.calendars) cal.append(new Option(c.label, c.value));
  $("editor").hidden = false; $("bar").hidden = false;
  setStatus("");
  await refreshData();
}

main().catch((e) => setStatus(e && e.message ? e.message : String(e), true));
