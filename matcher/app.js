/* あわせ暦：日程あわせのページ（/m）
   ・作るページ（/m）と、調整のページ（/m/<id>）の両方をこのファイルで動かす
   ・大雑把入力：ペン（○△✕／消す）を選んで、日の「昼」「夜」をタップして塗る
   ・インポート　：ICS・CSV・JSON・テキストの予定を読み、時間だけにして送る（予定の名前は送らない）。
                 ○✕ はサーバーが予定の時間から自動で決める。△ は本人がペンで付ける
   ・利用者の入力（名前など）は必ず textContent で表示する（HTML として扱わない） */
"use strict";

const $ = (id) => document.getElementById(id);
const WD = ["日", "月", "火", "水", "木", "金", "土"];
const MARK_TEXT = { o: "○", t: "△", x: "✕" };
const SLOT_TEXT = { d: "昼", n: "夜" };
const pad = (n) => String(n).padStart(2, "0");
const fmtHM = (m) => `${pad(Math.floor(m / 60))}:${pad(m % 60)}`;
const isoOf = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const dateOf = (iso) => { const [y, m, d] = iso.split("-").map(Number); return new Date(y, m - 1, d); };
const addDays = (d, n) => { const r = new Date(d); r.setDate(r.getDate() + n); return r; };
const startOfDay = (d) => { const r = new Date(d); r.setHours(0, 0, 0, 0); return r; };
const daysBetween = (a, b) => Math.round((startOfDay(b) - startOfDay(a)) / 86400000);
const md = (iso) => { const d = dateOf(iso); return `${d.getMonth() + 1}/${d.getDate()}(${WD[d.getDay()]})`; };

function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "style") for (const [p, val] of Object.entries(v)) e.style.setProperty(p, val);
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k != null) e.append(k.nodeType ? k : document.createTextNode(String(k)));
  return e;
}

function setStatus(text, err, id = "status") {
  const s = $(id); s.textContent = text || ""; s.classList.toggle("err", !!err);
}

async function api(path, body) {
  const opt = body === undefined ? { cache: "no-store" }
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  let r, d = null;
  try { r = await fetch(path, opt); } catch (e) { throw new Error("通信できませんでした。電波の良いところでもう一度試してください"); }
  try { d = await r.json(); } catch (e) { /* 中身が JSON でない */ }
  if (!r.ok || !d || d.ok === false) throw new Error((d && d.error) || `通信がうまくいきませんでした（${r.status}）`);
  return d;
}

/* ---------- このブラウザに残すもの（編集の鍵・開いた履歴）。使えなくてもページは動く ---------- */
const memoryStore = {};
const store = {
  get(k, fallback) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : fallback; } catch (e) { return memoryStore[k] ?? fallback; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); return true; } catch (e) { memoryStore[k] = v; return false; } },
};
const keyName = (eid) => `awasegoyomi:keys:${eid}`;
const myKeys = (eid) => store.get(keyName(eid), { people: {}, admin: null });
function saveKeys(eid, keys) { if (!store.set(keyName(eid), keys)) setStatus("このブラウザでは編集の鍵を保存できません。ページを閉じると編集できなくなります。", true); }
function remember(ev) {
  const h = store.get("awasegoyomi:history", []).filter((x) => x.id !== ev.id);
  h.unshift({ id: ev.id, title: ev.title, first: ev.first, last: ev.last });
  store.set("awasegoyomi:history", h.slice(0, 20));
}

/* ---------- 配色（サーバーが決めた CSS の変数をそのまま当てる） ----------
   雰囲気は、作った人が日程あわせごとに決め（ev.theme）、見る人もこのブラウザで「明るめ」「暗め」に切り替えられる（ev.themes） */
const viewerTone = () => store.get("awasegoyomi:tone", "auto");
function themeFor(ev) {
  const t = viewerTone();
  return t !== "auto" && ev.themes && ev.themes[t] ? ev.themes[t] : ev.theme;
}
function renderTone() {
  const box = $("toneBox"); box.hidden = false;
  for (const b of box.querySelectorAll("button")) {
    b.setAttribute("aria-checked", String(b.dataset.tone === viewerTone()));
    b.onclick = () => { store.set("awasegoyomi:tone", b.dataset.tone); renderTone(); if (S.data) applyTheme(themeFor(S.data.event)); };
  }
}
function applyTheme(theme) {
  const root = document.documentElement.style;
  for (const [k, v] of Object.entries(theme || {})) root.setProperty(k, v);
  document.documentElement.dataset.themed = "1";
}
function renderPalette(p) {
  const box = $("palette"); box.textContent = ""; box.hidden = false;
  const strip = el("div", { class: "strip" });
  p.colors.forEach((c) => strip.append(el("span", { title: `${c.name} ${c.hex}`, style: { background: c.hex } })));
  box.append(strip);   // 配色の出どころの説明文は出さない（利用者の指定）
}

/* =================================================================
   作るページ
   ================================================================= */
/* ---------- タップで日を選ぶカレンダー（作るページの範囲と、候補日を足すときに使う） ----------
   box に「‹ 年月 ›」と月の表を描く。cellFn(iso, d, wd) が各日のボタンを返す */
function renderPicker(box, P, cellFn) {
  box.textContent = "";
  const ym = `${P.month.getFullYear()}-${pad(P.month.getMonth() + 1)}`;
  const move = (n) => { P.month = new Date(P.month.getFullYear(), P.month.getMonth() + n, 1); P.redraw(); };
  box.append(el("div", { class: "pnav" },
    el("button", { type: "button", class: "small", "aria-label": "前の月", text: "‹", onclick: () => move(-1) }),
    el("span", { class: "pm", text: `${P.month.getFullYear()}年${P.month.getMonth() + 1}月` }),
    el("button", { type: "button", class: "small", "aria-label": "次の月", text: "›", onclick: () => move(1) })));
  box.append(monthGrid(ym, cellFn));
}
const todayIso = () => isoOf(new Date());

/* 範囲のタップ（/date の画面と同じ決まり）：開始日 → 終了日。決まったあとは、後ろをタップで伸ばし、前をタップで開始日を移し、
   中をタップで近い方の端を動かす */
function tapRange(R, key) {
  const diff = (a, b) => daysBetween(dateOf(a), dateOf(b));
  if (R.start === null) { R.start = key; R.end = null; }
  else if (R.end === null) { if (key < R.start) R.start = key; else R.end = key; }
  else if (key > R.end) R.end = key;
  else if (key < R.start) R.start = key;
  else if (diff(R.start, key) < diff(key, R.end)) R.start = key;
  else R.end = key;
}

/* =================================================================
   作るページ
   ================================================================= */
function initCreate() {
  $("create").hidden = false;
  const now = new Date();
  const C = { month: new Date(now.getFullYear(), now.getMonth(), 1), start: null, end: null, hol: {} };
  C.redraw = () => {
    renderPicker($("cCal"), C, (iso, d, wd) => {
      const inr = C.start !== null && (C.end === null ? iso === C.start : C.start <= iso && iso <= C.end);
      const past = iso < todayIso();
      return el("button", { type: "button", disabled: past, class: "pday" + (inr ? " on" : "") + (iso === C.start || iso === C.end ? " edge" : "")
        + (C.hol[iso] || wd === 0 ? " sun" : wd === 6 ? " sat" : "") + (C.hol[iso] ? " hol" : ""), title: C.hol[iso] || "",
        "aria-pressed": String(inr), "aria-label": md(iso) + (C.hol[iso] ? " " + C.hol[iso] : ""),
        onclick: () => { tapRange(C, iso); C.redraw(); } }, String(d));
    });
    $("cSum").textContent = C.start === null ? "日程が未選択"
      : C.end === null ? `${md(C.start)}〜（終了日をタップ。同じ日をもう一度で1日だけ）`
        : `${md(C.start)}〜${md(C.end)}（${daysBetween(dateOf(C.start), dateOf(C.end)) + 1}日）`;
  };
  C.redraw();
  // 祝日の色（読めなくても作るのには困らないので、失敗は黙って無視する）
  fetch("/m/api/holidays").then((r) => r.json()).then((j) => { if (j && j.holidays) { C.hol = j.holidays; C.redraw(); } }).catch(() => {});
  const getSlots = slotPicker($("cSlots"), { d: [600, 1080], n: [1260, 1440] });   // 初期値：昼 10〜18時、夜 21〜24時
  const cStatus = (text, err) => setStatus(text, err, "cStatus");   // 作るボタンの下に出す（利用者の指定）
  $("createForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (C.start === null || C.end === null) { cStatus("カレンダーで開始日と終了日をタップしてください", true); return; }
    const btn = $("cGo"); btn.disabled = true; cStatus("配色を作っています…（30秒ほどかかることがあります）");
    try {
      const sl = getSlots();
      if (sl.d[0] >= sl.d[1] || sl.n[0] >= sl.n[1]) { cStatus("時間帯の終わりは、始まりより後にしてください", true); btn.disabled = false; return; }
      const d = await api("/m/api/create", { title: $("cTitle").value, description: $("cDesc").value, first: C.start, last: C.end, slots: sl, tone: $("cTone").value });
      saveKeys(d.id, { people: {}, admin: d.admin });
      location.href = `/m/${d.id}`;
    } catch (e) { cStatus(e.message, true); btn.disabled = false; }
  });
  const h = store.get("awasegoyomi:history", []);
  if (h.length) {
    $("history").hidden = false;
    for (const x of h) $("histList").append(el("li", {}, el("a", { href: `/m/${encodeURIComponent(x.id)}` },
      el("span", { class: "t", text: x.title }), el("span", { class: "d", text: `${md(x.first)}〜${md(x.last)}` }))));
  }
}

/* =================================================================
   予定ファイルの読み込み（あわせ暦の読み取りを引き継いだもの）
   ================================================================= */
