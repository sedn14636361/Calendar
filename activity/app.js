// カレンダー画面（Discord のアクティビティ）。
// 予定の名前など、外から来た文字はすべて textContent で入れる（HTML として解釈させない）。
// 判定はボット側でも必ずやり直すので、ここでの検査は使いやすさのためだけ。
import { DiscordSDK } from "./discord-sdk.js";

const CLIENT_ID = document.body.dataset.clientId;
const WD = ["日", "月", "火", "水", "木", "金", "土"];
const SLOT = 30;                                   // 週表示の1枠（分）

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
const hm = (min) => `${Math.floor(min / 60)}:${pad(min % 60)}`;
const weekStart = (d) => addDays(d, -d.getDay());   // 日曜はじまり

const S = {
  session: null, calendars: [], today: null, maxDays: 31,
  month: null,                 // その月の1日
  week: null,                  // その週の日曜
  days: new Set(),             // 選んだ日（"YYYY-MM-DD"）
  allDay: true, start: null, end: null, pendingStart: null,
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
    if (!S.allDay) await ensureEvents(S.week, addDays(S.week, 6));
  } catch (e) {
    setStatus(e.message, true);
  }
  render();
}

// ---------- 月表示 ----------
function toggleDay(key) {
  if (S.done) return;
  if (S.days.has(key)) S.days.delete(key);   // 外したときは週表示を動かさない
  else if (S.days.size >= S.maxDays) { setStatus(`一度に選べるのは${S.maxDays}日までです`, true); return; }
  else { S.days.add(key); S.week = weekStart(fromIso(key)); }   // 選んだ日の週を週表示に出す
  setStatus("");
  render();
  if (!S.allDay) refreshData();
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
    if (!S.allDay && iso(weekStart(d)) === wk) b.classList.add("week");
    b.setAttribute("aria-pressed", S.days.has(key) ? "true" : "false");
    b.setAttribute("aria-label", `${md(d)}${S.days.has(key) ? "（選択中）" : ""}`);
    b.append(el("span", "num", String(d.getDate())));
    const marks = el("span", "marks");
    const evs = S.events.get(key) || [];
    for (const e of evs.slice(0, 4)) marks.append(el("span", e.all_day ? "mark all" : "mark"));
    b.append(marks);
    b.addEventListener("click", () => toggleDay(key));
    grid.append(b);
  }
}

// ---------- 週表示（時間の枠） ----------
function tapSlot(min) {
  if (S.done) return;
  if (S.pendingStart === null) {           // 1回目：開始
    S.pendingStart = min; S.start = min; S.end = null;
  } else if (min >= S.pendingStart) {      // 2回目：終了（その枠の終わりまで）
    S.start = S.pendingStart; S.end = min + SLOT; S.pendingStart = null;
  } else {                                 // 開始より前を押したら、そこを新しい開始にする
    S.pendingStart = min; S.start = min; S.end = null;
  }
  render();
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
    const hb = el("button", "col" + (sel ? " selected" : ""));
    hb.type = "button";
    hb.append(el("div", "", WD[d.getDay()]), el("div", "", `${d.getMonth() + 1}/${d.getDate()}`));
    hb.addEventListener("click", () => toggleDay(key));
    head.append(hb);

    const col = el("div", "daycol" + (sel ? " selected" : ""));
    for (let m = 0; m < 1440; m += SLOT) {
      const s = el("button", "slot" + (m % 60 === 0 ? " hour" : ""));
      s.type = "button";
      s.setAttribute("aria-label", `${md(d)} ${hm(m)}`);
      s.addEventListener("click", () => tapSlot(m));
      col.append(s);
    }
    for (const e of S.events.get(key) || []) {
      const box = el("div", e.all_day ? "ev all" : "ev");
      box.style.top = `calc(var(--row) * ${e.start / SLOT})`;
      box.style.height = `calc(var(--row) * ${Math.max(e.end - e.start, 15) / SLOT})`;
      box.textContent = e.title || "";        // 名前は文字としてだけ入れる
      col.append(box);
    }
    if (S.pendingStart !== null) {
      const p = el("div", "pick pending" + (sel ? "" : " weak"));
      p.style.top = `calc(var(--row) * ${S.pendingStart / SLOT})`;
      col.append(p);
    } else if (S.start !== null && S.end !== null) {
      const ranges = S.end > S.start ? [[S.start, S.end]] : [[S.start, 1440], [0, S.end]];
      for (const [a, b] of ranges) {
        const p = el("div", "pick" + (sel ? "" : " weak"));
        p.style.top = `calc(var(--row) * ${a / SLOT})`;
        p.style.height = `calc(var(--row) * ${(b - a) / SLOT})`;
        col.append(p);
      }
    }
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
  for (let m = 15; m <= 1440; m += 15) es.append(new Option(m === 1440 ? "24:00" : hm(m), String(m)));
}

