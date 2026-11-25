/* あわせ暦：日程あわせのページ（/m）
   ・作るページ（/m）と、調整のページ（/m/<id>）の両方をこのファイルで動かす
   ・大雑把入力：ペン（○△✕／消す）を選んで、日の「昼」「夜」をタップして塗る
   ・詳細入力　：ICS・CSV・JSON・テキストの予定を読み、時間だけにして送る（予定の名前は送らない）。
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

function setStatus(text, err) {
  const s = $("status"); s.textContent = text || ""; s.classList.toggle("err", !!err);
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

/* ---------- 配色（サーバーが決めた CSS の変数をそのまま当てる） ---------- */
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
  const C = { month: new Date(now.getFullYear(), now.getMonth(), 1), start: null, end: null };
  C.redraw = () => {
    renderPicker($("cCal"), C, (iso, d, wd) => {
      const inr = C.start !== null && (C.end === null ? iso === C.start : C.start <= iso && iso <= C.end);
      const past = iso < todayIso();
      return el("button", { type: "button", disabled: past, class: "pday" + (inr ? " on" : "") + (iso === C.start || iso === C.end ? " edge" : "")
        + (wd === 0 ? " sun" : wd === 6 ? " sat" : ""), "aria-pressed": String(inr), "aria-label": md(iso),
        onclick: () => { tapRange(C, iso); C.redraw(); } }, String(d));
    });
    $("cSum").textContent = C.start === null ? "日程が未選択"
      : C.end === null ? `${md(C.start)}〜（終了日をタップ。同じ日をもう一度で1日だけ）`
        : `${md(C.start)}〜${md(C.end)}（${daysBetween(dateOf(C.start), dateOf(C.end)) + 1}日）`;
  };
  C.redraw();
  const getSlots = slotPicker($("cSlots"), { d: [600, 1080], n: [1260, 1440] });   // 初期値：昼 10〜18時、夜 21〜24時
  $("createForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (C.start === null || C.end === null) { setStatus("カレンダーで開始日と終了日をタップしてください", true); return; }
    const btn = $("cGo"); btn.disabled = true; setStatus("配色を作っています…（30秒ほどかかることがあります）");
    try {
      const sl = getSlots();
      if (sl.d[0] >= sl.d[1] || sl.n[0] >= sl.n[1]) { setStatus("時間帯の終わりは、始まりより後にしてください", true); btn.disabled = false; return; }
      const d = await api("/m/api/create", { title: $("cTitle").value, description: $("cDesc").value, first: C.start, last: C.end, slots: sl });
      saveKeys(d.id, { people: {}, admin: d.admin });
      location.href = `/m/${d.id}`;
    } catch (e) { setStatus(e.message, true); btn.disabled = false; }
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
function parseSimpleLines(text) {
  const timedRe = /^(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s+(\d{1,2}:\d{2})\s*[-〜~]\s*(\d{1,2}:\d{2})/;
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
    out.push({ start: st, end: en, rrule: null, allDay: false });
  }
  return out;
}
function detectAndParse(text, filename = "") {
  const name = filename.toLowerCase();
  if (name.endsWith(".ics") || text.includes("BEGIN:VCALENDAR")) return { events: parseICS(text), format: "ICS" };
  try { const j = JSON.parse(text); if (Array.isArray(j) || j.events) return { events: parseJSONEvents(text), format: "JSON" }; } catch (_) { /* JSON ではない */ }
  if (name.endsWith(".csv") || /^\s*\d{4}-\d{2}-\d{2}\s*,/.test(text)) { const ev = parseCSV(text); if (ev.length) return { events: ev, format: "CSV" }; }
  const simple = parseSimpleLines(text);
  if (simple.length) return { events: simple, format: "テキスト" };
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
const slotLabel = (slot) => `${SLOT_TEXT[slot]}（${slotHours(slot)}）`;
const isFixed = (p, iso, slot) => !!((p.fixed || {})[iso] || {})[slot];
/* 昼・夜の時間帯を選ぶ欄（1時間きざみ）。box に4つのメニューを描き、値を返す関数を返す */
function slotPicker(box, slots, onChange) {
  box.textContent = "";
  const sel = (val, lo, hi, label) => {
    const s = el("select", { "aria-label": label, onchange: onChange || null },
      ...Array.from({ length: hi - lo + 1 }, (_, i) => lo + i).map((h) => el("option", { value: h * 60, selected: h * 60 === val, text: `${h}時` })));
    return s;
  };
  const rows = {};
  for (const slot of ["d", "n"]) {
    const a = sel(slots[slot][0], 0, 23, `${SLOT_TEXT[slot]}の始まり`), b = sel(slots[slot][1], 1, 24, `${SLOT_TEXT[slot]}の終わり`);
    rows[slot] = [a, b];
    box.append(el("div", { class: "slot-row" }, el("span", { class: "lbl", text: SLOT_TEXT[slot] }), a, el("span", { text: "〜" }), b));
  }
  return () => ({ d: rows.d.map((x) => Number(x.value)), n: rows.n.map((x) => Number(x.value)) });
}
const busyMark = (ivs, slot) => (ivs || []).some(([s, e]) => s < SLOT_RANGE[slot][1] && e > SLOT_RANGE[slot][0]) ? "x" : "o";
function autoMark(p, iso, slot) { return p.mode === "detail" && p.busy_set ? busyMark(p.busy[iso], slot) : null; }
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
  eid: document.body.dataset.event, data: null, keys: null, current: null, bulkDays: new Set(), bulkSlots: new Set(["d", "n"]),
  dirty: {}, saving: false, saveTimer: null, selectedDay: null, pending: null, joining: false,
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
  $("copyUrl").addEventListener("click", () => copyText(location.origin + location.pathname, $("copyUrl"), "コピーしました"));
  $("saveImg").href = `/m/${S.eid}/image.png`;
  $("saveImg").setAttribute("download", "awasegoyomi.png");
  $("removeEvent").addEventListener("click", onRemoveEvent);
  $("editForm").addEventListener("submit", onSaveEdit);
  $("eCancel").addEventListener("click", () => { E.dirty = false; E.add.clear(); E.rm.clear(); renderEdit(); setStatus(""); });
  for (const id of ["eTitle", "eDesc"]) $(id).addEventListener("input", () => { E.dirty = true; });
  load(true);
  // ほかの人の入力を30秒ごとに取り込む。入力欄を操作している間と、保存を待っている間は描き直さない
  const idle = () => document.visibilityState === "visible" && !S.saving && !Object.keys(S.dirty).length
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

/* サーバーの答えを受け取って画面を描き直す。保存待ちの自分の印は、サーバーの古い値で上書きしない */
function accept(d) {
  const mine = me();
  const pendingMarks = mine && Object.keys(S.dirty).length ? mine.marks : null;
  S.data = d; SLOT_RANGE = d.slots || SLOT_RANGE;
  $("lgD").textContent = `上＝${slotLabel("d")}`; $("lgN").textContent = `下＝${slotLabel("n")}`;
  const ev = d.event;
  // 鍵を持っているのに、もういない参加者（削除された）は忘れる
  for (const pid of Object.keys(S.keys.people)) if (!d.people.some((p) => p.id === pid)) delete S.keys.people[pid];
  if (!S.current || !S.keys.people[S.current]) S.current = Object.keys(S.keys.people).find((pid) => d.people.some((p) => p.id === pid)) || null;
  if (pendingMarks && me()) me().marks = pendingMarks;
  document.title = `${ev.title} — あわせ暦`;
  $("title").textContent = ev.title;
  const contiguous = daysBetween(dateOf(ev.first), dateOf(ev.last)) + 1 === ev.days.length;
  $("lead").textContent = `${md(ev.first)} 〜 ${md(ev.last)}${contiguous ? "" : `（候補 ${ev.days.length}日）`}・${d.people.length}人が入力`;
  $("desc").textContent = ev.description || ""; $("desc").hidden = !ev.description;
  applyTheme(ev.theme); renderPalette(ev.palette); remember(ev);
  $("event").hidden = false;
  renderBest(); renderOverview(); renderDay(); renderPeople(); renderMine(); renderEdit();
}

/* ---------- 候補 ---------- */
function renderBest() {
  const ol = $("best"); ol.textContent = "";
  const n = S.data.people.length;
  if (!n) { $("bestNote").textContent = "まだ誰も入力していません。下の「あなたの入力」から参加してください。"; return; }
  if (!S.data.best.length) { $("bestNote").textContent = "○ か △ の付いた枠がまだありません。"; return; }
  $("bestNote").textContent = "✕（と未入力）の人が少ない順、次に ○ の多い順です。";
  for (const b of S.data.best) {
    const all = b.o === n, ok = b.x + b["-"] === 0;
    ol.append(el("li", {}, el("button", { type: "button", class: "best-item" + (all ? " all" : ok ? " ok" : ""), onclick: () => selectDay(b.day) },
      el("span", { class: "when", text: `${md(b.day)} ${SLOT_TEXT[b.slot]}` }),
      el("span", { class: "counts" }, countChip("o", b.o), countChip("t", b.t), countChip("x", b.x), b["-"] ? el("span", { class: "c none", text: `未${b["-"]}` }) : null),
      all ? el("span", { class: "badge", text: "★ 全員 ○" }) : ok ? el("span", { class: "badge soft", text: "✕なし" }) : null)));
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
    return el("button", { type: "button", class: "cell in" + (S.selectedDay === iso ? " sel" : "") + (wd === 0 ? " sun" : wd === 6 ? " sat" : ""),
      "aria-label": `${md(iso)} 昼 ○${t.d.o} △${t.d.t} ✕${t.d.x}、夜 ○${t.n.o} △${t.n.t} ✕${t.n.x}`, onclick: () => selectDay(iso) },
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
  box.append(el("div", { class: "head-row" }, el("h3", { text: md(iso) }), el("button", { type: "button", class: "small", text: "閉じる", onclick: () => { S.selectedDay = null; renderOverview(); renderDay(); } })));
  const count = (c) => `○${c.o} △${c.t} ✕${c.x}${c["-"] ? ` 未${c["-"]}` : ""}`;
  box.append(el("p", { class: "note", text: `昼 ${count(t.d)}　／　夜 ${count(t.n)}` }));
  if (!S.data.people.length) return;
  const table = el("div", { class: "dtable" });
  table.append(el("span", { class: "th", text: "名前" }), el("span", { class: "th", text: "昼" }), el("span", { class: "th", text: "夜" }));
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
    return el("button", { type: "button", disabled: past && !isCand, class: "pday" + st + (wd === 0 ? " sun" : wd === 6 ? " sat" : ""),
      "aria-label": md(iso) + (isCand ? "（候補日）" : "") + (E.add.has(iso) ? "（足す）" : "") + (E.rm.has(iso) ? "（消す）" : ""),
      onclick: () => {
        if (isCand) {
          if (!admin) { setStatus("候補日を消せるのは、作った人だけです", true); return; }
          if (E.rm.has(iso)) E.rm.delete(iso); else E.rm.add(iso);
        } else if (E.add.has(iso)) E.add.delete(iso); else E.add.add(iso);
        E.dirty = true; setStatus(""); renderEdit();
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
    if (sl.d[0] >= sl.d[1] || sl.n[0] >= sl.n[1]) { setStatus("時間帯の終わりは、始まりより後にしてください", true); return; }
    const d = await api(`/m/api/event/${S.eid}/edit`, { title: $("eTitle").value, description: $("eDesc").value, slots: sl,
      add_days: [...E.add], remove_days: [...E.rm], admin: S.keys.admin });
    E.dirty = false; E.add.clear(); E.rm.clear();
    accept(d); setStatus("保存しました");
  } catch (e) { setStatus(e.message, true); }
}

/* ---------- 参加者 ---------- */
function renderPeople() {
  const ul = $("people"); ul.textContent = "";
  const admin = !!S.keys.admin && S.data.admin;
  $("adminRow").hidden = !admin;
  if (!S.data.people.length) ul.append(el("li", { class: "note", text: "まだいません" }));
  for (const p of S.data.people) {
    const own = !!S.keys.people[p.id];
    ul.append(el("li", { class: "person" + (own ? " own" : "") },
      el("i", { class: "dot", style: { background: p.color } }), el("span", { class: "nm", text: p.name }),
      el("span", { class: "tag", text: p.mode === "detail" ? "詳細" : "大雑把" }),
      own ? el("span", { class: "tag own", text: "あなた" }) : el("span", { class: "lock", title: "ほかの人の入力（編集できません）", text: "🔒" }),
      admin && !own ? el("button", { type: "button", class: "small danger", text: "削除", onclick: () => onDeletePerson(p) }) : null,
      p.memo ? el("span", { class: "pmemo", text: p.memo }) : null));
  }
}
async function onDeletePerson(p) {
  if (!confirm(`「${p.name}」の入力を削除します。元に戻せません。`)) return;
  try { accept(await api(`/m/api/event/${S.eid}/delete`, { pid: p.id, key: S.keys.people[p.id] || null, admin: S.keys.admin })); setStatus("削除しました"); }
  catch (e) { setStatus(e.message, true); }
}
async function onRemoveEvent() {
  if (!confirm("この日程あわせを、全員の入力ごと削除します。元に戻せません。")) return;
  try { await api(`/m/api/event/${S.eid}/remove`, { admin: S.keys.admin }); store.set(keyName(S.eid), { people: {}, admin: null }); location.href = "/m"; }
  catch (e) { setStatus(e.message, true); }
}

/* ---------- あなたの入力 ---------- */
async function onJoin(ev) {
  ev.preventDefault();
  const mode = document.querySelector('input[name="jMode"]:checked').value;
  try {
    const d = await api(`/m/api/event/${S.eid}/join`, { name: $("jName").value, mode, admin: S.keys.admin });
    S.keys.people[d.joined] = d.key; saveKeys(S.eid, S.keys);
    S.current = d.joined; S.joining = false; $("jName").value = "";
    document.querySelector('input[name="jMode"][value="rough"]').checked = true;   // 「もう1人追加」のときは大雑把入力から
    accept(d); setStatus(mode === "rough" ? "参加しました。ペンを選んで、日の「昼」「夜」をタップしてください" : "参加しました。予定を読み込んでください");
  } catch (e) { setStatus(e.message, true); }
}

function renderMine() {
  const ownIds = Object.keys(S.keys.people).filter((pid) => S.data.people.some((p) => p.id === pid));
  const tabs = $("myTabs"); tabs.textContent = "";
  if (ownIds.length > 1 || (ownIds.length && S.joining)) {
    for (const pid of ownIds) {
      const p = S.data.people.find((x) => x.id === pid);
      tabs.append(el("button", { type: "button", role: "tab", "aria-selected": String(pid === S.current && !S.joining), class: "tab", text: p.name, onclick: () => { S.current = pid; S.joining = false; renderMine(); } }));
    }
  }
  const showJoin = !ownIds.length || S.joining;
  $("joinForm").hidden = !showJoin; $("jCancel").hidden = !ownIds.length;
  $("editor").hidden = showJoin;
  if (!showJoin) renderEditor();
}

function renderEditor() {
  const p = me(), box = $("editor"); if (!p) return;
  const focusId = document.activeElement && document.activeElement.id;
  box.textContent = "";
  const nameIn = el("input", { id: "eName", value: p.name, "aria-label": "名前" });
  nameIn.addEventListener("change", () => saveNow({ name: nameIn.value }));
  box.append(el("div", { class: "row name-row" }, el("label", { class: "field grow" }, el("span", { text: "名前" }), nameIn)));
  const memoIn = el("textarea", { id: "eMemo", rows: 2, placeholder: "例：21時以降なら行けます", "aria-label": "メモ" });
  memoIn.value = S.memoDraft && S.memoDraft.pid === p.id ? S.memoDraft.text : (p.memo || "");
  memoIn.addEventListener("input", () => { S.memoDraft = { pid: p.id, text: memoIn.value }; });
  memoIn.addEventListener("change", () => { S.memoDraft = null; saveNow({ memo: memoIn.value }); });
  box.append(el("label", { class: "field" }, el("span", { text: "メモ（みんなに見えます）" }), memoIn));

  const seg = el("div", { class: "seg two", role: "radiogroup", "aria-label": "入力のしかた" });
  for (const [mode, label, sub] of [["rough", "大雑把入力", "自分で ○△✕"], ["detail", "詳細入力", "予定から ○✕"]]) {
    seg.append(el("button", { type: "button", role: "radio", "aria-checked": String(p.mode === mode), class: "seg-btn" + (p.mode === mode ? " on" : ""),
      onclick: () => { if (p.mode !== mode) { p.mode = mode; saveNow({ mode }); renderEditor(); } } }, el("span", { text: label }), el("small", { text: sub })));
  }
  box.append(seg);
  if (p.mode === "detail") box.append(detailPanel(p));

  // まとめて選択：曜日 × 時間帯で枠を選び（自分のカレンダーに枠線で出る）、そのあと ○・△・✕・自動に戻す を押して付ける
  const clearName = p.mode === "detail" ? "自動に戻す" : "消す";
  const wdBtns = WD.map((w, i) => el("button", { type: "button", class: "tg" + (S.bulkDays.has(i) ? " on" : "") + (i === 0 ? " sun" : i === 6 ? " sat" : ""),
    "aria-pressed": String(S.bulkDays.has(i)), onclick: () => { toggleSet(S.bulkDays, i); renderEditor(); } }, w));
  const preset = (label, list) => el("button", { type: "button", class: "small", text: label,
    onclick: () => { S.bulkDays = new Set(list); renderEditor(); } });
  const slotBtns = ["d", "n"].map((slot) => el("button", { type: "button", class: "tg" + (S.bulkSlots.has(slot) ? " on" : ""),
    "aria-pressed": String(S.bulkSlots.has(slot)), onclick: () => { toggleSet(S.bulkSlots, slot); renderEditor(); } }, slotLabel(slot)));
  const { targets, skipped } = bulkTargets();
  const wdText = S.bulkDays.size === 7 ? "全部の曜日" : [...S.bulkDays].sort().map((i) => WD[i]).join("・");
  const bulk = el("div", { class: "bulk" },
    el("p", { class: "cap", text: "まとめて選択" }),
    el("div", { class: "bulk-row" }, el("span", { class: "lbl", text: "曜日" }), el("div", { class: "tgs" }, ...wdBtns),
      el("div", { class: "tgs" }, preset("全部", [0, 1, 2, 3, 4, 5, 6]), preset("平日", [1, 2, 3, 4, 5]), preset("土日", [0, 6]),
        preset("解除", []))),
    el("div", { class: "bulk-row" }, el("span", { class: "lbl", text: "時間帯" }), el("div", { class: "tgs" }, ...slotBtns)),
    el("p", { class: "note", text: targets.length || skipped
      ? `${wdText}の${[...S.bulkSlots].sort().map((x) => SLOT_TEXT[x]).join("と")}を選んでいます（${targets.length}枠）`
        + (skipped ? `。ほかの${skipped}枠は、手で選んだか予定があるので変えません` : "")
      : !S.bulkDays.size ? "曜日を選んでください" : "時間帯を選んでください" }),
    el("div", { class: "bulk-row" }, el("span", { class: "lbl", text: "選んだ枠に" }),
      el("div", { class: "tgs" }, ...[["o", "○"], ["t", "△"], ["x", "✕"], ["clear", clearName]].map(([k, label]) =>
        el("button", { type: "button", class: `apply pen-${k}`, disabled: !targets.length, onclick: () => paintBulk(k) }, label)))));
  box.append(el("p", { class: "note", text: p.mode === "detail"
    ? "予定から ○✕ が自動で付きます（メニューの「自動」）。行けるか分からない枠は、その枠のメニューで △ を選んでください。メニューで選んだ枠（太字・枠付き）は、自動の判定より優先され、まとめて選択でも変わりません。"
    : "日の「昼」「夜」のメニューから ○・△・✕ を選んでください。メニューで選んだ枠（太字・枠付き）は、まとめて選択でも変わりません。" }));
  box.append(bulk);

  // 自分のカレンダー
  const days = new Set(S.data.event.days);
  const cal = el("div", { class: "months mine" });
  for (const ym of monthsOf(S.data.event.days)) cal.append(monthGrid(ym, (iso, d, wd) => {
    if (!days.has(iso)) return el("div", { class: "cell out" }, el("span", { class: "dn", text: d }));
    // 枠ごとのプルダウンメニュー（利用者の指定）。詳細入力では「自動」（予定から決まった ○✕）も選べる
    const halves = ["d", "n"].map((slot) => {
      const manual = (p.marks[iso] || {})[slot], auto = autoMark(p, iso, slot), m = manual || auto;
      const fixed = isFixed(p, iso, slot);
      const picked = S.bulkSlots.has(slot) && S.bulkDays.has(wd) && !protectedSlot(p, iso, slot);   // まとめて選択で変わる枠
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
        title: fixed ? "手で選んだ枠（まとめて選択では変わりません）" : "" },
        el("small", { text: SLOT_TEXT[slot] }), el("b", { "aria-hidden": "true", text: m ? MARK_TEXT[m] : "−" }), sel);
    });
    return el("div", { class: "cell in" + (wd === 0 ? " sun" : wd === 6 ? " sat" : "") }, el("span", { class: "dn", text: d }), ...halves);
  }, (w, i) => el("button", { type: "button", class: "wd" + (i === 0 ? " sun" : i === 6 ? " sat" : "") + (S.bulkDays.has(i) ? " on" : ""),
    "aria-pressed": String(S.bulkDays.has(i)), title: "押すと「まとめて選択」の曜日に選ばれます", onclick: () => { toggleSet(S.bulkDays, i); renderEditor(); } }, w)));
  box.append(cal);
  if (p.mode === "detail" && Object.keys(p.marks).length) box.append(el("div", { class: "row" }, el("button", { type: "button", class: "small", text: "手で付けた印をすべて消す（自動に戻す）", onclick: () => { p.marks = {}; queueMarks(); renderEditor(); } })));

  box.append(el("p", { class: "status", id: "saveState", role: "status", text: S.saving ? "保存しています…" : Object.keys(S.dirty).length ? "保存を待っています…" : "" }));
  box.append(el("div", { class: "row foot-row" },
    el("button", { type: "button", class: "small", text: "別の端末で編集するリンクをコピー", onclick: (e) => copyText(`${location.origin}/m/${S.eid}#p=${p.id}.${S.keys.people[p.id]}`, e.currentTarget, "コピーしました（ほかの人に渡すと編集されます）") }),
    el("button", { type: "button", class: "small", text: "もう1人追加", onclick: () => { S.joining = true; renderMine(); } }),
    el("button", { type: "button", class: "small danger", text: "この入力を削除", onclick: () => onDeletePerson(p) })));
  if (focusId && $(focusId)) $(focusId).focus();
}

function setMark(iso, slot, mark) {          // 枠のメニューで選んだ印。○△✕ は「固定」（まとめて選択で変えない）、"clear" は印と固定を外す
  const p = me(); if (!p || !mark) return;
  const row = { ...(p.marks[iso] || {}) }, fx = { ...((p.fixed || {})[iso] || {}) };
  if (mark === "clear") { delete row[slot]; delete fx[slot]; } else { row[slot] = mark; fx[slot] = true; }
  if (Object.keys(row).length) p.marks[iso] = row; else delete p.marks[iso];
  p.fixed = p.fixed || {};
  if (Object.keys(fx).length) p.fixed[iso] = fx; else delete p.fixed[iso];
  queueMarks(); renderEditor();
}
function toggleSet(set, v) { if (set.has(v)) set.delete(v); else set.add(v); }
/* まとめて塗る対象の枠：選んだ曜日（選ばなければ全部）× 選んだ時間帯 */
/* まとめて選択の対象：選んだ曜日 × 選んだ時間帯の枠（曜日を何も選んでいなければ無し）。
   ただし、プルダウンで個別に選んだ枠と、詳細入力で予定があって ✕ になっている枠は変えない（利用者の指定） */
function protectedSlot(p, iso, slot) {
  return isFixed(p, iso, slot) || autoMark(p, iso, slot) === "x";
}
function bulkTargets() {
  const p = me(), targets = [];
  let skipped = 0;
  if (!p) return { targets, skipped };
  for (const iso of S.data.event.days) {
    if (!S.bulkDays.has(dateOf(iso).getDay())) continue;
    for (const slot of ["d", "n"]) {
      if (!S.bulkSlots.has(slot)) continue;
      if (protectedSlot(p, iso, slot)) skipped++; else targets.push([iso, slot]);
    }
  }
  return { targets, skipped };
}
function paintBulk(mark) {
  const p = me(); if (!p) return;
  for (const [iso, slot] of bulkTargets().targets) {
    const row = { ...(p.marks[iso] || {}) };
    if (mark === "clear") delete row[slot]; else row[slot] = mark;
    if (Object.keys(row).length) p.marks[iso] = row; else delete p.marks[iso];
  }
  queueMarks(); renderEditor();
}
function queueMarks() {
  S.dirty.marks = true;
  clearTimeout(S.saveTimer); S.saveTimer = setTimeout(flush, 900);
  const st = $("saveState"); if (st) st.textContent = "保存を待っています…";
}
async function flush() {
  if (S.saving) { S.saveTimer = setTimeout(flush, 400); return; }
  const p = me(); if (!p || !S.dirty.marks) return;
  S.dirty = {}; await saveNow({ marks: p.marks, fixed: p.fixed || {} });
}
async function saveNow(changes) {
  const p = me(); if (!p) return;
  S.saving = true;
  const st = $("saveState"); if (st) st.textContent = "保存しています…";
  try {
    const d = await api(`/m/api/event/${S.eid}/save`, { pid: p.id, key: S.keys.people[p.id], admin: S.keys.admin, changes });
    S.saving = false; accept(d);
    const st2 = $("saveState"); if (st2) st2.textContent = "保存しました";
  } catch (e) {
    S.saving = false; setStatus(e.message, true);
    const st2 = $("saveState"); if (st2) st2.textContent = "保存できませんでした";
  }
}

/* ---------- 詳細入力：予定の読み込みと、共有中の予定 ---------- */
function detailPanel(p) {
  const box = el("div", { class: "detail" });
  const file = el("input", { type: "file", accept: ".ics,.json,.csv,.txt,text/calendar,application/json,text/csv,text/plain", class: "visually-hidden", id: "dFile" });
  file.addEventListener("change", async () => {
    const f = file.files && file.files[0]; if (!f) return;
    try { readEvents(await f.text(), f.name); } catch (e) { setStatus(`読み込めませんでした：${e.message}`, true); }
    file.value = "";
  });
  const paste = el("textarea", { id: "dPaste", rows: 4, placeholder: "例：\n2026-12-04 19:00-20:30\n2026-12-05 終日\n（ICS の中身をそのまま貼っても読めます）" });
  paste.value = S.pasteText || "";
  paste.addEventListener("input", () => { S.pasteText = paste.value; });
  box.append(el("p", { class: "note", text: "Google カレンダーなどから書き出した .ics を選ぶか、予定を貼り付けてください。予定の名前は読み込んだ時点で捨て、時間だけを送ります。" }),
    el("div", { class: "row" }, el("label", { class: "btn", for: "dFile" }, "ファイルを選ぶ（.ics/.csv/.json/.txt）"), file),
    paste,
    el("div", { class: "row" }, el("button", { type: "button", text: "貼り付けた内容を読む", onclick: () => { try { if (paste.value.trim()) { readEvents(paste.value, ""); S.pasteText = ""; } } catch (e) { setStatus(`読み込めませんでした：${e.message}`, true); } } }),
      el("button", { type: "button", class: "small", text: "書き方", onclick: () => { const h = $("dHelp"); h.hidden = !h.hidden; } })),
    el("div", { class: "help", id: "dHelp", hidden: true },
      el("p", { text: "ICS：Google カレンダーの「設定」→ 対象のカレンダー →「エクスポート」で書き出せます（繰り返しの予定にも対応）。" }),
      el("p", { text: "テキスト：1行に1件、「2026-12-04 19:00-20:30」か「2026-12-05 終日」。" }),
      el("p", { text: "CSV：「2026-12-04,19:00,20:30」か「2026-12-05,終日」。" }),
      el("p", { text: "終日の予定は、その日の昼も夜も ✕ になります。" })));

  if (S.pending && S.pending.pid === p.id) {
    const pv = S.pending, list = el("ul", { class: "ivs" });
    const keys = Object.keys(pv.busy).sort();
    for (const iso of keys) list.append(el("li", {}, el("span", { class: "d", text: md(iso) }), el("span", { text: pv.busy[iso].map(([s, e]) => s === 0 && e === 1440 ? "終日" : `${fmtHM(s)}–${fmtHM(e)}`).join("、") })));
    box.append(el("div", { class: "preview" },
      el("p", { class: "cap", text: `読み込んだ予定（${pv.format}・${pv.count}件 → 期間内 ${keys.length}日）` }),
      keys.length ? list : el("p", { class: "note", text: "期間内の予定はありませんでした（全部 ○ になります）" }),
      el("div", { class: "row" },
        el("button", { type: "button", class: "primary", text: p.busy_set ? "今の予定と置き換えて共有" : "この時間を共有する", onclick: () => { S.pending = null; saveNow({ busy: pv.busy }); } }),
        p.busy_set ? el("button", { type: "button", text: "今の予定に足して共有", onclick: () => { S.pending = null; saveNow({ busy: unionBusy(p.busy, pv.busy) }); } }) : null,
        el("button", { type: "button", class: "small", text: "やめる", onclick: () => { S.pending = null; renderEditor(); } }))));
  }

  // 共有中の予定
  const shared = el("div", { class: "shared" });
  const keys = Object.keys(p.busy).sort();
  shared.append(el("p", { class: "cap", text: p.busy_set ? `共有中の予定（${keys.length}日）` : "まだ予定を共有していません（共有するまで ○✕ は付きません）" }));
  if (keys.length) {
    const ul = el("ul", { class: "ivs" });
    for (const iso of keys) p.busy[iso].forEach(([s, e], i) => ul.append(el("li", {}, el("span", { class: "d", text: md(iso) }),
      el("span", { text: s === 0 && e === 1440 ? "終日" : `${fmtHM(s)}–${fmtHM(e)}` }),
      el("button", { type: "button", class: "small", "aria-label": `${md(iso)} の予定を消す`, text: "✕", onclick: () => {
        const b = JSON.parse(JSON.stringify(p.busy)); b[iso].splice(i, 1); if (!b[iso].length) delete b[iso]; saveNow({ busy: b }); } }))));
    shared.append(ul);
  }
  // 手で足す
  const dSel = el("select", { "aria-label": "日付" }, ...S.data.event.days.map((iso) => el("option", { value: iso, text: md(iso) })));
  const sIn = el("input", { type: "time", value: "19:00", step: 900, "aria-label": "開始" }), eIn = el("input", { type: "time", value: "21:00", step: 900, "aria-label": "終了" });
  const allDay = el("input", { type: "checkbox", id: "dAllDay" });
  shared.append(el("div", { class: "row add-row" }, dSel, sIn, el("span", { text: "〜" }), eIn,
    el("label", { class: "chk" }, allDay, "終日"),
    el("button", { type: "button", class: "small", text: "予定を足す", onclick: () => {
      const toMin = (v) => { const [h, m] = v.split(":").map(Number); return h * 60 + m; };
      let iv;
      if (allDay.checked) iv = [0, 1440];
      else { const s = toMin(sIn.value || "0:0"); let e = toMin(eIn.value || "0:0"); if (e === 0) e = 1440; if (!(e > s)) { setStatus("終了は開始より後にしてください（日をまたぐ予定は2つに分けてください）", true); return; } iv = [s, e]; }
      saveNow({ busy: unionBusy(p.busy, { [dSel.value]: [iv] }) });
    } })));
  if (p.busy_set) shared.append(el("div", { class: "row" }, el("button", { type: "button", class: "small danger", text: "共有中の予定をすべて消す", onclick: () => { if (confirm("共有中の予定をすべて消します（すべて ○ になります）")) saveNow({ busy: {} }); } })));
  box.append(shared);
  return box;
}
function readEvents(text, name) {
  const { events, format } = detectAndParse(text, name);
  S.pending = { pid: S.current, busy: busyFromEvents(events, S.data.event.days), format, count: events.length };
  setStatus(""); renderEditor();
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