function unfoldICS(text) { return text.replace(/\r\n/g, "\n").replace(/\n[ \t]/g, ""); }
function parseICSDate(raw) {
  const m = raw.match(/^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2}))?(Z)?$/);
  if (!m) return null;
  const [, y, mo, da, hh, mi, se, z] = m;
  if (hh === undefined) return { date: new Date(+y, +mo - 1, +da), allDay: true };
  if (z) return { date: new Date(Date.UTC(+y, +mo - 1, +da, +hh, +mi, +se || 0)), allDay: false };
  return { date: new Date(+y, +mo - 1, +da, +hh, +mi, +se || 0), allDay: false };
}
function parseICS(text) {
  const events = []; let cur = null;
  for (const line of unfoldICS(text).split("\n").map((l) => l.trim()).filter(Boolean)) {
    if (line === "BEGIN:VEVENT") { cur = {}; continue; }
    if (line === "END:VEVENT") { if (cur && cur.start) events.push(cur); cur = null; continue; }
    if (!cur) continue;
    const idx = line.indexOf(":"); if (idx === -1) continue;
    const key = line.slice(0, idx).split(";")[0], value = line.slice(idx + 1);
    if (key === "DTSTART") { const p = parseICSDate(value); if (p) { cur.start = p.date; cur.allDay = p.allDay; } }
    else if (key === "DTEND") { const p = parseICSDate(value); if (p) cur.end = p.date; }
    else if (key === "RRULE") cur.rrule = value;
    else if (key === "STATUS" && value === "CANCELLED") cur.cancelled = true;
    // TRANSP:TRANSPARENT（「予定なし」）は捨てない。Google カレンダーの終日予定は初期設定がこれで、
    // 捨てると終日の予定が入らない。ボットの空き日の判定（/2026-9 など）も、この印に関係なく数えている
  }
  return events.filter((e) => e.start && !e.cancelled).map((e) => ({
    start: e.start, end: e.end || new Date(e.start.getTime() + (e.allDay ? 86400000 : 3600000)), rrule: e.rrule || null, allDay: !!e.allDay,
  }));
}
function parseJSONEvents(text) {
  const data = JSON.parse(text); const arr = Array.isArray(data) ? data : data.events || [];
  return arr.filter((e) => e && e.start && e.end).map((e) => ({ start: new Date(e.start), end: new Date(e.end), rrule: e.rrule || null, allDay: !!e.allDay }))
    .filter((e) => !isNaN(e.start) && !isNaN(e.end));
}
function parseCSV(text) {
  const out = [];
  for (const line of text.split(/\r\n|\n/).map((l) => l.trim()).filter(Boolean)) {
    const cols = line.split(",").map((c) => c.trim());
    if (cols.length < 2 || /^(date|start|開始|日付)/i.test(cols[0])) continue;
    const [d, s, e] = cols;
    if (/^(終日|allday|all day)$/i.test(s)) { const st = new Date(`${d}T00:00:00`); if (!isNaN(st)) out.push({ start: st, end: addDays(st, 1), rrule: null, allDay: true }); continue; }
    const st = new Date(`${d}T${pad5(s)}`), en = new Date(`${d}T${pad5(e)}`);
    if (!isNaN(st) && !isNaN(en)) out.push({ start: st, end: en, rrule: null, allDay: false });
  }
  return out;
}
const pad5 = (t) => (t || "").replace(/^(\d):/, "0$1:");
/* 行の終わりの印（大雑把入力を「出力」したテキスト）：✕ か印なし＝予定、△＝△ を付ける時間、○＝予定なし */
const LINE_MARK = { "○": "o", "◯": "o", "〇": "o", "△": "t", "✕": "x", "×": "x" };
function parseSimpleLines(text) {
  const timedRe = /^(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s+(\d{1,2}:\d{2})\s*[-〜~]\s*(\d{1,2}:\d{2})(?:\s+([○◯〇△✕×]))?/;
  const allDayRe = /^(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s+(終日|allday|all day)/i;
  const out = [];
  for (const raw of text.split(/\r\n|\n/)) {
    const line = raw.trim().replace(/[０-９：／－]/g, (c) => String.fromCharCode(c.charCodeAt(0) - 0xfee0));
    const ad = line.match(allDayRe);
    if (ad) { const st = new Date(+ad[1], +ad[2] - 1, +ad[3]); out.push({ start: st, end: addDays(st, 1), rrule: null, allDay: true }); continue; }
    const m = line.match(timedRe); if (!m) continue;
    const base = `${m[1]}-${pad(+m[2])}-${pad(+m[3])}`;
    const st = new Date(`${base}T${pad5(m[4])}:00`); let en = new Date(`${base}T${pad5(m[5])}:00`);
    if (isNaN(st) || isNaN(en)) continue;
    if (en <= st) en = addDays(en, 1);                 // 23:00-1:00 のように日をまたぐ書き方
    out.push({ start: st, end: en, rrule: null, allDay: false, mark: LINE_MARK[m[6]] || "x" });
  }
  return out;
}
function detectAndParse(text, filename = "") {
  const name = filename.toLowerCase();
  if (name.endsWith(".ics") || text.includes("BEGIN:VCALENDAR")) return { events: parseICS(text), format: "ICS" };
  try { const j = JSON.parse(text); if (Array.isArray(j) || j.events) return { events: parseJSONEvents(text), format: "JSON" }; } catch (_) { /* JSON ではない */ }
  if (name.endsWith(".csv") || /^\s*\d{4}-\d{2}-\d{2}\s*,/.test(text)) { const ev = parseCSV(text); if (ev.length) return { events: ev, format: "CSV" }; }
  const simple = parseSimpleLines(text);
  if (simple.length) return { events: simple, format: simple.some((e) => e.mark && e.mark !== "x") || text.includes(EXPORT_HEAD) ? "あわせ暦の出力" : "テキスト" };
  throw new Error("形式を読み取れませんでした（ヘルプの書き方を見てください）");
}
const WD_CODE = { SU: 0, MO: 1, TU: 2, WE: 3, TH: 4, FR: 5, SA: 6 };
const startOfWeek = (d) => { const r = startOfDay(d); r.setDate(r.getDate() - r.getDay()); return r; };
function occurrenceOn(ev, day) {
  const start = ev.start, dur = ev.end - ev.start;
  const params = {}; ev.rrule.split(";").forEach((p) => { const [k, v] = p.split("="); params[k] = v; });
  const interval = parseInt(params.INTERVAL || "1", 10), count = params.COUNT ? parseInt(params.COUNT, 10) : null;
  const until = params.UNTIL ? parseICSDate(params.UNTIL)?.date : null;
  const diff = daysBetween(start, day);
  if (diff < 0 || (until && startOfDay(day) > startOfDay(until))) return null;
  let ok = false, idx = 0;
  if (params.FREQ === "DAILY") { if (diff % interval === 0) { ok = true; idx = diff / interval; } }
  else if (params.FREQ === "WEEKLY") {
    const by = params.BYDAY ? params.BYDAY.split(",").map((c) => WD_CODE[c.trim().slice(-2).toUpperCase()]).filter((v) => v !== undefined) : [start.getDay()];
    const w = Math.round((startOfWeek(day) - startOfWeek(start)) / (7 * 86400000));
    if (w >= 0 && w % interval === 0 && by.includes(day.getDay())) { ok = true; idx = Math.floor(w / interval) * by.length; }
  } else if (params.FREQ === "MONTHLY") {
    const mdiff = (day.getFullYear() - start.getFullYear()) * 12 + day.getMonth() - start.getMonth();
    if (day.getDate() === start.getDate() && mdiff >= 0 && mdiff % interval === 0) { ok = true; idx = mdiff / interval; }
  } else if (params.FREQ === "YEARLY") {
    const y = day.getFullYear() - start.getFullYear();
    if (day.getMonth() === start.getMonth() && day.getDate() === start.getDate() && y >= 0 && y % interval === 0) { ok = true; idx = y / interval; }
  } else ok = diff === 0;
  if (!ok || (count !== null && idx >= count)) return null;
  const s = new Date(day); s.setHours(start.getHours(), start.getMinutes(), start.getSeconds(), 0);
  return { start: s, end: new Date(s.getTime() + dur) };
}
/* 1つの予定が、ある日のどの時間（分）を埋めるか。終日の予定はその日まるごと */
function intervalsOn(ev, day) {
  const d0 = startOfDay(day), d1 = addDays(d0, 1);
  if (ev.allDay) {
    if (ev.rrule) return occurrenceOn(ev, day) ? [[0, 1440]] : [];
    return d0 >= startOfDay(ev.start) && d0 < ev.end ? [[0, 1440]] : [];
  }
  let s = ev.start, e = ev.end;
  if (ev.rrule) { const occ = occurrenceOn(ev, day); if (!occ) return []; s = occ.start; e = occ.end; }
  const a = Math.max(s, d0), b = Math.min(e, d1);
  return b > a ? [[Math.round((a - d0) / 60000), Math.round((b - d0) / 60000)]] : [];
}
function mergeIntervals(list) {
  const out = [];
  for (const [s, e] of [...list].sort((x, y) => x[0] - y[0] || x[1] - y[1])) {
    const last = out[out.length - 1];
    if (last && s <= last[1]) last[1] = Math.max(last[1], e); else out.push([s, e]);
  }
  return out;
}
function busyFromEvents(events, days) {
  const out = {};
  for (const iso of days) {
    const day = dateOf(iso), list = [];
    for (const ev of events) list.push(...intervalsOn(ev, day));
    if (list.length) out[iso] = mergeIntervals(list).slice(0, 48);
  }
  return out;
}
function unionBusy(a, b) {
  const out = {};
  for (const iso of new Set([...Object.keys(a), ...Object.keys(b)])) out[iso] = mergeIntervals([...(a[iso] || []), ...(b[iso] || [])]).slice(0, 48);
  return out;
}

/* ---------- ○△✕ の判定（サーバーの busy_mark / resolve_mark と同じ） ---------- */
let SLOT_RANGE = { d: [600, 1080], n: [1260, 1440] };   // 日程あわせごとの昼・夜（サーバーから受け取って置き換える）
const slotHours = (slot) => `${SLOT_RANGE[slot][0] / 60}〜${SLOT_RANGE[slot][1] / 60}時`;
const hourText = (h) => (h > 24 ? `${h}時（翌${h - 24}時）` : `${h}時`);
const slotLabel = (slot) => `${SLOT_TEXT[slot]}（${slotHours(slot)}）`;
const isFixed = (p, iso, slot) => !!((p.fixed || {})[iso] || {})[slot];
/* 昼・夜の時間帯を選ぶ欄（1時間きざみ）。box に4つのメニューを描き、値を返す関数を返す */
function slotPicker(box, slots, onChange) {
  box.textContent = "";
  const sel = (val, lo, hi, label) => {
    const s = el("select", { "aria-label": label, onchange: onChange || null },
      ...Array.from({ length: hi - lo + 1 }, (_, i) => lo + i).map((h) => el("option", { value: h * 60, selected: h * 60 === val, text: hourText(h) })));
    return s;
  };
  const rows = {};
  for (const slot of ["d", "n"]) {
    // 夜は日をまたいで 27時（翌3時）まで選べる（利用者の指定）。昼は 0〜24時
    const top = slot === "n" ? 27 : 24;
    const a = sel(slots[slot][0], 0, top - 1, `${SLOT_TEXT[slot]}の始まり`), b = sel(slots[slot][1], 1, top, `${SLOT_TEXT[slot]}の終わり`);
    rows[slot] = [a, b];
    box.append(el("div", { class: "slot-row" }, el("span", { class: "lbl", text: SLOT_TEXT[slot] }), a, el("span", { text: "〜" }), b));
  }
  return () => ({ d: rows.d.map((x) => Number(x.value)), n: rows.n.map((x) => Number(x.value)) });
}
/* サーバーの busy_mark と同じ。夜が 24時を越えるとき（例：21〜27時）は、越えた分を翌日の 0時からの予定で調べる */
function busyMark(ivs, slot, nextIvs) {
  const [s0, e0] = SLOT_RANGE[slot];
  if ((ivs || []).some(([s, e]) => s < Math.min(e0, 1440) && e > s0)) return "x";
  if (e0 > 1440 && (nextIvs || []).some(([s, e]) => s < e0 - 1440 && e > Math.max(s0 - 1440, 0))) return "x";
  return "o";
}
const nextIso = (iso) => isoOf(addDays(dateOf(iso), 1));
function autoMark(p, iso, slot) { return p.mode === "detail" && p.busy_set ? busyMark(p.busy[iso], slot, p.busy[nextIso(iso)]) : null; }
/* 予定を受け取る日：候補日と、その翌日（夜が 24時を越えるときの未明の分） */
const busyDays = () => [...new Set(S.data.event.days.flatMap((iso) => [iso, nextIso(iso)]))].sort();
function resolveMark(p, iso, slot) { return (p.marks[iso] || {})[slot] || autoMark(p, iso, slot); }
/* 枠の濃さ（サーバーの heat_level と同じ）：✕ が1人でもいる・誰も入力していない → 白（0）、
   入力した全員が ○ → いちばん濃い（5）、○ と △ だけ → 入力した人数に対する ○ の割合で 1〜4 */
function heatLevel(c) {
  const entered = c.o + c.t + c.x;
  if (c.x > 0 || entered === 0) return 0;
  if (c.o === entered) return 5;
  return 1 + Math.min(3, Math.floor(c.o / entered * 4));
}

/* =================================================================
   調整のページ
   ================================================================= */
const S = {
  eid: document.body.dataset.event, data: null, keys: null, current: null, bulkDates: new Set(), tapDates: new Set(), pickMode: false, bulkSlots: new Set(["d", "n"]),
  unsaved: false, saving: false, selectedDay: null, pending: null, joining: false, roughTab: "input",
};

function me() { return S.data && S.data.people.find((p) => p.id === S.current); }

function initEvent() {
  S.keys = myKeys(S.eid);
  // 別の端末で編集するためのリンク（#p=参加者.鍵）と、作った人のリンク（#a=鍵）を受け取る
  const h = new URLSearchParams(location.hash.slice(1));
  if (h.get("p")) { const [pid, key] = h.get("p").split("."); if (pid && key) S.keys.people[pid] = key; }
  if (h.get("a")) S.keys.admin = h.get("a");
  if (location.hash) { saveKeys(S.eid, S.keys); history.replaceState(null, "", location.pathname); }
  $("joinForm").addEventListener("submit", onJoin);
  $("jCancel").addEventListener("click", () => { S.joining = false; renderMine(); });
  // コピーする URL には、中身が変わるたびに変わる印（?v=…）を付ける。Discord はプレビューを約30分キャッシュするが、
  // URL が変われば取り直すので、貼り直すと最新のプレビューになる
  $("copyUrl").addEventListener("click", () => copyText(`${location.origin}${location.pathname}?v=${S.data ? S.data.event.version : ""}`, $("copyUrl"), "コピーしました"));
  $("saveImg").href = `/m/${S.eid}/image.png`;
  $("saveImg").setAttribute("download", "awasegoyomi.png");
  $("removeEvent").addEventListener("click", onRemoveEvent);
  $("editForm").addEventListener("submit", onSaveEdit);
  $("eCancel").addEventListener("click", () => { E.dirty = false; E.add.clear(); E.rm.clear(); renderEdit(); setStatus("", false, "eStatus"); });
  $("jumpMine").addEventListener("click", jumpToMine);
  for (const id of ["eTitle", "eDesc"]) $(id).addEventListener("input", () => { E.dirty = true; });
  load(true);
  // ほかの人の入力を30秒ごとに取り込む。入力欄を操作している間と、保存を待っている間は描き直さない
  const idle = () => document.visibilityState === "visible" && !S.saving && !S.unsaved
    && !(document.activeElement && document.activeElement.closest && document.activeElement.closest("#mine input, #mine textarea, #mine select, #editBox input, #editBox textarea"))
    && !E.dirty;
  setInterval(() => { if (idle()) load(false); }, 30000);
  document.addEventListener("visibilitychange", () => { if (idle()) load(false); });
}

async function load(first) {
  try {
    const path = `/m/api/event/${encodeURIComponent(S.eid)}`;
    const d = S.keys.admin ? await api(`${path}/view`, { admin: S.keys.admin }) : await api(path);
    accept(d);
    if (first) setStatus("");
  } catch (e) {
    if (first) { setStatus(e.message, true); $("title").textContent = "日程あわせが見つかりません"; $("lead").textContent = ""; }
  }
}

/* サーバーの答えを受け取って画面を描き直す。まだ保存していない自分の入力は、サーバーの値で上書きしない */
const OWN_FIELDS = ["name", "memo", "mode", "marks", "fixed"];
function accept(d) {
  const mine = me();
  const keep = mine && S.unsaved ? Object.fromEntries(OWN_FIELDS.map((k) => [k, mine[k]])) : null;
  S.data = d; SLOT_RANGE = d.slots || SLOT_RANGE;
  $("lgD").textContent = `上＝${slotLabel("d")}`; $("lgN").textContent = `下＝${slotLabel("n")}`;
  const ev = d.event;
  // 鍵を持っているのに、もういない参加者（削除された）は忘れる
  for (const pid of Object.keys(S.keys.people)) if (!d.people.some((p) => p.id === pid)) delete S.keys.people[pid];
  if (!S.current || !d.people.some((p) => p.id === S.current)) S.current = Object.keys(S.keys.people).find((pid) => d.people.some((p) => p.id === pid)) || null;
  if (keep && me() && me().id === mine.id) Object.assign(me(), keep);
  document.title = `${ev.title} — あわせ暦`;
  $("title").textContent = ev.title;
  const contiguous = daysBetween(dateOf(ev.first), dateOf(ev.last)) + 1 === ev.days.length;
  $("lead").textContent = `${md(ev.first)} 〜 ${md(ev.last)}${contiguous ? "" : `（候補 ${ev.days.length}日）`}・${d.people.length}人が入力`;
  $("desc").textContent = ev.description || ""; $("desc").hidden = !ev.description;
  applyTheme(themeFor(ev)); renderTone(); renderPalette(ev.palette); remember(ev);
  $("jumpRow").hidden = Object.keys(S.keys.people).length > 0;   // このブラウザでまだ参加していない人にだけ出す
  $("event").hidden = false;
  renderBest(); renderOverview(); renderDay(); renderPeople(); renderMine(); renderEdit();
}

/* ---------- 候補 ---------- */
function renderBest() {
  const ol = $("best"); ol.textContent = "";
  const n = S.data.people.length;
  if (!n) { $("bestNote").textContent = "まだ誰も入力していません。下の「入力」から参加してください。"; return; }
  if (!S.data.best.length) { $("bestNote").textContent = "○ か △ の付いた枠がまだありません。"; return; }
  $("bestNote").textContent = "✕（と未入力）の人が少ない順、次に ○ の多い順です。";
  for (const b of S.data.best) {
    const all = b.o === n, ok = b.x + b["-"] === 0;
    ol.append(el("li", {}, el("button", { type: "button", class: "best-item" + (all ? " all" : ok ? " ok" : ""), onclick: () => selectDay(b.day) },
      el("span", { class: "when", text: `${md(b.day)} ${SLOT_TEXT[b.slot]}` }),
      el("span", { class: "counts" }, countChip("o", b.o), countChip("t", b.t), countChip("x", b.x), b["-"] ? el("span", { class: "c none", text: `未${b["-"]}` }) : null),
      !all && ok ? el("span", { class: "badge soft", text: "✕なし" }) : null)));   // 「★ 全員 ○」は出さない（利用者の指定）
  }
}
const countChip = (m, v) => el("span", { class: `c m-${m}`, text: `${MARK_TEXT[m]}${v}` });

/* ---------- みんなの空き（カレンダー） ---------- */
function monthsOf(days) {
  const out = []; const seen = new Set();
  for (const iso of days) { const k = iso.slice(0, 7); if (!seen.has(k)) { seen.add(k); out.push(k); } }
  return out;
}
function monthGrid(ym, cellFn, headFn) {
  const [y, m] = ym.split("-").map(Number);
  const wrap = el("div", { class: "month" }, el("h3", { class: "mh" }, el("span", { class: "y", text: `${y}` }), el("span", { class: "m", text: `${m}月` })));
  const grid = el("div", { class: "grid" });
  WD.forEach((w, i) => grid.append(headFn ? headFn(w, i) : el("div", { class: "wd" + (i === 0 ? " sun" : i === 6 ? " sat" : ""), text: w })));
  const first = new Date(y, m - 1, 1), lastDay = new Date(y, m, 0).getDate();
  for (let i = 0; i < first.getDay(); i++) grid.append(el("div", { class: "blank" }));
  for (let d = 1; d <= lastDay; d++) grid.append(cellFn(isoOf(new Date(y, m - 1, d)), d, new Date(y, m - 1, d).getDay()));
  wrap.append(grid);
  return wrap;
}
function renderOverview() {
  const box = $("overview"); box.textContent = "";
  const days = new Set(S.data.event.days), n = S.data.people.length;
  for (const ym of monthsOf(S.data.event.days)) box.append(monthGrid(ym, (iso, d, wd) => {
    if (!days.has(iso)) return el("div", { class: "cell out" }, el("span", { class: "dn", text: d }));
    const halves = ["d", "n"].map((slot) => {
      // 内訳はふだん出さない。カーソルを合わせると出し（title と CSS の :hover）、タップすると下に日の詳細を出す
      const c = S.data.tally[iso][slot], lv = heatLevel(c);
      const label = n ? `○${c.o}△${c.t}✕${c.x}` : "";   // マスが狭いので詰めて書く（title には読みやすい形で）
      return el("span", { class: `half lv${lv}`, title: n ? `${SLOT_TEXT[slot]} ○${c.o} △${c.t} ✕${c.x}` : "",
        style: { background: `var(--heat${lv})`, color: `var(--heat-ink${lv})` } }, el("span", { class: "lbl", text: label }));
    });
    const t = S.data.tally[iso];
    return el("button", { type: "button", class: "cell in" + (S.selectedDay === iso ? " sel" : "") + dayClass(iso, wd), title: holName(iso),
      "aria-label": `${md(iso)}${holName(iso) ? " " + holName(iso) : ""} 昼 ○${t.d.o} △${t.d.t} ✕${t.d.x}、夜 ○${t.n.o} △${t.n.t} ✕${t.n.x}`, onclick: () => selectDay(iso) },
    el("span", { class: "dn", text: d }), ...halves);
  }));
}
function selectDay(iso) {
  S.selectedDay = iso; renderOverview(); renderDay();
  $("day").scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "nearest" });
}
function renderDay() {
  // 日の詳細：昼・夜の ○△✕ の内訳だけ（利用者の指定）
  const box = $("day"), iso = S.selectedDay;
  if (!iso || !S.data.event.days.includes(iso)) { box.hidden = true; return; }
  box.hidden = false; box.textContent = "";
  const t = S.data.tally[iso];
  box.append(el("div", { class: "head-row" }, el("h3", { text: md(iso) + (holName(iso) ? ` ${holName(iso)}` : "") }), el("button", { type: "button", class: "small", text: "閉じる", onclick: () => { S.selectedDay = null; renderOverview(); renderDay(); } })));
  const count = (c) => `○${c.o} △${c.t} ✕${c.x}${c["-"] ? ` 未${c["-"]}` : ""}`;
  box.append(el("p", { class: "note", text: `昼 ${count(t.d)}　／　夜 ${count(t.n)}` }));
  if (!S.data.people.length) return;
  const table = el("div", { class: "dtable" });
  table.append(el("span", { class: "th", text: "名前" }), el("span", { class: "th c", text: "昼" }), el("span", { class: "th c", text: "夜" }));
  for (const p of S.data.people) {
    table.append(el("span", { class: "nm" }, el("i", { class: "dot", style: { background: p.color } }), el("span", { text: p.name })));
    for (const slot of ["d", "n"]) {
      const m = resolveMark(p, iso, slot);
      table.append(el("span", { class: `mk m-${m || "none"}`, text: m ? MARK_TEXT[m] : "−", title: m ? "" : "未入力" }));
    }
  }
  box.append(table);
}