// ---------- 全体 ----------
function valid() {
  if (S.days.size === 0) return "日付を選んでください";
  if (!S.allDay) {
    if (S.pendingStart !== null) return "終了の枠をタップしてください";
    if (S.start === null || S.end === null) return "時間の枠をタップして選んでください";
    if (S.start === S.end % 1440 && S.end !== 1440) return "開始と終了が同じ時刻です";
  }
  if (!$("name").value.trim()) return "予定の名前を入れてください";
  return null;
}
function renderSummary() {
  const list = [...S.days].sort().map((k) => md(fromIso(k)));
  let time = "終日";
  if (!S.allDay) {
    time = S.start === null ? "時間：未選択"
      : S.end === null ? `${hm(S.start)}〜（終了をタップ）`
      : `${hm(S.start)}〜${S.end <= S.start ? "翌" : ""}${S.end === 1440 ? "24:00" : hm(S.end)}`;
  }
  const head = list.length ? `${list.length}件：${list.join("・")}` : "日付が未選択";
  const problem = valid();
  $("summary").textContent = `${head}　${time}` + (problem ? `\n${problem}` : "");
  $("add").disabled = !!problem || S.busy || S.done;
}
function render() {
  renderMonth();
  $("mode-allday").setAttribute("aria-checked", String(S.allDay));
  $("mode-time").setAttribute("aria-checked", String(!S.allDay));
  $("time-box").hidden = S.allDay;
  if (!S.allDay) renderWeek();
  $("start-sel").value = S.start === null ? "" : String(S.start);
  $("end-sel").value = S.end === null ? "" : String(S.end);
  $("overnight").hidden = S.allDay || S.start === null || S.end === null || S.end > S.start;
  renderSummary();
}

async function submit() {
  if (valid() || S.busy) return;
  S.busy = true;
  renderSummary();
  setStatus("追加しています…");
  const body = {
    session: S.session, days: [...S.days].sort(), name: $("name").value, calendar: $("calendar").value,
    start: S.allDay ? null : hm(S.start), end: S.allDay ? null : (S.end === 1440 ? "24:00" : hm(S.end)),
  };
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

function wire() {
  $("prev-month").addEventListener("click", () => { S.month = new Date(S.month.getFullYear(), S.month.getMonth() - 1, 1); refreshData(); });
  $("next-month").addEventListener("click", () => { S.month = new Date(S.month.getFullYear(), S.month.getMonth() + 1, 1); refreshData(); });
  $("prev-week").addEventListener("click", () => { S.week = addDays(S.week, -7); refreshData(); });
  $("next-week").addEventListener("click", () => { S.week = addDays(S.week, 7); refreshData(); });
  $("mode-allday").addEventListener("click", () => { S.allDay = true; render(); });
  $("mode-time").addEventListener("click", () => { S.allDay = false; refreshData(); });
  $("start-sel").addEventListener("change", (e) => {
    S.pendingStart = null; S.start = e.target.value === "" ? null : Number(e.target.value); render();
  });
  $("end-sel").addEventListener("change", (e) => {
    S.pendingStart = null; S.end = e.target.value === "" ? null : Number(e.target.value); render();
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