/* ---------- タイトル・概要・候補日を直す（誰でも。候補日を消すのは作った人だけ） ---------- */
const E = { month: null, add: new Set(), rm: new Set(), dirty: false };
function renderEdit() {
  const ev = S.data.event, admin = !!S.keys.admin && S.data.admin;
  if (!E.dirty) {
    $("eTitle").value = ev.title; $("eDesc").value = ev.description || "";
    E.getSlots = slotPicker($("eSlots"), S.data.slots, () => { E.dirty = true; });
    $("eTone").value = ev.tone || "auto"; $("eTone").onchange = () => { E.dirty = true; };
  }
  if (!E.month) { const f = dateOf(ev.first); E.month = new Date(f.getFullYear(), f.getMonth(), 1); }
  $("eNote").textContent = admin
    ? "足したい日をタップしてください。今の候補日（色の付いた日）をタップすると、消す日になります。もう一度タップで取り消し。"
    : "足したい日をタップしてください（もう一度タップで取り消し）。";
  const cand = new Set(ev.days);
  E.redraw = renderEdit;
  renderPicker($("eCal"), E, (iso, d, wd) => {
    const isCand = cand.has(iso), past = iso < todayIso();
    const st = E.rm.has(iso) ? " rm" : E.add.has(iso) ? " add" : isCand ? " on" : "";
    return el("button", { type: "button", disabled: past && !isCand, class: "pday" + st + pickDayClass(iso, wd), title: pickHolName(iso),
      "aria-label": md(iso) + (isCand ? "（候補日）" : "") + (E.add.has(iso) ? "（足す）" : "") + (E.rm.has(iso) ? "（消す）" : ""),
      onclick: () => {
        if (isCand) {
          if (!admin) { setStatus("候補日を消せるのは、作った人だけです", true, "eStatus"); return; }
          if (E.rm.has(iso)) E.rm.delete(iso); else E.rm.add(iso);
        } else if (E.add.has(iso)) E.add.delete(iso); else E.add.add(iso);
        E.dirty = true; setStatus("", false, "eStatus"); renderEdit();
      } }, String(d));
  });
  const parts = [];
  if (E.add.size) parts.push(`足す：${[...E.add].sort().map(md).join("・")}`);
  if (E.rm.size) parts.push(`消す：${[...E.rm].sort().map(md).join("・")}（その日の入力も消えます）`);
  $("eSum").textContent = parts.join("\n");
}
async function onSaveEdit(ev) {
  ev.preventDefault();
  if (E.rm.size && !confirm(`${E.rm.size}日の候補日を消します。その日の全員の入力も見えなくなります。`)) return;
  try {
    const sl = E.getSlots();
    if (sl.d[0] >= sl.d[1] || sl.n[0] >= sl.n[1]) { setStatus("時間帯の終わりは、始まりより後にしてください", true, "eStatus"); return; }
    const d = await api(`/m/api/event/${S.eid}/edit`, { title: $("eTitle").value, description: $("eDesc").value, slots: sl, tone: $("eTone").value,
      add_days: [...E.add], remove_days: [...E.rm], admin: S.keys.admin });
    E.dirty = false; E.add.clear(); E.rm.clear();
    accept(d); setStatus("保存しました", false, "eStatus");
  } catch (e) { setStatus(e.message, true, "eStatus"); }
}

/* ---------- 参加者 ---------- */
function renderPeople() {
  const ul = $("people"); ul.textContent = "";
  const admin = !!S.keys.admin && S.data.admin;
  $("adminRow").hidden = !admin;
  if (!S.data.people.length) ul.append(el("li", { class: "note", text: "まだいません" }));
  for (const p of S.data.people) {
    // 誰でも、どの参加者の入力も編集・削除できる（利用者の指定）。「編集する」で「入力」の欄にその人を出す
    const own = !!S.keys.people[p.id];
    ul.append(el("li", { class: "person" + (p.id === S.current && !S.joining ? " own" : "") },
      el("i", { class: "dot", style: { background: p.color } }), el("span", { class: "nm", text: p.name }),
      own ? el("span", { class: "tag own", text: "あなた" }) : null,
      el("button", { type: "button", class: "small", text: "編集する", onclick: () => pickPerson(p.id, true) }),
      el("button", { type: "button", class: "small danger", text: "削除", "aria-label": `${p.name} の入力を削除`, onclick: () => onDeletePerson(p, "pStatus") }),
      p.memo ? el("span", { class: "pmemo", text: p.memo }) : null));
  }
}
/* 結果は押したボタンの近く（参加者の一覧の下か、入力の欄）に出す */
async function onDeletePerson(p, where) {
  if (!confirm(`「${p.name}」の入力を削除します。元に戻せません。`)) return;
  try { accept(await api(`/m/api/event/${S.eid}/delete`, { pid: p.id, key: S.keys.people[p.id] || null, admin: S.keys.admin })); setStatus(`「${p.name}」の入力を削除しました`, false, where); }
  catch (e) { setStatus(e.message, true, where); }
}
async function onRemoveEvent() {
  if (!confirm("この日程あわせを、全員の入力ごと削除します。元に戻せません。")) return;
  try { await api(`/m/api/event/${S.eid}/remove`, { admin: S.keys.admin }); store.set(keyName(S.eid), { people: {}, admin: null }); location.href = "/m"; }
  catch (e) { setStatus(e.message, true, "pStatus"); }
}

/* ---------- あなたの入力 ---------- */
async function onJoin(ev) {
  ev.preventDefault();
  const mode = document.querySelector('input[name="jMode"]:checked').value;
  try {
    const d = await api(`/m/api/event/${S.eid}/join`, { name: $("jName").value, mode, admin: S.keys.admin });
    S.keys.people[d.joined] = d.key; saveKeys(S.eid, S.keys);
    S.current = d.joined; S.joining = false; $("jName").value = "";
    document.querySelector('input[name="jMode"][value="rough"]').checked = true;   // 次に参加する人は大雑把入力から
    accept(d); setStatus(mode === "rough" ? "参加しました。日の「昼」「夜」のメニューから ○△✕ を選び、「保存する」を押してください" : "参加しました。予定を読み込んでください", false, "mStatus");
  } catch (e) { setStatus(e.message, true, "mStatus"); }
}

/* 見出しの下の「自分の都合を入力する」：入力の欄へ移り、名前の欄に入る（ほかの人を編集していたら、新しく参加するに切り替える） */
async function jumpToMine() {
  if ($("joinForm").hidden && !(await pickPerson(null))) return;
  $("mine").scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
  $("jName").focus({ preventScroll: true });
}

/* 「入力」の欄で編集する人を選ぶ。誰でも、どの参加者でも選べる（ブラウザの記録を消しても直せるように） */
async function pickPerson(pid, scroll) {
  if (pid !== S.current && !(await settleUnsaved())) return false;
  if (pid !== S.current) { S.undo = null; S.roughTab = "input"; setStatus("", false, "mStatus"); }
  S.current = pid; S.joining = !pid;
  renderMine(); renderPeople();
  if (scroll) $("mine").scrollIntoView({ behavior: "smooth", block: "start" });
  return true;
}
function renderMine() {
  const box = $("myTabs"); box.textContent = "";
  if (S.data.people.length) {
    const sel = el("select", { id: "whoSel", "aria-label": "編集する人", onchange: (e) => pickPerson(e.target.value || null) },
      el("option", { value: "", selected: S.joining || !me(), text: "新しく参加する" }),
      ...S.data.people.map((p) => el("option", { value: p.id, selected: !S.joining && p.id === S.current, text: p.name + (S.keys.people[p.id] ? "（あなた）" : "") })));
    box.append(el("label", { class: "field who" }, el("span", { text: "編集する人（誰の入力でも直せます）" }), sel));
  }
  const showJoin = S.joining || !me();
  $("joinForm").hidden = !showJoin; $("jCancel").hidden = true;
  $("editor").hidden = showJoin;
  if (!showJoin) renderEditor();
}

function renderEditor() {
  const p = me(), box = $("editor"); if (!p) return;
  const focusId = document.activeElement && document.activeElement.id;
  box.textContent = "";
  const nameIn = el("input", { id: "eName", value: p.name, "aria-label": "名前" });
  nameIn.addEventListener("input", () => { p.name = nameIn.value; markUnsaved(); });
  box.append(el("div", { class: "row name-row" }, el("label", { class: "field grow" }, el("span", { text: "名前" }), nameIn)));
  const memoIn = el("textarea", { id: "eMemo", rows: 2, placeholder: "例：21時以降なら行けます", "aria-label": "メモ" });
  memoIn.value = p.memo || "";
  memoIn.addEventListener("input", () => { p.memo = memoIn.value; markUnsaved(); });
  box.append(el("label", { class: "field" }, el("span", { text: "メモ（みんなに見えます）" }), memoIn));

  const seg = el("div", { class: "seg two", role: "radiogroup", "aria-label": "入力のしかた" });
  for (const [mode, label, sub] of [["rough", "大雑把入力", "自分で ○△✕"], ["detail", "インポート", "読み込んで ○✕"]]) {
    seg.append(el("button", { type: "button", role: "radio", "aria-checked": String(p.mode === mode), class: "seg-btn" + (p.mode === mode ? " on" : ""),
      onclick: () => { if (p.mode !== mode) { p.mode = mode; S.unsaved = true; renderEditor(); } } }, el("span", { text: label }), el("small", { text: sub })));
  }
  box.append(seg);
  if (p.mode === "detail") box.append(detailPanel(p));
  // 大雑把入力は「入力する」「出力する」のタブに分ける。出力（書き出し）はふだん格納しておき、タブで出す（利用者の指定）
  const tab = p.mode === "rough" ? S.roughTab : "input";
  let area = box;
  if (p.mode === "rough") {
    const tabBtn = (id, label) => el("button", { type: "button", role: "tab", id: `tab-${id}`, class: "ptab", "aria-selected": String(tab === id),
      "aria-controls": "roughPanel", tabindex: tab === id ? "0" : "-1", onclick: () => setRoughTab(id),
      onkeydown: (e) => { if (e.key === "ArrowRight" || e.key === "ArrowLeft") { e.preventDefault(); const to = tab === "input" ? "export" : "input"; setRoughTab(to); $(`tab-${to}`).focus(); } } }, label);
    box.append(el("div", { class: "ptabs", role: "tablist", "aria-label": "大雑把入力" }, tabBtn("input", "入力する"), tabBtn("export", "出力する")));
    area = el("div", { class: "tabpanel", role: "tabpanel", id: "roughPanel", "aria-labelledby": `tab-${tab}` });
    box.append(area);
    if (tab === "export") area.append(exportPanel(p));   // 入力内容の書き出し（ほかの日程あわせのインポートで使える）
  }

  // まとめて選択：日付 × 時間帯で枠を選び（自分のカレンダーに枠線で出る）、そのあと ○・△・✕・自動に戻す を押して付ける
  const clearName = p.mode === "detail" ? "自動に戻す" : "消す";
  // まとめて選択は「日付の集まり」を選ぶ。曜日のボタンは、その曜日の日をまとめて選ぶ・外す近道。
  // 曜日のボタンと「平日」は祝日を除く（利用者の指定 a）。「土日」は土日の祝日も含み、「土日祝」は土日と祝日
  const wdOn = (i) => { const ds = datesOfWeekday(i); return ds.length > 0 && ds.every((x) => S.bulkDates.has(x)); };
  const wdBtns = WD.map((w, i) => el("button", { type: "button", class: "tg wdt" + (wdOn(i) ? " on" : "") + (i === 0 ? " sun" : i === 6 ? " sat" : ""),
    "aria-pressed": String(wdOn(i)), onclick: () => { toggleWeekday(i); renderEditor(); } }, w));
  // 全部・平日・土日・土日祝：押すと、その日だけが選ばれた状態にする。すでにその日だけが選ばれているときにもう一度押すと、外す
  const same = (list) => list.length === S.bulkDates.size && list.every((x) => S.bulkDates.has(x));
  const preset = (label, list) => el("button", { type: "button", class: "small" + (list.length && same(list) ? " on" : ""), text: label,
    "aria-pressed": label === "解除" ? null : String(!!(list.length && same(list))),
    onclick: () => {
      S.bulkDates = list.length && !same(list) ? new Set(list) : new Set();
      S.tapDates = new Set();
      renderEditor();
    } });
  const evDays = S.data.event.days, wdOf = (x) => dateOf(x).getDay();
  const slotBtns = ["d", "n"].map((slot) => el("button", { type: "button", class: "tg" + (S.bulkSlots.has(slot) ? " on" : ""),
    "aria-pressed": String(S.bulkSlots.has(slot)), onclick: () => { toggleSet(S.bulkSlots, slot); renderEditor(); } }, slotLabel(slot)));
  const { targets } = bulkTargets();
  const bulk = el("div", { class: "bulk" },
    el("p", { class: "cap", text: "まとめて選択" }),
    el("div", { class: "bulk-row" }, el("span", { class: "lbl", text: "曜日" }), el("div", { class: "tgs" }, ...wdBtns),
      el("div", { class: "tgs" }, preset("全部", evDays.slice()),
        preset("平日", evDays.filter((x) => wdOf(x) >= 1 && wdOf(x) <= 5 && !isHol(x))),
        preset("土日", evDays.filter((x) => wdOf(x) === 0 || wdOf(x) === 6)),
        preset("土日祝", evDays.filter((x) => wdOf(x) === 0 || wdOf(x) === 6 || isHol(x))),
        preset("解除", []))),
    el("div", { class: "bulk-row" }, el("span", { class: "lbl", text: "日付" }),
      // オン・オフは色だけで見せる（利用者の指定）。読み上げには aria-pressed で伝える
      el("button", { type: "button", class: "tg pickmode" + (S.pickMode ? " on" : ""), "aria-pressed": String(S.pickMode),
        onclick: () => setPickMode(!S.pickMode) }, "複数選択モード"),
      el("button", { type: "button", class: "small clearsel", disabled: !S.bulkDates.size, text: "選択解除",
        onclick: () => { S.bulkDates = new Set(); S.tapDates = new Set(); renderEditor(); } }),
      el("span", { class: "note", text: S.bulkDates.size ? `${S.bulkDates.size}日を選んでいます` : "" })),
    el("div", { class: "bulk-row" }, el("span", { class: "lbl", text: "時間帯" }), el("div", { class: "tgs" }, ...slotBtns)),
    el("div", { class: "bulk-row" }, el("span", { class: "lbl", text: "選んだ枠に" }),
      el("div", { class: "tgs" }, ...[["o", "○"], ["t", "△"], ["x", "✕"], ["clear", clearName]].map(([k, label]) =>
        el("button", { type: "button", class: `apply pen-${k}`, disabled: !targets.length, onclick: () => paintBulk(k) }, label)))));
  if (tab === "input") area.append(el("p", { class: "note", text: p.mode === "detail"
    ? "予定から ○✕ が自動で付きます（メニューの「自動」）。行けるか分からない枠は、その枠のメニューで △ を選んでください。メニューで選んだ枠（太字・枠付き）は、自動の判定より優先され、曜日などのまとめて選択では変わりません（複数選択モードで選んだ日は上書きできます）。"
    : "日の「昼」「夜」のメニューから ○・△・✕ を選んでください。メニューで選んだ枠（太字・枠付き）は、曜日などのまとめて選択では変わりません（複数選択モードで選んだ日は上書きできます）。" }));
  if (tab === "input") area.append(bulk);

  // 自分のカレンダー
  const days = new Set(S.data.event.days);
  const cal = el("div", { class: "months mine" + (S.pickMode ? " picking" : "") });
  for (const ym of monthsOf(S.data.event.days)) cal.append(monthGrid(ym, (iso, d, wd) => {
    if (!days.has(iso)) return el("div", { class: "cell out" }, el("span", { class: "dn", text: d }));
    // 枠ごとのプルダウンメニュー（利用者の指定）。インポートでは「自動」（予定から決まった ○✕）も選べる
    const halves = ["d", "n"].map((slot) => {
      const manual = (p.marks[iso] || {})[slot], auto = autoMark(p, iso, slot), m = manual || auto;
      const fixed = isFixed(p, iso, slot);
      const picked = S.bulkSlots.has(slot) && S.bulkDates.has(iso) && !protectedSlot(p, iso, slot);   // まとめて選択で変わる枠
      const autoText = p.mode === "detail" ? (auto ? `自動${MARK_TEXT[auto]}` : "自動") : "−";
      // メニュー：固定していない枠は、今の状態（自動・まとめて付けた印・未入力）を先頭に出す。○△✕ を選ぶと固定になる
      const opts = [];
      if (!fixed) opts.push(["", manual ? `${MARK_TEXT[manual]}（まとめて）` : autoText]);
      opts.push(["o", "○"], ["t", "△"], ["x", "✕"]);
      if (manual) opts.push(["clear", p.mode === "detail" ? `${autoText}に戻す` : "−（消す）"]);
      const sel = el("select", { class: `mark m-${m || "none"}`, "aria-label": `${md(iso)} ${SLOT_TEXT[slot]}`,
        onchange: (e) => setMark(iso, slot, e.target.value) },
      ...opts.map(([v, t]) => el("option", { value: v, selected: fixed ? v === manual : v === "", text: t })));
      // 見た目は印だけを出し、メニューは透明にして枠いっぱいに重ねる（狭い枠でも選んだ印が読めるように）
      // fixed（太字・枠）＝プルダウンで手で選んだ枠、auto（細字）＝予定から自動
      return el("label", { class: `paint m-${m || "none"}` + (fixed ? " fixed" : manual ? "" : m ? " auto" : "") + (picked ? " picked" : ""),
        title: fixed ? "固定の枠（曜日などのまとめて選択では変わりません。複数選択モードで選ぶと上書きできます）" : "" },
        el("small", { text: SLOT_TEXT[slot] }), el("b", { "aria-hidden": "true", text: m ? MARK_TEXT[m] : "−" }), sel);
    });
    // 複数選択モードの間は、日の上に透明なボタンをかぶせて、タップでその日を選ぶ・外す（メニューは開かない）。
    // マウスのドラッグ・指のスワイプで続けて選べる。かぶせたボタンの上ではページは動かない（それ以外の場所のスワイプで上下に動く）
    const chosen = S.bulkDates.has(iso);
    const pick = S.pickMode ? el("button", { type: "button", class: "daypick", "data-iso": iso, "aria-pressed": String(chosen), "aria-label": `${md(iso)}${holName(iso) ? " " + holName(iso) : ""}を${chosen ? "外す" : "選ぶ"}`,
      onpointerdown: (e) => startDrag(e, iso),
      // マウス・指は押した時点で選ぶ（startDrag）。クリックで切り替えるのは、キーボード（Enter・スペース。detail が 0）のときだけ
      onclick: (e) => { if (e.detail !== 0) return; tapPick(iso, !chosen); renderEditor(); } }) : null;
    return el("div", { class: "cell in" + dayClass(iso, wd) + (chosen ? " daysel" : ""), title: holName(iso) },
      el("span", { class: "dn", text: d }), ...halves, pick);
  }, (w, i) => el("button", { type: "button", class: "wd" + (i === 0 ? " sun" : i === 6 ? " sat" : "") + (datesOfWeekday(i).length && datesOfWeekday(i).every((x) => S.bulkDates.has(x)) ? " on" : ""),
    "aria-pressed": String(datesOfWeekday(i).length > 0 && datesOfWeekday(i).every((x) => S.bulkDates.has(x))), title: "押すと、この曜日の日（祝日を除く）をまとめて選ぶ・外す", onclick: () => { toggleWeekday(i); renderEditor(); } }, w)));
  if (tab === "input") area.append(cal);
  // 複数選択モードの間は、画面の右下にいつでも終えられるボタンを出す（利用者の指定）
  if (S.pickMode && tab === "input") box.append(el("button", { type: "button", class: "pickfloat", "aria-label": "複数選択モードを終える",
    onclick: () => setPickMode(false) }, el("span", { text: "複数選択モード" }), el("b", { "aria-hidden": "true", text: "✕" })));
  if (p.mode === "detail" && Object.keys(p.marks).length) box.append(el("div", { class: "row" }, el("button", { type: "button", class: "small", text: "手で付けた印をすべて消す（自動に戻す）", onclick: () => { p.marks = {}; p.fixed = {}; S.unsaved = true; renderEditor(); } })));

  // 保存のバー：変更は「保存する」を押すまで反映しない（利用者の指定）
  box.append(el("div", { class: "savebar" + (S.unsaved ? " unsaved" : "") },
    el("p", { class: "status", id: "saveState", role: "status", text: S.saving ? "保存しています…" : S.unsaved ? "保存していない変更があります" : (S.savedOnce ? "保存しました" : "") }),
    el("button", { type: "button", id: "saveBtn", class: "primary", disabled: !S.unsaved || S.saving, onclick: () => saveNow({}) }, "保存する")));
  box.append(el("div", { class: "row foot-row" },
    el("button", { type: "button", class: "small", text: "新しく参加する", onclick: async () => { if (await settleUnsaved()) { S.joining = true; renderMine(); } } }),
    el("button", { type: "button", class: "small danger", text: "この入力を削除", onclick: () => onDeletePerson(p, "mStatus") })));
  if (focusId && $(focusId)) $(focusId).focus();
  // 右下の「複数選択モード ✕」を、下に貼り付く保存のバーの上に置くため、バーの高さを測っておく
  const sb = box.querySelector(".savebar");
  if (sb) document.documentElement.style.setProperty("--savebar-h", `${sb.offsetHeight}px`);
}

function setMark(iso, slot, mark) {          // 枠のメニューで選んだ印。○△✕ は「固定」（まとめて選択で変えない）、"clear" は印と固定を外す
  const p = me(); if (!p || !mark) return;
  const row = { ...(p.marks[iso] || {}) }, fx = { ...((p.fixed || {})[iso] || {}) };
  if (mark === "clear") { delete row[slot]; delete fx[slot]; } else { row[slot] = mark; fx[slot] = true; }
  if (Object.keys(row).length) p.marks[iso] = row; else delete p.marks[iso];
  p.fixed = p.fixed || {};
  if (Object.keys(fx).length) p.fixed[iso] = fx; else delete p.fixed[iso];
  S.unsaved = true; renderEditor();
}
function toggleSet(set, v) { if (set.has(v)) set.delete(v); else set.add(v); }
/* まとめて選択の対象：選んだ日付 × 選んだ時間帯の枠（日付を何も選んでいなければ無し）。
   ただし、固定の枠（プルダウンで個別に選んだ枠など）と、インポートで予定があって ✕ になっている枠は変えない（利用者の指定）。
   複数選択モードで選んだ日だけは、固定の枠も上書きできる（利用者の指定） */
function protectedSlot(p, iso, slot) {
  return (isFixed(p, iso, slot) && !S.tapDates.has(iso)) || autoMark(p, iso, slot) === "x";
}
/* 祝日（作ったときに内閣府の一覧から読み込んだもの） */
const holName = (iso) => ((S.data && S.data.event.holidays) || {})[iso] || "";
const isHol = (iso) => !!holName(iso);
const dayClass = (iso, wd) => (isHol(iso) || wd === 0 ? " sun" : wd === 6 ? " sat" : "") + (isHol(iso) ? " hol" : "");   // 祝日は日曜と同じ色
/* 「候補日を足す」カレンダーでは、候補日の外の祝日も色を付ける */
const pickHolName = (iso) => holName(iso) || ((S.data && S.data.event.picker_holidays) || {})[iso] || "";
const pickDayClass = (iso, wd) => (pickHolName(iso) || wd === 0 ? " sun" : wd === 6 ? " sat" : "") + (pickHolName(iso) ? " hol" : "");
/* 曜日のボタンが選ぶ日：その曜日の候補日のうち、祝日でない日（利用者の指定 a） */
const datesOfWeekday = (i) => S.data.event.days.filter((x) => dateOf(x).getDay() === i && !isHol(x));
function toggleWeekday(i) {
  const ds = datesOfWeekday(i);
  if (ds.length && ds.every((x) => S.bulkDates.has(x))) ds.forEach((x) => { S.bulkDates.delete(x); S.tapDates.delete(x); });
  else ds.forEach((x) => S.bulkDates.add(x));
}
function setPickMode(on) { S.pickMode = on; renderEditor(); }
/* 大雑把入力のタブ（入力する・出力する）。出力に切り替えるときは、複数選択モードを終える（右下の「終える」ボタンが残らないように） */
function setRoughTab(id) { if (id === "export") S.pickMode = false; S.roughTab = id; renderEditor(); }
/* 複数選択モードで選んだ日（S.tapDates）。ここに ○△✕ をまとめて付けると、手で選んだ枠と同じ「固定」になる（利用者の指定） */
function tapPick(iso, on) {
  if (on) { S.bulkDates.add(iso); S.tapDates.add(iso); } else { S.bulkDates.delete(iso); S.tapDates.delete(iso); }
}
/* 複数選択モード：押した日を選ぶ・外し、そのまま PC はマウスのドラッグ、スマホは指のスワイプで、通った日をまとめて選ぶ・外す
   （マウスは左ボタンを押している間だけ。選ばれている日から始めると外す）。
   動かしている間は描き直さず、印だけを付け替えて、離したときに描き直す */
function startDrag(e, iso) {
  if (e.pointerType === "mouse" && e.button !== 0) return;
  e.preventDefault();
  const on = !S.bulkDates.has(iso), seen = new Set();
  const mark = (x) => {
    if (!x || seen.has(x)) return;
    seen.add(x); tapPick(x, on);
    const b = document.querySelector(`.daypick[data-iso="${x}"]`);
    if (b) { b.setAttribute("aria-pressed", String(on)); b.closest(".cell").classList.toggle("daysel", on); }
  };
  mark(iso);
  const move = (ev) => { const t = document.elementFromPoint(ev.clientX, ev.clientY); mark(t && t.closest && t.closest(".daypick") && t.closest(".daypick").dataset.iso); };
  const up = () => {
    document.removeEventListener("pointermove", move); document.removeEventListener("pointerup", up); document.removeEventListener("pointercancel", up);
    renderEditor();
  };
  document.addEventListener("pointermove", move); document.addEventListener("pointerup", up); document.addEventListener("pointercancel", up);
}
function bulkTargets() {
  const p = me(), targets = [];
  let skipped = 0;
  if (!p) return { targets, skipped };
  for (const iso of S.data.event.days) {
    if (!S.bulkDates.has(iso)) continue;
    for (const slot of ["d", "n"]) {
      if (!S.bulkSlots.has(slot)) continue;
      if (protectedSlot(p, iso, slot)) skipped++; else targets.push([iso, slot]);
    }
  }
  return { targets, skipped };
}
function paintBulk(mark) {
  const p = me(); if (!p) return;
  p.fixed = p.fixed || {};
  for (const [iso, slot] of bulkTargets().targets) {
    const row = { ...(p.marks[iso] || {}) };
    if (mark === "clear") delete row[slot]; else row[slot] = mark;
    if (Object.keys(row).length) p.marks[iso] = row; else delete p.marks[iso];
    // 複数選択モードで選んだ日に付けた ○△✕ は固定にする：あとの曜日などのまとめて選択や、予定からの自動の ○✕ で変わらない。
    // 複数選択モードで選んだ日を「消す」（自動に戻す）ときは、固定も外す
    if (S.tapDates.has(iso)) {
      const fx = { ...(p.fixed[iso] || {}) };
      if (mark === "clear") delete fx[slot]; else fx[slot] = true;
      if (Object.keys(fx).length) p.fixed[iso] = fx; else delete p.fixed[iso];
    }
  }
  S.unsaved = true; renderEditor();
}
/* 文字の入力では描き直さず、保存のバーだけを「保存していない変更があります」にする（入力中の欄を消さないため） */
function markUnsaved() {
  S.unsaved = true;
  const st = $("saveState"); if (st) st.textContent = "保存していない変更があります";
  const b = $("saveBtn"); if (b) b.disabled = S.saving;
  const bar = st && st.closest(".savebar"); if (bar) bar.classList.add("unsaved");
}
/* 保存していない変更があるときに、別の人へ切り替える前に聞く。続けてよければ true */
async function settleUnsaved() {
  if (!S.unsaved) return true;
  if (confirm("保存していない変更があります。保存しますか？（キャンセルで変更を捨てます）")) return saveNow({});
  S.unsaved = false; await load(false); return true;
}
/* 保存。まだ保存していない名前・メモ・入力のしかた・印も一緒に送る（「この時間を共有する」などのときも） */
async function saveNow(extra) {
  const p = me(); if (!p) return false;
  const changes = S.unsaved ? { name: p.name, memo: p.memo || "", mode: p.mode, marks: p.marks, fixed: p.fixed || {}, ...extra } : extra;
  if (!Object.keys(changes).length) return true;
  S.saving = true;
  const st = $("saveState"); if (st) st.textContent = "保存しています…";
  const b = $("saveBtn"); if (b) b.disabled = true;
  try {
    const d = await api(`/m/api/event/${S.eid}/save`, { pid: p.id, key: S.keys.people[p.id], admin: S.keys.admin, changes });
    // 予定を1つ消したときだけ「元に戻す」を出す。ほかの予定の変更をしたら、戻す先が古くなるので消す
    if ("busy" in changes) S.undo = S.undoNext || null;
    S.undoNext = null;
    S.saving = false; S.unsaved = false; S.savedOnce = true; accept(d);
    return true;
  } catch (e) {
    S.saving = false; S.undoNext = null;
    // 理由は、画面の下に留まる保存のバーに出す（ページの上の知らせはスマホでは見えないため）
    const st2 = $("saveState");
    if (st2) { st2.textContent = `保存できませんでした（${e.message}）。` + (Object.keys(extra).length ? "もう一度押してください" : "もう一度「保存する」を押してください"); st2.classList.add("err"); }
    const b2 = $("saveBtn"); if (b2) b2.disabled = false;
    return false;
  }
}
window.addEventListener("beforeunload", (e) => { if (S.unsaved) { e.preventDefault(); e.returnValue = ""; } });

/* ---------- インポート：予定の読み込みと、共有中の予定 ---------- */
function detailPanel(p) {
  const box = el("div", { class: "detail" });
  const file = el("input", { type: "file", accept: ".ics,.json,.csv,.txt,text/calendar,application/json,text/csv,text/plain", class: "visually-hidden", id: "dFile" });
  file.addEventListener("change", async () => {
    const f = file.files && file.files[0]; if (!f) return;
    try { readEvents(await f.text(), f.name); } catch (e) { setStatus(`読み込めませんでした：${e.message}`, true, "dStatus"); }
    file.value = "";
  });
  const paste = el("textarea", { id: "dPaste", rows: 4, placeholder: "例：\n2026-12-04 19:00-20:30\n2026-12-05 終日\n（ICS の中身をそのまま貼っても読めます）" });
  paste.value = S.pasteText || "";
  paste.addEventListener("input", () => { S.pasteText = paste.value; });
  box.append(el("p", { class: "note", text: "Google カレンダーなどから書き出した .ics を選ぶか、予定を貼り付けてください。予定の名前は読み込んだ時点で捨て、時間だけを送ります。ほかの日程あわせの大雑把入力で「出力」したファイル・テキストも読めます（○△✕ を取り込みます）。" }),
    el("div", { class: "row" }, el("label", { class: "btn", for: "dFile" }, "ファイルを選ぶ（.ics/.csv/.json/.txt）"), file),
    paste,
    el("div", { class: "row" }, el("button", { type: "button", text: "貼り付けた内容を読む", onclick: () => { try { if (paste.value.trim()) { readEvents(paste.value, ""); S.pasteText = ""; } } catch (e) { setStatus(`読み込めませんでした：${e.message}`, true, "dStatus"); } } }),
      el("button", { type: "button", class: "small", text: "書き方", onclick: () => { const h = $("dHelp"); h.hidden = !h.hidden; } })),
    el("div", { class: "help", id: "dHelp", hidden: true },
      el("p", { text: "ICS：Google カレンダーの「設定」→ 対象のカレンダー →「エクスポート」で書き出せます（繰り返しの予定にも対応）。" }),
      el("p", { text: "テキスト：1行に1件、「2026-12-04 19:00-20:30」か「2026-12-05 終日」。" }),
      el("p", { text: "CSV：「2026-12-04,19:00,20:30」か「2026-12-05,終日」。" }),
      el("p", { text: "終日の予定は、その日の昼も夜も ✕ になります。" }),
      el("p", { text: "あわせ暦の出力：大雑把入力の「入力内容を出力」で作ったテキスト（「2026-12-04 10:00-18:00 △」のように行の終わりに ○△✕）。✕ は予定、△ は重なる枠に △（固定）、○ は予定なしとして取り込みます。" })),
    el("p", { class: "status act-status", id: "dStatus", role: "status" }));   // 読み込み・予定を足すときの知らせ（ボタンの近くに出す）

  if (S.pending && S.pending.pid === p.id) {
    const pv = S.pending, list = el("ul", { class: "ivs" });
    const keys = Object.keys(pv.busy).sort();
    for (const iso of keys) list.append(el("li", {}, el("span", { class: "d", text: md(iso) }), el("span", { text: pv.busy[iso].map(([s, e]) => s === 0 && e === 1440 ? "終日" : `${fmtHM(s)}–${fmtHM(e)}`).join("、") })));
    box.append(el("div", { class: "preview" },
      el("p", { class: "cap", text: `読み込んだ予定（${pv.format}・${pv.count}件 → 期間内 ${keys.length}日）` }),
      keys.length ? list : el("p", { class: "note", text: "期間内の予定（✕）はありませんでした" }),
      pv.format === "あわせ暦の出力" ? importNote(p, pv) : null,
      el("div", { class: "row" },
        el("button", { type: "button", class: "primary", text: p.busy_set ? "今の予定と置き換えて共有" : "この時間を共有する", onclick: () => shareImport(p, pv.busy, pv.tent) }),
        p.busy_set ? el("button", { type: "button", text: "今の予定に足して共有", onclick: () => { const b = unionBusy(p.busy, pv.busy); shareImport(p, b, pv.tent); } }) : null,
        el("button", { type: "button", class: "small", text: "やめる", onclick: () => { S.pending = null; renderEditor(); } }))));
  }

  // 共有中の予定
  const shared = el("div", { class: "shared" });
  const keys = Object.keys(p.busy).sort();
  shared.append(el("p", { class: "cap", text: p.busy_set ? `共有中の予定（${keys.length}日）` : "まだ予定を共有していません（共有するまで ○✕ は付きません）" }));
  // 予定を1つ消した直後は「元に戻す」を出す（確認の窓は、何件も消すときに煩わしいので出さない）
  if (S.undo && S.undo.pid === p.id) shared.append(el("div", { class: "row undo", role: "status" },
    el("span", { text: `${S.undo.text} を消しました` }),
    el("button", { type: "button", class: "small", text: "元に戻す", onclick: () => { const u = S.undo; S.undo = null; saveNow({ busy: u.busy }); } })));
  if (keys.length) {
    const ul = el("ul", { class: "ivs" });
    const span = (s, e) => (s === 0 && e === 1440 ? "終日" : `${fmtHM(s)}–${fmtHM(e)}`);
    // 消すボタン（✕）は日付の左に置く（利用者の指定）
    for (const iso of keys) p.busy[iso].forEach(([s, e], i) => ul.append(el("li", {},
      el("button", { type: "button", class: "small del", "aria-label": `${md(iso)} ${span(s, e)} の予定を消す`, text: "✕", onclick: () => {
        const b = JSON.parse(JSON.stringify(p.busy)); b[iso].splice(i, 1); if (!b[iso].length) delete b[iso];
        S.undoNext = { pid: p.id, busy: JSON.parse(JSON.stringify(p.busy)), text: `${md(iso)} ${span(s, e)}` };
        saveNow({ busy: b }); } }),
      el("span", { class: "d", text: md(iso) }),
      el("span", { text: s === 0 && e === 1440 ? "終日" : `${fmtHM(s)}–${fmtHM(e)}` }))));
    shared.append(ul);
  }
  // 手で足す
  const dSel = el("select", { "aria-label": "日付" }, ...busyDays().map((iso) => el("option", { value: iso, text: md(iso) })));
  const sIn = el("input", { type: "time", value: "19:00", step: 900, "aria-label": "開始" }), eIn = el("input", { type: "time", value: "21:00", step: 900, "aria-label": "終了" });
  const allDay = el("input", { type: "checkbox", id: "dAllDay" });
  shared.append(el("div", { class: "row add-row" }, dSel, sIn, el("span", { text: "〜" }), eIn,
    el("label", { class: "chk" }, allDay, "終日"),
    el("button", { type: "button", class: "small", text: "予定を足す", onclick: () => {
      const toMin = (v) => { const [h, m] = v.split(":").map(Number); return h * 60 + m; };
      let iv;
      if (allDay.checked) iv = [0, 1440];
      else { const s = toMin(sIn.value || "0:0"); let e = toMin(eIn.value || "0:0"); if (e === 0) e = 1440; if (!(e > s)) { setStatus("終了は開始より後にしてください（日をまたぐ予定は2つに分けてください）", true, "dStatus"); return; } iv = [s, e]; }
      saveNow({ busy: unionBusy(p.busy, { [dSel.value]: [iv] }) });
    } })));
  if (p.busy_set) shared.append(el("div", { class: "row" }, el("button", { type: "button", class: "small danger", text: "共有中の予定をすべて消す", onclick: () => { if (confirm("共有中の予定をすべて消します（すべて ○ になります）")) saveNow({ busy: {} }); } })));
  box.append(shared);
  return box;
}
/* あわせ暦の出力を読んだときの案内：△ を付ける枠の数と、未入力だった枠が ○ になること */
function importNote(p, pv) {
  // 案内は箇条書きで示す（利用者の指定）
  const n = applyTentative(p, pv.busy, pv.tent).n;
  const items = [
    `✕（予定）：${Object.keys(pv.busy).length}日（上の一覧）`,
    `△ を付ける枠：${n}（固定）`,
    "予定（✕）と重なる枠は、△ ではなく ✕ になります",
  ];
  if (pv.missing) items.push(`書き出した内容に無い（未入力だった）枠：${pv.missing}（予定が無いので ○ になります）`,
    "行けるか分からない枠は、共有したあとにメニューで △ や ✕ を選んでください");
  return el("ul", { class: "note spec" }, ...items.map((t) => el("li", { text: t })));
}
function readEvents(text, name) {
  const { events, format } = detectAndParse(text, name);
  const days = busyDays(), of = (m) => events.filter((e) => (e.mark || "x") === m);
  const pv = { pid: S.current, busy: busyFromEvents(of("x"), days), format, count: events.length, tent: {}, missing: 0 };
  // あわせ暦の出力：△ の時間と、書き出した内容に無かった（未入力だった）枠の数も持つ
  if (format === "あわせ暦の出力") {
    pv.tent = busyFromEvents(of("t"), days);
    const all = busyFromEvents(events, days);
    for (const iso of S.data.event.days) for (const slot of ["d", "n"]) if (busyMark(all[iso], slot, all[nextIso(iso)]) === "o") pv.missing++;
  }
  S.pending = pv;
  renderEditor();
}
/* インポートで共有するとき：△ の時間に重なる枠に △ を付けて固定する（利用者の指定）。予定（✕）と重なる枠は ✕ のまま */
function applyTentative(p, busy, tent) {
  const marks = JSON.parse(JSON.stringify(p.marks || {})), fixed = JSON.parse(JSON.stringify(p.fixed || {}));
  let n = 0;
  for (const iso of S.data.event.days) for (const slot of ["d", "n"]) {
    if (busyMark(tent[iso], slot, tent[nextIso(iso)]) !== "x") continue;            // △ の時間と重ならない
    if (busyMark(busy[iso], slot, busy[nextIso(iso)]) === "x") continue;            // 予定と重なる（✕ が優先）
    (marks[iso] = marks[iso] || {})[slot] = "t"; (fixed[iso] = fixed[iso] || {})[slot] = true; n++;
  }
  return { marks, fixed, n };
}
function shareImport(p, busy, tent) {
  S.pending = null;
  const t = applyTentative(p, busy, tent);
  if (t.n) { p.marks = t.marks; p.fixed = t.fixed; S.unsaved = true; }   // 印も一緒に送る（saveNow はまだ保存していない変更をまとめて送る）
  saveNow({ busy });
}

/* ---------- 大雑把入力の書き出し（ほかの日程あわせの「インポート」で読める形） ---------- */
const EXPORT_HEAD = "# あわせ暦の大雑把入力";
const EXPORT_SPEC = [
  "1枠1行のテキストです（例：2026-12-01 10:00-18:00 △）",
  "ほかの日程あわせの「インポート」で、このファイルを選ぶか貼り付けると、同じ ○△✕ を使えます",
  "日付と時刻で書くので、昼・夜の時間帯がちがう日程あわせでも、時刻の重なりで合わせます",
  "インポートでは、✕＝予定あり、△＝重なる枠に △（固定）、○＝空きとして取り込みます",
  "未入力の枠は書き出しません（インポートすると ○ になります）",
  "書き出しはこのブラウザの中だけで行い、サーバーには送りません",
];
function exportText(p) {
  const hm = (m) => fmtHM(m % 1440), lines = [];
  for (const iso of [...S.data.event.days].sort()) for (const slot of ["d", "n"]) {
    const m = (p.marks[iso] || {})[slot]; if (!m) continue;
    let [s, e] = SLOT_RANGE[slot], day = iso;
    if (s >= 1440) { day = nextIso(iso); s -= 1440; e -= 1440; }   // 夜が 24時を過ぎてから始まるときは、翌日の日付で書く
    lines.push(`${day} ${hm(s)}-${hm(e)} ${MARK_TEXT[m]}`);
  }
  const one = (t) => String(t || "").replace(/\s+/g, " ").trim();
  return { count: lines.length, text: [
    `${EXPORT_HEAD}（${one(S.data.event.title)}／${one(p.name)}）`,
    ...EXPORT_SPEC.map((t) => `# ・${t}`),
    ...lines, ""].join("\n") };
}
function exportPanel(p) {
  const { text, count } = exportText(p);
  const save = el("button", { type: "button", class: "small", disabled: !count, text: "ファイルに保存（.txt）", onclick: () => {
    const url = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
    const a = el("a", { href: url, download: `awasegoyomi-${S.eid}-${p.id}.txt` });   // 名前は英数字だけ（日本語の名前は、ブラウザによって「download」に置き換わることがある。タイトルと名前はファイルの1行目にある）
    document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  } });
  const copy = el("button", { type: "button", class: "small", disabled: !count, text: "コピー", onclick: (e) => copyText(text, e.currentTarget, "コピーしました") });
  return el("div", { class: "export" },
    el("p", { class: "cap", text: "入力内容を出力" }),
    // 書き出しの仕様は箇条書きで示す（利用者の指定）。ファイルの冒頭にも同じ項目を書く
    count ? el("ul", { class: "note spec" }, el("li", { text: `今の ○△✕（${count}枠）を書き出します` }), ...EXPORT_SPEC.map((t) => el("li", { text: t })))
      : el("p", { class: "note", text: "まだ ○△✕ がありません。印を付けると、ほかの日程あわせの「インポート」で使える形で書き出せます。" }),
    el("div", { class: "row" }, save, copy));
}

async function copyText(text, btn, msg) {
  const old = btn.textContent;
  try { await navigator.clipboard.writeText(text); btn.textContent = msg; }
  catch (e) { prompt("コピーしてください", text); }
  setTimeout(() => { btn.textContent = old; }, 1800);
}

if (typeof document !== "undefined" && document.body) {
  if (S.eid) initEvent(); else initCreate();
}
